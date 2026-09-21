"""El bucle del jardín: un solo proceso, un solo hilo, indefinidamente.

Con timeframe de 1h y poblaciones de cientos de bots, un tick tarda
milisegundos y hay 3.600 segundos entre velas. No hace falta concurrencia aquí;
la paralelización vive en la incubadora.

La secuencia de cada vela es la misma que la del backtest —de hecho la decisión
la toma la misma función, ``backtest.decide_order``— porque si el vivo y el
backtest pudieran divergir, una sorpresa en vivo no significaría nada.

El jardín es multi-símbolo: cada bot opera el mercado que dice su genoma y el
reloj lo marca el símbolo primario. Un bot cuyo mercado no tiene vela en un
tick simplemente no actúa en ese tick; inventar precio es inventar rentabilidad.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import numpy as np

from ..config import Config, effective_config
from ..data.candles import timeframe_ms
from ..evaluation.metrics import Metrics, compute_metrics
from ..genome.schema import Genome
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

    from ..evolution.population import GenerationOutcome, Population
    from ..genome.catalog import GeneCatalog
    from ..storage.db import Database
    from ..storage.repositories import Repositories
    from .incubator import MultiSymbolIncubator

#: Cada cuántos ticks se guarda el pulso del jardín (duración del tick) en
#: ``garden_meta``. Es lo que alimenta el panel de salud del dashboard.
HEALTH_EVERY = 24


@dataclass(slots=True)
class _Series:
    """Un mercado: sus velas, sus series derivadas y su alineación al reloj.

    ``local`` traduce el índice del reloj —que lo marca el símbolo primario— al
    índice de esta serie, o -1 si este mercado no tiene vela en ese momento.
    """

    symbol: str
    candles: Any                      # pd.DataFrame
    features: Any                     # IndicatorCache
    ts: np.ndarray
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    atr: np.ndarray
    gap_ahead: np.ndarray
    local: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype="int64"))

    def candle(self, i: int) -> dict[str, float]:
        return {
            "ts": float(self.ts[i]), "open": self.open[i], "high": self.high[i],
            "low": self.low[i], "close": self.close[i], "volume": self.volume[i],
            "atr": float(self.atr[i]),
        }

    def index_of(self, ts: Timestamp) -> int:
        pos = int(np.searchsorted(self.ts, int(ts)))
        if pos < len(self.ts) and int(self.ts[pos]) == int(ts):
            return pos
        return -1


@dataclass(slots=True)
class _Tramo:
    """Lo vivido por un bot en una generación: su trozo de curva, sus
    operaciones y las comisiones que llevaba acumuladas al empezarla."""

    start_index: int
    equity: list[float]
    trades: list[dict[str, Any]]
    fees_at_start: float

    @property
    def n_trades(self) -> int:
        return len(self.trades)


@dataclass(slots=True)
class _BotState:
    """Todo lo que un bot vivo necesita para operar un tick.

    Las señales se compilan una vez sobre toda la serie y se indexan por tick.
    Es correcto porque los indicadores son causales —está probado en el hito
    2— y es lo que hace que un dry-run de seis meses tarde minutos.
    """

    bot_id: BotId
    genome: Genome
    symbol: str
    portfolio: Portfolio
    broker: PaperBroker
    codes: np.ndarray
    stop_atr: np.ndarray
    realized_vol: np.ndarray
    warmup: int
    cooldown_ms: int
    #: Primer índice del reloj en el que puede operar: nadie opera al nacer.
    first_index: int
    #: Unidades del benchmark compradas con su capital inicial. Así la cartera
    #: espejo recibe exactamente las mismas entradas y salidas de dinero que el
    #: jardín, y el alfa compara dos cosas comparables.
    benchmark_units: float = 0.0
    #: Curva de la generación que se está viviendo.
    equity_window: list[float] = field(default_factory=list)
    #: Las generaciones anteriores, para la ventana deslizante del fitness
    #: vivo. Sin ellas, una semana de 168 velas casi nunca reúne las
    #: ``fitness.min_trades`` operaciones que hacen falta para juzgar a un bot
    #: (docs/DECISIONS.md D-030 y D-031).
    history: deque[_Tramo] = field(default_factory=deque)
    #: Id de la fila de ``trades`` abierta por lado.
    open_trades: dict[str, int] = field(default_factory=dict)
    fees_at_window_start: float = 0.0
    #: Índice del reloj en el que empezó la generación en curso para este bot.
    window_start_index: int = 0
    #: Último cierre conocido de su mercado, para valorarlo cuando su símbolo
    #: no tiene vela en este tick.
    last_price: float = 0.0


@dataclass(slots=True)
class GardenRunner:
    """Orquestador del jardín vivo."""

    cfg: Config
    db: Database
    status: GardenStatus = GardenStatus.STOPPED

    # -- estado interno, todo reconstruible desde SQLite -------------------- #
    repos: Repositories | None = None
    catalog: GeneCatalog | None = None
    series: dict[str, _Series] = field(default_factory=dict)
    primary: str = ""
    clock: MarketClock | None = None
    population: Population | None = None
    bots: dict[BotId, _BotState] = field(default_factory=dict)
    generation: int = 0
    ticks_done: int = 0
    verbose: bool = True

    _benchmark_units: dict[str, float] = field(default_factory=dict, repr=False)
    _garden_peak: float = 0.0
    _pending_events: list[dict[str, Any]] = field(default_factory=list, repr=False)
    _window_start_index: int = 0
    _tick_ms: float = 0.0
    #: Bots resucitados de un jardín anterior a ``bot_runtime`` (D-033), cuyo
    #: estado vivo hay que aproximar en vez de leerlo.
    _legacy_resume: set[BotId] = field(default_factory=set, repr=False)

    # ---------------------------------------------------------------- arranque

    @property
    def master(self) -> _Series:
        """La serie que marca el reloj: la del símbolo primario."""
        return self.series[self.primary]

    @property
    def candles(self) -> Any:
        """Las velas del símbolo primario. Lo que el reloj recorre."""
        return self.master.candles if self.primary else None

    def symbols(self) -> list[str]:
        """Los símbolos del jardín: los que dicen los bots vivos, más el de la
        configuración. Un jardín vacío sigue teniendo su mercado semilla."""
        guardados = self.db.get_meta("symbols")
        if guardados:
            import json

            try:
                return list(json.loads(guardados))
            except ValueError:
                pass
        principal = self.db.get_meta("symbol") or self.cfg.primary_symbol
        de_los_bots = [
            str(f["symbol"])
            for f in self.db.query("SELECT DISTINCT symbol FROM genomes")
        ]
        return list(dict.fromkeys([principal, *de_los_bots, *self.cfg.market.symbols]))

    def prepare(self) -> None:
        """Carga velas, catálogo, población y reloj. Idempotente."""
        import random

        from ..evolution.population import Population
        from ..genome.catalog import load_catalog
        from ..storage.repositories import Repositories

        if self.repos is None:
            self.repos = Repositories.open(self.db)
        # Los ajustes del jardinero viven en la base, no en el YAML: el motor
        # arranca ya con ellos aplicados (docs/GARDENER_PROTOCOL.md §Límites).
        self.cfg = effective_config(self.cfg, self.db)
        self.catalog = load_catalog(self.cfg.path("config/genes.yaml"))

        simbolos = self.symbols()
        self.primary = simbolos[0]
        self.series = {}
        for simbolo in simbolos:
            serie = self._load_series(simbolo)
            if serie is not None:
                self.series[simbolo] = serie
        if self.primary not in self.series:
            raise RuntimeError(
                f"no hay velas de {self.primary} en caché: "
                f"'keepgarden data backfill' antes de arrancar el jardín"
            )
        self._align_series()

        self.population = Population(
            cfg=self.cfg, catalog=self.catalog, db=self.db,
            rng=random.Random(self.cfg.seed), repos=self.repos,
        )
        self.generation = int(self.db.get_meta("current_generation") or 0)

    def _load_series(self, simbolo: str) -> _Series | None:
        from ..data.indicators import IndicatorCache
        from ..data.store import CandleStore, SeriesKey

        timeframe = self.db.get_meta("timeframe") or self.cfg.market.timeframe
        store = CandleStore(cache_dir=self.cfg.path(self.cfg.storage.cache_dir))
        velas = store.load(SeriesKey(self.cfg.market.venue, simbolo, timeframe))
        if velas.empty:
            return None
        contexto = {}
        for tf in self.cfg.market.context_timeframes:
            extra = store.load(SeriesKey(self.cfg.market.venue, simbolo, tf))
            if not extra.empty:
                contexto[tf] = extra

        ts = np.asarray(velas.index, dtype="int64")
        hueco = np.zeros(len(velas), dtype=bool)
        if len(velas) > 1:
            hueco[:-1] = np.diff(ts) > timeframe_ms(timeframe)  # type: ignore[arg-type]
        features = IndicatorCache(
            candles=velas, context=contexto, catalog=self.catalog, timeframe=timeframe
        )
        return _Series(
            symbol=simbolo,
            candles=velas,
            features=features,
            ts=ts,
            open=velas["open"].to_numpy(dtype="float64"),
            high=velas["high"].to_numpy(dtype="float64"),
            low=velas["low"].to_numpy(dtype="float64"),
            close=velas["close"].to_numpy(dtype="float64"),
            volume=velas["volume"].to_numpy(dtype="float64"),
            atr=auxiliary_series(
                features, "ATR", DEFAULT_ATR_PARAMS, PriceField.HLC3, len(velas)
            ),
            gap_ahead=hueco,
        )

    def _align_series(self) -> None:
        """Traduce el reloj del símbolo primario al índice de cada mercado."""
        maestro = self.master.ts
        for serie in self.series.values():
            if serie.symbol == self.primary:
                serie.local = np.arange(len(maestro), dtype="int64")
                continue
            pos = np.searchsorted(serie.ts, maestro)
            dentro = pos < len(serie.ts)
            iguales = np.zeros(len(maestro), dtype=bool)
            if len(serie.ts):
                iguales[dentro] = serie.ts[pos[dentro]] == maestro[dentro]
            serie.local = np.where(iguales, pos, -1).astype("int64")

    def _index_of(self, ts: Timestamp) -> int:
        """Posición de una vela en el reloj del jardín. -1 si no está."""
        return self.master.index_of(ts)

    # ------------------------------------------------------------- población

    def _build_state(
        self, bot_id: BotId, genome: Genome, first_index: int, *, resume: bool = False
    ) -> _BotState:
        """Compila las señales de un bot y reconstruye su cartera.

        ``resume`` distingue al bot que vuelve de una ejecución anterior —y
        tiene estado vivo que recuperar— del que acaba de nacer, que no tiene
        nada, y del que sólo se está recompilando, que conserva el suyo en
        memoria.
        """
        from ..genome.compile import compile_genome

        assert self.repos is not None
        fila = self.repos.bots.get(bot_id)
        assert fila is not None
        simbolo = str(genome.market.symbol)
        serie = self.series.get(simbolo)
        if serie is None:
            raise RuntimeError(
                f"{bot_id} opera {simbolo} y no hay velas de ese mercado en caché"
            )

        miembros = self.repos.bots.alive_genomes() if genome.is_ensemble else None
        compilado = compile_genome(genome, serie.candles, serie.features, members=miembros)

        n = len(serie.candles)
        stop_atr = serie.atr
        if genome.risk.stop.atr_ref and genome.risk.stop.atr_ref in compilado.feature_arrays:
            stop_atr = np.nan_to_num(
                compilado.feature_arrays[genome.risk.stop.atr_ref], nan=0.0
            )
        realized_vol = (
            auxiliary_series(
                serie.features, "REALIZED_VOL", DEFAULT_VOL_PARAMS, PriceField.CLOSE, n
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
            symbol=simbolo,
            portfolio=port,
            broker=PaperBroker(frictions=self.cfg.frictions),
            codes=compilado.codes,
            stop_atr=stop_atr,
            realized_vol=realized_vol,
            warmup=compilado.warmup_bars,
            cooldown_ms=max(0, int(genome.risk.cooldown_bars))
            * timeframe_ms(genome.market.timeframe),
            first_index=first_index,
            window_start_index=first_index,
            history=deque(maxlen=max(0, self.cfg.fitness.live_window_generations - 1)),
            last_price=float(serie.close[min(max(0, first_index), n - 1)]),
        )
        if resume and not self._restore_runtime(estado):
            self._restore_positions(estado, serie)
            self._legacy_resume.add(bot_id)
        return estado

    # -- estado vivo entre ejecuciones -------------------------------------- #

    def _runtime_payload(self, estado: _BotState) -> dict[str, Any]:
        """El estado vivo de un bot, en JSON.

        Es todo lo que no cabe en ``trades``: el ancla del trailing, el 1R, las
        velas aguantadas, la comisión de entrada, la cola del broker y el
        enfriamiento. Sin esto, reanudar con una posición abierta la cierra con
        otro precio, otras comisiones y otra duración: no es el mismo jardín.
        """
        port = estado.portfolio
        return {
            "cooldown_until_ts": port.cooldown_until_ts,
            "total_fees": port.total_fees,
            "last_price": estado.last_price,
            "open_trades": dict(estado.open_trades),
            "positions": [
                {
                    "side": str(pos.side),
                    "amount": pos.amount,
                    "entry_price": pos.entry_price,
                    "entry_ts": int(pos.entry_ts),
                    "stop_price": pos.stop_price,
                    "take_price": pos.take_price,
                    "trailing_anchor": pos.trailing_anchor,
                    "initial_risk": pos.initial_risk,
                    "bars_held": int(pos.bars_held),
                    "entry_atr": pos.entry_atr,
                    "entry_fee": pos.entry_fee,
                    "trailing_active": bool(pos.trailing_active),
                }
                for pos in port.positions
            ],
            "pending": [
                {
                    "candle_ts": int(orden.candle_ts),
                    "kind": str(orden.kind),
                    "side": str(orden.side),
                    "amount": orden.amount,
                    "trigger_price": orden.trigger_price,
                }
                for orden in estado.broker.pending
            ],
        }

    def _restore_runtime(self, estado: _BotState) -> bool:
        """Devuelve el bot a donde estaba. Falso si no había nada guardado."""
        assert self.repos is not None
        datos = self.repos.bots.load_runtime(estado.bot_id)
        if datos is None:
            return False

        port = estado.portfolio
        cooldown = datos.get("cooldown_until_ts")
        port.cooldown_until_ts = int(cooldown) if cooldown is not None else None
        port.total_fees = float(datos.get("total_fees", 0.0))
        port.positions = [
            Position(
                side=Side(str(pos["side"])),
                amount=float(pos["amount"]),
                entry_price=float(pos["entry_price"]),
                entry_ts=int(pos["entry_ts"]),
                stop_price=pos.get("stop_price"),
                take_price=pos.get("take_price"),
                trailing_anchor=pos.get("trailing_anchor"),
                initial_risk=float(pos.get("initial_risk", 0.0)),
                bars_held=int(pos.get("bars_held", 0)),
                entry_atr=float(pos.get("entry_atr", 0.0)),
                entry_fee=float(pos.get("entry_fee", 0.0)),
                trailing_active=bool(pos.get("trailing_active", False)),
            )
            for pos in datos.get("positions") or ()
        ]
        for orden in datos.get("pending") or ():
            estado.broker.submit(
                OrderRequest(
                    estado.bot_id,
                    int(orden["candle_ts"]),
                    OrderKind(str(orden["kind"])),
                    Side(str(orden["side"])),
                    float(orden["amount"]),
                    orden.get("trigger_price"),
                )
            )
        estado.open_trades = {
            str(lado): int(trade_id)
            for lado, trade_id in (datos.get("open_trades") or {}).items()
        }
        precio = float(datos.get("last_price") or 0.0)
        if precio > 0:
            estado.last_price = precio
        return True

    def _restore_positions(self, estado: _BotState, serie: _Series) -> None:
        """Reconstruye las posiciones abiertas desde ``trades``.

        Sólo para jardines anteriores a ``bot_runtime`` (D-033), que no
        guardaron su estado vivo. Recupera lo que la tabla sabe —lado, tamaño,
        precio de entrada, stop y take— y da por perdido lo que sólo vivía en
        memoria: el ancla del trailing, la comisión de entrada y las velas
        aguantadas. Es una aproximación, y por eso ocurre una sola vez: el
        primer tick ya escribe estado vivo de verdad.
        """
        assert self.repos is not None
        for fila in self.repos.trades.open_positions(estado.bot_id):
            lado = Side(str(fila["side"]))
            i = serie.index_of(int(fila["open_ts"]))
            estado.portfolio.positions.append(
                Position(
                    side=lado,
                    amount=float(fila["amount"]),
                    entry_price=float(fila["open_price"]),
                    entry_ts=int(fila["open_ts"]),
                    stop_price=fila["stop_price"],
                    take_price=fila["take_price"],
                    entry_atr=float(serie.atr[i]) if i >= 0 else 0.0,
                )
            )
            estado.open_trades[str(lado)] = int(fila["trade_id"])

    def _load_population(self, first_index: int) -> None:
        assert self.repos is not None
        self.bots = {}
        for bot_id, genoma in self.repos.bots.alive_genomes().items():
            if str(genoma.market.symbol) not in self.series:
                self._say(
                    f"  aviso: {bot_id} opera {genoma.market.symbol} y no hay velas "
                    f"de ese mercado: se queda fuera de este arranque"
                )
                continue
            self.bots[bot_id] = self._build_state(
                bot_id, genoma, first_index, resume=True
            )

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
        if not self.series:
            self.prepare()
        assert self.repos is not None

        maestro = self.master
        ultimo = self.db.get_meta("last_tick_ts")
        ultimo_ts = int(ultimo) if ultimo else None
        if ultimo_ts is None and start_index:
            # El reloj salta lo anterior porque ya "lo ha visto": así el replay
            # empieza donde se le pide sin que el runner tenga dos índices.
            inicio = max(0, min(int(start_index), len(maestro.ts) - 1))
            ultimo_ts = int(maestro.ts[inicio - 1]) if inicio > 0 else None
        arranque = (self._index_of(ultimo_ts) + 1) if ultimo_ts is not None else 0
        self._load_population(max(0, arranque))
        self._restore_benchmark(arranque)
        if ultimo and arranque > 0 and self._legacy_resume:
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
            fuente = self.clock.replay(maestro.candles, speed)
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
        """Aproxima la cola de órdenes de un jardín anterior a ``bot_runtime``.

        Una orden decidida en la vela ``t`` se rellena en la apertura de
        ``t+1``. Los jardines de D-033 en adelante guardan esa cola tal cual y
        no pasan por aquí; los anteriores no la tienen en ninguna parte, así
        que se vuelve a decidir sobre ``t``, que es lo más cerca que se puede
        estar: la decisión es pura y la cartera se ha restaurado.
        """
        if t < 0 or t >= len(self.master.ts):
            return
        for bot_id in sorted(self._legacy_resume):
            estado = self.bots.get(bot_id)
            if estado is None:
                continue
            serie = self.series[estado.symbol]
            i = int(serie.local[t])
            if i < 0 or t < estado.warmup or t < estado.first_index or serie.gap_ahead[i]:
                continue
            decide_order(
                estado.broker, estado.portfolio, estado.genome, estado.codes[i], i,
                serie.ts, serie.close, estado.stop_atr, estado.realized_vol, self.cfg,
            )

    def catch_up(self) -> int:
        """Procesa las velas perdidas tras una caída. Devuelve cuántas.

        En vivo, las velas que ya están en la caché y son posteriores al último
        tick procesado se recorren sin esperas antes de entrar al bucle normal.
        """
        assert self.clock is not None
        ultimo = self.clock.last_processed_ts
        if ultimo is None:
            return 0
        maestro = self.master
        desde = self._index_of(ultimo) + 1
        if desde <= 0 or desde >= len(maestro.ts):
            return 0
        pendientes = min(
            len(maestro.ts) - desde, self.cfg.execution.max_catchup_candles
        )
        if not pendientes:
            return 0
        self._say(f"recuperando {pendientes} velas perdidas…")
        for i in range(desde, desde + pendientes):
            self.process_tick(self.clock._emit(int(maestro.ts[i]), i, is_catchup=True))
        return pendientes

    # -------------------------------------------------------------- el tick

    def process_tick(self, tick: Tick) -> None:
        """Un tick completo: los 8 pasos de docs/ARCHITECTURE.md §4."""
        assert self.repos is not None
        empezado = time.perf_counter()
        muertos: dict[BotId, DeathCause] = {}

        with self.db.transaction():
            for estado in list(self.bots.values()):
                causa = self._step_bot(estado, tick)
                if causa is not None:
                    muertos[estado.bot_id] = causa
            for bot_id, causa in muertos.items():
                self._kill(bot_id, causa, tick)
            self._persist_tick(tick)

        self._tick_ms = (time.perf_counter() - empezado) * 1000.0
        if tick.index % HEALTH_EVERY == 0:
            self.db.set_meta("last_tick_ms", round(self._tick_ms, 3))

        if tick.closes_generation:
            self.close_generation(tick.generation)

    def _step_bot(self, estado: _BotState, tick: Tick) -> DeathCause | None:
        """Un bot, una vela de su mercado. Mismo orden que ``run_backtest``.

        Devuelve la causa de muerte si un freno lo mata en el acto.
        """
        serie = self.series[estado.symbol]
        t = int(serie.local[tick.index])
        if t < 0:
            # Su mercado no tiene vela ahora mismo: ni opera ni se revalora con
            # un precio inventado. Se queda quieto y con su último cierre.
            estado.equity_window.append(estado.portfolio.equity(estado.last_price))
            return None

        vela = serie.candle(t)
        cierre = float(serie.close[t])
        estado.last_price = cierre
        port = estado.portfolio
        risk = estado.genome.risk

        # [5] lo encolado en t-1 se rellena en la apertura de t
        self._settle(estado, serie, vela, t, tick)

        # [6] salidas dentro de la vela, con la convención pesimista
        for pos, kind, disparo in port.check_exits(
            serie.high[t], serie.low[t], cierre, int(serie.ts[t]), risk
        ):
            estado.broker.submit(
                OrderRequest(estado.bot_id, int(serie.ts[t]), kind, pos.side, pos.amount, disparo)
            )
        self._settle(estado, serie, vela, t, tick)

        # [8] frenos
        if port.drawdown(cierre) > self.cfg.risk.hard_max_drawdown:
            self._close_all(estado, tick, cierre)
            port.mark_to_market(cierre, int(serie.ts[t]))
            estado.equity_window.append(port.equity(cierre))
            return DeathCause.DRAWDOWN_BREAKER

        if serie.gap_ahead[t] and port.positions:
            # Atravesar una parada del venue con posiciones abiertas es inventar
            # precio, y con él rentabilidad.
            self._close_all(estado, tick, cierre)

        # [3][4] decisión con datos cerrados hasta t, para la apertura de t+1
        if (
            t >= estado.warmup
            and tick.index >= estado.first_index
            and not serie.gap_ahead[t]
        ):
            decide_order(
                estado.broker, port, estado.genome, estado.codes[t], t, serie.ts,
                serie.close, estado.stop_atr, estado.realized_vol, self.cfg,
            )

        # [6] revalorar
        port.mark_to_market(cierre, int(serie.ts[t]))
        estado.equity_window.append(port.equity(cierre))
        return None

    def _settle(
        self, estado: _BotState, serie: _Series, vela: dict[str, float], t: int, tick: Tick
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
        if not estado.portfolio.positions:
            return
        serie = self.series[estado.symbol]
        t = int(serie.local[tick.index])
        if t < 0:
            t = max(0, serie.index_of(int(tick.ts)))
        for pos in list(estado.portfolio.positions):
            estado.broker.submit(
                OrderRequest(
                    estado.bot_id, int(serie.ts[t]), OrderKind.EXIT_FORCED,
                    pos.side, pos.amount,
                )
            )
        self._settle(estado, serie, {**serie.candle(t), "open": precio}, t, tick)

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

        # La orden va primero, y es ella quien manda: si ya estaba, esta vela
        # se procesó en una vida anterior del proceso y no se toca 'trades'.
        # Su índice único sobre (bot_id, candle_ts, kind) es el que protege a
        # las dos tablas, no sólo a la suya.
        order_id = self.repos.trades.record_order(
            bot_id=estado.bot_id, trade_id=trade_id, candle_ts=fill.candle_ts,
            fill_ts=fill.fill_ts, kind=str(fill.kind), side=lado, price=fill.price,
            reference_price=fill.reference_price, slippage=fill.slippage,
            amount=fill.amount, notional=fill.notional, fee=fill.fee,
        )
        if order_id == 0:
            return

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
            self.repos.trades.link_order(order_id, trade_id)
            self._pending_events.append(
                {
                    "type": EventType.TRADE_OPENED, "ts": int(fill.fill_ts),
                    "generation": tick.generation, "bot_id": estado.bot_id,
                    "summary": f"{estado.bot_id} abre {lado} en {estado.symbol} "
                               f"a {fill.price:.2f}",
                    "payload": {"amount": fill.amount, "notional": fill.notional,
                                "fee": fill.fee, "slippage": fill.slippage,
                                "symbol": estado.symbol},
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
                                "return": operacion.get("return"),
                                "symbol": estado.symbol},
                }
            )

    # ------------------------------------------------------------- persistencia

    def _persist_tick(self, tick: Tick) -> None:
        """Curvas, estado de los bots, jardín y eventos. Una vez por tick."""
        assert self.repos is not None
        momento = int(self.master.ts[tick.index])

        filas = []
        vivo = []
        capital = 0.0
        abiertas = 0
        for estado in self.bots.values():
            port = estado.portfolio
            precio = estado.last_price
            equity = port.equity(precio)
            capital += equity
            abiertas += len(port.positions)
            filas.append(
                (estado.bot_id, momento, equity, port.cash,
                 port.position_value(precio), port.drawdown(precio))
            )
            self.repos.bots.update_equity(
                estado.bot_id, equity, port.cash, port.peak_equity
            )
            vivo.append((estado.bot_id, momento, self._runtime_payload(estado)))
        if filas:
            self.db.executemany(
                "INSERT OR REPLACE INTO equity_snapshots (bot_id, ts, equity, cash, "
                "position_value, drawdown) VALUES (?,?,?,?,?,?)",
                filas,
            )
        self.repos.bots.save_runtime(vivo)

        benchmark = self._benchmark_value(tick.index)
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

    def _price_at(self, simbolo: str, indice_maestro: int) -> float:
        """Último cierre conocido de un mercado en ese momento del reloj."""
        serie = self.series.get(simbolo)
        if serie is None or not len(serie.close):
            return 0.0
        i = int(serie.local[min(indice_maestro, len(serie.local) - 1)])
        if i < 0:
            # Sin vela ahora: el último cierre anterior, no un precio inventado.
            anterior = int(np.searchsorted(serie.ts, int(self.master.ts[indice_maestro])))
            i = max(0, min(anterior - 1, len(serie.close) - 1))
        return float(serie.close[i])

    def _benchmark_value(self, indice_maestro: int) -> float:
        return sum(
            unidades * self._price_at(simbolo, indice_maestro)
            for simbolo, unidades in self._benchmark_units.items()
        )

    def _restore_benchmark(self, arranque: int) -> None:
        """Reconstruye la cartera espejo desde la última fila escrita.

        El benchmark recibe las mismas entradas de capital que el jardín: cada
        bot que nace compra unidades de *su* mercado al precio de su
        nacimiento, y cada bot que muere las devuelve. Comparar un jardín que
        crece contra un buy & hold de capital fijo no diría nada.
        """
        assert self.repos is not None
        indice = max(0, min(arranque, len(self.master.ts) - 1))
        fila = self.db.query_one(
            "SELECT benchmark_equity FROM garden_equity ORDER BY ts DESC LIMIT 1"
        )
        self._benchmark_units = {}
        for estado in self.bots.values():
            precio = self._price_at(estado.symbol, indice)
            if precio > 0:
                estado.benchmark_units = estado.portfolio.initial_capital / precio
                self._benchmark_units[estado.symbol] = (
                    self._benchmark_units.get(estado.symbol, 0.0) + estado.benchmark_units
                )

        if fila is not None:
            # Ya había historia: se respeta el valor escrito repartiéndolo entre
            # los mercados con la proporción actual, para que la curva no salte.
            objetivo = float(fila["benchmark_equity"])
            actual = self._benchmark_value(indice)
            if actual > 0 and objetivo > 0:
                factor = objetivo / actual
                self._benchmark_units = {
                    s: u * factor for s, u in self._benchmark_units.items()
                }
                for estado in self.bots.values():
                    estado.benchmark_units *= factor
            pico = self.db.query_one("SELECT MAX(garden_equity) AS pico FROM garden_equity")
            self._garden_peak = float((pico or {"pico": 0.0})["pico"] or 0.0)
        else:
            self._garden_peak = sum(
                e.portfolio.equity(self._price_at(e.symbol, indice))
                for e in self.bots.values()
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
        assert self.repos is not None and self.population is not None
        assert self.clock is not None

        hasta = self._index_of(self.clock.last_processed_ts or 0) + 1
        ahora_ts = int(self.master.ts[max(0, hasta - 1)])
        incubadora = self._incubators(ahora_ts)
        if incubadora is None:
            # Sin histórico para el walk-forward no se puede criar, pero el
            # jardín no se para por eso: sigue viviendo y vuelve a intentarlo
            # la generación que viene, con una semana más de datos.
            return self._close_without_breeding(generation, hasta, ahora_ts)

        # La actividad va aparte de las métricas: el fitness se mide sobre la
        # ventana deslizante, pero "¿ha operado esta generación?" tiene que
        # seguir siendo de esta generación, o nadie moriría nunca por inactivo.
        actividad = {
            bot_id: len(estado.portfolio.closed_trades)
            for bot_id, estado in self.bots.items()
        }
        outcome = self.population.evolve_generation(
            generation, incubadora, window_metrics=self._window_metrics(),
            scope="live", activity=actividad,
        )

        tick = Tick(ts=ahora_ts, generation=generation, index=max(0, hasta - 1))
        with self.db.transaction():
            for bot_id in outcome.deaths:
                estado = self.bots.pop(bot_id, None)
                if estado is not None:
                    # Las posiciones de un muerto se cierran al precio de cierre
                    # de la vela que cerró la generación, no se abandonan.
                    self._close_all(estado, tick, estado.last_price)
                    self.repos.bots.drop_runtime(bot_id)
                    self._benchmark_units[estado.symbol] = max(
                        0.0,
                        self._benchmark_units.get(estado.symbol, 0.0)
                        - estado.benchmark_units,
                    )
            for bot_id in outcome.births:
                genoma = self.repos.bots.genome_of(bot_id)
                if str(genoma.market.symbol) not in self.series:
                    continue
                estado = self._build_state(bot_id, genoma, first_index=hasta)
                precio = self._price_at(estado.symbol, max(0, hasta - 1))
                if precio > 0:
                    estado.benchmark_units = estado.portfolio.initial_capital / precio
                    self._benchmark_units[estado.symbol] = (
                        self._benchmark_units.get(estado.symbol, 0.0)
                        + estado.benchmark_units
                    )
                self.bots[bot_id] = estado

            capital = sum(e.portfolio.equity(e.last_price) for e in self.bots.values())
            benchmark = self._benchmark_value(max(0, hasta - 1))
            self.repos.generations.close(
                generation,
                {
                    "ended_ts": ahora_ts,
                    "n_ticks": len(next(iter(self.bots.values())).equity_window)
                    if self.bots else 0,
                    "garden_equity": capital,
                    "benchmark_equity": benchmark,
                    "garden_alpha": (capital / benchmark - 1.0) if benchmark > 0 else None,
                    "garden_drawdown": self._garden_drawdown(),
                },
            )
            if self._pending_events:
                self.repos.events.log_many(self._pending_events)
                self._pending_events.clear()

        self._roll_windows(hasta)
        self.generation = generation
        self._snapshot_if_due(generation)
        self._say(outcome.summary_line)
        for aviso in outcome.alerts:
            self._say(f"           aviso: {aviso}")
        return outcome

    def _incubators(self, ahora_ts: Timestamp) -> MultiSymbolIncubator | None:
        """Una incubadora por mercado, cortadas en el momento actual.

        Un mercado sin histórico suficiente para el walk-forward se queda
        fuera: sus candidatos no se pueden cribar y no van a nacer. Si no queda
        ninguno, devuelve ``None`` y esta generación no cría.
        """
        from ..engine.incubator import Incubator, MultiSymbolIncubator
        from ..evaluation.walkforward import NotEnoughData

        assert self.repos is not None
        incubadoras: dict[str, Any] = {}
        for simbolo, serie in self.series.items():
            corte = serie.index_of(ahora_ts)
            velas = serie.candles.iloc[: corte + 1] if corte >= 0 else serie.candles
            incubadora = Incubator(
                cfg=self.cfg, candles=velas, catalog=self.catalog,
                repo=self.repos.incubation,
            )
            try:
                incubadora.split  # noqa: B018 - construye y valida la partición
            except NotEnoughData:
                continue
            incubadoras[simbolo] = incubadora
        if not incubadoras:
            return None
        primario = self.primary if self.primary in incubadoras else next(iter(incubadoras))
        return MultiSymbolIncubator(incubators=incubadoras, primary=primario)

    def _close_without_breeding(
        self, generation: int, hasta: int, ahora_ts: Timestamp
    ) -> None:
        """Cierra la generación sin criar y deja dicho por qué."""
        assert self.repos is not None
        capital = sum(e.portfolio.equity(e.last_price) for e in self.bots.values())
        benchmark = self._benchmark_value(max(0, hasta - 1))
        with self.db.transaction():
            self.repos.generations.close(
                generation,
                {
                    "ended_ts": ahora_ts,
                    "population_size": len(self.bots),
                    "n_ticks": len(next(iter(self.bots.values())).equity_window)
                    if self.bots else 0,
                    "garden_equity": capital,
                    "benchmark_equity": benchmark,
                    "garden_alpha": (capital / benchmark - 1.0) if benchmark > 0 else None,
                    "garden_drawdown": self._garden_drawdown(),
                },
            )
            self.repos.events.log(
                EventType.GENERATION_CLOSED,
                f"generación {generation} cerrada sin criar: no hay histórico "
                f"suficiente para el walk-forward de la incubadora",
                ts=ahora_ts, generation=generation, severity="warn",
            )
        self._roll_windows(hasta)
        self.generation = generation
        self._snapshot_if_due(generation)
        self._say(
            f"gen {generation:>3}  sin criar: la incubadora necesita más histórico"
        )

    def _snapshot_if_due(self, generation: int) -> None:
        """Copia fechada de la base cada ``storage.snapshot_every_generations``.

        Un jardín es un archivo: copiarlo es todo el respaldo que necesita, y
        tener la foto de la generación 40 permite reproducir lo que se decidió
        entonces aunque el jardín haya seguido corriendo.
        """
        cada = int(self.cfg.storage.snapshot_every_generations)
        if cada <= 0 or generation % cada != 0:
            return
        try:
            ruta = self.db.snapshot(f"gen{generation}")
        except Exception as exc:
            self._say(f"  aviso: no se ha podido guardar la copia: {exc}")
            return
        self._say(f"  copia del jardín en {ruta.name}")

    def _current_slice(self, estado: _BotState) -> _Tramo:
        """La generación que se está cerrando, como tramo."""
        return _Tramo(
            start_index=estado.window_start_index,
            equity=estado.equity_window,
            trades=list(estado.portfolio.closed_trades),
            fees_at_start=estado.fees_at_window_start,
        )

    def _roll_windows(self, hasta: int) -> None:
        """Cierra el tramo de esta generación y abre el siguiente.

        El tramo que se cierra pasa a la ventana deslizante; el más antiguo
        cae solo cuando se pasa de ``fitness.live_window_generations``.
        """
        for estado in self.bots.values():
            estado.history.append(self._current_slice(estado))
            estado.equity_window = []
            estado.portfolio.closed_trades = []
            estado.fees_at_window_start = estado.portfolio.total_fees
            estado.window_start_index = hasta
        self._window_start_index = hasta

    def _reference_curve(self, serie: _Series, desde: int, largo: int) -> np.ndarray:
        """El precio del mercado de un bot en cada tick del reloj.

        Cuando ese mercado no tiene vela en un tick se arrastra su último
        cierre conocido, que es exactamente con lo que se valora al bot. Sin
        esto, la serie de referencia de un símbolo secundario iría desfasada
        respecto a su propia curva de capital.
        """
        tramo = serie.local[desde : desde + largo]
        if not tramo.size:
            return np.zeros(0, dtype="float64")
        rellenado = np.maximum.accumulate(tramo)
        rellenado = np.where(rellenado < 0, 0, rellenado)
        return serie.close[rellenado]

    def _window_metrics(self) -> dict[BotId, Metrics]:
        """Métricas de la ventana deslizante, bot a bot.

        La ventana empieza en la generación que se acaba de cerrar y crece
        hacia atrás **sólo hasta reunir ``fitness.min_trades`` operaciones**,
        con el tope de ``fitness.live_window_generations``. Un bot que opera
        mucho se juzga por su última semana; uno selectivo, por el último mes.
        Medir siempre una sola generación dejaba al jardín entero sin fitness
        definido (docs/DECISIONS.md D-030).
        """
        minimo = int(self.cfg.fitness.min_trades)
        salida: dict[BotId, Metrics] = {}
        for bot_id, estado in self.bots.items():
            tramos = [*estado.history, self._current_slice(estado)]
            elegidos: list[_Tramo] = []
            operaciones = 0
            for tramo in reversed(tramos):
                elegidos.insert(0, tramo)
                operaciones += tramo.n_trades
                if operaciones >= minimo:
                    break

            curva = np.asarray(
                [valor for tramo in elegidos for valor in tramo.equity], dtype="float64"
            )
            if curva.size < 2:
                salida[bot_id] = Metrics()
                continue
            operaciones_ventana = [op for tramo in elegidos for op in tramo.trades]
            referencia = self._reference_curve(
                self.series[estado.symbol], elegidos[0].start_index, int(curva.size)
            )
            salida[bot_id] = compute_metrics(
                curva,
                operaciones_ventana,
                self.cfg.market.timeframe,
                total_fees=estado.portfolio.total_fees - elegidos[0].fees_at_start,
                benchmark=referencia if referencia.size == curva.size else None,
            )
        return salida

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
        return [
            bot_id
            for bot_id, estado in self.bots.items()
            if estado.portfolio.drawdown(estado.last_price) > self.cfg.risk.hard_max_drawdown
        ]

    def _kill(self, bot_id: BotId, causa: DeathCause, tick: Tick) -> None:
        """Poda inmediata: el bot deja de operar en el acto."""
        assert self.repos is not None
        estado = self.bots.pop(bot_id, None)
        if estado is None:
            return
        estado.broker.cancel_all()
        self.repos.bots.drop_runtime(bot_id)
        self._benchmark_units[estado.symbol] = max(
            0.0, self._benchmark_units.get(estado.symbol, 0.0) - estado.benchmark_units
        )
        self.repos.bots.set_status(
            bot_id, BotStatus.CULLED, generation=tick.generation, cause=causa
        )
        self._pending_events.append(
            {
                "type": EventType.CIRCUIT_BREAKER, "ts": int(tick.ts),
                "generation": tick.generation, "bot_id": bot_id, "severity": "warn",
                "summary": f"{bot_id} podado en el acto por {causa}",
                "payload": {"cause": str(causa), "symbol": estado.symbol},
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

        Devuelve pares ``(ts, índice)`` ya dentro del reloj del jardín, que es
        lo que el reloj necesita para emitir ticks.
        """
        from ..data.backfill import backfill
        from ..data.sources import VenueClient
        from ..data.store import CandleStore, SeriesKey

        timeframe = self.db.get_meta("timeframe") or self.cfg.market.timeframe
        store = CandleStore(cache_dir=self.cfg.path(self.cfg.storage.cache_dir))
        cliente = VenueClient(venue=self.cfg.market.venue)
        empezado = time.perf_counter()
        for simbolo, serie in self.series.items():
            backfill(
                store, cliente, SeriesKey(self.cfg.market.venue, simbolo, timeframe),
                since=int(serie.ts[-1]) if len(serie.ts) else None,
                jump_threshold=self.cfg.risk.price_jump_anomaly,
            )
        self.db.set_meta("venue_latency_ms", round((time.perf_counter() - empezado) * 1000, 1))

        nuevas = {}
        for simbolo in list(self.series):
            serie = self._load_series(simbolo)
            if serie is not None:
                nuevas[simbolo] = serie
        self.series = nuevas
        self._align_series()
        self._recompile()

        if self.status is GardenStatus.DEGRADED:
            self.status = GardenStatus.RUNNING
            self.db.set_meta("status", str(self.status))
            self.repos.events.clear_alert(  # type: ignore[union-attr]
                str(AlertKind.VENUE_FAILURE), int(time.time() * 1000)
            )
        corte = int(desde) if desde is not None else -1
        return [(int(ts), i) for i, ts in enumerate(self.master.ts) if int(ts) > corte]

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
            nuevo.history = estado.history
            nuevo.window_start_index = estado.window_start_index
            nuevo.benchmark_units = estado.benchmark_units
            nuevo.fees_at_window_start = estado.fees_at_window_start
            nuevo.last_price = estado.last_price
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


__all__ = ("HEALTH_EVERY", "GardenRunner")
