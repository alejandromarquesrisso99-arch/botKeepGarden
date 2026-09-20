"""Métricas de rendimiento y riesgo.

Todo lo de docs/METRICS.md §1. Este módulo es matemática financiera pura: sin
estado, sin base de datos, sin configuración más allá del timeframe.

**Escribe los tests antes que la implementación.** Un error silencioso aquí
contamina el fitness, la selección y todas las decisiones del jardinero durante
meses sin que nada falle visiblemente.

CONTRATO — implementar en el hito 3.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Sequence

from ..types import Timeframe

if TYPE_CHECKING:  # pragma: no cover
    import numpy as np


@dataclass(slots=True)
class Metrics:
    """Todas las métricas de un bot sobre una ventana."""

    # retorno
    total_return: float = 0.0
    cagr: float = 0.0
    avg_trade_return: float = 0.0
    expectancy: float = 0.0
    # riesgo
    max_drawdown: float = 0.0
    ulcer_index: float = 0.0
    downside_dev: float = 0.0
    time_in_market: float = 0.0
    worst_trade: float = 0.0
    # ajustadas
    sharpe: float = 0.0
    sortino: float = 0.0
    calmar: float = 0.0
    martin: float = 0.0
    profit_factor: float = 0.0
    # comportamiento
    n_trades: int = 0
    win_rate: float = 0.0
    turnover: float = 0.0
    avg_holding_bars: float = 0.0
    fee_drag: float = 0.0
    consistency: float = 0.0
    # relacionales (las rellena el evaluador de población)
    corr_to_population: float = 0.0
    corr_to_benchmark: float = 0.0
    novelty: float = 0.0

    def to_dict(self) -> dict[str, float]:
        return asdict(self)

    @property
    def is_evaluable(self) -> bool:
        """Con muy pocas operaciones no hay evidencia: el fitness queda
        indefinido, que no es lo mismo que bajo."""
        return self.n_trades > 0


def compute_metrics(
    equity: "np.ndarray",
    trades: Sequence[dict[str, object]],
    timeframe: Timeframe,
    *,
    total_fees: float = 0.0,
    benchmark: "np.ndarray | None" = None,
) -> Metrics:
    """Calcula todas las métricas de una ventana.

    Detalles que importan:

    * Los retornos son **logarítmicos** para agregar, aritméticos para reportar.
    * ``sortino`` usa ``downside_dev`` con umbral 0, no la desviación total.
      Es la métrica principal del sistema.
    * ``ulcer_index = sqrt(mean(drawdown_t^2))`` — castiga los drawdowns
      *largos*, no sólo los profundos, que es lo que de verdad hace abandonar
      una estrategia.
    * ``consistency`` = fracción de subventanas diarias con retorno positivo.
    * ``fee_drag = fees_pagadas / max(pnl_bruto, ε)``. Por encima de 0.5 el bot
      vive para pagar comisiones.
    * Con ``max_drawdown == 0`` (nunca perdió), ``calmar`` no es infinito: se
      recorta a un tope configurable para que no domine el ranking un bot con
      dos operaciones afortunadas.

    Anualización con ``PERIODS_PER_YEAR[timeframe]`` (8760 para 1h).
    """
    raise NotImplementedError


def drawdown_series(equity: "np.ndarray") -> "np.ndarray":
    """Serie de drawdown desde máximos, en fracción y positiva."""
    raise NotImplementedError


def rolling_correlation(a: "np.ndarray", b: "np.ndarray") -> float:
    """Correlación de Pearson entre dos series de retornos, robusta a NaN."""
    raise NotImplementedError


def population_correlations(equities: dict[str, "np.ndarray"]) -> dict[str, float]:
    """Correlación media de cada bot con el resto de la población.

    Es la métrica que hace posible la fusión: se buscan padres con correlación
    baja entre sí, porque dos estrategias mediocres descorrelacionadas producen
    al combinarse una curva con menos drawdown que cualquiera de las dos. Es la
    única comida gratis que hay en esto.
    """
    raise NotImplementedError


__all__ = (
    "Metrics",
    "compute_metrics",
    "drawdown_series",
    "rolling_correlation",
    "population_correlations",
)
