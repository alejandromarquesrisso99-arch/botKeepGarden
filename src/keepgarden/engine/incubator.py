"""La incubadora: dónde se decide quién llega a nacer.

Es el reloj rápido del sistema (docs/ARCHITECTURE.md §2) y el sitio donde el
sobreajuste tiene más ganas de entrar. Casi todo lo que hay aquí son defensas
contra eso.

CONTRATO — implementar en el hito 4.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Sequence

from ..config import Config
from ..genome.schema import Genome

if TYPE_CHECKING:  # pragma: no cover
    import pandas as pd


@dataclass(slots=True)
class IncubationResult:
    genome: Genome
    passed: bool
    reject_reason: str = ""
    median_sortino: float = 0.0
    median_drawdown: float = 0.0
    median_trades: float = 0.0
    oos_decay: float = 0.0
    threshold_used: float = 0.0
    fold_metrics: list[dict[str, float]] = field(default_factory=list)
    fitness_incubator: float = 0.0
    duration_ms: int = 0


@dataclass(slots=True)
class Incubator:
    """Criba de candidatos antes de entrar al jardín vivo."""

    cfg: Config
    candles: "pd.DataFrame"

    def screen(
        self,
        candidates: Sequence[Genome],
        *,
        alive_genomes: Sequence[Genome] = (),
        generation: int = 0,
    ) -> list[IncubationResult]:
        """Evalúa una cosecha entera y decide quién nace.

        1. Correr walk-forward (``evaluation.walkforward``) sobre cada
           candidato, en paralelo.
        2. Tomar la **mediana** de los folds, no la media: la mediana castiga a
           los bots que dependen de un único periodo afortunado.
        3. Calcular ``oos_decay = 1 - sortino_validation / max(sortino_train, ε)``.
        4. Inflar el umbral con el número de candidatos de la cosecha:
           ``min_sortino * (1 + multiplicity_penalty * log10(max(1, n)))``.
           Probar 10.000 genomas y quedarse con el mejor no es selección, es
           dragado de datos; esto lo compensa explícitamente.
        5. Rechazar clones: distancia mínima a cualquier genoma vivo por debajo
           de ``speciation.clone_threshold``.
        6. Registrar TODOS los resultados en ``incubation_runs``, también los
           rechazados: saber qué no funciona es la mitad del informe del
           jardinero.

        **El holdout no se toca aquí.** Sólo en ``promote``.
        """
        raise NotImplementedError

    def promote(self, genome: Genome, generation: int) -> IncubationResult:
        """Evalúa el holdout UNA sola vez y decide el ascenso definitivo.

        Este es el único punto del sistema autorizado a leer el tramo de
        holdout. Se escribe una fila en ``holdout_results`` con clave primaria
        ``bot_id``: si alguien intenta escribir una segunda, la base lo impide y
        eso es exactamente lo que se quiere, porque significaría que alguien
        está mirando el holdout más de una vez.

        Si el bot se degrada en holdout, no asciende y se penaliza a su familia
        de ideas en la siguiente cosecha.
        """
        raise NotImplementedError


__all__ = ("IncubationResult", "Incubator")
