"""Backfill reanudable y auditoría de la caché.

El test que manda aquí es ``test_backfill_reanuda_tras_una_muerte_a_mitad``: es
el criterio de aceptación del hito 1. Un backfill de siete años se corta, y
tiene que poder continuar sin volver a empezar ni duplicar una sola vela.
"""

from __future__ import annotations

import pytest

from conftest import build_candles
from keepgarden.data.backfill import (
    backfill,
    heal,
    parse_since,
    series_status,
    status_lines,
)
from keepgarden.data.candles import validate_candles
from keepgarden.data.sources import VenueClient, VenueError
from keepgarden.data.store import CandleStore, SeriesKey
from keepgarden.types import TIMEFRAME_MS

H = TIMEFRAME_MS["1h"]
KEY = SeriesKey("binance", "BTC/USDT", "1h")


def rows_from(df) -> list[list[float]]:
    return [
        [int(ts), float(r.open), float(r.high), float(r.low), float(r.close), float(r.volume)]
        for ts, r in zip(df.index, df.itertuples())
    ]


class Venue:
    """Venue de mentira que puede caerse tras N bloques."""

    def __init__(self, rows, *, block: int = 100, die_after: int | None = None):
        self.rows = rows
        self.block = block
        self.die_after = die_after
        self.calls: list[int] = []

    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None):
        import ccxt

        self.calls.append(int(since))
        if self.die_after is not None and len(self.calls) > self.die_after:
            raise ccxt.NetworkError("el venue se cayó a mitad del backfill")
        return [r for r in self.rows if r[0] >= since][: self.block]


def client_for(df, **kwargs) -> VenueClient:
    venue = Venue(rows_from(df), **kwargs)
    return VenueClient(
        exchange=venue,
        clock=lambda: int(df.index[-1]) + H,
        sleep=lambda _: None,
        max_retries=0,
    )


def hueco(df, positions):
    keep = [i for i in range(len(df)) if i not in set(positions)]
    return df.iloc[keep]


@pytest.fixture
def store(tmp_path) -> CandleStore:
    return CandleStore(cache_dir=tmp_path)


# --------------------------------------------------------------------------- #
# Descarga                                                                     #
# --------------------------------------------------------------------------- #


def test_backfill_completo_desde_cero(store) -> None:
    df = build_candles(350)
    report = backfill(store, client_for(df), KEY, since=int(df.index[0]))

    assert report.fetched == 350
    assert report.resumed is False
    assert store.count(KEY) == 350
    assert validate_candles(store.load(KEY), "1h") == ([], [])


def test_backfill_idempotente(store) -> None:
    df = build_candles(250)
    backfill(store, client_for(df), KEY, since=int(df.index[0]))
    segundo = backfill(store, client_for(df), KEY, since=int(df.index[0]))

    assert segundo.fetched == 0
    assert segundo.up_to_date
    assert segundo.resumed is True
    assert store.count(KEY) == 250


def test_no_vuelve_a_pedir_lo_que_ya_tiene(store) -> None:
    df = build_candles(200)
    store.append(KEY, df.iloc[:50])

    venue = Venue(rows_from(df), block=100)
    cliente = VenueClient(
        exchange=venue, clock=lambda: int(df.index[-1]) + H, sleep=lambda _: None
    )
    report = backfill(store, cliente, KEY, since=int(df.index[0]))

    assert venue.calls[0] == int(df.index[49]) + H
    assert report.resumed_from == int(df.index[49]) + H
    assert report.fetched == 150
    assert store.count(KEY) == 200


def test_backfill_reanuda_tras_una_muerte_a_mitad(store) -> None:
    """El criterio del roadmap: matarlo a mitad y relanzarlo termina el trabajo."""
    df = build_candles(1000)
    desde = int(df.index[0])

    with pytest.raises(VenueError):
        backfill(store, client_for(df, block=100, die_after=3), KEY, since=desde)

    a_medias = store.count(KEY)
    assert 0 < a_medias < 1000

    report = backfill(store, client_for(df, block=100), KEY, since=desde)

    assert report.resumed is True
    assert report.resumed_from == int(df.index[a_medias - 1]) + H
    assert store.count(KEY) == 1000
    assert report.fetched == 1000 - a_medias
    recuperado = store.load(KEY)
    assert not recuperado.index.duplicated().any()
    assert validate_candles(recuperado, "1h") == ([], [])


def test_backfill_de_una_serie_vacia_no_explota(store) -> None:
    df = build_candles(10)
    venue = Venue([], block=100)
    cliente = VenueClient(exchange=venue, clock=lambda: int(df.index[0]), sleep=lambda _: None)
    report = backfill(store, cliente, KEY, since=int(df.index[0]))
    assert report.fetched == 0
    assert report.n_candles == 0
    assert store.bounds(KEY) is None


# --------------------------------------------------------------------------- #
# Saneamiento                                                                  #
# --------------------------------------------------------------------------- #


def test_rellena_el_hueco_corto_y_lo_marca(store) -> None:
    df = build_candles(200)
    roto = hueco(df, [80])
    report = backfill(store, client_for(roto), KEY, since=int(df.index[0]))

    assert report.heal.filled == 1
    assert [g.n_missing for g in report.heal.short_gaps] == [1]
    assert store.count(KEY) == 200
    marcados = store.marked_gaps(KEY)
    assert [(g["n_missing"], g["filled"]) for g in marcados] == [(1, True)]
    rellenada = store.load(KEY).loc[int(df.index[80])]
    assert float(rellenada["volume"]) == 0.0
    assert series_status(store, KEY).ok


def test_el_hueco_largo_se_marca_pero_no_se_inventa(store) -> None:
    df = build_candles(200)
    roto = hueco(df, [80, 81, 82, 83])
    report = backfill(store, client_for(roto), KEY, since=int(df.index[0]))

    assert report.heal.filled == 0
    assert [g.n_missing for g in report.heal.long_gaps] == [4]
    assert store.count(KEY) == 196
    assert [(g["n_missing"], g["filled"]) for g in store.marked_gaps(KEY)] == [(4, False)]

    status = series_status(store, KEY)
    assert [g.n_missing for g in status.gaps] == [4]
    assert status.unmarked_gaps == []
    assert status.missing == 4
    assert status.ok


def test_sanear_dos_veces_no_cambia_nada(store) -> None:
    df = build_candles(200)
    backfill(store, client_for(hueco(df, [40, 90, 91, 92])), KEY, since=int(df.index[0]))
    antes = store.manifest(KEY)["hash"]
    marcados = store.marked_gaps(KEY)

    segundo = heal(store, KEY)

    assert segundo.filled == 0
    assert store.manifest(KEY)["hash"] == antes
    assert store.marked_gaps(KEY) == marcados


def test_las_anomalias_se_registran_no_se_borran(store) -> None:
    df = build_candles(100).copy()
    df.iloc[50, df.columns.get_loc("close")] = float(df["close"].iloc[49]) * 3.0
    report = backfill(store, client_for(df), KEY, since=int(df.index[0]))

    kinds = [a.kind for a in report.heal.anomalies]
    # el cierre disparatado deja la vela incoherente y provoca el salto de ida y vuelta
    assert kinds.count("price_jump") == 2
    assert "bad_ohlc" in kinds
    assert store.count(KEY) == 100          # no se ha tirado ninguna vela


# --------------------------------------------------------------------------- #
# Estado                                                                       #
# --------------------------------------------------------------------------- #


def test_status_de_una_serie_sana(store) -> None:
    df = build_candles(500)
    backfill(store, client_for(df), KEY, since=int(df.index[0]))

    s = series_status(store, KEY)

    assert s.n_candles == 500
    assert s.expected == 500
    assert s.coverage == 1.0
    assert s.missing == 0
    assert s.gaps == []
    assert s.unmarked_gaps == []
    assert s.ok


def test_status_denuncia_un_hueco_que_nadie_ha_mirado(store) -> None:
    """Un hueco que aparece sin pasar por el backfill no puede colarse."""
    df = build_candles(100)
    store.append(KEY, hueco(df, [30, 31, 32]))

    s = series_status(store, KEY)

    assert [g.n_missing for g in s.unmarked_gaps] == [3]
    assert not s.ok
    assert any("SIN MARCAR" in line for line in status_lines(s))


def test_status_de_una_serie_que_no_existe(store) -> None:
    s = series_status(store, SeriesKey("binance", "ETH/USDT", "1h"))
    assert s.n_candles == 0
    assert s.coverage == 1.0
    assert s.ok


def test_status_cuenta_las_anomalias_por_tipo(store) -> None:
    df = build_candles(50).copy()
    df.iloc[10, df.columns.get_loc("volume")] = 0.0
    df.iloc[20, df.columns.get_loc("volume")] = -5.0
    store.append(KEY, df)

    s = series_status(store, KEY)

    assert s.anomalies_by_kind() == {"zero_volume": 1, "negative_volume": 1}
    assert not s.ok                          # el volumen negativo es estructural


def test_status_lines_resume_la_serie(store) -> None:
    df = build_candles(120)
    backfill(store, client_for(df), KEY, since=int(df.index[0]))
    texto = "\n".join(status_lines(series_status(store, KEY)))
    assert "binance:BTC/USDT:1h" in texto
    assert "120" in texto
    assert "ninguna" in texto


# --------------------------------------------------------------------------- #
# Fechas                                                                       #
# --------------------------------------------------------------------------- #


def test_parse_since_acepta_fecha_simple() -> None:
    assert parse_since("2019-01-01") == 1_546_300_800_000


def test_parse_since_acepta_iso_completo_con_z() -> None:
    assert parse_since("2019-01-01T00:00:00Z") == 1_546_300_800_000


def test_parse_since_se_queja_de_una_fecha_absurda() -> None:
    with pytest.raises(ValueError, match="ISO"):
        parse_since("el martes")


# --------------------------------------------------------------------------- #
# Tramos a descargar                                                           #
# --------------------------------------------------------------------------- #


def test_sin_cache_se_pide_todo_de_una() -> None:
    from keepgarden.data.backfill import ranges_to_fetch

    assert ranges_to_fetch(None, 1000, None, H) == [(1000, None)]


def test_con_cache_solo_se_pide_la_cola() -> None:
    from keepgarden.data.backfill import ranges_to_fetch

    assert ranges_to_fetch((1000, 5000), 1000, None, H) == [(5000 + H, None)]


def test_pedir_mas_historia_anade_un_tramo_por_delante() -> None:
    from keepgarden.data.backfill import ranges_to_fetch

    tramos = ranges_to_fetch((5000, 9000), 1000, None, H)
    assert tramos == [(1000, 5000 - H), (9000 + H, None)]


def test_la_cola_no_se_pide_si_ya_pasa_del_limite() -> None:
    from keepgarden.data.backfill import ranges_to_fetch

    assert ranges_to_fetch((1000, 9000), 1000, 9000, H) == []


def test_pedir_mas_historia_la_descarga_de_verdad(store) -> None:
    """El error que sólo se ve con datos reales: tener el último mes en caché y
    pedir desde 2019 no puede quedarse en 'ya estaba al día'."""
    df = build_candles(500)
    reciente = df.iloc[400:]
    store.append(KEY, reciente)

    report = backfill(store, client_for(df), KEY, since=int(df.index[0]))

    assert report.extended_backwards
    assert report.fetched == 400
    assert store.count(KEY) == 500
    assert int(store.bounds(KEY)[0]) == int(df.index[0])
    assert validate_candles(store.load(KEY), "1h") == ([], [])


def test_las_velas_de_relleno_no_se_cuentan_como_anomalias(store) -> None:
    df = build_candles(200)
    backfill(store, client_for(hueco(df, [80, 120, 121])), KEY, since=int(df.index[0]))

    s = series_status(store, KEY)

    assert s.synthetic == 3
    assert "zero_volume" not in s.anomalies_by_kind()
    assert s.ok
    assert any("sintéticas" in line for line in status_lines(s))


def test_un_volumen_cero_de_verdad_si_es_anomalia(store) -> None:
    df = build_candles(50).copy()
    df.iloc[10, df.columns.get_loc("volume")] = 0.0
    store.append(KEY, df)

    s = series_status(store, KEY)

    assert s.synthetic == 0
    assert s.anomalies_by_kind() == {"zero_volume": 1}
