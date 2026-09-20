"""Mutación: variación sobre un solo padre.

Tabla de mutaciones y pesos en docs/EVOLUTION.md §MUTATE.

CONTRATO — implementar en el hito 4.
"""

from __future__ import annotations

import random

from ..config import Config
from ..genome.catalog import GeneCatalog
from ..genome.schema import Genome
from ..types import MutationKind

#: Pesos por defecto. La suma no necesita ser 1: se normaliza.
MUTATION_WEIGHTS: dict[MutationKind, float] = {
    MutationKind.TWEAK_PARAM: 0.35,
    MutationKind.TWEAK_THRESHOLD: 0.20,
    MutationKind.TWEAK_RISK: 0.15,
    MutationKind.ADD_CONDITION: 0.10,
    MutationKind.DROP_CONDITION: 0.10,
    MutationKind.SWAP_INDICATOR: 0.05,
    MutationKind.TOGGLE_REGIME: 0.03,
    MutationKind.TOGGLE_SHORT: 0.02,
}


def mutate(
    parent: Genome,
    cfg: Config,
    catalog: GeneCatalog,
    rng: random.Random,
    *,
    rate_factor: float = 1.0,
    n_mutations: int | None = None,
) -> Genome:
    """Produce un hijo mutado.

    Aplica entre 1 y 3 mutaciones (``cfg.evolution.mutations_per_child``)
    elegidas por ``MUTATION_WEIGHTS``, valida y repara. El hijo es un genoma
    nuevo con id nuevo y ``meta.operator = MUTATE``: el padre no cambia. Un bot
    nunca muta en sitio, porque entonces la genealogía dejaría de significar
    nada.

    ``rate_factor`` viene de ``adaptive_rate_factor``.

    Detalles de cada mutación:

    * ``TWEAK_PARAM``: paso gaussiano con σ = 15 % del rango del ``ParamSpec``,
      recortado al rango. Mantener coherencias (en MACD, ``fast < slow``).
    * ``TWEAK_THRESHOLD``: mueve una constante dentro del ``value_range`` del
      indicador con el que se compara.
    * ``TWEAK_RISK``: uno de stop / take profit / trailing / ``risk_per_trade``,
      dentro de los límites de ``cfg.risk``.
    * ``ADD_CONDITION``: añade una hoja con sentido si no se supera
      ``max_rule_leaves``; si el feature necesario no existe, se añade también.
    * ``DROP_CONDITION``: quita una hoja si queda al menos una; poda los
      features que quedan huérfanos.
    * ``SWAP_INDICATOR``: sustituye por otro de la **misma categoría**
      (``catalog.BY_CATEGORY``) y reescribe las referencias.
    * ``TOGGLE_REGIME`` / ``TOGGLE_SHORT``: activan o desactivan.

    Si tras 5 intentos no se consigue un hijo válido y distinto del padre,
    devolver el padre con id nuevo (una mutación neutra) en vez de fallar.
    """
    raise NotImplementedError


def adaptive_rate_factor(
    median_fitness_history: list[float], cfg: Config
) -> float:
    """Tasa de mutación adaptativa.

    Si la mediana de fitness lleva ``stagnation_generations`` sin mejorar, sube
    hasta ``mutation_rate_max_factor``; si mejora rápido, baja hasta
    ``mutation_rate_min_factor``. Evita los dos fallos clásicos de estos
    sistemas: la convergencia prematura y el ruido perpetuo.
    """
    raise NotImplementedError


__all__ = ("MUTATION_WEIGHTS", "mutate", "adaptive_rate_factor")
