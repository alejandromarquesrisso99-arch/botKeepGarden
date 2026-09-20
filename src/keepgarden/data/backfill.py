"""Trabajos sobre la caché de velas: descargar, sanear y auditar.

Aquí vive lo que hacen ``keepgarden data backfill`` y ``keepgarden data
status``. Está fuera de ``cli.py`` a propósito: un backfill reanudable tiene
suficiente lógica como para merecer tests propios, y la CLI debe ser una capa
de presentación, no el sitio donde vive el comportamiento.

El saneamiento sigue docs/DATA.md al pie de la letra:

* hueco de 1–2 velas  → se rellena por forward fill con volumen 0 y se marca;
* hueco más largo     → **no** se rellena, sólo se marca;
* vela sospechosa     → se registra, nunca se descarta en silencio.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from ..types import Timestamp
from .candles import (
    Anomaly,
    Gap,
    fill_short_gaps,
    timeframe_ms,
    validate_candles,
)
from .sources import VenueClient
from .store import CandleStore, SeriesKey

#: Aviso de cada bloque persistido: (serie, último ts del bloque, velas nuevas).
Progress = Callable[[SeriesKey, Timestamp, int], None]

NOTE_FILLED = "hueco corto rellenado por forward fill (volumen 0)"
NOTE_LONG = "hueco largo del venue: no se rellena"


def parse_since(text: str) -> Timestamp:
    """Convierte una fecha ISO (``2019-01-01`` o completa) a ms UTC."""
    raw = text.strip().replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        raise ValueError(
            f"fecha no reconocida: {text!r}. Usa ISO, por ejemplo 2019-01-01"
        ) from None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return int(moment.timestamp() * 1000)


def miles(n: int) -> str:
    """Un entero con separador de miles a la española."""
    return f"{int(n):,}".replace(",", ".")


def format_ts(ts: Timestamp | None) -> str:
    """Un ts en ms a texto legible en UTC."""
    if ts is None:
        return "—"
    return datetime.fromtimestamp(int(ts) / 1000.0, tz=UTC).strftime("%Y-%m-%d %H:%M")


# --------------------------------------------------------------------------- #
# Saneamiento                                                                  #
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class HealReport:
    """Qué se arregló y qué se dejó marcado tras una pasada de saneamiento."""

    filled: int = 0
    short_gaps: list[Gap] = field(default_factory=list)
    long_gaps: list[Gap] = field(default_factory=list)
    anomalies: list[Anomaly] = field(default_factory=list)


def heal(
    store: CandleStore, key: SeriesKey, *, jump_threshold: float = 0.40
) -> HealReport:
    """Rellena los huecos cortos, marca todos y registra las anomalías.

    Es idempotente: pasarlo dos veces no cambia nada, porque tras la primera
    pasada los huecos cortos ya no existen y los largos ya están marcados.
    """
    data = store.load(key)
    report = HealReport()
    if data.empty:
        return report

    gaps, anomalies = validate_candles(data, key.timeframe, jump_threshold=jump_threshold)
    report.anomalies = anomalies
    report.short_gaps = [g for g in gaps if g.is_short]
    report.long_gaps = [g for g in gaps if not g.is_short]

    if report.short_gaps:
        healed = fill_short_gaps(data, report.short_gaps, key.timeframe)
        synthetic = healed.loc[~healed.index.isin(data.index)]
        report.filled = len(synthetic)
        if report.filled:
            store.append(key, synthetic)
        store.mark_gaps(key, report.short_gaps, filled=True, note=NOTE_FILLED)

    if report.long_gaps:
        store.mark_gaps(key, report.long_gaps, filled=False, note=NOTE_LONG)

    return report


# --------------------------------------------------------------------------- #
# Backfill                                                                     #
# --------------------------------------------------------------------------- #


#: Un tramo a pedir al venue: desde dónde y hasta dónde (``None`` = hasta hoy).
Range = tuple[Timestamp, "Timestamp | None"]


def ranges_to_fetch(
    bounds: tuple[Timestamp, Timestamp] | None,
    since: Timestamp,
    until: Timestamp | None,
    tf_ms: int,
) -> list[Range]:
    """Qué tramos faltan por descargar, dados los que ya hay en caché.

    Son como mucho dos: la **cabeza** (historia anterior a la primera vela
    guardada) y la **cola** (lo que haya pasado desde la última). Pedir sólo la
    cola es el error evidente: quien ya tenía el último mes y pide desde 2019 se
    quedaría con el último mes y creyéndose que tiene siete años.

    Los huecos interiores no salen de aquí: los detecta y los marca ``heal``,
    porque casi siempre son paradas del venue y no datos que falten por pedir.
    """
    since = int(since)
    if bounds is None:
        return [(since, until)]

    first, last = int(bounds[0]), int(bounds[1])
    ranges: list[Range] = []
    if since < first:
        ranges.append((since, first - tf_ms))
    tail_from = last + tf_ms
    if until is None or tail_from <= int(until):
        ranges.append((tail_from, until))
    return ranges


@dataclass(slots=True)
class BackfillReport:
    """Resultado de una pasada de backfill sobre una serie."""

    key: SeriesKey
    resumed_from: Timestamp
    resumed: bool
    ranges: list[Range] = field(default_factory=list)
    fetched: int = 0
    blocks: int = 0
    heal: HealReport = field(default_factory=HealReport)
    first_ts: Timestamp | None = None
    last_ts: Timestamp | None = None
    n_candles: int = 0

    @property
    def up_to_date(self) -> bool:
        """No había nada nuevo que traerse."""
        return self.fetched == 0

    @property
    def extended_backwards(self) -> bool:
        """¿Se ha tenido que ir a buscar historia anterior a la que había?"""
        return len(self.ranges) > 1


def backfill(
    store: CandleStore,
    client: VenueClient,
    key: SeriesKey,
    *,
    since: Timestamp,
    until: Timestamp | None = None,
    jump_threshold: float = 0.40,
    progress: Progress | None = None,
) -> BackfillReport:
    """Descarga lo que falte de una serie y deja la caché sana.

    Reanudable por construcción: el punto de partida sale de la propia caché,
    no de un archivo de estado aparte. Cada bloque se persiste según llega, de
    forma que matar el proceso a mitad y relanzarlo continúa donde estaba sin
    volver a pedir lo ya descargado.

    Si ``since`` es anterior a lo que hay en caché, también se descarga la
    historia que falta por delante.
    """
    tf_ms = timeframe_ms(key.timeframe)
    bounds = store.bounds(key)
    ranges = ranges_to_fetch(bounds, since, until, tf_ms)

    report = BackfillReport(
        key=key,
        resumed_from=ranges[0][0] if ranges else int(since),
        resumed=bounds is not None,
        ranges=ranges,
    )

    for start, stop in ranges:
        for block in client.iter_backfill(key.symbol, key.timeframe, start, until=stop):
            new = store.append(key, block)
            report.fetched += new
            report.blocks += 1
            if progress is not None:
                progress(key, int(block.index[-1]), report.fetched)

    report.heal = heal(store, key, jump_threshold=jump_threshold)

    current = store.bounds(key)
    if current is not None:
        report.first_ts, report.last_ts = current
    report.n_candles = store.count(key)
    return report


# --------------------------------------------------------------------------- #
# Estado                                                                       #
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class SeriesStatus:
    """Foto de una serie en caché: lo que hay, lo que falta y lo raro."""

    key: SeriesKey
    n_candles: int = 0
    first_ts: Timestamp | None = None
    last_ts: Timestamp | None = None
    expected: int = 0
    gaps: list[Gap] = field(default_factory=list)
    unmarked_gaps: list[Gap] = field(default_factory=list)
    marked_gaps: list[dict] = field(default_factory=list)
    anomalies: list[Anomaly] = field(default_factory=list)
    synthetic: int = 0

    @property
    def missing(self) -> int:
        """Velas que faltan para que el rango esté completo."""
        return max(0, self.expected - self.n_candles)

    @property
    def coverage(self) -> float:
        """Fracción del rango cubierta, en [0, 1]."""
        return 1.0 if not self.expected else self.n_candles / self.expected

    def anomalies_by_kind(self) -> dict[str, int]:
        """Cuántas anomalías hay de cada tipo."""
        counts: dict[str, int] = {}
        for a in self.anomalies:
            counts[a.kind] = counts.get(a.kind, 0) + 1
        return counts

    @property
    def ok(self) -> bool:
        """¿Está la serie en condiciones de alimentar al jardín?

        Lo está si no hay huecos sin explicar ni errores estructurales. Un
        salto de precio marcado o un volumen cero no invalidan la serie: son
        avisos, y el mercado a veces es así de feo.
        """
        estructurales = {"duplicate", "misaligned", "bad_ohlc", "negative_volume"}
        return not self.unmarked_gaps and not any(
            a.kind in estructurales for a in self.anomalies
        )


def series_status(
    store: CandleStore, key: SeriesKey, *, jump_threshold: float = 0.40
) -> SeriesStatus:
    """Audita una serie de la caché sin tocarla."""
    data = store.load(key)
    status = SeriesStatus(key=key, n_candles=len(data))
    status.marked_gaps = store.marked_gaps(key)
    if data.empty:
        return status

    tf_ms = timeframe_ms(key.timeframe)
    status.first_ts = int(data.index[0])
    status.last_ts = int(data.index[-1])
    status.expected = int((status.last_ts - status.first_ts) // tf_ms) + 1
    status.gaps, anomalies = validate_candles(
        data, key.timeframe, jump_threshold=jump_threshold
    )
    status.unmarked_gaps = [g for g in status.gaps if not store.is_marked(key, g)]

    # El volumen cero de una vela que pusimos nosotros al rellenar un hueco no
    # es una anomalía del mercado: es nuestra firma. Se cuenta aparte, porque
    # confundir las dos cosas hace que el informe mienta en los dos sentidos.
    rellenos = [
        (int(g["start_ts"]), int(g["end_ts"])) for g in status.marked_gaps if g.get("filled")
    ]
    for anomaly in anomalies:
        if anomaly.kind == "zero_volume" and any(
            lo <= anomaly.ts <= hi for lo, hi in rellenos
        ):
            status.synthetic += 1
        else:
            status.anomalies.append(anomaly)
    return status


def status_lines(status: SeriesStatus) -> list[str]:
    """El informe de una serie, listo para imprimir."""
    s = status
    marca = "ok " if s.ok else "!! "
    lines = [
        f"{marca}{s.key}",
        f"    rango      {format_ts(s.first_ts)} → {format_ts(s.last_ts)}",
        f"    velas      {miles(s.n_candles)} de {miles(s.expected)} esperadas "
        f"({s.coverage * 100:.2f} %)",
    ]
    if s.missing:
        lines.append(f"    faltan     {miles(s.missing)}")
    lines.append(
        f"    huecos     {len(s.gaps)} en la serie · {len(s.marked_gaps)} marcados · "
        f"{len(s.unmarked_gaps)} SIN MARCAR"
    )
    if s.synthetic:
        lines.append(f"    relleno    {miles(s.synthetic)} velas sintéticas (volumen 0)")
    for gap in s.unmarked_gaps[:5]:
        lines.append(
            f"               sin marcar: {format_ts(gap.start_ts)} "
            f"({gap.n_missing} velas)"
        )
    if len(s.unmarked_gaps) > 5:
        lines.append(f"               … y {len(s.unmarked_gaps) - 5} más")
    kinds = s.anomalies_by_kind()
    if kinds:
        detalle = " · ".join(f"{k}: {v}" for k, v in sorted(kinds.items()))
        lines.append(f"    anomalías  {detalle}")
    else:
        lines.append("    anomalías  ninguna")
    return lines


def resolve_keys(store: CandleStore, symbol: str | None = None) -> Sequence[SeriesKey]:
    """Las series de la caché, filtradas por símbolo si se pide."""
    keys = store.series_keys()
    if symbol:
        wanted = symbol.upper()
        keys = [k for k in keys if k.symbol.upper() == wanted]
    return keys


__all__ = (
    "NOTE_FILLED",
    "NOTE_LONG",
    "BackfillReport",
    "HealReport",
    "Progress",
    "SeriesStatus",
    "backfill",
    "format_ts",
    "heal",
    "miles",
    "parse_since",
    "resolve_keys",
    "series_status",
    "status_lines",
)
