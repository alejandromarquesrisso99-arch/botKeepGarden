"""La incubadora: el sitio donde el sobreajuste tiene más ganas de entrar."""

from __future__ import annotations

import dataclasses
import random

import pytest

from keepgarden.config import IncubatorConfig
from keepgarden.engine import incubator as inc_mod
from keepgarden.engine.incubator import (
    HoldoutViolation,
    IncubationResult,
    Incubator,
)
from keepgarden.genome.random_genome import random_population
from keepgarden.genome.schema import MarketSpec
from keepgarden.storage.db import open_database
from keepgarden.storage.repositories import Repositories


@pytest.fixture
def cfg_pequeno(cfg):
    """Una configuración a escala de test: pliegues de cientos de velas."""
    return dataclasses.replace(
        cfg,
        incubator=IncubatorConfig(
            n_folds=3, train_bars=400, validation_bars=150, embargo_bars=20,
            holdout_bars=200, min_sortino=0.8, max_drawdown=0.30,
            min_trades_per_fold=1, max_oos_decay=0.50, workers=1,
        ),
    )


@pytest.fixture
def velas(walk):
    return walk(3000, seed=20260921)


@pytest.fixture
def incubadora(cfg_pequeno, velas, catalog) -> Incubator:
    return Incubator(cfg=cfg_pequeno, candles=velas, catalog=catalog)


@pytest.fixture
def candidatos(cfg_pequeno, catalog):
    return random_population(10, MarketSpec(), cfg_pequeno, catalog, random.Random(5))


# --------------------------------------------------------------------------- #
# El holdout                                                                   #
# --------------------------------------------------------------------------- #


def test_la_criba_nunca_mira_el_holdout(incubadora, candidatos, monkeypatch) -> None:
    """Criterio de aceptación del hito 4: un test que falla si algún código lee
    el rango de holdout fuera de ``promote()``.

    Se espía el motor y se anota la vela más reciente que ha visto cualquier
    backtest de la criba. Si alguna supera el inicio del holdout, el único juez
    honesto del sistema ha dejado de serlo.
    """
    holdout_ts = int(incubadora.candles.index[incubadora.split.holdout_start])
    vistas: list[int] = []
    original = inc_mod.run_batch

    def espia(genomes, candles, cfg, **kw):
        vistas.append(int(candles.index[-1]))
        return original(genomes, candles, cfg, **kw)

    monkeypatch.setattr(inc_mod, "run_batch", espia)
    incubadora.screen(candidatos)

    assert vistas, "la criba no ha corrido ningún backtest"
    assert max(vistas) < holdout_ts, (
        f"la criba ha mirado hasta {max(vistas)} y el holdout empieza en {holdout_ts}"
    )


def test_promote_es_el_unico_que_llega_al_holdout(incubadora, candidatos, monkeypatch) -> None:
    holdout_ts = int(incubadora.candles.index[incubadora.split.holdout_start])
    vistas: list[int] = []
    original = inc_mod.run_backtest

    def espia(genome, candles, cfg, **kw):
        vistas.append(int(candles.index[-1]))
        return original(genome, candles, cfg, **kw)

    monkeypatch.setattr(inc_mod, "run_backtest", espia)
    incubadora.promote(candidatos[0], generation=1)
    assert max(vistas) >= holdout_ts


def test_un_tramo_que_invade_el_holdout_es_un_error(incubadora) -> None:
    incubadora.assert_no_holdout_range(incubadora.split.holdout_start)
    with pytest.raises(HoldoutViolation):
        incubadora.assert_no_holdout_range(incubadora.split.holdout_start + 1)


# --------------------------------------------------------------------------- #
# La criba                                                                     #
# --------------------------------------------------------------------------- #


def test_devuelve_un_resultado_por_candidato_y_en_orden(incubadora, candidatos) -> None:
    salida = incubadora.screen(candidatos)
    assert [r.genome.id for r in salida] == [g.id for g in candidatos]


def test_el_umbral_sube_con_el_tamano_de_la_cosecha(incubadora, candidatos) -> None:
    """Probar 10.000 genomas y quedarse con el mejor no es selección, es
    dragado de datos."""
    uno = incubadora.screen(candidatos[:1])[0].threshold_used
    diez = incubadora.screen(candidatos)[0].threshold_used
    assert diez > uno
    esperado = incubadora.cfg.incubator.min_sortino * (
        1 + incubadora.cfg.incubator.multiplicity_penalty * 1.0
    )
    assert diez == pytest.approx(esperado)


def test_los_clones_se_rechazan_sin_gastar_un_backtest(incubadora, candidatos) -> None:
    gemelo = dataclasses.replace(candidatos[0], id="gen_gemelo")
    salida = incubadora.screen([gemelo], alive_genomes=[candidatos[0]])
    assert not salida[0].passed
    assert "clon" in salida[0].reject_reason
    assert salida[0].fold_metrics == []


def test_un_candidato_distinto_no_es_clon(incubadora, candidatos) -> None:
    salida = incubadora.screen([candidatos[0]], alive_genomes=[candidatos[5]])
    assert "clon" not in salida[0].reject_reason


def test_hay_un_resultado_por_pliegue(incubadora, candidatos) -> None:
    salida = incubadora.screen(candidatos[:3])
    for r in salida:
        if r.fold_metrics:
            assert len(r.fold_metrics) == incubadora.cfg.incubator.n_folds


def test_el_que_opera_poco_no_nace(incubadora, candidatos) -> None:
    exigente = dataclasses.replace(
        incubadora.cfg,
        incubator=dataclasses.replace(
            incubadora.cfg.incubator, min_trades_per_fold=10_000
        ),
    )
    otra = Incubator(cfg=exigente, candles=incubadora.candles, catalog=incubadora.catalog)
    for r in otra.screen(candidatos):
        assert not r.passed
        assert "opera poco" in r.reject_reason or "drawdown" in r.reject_reason


def test_cosecha_vacia(incubadora) -> None:
    assert incubadora.screen([]) == []


def test_la_criba_es_determinista(cfg_pequeno, velas, catalog, candidatos) -> None:
    una = Incubator(cfg=cfg_pequeno, candles=velas, catalog=catalog).screen(candidatos)
    otra = Incubator(cfg=cfg_pequeno, candles=velas, catalog=catalog).screen(candidatos)
    assert [(r.genome.id, r.passed, r.median_sortino) for r in una] == [
        (r.genome.id, r.passed, r.median_sortino) for r in otra
    ]


def test_cada_pliegue_arranca_con_capital_fresco(incubadora, candidatos) -> None:
    """Arrastrar el capital haría que un mal año dejara al bot abortado y
    vaciara los pliegues siguientes, que es justo lo que no se quiere medir."""
    salida = [r for r in incubadora.screen(candidatos) if r.fold_metrics]
    assert salida
    # Si el capital se arrastrase, tras un pliegue abortado los siguientes
    # tendrían 0 operaciones siempre. Al menos un bot opera en el último.
    assert any(r.fold_metrics[-1]["n_trades"] > 0 for r in salida)


# --------------------------------------------------------------------------- #
# El ascenso                                                                   #
# --------------------------------------------------------------------------- #


def test_el_holdout_se_escribe_una_sola_vez(
    tmp_path, cfg_pequeno, velas, catalog, candidatos
) -> None:
    """Si alguien intenta escribir una segunda fila, la base lo impide: sería
    señal de que se está mirando el holdout más de una vez."""
    import sqlite3

    base = open_database(tmp_path / "garden.db")
    repos = Repositories.open(base)
    repos.bots.create(candidatos[0], generation=0, initial_capital=1000.0)

    incubadora = Incubator(
        cfg=cfg_pequeno, candles=velas, catalog=catalog, repo=repos.incubation
    )
    incubadora.promote(candidatos[0], generation=1)
    with pytest.raises(sqlite3.IntegrityError):
        incubadora.promote(candidatos[0], generation=2)
    base.close()


def test_el_que_se_degrada_en_holdout_no_asciende(incubadora, candidatos) -> None:
    resultado = incubadora.promote(candidatos[0], generation=1, reference_sortino=50.0)
    assert not resultado.passed
    assert "degrada" in resultado.reject_reason or "operado" in resultado.reject_reason


def test_el_que_no_opera_en_holdout_no_asciende(incubadora, candidatos) -> None:
    resultados = [incubadora.promote(g, generation=1) for g in candidatos]
    sin_operar = [r for r in resultados if r.median_trades == 0]
    for r in sin_operar:
        assert not r.passed
        assert "operado" in r.reject_reason


# --------------------------------------------------------------------------- #
# Persistencia                                                                 #
# --------------------------------------------------------------------------- #


def test_la_criba_registra_tambien_a_los_rechazados(
    tmp_path, cfg_pequeno, velas, catalog, candidatos
) -> None:
    """Saber qué no funciona es la mitad del informe del jardinero."""
    base = open_database(tmp_path / "garden.db")
    repos = Repositories.open(base)
    incubadora = Incubator(
        cfg=cfg_pequeno, candles=velas, catalog=catalog, repo=repos.incubation
    )
    salida = incubadora.screen(candidatos, generation=3)

    filas = repos.incubation.for_generation(3)
    assert len(filas) == len(candidatos)
    assert sum(1 for f in filas if not f["passed"]) == sum(
        1 for r in salida if not r.passed
    )
    assert all(f["n_candidates"] == len(candidatos) for f in filas)
    base.close()


def test_el_resultado_tiene_los_campos_del_contrato() -> None:
    r = IncubationResult(genome=None, passed=False)  # type: ignore[arg-type]
    for campo in (
        "median_sortino", "median_drawdown", "median_trades", "oos_decay",
        "threshold_used", "fold_metrics", "fitness_incubator", "duration_ms",
    ):
        assert hasattr(r, campo)
