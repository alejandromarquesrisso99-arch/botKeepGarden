"""Especiación: agrupar por distancia genética y repartir cupos.

Es el mecanismo que impide que el jardín entero acabe siendo variaciones de la
misma EMA. Ver docs/EVOLUTION.md §Selección y §Mantener el jardín diverso.

CONTRATO — implementar en el hito 4.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence

from ..config import Config
from ..genome.catalog import GeneCatalog
from ..genome.schema import Genome
from ..types import BotId, IdeaFamily, SpeciesId


@dataclass(slots=True)
class Species:
    species_id: SpeciesId
    representative: BotId
    members: list[BotId] = field(default_factory=list)
    dominant_family: IdeaFamily | None = None
    mean_fitness: float = 0.0
    shared_fitness: float = 0.0
    breeding_quota: int = 0
    mean_age: float = 0.0


def speciate(
    genomes: Mapping[BotId, Genome],
    cfg: Config,
    catalog: GeneCatalog,
    *,
    previous: Sequence[Species] = (),
) -> list[Species]:
    """Agrupa la población en especies por umbral de distancia.

    Algoritmo (el clásico de NEAT, que funciona bien y es barato):

    1. Cada especie de la generación anterior conserva su representante.
    2. Cada bot se asigna a la primera especie cuyo representante esté a
       distancia menor que ``cfg.speciation.species_threshold``.
    3. Los que no encajan en ninguna fundan una especie nueva.
    4. Las especies vacías desaparecen.

    Conservar los representantes entre generaciones es lo que da estabilidad a
    los ids de especie, y sin eso el dashboard mostraría un baile de colores sin
    significado.
    """
    raise NotImplementedError


def shared_fitness(
    fitness: Mapping[BotId, float], species: Sequence[Species]
) -> dict[BotId, float]:
    """Divide el fitness de cada bot por el tamaño de su especie.

    Una especie que domina se penaliza a sí misma. Es el mecanismo central
    contra la convergencia prematura: sin él, la primera idea que funciona
    coloniza el jardín en tres generaciones y la exploración se acaba.
    """
    raise NotImplementedError


def assign_quotas(
    species: Sequence[Species], total_births: int, fitness: Mapping[BotId, float]
) -> dict[SpeciesId, int]:
    """Reparte los nacimientos entre especies.

    Proporcional al fitness compartido agregado, con un mínimo de 1 para toda
    especie que tenga al menos un bot por encima de la mediana global. El
    redondeo sobrante va a la especie con mayor fitness compartido.
    """
    raise NotImplementedError


def detect_clones(
    genomes: Mapping[BotId, Genome], cfg: Config, catalog: GeneCatalog
) -> list[tuple[BotId, BotId]]:
    """Pares por debajo de ``clone_threshold``.

    Primero por hash de genoma (barato y exacto), después por distancia (caro,
    pero pilla los casi-clones). El de peor fitness de cada par se poda con
    causa ``CLONE``.
    """
    raise NotImplementedError


def family_shares(genomes: Mapping[BotId, Genome]) -> dict[IdeaFamily, float]:
    """Reparto de la población por familia de ideas.

    Si alguna supera ``cfg.evolution.max_family_share``, sus nacimientos se
    bloquean esa generación.
    """
    raise NotImplementedError


__all__ = (
    "Species",
    "speciate",
    "shared_fitness",
    "assign_quotas",
    "detect_clones",
    "family_shares",
)
