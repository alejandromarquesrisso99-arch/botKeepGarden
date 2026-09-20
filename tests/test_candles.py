"""Velas: continuidad, anomalías y —sobre todo— causalidad.

Los tres últimos bloques de este archivo son la barrera contra el look-ahead.
Si alguno se pone rojo, todo backtest posterior miente.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from keepgarden.data.candles import (
    COLUMNS,
    Gap,
    align_context,
    ensure_canonical,
    fill_short_gaps,
    slice_causal,
    validate_candles,
)
from keepgarden.types import TIMEFRAME_MS

H = TIMEFRAME_MS["1h"]
D = TIMEFRAME_MS["1d"]


def drop_ts(df: pd.DataFrame, positions) -> pd.DataFrame:
    """Quita velas por posición, dejando un hueco real."""
    keep = [i for i in range(len(df)) if i not in set(positions)]
    return df.iloc[keep]


# --------------------------------------------------------------------------- #
# Forma canónica                                                               #
# --------------------------------------------------------------------------- #


def test_forma_canonica_desde_columna_ts(candles) -> None:
    df = candles(10).reset_index()          # ts vuelve a ser columna
    out = ensure_canonical(df)
    assert out.index.name == "ts"
    assert out.index.dtype == np.int64
    assert list(out.columns) == [c for c in COLUMNS if c != "ts"]
    assert out.index.is_monotonic_increasing


def test_forma_canonica_ordena_y_no_muta_el_original(candles) -> None:
    df = candles(10).iloc[::-1]             # al revés
    original = df.copy()
    out = ensure_canonical(df)
    assert out.index.is_monotonic_increasing
    pd.testing.assert_frame_equal(df, original)


def test_forma_canonica_acepta_vacio() -> None:
    out = ensure_canonical(pd.DataFrame())
    assert out.empty
    assert out.index.name == "ts"
    assert list(out.columns) == [c for c in COLUMNS if c != "ts"]


# --------------------------------------------------------------------------- #
# Validación                                                                   #
# --------------------------------------------------------------------------- #


def test_serie_limpia_no_tiene_problemas(candles) -> None:
    gaps, anomalies = validate_candles(candles(500), "1h")
    assert gaps == []
    assert anomalies == []


def test_detecta_hueco_y_lo_describe_por_las_velas_que_faltan(candles) -> None:
    df = candles(20)
    faltan = df.index[[7, 8, 9]]
    gaps, _ = validate_candles(drop_ts(df, [7, 8, 9]), "1h")
    assert gaps == [Gap(start_ts=int(faltan[0]), end_ts=int(faltan[-1]), n_missing=3)]
    assert not gaps[0].is_short


def test_hueco_de_una_vela_es_corto(candles) -> None:
    df = candles(20)
    gaps, _ = validate_candles(drop_ts(df, [5]), "1h")
    assert [g.n_missing for g in gaps] == [1]
    assert gaps[0].is_short


def test_varios_huecos_en_orden(candles) -> None:
    df = candles(40)
    gaps, _ = validate_candles(drop_ts(df, [3, 10, 11]), "1h")
    assert [g.n_missing for g in gaps] == [1, 2]
    assert gaps[0].start_ts < gaps[1].start_ts


def test_detecta_duplicados(candles) -> None:
    df = candles(10)
    doble = pd.concat([df, df.iloc[[4]]]).sort_index()
    _, anomalies = validate_candles(doble, "1h")
    kinds = [a.kind for a in anomalies]
    assert "duplicate" in kinds
    assert [a.ts for a in anomalies if a.kind == "duplicate"] == [int(df.index[4])]


def test_detecta_ohlc_imposible(candles) -> None:
    df = candles(10).copy()
    df.iloc[3, df.columns.get_loc("high")] = df["low"].iloc[3] - 1.0
    _, anomalies = validate_candles(df, "1h")
    assert [(a.ts, a.kind) for a in anomalies] == [(int(df.index[3]), "bad_ohlc")]


def test_detecta_high_por_debajo_del_cierre(candles) -> None:
    df = candles(10).copy()
    df.iloc[6, df.columns.get_loc("high")] = df["close"].iloc[6] - 0.01
    _, anomalies = validate_candles(df, "1h")
    assert [a.kind for a in anomalies] == ["bad_ohlc"]


def test_detecta_volumen_negativo(candles) -> None:
    df = candles(10).copy()
    df.iloc[2, df.columns.get_loc("volume")] = -1.0
    _, anomalies = validate_candles(df, "1h")
    assert [a.kind for a in anomalies] == ["negative_volume"]


def test_detecta_volumen_cero(candles) -> None:
    df = candles(10).copy()
    df.iloc[2, df.columns.get_loc("volume")] = 0.0
    _, anomalies = validate_candles(df, "1h")
    assert [a.kind for a in anomalies] == ["zero_volume"]


def test_detecta_salto_de_precio(candles) -> None:
    df = candles(10).copy()
    col = df.columns.get_loc("close")
    df.iloc[5, col] = df["close"].iloc[4] * 2.0
    _, anomalies = validate_candles(df, "1h", jump_threshold=0.40)
    saltos = [a for a in anomalies if a.kind == "price_jump"]
    # dos: el salto hacia arriba y la vuelta a la normalidad en la vela siguiente
    assert [a.ts for a in saltos] == [int(df.index[5]), int(df.index[6])]


def test_el_umbral_de_salto_se_respeta(candles) -> None:
    df = candles(10).copy()
    df.iloc[5, df.columns.get_loc("close")] = df["close"].iloc[4] * 1.30
    _, con_umbral_alto = validate_candles(df, "1h", jump_threshold=0.40)
    _, con_umbral_bajo = validate_candles(df, "1h", jump_threshold=0.20)
    assert not [a for a in con_umbral_alto if a.kind == "price_jump"]
    assert [a for a in con_umbral_bajo if a.kind == "price_jump"]


def test_validar_no_toca_el_dataframe(candles) -> None:
    df = drop_ts(candles(20), [4])
    original = df.copy()
    validate_candles(df, "1h")
    pd.testing.assert_frame_equal(df, original)


# --------------------------------------------------------------------------- #
# Relleno de huecos cortos                                                     #
# --------------------------------------------------------------------------- #


def test_rellena_huecos_cortos_con_volumen_cero(candles) -> None:
    df = candles(20)
    cierre_previo = float(df["close"].iloc[5])
    roto = drop_ts(df, [6])
    gaps, _ = validate_candles(roto, "1h")
    lleno = fill_short_gaps(roto, gaps, "1h")

    assert len(lleno) == len(df)
    vela = lleno.loc[int(df.index[6])]
    assert float(vela["volume"]) == 0.0
    assert float(vela["trades"]) == 0.0
    assert float(vela["open"]) == cierre_previo
    assert float(vela["high"]) == cierre_previo
    assert float(vela["low"]) == cierre_previo
    assert float(vela["close"]) == cierre_previo
    assert validate_candles(lleno, "1h")[0] == []


def test_no_rellena_huecos_largos(candles) -> None:
    df = candles(30)
    roto = drop_ts(df, [10, 11, 12, 13])
    gaps, _ = validate_candles(roto, "1h")
    lleno = fill_short_gaps(roto, gaps, "1h")
    assert len(lleno) == len(roto)
    assert [g.n_missing for g in validate_candles(lleno, "1h")[0]] == [4]


def test_rellena_solo_los_cortos_cuando_hay_de_los_dos(candles) -> None:
    df = candles(40)
    roto = drop_ts(df, [5, 20, 21, 22])
    gaps, _ = validate_candles(roto, "1h")
    lleno = fill_short_gaps(roto, gaps, "1h")
    assert len(lleno) == len(roto) + 1
    assert [g.n_missing for g in validate_candles(lleno, "1h")[0]] == [3]


def test_rellenar_no_muta_el_original(candles) -> None:
    roto = drop_ts(candles(20), [6])
    original = roto.copy()
    gaps, _ = validate_candles(roto, "1h")
    fill_short_gaps(roto, gaps, "1h")
    pd.testing.assert_frame_equal(roto, original)


# --------------------------------------------------------------------------- #
# Causalidad                                                                   #
# --------------------------------------------------------------------------- #


def test_slice_causal_excluye_la_vela_en_curso(candles) -> None:
    df = candles(10)
    en_curso = int(df.index[-1])
    # estamos a mitad de la última vela
    out = slice_causal(df, en_curso + H // 2)
    assert int(out.index[-1]) == int(df.index[-2])


def test_slice_causal_incluye_la_vela_justo_al_cerrar(candles) -> None:
    df = candles(10)
    ultima = int(df.index[-1])
    out = slice_causal(df, ultima + H)
    assert int(out.index[-1]) == ultima


@pytest.mark.parametrize("avance", [0, 1, H - 1, H, H + 1, 5 * H, 50 * H])
def test_ninguna_vela_devuelta_cierra_en_el_futuro(candles, avance: int) -> None:
    """El criterio del roadmap, hito 1: nunca una vela con close_time > now."""
    df = candles(200)
    now = int(df.index[0]) + avance
    out = slice_causal(df, now)
    assert (out.index + H <= now).all()
    # y no se deja ninguna cerrada fuera
    assert len(out) == int((df.index + H <= now).sum())


def test_slice_causal_vacio_antes_del_primer_cierre(candles) -> None:
    df = candles(10)
    assert slice_causal(df, int(df.index[0])).empty


def test_slice_causal_funciona_con_huecos(candles) -> None:
    df = drop_ts(candles(50), [10, 11, 12])
    now = int(df.index[30]) + H
    out = slice_causal(df, now)
    assert int(out.index[-1]) == int(df.index[30])


def test_slice_causal_acepta_timeframe_explicito(candles) -> None:
    df = candles(3, timeframe="1d")
    out = slice_causal(df.iloc[[0]], int(df.index[0]) + D, timeframe="1d")
    assert len(out) == 1


# --------------------------------------------------------------------------- #
# Alineación de contexto                                                       #
# --------------------------------------------------------------------------- #


def test_la_diaria_disponible_a_las_13_es_la_de_ayer(candles) -> None:
    base = candles(48, timeframe="1h")              # dos días desde 2019-01-01
    ctx = candles(3, timeframe="1d", first_close=1000.0, step=100.0)
    alineado = align_context(base, ctx, "1d")

    ayer = float(ctx["close"].iloc[0])              # vela diaria de 2019-01-01
    a_las_13_del_dia_2 = int(base.index[24 + 13])   # 2019-01-02T13:00Z
    assert float(alineado.loc[a_las_13_del_dia_2, "close"]) == ayer


def test_el_contexto_nunca_adelanta_su_propio_cierre(candles) -> None:
    """Invariante duro: la vela de contexto usada en ``t`` cerró en ``t`` o antes."""
    base = candles(240, timeframe="1h")
    ctx = candles(12, timeframe="1d", first_close=1000.0, step=100.0)
    alineado = align_context(base, ctx, "1d")

    cierre_por_valor = {float(c): int(t) + D for t, c in zip(ctx.index, ctx["close"])}
    usadas = alineado["close"].dropna()
    for base_ts, valor in zip(usadas.index, usadas):
        assert cierre_por_valor[float(valor)] <= int(base_ts)


def test_el_contexto_sin_historia_previa_queda_en_nan(candles) -> None:
    base = candles(24, timeframe="1h")
    ctx = candles(3, timeframe="1d", first_close=1000.0, step=100.0)
    alineado = align_context(base, ctx, "1d")
    # el primer día no tiene ninguna diaria cerrada por detrás
    assert alineado["close"].isna().all()


def test_el_contexto_se_alinea_sobre_el_indice_de_la_base(candles) -> None:
    base = candles(100, timeframe="1h")
    ctx = candles(10, timeframe="4h", first_close=500.0)
    alineado = align_context(base, ctx, "4h")
    assert alineado.index.equals(base.index)
    assert list(alineado.columns) == list(ctx.columns)
