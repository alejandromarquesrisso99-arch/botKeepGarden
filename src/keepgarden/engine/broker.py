"""Broker simulado: fricción, fills y la convención pesimista.

La regla que gobierna todo este módulo: **la decisión se toma con la vela `t`
cerrada; el fill ocurre en la apertura de `t+1`.** Ver docs/EXECUTION.md.

CONTRATO — implementar en el hito 3.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..config import FrictionConfig
from ..types import OrderKind, Side, Timestamp


@dataclass(frozen=True, slots=True)
class OrderRequest:
    """Intención de operar, emitida en la vela ``candle_ts``."""

    bot_id: str
    candle_ts: Timestamp
    kind: OrderKind
    side: Side
    amount: float
    #: Precio de referencia para stops y take profits; ``None`` para órdenes a mercado.
    trigger_price: float | None = None


@dataclass(frozen=True, slots=True)
class Fill:
    """Ejecución real, con fricción aplicada."""

    bot_id: str
    candle_ts: Timestamp
    fill_ts: Timestamp
    kind: OrderKind
    side: Side
    price: float             # con slippage
    reference_price: float   # sin slippage
    slippage: float
    amount: float
    notional: float
    fee: float


class BrokerAdapter(Protocol):
    """Interfaz común a paper y (algún día) live.

    Existe para que el día que se conecte un broker real no haya que tocar el
    motor. ``config.execution.mode: live`` está bloqueado: ver DECISIONS D-006.
    """

    def submit(self, order: OrderRequest) -> None: ...
    def settle(self, candle: dict[str, float]) -> list[Fill]: ...


@dataclass(slots=True)
class PaperBroker:
    """Broker simulado contra las velas reales."""

    frictions: FrictionConfig

    def submit(self, order: OrderRequest) -> None:
        """Encola una orden para rellenar en la apertura de la siguiente vela."""
        raise NotImplementedError

    def settle(self, candle: dict[str, float]) -> list[Fill]:
        """Rellena las órdenes pendientes contra la vela dada.

        Reglas, todas pesimistas a propósito:

        * Orden a mercado: fill en ``candle["open"]`` más slippage en contra.
        * Si la vela abre más allá del stop (gap), el fill es en ``open``, no en
          el precio del stop. Los gaps existen y cuestan dinero.
        * Si en la misma vela el ``low`` toca el stop **y** el ``high`` toca el
          take profit, se asume que saltó el **stop** primero. Siempre el peor
          caso: con velas de 1h no hay forma de saber el orden intrabarra, y
          suponer lo favorable infla sistemáticamente cualquier backtest.
        * Comisión ``taker_fee_bps`` sobre el notional, en entrada y en salida.
        * Rechaza órdenes por debajo de ``min_notional`` y redondea a
          ``price_precision`` / ``amount_precision``.
        """
        raise NotImplementedError

    def slippage_for(self, price: float, atr: float, volume: float, amount: float) -> float:
        """Deslizamiento en unidades de precio, siempre en contra.

        * ``fixed_bps``: ``price * slippage_bps / 10000``.
        * ``atr``: ``slippage_atr_frac * atr``. Es el modelo por defecto y el más
          honesto: el coste de cruzar el spread escala con la volatilidad.
        * ``volume_aware``: el anterior más un término proporcional a
          ``amount * price / (volume * price)``.
        """
        raise NotImplementedError


__all__ = ("OrderRequest", "Fill", "BrokerAdapter", "PaperBroker")
