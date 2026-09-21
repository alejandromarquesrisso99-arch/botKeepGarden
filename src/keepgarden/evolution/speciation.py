"""Especiación: agrupar por distancia genética y repartir cupos.

Es el mecanismo que impide que el jardín entero acabe siendo variaciones de la
misma EMA. Ver docs/EVOLUTION.md §Selección y §Mantener el jardín diverso.
"""

from __future__ import annotations

import hashlib
import statistics
from collections import Counter
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from ..config import Config
from ..genome.catalog import GeneCatalog
from ..genome.distance import genome_distance
from ..genome.schema import Genome, genome_hash
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

    @property
    def size(self) -> int:
        return len(self.members)


def new_species_id(representative: BotId) -> SpeciesId:
    """Id estable y legible, derivado de quien funda la especie."""
    huella = hashlib.sha256(representative.encode("utf-8")).hexdigest()[:6]
    return f"sp_{huella}"


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
    weights = cfg.speciation.distance_weights
    umbral = cfg.speciation.species_threshold

    # Los representantes de la generación anterior que sigan vivos abren la
    # lista; los que murieron ceden el sitio a otro miembro de su especie que
    # siga vivo, para no perder la continuidad del id.
    abiertas: list[Species] = []
    for vieja in previous:
        heredero = vieja.representative if vieja.representative in genomes else next(
            (m for m in vieja.members if m in genomes), None
        )
        if heredero is not None:
            abiertas.append(Species(species_id=vieja.species_id, representative=heredero))

    for bot in sorted(genomes):
        genoma = genomes[bot]
        destino = next(
            (
                sp
                for sp in abiertas
                if genome_distance(genoma, genomes[sp.representative], weights, catalog,
                                   members=genomes)
                < umbral
            ),
            None,
        )
        if destino is None:
            destino = Species(species_id=new_species_id(bot), representative=bot)
            abiertas.append(destino)
        destino.members.append(bot)

    vivas = [sp for sp in abiertas if sp.members]
    for sp in vivas:
        familias = Counter(genomes[m].family for m in sp.members)
        sp.dominant_family = familias.most_common(1)[0][0]
    return vivas


def shared_fitness(
    fitness: Mapping[BotId, float], species: Sequence[Species]
) -> dict[BotId, float]:
    """Divide el fitness de cada bot por el tamaño de su especie.

    Una especie que domina se penaliza a sí misma. Es el mecanismo central
    contra la convergencia prematura: sin él, la primera idea que funciona
    coloniza el jardín en tres generaciones y la exploración se acaba.
    """
    out: dict[BotId, float] = {}
    for sp in species:
        tamaño = max(1, sp.size)
        # Un fitness indefinido —el de quien aún no ha operado lo suficiente—
        # no se reparte ni cuenta para la media: no es un cero, es un "todavía
        # no se sabe", y tratarlo como cero hundiría a su especie entera.
        propios = [
            fitness[m] for m in sp.members if m in fitness and fitness[m] == fitness[m]
        ]
        for bot in sp.members:
            valor = fitness.get(bot)
            if valor is not None and valor == valor:
                out[bot] = valor / tamaño
        sp.mean_fitness = float(statistics.fmean(propios)) if propios else 0.0
        sp.shared_fitness = sum(out.get(m, 0.0) for m in sp.members)
    return out


def assign_quotas(
    species: Sequence[Species], total_births: int, fitness: Mapping[BotId, float]
) -> dict[SpeciesId, int]:
    """Reparte los nacimientos entre especies.

    Proporcional al fitness compartido agregado, con un mínimo de 1 para toda
    especie que tenga al menos un bot por encima de la mediana global. El
    redondeo sobrante va a la especie con mayor fitness compartido.

    Cuando hay más especies con derecho a mínimo que nacimientos disponibles
    —lo normal en un jardín recién sembrado, donde casi cada bot es su propia
    especie— el mínimo se reparte por orden de fitness compartido hasta que se
    acaban los nacimientos. Repartir de menos es correcto; inventarse
    nacimientos que la configuración no autoriza, no.
    """
    total_births = max(0, int(total_births))
    if not species or total_births == 0:
        return {sp.species_id: 0 for sp in species}

    valores = [f for f in fitness.values() if f == f]
    mediana = float(statistics.median(valores)) if valores else 0.0
    con_derecho = [
        sp
        for sp in species
        if any(fitness.get(m, float("nan")) > mediana for m in sp.members)
    ]

    # El fitness compartido puede ser negativo (es un z-score): se desplaza para
    # que el reparto proporcional tenga sentido. Un NaN entra como el suelo: una
    # especie sin evidencia no se lleva cupo, pero tampoco rompe el reparto.
    pesos = {
        sp.species_id: (sp.shared_fitness if sp.shared_fitness == sp.shared_fitness else 0.0)
        for sp in species
    }
    suelo = min(pesos.values())
    desplazados = {k: v - suelo + 1e-6 for k, v in pesos.items()}
    suma = sum(desplazados.values()) or 1.0

    cuotas = {sp.species_id: 0 for sp in species}
    orden = sorted(species, key=lambda sp: (-sp.shared_fitness, sp.species_id))

    restantes = total_births
    for sp in sorted(con_derecho, key=lambda sp: (-sp.shared_fitness, sp.species_id)):
        if restantes == 0:
            break
        cuotas[sp.species_id] = 1
        restantes -= 1

    if restantes:
        reparto = {
            sp.species_id: desplazados[sp.species_id] / suma * restantes for sp in species
        }
        for sp in species:
            entero = int(reparto[sp.species_id])
            cuotas[sp.species_id] += entero
            restantes -= entero
        for sp in sorted(
            species, key=lambda s: (-(reparto[s.species_id] % 1.0), s.species_id)
        ):
            if restantes <= 0:
                break
            cuotas[sp.species_id] += 1
            restantes -= 1
    if restantes > 0 and orden:
        cuotas[orden[0].species_id] += restantes

    for sp in species:
        sp.breeding_quota = cuotas[sp.species_id]
    return cuotas


def detect_clones(
    genomes: Mapping[BotId, Genome], cfg: Config, catalog: GeneCatalog
) -> list[tuple[BotId, BotId]]:
    """Pares por debajo de ``clone_threshold``.

    Primero por hash de genoma (barato y exacto), después por distancia (caro,
    pero pilla los casi-clones). Cada par sale ordenado: el segundo es el que la
    poda mata, y el llamador decide cuál de los dos es con el fitness delante.
    """
    ids = sorted(genomes)
    weights = cfg.speciation.distance_weights
    umbral = cfg.speciation.clone_threshold

    pares: list[tuple[BotId, BotId]] = []
    vistos: set[tuple[BotId, BotId]] = set()

    por_hash: dict[str, list[BotId]] = {}
    for bot in ids:
        por_hash.setdefault(genome_hash(genomes[bot]), []).append(bot)
    for grupo in por_hash.values():
        for i, a in enumerate(grupo):
            for b in grupo[i + 1 :]:
                pares.append((a, b))
                vistos.add((a, b))

    for i, a in enumerate(ids):
        for b in ids[i + 1 :]:
            if (a, b) in vistos:
                continue
            if genome_distance(genomes[a], genomes[b], weights, catalog, members=genomes) < umbral:
                pares.append((a, b))
                vistos.add((a, b))
    return pares


def family_shares(genomes: Mapping[BotId, Genome]) -> dict[IdeaFamily, float]:
    """Reparto de la población por familia de ideas.

    Si alguna supera ``cfg.evolution.max_family_share``, sus nacimientos se
    bloquean esa generación.
    """
    if not genomes:
        return {}
    cuenta = Counter(g.family for g in genomes.values())
    total = float(len(genomes))
    return {familia: n / total for familia, n in cuenta.items()}


def genetic_diversity(
    genomes: Mapping[BotId, Genome], cfg: Config, catalog: GeneCatalog
) -> float:
    """Distancia genética media entre pares de la población viva."""
    from ..genome.distance import mean_pairwise_distance

    return mean_pairwise_distance(
        [genomes[b] for b in sorted(genomes)], cfg.speciation.distance_weights, catalog,
        members=genomes,
    )


__all__ = (
    "Species",
    "new_species_id",
    "speciate",
    "shared_fitness",
    "assign_quotas",
    "detect_clones",
    "family_shares",
    "genetic_diversity",
)
