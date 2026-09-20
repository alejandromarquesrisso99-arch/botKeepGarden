"""Tipos y enumeraciones compartidas por todo el jardín.

Este módulo no importa nada del resto del paquete: es la base de la pirámide de
dependencias descrita en docs/ARCHITECTURE.md §6.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal, TypeAlias

# --------------------------------------------------------------------------- #
# Alias                                                                        #
# --------------------------------------------------------------------------- #

BotId: TypeAlias = str
GenomeId: TypeAlias = str
LineageId: TypeAlias = str
SpeciesId: TypeAlias = str
Timestamp: TypeAlias = int  # milisegundos UTC, apertura de la vela


class StrEnum(str, Enum):
    """Enum que serializa a su valor en JSON sin ceremonia."""

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


# --------------------------------------------------------------------------- #
# Genoma                                                                       #
# --------------------------------------------------------------------------- #


class IdeaFamily(StrEnum):
    """Familia de ideas de una estrategia. Ver docs/GENOME.md."""

    TREND = "TREND"
    MEAN_REVERSION = "MEAN_REVERSION"
    BREAKOUT = "BREAKOUT"
    MOMENTUM = "MOMENTUM"
    VOLATILITY = "VOLATILITY"
    MICROSTRUCTURE = "MICROSTRUCTURE"
    HYBRID = "HYBRID"


class LogicOp(StrEnum):
    """Nodos lógicos de un árbol de reglas."""

    AND = "AND"
    OR = "OR"
    NOT = "NOT"


class CompareOp(StrEnum):
    """Nodos hoja de un árbol de reglas."""

    GT = "GT"
    GTE = "GTE"
    LT = "LT"
    LTE = "LTE"
    CROSS_ABOVE = "CROSS_ABOVE"
    CROSS_BELOW = "CROSS_BELOW"
    RISING = "RISING"
    FALLING = "FALLING"
    BETWEEN = "BETWEEN"
    PCT_RANK_GT = "PCT_RANK_GT"
    PCT_RANK_LT = "PCT_RANK_LT"


#: Operadores que no usan el operando derecho.
UNARY_COMPARE_OPS: frozenset[CompareOp] = frozenset(
    {CompareOp.RISING, CompareOp.FALLING}
)

#: Operadores que necesitan dos valores a la derecha (lo, hi).
RANGE_COMPARE_OPS: frozenset[CompareOp] = frozenset({CompareOp.BETWEEN})


class PriceField(StrEnum):
    """Campos de la vela que un operando puede referenciar directamente."""

    OPEN = "open"
    HIGH = "high"
    LOW = "low"
    CLOSE = "close"
    VOLUME = "volume"
    HLC3 = "hlc3"
    OHLC4 = "ohlc4"


class SizingKind(StrEnum):
    FIXED_FRACTION = "FIXED_FRACTION"
    ATR_RISK = "ATR_RISK"
    VOL_TARGET = "VOL_TARGET"


class StopKind(StrEnum):
    NONE = "NONE"
    PERCENT = "PERCENT"
    ATR_MULT = "ATR_MULT"


class TakeProfitKind(StrEnum):
    NONE = "NONE"
    PERCENT = "PERCENT"
    ATR_MULT = "ATR_MULT"
    R_MULTIPLE = "R_MULTIPLE"


class TrailingKind(StrEnum):
    NONE = "NONE"
    PERCENT = "PERCENT"
    ATR_MULT = "ATR_MULT"
    CHANDELIER = "CHANDELIER"


class CombineMode(StrEnum):
    """Cómo un bot fusionado combina las señales de sus padres."""

    VOTE = "VOTE"
    WEIGHTED = "WEIGHTED"
    UNANIMOUS = "UNANIMOUS"
    ANY = "ANY"


# --------------------------------------------------------------------------- #
# Evolución                                                                    #
# --------------------------------------------------------------------------- #


class BreedOperator(StrEnum):
    """Operador que dio origen a un genoma."""

    SEED = "SEED"
    MUTATE = "MUTATE"
    CROSSOVER = "CROSSOVER"
    FUSION = "FUSION"
    GRAFT = "GRAFT"


class MutationKind(StrEnum):
    TWEAK_PARAM = "TWEAK_PARAM"
    TWEAK_THRESHOLD = "TWEAK_THRESHOLD"
    TWEAK_RISK = "TWEAK_RISK"
    ADD_CONDITION = "ADD_CONDITION"
    DROP_CONDITION = "DROP_CONDITION"
    SWAP_INDICATOR = "SWAP_INDICATOR"
    TOGGLE_REGIME = "TOGGLE_REGIME"
    TOGGLE_SHORT = "TOGGLE_SHORT"


class BotStatus(StrEnum):
    """Estado en el ciclo de vida. Ver docs/ARCHITECTURE.md §3."""

    CONCEIVED = "CONCEIVED"
    INCUBATING = "INCUBATING"
    DISCARDED = "DISCARDED"
    ALIVE = "ALIVE"
    CULLED = "CULLED"
    RETIRED = "RETIRED"


class DeathCause(StrEnum):
    LOW_FITNESS = "LOW_FITNESS"
    DRAWDOWN_BREAKER = "DRAWDOWN_BREAKER"
    IDLE = "IDLE"
    CLONE = "CLONE"
    GARDENER = "GARDENER"
    ORPHAN_ENSEMBLE = "ORPHAN_ENSEMBLE"
    FAILED_INCUBATION = "FAILED_INCUBATION"


# --------------------------------------------------------------------------- #
# Ejecución                                                                    #
# --------------------------------------------------------------------------- #


class Signal(StrEnum):
    NONE = "NONE"
    ENTER_LONG = "ENTER_LONG"
    EXIT_LONG = "EXIT_LONG"
    ENTER_SHORT = "ENTER_SHORT"
    EXIT_SHORT = "EXIT_SHORT"


class Side(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


class OrderKind(StrEnum):
    ENTRY = "ENTRY"
    EXIT_SIGNAL = "EXIT_SIGNAL"
    EXIT_STOP = "EXIT_STOP"
    EXIT_TAKE_PROFIT = "EXIT_TAKE_PROFIT"
    EXIT_TRAILING = "EXIT_TRAILING"
    EXIT_TIME = "EXIT_TIME"
    EXIT_FORCED = "EXIT_FORCED"


#: Órdenes que cierran una posición.
EXIT_ORDER_KINDS: frozenset[OrderKind] = frozenset(
    k for k in OrderKind if k is not OrderKind.ENTRY
)


class ExecutionMode(StrEnum):
    PAPER = "PAPER"
    LIVE = "LIVE"


class GardenStatus(StrEnum):
    RUNNING = "RUNNING"
    DEGRADED = "DEGRADED"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"


# --------------------------------------------------------------------------- #
# Eventos y jardinero                                                          #
# --------------------------------------------------------------------------- #


class EventType(StrEnum):
    BOT_CONCEIVED = "BOT_CONCEIVED"
    BOT_BORN = "BOT_BORN"
    BOT_DISCARDED = "BOT_DISCARDED"
    BOT_DIED = "BOT_DIED"
    BOT_PROTECTED = "BOT_PROTECTED"
    FUSION_CREATED = "FUSION_CREATED"
    FUSION_REWEIGHTED = "FUSION_REWEIGHTED"
    TRADE_OPENED = "TRADE_OPENED"
    TRADE_CLOSED = "TRADE_CLOSED"
    GENERATION_CLOSED = "GENERATION_CLOSED"
    CIRCUIT_BREAKER = "CIRCUIT_BREAKER"
    DATA_ANOMALY = "DATA_ANOMALY"
    GARDENER_SESSION = "GARDENER_SESSION"
    GARDENER_PROPOSAL = "GARDENER_PROPOSAL"
    ALERT_RAISED = "ALERT_RAISED"
    ALERT_CLEARED = "ALERT_CLEARED"


class ProposalKind(StrEnum):
    """Acciones que el jardinero puede proponer. Ver docs/GARDENER_PROTOCOL.md."""

    GRAFT = "GRAFT"
    SEED_FAMILY = "SEED_FAMILY"
    FUSE = "FUSE"
    RETIRE = "RETIRE"
    PROTECT = "PROTECT"
    TUNE = "TUNE"
    ADD_GENE = "ADD_GENE"
    REBALANCE_QUOTAS = "REBALANCE_QUOTAS"
    NOTE = "NOTE"


class ProposalStatus(StrEnum):
    PENDING = "PENDING"
    APPLIED = "APPLIED"
    REJECTED = "REJECTED"
    REVIEWED = "REVIEWED"


class AlertKind(StrEnum):
    DIVERSITY_FLOOR = "DIVERSITY_FLOOR"
    GARDEN_DRAWDOWN = "GARDEN_DRAWDOWN"
    FITNESS_STAGNATION = "FITNESS_STAGNATION"
    FAMILY_QUOTA = "FAMILY_QUOTA"
    HOLDOUT_DECAY = "HOLDOUT_DECAY"
    VENUE_FAILURE = "VENUE_FAILURE"
    DATA_GAP = "DATA_GAP"


#: Timeframes soportados.
Timeframe: TypeAlias = Literal["1m", "5m", "15m", "1h", "4h", "1d"]

#: Milisegundos por timeframe.
TIMEFRAME_MS: dict[str, int] = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}

#: Periodos por año, usado para anualizar métricas.
PERIODS_PER_YEAR: dict[str, float] = {
    "1m": 525_600.0,
    "5m": 105_120.0,
    "15m": 35_040.0,
    "1h": 8_760.0,
    "4h": 2_190.0,
    "1d": 365.0,
}


__all__ = (
    # alias
    "BotId", "GenomeId", "LineageId", "SpeciesId", "Timestamp", "Timeframe", "StrEnum",
    # genoma
    "IdeaFamily", "LogicOp", "CompareOp", "PriceField", "SizingKind", "StopKind",
    "TakeProfitKind", "TrailingKind", "CombineMode",
    "UNARY_COMPARE_OPS", "RANGE_COMPARE_OPS",
    # evolución
    "BreedOperator", "MutationKind", "BotStatus", "DeathCause",
    # ejecución
    "Signal", "Side", "OrderKind", "EXIT_ORDER_KINDS", "ExecutionMode", "GardenStatus",
    # eventos y jardinero
    "EventType", "ProposalKind", "ProposalStatus", "AlertKind",
    # tablas
    "TIMEFRAME_MS", "PERIODS_PER_YEAR",
)
