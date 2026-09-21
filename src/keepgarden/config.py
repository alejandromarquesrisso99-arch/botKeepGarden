"""Carga y validación de ``config/garden.yaml``.

Reglas de este módulo:

* Toda constante que gobierne el comportamiento del jardín vive aquí, no
  dispersa por el código.
* La configuración se valida al cargarse y falla ruidosamente. Un jardín que
  arranca con una configuración incoherente y se descubre tres semanas después
  es peor que uno que no arranca.
* ``execution.mode: live`` **aborta**. Ver docs/DECISIONS.md D-006.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields, is_dataclass, replace
from pathlib import Path
from typing import Any, Mapping, TypeVar

import yaml

from .types import ExecutionMode, IdeaFamily, Timeframe

T = TypeVar("T")


class ConfigError(RuntimeError):
    """Configuración incoherente o prohibida."""


# --------------------------------------------------------------------------- #
# Secciones                                                                    #
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class MarketConfig:
    venue: str = "binance"
    symbols: list[str] = field(default_factory=lambda: ["BTC/USDT"])
    timeframe: Timeframe = "1h"
    context_timeframes: list[str] = field(default_factory=lambda: ["4h", "1d"])
    history_start: str = "2019-01-01"


@dataclass(slots=True)
class ExecutionConfig:
    mode: str = "paper"
    settle_delay_seconds: int = 20
    max_catchup_candles: int = 2000
    venue_failure_threshold: int = 5


@dataclass(slots=True)
class FrictionConfig:
    taker_fee_bps: float = 10.0
    maker_fee_bps: float = 10.0
    slippage_model: str = "atr"
    slippage_bps: float = 3.0
    slippage_atr_frac: float = 0.05
    min_notional: float = 10.0
    price_precision: int = 2
    amount_precision: int = 6


@dataclass(slots=True)
class GardenConfig:
    initial_capital_per_bot: float = 1000.0
    target_population: int = 60
    max_population: int = 120
    min_population: int = 20
    ticks_per_generation: int = 168          # 1 semana de velas de 1h
    elite_count: int = 5
    cull_fraction: float = 0.20
    min_age_generations: int = 2
    idle_generations_limit: int = 3
    births_per_generation: int = 12


@dataclass(slots=True)
class RiskConfig:
    hard_max_drawdown: float = 0.35          # poda inmediata del bot
    garden_max_drawdown: float = 0.25        # freno de nacimientos
    max_risk_per_trade: float = 0.02
    min_risk_per_trade: float = 0.002
    price_jump_anomaly: float = 0.40


@dataclass(slots=True)
class EvolutionConfig:
    mutation_rate: float = 0.35
    mutation_rate_min_factor: float = 0.5
    mutation_rate_max_factor: float = 3.0
    mutations_per_child: list[int] = field(default_factory=lambda: [1, 3])
    crossover_share: float = 0.35
    mutation_share: float = 0.40
    fusion_share: float = 0.15
    seed_share: float = 0.10
    crossover_min_distance: float = 0.20
    fitness_bias_to_better_parent: float = 0.60
    tournament_size: int = 3
    diversity_floor: float = 0.30
    max_family_share: float = 0.40
    stagnation_generations: int = 4
    max_rule_depth: int = 4
    max_rule_leaves: int = 6
    max_features: int = 6


@dataclass(slots=True)
class FusionConfig:
    max_depth: int = 2
    min_members: int = 2
    max_members: int = 4
    max_parent_correlation: float = 0.45
    min_parent_generations: int = 2
    default_combine: str = "WEIGHTED"
    default_threshold: float = 0.55


@dataclass(slots=True)
class SpeciationConfig:
    clone_threshold: float = 0.15
    species_threshold: float = 0.35
    distance_weights: dict[str, float] = field(
        default_factory=lambda: {
            "features": 0.35,
            "rules": 0.35,
            "risk": 0.20,
            "family": 0.10,
        }
    )


@dataclass(slots=True)
class IncubatorConfig:
    min_sortino: float = 0.8
    max_drawdown: float = 0.30
    min_trades_per_fold: float = 15
    max_oos_decay: float = 0.50
    multiplicity_penalty: float = 0.08
    n_folds: int = 5
    train_bars: int = 8760          # ~1 año de velas de 1h
    validation_bars: int = 2190     # ~3 meses
    embargo_bars: int = 120         # 5 días
    holdout_bars: int = 1440        # ~2 meses, intocables
    workers: int = 0                # 0 = auto (cpu_count - 1)


@dataclass(slots=True)
class FitnessConfig:
    min_trades: int = 10
    weights: dict[str, float] = field(
        default_factory=lambda: {
            "sortino": 0.35,
            "calmar": 0.20,
            "ulcer": 0.15,
            "profit_factor": 0.15,
            "consistency": 0.15,
        }
    )
    penalty_complexity: float = 0.02
    penalty_correlation: float = 0.30
    correlation_threshold: float = 0.60
    penalty_fees: float = 0.25
    fee_drag_threshold: float = 0.30
    penalty_turnover: float = 0.15
    bonus_novelty: float = 0.10
    bonus_age: float = 0.05
    age_saturation_generations: int = 8
    live_weight: float = 0.70
    incubator_weight: float = 0.30


@dataclass(slots=True)
class GardenerConfig:
    every_generations: int = 4
    stagnation_generations: int = 4
    max_retire_share: float = 0.20
    max_tune_delta: float = 0.50
    report_dir: str = "state/reports"
    journal_max_entries: int = 500


@dataclass(slots=True)
class StorageConfig:
    db_path: str = "state/db/garden.db"
    cache_dir: str = "state/cache"
    log_dir: str = "state/logs"
    snapshot_every_generations: int = 10


@dataclass(slots=True)
class DashboardConfig:
    host: str = "127.0.0.1"
    port: int = 8756
    graph_node_limit: int = 2000
    series_points: int = 2000
    open_browser: bool = True


@dataclass(slots=True)
class Config:
    """Configuración completa del jardín."""

    seed: int = 20260914
    market: MarketConfig = field(default_factory=MarketConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    frictions: FrictionConfig = field(default_factory=FrictionConfig)
    garden: GardenConfig = field(default_factory=GardenConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    evolution: EvolutionConfig = field(default_factory=EvolutionConfig)
    fusion: FusionConfig = field(default_factory=FusionConfig)
    speciation: SpeciationConfig = field(default_factory=SpeciationConfig)
    incubator: IncubatorConfig = field(default_factory=IncubatorConfig)
    fitness: FitnessConfig = field(default_factory=FitnessConfig)
    gardener: GardenerConfig = field(default_factory=GardenerConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)

    #: Raíz del proyecto, deducida al cargar. No viene del YAML.
    root: Path = field(default_factory=Path.cwd)

    # -- derivados -------------------------------------------------------- #

    @property
    def execution_mode(self) -> ExecutionMode:
        return ExecutionMode(self.execution.mode.upper())

    @property
    def primary_symbol(self) -> str:
        return self.market.symbols[0]

    def path(self, relative: str) -> Path:
        """Resuelve una ruta relativa contra la raíz del proyecto."""
        p = Path(relative)
        return p if p.is_absolute() else (self.root / p)

    @property
    def db_file(self) -> Path:
        return self.path(self.storage.db_path)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("root", None)
        return d


# --------------------------------------------------------------------------- #
# Carga                                                                        #
# --------------------------------------------------------------------------- #


def _build(cls: type[T], data: Mapping[str, Any] | None) -> T:
    """Construye una dataclass desde un dict, ignorando claves desconocidas
    con un error explícito (una clave mal escrita en el YAML es un bug, no un
    detalle que deba tragarse en silencio)."""
    data = data or {}
    known = {f.name for f in fields(cls)}  # type: ignore[arg-type]
    unknown = set(data) - known
    if unknown:
        raise ConfigError(
            f"Claves desconocidas en la sección '{cls.__name__}': {sorted(unknown)}. "
            f"Válidas: {sorted(known)}"
        )
    kwargs: dict[str, Any] = {}
    for f in fields(cls):  # type: ignore[arg-type]
        if f.name not in data:
            continue
        value = data[f.name]
        if is_dataclass(f.type) and isinstance(value, Mapping):
            kwargs[f.name] = _build(f.type, value)  # type: ignore[arg-type]
        else:
            kwargs[f.name] = value
    return cls(**kwargs)  # type: ignore[return-value]


def find_project_root(start: Path | None = None) -> Path:
    """Sube directorios hasta encontrar ``config/garden.yaml``."""
    cur = (start or Path.cwd()).resolve()
    for candidate in (cur, *cur.parents):
        if (candidate / "config" / "garden.yaml").is_file():
            return candidate
    raise ConfigError(
        "No se encuentra config/garden.yaml. Ejecuta el comando desde dentro "
        "del proyecto botKeepGarden."
    )


def load_config(path: str | Path | None = None) -> Config:
    """Carga, valida y devuelve la configuración."""
    if path is None:
        root = find_project_root()
        path = root / "config" / "garden.yaml"
    else:
        path = Path(path)
        root = find_project_root(path.parent)

    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(raw, Mapping):
        raise ConfigError(f"{path} no contiene un mapeo en la raíz")

    sections = {
        "market": MarketConfig,
        "execution": ExecutionConfig,
        "frictions": FrictionConfig,
        "garden": GardenConfig,
        "risk": RiskConfig,
        "evolution": EvolutionConfig,
        "fusion": FusionConfig,
        "speciation": SpeciationConfig,
        "incubator": IncubatorConfig,
        "fitness": FitnessConfig,
        "gardener": GardenerConfig,
        "storage": StorageConfig,
        "dashboard": DashboardConfig,
    }
    unknown = set(raw) - set(sections) - {"seed"}
    if unknown:
        raise ConfigError(f"Secciones desconocidas en {path}: {sorted(unknown)}")

    cfg = Config(
        seed=int(raw.get("seed", 20260914)),
        root=root,
        **{name: _build(cls, raw.get(name)) for name, cls in sections.items()},  # type: ignore[arg-type]
    )
    validate_config(cfg)
    return cfg


def validate_config(cfg: Config) -> None:
    """Comprueba coherencia. Levanta ``ConfigError`` con un mensaje accionable."""

    # --- el freno de mano ------------------------------------------------- #
    if cfg.execution.mode.lower() != "paper":
        raise ConfigError(
            "execution.mode sólo puede ser 'paper'. El modo 'live' está "
            "bloqueado a propósito (docs/DECISIONS.md D-006): operar con dinero "
            "real exige editar este bloqueo a mano y de forma consciente."
        )

    e = cfg.evolution
    shares = e.crossover_share + e.mutation_share + e.fusion_share + e.seed_share
    if abs(shares - 1.0) > 1e-6:
        raise ConfigError(
            f"Las cuotas de operadores deben sumar 1.0, suman {shares:.3f} "
            "(evolution.crossover_share + mutation_share + fusion_share + seed_share)"
        )

    g = cfg.garden
    if not (g.min_population <= g.target_population <= g.max_population):
        raise ConfigError(
            "Debe cumplirse garden.min_population <= target_population <= max_population"
        )
    if g.elite_count >= g.min_population:
        raise ConfigError("garden.elite_count debe ser menor que min_population")
    if not 0.0 < g.cull_fraction < 1.0:
        raise ConfigError("garden.cull_fraction debe estar en (0, 1)")

    s = cfg.speciation
    if s.clone_threshold >= s.species_threshold:
        raise ConfigError(
            "speciation.clone_threshold debe ser menor que species_threshold"
        )
    wsum = sum(s.distance_weights.values())
    if abs(wsum - 1.0) > 1e-6:
        raise ConfigError(
            f"speciation.distance_weights debe sumar 1.0, suma {wsum:.3f}"
        )

    f = cfg.fitness
    if abs(f.live_weight + f.incubator_weight - 1.0) > 1e-6:
        raise ConfigError("fitness.live_weight + incubator_weight debe ser 1.0")

    i = cfg.incubator
    if i.holdout_bars <= 0:
        raise ConfigError(
            "incubator.holdout_bars debe ser > 0: sin holdout no hay forma "
            "honesta de saber si un bot generaliza"
        )
    if i.embargo_bars <= 0:
        raise ConfigError("incubator.embargo_bars debe ser > 0 para evitar fugas")

    r = cfg.risk
    if not 0.0 < r.hard_max_drawdown < 1.0:
        raise ConfigError("risk.hard_max_drawdown debe estar en (0, 1)")
    if r.min_risk_per_trade >= r.max_risk_per_trade:
        raise ConfigError("risk.min_risk_per_trade debe ser menor que max_risk_per_trade")

    if cfg.frictions.taker_fee_bps <= 0:
        raise ConfigError(
            "frictions.taker_fee_bps debe ser > 0. Un backtest sin comisiones "
            "miente (docs/EXECUTION.md)."
        )
    if cfg.frictions.slippage_model not in {"fixed_bps", "atr", "volume_aware"}:
        raise ConfigError(
            "frictions.slippage_model debe ser fixed_bps, atr o volume_aware"
        )

    if not cfg.market.symbols:
        raise ConfigError("market.symbols no puede estar vacío")

    if e.max_rule_leaves < 1 or e.max_rule_depth < 1:
        raise ConfigError("evolution.max_rule_leaves y max_rule_depth deben ser >= 1")

    if not 0.0 < e.max_family_share <= 1.0:
        raise ConfigError("evolution.max_family_share debe estar en (0, 1]")
    n_families = len([f for f in IdeaFamily if f is not IdeaFamily.HYBRID])
    if e.max_family_share < 1.0 / n_families:
        raise ConfigError(
            f"evolution.max_family_share ({e.max_family_share}) es tan bajo que "
            f"ninguna población puede satisfacerlo con {n_families} familias"
        )


# --------------------------------------------------------------------------- #
# Ajustes del jardinero                                                        #
#
# Un TUNE del jardinero no toca config/garden.yaml: vive en la base y es
# reversible (docs/GARDENER_PROTOCOL.md §Límites). Estas dos funciones son el
# puente entre el YAML y esos ajustes efectivos.
# --------------------------------------------------------------------------- #


def current_params(cfg: Config) -> dict[str, float]:
    """Todos los parámetros numéricos de la config, por ruta con puntos.

    ``{"evolution.mutation_rate": 0.35, "garden.cull_fraction": 0.2, ...}``.
    Es lo que necesita el validador de propuestas para saber cuánto se mueve un
    parámetro respecto a su valor vigente.
    """
    salida: dict[str, float] = {}
    for seccion, valores in cfg.to_dict().items():
        if not isinstance(valores, dict):
            if isinstance(valores, (int, float)) and not isinstance(valores, bool):
                salida[str(seccion)] = float(valores)
            continue
        for clave, valor in valores.items():
            if isinstance(valor, (int, float)) and not isinstance(valor, bool):
                salida[f"{seccion}.{clave}"] = float(valor)
    return salida


def apply_overrides(cfg: Config, overrides: Mapping[str, Any]) -> Config:
    """Devuelve una copia de la config con los ajustes del jardinero aplicados.

    Sólo toca rutas ``seccion.campo`` que existan y sean numéricas. Una ruta
    desconocida se ignora en silencio a propósito: la validación de la
    propuesta ya la rechazó, y un jardín no debe negarse a arrancar porque en
    la base quedara un ajuste de una versión anterior del código.
    """
    if not overrides:
        return cfg
    secciones: dict[str, dict[str, Any]] = {}
    for ruta, valor in overrides.items():
        if "." not in str(ruta):
            continue
        seccion, campo = str(ruta).split(".", 1)
        actual = getattr(cfg, seccion, None)
        if actual is None or not hasattr(actual, campo):
            continue
        previo = getattr(actual, campo)
        if isinstance(previo, bool) or not isinstance(previo, (int, float)):
            continue
        secciones.setdefault(seccion, {})[campo] = (
            int(valor) if isinstance(previo, int) else float(valor)
        )
    if not secciones:
        return cfg
    nuevo = cfg
    for seccion, campos in secciones.items():
        nuevo = replace(nuevo, **{seccion: replace(getattr(nuevo, seccion), **campos)})
    return nuevo


__all__ = (
    "Config",
    "ConfigError",
    "apply_overrides",
    "current_params",
    "load_config",
    "validate_config",
    "find_project_root",
    "MarketConfig",
    "ExecutionConfig",
    "FrictionConfig",
    "GardenConfig",
    "RiskConfig",
    "EvolutionConfig",
    "FusionConfig",
    "SpeciationConfig",
    "IncubatorConfig",
    "FitnessConfig",
    "GardenerConfig",
    "StorageConfig",
    "DashboardConfig",
)
