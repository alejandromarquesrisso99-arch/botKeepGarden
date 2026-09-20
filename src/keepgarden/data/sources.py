"""Descarga de velas desde el venue (Binance vía ccxt).

Todo lo que sale de aquí está ya en forma canónica y **cerrado**. El resto del
sistema confía en eso y no vuelve a comprobarlo, así que la barrera contra el
look-ahead vive en ``_only_closed`` y tiene tests dedicados.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from ..types import Timeframe, Timestamp
from .candles import empty_frame, ensure_canonical, timeframe_ms

#: Máximo de velas por petición en Binance.
MAX_LIMIT = 1000

#: Columnas que devuelve ``ccxt.fetch_ohlcv``. No incluye el nº de operaciones.
CCXT_COLUMNS = ("ts", "open", "high", "low", "close", "volume")

_ccxt_module: Any = None


class VenueError(RuntimeError):
    """Fallo del venue: red, rate limit, símbolo inexistente."""


def _ccxt() -> Any:
    """Importa ``ccxt`` la primera vez que hace falta.

    Importarlo cuesta casi un segundo y arrastra cientos de módulos; la CLI
    tiene comandos que no tocan el venue y no tienen por qué pagarlo.
    """
    global _ccxt_module
    if _ccxt_module is None:
        import ccxt

        _ccxt_module = ccxt
    return _ccxt_module


@dataclass(slots=True)
class VenueClient:
    """Cliente del venue. Sólo lectura: no necesita claves API.

    Usa ``ccxt`` con ``enableRateLimit=True``. No hay websockets: con timeframe
    de 1h, pedir por REST la última vela cerrada es más simple y más robusto
    ante desconexiones. Ver docs/DATA.md.

    ``exchange``, ``clock`` y ``sleep`` existen para poder probar el cliente sin
    red: en producción se dejan a ``None`` y se usan ccxt y el reloj del sistema.
    """

    venue: str = "binance"
    timeout_ms: int = 20000
    max_retries: int = 4
    exchange: Any = None
    clock: Callable[[], int] | None = None
    sleep: Callable[[float], None] | None = None
    backoff_base_seconds: float = 1.0

    # -- fontanería --------------------------------------------------------- #

    def _exchange(self) -> Any:
        if self.exchange is None:
            ccxt = _ccxt()
            try:
                factory = getattr(ccxt, self.venue)
            except AttributeError:
                raise VenueError(f"venue desconocido para ccxt: {self.venue!r}") from None
            self.exchange = factory({"enableRateLimit": True, "timeout": self.timeout_ms})
        return self.exchange

    def now_ms(self) -> Timestamp:
        """Instante actual en ms UTC. Inyectable para los tests."""
        return int(self.clock()) if self.clock is not None else int(time.time() * 1000)

    def _wait(self, seconds: float) -> None:
        (self.sleep or time.sleep)(seconds)

    def _call(self, what: str, fn: Callable[[], Any]) -> Any:
        """Llama al venue reintentando con backoff exponencial.

        Sólo se reintenta lo que puede arreglarse esperando: red, timeouts y
        rate limit. Un símbolo que no existe no mejora por insistir.
        """
        ccxt = _ccxt()
        last: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                return fn()
            except ccxt.NetworkError as exc:       # incluye timeouts y rate limit
                last = exc
                if attempt == self.max_retries:
                    break
                self._wait(self.backoff_base_seconds * (2.0**attempt))
            except ccxt.BaseError as exc:          # BadSymbol, ExchangeError…
                raise VenueError(f"{what}: {type(exc).__name__}: {exc}") from exc
        raise VenueError(
            f"{what}: el venue sigue fallando tras {self.max_retries} reintentos "
            f"({type(last).__name__}: {last})"
        )

    # -- conversión --------------------------------------------------------- #

    @staticmethod
    def _to_frame(rows: Any) -> pd.DataFrame:
        """Pasa la lista de listas de ccxt a la forma canónica.

        ``trades`` queda en ``NaN``: ``fetch_ohlcv`` no lo expone. Es mejor un
        desconocido explícito que un cero que se confundiría con una vela de
        relleno.
        """
        if rows is None or len(rows) == 0:
            return empty_frame()
        arr = np.asarray(rows, dtype="float64")
        df = pd.DataFrame(arr[:, : len(CCXT_COLUMNS)], columns=list(CCXT_COLUMNS))
        df["ts"] = df["ts"].astype("int64")
        df["trades"] = np.nan
        out = ensure_canonical(df)
        return out[~out.index.duplicated(keep="last")]

    def _only_closed(self, df: pd.DataFrame, timeframe: Timeframe) -> pd.DataFrame:
        """Quita la vela en curso. La barrera contra el look-ahead más caro."""
        if df.empty:
            return df
        tf_ms = timeframe_ms(timeframe)
        return df.loc[np.asarray(df.index, dtype="int64") + tf_ms <= self.now_ms()]

    # -- API ---------------------------------------------------------------- #

    def fetch_ohlcv(
        self, symbol: str, timeframe: Timeframe, since: Timestamp, limit: int = MAX_LIMIT
    ) -> pd.DataFrame:
        """Pide un bloque de velas desde ``since``.

        Devuelve sólo velas **cerradas**: la última que devuelve el venue suele
        ser la vela en curso y hay que descartarla siempre. Este es el punto
        exacto donde se cuela el look-ahead más caro del sistema.

        Reintenta con backoff exponencial ante errores de red y rate limit.
        """
        exchange = self._exchange()
        capped = max(1, min(int(limit), MAX_LIMIT))
        rows = self._call(
            f"fetch_ohlcv {symbol} {timeframe}",
            lambda: exchange.fetch_ohlcv(symbol, timeframe, since=int(since), limit=capped),
        )
        return self._only_closed(self._to_frame(rows), timeframe)

    def iter_backfill(
        self,
        symbol: str,
        timeframe: Timeframe,
        since: Timestamp,
        until: Timestamp | None = None,
    ) -> Iterator[pd.DataFrame]:
        """Itera bloques hacia adelante hasta cubrir el rango.

        Reanudable: el llamador persiste cada bloque según llega, así que matar
        el proceso a mitad y relanzarlo continúa desde la última vela guardada.

        Si el venue deja de avanzar —devuelve siempre las mismas velas— el
        bucle corta en vez de colgarse: un backfill que no termina es peor que
        uno que falla.
        """
        tf_ms = timeframe_ms(timeframe)
        cursor = int(since)
        while True:
            block = self.fetch_ohlcv(symbol, timeframe, cursor)
            if block.empty:
                return
            block = block.loc[np.asarray(block.index, dtype="int64") >= cursor]
            if until is not None:
                block = block.loc[np.asarray(block.index, dtype="int64") <= int(until)]
            if block.empty:
                return

            yield block

            cursor = int(block.index[-1]) + tf_ms
            if until is not None and cursor > int(until):
                return

    def fetch_latest_closed(
        self, symbol: str, timeframe: Timeframe, n: int = 5
    ) -> pd.DataFrame:
        """Las ``n`` últimas velas cerradas. Lo que usa el reloj del jardín."""
        tf_ms = timeframe_ms(timeframe)
        wanted = max(1, int(n))
        since = self.now_ms() - (wanted + 2) * tf_ms
        block = self.fetch_ohlcv(symbol, timeframe, since, limit=wanted + 3)
        return block.tail(wanted)

    def market_info(self, symbol: str) -> dict[str, object]:
        """Precisión de precio y cantidad, mínimos de orden, comisiones.

        Se contrasta con ``config.frictions`` al arrancar: si el venue dice que
        la comisión es mayor que la configurada, hay que avisar, porque el
        jardín estaría evolucionando contra una fricción irreal.
        """
        exchange = self._exchange()
        markets = self._call("load_markets", exchange.load_markets)
        market = markets.get(symbol)
        if market is None:
            raise VenueError(
                f"el venue {self.venue} no lista {symbol!r}. "
                f"Revisa market.symbols en config/garden.yaml"
            )
        precision = market.get("precision") or {}
        limits = market.get("limits") or {}
        return {
            "symbol": symbol,
            "venue": self.venue,
            "active": bool(market.get("active", True)),
            "price_precision": precision.get("price"),
            "amount_precision": precision.get("amount"),
            "min_notional": (limits.get("cost") or {}).get("min"),
            "min_amount": (limits.get("amount") or {}).get("min"),
            "taker_fee_bps": float(market["taker"]) * 10_000.0 if market.get("taker") else None,
            "maker_fee_bps": float(market["maker"]) * 10_000.0 if market.get("maker") else None,
        }


__all__ = ("CCXT_COLUMNS", "MAX_LIMIT", "VenueClient", "VenueError")
