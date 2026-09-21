"""Distancia genética: simetría, rejilla de parámetros y ensembles."""

from __future__ import annotations

import dataclasses
import random

import pytest

from keepgarden.genome.distance import (
    ENSEMBLE_FLOOR,
    distance_matrix,
    genome_distance,
    mean_pairwise_distance,
    nearest_neighbours,
    novelty,
)
from keepgarden.genome.random_genome import random_genome, random_population
from keepgarden.genome.schema import (
    Condition,
    EnsembleGene,
    FeatureGene,
    Genome,
    MarketSpec,
    Operand,
)
from keepgarden.types import CombineMode, CompareOp, IdeaFamily, PriceField


@pytest.fixture
def weights(cfg) -> dict[str, float]:
    return cfg.speciation.distance_weights


def test_un_genoma_dista_cero_de_si_mismo(trend_genome, weights, catalog) -> None:
    assert genome_distance(trend_genome, trend_genome, weights, catalog) == 0.0


def test_una_copia_con_otro_id_tambien_dista_cero(trend_genome, weights, catalog) -> None:
    """La identidad no es el id: dos genomas con la misma estructura son el
    mismo organismo, y de eso depende la detección de clones."""
    copia = dataclasses.replace(trend_genome, id="otro_id")
    assert genome_distance(trend_genome, copia, weights, catalog) == pytest.approx(0.0)


def test_es_simetrica(trend_genome, breakout_genome, weights, catalog) -> None:
    ida = genome_distance(trend_genome, breakout_genome, weights, catalog)
    vuelta = genome_distance(breakout_genome, trend_genome, weights, catalog)
    assert ida == pytest.approx(vuelta)


def test_esta_en_el_intervalo_unidad(weights, catalog, cfg) -> None:
    rng = random.Random(7)
    poblacion = random_population(30, MarketSpec(), cfg, catalog, rng)
    for fila in distance_matrix(poblacion, weights, catalog):
        assert all(0.0 <= d <= 1.0 for d in fila)


def test_familias_distintas_pesan(trend_genome, weights, catalog) -> None:
    otra = dataclasses.replace(trend_genome, id="x", family=IdeaFamily.MOMENTUM)
    d = genome_distance(trend_genome, otra, weights, catalog)
    assert d == pytest.approx(weights["family"])


def test_la_rejilla_iguala_parametros_vecinos(trend_genome, weights, catalog) -> None:
    """EMA(120) y EMA(122) son el mismo gen: el bin de period es 5."""
    vecino = dataclasses.replace(
        trend_genome,
        id="vecino",
        features=tuple(
            dataclasses.replace(f, params={**f.params, "period": 122})
            if f.id == "ema_slow"
            else f
            for f in trend_genome.features
        ),
    )
    assert genome_distance(trend_genome, vecino, weights, catalog) == pytest.approx(0.0)


def test_dos_variaciones_de_la_misma_idea_caben_en_una_especie(
    trend_genome, weights, catalog, cfg
) -> None:
    """Es la razón de ser de la especiación: impedir que el jardín entero acabe
    siendo variaciones de la misma EMA. Si dos cruces de medias con periodos
    distintos fuesen especies separadas, el mecanismo no serviría de nada."""
    variacion = dataclasses.replace(
        trend_genome,
        id="variacion",
        features=tuple(
            dataclasses.replace(f, params={**f.params, "period": f.params["period"] * 1.7})
            if "period" in f.params
            else f
            for f in trend_genome.features
        ),
    )
    d = genome_distance(trend_genome, variacion, weights, catalog)
    assert d > cfg.speciation.clone_threshold, "tampoco son el mismo organismo"
    assert d < cfg.speciation.species_threshold


def test_mover_una_constante_de_regla_se_nota(trend_genome, weights, catalog) -> None:
    """``TWEAK_THRESHOLD`` es una de cada cinco mutaciones. Si la distancia no
    la viera, todos sus hijos morirían como clones sin haberlos mirado."""
    movido = dataclasses.replace(
        trend_genome,
        id="movido",
        entry_long=dataclasses.replace(
            trend_genome.entry_long,
            children=(
                trend_genome.entry_long.children[0],
                Condition(CompareOp.GT, Operand(ref="adx"), Operand(const=35.0)),
            ),
        ),
    )
    assert genome_distance(trend_genome, movido, weights, catalog) > 0.0


def test_un_periodo_lejano_si_separa(trend_genome, weights, catalog) -> None:
    lejano = dataclasses.replace(
        trend_genome,
        id="lejano",
        features=tuple(
            dataclasses.replace(f, params={**f.params, "period": 400})
            if f.id == "ema_slow"
            else f
            for f in trend_genome.features
        ),
    )
    assert genome_distance(trend_genome, lejano, weights, catalog) > 0.0


def test_las_reglas_ignoran_los_ids_locales(trend_genome, weights, catalog) -> None:
    """Renombrar ``ema_fast`` a ``rapida`` no cambia la idea del bot."""
    renombres = {"ema_fast": "rapida", "ema_slow": "lenta"}

    def renombra_operando(o):
        if o is not None and o.ref in renombres:
            return Operand(ref=renombres[o.ref])
        return o

    def renombra(nodo):
        if isinstance(nodo, Condition):
            return dataclasses.replace(
                nodo,
                left=renombra_operando(nodo.left),
                right=renombra_operando(nodo.right),
                right2=renombra_operando(nodo.right2),
            )
        return dataclasses.replace(nodo, children=tuple(renombra(c) for c in nodo.children))

    gemelo = dataclasses.replace(
        trend_genome,
        id="gemelo",
        features=tuple(
            dataclasses.replace(f, id=renombres.get(f.id, f.id)) for f in trend_genome.features
        ),
        entry_long=renombra(trend_genome.entry_long),
        exit_long=renombra(trend_genome.exit_long),
    )
    assert genome_distance(trend_genome, gemelo, weights, catalog) == pytest.approx(0.0)


def test_el_riesgo_separa(trend_genome, weights, catalog) -> None:
    agresivo = dataclasses.replace(
        trend_genome,
        id="agresivo",
        risk=dataclasses.replace(trend_genome.risk, risk_per_trade=0.05, max_holding_bars=10),
    )
    d = genome_distance(trend_genome, agresivo, weights, catalog)
    assert 0.0 < d <= weights["risk"]


def _ensemble(bot_ids: tuple[str, ...], genome_id: str) -> Genome:
    return Genome(
        id=genome_id,
        family=IdeaFamily.HYBRID,
        market=MarketSpec(),
        ensemble=EnsembleGene(
            members=bot_ids,
            weights=tuple(1.0 for _ in bot_ids),
            combine=CombineMode.WEIGHTED,
            threshold=0.55,
        ),
    )


def test_un_ensemble_y_un_bot_simple_distan_al_menos_el_suelo(
    trend_genome, weights, catalog
) -> None:
    fusion = _ensemble(("bot_a", "bot_b"), "fus_1")
    d = genome_distance(trend_genome, fusion, weights, catalog)
    assert d >= ENSEMBLE_FLOOR
    assert genome_distance(fusion, trend_genome, weights, catalog) == pytest.approx(d)


def test_dos_ensembles_iguales_distan_cero(weights, catalog) -> None:
    a = _ensemble(("bot_a", "bot_b"), "fus_1")
    b = _ensemble(("bot_b", "bot_a"), "fus_2")
    assert genome_distance(a, b, weights, catalog) == pytest.approx(0.0)


def test_dos_ensembles_sin_miembros_comunes_distan(weights, catalog) -> None:
    a = _ensemble(("bot_a", "bot_b"), "fus_1")
    b = _ensemble(("bot_c", "bot_d"), "fus_2")
    assert genome_distance(a, b, weights, catalog) > 0.5


def test_la_diversidad_de_una_poblacion_sembrada_es_alta(cfg, catalog) -> None:
    """Criterio de aceptación del hito 4: diversidad genética > 0.4."""
    rng = random.Random(cfg.seed)
    poblacion = random_population(60, MarketSpec(), cfg, catalog, rng)
    assert len(poblacion) == 60
    diversidad = mean_pairwise_distance(poblacion, cfg.speciation.distance_weights, catalog)
    assert diversidad > 0.4


def test_la_diversidad_de_una_poblacion_clonada_es_cero(trend_genome, weights, catalog) -> None:
    clones = [dataclasses.replace(trend_genome, id=f"c{i}") for i in range(5)]
    assert mean_pairwise_distance(clones, weights, catalog) == pytest.approx(0.0)


def test_diversidad_de_uno_o_ninguno(trend_genome, weights, catalog) -> None:
    assert mean_pairwise_distance([], weights, catalog) == 0.0
    assert mean_pairwise_distance([trend_genome], weights, catalog) == 0.0


def test_los_vecinos_vienen_ordenados_y_sin_el_propio(cfg, catalog) -> None:
    rng = random.Random(11)
    poblacion = random_population(15, MarketSpec(), cfg, catalog, rng)
    objetivo = poblacion[0]
    vecinos = nearest_neighbours(
        objetivo, poblacion, cfg.speciation.distance_weights, catalog, k=5
    )
    assert len(vecinos) == 5
    assert objetivo.id not in {bot for bot, _ in vecinos}
    assert [d for _, d in vecinos] == sorted(d for _, d in vecinos)


def test_la_novedad_de_un_clon_rodeado_de_clones_es_cero(trend_genome, weights, catalog) -> None:
    clones = [dataclasses.replace(trend_genome, id=f"c{i}") for i in range(5)]
    assert novelty(clones[0], clones, weights, catalog) == pytest.approx(0.0)


def test_el_raro_es_mas_novedoso_que_el_gris(cfg, catalog, trend_genome) -> None:
    weights = cfg.speciation.distance_weights
    grises = [dataclasses.replace(trend_genome, id=f"gris{i}") for i in range(8)]
    raro = random_genome(IdeaFamily.MICROSTRUCTURE, MarketSpec(), cfg, catalog, random.Random(3))
    poblacion = [*grises, raro]
    assert novelty(raro, poblacion, weights, catalog, k=4) > novelty(
        grises[0], poblacion, weights, catalog, k=4
    )


def test_la_matriz_es_simetrica_y_con_diagonal_cero(cfg, catalog) -> None:
    rng = random.Random(5)
    poblacion = random_population(12, MarketSpec(), cfg, catalog, rng)
    matriz = distance_matrix(poblacion, cfg.speciation.distance_weights, catalog)
    n = len(poblacion)
    for i in range(n):
        assert matriz[i][i] == 0.0
        for j in range(n):
            assert matriz[i][j] == pytest.approx(matriz[j][i])


def test_un_feature_de_mas_separa_poco_pero_separa(trend_genome, weights, catalog) -> None:
    con_extra = dataclasses.replace(
        trend_genome,
        id="extra",
        features=(
            *trend_genome.features,
            FeatureGene("rsi_x", "RSI", {"period": 14}, PriceField.CLOSE),
        ),
        exit_long=Condition(
            CompareOp.LT, Operand(ref="rsi_x"), Operand(const=30.0)
        ),
    )
    d = genome_distance(trend_genome, con_extra, weights, catalog)
    assert 0.0 < d < 0.6
