"""Fusión: la única comida gratis del oficio, buscada de forma explícita."""

from __future__ import annotations

import dataclasses
import random

import numpy as np
import pytest

from keepgarden.engine.backtest import run_backtest
from keepgarden.evaluation.metrics import Metrics, compute_metrics
from keepgarden.evolution.fusion import (
    combine_signals,
    fuse,
    reweight_on_death,
    select_fusion_candidates,
)
from keepgarden.genome.random_genome import random_population
from keepgarden.genome.schema import MarketSpec, genome_hash
from keepgarden.ids import bot_id_of
from keepgarden.types import BreedOperator, CombineMode, IdeaFamily


@pytest.fixture(scope="module")
def poblacion(cfg, catalog):
    return random_population(8, MarketSpec(), cfg, catalog, random.Random(20260921))


def _metrics(**kw) -> Metrics:
    base = dict(n_trades=30, sortino=1.0, calmar=1.0)
    base.update(kw)
    return Metrics(**base)


# --------------------------------------------------------------------------- #
# Combinación de señales                                                       #
# --------------------------------------------------------------------------- #


def test_vote_es_mayoria_simple() -> None:
    assert combine_signals([1, 1, 0], [1, 1, 1], "VOTE", 0.5) == 1
    assert combine_signals([1, 0, 0], [1, 1, 1], "VOTE", 0.5) == 0
    assert combine_signals([1, 1], [1, 1], "VOTE", 0.5) == 1
    assert combine_signals([1, 0], [1, 1], "VOTE", 0.5) == 0


def test_unanimous_es_muy_selectivo() -> None:
    assert combine_signals([1, 1, 1], [1, 1, 1], "UNANIMOUS", 0.5) == 1
    assert combine_signals([1, 1, 0], [1, 1, 1], "UNANIMOUS", 0.5) == 0


def test_any_es_muy_activo() -> None:
    assert combine_signals([0, 0, 1], [1, 1, 1], "ANY", 0.5) == 1
    assert combine_signals([0, 0, 0], [1, 1, 1], "ANY", 0.5) == 0


def test_weighted_pesa_contra_el_umbral() -> None:
    assert combine_signals([1, 0], [0.7, 0.3], "WEIGHTED", 0.55) == 1
    assert combine_signals([0, 1], [0.7, 0.3], "WEIGHTED", 0.55) == 0
    assert combine_signals([1, 1], [0.7, 0.3], "WEIGHTED", 0.55) == 1


def test_weighted_normaliza_los_pesos() -> None:
    assert combine_signals([1, 0], [7.0, 3.0], "WEIGHTED", 0.55) == 1


def test_combinar_sin_miembros() -> None:
    assert combine_signals([], [], "WEIGHTED", 0.5) == 0


def test_la_combinacion_coincide_con_la_del_compilador(cfg, catalog, poblacion, walk) -> None:
    """El compilador vectoriza lo mismo que ``combine_signals`` hace vela a
    vela. Si divergieran, el ensemble haría en vivo algo distinto de lo que se
    probó en la incubadora."""
    from keepgarden.genome.compile import _combine_positions

    rng = random.Random(3)
    for _ in range(50):
        n = rng.randint(2, 4)
        posiciones = [np.array([float(rng.randint(0, 1))]) for _ in range(n)]
        pesos = tuple(rng.random() for _ in range(n))
        total = sum(pesos) or 1.0
        pesos = tuple(p / total for p in pesos)
        for modo in CombineMode:
            vectorial = bool(
                _combine_positions(posiciones, pesos, modo, 0.55)[0]
            )
            suelto = bool(
                combine_signals([int(p[0]) for p in posiciones], pesos, str(modo), 0.55)
            )
            assert vectorial == suelto, modo


# --------------------------------------------------------------------------- #
# Crear la fusión                                                              #
# --------------------------------------------------------------------------- #


def test_los_pesos_salen_del_sortino(cfg, poblacion) -> None:
    miembros = poblacion[:2]
    ids = [bot_id_of(g.id) for g in miembros]
    metricas = {ids[0]: _metrics(sortino=3.0), ids[1]: _metrics(sortino=1.0)}
    hijo = fuse(miembros, metricas, cfg, random.Random(1))
    pesos = hijo.ensemble.normalized_weights()
    assert pesos[0] == pytest.approx(0.75, abs=1e-4)
    assert pesos[1] == pytest.approx(0.25, abs=1e-4)


def test_un_sortino_negativo_no_da_peso_negativo(cfg, poblacion) -> None:
    """Un miembro que no aporta debe pesar poco, no votar al revés."""
    miembros = poblacion[:2]
    ids = [bot_id_of(g.id) for g in miembros]
    metricas = {ids[0]: _metrics(sortino=2.0), ids[1]: _metrics(sortino=-5.0)}
    hijo = fuse(miembros, metricas, cfg, random.Random(1))
    assert all(w > 0 for w in hijo.ensemble.weights)
    assert sum(hijo.ensemble.normalized_weights()) == pytest.approx(1.0)


def test_el_riesgo_lo_hereda_el_de_mejor_calmar(cfg, poblacion) -> None:
    """Un ensemble hereda señales, pero gestiona su propio riesgo."""
    miembros = poblacion[:3]
    ids = [bot_id_of(g.id) for g in miembros]
    metricas = {
        ids[0]: _metrics(calmar=0.1),
        ids[1]: _metrics(calmar=5.0),
        ids[2]: _metrics(calmar=1.0),
    }
    hijo = fuse(miembros, metricas, cfg, random.Random(1))
    assert hijo.risk == miembros[1].risk


def test_la_fusion_no_tiene_reglas_propias(cfg, poblacion) -> None:
    miembros = poblacion[:2]
    metricas = {bot_id_of(g.id): _metrics() for g in miembros}
    hijo = fuse(miembros, metricas, cfg, random.Random(1))
    assert hijo.is_ensemble
    assert hijo.features == ()
    assert hijo.entry_long is None and hijo.exit_long is None
    assert hijo.meta.operator is BreedOperator.FUSION


def test_los_padres_siguen_vivos_e_intactos(cfg, poblacion) -> None:
    """La fusión no los consume: así se puede medir si aporta, corriendo hijo y
    padres en paralelo sobre los mismos datos."""
    miembros = poblacion[:2]
    antes = [genome_hash(g) for g in miembros]
    metricas = {bot_id_of(g.id): _metrics() for g in miembros}
    fuse(miembros, metricas, cfg, random.Random(1))
    assert [genome_hash(g) for g in miembros] == antes


def test_familias_distintas_dan_un_hibrido(cfg, poblacion) -> None:
    miembros = [g for g in poblacion[:4]]
    metricas = {bot_id_of(g.id): _metrics() for g in miembros}
    hijo = fuse(miembros[:2], metricas, cfg, random.Random(1))
    familias = {miembros[0].family, miembros[1].family}
    esperada = familias.pop() if len(familias) == 1 else IdeaFamily.HYBRID
    assert hijo.family is esperada


def test_la_profundidad_crece_con_los_ensembles_anidados(cfg, poblacion) -> None:
    metricas = {bot_id_of(g.id): _metrics() for g in poblacion}
    simple = fuse(poblacion[:2], metricas, cfg, random.Random(1))
    assert simple.ensemble.depth == 1

    metricas[bot_id_of(simple.id)] = _metrics()
    doble = fuse([simple, poblacion[2]], metricas, cfg, random.Random(2))
    assert doble.ensemble.depth == 2


def test_no_se_pasa_de_max_depth(cfg, poblacion) -> None:
    """Los ensembles de ensembles degeneran en promedios de todo el jardín."""
    metricas = {bot_id_of(g.id): _metrics() for g in poblacion}
    simple = fuse(poblacion[:2], metricas, cfg, random.Random(1))
    metricas[bot_id_of(simple.id)] = _metrics()
    doble = fuse([simple, poblacion[2]], metricas, cfg, random.Random(2))
    metricas[bot_id_of(doble.id)] = _metrics()
    with pytest.raises(ValueError, match="max_depth"):
        fuse([doble, poblacion[3]], metricas, cfg, random.Random(3))


def test_una_fusion_de_uno_no_es_una_fusion(cfg, poblacion) -> None:
    metricas = {bot_id_of(poblacion[0].id): _metrics()}
    with pytest.raises(ValueError):
        fuse(poblacion[:1], metricas, cfg, random.Random(1))


def test_la_fusion_es_determinista(cfg, poblacion) -> None:
    metricas = {bot_id_of(g.id): _metrics() for g in poblacion}
    uno = fuse(poblacion[:2], metricas, cfg, random.Random(9))
    otro = fuse(poblacion[:2], metricas, cfg, random.Random(9))
    assert uno.to_dict() == otro.to_dict()


# --------------------------------------------------------------------------- #
# Elegir padres                                                                #
# --------------------------------------------------------------------------- #


def test_solo_se_fusionan_los_descorrelacionados(cfg, poblacion) -> None:
    ids = [bot_id_of(g.id) for g in poblacion[:4]]
    fitness = {b: 1.0 for b in ids}
    ages = {b: 5 for b in ids}
    correlaciones = {
        (ids[0], ids[1]): -0.3,     # descorrelacionados: candidatos
        (ids[0], ids[2]): 0.95,
        (ids[0], ids[3]): 0.95,
        (ids[1], ids[2]): 0.95,
        (ids[1], ids[3]): 0.95,
        (ids[2], ids[3]): 0.98,
    }
    grupos = select_fusion_candidates(
        poblacion[:4], correlaciones, fitness, ages, cfg, random.Random(1)
    )
    assert len(grupos) == 1
    assert set(grupos[0]) == {ids[0], ids[1]}


def test_los_jovenes_no_se_fusionan(cfg, poblacion) -> None:
    """No basta con un buen backtest: hace falta historial en vivo."""
    ids = [bot_id_of(g.id) for g in poblacion[:2]]
    correlaciones = {(ids[0], ids[1]): 0.0}
    fitness = {b: 1.0 for b in ids}
    assert select_fusion_candidates(
        poblacion[:2], correlaciones, fitness, {b: 0 for b in ids}, cfg, random.Random(1)
    ) == []


def test_una_correlacion_desconocida_no_se_asume_baja(cfg, poblacion) -> None:
    """Fusionar a ciegas dos bots que podrían ser el mismo es justo lo que la
    fusión intenta evitar."""
    ids = [bot_id_of(g.id) for g in poblacion[:2]]
    grupos = select_fusion_candidates(
        poblacion[:2], {}, {b: 1.0 for b in ids}, {b: 9 for b in ids}, cfg, random.Random(1)
    )
    assert grupos == []


def test_los_grupos_no_pasan_de_max_members(cfg, catalog) -> None:
    poblacion = random_population(8, MarketSpec(), cfg, catalog, random.Random(5))
    ids = [bot_id_of(g.id) for g in poblacion]
    correlaciones = {
        (a, b): 0.0 for i, a in enumerate(ids) for b in ids[i + 1 :]
    }
    grupos = select_fusion_candidates(
        poblacion, correlaciones, {b: 1.0 for b in ids}, {b: 4 for b in ids},
        cfg, random.Random(1),
    )
    assert grupos
    for grupo in grupos:
        assert cfg.fusion.min_members <= len(grupo) <= cfg.fusion.max_members
        assert len(set(grupo)) == len(grupo)


# --------------------------------------------------------------------------- #
# Muerte de un miembro                                                         #
# --------------------------------------------------------------------------- #


def test_al_morir_un_miembro_su_peso_se_reparte(cfg, poblacion) -> None:
    miembros = poblacion[:3]
    metricas = {bot_id_of(g.id): _metrics() for g in miembros}
    ensemble = fuse(miembros, metricas, cfg, random.Random(1))
    muerto = ensemble.ensemble.members[0]

    quedan = reweight_on_death(ensemble, muerto)
    assert quedan is not None
    assert muerto not in quedan.ensemble.members
    assert len(quedan.ensemble.members) == 2
    assert sum(quedan.ensemble.weights) == pytest.approx(1.0)


def test_con_menos_de_dos_miembros_el_ensemble_se_jubila(cfg, poblacion) -> None:
    miembros = poblacion[:2]
    metricas = {bot_id_of(g.id): _metrics() for g in miembros}
    ensemble = fuse(miembros, metricas, cfg, random.Random(1))
    assert reweight_on_death(ensemble, ensemble.ensemble.members[0]) is None


def test_la_muerte_de_un_extraño_no_cambia_nada(cfg, poblacion) -> None:
    miembros = poblacion[:3]
    metricas = {bot_id_of(g.id): _metrics() for g in miembros}
    ensemble = fuse(miembros, metricas, cfg, random.Random(1))
    assert reweight_on_death(ensemble, "bot_nadie") is ensemble


# --------------------------------------------------------------------------- #
# El criterio de aceptación del hito                                           #
# --------------------------------------------------------------------------- #


def test_fusionar_dos_padres_descorrelacionados_baja_el_drawdown(
    cfg, catalog, walk
) -> None:
    """Dos estrategias mediocres pero descorrelacionadas, combinadas, producen
    una curva con menos drawdown que cualquiera de las dos. Es la única comida
    gratis que hay en esto, y es lo que este test comprueba sobre el motor de
    verdad, no sobre una fórmula.
    """
    velas = walk(4000, seed=20260921)
    rng = random.Random(20260921)
    poblacion = random_population(24, MarketSpec(), cfg, catalog, rng)

    resultados = {}
    for g in poblacion:
        r = run_backtest(g, velas, cfg)
        if len(r.trades) >= 5:
            resultados[g.id] = (g, r)
    assert len(resultados) >= 2, "la serie no hace operar a nadie: test inservible"

    retornos = {
        gid: np.diff(r.equity_curve) for gid, (_, r) in resultados.items()
    }

    # La pareja más descorrelacionada de las que además pierden poco solas.
    mejor = None
    ids = list(resultados)
    for i, a in enumerate(ids):
        for b in ids[i + 1 :]:
            if retornos[a].std() == 0 or retornos[b].std() == 0:
                continue
            c = float(np.corrcoef(retornos[a], retornos[b])[0, 1])
            if mejor is None or c < mejor[0]:
                mejor = (c, a, b)
    assert mejor is not None
    correlacion, ida, idb = mejor
    assert correlacion < 0.0, f"no hay ninguna pareja descorrelacionada (mín {correlacion:.2f})"

    ga, gb = resultados[ida][0], resultados[idb][0]
    miembros = {bot_id_of(ga.id): ga, bot_id_of(gb.id): gb}
    metricas = {bot_id_of(ga.id): _metrics(), bot_id_of(gb.id): _metrics()}
    hijo = fuse([ga, gb], metricas, cfg, random.Random(1))

    def dd(resultado) -> float:
        return compute_metrics(
            resultado.equity_curve, resultado.trades, "1h",
            total_fees=resultado.total_fees,
        ).max_drawdown

    hijo_r = run_backtest(hijo, velas, cfg, members=miembros)
    assert dd(hijo_r) < dd(resultados[ida][1])
    assert dd(hijo_r) < dd(resultados[idb][1])
