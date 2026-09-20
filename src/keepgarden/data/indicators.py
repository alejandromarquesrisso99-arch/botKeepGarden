"""Librería de indicadores y caché compartida.

Tres reglas, y las tres importan más que la elegancia del código:

1. **Vectorizados.** La incubadora los llama millones de veces.
2. **Causales.** ``indicador[t]`` sólo puede depender de ``datos[0..t]``.
   Prohibido ``center=True`` y cualquier ``shift`` negativo. Hay un test
   automático que lo comprueba para todos los indicadores del catálogo.
3. **Cacheados.** Si 40 bots piden ``EMA(21)``, se calcula una vez.

Cómo se le pasa la fuente a un indicador: ``compute`` añade a las velas una
columna ``src`` con el campo pedido (``close``, ``hlc3``…) y la función lee de
ahí. Los indicadores que necesitan OHLC completo (ATR, ADX, estocástico) leen
además las columnas originales. Así la firma del registro sigue siendo
``(velas, params) -> array`` y no hay que arrastrar el ``source`` por todas
partes.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import cast

import numpy as np
import pandas as pd

from ..genome.catalog import GeneCatalog, spec
from ..types import PERIODS_PER_YEAR, TIMEFRAME_MS, PriceField, Timeframe

#: Firma de un indicador: (velas, params) -> array alineado al índice.
IndicatorFn = Callable[[pd.DataFrame, Mapping[str, float]], np.ndarray]

#: Registro kind -> función. Lo puebla el decorador ``register``.
REGISTRY: dict[str, IndicatorFn] = {}

#: Columna sintética con el campo de vela que pidió el feature.
SOURCE_COLUMN = "src"


def register(kind: str) -> Callable[[IndicatorFn], IndicatorFn]:
    """Decorador para dar de alta un indicador en el registro.

    Todo ``kind`` del catálogo (``genome.catalog.INDICATORS``) debe tener su
    función aquí. Hay un test que comprueba que no falta ninguno: un indicador
    en el catálogo sin implementación produce genomas que no compilan.
    """

    def deco(fn: IndicatorFn) -> IndicatorFn:
        REGISTRY[kind] = fn
        return fn

    return deco


# --------------------------------------------------------------------------- #
# Fuente y entrada                                                             #
# --------------------------------------------------------------------------- #


def source_series(candles: pd.DataFrame, source: PriceField | str) -> np.ndarray:
    """El campo de vela pedido, incluidos los sintéticos ``hlc3`` y ``ohlc4``."""
    field_name = str(source)
    if field_name == PriceField.HLC3:
        return (
            candles["high"].to_numpy() + candles["low"].to_numpy() + candles["close"].to_numpy()
        ) / 3.0
    if field_name == PriceField.OHLC4:
        return (
            candles["open"].to_numpy()
            + candles["high"].to_numpy()
            + candles["low"].to_numpy()
            + candles["close"].to_numpy()
        ) / 4.0
    try:
        return candles[field_name].to_numpy(dtype="float64")
    except KeyError:
        raise KeyError(
            f"campo de vela desconocido: {field_name!r}. "
            f"Válidos: {[str(f) for f in PriceField]}"
        ) from None


def prepare_frame(
    candles: pd.DataFrame,
    source: PriceField | str = PriceField.CLOSE,
    timeframe: Timeframe | None = None,
) -> pd.DataFrame:
    """Las velas con la columna ``src`` resuelta y el timeframe anotado."""
    frame = candles.assign(**{SOURCE_COLUMN: source_series(candles, source)})
    frame.attrs = {**candles.attrs, "timeframe": timeframe or candles.attrs.get("timeframe", "1h")}
    return frame


def compute(
    kind: str,
    candles: pd.DataFrame,
    params: Mapping[str, float] | None = None,
    source: PriceField | str = PriceField.CLOSE,
    timeframe: Timeframe | None = None,
) -> np.ndarray:
    """Calcula un indicador del catálogo sobre una serie de velas.

    Es la puerta de entrada de un solo cálculo: la caché la usa por debajo. No
    recorta los parámetros; eso lo hace la caché, que es quien conoce el
    genoma del que vienen.
    """
    try:
        fn = REGISTRY[kind]
    except KeyError:
        raise KeyError(
            f"indicador sin implementar: {kind!r}. Implementados: {sorted(REGISTRY)}"
        ) from None
    return np.asarray(fn(prepare_frame(candles, source, timeframe), params or {}), dtype="float64")


def warmup_bars(kind: str, params: Mapping[str, float] | None = None) -> int:
    """Velas de calentamiento declaradas en el catálogo para esos parámetros."""
    return spec(kind).warmup_bars(params or {})


# --------------------------------------------------------------------------- #
# Herramientas comunes                                                         #
# --------------------------------------------------------------------------- #


def _src(df: pd.DataFrame) -> np.ndarray:
    return df[SOURCE_COLUMN].to_numpy(dtype="float64")


def _n(params: Mapping[str, float], name: str = "period", default: float = 14) -> int:
    return max(1, round(float(params.get(name, default))))


def _f(params: Mapping[str, float], name: str, default: float) -> float:
    return float(params.get(name, default))


def _line(df_kind: str, params: Mapping[str, float]) -> str:
    return spec(df_kind).line_name(params)


def _roll(x: np.ndarray, n: int) -> pd.core.window.rolling.Rolling:
    return pd.Series(x).rolling(n, min_periods=n)


def _sma(x: np.ndarray, n: int) -> np.ndarray:
    return _roll(x, n).mean().to_numpy()


def _shift(x: np.ndarray, k: int) -> np.ndarray:
    """Desplaza hacia adelante en el tiempo: ``out[t] = x[t - k]``.

    Sólo hacia adelante. Un desplazamiento negativo traería el futuro y por eso
    esta función no lo admite.
    """
    if k < 0:
        raise ValueError("los indicadores no pueden mirar hacia adelante")
    out = np.full_like(x, np.nan)
    if k == 0:
        return x.copy()
    if k < len(x):
        out[k:] = x[:-k]
    return out


def _recursive_mean(x: np.ndarray, n: int, alpha: float) -> np.ndarray:
    """Media exponencial sembrada con la media simple de las ``n`` primeras.

    Es la forma clásica (y la de Wilder, con ``alpha = 1/n``). Se siembra con la
    SMA en vez de con el primer valor para que el arranque no dependa de una
    sola vela, y se calcula con ``ewm`` para no bajar a un bucle de Python.
    """
    out = np.full(len(x), np.nan, dtype="float64")
    valid = np.flatnonzero(~np.isnan(x))
    if valid.size == 0:
        return out
    start = int(valid[0])
    if len(x) - start < n:
        return out
    first = start + n - 1
    tail = x[first:].copy()
    tail[0] = float(np.mean(x[start : start + n]))
    out[first:] = pd.Series(tail).ewm(alpha=alpha, adjust=False).mean().to_numpy()
    return out


def _ema_array(x: np.ndarray, n: int) -> np.ndarray:
    return _recursive_mean(x, n, 2.0 / (n + 1.0))


def _wilder(x: np.ndarray, n: int) -> np.ndarray:
    return _recursive_mean(x, n, 1.0 / n)


def _true_range(df: pd.DataFrame) -> np.ndarray:
    high = df["high"].to_numpy(dtype="float64")
    low = df["low"].to_numpy(dtype="float64")
    prev_close = _shift(df["close"].to_numpy(dtype="float64"), 1)
    tr = high - low
    with np.errstate(invalid="ignore"):
        tr = np.maximum(tr, np.abs(high - prev_close))
        tr = np.maximum(tr, np.abs(low - prev_close))
    tr[0] = high[0] - low[0]
    return tr


def _atr_array(df: pd.DataFrame, n: int) -> np.ndarray:
    return _wilder(_true_range(df), n)


def _safe_divide(a: np.ndarray, b: np.ndarray, fill: float = np.nan) -> np.ndarray:
    """División que no explota ni contagia infinitos donde el divisor es cero."""
    out = np.full(len(a), fill, dtype="float64")
    ok = np.isfinite(a) & np.isfinite(b) & (b != 0.0)
    out[ok] = a[ok] / b[ok]
    return out


def _rolling_mad(x: np.ndarray, n: int, *, chunk: int = 20_000) -> np.ndarray:
    """Desviación absoluta media en ventana móvil.

    No tiene forma cerrada acumulable, así que se calcula sobre una vista
    deslizante por trozos: hacerlo de golpe sobre siete años de velas pediría
    cientos de megabytes, y hacerlo con ``rolling.apply`` baja a un bucle de
    Python por ventana.
    """
    out = np.full(len(x), np.nan, dtype="float64")
    if len(x) < n:
        return out
    windows = np.lib.stride_tricks.sliding_window_view(x, n)
    for start in range(0, len(windows), chunk):
        block = windows[start : start + chunk]
        out[start + n - 1 : start + n - 1 + len(block)] = np.abs(
            block - block.mean(axis=1, keepdims=True)
        ).mean(axis=1)
    return out


def _highest(x: np.ndarray, n: int, *, exclude_current: bool = True) -> np.ndarray:
    values = _roll(x, n).max().to_numpy()
    return _shift(values, 1) if exclude_current else values


def _lowest(x: np.ndarray, n: int, *, exclude_current: bool = True) -> np.ndarray:
    values = _roll(x, n).min().to_numpy()
    return _shift(values, 1) if exclude_current else values


# --------------------------------------------------------------------------- #
# Tendencia                                                                    #
# --------------------------------------------------------------------------- #


@register("SMA")
def _sma_ind(df: pd.DataFrame, params: Mapping[str, float]) -> np.ndarray:
    return _sma(_src(df), _n(params))


@register("EMA")
def _ema_ind(df: pd.DataFrame, params: Mapping[str, float]) -> np.ndarray:
    return _ema_array(_src(df), _n(params))


@register("WMA")
def _wma_ind(df: pd.DataFrame, params: Mapping[str, float]) -> np.ndarray:
    x = _src(df)
    n = _n(params)
    out = np.full(len(x), np.nan, dtype="float64")
    if len(x) < n:
        return out
    weights = np.arange(1.0, n + 1.0)
    weights /= weights.sum()
    out[n - 1 :] = np.convolve(x, weights[::-1], mode="valid")
    return out


@register("MACD")
def _macd_ind(df: pd.DataFrame, params: Mapping[str, float]) -> np.ndarray:
    x = _src(df)
    fast, slow = _n(params, "fast", 12), _n(params, "slow", 26)
    macd = _ema_array(x, fast) - _ema_array(x, slow)
    line = _line("MACD", params)
    if line == "macd":
        return macd
    signal = _recursive_mean(macd, _n(params, "signal", 9), 2.0 / (_n(params, "signal", 9) + 1.0))
    return signal if line == "signal" else macd - signal


@register("ADX")
def _adx_ind(df: pd.DataFrame, params: Mapping[str, float]) -> np.ndarray:
    n = _n(params)
    high = df["high"].to_numpy(dtype="float64")
    low = df["low"].to_numpy(dtype="float64")
    up = high - _shift(high, 1)
    down = _shift(low, 1) - low
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    plus_dm[0] = minus_dm[0] = np.nan

    atr = _wilder(_true_range(df), n)
    plus_di = 100.0 * _safe_divide(_wilder(plus_dm, n), atr)
    minus_di = 100.0 * _safe_divide(_wilder(minus_dm, n), atr)
    dx = 100.0 * _safe_divide(np.abs(plus_di - minus_di), plus_di + minus_di, fill=0.0)
    dx[~np.isfinite(plus_di) | ~np.isfinite(minus_di)] = np.nan
    return _wilder(dx, n)


@register("SUPERTREND")
def _supertrend_ind(df: pd.DataFrame, params: Mapping[str, float]) -> np.ndarray:
    n = _n(params)
    mult = _f(params, "multiplier", 3.0)
    close = df["close"].to_numpy(dtype="float64")
    mid = (df["high"].to_numpy(dtype="float64") + df["low"].to_numpy(dtype="float64")) / 2.0
    atr = _atr_array(df, n)
    upper = mid + mult * atr
    lower = mid - mult * atr

    out = np.full(len(close), np.nan, dtype="float64")
    start = int(np.argmax(np.isfinite(atr))) if np.isfinite(atr).any() else len(close)
    if start >= len(close):
        return out

    # Las bandas se arrastran: sólo se estrechan mientras la tendencia aguanta.
    # Es un recurrente de verdad y no hay forma vectorial honesta de hacerlo.
    final_upper, final_lower = upper[start], lower[start]
    up_trend = True
    out[start] = final_lower
    for i in range(start + 1, len(close)):
        final_upper = (
            upper[i] if (upper[i] < final_upper or close[i - 1] > final_upper) else final_upper
        )
        final_lower = (
            lower[i] if (lower[i] > final_lower or close[i - 1] < final_lower) else final_lower
        )
        if up_trend and close[i] < final_lower:
            up_trend = False
        elif not up_trend and close[i] > final_upper:
            up_trend = True
        out[i] = final_lower if up_trend else final_upper
    return out


@register("SLOPE")
def _slope_ind(df: pd.DataFrame, params: Mapping[str, float]) -> np.ndarray:
    x = _src(df)
    n = _n(params)
    if n < 2 or len(x) < n:
        return np.full(len(x), np.nan, dtype="float64")
    t = np.arange(n, dtype="float64")
    sum_t = t.sum()
    denom = n * (t**2).sum() - sum_t**2
    sum_y = _roll(x, n).sum().to_numpy()
    sum_ty = np.full(len(x), np.nan, dtype="float64")
    sum_ty[n - 1 :] = np.convolve(x, t[::-1], mode="valid")
    slope = (n * sum_ty - sum_t * sum_y) / denom
    return _safe_divide(slope, x)          # normalizada por precio: comparable entre épocas


# --------------------------------------------------------------------------- #
# Osciladores                                                                  #
# --------------------------------------------------------------------------- #


@register("RSI")
def _rsi_ind(df: pd.DataFrame, params: Mapping[str, float]) -> np.ndarray:
    x = _src(df)
    n = _n(params)
    delta = x - _shift(x, 1)
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    gain[0] = loss[0] = np.nan
    avg_gain = _wilder(gain, n)
    avg_loss = _wilder(loss, n)
    rs = _safe_divide(avg_gain, avg_loss)
    out = 100.0 - 100.0 / (1.0 + rs)
    # Sin pérdidas en la ventana el RS es infinito: el RSI vale 100, no NaN.
    sin_perdidas = np.isfinite(avg_gain) & (avg_loss == 0.0)
    out[sin_perdidas] = 100.0
    sin_ganancias = np.isfinite(avg_loss) & (avg_gain == 0.0)
    out[sin_ganancias] = 0.0
    return out


@register("STOCH")
def _stoch_ind(df: pd.DataFrame, params: Mapping[str, float]) -> np.ndarray:
    n = _n(params)
    smooth = _n(params, "smooth", 3)
    close = df["close"].to_numpy(dtype="float64")
    hh = _highest(df["high"].to_numpy(dtype="float64"), n, exclude_current=False)
    ll = _lowest(df["low"].to_numpy(dtype="float64"), n, exclude_current=False)
    raw = 100.0 * _safe_divide(close - ll, hh - ll, fill=50.0)
    raw[~np.isfinite(hh)] = np.nan
    return raw if smooth <= 1 else _sma(raw, smooth)


@register("CCI")
def _cci_ind(df: pd.DataFrame, params: Mapping[str, float]) -> np.ndarray:
    x = _src(df)
    n = _n(params)
    mad = _rolling_mad(x, n)
    return _safe_divide(x - _sma(x, n), 0.015 * mad, fill=0.0)


@register("WILLR")
def _willr_ind(df: pd.DataFrame, params: Mapping[str, float]) -> np.ndarray:
    n = _n(params)
    close = _src(df)
    hh = _highest(df["high"].to_numpy(dtype="float64"), n, exclude_current=False)
    ll = _lowest(df["low"].to_numpy(dtype="float64"), n, exclude_current=False)
    out = -100.0 * _safe_divide(hh - close, hh - ll, fill=-50.0)
    out[~np.isfinite(hh)] = np.nan
    return out


@register("ROC")
def _roc_ind(df: pd.DataFrame, params: Mapping[str, float]) -> np.ndarray:
    x = _src(df)
    previous = _shift(x, _n(params))
    return _safe_divide(x - previous, previous)


@register("ZSCORE")
def _zscore_ind(df: pd.DataFrame, params: Mapping[str, float]) -> np.ndarray:
    x = _src(df)
    n = _n(params)
    std = _roll(x, n).std(ddof=0).to_numpy()
    out = _safe_divide(x - _sma(x, n), std, fill=0.0)
    out[~np.isfinite(std)] = np.nan
    return out


# --------------------------------------------------------------------------- #
# Volatilidad                                                                  #
# --------------------------------------------------------------------------- #


@register("ATR")
def _atr_ind(df: pd.DataFrame, params: Mapping[str, float]) -> np.ndarray:
    return _atr_array(df, _n(params))


@register("ATR_PCT")
def _atr_pct_ind(df: pd.DataFrame, params: Mapping[str, float]) -> np.ndarray:
    return _safe_divide(_atr_array(df, _n(params)), df["close"].to_numpy(dtype="float64"))


def _bollinger(df: pd.DataFrame, params: Mapping[str, float]) -> tuple[np.ndarray, np.ndarray]:
    x = _src(df)
    n = _n(params)
    middle = _sma(x, n)
    spread = _f(params, "stdev", 2.0) * _roll(x, n).std(ddof=0).to_numpy()
    return middle, spread


@register("BBANDS")
def _bbands_ind(df: pd.DataFrame, params: Mapping[str, float]) -> np.ndarray:
    middle, spread = _bollinger(df, params)
    line = _line("BBANDS", params)
    if line == "middle":
        return middle
    return middle + spread if line == "upper" else middle - spread


@register("BB_WIDTH")
def _bb_width_ind(df: pd.DataFrame, params: Mapping[str, float]) -> np.ndarray:
    middle, spread = _bollinger(df, params)
    return _safe_divide(2.0 * spread, middle)


@register("KELTNER")
def _keltner_ind(df: pd.DataFrame, params: Mapping[str, float]) -> np.ndarray:
    n = _n(params)
    middle = _ema_array(_src(df), n)
    spread = _f(params, "multiplier", 2.0) * _atr_array(df, n)
    line = _line("KELTNER", params)
    if line == "middle":
        return middle
    return middle + spread if line == "upper" else middle - spread


@register("REALIZED_VOL")
def _realized_vol_ind(df: pd.DataFrame, params: Mapping[str, float]) -> np.ndarray:
    x = _src(df)
    n = _n(params)
    previous = _shift(x, 1)
    ratio = _safe_divide(x, previous)
    with np.errstate(invalid="ignore", divide="ignore"):
        log_returns = np.where(ratio > 0, np.log(ratio), np.nan)
    periods = PERIODS_PER_YEAR.get(str(df.attrs.get("timeframe", "1h")), PERIODS_PER_YEAR["1h"])
    return _roll(log_returns, n).std(ddof=0).to_numpy() * np.sqrt(periods)


# --------------------------------------------------------------------------- #
# Rango                                                                        #
#
# Los cuatro EXCLUYEN la vela actual. Incluirla haría imposible cualquier
# ruptura (``close > max(..., high[t])`` nunca puede ser cierto) y dejaría a la
# familia BREAKOUT entera evolucionando sobre reglas muertas. Ver D-013.
# --------------------------------------------------------------------------- #


@register("DONCHIAN_HIGH")
def _donchian_high_ind(df: pd.DataFrame, params: Mapping[str, float]) -> np.ndarray:
    return _highest(df["high"].to_numpy(dtype="float64"), _n(params))


@register("DONCHIAN_LOW")
def _donchian_low_ind(df: pd.DataFrame, params: Mapping[str, float]) -> np.ndarray:
    return _lowest(df["low"].to_numpy(dtype="float64"), _n(params))


@register("HIGHEST")
def _highest_ind(df: pd.DataFrame, params: Mapping[str, float]) -> np.ndarray:
    return _highest(_src(df), _n(params))


@register("LOWEST")
def _lowest_ind(df: pd.DataFrame, params: Mapping[str, float]) -> np.ndarray:
    return _lowest(_src(df), _n(params))


# --------------------------------------------------------------------------- #
# Volumen                                                                      #
# --------------------------------------------------------------------------- #


@register("OBV")
def _obv_ind(df: pd.DataFrame, params: Mapping[str, float]) -> np.ndarray:
    close = _src(df)
    volume = df["volume"].to_numpy(dtype="float64")
    direction = np.sign(close - _shift(close, 1))
    direction[0] = 0.0
    return np.nancumsum(direction * np.nan_to_num(volume))


@register("VWAP")
def _vwap_ind(df: pd.DataFrame, params: Mapping[str, float]) -> np.ndarray:
    n = _n(params)
    price = _src(df)
    volume = np.nan_to_num(df["volume"].to_numpy(dtype="float64"))
    traded = _roll(price * volume, n).sum().to_numpy()
    total = _roll(volume, n).sum().to_numpy()
    out = _safe_divide(traded, total)
    # Una ventana entera sin volumen (relleno de un hueco) no tiene VWAP: se
    # arrastra el precio medio simple antes que inventar un número.
    vacio = np.isfinite(total) & (total == 0.0)
    out[vacio] = _sma(price, n)[vacio]
    return out


@register("VOL_RATIO")
def _vol_ratio_ind(df: pd.DataFrame, params: Mapping[str, float]) -> np.ndarray:
    volume = _src(df)
    return _safe_divide(volume, _sma(volume, _n(params)), fill=0.0)


@register("MFI")
def _mfi_ind(df: pd.DataFrame, params: Mapping[str, float]) -> np.ndarray:
    n = _n(params)
    typical = _src(df)
    flow = typical * np.nan_to_num(df["volume"].to_numpy(dtype="float64"))
    delta = typical - _shift(typical, 1)
    positive = np.where(delta > 0, flow, 0.0)
    negative = np.where(delta < 0, flow, 0.0)
    positive[0] = negative[0] = np.nan
    pos_sum = _roll(positive, n).sum().to_numpy()
    neg_sum = _roll(negative, n).sum().to_numpy()
    ratio = _safe_divide(pos_sum, neg_sum)
    out = 100.0 - 100.0 / (1.0 + ratio)
    out[np.isfinite(pos_sum) & (neg_sum == 0.0)] = 100.0
    out[np.isfinite(neg_sum) & (pos_sum == 0.0)] = 0.0
    return out


# --------------------------------------------------------------------------- #
# Derivados                                                                    #
# --------------------------------------------------------------------------- #


@register("PCT_CHANGE")
def _pct_change_ind(df: pd.DataFrame, params: Mapping[str, float]) -> np.ndarray:
    x = _src(df)
    previous = _shift(x, _n(params, "period", 1))
    return _safe_divide(x - previous, previous)


@register("PCT_RANK")
def _pct_rank_ind(df: pd.DataFrame, params: Mapping[str, float]) -> np.ndarray:
    return pct_rank(_src(df), _n(params, "period", 100))


def pct_rank(x: np.ndarray, window: int) -> np.ndarray:
    """Percentil del valor actual dentro de su ventana, en [0, 1].

    Lo usan el indicador ``PCT_RANK`` y los operadores de regla
    ``PCT_RANK_GT`` / ``PCT_RANK_LT``. Sin escala de precio: es de los pocos
    genes que generalizan entre activos y entre épocas.
    """
    window = max(2, window)
    return pd.Series(x).rolling(window, min_periods=window).rank(pct=True).to_numpy()


# --------------------------------------------------------------------------- #
# Caché                                                                        #
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class IndicatorCache:
    """Caché de indicadores compartida por toda la población.

    Implementa el protocolo ``genome.compile.FeatureStore``.

    La clave es ``(kind, params ordenados, source, timeframe)``. Los features de
    timeframe superior se calculan sobre su propia serie y se alinean hacia
    atrás con la misma regla que ``data.candles.align_context``.
    """

    candles: pd.DataFrame
    context: dict[Timeframe, pd.DataFrame] = field(default_factory=dict)
    catalog: GeneCatalog | None = None
    timeframe: Timeframe = "1h"
    _cache: dict[tuple, np.ndarray] = field(default_factory=dict, repr=False)

    # -- normalización ------------------------------------------------------ #

    def _clean(self, kind: str, params: Mapping[str, float]) -> dict[str, float]:
        """Recorta a rango y tira lo que el catálogo no conoce.

        Sólo toca los parámetros presentes: los que faltan los resuelve el
        propio indicador con su valor por defecto, que es más sensato que la
        mitad del rango.
        """
        s = spec(kind)
        out: dict[str, float] = {}
        for name, value in params.items():
            p = s.param(name)
            if p is not None:
                out[name] = p.clamp(value)
        return out

    def _key(
        self, kind: str, params: Mapping[str, float], source: str, timeframe: Timeframe | None
    ) -> tuple:
        return (
            kind,
            tuple(sorted((k, float(v)) for k, v in params.items())),
            str(source),
            str(timeframe or self.timeframe),
        )

    # -- API ---------------------------------------------------------------- #

    def get(
        self,
        kind: str,
        params: Mapping[str, float],
        source: str = "close",
        timeframe: Timeframe | None = None,
    ) -> np.ndarray:
        """Devuelve la serie del indicador, calculándola si no está en caché."""
        clean = self._clean(kind, params)
        key = self._key(kind, clean, source, timeframe)
        hit = self._cache.get(key)
        if hit is not None:
            return hit

        tf = str(timeframe or self.timeframe)
        if tf == str(self.timeframe):
            values = compute(kind, self.candles, clean, source, self.timeframe)
        else:
            values = self._context_values(kind, clean, source, tf)

        values = np.asarray(values, dtype="float64")
        values.setflags(write=False)      # la comparten cuarenta bots: nadie la pisa
        self._cache[key] = values
        return values

    def _context_values(
        self, kind: str, params: Mapping[str, float], source: str, timeframe: str
    ) -> np.ndarray:
        """Calcula el indicador en su propio timeframe y lo alinea hacia atrás.

        Desplazar el índice del contexto una barra entera equivale a fecharlo en
        el instante en que esa vela **cerró**; el ``ffill`` posterior no puede
        entonces adelantar nada. Es la misma regla de ``align_context`` y la
        fuente número uno de look-ahead del sistema.
        """
        frame = self.context.get(cast("Timeframe", timeframe))
        if frame is None:
            raise KeyError(
                f"no hay velas de contexto de {timeframe} cargadas: un feature las pide. "
                f"Descárgalas con 'keepgarden data backfill --context'"
            )
        values = compute(kind, frame, params, source, cast("Timeframe", timeframe))
        closed_at = np.asarray(frame.index, dtype="int64") + TIMEFRAME_MS[timeframe]
        series = pd.Series(values, index=pd.Index(closed_at, name="ts"))
        return series.reindex(self.candles.index, method="ffill").to_numpy(dtype="float64")

    def warmup_bars(
        self, kind: str, params: Mapping[str, float], timeframe: Timeframe | None = None
    ) -> int:
        """Velas de calentamiento, desde ``IndicatorSpec.warmup_bars``.

        Un feature de contexto calienta en velas suyas, que valen por muchas de
        las operativas: 30 velas diarias son 720 horas.
        """
        bars = spec(kind).warmup_bars(self._clean(kind, params))
        tf = str(timeframe or self.timeframe)
        if tf != str(self.timeframe):
            ratio = TIMEFRAME_MS[tf] / TIMEFRAME_MS[str(self.timeframe)]
            bars = round(bars * ratio)
        return bars

    def clear(self) -> None:
        self._cache.clear()

    @property
    def size(self) -> int:
        return len(self._cache)


__all__ = (
    "REGISTRY",
    "SOURCE_COLUMN",
    "IndicatorCache",
    "IndicatorFn",
    "compute",
    "pct_rank",
    "prepare_frame",
    "register",
    "source_series",
    "warmup_bars",
)
