"""Conexión a SQLite y migraciones.

Un archivo = un jardín completo, copiable como snapshot. Modo WAL, de forma que
el dashboard pueda leer en paralelo sin bloquear al motor.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

SCHEMA_VERSION = 1
SCHEMA_PATH = Path(__file__).with_name("schema.sql")


class SchemaTooNew(RuntimeError):
    """La base la escribió una versión posterior del código."""


@dataclass(slots=True)
class Database:
    """Acceso a la base del jardín."""

    path: Path
    read_only: bool = False
    _con: sqlite3.Connection | None = field(default=None, repr=False)

    def connect(self) -> sqlite3.Connection:
        """Abre la conexión.

        En modo lectura usa URI ``file:...?mode=ro`` — es lo que usa el
        dashboard, que nunca escribe. ``row_factory = sqlite3.Row`` siempre, así
        las filas se leen por nombre y no por posición.
        """
        if self._con is not None:
            return self._con

        ruta = Path(self.path)
        if self.read_only:
            uri = f"file:{ruta.as_posix()}?mode=ro"
            con = sqlite3.connect(uri, uri=True, timeout=30.0)
        else:
            ruta.parent.mkdir(parents=True, exist_ok=True)
            con = sqlite3.connect(ruta, timeout=30.0, isolation_level=None)

        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        if not self.read_only:
            con.execute("PRAGMA journal_mode = WAL")
            con.execute("PRAGMA synchronous = NORMAL")
        self._con = con
        return con

    def initialize(self) -> None:
        """Crea el esquema si no existe y escribe ``garden_meta``."""
        if self.read_only:
            raise RuntimeError("una base abierta en sólo lectura no se inicializa")
        con = self.connect()
        con.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        if self.get_meta("schema_version") is None:
            self.set_meta("schema_version", str(SCHEMA_VERSION))
            self.set_meta("created_at", _ahora())
        self.migrate()

    def migrate(self) -> None:
        """Aplica migraciones pendientes comparando ``garden_meta.schema_version``.

        Mientras el jardín esté en desarrollo y no haya datos que valgan, una
        migración puede ser destructiva; en cuanto haya un jardín corriendo de
        verdad, cada cambio de esquema necesita su script en ``migrations/``.
        """
        actual = int(self.get_meta("schema_version") or SCHEMA_VERSION)
        if actual > SCHEMA_VERSION:
            raise SchemaTooNew(
                f"{self.path} usa el esquema {actual} y este código entiende "
                f"hasta el {SCHEMA_VERSION}. Actualiza keepgarden antes de "
                f"tocar este jardín."
            )
        if actual < SCHEMA_VERSION:
            # Todavía no hay ninguna migración que aplicar. Cuando la haya, su
            # script vive en migrations/ y se aplica aquí en orden.
            self.set_meta("schema_version", str(SCHEMA_VERSION))

    def snapshot(self, tag: str = "") -> Path:
        """Copia la base a ``state/db/snapshots/garden_<gen>_<tag>.db``.

        Usa la API ``backup`` de sqlite3, que es segura con el motor corriendo.
        Se llama cada ``storage.snapshot_every_generations`` generaciones.
        """
        generacion = self.get_meta("current_generation") or "0"
        destino_dir = Path(self.path).parent / "snapshots"
        destino_dir.mkdir(parents=True, exist_ok=True)
        sufijo = f"_{tag}" if tag else ""
        destino = destino_dir / f"garden_{int(generacion):04d}{sufijo}.db"

        origen = self.connect()
        copia = sqlite3.connect(destino)
        try:
            origen.backup(copia)
        finally:
            copia.close()
        return destino

    # -- metadatos --------------------------------------------------------- #

    def get_meta(self, key: str) -> str | None:
        fila = self.query_one("SELECT value FROM garden_meta WHERE key = ?", (key,))
        return None if fila is None else str(fila["value"])

    def set_meta(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO garden_meta (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (key, str(value), _ahora()),
        )

    # -- utilidades -------------------------------------------------------- #

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        return self.connect().execute(sql, tuple(params))

    def executemany(self, sql: str, rows: Sequence[Sequence[Any]]) -> sqlite3.Cursor:
        return self.connect().executemany(sql, [tuple(r) for r in rows])

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return list(self.connect().execute(sql, tuple(params)).fetchall())

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        return self.connect().execute(sql, tuple(params)).fetchone()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Context manager con commit al salir y rollback ante excepción.

        Una generación se cierra dentro de UNA transacción: o se escriben la
        fila de generación, las métricas, los nacimientos y las muertes, o no se
        escribe nada. Un cierre a medias dejaría el jardín en un estado que
        ningún arranque sabría interpretar.

        Anidarlas es seguro: la interna se apunta a la que ya está abierta en
        vez de abrir otra, porque SQLite no tiene transacciones anidadas de
        verdad y un commit interno cerraría la de fuera a traición.
        """
        con = self.connect()
        if con.in_transaction:
            yield con
            return
        con.execute("BEGIN IMMEDIATE")
        try:
            yield con
        except BaseException:
            con.rollback()
            raise
        con.commit()

    def close(self) -> None:
        if self._con is not None:
            self._con.close()
            self._con = None

    def __enter__(self) -> "Database":
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _ahora() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def open_database(path: Path | str, *, read_only: bool = False, create: bool = True) -> Database:
    """Abre —y si hace falta crea— la base del jardín."""
    db = Database(path=Path(path), read_only=read_only)
    if create and not read_only:
        db.initialize()
    else:
        db.connect()
    return db


__all__ = ("Database", "SchemaTooNew", "SCHEMA_VERSION", "SCHEMA_PATH", "open_database")
