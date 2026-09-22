"""Repositorios: el único sitio que escribe SQL.

Ni el motor ni la evolución construyen consultas. Todo pasa por aquí, para que
un cambio de esquema tenga un solo sitio que tocar.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from ..evolution.lineage import ParentEdge
from ..genome.schema import Genome, genome_hash
from ..ids import bot_id_of, bot_name
from ..types import BotId, BotStatus, BreedOperator, DeathCause, EventType, Timestamp
from .db import Database

#: Columnas de ``bot_metrics`` que se rellenan desde un ``Metrics``. El resto
#: (fitness, rango, Pareto) las pone la selección, no el cálculo.
METRIC_COLUMNS: tuple[str, ...] = (
    "total_return", "cagr", "avg_trade_return", "expectancy",
    "max_drawdown", "ulcer_index", "downside_dev", "time_in_market", "worst_trade",
    "sharpe", "sortino", "calmar", "martin", "profit_factor",
    "n_trades", "win_rate", "turnover", "avg_holding_bars", "fee_drag", "consistency",
    "corr_to_population", "corr_to_benchmark", "novelty",
    "fitness", "fitness_rank", "on_pareto_front",
)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


# --------------------------------------------------------------------------- #
# Bots y genomas                                                               #
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class BotRepository:
    """Bots, sus genomas y su estado en el ciclo de vida."""

    db: Database

    def create(
        self,
        genome: Genome,
        *,
        generation: int,
        initial_capital: float,
        parents: Sequence[ParentEdge] = (),
        status: BotStatus = BotStatus.ALIVE,
    ) -> BotId:
        """Da de alta genoma, bot y parentesco en una sola transacción."""
        bot_id = bot_id_of(genome.id)
        with self.db.transaction():
            self.db.execute(
                "INSERT OR REPLACE INTO genomes (genome_id, hash, family, venue, symbol, "
                "timeframe, is_ensemble, complexity, n_features, max_depth, generation, "
                "operator, root_lineage, payload) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    genome.id,
                    genome_hash(genome),
                    str(genome.family),
                    genome.market.venue,
                    genome.market.symbol,
                    genome.market.timeframe,
                    int(genome.is_ensemble),
                    genome.complexity(),
                    len(genome.features),
                    genome.max_rule_depth(),
                    int(generation),
                    str(genome.meta.operator),
                    genome.meta.root_lineage,
                    _json(genome.to_dict()),
                ),
            )
            self.db.execute(
                "INSERT OR REPLACE INTO bots (bot_id, genome_id, name, status, family, "
                "root_lineage, born_generation, operator, initial_capital, equity, "
                "peak_equity, cash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    bot_id,
                    genome.id,
                    bot_name(bot_id),
                    str(status),
                    str(genome.family),
                    genome.meta.root_lineage,
                    int(generation),
                    str(genome.meta.operator),
                    float(initial_capital),
                    float(initial_capital),
                    float(initial_capital),
                    float(initial_capital),
                ),
            )
            if parents:
                LineageRepository(self.db).add_edges(parents)
        return bot_id

    def get(self, bot_id: BotId) -> sqlite3.Row | None:
        return self.db.query_one("SELECT * FROM bots WHERE bot_id = ?", (bot_id,))

    def alive(self) -> list[sqlite3.Row]:
        return self.db.query(
            "SELECT * FROM bots WHERE status = ? ORDER BY bot_id", (str(BotStatus.ALIVE),)
        )

    def by_status(self, status: BotStatus) -> list[sqlite3.Row]:
        return self.db.query(
            "SELECT * FROM bots WHERE status = ? ORDER BY bot_id", (str(status),)
        )

    def all(self) -> list[sqlite3.Row]:
        return self.db.query("SELECT * FROM bots ORDER BY born_generation, bot_id")

    def count_alive(self) -> int:
        fila = self.db.query_one(
            "SELECT COUNT(*) AS n FROM bots WHERE status = ?", (str(BotStatus.ALIVE),)
        )
        return int(fila["n"]) if fila else 0

    def update_equity(self, bot_id: BotId, equity: float, cash: float, peak: float) -> None:
        self.db.execute(
            "UPDATE bots SET equity = ?, cash = ?, peak_equity = ?, "
            "updated_at = datetime('now') WHERE bot_id = ?",
            (float(equity), float(cash), float(peak), bot_id),
        )

    def set_status(
        self,
        bot_id: BotId,
        status: BotStatus,
        *,
        generation: int,
        cause: DeathCause | None = None,
    ) -> None:
        """Un bot nunca se borra: cambia de estado y su historia permanece."""
        muerto = status in (BotStatus.CULLED, BotStatus.RETIRED, BotStatus.DISCARDED)
        self.db.execute(
            "UPDATE bots SET status = ?, died_generation = ?, death_cause = ?, "
            "updated_at = datetime('now') WHERE bot_id = ?",
            (
                str(status),
                int(generation) if muerto else None,
                str(cause) if cause is not None else None,
                bot_id,
            ),
        )

    def set_fitness(
        self,
        bot_id: BotId,
        *,
        incubator: float | None,
        live: float | None,
        effective: float | None,
    ) -> None:
        self.db.execute(
            "UPDATE bots SET fitness_incubator = ?, fitness_live = ?, "
            "fitness_effective = ?, updated_at = datetime('now') WHERE bot_id = ?",
            (incubator, live, effective, bot_id),
        )

    def set_generation_stats(
        self,
        bot_id: BotId,
        *,
        generations_alive: int,
        idle_generations: int,
        total_trades: int,
        species_id: str | None = None,
        is_elite: bool = False,
        on_pareto_front: bool = False,
    ) -> None:
        self.db.execute(
            "UPDATE bots SET generations_alive = ?, idle_generations = ?, "
            "total_trades = ?, species_id = ?, is_elite = ?, on_pareto_front = ?, "
            "updated_at = datetime('now') WHERE bot_id = ?",
            (
                int(generations_alive), int(idle_generations), int(total_trades),
                species_id, int(is_elite), int(on_pareto_front), bot_id,
            ),
        )

    def protect(self, bot_id: BotId, until_generation: int) -> None:
        self.db.execute(
            "UPDATE bots SET protected_until_gen = ?, updated_at = datetime('now') "
            "WHERE bot_id = ?",
            (int(until_generation), bot_id),
        )

    def genome_of(self, bot_id: BotId) -> Genome:
        fila = self.db.query_one(
            "SELECT g.payload FROM bots b JOIN genomes g ON g.genome_id = b.genome_id "
            "WHERE b.bot_id = ?",
            (bot_id,),
        )
        if fila is None:
            raise KeyError(f"no hay ningún bot con id {bot_id!r}")
        return Genome.from_dict(json.loads(fila["payload"]))

    def genomes_of(self, bot_ids: Sequence[BotId]) -> dict[BotId, Genome]:
        if not bot_ids:
            return {}
        huecos = ",".join("?" for _ in bot_ids)
        filas = self.db.query(
            f"SELECT b.bot_id, g.payload FROM bots b "
            f"JOIN genomes g ON g.genome_id = b.genome_id WHERE b.bot_id IN ({huecos})",
            tuple(bot_ids),
        )
        return {
            row["bot_id"]: Genome.from_dict(json.loads(row["payload"])) for row in filas
        }

    def reweight_ensemble(self, bot_id: BotId, genome: Genome) -> None:
        """Reescribe el payload de un genoma de fusión.

        Es la única excepción a "un bot tiene un genoma y no cambia". No es
        evolución: un ensemble que pierde un miembro sigue siendo el mismo
        organismo, y docs/EVOLUTION.md §FUSION exige que reparta el peso del
        muerto entre los vivos. Mutar produciría un bot nuevo; esto no.
        """
        self.db.execute(
            "UPDATE genomes SET payload = ? WHERE genome_id = "
            "(SELECT genome_id FROM bots WHERE bot_id = ?)",
            (_json(genome.to_dict()), bot_id),
        )

    def alive_genomes(self) -> dict[BotId, Genome]:
        filas = self.db.query(
            "SELECT b.bot_id, g.payload FROM bots b "
            "JOIN genomes g ON g.genome_id = b.genome_id WHERE b.status = ? "
            "ORDER BY b.bot_id",
            (str(BotStatus.ALIVE),),
        )
        return {
            row["bot_id"]: Genome.from_dict(json.loads(row["payload"])) for row in filas
        }

    # -- estado vivo de la cartera ----------------------------------------- #

    def save_runtime(
        self, rows: Sequence[tuple[BotId, Timestamp, Mapping[str, Any]]]
    ) -> None:
        """Vuelca el estado vivo de varias carteras de una vez.

        Se llama una vez por tick desde dentro de la transacción del tick: o
        está el tick entero o no está, igual que las órdenes.
        """
        if not rows:
            return
        self.db.executemany(
            "INSERT INTO bot_runtime (bot_id, ts, payload) VALUES (?,?,?) "
            "ON CONFLICT(bot_id) DO UPDATE SET ts = excluded.ts, "
            "payload = excluded.payload",
            [(bot_id, int(ts), _json(payload)) for bot_id, ts, payload in rows],
        )

    def load_runtime(self, bot_id: BotId) -> dict[str, Any] | None:
        """Lo que dejó escrito la última ejecución, o ``None`` si no hay nada.

        Devuelve ``None`` tanto para un bot recién nacido como para un jardín
        anterior a esta tabla; quien llama decide qué hacer con cada caso.
        """
        fila = self.db.query_one(
            "SELECT payload FROM bot_runtime WHERE bot_id = ?", (bot_id,)
        )
        return None if fila is None else dict(json.loads(fila["payload"]))

    def drop_runtime(self, bot_id: BotId) -> None:
        """Olvida el estado vivo de un bot que acaba de morir."""
        self.db.execute("DELETE FROM bot_runtime WHERE bot_id = ?", (bot_id,))


# --------------------------------------------------------------------------- #
# Generaciones                                                                 #
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class GenerationRepository:
    """La fila por generación: demografía, fitness, diversidad y capital."""

    db: Database

    def open(self, generation: int, started_ts: Timestamp) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO generations (generation, started_ts) VALUES (?, ?)",
            (int(generation), int(started_ts)),
        )

    def close(self, generation: int, summary: Mapping[str, Any]) -> None:
        """Escribe el resumen. Las claves desconocidas se rechazan: una errata
        en el nombre de una métrica la haría desaparecer del dashboard sin que
        nadie se entere."""
        columnas = {
            "ended_ts", "n_ticks", "population_size", "births", "deaths", "fusions",
            "discarded", "n_species", "oldest_bot_age", "fitness_best", "fitness_p75",
            "fitness_median", "fitness_p25", "fitness_worst", "genetic_diversity",
            "family_shares", "lineage_shares", "garden_equity", "benchmark_equity",
            "garden_alpha", "garden_drawdown", "effective_params", "best_bot_id",
        }
        desconocidas = set(summary) - columnas
        if desconocidas:
            raise KeyError(
                f"columnas desconocidas al cerrar la generación {generation}: "
                f"{sorted(desconocidas)}"
            )
        datos = {
            k: (_json(v) if isinstance(v, (dict, list)) else v)
            for k, v in summary.items()
        }
        asignaciones = ", ".join(f"{k} = ?" for k in datos)
        self.db.execute(
            "INSERT OR IGNORE INTO generations (generation, started_ts) VALUES (?, 0)",
            (int(generation),),
        )
        self.db.execute(
            f"UPDATE generations SET {asignaciones}, closed_at = datetime('now') "
            f"WHERE generation = ?",
            (*datos.values(), int(generation)),
        )

    def latest(self) -> sqlite3.Row | None:
        return self.db.query_one(
            "SELECT * FROM generations ORDER BY generation DESC LIMIT 1"
        )

    def all(self) -> list[sqlite3.Row]:
        return self.db.query("SELECT * FROM generations ORDER BY generation")

    def get(self, generation: int) -> sqlite3.Row | None:
        return self.db.query_one(
            "SELECT * FROM generations WHERE generation = ?", (int(generation),)
        )

    def record_species(self, generation: int, species: Sequence[Any]) -> None:
        """Guarda las especies de una generación, **en su orden**.

        El orden no es decorativo: ``speciate`` recorre las especies heredadas
        y mete cada bot en la primera cuyo representante le queda cerca. Sin
        él, un jardín reanudado agrupa distinto (docs/DECISIONS.md D-035).
        """
        self.db.executemany(
            "INSERT OR REPLACE INTO species (species_id, generation, representative_id, "
            "dominant_family, size, mean_fitness, shared_fitness, breeding_quota, "
            "mean_age, ordinal) VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    sp.species_id, int(generation), sp.representative,
                    str(sp.dominant_family) if sp.dominant_family else None,
                    len(sp.members), sp.mean_fitness, sp.shared_fitness,
                    sp.breeding_quota, sp.mean_age, orden,
                )
                for orden, sp in enumerate(species)
            ],
        )

    def species_of(self, generation: int) -> list[sqlite3.Row]:
        return self.db.query(
            "SELECT * FROM species WHERE generation = ? ORDER BY size DESC, species_id",
            (int(generation),),
        )


# --------------------------------------------------------------------------- #
# Métricas                                                                     #
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class MetricsRepository:
    """Una fila por (bot, generación, ámbito). Alimenta todo el dashboard."""

    db: Database

    def upsert(
        self, bot_id: BotId, generation: int, scope: str, metrics: Mapping[str, Any]
    ) -> None:
        datos = {k: metrics.get(k) for k in METRIC_COLUMNS if k in metrics}
        columnas = ["bot_id", "generation", "scope", *datos]
        valores = [bot_id, int(generation), scope, *datos.values()]
        huecos = ",".join("?" for _ in columnas)
        self.db.execute(
            f"INSERT OR REPLACE INTO bot_metrics ({','.join(columnas)}) VALUES ({huecos})",
            valores,
        )

    def for_generation(self, generation: int, scope: str = "live") -> list[sqlite3.Row]:
        return self.db.query(
            "SELECT * FROM bot_metrics WHERE generation = ? AND scope = ? "
            "ORDER BY fitness DESC NULLS LAST, bot_id",
            (int(generation), scope),
        )

    def history(self, bot_id: BotId) -> list[sqlite3.Row]:
        return self.db.query(
            "SELECT * FROM bot_metrics WHERE bot_id = ? ORDER BY generation", (bot_id,)
        )

    def get(self, bot_id: BotId, generation: int, scope: str = "live") -> sqlite3.Row | None:
        return self.db.query_one(
            "SELECT * FROM bot_metrics WHERE bot_id = ? AND generation = ? AND scope = ?",
            (bot_id, int(generation), scope),
        )


# --------------------------------------------------------------------------- #
# Órdenes y operaciones                                                        #
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class TradeRepository:
    """Órdenes ejecutadas y operaciones abiertas y cerradas."""

    db: Database

    def open_trade(self, bot_id: BotId, generation: int, **kw: Any) -> int:
        cur = self.db.execute(
            "INSERT INTO trades (bot_id, generation, side, open_ts, open_price, amount, "
            "stop_price, take_price, is_open) VALUES (?,?,?,?,?,?,?,?,1)",
            (
                bot_id, int(generation), str(kw["side"]), int(kw["open_ts"]),
                float(kw["open_price"]), float(kw["amount"]),
                kw.get("stop_price"), kw.get("take_price"),
            ),
        )
        return int(cur.lastrowid or 0)

    def close_trade(self, trade_id: int, **kw: Any) -> None:
        self.db.execute(
            "UPDATE trades SET close_ts = ?, close_price = ?, exit_kind = ?, "
            "holding_bars = ?, pnl_gross = ?, pnl_net = ?, fees = ?, return_pct = ?, "
            "r_multiple = ?, is_open = 0 WHERE trade_id = ?",
            (
                int(kw["close_ts"]), float(kw["close_price"]), str(kw.get("exit_kind", "")),
                kw.get("holding_bars"), kw.get("pnl_gross"), kw.get("pnl_net"),
                float(kw.get("fees", 0.0)), kw.get("return_pct"), kw.get("r_multiple"),
                int(trade_id),
            ),
        )

    def record_order(self, **kw: Any) -> int:
        """INSERT OR IGNORE sobre ``(bot_id, candle_ts, kind)``.

        Es lo que hace idempotente el reprocesado de una vela tras un reinicio:
        un tick repetido no puede duplicar operaciones.

        Devuelve el ``order_id`` nuevo, o **0 si la orden ya estaba**: así
        quien llama sabe que esa vela ya se procesó y no debe tocar ``trades``.
        Sin esa distinción ``lastrowid`` devuelve el id de la última inserción
        que sí ocurrió, que no tiene nada que ver con esta orden.
        """
        cur = self.db.execute(
            "INSERT OR IGNORE INTO orders (bot_id, trade_id, candle_ts, fill_ts, kind, "
            "side, price, reference_price, slippage, amount, notional, fee) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                kw["bot_id"], kw.get("trade_id"), int(kw["candle_ts"]), int(kw["fill_ts"]),
                str(kw["kind"]), str(kw["side"]), float(kw["price"]),
                float(kw.get("reference_price", kw["price"])), float(kw.get("slippage", 0.0)),
                float(kw["amount"]), float(kw.get("notional", 0.0)), float(kw.get("fee", 0.0)),
            ),
        )
        if cur.rowcount == 0:
            return 0
        return int(cur.lastrowid or 0)

    def link_order(self, order_id: int, trade_id: int) -> None:
        """Ata una orden ya registrada a la operación que abrió.

        La orden se escribe antes que la operación —es ella quien dice si la
        vela ya se había procesado—, así que el vínculo se cierra después.
        """
        self.db.execute(
            "UPDATE orders SET trade_id = ? WHERE order_id = ?",
            (int(trade_id), int(order_id)),
        )

    def open_positions(self, bot_id: BotId | None = None) -> list[sqlite3.Row]:
        if bot_id is None:
            return self.db.query("SELECT * FROM trades WHERE is_open = 1 ORDER BY bot_id")
        return self.db.query(
            "SELECT * FROM trades WHERE is_open = 1 AND bot_id = ?", (bot_id,)
        )

    def for_bot(self, bot_id: BotId, limit: int = 500) -> list[sqlite3.Row]:
        return self.db.query(
            "SELECT * FROM trades WHERE bot_id = ? ORDER BY open_ts DESC LIMIT ?",
            (bot_id, int(limit)),
        )

    def for_generation(self, generation: int) -> list[sqlite3.Row]:
        return self.db.query(
            "SELECT * FROM trades WHERE generation = ? ORDER BY open_ts",
            (int(generation),),
        )

    def record_closed(
        self, bot_id: BotId, generation: int, trades: Sequence[Mapping[str, Any]]
    ) -> None:
        """Vuelca de golpe las operaciones cerradas que devuelve un backtest."""
        if not trades:
            return
        self.db.executemany(
            "INSERT INTO trades (bot_id, generation, side, open_ts, close_ts, open_price, "
            "close_price, amount, exit_kind, holding_bars, pnl_gross, pnl_net, fees, "
            "return_pct, r_multiple, is_open) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
            [
                (
                    bot_id, int(generation), str(t.get("side", "LONG")),
                    int(t.get("entry_ts", 0)), int(t.get("exit_ts", 0)),
                    float(t.get("entry_price", 0.0)), float(t.get("exit_price", 0.0)),
                    float(t.get("amount", 0.0)), str(t.get("exit_kind", "")),
                    int(t.get("bars_held", 0)), float(t.get("gross_pnl", 0.0)),
                    float(t.get("pnl", 0.0)), float(t.get("fees", 0.0)),
                    float(t.get("return", 0.0)), t.get("r_multiple"),
                )
                for t in trades
            ],
        )


# --------------------------------------------------------------------------- #
# Curvas de capital                                                            #
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class EquityRepository:
    """Las curvas: por bot y agregada del jardín."""

    db: Database

    def snapshot(
        self,
        bot_id: BotId,
        ts: Timestamp,
        equity: float,
        cash: float,
        position_value: float,
        drawdown: float,
    ) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO equity_snapshots (bot_id, ts, equity, cash, "
            "position_value, drawdown) VALUES (?,?,?,?,?,?)",
            (bot_id, int(ts), float(equity), float(cash), float(position_value), float(drawdown)),
        )

    def snapshot_many(self, bot_id: BotId, filas: Sequence[Sequence[Any]]) -> None:
        """Vuelca una curva entera. Es la tabla que más crece del jardín."""
        if not filas:
            return
        self.db.executemany(
            "INSERT OR REPLACE INTO equity_snapshots (bot_id, ts, equity, cash, "
            "position_value, drawdown) VALUES (?,?,?,?,?,?)",
            [(bot_id, *fila) for fila in filas],
        )

    def snapshot_garden(self, ts: Timestamp, generation: int, **kw: Any) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO garden_equity (ts, generation, garden_equity, "
            "benchmark_equity, n_alive, n_open_positions, garden_drawdown) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                int(ts), int(generation), float(kw.get("garden_equity", 0.0)),
                float(kw.get("benchmark_equity", 0.0)), int(kw.get("n_alive", 0)),
                int(kw.get("n_open_positions", 0)), float(kw.get("garden_drawdown", 0.0)),
            ),
        )

    def curve(self, bot_id: BotId, *, max_points: int = 2000) -> list[sqlite3.Row]:
        """Serie de equity decimada en servidor (LTTB) a ``max_points``."""
        return self._decimate(
            "SELECT ts, equity FROM equity_snapshots WHERE bot_id = ? ORDER BY ts",
            (bot_id,),
            max_points,
        )

    def garden_curve(self, *, max_points: int = 2000) -> list[sqlite3.Row]:
        return self._decimate(
            "SELECT ts, garden_equity AS equity, benchmark_equity FROM garden_equity "
            "ORDER BY ts",
            (),
            max_points,
        )

    def _decimate(
        self, sql: str, params: Sequence[Any], max_points: int
    ) -> list[sqlite3.Row]:
        filas = self.db.query(sql, params)
        return lttb(filas, max_points)


def lttb(filas: Sequence[sqlite3.Row], max_points: int) -> list[sqlite3.Row]:
    """Decimación *Largest Triangle Three Buckets*.

    Un muestreo cada N puntos se come los picos, que es justo lo que hay que
    ver en una curva de capital: el máximo y el suelo de un drawdown. LTTB
    conserva la silueta eligiendo de cada tramo el punto que forma el triángulo
    de mayor área con sus vecinos.
    """
    n = len(filas)
    if max_points <= 2 or n <= max_points:
        return list(filas)

    def x(i: int) -> float:
        return float(filas[i]["ts"])

    def y(i: int) -> float:
        return float(filas[i]["equity"])

    salida = [filas[0]]
    paso = (n - 2) / (max_points - 2)
    a = 0
    for i in range(max_points - 2):
        inicio = int((i + 1) * paso) + 1
        fin = min(int((i + 2) * paso) + 1, n - 1)
        siguiente_inicio = fin
        siguiente_fin = min(int((i + 3) * paso) + 1, n)
        if siguiente_inicio >= siguiente_fin:
            siguiente_inicio, siguiente_fin = n - 1, n
        media_x = sum(x(j) for j in range(siguiente_inicio, siguiente_fin)) / (
            siguiente_fin - siguiente_inicio
        )
        media_y = sum(y(j) for j in range(siguiente_inicio, siguiente_fin)) / (
            siguiente_fin - siguiente_inicio
        )

        mejor, mejor_area = inicio, -1.0
        for j in range(inicio, max(fin, inicio + 1)):
            area = abs(
                (x(a) - media_x) * (y(j) - y(a)) - (x(a) - x(j)) * (media_y - y(a))
            )
            if area > mejor_area:
                mejor, mejor_area = j, area
        salida.append(filas[mejor])
        a = mejor

    salida.append(filas[-1])
    return salida


# --------------------------------------------------------------------------- #
# Eventos y alertas                                                            #
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class EventRepository:
    """La historia completa del jardín. El dashboard la lee, no la recalcula."""

    db: Database

    def log(
        self,
        type: EventType,
        summary: str,
        *,
        ts: Timestamp,
        generation: int | None = None,
        bot_id: BotId | None = None,
        severity: str = "info",
        payload: Mapping[str, Any] | None = None,
    ) -> int:
        cur = self.db.execute(
            "INSERT INTO events (ts, generation, type, bot_id, severity, summary, payload) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                int(ts),
                None if generation is None else int(generation),
                str(type),
                bot_id,
                severity,
                summary,
                _json(dict(payload)) if payload else None,
            ),
        )
        return int(cur.lastrowid or 0)

    def log_many(self, eventos: Sequence[Mapping[str, Any]]) -> None:
        if not eventos:
            return
        self.db.executemany(
            "INSERT INTO events (ts, generation, type, bot_id, severity, summary, payload) "
            "VALUES (?,?,?,?,?,?,?)",
            [
                (
                    int(e["ts"]),
                    None if e.get("generation") is None else int(e["generation"]),
                    str(e["type"]), e.get("bot_id"), e.get("severity", "info"),
                    e["summary"],
                    _json(dict(e["payload"])) if e.get("payload") else None,
                )
                for e in eventos
            ],
        )

    def recent(self, limit: int = 200, type: EventType | None = None) -> list[sqlite3.Row]:
        if type is None:
            return self.db.query(
                "SELECT * FROM events ORDER BY event_id DESC LIMIT ?", (int(limit),)
            )
        return self.db.query(
            "SELECT * FROM events WHERE type = ? ORDER BY event_id DESC LIMIT ?",
            (str(type), int(limit)),
        )

    def for_bot(self, bot_id: BotId) -> list[sqlite3.Row]:
        return self.db.query(
            "SELECT * FROM events WHERE bot_id = ? ORDER BY event_id", (bot_id,)
        )

    def for_generation(self, generation: int) -> list[sqlite3.Row]:
        return self.db.query(
            "SELECT * FROM events WHERE generation = ? ORDER BY event_id",
            (int(generation),),
        )

    def raise_alert(
        self,
        kind: str,
        *,
        ts: Timestamp,
        generation: int,
        value: float,
        threshold: float,
        detail: str = "",
    ) -> int:
        """Una alerta abierta no se duplica: se actualiza la que ya está."""
        abierta = self.db.query_one(
            "SELECT alert_id FROM alerts WHERE kind = ? AND cleared_ts IS NULL", (str(kind),)
        )
        if abierta is not None:
            self.db.execute(
                "UPDATE alerts SET value = ?, threshold = ?, detail = ? WHERE alert_id = ?",
                (float(value), float(threshold), detail, int(abierta["alert_id"])),
            )
            return int(abierta["alert_id"])
        cur = self.db.execute(
            "INSERT INTO alerts (kind, raised_ts, raised_gen, value, threshold, detail) "
            "VALUES (?,?,?,?,?,?)",
            (str(kind), int(ts), int(generation), float(value), float(threshold), detail),
        )
        return int(cur.lastrowid or 0)

    def clear_alert(self, kind: str, ts: Timestamp) -> None:
        self.db.execute(
            "UPDATE alerts SET cleared_ts = ? WHERE kind = ? AND cleared_ts IS NULL",
            (int(ts), str(kind)),
        )

    def open_alerts(self) -> list[sqlite3.Row]:
        return self.db.query("SELECT * FROM v_open_alerts")


# --------------------------------------------------------------------------- #
# Genealogía                                                                   #
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class LineageRepository:
    """El parentesco: la tabla que el dashboard pinta como genealogía."""

    db: Database

    def add_edges(self, edges: Sequence[ParentEdge]) -> None:
        if not edges:
            return
        self.db.executemany(
            "INSERT OR IGNORE INTO parentage (child_id, parent_id, operator, weight, ordinal) "
            "VALUES (?,?,?,?,?)",
            [
                (e.child, e.parent, str(e.operator), float(e.weight), int(e.ordinal))
                for e in edges
            ],
        )

    def all_edges(self, until_generation: int | None = None) -> list[ParentEdge]:
        if until_generation is None:
            filas = self.db.query("SELECT * FROM parentage")
        else:
            filas = self.db.query(
                "SELECT p.* FROM parentage p JOIN bots b ON b.bot_id = p.child_id "
                "WHERE b.born_generation <= ?",
                (int(until_generation),),
            )
        return [
            ParentEdge(
                parent=row["parent_id"],
                child=row["child_id"],
                operator=BreedOperator(row["operator"]),
                weight=float(row["weight"]),
                ordinal=int(row["ordinal"]),
            )
            for row in filas
        ]

    def parents_of(self, bot_id: BotId) -> list[BotId]:
        return [
            row["parent_id"]
            for row in self.db.query(
                "SELECT parent_id FROM parentage WHERE child_id = ? ORDER BY ordinal",
                (bot_id,),
            )
        ]

    def children_of(self, bot_id: BotId) -> list[BotId]:
        return [
            row["child_id"]
            for row in self.db.query(
                "SELECT child_id FROM parentage WHERE parent_id = ?", (bot_id,)
            )
        ]


# --------------------------------------------------------------------------- #
# Incubadora                                                                   #
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class IncubationRepository:
    """El registro de la criba. Lo que NO nació también es información."""

    db: Database

    def record(
        self, generation: int, result: Any, *, n_candidates: int, parents: Sequence[str] = ()
    ) -> int:
        genome = result.genome
        cur = self.db.execute(
            "INSERT INTO incubation_runs (generation, genome_id, genome_hash, family, "
            "operator, parents, passed, reject_reason, median_sortino, median_drawdown, "
            "median_trades, oos_decay, threshold_used, n_candidates, fold_metrics, "
            "duration_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                int(generation), genome.id, genome_hash(genome), str(genome.family),
                str(genome.meta.operator), _json(list(parents)), int(result.passed),
                result.reject_reason, result.median_sortino, result.median_drawdown,
                result.median_trades, result.oos_decay, result.threshold_used,
                int(n_candidates), _json(result.fold_metrics), int(result.duration_ms),
            ),
        )
        return int(cur.lastrowid or 0)

    def record_many(
        self, generation: int, results: Sequence[Any], *, n_candidates: int
    ) -> None:
        for r in results:
            self.record(
                generation, r, n_candidates=n_candidates, parents=list(r.genome.meta.parents)
            )

    def for_generation(self, generation: int) -> list[sqlite3.Row]:
        return self.db.query(
            "SELECT * FROM incubation_runs WHERE generation = ? ORDER BY run_id",
            (int(generation),),
        )

    def rejection_reasons(self, generation: int) -> dict[str, int]:
        filas = self.db.query(
            "SELECT reject_reason, COUNT(*) AS n FROM incubation_runs "
            "WHERE generation = ? AND passed = 0 GROUP BY reject_reason ORDER BY n DESC",
            (int(generation),),
        )
        return {str(row["reject_reason"]): int(row["n"]) for row in filas}

    def record_holdout(
        self,
        bot_id: BotId,
        *,
        generation: int,
        evaluated_ts: Timestamp,
        sortino: float,
        max_drawdown: float,
        n_trades: int,
        decay: float,
        promoted: bool,
    ) -> None:
        """Escribe la fila del holdout. Una sola vez por bot: si aparece una
        segunda, alguien está mirando el holdout más de una vez y eso es un bug.
        La clave primaria de la tabla lo impide."""
        self.db.execute(
            "INSERT INTO holdout_results (bot_id, generation, evaluated_ts, sortino, "
            "max_drawdown, n_trades, decay, promoted) VALUES (?,?,?,?,?,?,?,?)",
            (
                bot_id, int(generation), int(evaluated_ts), float(sortino),
                float(max_drawdown), int(n_trades), float(decay), int(promoted),
            ),
        )

    def holdout_of(self, bot_id: BotId) -> sqlite3.Row | None:
        return self.db.query_one(
            "SELECT * FROM holdout_results WHERE bot_id = ?", (bot_id,)
        )


# --------------------------------------------------------------------------- #
# El jardinero                                                                 #
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class GardenerRepository:
    """Sesiones, decisiones y su revisión posterior."""

    db: Database

    def open_session(self, generation: int, trigger: str, report_path: str) -> int:
        import time

        cur = self.db.execute(
            "INSERT INTO gardener_sessions (generation, opened_ts, trigger, report_path) "
            "VALUES (?,?,?,?)",
            (int(generation), int(time.time() * 1000), trigger, report_path),
        )
        return int(cur.lastrowid or 0)

    def record_decision(self, session_id: int, proposal: Any) -> int:
        cur = self.db.execute(
            "INSERT INTO gardener_decisions (session_id, generation, kind, status, target, "
            "payload, rationale, expected_effect, review_in_generations, rejection_reason) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                int(session_id),
                int(self.db.get_meta("current_generation") or 0),
                str(proposal.kind), str(proposal.status), proposal.target,
                _json(proposal.payload), proposal.rationale, proposal.expected_effect,
                int(proposal.review_in_generations), proposal.rejection_reason or None,
            ),
        )
        return int(cur.lastrowid or 0)

    def close_session(self, session_id: int, journal_entry: str) -> None:
        import time

        self.db.execute(
            "UPDATE gardener_sessions SET closed_ts = ?, journal_entry = ? "
            "WHERE session_id = ?",
            (int(time.time() * 1000), journal_entry, int(session_id)),
        )

    def pending_reviews(self, generation: int) -> list[sqlite3.Row]:
        """Decisiones cuya generación de revisión ya ha llegado.

        El motor las recoge, mide el efecto real y lo compara con el esperado.
        Es lo que impide que el jardinero repita el mismo consejo cada mes.
        """
        return self.db.query(
            "SELECT * FROM gardener_decisions WHERE status = 'APPLIED' "
            "AND reviewed_generation IS NULL "
            "AND generation + review_in_generations <= ? ORDER BY decision_id",
            (int(generation),),
        )

    def record_review(
        self, decision_id: int, *, generation: int, observed: str, verdict: str
    ) -> None:
        self.db.execute(
            "UPDATE gardener_decisions SET reviewed_generation = ?, observed_effect = ?, "
            "verdict = ?, status = 'REVIEWED' WHERE decision_id = ?",
            (int(generation), observed, verdict, int(decision_id)),
        )

    def journal(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.db.query(
            "SELECT * FROM gardener_sessions WHERE journal_entry IS NOT NULL "
            "ORDER BY session_id DESC LIMIT ?",
            (int(limit),),
        )

    def operator_success_rates(self, generation: int, window: int = 6) -> dict[str, float]:
        """Fracción de hijos de cada operador que sobreviven 3 generaciones.

        Es la métrica que dice si la evolución está funcionando o sólo generando
        ruido, y va en el punto 7 del informe del jardinero. Sólo cuenta a los
        bots que ya han tenido tiempo de cumplir las tres generaciones: incluir
        a los recién nacidos haría que todo operador pareciera infalible.
        """
        desde = max(0, int(generation) - int(window))
        hasta = int(generation) - 3
        if hasta < desde:
            return {}
        filas = self.db.query(
            "SELECT operator, "
            "  COUNT(*) AS nacidos, "
            "  SUM(CASE WHEN died_generation IS NULL "
            "            OR died_generation >= born_generation + 3 THEN 1 ELSE 0 END) "
            "    AS supervivientes "
            "FROM bots WHERE born_generation BETWEEN ? AND ? GROUP BY operator",
            (desde, hasta),
        )
        return {
            str(row["operator"]): float(row["supervivientes"]) / float(row["nacidos"])
            for row in filas
            if int(row["nacidos"]) > 0
        }


# --------------------------------------------------------------------------- #
# Agrupador                                                                    #
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Repositories:
    """Agrupador. Se pasa entero al motor y al dashboard."""

    db: Database
    bots: BotRepository
    generations: GenerationRepository
    metrics: MetricsRepository
    trades: TradeRepository
    equity: EquityRepository
    events: EventRepository
    lineage: LineageRepository
    incubation: IncubationRepository
    gardener: GardenerRepository

    @staticmethod
    def open(db: Database) -> "Repositories":
        return Repositories(
            db=db,
            bots=BotRepository(db),
            generations=GenerationRepository(db),
            metrics=MetricsRepository(db),
            trades=TradeRepository(db),
            equity=EquityRepository(db),
            events=EventRepository(db),
            lineage=LineageRepository(db),
            incubation=IncubationRepository(db),
            gardener=GardenerRepository(db),
        )


__all__ = (
    "METRIC_COLUMNS",
    "BotRepository", "GenerationRepository", "MetricsRepository", "TradeRepository",
    "EquityRepository", "EventRepository", "LineageRepository", "IncubationRepository",
    "GardenerRepository", "Repositories", "lttb",
)
