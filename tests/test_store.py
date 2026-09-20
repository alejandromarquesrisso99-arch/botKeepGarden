"""Caché de velas en Parquet: idempotencia, particionado y manifiesto.

El backfill se mata y se relanza constantemente. Si ``append`` no es idempotente
al 100 %, la serie acumula duplicados que después envenenan cualquier métrica.
"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from keepgarden.data.candles import Gap, validate_candles
from keepgarden.data.store import CandleStore, SeriesKey
from keepgarden.types import TIMEFRAME_MS

H = TIMEFRAME_MS["1h"]
KEY = SeriesKey("binance", "BTC/USDT", "1h")


def ms(iso: str) -> int:
    return int(pd.Timestamp(iso, tz="UTC").timestamp() * 1000)


@pytest.fixture
def store(tmp_path) -> CandleStore:
    return CandleStore(cache_dir=tmp_path)


# --------------------------------------------------------------------------- #
# Ida y vuelta                                                                 #
# --------------------------------------------------------------------------- #


def test_guardar_y_recuperar_identico(store, candles) -> None:
    df = candles(100)
    assert store.append(KEY, df) == 100
    pd.testing.assert_frame_equal(store.load(KEY), df)


def test_la_serie_vacia_no_crea_nada(store) -> None:
    assert store.append(KEY, pd.DataFrame()) == 0
    assert store.load(KEY).empty
    assert store.bounds(KEY) is None
    assert store.count(KEY) == 0


def test_serie_desconocida_devuelve_marco_canonico(store) -> None:
    out = store.load(SeriesKey("binance", "ETH/USDT", "1h"))
    assert out.empty
    assert out.index.name == "ts"
    assert list(out.columns) == ["open", "high", "low", "close", "volume", "trades"]


# --------------------------------------------------------------------------- #
# Idempotencia                                                                 #
# --------------------------------------------------------------------------- #


def test_reescribir_el_mismo_bloque_no_duplica(store, candles) -> None:
    df = candles(50)
    assert store.append(KEY, df) == 50
    assert store.append(KEY, df) == 0
    assert store.count(KEY) == 50
    assert validate_candles(store.load(KEY), "1h") == ([], [])


def test_bloques_solapados_solo_cuentan_lo_nuevo(store, candles) -> None:
    df = candles(100)
    store.append(KEY, df.iloc[:60])
    assert store.append(KEY, df.iloc[40:]) == 40
    assert store.count(KEY) == 100
    pd.testing.assert_frame_equal(store.load(KEY), df)


def test_una_vela_corregida_pisa_a_la_antigua(store, candles) -> None:
    """Rebajar un backfill debe poder arreglar una vela mala, no conservarla."""
    df = candles(20)
    store.append(KEY, df)
    corregida = df.iloc[[5]].copy()
    corregida.iloc[0, corregida.columns.get_loc("close")] = 999.0
    assert store.append(KEY, corregida) == 0          # no es una vela nueva
    assert float(store.load(KEY)["close"].iloc[5]) == 999.0
    assert store.count(KEY) == 20


def test_el_orden_de_llegada_da_igual(store, candles) -> None:
    df = candles(30)
    store.append(KEY, df.iloc[20:])
    store.append(KEY, df.iloc[:10])
    store.append(KEY, df.iloc[10:20])
    pd.testing.assert_frame_equal(store.load(KEY), df)


# --------------------------------------------------------------------------- #
# Particionado por año                                                         #
# --------------------------------------------------------------------------- #


def test_particiona_por_ano(store, candles) -> None:
    df = candles(72, start_ts=ms("2019-12-30T00:00:00"))
    store.append(KEY, df)
    años = sorted(p.stem for p in store.series_dir(KEY).glob("*.parquet"))
    assert años == ["2019", "2020"]
    assert store.count(KEY) == 72


def test_solo_toca_los_anos_implicados(store, candles) -> None:
    df = candles(72, start_ts=ms("2019-12-30T00:00:00"))
    store.append(KEY, df)
    antes = store.series_dir(KEY).joinpath("2019.parquet").stat().st_mtime_ns
    store.append(KEY, candles(10, start_ts=ms("2020-03-01T00:00:00")))
    assert store.series_dir(KEY).joinpath("2019.parquet").stat().st_mtime_ns == antes


def test_la_ruta_sigue_el_esquema_documentado(store) -> None:
    partes = store.series_dir(KEY).relative_to(store.root).parts
    assert partes == ("BINANCE", "BTC_USDT", "1h")


# --------------------------------------------------------------------------- #
# Carga por rango                                                              #
# --------------------------------------------------------------------------- #


def test_carga_un_rango_inclusivo(store, candles) -> None:
    df = candles(100)
    store.append(KEY, df)
    out = store.load(KEY, start=int(df.index[10]), end=int(df.index[20]))
    assert len(out) == 11
    assert int(out.index[0]) == int(df.index[10])
    assert int(out.index[-1]) == int(df.index[20])


def test_carga_un_rango_a_caballo_entre_dos_anos(store, candles) -> None:
    df = candles(72, start_ts=ms("2019-12-30T00:00:00"))
    store.append(KEY, df)
    out = store.load(KEY, start=ms("2019-12-31T20:00:00"), end=ms("2020-01-01T03:00:00"))
    assert int(out.index[0]) == ms("2019-12-31T20:00:00")
    assert int(out.index[-1]) == ms("2020-01-01T03:00:00")
    assert len(out) == 8


def test_carga_fuera_de_rango_devuelve_vacio(store, candles) -> None:
    store.append(KEY, candles(10))
    assert store.load(KEY, start=ms("2030-01-01T00:00:00")).empty


# --------------------------------------------------------------------------- #
# Manifiesto                                                                   #
# --------------------------------------------------------------------------- #


def test_el_manifiesto_resume_la_serie(store, candles) -> None:
    df = candles(100)
    store.append(KEY, df)
    entry = store.manifest(KEY)
    assert entry is not None
    assert entry["first_ts"] == int(df.index[0])
    assert entry["last_ts"] == int(df.index[-1])
    assert entry["n_candles"] == 100
    assert entry["hash"]
    assert entry["symbol"] == "BTC/USDT"


def test_bounds_y_count_salen_del_manifiesto_sin_leer_parquet(store, candles, monkeypatch) -> None:
    df = candles(100)
    store.append(KEY, df)
    otro = CandleStore(cache_dir=store.cache_dir)
    monkeypatch.setattr(
        pd, "read_parquet", lambda *a, **k: pytest.fail("no debería leer Parquet")
    )
    assert otro.bounds(KEY) == (int(df.index[0]), int(df.index[-1]))
    assert otro.count(KEY) == 100


def test_el_manifiesto_se_reconstruye_si_se_pierde(store, candles) -> None:
    df = candles(72, start_ts=ms("2019-12-30T00:00:00"))
    store.append(KEY, df)
    esperado = store.manifest(KEY)

    store.manifest_path.unlink()
    recuperado = CandleStore(cache_dir=store.cache_dir).rebuild_manifest(KEY)

    assert recuperado["first_ts"] == esperado["first_ts"]
    assert recuperado["last_ts"] == esperado["last_ts"]
    assert recuperado["n_candles"] == esperado["n_candles"]
    assert recuperado["hash"] == esperado["hash"]


def test_el_manifiesto_corrupto_no_tumba_la_carga(store, candles) -> None:
    store.append(KEY, candles(20))
    store.manifest_path.write_text("{no es json", encoding="utf-8")
    otro = CandleStore(cache_dir=store.cache_dir)
    assert len(otro.load(KEY)) == 20
    assert otro.rebuild_manifest(KEY)["n_candles"] == 20


def test_el_hash_cambia_si_cambia_una_vela(store, candles) -> None:
    df = candles(20)
    store.append(KEY, df)
    antes = store.manifest(KEY)["hash"]
    corregida = df.iloc[[5]].copy()
    corregida.iloc[0, corregida.columns.get_loc("close")] = 999.0
    store.append(KEY, corregida)
    assert store.manifest(KEY)["hash"] != antes


def test_el_manifiesto_es_json_legible(store, candles) -> None:
    store.append(KEY, candles(10))
    raw = json.loads(store.manifest_path.read_text(encoding="utf-8"))
    assert "BINANCE/BTC_USDT/1h" in raw["series"]


# --------------------------------------------------------------------------- #
# Huecos marcados                                                              #
# --------------------------------------------------------------------------- #


def test_marcar_huecos_y_recuperarlos(store, candles) -> None:
    store.append(KEY, candles(10))
    gap = Gap(start_ts=ms("2019-01-01T05:00:00"), end_ts=ms("2019-01-01T06:00:00"), n_missing=2)
    assert store.mark_gaps(KEY, [gap], filled=True, note="parada de Binance") == 1
    marcados = store.marked_gaps(KEY)
    assert [(g["start_ts"], g["n_missing"], g["filled"]) for g in marcados] == [
        (gap.start_ts, 2, True)
    ]
    assert store.is_marked(KEY, gap)


def test_marcar_el_mismo_hueco_dos_veces_no_lo_duplica(store, candles) -> None:
    store.append(KEY, candles(10))
    gap = Gap(ms("2019-01-01T05:00:00"), ms("2019-01-01T05:00:00"), 1)
    store.mark_gaps(KEY, [gap])
    assert store.mark_gaps(KEY, [gap]) == 0
    assert len(store.marked_gaps(KEY)) == 1


def test_un_hueco_sin_marcar_no_figura(store, candles) -> None:
    store.append(KEY, candles(10))
    gap = Gap(ms("2019-01-01T05:00:00"), ms("2019-01-01T05:00:00"), 1)
    assert not store.is_marked(KEY, gap)


def test_las_marcas_sobreviven_a_reconstruir_el_manifiesto(store, candles) -> None:
    store.append(KEY, candles(10))
    gap = Gap(ms("2019-01-01T05:00:00"), ms("2019-01-01T05:00:00"), 1)
    store.mark_gaps(KEY, [gap], filled=True)
    store.rebuild_manifest(KEY)
    assert len(store.marked_gaps(KEY)) == 1


# --------------------------------------------------------------------------- #
# Inventario                                                                   #
# --------------------------------------------------------------------------- #


def test_keys_lista_lo_que_hay_en_cache(store, candles) -> None:
    store.append(KEY, candles(5))
    store.append(SeriesKey("binance", "BTC/USDT", "1d"), candles(5, timeframe="1d"))
    assert sorted(str(k) for k in store.series_keys()) == [
        "binance:BTC/USDT:1d",
        "binance:BTC/USDT:1h",
    ]


def test_keys_se_deduce_del_disco_sin_manifiesto(store, candles) -> None:
    store.append(KEY, candles(5))
    store.manifest_path.unlink()
    assert [str(k) for k in CandleStore(cache_dir=store.cache_dir).series_keys()] == [
        "binance:BTC/USDT:1h"
    ]
