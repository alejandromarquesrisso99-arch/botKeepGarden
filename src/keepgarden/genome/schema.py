"""El genoma: qué ES un bot.

Un bot no es código, es esta estructura de datos. Ver docs/GENOME.md.

Invariantes que este módulo garantiza:

* Todo genoma se serializa a JSON y se reconstruye idéntico (round-trip exacto).
* El orden de las claves en `to_dict()` es canónico y estable, de forma que el
  hash del genoma sea reproducible entre ejecuciones y entre máquinas.
* `genome_hash()` ignora `meta`: dos bots con el mismo hash son clones aunque
  tengan padres distintos.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from typing import Any, Iterator, Sequence

from ..types import (
    BotId,
    CombineMode,
    CompareOp,
    GenomeId,
    IdeaFamily,
    LineageId,
    LogicOp,
    PriceField,
    RANGE_COMPARE_OPS,
    SizingKind,
    StopKind,
    TakeProfitKind,
    Timeframe,
    TrailingKind,
    UNARY_COMPARE_OPS,
    BreedOperator,
)

# --------------------------------------------------------------------------- #
# Mercado                                                                      #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class MarketSpec:
    """En qué mercado vive el bot. Un bot opera un único símbolo."""

    venue: str = "binance"
    symbol: str = "BTC/USDT"
    timeframe: Timeframe = "1h"

    def key(self) -> str:
        return f"{self.venue}:{self.symbol}:{self.timeframe}"

    def to_dict(self) -> dict[str, Any]:
        return {"venue": self.venue, "symbol": self.symbol, "timeframe": self.timeframe}

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "MarketSpec":
        return MarketSpec(
            venue=d.get("venue", "binance"),
            symbol=d.get("symbol", "BTC/USDT"),
            timeframe=d.get("timeframe", "1h"),
        )


# --------------------------------------------------------------------------- #
# Features: los sentidos del bot                                               #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class FeatureGene:
    """Un indicador que este genoma observa.

    Attributes:
        id: Nombre local dentro del genoma. Las reglas lo referencian por aquí.
        kind: Clave del catálogo (``EMA``, ``RSI``, ``ATR``...).
        source: Campo de la vela sobre el que se calcula, cuando aplica.
        params: Parámetros del indicador. Deben estar dentro de los rangos del
            catálogo; ``genome.validate`` lo comprueba.
        timeframe: ``None`` significa el timeframe operativo del bot. Un valor
            distinto ("4h", "1d") hace de este un feature de contexto, alineado
            siempre hacia atrás para no introducir look-ahead.
    """

    id: str
    kind: str
    params: dict[str, float] = field(default_factory=dict)
    source: PriceField = PriceField.CLOSE
    timeframe: Timeframe | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "params": {k: _num(v) for k, v in sorted(self.params.items())},
            "source": str(self.source),
        }
        if self.timeframe is not None:
            d["timeframe"] = self.timeframe
        return d

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "FeatureGene":
        return FeatureGene(
            id=d["id"],
            kind=d["kind"],
            params={k: _num(v) for k, v in d.get("params", {}).items()},
            source=PriceField(d.get("source", "close")),
            timeframe=d.get("timeframe"),
        )


# --------------------------------------------------------------------------- #
# Operandos y reglas                                                           #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Operand:
    """Lado de una comparación: una referencia, una constante o un precio.

    Exactamente uno de ``ref``, ``const`` o ``price`` debe estar definido.
    """

    ref: str | None = None
    const: float | None = None
    price: PriceField | None = None

    def __post_init__(self) -> None:
        defined = sum(x is not None for x in (self.ref, self.const, self.price))
        if defined != 1:
            raise ValueError(
                f"Operand debe definir exactamente uno de ref/const/price, tiene {defined}"
            )

    def to_dict(self) -> dict[str, Any]:
        if self.ref is not None:
            return {"ref": self.ref}
        if self.price is not None:
            return {"price": str(self.price)}
        return {"const": _num(self.const)}

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Operand":
        if "ref" in d:
            return Operand(ref=d["ref"])
        if "price" in d:
            return Operand(price=PriceField(d["price"]))
        return Operand(const=_num(d["const"]))

    def describe(self) -> str:
        if self.ref is not None:
            return self.ref
        if self.price is not None:
            return str(self.price)
        return _fmt_num(self.const)


@dataclass(frozen=True, slots=True)
class Condition:
    """Hoja de un árbol de reglas: una comparación.

    ``right`` es ``None`` para operadores unarios (RISING, FALLING).
    ``right2`` sólo se usa con BETWEEN, como límite superior.
    """

    op: CompareOp
    left: Operand
    right: Operand | None = None
    right2: Operand | None = None
    lookback: int = 1

    def __post_init__(self) -> None:
        if self.op in UNARY_COMPARE_OPS:
            if self.right is not None:
                raise ValueError(f"{self.op} es unario y no admite 'right'")
        elif self.right is None:
            raise ValueError(f"{self.op} necesita 'right'")
        if self.op in RANGE_COMPARE_OPS and self.right2 is None:
            raise ValueError("BETWEEN necesita 'right2'")

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"op": str(self.op), "left": self.left.to_dict()}
        if self.right is not None:
            d["right"] = self.right.to_dict()
        if self.right2 is not None:
            d["right2"] = self.right2.to_dict()
        if self.lookback != 1:
            d["lookback"] = int(self.lookback)
        return d

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Condition":
        return Condition(
            op=CompareOp(d["op"]),
            left=Operand.from_dict(d["left"]),
            right=Operand.from_dict(d["right"]) if "right" in d else None,
            right2=Operand.from_dict(d["right2"]) if "right2" in d else None,
            lookback=int(d.get("lookback", 1)),
        )

    def describe(self) -> str:
        sym = {
            CompareOp.GT: ">",
            CompareOp.GTE: ">=",
            CompareOp.LT: "<",
            CompareOp.LTE: "<=",
            CompareOp.CROSS_ABOVE: "cruza por encima de",
            CompareOp.CROSS_BELOW: "cruza por debajo de",
            CompareOp.PCT_RANK_GT: "percentil >",
            CompareOp.PCT_RANK_LT: "percentil <",
        }
        if self.op is CompareOp.RISING:
            return f"{self.left.describe()} sube ({self.lookback})"
        if self.op is CompareOp.FALLING:
            return f"{self.left.describe()} baja ({self.lookback})"
        if self.op is CompareOp.BETWEEN:
            return (
                f"{self.left.describe()} entre {self.right.describe()} "
                f"y {self.right2.describe()}"
            )
        return f"{self.left.describe()} {sym[self.op]} {self.right.describe()}"


@dataclass(frozen=True, slots=True)
class LogicNode:
    """Nodo interno de un árbol de reglas: AND, OR o NOT."""

    op: LogicOp
    children: tuple["RuleNode", ...] = ()

    def __post_init__(self) -> None:
        if self.op is LogicOp.NOT and len(self.children) != 1:
            raise ValueError("NOT admite exactamente un hijo")
        if self.op is not LogicOp.NOT and len(self.children) < 1:
            raise ValueError(f"{self.op} necesita al menos un hijo")

    def to_dict(self) -> dict[str, Any]:
        return {
            "op": str(self.op),
            "children": [c.to_dict() for c in self.children],
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "LogicNode":
        return LogicNode(
            op=LogicOp(d["op"]),
            children=tuple(rule_from_dict(c) for c in d["children"]),
        )

    def describe(self) -> str:
        if self.op is LogicOp.NOT:
            return f"NO ({self.children[0].describe()})"
        joiner = " Y " if self.op is LogicOp.AND else " O "
        inner = joiner.join(c.describe() for c in self.children)
        return f"({inner})" if len(self.children) > 1 else inner


#: Un árbol de reglas es un nodo lógico o una condición hoja.
RuleNode = LogicNode | Condition


def rule_from_dict(d: dict[str, Any]) -> RuleNode:
    """Reconstruye un nodo de árbol, distinguiendo lógico de comparación."""
    op = d["op"]
    if op in {"AND", "OR", "NOT"}:
        return LogicNode.from_dict(d)
    return Condition.from_dict(d)


def walk_rules(node: RuleNode | None) -> Iterator[RuleNode]:
    """Recorre el árbol en preorden. Útil para validar, mutar y medir."""
    if node is None:
        return
    yield node
    if isinstance(node, LogicNode):
        for child in node.children:
            yield from walk_rules(child)


def rule_leaves(node: RuleNode | None) -> list[Condition]:
    """Todas las condiciones hoja del árbol."""
    return [n for n in walk_rules(node) if isinstance(n, Condition)]


def rule_depth(node: RuleNode | None) -> int:
    """Profundidad del árbol. Una hoja suelta tiene profundidad 1."""
    if node is None:
        return 0
    if isinstance(node, Condition):
        return 1
    return 1 + max((rule_depth(c) for c in node.children), default=0)


def referenced_features(node: RuleNode | None) -> set[str]:
    """Ids de feature que el árbol referencia."""
    out: set[str] = set()
    for leaf in rule_leaves(node):
        for operand in (leaf.left, leaf.right, leaf.right2):
            if operand is not None and operand.ref is not None:
                out.add(operand.ref)
    return out


# --------------------------------------------------------------------------- #
# Riesgo                                                                       #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class StopGene:
    kind: StopKind = StopKind.ATR_MULT
    value: float = 2.5
    #: Feature de ATR a usar cuando kind es ATR_MULT. None = el ATR por defecto.
    atr_ref: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"kind": str(self.kind), "value": _num(self.value)}
        if self.atr_ref:
            d["atr_ref"] = self.atr_ref
        return d

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "StopGene":
        return StopGene(
            kind=StopKind(d.get("kind", "ATR_MULT")),
            value=_num(d.get("value", 2.5)),
            atr_ref=d.get("atr_ref"),
        )


@dataclass(frozen=True, slots=True)
class TakeProfitGene:
    kind: TakeProfitKind = TakeProfitKind.NONE
    value: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {"kind": str(self.kind), "value": _num(self.value)}

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "TakeProfitGene":
        return TakeProfitGene(
            kind=TakeProfitKind(d.get("kind", "NONE")),
            value=_num(d.get("value", 0.0)),
        )


@dataclass(frozen=True, slots=True)
class TrailingGene:
    kind: TrailingKind = TrailingKind.NONE
    value: float = 0.0
    #: Múltiplo de R a partir del cual el trailing se activa.
    activate_at_r: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": str(self.kind),
            "value": _num(self.value),
            "activate_at_r": _num(self.activate_at_r),
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "TrailingGene":
        return TrailingGene(
            kind=TrailingKind(d.get("kind", "NONE")),
            value=_num(d.get("value", 0.0)),
            activate_at_r=_num(d.get("activate_at_r", 0.0)),
        )


@dataclass(frozen=True, slots=True)
class RiskGene:
    """Cómo arriesga el bot. Ver docs/GENOME.md."""

    sizing: SizingKind = SizingKind.ATR_RISK
    risk_per_trade: float = 0.01
    max_concurrent_positions: int = 1
    max_exposure: float = 0.95
    stop: StopGene = field(default_factory=StopGene)
    take_profit: TakeProfitGene = field(default_factory=TakeProfitGene)
    trailing: TrailingGene = field(default_factory=TrailingGene)
    max_holding_bars: int = 240
    cooldown_bars: int = 0
    allow_short: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "sizing": str(self.sizing),
            "risk_per_trade": _num(self.risk_per_trade),
            "max_concurrent_positions": int(self.max_concurrent_positions),
            "max_exposure": _num(self.max_exposure),
            "stop": self.stop.to_dict(),
            "take_profit": self.take_profit.to_dict(),
            "trailing": self.trailing.to_dict(),
            "max_holding_bars": int(self.max_holding_bars),
            "cooldown_bars": int(self.cooldown_bars),
            "allow_short": bool(self.allow_short),
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "RiskGene":
        return RiskGene(
            sizing=SizingKind(d.get("sizing", "ATR_RISK")),
            risk_per_trade=_num(d.get("risk_per_trade", 0.01)),
            max_concurrent_positions=int(d.get("max_concurrent_positions", 1)),
            max_exposure=_num(d.get("max_exposure", 0.95)),
            stop=StopGene.from_dict(d.get("stop", {})),
            take_profit=TakeProfitGene.from_dict(d.get("take_profit", {})),
            trailing=TrailingGene.from_dict(d.get("trailing", {})),
            max_holding_bars=int(d.get("max_holding_bars", 240)),
            cooldown_bars=int(d.get("cooldown_bars", 0)),
            allow_short=bool(d.get("allow_short", False)),
        )


# --------------------------------------------------------------------------- #
# Régimen y fusión                                                             #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class RegimeGene:
    """Filtro que apaga el bot fuera de su régimen de mercado."""

    enabled: bool = False
    rule: RuleNode | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"enabled": bool(self.enabled)}
        if self.rule is not None:
            d["rule"] = self.rule.to_dict()
        return d

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "RegimeGene":
        return RegimeGene(
            enabled=bool(d.get("enabled", False)),
            rule=rule_from_dict(d["rule"]) if d.get("rule") else None,
        )


@dataclass(frozen=True, slots=True)
class EnsembleGene:
    """Gen de la fusión: el bot combina las señales de sus padres.

    Un genoma con ``ensemble`` no definido es un bot normal con reglas propias.
    Un genoma con ``ensemble`` no tiene reglas de entrada ni de salida: las toma
    de sus miembros. El gen de riesgo, en cambio, siempre es propio.
    """

    members: tuple[BotId, ...] = ()
    weights: tuple[float, ...] = ()
    combine: CombineMode = CombineMode.WEIGHTED
    threshold: float = 0.55
    #: Profundidad de anidamiento: 1 = fusión de bots simples.
    depth: int = 1

    def __post_init__(self) -> None:
        if len(self.members) != len(self.weights):
            raise ValueError("members y weights deben tener la misma longitud")
        if len(self.members) < 2:
            raise ValueError("una fusión necesita al menos 2 miembros")
        if len(set(self.members)) != len(self.members):
            raise ValueError("no se admiten miembros repetidos en una fusión")

    def normalized_weights(self) -> tuple[float, ...]:
        total = sum(self.weights)
        if total <= 0:
            n = len(self.weights)
            return tuple(1.0 / n for _ in range(n))
        return tuple(w / total for w in self.weights)

    def to_dict(self) -> dict[str, Any]:
        return {
            "members": list(self.members),
            "weights": [_num(w) for w in self.weights],
            "combine": str(self.combine),
            "threshold": _num(self.threshold),
            "depth": int(self.depth),
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "EnsembleGene":
        return EnsembleGene(
            members=tuple(d["members"]),
            weights=tuple(_num(w) for w in d["weights"]),
            combine=CombineMode(d.get("combine", "WEIGHTED")),
            threshold=_num(d.get("threshold", 0.55)),
            depth=int(d.get("depth", 1)),
        )


# --------------------------------------------------------------------------- #
# Meta                                                                         #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class GenomeMeta:
    """El pedigrí. No entra en el hash del genoma."""

    born_at: str = ""
    generation: int = 0
    parents: tuple[BotId, ...] = ()
    operator: BreedOperator = BreedOperator.SEED
    root_lineage: LineageId = ""
    parent_families: tuple[IdeaFamily, ...] = ()
    gardener_note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "born_at": self.born_at,
            "generation": int(self.generation),
            "parents": list(self.parents),
            "operator": str(self.operator),
            "root_lineage": self.root_lineage,
            "parent_families": [str(f) for f in self.parent_families],
            "gardener_note": self.gardener_note,
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "GenomeMeta":
        return GenomeMeta(
            born_at=d.get("born_at", ""),
            generation=int(d.get("generation", 0)),
            parents=tuple(d.get("parents", ())),
            operator=BreedOperator(d.get("operator", "SEED")),
            root_lineage=d.get("root_lineage", ""),
            parent_families=tuple(
                IdeaFamily(f) for f in d.get("parent_families", ())
            ),
            gardener_note=d.get("gardener_note", ""),
        )


# --------------------------------------------------------------------------- #
# El genoma                                                                    #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Genome:
    """Un bot completo, como datos.

    Nunca construyas un Genome a mano en código de producción: usa
    ``genome.random_genome`` o los operadores de ``evolution``. A mano sólo en
    tests y en ejemplos.
    """

    id: GenomeId
    family: IdeaFamily
    market: MarketSpec = field(default_factory=MarketSpec)
    features: tuple[FeatureGene, ...] = ()
    entry_long: RuleNode | None = None
    exit_long: RuleNode | None = None
    entry_short: RuleNode | None = None
    exit_short: RuleNode | None = None
    risk: RiskGene = field(default_factory=RiskGene)
    regime: RegimeGene | None = None
    ensemble: EnsembleGene | None = None
    meta: GenomeMeta = field(default_factory=GenomeMeta)
    version: int = 1

    # -- consultas -------------------------------------------------------- #

    @property
    def is_ensemble(self) -> bool:
        return self.ensemble is not None

    def feature_ids(self) -> set[str]:
        return {f.id for f in self.features}

    def feature(self, fid: str) -> FeatureGene | None:
        for f in self.features:
            if f.id == fid:
                return f
        return None

    def rule_trees(self) -> list[tuple[str, RuleNode | None]]:
        """Los árboles del genoma con su nombre de bloque."""
        trees: list[tuple[str, RuleNode | None]] = [
            ("entry_long", self.entry_long),
            ("exit_long", self.exit_long),
            ("entry_short", self.entry_short),
            ("exit_short", self.exit_short),
        ]
        if self.regime is not None:
            trees.append(("regime", self.regime.rule))
        return trees

    def all_referenced_features(self) -> set[str]:
        out: set[str] = set()
        for _, tree in self.rule_trees():
            out |= referenced_features(tree)
        if self.risk.stop.atr_ref:
            out.add(self.risk.stop.atr_ref)
        return out

    def complexity(self) -> int:
        """Número de features más número de hojas de regla.

        Es el término que penaliza el fitness por falta de parsimonia.
        """
        leaves = sum(len(rule_leaves(tree)) for _, tree in self.rule_trees())
        return len(self.features) + leaves

    def max_rule_depth(self) -> int:
        return max((rule_depth(tree) for _, tree in self.rule_trees()), default=0)

    # -- serialización ---------------------------------------------------- #

    def to_dict(self, *, include_meta: bool = True) -> dict[str, Any]:
        """Diccionario canónico: claves en orden fijo, números normalizados."""
        d: dict[str, Any] = {
            "id": self.id,
            "version": int(self.version),
            "family": str(self.family),
            "market": self.market.to_dict(),
            "features": [f.to_dict() for f in self.features],
        }
        for name in ("entry_long", "exit_long", "entry_short", "exit_short"):
            tree = getattr(self, name)
            d[name] = tree.to_dict() if tree is not None else None
        d["risk"] = self.risk.to_dict()
        d["regime"] = self.regime.to_dict() if self.regime is not None else None
        d["ensemble"] = self.ensemble.to_dict() if self.ensemble is not None else None
        if include_meta:
            d["meta"] = self.meta.to_dict()
        return d

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Genome":
        return Genome(
            id=d["id"],
            version=int(d.get("version", 1)),
            family=IdeaFamily(d["family"]),
            market=MarketSpec.from_dict(d.get("market", {})),
            features=tuple(FeatureGene.from_dict(f) for f in d.get("features", [])),
            entry_long=rule_from_dict(d["entry_long"]) if d.get("entry_long") else None,
            exit_long=rule_from_dict(d["exit_long"]) if d.get("exit_long") else None,
            entry_short=rule_from_dict(d["entry_short"]) if d.get("entry_short") else None,
            exit_short=rule_from_dict(d["exit_short"]) if d.get("exit_short") else None,
            risk=RiskGene.from_dict(d.get("risk", {})),
            regime=RegimeGene.from_dict(d["regime"]) if d.get("regime") else None,
            ensemble=EnsembleGene.from_dict(d["ensemble"]) if d.get("ensemble") else None,
            meta=GenomeMeta.from_dict(d.get("meta", {})),
        )

    def with_meta(self, **kwargs: Any) -> "Genome":
        """Copia el genoma cambiando campos de ``meta``. No altera el hash."""
        return replace(self, meta=replace(self.meta, **kwargs))

    def describe(self) -> str:
        """Descripción legible del genoma, para el dashboard y los informes."""
        lines = [f"[{self.family}] {self.id} · {self.market.symbol} {self.market.timeframe}"]
        if self.is_ensemble:
            ens = self.ensemble
            assert ens is not None
            pares = ", ".join(
                f"{m} ({w:.0%})" for m, w in zip(ens.members, ens.normalized_weights())
            )
            lines.append(f"  FUSIÓN {ens.combine} (umbral {ens.threshold:.2f}): {pares}")
        else:
            for fg in self.features:
                ps = ", ".join(f"{k}={_fmt_num(v)}" for k, v in sorted(fg.params.items()))
                tf = f" @{fg.timeframe}" if fg.timeframe else ""
                lines.append(f"  {fg.id} = {fg.kind}({ps}){tf} sobre {fg.source}")
            for name, tree in self.rule_trees():
                if tree is not None:
                    lines.append(f"  {name}: {tree.describe()}")
        r = self.risk
        lines.append(
            f"  riesgo: {r.sizing} {r.risk_per_trade:.2%}/op · "
            f"stop {r.stop.kind}({_fmt_num(r.stop.value)}) · "
            f"tp {r.take_profit.kind}({_fmt_num(r.take_profit.value)}) · "
            f"max {r.max_holding_bars} velas"
        )
        return "\n".join(lines)


def canonical_json(genome: Genome, *, include_meta: bool = False) -> str:
    """JSON canónico del genoma: claves ordenadas y separadores fijos."""
    return json.dumps(
        genome.to_dict(include_meta=include_meta),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def genome_hash(genome: Genome) -> str:
    """SHA-256 del genoma sin ``meta``. Dos bots con el mismo hash son clones."""
    payload = canonical_json(genome, include_meta=False)
    # El id tampoco debe influir: sólo la estructura.
    payload = json.dumps(
        {k: v for k, v in json.loads(payload).items() if k != "id"},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# Utilidades internas                                                          #
# --------------------------------------------------------------------------- #


def _num(x: Any) -> float:
    """Normaliza un número a float redondeado, para que el hash sea estable."""
    return round(float(x), 10)


def _fmt_num(x: float | None) -> str:
    if x is None:
        return "?"
    if float(x).is_integer():
        return str(int(x))
    return f"{x:g}"


__all__: Sequence[str] = (
    "MarketSpec",
    "FeatureGene",
    "Operand",
    "Condition",
    "LogicNode",
    "RuleNode",
    "StopGene",
    "TakeProfitGene",
    "TrailingGene",
    "RiskGene",
    "RegimeGene",
    "EnsembleGene",
    "GenomeMeta",
    "Genome",
    "rule_from_dict",
    "walk_rules",
    "rule_leaves",
    "rule_depth",
    "referenced_features",
    "canonical_json",
    "genome_hash",
)
