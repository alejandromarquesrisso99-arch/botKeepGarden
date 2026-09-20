"""Modelo de velas y validación de continuidad.

Columnas canónicas: ``ts`` (ms UTC, apertura), ``open``, ``high``, ``low``,
``close``, ``volume``, ``trades``. El índice es ``ts``, estrictamente
creciente, sin duplicados.

En memoria, ``ts`` vive en el índice (``int64``, nombre ``ts``) y las seis
columnas restantes son ``float64``. ``trades`` puede ser ``NaN``: ``ccxt`` no
expone el número de operaciones de una vela, así que se guarda desconocido en
vez de inventarse un cero que luego nadie sabría distinguir de una vela de
relleno. Al escribir a Parquet, ``ts`` vuelve a ser columna: ver
docs/DATA.md.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd

from ..types import TIMEFRAME_MS, Timeframe, Timestamp

COLUMNS: tuple[str, ...] = ("ts", "open", "high", "low", "close", "volume", "trades")

#: Las columnas que lleva el DataFrame canónico, ya sin ``ts``.
VALUE_COLUMNS: tuple[str, ...] = COLUMNS[1:]

#: Sólo los precios, para las comprobaciones de coherencia OHLC.
PRICE_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close")


@dataclass(frozen=True, slots=True)
class Gap:
    """Hueco en la serie de velas.

    ``start_ts`` y ``end_ts`` son las aperturas de la **primera y la última vela
    que faltan**, no las de las velas sanas que rodean el hueco. Así
    ``n_missing == (end_ts - start_ts) / tf + 1`` y rellenar un hueco es generar
    exactamente ese rango.
    """

    start_ts: Timestamp
    end_ts: Timestamp
    n_missing: int

    @property
    def is_short(self) -> bool:
        """Un hueco corto (1-2 velas) se rellena; uno largo, no."""
        return self.n_missing <= 2


@dataclass(frozen=True, slots=True)
class Anomaly:
    """Una vela sospechosa. Se registra, no se descarta."""

    ts: Timestamp
    kind: str          # duplicate | misaligned | bad_ohlc | negative_volume | zero_volume | price_jump
    detail: str


#: Orden estable de las anomalías cuando varias caen en la misma vela.
_KIND_ORDER: dict[str, int] = {
    "duplicate": 0,
    "misaligned": 1,
    "bad_ohlc": 2,
    "negative_volume": 3,
    "zero_volume": 4,
    "price_jump": 5,
}


# --------------------------------------------------------------------------- #
# Forma canónica                                                               #
# --------------------------------------------------------------------------- #


def empty_frame() -> pd.DataFrame:
    """Un DataFrame de velas vacío pero con la forma correcta."""
    return pd.DataFrame(
        {c: pd.Series(dtype="float64") for c in VALUE_COLUMNS},
        index=pd.Index([], dtype="int64", name="ts"),
    )


def ensure_canonical(df: pd.DataFrame) -> pd.DataFrame:
    """Devuelve la serie en forma canónica sin tocar la original.

    Acepta ``ts`` como columna o como índice, ordena por ``ts`` y fuerza los
    tipos. No deduplica ni rellena: eso son decisiones, y las decisiones se
    toman arriba, a la vista.
    """
    if df is None or len(df.columns) == 0:
        return empty_frame()

    out = df.copy()
    if "ts" in out.columns:
        out = out.set_index("ts")
    out.index = pd.Index(np.asarray(out.index, dtype="int64"), name="ts")

    for col in VALUE_COLUMNS:
        if col not in out.columns:
            out[col] = np.nan
        out[col] = out[col].astype("float64")

    out = out[list(VALUE_COLUMNS)]
    if not out.index.is_monotonic_increasing:
        out = out.sort_index(kind="stable")
    return out


def timeframe_ms(timeframe: Timeframe | str) -> int:
    """Milisegundos de un timeframe, con un error legible si no existe."""
    try:
        return TIMEFRAME_MS[str(timeframe)]
    except KeyError:
        raise ValueError(
            f"timeframe desconocido: {timeframe!r}. Soportados: {sorted(TIMEFRAME_MS)}"
        ) from None


def infer_timeframe_ms(df: pd.DataFrame) -> int:
    """Deduce el timeframe de una serie por la distancia mínima entre velas.

    Con huecos sigue funcionando: un hueco es siempre un múltiplo del
    timeframe, así que el mínimo positivo es el timeframe.
    """
    idx = np.asarray(df.index, dtype="int64")
    if idx.size < 2:
        raise ValueError(
            "no se puede deducir el timeframe de una serie con menos de dos "
            "velas: pásalo explícitamente"
        )
    diffs = np.diff(idx)
    positive = diffs[diffs > 0]
    if positive.size == 0:
        raise ValueError("la serie no tiene dos velas con ts distintos")
    return int(positive.min())


# --------------------------------------------------------------------------- #
# Validación                                                                   #
# --------------------------------------------------------------------------- #


def validate_candles(
    df: pd.DataFrame, timeframe: Timeframe, *, jump_threshold: float = 0.40
) -> tuple[list[Gap], list[Anomaly]]:
    """Comprueba continuidad y sanidad de una serie de velas.

    Detecta:

    * **Huecos**: diferencias entre ``ts`` consecutivos distintas de
      ``TIMEFRAME_MS[timeframe]``.
    * **Duplicados**: mismo ``ts`` dos veces. Siempre es un error del backfill.
    * **Desalineación**: un ``ts`` que no cae en la rejilla del timeframe.
    * **OHLC imposible**: ``high < low``, ``high < max(open, close)``,
      ``low > min(open, close)``.
    * **Volumen negativo** y volumen exactamente cero.
    * **Saltos de precio** mayores que ``jump_threshold`` entre cierres
      consecutivos.

    No modifica el DataFrame. Devolver los problemas, no arreglarlos en
    silencio: descartar una vela rara sin dejar rastro es cómo se pierde la
    pista de un error de datos durante meses.
    """
    tf_ms = timeframe_ms(timeframe)
    data = ensure_canonical(df)
    gaps: list[Gap] = []
    anomalies: list[Anomaly] = []

    if data.empty:
        return gaps, anomalies

    ts = np.asarray(data.index, dtype="int64")

    # -- duplicados y desalineación ---------------------------------------- #
    dup_mask = data.index.duplicated(keep="first")
    for t in ts[dup_mask]:
        anomalies.append(Anomaly(int(t), "duplicate", f"ts repetido: {int(t)}"))

    for t in ts[(ts % tf_ms) != 0]:
        anomalies.append(
            Anomaly(int(t), "misaligned", f"ts fuera de la rejilla de {timeframe}")
        )

    # -- huecos, sobre los ts únicos --------------------------------------- #
    unique_ts = np.unique(ts)
    if unique_ts.size >= 2:
        diffs = np.diff(unique_ts)
        for prev, step in zip(unique_ts[:-1], diffs):
            if step <= tf_ms:
                continue
            n_missing = round(step / tf_ms) - 1
            if n_missing <= 0:
                continue
            start = int(prev) + tf_ms
            gaps.append(
                Gap(start_ts=start, end_ts=start + (n_missing - 1) * tf_ms, n_missing=n_missing)
            )

    # -- coherencia OHLC ---------------------------------------------------- #
    o, h, low, c = (data[k].to_numpy() for k in PRICE_COLUMNS)
    bad = (h < low) | (h < np.maximum(o, c)) | (low > np.minimum(o, c))
    for t in ts[bad]:
        anomalies.append(Anomaly(int(t), "bad_ohlc", "high/low incompatibles con open/close"))

    # -- volumen ------------------------------------------------------------ #
    vol = data["volume"].to_numpy()
    for t in ts[vol < 0]:
        anomalies.append(Anomaly(int(t), "negative_volume", "volumen negativo"))
    for t in ts[vol == 0]:
        anomalies.append(Anomaly(int(t), "zero_volume", "volumen cero"))

    # -- saltos de precio --------------------------------------------------- #
    if c.size >= 2:
        with np.errstate(divide="ignore", invalid="ignore"):
            change = np.abs(np.diff(c) / c[:-1])
        jumped = np.nonzero(np.isfinite(change) & (change > jump_threshold))[0] + 1
        for i in jumped:
            anomalies.append(
                Anomaly(
                    int(ts[i]),
                    "price_jump",
                    f"cierre {c[i - 1]:.8g} -> {c[i]:.8g} ({change[i - 1] * 100:.1f} %)",
                )
            )

    anomalies.sort(key=lambda a: (a.ts, _KIND_ORDER.get(a.kind, 99)))
    return gaps, anomalies


# --------------------------------------------------------------------------- #
# Relleno                                                                      #
# --------------------------------------------------------------------------- #


def fill_short_gaps(
    df: pd.DataFrame, gaps: Sequence[Gap], timeframe: Timeframe
) -> pd.DataFrame:
    """Rellena sólo los huecos cortos por forward fill, con volumen 0.

    Los huecos largos NO se rellenan: inventar precio en una parada larga del
    venue es inventar rentabilidad. Los backtests que los atraviesan deben
    cerrar posiciones al inicio del hueco y no reabrir hasta después.

    La vela sintética es plana (``open == high == low == close ==`` el cierre
    anterior) y lleva ``volume`` y ``trades`` a cero: así se reconoce a simple
    vista y no introduce ni rango ni actividad falsos.
    """
    tf_ms = timeframe_ms(timeframe)
    data = ensure_canonical(df)
    short = [g for g in gaps if g.is_short]
    if data.empty or not short:
        return data

    rows: list[dict[str, float]] = []
    index: list[int] = []
    closes = data["close"]
    for gap in short:
        previous = closes.loc[: gap.start_ts - 1]
        if previous.empty:
            # Un hueco antes de la primera vela no es un hueco: no hay nada que
            # arrastrar hacia adelante.
            continue
        last_close = float(previous.iloc[-1])
        for k in range(gap.n_missing):
            index.append(gap.start_ts + k * tf_ms)
            rows.append(
                {
                    "open": last_close,
                    "high": last_close,
                    "low": last_close,
                    "close": last_close,
                    "volume": 0.0,
                    "trades": 0.0,
                }
            )

    if not rows:
        return data

    synthetic = pd.DataFrame(rows, index=pd.Index(index, dtype="int64", name="ts"))
    return pd.concat([data, synthetic]).sort_index(kind="stable")


# --------------------------------------------------------------------------- #
# Causalidad                                                                   #
# --------------------------------------------------------------------------- #


def slice_causal(
    df: pd.DataFrame, now_ts: Timestamp, *, timeframe: Timeframe | None = None
) -> pd.DataFrame:
    """Devuelve sólo las velas CERRADAS a fecha ``now_ts``.

    Una vela con apertura ``ts`` está cerrada cuando ``ts + tf_ms <= now_ts``.
    Todo acceso a datos en el jardín vivo pasa por aquí. Es la barrera contra el
    look-ahead y tiene un test dedicado.

    El timeframe se deduce del índice; pásalo explícitamente cuando la serie
    pueda tener menos de dos velas.
    """
    data = ensure_canonical(df)
    if data.empty:
        return data
    tf_ms = timeframe_ms(timeframe) if timeframe is not None else infer_timeframe_ms(data)
    closed = np.asarray(data.index, dtype="int64") + tf_ms <= int(now_ts)
    return data.loc[closed]


def align_context(
    base: pd.DataFrame, context: pd.DataFrame, context_tf: Timeframe
) -> pd.DataFrame:
    """Alinea un timeframe superior sobre el operativo, hacia atrás.

    En la vela de 1h de las 13:00, la vela diaria disponible es la de **ayer**,
    no la de hoy en curso. Implementación: desplazar el contexto una barra de su
    propio timeframe y hacer ``reindex(base.index, method="ffill")``.

    Equivocarse aquí produce backtests espectaculares y falsos.
    """
    tf_ms = timeframe_ms(context_tf)
    base_data = ensure_canonical(base)
    ctx = ensure_canonical(context)
    if ctx.empty:
        return pd.DataFrame(
            {c: np.nan for c in VALUE_COLUMNS}, index=base_data.index, dtype="float64"
        )

    shifted = ctx.copy()
    shifted.index = pd.Index(
        np.asarray(ctx.index, dtype="int64") + tf_ms, dtype="int64", name="ts"
    )
    # ``ts + tf_ms`` es el instante en que esa vela de contexto quedó cerrada, así
    # que un ffill sobre el índice de la base nunca puede adelantar información.
    return shifted.reindex(base_data.index, method="ffill")


__all__ = (
    "COLUMNS",
    "PRICE_COLUMNS",
    "VALUE_COLUMNS",
    "Anomaly",
    "Gap",
    "align_context",
    "empty_frame",
    "ensure_canonical",
    "fill_short_gaps",
    "infer_timeframe_ms",
    "slice_causal",
    "timeframe_ms",
    "validate_candles",
)
