"""Backtest de un genoma sobre un rango de velas.

Es la unidad de trabajo de la incubadora y el comando ``keepgarden backtest``.
Es puro y sin estado: mismo genoma + mismas velas = mismo resultado, siempre, y
ejecutable en un proceso worker sin nada compartido.

La secuencia de cada vela es idéntica a la que usará el jardín vivo. Eso no es
elegancia: es la única forma de que el backtest y el vivo no puedan divergir, y
de que una sorpresa en vivo signifique algo.
"""

from __future__ import annotations

import os
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..config import Config
from ..data.candles import ensure_canonical, timeframe_ms
from ..data.indicators import IndicatorCache
from ..genome.compile import SIGNAL_TO_CODE, compile_genome
from ..genome.schema import Genome
from ..types import OrderKind, PriceField, Side, Signal, SizingKind
from .broker import OrderRequest, PaperBroker
from .portfolio import Portfolio

#: ATR por defecto cuando el genoma no declara ninguno para su stop.
DEFAULT_ATR_PARAMS = {"period": 14.0}

#: Ventana de la volatilidad realizada que usa el sizing VOL_TARGET: una semana
#: de velas de 1h.
DEFAULT_VOL_PARAMS = {"period": 168.0}

_ENTER_LONG = SIGNAL_TO_CODE[Signal.ENTER_LONG]
_EXIT_LONG = SIGNAL_TO_CODE[Signal.EXIT_LONG]
_ENTER_SHORT = SIGNAL_TO_CODE[Signal.ENTER_SHORT]
_EXIT_SHORT = SIGNAL_TO_CODE[Signal.EXIT_SHORT]


@dataclass(slots=True)
class BacktestResult:
    """Resultado crudo. Las métricas las calcula ``evaluation.metrics``."""

    genome_id: str
    start_ts: int
    end_ts: int
    n_bars: int
    trades: list[dict[str, object]] = field(default_factory=list)
    equity_curve: np.ndarray | None = None
    equity_ts: np.ndarray | None = None
    final_equity: float = 0.0
    total_fees: float = 0.0
    warmup_bars: int = 0
    aborted_reason: str = ""
    #: Beneficio de las posiciones que quedaron abiertas al final. Sin él la
    #: contabilidad no cuadra: ``final_equity = inicial + pnl cerrado + esto``.
    unrealized_pnl: float = 0.0


def _series(
    features: object, kind: str, params: dict[str, float], source: PriceField, n: int
) -> np.ndarray:
    """Una serie auxiliar del motor (ATR, volatilidad), con ceros si falla."""
    try:
        values = np.asarray(features.get(kind, params, str(source)), dtype="float64")  # type: ignore[attr-defined]
    except (KeyError, ValueError):
        return np.zeros(n, dtype="float64")
    return np.nan_to_num(values, nan=0.0, posinf=0.0, neginf=0.0)


def run_backtest(
    genome: Genome,
    candles: pd.DataFrame,
    cfg: Config,
    *,
    features: object | None = None,
    initial_capital: float | None = None,
) -> BacktestResult:
    """Corre un genoma sobre un rango de velas.

    Secuencia por vela ``t``:

    1. Rellenar en la apertura de ``t`` lo que se encoló en ``t-1``.
    2. Comprobar salidas de las posiciones abiertas con ``high``/``low`` de
       ``t`` y ejecutarlas dentro de la vela (convención pesimista del broker).
    3. Frenos: si el drawdown supera ``risk.hard_max_drawdown``, cerrar y
       abortar. Si por delante hay un hueco largo de datos, cerrar antes de
       atravesarlo: inventar precio en una parada del venue es inventar
       rentabilidad.
    4. Evaluar la señal del genoma con datos cerrados hasta ``t`` y encolar la
       orden, que se rellenará en la apertura de ``t+1``.
    5. Revalorar y anotar equity.

    Durante el calentamiento no se opera.
    """
    data = ensure_canonical(candles)
    n = len(data)
    capital = float(
        initial_capital if initial_capital is not None else cfg.garden.initial_capital_per_bot
    )
    result = BacktestResult(
        genome_id=genome.id,
        start_ts=int(data.index[0]) if n else 0,
        end_ts=int(data.index[-1]) if n else 0,
        n_bars=n,
        final_equity=capital,
        equity_curve=np.zeros(0, dtype="float64"),
        equity_ts=np.zeros(0, dtype="int64"),
    )
    if n == 0:
        return result

    if features is None:
        features = IndicatorCache(candles=data, timeframe=genome.market.timeframe)
    compiled = compile_genome(genome, data, features)  # type: ignore[arg-type]
    risk = genome.risk

    ts = np.asarray(data.index, dtype="int64")
    open_ = data["open"].to_numpy(dtype="float64")
    high = data["high"].to_numpy(dtype="float64")
    low = data["low"].to_numpy(dtype="float64")
    close = data["close"].to_numpy(dtype="float64")
    volume = data["volume"].to_numpy(dtype="float64")

    atr = _series(features, "ATR", DEFAULT_ATR_PARAMS, PriceField.HLC3, n)
    stop_atr = atr
    if risk.stop.atr_ref and risk.stop.atr_ref in compiled.feature_arrays:
        stop_atr = np.nan_to_num(compiled.feature_arrays[risk.stop.atr_ref], nan=0.0)
    realized_vol = (
        _series(features, "REALIZED_VOL", DEFAULT_VOL_PARAMS, PriceField.CLOSE, n)
        if risk.sizing is SizingKind.VOL_TARGET
        else np.zeros(n, dtype="float64")
    )

    tf_ms = timeframe_ms(genome.market.timeframe)
    hueco_delante = np.zeros(n, dtype=bool)
    if n > 1:
        hueco_delante[:-1] = np.diff(ts) > tf_ms

    broker = PaperBroker(frictions=cfg.frictions)
    port = Portfolio(
        bot_id=genome.id, cash=capital, initial_capital=capital, peak_equity=capital
    )
    cooldown_ms = max(0, int(risk.cooldown_bars)) * tf_ms
    warmup = compiled.warmup_bars
    equity = np.full(n, capital, dtype="float64")

    def vela(t: int) -> dict[str, float]:
        return {
            "ts": float(ts[t]), "open": open_[t], "high": high[t], "low": low[t],
            "close": close[t], "volume": volume[t], "atr": float(atr[t]),
        }

    def rellenar(candle: dict[str, float], t: int) -> None:
        for f in broker.settle(candle):
            port.apply_fill(f, risk=risk, atr=float(stop_atr[t]), cooldown_ms=cooldown_ms)

    def cerrar_todo(t: int, motivo_precio: float) -> None:
        """Cierre forzoso al precio dado, con su fricción."""
        for pos in list(port.positions):
            broker.submit(
                OrderRequest(genome.id, int(ts[t]), OrderKind.EXIT_FORCED, pos.side, pos.amount)
            )
        rellenar({**vela(t), "open": motivo_precio}, t)

    for t in range(n):
        candle = vela(t)

        rellenar(candle, t)

        for pos, kind, disparo in port.check_exits(
            high[t], low[t], close[t], int(ts[t]), risk
        ):
            broker.submit(
                OrderRequest(genome.id, int(ts[t]), kind, pos.side, pos.amount, disparo)
            )
        rellenar(candle, t)

        if port.drawdown(close[t]) > cfg.risk.hard_max_drawdown:
            cerrar_todo(t, close[t])
            result.aborted_reason = "DRAWDOWN_BREAKER"
            port.mark_to_market(close[t], int(ts[t]))
            equity[t:] = port.equity(close[t])
            break

        if hueco_delante[t] and port.positions:
            cerrar_todo(t, close[t])

        if t >= warmup and t + 1 < n and not hueco_delante[t]:
            _decidir(broker, port, genome, compiled.codes[t], t, ts, close, stop_atr,
                     realized_vol, cfg)

        port.mark_to_market(close[t], int(ts[t]))
        equity[t] = port.equity(close[t])

    result.trades = port.closed_trades
    result.total_fees = port.total_fees
    result.equity_curve = equity
    result.equity_ts = ts
    result.final_equity = float(equity[-1])
    result.unrealized_pnl = float(sum(p.unrealized(close[-1]) for p in port.positions))
    result.warmup_bars = warmup
    return result


def _decidir(
    broker: PaperBroker,
    port: Portfolio,
    genome: Genome,
    code: int,
    t: int,
    ts: np.ndarray,
    close: np.ndarray,
    stop_atr: np.ndarray,
    realized_vol: np.ndarray,
    cfg: Config,
) -> None:
    """Traduce la señal de la vela ``t`` en una orden para la apertura de ``t+1``."""
    risk = genome.risk
    momento = int(ts[t])
    largo = next((p for p in port.positions if p.side is Side.LONG), None)
    corto = next((p for p in port.positions if p.side is Side.SHORT), None)

    if code == _EXIT_LONG and largo is not None:
        broker.submit(
            OrderRequest(genome.id, momento, OrderKind.EXIT_SIGNAL, Side.LONG, largo.amount)
        )
        return
    if code == _EXIT_SHORT and corto is not None:
        broker.submit(
            OrderRequest(genome.id, momento, OrderKind.EXIT_SIGNAL, Side.SHORT, corto.amount)
        )
        return

    entrada = None
    if code == _ENTER_LONG and largo is None:
        entrada = Side.LONG
    elif code == _ENTER_SHORT and corto is None and risk.allow_short:
        entrada = Side.SHORT
    if entrada is None or not port.can_open(momento):
        return

    amount = port.size_order(
        risk, close[t], float(stop_atr[t]), float(realized_vol[t]), cfg
    )
    if amount > 0:
        broker.submit(OrderRequest(genome.id, momento, OrderKind.ENTRY, entrada, amount))


def _run_one(argumentos: tuple[Genome, pd.DataFrame, Config]) -> BacktestResult:
    """Punto de entrada de cada worker. Debe estar al nivel del módulo para que
    ``multiprocessing`` pueda encontrarlo al arrancar un proceso nuevo."""
    genome, candles, cfg = argumentos
    return run_backtest(genome, candles, cfg)


def run_batch(
    genomes: list[Genome], candles: pd.DataFrame, cfg: Config, *, workers: int = 0
) -> list[BacktestResult]:
    """Corre muchos genomas en paralelo con ``multiprocessing``.

    Cada worker recibe el genoma y las velas y devuelve el resultado. Sin estado
    compartido: es lo que hace la paralelización trivial y correcta.
    ``workers=0`` usa ``cpu_count() - 1``.

    Con un solo worker no se levanta ningún proceso: para tres genomas, arrancar
    intérpretes cuesta más que correrlos.
    """
    if not genomes:
        return []
    n = workers if workers > 0 else max(1, (os.cpu_count() or 2) - 1)
    if n <= 1 or len(genomes) == 1:
        return [run_backtest(g, candles, cfg) for g in genomes]
    with ProcessPoolExecutor(max_workers=n) as pool:
        return list(pool.map(_run_one, [(g, candles, cfg) for g in genomes]))


__all__ = (
    "DEFAULT_ATR_PARAMS",
    "DEFAULT_VOL_PARAMS",
    "BacktestResult",
    "run_backtest",
    "run_batch",
)
