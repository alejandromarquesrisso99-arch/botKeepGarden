"""Cartera de un bot: posiciones, tamaño, stops y capital.

Cada bot tiene su propia cartera aislada (DECISIONS D-004). No comparten
capital, así las comparaciones son limpias.

El sitio delicado de este módulo es ``size_order``: traducir "arriesgo el 1 %
por operación" a una cantidad concreta es donde un error no se ve nunca —el bot
opera, gana y pierde— pero hace que el riesgo real sea diez veces el que dice
su genoma.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..config import Config
from ..genome.schema import RiskGene
from ..types import (
    EXIT_ORDER_KINDS,
    OrderKind,
    Side,
    SizingKind,
    StopKind,
    TakeProfitKind,
    Timestamp,
    TrailingKind,
)

#: Días de mercado al año, para pasar la volatilidad anualizada a diaria.
DAYS_PER_YEAR = 365.0

#: Por debajo de esto un denominador se considera cero.
EPS = 1e-12


def _direction(side: Side) -> float:
    return 1.0 if side is Side.LONG else -1.0


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
    #: ATR en el momento de entrar. El trailing se mide con él y no con el ATR
    #: vivo: si la volatilidad sube, un trailing medido en ATR actual se
    #: alejaría del precio, que es justo lo contrario de lo que debe hacer.
    entry_atr: float = 0.0
    #: Comisión pagada al abrir, para poder cerrar la operación con el neto real.
    entry_fee: float = 0.0
    #: Cierto cuando el stop ya lo mueve el trailing y no el gen de stop.
    trailing_active: bool = False

    @property
    def direction(self) -> float:
        return _direction(self.side)

    @property
    def entry_notional(self) -> float:
        return self.entry_price * self.amount

    def unrealized(self, price: float) -> float:
        """Beneficio no realizado a este precio, en dinero."""
        return (price - self.entry_price) * self.amount * self.direction

    def r_multiple(self, price: float) -> float:
        """Beneficio actual en múltiplos del riesgo inicial."""
        if self.initial_risk <= EPS:
            return 0.0
        return (price - self.entry_price) * self.direction / self.initial_risk


# --------------------------------------------------------------------------- #
# Precios del gen de riesgo                                                    #
# --------------------------------------------------------------------------- #


def stop_distance(risk: RiskGene, price: float, atr: float) -> float:
    """Distancia del stop al precio de entrada, en unidades de precio."""
    if risk.stop.kind is StopKind.PERCENT:
        return abs(price) * risk.stop.value
    if risk.stop.kind is StopKind.ATR_MULT:
        return risk.stop.value * max(atr, 0.0)
    return 0.0


def take_profit_price(
    risk: RiskGene, side: Side, price: float, atr: float, riesgo_inicial: float
) -> float | None:
    """Precio del take profit, o ``None`` si el genoma no lleva."""
    kind = risk.take_profit.kind
    if kind is TakeProfitKind.NONE or risk.take_profit.value <= 0:
        return None
    d = _direction(side)
    if kind is TakeProfitKind.PERCENT:
        return price * (1.0 + d * risk.take_profit.value)
    if kind is TakeProfitKind.ATR_MULT:
        return price + d * risk.take_profit.value * max(atr, 0.0)
    if riesgo_inicial <= EPS:
        return None
    return price + d * risk.take_profit.value * riesgo_inicial


def trailing_distance(risk: RiskGene, price: float, atr: float) -> float:
    """Cuánto se separa el trailing del extremo favorable alcanzado."""
    kind = risk.trailing.kind
    if kind is TrailingKind.NONE or risk.trailing.value <= 0:
        return 0.0
    if kind is TrailingKind.PERCENT:
        return abs(price) * risk.trailing.value
    return risk.trailing.value * max(atr, 0.0)     # ATR_MULT y CHANDELIER


# --------------------------------------------------------------------------- #
# La cartera                                                                   #
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Portfolio:
    """Estado financiero de un bot."""

    bot_id: str
    cash: float
    initial_capital: float
    peak_equity: float = 0.0
    positions: list[Position] = field(default_factory=list)
    cooldown_until_ts: Timestamp | None = None
    #: Operaciones cerradas, en el formato que consume ``evaluation.metrics``.
    closed_trades: list[dict[str, object]] = field(default_factory=list)
    #: Comisiones pagadas en total, abiertas y cerradas.
    total_fees: float = 0.0

    # -- consulta ---------------------------------------------------------- #

    def position_value(self, price: float) -> float:
        """Valor con signo de las posiciones: un corto resta."""
        return sum(p.amount * price * p.direction for p in self.positions)

    def equity(self, price: float) -> float:
        """Capital total: caja más valor de las posiciones abiertas."""
        return self.cash + self.position_value(price)

    def drawdown(self, price: float) -> float:
        """Caída desde el máximo histórico de equity, en fracción."""
        if self.peak_equity <= EPS:
            return 0.0
        return max(0.0, 1.0 - self.equity(price) / self.peak_equity)

    def exposure(self, price: float) -> float:
        """Fracción del capital comprometida en el mercado."""
        equity = self.equity(price)
        if equity <= EPS:
            return 0.0
        return sum(abs(p.amount) * abs(price) for p in self.positions) / equity

    def can_open(self, ts: Timestamp) -> bool:
        """¿Ha pasado ya el enfriamiento tras la última salida?"""
        return self.cooldown_until_ts is None or int(ts) >= int(self.cooldown_until_ts)

    # -- decisión ---------------------------------------------------------- #

    def size_order(
        self, risk: RiskGene, price: float, atr: float, realized_vol: float, cfg: Config
    ) -> float:
        """Traduce una señal de entrada a una cantidad concreta.

        * ``FIXED_FRACTION``: invierte ``max_exposure`` del capital. Es la
          "fracción fija" del gen; ``risk_per_trade`` se reserva para lo que
          mide riesgo de verdad (ver docs/DECISIONS.md D-016).
        * ``ATR_RISK``: cantidad tal que, si salta el stop, la pérdida sea
          ``risk_per_trade`` del equity. Es el modo sensato por defecto y el que
          hace comparables estrategias de volatilidades muy distintas.
        * ``VOL_TARGET``: tamaño inversamente proporcional a la volatilidad
          realizada, con ``risk_per_trade`` como volatilidad diaria objetivo.

        Respeta ``max_exposure``, ``max_concurrent_positions``, ``min_notional``
        y la caja disponible —incluida la comisión de entrada—. Devuelve 0 si no
        se puede abrir, nunca una cantidad que deje la caja en negativo.
        """
        if len(self.positions) >= max(1, risk.max_concurrent_positions):
            return 0.0
        if price <= EPS:
            return 0.0
        equity = self.equity(price)
        if equity <= EPS:
            return 0.0

        f = cfg.frictions
        por_defecto = equity * risk.max_exposure

        if risk.sizing is SizingKind.ATR_RISK:
            distancia = stop_distance(risk, price, atr)
            notional = (
                (equity * risk.risk_per_trade / distancia) * price
                if distancia > EPS
                else por_defecto
            )
        elif risk.sizing is SizingKind.VOL_TARGET:
            vol_diaria = max(realized_vol, 0.0) / math.sqrt(DAYS_PER_YEAR)
            notional = (
                equity * risk.risk_per_trade / vol_diaria if vol_diaria > EPS else por_defecto
            )
        else:
            notional = por_defecto

        # Techos, de menos a más restrictivo.
        libre = equity * risk.max_exposure - sum(
            abs(p.amount) * abs(price) for p in self.positions
        )
        notional = min(notional, libre)
        fee_rate = f.taker_fee_bps / 10_000.0
        notional = min(notional, max(self.cash, 0.0) / (1.0 + fee_rate))
        if notional <= EPS:
            return 0.0

        escala = 10.0**f.amount_precision
        amount = math.floor(notional / price * escala + 1e-9) / escala
        if amount <= 0 or amount * price < f.min_notional:
            return 0.0
        return amount

    def check_exits(
        self, high: float, low: float, close: float, ts: Timestamp, risk: RiskGene
    ) -> list[tuple[Position, OrderKind, float]]:
        """Comprueba stops, take profits, trailing y time-stop en una vela.

        Orden pesimista: **primero el stop**. Si una vela toca el stop y el take
        profit, se supone que saltó el stop: con velas de 1h no hay forma de
        saber el orden intrabarra, y suponer lo favorable infla cualquier
        backtest de forma sistemática.

        El trailing se comprueba con el ancla de la vela anterior y sólo después
        se mueve con el máximo de ésta. Moverlo primero regalaría salidas que
        nunca ocurrieron, porque dentro de la vela no se sabe si el máximo llegó
        antes que el mínimo.
        """
        salidas: list[tuple[Position, OrderKind, float]] = []
        for pos in self.positions:
            disparo = self._exit_for(pos, high, low, close, risk)
            if disparo is not None:
                salidas.append((pos, disparo[0], disparo[1]))
                continue
            self._advance_trailing(pos, high, low, risk)
        return salidas

    @staticmethod
    def _exit_for(
        pos: Position, high: float, low: float, close: float, risk: RiskGene
    ) -> tuple[OrderKind, float] | None:
        largo = pos.side is Side.LONG

        if pos.stop_price is not None:
            tocado = low <= pos.stop_price if largo else high >= pos.stop_price
            if tocado:
                kind = OrderKind.EXIT_TRAILING if pos.trailing_active else OrderKind.EXIT_STOP
                return kind, pos.stop_price

        if pos.take_price is not None:
            tocado = high >= pos.take_price if largo else low <= pos.take_price
            if tocado:
                return OrderKind.EXIT_TAKE_PROFIT, pos.take_price

        if risk.max_holding_bars > 0 and pos.bars_held >= risk.max_holding_bars:
            return OrderKind.EXIT_TIME, close

        return None

    @staticmethod
    def _advance_trailing(pos: Position, high: float, low: float, risk: RiskGene) -> None:
        """Mueve el trailing a favor, nunca en contra, y sólo pasado su umbral."""
        distancia = trailing_distance(risk, pos.entry_price, pos.entry_atr)
        if distancia <= EPS:
            return

        largo = pos.side is Side.LONG
        extremo = high if largo else low
        pos.trailing_anchor = (
            extremo
            if pos.trailing_anchor is None
            else (max(pos.trailing_anchor, extremo) if largo else min(pos.trailing_anchor, extremo))
        )
        if pos.r_multiple(pos.trailing_anchor) < risk.trailing.activate_at_r:
            return

        candidato = pos.trailing_anchor - distancia if largo else pos.trailing_anchor + distancia
        mejora = pos.stop_price is None or (
            candidato > pos.stop_price if largo else candidato < pos.stop_price
        )
        if mejora:
            pos.stop_price, pos.trailing_active = candidato, True

    # -- movimiento de dinero ---------------------------------------------- #

    def apply_fill(
        self,
        fill: object,
        *,
        risk: RiskGene | None = None,
        atr: float = 0.0,
        cooldown_ms: int = 0,
    ) -> dict[str, object] | None:
        """Aplica un fill: abre o cierra posición, mueve caja, cobra comisión.

        Al abrir necesita el gen de riesgo y el ATR de la vela para fijar el
        stop, el take profit y el 1R de la posición. Al cerrar devuelve la
        operación ya cuadrada, que es lo que consumen las métricas.
        """
        kind: OrderKind = fill.kind  # type: ignore[attr-defined]
        self.total_fees += float(fill.fee)  # type: ignore[attr-defined]
        if kind is OrderKind.ENTRY:
            self._open(fill, risk, atr)
            return None
        if kind in EXIT_ORDER_KINDS:
            return self._close(fill, cooldown_ms)
        return None

    def _open(self, fill: object, risk: RiskGene | None, atr: float) -> None:
        side: Side = fill.side  # type: ignore[attr-defined]
        price = float(fill.price)  # type: ignore[attr-defined]
        amount = float(fill.amount)  # type: ignore[attr-defined]
        notional = float(fill.notional)  # type: ignore[attr-defined]
        fee = float(fill.fee)  # type: ignore[attr-defined]

        # Un largo paga el notional; un corto sintético cobra lo vendido.
        self.cash += (-notional if side is Side.LONG else notional) - fee

        riesgo_inicial = stop_distance(risk, price, atr) if risk is not None else 0.0
        stop = None
        if riesgo_inicial > EPS:
            stop = price - _direction(side) * riesgo_inicial
        take = (
            take_profit_price(risk, side, price, atr, riesgo_inicial) if risk is not None else None
        )

        self.positions.append(
            Position(
                side=side,
                amount=amount,
                entry_price=price,
                entry_ts=int(fill.fill_ts),  # type: ignore[attr-defined]
                stop_price=stop,
                take_price=take,
                initial_risk=riesgo_inicial,
                entry_atr=max(atr, 0.0),
                entry_fee=fee,
            )
        )

    def _close(self, fill: object, cooldown_ms: int) -> dict[str, object] | None:
        side: Side = fill.side  # type: ignore[attr-defined]
        pos = next((p for p in self.positions if p.side is side), None)
        if pos is None:
            return None

        price = float(fill.price)  # type: ignore[attr-defined]
        fee = float(fill.fee)  # type: ignore[attr-defined]
        amount = min(float(fill.amount), pos.amount)  # type: ignore[attr-defined]
        if amount <= EPS:
            return None
        parte = amount / pos.amount
        notional = price * amount

        self.cash += (notional if side is Side.LONG else -notional) - fee

        bruto = (price - pos.entry_price) * amount * pos.direction
        comisiones = pos.entry_fee * parte + fee
        entrada = pos.entry_price * amount
        operacion: dict[str, object] = {
            "entry_ts": pos.entry_ts,
            "exit_ts": int(fill.fill_ts),  # type: ignore[attr-defined]
            "side": str(side),
            "entry_price": pos.entry_price,
            "exit_price": price,
            "amount": amount,
            "notional": entrada,
            "gross_pnl": bruto,
            "fees": comisiones,
            "pnl": bruto - comisiones,
            "return": (bruto - comisiones) / entrada if entrada > EPS else 0.0,
            "bars_held": pos.bars_held,
            "exit_kind": str(fill.kind),  # type: ignore[attr-defined]
        }
        self.closed_trades.append(operacion)

        if parte >= 1.0 - 1e-9:
            self.positions.remove(pos)
        else:
            pos.amount -= amount
            pos.entry_fee *= 1.0 - parte

        if cooldown_ms > 0:
            self.cooldown_until_ts = int(fill.fill_ts) + cooldown_ms  # type: ignore[attr-defined]
        return operacion

    def mark_to_market(self, price: float, ts: Timestamp) -> None:
        """Revalora, actualiza ``peak_equity`` y envejece las posiciones."""
        self.peak_equity = max(self.peak_equity, self.equity(price))
        for pos in self.positions:
            pos.bars_held += 1


__all__ = (
    "DAYS_PER_YEAR",
    "Portfolio",
    "Position",
    "stop_distance",
    "take_profit_price",
    "trailing_distance",
)
