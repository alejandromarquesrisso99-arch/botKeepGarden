"""El diario del jardín.

Una entrada en prosa por sesión, escrita por el jardinero. No es un log de
máquina: es la narración de lo que está pasando en el ecosistema. Sirve para que
Alex pueda leer la historia del jardín como una historia, y para que el propio
jardinero recupere contexto rápido al volver dentro de un mes.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..storage.repositories import Repositories

#: Cuántas entradas se resumen en el digest de contexto.
DIGEST_ENTRIES = 3


@dataclass(slots=True)
class Journal:
    repos: Repositories

    def write(self, session_id: int, text: str) -> None:
        """Guarda la entrada de una sesión."""
        self.repos.gardener.close_session(int(session_id), text.strip())

    def recent(self, n: int = 3) -> list[str]:
        """Últimas entradas. Es lo primero que el jardinero lee al volver."""
        return [
            f"**Generación {fila['generation']}.** {fila['journal_entry']}"
            for fila in self.repos.gardener.journal(limit=n)
        ]

    def context_digest(self, generation: int) -> str:
        """Resumen compacto del estado narrativo del jardín.

        Va en la cabecera del informe para que el jardinero recupere el hilo sin
        releer meses de diario: qué linajes dominan, qué se probó
        recientemente y qué está pendiente de revisión.
        """
        db = self.repos.db
        partes: list[str] = []

        dominantes = db.query(
            "SELECT root_lineage, COUNT(*) AS n FROM bots WHERE status = 'ALIVE' "
            "GROUP BY root_lineage ORDER BY n DESC LIMIT 2"
        )
        vivos = self.repos.bots.count_alive()
        if dominantes and vivos:
            texto = ", ".join(
                f"`{f['root_lineage']}` ({int(f['n']) / vivos:.0%})" for f in dominantes
            )
            partes.append(f"Linajes dominantes: {texto}.")

        ultima = self.repos.gardener.journal(limit=1)
        if ultima:
            sesion = ultima[0]
            partes.append(
                f"Última sesión, generación {sesion['generation']}: "
                f"{str(sesion['journal_entry'])[:180]}"
            )
        else:
            partes.append("Primera visita al jardín: no hay diario todavía.")

        pendientes = db.query(
            "SELECT kind, target, generation, review_in_generations "
            "FROM gardener_decisions WHERE status = 'APPLIED' "
            "AND reviewed_generation IS NULL ORDER BY decision_id DESC LIMIT 4"
        )
        if pendientes:
            texto = ", ".join(
                f"{f['kind']}"
                + (f" sobre {f['target']}" if f["target"] else "")
                + f" (se revisa en la {int(f['generation']) + int(f['review_in_generations'])})"
                for f in pendientes
            )
            partes.append(f"Pendiente de revisión: {texto}.")

        return " ".join(partes)


__all__ = ("DIGEST_ENTRIES", "Journal")
