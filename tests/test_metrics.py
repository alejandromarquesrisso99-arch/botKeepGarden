"""Métricas de rendimiento y riesgo.

Matemática financiera pura, y el sitio donde un error silencioso hace más daño:
contamina el fitness, la selección y todas las decisiones del jardinero durante
meses sin que nada falle visiblemente. Por eso cada fórmula se comprueba contra
un número calculado a mano o deducido aparte con numpy, nunca contra lo que
devuelve la propia implementación.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from keepgarden.evaluation.metrics import (
    MAX_RATIO,
    Metrics,
    compute_metrics,
    drawdown_series,
    population_correlations,
    rolling_correlation,
)
from keepgarden.types import PERIODS_PER_YEAR

PPY = PERIODS_PER_YEAR["1h"]


def operacion(
    *,
    pnl: float,
    notional: float = 1000.0,
    fees: float = 0.0,
    bars: int = 10,
    entry_ts: int = 0,
    side: str = "LONG",
) -> dict[str, object]:
    """Una operación cerrada, en el formato que produce el backtest."""
    return {
        "entry_ts": entry_ts,
        "exit_ts": entry_ts + bars * 3_600_000,
        "side": side,
        "entry_price": 100.0,
        "exit_price": 100.0 + pnl,
        "amount": notional / 100.0,
        "notional": notional,
        "gross_pnl": pnl + fees,
        "fees": fees,
        "pnl": pnl,
        "return": pnl / notional,
        "bars_held": bars,
        "exit_kind": "EXIT_SIGNAL",
    }


# --------------------------------------------------------------------------- #
# Drawdown                                                                     #
# --------------------------------------------------------------------------- #


def test_drawdown_desde_el_maximo() -> None:
    dd = drawdown_series(np.array([100.0, 120.0, 90.0, 150.0]))
    np.testing.assert_allclose(dd, [0.0, 0.0, 0.25, 0.0])


def test_el_drawdown_nunca_es_negativo() -> None:
    dd = drawdown_series(np.array([100.0, 110.0, 120.0, 130.0]))
    assert (dd == 0.0).all()


def test_drawdown_de_una_serie_vacia() -> None:
    assert drawdown_series(np.array([])).size == 0


def test_drawdown_con_equity_a_cero_no_divide_por_cero() -> None:
    dd = drawdown_series(np.array([100.0, 0.0, 0.0]))
    assert np.isfinite(dd).all()
    assert dd[1] == pytest.approx(1.0)


# --------------------------------------------------------------------------- #
# Retorno y riesgo                                                             #
# --------------------------------------------------------------------------- #


def test_retorno_total() -> None:
    m = compute_metrics(np.array([1000.0, 1100.0, 1200.0]), [], "1h")
    assert m.total_return == pytest.approx(0.2)


def test_cagr_anualiza_con_los_periodos_del_timeframe() -> None:
    # un año entero de velas de 1h duplicando el capital
    n = int(PPY)
    equity = np.linspace(1000.0, 2000.0, n)
    m = compute_metrics(equity, [], "1h")
    assert m.cagr == pytest.approx(1.0, rel=1e-3)


def test_cagr_de_media_duplicacion_en_medio_ano() -> None:
    n = int(PPY // 2)
    equity = np.linspace(1000.0, 2000.0, n)
    m = compute_metrics(equity, [], "1h")
    assert m.cagr == pytest.approx(3.0, rel=1e-2)      # (2x)^2 - 1


def test_max_drawdown_y_ulcer() -> None:
    m = compute_metrics(np.array([100.0, 120.0, 90.0, 150.0]), [], "1h")
    assert m.max_drawdown == pytest.approx(0.25)
    assert m.ulcer_index == pytest.approx(math.sqrt(0.25**2 / 4))


def test_el_ulcer_castiga_los_drawdowns_largos() -> None:
    """Dos series con el mismo drawdown máximo: la que tarda en recuperarse
    duele más, y el ulcer es lo único que lo nota."""
    rapida = np.array([100.0, 80.0, 100.0, 100.0, 100.0, 100.0])
    lenta = np.array([100.0, 80.0, 82.0, 85.0, 90.0, 100.0])
    a = compute_metrics(rapida, [], "1h")
    b = compute_metrics(lenta, [], "1h")
    assert a.max_drawdown == pytest.approx(b.max_drawdown)
    assert b.ulcer_index > a.ulcer_index


def test_downside_dev_solo_mira_los_retornos_negativos() -> None:
    equity = np.array([100.0, 110.0, 99.0])
    m = compute_metrics(equity, [], "1h")
    r = np.diff(equity) / equity[:-1]
    esperado = math.sqrt(np.mean(np.minimum(r, 0.0) ** 2))
    assert m.downside_dev == pytest.approx(esperado)


def test_sin_retornos_negativos_el_downside_dev_es_cero() -> None:
    m = compute_metrics(np.array([100.0, 110.0, 120.0]), [], "1h")
    assert m.downside_dev == 0.0


# --------------------------------------------------------------------------- #
# Ajustadas por riesgo                                                         #
# --------------------------------------------------------------------------- #


def paseo(sigma: float, *, mu: float = 0.00005, n: int = 3000, seed: int = 3) -> np.ndarray:
    """Curva de capital con forma de mercado. Con sigma realista, los cocientes
    anualizados caen en el rango normal y no chocan con el tope."""
    rng = np.random.default_rng(seed)
    return 1000.0 * np.exp(np.cumsum(rng.normal(mu, sigma, n)))


def test_sharpe_anualizado() -> None:
    equity = paseo(0.006)
    m = compute_metrics(equity, [], "1h")
    r = np.diff(equity) / equity[:-1]
    esperado = r.mean() / r.std(ddof=0) * math.sqrt(PPY)
    assert abs(esperado) < MAX_RATIO
    assert m.sharpe == pytest.approx(esperado)


def test_sortino_usa_el_downside_dev() -> None:
    equity = paseo(0.006)
    m = compute_metrics(equity, [], "1h")
    r = np.diff(equity) / equity[:-1]
    esperado = r.mean() / math.sqrt(np.mean(np.minimum(r, 0.0) ** 2)) * math.sqrt(PPY)
    assert abs(esperado) < MAX_RATIO
    assert m.sortino == pytest.approx(esperado)


def test_el_sortino_premia_la_misma_ganancia_con_menos_sustos() -> None:
    """Misma deriva, distinta volatilidad: el Sortino tiene que notarlo."""
    suave = compute_metrics(paseo(0.0015), [], "1h")
    brusca = compute_metrics(paseo(0.006), [], "1h")
    assert suave.sortino > brusca.sortino
    assert suave.sortino < MAX_RATIO


def test_sin_perdidas_el_sortino_no_es_infinito() -> None:
    m = compute_metrics(np.array([100.0, 110.0, 120.0]), [], "1h")
    assert np.isfinite(m.sortino)
    assert m.sortino == MAX_RATIO


def test_calmar_es_cagr_entre_drawdown() -> None:
    # un año de velas de 1h: cae un 20 % y acaba un 20 % arriba
    equity = np.concatenate([
        np.linspace(1000.0, 800.0, 2000),
        np.linspace(800.0, 1200.0, int(PPY) - 2000),
    ])
    m = compute_metrics(equity, [], "1h")
    assert m.max_drawdown == pytest.approx(0.2)
    assert abs(m.cagr / m.max_drawdown) < MAX_RATIO
    assert m.calmar == pytest.approx(m.cagr / m.max_drawdown)


def test_sin_drawdown_el_calmar_se_recorta() -> None:
    """Un bot con dos operaciones afortunadas y cero drawdown no puede dominar
    el ranking con un calmar infinito."""
    m = compute_metrics(np.linspace(1000.0, 1100.0, 500), [], "1h")
    assert m.max_drawdown == 0.0
    assert m.calmar == MAX_RATIO


def test_martin_es_cagr_entre_ulcer() -> None:
    equity = np.concatenate([
        np.linspace(1000.0, 800.0, 2000),
        np.linspace(800.0, 1200.0, int(PPY) - 2000),
    ])
    m = compute_metrics(equity, [], "1h")
    assert abs(m.cagr / m.ulcer_index) < MAX_RATIO
    assert m.martin == pytest.approx(m.cagr / m.ulcer_index)


def test_ningun_cociente_se_desboca() -> None:
    """Todos los cocientes viven en [-MAX_RATIO, MAX_RATIO]: así un bot con dos
    operaciones afortunadas no domina el ranking para siempre."""
    equity = np.linspace(1000.0, 5000.0, 200)      # subida limpia y rápida
    m = compute_metrics(equity, [operacion(pnl=4000.0)], "1h")
    for nombre in ("sharpe", "sortino", "calmar", "martin", "profit_factor", "fee_drag"):
        assert abs(getattr(m, nombre)) <= MAX_RATIO, nombre


def test_el_cagr_de_una_ventana_muy_corta_no_desborda() -> None:
    m = compute_metrics(np.array([1000.0, 1300.0, 1700.0]), [], "1h")
    assert np.isfinite(m.cagr)


def test_profit_factor() -> None:
    trades = [operacion(pnl=100.0), operacion(pnl=50.0), operacion(pnl=-60.0)]
    m = compute_metrics(np.array([1000.0, 1090.0]), trades, "1h")
    assert m.profit_factor == pytest.approx(150.0 / 60.0)


def test_sin_operaciones_perdedoras_el_profit_factor_se_recorta() -> None:
    m = compute_metrics(np.array([1000.0, 1200.0]), [operacion(pnl=200.0)], "1h")
    assert m.profit_factor == MAX_RATIO


def test_sin_operaciones_ganadoras_el_profit_factor_es_cero() -> None:
    m = compute_metrics(np.array([1000.0, 900.0]), [operacion(pnl=-100.0)], "1h")
    assert m.profit_factor == 0.0


# --------------------------------------------------------------------------- #
# Comportamiento                                                               #
# --------------------------------------------------------------------------- #


def test_cuenta_operaciones_y_aciertos() -> None:
    trades = [operacion(pnl=10.0), operacion(pnl=-5.0), operacion(pnl=20.0)]
    m = compute_metrics(np.array([1000.0, 1025.0]), trades, "1h")
    assert m.n_trades == 3
    assert m.win_rate == pytest.approx(2 / 3)


def test_retorno_medio_por_operacion() -> None:
    trades = [operacion(pnl=100.0, notional=1000.0), operacion(pnl=-50.0, notional=1000.0)]
    m = compute_metrics(np.array([1000.0, 1050.0]), trades, "1h")
    assert m.avg_trade_return == pytest.approx((0.10 - 0.05) / 2)


def test_expectancy() -> None:
    trades = [operacion(pnl=100.0), operacion(pnl=100.0), operacion(pnl=-50.0)]
    m = compute_metrics(np.array([1000.0, 1150.0]), trades, "1h")
    # 2/3 de aciertos al 10 % contra 1/3 de fallos al 5 %
    assert m.expectancy == pytest.approx((2 / 3) * 0.10 - (1 / 3) * 0.05)


def test_peor_operacion() -> None:
    trades = [operacion(pnl=10.0), operacion(pnl=-80.0), operacion(pnl=-5.0)]
    m = compute_metrics(np.array([1000.0, 925.0]), trades, "1h")
    assert m.worst_trade == pytest.approx(-0.08)


def test_tiempo_en_mercado() -> None:
    equity = np.ones(100) * 1000.0
    trades = [operacion(pnl=0.0, bars=20), operacion(pnl=0.0, bars=5)]
    m = compute_metrics(equity, trades, "1h")
    assert m.time_in_market == pytest.approx(0.25)


def test_el_tiempo_en_mercado_no_pasa_de_uno() -> None:
    equity = np.ones(10) * 1000.0
    m = compute_metrics(equity, [operacion(pnl=0.0, bars=50)], "1h")
    assert m.time_in_market == 1.0


def test_velas_medias_por_operacion() -> None:
    trades = [operacion(pnl=0.0, bars=10), operacion(pnl=0.0, bars=20)]
    m = compute_metrics(np.ones(100) * 1000.0, trades, "1h")
    assert m.avg_holding_bars == pytest.approx(15.0)


def test_turnover_mide_el_notional_movido() -> None:
    trades = [operacion(pnl=0.0, notional=1000.0), operacion(pnl=0.0, notional=500.0)]
    m = compute_metrics(np.array([1000.0, 1000.0]), trades, "1h")
    assert m.turnover == pytest.approx(1.5)


def test_fee_drag() -> None:
    trades = [operacion(pnl=80.0, fees=20.0)]      # bruto 100, comisiones 20
    m = compute_metrics(np.array([1000.0, 1080.0]), trades, "1h", total_fees=20.0)
    assert m.fee_drag == pytest.approx(0.2)


def test_un_bot_que_vive_para_pagar_comisiones() -> None:
    trades = [operacion(pnl=-10.0, fees=60.0)]     # bruto 50, comisiones 60
    m = compute_metrics(np.array([1000.0, 990.0]), trades, "1h", total_fees=60.0)
    assert m.fee_drag > 1.0


def test_sin_beneficio_bruto_el_fee_drag_esta_acotado() -> None:
    trades = [operacion(pnl=-100.0, fees=10.0)]
    m = compute_metrics(np.array([1000.0, 900.0]), trades, "1h", total_fees=10.0)
    assert np.isfinite(m.fee_drag)
    assert m.fee_drag == MAX_RATIO


def test_consistencia_cuenta_dias_positivos() -> None:
    """Cuatro días de velas de 1h: suben tres y baja uno."""
    dia = 24
    equity = np.concatenate([
        np.linspace(1000.0, 1010.0, dia),
        np.linspace(1010.0, 1020.0, dia),
        np.linspace(1020.0, 1005.0, dia),
        np.linspace(1005.0, 1030.0, dia),
    ])
    m = compute_metrics(equity, [], "1h")
    assert m.consistency == pytest.approx(0.75)


def test_la_consistencia_de_una_ventana_corta_es_el_signo_del_tramo() -> None:
    m = compute_metrics(np.linspace(1000.0, 1100.0, 10), [], "1h")
    assert m.consistency == 1.0


# --------------------------------------------------------------------------- #
# Casos límite                                                                 #
# --------------------------------------------------------------------------- #


def test_una_serie_vacia_no_explota() -> None:
    m = compute_metrics(np.array([]), [], "1h")
    assert m == Metrics()
    assert not m.is_evaluable


def test_una_sola_vela_no_explota() -> None:
    m = compute_metrics(np.array([1000.0]), [], "1h")
    assert m.total_return == 0.0
    assert m.sortino == 0.0


def test_un_bot_que_nunca_opera() -> None:
    m = compute_metrics(np.ones(500) * 1000.0, [], "1h")
    assert m.n_trades == 0
    assert not m.is_evaluable
    assert m.total_return == 0.0
    assert m.max_drawdown == 0.0
    assert m.time_in_market == 0.0


def test_todas_las_metricas_son_finitas() -> None:
    rng = np.random.default_rng(7)
    equity = 1000.0 * np.exp(np.cumsum(rng.normal(0.0001, 0.01, 2000)))
    trades = [operacion(pnl=float(rng.normal(0, 50))) for _ in range(30)]
    m = compute_metrics(equity, trades, "1h", total_fees=45.0)
    for nombre, valor in m.to_dict().items():
        assert np.isfinite(valor), f"{nombre} no es finito: {valor}"


def test_las_metricas_ajustadas_no_dependen_de_la_escala() -> None:
    """Doblar el capital inicial no cambia el Sortino: si lo cambiara, el
    fitness premiaría el tamaño de la cartera y no la calidad del bot."""
    rng = np.random.default_rng(11)
    base = 1000.0 * np.exp(np.cumsum(rng.normal(0.0001, 0.01, 500)))
    a = compute_metrics(base, [], "1h")
    b = compute_metrics(base * 7.5, [], "1h")
    assert a.sortino == pytest.approx(b.sortino)
    assert a.sharpe == pytest.approx(b.sharpe)
    assert a.max_drawdown == pytest.approx(b.max_drawdown)
    assert a.total_return == pytest.approx(b.total_return)


# --------------------------------------------------------------------------- #
# Correlaciones                                                                #
# --------------------------------------------------------------------------- #


def test_correlacion_de_una_serie_consigo_misma() -> None:
    a = np.array([0.01, -0.02, 0.03, 0.0])
    assert rolling_correlation(a, a) == pytest.approx(1.0)


def test_correlacion_inversa() -> None:
    a = np.array([0.01, -0.02, 0.03, 0.0])
    assert rolling_correlation(a, -a) == pytest.approx(-1.0)


def test_correlacion_con_una_serie_plana_es_cero() -> None:
    a = np.array([0.01, -0.02, 0.03])
    assert rolling_correlation(a, np.zeros(3)) == 0.0


def test_correlacion_robusta_a_nan() -> None:
    a = np.array([0.01, np.nan, 0.03, 0.02])
    b = np.array([0.02, 0.05, 0.06, 0.04])
    assert np.isfinite(rolling_correlation(a, b))


def test_correlacion_de_series_de_distinta_longitud() -> None:
    assert rolling_correlation(np.array([0.1, 0.2]), np.array([0.1, 0.2, 0.3])) == 0.0


def test_correlacion_con_el_benchmark() -> None:
    equity = np.array([100.0, 110.0, 99.0, 120.0])
    m = compute_metrics(equity, [], "1h", benchmark=equity * 3.0)
    assert m.corr_to_benchmark == pytest.approx(1.0)


def test_correlaciones_de_poblacion() -> None:
    base = np.array([100.0, 110.0, 105.0, 120.0, 118.0])
    poblacion = {
        "igual_a": base,
        "igual_b": base * 2.0,
        "contrario": np.array([100.0, 90.0, 95.0, 80.0, 82.0]),
    }
    corr = population_correlations(poblacion)
    assert set(corr) == set(poblacion)
    # la escala no importa: dos curvas proporcionales son el mismo bot
    assert corr["igual_a"] == pytest.approx(corr["igual_b"])
    assert corr["contrario"] < corr["igual_a"]
    assert corr["contrario"] < 0.0


def test_correlaciones_de_un_solo_bot() -> None:
    corr = population_correlations({"solo": np.array([100.0, 110.0, 105.0])})
    assert corr == {"solo": 0.0}


def test_correlaciones_de_una_poblacion_vacia() -> None:
    assert population_correlations({}) == {}
