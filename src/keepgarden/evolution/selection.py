"""Selección y poda: quién se reproduce y quién muere.

CONTRATO — implementar en el hito 4.
"""

from __future__ import annotations

import random
from typing import Mapping, Sequence

from ..config import Config
from ..types import BotId, DeathCause
from .speciation import Species


def select_parents(
    species: Sequence[Species],
    fitness: Mapping[BotId, float],
    quotas: Mapping[str, int],
    cfg: Config,
    rng: random.Random,
) -> list[BotId]:
    """Elige los padres de la siguiente cosecha.

    * **Elitismo**: los ``cfg.garden.elite_count`` mejores del jardín se
      reproducen seguro y están protegidos de la poda.
    * **Torneo dentro de especie**: tamaño ``cfg.evolution.tournament_size``,
      gana el de mejor fitness compartido.
    * Un bot puede ser padre varias veces en la misma generación, pero no más de
      3 (si no, el jardín se llena de hijos de un solo bot en dos generaciones).
    * Los bots con fitness indefinido (pocas operaciones) no son padres, pero
      tampoco mueren: se les da otra generación.
    """
    raise NotImplementedError


def select_deaths(
    fitness: Mapping[BotId, float],
    ages: Mapping[BotId, int],
    idle: Mapping[BotId, int],
    drawdowns: Mapping[BotId, float],
    protected: set[BotId],
    pareto: set[BotId],
    clones: Sequence[tuple[BotId, BotId]],
    cfg: Config,
) -> dict[BotId, DeathCause]:
    """Decide quién muere y por qué.

    Un bot muere si se cumple cualquiera (ver docs/EVOLUTION.md §Poda):

    * peor ``cull_fraction`` por fitness **y** edad >= ``min_age_generations``,
    * drawdown > ``risk.hard_max_drawdown`` (esto lo detecta antes el circuit
      breaker, pero se comprueba aquí también),
    * ``idle_generations >= idle_generations_limit``,
    * es clon de otro vivo con mejor fitness,
    * lo jubila el jardinero.

    Exentos: la élite, los bots del frente de Pareto, los protegidos por el
    jardinero y los que no llegan a ``min_age_generations`` (salvo por drawdown,
    que no perdona a nadie).

    No puede dejar la población por debajo de ``cfg.garden.min_population``: si
    la poda lo haría, se recorta empezando por los de mejor fitness entre los
    condenados.
    """
    raise NotImplementedError


def plan_births(
    quotas: Mapping[str, int], cfg: Config, *, diversity: float, family_shares: Mapping[str, float]
) -> dict[str, int]:
    """Reparte los nacimientos entre operadores.

    Base: ``mutation_share``, ``crossover_share``, ``fusion_share``,
    ``seed_share``.

    Ajustes:

    * Si ``diversity < cfg.evolution.diversity_floor``, toda la cosecha pasa a
      ``SEED``: el jardín está convergiendo y hay que meter sangre nueva.
    * Si una familia supera ``max_family_share``, sus nacimientos se bloquean.
    * Si el drawdown del jardín supera ``risk.garden_max_drawdown``, se reducen
      los nacimientos a la mitad y sube la presión de poda: cuando el ecosistema
      está sufriendo no es momento de poblarlo.
    """
    raise NotImplementedError


__all__ = ("select_parents", "select_deaths", "plan_births")
