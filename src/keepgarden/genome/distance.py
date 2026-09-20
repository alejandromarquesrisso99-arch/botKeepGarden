"""Distancia genética entre genomas.

Es la base de la especiación, de la detección de clones y del scatter del
dashboard. Ver docs/GENOME.md §Distancia genética.

CONTRATO — implementar en el hito 4.
"""

from __future__ import annotations

from typing import Mapping, Sequence

from .catalog import GeneCatalog
from .schema import Genome


def genome_distance(
    a: Genome,
    b: Genome,
    weights: Mapping[str, float],
    catalog: GeneCatalog,
) -> float:
    """Distancia en [0, 1]. 0 = mismo organismo, 1 = nada en común.

    Combinación ponderada de cuatro términos (los pesos vienen de
    ``cfg.speciation.distance_weights`` y suman 1):

    * ``features``: Jaccard sobre el conjunto de ``(kind, params discretizados
      con ParamSpec.binned)``. Discretizar es esencial: EMA(21) y EMA(22) son el
      mismo gen a efectos evolutivos.
    * ``rules``: Jaccard sobre las hojas normalizadas de todos los árboles. Una
      hoja se normaliza a ``(op, kind_del_feature_izq, kind_o_const_der)``,
      ignorando los ids locales, que son arbitrarios.
    * ``risk``: distancia euclídea normalizada entre los genes de riesgo, con
      cada campo escalado a [0, 1] por su rango válido.
    * ``family``: 0 si coinciden, 1 si no.

    Para ensembles: la distancia se calcula sobre el conjunto de miembros
    (Jaccard de ``members``) combinado con la distancia media entre los genomas
    de los miembros. Un ensemble y un bot simple siempre distan al menos 0.5.

    Debe ser simétrica y cumplir ``distance(a, a) == 0``. Hay tests.
    """
    raise NotImplementedError


def distance_matrix(
    genomes: Sequence[Genome],
    weights: Mapping[str, float],
    catalog: GeneCatalog,
) -> list[list[float]]:
    """Matriz simétrica de distancias. Calcula sólo el triángulo superior."""
    raise NotImplementedError


def mean_pairwise_distance(
    genomes: Sequence[Genome],
    weights: Mapping[str, float],
    catalog: GeneCatalog,
) -> float:
    """Diversidad genética del jardín: la media del triángulo superior.

    Es la métrica de alerta temprana más importante del sistema: si cae de forma
    sostenida, el jardín está convergiendo y va a dejar de descubrir.
    """
    raise NotImplementedError


def nearest_neighbours(
    target: Genome,
    population: Sequence[Genome],
    weights: Mapping[str, float],
    catalog: GeneCatalog,
    k: int = 10,
) -> list[tuple[str, float]]:
    """Los ``k`` genomas vivos más cercanos. Alimenta la métrica ``novelty``."""
    raise NotImplementedError


__all__ = (
    "genome_distance",
    "distance_matrix",
    "mean_pairwise_distance",
    "nearest_neighbours",
)
