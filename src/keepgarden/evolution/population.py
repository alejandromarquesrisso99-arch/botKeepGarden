"""Orquestador de una generación: junta todas las piezas de la evolución.

Este módulo no inventa nada; llama a los demás en el orden correcto. Es el sitio
donde mirar para entender qué pasa cuando se cierra una generación.

CONTRATO — implementar en el hito 4.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..config import Config
from ..genome.catalog import GeneCatalog
from ..types import BotId, DeathCause

if TYPE_CHECKING:  # pragma: no cover
    from ..engine.incubator import Incubator
    from ..storage.db import Database


@dataclass(slots=True)
class GenerationOutcome:
    """Lo que pasó al cerrar una generación. Se persiste y se muestra."""

    generation: int
    births: list[BotId] = field(default_factory=list)
    deaths: dict[BotId, DeathCause] = field(default_factory=dict)
    fusions: list[BotId] = field(default_factory=list)
    discarded: int = 0
    n_species: int = 0
    genetic_diversity: float = 0.0
    fitness_best: float = 0.0
    fitness_median: float = 0.0
    alerts: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Population:
    """La población viva y las operaciones sobre ella."""

    cfg: Config
    catalog: GeneCatalog
    db: "Database"
    rng: random.Random

    def evolve_generation(
        self, generation: int, incubator: "Incubator"
    ) -> GenerationOutcome:
        """Los 10 pasos de docs/ARCHITECTURE.md §5, en orden:

        1. Cerrar la ventana y recoger trades y equity.
        2. ``evaluation.metrics.compute_metrics`` por bot.
        3. ``evaluation.fitness.compute_fitness`` + frente de Pareto.
        4. ``evolution.speciation.speciate``.
        5. ``shared_fitness`` y ``assign_quotas``.
        6. ``selection.select_parents``.
        7. Generar descendencia: ``mutate``, ``crossover``, ``fuse``, ``seed``
           según ``plan_births``.
        8. ``incubator.screen`` — sólo los que pasan nacen.
        9. ``selection.select_deaths`` y podar.
        10. Persistir generación, métricas, eventos y alertas.

        Todo lo aleatorio pasa por ``self.rng``: misma semilla, mismo jardín.
        """
        raise NotImplementedError

    def admit(self, newborns: list[object], generation: int) -> list[BotId]:
        """Da de alta a los recién nacidos: bot, genoma, parentesco, cartera."""
        raise NotImplementedError

    def cull(self, deaths: dict[BotId, DeathCause], generation: int) -> None:
        """Mata bots: cierra sus posiciones abiertas al precio actual, cambia su
        estado y registra el evento. Un bot nunca se borra: cambia de estado y
        su historia completa permanece, que es lo que alimenta la genealogía."""
        raise NotImplementedError

    def alive_ids(self) -> list[BotId]:
        raise NotImplementedError


__all__ = ("GenerationOutcome", "Population")
