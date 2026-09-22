"""La base del jardín: esquema, transacciones y repositorios."""

from __future__ import annotations

import random
import sqlite3
from pathlib import Path

import pytest

from keepgarden.evolution.lineage import ParentEdge
from keepgarden.genome.random_genome import random_population
from keepgarden.genome.schema import MarketSpec
from keepgarden.ids import bot_id_of
from keepgarden.storage.db import SCHEMA_VERSION, Database, SchemaTooNew, open_database
from keepgarden.storage.repositories import Repositories, lttb
from keepgarden.types import BotStatus, BreedOperator, DeathCause, EventType


@pytest.fixture
def db(tmp_path: Path) -> Database:
    base = open_database(tmp_path / "garden.db")
    yield base
    base.close()


@pytest.fixture
def repos(db: Database) -> Repositories:
    return Repositories.open(db)


@pytest.fixture
def genomas(cfg, catalog):
    return random_population(6, MarketSpec(), cfg, catalog, random.Random(20260921))


# --------------------------------------------------------------------------- #
# La base                                                                      #
# --------------------------------------------------------------------------- #


def test_una_orden_repetida_devuelve_cero_y_no_se_duplica(
    repos: Repositories, genomas
) -> None:
    """El índice único protege a 'orders'; el 0 avisa a quien llama.

    Sin esa distinción, ``lastrowid`` devuelve el id de la última inserción que
    sí ocurrió —que no tiene nada que ver con esta orden— y el motor creería
    haber registrado algo nuevo.
    """
    bot = repos.bots.create(genomas[0], generation=0, initial_capital=1000.0)
    datos = dict(
        bot_id=bot, candle_ts=1_600_000_000_000, fill_ts=1_600_003_600_000,
        kind="ENTRY", side="LONG", price=100.0, amount=1.0, notional=100.0, fee=0.1,
    )
    primera = repos.trades.record_order(**datos)
    assert primera > 0

    segunda = repos.trades.record_order(**datos)
    assert segunda == 0, "una orden ya registrada tiene que avisar de que ya estaba"
    assert repos.db.query_one("SELECT COUNT(*) AS n FROM orders")["n"] == 1

    # Cambiar el tipo sí es otra orden: la clave es (bot, vela, tipo).
    assert repos.trades.record_order(**{**datos, "kind": "EXIT_SIGNAL"}) > 0
    assert repos.db.query_one("SELECT COUNT(*) AS n FROM orders")["n"] == 2


def test_link_order_ata_la_orden_a_su_operacion(repos: Repositories, genomas) -> None:
    bot = repos.bots.create(genomas[0], generation=0, initial_capital=1000.0)
    orden = repos.trades.record_order(
        bot_id=bot, candle_ts=1_600_000_000_000, fill_ts=1_600_003_600_000,
        kind="ENTRY", side="LONG", price=100.0, amount=1.0, notional=100.0, fee=0.1,
    )
    repos.generations.open(0, 0)
    operacion = repos.trades.open_trade(
        bot, 0, side="LONG", open_ts=1_600_003_600_000, open_price=100.0, amount=1.0
    )
    repos.trades.link_order(orden, operacion)
    fila = repos.db.query_one("SELECT trade_id FROM orders WHERE order_id = ?", (orden,))
    assert int(fila["trade_id"]) == operacion


def test_inicializar_crea_el_esquema_y_la_version(db: Database) -> None:
    assert db.get_meta("schema_version") == str(SCHEMA_VERSION)
    tablas = {
        row["name"]
        for row in db.query("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert {"bots", "genomes", "generations", "bot_metrics", "events"} <= tablas


def test_inicializar_es_idempotente(tmp_path: Path) -> None:
    ruta = tmp_path / "garden.db"
    open_database(ruta).close()
    base = open_database(ruta)
    assert base.get_meta("schema_version") == str(SCHEMA_VERSION)
    base.close()


def test_la_base_va_en_modo_wal(db: Database) -> None:
    """Para que el dashboard pueda leer en paralelo sin bloquear al motor."""
    modo = db.query_one("PRAGMA journal_mode")
    assert str(list(modo)[0]).lower() == "wal"


def test_las_filas_se_leen_por_nombre(db: Database) -> None:
    db.set_meta("prueba", "valor")
    fila = db.query_one("SELECT * FROM garden_meta WHERE key = 'prueba'")
    assert fila["value"] == "valor"


def test_la_transaccion_revierte_ante_una_excepcion(db: Database) -> None:
    """O se escribe la generación entera o no se escribe nada: un cierre a
    medias dejaría el jardín en un estado que ningún arranque sabría leer."""
    with pytest.raises(RuntimeError):
        with db.transaction():
            db.execute("INSERT INTO generations (generation, started_ts) VALUES (7, 1)")
            raise RuntimeError("algo se rompió a mitad")
    assert db.query_one("SELECT * FROM generations WHERE generation = 7") is None


def test_la_transaccion_confirma_al_salir(db: Database) -> None:
    with db.transaction():
        db.execute("INSERT INTO generations (generation, started_ts) VALUES (7, 1)")
    assert db.query_one("SELECT * FROM generations WHERE generation = 7") is not None


def test_las_transacciones_se_pueden_anidar(db: Database) -> None:
    with db.transaction():
        db.execute("INSERT INTO generations (generation, started_ts) VALUES (1, 1)")
        with db.transaction():
            db.execute("INSERT INTO generations (generation, started_ts) VALUES (2, 1)")
    assert len(db.query("SELECT * FROM generations")) == 2


def test_un_jardin_viejo_gana_las_tablas_nuevas_al_abrirlo(tmp_path: Path) -> None:
    """Abrir sin crear tiene que poner al día un jardín de una versión anterior.

    ``keepgarden run`` abre con ``create=False`` —no quiere fabricar un jardín
    por una errata en la ruta—, así que si la puesta al día viviera sólo en
    ``initialize`` el motor se encontraría con una tabla que su código da por
    hecha. Ver docs/DECISIONS.md D-033.
    """
    ruta = tmp_path / "viejo.db"
    base = open_database(ruta)
    base.execute("DROP TABLE bot_runtime")          # así era el esquema 1
    base.set_meta("schema_version", "1")
    base.close()

    puesto_al_dia = open_database(ruta, create=False)
    try:
        assert puesto_al_dia.get_meta("schema_version") == str(SCHEMA_VERSION)
        assert puesto_al_dia.query_one(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'bot_runtime'"
        ) is not None
    finally:
        puesto_al_dia.close()


def test_un_jardin_viejo_gana_las_columnas_nuevas(tmp_path: Path) -> None:
    """Volver a pasar ``schema.sql`` crea tablas, no columnas.

    Una columna añadida a una tabla que ya existe necesita su ``ALTER TABLE``,
    o el jardín viejo sube de versión sin la columna y el código que la da por
    hecha falla al primer INSERT.
    """
    ruta = tmp_path / "viejo.db"
    base = open_database(ruta)
    base.execute("ALTER TABLE species DROP COLUMN ordinal")   # así era el esquema 2
    base.set_meta("schema_version", "2")
    base.close()

    puesto_al_dia = open_database(ruta, create=False)
    try:
        columnas = {
            f["name"] for f in puesto_al_dia.query("PRAGMA table_info(species)")
        }
        assert "ordinal" in columnas
        assert puesto_al_dia.get_meta("schema_version") == str(SCHEMA_VERSION)
        # Y la columna sirve de verdad, no está sólo declarada.
        puesto_al_dia.execute(
            "INSERT INTO species (species_id, generation, size, ordinal) "
            "VALUES ('sp_x', 1, 0, 7)"
        )
        assert puesto_al_dia.query_one(
            "SELECT ordinal FROM species WHERE species_id = 'sp_x'"
        )["ordinal"] == 7
    finally:
        puesto_al_dia.close()


def test_una_base_de_esquema_futuro_no_se_abre(tmp_path: Path) -> None:
    ruta = tmp_path / "futuro.db"
    base = open_database(ruta)
    base.set_meta("schema_version", str(SCHEMA_VERSION + 5))
    base.close()

    otra = Database(path=ruta)
    otra.connect()
    with pytest.raises(SchemaTooNew):
        otra.migrate()
    otra.close()


def test_el_dashboard_abre_en_solo_lectura(tmp_path: Path) -> None:
    ruta = tmp_path / "garden.db"
    open_database(ruta).close()
    lector = Database(path=ruta, read_only=True)
    lector.connect()
    with pytest.raises(sqlite3.OperationalError):
        lector.execute("INSERT INTO garden_meta (key, value) VALUES ('x', 'y')")
    lector.close()


def test_el_snapshot_es_una_copia_completa(db: Database, repos, genomas) -> None:
    repos.bots.create(genomas[0], generation=0, initial_capital=1000.0)
    db.set_meta("current_generation", "12")
    copia = db.snapshot("prueba")
    assert copia.exists()
    assert "0012" in copia.name

    leida = Database(path=copia, read_only=True)
    leida.connect()
    assert len(leida.query("SELECT * FROM bots")) == 1
    leida.close()


# --------------------------------------------------------------------------- #
# Bots                                                                         #
# --------------------------------------------------------------------------- #


def test_crear_un_bot_escribe_genoma_bot_y_parentesco(repos, genomas) -> None:
    padre = repos.bots.create(genomas[0], generation=0, initial_capital=1000.0)
    hijo_id = bot_id_of(genomas[1].id)
    repos.bots.create(
        genomas[1],
        generation=1,
        initial_capital=1000.0,
        parents=[ParentEdge(padre, hijo_id, BreedOperator.MUTATE)],
    )
    assert repos.bots.get(hijo_id)["born_generation"] == 1
    assert repos.lineage.parents_of(hijo_id) == [padre]
    assert repos.lineage.children_of(padre) == [hijo_id]


def test_el_bot_nace_con_su_capital_intacto(repos, genomas) -> None:
    bot = repos.bots.create(genomas[0], generation=0, initial_capital=1500.0)
    fila = repos.bots.get(bot)
    assert fila["equity"] == pytest.approx(1500.0)
    assert fila["cash"] == pytest.approx(1500.0)
    assert fila["peak_equity"] == pytest.approx(1500.0)
    assert fila["name"]


def test_un_bot_nunca_se_borra_cambia_de_estado(repos, genomas) -> None:
    """Su historia completa permanece, que es lo que alimenta la genealogía."""
    bot = repos.bots.create(genomas[0], generation=0, initial_capital=1000.0)
    repos.bots.set_status(
        bot, BotStatus.CULLED, generation=4, cause=DeathCause.LOW_FITNESS
    )
    fila = repos.bots.get(bot)
    assert fila is not None
    assert fila["status"] == str(BotStatus.CULLED)
    assert fila["died_generation"] == 4
    assert fila["death_cause"] == str(DeathCause.LOW_FITNESS)
    assert repos.bots.alive() == []


def test_el_genoma_sobrevive_al_viaje_de_ida_y_vuelta(repos, genomas) -> None:
    bot = repos.bots.create(genomas[0], generation=0, initial_capital=1000.0)
    recuperado = repos.bots.genome_of(bot)
    assert recuperado.to_dict() == genomas[0].to_dict()


def test_genomes_of_devuelve_los_pedidos(repos, genomas) -> None:
    ids = [
        repos.bots.create(g, generation=0, initial_capital=1000.0) for g in genomas[:3]
    ]
    recuperados = repos.bots.genomes_of(ids)
    assert set(recuperados) == set(ids)
    assert repos.bots.genomes_of([]) == {}


def test_pedir_un_bot_que_no_existe_lo_dice_claro(repos) -> None:
    with pytest.raises(KeyError, match="bot_fantasma"):
        repos.bots.genome_of("bot_fantasma")


def test_actualizar_equity_y_fitness(repos, genomas) -> None:
    bot = repos.bots.create(genomas[0], generation=0, initial_capital=1000.0)
    repos.bots.update_equity(bot, 1200.0, 300.0, 1250.0)
    repos.bots.set_fitness(bot, incubator=1.0, live=2.0, effective=1.7)
    fila = repos.bots.get(bot)
    assert fila["equity"] == pytest.approx(1200.0)
    assert fila["fitness_effective"] == pytest.approx(1.7)


def test_proteger_un_bot(repos, genomas) -> None:
    bot = repos.bots.create(genomas[0], generation=0, initial_capital=1000.0)
    repos.bots.protect(bot, 9)
    assert repos.bots.get(bot)["protected_until_gen"] == 9


# --------------------------------------------------------------------------- #
# Generaciones y métricas                                                      #
# --------------------------------------------------------------------------- #


def test_abrir_y_cerrar_una_generacion(repos) -> None:
    repos.generations.open(0, 1_546_300_800_000)
    repos.generations.close(
        0,
        {
            "population_size": 60, "births": 12, "deaths": 8,
            "fitness_median": 0.4, "genetic_diversity": 0.71,
            "family_shares": {"TREND": 0.5, "BREAKOUT": 0.5},
        },
    )
    fila = repos.generations.get(0)
    assert fila["population_size"] == 60
    assert fila["genetic_diversity"] == pytest.approx(0.71)
    assert '"TREND"' in fila["family_shares"]
    assert fila["closed_at"] is not None


def test_una_columna_mal_escrita_al_cerrar_es_un_error(repos) -> None:
    """Una errata haría desaparecer una métrica del dashboard sin avisar."""
    repos.generations.open(0, 1)
    with pytest.raises(KeyError, match="fitnes_median"):
        repos.generations.close(0, {"fitnes_median": 0.4})


def test_la_ultima_generacion_es_la_de_numero_mayor(repos) -> None:
    for g in range(5):
        repos.generations.open(g, g)
    assert repos.generations.latest()["generation"] == 4
    assert len(repos.generations.all()) == 5


def test_las_metricas_se_guardan_por_ambito(repos, genomas) -> None:
    bot = repos.bots.create(genomas[0], generation=0, initial_capital=1000.0)
    repos.generations.open(0, 1)
    repos.metrics.upsert(bot, 0, "live", {"sortino": 1.5, "n_trades": 22, "fitness": 0.8})
    repos.metrics.upsert(bot, 0, "incubator", {"sortino": 2.0, "n_trades": 40})

    vivas = repos.metrics.for_generation(0, "live")
    assert len(vivas) == 1
    assert vivas[0]["sortino"] == pytest.approx(1.5)
    assert repos.metrics.get(bot, 0, "incubator")["sortino"] == pytest.approx(2.0)
    assert len(repos.metrics.history(bot)) == 2


def test_reescribir_una_metrica_la_sustituye(repos, genomas) -> None:
    bot = repos.bots.create(genomas[0], generation=0, initial_capital=1000.0)
    repos.generations.open(0, 1)
    repos.metrics.upsert(bot, 0, "live", {"sortino": 1.0})
    repos.metrics.upsert(bot, 0, "live", {"sortino": 2.0})
    assert len(repos.metrics.for_generation(0, "live")) == 1
    assert repos.metrics.get(bot, 0, "live")["sortino"] == pytest.approx(2.0)


# --------------------------------------------------------------------------- #
# Órdenes: idempotencia                                                        #
# --------------------------------------------------------------------------- #


def test_una_vela_reprocesada_no_duplica_ordenes(repos, genomas) -> None:
    """Es lo que hace seguro reanudar tras un reinicio."""
    bot = repos.bots.create(genomas[0], generation=0, initial_capital=1000.0)
    orden = dict(
        bot_id=bot, candle_ts=1000, fill_ts=2000, kind="ENTRY", side="LONG",
        price=100.0, reference_price=99.9, slippage=0.1, amount=1.0,
        notional=100.0, fee=0.1,
    )
    repos.trades.record_order(**orden)
    repos.trades.record_order(**orden)
    assert len(repos.db.query("SELECT * FROM orders")) == 1


def test_abrir_y_cerrar_una_operacion(repos, genomas) -> None:
    bot = repos.bots.create(genomas[0], generation=0, initial_capital=1000.0)
    tid = repos.trades.open_trade(
        bot, 0, side="LONG", open_ts=1000, open_price=100.0, amount=2.0
    )
    assert len(repos.trades.open_positions(bot)) == 1
    repos.trades.close_trade(
        tid, close_ts=2000, close_price=110.0, exit_kind="EXIT_SIGNAL",
        holding_bars=4, pnl_gross=20.0, pnl_net=19.5, fees=0.5, return_pct=0.0975,
    )
    assert repos.trades.open_positions(bot) == []
    assert repos.trades.for_bot(bot)[0]["pnl_net"] == pytest.approx(19.5)


# --------------------------------------------------------------------------- #
# Curvas                                                                       #
# --------------------------------------------------------------------------- #


def test_la_curva_se_decima_al_maximo_pedido(repos, genomas) -> None:
    bot = repos.bots.create(genomas[0], generation=0, initial_capital=1000.0)
    repos.equity.snapshot_many(
        bot, [(1000 + i, 1000.0 + i, 500.0, 500.0, 0.0) for i in range(5000)]
    )
    curva = repos.equity.curve(bot, max_points=200)
    assert len(curva) <= 200
    assert curva[0]["ts"] == 1000
    assert curva[-1]["ts"] == 1000 + 4999


def test_una_curva_corta_no_se_toca(repos, genomas) -> None:
    bot = repos.bots.create(genomas[0], generation=0, initial_capital=1000.0)
    repos.equity.snapshot_many(bot, [(1000 + i, 1000.0, 0.0, 0.0, 0.0) for i in range(10)])
    assert len(repos.equity.curve(bot, max_points=200)) == 10


def test_la_decimacion_conserva_los_picos() -> None:
    """Un muestreo cada N puntos se come el suelo de un drawdown, que es justo
    lo que hay que ver en una curva de capital."""
    filas = [{"ts": i, "equity": 100.0} for i in range(1000)]
    filas[500] = {"ts": 500, "equity": 10.0}     # el desplome
    salida = lttb(filas, 50)
    assert any(f["equity"] == 10.0 for f in salida)


def test_la_curva_del_jardin(repos) -> None:
    for i in range(100):
        repos.equity.snapshot_garden(
            1000 + i, 0, garden_equity=60000.0 + i, benchmark_equity=50000.0, n_alive=60
        )
    curva = repos.equity.garden_curve(max_points=20)
    assert len(curva) <= 20
    assert curva[-1]["benchmark_equity"] == pytest.approx(50000.0)


# --------------------------------------------------------------------------- #
# Eventos y alertas                                                            #
# --------------------------------------------------------------------------- #


def test_los_eventos_guardan_su_payload(repos, genomas) -> None:
    bot = repos.bots.create(genomas[0], generation=0, initial_capital=1000.0)
    repos.events.log(
        EventType.BOT_BORN, "nace un bot", ts=1000, generation=0, bot_id=bot,
        payload={"operator": "SEED", "fitness": 1.2},
    )
    evento = repos.events.recent(1)[0]
    assert evento["type"] == str(EventType.BOT_BORN)
    assert "SEED" in evento["payload"]
    assert repos.events.for_bot(bot)[0]["summary"] == "nace un bot"


def test_una_alerta_abierta_no_se_duplica(repos) -> None:
    repos.events.raise_alert(
        "DIVERSITY_FLOOR", ts=1000, generation=1, value=0.2, threshold=0.3
    )
    repos.events.raise_alert(
        "DIVERSITY_FLOOR", ts=2000, generation=2, value=0.1, threshold=0.3
    )
    abiertas = repos.events.open_alerts()
    assert len(abiertas) == 1
    assert abiertas[0]["value"] == pytest.approx(0.1)


def test_cerrar_una_alerta(repos) -> None:
    repos.events.raise_alert(
        "DIVERSITY_FLOOR", ts=1000, generation=1, value=0.2, threshold=0.3
    )
    repos.events.clear_alert("DIVERSITY_FLOOR", 3000)
    assert repos.events.open_alerts() == []


# --------------------------------------------------------------------------- #
# Incubadora y holdout                                                         #
# --------------------------------------------------------------------------- #


def test_el_holdout_solo_se_puede_escribir_una_vez(repos, genomas) -> None:
    """Si alguien intenta escribir una segunda fila, la base lo impide, y eso
    es exactamente lo que se quiere: significaría que se está mirando el
    holdout más de una vez."""
    bot = repos.bots.create(genomas[0], generation=0, initial_capital=1000.0)
    datos = dict(
        generation=1, evaluated_ts=1000, sortino=1.0, max_drawdown=0.2,
        n_trades=30, decay=0.1, promoted=True,
    )
    repos.incubation.record_holdout(bot, **datos)
    with pytest.raises(sqlite3.IntegrityError):
        repos.incubation.record_holdout(bot, **datos)


def test_los_rechazos_de_la_incubadora_se_cuentan(repos, genomas) -> None:
    from keepgarden.engine.incubator import IncubationResult

    resultados = [
        IncubationResult(genome=genomas[0], passed=False, reject_reason="sortino"),
        IncubationResult(genome=genomas[1], passed=False, reject_reason="sortino"),
        IncubationResult(genome=genomas[2], passed=True),
    ]
    repos.incubation.record_many(3, resultados, n_candidates=3)
    assert repos.incubation.rejection_reasons(3) == {"sortino": 2}
    assert len(repos.incubation.for_generation(3)) == 3


# --------------------------------------------------------------------------- #
# Jardinero                                                                    #
# --------------------------------------------------------------------------- #


def test_una_sesion_del_jardinero_con_su_decision(repos) -> None:
    from keepgarden.gardener.proposals import Proposal
    from keepgarden.types import ProposalKind, ProposalStatus

    sesion = repos.gardener.open_session(4, "scheduled", "state/reports/g4.md")
    propuesta = Proposal(
        kind=ProposalKind.NOTE,
        rationale="la diversidad lleva tres generaciones cayendo",
        expected_effect="subirá por encima de 0.35",
        review_in_generations=3,
        status=ProposalStatus.APPLIED,
    )
    repos.gardener.record_decision(sesion, propuesta)
    repos.gardener.close_session(sesion, "Sesión tranquila.")

    diario = repos.gardener.journal()
    assert len(diario) == 1
    assert diario[0]["journal_entry"] == "Sesión tranquila."


def test_las_revisiones_vencen_cuando_toca(repos) -> None:
    """Es lo que impide que el jardinero repita el mismo consejo cada mes."""
    from keepgarden.gardener.proposals import Proposal
    from keepgarden.types import ProposalKind, ProposalStatus

    repos.db.set_meta("current_generation", "4")
    sesion = repos.gardener.open_session(4, "scheduled", "informe.md")
    decision = repos.gardener.record_decision(
        sesion,
        Proposal(
            kind=ProposalKind.NOTE, rationale="x", expected_effect="y",
            review_in_generations=3, status=ProposalStatus.APPLIED,
        ),
    )
    assert repos.gardener.pending_reviews(6) == []
    assert len(repos.gardener.pending_reviews(7)) == 1

    repos.gardener.record_review(
        decision, generation=7, observed="no pasó nada", verdict="no_effect"
    )
    assert repos.gardener.pending_reviews(7) == []


def test_la_tasa_de_exito_por_operador(repos, cfg, catalog) -> None:
    """Dice si la evolución está funcionando o sólo generando ruido."""
    genomas = random_population(6, MarketSpec(), cfg, catalog, random.Random(5))
    vivos = [
        repos.bots.create(g, generation=0, initial_capital=1000.0) for g in genomas[:3]
    ]
    muertos = [
        repos.bots.create(g, generation=0, initial_capital=1000.0) for g in genomas[3:]
    ]
    for bot in muertos:
        repos.bots.set_status(bot, BotStatus.CULLED, generation=1)

    tasas = repos.gardener.operator_success_rates(10, window=10)
    assert tasas[str(BreedOperator.SEED)] == pytest.approx(len(vivos) / 6)


def test_sin_generaciones_suficientes_no_hay_tasa(repos) -> None:
    """Incluir a los recién nacidos haría que todo operador pareciese infalible."""
    assert repos.gardener.operator_success_rates(2, window=6) == {}
