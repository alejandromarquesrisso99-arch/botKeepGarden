"""Fitness: convertir métricas en un número comparable.

La fórmula completa está en docs/METRICS.md §Fitness. El principio: el fitness
no es "cuánto gana". Un bot que gana mucho con drawdowns del 60 %, 11
operaciones y correlación 0.98 con el resto del jardín es peor para el
ecosistema que uno que gana la mitad de forma estable y descorrelacionada.

CONTRATO — implementar en el hito 4.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence

from ..config import FitnessConfig
from .metrics import Metrics


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


def robust_z(values: Sequence[float], *, clip: float = 3.0) -> list[float]:
    """Z-score robusto: mediana y MAD, no media y desviación.

    En este dominio los outliers son constantes —siempre hay un bot con un
    Sharpe absurdo por haber hecho tres operaciones—, y la media y la desviación
    los siguen. La mediana y el MAD no.

    MAD cero (todos iguales) devuelve ceros, no NaN ni división por cero.
    """
    raise NotImplementedError


def compute_fitness(
    metrics_by_bot: Mapping[str, Metrics],
    cfg: FitnessConfig,
    *,
    ages: Mapping[str, int] | None = None,
    complexities: Mapping[str, int] | None = None,
) -> dict[str, FitnessBreakdown]:
    """Calcula el fitness de toda una población de golpe.

    Se hace en bloque, no bot a bot, porque la normalización es **relativa a la
    población viva**: el fitness dice cómo de bueno es un bot *comparado con sus
    contemporáneos*, que es lo que la selección necesita.

    Un bot con ``n_trades < cfg.min_trades`` recibe fitness **indefinido**, no
    bajo, con ``undefined_reason`` explicando por qué. La selección le da otra
    generación antes de juzgarlo: matar a un bot por no haber operado todavía es
    matar precisamente a los selectivos.
    """
    raise NotImplementedError


def effective_fitness(
    incubator: float | None, live: float | None, generations_alive: int, cfg: FitnessConfig
) -> float | None:
    """Combina backtest y jardín vivo.

    Con menos de 2 generaciones vividas, sólo cuenta la incubadora. A partir de
    ahí, ``0.3 * incubadora + 0.7 * vivo``. El backtest sirve para llegar a la
    puerta; después manda la realidad.
    """
    raise NotImplementedError


def pareto_front(
    objectives: Mapping[str, tuple[float, ...]], *, maximize: tuple[bool, ...]
) -> set[str]:
    """Frente no dominado sobre (sortino ↑, ulcer ↓, novelty ↑).

    Los bots del primer frente están protegidos de la poda una generación aunque
    su escalar sea mediocre. Es lo que mantiene vivos a los raros que aún no han
    demostrado su valor, y de los que suelen salir los saltos.
    """
    raise NotImplementedError


__all__ = (
    "FitnessBreakdown",
    "robust_z",
    "compute_fitness",
    "effective_fitness",
    "pareto_front",
)
