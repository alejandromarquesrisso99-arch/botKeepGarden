"""Especiación, fitness compartido, cupos y detección de clones."""

from __future__ import annotations

import dataclasses
import random

import pytest

from keepgarden.evolution.speciation import (
    Species,
    assign_quotas,
    detect_clones,
    family_shares,
    genetic_diversity,
    new_species_id,
    shared_fitness,
    speciate,
)
from keepgarden.genome.random_genome import random_population
from keepgarden.genome.schema import MarketSpec
from keepgarden.ids import bot_id_of
from keepgarden.types import IdeaFamily


@pytest.fixture
def poblacion(cfg, catalog) -> dict[str, object]:
    genomas = random_population(24, MarketSpec(), cfg, catalog, random.Random(31))
    return {bot_id_of(g.id): g for g in genomas}


def test_cada_bot_cae_en_una_y_solo_una_especie(poblacion, cfg, catalog) -> None:
    especies = speciate(poblacion, cfg, catalog)
    asignados = [m for sp in especies for m in sp.members]
    assert sorted(asignados) == sorted(poblacion)


def test_los_clones_caen_en_la_misma_especie(cfg, catalog, trend_genome) -> None:
    genomas = {
        f"bot_{i}": dataclasses.replace(trend_genome, id=f"gen_{i}") for i in range(6)
    }
    especies = speciate(genomas, cfg, catalog)
    assert len(especies) == 1
    assert especies[0].size == 6


def test_las_variaciones_de_una_idea_caen_juntas(cfg, catalog, trend_genome) -> None:
    """Es el motivo de existir de la especiación."""
    genomas = {}
    for i in range(5):
        genomas[f"bot_{i}"] = dataclasses.replace(
            trend_genome,
            id=f"gen_{i}",
            features=tuple(
                dataclasses.replace(f, params={**f.params, "period": f.params["period"] * (1 + 0.15 * i)})
                if "period" in f.params
                else f
                for f in trend_genome.features
            ),
        )
    especies = speciate(genomas, cfg, catalog)
    assert len(especies) == 1


def test_una_poblacion_sembrada_da_muchas_especies(poblacion, cfg, catalog) -> None:
    """Sembrar es sembrar diverso: si saliera una sola especie, el jardín
    arrancaría con menos exploración de la que aparenta."""
    especies = speciate(poblacion, cfg, catalog)
    assert len(especies) > len(poblacion) // 2


def test_los_representantes_sobreviven_entre_generaciones(poblacion, cfg, catalog) -> None:
    """Sin esto el dashboard mostraría un baile de colores sin significado."""
    primera = speciate(poblacion, cfg, catalog)
    segunda = speciate(poblacion, cfg, catalog, previous=primera)
    assert {sp.species_id for sp in segunda} == {sp.species_id for sp in primera}


def test_una_especie_sin_miembros_vivos_desaparece(poblacion, cfg, catalog) -> None:
    primera = speciate(poblacion, cfg, catalog)
    supervivientes = dict(list(poblacion.items())[:5])
    segunda = speciate(supervivientes, cfg, catalog, previous=primera)
    assert sum(sp.size for sp in segunda) == 5
    assert all(sp.size > 0 for sp in segunda)


def test_si_muere_el_representante_la_especie_conserva_el_id(cfg, catalog, trend_genome) -> None:
    genomas = {f"bot_{i}": dataclasses.replace(trend_genome, id=f"gen_{i}") for i in range(4)}
    primera = speciate(genomas, cfg, catalog)
    muerto = primera[0].representative
    quedan = {k: v for k, v in genomas.items() if k != muerto}
    segunda = speciate(quedan, cfg, catalog, previous=primera)
    assert [sp.species_id for sp in segunda] == [primera[0].species_id]


def test_el_id_de_especie_es_determinista() -> None:
    assert new_species_id("bot_abc") == new_species_id("bot_abc")
    assert new_species_id("bot_abc") != new_species_id("bot_abd")


def test_la_especiacion_es_determinista(poblacion, cfg, catalog) -> None:
    una = speciate(poblacion, cfg, catalog)
    otra = speciate(poblacion, cfg, catalog)
    assert [(sp.species_id, sp.members) for sp in una] == [
        (sp.species_id, sp.members) for sp in otra
    ]


# --------------------------------------------------------------------------- #
# Fitness compartido                                                           #
# --------------------------------------------------------------------------- #


def test_una_especie_grande_se_penaliza_a_si_misma() -> None:
    """Sin esto, la primera idea que funciona coloniza el jardín en tres
    generaciones y la exploración se acaba."""
    grande = Species("sp_g", "a", members=["a", "b", "c", "d"])
    pequeña = Species("sp_p", "z", members=["z"])
    fitness = {b: 1.0 for b in ["a", "b", "c", "d", "z"]}
    compartido = shared_fitness(fitness, [grande, pequeña])
    assert compartido["a"] == pytest.approx(0.25)
    assert compartido["z"] == pytest.approx(1.0)


def test_el_fitness_compartido_rellena_las_estadisticas_de_especie() -> None:
    sp = Species("sp_1", "a", members=["a", "b"])
    shared_fitness({"a": 2.0, "b": 4.0}, [sp])
    assert sp.mean_fitness == pytest.approx(3.0)
    assert sp.shared_fitness == pytest.approx(3.0)


def test_el_fitness_compartido_ignora_a_los_que_no_tienen() -> None:
    sp = Species("sp_1", "a", members=["a", "mudo"])
    compartido = shared_fitness({"a": 2.0}, [sp])
    assert "mudo" not in compartido


# --------------------------------------------------------------------------- #
# Cupos                                                                        #
# --------------------------------------------------------------------------- #


def test_los_cupos_suman_los_nacimientos_pedidos() -> None:
    especies = [
        Species("sp_a", "a", members=["a", "b"], shared_fitness=2.0),
        Species("sp_b", "c", members=["c"], shared_fitness=1.0),
        Species("sp_c", "d", members=["d"], shared_fitness=0.5),
    ]
    fitness = {"a": 2.0, "b": 1.0, "c": 1.0, "d": 0.2}
    cuotas = assign_quotas(especies, 12, fitness)
    assert sum(cuotas.values()) == 12


def test_la_especie_con_mas_fitness_compartido_se_lleva_mas() -> None:
    especies = [
        Species("sp_a", "a", members=["a"], shared_fitness=10.0),
        Species("sp_b", "b", members=["b"], shared_fitness=0.1),
    ]
    cuotas = assign_quotas(especies, 20, {"a": 5.0, "b": 0.1})
    assert cuotas["sp_a"] > cuotas["sp_b"]


def test_toda_especie_por_encima_de_la_mediana_recibe_al_menos_uno() -> None:
    especies = [
        Species(f"sp_{i}", f"b{i}", members=[f"b{i}"], shared_fitness=float(i))
        for i in range(4)
    ]
    fitness = {f"b{i}": float(i) for i in range(4)}
    cuotas = assign_quotas(especies, 10, fitness)
    mediana = 1.5
    for sp in especies:
        if fitness[sp.members[0]] > mediana:
            assert cuotas[sp.species_id] >= 1


def test_con_mas_especies_que_nacimientos_no_se_inventan_hijos() -> None:
    """Es lo normal en un jardín recién sembrado, donde casi cada bot es su
    propia especie. Repartir de menos es correcto; pasarse, no."""
    especies = [
        Species(f"sp_{i}", f"b{i}", members=[f"b{i}"], shared_fitness=float(i))
        for i in range(40)
    ]
    fitness = {f"b{i}": float(i) for i in range(40)}
    cuotas = assign_quotas(especies, 12, fitness)
    assert sum(cuotas.values()) == 12
    assert all(v >= 0 for v in cuotas.values())


def test_cupos_con_fitness_negativo() -> None:
    """El fitness es un z-score: los negativos son la mitad de la población."""
    especies = [
        Species("sp_a", "a", members=["a"], shared_fitness=-0.5),
        Species("sp_b", "b", members=["b"], shared_fitness=-2.0),
    ]
    cuotas = assign_quotas(especies, 6, {"a": -0.5, "b": -2.0})
    assert sum(cuotas.values()) == 6
    assert cuotas["sp_a"] >= cuotas["sp_b"]


def test_cero_nacimientos() -> None:
    especies = [Species("sp_a", "a", members=["a"], shared_fitness=1.0)]
    assert assign_quotas(especies, 0, {"a": 1.0}) == {"sp_a": 0}


# --------------------------------------------------------------------------- #
# Clones y familias                                                            #
# --------------------------------------------------------------------------- #


def test_dos_genomas_identicos_son_clones(cfg, catalog, trend_genome) -> None:
    genomas = {
        "bot_a": trend_genome,
        "bot_b": dataclasses.replace(trend_genome, id="gen_b"),
        "bot_c": dataclasses.replace(trend_genome, id="gen_c", family=IdeaFamily.BREAKOUT),
    }
    pares = detect_clones(genomas, cfg, catalog)
    assert ("bot_a", "bot_b") in pares


def test_una_poblacion_sembrada_no_tiene_clones(poblacion, cfg, catalog) -> None:
    assert detect_clones(poblacion, cfg, catalog) == []


def test_el_reparto_por_familias_suma_uno(poblacion) -> None:
    cuotas = family_shares(poblacion)
    assert sum(cuotas.values()) == pytest.approx(1.0)
    assert all(0.0 < v <= 1.0 for v in cuotas.values())


def test_reparto_de_familias_vacio() -> None:
    assert family_shares({}) == {}


def test_la_diversidad_de_la_poblacion_sembrada(poblacion, cfg, catalog) -> None:
    assert genetic_diversity(poblacion, cfg, catalog) > cfg.evolution.diversity_floor
