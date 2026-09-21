"""Cruce por bloques: reparación de referencias y fuzz sobre 5.000 cruces."""

from __future__ import annotations

import dataclasses
import random

import pytest

from keepgarden.evolution.crossover import BLOCKS, crossover, pick_pair
from keepgarden.evolution.lineage import ParentEdge, Pedigree
from keepgarden.genome.distance import genome_distance
from keepgarden.genome.random_genome import random_population
from keepgarden.genome.schema import MarketSpec, genome_hash
from keepgarden.genome.validate import validate_genome
from keepgarden.ids import bot_id_of
from keepgarden.types import BreedOperator, IdeaFamily


@pytest.fixture(scope="module")
def poblacion(cfg, catalog):
    return random_population(40, MarketSpec(), cfg, catalog, random.Random(20260921))


def test_los_bloques_son_los_de_la_doc() -> None:
    assert BLOCKS == (
        "features", "entry_long", "exit_long", "entry_short", "exit_short",
        "risk", "regime",
    )


def test_ningun_cruce_produce_referencias_rotas(cfg, catalog, poblacion) -> None:
    """Fuzz sobre 5.000 cruces: es el criterio de aceptación del hito 4.

    Las referencias rotas son el fallo natural del cruce por bloques —una regla
    de A que nombra un feature que se quedó en A— y el sitio donde un genoma
    inválido se colaría hasta el motor.
    """
    rng = random.Random(7)
    hijos = 0
    for _ in range(5_000):
        a, b = rng.sample(poblacion, 2)
        hijo = crossover(a, b, cfg, catalog, rng, a_is_better=rng.random() < 0.5)
        if hijo is None:
            continue
        hijos += 1
        conocidos = hijo.feature_ids()
        faltan = hijo.all_referenced_features() - conocidos
        assert not faltan, f"{hijo.id} referencia features inexistentes: {faltan}"
        validate_genome(hijo, cfg, catalog, strict=True)
    assert hijos > 4_000, f"sólo {hijos} de 5.000 cruces dieron hijo"


def test_el_hijo_respeta_los_limites_de_forma(cfg, catalog, poblacion) -> None:
    rng = random.Random(11)
    for _ in range(400):
        a, b = rng.sample(poblacion, 2)
        hijo = crossover(a, b, cfg, catalog, rng)
        if hijo is None:
            continue
        assert len(hijo.features) <= cfg.evolution.max_features
        assert hijo.max_rule_depth() <= cfg.evolution.max_rule_depth
        for _, tree in hijo.rule_trees():
            from keepgarden.genome.schema import rule_leaves

            assert len(rule_leaves(tree)) <= cfg.evolution.max_rule_leaves


def test_no_se_cruzan_dos_casi_clones(cfg, catalog, poblacion) -> None:
    """Cruzar dos casi-clones gasta un nacimiento sin explorar nada."""
    a = poblacion[0]
    gemelo = dataclasses.replace(a, id="gen_gemelo")
    assert crossover(a, gemelo, cfg, catalog, random.Random(1)) is None


def test_padres_de_la_misma_familia_dan_hijo_de_esa_familia(cfg, catalog) -> None:
    rng = random.Random(3)
    trend = [
        g
        for g in random_population(30, MarketSpec(), cfg, catalog, rng)
        if g.family is IdeaFamily.TREND
    ]
    assert len(trend) >= 2
    hijo = crossover(trend[0], trend[1], cfg, catalog, rng)
    if hijo is not None:
        assert hijo.family is IdeaFamily.TREND


def test_padres_de_familias_distintas_dan_un_hibrido(cfg, catalog, poblacion) -> None:
    """Y se guarda de dónde viene cada mitad, para rastrear qué mezclas
    funcionan."""
    rng = random.Random(5)
    for _ in range(200):
        a, b = rng.sample(poblacion, 2)
        if a.family is b.family:
            continue
        hijo = crossover(a, b, cfg, catalog, rng)
        if hijo is None:
            continue
        assert hijo.family is IdeaFamily.HYBRID
        assert set(hijo.meta.parent_families) == {a.family, b.family}
        return
    pytest.fail("no se ha conseguido ningún cruce entre familias distintas")


def test_el_hijo_registra_a_sus_dos_padres(cfg, catalog, poblacion) -> None:
    rng = random.Random(13)
    a, b = poblacion[0], poblacion[1]
    hijo = crossover(a, b, cfg, catalog, rng)
    assert hijo is not None
    assert hijo.meta.operator is BreedOperator.CROSSOVER
    assert hijo.meta.parents == (a.id, b.id)


def test_los_padres_no_se_tocan(cfg, catalog, poblacion) -> None:
    rng = random.Random(17)
    a, b = poblacion[2], poblacion[3]
    antes_a, antes_b = genome_hash(a), genome_hash(b)
    crossover(a, b, cfg, catalog, rng)
    assert genome_hash(a) == antes_a
    assert genome_hash(b) == antes_b


def test_dos_features_homonimos_y_distintos_se_desdoblan(cfg, catalog) -> None:
    """Si A y B llaman ``ema_1`` a medias de periodos distintos y el hijo hereda
    reglas de los dos, no puede quedarse con una sola: perdería la mitad de una
    de las dos ideas sin avisar."""
    rng = random.Random(23)
    poblacion = random_population(60, MarketSpec(), cfg, catalog, rng)
    for _ in range(600):
        a, b = rng.sample(poblacion, 2)
        comunes = a.feature_ids() & b.feature_ids()
        distintos = [
            fid
            for fid in comunes
            if a.feature(fid).params != b.feature(fid).params
        ]
        if not distintos:
            continue
        hijo = crossover(a, b, cfg, catalog, rng)
        if hijo is None:
            continue
        # Ningún id puede aparecer dos veces, con la definición que sea.
        ids = [f.id for f in hijo.features]
        assert len(ids) == len(set(ids))
        validate_genome(hijo, cfg, catalog, strict=True)
    # El test vale por no haber reventado en ninguno de los casos anteriores.


def test_el_cruce_es_determinista(cfg, catalog, poblacion) -> None:
    a, b = poblacion[4], poblacion[5]
    uno = crossover(a, b, cfg, catalog, random.Random(99))
    otro = crossover(a, b, cfg, catalog, random.Random(99))
    assert uno is not None and otro is not None
    assert uno.to_dict() == otro.to_dict()


# --------------------------------------------------------------------------- #
# Elección de pareja                                                           #
# --------------------------------------------------------------------------- #


def test_pick_pair_devuelve_el_mejor_primero(cfg, catalog, poblacion) -> None:
    candidatos = [(g, float(i)) for i, g in enumerate(poblacion[:10])]
    pareja = pick_pair(candidatos, cfg, catalog, random.Random(2))
    assert pareja is not None
    a, b = pareja
    fitness = dict((g.id, f) for g, f in candidatos)
    assert fitness[a.id] >= fitness[b.id]


def test_pick_pair_respeta_la_distancia_minima(cfg, catalog, poblacion) -> None:
    rng = random.Random(4)
    for _ in range(50):
        pareja = pick_pair(
            [(g, 1.0) for g in poblacion], cfg, catalog, rng
        )
        assert pareja is not None
        a, b = pareja
        d = genome_distance(a, b, cfg.speciation.distance_weights, catalog)
        assert d > cfg.evolution.crossover_min_distance


def test_pick_pair_con_un_solo_candidato(cfg, catalog, poblacion) -> None:
    assert pick_pair([(poblacion[0], 1.0)], cfg, catalog, random.Random(1)) is None
    assert pick_pair([], cfg, catalog, random.Random(1)) is None


def test_pick_pair_evita_la_endogamia(cfg, catalog, poblacion) -> None:
    """Los hermanos cruzándose una generación tras otra colapsan la diversidad
    incluso con la especiación activa."""
    hermanos = poblacion[:6]
    padre = "bot_ancestro"
    ped = Pedigree.from_edges(
        [
            ParentEdge(padre, bot_id_of(g.id), BreedOperator.MUTATE)
            for g in hermanos
        ]
    )
    candidatos = [(g, 1.0) for g in hermanos]
    assert pick_pair(candidatos, cfg, catalog, random.Random(8), pedigree=ped) is None

    # Con un forastero en la lista, la pareja sale y lo incluye.
    forastero = poblacion[10]
    pareja = pick_pair(
        [*candidatos, (forastero, 2.0)], cfg, catalog, random.Random(8), pedigree=ped
    )
    assert pareja is not None
    assert forastero.id in {pareja[0].id, pareja[1].id}
