"""El esquema SQL debe aplicarse limpio y sostener sus invariantes."""

from __future__ import annotations

import sqlite3

import pytest

from keepgarden.storage.db import SCHEMA_PATH


@pytest.fixture
def con() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    c.row_factory = sqlite3.Row
    return c


def _names(con: sqlite3.Connection, kind: str) -> set[str]:
    return {r[0] for r in con.execute(f"select name from sqlite_master where type='{kind}'")}


def test_el_esquema_se_aplica(con: sqlite3.Connection) -> None:
    tablas = _names(con, "table")
    esperadas = {
        "garden_meta", "genomes", "bots", "parentage", "generations", "bot_metrics",
        "orders", "trades", "equity_snapshots", "garden_equity", "species",
        "genetic_distances", "events", "alerts", "gardener_sessions",
        "gardener_decisions", "incubation_runs", "holdout_results",
        "data_gaps", "data_anomalies",
    }
    assert esperadas <= tablas


def test_las_vistas_existen(con: sqlite3.Connection) -> None:
    assert {"v_alive_bots", "v_lineage_edges", "v_open_alerts", "v_pending_reviews"} <= _names(con, "view")


def test_las_ordenes_son_idempotentes(con: sqlite3.Connection) -> None:
    """Reprocesar una vela tras un reinicio no puede duplicar operaciones."""
    con.execute("insert into genomes values ('g1','h','TREND','binance','BTC/USDT','1h',0,4,2,2,0,'SEED','lin',' {}',datetime('now'))")
    con.execute(
        "insert into bots (bot_id,genome_id,name,status,family,root_lineage,born_generation,"
        "operator,initial_capital,equity,peak_equity,cash) "
        "values ('b1','g1','Zarza','ALIVE','TREND','lin',0,'SEED',1000,1000,1000,1000)"
    )
    fila = (
        "insert or ignore into orders (bot_id,candle_ts,fill_ts,kind,side,price,"
        "reference_price,slippage,amount,notional,fee) "
        "values ('b1',1000,1001,'ENTRY','LONG',100,100,0,1,100,0.1)"
    )
    con.execute(fila)
    con.execute(fila)
    assert con.execute("select count(*) from orders").fetchone()[0] == 1


def test_el_holdout_solo_se_mira_una_vez(con: sqlite3.Connection) -> None:
    """La clave primaria por bot impide una segunda evaluación de holdout."""
    con.execute("insert into genomes values ('g1','h','TREND','binance','BTC/USDT','1h',0,4,2,2,0,'SEED','lin','{}',datetime('now'))")
    con.execute(
        "insert into bots (bot_id,genome_id,name,status,family,root_lineage,born_generation,"
        "operator,initial_capital,equity,peak_equity,cash) "
        "values ('b1','g1','Zarza','ALIVE','TREND','lin',0,'SEED',1000,1000,1000,1000)"
    )
    con.execute("insert into holdout_results values ('b1',3,1000,1.2,0.2,40,0.1,1)")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("insert into holdout_results values ('b1',4,2000,0.9,0.3,30,0.4,0)")


def test_las_claves_ajenas_se_respetan(con: sqlite3.Connection) -> None:
    con.execute("pragma foreign_keys = ON")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            "insert into bots (bot_id,genome_id,name,status,family,root_lineage,"
            "born_generation,operator,initial_capital,equity,peak_equity,cash) "
            "values ('b9','no_existe','X','ALIVE','TREND','lin',0,'SEED',1000,1000,1000,1000)"
        )
