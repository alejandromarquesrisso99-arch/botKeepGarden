"""Informe de robustez: ¿el bot es bueno, o tuvo suerte?

Un bot que gana en el backtest no dice gran cosa. Lo que dice algo es un bot
que **sigue ganando** cuando se le mueve el suelo:

* **Fricción**: comisiones y slippage al doble y al triple. Si un bot vive de
  un margen menor que su propio coste de transacción, sólo existe en la hoja de
  cálculo.
* **Desplazamiento del inicio**: empezar unas velas más tarde. Una estrategia
  que depende de haber entrado justo ese martes no es una estrategia.
* **Monte Carlo sobre el orden de las operaciones**: las mismas operaciones,
  barajadas. El dinero final **no cambia** —sumar es conmutativo— y por eso lo
  que se mira es el drawdown, que cambia muchísimo: tres pérdidas seguidas
  hunden una curva que las mismas tres pérdidas repartidas ni tocan. Y el
  drawdown es lo que hace que alguien apague el bot.
* **Bootstrap**: las operaciones remuestreadas con reemplazo, que sí da una
  distribución del resultado final y responde a "¿y si le hubieran tocado otras
  operaciones parecidas?".

Nada de esto toca el holdout: se mide sobre el mismo tramo entrenable que ve la
incubadora.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import numpy as np

from ..config import Config
from ..engine.backtest import run_backtest
from ..genome.schema import Genome
from .metrics import compute_metrics, drawdown_series

if TYPE_CHECKING:  # pragma: no cover
    import pandas as pd

#: Multiplicadores de fricción del informe. El 1.0 es la referencia.
FRICTION_FACTORS: tuple[float, ...] = (1.0, 2.0, 3.0)

#: Desplazamientos del inicio, en velas.
START_SHIFTS: tuple[int, ...] = (0, 24, 72, 168)

#: Barajadas del Monte Carlo. Con mil ya no cambia la tercera cifra.
MONTE_CARLO_RUNS = 1000


@dataclass(slots=True)
class Scenario:
    """Una corrida del informe con su ajuste y lo que salió."""

    label: str
    friction_factor: float = 1.0
    start_shift: int = 0
    n_trades: int = 0
    total_return: float = 0.0
    sortino: float = 0.0
    max_drawdown: float = 0.0
    profit_factor: float = 0.0
    fee_drag: float = 0.0
    final_equity: float = 0.0
    aborted: str = ""


@dataclass(slots=True)
class MonteCarlo:
    """Qué le pasa al bot si le cambian el orden —o el reparto— de la suerte.

    ``final_return`` es uno solo y no una distribución: barajar el orden de las
    operaciones no cambia cuánto dinero se gana, sólo por dónde pasa la curva.
    La distribución que sí importa es la del drawdown.
    """

    runs: int = 0
    final_return: float = 0.0
    median_drawdown: float = 0.0
    p95_drawdown: float = 0.0
    worst_drawdown: float = 0.0
    observed_drawdown: float = 0.0
    #: Percentil en el que cae el drawdown que de verdad ocurrió. Si el real
    #: está en la cola buena, el bot tuvo suerte con el orden.
    observed_percentile: float = 0.0
    #: Bootstrap: operaciones remuestreadas con reemplazo. Esto sí da una
    #: distribución del resultado final.
    bootstrap_p05_return: float = 0.0
    bootstrap_median_return: float = 0.0
    bootstrap_p95_return: float = 0.0
    prob_loss: float = 0.0


@dataclass(slots=True)
class RobustnessReport:
    """Lo que aguanta un bot. ``survives`` es el titular."""

    bot_id: str
    genome_id: str
    symbol: str
    baseline: Scenario | None = None
    scenarios: list[Scenario] = field(default_factory=list)
    monte_carlo: MonteCarlo | None = None
    survives_double_friction: bool = False
    survives_all_shifts: bool = False

    @property
    def survives(self) -> bool:
        """El listón del hito 8: aguantar el doble de fricción."""
        return self.survives_double_friction

    def to_dict(self) -> dict[str, Any]:
        from dataclasses import asdict

        return asdict(self)


def _scaled_frictions(cfg: Config, factor: float) -> Config:
    """La misma config con la fricción multiplicada."""
    from dataclasses import replace

    f = cfg.frictions
    return replace(
        cfg,
        frictions=replace(
            f,
            taker_fee_bps=f.taker_fee_bps * factor,
            maker_fee_bps=f.maker_fee_bps * factor,
            slippage_bps=f.slippage_bps * factor,
            slippage_atr_frac=f.slippage_atr_frac * factor,
        ),
    )


def _run(
    genome: Genome,
    candles: pd.DataFrame,
    cfg: Config,
    *,
    label: str,
    factor: float,
    shift: int,
    members: dict[str, Genome] | None = None,
) -> Scenario:
    resultado = run_backtest(
        genome, candles.iloc[shift:], _scaled_frictions(cfg, factor), members=members
    )
    metricas = compute_metrics(
        resultado.equity_curve if resultado.equity_curve is not None else np.zeros(0),
        resultado.trades,
        genome.market.timeframe,
        total_fees=resultado.total_fees,
    )
    return Scenario(
        label=label,
        friction_factor=factor,
        start_shift=shift,
        n_trades=metricas.n_trades,
        total_return=metricas.total_return,
        sortino=metricas.sortino,
        max_drawdown=metricas.max_drawdown,
        profit_factor=metricas.profit_factor,
        fee_drag=metricas.fee_drag,
        final_equity=resultado.final_equity,
        aborted=resultado.aborted_reason,
    )


def monte_carlo_order(
    trades: list[dict[str, Any]],
    initial_capital: float,
    *,
    runs: int = MONTE_CARLO_RUNS,
    rng: random.Random | None = None,
) -> MonteCarlo:
    """Baraja el orden de las operaciones y mira qué le pasa al drawdown.

    La curva se construye sumando el PnL neto de cada operación, que es como se
    mueve de verdad la cartera. Componer el ``return`` de cada operación sería
    otra cosa: ese porcentaje es sobre el nocional de la posición, no sobre el
    capital, y tratarlo como si fuera el retorno de la cartera multiplica los
    drawdowns por tres o por cuatro.

    Sumar es conmutativo, así que el dinero final es el mismo en todas las
    barajadas. Eso no es un defecto del método: es el resultado. Lo que sí
    cambia —y mucho— es el camino, y el camino es lo que hace que alguien
    apague un bot antes de que llegue a su buen trimestre.
    """
    salida = MonteCarlo(runs=0)
    pnls = [float(t.get("pnl", 0.0)) for t in trades if t.get("pnl") is not None]
    if len(pnls) < 3:
        return salida

    azar = rng or random.Random(20260914)
    capital = max(1e-9, float(initial_capital))

    def curva(orden: Sequence[float]) -> np.ndarray:
        return capital + np.concatenate(([0.0], np.cumsum(np.asarray(orden, dtype="float64"))))

    real = curva(pnls)
    salida.observed_drawdown = float(drawdown_series(real).max())
    salida.final_return = float(real[-1] / capital - 1.0)

    drawdowns: list[float] = []
    barajado = list(pnls)
    for _ in range(runs):
        azar.shuffle(barajado)
        drawdowns.append(float(drawdown_series(curva(barajado)).max()))

    dd = np.asarray(drawdowns)
    salida.runs = runs
    salida.median_drawdown = float(np.median(dd))
    salida.p95_drawdown = float(np.percentile(dd, 95))
    salida.worst_drawdown = float(dd.max())
    salida.observed_percentile = float((dd <= salida.observed_drawdown).mean())

    # Bootstrap: otras operaciones igual de verosímiles, no las mismas en otro
    # orden. Es lo que da una distribución del resultado.
    finales = []
    n = len(pnls)
    for _ in range(runs):
        muestra = [pnls[azar.randrange(n)] for _ in range(n)]
        finales.append(float(sum(muestra)) / capital)
    finales_np = np.asarray(finales)
    salida.bootstrap_p05_return = float(np.percentile(finales_np, 5))
    salida.bootstrap_median_return = float(np.median(finales_np))
    salida.bootstrap_p95_return = float(np.percentile(finales_np, 95))
    salida.prob_loss = float((finales_np < 0).mean())
    return salida


def analyse(
    genome: Genome,
    candles: pd.DataFrame,
    cfg: Config,
    *,
    bot_id: str = "",
    members: dict[str, Genome] | None = None,
    runs: int = MONTE_CARLO_RUNS,
    rng: random.Random | None = None,
) -> RobustnessReport:
    """El informe completo de un bot sobre un tramo de velas."""
    informe = RobustnessReport(
        bot_id=bot_id or genome.id,
        genome_id=genome.id,
        symbol=str(genome.market.symbol),
    )

    base = _run(genome, candles, cfg, label="referencia", factor=1.0, shift=0, members=members)
    informe.baseline = base
    informe.scenarios.append(base)

    for factor in FRICTION_FACTORS:
        if factor == 1.0:
            continue
        informe.scenarios.append(
            _run(
                genome, candles, cfg, label=f"fricción ×{factor:g}",
                factor=factor, shift=0, members=members,
            )
        )
    for shift in START_SHIFTS:
        if shift == 0:
            continue
        informe.scenarios.append(
            _run(
                genome, candles, cfg, label=f"empieza {shift} velas después",
                factor=1.0, shift=shift, members=members,
            )
        )

    doble = next((e for e in informe.scenarios if e.friction_factor == 2.0), None)
    informe.survives_double_friction = bool(
        doble is not None
        and doble.n_trades > 0
        and doble.total_return > 0
        and not doble.aborted
    )
    desplazados = [e for e in informe.scenarios if e.start_shift > 0]
    informe.survives_all_shifts = bool(
        desplazados and all(e.total_return > 0 and not e.aborted for e in desplazados)
    )

    resultado = run_backtest(genome, candles, cfg, members=members)
    informe.monte_carlo = monte_carlo_order(
        resultado.trades, cfg.garden.initial_capital_per_bot, runs=runs, rng=rng
    )
    return informe


def format_report(informes: list[RobustnessReport]) -> str:
    """El informe en Markdown, listo para pegarlo en el diario."""

    def num(v: float, d: int = 2) -> str:
        return "—" if v is None or not math.isfinite(v) else f"{v:.{d}f}"

    def pct(v: float, d: int = 1) -> str:
        return "—" if v is None or not math.isfinite(v) else f"{100 * v:.{d}f} %"

    lineas = ["# Informe de robustez", ""]
    aguantan = sum(1 for i in informes if i.survives)
    lineas += [
        f"{aguantan} de {len(informes)} bots siguen en positivo con el **doble de "
        f"fricción**. Es el listón: un bot que vive de un margen menor que su "
        f"propio coste de transacción sólo existe en la hoja de cálculo.",
        "",
    ]

    for informe in informes:
        lineas += [
            f"## {informe.bot_id} · {informe.symbol}",
            "",
            "| Escenario | Ops | Retorno | Sortino | Drawdown | Profit factor | Comisiones/beneficio |",
            "|---|---|---|---|---|---|---|",
        ]
        for e in informe.scenarios:
            lineas.append(
                f"| {e.label} | {e.n_trades} | {pct(e.total_return)} | {num(e.sortino)} | "
                f"{pct(e.max_drawdown)} | {num(e.profit_factor)} | {pct(e.fee_drag)} |"
                + (f" _{e.aborted}_" if e.aborted else "")
            )
        mc = informe.monte_carlo
        if mc and mc.runs:
            lineas += [
                "",
                f"**Monte Carlo** · {mc.runs} barajadas del orden de las operaciones. "
                f"El dinero final es el mismo en todas ({pct(mc.final_return)}): "
                "sumar es conmutativo. Lo que cambia es el camino.",
                "",
                f"- Drawdown mediano {pct(mc.median_drawdown)} · "
                f"p95 {pct(mc.p95_drawdown)} · peor {pct(mc.worst_drawdown)}",
                f"- El drawdown que de verdad tuvo ({pct(mc.observed_drawdown)}) cae en el "
                f"percentil {pct(mc.observed_percentile, 0)}: "
                + (
                    "tuvo suerte con el orden que le tocó."
                    if mc.observed_percentile < 0.25
                    else "el orden que le tocó no le regaló nada."
                ),
                f"- Bootstrap (otras operaciones igual de verosímiles): resultado mediano "
                f"{pct(mc.bootstrap_median_return)}, p05 {pct(mc.bootstrap_p05_return)}, "
                f"p95 {pct(mc.bootstrap_p95_return)} · probabilidad de acabar en "
                f"pérdidas {pct(mc.prob_loss)}",
            ]
        else:
            lineas += ["", "_Muy pocas operaciones para un Monte Carlo con sentido._"]
        lineas += [
            "",
            "**Veredicto**: "
            + ("aguanta el doble de fricción" if informe.survives_double_friction
               else "**no** aguanta el doble de fricción")
            + " · "
            + ("aguanta empezar más tarde" if informe.survives_all_shifts
               else "**depende de cuándo empieza**"),
            "",
        ]
    return "\n".join(lineas)


__all__ = (
    "FRICTION_FACTORS",
    "MONTE_CARLO_RUNS",
    "START_SHIFTS",
    "MonteCarlo",
    "RobustnessReport",
    "Scenario",
    "analyse",
    "format_report",
    "monte_carlo_order",
)
