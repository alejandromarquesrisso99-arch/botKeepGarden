"""Conexión a SQLite y migraciones.

Un archivo = un jardín completo, copiable como snapshot. Modo WAL, de forma que
el dashboard pueda leer en paralelo sin bloquear al motor.

CONTRATO — implementar en el hito 4.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Sequence

SCHEMA_VERSION = 1
SCHEMA_PATH = Path(__file__).with_name("schema.sql")


@dataclass(slots=True)
class Database:
    """Acceso a la base del jardín."""

    path: Path
    read_only: bool = False
    _con: sqlite3.Connection | None = None

    def connect(self) -> sqlite3.Connection:
        """Abre la conexión.

        En modo lectura usa URI ``file:...?mode=ro`` — es lo que usa el
        dashboard, que nunca escribe. ``row_factory = sqlite3.Row`` siempre, así
        las filas se leen por nombre y no por posición.
        """
        raise NotImplementedError

    def initialize(self) -> None:
        """Crea el esquema si no existe y escribe ``garden_meta``."""
        raise NotImplementedError

    def migrate(self) -> None:
        """Aplica migraciones pendientes comparando ``garden_meta.schema_version``.

        Mientras el jardín esté en desarrollo y no haya datos que valgan, una
        migración puede ser destructiva; en cuanto haya un jardín corriendo de
        verdad, cada cambio de esquema necesita su script en ``migrations/``.
        """
        raise NotImplementedError

    def snapshot(self, tag: str = "") -> Path:
        """Copia la base a ``state/db/snapshots/garden_<gen>_<tag>.db``.

        Usa la API ``backup`` de sqlite3, que es segura con el motor corriendo.
        Se llama cada ``storage.snapshot_every_generations`` generaciones.
        """
        raise NotImplementedError

    # -- utilidades -------------------------------------------------------- #

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        raise NotImplementedError

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        raise NotImplementedError

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        raise NotImplementedError

    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Context manager con commit al salir y rollback ante excepción.

        Una generación se cierra dentro de UNA transacción: o se escriben la
        fila de generación, las métricas, los nacimientos y las muertes, o no se
        escribe nada. Un cierre a medias dejaría el jardín en un estado que
        ningún arranque sabría interpretar.
        """
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


__all__ = ("Database", "SCHEMA_VERSION", "SCHEMA_PATH")
