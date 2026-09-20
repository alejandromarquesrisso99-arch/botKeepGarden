"""Cruce por bloques entre dos padres.

Los bloques son: ``features``, ``entry_long``, ``exit_long``, ``entry_short``,
``exit_short``, ``risk``, ``regime``. Para cada uno se elige el del padre A o el
del B, sesgado hacia el de mejor fitness.

Cruzar por bits no tiene sentido aquí: un árbol de reglas partido por la mitad
no es un árbol. Por bloques, en cambio, el hijo hereda ideas completas.

CONTRATO — implementar en el hito 4.
"""

from __future__ import annotations

import random

from ..config import Config
from ..genome.catalog import GeneCatalog
from ..genome.schema import Genome

BLOCKS: tuple[str, ...] = (
    "features",
    "entry_long",
    "exit_long",
    "entry_short",
    "exit_short",
    "risk",
    "regime",
)


def crossover(
    parent_a: Genome,
    parent_b: Genome,
    cfg: Config,
    catalog: GeneCatalog,
    rng: random.Random,
    *,
    a_is_better: bool = True,
) -> Genome:
    """Cruza dos padres y devuelve un hijo válido.

    1. Comprobar que ``distance(a, b) > cfg.evolution.crossover_min_distance``.
       Cruzar dos casi-clones gasta un nacimiento sin explorar nada.
    2. Para cada bloque, elegir A o B con probabilidad
       ``fitness_bias_to_better_parent`` a favor del mejor.
    3. **Reparar** (este es el 80 % del trabajo real):
       * arrastrar al hijo los features que las reglas heredadas referencian,
       * renombrar features con id duplicado y distinta definición, reescribiendo
         sus referencias,
       * podar features huérfanos,
       * recortar a ``max_features``, ``max_rule_leaves`` y ``max_rule_depth``.
    4. La familia del hijo: la común si ambos padres coinciden; ``HYBRID`` si
       no, guardando las familias de origen en ``meta.parent_families`` para
       poder rastrear qué mezclas funcionan.
    5. Validar. Si el hijo no es reparable, devolver ``None`` y que el llamador
       reintente con otra pareja.

    Hay un test de fuzz sobre 5.000 cruces: ninguno puede salir con referencias
    rotas.
    """
    raise NotImplementedError


def pick_pair(
    candidates: list[tuple[Genome, float]],
    cfg: Config,
    catalog: GeneCatalog,
    rng: random.Random,
    *,
    pedigree: object | None = None,
) -> tuple[Genome, Genome] | None:
    """Elige dos padres compatibles.

    Filtra por distancia genética mínima y, si se pasa el pedigrí, por
    coeficiente de endogamia (``lineage.Pedigree.inbreeding_coefficient``): la
    endogamia colapsa la diversidad incluso con especiación activa, porque los
    hermanos se cruzan una generación tras otra.
    """
    raise NotImplementedError


__all__ = ("BLOCKS", "crossover", "pick_pair")
