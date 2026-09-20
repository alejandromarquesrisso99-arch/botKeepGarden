"""Repositorios: el único sitio que escribe SQL.

Ni el motor ni la evolución construyen consultas. Todo pasa por aquí, para que
un cambio de esquema tenga un solo sitio que tocar.

CONTRATO — implementar en el hito 4.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from ..evolution.lineage import ParentEdge
from ..genome.schema import Genome
from ..types import BotId, BotStatus, DeathCause, EventType, Timestamp
from .db import Database


@dataclass(slots=True)
class BotRepository:
    db: Database

    def create(self, genome: Genome, *, generation: int, initial_capital: float,
               parents: Sequence[ParentEdge] = ()) -> BotId: ...
    def get(self, bot_id: BotId) -> Any: ...
    def alive(self) -> list[Any]: ...
    def by_status(self, status: BotStatus) -> list[Any]: ...
    def update_equity(self, bot_id: BotId, equity: float, cash: float, peak: float) -> None: ...
    def set_status(self, bot_id: BotId, status: BotStatus,
                   *, generation: int, cause: DeathCause | None = None) -> None: ...
    def set_fitness(self, bot_id: BotId, *, incubator: float | None,
                    live: float | None, effective: float | None) -> None: ...
    def protect(self, bot_id: BotId, until_generation: int) -> None: ...
    def genome_of(self, bot_id: BotId) -> Genome: ...
    def genomes_of(self, bot_ids: Sequence[BotId]) -> dict[BotId, Genome]: ...


@dataclass(slots=True)
class GenerationRepository:
    db: Database

    def open(self, generation: int, started_ts: Timestamp) -> None: ...
    def close(self, generation: int, summary: Mapping[str, Any]) -> None: ...
    def latest(self) -> Any: ...
    def all(self) -> list[Any]: ...
    def get(self, generation: int) -> Any: ...


@dataclass(slots=True)
class MetricsRepository:
    db: Database

    def upsert(self, bot_id: BotId, generation: int, scope: str,
               metrics: Mapping[str, Any]) -> None: ...
    def for_generation(self, generation: int, scope: str = "live") -> list[Any]: ...
    def history(self, bot_id: BotId) -> list[Any]: ...


@dataclass(slots=True)
class TradeRepository:
    db: Database

    def open_trade(self, bot_id: BotId, generation: int, **kw: Any) -> int: ...
    def close_trade(self, trade_id: int, **kw: Any) -> None: ...
    def record_order(self, **kw: Any) -> int:
        """INSERT OR IGNORE sobre ``(bot_id, candle_ts, kind)``.

        Es lo que hace idempotente el reprocesado de una vela tras un reinicio:
        un tick repetido no puede duplicar operaciones.
        """
        ...
    def open_positions(self, bot_id: BotId | None = None) -> list[Any]: ...
    def for_bot(self, bot_id: BotId, limit: int = 500) -> list[Any]: ...
    def for_generation(self, generation: int) -> list[Any]: ...


@dataclass(slots=True)
class EquityRepository:
    db: Database

    def snapshot(self, bot_id: BotId, ts: Timestamp, equity: float,
                 cash: float, position_value: float, drawdown: float) -> None: ...
    def snapshot_garden(self, ts: Timestamp, generation: int, **kw: Any) -> None: ...
    def curve(self, bot_id: BotId, *, max_points: int = 2000) -> list[Any]:
        """Serie de equity decimada en servidor (LTTB) a ``max_points``."""
        ...
    def garden_curve(self, *, max_points: int = 2000) -> list[Any]: ...


@dataclass(slots=True)
class EventRepository:
    db: Database

    def log(self, type: EventType, summary: str, *, ts: Timestamp,
            generation: int | None = None, bot_id: BotId | None = None,
            severity: str = "info", payload: Mapping[str, Any] | None = None) -> int: ...
    def recent(self, limit: int = 200, type: EventType | None = None) -> list[Any]: ...
    def for_bot(self, bot_id: BotId) -> list[Any]: ...
    def raise_alert(self, kind: str, *, ts: Timestamp, generation: int,
                    value: float, threshold: float, detail: str = "") -> int: ...
    def clear_alert(self, kind: str, ts: Timestamp) -> None: ...
    def open_alerts(self) -> list[Any]: ...


@dataclass(slots=True)
class LineageRepository:
    db: Database

    def add_edges(self, edges: Sequence[ParentEdge]) -> None: ...
    def all_edges(self, until_generation: int | None = None) -> list[ParentEdge]: ...
    def parents_of(self, bot_id: BotId) -> list[BotId]: ...
    def children_of(self, bot_id: BotId) -> list[BotId]: ...


@dataclass(slots=True)
class GardenerRepository:
    db: Database

    def open_session(self, generation: int, trigger: str, report_path: str) -> int: ...
    def record_decision(self, session_id: int, proposal: Any) -> int: ...
    def close_session(self, session_id: int, journal_entry: str) -> None: ...
    def pending_reviews(self, generation: int) -> list[Any]:
        """Decisiones cuya generación de revisión ya ha llegado.

        El motor las recoge, mide el efecto real y lo compara con el esperado.
        Es lo que impide que el jardinero repita el mismo consejo cada mes.
        """
        ...
    def record_review(self, decision_id: int, *, generation: int,
                      observed: str, verdict: str) -> None: ...
    def journal(self, limit: int = 20) -> list[Any]: ...
    def operator_success_rates(self, generation: int, window: int = 6) -> dict[str, float]:
        """Fracción de hijos de cada operador que sobreviven 3 generaciones.

        Es la métrica que dice si la evolución está funcionando o sólo generando
        ruido, y va en el punto 7 del informe del jardinero.
        """
        ...


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
            gardener=GardenerRepository(db),
        )


__all__ = (
    "BotRepository", "GenerationRepository", "MetricsRepository", "TradeRepository",
    "EquityRepository", "EventRepository", "LineageRepository", "GardenerRepository",
    "Repositories",
)
