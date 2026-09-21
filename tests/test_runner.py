"""El jardín vivo: el bucle, la reanudación y la promesa de no divergir.

El test que importa es el de equivalencia: un dry-run sobre un tramo de
histórico tiene que producir exactamente las mismas operaciones que un backtest
del mismo genoma sobre el mismo tramo. Si divergen, una sorpresa en vivo no
significa nada y el dry-run no valida nada.
"""

from __future__ import annotations

import random
import sqlite3
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from keepgarden.config import Config
from keepgarden.engine.backtest import run_backtest
from keepgarden.engine.clock import MarketClock, Tick
from keepgarden.engine.runner import GardenRunner
from keepgarden.genome.random_genome import random_population
from keepgarden.genome.schema import MarketSpec
from keepgarden.storage.db import open_database
from keepgarden.storage.repositories import Repositories

BARS = 1200


def _velas(n: int = BARS, seed: int = 11) -> pd.DataFrame:
    """Una serie sintética con tendencias y rangos, para que haya operaciones."""
    rng = np.random.default_rng(seed)
    ts = 1_600_000_000_000 + np.arange(n) * 3_600_000
    deriva = np.repeat(rng.normal(0, 0.0006, n // 100 + 1), 100)[:n]
    ret = rng.normal(deriva, 0.012)
    close = 10_000.0 * np.exp(np.cumsum(ret))
    open_ = np.empty(n)
    open_[0] = 10_000.0
    open_[1:] = close[:-1]
    rango = np.abs(rng.normal(0, 0.01, n)) * close
    return pd.DataFrame(
        {
            "open": open_,
            "high": np.maximum(open_, close) + rango * 0.5,
            "low": np.minimum(open_, close) - rango * 0.5,
            "close": close,
            "volume": np.abs(rng.lognormal(6, 0.5, n)),
            "trades": np.nan,
        },
        index=pd.Index(ts, name="ts"),
    )


def _sembrar(tmp_path: Path, cfg: Config, catalog, *, n_bots: int, velas: pd.DataFrame):
    """Un jardín de juguete con sus velas ya en la caché."""
    from keepgarden.data.store import CandleStore, SeriesKey

    config = replace(
        cfg,
        root=tmp_path,
        garden=replace(cfg.garden, ticks_per_generation=10_000),
    )
    store = CandleStore(cache_dir=config.path(config.storage.cache_dir))
    market = MarketSpec(
        venue=config.market.venue, symbol=config.primary_symbol,
        timeframe=config.market.timeframe,
    )
    store.append(
        SeriesKey(market.venue, market.symbol, market.timeframe),
        velas.reset_index(),
    )

    db = open_database(config.db_file)
    repos = Repositories.open(db)
    genomas = random_population(n_bots, market, config, catalog, random.Random(3))
    bots = [
        repos.bots.create(g, generation=0, initial_capital=config.garden.initial_capital_per_bot)
        for g in genomas
    ]
    repos.generations.open(0, int(velas.index[0]))
    db.set_meta("current_generation", 0)
    db.set_meta("symbol", market.symbol)
    db.set_meta("timeframe", market.timeframe)
    return config, db, repos, dict(zip(bots, genomas))


# --------------------------------------------------------------------------- #
# El reloj                                                                     #
# --------------------------------------------------------------------------- #


def test_la_siguiente_vela_es_la_hora_en_punto_siguiente(cfg: Config) -> None:
    reloj = MarketClock(cfg=cfg)
    assert reloj.next_candle_open(1_600_000_000_000) == 1_600_002_000_000
    # Justo en el cambio de vela, la siguiente es la de después: la que acaba de
    # abrir todavía no ha cerrado y no se puede mirar.
    assert reloj.next_candle_open(1_600_002_000_000) == 1_600_005_600_000


def test_el_replay_cierra_generacion_cada_ticks_per_generation(cfg: Config) -> None:
    config = replace(cfg, garden=replace(cfg.garden, ticks_per_generation=5))
    reloj = MarketClock(cfg=config)
    ticks = list(reloj.replay(_velas(12)))
    assert [t.index for t in ticks] == list(range(12))
    assert [t.closes_generation for t in ticks[:6]] == [False] * 4 + [True, False]
    assert [t.generation for t in ticks] == [1] * 5 + [2] * 5 + [3] * 2


def test_el_replay_no_repite_lo_ya_procesado(cfg: Config) -> None:
    velas = _velas(10)
    reloj = MarketClock(cfg=cfg, last_processed_ts=int(velas.index[4]))
    assert [t.index for t in reloj.replay(velas)] == [5, 6, 7, 8, 9]


def test_el_reloj_vivo_recupera_las_velas_perdidas(cfg: Config) -> None:
    """Tres velas de golpe significan que estuvimos caídos: salen marcadas."""
    velas = _velas(6)
    momentos = [(int(ts), i) for i, ts in enumerate(velas.index)]
    reloj = MarketClock(
        cfg=cfg,
        fetch=lambda desde: momentos[:3],
        sleep=lambda _s: None,
        now=lambda: int(velas.index[0]),
    )
    salida = []
    for tick in reloj.ticks():
        salida.append(tick)
        if len(salida) == 3:
            break
    assert [t.index for t in salida] == [0, 1, 2]
    assert all(t.is_catchup for t in salida)


def test_el_reloj_avisa_cuando_el_venue_falla(cfg: Config) -> None:
    fallos: list[int] = []
    intentos = {"n": 0}

    def fetch(_desde):
        intentos["n"] += 1
        if intentos["n"] <= 3:
            raise ConnectionError("el venue no contesta")
        return [(1_600_000_000_000, 0)]

    reloj = MarketClock(
        cfg=cfg, fetch=fetch, sleep=lambda _s: None,
        now=lambda: 1_600_000_000_000,
        on_venue_failure=lambda n, _exc: fallos.append(n),
    )
    primero = next(iter(reloj.ticks()))
    assert fallos == [1, 2, 3]       # reintenta y avisa en cada fallo
    assert primero.index == 0        # y cuando el venue vuelve, sigue


# --------------------------------------------------------------------------- #
# El bucle                                                                     #
# --------------------------------------------------------------------------- #


def test_el_jardin_vivo_no_diverge_del_backtest(tmp_path: Path, cfg: Config, catalog) -> None:
    """La prueba de fuego del hito 5.

    Un dry-run sobre el tramo completo tiene que dar, bot a bot, exactamente
    las mismas operaciones que un backtest del mismo genoma sobre ese tramo.
    """
    velas = _velas()
    config, db, repos, genomas = _sembrar(tmp_path, cfg, catalog, n_bots=6, velas=velas)
    try:
        runner = GardenRunner(cfg=config, db=db, verbose=False)
        runner.run(dry_run=True)

        comparados = 0
        for bot_id, genoma in genomas.items():
            esperado = run_backtest(genoma, velas, config)
            vividas = repos.db.query(
                "SELECT open_ts, close_ts, open_price, close_price, pnl_net, exit_kind "
                "FROM trades WHERE bot_id = ? AND is_open = 0 ORDER BY open_ts",
                (bot_id,),
            )
            assert len(vividas) == len(esperado.trades), f"{bot_id} opera distinto"
            for viva, teorica in zip(vividas, esperado.trades):
                assert int(viva["open_ts"]) == int(teorica["entry_ts"])
                assert int(viva["close_ts"]) == int(teorica["exit_ts"])
                assert viva["open_price"] == pytest.approx(teorica["entry_price"])
                assert viva["close_price"] == pytest.approx(teorica["exit_price"])
                assert viva["pnl_net"] == pytest.approx(teorica["pnl"], rel=1e-9)
                assert viva["exit_kind"] == teorica["exit_kind"]
            comparados += len(vividas)
        assert comparados > 0, "sin operaciones no se ha comparado nada"
    finally:
        db.close()


def test_matarlo_a_mitad_y_relanzarlo_no_cambia_nada(
    tmp_path: Path, cfg: Config, catalog
) -> None:
    """Dos mitades tienen que dar el mismo jardín que una sola tirada."""
    velas = _velas()

    entero = tmp_path / "entero"
    config, db, repos, _ = _sembrar(entero, cfg, catalog, n_bots=4, velas=velas)
    try:
        GardenRunner(cfg=config, db=db, verbose=False).run(dry_run=True)
        completo = [
            tuple(f) for f in repos.db.query(
                "SELECT bot_id, open_ts, close_ts, open_price, close_price, pnl_net "
                "FROM trades ORDER BY bot_id, open_ts"
            )
        ]
        curva_completa = [
            tuple(f) for f in repos.db.query(
                "SELECT ts, garden_equity FROM garden_equity ORDER BY ts"
            )
        ]
    finally:
        db.close()

    partido = tmp_path / "partido"
    config2, db2, repos2, _ = _sembrar(partido, cfg, catalog, n_bots=4, velas=velas)
    try:
        # Se corta a mitad, como si alguien matara el proceso…
        GardenRunner(cfg=config2, db=db2, verbose=False).run(
            dry_run=True, max_ticks=BARS // 2
        )
        # …y se relanza desde cero, que es lo que haría un reinicio de verdad.
        GardenRunner(cfg=config2, db=db2, verbose=False).run(dry_run=True)
        troceado = [
            tuple(f) for f in repos2.db.query(
                "SELECT bot_id, open_ts, close_ts, open_price, close_price, pnl_net "
                "FROM trades ORDER BY bot_id, open_ts"
            )
        ]
        curva_troceada = [
            tuple(f) for f in repos2.db.query(
                "SELECT ts, garden_equity FROM garden_equity ORDER BY ts"
            )
        ]
    finally:
        db2.close()

    assert troceado == completo
    assert len(curva_troceada) == len(curva_completa) == BARS
    for (ts_a, eq_a), (ts_b, eq_b) in zip(curva_completa, curva_troceada):
        assert ts_a == ts_b
        assert eq_a == pytest.approx(eq_b, rel=1e-9)


def test_un_tick_reprocesado_no_duplica_ordenes(
    tmp_path: Path, cfg: Config, catalog
) -> None:
    """El índice único de ``orders`` es la red de seguridad del reinicio."""
    velas = _velas(400)
    config, db, repos, _ = _sembrar(tmp_path, cfg, catalog, n_bots=4, velas=velas)
    try:
        runner = GardenRunner(cfg=config, db=db, verbose=False)
        runner.run(dry_run=True, max_ticks=200)
        antes = repos.db.query_one("SELECT COUNT(*) AS n FROM orders")["n"]

        # Se vuelven a procesar diez velas ya vividas, a mano.
        otro = GardenRunner(cfg=config, db=db, verbose=False)
        otro.prepare()
        otro.run(dry_run=True, max_ticks=0)
        otro._load_population(0)
        for i in range(150, 160):
            otro.process_tick(Tick(ts=int(velas.index[i]), generation=1, index=i))

        duplicadas = repos.db.query_one(
            "SELECT COUNT(*) AS n FROM (SELECT bot_id, candle_ts, kind FROM orders "
            "GROUP BY 1,2,3 HAVING COUNT(*) > 1)"
        )["n"]
        assert duplicadas == 0
        assert repos.db.query_one("SELECT COUNT(*) AS n FROM orders")["n"] >= antes
    finally:
        db.close()


def test_el_freno_de_drawdown_mata_al_bot_en_el_acto(
    tmp_path: Path, cfg: Config, catalog
) -> None:
    """Un desplome del 60% en una vela: el bot no llega a fin de generación."""
    velas = _velas(300)
    velas.iloc[250:, :4] *= 0.35
    config, db, repos, genomas = _sembrar(
        tmp_path, cfg, catalog, n_bots=8, velas=velas
    )
    try:
        runner = GardenRunner(cfg=config, db=db, verbose=False)
        runner.run(dry_run=True)
        muertos = repos.db.query(
            "SELECT bot_id, death_cause FROM bots WHERE death_cause = 'DRAWDOWN_BREAKER'"
        )
        frenazos = repos.db.query(
            "SELECT * FROM events WHERE type = 'CIRCUIT_BREAKER'"
        )
        if muertos:
            assert len(frenazos) == len(muertos)
            assert all(f["severity"] == "warn" for f in frenazos)
            for m in muertos:
                assert m["bot_id"] not in runner.bots       # deja de operar
        # Con o sin muertos, el jardín sigue en pie y coherente.
        assert repos.db.query_one("SELECT COUNT(*) AS n FROM garden_equity")["n"] == 300
    finally:
        db.close()


def test_la_curva_del_jardin_lleva_su_espejo_de_buy_and_hold(
    tmp_path: Path, cfg: Config, catalog
) -> None:
    """El benchmark recibe el mismo capital que el jardín, o no compara nada."""
    velas = _velas(300)
    config, db, repos, genomas = _sembrar(tmp_path, cfg, catalog, n_bots=5, velas=velas)
    try:
        GardenRunner(cfg=config, db=db, verbose=False).run(dry_run=True)
        primera = repos.db.query_one("SELECT * FROM garden_equity ORDER BY ts LIMIT 1")
        ultima = repos.db.query_one("SELECT * FROM garden_equity ORDER BY ts DESC LIMIT 1")
        capital = 5 * config.garden.initial_capital_per_bot
        assert primera["garden_equity"] == pytest.approx(capital, rel=1e-6)
        assert primera["benchmark_equity"] == pytest.approx(capital, rel=1e-6)

        # El espejo es buy & hold puro: su retorno es el del activo.
        esperado = capital * float(velas["close"].iloc[-1] / velas["close"].iloc[0])
        assert ultima["benchmark_equity"] == pytest.approx(esperado, rel=1e-6)
        assert ultima["n_alive"] == repos.bots.count_alive()
    finally:
        db.close()


def test_el_jardin_deja_su_estado_marcado_al_parar(
    tmp_path: Path, cfg: Config, catalog
) -> None:
    velas = _velas(50)
    config, db, repos, _ = _sembrar(tmp_path, cfg, catalog, n_bots=3, velas=velas)
    try:
        GardenRunner(cfg=config, db=db, verbose=False).run(dry_run=True)
        assert db.get_meta("status") == "STOPPED"
        assert int(db.get_meta("last_tick_ts")) == int(velas.index[-1])
    finally:
        db.close()
