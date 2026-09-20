"""El diario del jardín.

Una entrada en prosa por sesión, escrita por el jardinero. No es un log de
máquina: es la narración de lo que está pasando en el ecosistema. Sirve para que
Alex pueda leer la historia del jardín como una historia, y para que el propio
jardinero recupere contexto rápido al volver dentro de un mes.

CONTRATO — implementar en el hito 7.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..storage.repositories import Repositories


@dataclass(slots=True)
class Journal:
    repos: Repositories

    def write(self, session_id: int, text: str) -> None:
        """Guarda la entrada de una sesión."""
        raise NotImplementedError

    def recent(self, n: int = 3) -> list[str]:
        """Últimas entradas. Es lo primero que el jardinero lee al volver."""
        raise NotImplementedError

    def context_digest(self, generation: int) -> str:
        """Resumen compacto del estado narrativo del jardín.

        Va en la cabecera del informe para que el jardinero recupere el hilo sin
        releer meses de diario: qué linajes dominan, qué se probó
        recientemente, qué está pendiente de revisión.
        """
        raise NotImplementedError


__all__ = ("Journal",)
