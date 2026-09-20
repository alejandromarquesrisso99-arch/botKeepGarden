"""Compilación del genoma a señales.

Dos bloques importan por encima del resto:

* ``eval_rule`` con arrays a mano, donde la semántica de cada operador se
  comprueba contra valores que se pueden verificar mentalmente;
* el **test de oro**: un genoma escrito a mano produce exactamente las señales
  esperadas sobre una serie sintética, con las esperadas deducidas aparte y no
  con el propio compilador.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from conftest import build_candles
from keepgarden.data.indicators import IndicatorCache, compute
from keepgarden.genome.compile import (
    DEFAULT_PCT_RANK_WINDOW,
    compile_genome,
    eval_rule,
)
from keepgarden.genome.schema import (
    Condition,
    FeatureGene,
    Genome,
    GenomeMeta,
    LogicNode,
    MarketSpec,
    Operand,
    RegimeGene,
    RiskGene,
    StopGene,
)
from keepgarden.types import (
    TIMEFRAME_MS,
    BreedOperator,
    CompareOp,
    IdeaFamily,
    LogicOp,
    PriceField,
    Signal,
    StopKind,
)

H = TIMEFRAME_MS["1h"]


def velas_de(cierres: list[float], *, highs=None, lows=None) -> pd.DataFrame:
    n = len(cierres)
    c = np.asarray(cierres, dtype="float64")
    ts = np.arange(n, dtype="int64") * H + 1_546_300_800_000
    return pd.DataFrame(
        {
            "open": c,
            "high": c if highs is None else np.asarray(highs, dtype="float64"),
            "low": c if lows is None else np.asarray(lows, dtype="float64"),
            "close": c,
            "volume": np.full(n, 100.0),
            "trades": np.full(n, 10.0),
        },
        index=pd.Index(ts, name="ts"),
    )


def arr(*valores: float) -> np.ndarray:
    return np.asarray(valores, dtype="float64")


# --------------------------------------------------------------------------- #
# Operadores de comparación                                                    #
# --------------------------------------------------------------------------- #


def test_mayor_que_una_constante() -> None:
    a = arr(1, 5, 10)
    out = eval_rule(
        Condition(CompareOp.GT, Operand(ref="a"), Operand(const=4.0)), {"a": a}, velas_de([1, 5, 10])
    )
    assert list(out) == [False, True, True]


def test_comparar_dos_series() -> None:
    velas = velas_de([0, 0, 0])
    cond = Condition(CompareOp.LT, Operand(ref="a"), Operand(ref="b"))
    out = eval_rule(cond, {"a": arr(1, 5, 9), "b": arr(2, 4, 9)}, velas)
    assert list(out) == [True, False, False]


def test_comparar_contra_el_precio() -> None:
    velas = velas_de([10, 20, 30])
    cond = Condition(CompareOp.GT, Operand(price=PriceField.CLOSE), Operand(ref="a"))
    out = eval_rule(cond, {"a": arr(15, 15, 15)}, velas)
    assert list(out) == [False, True, True]


def test_el_precio_sintetico_hlc3_tambien_vale() -> None:
    velas = velas_de([10, 20], highs=[12, 22], lows=[8, 18])
    cond = Condition(CompareOp.GT, Operand(price=PriceField.HLC3), Operand(const=15.0))
    assert list(eval_rule(cond, {}, velas)) == [False, True]


def test_cruce_por_encima_necesita_estar_debajo_antes() -> None:
    velas = velas_de([0] * 4)
    cond = Condition(CompareOp.CROSS_ABOVE, Operand(ref="a"), Operand(ref="b"))
    out = eval_rule(cond, {"a": arr(1, 3, 4, 2), "b": arr(2, 2, 2, 2)}, velas)
    assert list(out) == [False, True, False, False]


def test_cruce_por_debajo() -> None:
    velas = velas_de([0] * 4)
    cond = Condition(CompareOp.CROSS_BELOW, Operand(ref="a"), Operand(ref="b"))
    out = eval_rule(cond, {"a": arr(3, 1, 0, 5), "b": arr(2, 2, 2, 2)}, velas)
    assert list(out) == [False, True, False, False]


def test_un_cruce_en_la_primera_vela_nunca_es_cierto() -> None:
    """No hay vela anterior con la que comparar: inventarla sería look-ahead."""
    velas = velas_de([0])
    cond = Condition(CompareOp.CROSS_ABOVE, Operand(ref="a"), Operand(const=0.0))
    assert list(eval_rule(cond, {"a": arr(5)}, velas)) == [False]


def test_subiendo_mira_atras_lo_que_le_digan() -> None:
    velas = velas_de([0] * 5)
    cond = Condition(CompareOp.RISING, Operand(ref="a"), lookback=2)
    out = eval_rule(cond, {"a": arr(1, 2, 3, 2, 1)}, velas)
    assert list(out) == [False, False, True, False, False]


def test_bajando() -> None:
    velas = velas_de([0] * 4)
    cond = Condition(CompareOp.FALLING, Operand(ref="a"), lookback=1)
    out = eval_rule(cond, {"a": arr(3, 2, 2, 5)}, velas)
    assert list(out) == [False, True, False, False]


def test_entre_dos_limites_incluye_los_extremos() -> None:
    velas = velas_de([0] * 4)
    cond = Condition(
        CompareOp.BETWEEN, Operand(ref="a"), Operand(const=2.0), Operand(const=4.0)
    )
    out = eval_rule(cond, {"a": arr(1, 2, 3, 5)}, velas)
    assert list(out) == [False, True, True, False]


def test_percentil_sobre_la_ventana_del_lookback() -> None:
    velas = velas_de([0] * 6)
    cond = Condition(
        CompareOp.PCT_RANK_GT, Operand(ref="a"), Operand(const=0.9), lookback=4
    )
    out = eval_rule(cond, {"a": arr(1, 2, 3, 4, 5, 1)}, velas)
    # sólo donde el valor actual es el máximo de sus últimas 4 velas
    assert list(out) == [False, False, False, True, True, False]


def test_percentil_bajo() -> None:
    velas = velas_de([0] * 5)
    cond = Condition(
        CompareOp.PCT_RANK_LT, Operand(ref="a"), Operand(const=0.3), lookback=4
    )
    out = eval_rule(cond, {"a": arr(5, 4, 3, 2, 1)}, velas)
    assert list(out) == [False, False, False, True, True]


def test_sin_lookback_el_percentil_usa_una_ventana_por_defecto() -> None:
    """Una ventana de 1 haría que el percentil valiera siempre 1 y la regla
    fuese una constante disfrazada."""
    n = DEFAULT_PCT_RANK_WINDOW + 5
    velas = velas_de([0] * n)
    valores = np.arange(n, dtype="float64")
    cond = Condition(CompareOp.PCT_RANK_GT, Operand(ref="a"), Operand(const=0.5))
    out = eval_rule(cond, {"a": valores}, velas)
    assert not out[: DEFAULT_PCT_RANK_WINDOW - 1].any()
    assert out[DEFAULT_PCT_RANK_WINDOW - 1 :].all()


# --------------------------------------------------------------------------- #
# NaN y árboles lógicos                                                        #
# --------------------------------------------------------------------------- #


def test_un_nan_nunca_es_verdad() -> None:
    velas = velas_de([0] * 3)
    cond = Condition(CompareOp.GT, Operand(ref="a"), Operand(const=0.0))
    out = eval_rule(cond, {"a": arr(np.nan, 1, np.nan)}, velas)
    assert list(out) == [False, True, False]


def test_and_y_or() -> None:
    velas = velas_de([0] * 3)
    izq = Condition(CompareOp.GT, Operand(ref="a"), Operand(const=0.0))
    der = Condition(CompareOp.GT, Operand(ref="b"), Operand(const=0.0))
    arrays = {"a": arr(1, 1, -1), "b": arr(1, -1, -1)}
    assert list(eval_rule(LogicNode(LogicOp.AND, (izq, der)), arrays, velas)) == [
        True, False, False
    ]
    assert list(eval_rule(LogicNode(LogicOp.OR, (izq, der)), arrays, velas)) == [
        True, True, False
    ]


def test_not_invierte() -> None:
    velas = velas_de([0] * 2)
    cond = Condition(CompareOp.GT, Operand(ref="a"), Operand(const=0.0))
    out = eval_rule(LogicNode(LogicOp.NOT, (cond,)), {"a": arr(1, -1)}, velas)
    assert list(out) == [False, True]


def test_arboles_anidados() -> None:
    velas = velas_de([0] * 4)
    arrays = {"a": arr(1, 1, 0, 0), "b": arr(1, 0, 1, 0)}
    tree = LogicNode(
        LogicOp.AND,
        (
            Condition(CompareOp.GT, Operand(ref="a"), Operand(const=0.5)),
            LogicNode(
                LogicOp.NOT,
                (Condition(CompareOp.GT, Operand(ref="b"), Operand(const=0.5)),),
            ),
        ),
    )
    assert list(eval_rule(tree, arrays, velas)) == [False, True, False, False]


def test_un_arbol_vacio_no_dispara_nunca() -> None:
    velas = velas_de([0] * 3)
    assert not eval_rule(None, {}, velas).any()


def test_una_referencia_rota_se_queja_con_nombre() -> None:
    velas = velas_de([0] * 3)
    cond = Condition(CompareOp.GT, Operand(ref="no_existe"), Operand(const=0.0))
    with pytest.raises(KeyError, match="no_existe"):
        eval_rule(cond, {"a": arr(1, 2, 3)}, velas)


# --------------------------------------------------------------------------- #
# El test de oro                                                               #
# --------------------------------------------------------------------------- #


def genoma_de_cruce() -> Genome:
    """Cruce de medias, escrito a mano. Lo más simple que sigue siendo un bot."""
    return Genome(
        id="gen_oro",
        family=IdeaFamily.TREND,
        market=MarketSpec(venue="binance", symbol="BTC/USDT", timeframe="1h"),
        features=(
            FeatureGene("rapida", "SMA", {"period": 5}, PriceField.CLOSE),
            FeatureGene("lenta", "SMA", {"period": 20}, PriceField.CLOSE),
            FeatureGene("atr", "ATR", {"period": 14}, PriceField.HLC3),
        ),
        entry_long=Condition(CompareOp.CROSS_ABOVE, Operand(ref="rapida"), Operand(ref="lenta")),
        exit_long=Condition(CompareOp.CROSS_BELOW, Operand(ref="rapida"), Operand(ref="lenta")),
        risk=RiskGene(stop=StopGene(StopKind.ATR_MULT, 2.0, atr_ref="atr")),
        meta=GenomeMeta(born_at="2026-09-20T00:00:00Z", operator=BreedOperator.SEED),
    )


def onda(n: int = 400) -> pd.DataFrame:
    """Una serie con varias vueltas completas: hay cruces en los dos sentidos."""
    i = np.arange(n, dtype="float64")
    close = 100.0 + 12.0 * np.sin(i / 11.0) + 4.0 * np.sin(i / 3.0)
    return velas_de(list(close), highs=list(close * 1.002), lows=list(close * 0.998))


def test_de_oro_el_genoma_produce_exactamente_las_senales_esperadas() -> None:
    """Criterio de aceptación del hito 2.

    Las señales esperadas se deducen aquí con numpy, sin pasar por el
    compilador: si ambos coinciden, la compilación hace lo que dice.
    """
    velas = onda()
    genome = genoma_de_cruce()
    compilado = compile_genome(genome, velas, IndicatorCache(candles=velas))

    rapida = compute("SMA", velas, {"period": 5})
    lenta = compute("SMA", velas, {"period": 20})
    por_encima = rapida > lenta
    antes = np.concatenate([[False], por_encima[:-1]])
    valido = np.isfinite(rapida) & np.isfinite(lenta)
    valido_antes = np.concatenate([[False], valido[:-1]])

    cruza_arriba = por_encima & ~antes & valido & valido_antes
    cruza_abajo = ~por_encima & antes & valido & valido_antes

    esperado = np.full(len(velas), Signal.NONE, dtype=object)
    esperado[cruza_arriba] = Signal.ENTER_LONG
    esperado[cruza_abajo] = Signal.EXIT_LONG
    esperado[: compilado.warmup_bars] = Signal.NONE

    np.testing.assert_array_equal(compilado.signals(), esperado)
    assert (compilado.signals() == Signal.ENTER_LONG).sum() >= 3
    assert (compilado.signals() == Signal.EXIT_LONG).sum() >= 3


def test_signal_at_coincide_con_el_array() -> None:
    velas = onda(200)
    compilado = compile_genome(genoma_de_cruce(), velas, IndicatorCache(candles=velas))
    señales = compilado.signals()
    for i in (0, 50, 120, 199):
        assert compilado.signal_at(i) is señales[i]


def test_los_features_quedan_disponibles_por_id() -> None:
    velas = onda(200)
    compilado = compile_genome(genoma_de_cruce(), velas, IndicatorCache(candles=velas))
    assert set(compilado.feature_arrays) == {"rapida", "lenta", "atr"}
    np.testing.assert_array_equal(
        compilado.feature_arrays["rapida"], compute("SMA", velas, {"period": 5})
    )


# --------------------------------------------------------------------------- #
# Calentamiento, régimen y prioridad                                           #
# --------------------------------------------------------------------------- #


def test_no_hay_senales_durante_el_calentamiento() -> None:
    velas = onda(400)
    compilado = compile_genome(genoma_de_cruce(), velas, IndicatorCache(candles=velas))
    assert compilado.warmup_bars >= 60          # SMA(20) declara 3x el periodo
    assert (compilado.signals()[: compilado.warmup_bars] == Signal.NONE).all()


def test_el_calentamiento_cubre_el_feature_mas_lento() -> None:
    velas = onda(600)
    genome = genoma_de_cruce()
    lento = Genome(
        **{
            **{f.name: getattr(genome, f.name) for f in genome.__dataclass_fields__.values()},
            "features": (
                FeatureGene("rapida", "SMA", {"period": 5}, PriceField.CLOSE),
                FeatureGene("lenta", "SMA", {"period": 150}, PriceField.CLOSE),
                FeatureGene("atr", "ATR", {"period": 14}, PriceField.HLC3),
            ),
        }
    )
    compilado = compile_genome(lento, velas, IndicatorCache(candles=velas))
    assert compilado.warmup_bars >= 450
    assert np.isfinite(compilado.feature_arrays["lenta"][compilado.warmup_bars :]).all()


def test_el_filtro_de_regimen_apaga_las_entradas() -> None:
    velas = onda(400)
    base = genoma_de_cruce()
    nunca = Genome(
        **{
            **{f.name: getattr(base, f.name) for f in base.__dataclass_fields__.values()},
            "features": (*base.features, FeatureGene("adx", "ADX", {"period": 14})),
            "regime": RegimeGene(
                enabled=True,
                rule=Condition(CompareOp.GT, Operand(ref="adx"), Operand(const=999.0)),
            ),
        }
    )
    compilado = compile_genome(nunca, velas, IndicatorCache(candles=velas))
    assert not (compilado.signals() == Signal.ENTER_LONG).any()


def test_el_regimen_no_atrapa_una_posicion_abierta() -> None:
    """Un régimen adverso apaga las entradas, nunca las salidas."""
    velas = onda(400)
    base = genoma_de_cruce()
    con_regimen = Genome(
        **{
            **{f.name: getattr(base, f.name) for f in base.__dataclass_fields__.values()},
            "features": (*base.features, FeatureGene("adx", "ADX", {"period": 14})),
            "regime": RegimeGene(
                enabled=True,
                rule=Condition(CompareOp.GT, Operand(ref="adx"), Operand(const=999.0)),
            ),
        }
    )
    compilado = compile_genome(con_regimen, velas, IndicatorCache(candles=velas))
    assert (compilado.signals() == Signal.EXIT_LONG).any()


def test_la_salida_manda_sobre_la_entrada_en_la_misma_vela() -> None:
    velas = velas_de([100.0] * 80)
    siempre = Condition(CompareOp.GT, Operand(price=PriceField.CLOSE), Operand(const=0.0))
    genome = Genome(
        id="gen_choque",
        family=IdeaFamily.TREND,
        features=(FeatureGene("sma", "SMA", {"period": 5}, PriceField.CLOSE),),
        entry_long=siempre,
        exit_long=siempre,
        meta=GenomeMeta(operator=BreedOperator.SEED),
    )
    compilado = compile_genome(genome, velas, IndicatorCache(candles=velas))
    despues = compilado.signals()[compilado.warmup_bars :]
    assert (despues == Signal.EXIT_LONG).all()


def test_sin_cortos_no_se_emiten_senales_de_corto() -> None:
    velas = onda(300)
    base = genoma_de_cruce()
    con_corto = Genome(
        **{
            **{f.name: getattr(base, f.name) for f in base.__dataclass_fields__.values()},
            "entry_short": Condition(
                CompareOp.CROSS_BELOW, Operand(ref="rapida"), Operand(ref="lenta")
            ),
            "exit_short": Condition(
                CompareOp.CROSS_ABOVE, Operand(ref="rapida"), Operand(ref="lenta")
            ),
        }
    )
    compilado = compile_genome(con_corto, velas, IndicatorCache(candles=velas))
    assert not (compilado.signals() == Signal.ENTER_SHORT).any()


# --------------------------------------------------------------------------- #
# Causalidad de la compilación entera                                          #
# --------------------------------------------------------------------------- #


def test_compilar_sobre_un_prefijo_da_las_mismas_senales() -> None:
    """El mismo criterio del hito 1, ahora de punta a punta."""
    velas = build_candles(500)
    genome = genoma_de_cruce()

    corto = compile_genome(genome, velas.iloc[:300], IndicatorCache(candles=velas.iloc[:300]))
    largo = compile_genome(genome, velas, IndicatorCache(candles=velas))

    np.testing.assert_array_equal(corto.signals(), largo.signals()[:300])


def test_un_feature_de_contexto_se_compila_alineado_hacia_atras() -> None:
    base = build_candles(480, timeframe="1h")
    diario = build_candles(20, timeframe="1d", first_close=1000.0, step=50.0)
    genome = Genome(
        id="gen_ctx",
        family=IdeaFamily.TREND,
        features=(
            FeatureGene("sma_h", "SMA", {"period": 10}, PriceField.CLOSE),
            FeatureGene("sma_d", "SMA", {"period": 5}, PriceField.CLOSE, timeframe="1d"),
        ),
        entry_long=Condition(CompareOp.GT, Operand(ref="sma_h"), Operand(ref="sma_d")),
        exit_long=Condition(CompareOp.LT, Operand(ref="sma_h"), Operand(ref="sma_d")),
        meta=GenomeMeta(operator=BreedOperator.SEED),
    )
    cache = IndicatorCache(candles=base, context={"1d": diario})
    compilado = compile_genome(genome, base, cache)

    esperado = compute("SMA", diario, {"period": 5})
    dias = (np.asarray(base.index) - int(diario.index[0])) // 86_400_000
    serie = compilado.feature_arrays["sma_d"]
    for i, dia in enumerate(dias):
        anterior = int(dia) - 1
        if anterior < 0 or np.isnan(esperado[anterior]):
            assert np.isnan(serie[i])
        else:
            assert serie[i] == pytest.approx(esperado[anterior])


def test_el_contexto_diario_dispara_el_calentamiento() -> None:
    """Cinco velas diarias son 120 horas: el calentamiento debe contarlo."""
    base = build_candles(480, timeframe="1h")
    diario = build_candles(20, timeframe="1d", first_close=1000.0, step=50.0)
    genome = Genome(
        id="gen_ctx",
        family=IdeaFamily.TREND,
        features=(
            FeatureGene("sma_d", "SMA", {"period": 5}, PriceField.CLOSE, timeframe="1d"),
        ),
        entry_long=Condition(CompareOp.GT, Operand(ref="sma_d"), Operand(const=0.0)),
        exit_long=Condition(CompareOp.LT, Operand(ref="sma_d"), Operand(const=0.0)),
        meta=GenomeMeta(operator=BreedOperator.SEED),
    )
    compilado = compile_genome(
        genome, base, IndicatorCache(candles=base, context={"1d": diario})
    )
    assert compilado.warmup_bars >= 15 * 24
