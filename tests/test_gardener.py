"""El jardinero: informe, límites, aplicación y revisión.

Lo que se prueba aquí no es que el código corra, sino que los límites de
docs/GARDENER_PROTOCOL.md §Límites se cumplan: son lo único que impide que una
sesión de jardinero destroce el jardín.
"""

from __future__ import annotations

import json
import random
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from keepgarden.config import (
    FAMILY_WEIGHTS_KEY,
    Config,
    apply_overrides,
    current_params,
    family_weights,
)
from keepgarden.gardener.apply import OVERRIDES_KEY, ProposalApplier, effective_config
from keepgarden.gardener.journal import Journal
from keepgarden.gardener.proposals import Proposal, ProposalError, parse_session
from keepgarden.gardener.report import ReportBuilder
from keepgarden.genome.random_genome import random_population
from keepgarden.genome.schema import MarketSpec
from keepgarden.storage.db import open_database
from keepgarden.storage.repositories import Repositories
from keepgarden.types import BotStatus, DeathCause, EventType, ProposalStatus


def _velas(n: int = 900, seed: int = 5) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    ts = 1_600_000_000_000 + np.arange(n) * 3_600_000
    close = 10_000.0 * np.exp(np.cumsum(rng.normal(0.0002, 0.01, n)))
    open_ = np.empty(n)
    open_[0] = 10_000.0
    open_[1:] = close[:-1]
    rango = np.abs(rng.normal(0, 0.008, n)) * close
    return pd.DataFrame(
        {
            "open": open_,
            "high": np.maximum(open_, close) + rango,
            "low": np.minimum(open_, close) - rango,
            "close": close,
            "volume": np.abs(rng.lognormal(6, 0.4, n)),
            "trades": np.nan,
        },
        index=pd.Index(ts, name="ts"),
    )


@pytest.fixture
def jardin(tmp_path: Path, cfg: Config, catalog):
    """Un jardín con dos generaciones cerradas y algo de historia."""
    config = replace(cfg, root=tmp_path)
    db = open_database(config.db_file)
    repos = Repositories.open(db)
    market = MarketSpec(
        venue=config.market.venue, symbol=config.primary_symbol,
        timeframe=config.market.timeframe,
    )
    genomas = random_population(8, market, config, catalog, random.Random(19))
    bots = [
        repos.bots.create(g, generation=0, initial_capital=1000.0) for g in genomas
    ]
    for generacion, diversidad, mediana in ((0, 0.80, 0.10), (1, 0.70, 0.30)):
        repos.generations.open(generacion, 1_600_000_000_000)
        repos.generations.close(generacion, {
            "population_size": 8, "births": 2, "deaths": 1, "n_species": 3,
            "genetic_diversity": diversidad, "fitness_median": mediana,
            "fitness_best": 1.0, "fitness_worst": -0.5,
            "family_shares": {"TREND": 0.5, "BREAKOUT": 0.5},
            "garden_equity": 8100.0, "benchmark_equity": 8000.0, "garden_alpha": 0.0125,
            "ended_ts": 1_600_100_000_000,
        })
    # Las métricas van después: su clave foránea apunta a la generación.
    for i, bot in enumerate(bots):
        repos.bots.set_fitness(bot, incubator=1.0 - i * 0.1, live=None, effective=1.0 - i * 0.1)
        repos.metrics.upsert(
            bot, 1, "incubator",
            {"fitness": 1.0 - i * 0.1, "sortino": 1.2, "n_trades": 20, "max_drawdown": 0.1},
        )
    db.set_meta("current_generation", 1)
    db.set_meta("symbol", market.symbol)
    db.set_meta("timeframe", market.timeframe)
    yield config, db, repos, bots
    db.close()


def _propuesta(kind: str, payload: dict, **kw) -> Proposal:
    return Proposal.from_dict(
        {
            "kind": kind,
            "payload": payload,
            "rationale": kw.get("rationale", "porque sí, y con razón"),
            "expected_effect": kw.get("expected_effect", "la diversidad sube de 0.70 a 0.75"),
            "review_in_generations": kw.get("review_in_generations", 2),
        }
    )


# --------------------------------------------------------------------------- #
# El informe                                                                   #
# --------------------------------------------------------------------------- #


def test_el_informe_trae_las_nueve_secciones(jardin) -> None:
    config, db, repos, _ = jardin
    texto = ReportBuilder(cfg=config, repos=repos).build(1)
    for seccion in (
        "## 1. Resumen", "## 2. Demografía", "## 3. Rendimiento", "## 4. Diversidad",
        "## 5. Los mejores", "## 6. Los muertos", "## 7. Qué ha funcionado",
        "## 8. Alertas", "## 9. Decisiones anteriores",
    ):
        assert seccion in texto, f"falta la sección {seccion}"


def test_el_informe_lleva_los_genomas_en_legible(jardin) -> None:
    """Sin el genoma en prosa hay que abrir la base, y entonces el informe falla."""
    config, db, repos, bots = jardin
    texto = ReportBuilder(cfg=config, repos=repos).build(1)
    descripcion = repos.bots.genome_of(bots[0]).describe()
    assert descripcion.splitlines()[1].strip() in texto


def test_el_informe_se_escribe_donde_dice_la_config(jardin) -> None:
    config, db, repos, _ = jardin
    ruta = ReportBuilder(cfg=config, repos=repos).write(1)
    assert ruta.name == "gen_1.md"
    assert ruta.parent == config.path(config.gardener.report_dir)
    assert "# Jardín · generación 1" in ruta.read_text(encoding="utf-8")


def test_latest_resuelve_a_la_ultima_generacion_cerrada(jardin) -> None:
    config, db, repos, _ = jardin
    assert ReportBuilder(cfg=config, repos=repos)._resolve("latest") == 1


# --------------------------------------------------------------------------- #
# Los límites                                                                  #
# --------------------------------------------------------------------------- #


def test_un_parametro_prohibido_se_rechaza_con_su_motivo(jardin) -> None:
    config, db, repos, _ = jardin
    aplicador = ProposalApplier(cfg=config, repos=repos)
    _validas, invalidas = aplicador.check(
        [_propuesta("TUNE", {"param": "risk.hard_max_drawdown", "value": 0.6})], 1
    )
    assert len(invalidas) == 1
    assert "fuera del alcance" in invalidas[0][1]


def test_mover_un_parametro_mas_del_limite_se_rechaza(jardin) -> None:
    """El tope es el 50 % por sesión: los saltos grandes se dan en varios pasos."""
    config, db, repos, _ = jardin
    actual = current_params(config)["evolution.mutation_rate"]
    aplicador = ProposalApplier(cfg=config, repos=repos)
    # Un valor legal en sí mismo (dentro del rango) pero que salta demasiado
    # de golpe: lo que se rechaza es el tamaño del paso, no el destino.
    _validas, invalidas = aplicador.check(
        [_propuesta("TUNE", {"param": "evolution.mutation_rate", "value": actual * 1.9})], 1
    )
    assert invalidas and "supera el máximo" in invalidas[0][1]


def test_una_propuesta_invalida_no_tumba_las_demas(jardin) -> None:
    config, db, repos, bots = jardin
    aplicador = ProposalApplier(cfg=config, repos=repos)
    validas, invalidas = aplicador.check(
        [
            _propuesta("PROTECT", {"bots": [bots[0]], "generations": 2}),
            _propuesta("TUNE", {"param": "seed", "value": 7}),
            _propuesta("NOTE", {"text": "nada"}),
        ],
        1,
    )
    assert [p.kind for p in validas] == [p.kind for p in validas]
    assert len(validas) == 2 and len(invalidas) == 1


def test_una_sesion_que_vacia_el_jardin_no_se_aplica_entera(jardin) -> None:
    """Límite acumulativo: este sí tumba la sesión, y es su razón de ser."""
    config, db, repos, bots = jardin
    aplicador = ProposalApplier(cfg=config, repos=repos)
    with pytest.raises(ProposalError, match="vaciar el jardín"):
        aplicador.check([_propuesta("RETIRE", {"bots": bots[:6]})], 1)


def test_el_jardinero_no_puede_escribir_un_genoma_entero() -> None:
    """Sólo bloques: un genoma a mano no pasa ni el parseo."""
    with pytest.raises(ProposalError):
        parse_session(json.dumps([{"kind": "GRAFT", "payload": {"genome": {}}}]))


# --------------------------------------------------------------------------- #
# La aplicación                                                                #
# --------------------------------------------------------------------------- #


def test_aplicar_registra_lo_aplicado_y_lo_rechazado(jardin) -> None:
    config, db, repos, bots = jardin
    resultado = ProposalApplier(cfg=config, repos=repos).apply(
        [
            _propuesta("PROTECT", {"bots": [bots[0]], "generations": 3}),
            _propuesta("NOTE", {"text": "el linaje de rupturas se está comiendo el jardín"}),
            _propuesta("TUNE", {"param": "execution.mode", "value": 1}),
        ],
        1,
    )
    assert len(resultado.applied) == 2 and len(resultado.rejected) == 1

    decisiones = repos.db.query("SELECT kind, status FROM gardener_decisions ORDER BY decision_id")
    assert len(decisiones) == 3
    assert {str(d["status"]) for d in decisiones} == {
        str(ProposalStatus.APPLIED), str(ProposalStatus.REJECTED)
    }
    eventos = repos.events.recent(type=EventType.GARDENER_PROPOSAL)
    assert len(eventos) == 3


def test_protect_deja_al_bot_a_salvo_de_la_poda(jardin) -> None:
    config, db, repos, bots = jardin
    ProposalApplier(cfg=config, repos=repos).apply(
        [_propuesta("PROTECT", {"bots": [bots[2]], "generations": 4})], 1
    )
    fila = repos.bots.get(bots[2])
    assert int(fila["protected_until_gen"]) == 5


def test_tune_no_toca_el_yaml_sino_la_base(jardin) -> None:
    """Los ajustes del jardinero son reversibles: viven en garden_meta."""
    config, db, repos, _ = jardin
    ProposalApplier(cfg=config, repos=repos).apply(
        [_propuesta("TUNE", {"param": "evolution.diversity_floor", "value": 0.4})], 1
    )
    guardado = json.loads(db.get_meta(OVERRIDES_KEY))
    assert guardado == {"evolution.diversity_floor": 0.4}

    ajustada = effective_config(config, db)
    assert ajustada.evolution.diversity_floor == 0.4
    assert config.evolution.diversity_floor != 0.4     # el original, intacto
    # Y el ajuste se puede deshacer borrando una fila, sin tocar ningún archivo.
    db.set_meta(OVERRIDES_KEY, "{}")
    assert effective_config(config, db).evolution.diversity_floor == (
        config.evolution.diversity_floor
    )


def test_retire_jubila_y_cierra_posiciones(jardin) -> None:
    config, _db, repos, bots = jardin
    from keepgarden.data.store import CandleStore, SeriesKey

    velas = _velas(200)
    store = CandleStore(cache_dir=config.path(config.storage.cache_dir))
    store.append(
        SeriesKey(config.market.venue, config.primary_symbol, config.market.timeframe),
        velas.reset_index(),
    )
    trade_id = repos.trades.open_trade(
        bots[1], 1, side="LONG", open_ts=int(velas.index[10]),
        open_price=float(velas["close"].iloc[10]), amount=0.05,
    )

    ProposalApplier(cfg=config, repos=repos).apply(
        [_propuesta("RETIRE", {"bots": [bots[1]], "reason": "no opera desde la 0"})], 1
    )
    fila = repos.bots.get(bots[1])
    assert str(fila["status"]) == str(BotStatus.RETIRED)
    assert str(fila["death_cause"]) == str(DeathCause.GARDENER)
    cerrada = repos.db.query_one("SELECT * FROM trades WHERE trade_id = ?", (trade_id,))
    assert int(cerrada["is_open"]) == 0
    assert cerrada["exit_kind"] == "EXIT_FORCED"


def test_add_gene_no_se_aplica_en_caliente_sino_que_deja_tarea(jardin) -> None:
    config, db, repos, _ = jardin
    ProposalApplier(cfg=config, repos=repos).apply(
        [
            _propuesta(
                "ADD_GENE",
                {"name": "VWAP_DIST", "category": "volume",
                 "description": "distancia al VWAP de la sesión"},
            )
        ],
        1,
    )
    tarea = config.path(config.gardener.report_dir) / "tareas_ingenieria.md"
    assert tarea.exists() and "VWAP_DIST" in tarea.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# La revisión                                                                  #
# --------------------------------------------------------------------------- #


def test_la_revision_mide_el_efecto_y_dicta_veredicto(jardin) -> None:
    """Lo que impide que el jardinero repita el mismo consejo cada mes."""
    config, db, repos, bots = jardin
    aplicador = ProposalApplier(cfg=config, repos=repos)
    aplicador.apply(
        [
            _propuesta(
                "TUNE", {"param": "evolution.mutation_rate", "value": 0.4},
                review_in_generations=1,
            )
        ],
        1,
    )
    # Llega la generación de revisión con la mediana de fitness mucho mejor.
    repos.generations.open(2, 1_600_200_000_000)
    repos.generations.close(2, {
        "population_size": 8, "fitness_median": 0.9, "genetic_diversity": 0.72,
    })
    assert aplicador.review_due(2) == 1

    decision = repos.db.query_one("SELECT * FROM gardener_decisions LIMIT 1")
    assert decision["verdict"] == "worked"
    assert "fitness mediana" in decision["observed_effect"]
    assert int(decision["reviewed_generation"]) == 2
    assert str(decision["status"]) == str(ProposalStatus.REVIEWED)


def test_una_propuesta_que_empeora_el_jardin_sale_como_backfired(jardin) -> None:
    config, db, repos, _ = jardin
    aplicador = ProposalApplier(cfg=config, repos=repos)
    aplicador.apply(
        [_propuesta("TUNE", {"param": "garden.cull_fraction", "value": 0.3},
                    review_in_generations=1)],
        1,
    )
    repos.generations.open(2, 1_600_200_000_000)
    repos.generations.close(2, {"population_size": 8, "fitness_median": -0.4})
    aplicador.review_due(2)
    assert repos.db.query_one("SELECT verdict FROM gardener_decisions")["verdict"] == "backfired"


def test_el_informe_siguiente_enseña_la_decision_y_su_resultado(jardin) -> None:
    config, db, repos, bots = jardin
    aplicador = ProposalApplier(cfg=config, repos=repos)
    aplicador.apply(
        [_propuesta("PROTECT", {"bots": [bots[0]], "generations": 2},
                    review_in_generations=1)],
        1,
    )
    repos.generations.open(2, 1_600_200_000_000)
    repos.generations.close(2, {"population_size": 8, "fitness_median": 0.35})
    aplicador.review_due(2)

    texto = ReportBuilder(cfg=config, repos=repos).build(2)
    assert "PROTECT" in texto
    assert "siguen vivos" in texto


# --------------------------------------------------------------------------- #
# El diario                                                                    #
# --------------------------------------------------------------------------- #


def test_el_diario_guarda_la_narracion_y_la_devuelve(jardin) -> None:
    config, db, repos, bots = jardin
    aplicador = ProposalApplier(cfg=config, repos=repos)
    resultado = aplicador.apply([_propuesta("NOTE", {"text": "x"})], 1)
    diario = Journal(repos)
    diario.write(resultado.session_id, "El linaje de rupturas se ha derrumbado.")

    assert "derrumbado" in diario.recent(1)[0]
    digest = diario.context_digest(2)
    assert "Linajes dominantes" in digest
    assert "derrumbado" in digest


def test_el_digest_dice_que_hay_pendiente_de_revision(jardin) -> None:
    config, db, repos, bots = jardin
    aplicador = ProposalApplier(cfg=config, repos=repos)
    resultado = aplicador.apply(
        [_propuesta("PROTECT", {"bots": [bots[0]], "generations": 2},
                    review_in_generations=3)],
        1,
    )
    Journal(repos).write(resultado.session_id, "Sesión de prueba.")
    assert "Pendiente de revisión" in Journal(repos).context_digest(2)


# --------------------------------------------------------------------------- #
# Los pesos de siembra por familia                                             #
# --------------------------------------------------------------------------- #


def _poblacion(config: Config, db, catalog, repos):
    from keepgarden.evolution.population import Population

    return Population(
        cfg=config, catalog=catalog, db=db, rng=random.Random(11), repos=repos
    )


def test_rebalance_quotas_deja_los_pesos_donde_el_motor_los_lee(jardin, catalog) -> None:
    config, db, repos, _ = jardin
    ProposalApplier(cfg=config, repos=repos).apply(
        [
            _propuesta(
                "REBALANCE_QUOTAS",
                {"family_weights": {"VOLATILITY": 3.0, "TREND": 1.0}},
            )
        ],
        1,
    )
    assert family_weights(db) == {"VOLATILITY": 3.0, "TREND": 1.0}


def test_los_pesos_del_jardinero_sesgan_de_verdad_la_siembra(jardin, catalog) -> None:
    """Antes se guardaban y no los leía nadie: la propuesta no hacía nada."""
    from keepgarden.types import IdeaFamily

    config, db, repos, _ = jardin
    poblacion = _poblacion(config, db, catalog, repos)
    disponibles = [IdeaFamily.VOLATILITY, IdeaFamily.TREND, IdeaFamily.BREAKOUT]

    poblacion._seed_weights = {"VOLATILITY": 8.0, "TREND": 1.0, "BREAKOUT": 1.0}
    salidas = [poblacion._pick_family(disponibles) for _ in range(400)]
    volatilidad = salidas.count(IdeaFamily.VOLATILITY) / len(salidas)
    assert 0.7 < volatilidad < 0.9, f"esperaba ~80 % de VOLATILITY, salió {volatilidad:.0%}"
    assert set(salidas) == set(disponibles), "los otros no pueden desaparecer"


def test_una_familia_con_peso_cero_no_se_siembra(jardin, catalog) -> None:
    from keepgarden.types import IdeaFamily

    config, db, repos, _ = jardin
    poblacion = _poblacion(config, db, catalog, repos)
    disponibles = [IdeaFamily.VOLATILITY, IdeaFamily.TREND]
    poblacion._seed_weights = {"VOLATILITY": 1.0}
    assert {poblacion._pick_family(disponibles) for _ in range(50)} == {
        IdeaFamily.VOLATILITY
    }


def test_sin_pesos_la_siembra_es_la_de_siempre(jardin, catalog) -> None:
    """Determinismo: un jardín sin REBALANCE_QUOTAS sale idéntico a antes."""
    from keepgarden.types import IdeaFamily

    config, db, repos, _ = jardin
    disponibles = [IdeaFamily.VOLATILITY, IdeaFamily.TREND, IdeaFamily.BREAKOUT]

    poblacion = _poblacion(config, db, catalog, repos)
    con_pesos_vacios = [poblacion._pick_family(disponibles) for _ in range(20)]

    esperado_rng = random.Random(11)
    esperado = [disponibles[esperado_rng.randrange(len(disponibles))] for _ in range(20)]
    assert con_pesos_vacios == esperado


def test_family_weights_ignora_la_basura(jardin) -> None:
    import json as _json

    config, db, repos, _ = jardin
    assert family_weights(db) == {}
    db.set_meta(
        FAMILY_WEIGHTS_KEY,
        _json.dumps({"TREND": 2.0, "BREAKOUT": -1.0, "MOMENTUM": "x", "VOL": 0}),
    )
    assert family_weights(db) == {"TREND": 2.0}

    db.set_meta(FAMILY_WEIGHTS_KEY, "esto no es json")
    assert family_weights(db) == {}


# --------------------------------------------------------------------------- #
# Los ajustes efectivos                                                        #
# --------------------------------------------------------------------------- #


def test_apply_overrides_no_muta_el_original_ni_acepta_rutas_falsas(cfg: Config) -> None:
    nuevo = apply_overrides(
        cfg, {"evolution.mutation_rate": 0.5, "no.existe": 3, "tampoco": 4}
    )
    assert nuevo.evolution.mutation_rate == 0.5
    assert cfg.evolution.mutation_rate != 0.5
    assert not hasattr(nuevo, "no")
