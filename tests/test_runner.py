"""El jardín vivo: el bucle, la reanudación y la promesa de no divergir.

El test que importa es el de equivalencia: un dry-run sobre un tramo de
histórico tiene que producir exactamente las mismas operaciones que un backtest
del mismo genoma sobre el mismo tramo. Si divergen, una sorpresa en vivo no
significa nada y el dry-run no valida nada.
"""

from __future__ import annotations

import random
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from keepgarden.config import Config
from keepgarden.engine.backtest import run_backtest
from keepgarden.engine.clock import MarketClock, Tick
from keepgarden.engine.runner import GardenRunner, flow_adjusted_peak
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


def _operaciones(repos) -> list[tuple]:
    """El registro completo de operaciones, con todo lo que puede divergir."""
    return [
        tuple(f) for f in repos.db.query(
            "SELECT bot_id, open_ts, close_ts, open_price, close_price, "
            "round(pnl_net, 9), round(fees, 9), exit_kind, holding_bars "
            "FROM trades ORDER BY bot_id, open_ts"
        )
    ]


def _tirada(tmp_path: Path, cfg: Config, catalog, velas, corte: int | None) -> list[tuple]:
    """Corre el jardín entero, opcionalmente matándolo en el tick ``corte``."""
    config, db, repos, _ = _sembrar(tmp_path, cfg, catalog, n_bots=6, velas=velas)
    try:
        if corte is not None:
            GardenRunner(cfg=config, db=db, verbose=False).run(
                dry_run=True, max_ticks=corte
            )
        GardenRunner(cfg=config, db=db, verbose=False).run(dry_run=True)
        return _operaciones(repos)
    finally:
        db.close()


def _tick_a_mitad_de_una_operacion(operaciones, velas) -> int | None:
    """Un tick en el que alguien tiene una posición abierta desde antes.

    Se busca en vez de fijarlo para que el test siga probando lo que dice
    aunque cambien la semilla, los genomas o las velas.
    """
    for i in range(1, len(velas)):
        anterior, actual = int(velas.index[i - 1]), int(velas.index[i])
        for op in operaciones:
            abierta, cerrada = op[1], op[2]
            if abierta <= anterior and cerrada is not None and cerrada >= actual:
                return i
    return None


def test_reanudar_a_mitad_de_una_operacion_no_la_cambia(
    tmp_path: Path, cfg: Config, catalog
) -> None:
    """Matar el jardín con una posición abierta y relanzarlo no puede moverla.

    Lo que un bot lleva en memoria —el ancla del trailing, el 1R, las velas
    aguantadas, la comisión que pagó al entrar— no cabe en ``trades``. Antes de
    ``bot_runtime`` se perdía, y la única operación que cruzaba el corte se
    cerraba con otras comisiones y otra duración. Ver docs/DECISIONS.md D-033.
    """
    velas = _velas()
    entero = _tirada(tmp_path / "entero", cfg, catalog, velas, corte=None)

    corte = _tick_a_mitad_de_una_operacion(entero, velas)
    assert corte is not None, (
        "ningún bot aguanta una posición de una vela a otra: el test no prueba nada"
    )

    partido = _tirada(tmp_path / "partido", cfg, catalog, velas, corte=corte)
    assert partido == entero


# --------------------------------------------------------------------------- #
# El capital del jardín entra y sale: el pico tiene que enterarse              #
# --------------------------------------------------------------------------- #


def _dd(pico: float, capital: float) -> float:
    return max(0.0, 1.0 - capital / pico) if pico > 0 else 0.0


def test_sin_flujo_el_pico_es_el_maximo_de_siempre() -> None:
    assert flow_adjusted_peak(100.0, 100.0, 0.0, 120.0) == 120.0   # sube
    assert flow_adjusted_peak(120.0, 120.0, 0.0, 90.0) == 120.0    # baja, el pico aguanta


def test_el_primer_tick_fija_el_pico() -> None:
    """Sin capital previo no hay nada que escalar: el jardín empieza aquí."""
    assert flow_adjusted_peak(0.0, 0.0, 0.0, 60_000.0) == 60_000.0


def test_morirse_media_poblacion_no_es_un_drawdown() -> None:
    """El bug que encontramos mirando una tirada real.

    60 bots con 1.000 cada uno; mueren 30 y se llevan su capital. No se ha
    perdido un céntimo: el drawdown tiene que seguir en cero.
    """
    pico = flow_adjusted_peak(60_000.0, 60_000.0, -30_000.0, 30_000.0)
    assert _dd(pico, 30_000.0) == pytest.approx(0.0)


def test_nacer_tampoco_es_un_drawdown() -> None:
    """Y al revés: meter capital nuevo no puede maquillar una caída."""
    # el jardín venía cayendo un 25 %
    pico, capital = 40_000.0, 30_000.0
    assert _dd(pico, capital) == pytest.approx(0.25)
    # nacen 5 bots con 1.000 cada uno
    nuevo = flow_adjusted_peak(pico, capital, 5_000.0, 35_000.0)
    assert _dd(nuevo, 35_000.0) == pytest.approx(0.25)


def test_la_perdida_de_verdad_si_aparece() -> None:
    """Lo que no puede hacer el arreglo es esconder una pérdida real."""
    pico = flow_adjusted_peak(60_000.0, 60_000.0, 0.0, 48_000.0)
    assert _dd(pico, 48_000.0) == pytest.approx(0.20)


def test_muerte_y_perdida_a_la_vez_deja_ver_solo_la_perdida() -> None:
    """Mueren 30 bots (sale la mitad del capital) y los 30 que quedan pierden
    un 20 %. El drawdown tiene que ser 20 %, no 60 %."""
    pico = flow_adjusted_peak(60_000.0, 60_000.0, -30_000.0, 24_000.0)
    assert _dd(pico, 24_000.0) == pytest.approx(0.20)


def test_si_se_va_todo_el_capital_no_revienta() -> None:
    """Jardín extinguido: ni división por cero ni un drawdown inventado."""
    pico = flow_adjusted_peak(60_000.0, 60_000.0, -60_000.0, 0.0)
    assert pico >= 0.0
    assert _dd(pico, 0.0) == pytest.approx(0.0)


def test_la_poda_masiva_no_dispara_el_freno_del_jardin(
    tmp_path: Path, cfg: Config, catalog
) -> None:
    """El mismo bug, de punta a punta y sobre datos persistidos.

    Se busca el tick en el que más bots desaparecen y se comprueba que el
    drawdown del jardín no empeora más de lo que realmente perdieron los que
    siguen vivos. Antes de D-034 aquí había un salto de 0.165 a 0.672 sin que
    nadie hubiera perdido un céntimo, y ese salto recortaba los nacimientos a
    la mitad durante el resto de la simulación.
    """
    velas = _velas(1400)
    config, db, repos, _ = _sembrar(tmp_path, cfg, catalog, n_bots=24, velas=velas)
    config = _incubadora_pequeña(
        replace(config, garden=replace(config.garden, ticks_per_generation=120))
    )
    try:
        GardenRunner(cfg=config, db=db, verbose=False).run(dry_run=True)

        curva = repos.db.query(
            "SELECT ts, n_alive, garden_drawdown FROM garden_equity ORDER BY ts"
        )
        # El tick en el que más bots desaparecen de golpe.
        caidas = [
            (curva[i - 1]["n_alive"] - curva[i]["n_alive"], i)
            for i in range(1, len(curva))
        ]
        bajada, i = max(caidas)
        assert bajada > 0, "en esta tirada no muere nadie: el test no prueba nada"

        antes, despues = curva[i - 1], curva[i]
        # Lo que de verdad perdieron los que siguen vivos en los dos ticks.
        def capital(ts: int, ids: set[str]) -> float:
            filas = repos.db.query(
                "SELECT bot_id, equity FROM equity_snapshots WHERE ts = ?", (int(ts),)
            )
            return sum(f["equity"] for f in filas if f["bot_id"] in ids)

        ids_despues = {
            f["bot_id"] for f in repos.db.query(
                "SELECT bot_id FROM equity_snapshots WHERE ts = ?", (int(despues["ts"]),)
            )
        }
        cap_antes = capital(antes["ts"], ids_despues)
        cap_despues = capital(despues["ts"], ids_despues)
        perdida = max(0.0, 1.0 - cap_despues / cap_antes) if cap_antes > 0 else 0.0

        empeora = despues["garden_drawdown"] - antes["garden_drawdown"]
        assert empeora <= perdida + 1e-6, (
            f"el drawdown del jardín empeora {empeora:.3f} cuando los "
            f"supervivientes sólo perdieron {perdida:.3f}: la poda se está "
            f"contando como pérdida"
        )
    finally:
        db.close()


def _curva_dd(carpeta: Path, cfg: Config, catalog, velas, corte: int | None):
    """Corre el jardín, opcionalmente matándolo en el tick ``corte``, y
    devuelve la curva de drawdown que ha quedado escrita."""
    config, db, repos, _ = _sembrar(carpeta, cfg, catalog, n_bots=26, velas=velas)
    config = _incubadora_pequeña(
        replace(config, garden=replace(config.garden, ticks_per_generation=120))
    )
    try:
        if corte is not None:
            GardenRunner(cfg=config, db=db, verbose=False).run(
                dry_run=True, max_ticks=corte
            )
        GardenRunner(cfg=config, db=db, verbose=False).run(dry_run=True)
        return [
            (f["ts"], round(f["garden_drawdown"], 9)) for f in repos.db.query(
                "SELECT ts, garden_drawdown FROM garden_equity ORDER BY ts")
        ], repos.db.query_one(
            "SELECT COUNT(*) AS n FROM bots WHERE death_cause IS NOT NULL")["n"]
    finally:
        db.close()


def test_reanudar_al_cerrar_generacion_no_cierra_otra_de_golpe(
    tmp_path: Path, cfg: Config, catalog
) -> None:
    """El contador de la generación en curso cuenta ticks, no filas.

    ``_ticks_in_current_generation`` miraba cuántas filas tenía la última
    generación de ``garden_equity``. Si el jardín se para justo al cerrar una,
    esa última generación ya estaba completa, así que al reanudar el contador
    arrancaba lleno y el primer tick cerraba otra generación en el acto. El
    jardín reanudado iba adelantado para siempre.
    """
    velas = _velas(600)
    config, db, repos, _ = _sembrar(tmp_path, cfg, catalog, n_bots=6, velas=velas)
    config = replace(config, garden=replace(config.garden, ticks_per_generation=120))
    try:
        GardenRunner(cfg=config, db=db, verbose=False).run(dry_run=True, max_ticks=120)
        cerradas = repos.db.query_one(
            "SELECT COUNT(*) AS n FROM generations WHERE ended_ts IS NOT NULL")["n"]

        GardenRunner(cfg=config, db=db, verbose=False).run(dry_run=True, max_ticks=1)
        despues = repos.db.query_one(
            "SELECT COUNT(*) AS n FROM generations WHERE ended_ts IS NOT NULL")["n"]
        assert despues == cerradas, (
            "un solo tick tras reanudar ha cerrado una generación entera"
        )
    finally:
        db.close()


def _censo(carpeta: Path, cfg: Config, catalog, velas, corte: int | None):
    """Corre el jardín, opcionalmente partido, y devuelve quién vivió y murió."""
    config, db, repos, _ = _sembrar(carpeta, cfg, catalog, n_bots=26, velas=velas)
    config = _incubadora_pequeña(
        replace(config, garden=replace(config.garden, ticks_per_generation=120))
    )
    try:
        if corte is not None:
            GardenRunner(cfg=config, db=db, verbose=False).run(
                dry_run=True, max_ticks=corte
            )
        GardenRunner(cfg=config, db=db, verbose=False).run(dry_run=True)
        return [
            tuple(f) for f in repos.db.query(
                "SELECT bot_id, born_generation, died_generation, death_cause "
                "FROM bots ORDER BY born_generation, bot_id"
            )
        ]
    finally:
        db.close()


def test_reanudar_no_cambia_a_quien_nace(
    tmp_path: Path, cfg: Config, catalog
) -> None:
    """Invariante 7 del CLAUDE.md, en el punto donde se rompía.

    El generador aleatorio de la evolución se sembraba de nuevo en cada
    arranque, así que una generación criada tras un reinicio veía otro flujo de
    azar: otros padres, otras mutaciones, otro jardín. El test de reanudación
    que ya existía no lo veía porque nunca cerraba una generación.
    """
    velas = _velas(700)
    seguido = _censo(tmp_path / "entero", cfg, catalog, velas, None)
    assert any(f[1] > 0 for f in seguido), "no nace nadie: el test no prueba nada"

    partido = _censo(tmp_path / "partido", cfg, catalog, velas, 120)
    assert partido == seguido


def test_reanudar_despues_de_criar_sigue_el_mismo_hilo_de_azar(
    tmp_path: Path, cfg: Config, catalog
) -> None:
    """El corte cae cuando ya se ha criado, que es cuando el azar importa.

    ``Population`` se construye con ``random.Random(cfg.seed)`` en cada
    arranque. Si el flujo de azar no se guarda, la generación que se cría tras
    un reinicio vuelve al principio del flujo: otros padres, otras mutaciones,
    otro jardín. Ver docs/DECISIONS.md D-035.
    """
    velas = _velas(900)
    seguido = _censo(tmp_path / "entero", cfg, catalog, velas, None)
    criados = [f for f in seguido if f[1] > 0]
    assert criados, "no cría nadie: el test no prueba nada"

    # 600 ticks = cinco generaciones de 120: ya ha habido cosechas antes del corte.
    partido = _censo(tmp_path / "partido", cfg, catalog, velas, 600)
    assert partido == seguido


def test_el_pico_del_jardin_sobrevive_al_reinicio(
    tmp_path: Path, cfg: Config, catalog
) -> None:
    """El pico es estado, no un derivado de ``garden_equity``.

    El corte cae **justo al cerrar una generación**, que es el único momento en
    el que hay capital entrando y saliendo. Si ese flujo no se persiste con la
    generación, al reanudar el jardín confunde a sus muertos con una pérdida y
    la curva de drawdown se parte en dos.
    """
    velas = _velas(700)
    seguido, muertos = _curva_dd(tmp_path / "entero", cfg, catalog, velas, None)
    assert muertos, "en esta tirada no muere nadie: el test no prueba nada"

    # 120 velas por generación: el corte cae en el primer cierre de generación.
    troceado, _ = _curva_dd(tmp_path / "partido", cfg, catalog, velas, 120)

    assert troceado == seguido


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


def test_un_fill_ya_registrado_no_vuelve_a_escribir_la_operacion(
    tmp_path: Path, cfg: Config, catalog
) -> None:
    """El índice único de 'orders' protege también a 'trades'.

    Si el mismo relleno se procesa dos veces —un reinicio mal cortado—, la
    segunda vez la orden ya está y no puede nacer una operación fantasma.
    """
    from keepgarden.engine.broker import Fill
    from keepgarden.types import OrderKind, Side

    velas = _velas(200)
    config, db, repos, _ = _sembrar(tmp_path, cfg, catalog, n_bots=1, velas=velas)
    try:
        runner = GardenRunner(cfg=config, db=db, verbose=False)
        runner.prepare()
        runner._load_population(0)
        estado = next(iter(runner.bots.values()))
        tick = Tick(ts=int(velas.index[10]), generation=1, index=10)
        relleno = Fill(
            bot_id=estado.bot_id, candle_ts=int(velas.index[9]),
            fill_ts=int(velas.index[10]), kind=OrderKind.ENTRY, side=Side.LONG,
            price=float(velas["open"].iloc[10]), reference_price=float(velas["open"].iloc[10]),
            slippage=0.0, amount=0.01, notional=10.0, fee=0.01,
        )

        runner._record_fill(estado, relleno, None, tick, None)
        assert repos.db.query_one("SELECT COUNT(*) AS n FROM trades")["n"] == 1
        assert repos.db.query_one("SELECT COUNT(*) AS n FROM orders")["n"] == 1
        # La orden quedó atada a su operación.
        assert repos.db.query_one("SELECT trade_id FROM orders")["trade_id"] is not None

        # Segunda vuelta con el mismo relleno: la base no se mueve.
        runner._record_fill(estado, relleno, None, tick, None)
        assert repos.db.query_one("SELECT COUNT(*) AS n FROM trades")["n"] == 1, (
            "operación fantasma: la vela ya se había procesado"
        )
        assert repos.db.query_one("SELECT COUNT(*) AS n FROM orders")["n"] == 1
    finally:
        db.close()


def test_el_freno_de_drawdown_mata_al_bot_en_el_acto(
    tmp_path: Path, cfg: Config, catalog
) -> None:
    """Un desplome del 60% en una vela: el bot no llega a fin de generación."""
    velas = _velas(300)
    velas.iloc[250:, :4] *= 0.35
    config, db, repos, _genomas = _sembrar(
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
            vivos = {
                f["bot_id"] for f in repos.db.query("SELECT bot_id FROM bot_runtime")
            }
            for m in muertos:
                assert m["bot_id"] not in runner.bots       # deja de operar
                assert m["bot_id"] not in vivos             # y deja de tener estado
        # Con o sin muertos, el jardín sigue en pie y coherente.
        assert repos.db.query_one("SELECT COUNT(*) AS n FROM garden_equity")["n"] == 300
    finally:
        db.close()


def test_la_curva_del_jardin_lleva_su_espejo_de_buy_and_hold(
    tmp_path: Path, cfg: Config, catalog
) -> None:
    """El benchmark recibe el mismo capital que el jardín, o no compara nada."""
    velas = _velas(300)
    config, db, repos, _genomas = _sembrar(tmp_path, cfg, catalog, n_bots=5, velas=velas)
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


def test_un_jardin_multisimbolo_opera_cada_bot_en_su_mercado(
    tmp_path: Path, cfg: Config, catalog
) -> None:
    """Cada bot opera el mercado de su genoma, y el reloj lo marca el primario."""
    import json

    from keepgarden.data.store import CandleStore, SeriesKey

    btc = _velas(600, seed=21)
    # Un segundo mercado con vida propia y, a propósito, sin las 100 primeras
    # velas: los mercados no nacen todos el mismo día.
    eth = _velas(600, seed=22).iloc[100:]

    config, db, repos, _genomas = _sembrar(tmp_path, cfg, catalog, n_bots=4, velas=btc)
    try:
        store = CandleStore(cache_dir=config.path(config.storage.cache_dir))
        store.append(
            SeriesKey(config.market.venue, "ETH/USDT", config.market.timeframe),
            eth.reset_index(),
        )
        # Cuatro bots más, esta vez sobre ETH.
        mercado_eth = MarketSpec(
            venue=config.market.venue, symbol="ETH/USDT",
            timeframe=config.market.timeframe,
        )
        for g in random_population(4, mercado_eth, config, catalog, random.Random(8)):
            repos.bots.create(g, generation=0, initial_capital=1000.0)
        db.set_meta("symbols", json.dumps(["BTC/USDT", "ETH/USDT"]))

        runner = GardenRunner(cfg=config, db=db, verbose=False)
        runner.run(dry_run=True)

        assert set(runner.series) == {"BTC/USDT", "ETH/USDT"}
        # ETH empieza 100 velas más tarde: esas quedan sin alinear.
        assert int((runner.series["ETH/USDT"].local < 0).sum()) == 100

        por_mercado = repos.db.query(
            "SELECT g.symbol, COUNT(*) AS ops FROM trades t "
            "JOIN bots b ON b.bot_id = t.bot_id "
            "JOIN genomes g ON g.genome_id = b.genome_id GROUP BY g.symbol"
        )
        assert {str(f["symbol"]) for f in por_mercado} <= {"BTC/USDT", "ETH/USDT"}

        # Ninguna operación de un bot de ETH puede tener un precio de BTC.
        for fila in repos.db.query(
            "SELECT t.open_price FROM trades t JOIN bots b ON b.bot_id = t.bot_id "
            "JOIN genomes g ON g.genome_id = b.genome_id WHERE g.symbol = 'ETH/USDT'"
        ):
            assert float(eth["low"].min()) <= float(fila["open_price"]) <= float(eth["high"].max())

        # El reloj lo marca el primario: hay un punto de curva por vela de BTC.
        assert repos.db.query_one(
            "SELECT COUNT(*) AS n FROM garden_equity"
        )["n"] == len(btc)
    finally:
        db.close()


def test_un_bot_sin_vela_en_su_mercado_no_opera_ni_se_revalora(
    tmp_path: Path, cfg: Config, catalog
) -> None:
    """Inventar precio en un mercado parado es inventar rentabilidad."""
    import json

    from keepgarden.data.store import CandleStore, SeriesKey

    btc = _velas(300, seed=31)
    # ETH sólo tiene las velas pares: la mitad de los ticks no le tocan.
    eth = _velas(300, seed=32).iloc[::2]

    config, db, repos, _ = _sembrar(tmp_path, cfg, catalog, n_bots=2, velas=btc)
    try:
        store = CandleStore(cache_dir=config.path(config.storage.cache_dir))
        store.append(
            SeriesKey(config.market.venue, "ETH/USDT", config.market.timeframe),
            eth.reset_index(),
        )
        mercado_eth = MarketSpec(
            venue=config.market.venue, symbol="ETH/USDT",
            timeframe=config.market.timeframe,
        )
        genoma = random_population(1, mercado_eth, config, catalog, random.Random(4))[0]
        bot_eth = repos.bots.create(genoma, generation=0, initial_capital=1000.0)
        db.set_meta("symbols", json.dumps(["BTC/USDT", "ETH/USDT"]))

        runner = GardenRunner(cfg=config, db=db, verbose=False)
        runner.run(dry_run=True)

        # Tiene un punto de curva por cada tick del reloj, aunque su mercado
        # sólo tenga la mitad de las velas: se queda quieto, no desaparece.
        puntos = repos.db.query(
            "SELECT COUNT(*) AS n FROM equity_snapshots WHERE bot_id = ?", (bot_eth,)
        )[0]["n"]
        assert puntos == len(btc)

        # Y ninguna de sus operaciones cae en un momento sin vela de ETH.
        momentos = set(int(t) for t in eth.index)
        for fila in repos.db.query(
            "SELECT open_ts FROM trades WHERE bot_id = ?", (bot_eth,)
        ):
            assert int(fila["open_ts"]) in momentos
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# La ventana deslizante del fitness vivo (D-030 / D-031)                       #
# --------------------------------------------------------------------------- #


def _incubadora_pequeña(config: Config) -> Config:
    """Recorta el walk-forward para que quepa en unos cientos de velas.

    Con la partición real hacen falta 21.390 velas y ninguna generación
    llegaría a criar en un test.
    """
    return replace(
        config,
        incubator=replace(
            config.incubator,
            n_folds=2, train_bars=200, validation_bars=60,
            embargo_bars=10, holdout_bars=80, min_trades_per_fold=1,
        ),
    )


def _ultima_generacion_viva(repos) -> int:
    return repos.db.query_one(
        "SELECT MAX(generation) AS g FROM bot_metrics WHERE scope = 'live'"
    )["g"]


def _ventana_de(repos, generacion: int) -> tuple[int, int]:
    """Los dos extremos temporales de una generación ya cerrada."""
    fila = repos.generations.get(generacion)
    anterior = repos.generations.get(generacion - 1)
    desde = int(anterior["ended_ts"]) if anterior and anterior["ended_ts"] else 0
    return desde, int(fila["ended_ts"])


def _correr_con_ventana(
    carpeta: Path, cfg: Config, catalog, velas: pd.DataFrame, generaciones: int
):
    """Corre el mismo jardín con una ventana de N generaciones y devuelve las
    métricas vivas de la última generación cerrada."""
    config, db, repos, _ = _sembrar(carpeta, cfg, catalog, n_bots=6, velas=velas)
    try:
        config = _incubadora_pequeña(
            replace(
                config,
                garden=replace(config.garden, ticks_per_generation=120),
                fitness=replace(config.fitness, live_window_generations=generaciones),
            )
        )
        GardenRunner(cfg=config, db=db, verbose=False).run(dry_run=True)
        ultima = _ultima_generacion_viva(repos)
        return [
            dict(f) for f in repos.db.query(
                "SELECT bot_id, n_trades, fitness FROM bot_metrics "
                "WHERE scope = 'live' AND generation = ?",
                (ultima,),
            )
        ]
    finally:
        db.close()


def test_la_ventana_deslizante_reune_la_evidencia_que_una_semana_no_da(
    tmp_path: Path, cfg: Config, catalog
) -> None:
    """El fondo de D-030: midiendo sólo la última generación casi nadie llega
    al mínimo de operaciones, y el jardín se queda sin fitness que comparar."""
    velas = _velas()

    una = _correr_con_ventana(tmp_path / "una", cfg, catalog, velas, generaciones=1)
    cuatro = _correr_con_ventana(tmp_path / "cuatro", cfg, catalog, velas, generaciones=4)

    assert una and cuatro
    ops_una = sum(f["n_trades"] or 0 for f in una)
    ops_cuatro = sum(f["n_trades"] or 0 for f in cuatro)
    assert ops_cuatro > ops_una, "la ventana larga tiene que acumular más evidencia"

    definidos_una = sum(1 for f in una if f["fitness"] is not None)
    definidos_cuatro = sum(1 for f in cuatro if f["fitness"] is not None)
    assert definidos_cuatro >= definidos_una


def test_la_ventana_no_crece_mas_de_lo_necesario(
    tmp_path: Path, cfg: Config, catalog
) -> None:
    """Un bot que ya tiene evidencia se juzga por su última generación.

    Con el mínimo en una operación, la ventana no debe tirar de ningún tramo
    anterior: lo que se mide es exactamente lo que se cerró esta generación.
    """
    velas = _velas()
    config, db, repos, _ = _sembrar(tmp_path, cfg, catalog, n_bots=6, velas=velas)
    try:
        config = _incubadora_pequeña(
            replace(
                config,
                garden=replace(config.garden, ticks_per_generation=120),
                fitness=replace(config.fitness, min_trades=1, live_window_generations=4),
            )
        )
        GardenRunner(cfg=config, db=db, verbose=False).run(dry_run=True)

        ultima = _ultima_generacion_viva(repos)
        desde, hasta = _ventana_de(repos, ultima)
        comprobados = 0
        for fila in repos.db.query(
            "SELECT bot_id, n_trades FROM bot_metrics WHERE scope = 'live' "
            "AND generation = ? AND n_trades > 0",
            (ultima,),
        ):
            cerradas = repos.db.query_one(
                "SELECT COUNT(*) AS n FROM trades WHERE bot_id = ? "
                "AND close_ts > ? AND close_ts <= ?",
                (fila["bot_id"], desde, hasta),
            )["n"]
            assert int(fila["n_trades"]) == cerradas, (
                f"{fila['bot_id']} arrastra tramos que ya no necesitaba"
            )
            comprobados += 1
        assert comprobados, "alguien tiene que haber operado"
    finally:
        db.close()


def test_el_contador_de_inactividad_mira_solo_la_ultima_generacion(
    tmp_path: Path, cfg: Config, catalog
) -> None:
    """Si mirara la ventana entera, un bot parado hace tres semanas seguiría
    contando como activo y no moriría nunca por inactivo."""
    velas = _velas()
    config, db, repos, _ = _sembrar(tmp_path, cfg, catalog, n_bots=6, velas=velas)
    try:
        config = _incubadora_pequeña(
            replace(
                config,
                garden=replace(config.garden, ticks_per_generation=120),
                fitness=replace(config.fitness, live_window_generations=4),
            )
        )
        GardenRunner(cfg=config, db=db, verbose=False).run(dry_run=True)

        ultima = _ultima_generacion_viva(repos)
        desde, hasta = _ventana_de(repos, ultima)
        comprobados = 0
        for fila in repos.bots.alive():
            if int(fila["born_generation"]) >= ultima:
                continue        # nació al cerrarla: no ha vivido la generación
            # Lo que cuenta el runner son las operaciones CERRADAS dentro de la
            # generación, que es lo que tiene en portfolio.closed_trades.
            cerradas = repos.db.query_one(
                "SELECT COUNT(*) AS n FROM trades WHERE bot_id = ? "
                "AND close_ts > ? AND close_ts <= ?",
                (fila["bot_id"], desde, hasta),
            )["n"]
            if cerradas == 0:
                assert int(fila["idle_generations"]) >= 1, (
                    f"{fila['bot_id']} no cerró nada en la {ultima} y sigue a cero"
                )
            else:
                assert int(fila["idle_generations"]) == 0
            comprobados += 1
        assert comprobados, "sin bots vivos no se ha comprobado nada"
    finally:
        db.close()


def test_el_jardin_guarda_copias_de_si_mismo(
    tmp_path: Path, cfg: Config, catalog
) -> None:
    """Un jardín es un archivo: copiarlo es todo el respaldo que necesita."""
    velas = _velas(300)
    config, db, repos, _ = _sembrar(tmp_path, cfg, catalog, n_bots=3, velas=velas)
    try:
        # Generaciones cortas y una copia por generación, para no correr 1.680
        # velas sólo por ver un archivo.
        config = replace(
            config,
            garden=replace(config.garden, ticks_per_generation=60),
            storage=replace(config.storage, snapshot_every_generations=1),
        )
        GardenRunner(cfg=config, db=db, verbose=False).run(dry_run=True, max_ticks=130)

        copias = sorted((config.db_file.parent / "snapshots").glob("*.db"))
        assert len(copias) == 2, "una copia por generación cerrada"
        assert copias[0].name.startswith("garden_")
        # Y la copia es un jardín de verdad, no un archivo vacío.
        copia = open_database(copias[0], read_only=True, create=False)
        try:
            assert copia.query_one("SELECT COUNT(*) AS n FROM bots")["n"] == 3
        finally:
            copia.close()
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
