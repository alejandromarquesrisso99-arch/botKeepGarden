"""Genealogía: el pedigrí debe responder bien a fusiones y linajes cruzados."""

from __future__ import annotations

import pytest

from keepgarden.evolution.lineage import ParentEdge, Pedigree, build_graph
from keepgarden.types import BreedOperator as Op


@pytest.fixture
def ped() -> Pedigree:
    #   a ──► c ─┐
    #             ├─► e ──┐
    #   b ──► d ─┘         ├─► f   (f es una fusión de e y a)
    #                 a ───┘
    return Pedigree.from_edges(
        [
            ParentEdge("a", "c", Op.MUTATE),
            ParentEdge("b", "d", Op.MUTATE),
            ParentEdge("c", "e", Op.CROSSOVER, ordinal=0),
            ParentEdge("d", "e", Op.CROSSOVER, ordinal=1),
            ParentEdge("e", "f", Op.FUSION, weight=0.6, ordinal=0),
            ParentEdge("a", "f", Op.FUSION, weight=0.4, ordinal=1),
        ]
    )


def test_fundadores(ped: Pedigree) -> None:
    assert ped.is_founder("a")
    assert ped.is_founder("b")
    assert not ped.is_founder("c")


def test_ancestros_y_descendientes(ped: Pedigree) -> None:
    assert ped.ancestors("f") == {"a", "b", "c", "d", "e"}
    assert ped.descendants("a") == {"c", "e", "f"}
    assert ped.founders_of("f") == {"a", "b"}


def test_profundidad_evolutiva(ped: Pedigree) -> None:
    assert ped.generation_depth("a") == 0
    assert ped.generation_depth("c") == 1
    assert ped.generation_depth("e") == 2
    assert ped.generation_depth("f") == 3


def test_padres_ordenados(ped: Pedigree) -> None:
    assert ped.parents_of("f") == ["e", "a"]


def test_endogamia(ped: Pedigree) -> None:
    assert ped.inbreeding_coefficient("c", "d") == 0.0
    assert ped.inbreeding_coefficient("c", "e") > 0.0
    assert ped.inbreeding_coefficient("c", "c") == 1.0


def test_parentesco(ped: Pedigree) -> None:
    assert ped.related("c", "e")
    assert ped.related("c", "d")   # comparten descendiente y ancestro común vía f
    assert not ped.related("b", "c")


def test_nodos_de_fusion(ped: Pedigree) -> None:
    assert ped.fusion_nodes() == {"f"}


def test_mas_prolificos(ped: Pedigree) -> None:
    top = dict(ped.most_prolific(3))
    assert top["a"] == 2


def test_grafo_corta_por_generacion(ped: Pedigree) -> None:
    bots = [
        {
            "bot_id": b,
            "name": b,
            "family": "TREND",
            "root_lineage": "lin_x",
            "born_generation": i,
            "status": "ALIVE",
            "fitness_effective": 0.1 * i,
        }
        for i, b in enumerate("abcdef")
    ]
    g = build_graph(bots, ped.edges, until_generation=3)
    assert {n.id for n in g.nodes} == {"a", "b", "c", "d"}
    # ninguna arista puede apuntar fuera del corte
    ids = {n.id for n in g.nodes}
    assert all(e.source in ids and e.target in ids for e in g.edges)
