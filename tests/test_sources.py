"""Cliente del venue, sin tocar la red.

El punto crítico está en el primer bloque: la última vela que devuelve Binance
es la vela EN CURSO. Si se cuela, todo el jardín opera con información del
futuro y ningún test posterior lo detectaría.
"""

from __future__ import annotations

import numpy as np
import pytest

from conftest import build_candles
from keepgarden.data.sources import VenueClient, VenueError
from keepgarden.types import TIMEFRAME_MS

H = TIMEFRAME_MS["1h"]
START = 1_546_300_800_000      # 2019-01-01T00:00:00Z


def rows_from(df) -> list[list[float]]:
    """La serie en el formato que devuelve ``ccxt``: sin trades."""
    return [
        [int(ts), float(r.open), float(r.high), float(r.low), float(r.close), float(r.volume)]
        for ts, r in zip(df.index, df.itertuples())
    ]


class FakeExchange:
    """Un Binance de mentira: sirve velas de una lista y cuenta las llamadas."""

    def __init__(self, rows, *, fail_times: int = 0, error=None, max_limit: int = 1000):
        self.rows = rows
        self.fail_times = fail_times
        self.error = error
        self.max_limit = max_limit
        self.calls: list[tuple] = []
        self.markets_loaded = 0
        self.markets = {
            "BTC/USDT": {
                "precision": {"price": 2, "amount": 6},
                "limits": {"cost": {"min": 10.0}, "amount": {"min": 0.00001}},
                "taker": 0.001,
                "maker": 0.001,
                "active": True,
            }
        }

    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None):
        self.calls.append((symbol, timeframe, since, limit))
        if self.fail_times > 0:
            self.fail_times -= 1
            raise self.error
        n = min(limit or self.max_limit, self.max_limit)
        out = [r for r in self.rows if since is None or r[0] >= since]
        return out[:n]

    def load_markets(self, reload=False):
        self.markets_loaded += 1
        return self.markets


def client(exchange, **kwargs) -> VenueClient:
    kwargs.setdefault("sleep", lambda _: None)
    return VenueClient(exchange=exchange, **kwargs)


# --------------------------------------------------------------------------- #
# La vela en curso                                                             #
# --------------------------------------------------------------------------- #


def test_descarta_la_vela_en_curso() -> None:
    df = build_candles(10)
    ex = FakeExchange(rows_from(df))
    ahora = int(df.index[-1]) + H // 3          # la última vela sigue abierta
    c = client(ex, clock=lambda: ahora)

    out = c.fetch_ohlcv("BTC/USDT", "1h", START)

    assert int(out.index[-1]) == int(df.index[-2])
    assert len(out) == 9


def test_acepta_la_vela_justo_al_cerrar() -> None:
    df = build_candles(10)
    ex = FakeExchange(rows_from(df))
    c = client(ex, clock=lambda: int(df.index[-1]) + H)
    assert len(c.fetch_ohlcv("BTC/USDT", "1h", START)) == 10


def test_ninguna_vela_descargada_cierra_en_el_futuro() -> None:
    df = build_candles(200)
    ex = FakeExchange(rows_from(df))
    ahora = int(df.index[120]) + H + 7
    c = client(ex, clock=lambda: ahora)
    out = c.fetch_ohlcv("BTC/USDT", "1h", START)
    assert (np.asarray(out.index) + H <= ahora).all()


# --------------------------------------------------------------------------- #
# Forma del resultado                                                          #
# --------------------------------------------------------------------------- #


def test_devuelve_la_forma_canonica() -> None:
    df = build_candles(5)
    c = client(FakeExchange(rows_from(df)), clock=lambda: int(df.index[-1]) + H)
    out = c.fetch_ohlcv("BTC/USDT", "1h", START)
    assert out.index.name == "ts"
    assert out.index.dtype == np.int64
    assert list(out.columns) == ["open", "high", "low", "close", "volume", "trades"]
    assert out["trades"].isna().all()          # ccxt no expone el nº de operaciones
    assert float(out["close"].iloc[0]) == float(df["close"].iloc[0])


def test_ordena_y_deduplica_lo_que_llega_del_venue() -> None:
    df = build_candles(5)
    rows = rows_from(df)
    desordenado = [rows[2], rows[0], rows[2], rows[1], rows[4], rows[3]]
    c = client(FakeExchange(desordenado), clock=lambda: int(df.index[-1]) + H)
    out = c.fetch_ohlcv("BTC/USDT", "1h", START)
    assert len(out) == 5
    assert out.index.is_monotonic_increasing


def test_bloque_vacio_es_un_marco_vacio_no_un_error() -> None:
    c = client(FakeExchange([]), clock=lambda: START)
    out = c.fetch_ohlcv("BTC/USDT", "1h", START)
    assert out.empty
    assert out.index.name == "ts"


# --------------------------------------------------------------------------- #
# Reintentos                                                                   #
# --------------------------------------------------------------------------- #


def test_reintenta_ante_un_fallo_de_red() -> None:
    import ccxt

    df = build_candles(5)
    ex = FakeExchange(rows_from(df), fail_times=2, error=ccxt.NetworkError("cayó la red"))
    esperas: list[float] = []
    c = client(ex, clock=lambda: int(df.index[-1]) + H, sleep=esperas.append)

    assert len(c.fetch_ohlcv("BTC/USDT", "1h", START)) == 5
    assert len(ex.calls) == 3
    assert esperas == sorted(esperas) and len(esperas) == 2     # backoff creciente
    assert esperas[1] > esperas[0]


def test_se_rinde_tras_agotar_los_reintentos() -> None:
    import ccxt

    ex = FakeExchange([], fail_times=99, error=ccxt.NetworkError("sigue cayéndose"))
    c = client(ex, clock=lambda: START, max_retries=3, sleep=lambda _: None)

    with pytest.raises(VenueError) as exc:
        c.fetch_ohlcv("BTC/USDT", "1h", START)
    assert "3" in str(exc.value)
    assert len(ex.calls) == 4               # el intento original más 3 reintentos


def test_un_simbolo_inexistente_no_se_reintenta() -> None:
    import ccxt

    ex = FakeExchange([], fail_times=99, error=ccxt.BadSymbol("NOPE/USDT"))
    c = client(ex, clock=lambda: START, sleep=lambda _: None)

    with pytest.raises(VenueError):
        c.fetch_ohlcv("NOPE/USDT", "1h", START)
    assert len(ex.calls) == 1


# --------------------------------------------------------------------------- #
# Backfill paginado                                                            #
# --------------------------------------------------------------------------- #


def test_el_backfill_recorre_toda_la_serie() -> None:
    df = build_candles(2500)
    ex = FakeExchange(rows_from(df), max_limit=1000)
    c = client(ex, clock=lambda: int(df.index[-1]) + H)

    bloques = list(c.iter_backfill("BTC/USDT", "1h", START))

    assert [len(b) for b in bloques] == [1000, 1000, 500]
    juntos = np.concatenate([np.asarray(b.index) for b in bloques])
    assert (juntos == np.asarray(df.index)).all()


def test_el_backfill_no_repite_velas_entre_bloques() -> None:
    df = build_candles(1500)
    ex = FakeExchange(rows_from(df), max_limit=500)
    c = client(ex, clock=lambda: int(df.index[-1]) + H)
    vistos = [int(ts) for b in c.iter_backfill("BTC/USDT", "1h", START) for ts in b.index]
    assert len(vistos) == len(set(vistos)) == 1500


def test_el_backfill_respeta_el_limite_superior() -> None:
    df = build_candles(1000)
    hasta = int(df.index[250])
    ex = FakeExchange(rows_from(df), max_limit=100)
    c = client(ex, clock=lambda: int(df.index[-1]) + H)

    bloques = list(c.iter_backfill("BTC/USDT", "1h", START, until=hasta))

    assert max(int(b.index[-1]) for b in bloques) == hasta
    assert sum(len(b) for b in bloques) == 251


def test_el_backfill_reanuda_desde_donde_se_quedo() -> None:
    df = build_candles(300)
    ex = FakeExchange(rows_from(df), max_limit=100)
    c = client(ex, clock=lambda: int(df.index[-1]) + H)

    corte = int(df.index[120])
    resto = list(c.iter_backfill("BTC/USDT", "1h", corte + H))

    assert int(resto[0].index[0]) == int(df.index[121])
    assert sum(len(b) for b in resto) == 179


def test_el_backfill_termina_aunque_el_venue_no_avance() -> None:
    """Un venue que devuelve siempre lo mismo no puede colgar el proceso."""
    df = build_candles(5)
    rows = rows_from(df)

    class Atascado(FakeExchange):
        def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None):
            self.calls.append((symbol, timeframe, since, limit))
            return rows[:3]

    ex = Atascado(rows)
    c = client(ex, clock=lambda: int(df.index[-1]) + H)
    bloques = list(c.iter_backfill("BTC/USDT", "1h", START))
    assert len(bloques) <= 2
    assert len(ex.calls) <= 3


# --------------------------------------------------------------------------- #
# Últimas velas y metadatos                                                    #
# --------------------------------------------------------------------------- #


def test_ultimas_velas_cerradas() -> None:
    df = build_candles(100)
    ahora = int(df.index[-1]) + H // 2         # la última está en curso
    ex = FakeExchange(rows_from(df))
    c = client(ex, clock=lambda: ahora)

    out = c.fetch_latest_closed("BTC/USDT", "1h", n=3)

    assert len(out) == 3
    assert int(out.index[-1]) == int(df.index[-2])
    assert (np.asarray(out.index) + H <= ahora).all()


def test_info_de_mercado() -> None:
    ex = FakeExchange([])
    info = client(ex, clock=lambda: START).market_info("BTC/USDT")
    assert info["price_precision"] == 2
    assert info["amount_precision"] == 6
    assert info["min_notional"] == 10.0
    assert info["taker_fee_bps"] == pytest.approx(10.0)
    assert info["maker_fee_bps"] == pytest.approx(10.0)


def test_info_de_un_simbolo_que_no_existe() -> None:
    with pytest.raises(VenueError):
        client(FakeExchange([]), clock=lambda: START).market_info("NOPE/USDT")
