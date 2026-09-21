"""El motor de simulación completo.

Los tres primeros tests son los criterios de aceptación del hito 3 y valen por
todos los demás:

* **no-look-ahead**: correr sobre ``velas[0:n]`` da exactamente las mismas
  operaciones que correr sobre ``velas[0:n+500]`` y quedarse con las de ``n``;
* **fricción**: sin comisión ni deslizamiento, un buy & hold reproduce el
  retorno del activo con error menor que 1e-9;
* **contabilidad**: ``equity_final == capital_inicial + suma(pnl_neto)``.
"""

from __future__ import annotations

import dataclasses

import numpy as np
import pandas as pd
import pytest

from conftest import build_walk
from keepgarden.config import Config, FrictionConfig
from keepgarden.engine.backtest import BacktestResult, run_backtest, run_batch
from keepgarden.genome.schema import (
    Condition,
    FeatureGene,
    Genome,
    GenomeMeta,
    MarketSpec,
    Operand,
    RiskGene,
    StopGene,
    TakeProfitGene,
    TrailingGene,
)
from keepgarden.types import (
    TIMEFRAME_MS,
    BreedOperator,
    CompareOp,
    IdeaFamily,
    PriceField,
    SizingKind,
    StopKind,
    TakeProfitKind,
    TrailingKind,
)

H = TIMEFRAME_MS["1h"]
TS0 = 1_546_300_800_000


# --------------------------------------------------------------------------- #
# Utilidades                                                                   #
# --------------------------------------------------------------------------- #


def sin_friccion(cfg: Config) -> Config:
    """Una configuración sin comisión ni deslizamiento.

    Se construye a mano y no con load_config a propósito: `validate_config`
    prohíbe la fricción cero, porque un backtest sin fricción miente. Aquí
    interesa justo eso, para poder comparar el motor contra la aritmética pura.
    """
    return dataclasses.replace(
        cfg,
        frictions=FrictionConfig(
            taker_fee_bps=0.0, maker_fee_bps=0.0, slippage_model="fixed_bps",
            slippage_bps=0.0, slippage_atr_frac=0.0, min_notional=10.0,
            price_precision=8, amount_precision=8,
        ),
    )


def velas_de(closes: list[float], *, opens: list[float] | None = None,
             saltos: dict[int, int] | None = None) -> pd.DataFrame:
    """Serie con aperturas iguales al cierre anterior, salvo que se diga otra cosa."""
    n = len(closes)
    if n == 0:
        from keepgarden.data.candles import empty_frame

        return empty_frame()
    c = np.asarray(closes, dtype="float64")
    if opens is None:
        o = np.empty(n)
        o[0] = c[0]
        o[1:] = c[:-1]
    else:
        o = np.asarray(opens, dtype="float64")
    ts = np.arange(n, dtype="int64") * H + TS0
    if saltos:
        for desde, velas in saltos.items():
            ts[desde:] += velas * H
    return pd.DataFrame(
        {
            "open": o,
            "high": np.maximum(o, c) * 1.001,
            "low": np.minimum(o, c) * 0.999,
            "close": c,
            "volume": np.full(n, 500.0),
            "trades": np.full(n, 100.0),
        },
        index=pd.Index(ts, name="ts"),
    )


def genoma(entry: Condition, exit_: Condition, **risk_cambios) -> Genome:
    risk = dataclasses.replace(
        RiskGene(
            sizing=SizingKind.FIXED_FRACTION,
            risk_per_trade=0.01,
            max_concurrent_positions=1,
            max_exposure=1.0,
            stop=StopGene(StopKind.NONE, 0.0),
            take_profit=TakeProfitGene(TakeProfitKind.NONE, 0.0),
            trailing=TrailingGene(TrailingKind.NONE, 0.0),
            max_holding_bars=100_000,
            cooldown_bars=0,
            allow_short=False,
        ),
        **risk_cambios,
    )
    return Genome(
        id="gen_test",
        family=IdeaFamily.TREND,
        market=MarketSpec(venue="binance", symbol="BTC/USDT", timeframe="1h"),
        features=(FeatureGene("sma", "SMA", {"period": 5}, PriceField.CLOSE),),
        entry_long=entry,
        exit_long=exit_,
        risk=risk,
        meta=GenomeMeta(operator=BreedOperator.SEED),
    )


SIEMPRE = Condition(CompareOp.GT, Operand(price=PriceField.CLOSE), Operand(const=0.0))
NUNCA = Condition(CompareOp.LT, Operand(price=PriceField.CLOSE), Operand(const=0.0))


def cruce_de_media() -> Genome:
    """Un bot de verdad, con stop y take profit, para los tests generales."""
    g = genoma(
        Condition(CompareOp.GT, Operand(price=PriceField.CLOSE), Operand(ref="sma")),
        Condition(CompareOp.LT, Operand(price=PriceField.CLOSE), Operand(ref="sma")),
        sizing=SizingKind.ATR_RISK,
        stop=StopGene(StopKind.ATR_MULT, 2.0, atr_ref="atr"),
        max_holding_bars=200,
    )
    return dataclasses.replace(
        g,
        features=(
            FeatureGene("sma", "SMA", {"period": 20}, PriceField.CLOSE),
            FeatureGene("atr", "ATR", {"period": 14}, PriceField.HLC3),
        ),
    )


# --------------------------------------------------------------------------- #
# Criterio 1: no-look-ahead                                                    #
# --------------------------------------------------------------------------- #


def test_conocer_el_futuro_no_cambia_ni_una_operacion(cfg) -> None:
    """Criterio de aceptación del hito 3."""
    velas = build_walk(1500)
    n = 900
    genome = cruce_de_media()

    corto = run_backtest(genome, velas.iloc[:n], cfg)
    largo = run_backtest(genome, velas, cfg)

    corte = int(velas.index[n - 1])
    truncadas = [t for t in largo.trades if int(t["exit_ts"]) <= corte]

    assert corto.trades == truncadas
    assert len(corto.trades) > 5, "el test no dice nada si el bot no opera"
    np.testing.assert_allclose(corto.equity_curve, largo.equity_curve[:n], rtol=0, atol=0)


def test_el_prefijo_coincide_para_varios_cortes(cfg) -> None:
    velas = build_walk(1200)
    genome = cruce_de_media()
    largo = run_backtest(genome, velas, cfg)
    for n in (400, 700, 1100):
        corto = run_backtest(genome, velas.iloc[:n], cfg)
        np.testing.assert_allclose(corto.equity_curve, largo.equity_curve[:n], rtol=0, atol=0)


# --------------------------------------------------------------------------- #
# Criterio 2: fricción                                                         #
# --------------------------------------------------------------------------- #


def test_sin_friccion_un_buy_and_hold_reproduce_el_activo(cfg) -> None:
    """Criterio de aceptación del hito 3.

    Dieciséis velas planas a 100 para que la entrada caiga en un precio exacto,
    y después una subida. Con comisión y deslizamiento a cero, el bot compra
    todo el capital y no vende nunca: su retorno tiene que ser, hasta el último
    decimal, el del activo.
    """
    closes = [100.0] * 16 + [100.0 + i for i in range(1, 85)]
    velas = velas_de(closes)
    resultado = run_backtest(
        genoma(SIEMPRE, NUNCA), velas, sin_friccion(cfg), initial_capital=1000.0
    )

    assert len(resultado.trades) == 0            # nunca cierra
    esperado = closes[-1] / 100.0 - 1.0
    obtenido = resultado.final_equity / 1000.0 - 1.0
    assert abs(obtenido - esperado) < 1e-9


def test_la_friccion_se_come_parte_del_retorno(cfg) -> None:
    closes = [100.0] * 16 + [100.0 + i for i in range(1, 85)]
    velas = velas_de(closes)
    limpio = run_backtest(genoma(SIEMPRE, NUNCA), velas, sin_friccion(cfg), initial_capital=1000.0)
    con_costes = run_backtest(genoma(SIEMPRE, NUNCA), velas, cfg, initial_capital=1000.0)
    assert con_costes.final_equity < limpio.final_equity
    assert con_costes.total_fees > 0.0


def test_un_bot_que_no_opera_conserva_el_capital(cfg) -> None:
    velas = velas_de([100.0 + i for i in range(200)])
    r = run_backtest(genoma(NUNCA, NUNCA), velas, cfg, initial_capital=1000.0)
    assert r.final_equity == pytest.approx(1000.0)
    assert r.trades == []
    assert r.total_fees == 0.0


# --------------------------------------------------------------------------- #
# Criterio 3: contabilidad                                                     #
# --------------------------------------------------------------------------- #


def test_el_capital_final_es_el_inicial_mas_los_beneficios(cfg) -> None:
    """Criterio de aceptación del hito 3, con el bot cerrado al terminar."""
    closes = [100.0 + i * 0.5 for i in range(400)]       # de 100 a 299.5
    velas = velas_de(closes)
    g = genoma(
        Condition(CompareOp.LT, Operand(price=PriceField.CLOSE), Operand(const=150.0)),
        Condition(CompareOp.GT, Operand(price=PriceField.CLOSE), Operand(const=155.0)),
    )
    r = run_backtest(g, velas, cfg, initial_capital=1000.0)

    assert r.trades, "sin operaciones el test no comprueba nada"
    assert r.unrealized_pnl == 0.0                       # acaba plano
    suma = sum(float(t["pnl"]) for t in r.trades)
    assert r.final_equity == pytest.approx(1000.0 + suma, abs=1e-9)


def test_la_contabilidad_cuadra_tambien_con_una_posicion_abierta(cfg) -> None:
    velas = build_walk(800)
    r = run_backtest(cruce_de_media(), velas, cfg, initial_capital=1000.0)
    suma = sum(float(t["pnl"]) for t in r.trades)
    assert r.final_equity == pytest.approx(1000.0 + suma + r.unrealized_pnl, abs=1e-6)


def test_las_comisiones_cuadran_con_las_de_las_operaciones(cfg) -> None:
    velas = build_walk(600)
    r = run_backtest(cruce_de_media(), velas, cfg, initial_capital=1000.0)
    de_operaciones = sum(float(t["fees"]) for t in r.trades)
    assert r.total_fees >= de_operaciones - 1e-9        # más la entrada aún abierta


# --------------------------------------------------------------------------- #
# La secuencia de la vela                                                      #
# --------------------------------------------------------------------------- #


def test_el_fill_ocurre_en_la_apertura_de_la_vela_siguiente(cfg) -> None:
    """La decisión se toma con la vela t cerrada; el fill es en open[t+1]."""
    closes = [100.0] * 16 + [100.0, 200.0, 201.0, 202.0, 203.0]
    opens = [100.0] * 17 + [100.0, 200.0, 201.0, 202.0]
    velas = velas_de(closes, opens=opens)
    r = run_backtest(
        genoma(SIEMPRE, NUNCA), velas, sin_friccion(cfg), initial_capital=1000.0
    )
    # la señal de la vela 15 se rellena en open[16] = 100, no en close[15]
    assert r.final_equity == pytest.approx(1000.0 / 100.0 * closes[-1])


def test_no_se_opera_durante_el_calentamiento(cfg) -> None:
    velas = velas_de([100.0 + i for i in range(300)])
    r = run_backtest(genoma(SIEMPRE, NUNCA), velas, cfg, initial_capital=1000.0)
    assert r.warmup_bars >= 15
    # el capital no se mueve hasta que el calentamiento termina
    np.testing.assert_allclose(r.equity_curve[: r.warmup_bars], 1000.0)


def test_el_stop_sale_dentro_de_la_vela_y_no_en_la_siguiente(cfg) -> None:
    closes = [100.0] * 20 + [100.0, 80.0, 81.0, 82.0]
    velas = velas_de(closes)
    g = genoma(SIEMPRE, NUNCA, sizing=SizingKind.ATR_RISK,
               stop=StopGene(StopKind.PERCENT, 0.05))
    r = run_backtest(g, velas, cfg, initial_capital=1000.0)
    assert [t["exit_kind"] for t in r.trades] == ["EXIT_STOP"]


# --------------------------------------------------------------------------- #
# Huecos y frenos                                                              #
# --------------------------------------------------------------------------- #


def test_un_hueco_largo_cierra_la_posicion_antes_de_atravesarlo(cfg) -> None:
    closes = [100.0] * 16 + [100.0 + i for i in range(1, 40)]
    velas = velas_de(closes, saltos={30: 48})     # 48 velas de parada del venue
    r = run_backtest(genoma(SIEMPRE, NUNCA), velas, cfg, initial_capital=1000.0)
    assert any(t["exit_kind"] == "EXIT_FORCED" for t in r.trades)
    cierre = next(t for t in r.trades if t["exit_kind"] == "EXIT_FORCED")
    assert int(cierre["exit_ts"]) == int(velas.index[29])


def test_el_freno_de_drawdown_aborta_el_backtest(cfg) -> None:
    closes = [100.0] * 16 + [100.0] + [100.0 * (1 - 0.02 * i) for i in range(1, 40)]
    velas = velas_de(closes)
    r = run_backtest(genoma(SIEMPRE, NUNCA), velas, cfg, initial_capital=1000.0)
    assert r.aborted_reason == "DRAWDOWN_BREAKER"
    assert len(r.equity_curve) == len(velas)          # la curva se rellena hasta el final


def test_sin_derrumbe_no_se_aborta(cfg) -> None:
    velas = velas_de([100.0 + i for i in range(200)])
    r = run_backtest(genoma(SIEMPRE, NUNCA), velas, cfg, initial_capital=1000.0)
    assert r.aborted_reason == ""


def test_el_enfriamiento_impide_reentrar_en_la_vela_siguiente(cfg) -> None:
    closes = [100.0] * 16 + [100.0, 95.0, 100.0, 95.0, 100.0, 95.0, 100.0] * 6
    velas = velas_de(closes)
    g = genoma(
        Condition(CompareOp.GT, Operand(price=PriceField.CLOSE), Operand(const=99.0)),
        Condition(CompareOp.LT, Operand(price=PriceField.CLOSE), Operand(const=99.0)),
    )
    sin_espera = run_backtest(g, velas, cfg, initial_capital=1000.0)
    con_espera = run_backtest(
        dataclasses.replace(g, risk=dataclasses.replace(g.risk, cooldown_bars=10)),
        velas, cfg, initial_capital=1000.0,
    )
    assert len(con_espera.trades) < len(sin_espera.trades)


# --------------------------------------------------------------------------- #
# Forma del resultado y determinismo                                           #
# --------------------------------------------------------------------------- #


def test_el_resultado_describe_la_ventana(cfg) -> None:
    velas = build_walk(500)
    r = run_backtest(cruce_de_media(), velas, cfg, initial_capital=1000.0)
    assert isinstance(r, BacktestResult)
    assert r.genome_id == "gen_test"
    assert r.start_ts == int(velas.index[0])
    assert r.end_ts == int(velas.index[-1])
    assert r.n_bars == 500
    assert r.equity_curve is not None and len(r.equity_curve) == 500
    assert r.equity_ts is not None and len(r.equity_ts) == 500
    assert np.isfinite(r.equity_curve).all()


def test_dos_ejecuciones_iguales_dan_el_mismo_resultado(cfg) -> None:
    velas = build_walk(600)
    a = run_backtest(cruce_de_media(), velas, cfg)
    b = run_backtest(cruce_de_media(), velas, cfg)
    assert a.trades == b.trades
    np.testing.assert_array_equal(a.equity_curve, b.equity_curve)


def test_una_serie_vacia_no_explota(cfg) -> None:
    r = run_backtest(cruce_de_media(), velas_de([]), cfg, initial_capital=1000.0)
    assert r.n_bars == 0
    assert r.trades == []
    assert r.final_equity == pytest.approx(1000.0)


def test_una_serie_mas_corta_que_el_calentamiento_no_opera(cfg) -> None:
    r = run_backtest(cruce_de_media(), build_walk(10), cfg, initial_capital=1000.0)
    assert r.trades == []
    assert r.final_equity == pytest.approx(1000.0)


def test_el_capital_inicial_sale_de_la_config_si_no_se_dice_otro(cfg) -> None:
    r = run_backtest(cruce_de_media(), build_walk(300), cfg)
    assert r.equity_curve[0] == pytest.approx(cfg.garden.initial_capital_per_bot)


# --------------------------------------------------------------------------- #
# Lotes                                                                        #
# --------------------------------------------------------------------------- #


def test_un_lote_secuencial_da_lo_mismo_que_uno_a_uno(cfg) -> None:
    velas = build_walk(400)
    genomas = [
        dataclasses.replace(cruce_de_media(), id=f"gen_{i}")
        for i in range(3)
    ]
    en_lote = run_batch(genomas, velas, cfg, workers=1)
    uno_a_uno = [run_backtest(g, velas, cfg) for g in genomas]

    assert [r.genome_id for r in en_lote] == ["gen_0", "gen_1", "gen_2"]
    for a, b in zip(en_lote, uno_a_uno):
        assert a.trades == b.trades
        np.testing.assert_array_equal(a.equity_curve, b.equity_curve)


def test_un_lote_vacio_devuelve_una_lista_vacia(cfg) -> None:
    assert run_batch([], build_walk(100), cfg, workers=1) == []
