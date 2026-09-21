"""Mutación: variación puntual sobre un padre que sigue intacto."""

from __future__ import annotations

import random
import statistics

import pytest

from keepgarden.evolution.mutation import (
    MUTATION_WEIGHTS,
    adaptive_rate_factor,
    mutate,
)
from keepgarden.genome.distance import genome_distance
from keepgarden.genome.random_genome import random_population
from keepgarden.genome.schema import MarketSpec, genome_hash, rule_leaves
from keepgarden.genome.validate import validate_genome
from keepgarden.types import BreedOperator, MutationKind


@pytest.fixture(scope="module")
def poblacion(cfg, catalog):
    return random_population(20, MarketSpec(), cfg, catalog, random.Random(20260921))


def test_la_tabla_de_mutaciones_esta_completa() -> None:
    assert set(MUTATION_WEIGHTS) == set(MutationKind)
    assert MUTATION_WEIGHTS[MutationKind.TWEAK_PARAM] == pytest.approx(0.35)


def test_el_hijo_siempre_es_valido(cfg, catalog, poblacion) -> None:
    rng = random.Random(3)
    for i in range(600):
        padre = poblacion[i % len(poblacion)]
        hijo = mutate(padre, cfg, catalog, rng)
        validate_genome(hijo, cfg, catalog, strict=True)
        assert not (hijo.all_referenced_features() - hijo.feature_ids())


def test_el_padre_no_se_toca(cfg, catalog, poblacion) -> None:
    """Un bot nunca muta en sitio: si lo hiciera, la genealogía dejaría de
    significar nada."""
    padre = poblacion[0]
    antes = genome_hash(padre)
    dict_antes = padre.to_dict()
    mutate(padre, cfg, catalog, random.Random(5))
    assert genome_hash(padre) == antes
    assert padre.to_dict() == dict_antes


def test_el_hijo_tiene_identidad_y_pedigri_propios(cfg, catalog, poblacion) -> None:
    padre = poblacion[1]
    hijo = mutate(padre, cfg, catalog, random.Random(7))
    assert hijo.id != padre.id
    assert hijo.meta.operator is BreedOperator.MUTATE
    assert hijo.meta.parents == (padre.id,)
    assert hijo.meta.root_lineage == padre.meta.root_lineage


def test_el_hijo_no_es_nunca_identico_al_padre(cfg, catalog, poblacion) -> None:
    rng = random.Random(11)
    for i in range(300):
        padre = poblacion[i % len(poblacion)]
        hijo = mutate(padre, cfg, catalog, rng)
        assert hijo.to_dict(include_meta=False) != padre.to_dict(include_meta=False)


def test_el_hijo_casi_nunca_es_un_casi_clon(cfg, catalog, poblacion) -> None:
    """Una sola mutación puntual cae por debajo de ``clone_threshold`` y la
    incubadora la rechazaría sin llegar a probarla. Ver DECISIONS D-020."""
    rng = random.Random(13)
    w = cfg.speciation.distance_weights
    distancias = []
    for i in range(300):
        padre = poblacion[i % len(poblacion)]
        hijo = mutate(padre, cfg, catalog, rng)
        distancias.append(genome_distance(padre, hijo, w, catalog))
    clones = sum(1 for d in distancias if d < cfg.speciation.clone_threshold)
    assert clones / len(distancias) < 0.15
    assert statistics.median(distancias) > cfg.speciation.clone_threshold


def test_el_hijo_respeta_los_limites_de_forma(cfg, catalog, poblacion) -> None:
    rng = random.Random(17)
    for i in range(300):
        hijo = mutate(poblacion[i % len(poblacion)], cfg, catalog, rng)
        assert len(hijo.features) <= cfg.evolution.max_features
        assert hijo.max_rule_depth() <= cfg.evolution.max_rule_depth
        for _, tree in hijo.rule_trees():
            assert len(rule_leaves(tree)) <= cfg.evolution.max_rule_leaves
        assert (
            cfg.risk.min_risk_per_trade
            <= hijo.risk.risk_per_trade
            <= cfg.risk.max_risk_per_trade
        )


def test_el_hijo_conserva_entrada_y_salida(cfg, catalog, poblacion) -> None:
    rng = random.Random(19)
    for i in range(200):
        hijo = mutate(poblacion[i % len(poblacion)], cfg, catalog, rng)
        assert hijo.entry_long is not None
        assert hijo.exit_long is not None


def test_la_mutacion_es_determinista(cfg, catalog, poblacion) -> None:
    padre = poblacion[2]
    uno = mutate(padre, cfg, catalog, random.Random(23))
    otro = mutate(padre, cfg, catalog, random.Random(23))
    assert uno.to_dict() == otro.to_dict()


def test_mas_rate_factor_mueve_mas(cfg, catalog, poblacion) -> None:
    w = cfg.speciation.distance_weights
    suave, fuerte = [], []
    for i in range(120):
        padre = poblacion[i % len(poblacion)]
        suave.append(
            genome_distance(padre, mutate(padre, cfg, catalog, random.Random(i), rate_factor=0.5), w, catalog)
        )
        fuerte.append(
            genome_distance(padre, mutate(padre, cfg, catalog, random.Random(i), rate_factor=3.0), w, catalog)
        )
    assert statistics.fmean(fuerte) > statistics.fmean(suave)


def test_n_mutations_explicito_se_respeta(cfg, catalog, poblacion) -> None:
    hijo = mutate(poblacion[0], cfg, catalog, random.Random(29), n_mutations=1)
    validate_genome(hijo, cfg, catalog, strict=True)


def test_en_spot_nadie_enciende_los_cortos(cfg, catalog, poblacion) -> None:
    """Ver docs/DECISIONS.md D-001: en spot no hay cortos que valgan."""
    rng = random.Random(31)
    for i in range(400):
        hijo = mutate(poblacion[i % len(poblacion)], cfg, catalog, rng)
        assert not hijo.risk.allow_short


# --------------------------------------------------------------------------- #
# Tasa adaptativa                                                              #
# --------------------------------------------------------------------------- #


def test_sin_historia_la_tasa_es_la_base(cfg) -> None:
    assert adaptive_rate_factor([], cfg) == pytest.approx(1.0)
    assert adaptive_rate_factor([0.5], cfg) == pytest.approx(1.0)


def test_el_estancamiento_sube_la_tasa(cfg) -> None:
    """Convergencia prematura: el jardín ha dejado de descubrir y hay que
    agitarlo."""
    plano = [1.0] * 8
    assert adaptive_rate_factor(plano, cfg) == pytest.approx(
        cfg.evolution.mutation_rate_max_factor
    )


def test_mejorar_rapido_baja_la_tasa(cfg) -> None:
    """Ruido perpetuo: si está funcionando, no lo rompas."""
    subiendo = [0.0, 0.3, 0.7, 1.2, 1.8]
    assert adaptive_rate_factor(subiendo, cfg) == pytest.approx(
        cfg.evolution.mutation_rate_min_factor
    )


def test_la_tasa_se_queda_entre_los_limites(cfg) -> None:
    rng = random.Random(37)
    for _ in range(200):
        historia = [rng.uniform(-3, 3) for _ in range(rng.randint(2, 12))]
        factor = adaptive_rate_factor(historia, cfg)
        assert cfg.evolution.mutation_rate_min_factor <= factor
        assert factor <= cfg.evolution.mutation_rate_max_factor


def test_un_retroceso_tambien_agita(cfg) -> None:
    bajando = [2.0, 1.5, 1.0, 0.5, 0.1]
    assert adaptive_rate_factor(bajando, cfg) > 1.0
