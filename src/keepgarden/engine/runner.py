"""El bucle del jardín: un solo proceso, un solo hilo, indefinidamente.

Con timeframe de 1h y poblaciones de cientos de bots, un tick tarda
milisegundos y hay 3.600 segundos entre velas. No hace falta concurrencia aquí;
la paralelización vive en la incubadora.

La secuencia de cada vela es la misma que la del backtest —de hecho la decisión
la toma la misma función, ``backtest.decide_order``— porque si el vivo y el
backtest pudieran divergir, una sorpresa en vivo no significaría nada.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import numpy as np

from ..config import Config
from ..data.candles import timeframe_ms
from ..evaluation.metrics import Metrics, compute_metrics
from ..genome.schema import Genome, MarketSpec
from ..types import (
    AlertKind,
    BotId,
    BotStatus,
    DeathCause,
    EventType,
    GardenStatus,
    OrderKind,
    PriceField,
    Side,
    SizingKind,
    Timestamp,
)
from .backtest import (
    DEFAULT_ATR_PARAMS,
    DEFAULT_VOL_PARAMS,
    auxiliary_series,
    decide_order,
)
from .broker import Fill, OrderRequest, PaperBroker
from .clock import MarketClock, Tick
from .portfolio import Portfolio, Position

if TYPE_CHECKING:  # pragma: no cover
    import pandas as pd

    from ..data.indicators import IndicatorCache
    from ..evolution.population import GenerationOutcome, Population
    from ..genome.catalog import GeneCatalog
    from ..storage.db import Database
    from ..storage.repositories import Repositories

#: Cada cuántos ticks se imprime una línea de progreso en el dry-run.
PROGRESS_EVERY = 168


@dataclass(slots=True)
class _BotState:
    """Todo lo que un bot vivo necesita para operar un tick.

    Las señales se compilan una vez sobre toda la serie y se indexan por tick.
    Es correcto porque los indicadores son causales —está probado en el hito
    2— y es lo que hace que un dry-run de seis meses tarde minutos.
    """

    bot_id: BotId
    genome: Genome
    portfolio: Portfolio
    broker: PaperBroker
    codes: np.ndarray
    stop_atr: np.ndarray
    realized_vol: np.ndarray
    warmup: int
    cooldown_ms: int
    #: Primer índice en el que puede operar: nadie opera en el tick en el que nace.
    first_index: int
    #: Unidades del benchmark compradas con su capital inicial. Así la cartera
    #: espejo recibe exactamente las mismas entradas y salidas de dinero que el
    #: jardín, y el alfa compara dos cosas comparables.
    benchmark_units: float = 0.0
    #: Curva de la ventana que se está viviendo, para las métricas de generación.
    equity_window: list[float] = field(default_factory=list)
    #: Id de la fila de ``trades`` abierta por lado.
    open_trades: dict[str, int] = field(default_factory=dict)
    fees_at_window_start: float = 0.0


@dataclass(slots=True)
class GardenRunner:
    """Orquestador del jardín vivo."""

    cfg: Config
    db: Database
    status: GardenStatus = GardenStatus.STOPPED

    # -- estado interno, todo reconstruible desde SQLite -------------------- #
    repos: Repositories | None = None
    catalog: GeneCatalog | None = None
    candles: pd.DataFrame | None = None
    features: IndicatorCache | None = None
    clock: MarketClock | None = None
    population: Population | None = None
    bots: dict[BotId, _BotState] = field(default_factory=dict)
    generation: int = 0
    ticks_done: int = 0
    verbose: bool = True

    _ts: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype="int64"), repr=False)
    _open: np.ndarray = field(default_factory=lambda: np.zeros(0), repr=False)
    _high: np.ndarray = field(default_factory=lambda: np.zeros(0), repr=False)
    _low: np.ndarray = field(default_factory=lambda: np.zeros(0), repr=False)
    _close: np.ndarray = field(default_factory=lambda: np.zeros(0), repr=False)
    _volume: np.ndarray = field(default_factory=lambda: np.zeros(0), repr=False)
    _atr: np.ndarray = field(default_factory=lambda: np.zeros(0), repr=False)
    _gap_ahead: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=bool), repr=False)
    _benchmark_units: float = 0.0
    _garden_peak: float = 0.0
    _pending_events: list[dict[str, Any]] = field(default_factory=list, repr=False)
    _window_start_index: int = 0

    # ---------------------------------------------------------------- arranque

    def prepare(self) -> None:
        """Carga velas, catálogo, población y reloj. Idempotente."""
        import random

        from ..data.indicators import IndicatorCache
        from ..data.store import CandleStore, SeriesKey
        from ..evolution.population import Population
        from ..gardener.apply import effective_config
        from ..genome.catalog import load_catalog
        from ..storage.repositories import Repositories

        if self.repos is None:
            self.repos = Repositories.open(self.db)
        # Los ajustes del jardinero viven en la base, no en el YAML: el motor
        # arranca ya con ellos aplicados (docs/GARDENER_PROTOCOL.md §Límites).
        self.cfg = effective_config(self.cfg, self.db)
        self.catalog = load_catalog(self.cfg.path("config/genes.yaml"))

        market = MarketSpec(
            venue=self.cfg.market.venue,
            symbol=self.db.get_meta("symbol") or self.cfg.primary_symbol,
            timeframe=self.db.get_meta("timeframe") or self.cfg.market.timeframe,
        )
        store = CandleStore(cache_dir=self.cfg.path(self.cfg.storage.cache_dir))
        velas = store.load(SeriesKey(market.venue, market.symbol, market.timeframe))
        if velas.empty:
            raise RuntimeError(
                f"no hay velas de {market.symbol} en caché: "
                f"'keepgarden data backfill' antes de arrancar el jardín"
            )
        contexto = {}
        for tf in self.cfg.market.context_timeframes:
            serie = store.load(SeriesKey(market.venue, market.symbol, tf))
            if not serie.empty:
                contexto[tf] = serie

        self.candles = velas
        self.features = IndicatorCache(
            candles=velas, context=contexto, catalog=self.catalog,
            timeframe=market.timeframe,
        )
        self._load_arrays(market.timeframe)

        self.population = Population(
            cfg=self.cfg, catalog=self.catalog, db=self.db,
            rng=random.Random(self.cfg.seed), repos=self.repos,
        )
        self.generation = int(self.db.get_meta("current_generation") or 0)

    def _load_arrays(self, timeframe: str) -> None:
        data = self.candles
        assert data is not None
        n = len(data)
        self._ts = np.asarray(data.index, dtype="int64")
        self._open = data["open"].to_numpy(dtype="float64")
        self._high = data["high"].to_numpy(dtype="float64")
        self._low = data["low"].to_numpy(dtype="float64")
        self._close = data["close"].to_numpy(dtype="float64")
        self._volume = data["volume"].to_numpy(dtype="float64")
        self._atr = auxiliary_series(
            self.features, "ATR", DEFAULT_ATR_PARAMS, PriceField.HLC3, n
        )
        hueco = np.zeros(n, dtype=bool)
        if n > 1:
            hueco[:-1] = np.diff(self._ts) > timeframe_ms(timeframe)  # type: ignore[arg-type]
        self._gap_ahead = hueco

    def _index_of(self, ts: Timestamp) -> int:
        """Posición de una vela en la serie. -1 si no está."""
        pos = int(np.searchsorted(self._ts, int(ts)))
        if pos < len(self._ts) and int(self._ts[pos]) == int(ts):
            return pos
        return -1

    # ------------------------------------------------------------- población

    def _build_state(self, bot_id: BotId, genome: Genome, first_index: int) -> _BotState:
        """Compila las señales de un bot y reconstruye su cartera."""
        from ..genome.compile import compile_genome

        assert self.repos is not None and self.candles is not None
        fila = self.repos.bots.get(bot_id)
        assert fila is not None
        miembros = self.repos.bots.alive_genomes() if genome.is_ensemble else None
        compilado = compile_genome(genome, self.candles, self.features, members=miembros)

        n = len(self.candles)
        stop_atr = self._atr
        if genome.risk.stop.atr_ref and genome.risk.stop.atr_ref in compilado.feature_arrays:
            stop_atr = np.nan_to_num(
                compilado.feature_arrays[genome.risk.stop.atr_ref], nan=0.0
            )
        realized_vol = (
            auxiliary_series(
                self.features, "REALIZED_VOL", DEFAULT_VOL_PARAMS, PriceField.CLOSE, n
            )
            if genome.risk.sizing is SizingKind.VOL_TARGET
            else np.zeros(n, dtype="float64")
        )

        port = Portfolio(
            bot_id=bot_id,
            cash=float(fila["cash"]),
            initial_capital=float(fila["initial_capital"]),
            peak_equity=float(fila["peak_equity"]),
        )
        estado = _BotState(
            bot_id=bot_id,
            genome=genome,
            portfolio=port,
            broker=PaperBroker(frictions=self.cfg.frictions),
            codes=compilado.codes,
            stop_atr=stop_atr,
            realized_vol=realized_vol,
            warmup=compilado.warmup_bars,
            cooldown_ms=max(0, int(genome.risk.cooldown_bars))
            * timeframe_ms(genome.market.timeframe),
            first_index=first_index,
        )
        self._restore_positions(estado)
        return estado

    def _restore_positions(self, estado: _BotState) -> None:
        """Vuelve a poner en pie las posiciones abiertas que dejó la caída."""
        assert self.repos is not None
        for fila in self.repos.trades.open_positions(estado.bot_id):
            lado = Side(str(fila["side"]))
            estado.portfolio.positions.append(
                Position(
                    side=lado,
                    amount=float(fila["amount"]),
                    entry_price=float(fila["open_price"]),
                    entry_ts=int(fila["open_ts"]),
                    stop_price=fila["stop_price"],
                    take_price=fila["take_price"],
                    entry_atr=float(self._atr[max(0, self._index_of(int(fila["open_ts"])))]),
                )
            )
            estado.open_trades[str(lado)] = int(fila["trade_id"])

    def _load_population(self, first_index: int) -> None:
        assert self.repos is not None
        self.bots = {}
        genomas = self.repos.bots.alive_genomes()
        for bot_id, genoma in genomas.items():
            self.bots[bot_id] = self._build_state(bot_id, genoma, first_index)

    # ------------------------------------------------------------------ bucle

    def run(
        self,
        *,
        dry_run: bool = False,
        speed: float = 0.0,
        max_ticks: int | None = None,
        start_index: int | None = None,
    ) -> None:
        """Bucle principal. Ver el pseudocódigo de docs/EVOLUTION.md.

        Al arrancar carga el estado desde SQLite, recupera las velas perdidas
        mientras estaba caído y entra en el bucle normal.

        El jardín puede morir y resucitar sin perder nada: cada tick se escribe
        en una transacción que incluye ``last_tick_ts``, así que o el tick
        entero está en la base o no está. Reprocesar una vela tampoco duplica
        nada: ``orders`` tiene índice único sobre ``(bot_id, candle_ts, kind)``.

        ``start_index`` es por dónde empieza un dry-run que arranca de cero. Un
        jardín ya iniciado lo ignora: continúa donde lo dejó, que es el único
        comportamiento que hace que matar el proceso sea inofensivo.
        """
        if self.candles is None:
            self.prepare()
        assert self.repos is not None and self.candles is not None

        ultimo = self.db.get_meta("last_tick_ts")
        ultimo_ts = int(ultimo) if ultimo else None
        if ultimo_ts is None and start_index:
            # El reloj salta lo anterior porque ya "lo ha visto": así el replay
            # empieza donde se le pide sin que el runner tenga dos índices.
            inicio = max(0, min(int(start_index), len(self._ts) - 1))
            ultimo_ts = int(self._ts[inicio - 1]) if inicio > 0 else None
        arranque = (self._index_of(ultimo_ts) + 1) if ultimo_ts is not None else 0
        self._load_population(max(0, arranque))
        self._restore_benchmark(arranque)
        if ultimo and arranque > 0:
            self._rebuild_pending_orders(arranque - 1)

        self.clock = MarketClock(
            cfg=self.cfg,
            start_generation=self.generation,
            last_processed_ts=ultimo_ts,
            ticks_in_generation=self._ticks_in_current_generation(),
        )
        self._window_start_index = max(0, arranque)

        self.status = GardenStatus.RUNNING
        self.db.set_meta("status", str(self.status))

        if dry_run:
            fuente = self.clock.replay(self.candles, speed)
        else:
            self.clock.fetch = self._fetch_live
            self.clock.on_venue_failure = self._venue_failed
            self.catch_up()
            fuente = self.clock.ticks()

        procesados = 0
        try:
            for tick in fuente:
                self.process_tick(tick)
                procesados += 1
                self.ticks_done += 1
                if max_ticks is not None and procesados >= max_ticks:
                    break
        except KeyboardInterrupt:  # pragma: no cover - salida a mano
            print("\nparando el jardín…")
        finally:
            self.shutdown()

    def _rebuild_pending_orders(self, t: int) -> None:
        """Reconstruye la cola de órdenes que dejó la caída.

        Una orden decidida en la vela ``t`` se rellena en la apertura de
        ``t+1``. Si el proceso muere entre medias, esa intención vive sólo en
        memoria y al reanudar el bot se habría saltado su entrada. Volver a
        decidir sobre ``t`` la reproduce exactamente: la decisión es pura y la
        cartera se ha restaurado tal y como quedó al cerrar esa vela.
        """
        if t < 0 or t >= len(self._ts):
            return
        for estado in self.bots.values():
            if t < estado.warmup or t < estado.first_index or self._gap_ahead[t]:
                continue
            decide_order(
                estado.broker, estado.portfolio, estado.genome, estado.codes[t], t,
                self._ts, self._close, estado.stop_atr, estado.realized_vol, self.cfg,
            )

    def catch_up(self) -> int:
        """Procesa las velas perdidas tras una caída. Devuelve cuántas.

        En vivo, las velas que ya están en la caché y son posteriores al último
        tick procesado se recorren sin esperas antes de entrar al bucle normal.
        """
        assert self.clock is not None and self.candles is not None
        ultimo = self.clock.last_processed_ts
        if ultimo is None:
            return 0
        desde = self._index_of(ultimo) + 1
        if desde <= 0 or desde >= len(self.candles):
            return 0
        pendientes = min(
            len(self.candles) - desde, self.cfg.execution.max_catchup_candles
        )
        if not pendientes:
            return 0
        self._say(f"recuperando {pendientes} velas perdidas…")
        recuperadas = 0
        for i in range(desde, desde + pendientes):
            tick = self.clock._emit(int(self._ts[i]), i, is_catchup=True)
            self.process_tick(tick)
            recuperadas += 1
        return recuperadas

    # -------------------------------------------------------------- el tick

    def process_tick(self, tick: Tick) -> None:
        """Un tick completo: los 8 pasos de docs/ARCHITECTURE.md §4."""
        assert self.repos is not None
        t = tick.index
        vela = self._candle(t)
        muertos: dict[BotId, DeathCause] = {}

        with self.db.transaction():
            for estado in list(self.bots.values()):
                causa = self._step_bot(estado, tick, vela)
                if causa is not None:
                    muertos[estado.bot_id] = causa
            for bot_id, causa in muertos.items():
                self._kill(bot_id, causa, tick)
            self._persist_tick(tick)

        if tick.closes_generation:
            self.close_generation(tick.generation)

    def _candle(self, t: int) -> dict[str, float]:
        return {
            "ts": float(self._ts[t]), "open": self._open[t], "high": self._high[t],
            "low": self._low[t], "close": self._close[t], "volume": self._volume[t],
            "atr": float(self._atr[t]),
        }

    def _step_bot(
        self, estado: _BotState, tick: Tick, vela: dict[str, float]
    ) -> DeathCause | None:
        """Un bot, una vela. Mismo orden que ``backtest.run_backtest``.

        Devuelve la causa de muerte si un freno lo mata en el acto.
        """
        t = tick.index
        cierre = float(self._close[t])
        port = estado.portfolio
        risk = estado.genome.risk

        # [5] lo encolado en t-1 se rellena en la apertura de t
        self._settle(estado, vela, t, tick)

        # [6] salidas dentro de la vela, con la convención pesimista
        for pos, kind, disparo in port.check_exits(
            self._high[t], self._low[t], cierre, int(self._ts[t]), risk
        ):
            estado.broker.submit(
                OrderRequest(estado.bot_id, int(self._ts[t]), kind, pos.side, pos.amount, disparo)
            )
        self._settle(estado, vela, t, tick)

        # [8] frenos
        if port.drawdown(cierre) > self.cfg.risk.hard_max_drawdown:
            self._close_all(estado, tick, cierre)
            port.mark_to_market(cierre, int(self._ts[t]))
            estado.equity_window.append(port.equity(cierre))
            return DeathCause.DRAWDOWN_BREAKER

        if self._gap_ahead[t] and port.positions:
            # Atravesar una parada del venue con posiciones abiertas es inventar
            # precio, y con él rentabilidad.
            self._close_all(estado, tick, cierre)

        # [3][4] decisión con datos cerrados hasta t, para la apertura de t+1
        if t >= estado.warmup and t >= estado.first_index and not self._gap_ahead[t]:
            decide_order(
                estado.broker, port, estado.genome, estado.codes[t], t, self._ts,
                self._close, estado.stop_atr, estado.realized_vol, self.cfg,
            )

        # [6] revalorar
        port.mark_to_market(cierre, int(self._ts[t]))
        estado.equity_window.append(port.equity(cierre))
        return None

    def _settle(
        self, estado: _BotState, vela: dict[str, float], t: int, tick: Tick
    ) -> None:
        """Rellena lo pendiente y lo escribe: orden, operación y evento."""
        for fill in estado.broker.settle(vela):
            previa = next(
                (p for p in estado.portfolio.positions if p.side is fill.side), None
            )
            r_multiple = previa.r_multiple(float(fill.price)) if previa else None
            operacion = estado.portfolio.apply_fill(
                fill, risk=estado.genome.risk, atr=float(estado.stop_atr[t]),
                cooldown_ms=estado.cooldown_ms,
            )
            self._record_fill(estado, fill, operacion, tick, r_multiple)

    def _close_all(self, estado: _BotState, tick: Tick, precio: float) -> None:
        """Cierre forzoso al precio dado, con su fricción."""
        t = tick.index
        if not estado.portfolio.positions:
            return
        for pos in list(estado.portfolio.positions):
            estado.broker.submit(
                OrderRequest(
                    estado.bot_id, int(self._ts[t]), OrderKind.EXIT_FORCED,
                    pos.side, pos.amount,
                )
            )
        self._settle(estado, {**self._candle(t), "open": precio}, t, tick)

    def _record_fill(
        self,
        estado: _BotState,
        fill: Fill,
        operacion: Mapping[str, Any] | None,
        tick: Tick,
        r_multiple: float | None,
    ) -> None:
        assert self.repos is not None
        lado = str(fill.side)
        trade_id: int | None = estado.open_trades.get(lado)

        if fill.kind is OrderKind.ENTRY:
            pos = next(
                (p for p in estado.portfolio.positions if p.side is fill.side), None
            )
            trade_id = self.repos.trades.open_trade(
                estado.bot_id, tick.generation,
                side=lado, open_ts=fill.fill_ts, open_price=fill.price,
                amount=fill.amount,
                stop_price=pos.stop_price if pos else None,
                take_price=pos.take_price if pos else None,
            )
            estado.open_trades[lado] = trade_id
            self._pending_events.append(
                {
                    "type": EventType.TRADE_OPENED, "ts": int(fill.fill_ts),
                    "generation": tick.generation, "bot_id": estado.bot_id,
                    "summary": f"{estado.bot_id} abre {lado} a {fill.price:.2f}",
                    "payload": {"amount": fill.amount, "notional": fill.notional,
                                "fee": fill.fee, "slippage": fill.slippage},
                }
            )
        elif operacion is not None and trade_id is not None:
            self.repos.trades.close_trade(
                trade_id,
                close_ts=fill.fill_ts, close_price=fill.price,
                exit_kind=str(fill.kind), holding_bars=operacion.get("bars_held"),
                pnl_gross=operacion.get("gross_pnl"), pnl_net=operacion.get("pnl"),
                fees=operacion.get("fees", 0.0), return_pct=operacion.get("return"),
                r_multiple=r_multiple,
            )
            estado.open_trades.pop(lado, None)
            self._pending_events.append(
                {
                    "type": EventType.TRADE_CLOSED, "ts": int(fill.fill_ts),
                    "generation": tick.generation, "bot_id": estado.bot_id,
                    "summary": (
                        f"{estado.bot_id} cierra {lado} por {fill.kind} "
                        f"con {float(operacion.get('pnl', 0.0)):+.2f}"
                    ),
                    "payload": {"exit_kind": str(fill.kind),
                                "pnl": operacion.get("pnl"),
                                "return": operacion.get("return")},
                }
            )

        self.repos.trades.record_order(
            bot_id=estado.bot_id, trade_id=trade_id, candle_ts=fill.candle_ts,
            fill_ts=fill.fill_ts, kind=str(fill.kind), side=lado, price=fill.price,
            reference_price=fill.reference_price, slippage=fill.slippage,
            amount=fill.amount, notional=fill.notional, fee=fill.fee,
        )

    # ------------------------------------------------------------- persistencia

    def _persist_tick(self, tick: Tick) -> None:
        """Curvas, estado de los bots, jardín y eventos. Una vez por tick."""
        assert self.repos is not None
        t = tick.index
        cierre = float(self._close[t])
        momento = int(self._ts[t])

        filas = []
        capital = 0.0
        abiertas = 0
        for estado in self.bots.values():
            port = estado.portfolio
            equity = port.equity(cierre)
            capital += equity
            abiertas += len(port.positions)
            filas.append(
                (estado.bot_id, momento, equity, port.cash,
                 port.position_value(cierre), port.drawdown(cierre))
            )
            self.repos.bots.update_equity(
                estado.bot_id, equity, port.cash, port.peak_equity
            )
        if filas:
            self.db.executemany(
                "INSERT OR REPLACE INTO equity_snapshots (bot_id, ts, equity, cash, "
                "position_value, drawdown) VALUES (?,?,?,?,?,?)",
                filas,
            )

        benchmark = self._benchmark_units * cierre
        self._garden_peak = max(self._garden_peak, capital)
        drawdown = (
            max(0.0, 1.0 - capital / self._garden_peak) if self._garden_peak > 0 else 0.0
        )
        self.repos.equity.snapshot_garden(
            momento, tick.generation, garden_equity=capital,
            benchmark_equity=benchmark, n_alive=len(self.bots),
            n_open_positions=abiertas, garden_drawdown=drawdown,
        )

        if drawdown > self.cfg.risk.garden_max_drawdown:
            self.repos.events.raise_alert(
                str(AlertKind.GARDEN_DRAWDOWN), ts=momento, generation=tick.generation,
                value=drawdown, threshold=self.cfg.risk.garden_max_drawdown,
                detail="se frenan los nacimientos y sube la poda",
            )
        elif drawdown < self.cfg.risk.garden_max_drawdown * 0.8:
            self.repos.events.clear_alert(str(AlertKind.GARDEN_DRAWDOWN), momento)

        if self._pending_events:
            self.repos.events.log_many(self._pending_events)
            self._pending_events.clear()

        self.db.set_meta("last_tick_ts", momento)

    def _restore_benchmark(self, arranque: int) -> None:
        """Reconstruye la cartera espejo desde la última fila escrita.

        El benchmark recibe las mismas entradas de capital que el jardín: cada
        bot que nace compra unidades a su precio de nacimiento, y cada bot que
        muere las devuelve. Comparar un jardín que crece contra un buy & hold
        de capital fijo no diría nada.
        """
        assert self.repos is not None
        fila = self.db.query_one(
            "SELECT benchmark_equity, garden_equity FROM garden_equity "
            "ORDER BY ts DESC LIMIT 1"
        )
        precio = float(self._close[max(0, min(arranque, len(self._close) - 1))])
        if fila is not None and precio > 0:
            self._benchmark_units = float(fila["benchmark_equity"]) / precio
            self._garden_peak = float(
                self.db.query_one(
                    "SELECT MAX(garden_equity) AS pico FROM garden_equity"
                )["pico"] or 0.0
            )
            return
        # Primer arranque: cada bot vivo entra con su capital inicial.
        unidades = 0.0
        for estado in self.bots.values():
            if precio > 0:
                estado.benchmark_units = estado.portfolio.initial_capital / precio
                unidades += estado.benchmark_units
        self._benchmark_units = unidades
        self._garden_peak = sum(
            e.portfolio.equity(precio) for e in self.bots.values()
        )

    def _ticks_in_current_generation(self) -> int:
        fila = self.db.query_one(
            "SELECT COUNT(*) AS n FROM garden_equity WHERE generation = "
            "(SELECT generation FROM garden_equity ORDER BY ts DESC LIMIT 1)"
        )
        return int(fila["n"] or 0) if fila else 0

    # ---------------------------------------------------------- generaciones

    def close_generation(self, generation: int) -> GenerationOutcome | None:
        """Evaluación de generación: los 10 pasos de docs/ARCHITECTURE.md §5.

        Las métricas salen de la ventana vivida, no del histórico: el jardín
        vivo manda sobre el backtest (``fitness.live_weight``). La incubadora
        sigue criando, pero sólo ve las velas hasta hoy — dejarla mirar el
        futuro del replay convertiría el dry-run en una mentira optimista.
        """
        from ..engine.incubator import Incubator

        assert self.repos is not None and self.population is not None
        assert self.candles is not None and self.clock is not None

        hasta = self._index_of(self.clock.last_processed_ts or 0) + 1
        velas = self.candles.iloc[:hasta] if hasta > 0 else self.candles
        incubadora = Incubator(
            cfg=self.cfg, candles=velas, catalog=self.catalog,
            repo=self.repos.incubation,
        )

        metricas = self._window_metrics()
        outcome = self.population.evolve_generation(
            generation, incubadora, window_metrics=metricas, scope="live"
        )

        precio = float(self._close[max(0, hasta - 1)])
        tick = Tick(ts=int(self._ts[max(0, hasta - 1)]), generation=generation,
                    index=max(0, hasta - 1))
        with self.db.transaction():
            for bot_id in outcome.deaths:
                estado = self.bots.pop(bot_id, None)
                if estado is not None:
                    # Las posiciones de un muerto se cierran al precio de cierre
                    # de la vela que cerró la generación, no se abandonan.
                    self._close_all(estado, tick, precio)
                    self._benchmark_units -= estado.benchmark_units
            for bot_id in outcome.births:
                genoma = self.repos.bots.genome_of(bot_id)
                estado = self._build_state(bot_id, genoma, first_index=hasta)
                if precio > 0:
                    estado.benchmark_units = (
                        estado.portfolio.initial_capital / precio
                    )
                    self._benchmark_units += estado.benchmark_units
                self.bots[bot_id] = estado
            self.repos.generations.close(
                generation,
                {
                    "ended_ts": int(self._ts[max(0, hasta - 1)]),
                    "n_ticks": len(next(iter(self.bots.values())).equity_window)
                    if self.bots else 0,
                    "garden_equity": sum(
                        e.portfolio.equity(precio) for e in self.bots.values()
                    ),
                    "benchmark_equity": self._benchmark_units * precio,
                    "garden_alpha": self._alpha(precio),
                    "garden_drawdown": self._garden_drawdown(),
                },
            )
            if self._pending_events:
                self.repos.events.log_many(self._pending_events)
                self._pending_events.clear()

        for estado in self.bots.values():
            estado.equity_window.clear()
            estado.fees_at_window_start = estado.portfolio.total_fees
            estado.portfolio.closed_trades.clear()
        self._window_start_index = hasta
        self.generation = generation
        self._say(outcome.summary_line)
        for aviso in outcome.alerts:
            self._say(f"           aviso: {aviso}")
        return outcome

    def _window_metrics(self) -> dict[BotId, Metrics]:
        """Métricas de la ventana vivida, bot a bot."""
        assert self.candles is not None
        salida: dict[BotId, Metrics] = {}
        referencia = self._close[self._window_start_index :]
        for bot_id, estado in self.bots.items():
            curva = np.asarray(estado.equity_window, dtype="float64")
            if curva.size < 2:
                salida[bot_id] = Metrics()
                continue
            salida[bot_id] = compute_metrics(
                curva,
                estado.portfolio.closed_trades,
                self.cfg.market.timeframe,
                total_fees=estado.portfolio.total_fees - estado.fees_at_window_start,
                benchmark=referencia[: curva.size] if referencia.size >= curva.size else None,
            )
        return salida

    def _alpha(self, precio: float) -> float | None:
        benchmark = self._benchmark_units * precio
        if benchmark <= 0:
            return None
        jardin = sum(e.portfolio.equity(precio) for e in self.bots.values())
        return jardin / benchmark - 1.0

    def _garden_drawdown(self) -> float:
        fila = self.db.query_one(
            "SELECT garden_drawdown FROM garden_equity ORDER BY ts DESC LIMIT 1"
        )
        return float(fila["garden_drawdown"]) if fila else 0.0

    # --------------------------------------------------------------- frenos

    def check_circuit_breakers(self) -> list[BotId]:
        """Comprobaciones de cada tick, independientes del fitness.

        Ver la tabla de docs/EXECUTION.md §Circuit breakers. Devuelve los ids de
        los bots podados en el acto. El freno de drawdown por bot se aplica
        dentro del tick, en cuanto se conoce el cierre; esto es la vista de
        conjunto que usan los tests y la parada de emergencia.
        """
        assert self.candles is not None
        podados: list[BotId] = []
        if not self.bots:
            return podados
        t = self._index_of(int(self.db.get_meta("last_tick_ts") or 0))
        if t < 0:
            return podados
        cierre = float(self._close[t])
        for bot_id, estado in list(self.bots.items()):
            if estado.portfolio.drawdown(cierre) > self.cfg.risk.hard_max_drawdown:
                podados.append(bot_id)
        return podados

    def _kill(self, bot_id: BotId, causa: DeathCause, tick: Tick) -> None:
        """Poda inmediata: el bot deja de operar en el acto."""
        assert self.repos is not None
        estado = self.bots.pop(bot_id, None)
        if estado is None:
            return
        estado.broker.cancel_all()
        self._benchmark_units -= estado.benchmark_units
        self.repos.bots.set_status(
            bot_id, BotStatus.CULLED, generation=tick.generation, cause=causa
        )
        self._pending_events.append(
            {
                "type": EventType.CIRCUIT_BREAKER, "ts": int(tick.ts),
                "generation": tick.generation, "bot_id": bot_id, "severity": "warn",
                "summary": f"{bot_id} podado en el acto por {causa}",
                "payload": {"cause": str(causa)},
            }
        )

    def _venue_failed(self, fallos: int, exc: BaseException) -> None:
        """El venue no contesta. Pasado el umbral el jardín entra en DEGRADED:
        sigue registrando, no opera."""
        assert self.repos is not None
        ahora = int(time.time() * 1000)
        if fallos < self.cfg.execution.venue_failure_threshold:
            return
        self.status = GardenStatus.DEGRADED
        self.db.set_meta("status", str(self.status))
        self.repos.events.log(
            EventType.CIRCUIT_BREAKER,
            f"el venue lleva {fallos} fallos seguidos: jardín en DEGRADED",
            ts=ahora, generation=self.generation, severity="critical",
            payload={"error": str(exc), "failures": fallos},
        )
        self.repos.events.raise_alert(
            str(AlertKind.VENUE_FAILURE), ts=ahora, generation=self.generation,
            value=float(fallos), threshold=float(self.cfg.execution.venue_failure_threshold),
            detail=str(exc)[:200],
        )

    # ------------------------------------------------------------------ vivo

    def _fetch_live(self, desde: Timestamp | None) -> Sequence[tuple[Timestamp, int]]:
        """Descarga las velas cerradas que falten, las persiste y recompila.

        Devuelve pares ``(ts, índice)`` ya dentro de la serie del jardín, que es
        lo que el reloj necesita para emitir ticks.
        """
        from ..data.backfill import backfill
        from ..data.sources import VenueClient
        from ..data.store import CandleStore, SeriesKey

        assert self.candles is not None
        market = MarketSpec(
            venue=self.cfg.market.venue,
            symbol=self.db.get_meta("symbol") or self.cfg.primary_symbol,
            timeframe=self.db.get_meta("timeframe") or self.cfg.market.timeframe,
        )
        store = CandleStore(cache_dir=self.cfg.path(self.cfg.storage.cache_dir))
        clave = SeriesKey(market.venue, market.symbol, market.timeframe)
        backfill(
            store, VenueClient(venue=market.venue), clave,
            since=int(self._ts[-1]) if len(self._ts) else None,
            jump_threshold=self.cfg.risk.price_jump_anomaly,
        )
        self.candles = store.load(clave)
        self._load_arrays(market.timeframe)
        self._recompile()
        if self.status is GardenStatus.DEGRADED:
            self.status = GardenStatus.RUNNING
            self.db.set_meta("status", str(self.status))
        corte = int(desde) if desde is not None else -1
        return [(int(ts), i) for i, ts in enumerate(self._ts) if int(ts) > corte]

    def _recompile(self) -> None:
        """Recompila las señales de todos los vivos sobre la serie ya crecida.

        En vivo hay una hora entre velas y esto tarda segundos: no hay atajo que
        merezca el riesgo de que las señales del vivo dejen de coincidir con las
        del backtest.
        """
        for bot_id, estado in list(self.bots.items()):
            nuevo = self._build_state(bot_id, estado.genome, estado.first_index)
            nuevo.portfolio = estado.portfolio
            nuevo.broker = estado.broker
            nuevo.open_trades = estado.open_trades
            nuevo.equity_window = estado.equity_window
            nuevo.benchmark_units = estado.benchmark_units
            nuevo.fees_at_window_start = estado.fees_at_window_start
            self.bots[bot_id] = nuevo

    # ------------------------------------------------------------------ cierre

    def shutdown(self) -> None:
        """Parada limpia: vuelca estado y marca ``garden_meta.status``."""
        assert self.repos is not None
        self.status = GardenStatus.STOPPED
        with self.db.transaction():
            if self._pending_events:
                self.repos.events.log_many(self._pending_events)
                self._pending_events.clear()
            self.db.set_meta("status", str(self.status))
            self.db.set_meta("current_generation", self.generation)

    # ----------------------------------------------------------------- varios

    def _say(self, mensaje: str) -> None:
        if self.verbose:
            print(mensaje, flush=True)

    def _log_event(self, tipo: EventType, resumen: str, **kw: Any) -> None:
        assert self.repos is not None
        self.repos.events.log(
            tipo, resumen, ts=int(time.time() * 1000),
            generation=self.generation, **kw,
        )


__all__ = ("PROGRESS_EVERY", "GardenRunner")
