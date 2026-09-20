"""Cartera de un bot: posiciones, tamaño, stops y capital.

Cada bot tiene su propia cartera aislada (DECISIONS D-004). No comparten
capital, así las comparaciones son limpias.

CONTRATO — implementar en el hito 3.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import Config
from ..genome.schema import RiskGene
from ..types import OrderKind, Side, Timestamp


@dataclass(slots=True)
class Position:
    side: Side
    amount: float
    entry_price: float
    entry_ts: Timestamp
    stop_price: float | None = None
    take_price: float | None = None
    trailing_anchor: float | None = None   # extremo favorable alcanzado
    initial_risk: float = 0.0              # 1R en unidades de precio
    bars_held: int = 0

    def unrealized(self, price: float) -> float:
        raise NotImplementedError

    def r_multiple(self, price: float) -> float:
        """Beneficio actual en múltiplos del riesgo inicial."""
        raise NotImplementedError


@dataclass(slots=True)
class Portfolio:
    """Estado financiero de un bot."""

    bot_id: str
    cash: float
    initial_capital: float
    peak_equity: float = 0.0
    positions: list[Position] = field(default_factory=list)
    cooldown_until_ts: Timestamp | None = None

    # -- consulta ---------------------------------------------------------- #

    def equity(self, price: float) -> float:
        """Capital total: caja más valor de las posiciones abiertas."""
        raise NotImplementedError

    def drawdown(self, price: float) -> float:
        """Caída desde el máximo histórico de equity, en fracción."""
        raise NotImplementedError

    def exposure(self, price: float) -> float:
        raise NotImplementedError

    # -- decisión ---------------------------------------------------------- #

    def size_order(
        self, risk: RiskGene, price: float, atr: float, realized_vol: float, cfg: Config
    ) -> float:
        """Traduce una señal de entrada a una cantidad concreta.

        * ``FIXED_FRACTION``: ``equity * risk_per_trade * apalancamiento_1`` /
          precio.
        * ``ATR_RISK``: cantidad tal que, si salta el stop, la pérdida sea
          ``risk_per_trade`` del equity. Es el modo sensato por defecto y el que
          hace comparables estrategias de volatilidades muy distintas.
        * ``VOL_TARGET``: tamaño inversamente proporcional a la volatilidad
          realizada, con un objetivo de volatilidad de cartera.

        Debe respetar ``max_exposure``, ``max_concurrent_positions``,
        ``min_notional`` y la caja disponible. Devuelve 0 si no se puede abrir,
        nunca una cantidad que deje la caja en negativo.
        """
        raise NotImplementedError

    def check_exits(
        self, high: float, low: float, close: float, ts: Timestamp, risk: RiskGene
    ) -> list[tuple[Position, OrderKind, float]]:
        """Comprueba stops, take profits, trailing y time-stop en una vela.

        Orden de comprobación (pesimista): stop, luego take profit, luego
        trailing, luego time-stop. Devuelve las salidas a ejecutar con su
        precio de disparo.

        El trailing sólo se activa cuando el beneficio supera
        ``trailing.activate_at_r`` múltiplos de R, y sólo se mueve a favor.
        """
        raise NotImplementedError

    def apply_fill(self, fill: "object") -> None:
        """Aplica un fill: abre o cierra posición, mueve caja, cobra comisión."""
        raise NotImplementedError

    def mark_to_market(self, price: float, ts: Timestamp) -> None:
        """Revalora, actualiza ``peak_equity`` y envejece las posiciones."""
        raise NotImplementedError


__all__ = ("Position", "Portfolio")
