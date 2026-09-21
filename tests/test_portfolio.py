"""Cartera de un bot: posiciones, tamaño, stops y contabilidad.

El bloque de ``size_order`` es el que más vigilancia merece: traducir "arriesgo
el 1 % por operación" a una cantidad concreta es donde un error no se ve nunca
—el bot opera, gana y pierde— pero hace que el riesgo real sea diez veces el
que dice su genoma.
"""

from __future__ import annotations

import dataclasses

import pytest

from keepgarden.engine.broker import Fill
from keepgarden.engine.portfolio import Portfolio, Position
from keepgarden.genome.schema import RiskGene, StopGene, TakeProfitGene, TrailingGene
from keepgarden.types import (
    OrderKind,
    Side,
    SizingKind,
    StopKind,
    TakeProfitKind,
    TrailingKind,
)

TS = 1_546_300_800_000
H = 3_600_000


def cartera(cash: float = 1000.0) -> Portfolio:
    return Portfolio(bot_id="bot_test", cash=cash, initial_capital=cash, peak_equity=cash)


def riesgo(**cambios) -> RiskGene:
    base = RiskGene(
        sizing=SizingKind.ATR_RISK,
        risk_per_trade=0.01,
        max_concurrent_positions=1,
        max_exposure=0.95,
        stop=StopGene(StopKind.ATR_MULT, 2.0),
        take_profit=TakeProfitGene(TakeProfitKind.NONE, 0.0),
        trailing=TrailingGene(TrailingKind.NONE, 0.0),
        max_holding_bars=240,
        cooldown_bars=0,
        allow_short=False,
    )
    return dataclasses.replace(base, **cambios)


def fill(
    kind: OrderKind,
    side: Side,
    price: float,
    amount: float,
    *,
    fee: float = 0.0,
    ts: int = TS,
) -> Fill:
    return Fill(
        bot_id="bot_test", candle_ts=ts, fill_ts=ts, kind=kind, side=side,
        price=price, reference_price=price, slippage=0.0, amount=amount,
        notional=price * amount, fee=fee,
    )


def abierta(p: Portfolio, price: float, amount: float, *, risk: RiskGene | None = None,
            atr: float = 5.0, side: Side = Side.LONG) -> Position:
    p.apply_fill(fill(OrderKind.ENTRY, side, price, amount), risk=risk or riesgo(), atr=atr)
    return p.positions[-1]


# --------------------------------------------------------------------------- #
# Posición                                                                     #
# --------------------------------------------------------------------------- #


def test_beneficio_no_realizado_de_un_largo() -> None:
    pos = Position(Side.LONG, amount=2.0, entry_price=100.0, entry_ts=TS)
    assert pos.unrealized(110.0) == pytest.approx(20.0)
    assert pos.unrealized(90.0) == pytest.approx(-20.0)


def test_beneficio_no_realizado_de_un_corto() -> None:
    pos = Position(Side.SHORT, amount=2.0, entry_price=100.0, entry_ts=TS)
    assert pos.unrealized(90.0) == pytest.approx(20.0)
    assert pos.unrealized(110.0) == pytest.approx(-20.0)


def test_multiplo_de_r() -> None:
    pos = Position(Side.LONG, amount=1.0, entry_price=100.0, entry_ts=TS, initial_risk=5.0)
    assert pos.r_multiple(105.0) == pytest.approx(1.0)
    assert pos.r_multiple(115.0) == pytest.approx(3.0)
    assert pos.r_multiple(95.0) == pytest.approx(-1.0)


def test_sin_riesgo_inicial_el_multiplo_de_r_es_cero_y_no_infinito() -> None:
    pos = Position(Side.LONG, amount=1.0, entry_price=100.0, entry_ts=TS, initial_risk=0.0)
    assert pos.r_multiple(150.0) == 0.0


# --------------------------------------------------------------------------- #
# Capital                                                                      #
# --------------------------------------------------------------------------- #


def test_sin_posiciones_el_equity_es_la_caja() -> None:
    assert cartera(1000.0).equity(123.0) == pytest.approx(1000.0)


def test_el_equity_incluye_el_valor_de_la_posicion() -> None:
    p = cartera(1000.0)
    abierta(p, price=100.0, amount=5.0)
    assert p.cash == pytest.approx(500.0)
    assert p.equity(100.0) == pytest.approx(1000.0)
    assert p.equity(120.0) == pytest.approx(1100.0)


def test_el_equity_de_un_corto_sube_cuando_baja_el_precio() -> None:
    p = cartera(1000.0)
    abierta(p, price=100.0, amount=2.0, side=Side.SHORT,
            risk=riesgo(allow_short=True))
    assert p.equity(100.0) == pytest.approx(1000.0)
    assert p.equity(90.0) == pytest.approx(1020.0)


def test_drawdown_desde_el_maximo() -> None:
    p = cartera(1000.0)
    p.mark_to_market(100.0, TS)
    abierta(p, price=100.0, amount=5.0)
    p.mark_to_market(120.0, TS + H)          # equity 1100, nuevo máximo
    assert p.drawdown(120.0) == pytest.approx(0.0)
    assert p.drawdown(100.0) == pytest.approx(100.0 / 1100.0)


def test_exposicion() -> None:
    p = cartera(1000.0)
    abierta(p, price=100.0, amount=4.0)
    assert p.exposure(100.0) == pytest.approx(0.4)


def test_la_exposicion_de_una_cartera_vacia_es_cero() -> None:
    assert cartera().exposure(100.0) == 0.0


# --------------------------------------------------------------------------- #
# Tamaño de la orden                                                           #
# --------------------------------------------------------------------------- #


def test_atr_risk_arriesga_exactamente_lo_que_dice_el_genoma(cfg) -> None:
    """El criterio que hace comparables estrategias de volatilidades muy
    distintas: si salta el stop, la pérdida es risk_per_trade del capital."""
    p = cartera(1000.0)
    r = riesgo(sizing=SizingKind.ATR_RISK, risk_per_trade=0.01,
               stop=StopGene(StopKind.ATR_MULT, 2.0))
    atr = 5.0

    amount = p.size_order(r, price=100.0, atr=atr, realized_vol=0.5, cfg=cfg)

    distancia = 2.0 * atr                       # 10 unidades de precio
    assert amount * distancia == pytest.approx(1000.0 * 0.01, rel=1e-3)


def test_atr_risk_con_stop_porcentual(cfg) -> None:
    p = cartera(1000.0)
    r = riesgo(sizing=SizingKind.ATR_RISK, risk_per_trade=0.02,
               stop=StopGene(StopKind.PERCENT, 0.05))
    amount = p.size_order(r, price=200.0, atr=5.0, realized_vol=0.5, cfg=cfg)
    distancia = 200.0 * 0.05
    assert amount * distancia == pytest.approx(1000.0 * 0.02, rel=1e-3)


def test_con_mas_volatilidad_se_compra_menos(cfg) -> None:
    p = cartera(1000.0)
    r = riesgo(sizing=SizingKind.ATR_RISK)
    tranquilo = p.size_order(r, price=100.0, atr=2.0, realized_vol=0.5, cfg=cfg)
    agitado = p.size_order(r, price=100.0, atr=10.0, realized_vol=0.5, cfg=cfg)
    assert agitado < tranquilo
    assert agitado * 10.0 == pytest.approx(tranquilo * 2.0, rel=1e-2)


def test_fixed_fraction_invierte_su_fraccion_del_capital(cfg) -> None:
    p = cartera(1000.0)
    r = riesgo(sizing=SizingKind.FIXED_FRACTION, max_exposure=0.5)
    amount = p.size_order(r, price=100.0, atr=5.0, realized_vol=0.5, cfg=cfg)
    assert amount * 100.0 == pytest.approx(500.0, rel=1e-3)


def test_vol_target_compra_menos_cuando_la_volatilidad_realizada_sube(cfg) -> None:
    p = cartera(1000.0)
    r = riesgo(sizing=SizingKind.VOL_TARGET, risk_per_trade=0.01)
    calmado = p.size_order(r, price=100.0, atr=5.0, realized_vol=0.30, cfg=cfg)
    nervioso = p.size_order(r, price=100.0, atr=5.0, realized_vol=1.20, cfg=cfg)
    assert nervioso < calmado
    assert calmado / max(nervioso, 1e-9) == pytest.approx(4.0, rel=0.05)


def test_el_tamano_respeta_la_exposicion_maxima(cfg) -> None:
    p = cartera(1000.0)
    r = riesgo(sizing=SizingKind.ATR_RISK, risk_per_trade=0.02,
               stop=StopGene(StopKind.ATR_MULT, 0.5), max_exposure=0.30)
    amount = p.size_order(r, price=100.0, atr=1.0, realized_vol=0.5, cfg=cfg)
    assert amount * 100.0 <= 300.0 + 1e-6


def test_el_tamano_nunca_deja_la_caja_en_negativo(cfg) -> None:
    p = cartera(100.0)
    r = riesgo(sizing=SizingKind.FIXED_FRACTION, max_exposure=1.0)
    amount = p.size_order(r, price=100.0, atr=1.0, realized_vol=0.5, cfg=cfg)
    assert amount * 100.0 <= 100.0


def test_por_debajo_del_minimo_no_se_abre_nada(cfg) -> None:
    p = cartera(5.0)
    r = riesgo(sizing=SizingKind.FIXED_FRACTION, max_exposure=1.0)
    assert p.size_order(r, price=100.0, atr=1.0, realized_vol=0.5, cfg=cfg) == 0.0


def test_no_se_abren_mas_posiciones_de_las_permitidas(cfg) -> None:
    p = cartera(1000.0)
    abierta(p, price=100.0, amount=1.0)
    r = riesgo(max_concurrent_positions=1)
    assert p.size_order(r, price=100.0, atr=5.0, realized_vol=0.5, cfg=cfg) == 0.0


def test_sin_capital_no_se_abre_nada(cfg) -> None:
    p = Portfolio(bot_id="bot_test", cash=0.0, initial_capital=1000.0)
    r = riesgo(sizing=SizingKind.FIXED_FRACTION, max_exposure=1.0)
    assert p.size_order(r, price=100.0, atr=1.0, realized_vol=0.5, cfg=cfg) == 0.0


def test_sin_stop_el_atr_risk_cae_a_fraccion_fija(cfg) -> None:
    p = cartera(1000.0)
    r = riesgo(sizing=SizingKind.ATR_RISK, stop=StopGene(StopKind.NONE, 0.0), max_exposure=0.4)
    amount = p.size_order(r, price=100.0, atr=5.0, realized_vol=0.5, cfg=cfg)
    assert amount * 100.0 == pytest.approx(400.0, rel=1e-3)


# --------------------------------------------------------------------------- #
# Apertura y cierre                                                            #
# --------------------------------------------------------------------------- #


def test_al_abrir_se_fijan_stop_y_riesgo_inicial() -> None:
    p = cartera(1000.0)
    pos = abierta(p, price=100.0, amount=2.0,
                  risk=riesgo(stop=StopGene(StopKind.ATR_MULT, 2.0)), atr=5.0)
    assert pos.stop_price == pytest.approx(90.0)
    assert pos.initial_risk == pytest.approx(10.0)


def test_el_take_profit_en_multiplos_de_r() -> None:
    p = cartera(1000.0)
    pos = abierta(p, price=100.0, amount=1.0, atr=5.0, risk=riesgo(
        stop=StopGene(StopKind.ATR_MULT, 2.0),
        take_profit=TakeProfitGene(TakeProfitKind.R_MULTIPLE, 3.0),
    ))
    assert pos.take_price == pytest.approx(130.0)      # 100 + 3 * 10


def test_el_take_profit_porcentual() -> None:
    p = cartera(1000.0)
    pos = abierta(p, price=100.0, amount=1.0, risk=riesgo(
        take_profit=TakeProfitGene(TakeProfitKind.PERCENT, 0.08)))
    assert pos.take_price == pytest.approx(108.0)


def test_un_stop_de_corto_va_por_encima_del_precio() -> None:
    p = cartera(1000.0)
    pos = abierta(p, price=100.0, amount=1.0, side=Side.SHORT, atr=5.0,
                  risk=riesgo(allow_short=True, stop=StopGene(StopKind.ATR_MULT, 2.0)))
    assert pos.stop_price == pytest.approx(110.0)


def test_cerrar_una_posicion_produce_la_operacion() -> None:
    p = cartera(1000.0)
    abierta(p, price=100.0, amount=2.0)
    p.mark_to_market(100.0, TS + H)
    p.apply_fill(fill(OrderKind.EXIT_SIGNAL, Side.LONG, 120.0, 2.0, fee=0.24, ts=TS + H))

    assert p.positions == []
    op = p.closed_trades[-1]
    assert op["entry_price"] == pytest.approx(100.0)
    assert op["exit_price"] == pytest.approx(120.0)
    assert op["gross_pnl"] == pytest.approx(40.0)
    assert op["fees"] == pytest.approx(0.24)
    assert op["pnl"] == pytest.approx(39.76)
    assert op["return"] == pytest.approx(39.76 / 200.0)
    assert op["bars_held"] == 1
    assert op["exit_kind"] == "EXIT_SIGNAL"


def test_la_operacion_recoge_tambien_la_comision_de_entrada() -> None:
    p = cartera(1000.0)
    p.apply_fill(fill(OrderKind.ENTRY, Side.LONG, 100.0, 2.0, fee=0.2), risk=riesgo(), atr=5.0)
    p.apply_fill(fill(OrderKind.EXIT_SIGNAL, Side.LONG, 110.0, 2.0, fee=0.22, ts=TS + H))
    op = p.closed_trades[-1]
    assert op["fees"] == pytest.approx(0.42)
    assert op["pnl"] == pytest.approx(20.0 - 0.42)


def test_cerrar_un_corto() -> None:
    p = cartera(1000.0)
    abierta(p, price=100.0, amount=2.0, side=Side.SHORT, risk=riesgo(allow_short=True))
    p.apply_fill(fill(OrderKind.EXIT_SIGNAL, Side.SHORT, 90.0, 2.0, ts=TS + H))
    assert p.closed_trades[-1]["gross_pnl"] == pytest.approx(20.0)
    assert p.cash == pytest.approx(1020.0)


def test_la_caja_cuadra_tras_abrir_y_cerrar() -> None:
    p = cartera(1000.0)
    p.apply_fill(fill(OrderKind.ENTRY, Side.LONG, 100.0, 5.0, fee=0.5), risk=riesgo(), atr=5.0)
    assert p.cash == pytest.approx(1000.0 - 500.0 - 0.5)
    p.apply_fill(fill(OrderKind.EXIT_SIGNAL, Side.LONG, 110.0, 5.0, fee=0.55, ts=TS + H))
    assert p.cash == pytest.approx(1000.0 - 500.0 - 0.5 + 550.0 - 0.55)
    assert p.equity(110.0) == pytest.approx(p.initial_capital + p.closed_trades[-1]["pnl"])


def test_una_salida_sin_posicion_no_hace_nada() -> None:
    p = cartera(1000.0)
    p.apply_fill(fill(OrderKind.EXIT_SIGNAL, Side.LONG, 110.0, 1.0))
    assert p.cash == pytest.approx(1000.0)
    assert p.closed_trades == []


# --------------------------------------------------------------------------- #
# Salidas                                                                      #
# --------------------------------------------------------------------------- #


def test_el_stop_salta_cuando_el_minimo_lo_toca() -> None:
    p = cartera(1000.0)
    abierta(p, price=100.0, amount=1.0, atr=5.0)          # stop en 90
    salidas = p.check_exits(high=101.0, low=89.0, close=95.0, ts=TS + H, risk=riesgo())
    assert [(k, precio) for _, k, precio in salidas] == [(OrderKind.EXIT_STOP, 90.0)]


def test_el_stop_no_salta_si_el_minimo_no_llega() -> None:
    p = cartera(1000.0)
    abierta(p, price=100.0, amount=1.0, atr=5.0)
    assert p.check_exits(high=105.0, low=91.0, close=103.0, ts=TS + H, risk=riesgo()) == []


def test_el_take_profit_salta_cuando_el_maximo_lo_toca() -> None:
    p = cartera(1000.0)
    r = riesgo(take_profit=TakeProfitGene(TakeProfitKind.PERCENT, 0.10))
    abierta(p, price=100.0, amount=1.0, atr=5.0, risk=r)
    salidas = p.check_exits(high=112.0, low=99.0, close=111.0, ts=TS + H, risk=r)
    assert [k for _, k, _ in salidas] == [OrderKind.EXIT_TAKE_PROFIT]
    assert salidas[0][2] == pytest.approx(110.0)


def test_si_una_vela_toca_el_stop_y_el_take_profit_gana_el_stop() -> None:
    """No hay forma de saber el orden intrabarra en una vela de 1h, y suponer
    lo favorable infla sistemáticamente cualquier backtest."""
    p = cartera(1000.0)
    r = riesgo(stop=StopGene(StopKind.ATR_MULT, 2.0),
               take_profit=TakeProfitGene(TakeProfitKind.PERCENT, 0.10))
    abierta(p, price=100.0, amount=1.0, atr=5.0, risk=r)
    salidas = p.check_exits(high=115.0, low=85.0, close=100.0, ts=TS + H, risk=r)
    assert [k for _, k, _ in salidas] == [OrderKind.EXIT_STOP]


def test_el_time_stop_cierra_tras_las_velas_del_genoma() -> None:
    p = cartera(1000.0)
    r = riesgo(max_holding_bars=3, stop=StopGene(StopKind.NONE, 0.0))
    pos = abierta(p, price=100.0, amount=1.0, risk=r)
    for i in range(3):
        assert p.check_exits(101.0, 99.0, 100.0, TS + (i + 1) * H, r) == []
        p.mark_to_market(100.0, TS + (i + 1) * H)
    assert pos.bars_held == 3
    salidas = p.check_exits(101.0, 99.0, 100.0, TS + 4 * H, r)
    assert [k for _, k, _ in salidas] == [OrderKind.EXIT_TIME]


def test_el_trailing_no_se_activa_hasta_su_multiplo_de_r() -> None:
    p = cartera(1000.0)
    r = riesgo(stop=StopGene(StopKind.ATR_MULT, 2.0),
               trailing=TrailingGene(TrailingKind.ATR_MULT, 1.0, activate_at_r=2.0))
    pos = abierta(p, price=100.0, amount=1.0, atr=5.0, risk=r)

    p.check_exits(high=110.0, low=105.0, close=108.0, ts=TS + H, risk=r)   # sólo 1R
    assert pos.stop_price == pytest.approx(90.0)

    p.check_exits(high=125.0, low=120.0, close=124.0, ts=TS + 2 * H, risk=r)  # 2.5R
    assert pos.stop_price == pytest.approx(120.0)      # 125 - 1 * ATR


def test_el_trailing_solo_se_mueve_a_favor() -> None:
    p = cartera(1000.0)
    r = riesgo(stop=StopGene(StopKind.ATR_MULT, 2.0),
               trailing=TrailingGene(TrailingKind.ATR_MULT, 1.0, activate_at_r=1.0))
    pos = abierta(p, price=100.0, amount=1.0, atr=5.0, risk=r)
    p.check_exits(high=130.0, low=120.0, close=128.0, ts=TS + H, risk=r)
    subido = pos.stop_price
    p.check_exits(high=115.0, low=110.0, close=112.0, ts=TS + 2 * H, risk=r)
    assert pos.stop_price == pytest.approx(subido)


def test_al_saltar_el_trailing_la_salida_se_marca_como_tal() -> None:
    p = cartera(1000.0)
    r = riesgo(stop=StopGene(StopKind.ATR_MULT, 2.0),
               trailing=TrailingGene(TrailingKind.ATR_MULT, 1.0, activate_at_r=1.0))
    abierta(p, price=100.0, amount=1.0, atr=5.0, risk=r)
    p.check_exits(high=130.0, low=125.0, close=128.0, ts=TS + H, risk=r)     # sube a 125
    salidas = p.check_exits(high=127.0, low=120.0, close=121.0, ts=TS + 2 * H, risk=r)
    assert [k for _, k, _ in salidas] == [OrderKind.EXIT_TRAILING]


def test_el_trailing_se_comprueba_antes_de_moverse() -> None:
    """Dentro de una vela no se sabe si primero se hizo el máximo o el mínimo:
    mover el trailing con el máximo de la vela y comprobarlo después regalaría
    salidas que nunca ocurrieron."""
    p = cartera(1000.0)
    r = riesgo(stop=StopGene(StopKind.ATR_MULT, 2.0),
               trailing=TrailingGene(TrailingKind.ATR_MULT, 1.0, activate_at_r=1.0))
    pos = abierta(p, price=100.0, amount=1.0, atr=5.0, risk=r)
    salidas = p.check_exits(high=130.0, low=110.0, close=112.0, ts=TS + H, risk=r)
    assert salidas == []                       # el trailing aún no existía
    assert pos.stop_price == pytest.approx(125.0)


def test_una_cartera_sin_posiciones_no_tiene_salidas() -> None:
    assert cartera().check_exits(100.0, 90.0, 95.0, TS, riesgo()) == []


def test_el_stop_de_un_corto_salta_con_el_maximo() -> None:
    p = cartera(1000.0)
    r = riesgo(allow_short=True, stop=StopGene(StopKind.ATR_MULT, 2.0))
    abierta(p, price=100.0, amount=1.0, side=Side.SHORT, atr=5.0, risk=r)
    salidas = p.check_exits(high=111.0, low=99.0, close=110.0, ts=TS + H, risk=r)
    assert [(k, precio) for _, k, precio in salidas] == [(OrderKind.EXIT_STOP, 110.0)]


# --------------------------------------------------------------------------- #
# Paso del tiempo                                                              #
# --------------------------------------------------------------------------- #


def test_marcar_a_mercado_envejece_las_posiciones() -> None:
    p = cartera(1000.0)
    pos = abierta(p, price=100.0, amount=1.0)
    p.mark_to_market(105.0, TS + H)
    p.mark_to_market(106.0, TS + 2 * H)
    assert pos.bars_held == 2


def test_marcar_a_mercado_actualiza_el_maximo() -> None:
    p = cartera(1000.0)
    abierta(p, price=100.0, amount=5.0)
    p.mark_to_market(120.0, TS + H)
    assert p.peak_equity == pytest.approx(1100.0)
    p.mark_to_market(90.0, TS + 2 * H)
    assert p.peak_equity == pytest.approx(1100.0)
