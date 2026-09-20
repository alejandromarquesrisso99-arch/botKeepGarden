"""El bucle del jardín: un solo proceso, un solo hilo, indefinidamente.

Con timeframe de 1h y poblaciones de cientos de bots, un tick tarda
milisegundos y hay 3.600 segundos entre velas. No hace falta concurrencia aquí;
la paralelización vive en la incubadora.

CONTRATO — implementar en el hito 5.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..config import Config
from ..types import GardenStatus

if TYPE_CHECKING:  # pragma: no cover
    from ..storage.db import Database
    from .clock import Tick


@dataclass(slots=True)
class GardenRunner:
    """Orquestador del jardín vivo."""

    cfg: Config
    db: "Database"
    status: GardenStatus = GardenStatus.STOPPED

    def run(self, *, dry_run: bool = False, speed: float = 0.0, max_ticks: int | None = None) -> None:
        """Bucle principal. Ver el pseudocódigo de docs/EVOLUTION.md.

        Al arrancar:

        1. Cargar estado desde SQLite: población viva, último tick procesado,
           generación actual.
        2. Recuperar las velas perdidas mientras estaba caído (*catch-up*) y
           procesarlas en orden, sin esperas.
        3. Entrar en el bucle normal.

        El jardín debe poder morir y resucitar sin perder nada. Un tick es
        idempotente: reprocesar la vela ``t`` no duplica trades, porque
        ``orders`` tiene índice único sobre ``(bot_id, candle_ts, kind)``.
        """
        raise NotImplementedError

    def process_tick(self, tick: "Tick") -> None:
        """Un tick completo: los 8 pasos de docs/ARCHITECTURE.md §4."""
        raise NotImplementedError

    def close_generation(self, generation: int) -> None:
        """Evaluación de generación: los 10 pasos de docs/ARCHITECTURE.md §5.

        Al terminar, si toca sesión de jardinero
        (``generation % gardener.every_generations == 0``) o hay una alerta
        activa que la dispare, genera el informe y encola la sesión. El jardín
        **no se detiene** esperando: sigue con las reglas actuales.
        """
        raise NotImplementedError

    def check_circuit_breakers(self) -> list[str]:
        """Comprobaciones de cada tick, independientes del fitness.

        Ver la tabla de docs/EXECUTION.md §Circuit breakers. Devuelve los ids de
        los bots podados en el acto.
        """
        raise NotImplementedError

    def catch_up(self) -> int:
        """Procesa las velas perdidas tras una caída. Devuelve cuántas."""
        raise NotImplementedError

    def shutdown(self) -> None:
        """Parada limpia: cerrar la generación en curso a medias no, pero sí
        volcar estado y marcar ``garden_meta.status = STOPPED``."""
        raise NotImplementedError


__all__ = ("GardenRunner",)
