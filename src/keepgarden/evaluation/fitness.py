"""Fitness: convertir métricas en un número comparable.

La fórmula completa está en docs/METRICS.md §Fitness. El principio: el fitness
no es "cuánto gana". Un bot que gana mucho con drawdowns del 60 %, 11
operaciones y correlación 0.98 con el resto del jardín es peor para el
ecosistema que uno que gana la mitad de forma estable y descorrelacionada.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from ..config import FitnessConfig
from .metrics import Metrics

#: Constante que hace el MAD comparable con una desviación típica bajo
#: normalidad. Sin ella, un umbral como "z(turnover) > 1.5" significaría cosas
#: distintas según la forma de la distribución.
MAD_TO_SIGMA = 1.4826

#: Métricas que entran en la base, y de dónde salen. El signo dice si la
#: métrica suma tal cual o al revés: el ulcer index es lo único que se quiere
#: bajo, y entra negado.
BASE_METRICS: tuple[tuple[str, str, float], ...] = (
    ("sortino", "sortino", +1.0),
    ("calmar", "calmar", +1.0),
    ("ulcer", "ulcer_index", -1.0),
    ("profit_factor", "profit_factor", +1.0),
    ("consistency", "consistency", +1.0),
)

#: Complejidad a partir de la cual empieza a pagarse la falta de parsimonia:
#: dos features y dos hojas es el bot más simple que expresa una idea.
COMPLEXITY_FREE = 4

#: Umbral, en z, a partir del cual el turnover se considera sobreoperación.
TURNOVER_Z_THRESHOLD = 1.5


@dataclass(frozen=True, slots=True)
class RobustScale:
    """Centro y escala de una métrica: mediana y MAD ya convertido a sigma."""

    median: float
    sigma: float

    def z(self, value: float, clip: float = 3.0) -> float:
        if self.sigma <= 0.0:
            return 0.0
        return float(min(max((float(value) - self.median) / self.sigma, -clip), clip))


@dataclass(slots=True)
class FitnessBreakdown:
    """Fitness con sus componentes, para poder explicarlo en el dashboard.

    Que el fitness sea explicable no es cosmético: es lo que permite al
    jardinero diagnosticar por qué un bot está donde está.
    """

    bot_id: str
    base: float = 0.0
    components: dict[str, float] = field(default_factory=dict)
    penalties: dict[str, float] = field(default_factory=dict)
    bonuses: dict[str, float] = field(default_factory=dict)
    total: float = 0.0
    undefined_reason: str = ""

    @property
    def is_defined(self) -> bool:
        return not self.undefined_reason


# --------------------------------------------------------------------------- #
# Normalización                                                                #
# --------------------------------------------------------------------------- #


def _finite(value: float) -> float:
    """Un número utilizable. NaN e infinitos entran como 0, no como veneno."""
    x = float(value)
    if not math.isfinite(x):
        return 0.0
    return x


def robust_scale(values: Sequence[float]) -> RobustScale:
    """Centro y escala robustos de una muestra."""
    limpios = [_finite(v) for v in values]
    if not limpios:
        return RobustScale(0.0, 0.0)
    mediana = float(statistics.median(limpios))
    mad = float(statistics.median([abs(v - mediana) for v in limpios]))
    return RobustScale(mediana, mad * MAD_TO_SIGMA)


def robust_z(values: Sequence[float], *, clip: float = 3.0) -> list[float]:
    """Z-score robusto: mediana y MAD, no media y desviación.

    En este dominio los outliers son constantes —siempre hay un bot con un
    Sharpe absurdo por haber hecho tres operaciones—, y la media y la desviación
    los siguen. La mediana y el MAD no.

    MAD cero (todos iguales) devuelve ceros, no NaN ni división por cero.
    """
    escala = robust_scale(values)
    return [escala.z(_finite(v), clip) for v in values]


def robust_reference(
    metrics_by_bot: Mapping[str, Metrics], cfg: FitnessConfig
) -> dict[str, RobustScale]:
    """Congela la escala de una población para poder comparar contra ella.

    El fitness normal es relativo a los contemporáneos, que es lo que la
    selección necesita: dice cómo de bueno es un bot *aquí y ahora*. Pero eso
    hace que la mediana del jardín valga 0 por construcción, y entonces no hay
    forma de saber si veinte generaciones después el jardín ha mejorado o sólo
    se ha reordenado. Guardando la escala de la generación 0 y pasándola como
    ``reference``, el fitness pasa a medirse contra un metro fijo.
    """
    evaluables = _evaluables(metrics_by_bot, cfg)
    return _scales(evaluables)


def _evaluables(
    metrics_by_bot: Mapping[str, Metrics], cfg: FitnessConfig
) -> dict[str, Metrics]:
    """Los bots con evidencia suficiente como para fijar la escala."""
    return {
        bot: met
        for bot, met in metrics_by_bot.items()
        if met.n_trades >= cfg.min_trades
    }


def _scales(evaluables: Mapping[str, Metrics]) -> dict[str, RobustScale]:
    escalas: dict[str, RobustScale] = {}
    for nombre, campo, signo in BASE_METRICS:
        escalas[nombre] = robust_scale(
            [signo * getattr(met, campo) for met in evaluables.values()]
        )
    for campo in ("turnover", "novelty"):
        escalas[campo] = robust_scale([getattr(met, campo) for met in evaluables.values()])
    return escalas


# --------------------------------------------------------------------------- #
# El escalar                                                                   #
# --------------------------------------------------------------------------- #


def compute_fitness(
    metrics_by_bot: Mapping[str, Metrics],
    cfg: FitnessConfig,
    *,
    ages: Mapping[str, int] | None = None,
    complexities: Mapping[str, int] | None = None,
    reference: Mapping[str, RobustScale] | None = None,
) -> dict[str, FitnessBreakdown]:
    """Calcula el fitness de toda una población de golpe.

    Se hace en bloque, no bot a bot, porque la normalización es **relativa a la
    población viva**: el fitness dice cómo de bueno es un bot *comparado con sus
    contemporáneos*, que es lo que la selección necesita.

    Un bot con ``n_trades < cfg.min_trades`` recibe fitness **indefinido**, no
    bajo, con ``undefined_reason`` explicando por qué. La selección le da otra
    generación antes de juzgarlo: matar a un bot por no haber operado todavía es
    matar precisamente a los selectivos.

    Args:
        reference: escala congelada de otra población (ver ``robust_reference``).
            Con ella el fitness es comparable entre generaciones; sin ella es
            relativo a los presentes, que es lo que la selección quiere.
    """
    ages = ages or {}
    complexities = complexities or {}
    evaluables = _evaluables(metrics_by_bot, cfg)
    escalas = dict(reference) if reference else _scales(evaluables)

    out: dict[str, FitnessBreakdown] = {}
    for bot, met in metrics_by_bot.items():
        f = FitnessBreakdown(bot_id=bot)
        if met.n_trades < cfg.min_trades:
            f.undefined_reason = (
                f"sólo {met.n_trades} operaciones, hacen falta {cfg.min_trades}: "
                f"sin evidencia no hay juicio"
            )
            f.total = float("nan")
            out[bot] = f
            continue

        for nombre, campo, signo in BASE_METRICS:
            escala = escalas.get(nombre, RobustScale(0.0, 0.0))
            z = escala.z(signo * _finite(getattr(met, campo)))
            f.components[nombre] = z
            f.base += float(cfg.weights.get(nombre, 0.0)) * z

        complejidad = int(complexities.get(bot, COMPLEXITY_FREE))
        castigo = cfg.penalty_complexity * max(0, complejidad - COMPLEXITY_FREE)
        if castigo > 0:
            f.penalties["complexity"] = castigo

        exceso_corr = _finite(met.corr_to_population) - cfg.correlation_threshold
        if exceso_corr > 0:
            f.penalties["correlation"] = cfg.penalty_correlation * exceso_corr

        exceso_fees = _finite(met.fee_drag) - cfg.fee_drag_threshold
        if exceso_fees > 0:
            f.penalties["fees"] = cfg.penalty_fees * exceso_fees

        z_turnover = escalas.get("turnover", RobustScale(0.0, 0.0)).z(_finite(met.turnover))
        if z_turnover > TURNOVER_Z_THRESHOLD:
            f.penalties["turnover"] = cfg.penalty_turnover * (
                z_turnover - TURNOVER_Z_THRESHOLD
            )

        z_novedad = escalas.get("novelty", RobustScale(0.0, 0.0)).z(_finite(met.novelty))
        if z_novedad:
            f.bonuses["novelty"] = cfg.bonus_novelty * z_novedad

        edad = int(ages.get(bot, 0))
        saturacion = max(1, cfg.age_saturation_generations)
        bono_edad = cfg.bonus_age * min(1.0, edad / saturacion)
        if bono_edad > 0:
            f.bonuses["age"] = bono_edad

        f.total = f.base - sum(f.penalties.values()) + sum(f.bonuses.values())
        out[bot] = f

    return out


def effective_fitness(
    incubator: float | None, live: float | None, generations_alive: int, cfg: FitnessConfig
) -> float | None:
    """Combina backtest y jardín vivo.

    Con menos de 2 generaciones vividas, sólo cuenta la incubadora. A partir de
    ahí, ``0.3 * incubadora + 0.7 * vivo``. El backtest sirve para llegar a la
    puerta; después manda la realidad.
    """
    if incubator is not None and not math.isfinite(incubator):
        incubator = None
    if live is not None and not math.isfinite(live):
        live = None

    if live is None:
        return incubator
    if incubator is None:
        return live
    if generations_alive < 2:
        return float(incubator)
    return float(cfg.incubator_weight * incubator + cfg.live_weight * live)


# --------------------------------------------------------------------------- #
# Pareto                                                                       #
# --------------------------------------------------------------------------- #


def _dominates(a: Sequence[float], b: Sequence[float], maximize: Sequence[bool]) -> bool:
    """``a`` domina a ``b``: no es peor en nada y es mejor en algo."""
    mejor_en_algo = False
    for x, y, maxi in zip(a, b, maximize):
        if maxi:
            if x < y:
                return False
            if x > y:
                mejor_en_algo = True
        else:
            if x > y:
                return False
            if x < y:
                mejor_en_algo = True
    return mejor_en_algo


def pareto_front(
    objectives: Mapping[str, tuple[float, ...]], *, maximize: tuple[bool, ...]
) -> set[str]:
    """Frente no dominado sobre (sortino ↑, ulcer ↓, novelty ↑).

    Los bots del primer frente están protegidos de la poda una generación aunque
    su escalar sea mediocre. Es lo que mantiene vivos a los raros que aún no han
    demostrado su valor, y de los que suelen salir los saltos.
    """
    limpios = {
        bot: tuple(_finite(v) for v in valores) for bot, valores in objectives.items()
    }
    return {
        bot
        for bot, valores in limpios.items()
        if not any(
            _dominates(otros, valores, maximize)
            for otro, otros in limpios.items()
            if otro != bot
        )
    }


__all__ = (
    "BASE_METRICS",
    "COMPLEXITY_FREE",
    "MAD_TO_SIGMA",
    "TURNOVER_Z_THRESHOLD",
    "FitnessBreakdown",
    "RobustScale",
    "robust_scale",
    "robust_reference",
    "robust_z",
    "compute_fitness",
    "effective_fitness",
    "pareto_front",
)
