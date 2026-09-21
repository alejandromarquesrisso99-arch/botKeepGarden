"""La incubadora: dónde se decide quién llega a nacer.

Es el reloj rápido del sistema (docs/ARCHITECTURE.md §2) y el sitio donde el
sobreajuste tiene más ganas de entrar. Casi todo lo que hay aquí son defensas
contra eso.
"""

from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Mapping, Sequence

import numpy as np

from ..config import Config
from ..evaluation.fitness import RobustScale, compute_fitness
from ..evaluation.metrics import Metrics, compute_metrics
from ..evaluation.walkforward import Fold, Split, aggregate_folds, make_split, oos_decay
from ..genome.catalog import DEFAULT_CATALOG, GeneCatalog
from ..genome.distance import genome_distance
from ..genome.schema import Genome
from ..ids import bot_id_of
from ..types import BotId
from .backtest import BacktestResult, run_backtest, run_batch

if TYPE_CHECKING:  # pragma: no cover
    import pandas as pd

    from ..storage.repositories import IncubationRepository

#: Mínimo de candidatos para que compense levantar procesos. Con tres genomas,
#: arrancar intérpretes cuesta más que correrlos.
PARALLEL_FROM = 6


class HoldoutViolation(RuntimeError):
    """Alguien ha intentado mirar el holdout fuera de ``promote``.

    Es un error y no un aviso a propósito: el holdout es el único juez honesto
    que tiene el sistema, y en cuanto se mira dos veces deja de serlo para
    siempre. Ver docs/EVOLUTION.md §Anti-sobreajuste punto 6.
    """


@dataclass(slots=True)
class FoldRun:
    """Lo que el walk-forward mide de un genoma, antes de juzgarlo.

    ``valid_returns`` son los retornos de las ventanas de validación pegados
    uno detrás de otro. Es lo que permite correlacionar dos bots sin jardín
    vivo: la fusión busca padres descorrelacionados y sin curvas no hay forma
    de saber quién se parece a quién.
    """

    fold_metrics: list[dict[str, float]] = field(default_factory=list)
    aborted_folds: int = 0
    valid_returns: "np.ndarray" = field(default_factory=lambda: np.zeros(0))


@dataclass(slots=True)
class IncubationResult:
    genome: Genome
    passed: bool
    reject_reason: str = ""
    median_sortino: float = 0.0
    median_drawdown: float = 0.0
    median_trades: float = 0.0
    oos_decay: float = 0.0
    threshold_used: float = 0.0
    fold_metrics: list[dict[str, float]] = field(default_factory=list)
    fitness_incubator: float = 0.0
    duration_ms: int = 0


@dataclass(slots=True)
class Incubator:
    """Criba de candidatos antes de entrar al jardín vivo."""

    cfg: Config
    candles: "pd.DataFrame"
    catalog: GeneCatalog = field(default_factory=lambda: DEFAULT_CATALOG)
    repo: "IncubationRepository | None" = None
    _split: Split | None = field(default=None, repr=False)

    # -- particiones ------------------------------------------------------- #

    @property
    def split(self) -> Split:
        if self._split is None:
            self._split = make_split(len(self.candles), self.cfg.incubator)
        return self._split

    @property
    def trainable(self) -> "pd.DataFrame":
        """El tramo que la criba puede mirar. Todo lo demás es holdout.

        Es la única puerta por la que ``screen`` accede a las velas, y por eso
        el corte está aquí y no repartido por el módulo: un sitio que revisar,
        no siete.
        """
        return self.candles.iloc[: self.split.holdout_start]

    def assert_no_holdout_range(self, end_index: int) -> None:
        """Comprueba que un tramo que llega hasta ``end_index`` no lo invade."""
        if int(end_index) > self.split.holdout_start:
            raise HoldoutViolation(
                f"el tramo llega hasta la vela {end_index} y el holdout empieza "
                f"en la {self.split.holdout_start}: la selección no puede mirar ahí"
            )

    # -- criba ------------------------------------------------------------- #

    def measure(
        self,
        genomes: Sequence[Genome],
        *,
        members: Mapping[BotId, Genome] | None = None,
        workers: int | None = None,
    ) -> dict[str, "FoldRun"]:
        """Corre el walk-forward y devuelve lo medido, sin juzgar a nadie.

        Es la mitad cara de ``screen`` y se usa también para volver a medir a
        los que ya están vivos cuando no hay jardín en marcha: entonces no hay
        ventana que cerrar y la evidencia sólo puede salir del histórico.
        """
        if not genomes:
            return {}
        folds = self.split.folds
        n_workers = self._workers(len(genomes), workers)
        miembros = dict(members) if members else None

        por_fold: list[list[BacktestResult]] = []
        for fold in folds:
            velas = self.candles.iloc[fold.train_start : fold.valid_end]
            self.assert_no_holdout_range(fold.valid_end)
            por_fold.append(
                run_batch(
                    list(genomes), velas, self.cfg, workers=n_workers, members=miembros
                )
            )
        return {
            g.id: self._fold_run(g, [corridas[i] for corridas in por_fold], folds)
            for i, g in enumerate(genomes)
        }

    def screen(
        self,
        candidates: Sequence[Genome],
        *,
        alive_genomes: Sequence[Genome] = (),
        generation: int = 0,
        members: Mapping[BotId, Genome] | None = None,
        reference: Mapping[str, RobustScale] | None = None,
        workers: int | None = None,
    ) -> list[IncubationResult]:
        """Evalúa una cosecha entera y decide quién nace.

        1. Corre walk-forward (``evaluation.walkforward``) sobre cada candidato,
           en paralelo.
        2. Toma la **mediana** de los folds, no la media: la mediana castiga a
           los bots que dependen de un único periodo afortunado.
        3. Calcula ``oos_decay = 1 - sortino_validation / max(sortino_train, ε)``.
        4. Infla el umbral con el número de candidatos de la cosecha:
           ``min_sortino * (1 + multiplicity_penalty * log10(max(1, n)))``.
           Probar 10.000 genomas y quedarse con el mejor no es selección, es
           dragado de datos; esto lo compensa explícitamente.
        5. Rechaza clones: distancia mínima a cualquier genoma vivo por debajo
           de ``speciation.clone_threshold``.
        6. Devuelve TODOS los resultados, también los rechazados, para que
           queden en ``incubation_runs``: saber qué no funciona es la mitad del
           informe del jardinero.

        **El holdout no se toca aquí.** Sólo en ``promote``.

        Cada pliegue se corre por separado y con capital fresco. Arrastrar el
        capital de un pliegue al siguiente haría que un mal año de 2019 dejara
        al bot abortado por el freno de drawdown y vaciara los cuatro pliegues
        restantes, que es exactamente lo que no se quiere medir.
        """
        if not candidates:
            return []

        inc = self.cfg.incubator
        folds = self.split.folds
        umbral = inc.min_sortino * (
            1.0 + inc.multiplicity_penalty * math.log10(max(1, len(candidates)))
        )

        # Los clones se descartan antes de gastar un backtest en ellos.
        pendientes: list[Genome] = []
        resultados: dict[str, IncubationResult] = {}
        for genoma in candidates:
            motivo = self._clone_reason(genoma, alive_genomes)
            if motivo:
                resultados[genoma.id] = IncubationResult(
                    genome=genoma, passed=False, reject_reason=motivo,
                    threshold_used=umbral,
                )
            else:
                pendientes.append(genoma)

        if pendientes:
            arranque = time.perf_counter()
            medidos = self.measure(pendientes, members=members, workers=workers)
            gastado = int((time.perf_counter() - arranque) * 1000 / max(1, len(pendientes)))
            for genoma in pendientes:
                resultados[genoma.id] = self._judge(
                    genoma, medidos[genoma.id], folds, umbral, duration_ms=gastado
                )

        salida = [resultados[g.id] for g in candidates]
        self._score(salida, reference)
        if self.repo is not None:
            self.repo.record_many(generation, salida, n_candidates=len(candidates))
        return salida

    def _workers(self, n_candidatos: int, workers: int | None) -> int:
        if workers is not None:
            return workers
        if n_candidatos < PARALLEL_FROM:
            return 1
        configurado = int(self.cfg.incubator.workers)
        if configurado > 0:
            return configurado
        return max(1, (os.cpu_count() or 2) - 1)

    def _clone_reason(self, genome: Genome, alive: Sequence[Genome]) -> str:
        if not alive:
            return ""
        weights = self.cfg.speciation.distance_weights
        umbral = self.cfg.speciation.clone_threshold
        for vivo in alive:
            d = genome_distance(genome, vivo, weights, self.catalog)
            if d < umbral:
                return f"clon de {vivo.id} (distancia {d:.3f} < {umbral})"
        return ""

    def _fold_run(
        self, genome: Genome, corridas: Sequence[BacktestResult], folds: Sequence[Fold]
    ) -> "FoldRun":
        """Traduce las corridas de un genoma a métricas por pliegue."""
        timeframe = genome.market.timeframe
        por_fold: list[dict[str, float]] = []
        abortados = 0
        retornos: list[np.ndarray] = []

        for fold, corrida in zip(folds, corridas):
            if corrida.aborted_reason:
                abortados += 1
            equity = np.asarray(corrida.equity_curve, dtype="float64")
            ts = np.asarray(corrida.equity_ts, dtype="int64")
            # Los índices del pliegue son globales; la corrida empieza en
            # ``train_start``, así que hay que trasladarlos.
            origen = fold.train_start
            entreno = _window_metrics(
                equity, ts, corrida,
                fold.train_start - origen, fold.train_end - origen, timeframe,
            )
            valida = _window_metrics(
                equity, ts, corrida,
                fold.valid_start - origen, fold.valid_end - origen, timeframe,
            )
            retornos.append(
                _window_returns(equity, fold.valid_start - origen, fold.valid_end - origen)
            )
            por_fold.append(
                {
                    "fold": float(fold.index),
                    "train_sortino": entreno.sortino,
                    "train_trades": float(entreno.n_trades),
                    "sortino": valida.sortino,
                    "max_drawdown": valida.max_drawdown,
                    "n_trades": float(valida.n_trades),
                    "profit_factor": valida.profit_factor,
                    "calmar": valida.calmar,
                    "ulcer_index": valida.ulcer_index,
                    "consistency": valida.consistency,
                    "fee_drag": valida.fee_drag,
                    "turnover": valida.turnover,
                    "total_return": valida.total_return,
                }
            )

        return FoldRun(
            fold_metrics=por_fold,
            aborted_folds=abortados,
            valid_returns=np.concatenate(retornos) if retornos else np.zeros(0),
        )

    def _judge(
        self,
        genome: Genome,
        run: "FoldRun",
        folds: Sequence[Fold],
        umbral: float,
        *,
        duration_ms: int,
    ) -> IncubationResult:
        """Aplica los cuatro filtros sobre lo medido."""
        inc = self.cfg.incubator
        por_fold = run.fold_metrics

        mediana_sortino = aggregate_folds(por_fold, "sortino")
        mediana_dd = aggregate_folds(por_fold, "max_drawdown")
        mediana_trades = aggregate_folds(por_fold, "n_trades")
        decay = oos_decay(
            aggregate_folds(por_fold, "train_sortino"), mediana_sortino
        )

        abortados = run.aborted_folds
        motivo = ""
        if abortados > len(folds) // 2:
            motivo = (
                f"el freno de drawdown salta en {abortados} de {len(folds)} pliegues"
            )
        elif mediana_trades < inc.min_trades_per_fold:
            motivo = (
                f"opera poco: mediana de {mediana_trades:.0f} operaciones por "
                f"pliegue, mínimo {inc.min_trades_per_fold:.0f}"
            )
        elif mediana_sortino < umbral:
            motivo = f"sortino {mediana_sortino:.2f} por debajo del umbral {umbral:.2f}"
        elif mediana_dd > inc.max_drawdown:
            motivo = f"drawdown {mediana_dd:.2%} por encima de {inc.max_drawdown:.2%}"
        elif decay > inc.max_oos_decay:
            motivo = f"se degrada fuera de muestra: {decay:.2f} > {inc.max_oos_decay}"

        return IncubationResult(
            genome=genome,
            passed=not motivo,
            reject_reason=motivo,
            median_sortino=mediana_sortino,
            median_drawdown=mediana_dd,
            median_trades=mediana_trades,
            oos_decay=decay,
            threshold_used=umbral,
            fold_metrics=por_fold,
            duration_ms=duration_ms,
        )

    def _score(
        self,
        resultados: Sequence[IncubationResult],
        reference: Mapping[str, RobustScale] | None,
    ) -> None:
        """Fitness de la cosecha, para poder ordenar a los que sí pasan."""
        metricas = {
            r.genome.id: fold_metrics_to_metrics(r.fold_metrics) for r in resultados
        }
        fitness = compute_fitness(
            metricas,
            self.cfg.fitness,
            complexities={r.genome.id: r.genome.complexity() for r in resultados},
            reference=reference,
        )
        for r in resultados:
            desglose = fitness.get(r.genome.id)
            if desglose is not None and desglose.is_defined:
                r.fitness_incubator = desglose.total

    # -- ascenso ----------------------------------------------------------- #

    def promote(
        self,
        genome: Genome,
        generation: int,
        *,
        reference_sortino: float | None = None,
        members: Mapping[BotId, Genome] | None = None,
    ) -> IncubationResult:
        """Evalúa el holdout UNA sola vez y decide el ascenso definitivo.

        Este es el único punto del sistema autorizado a leer el tramo de
        holdout. Se escribe una fila en ``holdout_results`` con clave primaria
        ``bot_id``: si alguien intenta escribir una segunda, la base lo impide y
        eso es exactamente lo que se quiere, porque significaría que alguien
        está mirando el holdout más de una vez.

        El bot corre sobre toda la serie y sólo se **miden** las velas del
        holdout: sin el histórico previo sus indicadores llegarían fríos al
        tramo que decide su vida, y estaríamos juzgando el calentamiento.

        Si el bot se degrada en holdout, no asciende; quien llama penaliza a su
        familia de ideas en la siguiente cosecha.
        """
        inc = self.cfg.incubator
        arranque = time.perf_counter()
        corrida = run_backtest(
            genome, self.candles, self.cfg, members=dict(members) if members else None
        )
        equity = np.asarray(corrida.equity_curve, dtype="float64")
        ts = np.asarray(corrida.equity_ts, dtype="int64")
        split = self.split

        holdout = _window_metrics(
            equity, ts, corrida, split.holdout_start, split.holdout_end,
            genome.market.timeframe,
        )
        referencia = (
            float(reference_sortino) if reference_sortino is not None else inc.min_sortino
        )
        decay = oos_decay(referencia, holdout.sortino)

        motivo = ""
        if holdout.n_trades < 1:
            motivo = "no ha operado ni una vez en el holdout"
        elif decay > inc.max_oos_decay:
            motivo = (
                f"se degrada en holdout: sortino {holdout.sortino:.2f} frente a "
                f"{referencia:.2f} esperados (decay {decay:.2f})"
            )
        elif holdout.max_drawdown > inc.max_drawdown:
            motivo = (
                f"drawdown de {holdout.max_drawdown:.2%} en holdout, por encima "
                f"de {inc.max_drawdown:.2%}"
            )

        resultado = IncubationResult(
            genome=genome,
            passed=not motivo,
            reject_reason=motivo,
            median_sortino=holdout.sortino,
            median_drawdown=holdout.max_drawdown,
            median_trades=float(holdout.n_trades),
            oos_decay=decay,
            threshold_used=referencia,
            fold_metrics=[{"scope": 0.0, **_metrics_row(holdout)}],
            duration_ms=int((time.perf_counter() - arranque) * 1000),
        )

        if self.repo is not None:
            self.repo.record_holdout(
                bot_id_of(genome.id),
                generation=generation,
                evaluated_ts=int(ts[-1]) if ts.size else 0,
                sortino=holdout.sortino,
                max_drawdown=holdout.max_drawdown,
                n_trades=holdout.n_trades,
                decay=decay,
                promoted=resultado.passed,
            )
        return resultado


# --------------------------------------------------------------------------- #
# Medir una ventana                                                            #
# --------------------------------------------------------------------------- #


def _window_metrics(
    equity: np.ndarray,
    ts: np.ndarray,
    corrida: BacktestResult,
    inicio: int,
    fin: int,
    timeframe: str,
) -> Metrics:
    """Métricas de un tramo de una corrida ya hecha.

    Las operaciones se asignan al tramo por su vela de **cierre**: una
    operación pertenece a la ventana en la que se resolvió, porque es ahí donde
    su resultado se conoce y donde el capital se mueve.
    """
    if equity.size == 0 or fin <= inicio:
        return Metrics()
    inicio = max(0, min(inicio, equity.size))
    fin = max(inicio, min(fin, equity.size))
    tramo = equity[inicio:fin]
    if tramo.size < 2:
        return Metrics()

    desde = int(ts[inicio]) if ts.size > inicio else 0
    hasta = int(ts[fin - 1]) if ts.size >= fin else 0
    trades = [
        t
        for t in corrida.trades
        if desde <= int(t.get("exit_ts", t.get("entry_ts", 0))) <= hasta
    ]
    comisiones = float(sum(float(t.get("fees", 0.0)) for t in trades))
    return compute_metrics(tramo, trades, timeframe, total_fees=comisiones)  # type: ignore[arg-type]


def _window_returns(equity: np.ndarray, inicio: int, fin: int) -> np.ndarray:
    """Retornos vela a vela de un tramo, para poder correlacionar dos bots."""
    from ..evaluation.metrics import simple_returns

    if equity.size == 0 or fin <= inicio:
        return np.zeros(0, dtype="float64")
    inicio = max(0, min(inicio, equity.size))
    fin = max(inicio, min(fin, equity.size))
    return simple_returns(equity[inicio:fin])


def _metrics_row(m: Metrics) -> dict[str, float]:
    return {
        "sortino": m.sortino,
        "max_drawdown": m.max_drawdown,
        "n_trades": float(m.n_trades),
        "profit_factor": m.profit_factor,
        "calmar": m.calmar,
        "ulcer_index": m.ulcer_index,
        "consistency": m.consistency,
        "fee_drag": m.fee_drag,
        "turnover": m.turnover,
        "total_return": m.total_return,
    }


def fold_metrics_to_metrics(folds: Sequence[dict[str, float]]) -> Metrics:
    """Reconstruye un ``Metrics`` con la mediana de los pliegues.

    El fitness compara poblaciones y necesita el mismo objeto que usa el jardín
    vivo; la mediana entre pliegues es la versión honesta de cada métrica.
    """
    return Metrics(
        sortino=aggregate_folds(folds, "sortino"),
        calmar=aggregate_folds(folds, "calmar"),
        ulcer_index=aggregate_folds(folds, "ulcer_index"),
        profit_factor=aggregate_folds(folds, "profit_factor"),
        consistency=aggregate_folds(folds, "consistency"),
        max_drawdown=aggregate_folds(folds, "max_drawdown"),
        fee_drag=aggregate_folds(folds, "fee_drag"),
        turnover=aggregate_folds(folds, "turnover"),
        total_return=aggregate_folds(folds, "total_return"),
        n_trades=int(aggregate_folds(folds, "n_trades")),
    )


@dataclass(slots=True)
class MultiSymbolIncubator:
    """Una incubadora por símbolo, con la interfaz de una sola.

    Un jardín multi-símbolo no puede cribar un genoma de ETH contra las velas
    de BTC. Cada candidato va a la incubadora de su mercado y los resultados se
    devuelven juntos, así que ni la población ni el runner tienen que saber que
    hay más de un símbolo.

    Los vivos con los que se comparan los clones son también los de ese
    símbolo: dos genomas idénticos sobre mercados distintos no son clones, son
    la misma idea puesta a prueba en dos sitios.
    """

    incubators: dict[str, Incubator]
    primary: str = ""

    @property
    def split(self) -> Split:
        """El corte del símbolo primario, que es el que se enseña."""
        clave = self.primary or next(iter(self.incubators))
        return self.incubators[clave].split

    def _for(self, genome: Genome) -> "Incubator | None":
        return self.incubators.get(str(genome.market.symbol))

    def _group(self, genomes: Sequence[Genome]) -> dict[str, list[Genome]]:
        grupos: dict[str, list[Genome]] = {}
        for g in genomes:
            if self._for(g) is not None:
                grupos.setdefault(str(g.market.symbol), []).append(g)
        return grupos

    def measure(
        self,
        genomes: Sequence[Genome],
        *,
        members: Mapping[BotId, Genome] | None = None,
        workers: int | None = None,
    ) -> dict[str, "FoldRun"]:
        salida: dict[str, FoldRun] = {}
        for simbolo, grupo in self._group(genomes).items():
            salida.update(
                self.incubators[simbolo].measure(grupo, members=members, workers=workers)
            )
        return salida

    def screen(
        self,
        candidates: Sequence[Genome],
        *,
        alive_genomes: Sequence[Genome] = (),
        generation: int = 0,
        members: Mapping[BotId, Genome] | None = None,
        reference: Mapping[str, RobustScale] | None = None,
        workers: int | None = None,
    ) -> list[IncubationResult]:
        resultados: list[IncubationResult] = []
        for simbolo, grupo in self._group(candidates).items():
            vivos = [g for g in alive_genomes if str(g.market.symbol) == simbolo]
            resultados.extend(
                self.incubators[simbolo].screen(
                    grupo, alive_genomes=vivos, generation=generation,
                    members=members, reference=reference, workers=workers,
                )
            )
        return resultados


__all__ = (
    "PARALLEL_FROM",
    "FoldRun",
    "HoldoutViolation",
    "IncubationResult",
    "Incubator",
    "MultiSymbolIncubator",
    "fold_metrics_to_metrics",
)
