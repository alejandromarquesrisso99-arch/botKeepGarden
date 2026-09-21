"""La API del dashboard: lo que lee y lo que promete no tocar.

Se prueba ``DashboardAPI`` directamente, sin levantar servidor: por eso la
lógica vive separada del enrutado de FastAPI. El jardín de juguete se escribe a
mano para que cada aserción diga exactamente qué dato produce qué respuesta.
"""

from __future__ import annotations

import random
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from keepgarden.config import Config
from keepgarden.dashboard.api import DashboardAPI, reject_category
from keepgarden.evolution.lineage import ParentEdge
from keepgarden.genome.random_genome import random_population
from keepgarden.genome.schema import MarketSpec
from keepgarden.ids import bot_id_of
from keepgarden.storage.db import open_database
from keepgarden.storage.repositories import Repositories
from keepgarden.types import BotStatus, BreedOperator, DeathCause, EventType


class _Resultado:
    """Lo mínimo que ``IncubationRepository.record`` espera de un resultado."""

    def __init__(self, genome, passed: bool, reason: str = "") -> None:
        self.genome = genome
        self.passed = passed
        self.reject_reason = reason
        self.median_sortino = 1.4 if passed else -0.3
        self.median_drawdown = 0.12
        self.median_trades = 40.0
        self.oos_decay = 0.2
        self.threshold_used = 0.86
        self.fold_metrics = [{"fold": 0, "sortino": 1.1, "n_trades": 30}]
        self.duration_ms = 12


@pytest.fixture
def jardin(tmp_path: Path, cfg: Config, catalog):
    """Un jardín de juguete: 6 bots, dos generaciones, una muerte y una cría."""
    config = replace(cfg, root=tmp_path)
    ruta = tmp_path / "state" / "db" / "garden.db"
    db = open_database(ruta)
    repos = Repositories.open(db)

    genomas = random_population(6, MarketSpec(), config, catalog, random.Random(7))
    bots = [
        repos.bots.create(g, generation=0, initial_capital=1000.0) for g in genomas[:4]
    ]
    # El operador de un bot lo lleva su propio genoma: el hijo nace por cruce.
    hijo = genomas[4].with_meta(
        operator=BreedOperator.CROSSOVER,
        parents=(genomas[0].id, genomas[1].id),
    )
    descartado = genomas[5]

    repos.generations.open(0, 1_600_000_000_000)
    for i, bot in enumerate(bots):
        repos.bots.set_fitness(
            bot, incubator=0.5 + i * 0.1, live=None, effective=0.5 + i * 0.1
        )
        repos.metrics.upsert(bot, 0, "incubator", {"fitness": 0.5 + i * 0.1, "sortino": 1.0})
        repos.events.log(
            EventType.BOT_BORN, f"se siembra {bot}", ts=1_600_000_000_000,
            generation=0, bot_id=bot, payload={"operator": "SEED"},
        )
    repos.generations.close(0, {
        "population_size": 4, "births": 4, "deaths": 0, "n_species": 2,
        "fitness_best": 0.8, "fitness_median": 0.65, "fitness_worst": 0.5,
        "genetic_diversity": 0.7, "family_shares": {"TREND": 1.0},
    })

    # Generación 1: nace un hijo por cruce y muere un fundador.
    nuevo = bot_id_of(hijo.id)
    repos.bots.create(
        hijo, generation=1, initial_capital=1000.0,
        parents=[
            ParentEdge(bots[0], nuevo, BreedOperator.CROSSOVER, 1.0, 0),
            ParentEdge(bots[1], nuevo, BreedOperator.CROSSOVER, 1.0, 1),
        ],
    )
    repos.bots.set_fitness(nuevo, incubator=0.95, live=None, effective=0.95)
    repos.bots.set_status(bots[3], BotStatus.CULLED, generation=1,
                          cause=DeathCause.LOW_FITNESS)
    repos.incubation.record(1, _Resultado(hijo, True), n_candidates=2,
                            parents=[bots[0], bots[1]])
    repos.incubation.record(
        1, _Resultado(descartado, False, "clon de gen_abc (distancia 0.010 < 0.15)"),
        n_candidates=2, parents=[bots[2]],
    )
    repos.generations.open(1, 1_600_100_000_000)
    repos.generations.close(1, {
        "population_size": 4, "births": 1, "deaths": 1, "discarded": 1,
        "n_species": 2, "fitness_best": 0.95, "fitness_median": 0.7,
        "fitness_worst": 0.5, "genetic_diversity": 0.68,
        "family_shares": {"TREND": 1.0}, "best_bot_id": nuevo,
    })
    db.set_meta("current_generation", 1)
    db.close()

    solo_lectura = open_database(ruta, read_only=True, create=False)
    api = DashboardAPI(cfg=config, repos=Repositories.open(solo_lectura))
    yield api, {"fundadores": bots, "hijo": nuevo, "muerto": bots[3], "ruta": ruta}
    solo_lectura.close()


# --------------------------------------------------------------------------- #
# Portada                                                                      #
# --------------------------------------------------------------------------- #


def test_el_resumen_cuenta_vivos_y_muertos(jardin) -> None:
    api, datos = jardin
    s = api.garden_summary()
    assert s["generation"] == 1
    assert s["n_alive"] == 4        # 4 fundadores - 1 muerto + 1 hijo
    assert s["n_total"] == 5
    assert s["n_dead"] == 1
    assert s["top_bots"][0]["bot_id"] == datos["hijo"]  # el de mayor fitness


def test_sin_jardin_vivo_la_curva_sale_vacia_en_vez_de_reventar(jardin) -> None:
    api, _ = jardin
    curva = api.garden_equity()
    assert curva["empty"] is True and curva["points"] == []


# --------------------------------------------------------------------------- #
# Generaciones y demografía                                                    #
# --------------------------------------------------------------------------- #


def test_las_generaciones_traen_la_demografia_cruzada(jardin) -> None:
    api, _ = jardin
    gens = api.generations()
    assert [g["generation"] for g in gens] == [0, 1]
    assert gens[0]["births_by_operator"] == {"SEED": 4}
    assert gens[1]["births_by_operator"] == {"CROSSOVER": 1}
    assert gens[1]["deaths_by_cause"] == {"LOW_FITNESS": 1}
    assert gens[1]["incubation"] == {"candidates": 2, "passed": 1}


def test_el_detalle_de_una_generacion_lista_quien_nace_y_quien_muere(jardin) -> None:
    api, datos = jardin
    g = api.generation(1)
    assert [b["bot_id"] for b in g["births"]] == [datos["hijo"]]
    # En orden de ordinal: el padre 0 primero. Una fusión de varios padres
    # depende de ese orden para repartir pesos.
    assert g["births"][0]["parents"] == datos["fundadores"][:2]
    assert [b["bot_id"] for b in g["deaths"]] == [datos["muerto"]]
    assert g["deaths"][0]["death_cause"] == "LOW_FITNESS"


def test_una_generacion_que_no_existe_es_un_error_claro(jardin) -> None:
    api, _ = jardin
    with pytest.raises(KeyError):
        api.generation(99)


# --------------------------------------------------------------------------- #
# Cría                                                                         #
# --------------------------------------------------------------------------- #


def test_la_criba_separa_lo_que_nace_de_lo_que_se_descarta(jardin) -> None:
    api, _ = jardin
    inc = api.incubation(1)
    assert inc["candidates"] == 2 and inc["passed"] == 1
    assert inc["pass_rate"] == 0.5
    assert inc["reject_reasons"] == {"clon de un vivo": 1}
    aprobado = next(r for r in inc["runs"] if r["passed"])
    assert aprobado["reject_category"] is None
    assert aprobado["fold_metrics"]


def test_el_embudo_de_cria_encadena_concebidos_nacidos_y_vivos(jardin) -> None:
    api, _ = jardin
    b = api.breeding()
    gen1 = next(g for g in b["by_generation"] if g["generation"] == 1)
    assert gen1["candidates"] == 2 and gen1["passed"] == 1
    assert gen1["born"] == 1 and gen1["alive"] == 1
    assert b["reject_reasons"] == {"clon de un vivo": 1}
    prolificos = {p["bot_id"]: p["hijos"] for p in b["most_prolific"]}
    assert set(prolificos.values()) == {1}


@pytest.mark.parametrize(
    "motivo, categoria",
    [
        ("clon de gen_abc (distancia 0.010 < 0.15)", "clon de un vivo"),
        ("opera poco: mediana de 7 operaciones por pliegue, mínimo 15", "opera poco"),
        ("sortino -2.60 por debajo del umbral 0.86", "sortino bajo"),
        ("el freno de drawdown salta en 3 de 5 pliegues", "freno de drawdown"),
        ("drawdown de 40.00% en holdout, por encima de 30.00%", "drawdown alto"),
        ("se degrada fuera de muestra: 0.80 > 0.5", "se degrada fuera de muestra"),
        ("", "sin motivo"),
    ],
)
def test_los_motivos_de_rechazo_se_agrupan_en_familias_contables(motivo, categoria) -> None:
    """Los motivos traen números: como cadena cruda cada rechazo sería su propia
    categoría y el gráfico no diría nada."""
    assert reject_category(motivo) == categoria


# --------------------------------------------------------------------------- #
# Genealogía                                                                   #
# --------------------------------------------------------------------------- #


def test_el_grafo_trae_nodos_aristas_y_los_ejes_del_dashboard(jardin) -> None:
    api, datos = jardin
    g = api.lineage_graph()
    assert len(g["nodes"]) == 5
    assert len(g["edges"]) == 2
    assert g["generations"] == {"min": 0, "max": 1}
    hijo = next(n for n in g["nodes"] if n["id"] == datos["hijo"])
    assert hijo["operator"] == "CROSSOVER" and hijo["generation"] == 1


def test_el_slider_temporal_no_ensena_el_futuro(jardin) -> None:
    """``until_generation`` es lo que reproduce la evolución: en la generación 0
    el hijo todavía no existe y el que muere en la 1 sigue vivo."""
    api, datos = jardin
    g = api.lineage_graph(0)
    ids = {n["id"] for n in g["nodes"]}
    assert datos["hijo"] not in ids
    assert g["edges"] == []
    muerto = next(n for n in g["nodes"] if n["id"] == datos["muerto"])
    assert muerto["status_at"] == "ALIVE"     # muere en la generación 1
    assert muerto["status"] == "CULLED"       # hoy está muerto

    despues = api.lineage_graph(1)
    muerto = next(n for n in despues["nodes"] if n["id"] == datos["muerto"])
    assert muerto["status_at"] == "CULLED"


# --------------------------------------------------------------------------- #
# Ficha de bot                                                                 #
# --------------------------------------------------------------------------- #


def test_la_ficha_trae_padres_hijos_genoma_legible_y_crudo(jardin) -> None:
    api, datos = jardin
    ficha = api.bot(datos["hijo"])
    assert {p["bot_id"] for p in ficha["parents"]} == set(datos["fundadores"][:2])
    assert all(p["operator"] == "CROSSOVER" for p in ficha["parents"])
    assert ficha["genome"]["describe"].startswith("[")
    assert ficha["genome"]["raw"]["id"] == ficha["genome_id"]
    assert ficha["incubation"][0]["passed"] == 1

    padre = api.bot(datos["fundadores"][0])
    assert [h["bot_id"] for h in padre["children"]] == [datos["hijo"]]


def test_un_bot_que_no_existe_es_un_error_claro(jardin) -> None:
    api, _ = jardin
    with pytest.raises(KeyError):
        api.bot("bot_inventado")


# --------------------------------------------------------------------------- #
# Especies                                                                     #
# --------------------------------------------------------------------------- #


def test_el_mapa_genetico_proyecta_a_dos_ejes(jardin) -> None:
    api, _ = jardin
    sc = api.species_scatter()
    assert len(sc["points"]) == 4              # 4 fundadores - 1 muerto + el hijo
    assert all(set(p) >= {"x", "y", "family", "fitness"} for p in sc["points"])
    assert 0.0 <= sc["explained"] <= 1.0
    assert 0.0 <= sc["mean_distance"] <= 1.0


def test_sin_curvas_de_capital_no_hay_heatmap_pero_tampoco_error(jardin) -> None:
    api, _ = jardin
    assert api.species_correlation()["empty"] is True


# --------------------------------------------------------------------------- #
# La promesa: sólo lectura                                                     #
# --------------------------------------------------------------------------- #


def test_el_dashboard_abre_la_base_en_solo_lectura(jardin) -> None:
    """El dashboard nunca escribe en el jardín. La base lo impide, no la buena fe."""
    api, _ = jardin
    with pytest.raises(sqlite3.OperationalError):
        api.repos.db.execute("INSERT INTO garden_meta (key, value) VALUES ('x', 'y')")


def test_recorrer_toda_la_api_no_deja_la_base_modificada(jardin) -> None:
    api, datos = jardin
    antes = datos["ruta"].stat().st_size
    llamadas = [
        api.garden_summary,
        api.garden_equity,
        api.generations,
        lambda: api.generation(1),
        api.breeding,
        lambda: api.incubation(1),
        api.lineage_graph,
        lambda: api.lineage_graph(0),
        api.bots,
        lambda: api.bot(datos["hijo"]),
        lambda: api.bot_equity(datos["hijo"]),
        lambda: api.bot_trades(datos["hijo"]),
        lambda: api.bot_genome(datos["hijo"]),
        api.species,
        api.species_scatter,
        api.species_correlation,
        api.journal,
        api.events,
        api.alerts,
    ]
    for llamada in llamadas:
        llamada()
    assert datos["ruta"].stat().st_size == antes
