"""Aplicación de las propuestas del jardinero.

Valida contra los límites, aplica lo que pase, registra todo —lo aplicado y lo
rechazado, con su motivo— y programa la revisión del efecto.

CONTRATO — implementar en el hito 7.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from ..config import Config
from ..storage.repositories import Repositories
from .proposals import Proposal


@dataclass(slots=True)
class ApplyResult:
    applied: list[Proposal] = field(default_factory=list)
    rejected: list[tuple[Proposal, str]] = field(default_factory=list)
    created_bots: list[str] = field(default_factory=list)
    session_id: int = 0


@dataclass(slots=True)
class ProposalApplier:
    cfg: Config
    repos: Repositories

    def apply(self, proposals: Sequence[Proposal], generation: int) -> ApplyResult:
        """Valida y aplica una sesión completa.

        1. ``validate_session`` con los límites de ``cfg.gardener``. Una
           propuesta que viola un límite se rechaza con el motivo, y las demás
           siguen su curso: un error no invalida la sesión entera.
        2. Aplicar por tipo:
           * ``GRAFT``: construir el hijo injertando el bloque y mandarlo a la
             incubadora **como a cualquier otro**. Un injerto del jardinero no
             tiene privilegios: si no pasa el filtro, no nace.
           * ``SEED_FAMILY``: muestrear N genomas de la familia → incubadora.
           * ``FUSE``: crear el ensemble → incubadora.
           * ``RETIRE``: cambiar estado a RETIRED, cerrar posiciones.
           * ``PROTECT``: fijar ``protected_until_gen``.
           * ``TUNE``: escribir el parámetro efectivo de la siguiente generación
             (no toca ``garden.yaml``: los ajustes del jardinero viven en la
             base y son reversibles).
           * ``ADD_GENE``: NO se aplica en caliente. Genera un evento y una
             tarea de ingeniería: añadir un indicador requiere escribir código.
           * ``REBALANCE_QUOTAS``: pesos de familia efectivos.
           * ``NOTE``: sólo al diario.
        3. Registrar cada decisión en ``gardener_decisions`` con su
           ``review_in_generations``.
        4. Emitir eventos ``GARDENER_PROPOSAL``.
        """
        raise NotImplementedError

    def review_due(self, generation: int) -> int:
        """Revisa las decisiones cuya generación de revisión ha llegado.

        Mide el efecto real —la métrica que la propuesta decía que iba a
        cambiar— y lo compara con el esperado, dictando ``worked``,
        ``no_effect`` o ``backfired``. Esto es lo que aparece en el punto 9 del
        siguiente informe.
        """
        raise NotImplementedError


__all__ = ("ApplyResult", "ProposalApplier")
