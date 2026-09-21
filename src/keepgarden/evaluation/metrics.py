"""Métricas de rendimiento y riesgo.

Todo lo de docs/METRICS.md §1. Este módulo es matemática financiera pura: sin
estado, sin base de datos, sin configuración más allá del timeframe.

Dos criterios gobiernan las decisiones de aquí:

* **Ninguna métrica puede ser infinita ni NaN.** Un bot con dos operaciones
  afortunadas y cero drawdown tendría un Calmar infinito y dominaría el ranking
  para siempre. Los cocientes cuyo denominador puede anularse se recortan a
  ``MAX_RATIO``.
* **Las métricas ajustadas por riesgo no dependen de la escala.** Doblar el
  capital no puede cambiar el Sortino: si lo cambiara, el fitness premiaría el
  tamaño de la cartera en vez de la calidad del bot.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any, TypeAlias

import numpy as np

from ..types import PERIODS_PER_YEAR, Timeframe

#: Una operación cerrada, tal y como la produce el backtest. Es JSON
#: heterogéneo: tiparlo más fino sólo añadiría ceremonia.
Trade: TypeAlias = Mapping[str, Any]

#: Tope de los cocientes cuyo denominador puede ser cero (Calmar, Martin,
#: Sortino sin pérdidas, profit factor sin operaciones perdedoras, fee drag sin
#: beneficio bruto). Es alto para que siga distinguiéndose de un bot normal,
#: pero finito para que no rompa ninguna ordenación.
MAX_RATIO = 10.0

#: Tope del retorno anualizado, en logaritmos. Anualizar una ventana de pocas
#: velas produce potencias que desbordan un float; acotarlo mantiene el orden
#: entre bots sin llegar nunca a infinito.
MAX_LOG_CAGR = 20.0

#: Por debajo de esto un denominador se considera cero.
EPS = 1e-12


@dataclass(slots=True)
class Metrics:
    """Todas las métricas de un bot sobre una ventana."""

    # retorno
    total_return: float = 0.0
    cagr: float = 0.0
    avg_trade_return: float = 0.0
    expectancy: float = 0.0
    # riesgo
    max_drawdown: float = 0.0
    ulcer_index: float = 0.0
    downside_dev: float = 0.0
    time_in_market: float = 0.0
    worst_trade: float = 0.0
    # ajustadas
    sharpe: float = 0.0
    sortino: float = 0.0
    calmar: float = 0.0
    martin: float = 0.0
    profit_factor: float = 0.0
    # comportamiento
    n_trades: int = 0
    win_rate: float = 0.0
    turnover: float = 0.0
    avg_holding_bars: float = 0.0
    fee_drag: float = 0.0
    consistency: float = 0.0
    # relacionales (las rellena el evaluador de población)
    corr_to_population: float = 0.0
    corr_to_benchmark: float = 0.0
    novelty: float = 0.0

    def to_dict(self) -> dict[str, float]:
        return asdict(self)

    @property
    def is_evaluable(self) -> bool:
        """Con muy pocas operaciones no hay evidencia: el fitness queda
        indefinido, que no es lo mismo que bajo."""
        return self.n_trades > 0


# --------------------------------------------------------------------------- #
# Piezas sueltas                                                               #
# --------------------------------------------------------------------------- #


def _ratio(numerador: float, denominador: float) -> float:
    """Cociente acotado: el denominador a cero no devuelve infinito."""
    if abs(denominador) <= EPS:
        if abs(numerador) <= EPS:
            return 0.0
        return MAX_RATIO if numerador > 0 else -MAX_RATIO
    return float(np.clip(numerador / denominador, -MAX_RATIO, MAX_RATIO))


def simple_returns(equity: np.ndarray) -> np.ndarray:
    """Retornos aritméticos vela a vela, que es lo que se reporta."""
    if equity.size < 2:
        return np.zeros(0, dtype="float64")
    previo = equity[:-1]
    out = np.zeros(equity.size - 1, dtype="float64")
    ok = np.abs(previo) > EPS
    out[ok] = equity[1:][ok] / previo[ok] - 1.0
    return out


def drawdown_series(equity: np.ndarray) -> np.ndarray:
    """Serie de drawdown desde máximos, en fracción y positiva."""
    equity = np.asarray(equity, dtype="float64")
    if equity.size == 0:
        return equity
    picos = np.maximum.accumulate(equity)
    out = np.zeros_like(equity)
    ok = np.abs(picos) > EPS
    out[ok] = 1.0 - equity[ok] / picos[ok]
    return np.maximum(out, 0.0)


def bars_per_day(timeframe: Timeframe) -> int:
    """Velas que caben en un día en este timeframe."""
    return max(1, round(PERIODS_PER_YEAR[str(timeframe)] / 365.0))


def _consistency(equity: np.ndarray, timeframe: Timeframe) -> float:
    """Fracción de días con retorno positivo.

    Un bot que gana lo mismo repartido en muchos días pequeños es más creíble
    que uno que lo gana entero en una tarde, y esta es la métrica que lo nota.
    """
    if equity.size < 2:
        return 0.0
    paso = bars_per_day(timeframe)
    cierres = equity[paso - 1 :: paso]
    if cierres.size == 0 or cierres[-1] != equity[-1]:
        cierres = np.append(cierres, equity[-1])
    bases = np.concatenate([[equity[0]], cierres[:-1]])
    ok = np.abs(bases) > EPS
    if not ok.any():
        return 0.0
    retornos = cierres[ok] / bases[ok] - 1.0
    return float((retornos > 0).mean())


def rolling_correlation(a: np.ndarray, b: np.ndarray) -> float:
    """Correlación de Pearson entre dos series de retornos, robusta a NaN.

    Devuelve 0.0 —no NaN— cuando no se puede calcular: series de distinta
    longitud, demasiado cortas o sin varianza. Una correlación desconocida se
    trata como "no aporta información", nunca como un número que contamine el
    fitness.
    """
    a = np.asarray(a, dtype="float64")
    b = np.asarray(b, dtype="float64")
    if a.size != b.size or a.size < 2:
        return 0.0
    ok = np.isfinite(a) & np.isfinite(b)
    if ok.sum() < 2:
        return 0.0
    a, b = a[ok], b[ok]
    if a.std() <= EPS or b.std() <= EPS:
        return 0.0
    return float(np.clip(np.corrcoef(a, b)[0, 1], -1.0, 1.0))


def population_correlations(equities: dict[str, np.ndarray]) -> dict[str, float]:
    """Correlación media de cada bot con el resto de la población.

    Es la métrica que hace posible la fusión: se buscan padres con correlación
    baja entre sí, porque dos estrategias mediocres descorrelacionadas producen
    al combinarse una curva con menos drawdown que cualquiera de las dos. Es la
    única comida gratis que hay en esto.
    """
    retornos = {bot: simple_returns(np.asarray(e, dtype="float64")) for bot, e in equities.items()}
    out: dict[str, float] = {}
    for bot, propio in retornos.items():
        otros = [
            rolling_correlation(propio, ajeno) for nombre, ajeno in retornos.items() if nombre != bot
        ]
        out[bot] = float(np.mean(otros)) if otros else 0.0
    return out


# --------------------------------------------------------------------------- #
# El cálculo completo                                                          #
# --------------------------------------------------------------------------- #


def _trade_metrics(trades: Sequence[Trade], m: Metrics, n_bars: int) -> float:
    """Rellena las métricas que salen de las operaciones. Devuelve el bruto."""
    m.n_trades = len(trades)
    if not trades:
        return 0.0

    retornos = np.array([float(t.get("return", 0.0)) for t in trades], dtype="float64")
    pnls = np.array([float(t.get("pnl", 0.0)) for t in trades], dtype="float64")
    brutos = np.array([float(t.get("gross_pnl", 0.0)) for t in trades], dtype="float64")
    barras = np.array([float(t.get("bars_held", 0)) for t in trades], dtype="float64")

    ganadoras = retornos[pnls > 0]
    perdedoras = retornos[pnls < 0]

    m.avg_trade_return = float(retornos.mean())
    m.worst_trade = float(retornos.min())
    m.win_rate = float(len(ganadoras) / len(trades))
    m.avg_holding_bars = float(barras.mean())
    m.time_in_market = min(1.0, float(barras.sum() / n_bars)) if n_bars else 0.0

    media_gana = float(ganadoras.mean()) if ganadoras.size else 0.0
    media_pierde = abs(float(perdedoras.mean())) if perdedoras.size else 0.0
    m.expectancy = m.win_rate * media_gana - (1.0 - m.win_rate) * media_pierde

    suma_gana = float(pnls[pnls > 0].sum())
    suma_pierde = abs(float(pnls[pnls < 0].sum()))
    m.profit_factor = _ratio(suma_gana, suma_pierde)

    return float(brutos.sum())


def compute_metrics(
    equity: np.ndarray,
    trades: Sequence[Trade],
    timeframe: Timeframe,
    *,
    total_fees: float = 0.0,
    benchmark: np.ndarray | None = None,
) -> Metrics:
    """Calcula todas las métricas de una ventana.

    Detalles que importan:

    * ``sortino`` usa ``downside_dev`` con umbral 0, no la desviación total.
      Es la métrica principal del sistema.
    * ``ulcer_index = sqrt(mean(drawdown_t^2))`` — castiga los drawdowns
      *largos*, no sólo los profundos, que es lo que de verdad hace abandonar
      una estrategia.
    * ``consistency`` = fracción de subventanas diarias con retorno positivo.
    * ``fee_drag = fees / beneficio_bruto``. Por encima de 0.5 el bot vive para
      pagar comisiones; sin beneficio bruto se recorta a ``MAX_RATIO``.
    * Ningún cociente puede salir infinito: ver ``MAX_RATIO``.

    Anualización con ``PERIODS_PER_YEAR[timeframe]`` (8760 para 1h).
    """
    m = Metrics()
    equity = np.asarray(equity, dtype="float64")
    n_bars = int(equity.size)
    if n_bars == 0:
        return m

    ppy = PERIODS_PER_YEAR[str(timeframe)]
    notional_total = 0.0
    bruto = _trade_metrics(trades, m, n_bars)
    if trades:
        notional_total = float(sum(float(t.get("notional", 0.0)) for t in trades))

    capital = float(equity[0])
    if abs(capital) > EPS:
        m.total_return = float(equity[-1] / capital - 1.0)
        m.turnover = float(notional_total / capital)

    dd = drawdown_series(equity)
    m.max_drawdown = float(dd.max())
    m.ulcer_index = float(np.sqrt(np.mean(dd**2)))

    retornos = simple_returns(equity)
    if retornos.size:
        m.downside_dev = float(np.sqrt(np.mean(np.minimum(retornos, 0.0) ** 2)))
        anualizador = float(np.sqrt(ppy))
        m.sharpe = _ratio(float(retornos.mean()) * anualizador, float(retornos.std(ddof=0)))
        m.sortino = _ratio(float(retornos.mean()) * anualizador, m.downside_dev)
        periodos = float(retornos.size)
        if abs(capital) > EPS and equity[-1] > 0:
            # En logaritmos y acotado: anualizar una ventana de cuatro velas da
            # por construcción un número astronómico, y un inf aquí envenena
            # después el Calmar, el Martin y cualquier promedio de población.
            log_anual = float(np.log(equity[-1] / capital)) * ppy / periodos
            m.cagr = float(np.expm1(min(max(log_anual, -50.0), MAX_LOG_CAGR)))
        else:
            m.cagr = -1.0

    m.calmar = _ratio(m.cagr, m.max_drawdown)
    m.martin = _ratio(m.cagr, m.ulcer_index)

    comisiones = total_fees if total_fees > 0 else float(
        sum(float(t.get("fees", 0.0)) for t in trades)
    )
    m.fee_drag = _ratio(comisiones, bruto) if bruto > EPS else (MAX_RATIO if comisiones > 0 else 0.0)

    m.consistency = _consistency(equity, timeframe)

    if benchmark is not None:
        m.corr_to_benchmark = rolling_correlation(retornos, simple_returns(np.asarray(benchmark)))

    return m


__all__ = (
    "EPS",
    "MAX_LOG_CAGR",
    "MAX_RATIO",
    "Metrics",
    "Trade",
    "bars_per_day",
    "compute_metrics",
    "drawdown_series",
    "population_correlations",
    "rolling_correlation",
    "simple_returns",
)
