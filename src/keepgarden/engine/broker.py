"""Broker simulado: fricción, fills y la convención pesimista.

La regla que gobierna todo este módulo: **la decisión se toma con la vela `t`
cerrada; el fill ocurre en la apertura de `t+1`.** Ver docs/EXECUTION.md.

Hay tres formas clásicas de mentirse en un backtest y las tres están cerradas
aquí:

1. rellenar al cierre de la misma vela que dio la señal;
2. cobrar un stop a su precio cuando el mercado abrió más abajo;
3. suponer que dentro de una vela saltó primero lo que nos convenía.

Con velas de 1h no hay forma de saber el orden intrabarra, así que siempre se
supone el peor caso. Un backtest optimista es peor que ninguno: da confianza
donde no la hay.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Protocol

from ..config import FrictionConfig
from ..types import EXIT_ORDER_KINDS, OrderKind, Side, Timestamp

#: Órdenes que reposan en el mercado y se ejecutan dentro de la vela al tocarse
#: su precio, en vez de a la apertura de la siguiente.
RESTING_KINDS: frozenset[OrderKind] = frozenset(
    {OrderKind.EXIT_STOP, OrderKind.EXIT_TAKE_PROFIT, OrderKind.EXIT_TRAILING}
)


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

    @property
    def is_buy(self) -> bool:
        """¿Esta orden compra?

        ``side`` es el lado de la *posición*, no el de la orden: se entra en
        largo comprando y se sale vendiendo, y al revés en corto.
        """
        return (self.kind is OrderKind.ENTRY) == (self.side is Side.LONG)


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

    @property
    def is_buy(self) -> bool:
        return (self.kind is OrderKind.ENTRY) == (self.side is Side.LONG)


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
    _pending: list[OrderRequest] = field(default_factory=list, repr=False)

    # -- cola --------------------------------------------------------------- #

    def submit(self, order: OrderRequest) -> None:
        """Encola una orden para rellenar en la apertura de la siguiente vela."""
        self._pending.append(order)

    def cancel_all(self) -> None:
        """Vacía la cola sin rellenar nada. Lo usa el freno de emergencia."""
        self._pending.clear()

    @property
    def pending(self) -> tuple[OrderRequest, ...]:
        """Órdenes encoladas, para inspección."""
        return tuple(self._pending)

    # -- fricción ----------------------------------------------------------- #

    def slippage_for(self, price: float, atr: float, volume: float, amount: float) -> float:
        """Deslizamiento en unidades de precio, siempre en contra.

        * ``fixed_bps``: ``price * slippage_bps / 10000``.
        * ``atr``: ``slippage_atr_frac * atr``. Es el modelo por defecto y el más
          honesto: el coste de cruzar el spread escala con la volatilidad. Si no
          hay ATR todavía (calentamiento), cae al modelo de puntos básicos.
        * ``volume_aware``: el anterior más un término proporcional a la
          fracción del volumen de la vela que se está tragando la orden.
        """
        f = self.frictions
        base = abs(price) * f.slippage_bps / 10_000.0
        if f.slippage_model == "fixed_bps":
            return max(0.0, base)

        por_atr = f.slippage_atr_frac * atr if atr and math.isfinite(atr) and atr > 0 else base
        if f.slippage_model == "atr":
            return max(0.0, por_atr)

        # volume_aware: tragarse una parte grande de la vela mueve el precio.
        cuota = abs(amount) / volume if volume and volume > 0 else 0.0
        return max(0.0, por_atr + abs(price) * min(cuota, 1.0))

    @staticmethod
    def _floor_to(value: float, precision: int) -> float:
        """Trunca a la precisión del venue.

        Truncar y no redondear: redondear hacia arriba compraría más de lo que
        hay en caja. El epsilon es contra los artefactos del binario, que hacen
        que ``0.123 * 1000`` valga 122.99999999999999 y truncar dé 0.122.
        """
        escala = 10.0**precision
        return math.floor(value * escala + 1e-9) / escala

    def _fee(self, notional: float) -> float:
        return abs(notional) * self.frictions.taker_fee_bps / 10_000.0

    # -- fills -------------------------------------------------------------- #

    def _reference_price(self, order: OrderRequest, candle: dict[str, float]) -> float:
        """Precio al que se ejecuta la orden, antes del deslizamiento."""
        open_ = float(candle["open"])
        if order.trigger_price is None or order.kind not in RESTING_KINDS:
            return open_

        trigger = float(order.trigger_price)
        if order.kind is OrderKind.EXIT_TAKE_PROFIT:
            # El hueco a favor no se cobra: un take profit se paga a su precio.
            return trigger

        # Stop y trailing: si la vela abre más allá, no hubo forma de salir en el
        # precio del stop. Los gaps existen y cuestan dinero.
        if order.is_buy:                      # stop de un corto: salta al subir
            return max(trigger, open_)
        return min(trigger, open_)            # stop de un largo: salta al caer

    def settle(self, candle: dict[str, float]) -> list[Fill]:
        """Rellena las órdenes pendientes contra la vela dada.

        Reglas, todas pesimistas a propósito:

        * Orden a mercado: fill en ``candle["open"]`` más slippage en contra.
        * Si la vela abre más allá del stop (gap), el fill es en ``open``, no en
          el precio del stop.
        * El take profit se cobra a su precio aunque la vela abra mejor.
        * Comisión ``taker_fee_bps`` sobre el notional, en entrada y en salida.
        * Rechaza **entradas** por debajo de ``min_notional`` y redondea a
          ``price_precision`` / ``amount_precision``. Una salida no se rechaza
          nunca: dejaría la posición atrapada para siempre.

        Las salidas se rellenan antes que las entradas: si en la misma vela se
        cierra una posición y se abre otra, el dinero entra en caja primero.
        """
        if not self._pending:
            return []

        f = self.frictions
        pendientes = sorted(self._pending, key=lambda o: 0 if o.kind in EXIT_ORDER_KINDS else 1)
        self._pending = []

        fill_ts = int(candle.get("ts", 0))
        atr = float(candle.get("atr", 0.0) or 0.0)
        volume = float(candle.get("volume", 0.0) or 0.0)

        fills: list[Fill] = []
        for order in pendientes:
            amount = self._floor_to(abs(float(order.amount)), f.amount_precision)
            if amount <= 0:
                continue

            referencia = self._reference_price(order, candle)
            slippage = self.slippage_for(referencia, atr, volume, amount)
            precio = round(
                referencia + slippage if order.is_buy else referencia - slippage,
                f.price_precision,
            )
            if precio <= 0:
                continue

            notional = precio * amount
            if order.kind is OrderKind.ENTRY and notional < f.min_notional:
                continue

            fills.append(
                Fill(
                    bot_id=order.bot_id,
                    candle_ts=order.candle_ts,
                    fill_ts=fill_ts,
                    kind=order.kind,
                    side=order.side,
                    price=precio,
                    reference_price=referencia,
                    slippage=slippage,
                    amount=amount,
                    notional=notional,
                    fee=self._fee(notional),
                )
            )
        return fills


__all__ = ("RESTING_KINDS", "BrokerAdapter", "Fill", "OrderRequest", "PaperBroker")
