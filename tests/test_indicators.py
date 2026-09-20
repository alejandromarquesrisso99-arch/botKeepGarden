"""Indicadores: causalidad, valores conocidos y caché.

El primer bloque es el criterio de aceptación del hito 2 y el más importante del
archivo: calcular un indicador sobre las primeras ``n`` velas tiene que dar
exactamente lo mismo que calcularlo sobre ``n + 50`` y quedarse con las ``n``
primeras. Si eso no se cumple, el indicador está mirando al futuro.
"""

from __future__ import annotations

import numpy as np
import pytest

from conftest import build_candles
from keepgarden.data.indicators import (
    REGISTRY,
    IndicatorCache,
    compute,
    warmup_bars,
)
from keepgarden.genome.catalog import INDICATORS, spec
from keepgarden.types import PriceField

KINDS = sorted(INDICATORS)


def params_low(kind: str) -> dict[str, float]:
    """Los parámetros más pequeños válidos: calientan rápido y dan señal pronto."""
    return {p.name: p.low for p in spec(kind).params}


def params_mid(kind: str) -> dict[str, float]:
    """Un juego de parámetros mayor, para ejercitar otra rama de cada fórmula."""
    return {p.name: p.clamp(min(p.high, p.low * 3)) for p in spec(kind).params}


def default_source(kind: str) -> PriceField:
    return spec(kind).sources[0]


# --------------------------------------------------------------------------- #
# Causalidad                                                                   #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("params_fn", [params_low, params_mid], ids=["low", "mid"])
def test_ningun_indicador_mira_al_futuro(kind: str, params_fn) -> None:
    """Criterio de aceptación del hito 2, para todo el catálogo."""
    velas = build_candles(400)
    params = params_fn(kind)
    src = default_source(kind)

    corto = compute(kind, velas.iloc[:250], params, source=src)
    largo = compute(kind, velas, params, source=src)

    np.testing.assert_array_equal(
        np.isnan(corto), np.isnan(largo[:250]), err_msg=f"{kind}: NaN distintos"
    )
    np.testing.assert_allclose(
        corto[~np.isnan(corto)],
        largo[:250][~np.isnan(largo[:250])],
        rtol=0,
        atol=0,
        err_msg=f"{kind}: el pasado cambia al conocer el futuro",
    )


@pytest.mark.parametrize("kind", KINDS)
def test_todo_indicador_del_catalogo_esta_implementado(kind: str) -> None:
    assert kind in REGISTRY, f"{kind} está en el catálogo pero no tiene implementación"


@pytest.mark.parametrize("kind", KINDS)
def test_la_salida_tiene_la_forma_de_la_serie(kind: str) -> None:
    velas = build_candles(300)
    out = compute(kind, velas, params_low(kind), source=default_source(kind))
    assert out.shape == (300,)
    assert out.dtype == np.float64


@pytest.mark.parametrize("kind", KINDS)
def test_todo_indicador_calienta_y_luego_da_valores(kind: str) -> None:
    velas = build_candles(600)
    out = compute(kind, velas, params_low(kind), source=default_source(kind))
    calentamiento = warmup_bars(kind, params_low(kind))
    assert np.isfinite(out[calentamiento:]).all(), f"{kind}: NaN después del calentamiento"


@pytest.mark.parametrize("kind", KINDS)
def test_todo_indicador_admite_sus_fuentes_declaradas(kind: str) -> None:
    velas = build_candles(200)
    for source in spec(kind).sources:
        out = compute(kind, velas, params_low(kind), source=source)
        assert out.shape == (200,)


# --------------------------------------------------------------------------- #
# Valores conocidos                                                            #
# --------------------------------------------------------------------------- #


def serie(valores: list[float]):
    """Velas planas con los cierres dados. Para comprobar fórmulas a mano."""
    import pandas as pd

    from keepgarden.types import TIMEFRAME_MS

    n = len(valores)
    ts = np.arange(n, dtype="int64") * TIMEFRAME_MS["1h"] + 1_546_300_800_000
    c = np.asarray(valores, dtype="float64")
    return pd.DataFrame(
        {
            "open": c,
            "high": c,
            "low": c,
            "close": c,
            "volume": np.full(n, 100.0),
            "trades": np.full(n, 10.0),
        },
        index=pd.Index(ts, name="ts"),
    )


def test_sma_es_la_media_de_la_ventana() -> None:
    out = compute("SMA", serie([1, 2, 3, 4, 5]), {"period": 3})
    assert np.isnan(out[0]) and np.isnan(out[1])
    np.testing.assert_allclose(out[2:], [2.0, 3.0, 4.0])


def test_ema_arranca_en_la_media_simple() -> None:
    out = compute("EMA", serie([1, 2, 3, 4, 5, 6]), {"period": 3})
    # los dos primeros son calentamiento; el tercero es la SMA(3) inicial
    assert np.isnan(out[0]) and np.isnan(out[1])
    assert out[2] == pytest.approx(2.0)
    assert out[3] == pytest.approx(2.0 + 0.5 * (4 - 2.0))


def test_wma_pondera_linealmente() -> None:
    out = compute("WMA", serie([1, 2, 3]), {"period": 3})
    assert out[2] == pytest.approx((1 * 1 + 2 * 2 + 3 * 3) / 6)


def test_rsi_de_una_subida_constante_es_cien() -> None:
    out = compute("RSI", serie(list(range(1, 40))), {"period": 14})
    assert out[-1] == pytest.approx(100.0)


def test_rsi_de_una_bajada_constante_es_cero() -> None:
    out = compute("RSI", serie(list(range(40, 1, -1))), {"period": 14})
    assert out[-1] == pytest.approx(0.0)


def test_roc_es_una_fraccion_no_un_porcentaje() -> None:
    out = compute("ROC", serie([100, 100, 110]), {"period": 2})
    assert out[2] == pytest.approx(0.10)


def test_zscore_de_una_serie_plana_es_cero_y_no_infinito() -> None:
    out = compute("ZSCORE", serie([5.0] * 40), {"period": 10})
    assert np.isfinite(out[-1])
    assert out[-1] == pytest.approx(0.0)


def test_atr_de_velas_planas_mide_el_salto_entre_cierres() -> None:
    # la primera vela no tiene cierre anterior, así que su rango verdadero es 0
    # y la media de Wilder converge al salto real desde abajo
    out = compute("ATR", serie(list(range(10, 80, 2))), {"period": 3})
    assert out[-1] == pytest.approx(2.0, rel=1e-6)
    assert out[2] == pytest.approx((0.0 + 2.0 + 2.0) / 3.0)


def test_obv_acumula_volumen_con_el_signo_del_cierre() -> None:
    out = compute("OBV", serie([10, 11, 10, 12]))
    np.testing.assert_allclose(out, [0.0, 100.0, 0.0, 100.0])


def test_vol_ratio_de_volumen_constante_es_uno() -> None:
    out = compute("VOL_RATIO", serie([1, 2, 3, 4, 5, 6]), {"period": 3},
                  source=PriceField.VOLUME)
    assert out[-1] == pytest.approx(1.0)


def test_pct_rank_del_maximo_es_uno() -> None:
    out = compute("PCT_RANK", serie([1, 2, 3, 4, 5]), {"period": 3})
    assert out[-1] == pytest.approx(1.0)


def test_pct_rank_del_minimo_es_el_menor_percentil() -> None:
    out = compute("PCT_RANK", serie([5, 4, 3, 2, 1]), {"period": 4})
    assert out[-1] == pytest.approx(0.25)


# --------------------------------------------------------------------------- #
# La trampa de las rupturas                                                    #
# --------------------------------------------------------------------------- #


def test_donchian_excluye_la_vela_actual() -> None:
    """Si el máximo incluyera la vela en curso, ninguna ruptura sería posible y
    cualquier estrategia de Donchian se convertiría en un oráculo."""
    velas = serie([1, 2, 3, 10, 4])
    out = compute("DONCHIAN_HIGH", velas, {"period": 3}, source=PriceField.HIGH)
    assert out[3] == pytest.approx(3.0)       # el 10 de hoy no cuenta
    assert out[4] == pytest.approx(10.0)      # mañana sí


def test_donchian_low_excluye_la_vela_actual() -> None:
    out = compute("DONCHIAN_LOW", serie([9, 8, 7, 1, 6]), {"period": 3}, source=PriceField.LOW)
    assert out[3] == pytest.approx(7.0)
    assert out[4] == pytest.approx(1.0)


def test_highest_y_lowest_tambien_excluyen_la_actual() -> None:
    velas = serie([1, 2, 3, 10, 4])
    alto = compute("HIGHEST", velas, {"period": 3})
    bajo = compute("LOWEST", velas, {"period": 3})
    assert alto[3] == pytest.approx(3.0)
    assert bajo[3] == pytest.approx(1.0)


def test_una_ruptura_de_donchian_puede_ocurrir_de_verdad() -> None:
    velas = serie([1, 1, 1, 1, 5])
    dc = compute("DONCHIAN_HIGH", velas, {"period": 3}, source=PriceField.HIGH)
    cierres = velas["close"].to_numpy()
    assert (cierres > dc)[4]


# --------------------------------------------------------------------------- #
# Multi-salida                                                                 #
# --------------------------------------------------------------------------- #


def test_las_bandas_de_bollinger_van_en_orden() -> None:
    velas = build_candles(120)
    p = {"period": 20, "stdev": 2.0}
    upper = compute("BBANDS", velas, {**p, "line": 0})
    middle = compute("BBANDS", velas, {**p, "line": 1})
    lower = compute("BBANDS", velas, {**p, "line": 2})
    assert (upper[30:] > middle[30:]).all()
    assert (middle[30:] > lower[30:]).all()


def test_el_histograma_del_macd_es_la_diferencia() -> None:
    velas = build_candles(300)
    p = {"fast": 12, "slow": 26, "signal": 9}
    macd = compute("MACD", velas, {**p, "line": 0})
    signal = compute("MACD", velas, {**p, "line": 1})
    hist = compute("MACD", velas, {**p, "line": 2})
    ok = ~np.isnan(hist)
    np.testing.assert_allclose(hist[ok], (macd - signal)[ok])


def test_sin_line_se_devuelve_la_primera_salida() -> None:
    velas = build_candles(120)
    p = {"period": 20, "stdev": 2.0}
    np.testing.assert_array_equal(
        compute("BBANDS", velas, p), compute("BBANDS", velas, {**p, "line": 0})
    )


def test_keltner_se_ensancha_con_el_multiplicador() -> None:
    velas = build_candles(200)
    estrecho = compute("KELTNER", velas, {"period": 20, "multiplier": 1.0, "line": 0})
    ancho = compute("KELTNER", velas, {"period": 20, "multiplier": 3.0, "line": 0})
    assert (ancho[60:] > estrecho[60:]).all()


# --------------------------------------------------------------------------- #
# Caché                                                                        #
# --------------------------------------------------------------------------- #


def test_la_cache_calcula_una_sola_vez(monkeypatch) -> None:
    velas = build_candles(200)
    cache = IndicatorCache(candles=velas)
    llamadas = []
    original = REGISTRY["EMA"]
    monkeypatch.setitem(
        REGISTRY, "EMA", lambda df, p: (llamadas.append(1), original(df, p))[1]
    )

    a = cache.get("EMA", {"period": 21})
    b = cache.get("EMA", {"period": 21})

    assert len(llamadas) == 1
    assert a is b
    assert cache.size == 1


def test_la_cache_distingue_parametros_fuente_y_timeframe() -> None:
    velas = build_candles(200)
    cache = IndicatorCache(candles=velas)
    cache.get("EMA", {"period": 21})
    cache.get("EMA", {"period": 50})
    cache.get("EMA", {"period": 21}, source="high")
    assert cache.size == 3


def test_la_cache_normaliza_parametros_equivalentes() -> None:
    velas = build_candles(200)
    cache = IndicatorCache(candles=velas)
    cache.get("EMA", {"period": 21})
    cache.get("EMA", {"period": 21.0})
    assert cache.size == 1


def test_la_cache_recorta_parametros_fuera_de_rango() -> None:
    velas = build_candles(200)
    cache = IndicatorCache(candles=velas)
    out = cache.get("RSI", {"period": 9999})
    esperado = compute("RSI", velas, {"period": spec("RSI").param("period").high})
    np.testing.assert_array_equal(out, esperado)


def test_la_cache_devuelve_arrays_de_solo_lectura() -> None:
    """Un bot no puede pisar la serie que comparten otros cuarenta."""
    cache = IndicatorCache(candles=build_candles(100))
    out = cache.get("EMA", {"period": 10})
    with pytest.raises(ValueError):
        out[0] = 1.0


def test_la_cache_calienta_segun_el_catalogo() -> None:
    cache = IndicatorCache(candles=build_candles(100))
    assert cache.warmup_bars("EMA", {"period": 20}) == spec("EMA").warmup_bars({"period": 20})


def test_clear_vacia_la_cache() -> None:
    cache = IndicatorCache(candles=build_candles(100))
    cache.get("EMA", {"period": 10})
    cache.clear()
    assert cache.size == 0


# --------------------------------------------------------------------------- #
# Contexto de timeframe superior                                               #
# --------------------------------------------------------------------------- #


def test_un_feature_de_contexto_se_alinea_hacia_atras() -> None:
    base = build_candles(240, timeframe="1h")          # 10 días
    diario = build_candles(10, timeframe="1d", first_close=1000.0, step=100.0)
    cache = IndicatorCache(candles=base, context={"1d": diario})

    serie_ctx = cache.get("SMA", {"period": 5}, timeframe="1d")
    esperado = compute("SMA", diario, {"period": 5})

    # a las 13:00 del octavo día, la diaria disponible es la del séptimo
    assert serie_ctx[24 * 7 + 13] == pytest.approx(esperado[6])
    # el primer día no tiene ninguna diaria cerrada por detrás
    assert np.isnan(serie_ctx[:24]).all()


def test_el_contexto_nunca_adelanta_su_cierre() -> None:
    base = build_candles(240, timeframe="1h")
    diario = build_candles(10, timeframe="1d", first_close=1000.0, step=100.0)
    cache = IndicatorCache(candles=base, context={"1d": diario})

    alineado = cache.get("SMA", {"period": 5}, timeframe="1d")
    esperado = compute("SMA", diario, {"period": 5})
    dias = (np.asarray(base.index) - int(diario.index[0])) // 86_400_000

    for i, dia in enumerate(dias):
        anterior = int(dia) - 1                    # la última diaria cerrada
        if anterior < 0 or np.isnan(esperado[anterior]):
            assert np.isnan(alineado[i]), f"hora {i}: se ha colado una diaria del futuro"
        else:
            assert alineado[i] == pytest.approx(esperado[anterior])


def test_pedir_un_contexto_que_no_se_ha_cargado_es_un_error() -> None:
    cache = IndicatorCache(candles=build_candles(48))
    with pytest.raises(KeyError, match="1d"):
        cache.get("SMA", {"period": 5}, timeframe="1d")


def test_el_timeframe_operativo_no_se_realinea() -> None:
    base = build_candles(100, timeframe="1h")
    cache = IndicatorCache(candles=base, timeframe="1h")
    directo = compute("EMA", base, {"period": 10})
    np.testing.assert_array_equal(cache.get("EMA", {"period": 10}, timeframe="1h"), directo)
