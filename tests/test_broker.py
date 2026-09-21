"""Broker simulado: fricción, fills y la convención pesimista.

La regla que gobierna todo: la decisión se toma con la vela `t` cerrada y el
fill ocurre en la apertura de `t+1`. Lo demás son las tres formas de mentirse a
uno mismo en un backtest —rellenar al cierre de la vela que dio la señal,
cobrar el stop a su precio cuando el mercado abrió más abajo, y suponer que en
una vela saltó primero lo que nos convenía— y aquí están las tres cerradas.
"""

from __future__ import annotations

import pytest

from keepgarden.config import FrictionConfig
from keepgarden.engine.broker import Fill, OrderRequest, PaperBroker
from keepgarden.types import OrderKind, Side

TS = 1_546_300_800_000
H = 3_600_000


def friccion(**cambios) -> FrictionConfig:
    base = {
        "taker_fee_bps": 10.0,
        "maker_fee_bps": 10.0,
        "slippage_model": "fixed_bps",
        "slippage_bps": 0.0,
        "slippage_atr_frac": 0.05,
        "min_notional": 10.0,
        "price_precision": 8,
        "amount_precision": 8,
    }
    base.update(cambios)
    return FrictionConfig(**base)


def vela(open_: float, high: float, low: float, close: float, *, ts: int = TS + H,
         atr: float = 0.0, volume: float = 1000.0) -> dict[str, float]:
    return {
        "ts": ts, "open": open_, "high": high, "low": low, "close": close,
        "volume": volume, "atr": atr,
    }


def orden(kind: OrderKind, side: Side, amount: float = 1.0,
          trigger: float | None = None) -> OrderRequest:
    return OrderRequest(
        bot_id="bot_test", candle_ts=TS, kind=kind, side=side,
        amount=amount, trigger_price=trigger,
    )


def unico(fills: list[Fill]) -> Fill:
    assert len(fills) == 1, f"se esperaba un fill y hay {len(fills)}"
    return fills[0]


# --------------------------------------------------------------------------- #
# Órdenes a mercado                                                            #
# --------------------------------------------------------------------------- #


def test_una_compra_a_mercado_se_rellena_en_la_apertura_siguiente() -> None:
    b = PaperBroker(frictions=friccion())
    b.submit(orden(OrderKind.ENTRY, Side.LONG, 2.0))
    f = unico(b.settle(vela(100.0, 105.0, 99.0, 104.0)))

    assert f.reference_price == 100.0
    assert f.price == 100.0
    assert f.amount == 2.0
    assert f.notional == 200.0
    assert f.fill_ts == TS + H
    assert f.candle_ts == TS          # la vela que tomó la decisión


def test_nunca_se_rellena_al_cierre_de_la_vela_que_dio_la_senal() -> None:
    b = PaperBroker(frictions=friccion())
    b.submit(orden(OrderKind.ENTRY, Side.LONG))
    f = unico(b.settle(vela(100.0, 130.0, 99.0, 128.0)))
    assert f.reference_price == 100.0     # la apertura, no el cierre de 128


def test_la_comision_se_cobra_sobre_el_notional() -> None:
    b = PaperBroker(frictions=friccion(taker_fee_bps=10.0))
    b.submit(orden(OrderKind.ENTRY, Side.LONG, 2.0))
    f = unico(b.settle(vela(100.0, 105.0, 99.0, 104.0)))
    assert f.fee == pytest.approx(200.0 * 0.001)


def test_la_comision_tambien_se_cobra_al_salir() -> None:
    b = PaperBroker(frictions=friccion())
    b.submit(orden(OrderKind.EXIT_SIGNAL, Side.LONG, 2.0))
    f = unico(b.settle(vela(100.0, 105.0, 99.0, 104.0)))
    assert f.fee == pytest.approx(0.2)


def test_la_cola_se_vacia_al_rellenar() -> None:
    b = PaperBroker(frictions=friccion())
    b.submit(orden(OrderKind.ENTRY, Side.LONG))
    b.settle(vela(100.0, 105.0, 99.0, 104.0))
    assert b.settle(vela(101.0, 105.0, 99.0, 104.0)) == []


def test_una_orden_de_cantidad_cero_no_produce_fill() -> None:
    b = PaperBroker(frictions=friccion())
    b.submit(orden(OrderKind.ENTRY, Side.LONG, 0.0))
    assert b.settle(vela(100.0, 105.0, 99.0, 104.0)) == []


# --------------------------------------------------------------------------- #
# Dirección del deslizamiento                                                  #
# --------------------------------------------------------------------------- #


def test_el_deslizamiento_siempre_va_en_contra_al_comprar() -> None:
    b = PaperBroker(frictions=friccion(slippage_bps=10.0))
    b.submit(orden(OrderKind.ENTRY, Side.LONG))
    f = unico(b.settle(vela(100.0, 105.0, 99.0, 104.0)))
    assert f.price == pytest.approx(100.1)       # se paga más
    assert f.slippage == pytest.approx(0.1)


def test_el_deslizamiento_siempre_va_en_contra_al_vender() -> None:
    b = PaperBroker(frictions=friccion(slippage_bps=10.0))
    b.submit(orden(OrderKind.EXIT_SIGNAL, Side.LONG))
    f = unico(b.settle(vela(100.0, 105.0, 99.0, 104.0)))
    assert f.price == pytest.approx(99.9)        # se cobra menos


def test_abrir_un_corto_vende_y_cerrarlo_compra() -> None:
    b = PaperBroker(frictions=friccion(slippage_bps=10.0))
    b.submit(orden(OrderKind.ENTRY, Side.SHORT))
    abre = unico(b.settle(vela(100.0, 105.0, 99.0, 104.0)))
    b.submit(orden(OrderKind.EXIT_SIGNAL, Side.SHORT))
    cierra = unico(b.settle(vela(100.0, 105.0, 99.0, 104.0)))
    assert abre.price == pytest.approx(99.9)
    assert cierra.price == pytest.approx(100.1)


# --------------------------------------------------------------------------- #
# Modelos de deslizamiento                                                     #
# --------------------------------------------------------------------------- #


def test_modelo_fixed_bps() -> None:
    b = PaperBroker(frictions=friccion(slippage_model="fixed_bps", slippage_bps=25.0))
    assert b.slippage_for(200.0, atr=5.0, volume=1000.0, amount=1.0) == pytest.approx(0.5)


def test_modelo_atr() -> None:
    b = PaperBroker(frictions=friccion(slippage_model="atr", slippage_atr_frac=0.05))
    assert b.slippage_for(200.0, atr=4.0, volume=1000.0, amount=1.0) == pytest.approx(0.2)


def test_el_modelo_atr_escala_con_la_volatilidad() -> None:
    """Es lo que lo hace más honesto que un número fijo de puntos básicos: el
    coste de cruzar el spread sube cuando el mercado se mueve."""
    b = PaperBroker(frictions=friccion(slippage_model="atr"))
    tranquilo = b.slippage_for(200.0, atr=1.0, volume=1000.0, amount=1.0)
    agitado = b.slippage_for(200.0, atr=10.0, volume=1000.0, amount=1.0)
    assert agitado > tranquilo * 5


def test_el_modelo_atr_sin_atr_cae_al_de_puntos_basicos() -> None:
    b = PaperBroker(frictions=friccion(slippage_model="atr", slippage_bps=3.0))
    assert b.slippage_for(200.0, atr=0.0, volume=1000.0, amount=1.0) == pytest.approx(0.06)


def test_modelo_volume_aware_penaliza_las_ordenes_grandes() -> None:
    b = PaperBroker(frictions=friccion(slippage_model="volume_aware", slippage_atr_frac=0.05))
    pequena = b.slippage_for(200.0, atr=4.0, volume=1000.0, amount=1.0)
    grande = b.slippage_for(200.0, atr=4.0, volume=1000.0, amount=500.0)
    assert grande > pequena


def test_el_deslizamiento_nunca_es_negativo() -> None:
    b = PaperBroker(frictions=friccion(slippage_model="atr"))
    assert b.slippage_for(100.0, atr=-5.0, volume=0.0, amount=1.0) >= 0.0


# --------------------------------------------------------------------------- #
# Stops: la convención pesimista                                               #
# --------------------------------------------------------------------------- #


def test_un_stop_normal_se_rellena_en_su_precio() -> None:
    b = PaperBroker(frictions=friccion(slippage_bps=10.0))
    b.submit(orden(OrderKind.EXIT_STOP, Side.LONG, trigger=95.0))
    f = unico(b.settle(vela(100.0, 101.0, 94.0, 96.0)))
    assert f.reference_price == 95.0
    assert f.price == pytest.approx(95.0 * (1 - 0.001))


def test_el_fill_del_stop_no_es_el_minimo_de_la_vela() -> None:
    """Cobrar el stop al low sería regalarse el peor precio; cobrarlo al low
    sería regalarse el mejor. Es su precio, ni uno ni otro."""
    b = PaperBroker(frictions=friccion())
    b.submit(orden(OrderKind.EXIT_STOP, Side.LONG, trigger=95.0))
    f = unico(b.settle(vela(100.0, 101.0, 80.0, 96.0)))
    assert f.reference_price == 95.0


def test_un_hueco_de_apertura_por_debajo_del_stop_rellena_en_la_apertura() -> None:
    """Los gaps existen y cuestan dinero: si la vela abre en 90 con el stop en
    95, no hay forma de salir en 95."""
    b = PaperBroker(frictions=friccion())
    b.submit(orden(OrderKind.EXIT_STOP, Side.LONG, trigger=95.0))
    f = unico(b.settle(vela(90.0, 92.0, 88.0, 91.0)))
    assert f.reference_price == 90.0


def test_un_hueco_al_alza_no_regala_nada_en_un_stop_de_corto() -> None:
    b = PaperBroker(frictions=friccion())
    b.submit(orden(OrderKind.EXIT_STOP, Side.SHORT, trigger=105.0))
    f = unico(b.settle(vela(112.0, 115.0, 110.0, 114.0)))
    assert f.reference_price == 112.0


def test_el_take_profit_se_cobra_a_su_precio_aunque_la_vela_abra_mejor() -> None:
    """Pesimista también aquí: el hueco a favor no se cobra."""
    b = PaperBroker(frictions=friccion())
    b.submit(orden(OrderKind.EXIT_TAKE_PROFIT, Side.LONG, trigger=110.0))
    f = unico(b.settle(vela(115.0, 118.0, 114.0, 117.0)))
    assert f.reference_price == 110.0


def test_el_trailing_se_rellena_como_un_stop() -> None:
    b = PaperBroker(frictions=friccion())
    b.submit(orden(OrderKind.EXIT_TRAILING, Side.LONG, trigger=105.0))
    f = unico(b.settle(vela(107.0, 108.0, 104.0, 106.0)))
    assert f.reference_price == 105.0


def test_un_trailing_ya_rebasado_a_la_apertura_tambien_es_un_hueco() -> None:
    b = PaperBroker(frictions=friccion())
    b.submit(orden(OrderKind.EXIT_TRAILING, Side.LONG, trigger=105.0))
    f = unico(b.settle(vela(100.0, 103.0, 99.0, 102.0)))
    assert f.reference_price == 100.0


def test_una_salida_por_tiempo_es_una_orden_a_mercado() -> None:
    b = PaperBroker(frictions=friccion())
    b.submit(orden(OrderKind.EXIT_TIME, Side.LONG))
    f = unico(b.settle(vela(100.0, 105.0, 99.0, 104.0)))
    assert f.reference_price == 100.0


# --------------------------------------------------------------------------- #
# Mínimos y precisión                                                          #
# --------------------------------------------------------------------------- #


def test_una_entrada_por_debajo_del_minimo_se_rechaza() -> None:
    b = PaperBroker(frictions=friccion(min_notional=50.0))
    b.submit(orden(OrderKind.ENTRY, Side.LONG, 0.1))     # 10 USDT
    assert b.settle(vela(100.0, 105.0, 99.0, 104.0)) == []


def test_una_salida_pequena_nunca_se_rechaza() -> None:
    """Rechazar una salida dejaría la posición atrapada para siempre."""
    b = PaperBroker(frictions=friccion(min_notional=50.0))
    b.submit(orden(OrderKind.EXIT_SIGNAL, Side.LONG, 0.1))
    f = unico(b.settle(vela(100.0, 105.0, 99.0, 104.0)))
    assert f.amount == pytest.approx(0.1)


def test_precios_y_cantidades_se_redondean_a_la_precision_del_venue() -> None:
    b = PaperBroker(frictions=friccion(price_precision=2, amount_precision=3,
                                       slippage_model="atr", slippage_atr_frac=0.05))
    b.submit(orden(OrderKind.ENTRY, Side.LONG, 1.23456789))
    f = unico(b.settle(vela(100.123456, 105.0, 99.0, 104.0, atr=1.0)))
    # se trunca, no se redondea: redondear hacia arriba compraría más de lo
    # que hay en caja
    assert f.amount == pytest.approx(1.234)
    assert f.price == pytest.approx(round(f.price, 2))


def test_truncar_la_cantidad_no_sufre_los_artefactos_del_binario() -> None:
    b = PaperBroker(frictions=friccion(amount_precision=3))
    b.submit(orden(OrderKind.ENTRY, Side.LONG, 0.123))
    f = unico(b.settle(vela(100.0, 105.0, 99.0, 104.0)))
    assert f.amount == pytest.approx(0.123)


def test_el_notional_es_coherente_con_el_precio_y_la_cantidad_finales() -> None:
    b = PaperBroker(frictions=friccion(price_precision=2, amount_precision=3,
                                       slippage_bps=7.0))
    b.submit(orden(OrderKind.ENTRY, Side.LONG, 2.5))
    f = unico(b.settle(vela(100.0, 105.0, 99.0, 104.0)))
    assert f.notional == pytest.approx(f.price * f.amount)


# --------------------------------------------------------------------------- #
# Varias órdenes                                                               #
# --------------------------------------------------------------------------- #


def test_se_rellenan_todas_las_ordenes_encoladas() -> None:
    b = PaperBroker(frictions=friccion())
    b.submit(orden(OrderKind.EXIT_SIGNAL, Side.LONG, 1.0))
    b.submit(orden(OrderKind.ENTRY, Side.LONG, 2.0))
    fills = b.settle(vela(100.0, 105.0, 99.0, 104.0))
    assert [f.kind for f in fills] == [OrderKind.EXIT_SIGNAL, OrderKind.ENTRY]


def test_las_salidas_se_rellenan_antes_que_las_entradas() -> None:
    """Si en la misma vela se cierra y se abre, primero entra el dinero."""
    b = PaperBroker(frictions=friccion())
    b.submit(orden(OrderKind.ENTRY, Side.LONG, 2.0))
    b.submit(orden(OrderKind.EXIT_SIGNAL, Side.LONG, 1.0))
    fills = b.settle(vela(100.0, 105.0, 99.0, 104.0))
    assert fills[0].kind is OrderKind.EXIT_SIGNAL


def test_cancelar_vacia_la_cola() -> None:
    b = PaperBroker(frictions=friccion())
    b.submit(orden(OrderKind.ENTRY, Side.LONG))
    b.cancel_all()
    assert b.settle(vela(100.0, 105.0, 99.0, 104.0)) == []
