"""Caché de velas en Parquet particionado por año.

    state/cache/candles/<VENUE>/<SYMBOL>/<TIMEFRAME>/<AÑO>.parquet

Más un manifiesto JSON con primer ts, último ts, número de velas y hash, para
arrancar sin releer todo.

El manifiesto es una **conveniencia, no la verdad**: los Parquet mandan. Si se
pierde o se corrompe, ``rebuild_manifest`` lo regenera leyendo el disco. Lo
único que vive sólo ahí son las marcas de huecos conocidos, que en el hito 4 se
volcarán además a la tabla ``data_gaps`` de SQLite (ver docs/DECISIONS.md
D-010): el paquete ``data`` no importa de ``storage`` por la regla de
dependencias de docs/ARCHITECTURE.md §6.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeAlias

import numpy as np
import pandas as pd

from ..types import Timeframe, Timestamp
from .candles import VALUE_COLUMNS, Gap, empty_frame, ensure_canonical

MANIFEST_VERSION = 1

#: Una entrada del manifiesto. Es JSON heterogéneo y tiparlo más fino sólo
#: añadiría ceremonia: la verdad está en los Parquet, no aquí.
ManifestEntry: TypeAlias = dict[str, Any]


@dataclass(frozen=True, slots=True)
class SeriesKey:
    venue: str
    symbol: str
    timeframe: Timeframe

    def path_parts(self) -> tuple[str, str, str]:
        return (self.venue.upper(), self.symbol.replace("/", "_"), self.timeframe)

    def __str__(self) -> str:
        return f"{self.venue}:{self.symbol}:{self.timeframe}"

    @property
    def manifest_id(self) -> str:
        """Clave de esta serie dentro del manifiesto."""
        return "/".join(self.path_parts())

    @staticmethod
    def from_path_parts(parts: Sequence[str]) -> SeriesKey:
        """Reconstruye la clave desde las carpetas de la caché."""
        venue, symbol, timeframe = parts
        return SeriesKey(venue.lower(), symbol.replace("_", "/"), timeframe)  # type: ignore[arg-type]


def _year_of(ts: Timestamp) -> int:
    return datetime.fromtimestamp(int(ts) / 1000.0, tz=UTC).year


def _years_of(index: pd.Index) -> np.ndarray:
    return np.asarray(
        pd.to_datetime(np.asarray(index, dtype="int64"), unit="ms", utc=True).year,
        dtype="int64",
    )


def _hash_frame(df: pd.DataFrame) -> str:
    """Huella de una serie: los ts y todos los valores, en orden."""
    h = hashlib.sha256()
    h.update(np.ascontiguousarray(np.asarray(df.index, dtype="int64")).tobytes())
    for col in VALUE_COLUMNS:
        h.update(np.ascontiguousarray(df[col].to_numpy(dtype="float64")).tobytes())
    return h.hexdigest()


@dataclass(slots=True)
class CandleStore:
    """Persistencia de velas. No sabe nada del venue ni de las estrategias."""

    cache_dir: Path
    compression: str = field(default="zstd")

    def __post_init__(self) -> None:
        self.cache_dir = Path(self.cache_dir)

    # -- rutas -------------------------------------------------------------- #

    @property
    def root(self) -> Path:
        """Raíz de la caché de velas."""
        return self.cache_dir / "candles"

    @property
    def manifest_path(self) -> Path:
        """Archivo del manifiesto."""
        return self.root / "manifest.json"

    def series_dir(self, key: SeriesKey) -> Path:
        """Carpeta de una serie concreta."""
        return self.root.joinpath(*key.path_parts())

    def path_for(self, key: SeriesKey, year: int) -> Path:
        """Parquet de un año de una serie."""
        return self.series_dir(key) / f"{year}.parquet"

    def years_on_disk(self, key: SeriesKey) -> list[int]:
        """Años con Parquet escrito, leídos del disco y no del manifiesto."""
        directory = self.series_dir(key)
        if not directory.is_dir():
            return []
        years: list[int] = []
        for p in directory.glob("*.parquet"):
            try:
                years.append(int(p.stem))
            except ValueError:
                continue
        return sorted(years)

    def series_keys(self) -> list[SeriesKey]:
        """Todas las series presentes en la caché, deducidas del disco.

        No se llama ``keys`` a propósito: un objeto con ``keys()`` se comporta
        como un mapeo para media biblioteca estándar, y esto no lo es.
        """
        if not self.root.is_dir():
            return []
        found: list[SeriesKey] = []
        for directory in sorted(self.root.glob("*/*/*")):
            if directory.is_dir() and any(directory.glob("*.parquet")):
                found.append(SeriesKey.from_path_parts(directory.relative_to(self.root).parts))
        return found

    # -- escritura ---------------------------------------------------------- #

    def append(self, key: SeriesKey, df: pd.DataFrame) -> int:
        """Añade velas, deduplicando por ``ts``. Devuelve cuántas eran nuevas.

        Debe ser idempotente: añadir dos veces el mismo bloque no duplica nada.
        Ante un ``ts`` ya presente gana el dato que llega: así un backfill
        repetido corrige una vela mala en vez de conservarla para siempre.
        """
        data = ensure_canonical(df)
        if data.empty:
            return 0
        data = data[~data.index.duplicated(keep="last")]

        new_count = 0
        years = _years_of(data.index)
        manifest = self._read_manifest()
        entry = manifest["series"].setdefault(key.manifest_id, self._blank_entry(key))

        for year in np.unique(years):
            chunk = data.loc[years == year]
            existing = self._read_year(key, int(year))
            if existing.empty:
                merged = chunk
                new_count += len(chunk)
            else:
                new_count += len(chunk.index.difference(existing.index))
                merged = pd.concat([existing, chunk])
                merged = merged[~merged.index.duplicated(keep="last")].sort_index(kind="stable")
                if merged.equals(existing):
                    continue
            self._write_year(key, int(year), merged)
            entry["years"][str(int(year))] = self._year_stats(merged)

        self._recompute_totals(entry)
        self._write_manifest(manifest)
        return new_count

    def _write_year(self, key: SeriesKey, year: int, df: pd.DataFrame) -> None:
        directory = self.series_dir(key)
        directory.mkdir(parents=True, exist_ok=True)
        target = self.path_for(key, year)
        fd, tmp_name = tempfile.mkstemp(dir=directory, suffix=".parquet.tmp")
        os.close(fd)
        tmp = Path(tmp_name)
        try:
            df.reset_index().to_parquet(
                tmp, index=False, engine="pyarrow", compression=self.compression
            )
            os.replace(tmp, target)
        finally:
            if tmp.exists():
                tmp.unlink()

    # -- lectura ------------------------------------------------------------ #

    def _read_year(self, key: SeriesKey, year: int) -> pd.DataFrame:
        path = self.path_for(key, year)
        if not path.is_file():
            return empty_frame()
        return ensure_canonical(pd.read_parquet(path, engine="pyarrow"))

    def load(
        self,
        key: SeriesKey,
        start: Timestamp | None = None,
        end: Timestamp | None = None,
    ) -> pd.DataFrame:
        """Carga un rango. Lee sólo los Parquet de los años implicados."""
        years = self.years_on_disk(key)
        if start is not None:
            years = [y for y in years if y >= _year_of(start)]
        if end is not None:
            years = [y for y in years if y <= _year_of(end)]
        if not years:
            return empty_frame()

        frames = [self._read_year(key, y) for y in years]
        frames = [f for f in frames if not f.empty]
        if not frames:
            return empty_frame()

        data = pd.concat(frames).sort_index(kind="stable")
        if start is not None:
            data = data.loc[data.index >= int(start)]
        if end is not None:
            data = data.loc[data.index <= int(end)]
        return data

    def bounds(self, key: SeriesKey) -> tuple[Timestamp, Timestamp] | None:
        """Primer y último ``ts`` en caché, o ``None`` si no hay nada."""
        entry = self.manifest(key) or self.rebuild_manifest(key)
        if not entry["n_candles"]:
            return None
        return (int(entry["first_ts"]), int(entry["last_ts"]))

    def count(self, key: SeriesKey) -> int:
        """Número de velas en caché."""
        entry = self.manifest(key) or self.rebuild_manifest(key)
        return int(entry["n_candles"])

    # -- manifiesto ---------------------------------------------------------- #

    def _blank_entry(self, key: SeriesKey) -> ManifestEntry:
        return {
            "venue": key.venue,
            "symbol": key.symbol,
            "timeframe": key.timeframe,
            "first_ts": None,
            "last_ts": None,
            "n_candles": 0,
            "hash": "",
            "years": {},
            "gaps": [],
            "updated_at": None,
        }

    @staticmethod
    def _year_stats(df: pd.DataFrame) -> ManifestEntry:
        return {
            "n": len(df),
            "first_ts": int(df.index[0]),
            "last_ts": int(df.index[-1]),
            "hash": _hash_frame(df),
        }

    @staticmethod
    def _recompute_totals(entry: ManifestEntry) -> None:
        years = entry["years"]
        if not years:
            entry.update(first_ts=None, last_ts=None, n_candles=0, hash="")
        else:
            ordered = [years[y] for y in sorted(years, key=int)]
            entry["n_candles"] = sum(int(y["n"]) for y in ordered)
            entry["first_ts"] = int(ordered[0]["first_ts"])
            entry["last_ts"] = int(ordered[-1]["last_ts"])
            digest = hashlib.sha256()
            for y in ordered:
                digest.update(str(y["hash"]).encode("ascii"))
            entry["hash"] = digest.hexdigest()
        entry["updated_at"] = datetime.now(UTC).isoformat(timespec="seconds")

    def _read_manifest(self) -> ManifestEntry:
        """Lee el manifiesto del disco. Uno corrupto se trata como inexistente:
        es una caché, y reconstruirla cuesta segundos."""
        blank: ManifestEntry = {"version": MANIFEST_VERSION, "series": {}}
        if not self.manifest_path.is_file():
            return blank
        try:
            raw = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            return blank
        if not isinstance(raw, dict) or not isinstance(raw.get("series"), dict):
            return blank
        raw.setdefault("version", MANIFEST_VERSION)
        return raw

    def _write_manifest(self, manifest: ManifestEntry) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=self.root, suffix=".json.tmp")
        os.close(fd)
        tmp = Path(tmp_name)
        try:
            tmp.write_text(
                json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            os.replace(tmp, self.manifest_path)
        finally:
            if tmp.exists():
                tmp.unlink()

    def manifest(self, key: SeriesKey) -> ManifestEntry | None:
        """La entrada de esta serie en el manifiesto, o ``None`` si no está."""
        return self._read_manifest()["series"].get(key.manifest_id)

    def rebuild_manifest(self, key: SeriesKey) -> ManifestEntry:
        """Recalcula el manifiesto leyendo los Parquet. Para recuperarse de un
        manifiesto corrupto o desincronizado.

        Las marcas de huecos se conservan: no se pueden deducir del disco.
        """
        manifest = self._read_manifest()
        previous = manifest["series"].get(key.manifest_id, {})
        entry = self._blank_entry(key)
        entry["gaps"] = list(previous.get("gaps", []))

        for year in self.years_on_disk(key):
            df = self._read_year(key, year)
            if df.empty:
                continue
            entry["years"][str(year)] = self._year_stats(df)

        self._recompute_totals(entry)
        manifest["series"][key.manifest_id] = entry
        self._write_manifest(manifest)
        return entry

    # -- huecos conocidos ---------------------------------------------------- #

    def mark_gaps(
        self,
        key: SeriesKey,
        gaps: Iterable[Gap],
        *,
        filled: bool = False,
        note: str = "",
    ) -> int:
        """Registra huecos conocidos. Devuelve cuántos no estaban marcados.

        Marcar un hueco es decir "sé que esto falta y por qué". ``data status``
        distingue los marcados de los que aparecieron sin que nadie los mirara,
        que son los que hay que investigar.
        """
        manifest = self._read_manifest()
        entry = manifest["series"].setdefault(key.manifest_id, self._blank_entry(key))
        known = {int(g["start_ts"]): g for g in entry.get("gaps", [])}
        added = 0
        for gap in gaps:
            record = {
                "start_ts": int(gap.start_ts),
                "end_ts": int(gap.end_ts),
                "n_missing": int(gap.n_missing),
                "filled": bool(filled),
                "note": note,
            }
            if int(gap.start_ts) not in known:
                added += 1
            known[int(gap.start_ts)] = record
        entry["gaps"] = [known[k] for k in sorted(known)]
        self._write_manifest(manifest)
        return added

    def marked_gaps(self, key: SeriesKey) -> list[ManifestEntry]:
        """Huecos ya registrados para esta serie, ordenados por ``start_ts``."""
        entry = self.manifest(key)
        if not entry:
            return []
        return sorted(entry.get("gaps", []), key=lambda g: int(g["start_ts"]))

    def is_marked(self, key: SeriesKey, gap: Gap) -> bool:
        """¿Está este hueco ya registrado, con el mismo tamaño?"""
        return any(
            int(g["start_ts"]) == int(gap.start_ts) and int(g["n_missing"]) == int(gap.n_missing)
            for g in self.marked_gaps(key)
        )


__all__ = ("MANIFEST_VERSION", "CandleStore", "ManifestEntry", "SeriesKey")
