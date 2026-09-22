"""Selección de padres, poda y reparto de nacimientos."""

from __future__ import annotations

import random

import pytest

from keepgarden.evolution.selection import (
    BREED_OPERATORS,
    DRAWDOWN_CULL_FACTOR,
    MAX_TIMES_AS_PARENT,
    blocked_families,
    plan_births,
    select_deaths,
    select_parents,
)
from keepgarden.evolution.speciation import Species
from keepgarden.types import BreedOperator, DeathCause, IdeaFamily


def _poblacion(n: int) -> dict[str, float]:
    """``n`` bots con fitness de 0 a n-1."""
    return {f"b{i}": float(i) for i in range(n)}


# --------------------------------------------------------------------------- #
# Padres                                                                       #
# --------------------------------------------------------------------------- #


def test_la_elite_se_reproduce_seguro(cfg) -> None:
    fitness = _poblacion(10)
    especies = [Species("sp", "b0", members=list(fitness))]
    padres = select_parents(
        especies, fitness, {"sp": 0}, cfg, random.Random(1), elite=["b9", "b8"]
    )
    assert "b9" in padres and "b8" in padres


def test_nadie_es_padre_mas_de_tres_veces(cfg) -> None:
    """Sin tope, el jardín se llena de hijos de un solo bot en dos
    generaciones."""
    fitness = {"a": 5.0, "b": 1.0}
    especies = [Species("sp", "a", members=["a", "b"])]
    padres = select_parents(
        especies, fitness, {"sp": 20}, cfg, random.Random(1), elite=["a"] * 5
    )
    for bot in set(padres):
        assert padres.count(bot) <= MAX_TIMES_AS_PARENT


def test_el_torneo_favorece_al_mejor(cfg) -> None:
    fitness = _poblacion(12)
    especies = [Species("sp", "b0", members=list(fitness))]
    padres = select_parents(especies, fitness, {"sp": 30}, cfg, random.Random(7))
    medio = sum(fitness[p] for p in padres) / len(padres)
    assert medio > sum(fitness.values()) / len(fitness)


def test_los_indefinidos_no_son_padres_pero_no_mueren(cfg) -> None:
    """Se les da otra generación: matar a un bot por no haber operado todavía
    es matar precisamente a los selectivos."""
    fitness = {"a": 1.0, "mudo": float("nan")}
    especies = [Species("sp", "a", members=["a", "mudo"])]
    padres = select_parents(especies, fitness, {"sp": 10}, cfg, random.Random(1))
    assert "mudo" not in padres

    muertes = select_deaths(
        fitness, {"a": 9, "mudo": 9}, {}, {}, set(), set(), [], cfg
    )
    assert "mudo" not in muertes


def test_sin_cupo_y_sin_elite_no_hay_padres(cfg) -> None:
    fitness = _poblacion(5)
    especies = [Species("sp", "b0", members=list(fitness))]
    assert select_parents(especies, fitness, {"sp": 0}, cfg, random.Random(1)) == []


# --------------------------------------------------------------------------- #
# Poda                                                                         #
# --------------------------------------------------------------------------- #


def test_muere_el_peor_cull_fraction(cfg) -> None:
    fitness = _poblacion(40)
    edades = {b: 9 for b in fitness}
    muertes = select_deaths(fitness, edades, {}, {}, set(), set(), [], cfg)
    esperados = int(40 * cfg.garden.cull_fraction)
    assert len(muertes) == esperados
    assert all(muertes[b] is DeathCause.LOW_FITNESS for b in muertes)
    assert "b0" in muertes and "b39" not in muertes


def test_no_se_mata_a_un_recien_nacido_por_una_mala_semana(cfg) -> None:
    fitness = _poblacion(40)
    edades = {b: 0 for b in fitness}
    assert select_deaths(fitness, edades, {}, {}, set(), set(), [], cfg) == {}


def test_el_drawdown_no_perdona_a_nadie(cfg) -> None:
    """Ni élite, ni Pareto, ni recién nacidos."""
    fitness = _poblacion(40)
    edades = {b: 0 for b in fitness}
    drawdowns = {"b39": cfg.risk.hard_max_drawdown + 0.01}
    muertes = select_deaths(
        fitness, edades, {}, drawdowns, {"b39"}, {"b39"}, [], cfg
    )
    assert muertes["b39"] is DeathCause.DRAWDOWN_BREAKER


def test_el_ocioso_muere(cfg) -> None:
    fitness = _poblacion(40)
    edades = {b: 9 for b in fitness}
    ocio = {"b39": cfg.garden.idle_generations_limit}
    muertes = select_deaths(fitness, edades, ocio, {}, set(), set(), [], cfg)
    assert muertes["b39"] is DeathCause.IDLE


def test_el_clon_peor_muere(cfg) -> None:
    fitness = _poblacion(40)
    edades = {b: 9 for b in fitness}
    muertes = select_deaths(
        fitness, edades, {}, {}, set(), set(), [("b30", "b31")], cfg
    )
    assert muertes["b30"] is DeathCause.CLONE
    assert "b31" not in muertes


def test_el_frente_de_pareto_esta_protegido(cfg) -> None:
    """Es lo que mantiene vivos a los raros que aún no han demostrado su valor,
    y de los que suelen salir los saltos."""
    fitness = _poblacion(40)
    edades = {b: 9 for b in fitness}
    sin_proteger = select_deaths(fitness, edades, {}, {}, set(), set(), [], cfg)
    assert "b0" in sin_proteger

    con_pareto = select_deaths(fitness, edades, {}, {}, set(), {"b0"}, [], cfg)
    assert "b0" not in con_pareto


def test_los_protegidos_por_el_jardinero_sobreviven(cfg) -> None:
    fitness = _poblacion(40)
    edades = {b: 9 for b in fitness}
    muertes = select_deaths(fitness, edades, {}, {}, {"b0", "b1"}, set(), [], cfg)
    assert "b0" not in muertes and "b1" not in muertes


def test_la_poda_no_baja_de_la_poblacion_minima(cfg) -> None:
    n = cfg.garden.min_population + 2
    fitness = _poblacion(n)
    edades = {b: 9 for b in fitness}
    ocio = {b: cfg.garden.idle_generations_limit for b in fitness}
    muertes = select_deaths(fitness, edades, ocio, {}, set(), set(), [], cfg)
    assert n - len(muertes) >= cfg.garden.min_population


def test_el_suelo_de_poblacion_indulta_por_arriba(cfg) -> None:
    n = cfg.garden.min_population + 1
    fitness = _poblacion(n)
    edades = {b: 9 for b in fitness}
    ocio = {b: cfg.garden.idle_generations_limit for b in fitness}
    muertes = select_deaths(fitness, edades, ocio, {}, set(), set(), [], cfg)
    vivos = set(fitness) - set(muertes)
    mejor = max(fitness, key=lambda b: fitness[b])
    assert mejor in vivos


def test_el_drawdown_del_jardin_aprieta_la_poda(cfg) -> None:
    """El breaker del jardín de docs/EXECUTION.md §Circuit breakers.

    Cuando el ecosistema entero está perdiendo, se recorta por los dos lados:
    la mitad de nacimientos —eso ya lo hacía ``plan_births``— y el doble de
    poda, que es la mitad que faltaba.
    """
    fitness = _poblacion(60)
    edades = {b: 9 for b in fitness}
    tranquilo = select_deaths(fitness, edades, {}, {}, set(), set(), [], cfg)
    sufriendo = select_deaths(
        fitness, edades, {}, {}, set(), set(), [], cfg,
        garden_drawdown=cfg.risk.garden_max_drawdown + 0.01,
    )
    assert len(sufriendo) > len(tranquilo)
    assert len(sufriendo) == len(tranquilo) * DRAWDOWN_CULL_FACTOR


def test_la_poda_apretada_muere_por_abajo(cfg) -> None:
    """Apretar la poda mata a más, pero sigue matando a los peores."""
    fitness = _poblacion(60)
    edades = {b: 9 for b in fitness}
    muertes = select_deaths(
        fitness, edades, {}, {}, set(), set(), [], cfg,
        garden_drawdown=cfg.risk.garden_max_drawdown + 0.01,
    )
    vivos = set(fitness) - set(muertes)
    assert max(fitness[b] for b in muertes) < min(fitness[b] for b in vivos)


def test_la_poda_apretada_respeta_el_suelo_y_a_los_protegidos(cfg) -> None:
    """El breaker aprieta, no arrasa: el suelo de población y los protegidos
    mandan sobre él."""
    n = cfg.garden.min_population + 3
    fitness = _poblacion(n)
    edades = {b: 9 for b in fitness}
    muertes = select_deaths(
        fitness, edades, {}, {}, {"b0"}, set(), [], cfg,
        garden_drawdown=cfg.risk.garden_max_drawdown + 0.01,
    )
    assert n - len(muertes) >= cfg.garden.min_population
    assert "b0" not in muertes


def test_sin_drawdown_del_jardin_la_poda_es_la_de_siempre(cfg) -> None:
    """Que el parámetro exista no puede cambiar el jardín de nadie que no esté
    en drawdown: un cambio silencioso de dinámica es lo peor que puede pasar."""
    fitness = _poblacion(60)
    edades = {b: 9 for b in fitness}
    sin_parametro = select_deaths(fitness, edades, {}, {}, set(), set(), [], cfg)
    con_cero = select_deaths(
        fitness, edades, {}, {}, set(), set(), [], cfg, garden_drawdown=0.0
    )
    justo_debajo = select_deaths(
        fitness, edades, {}, {}, set(), set(), [], cfg,
        garden_drawdown=cfg.risk.garden_max_drawdown,
    )
    assert sin_parametro == con_cero == justo_debajo


def test_el_suelo_de_poblacion_no_indulta_un_drawdown(cfg) -> None:
    """Un bot que se ha desangrado no sigue vivo porque el censo quede corto:
    para eso está la siembra."""
    n = cfg.garden.min_population
    fitness = _poblacion(n)
    edades = {b: 9 for b in fitness}
    drawdowns = {b: cfg.risk.hard_max_drawdown + 0.1 for b in fitness}
    muertes = select_deaths(fitness, edades, {}, drawdowns, set(), set(), [], cfg)
    assert len(muertes) == n


# --------------------------------------------------------------------------- #
# Nacimientos                                                                  #
# --------------------------------------------------------------------------- #


def test_el_plan_reparte_todos_los_nacimientos(cfg) -> None:
    plan = plan_births({"a": 12}, cfg, diversity=0.8, family_shares={})
    assert sum(plan.values()) == 12
    assert set(plan) == {str(op) for op in BREED_OPERATORS}


def test_el_plan_sigue_las_cuotas_de_la_configuracion(cfg) -> None:
    plan = plan_births({"a": 100}, cfg, diversity=0.8, family_shares={})
    assert plan[str(BreedOperator.MUTATE)] > plan[str(BreedOperator.CROSSOVER)]
    assert plan[str(BreedOperator.CROSSOVER)] > plan[str(BreedOperator.FUSION)]
    assert plan[str(BreedOperator.FUSION)] > plan[str(BreedOperator.SEED)]


def test_bajo_el_suelo_de_diversidad_toda_la_cosecha_es_siembra(cfg) -> None:
    """El jardín está convergiendo y hay que meter sangre nueva."""
    plan = plan_births(
        {"a": 12}, cfg, diversity=cfg.evolution.diversity_floor - 0.01, family_shares={}
    )
    assert plan[str(BreedOperator.SEED)] == 12
    assert plan[str(BreedOperator.MUTATE)] == 0


def test_una_familia_pasada_de_cuota_empuja_hacia_la_siembra(cfg) -> None:
    """Mutar y cruzar reproduce las familias que ya están; sembrar es lo único
    que puede traer otra."""
    normal = plan_births({"a": 40}, cfg, diversity=0.8, family_shares={})
    saturado = plan_births(
        {"a": 40}, cfg, diversity=0.8,
        family_shares={IdeaFamily.TREND: cfg.evolution.max_family_share + 0.2},
    )
    assert saturado[str(BreedOperator.SEED)] > normal[str(BreedOperator.SEED)]
    assert saturado[str(BreedOperator.MUTATE)] < normal[str(BreedOperator.MUTATE)]
    assert sum(saturado.values()) == 40


def test_con_el_jardin_en_drawdown_nacen_la_mitad(cfg) -> None:
    """Cuando el ecosistema está sufriendo no es momento de poblarlo."""
    plan = plan_births(
        {"a": 12}, cfg, diversity=0.8, family_shares={},
        garden_drawdown=cfg.risk.garden_max_drawdown + 0.05,
    )
    assert sum(plan.values()) == 6


def test_plan_sin_nacimientos(cfg) -> None:
    plan = plan_births({}, cfg, diversity=0.8, family_shares={})
    assert sum(plan.values()) == 0


def test_familias_bloqueadas(cfg) -> None:
    tope = cfg.evolution.max_family_share
    bloqueadas = blocked_families(
        {IdeaFamily.TREND: tope + 0.1, IdeaFamily.BREAKOUT: tope - 0.1}, cfg
    )
    assert bloqueadas == {IdeaFamily.TREND}


def test_familias_bloqueadas_acepta_texto(cfg) -> None:
    """Las cuotas vienen de JSON en la base, y allí una familia es una cadena."""
    bloqueadas = blocked_families({"TREND": 0.9}, cfg)
    assert bloqueadas == {IdeaFamily.TREND}
