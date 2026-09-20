"""Propuestas del jardinero: el único canal por el que el LLM toca el jardín.

Todo lo que el jardinero puede hacer está tipado aquí y se valida antes de
aplicarse. Ver docs/GARDENER_PROTOCOL.md.

Tres campos son obligatorios en toda propuesta y no son burocracia:

* ``rationale`` — por qué. Obliga a articular el razonamiento.
* ``expected_effect`` — qué espera ver, en métricas concretas. Es lo que hace
  la propuesta falsable.
* ``review_in_generations`` — cuándo se comprueba. El motor mide el efecto real
  y lo compara con el esperado, automáticamente.

Sin esos tres campos la propuesta se rechaza. Un jardinero que no puede decir
qué espera que pase no está diagnosticando: está adivinando.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from ..types import BotId, IdeaFamily, LineageId, ProposalKind, ProposalStatus


class ProposalError(ValueError):
    """Propuesta mal formada o que viola un límite del jardinero."""


#: Parámetros del motor que el jardinero puede ajustar con TUNE. Cualquier otro
#: se rechaza. Cada entrada es (ruta_en_config, mínimo, máximo).
TUNABLE_PARAMS: dict[str, tuple[float, float]] = {
    "evolution.mutation_rate": (0.05, 0.90),
    "evolution.mutation_share": (0.10, 0.70),
    "evolution.crossover_share": (0.05, 0.60),
    "evolution.fusion_share": (0.00, 0.40),
    "evolution.seed_share": (0.00, 0.50),
    "evolution.crossover_min_distance": (0.05, 0.50),
    "evolution.tournament_size": (2, 7),
    "evolution.diversity_floor": (0.15, 0.60),
    "evolution.max_family_share": (0.20, 0.80),
    "garden.cull_fraction": (0.05, 0.40),
    "garden.births_per_generation": (2, 30),
    "garden.target_population": (20, 120),
    "garden.idle_generations_limit": (1, 8),
    "fusion.max_parent_correlation": (0.10, 0.80),
    "incubator.min_sortino": (0.3, 2.0),
    "incubator.max_drawdown": (0.10, 0.50),
    "fitness.penalty_correlation": (0.0, 0.80),
    "fitness.bonus_novelty": (0.0, 0.40),
    "fitness.penalty_complexity": (0.0, 0.10),
}

#: Parámetros explícitamente fuera del alcance del jardinero.
FORBIDDEN_PARAMS: frozenset[str] = frozenset(
    {
        "execution.mode",
        "garden.initial_capital_per_bot",
        "incubator.holdout_bars",
        "incubator.embargo_bars",
        "frictions.taker_fee_bps",
        "frictions.slippage_atr_frac",
        "risk.hard_max_drawdown",
        "seed",
    }
)


# --------------------------------------------------------------------------- #
# Propuesta                                                                    #
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Proposal:
    """Una acción propuesta por el jardinero.

    ``payload`` cambia según ``kind``; su forma esperada está documentada en
    ``PAYLOAD_SHAPES`` y la comprueba ``validate_proposal``.
    """

    kind: ProposalKind
    rationale: str
    expected_effect: str
    review_in_generations: int
    payload: dict[str, Any] = field(default_factory=dict)
    target: str = ""
    status: ProposalStatus = ProposalStatus.PENDING
    rejection_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": str(self.kind),
            "target": self.target,
            "payload": self.payload,
            "rationale": self.rationale,
            "expected_effect": self.expected_effect,
            "review_in_generations": int(self.review_in_generations),
            "status": str(self.status),
            "rejection_reason": self.rejection_reason,
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Proposal":
        missing = [
            k for k in ("kind", "rationale", "expected_effect", "review_in_generations")
            if k not in d
        ]
        if missing:
            raise ProposalError(
                f"Faltan campos obligatorios en la propuesta: {missing}. "
                "Toda propuesta debe decir por qué se hace, qué se espera que "
                "pase y cuándo se comprueba."
            )
        return Proposal(
            kind=ProposalKind(d["kind"]),
            rationale=str(d["rationale"]),
            expected_effect=str(d["expected_effect"]),
            review_in_generations=int(d["review_in_generations"]),
            payload=dict(d.get("payload", {})),
            target=str(d.get("target", "")),
        )


#: Claves esperadas en ``payload`` por tipo de propuesta.
#: Formato: (obligatorias, opcionales).
PAYLOAD_SHAPES: dict[ProposalKind, tuple[tuple[str, ...], tuple[str, ...]]] = {
    ProposalKind.GRAFT: (
        ("block", "into"),
        ("feature", "rule", "risk", "n_children", "family_hint"),
    ),
    ProposalKind.SEED_FAMILY: (("family", "count"), ("template_hint", "notes")),
    ProposalKind.FUSE: (("members",), ("combine", "weights", "threshold")),
    ProposalKind.RETIRE: ((), ("bots", "lineage", "reason")),
    ProposalKind.PROTECT: (("bots", "generations"), ()),
    ProposalKind.TUNE: (("param", "value"), ("ramp_generations",)),
    ProposalKind.ADD_GENE: (("name", "category", "description"), ("formula", "params")),
    ProposalKind.REBALANCE_QUOTAS: ((), ("family_weights", "max_family_share")),
    ProposalKind.NOTE: ((), ("text",)),
}

#: Bloques de genoma que un GRAFT puede injertar.
GRAFTABLE_BLOCKS: frozenset[str] = frozenset(
    {"feature", "entry_long", "exit_long", "entry_short", "exit_short", "regime", "risk"}
)


# --------------------------------------------------------------------------- #
# Contexto y validación                                                        #
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class GardenSnapshot:
    """Lo mínimo que hace falta saber del jardín para validar propuestas."""

    generation: int
    alive_bot_ids: frozenset[BotId]
    lineages: frozenset[LineageId]
    population_size: int
    current_params: dict[str, float]
    #: Generaciones vividas por bot, para comprobar min_parent_generations.
    bot_ages: dict[BotId, int] = field(default_factory=dict)


@dataclass(slots=True)
class GardenerLimits:
    """Los límites duros del jardinero. Vienen de ``config.gardener``."""

    max_retire_share: float = 0.20
    max_tune_delta: float = 0.50
    max_seed_per_session: int = 15
    max_protect_generations: int = 6
    max_fusions_per_session: int = 5
    max_grafts_per_session: int = 8
    max_proposals_per_session: int = 25


def validate_proposal(
    proposal: Proposal,
    snapshot: GardenSnapshot,
    limits: GardenerLimits,
) -> None:
    """Valida una propuesta aislada. Levanta ``ProposalError`` si no pasa.

    No comprueba límites acumulativos entre propuestas (cuántas retiradas hay
    en total en la sesión): de eso se encarga ``validate_session``.
    """
    if not proposal.rationale.strip():
        raise ProposalError("rationale no puede estar vacío")
    if not proposal.expected_effect.strip():
        raise ProposalError(
            "expected_effect no puede estar vacío: di qué métrica esperas que "
            "cambie y en qué dirección"
        )
    if proposal.review_in_generations < 1 or proposal.review_in_generations > 24:
        raise ProposalError("review_in_generations debe estar entre 1 y 24")

    required, optional = PAYLOAD_SHAPES[proposal.kind]
    missing = [k for k in required if k not in proposal.payload]
    if missing:
        raise ProposalError(f"{proposal.kind}: faltan claves en payload: {missing}")
    unknown = set(proposal.payload) - set(required) - set(optional)
    if unknown:
        raise ProposalError(
            f"{proposal.kind}: claves desconocidas en payload: {sorted(unknown)}"
        )

    p = proposal.payload

    if proposal.kind is ProposalKind.TUNE:
        param = str(p["param"])
        if param in FORBIDDEN_PARAMS:
            raise ProposalError(
                f"'{param}' está fuera del alcance del jardinero. "
                "Los frenos del sistema (holdout, embargo, fricción, modo de "
                "ejecución, capital, drawdown duro) no se ajustan desde aquí."
            )
        if param not in TUNABLE_PARAMS:
            raise ProposalError(
                f"'{param}' no es ajustable. Ajustables: {sorted(TUNABLE_PARAMS)}"
            )
        lo, hi = TUNABLE_PARAMS[param]
        value = float(p["value"])
        if not lo <= value <= hi:
            raise ProposalError(f"{param}={value} fuera del rango permitido [{lo}, {hi}]")
        current = snapshot.current_params.get(param)
        if current is not None and current != 0:
            delta = abs(value - current) / abs(current)
            if delta > limits.max_tune_delta:
                raise ProposalError(
                    f"{param}: cambio del {delta:.0%} sobre {current} supera el "
                    f"máximo del {limits.max_tune_delta:.0%} por sesión. "
                    "Muévelo en varios pasos y mide entre medias."
                )

    elif proposal.kind is ProposalKind.RETIRE:
        bots = [str(b) for b in p.get("bots", [])]
        lineage = p.get("lineage")
        if not bots and not lineage:
            raise ProposalError("RETIRE necesita 'bots' o 'lineage'")
        unknown_bots = [b for b in bots if b not in snapshot.alive_bot_ids]
        if unknown_bots:
            raise ProposalError(f"RETIRE: bots no vivos: {unknown_bots}")
        if lineage and str(lineage) not in snapshot.lineages:
            raise ProposalError(f"RETIRE: linaje desconocido '{lineage}'")

    elif proposal.kind is ProposalKind.PROTECT:
        bots = [str(b) for b in p["bots"]]
        unknown_bots = [b for b in bots if b not in snapshot.alive_bot_ids]
        if unknown_bots:
            raise ProposalError(f"PROTECT: bots no vivos: {unknown_bots}")
        gens = int(p["generations"])
        if not 1 <= gens <= limits.max_protect_generations:
            raise ProposalError(
                f"PROTECT: generations debe estar entre 1 y {limits.max_protect_generations}"
            )

    elif proposal.kind is ProposalKind.FUSE:
        members = [str(m) for m in p["members"]]
        if len(members) < 2:
            raise ProposalError("FUSE necesita al menos 2 miembros")
        if len(set(members)) != len(members):
            raise ProposalError("FUSE: miembros repetidos")
        not_alive = [m for m in members if m not in snapshot.alive_bot_ids]
        if not_alive:
            raise ProposalError(f"FUSE: miembros no vivos: {not_alive}")
        weights = p.get("weights")
        if weights is not None and len(weights) != len(members):
            raise ProposalError("FUSE: weights debe tener la misma longitud que members")

    elif proposal.kind is ProposalKind.SEED_FAMILY:
        try:
            IdeaFamily(str(p["family"]))
        except ValueError as exc:
            raise ProposalError(f"SEED_FAMILY: familia desconocida '{p['family']}'") from exc
        if str(p["family"]) == str(IdeaFamily.HYBRID):
            raise ProposalError(
                "HYBRID no se siembra: sólo nace de cruzar familias distintas"
            )
        count = int(p["count"])
        if not 1 <= count <= limits.max_seed_per_session:
            raise ProposalError(
                f"SEED_FAMILY: count debe estar entre 1 y {limits.max_seed_per_session}"
            )

    elif proposal.kind is ProposalKind.GRAFT:
        block = str(p["block"])
        if block not in GRAFTABLE_BLOCKS:
            raise ProposalError(
                f"GRAFT: bloque '{block}' no injertable. "
                f"Injertables: {sorted(GRAFTABLE_BLOCKS)}"
            )
        into = str(p["into"])
        if into not in snapshot.alive_bot_ids and into not in snapshot.lineages:
            raise ProposalError(f"GRAFT: 'into' debe ser un bot vivo o un linaje, no '{into}'")
        n = int(p.get("n_children", 1))
        if not 1 <= n <= 5:
            raise ProposalError("GRAFT: n_children debe estar entre 1 y 5")

    elif proposal.kind is ProposalKind.REBALANCE_QUOTAS:
        share = p.get("max_family_share")
        if share is not None and not 0.2 <= float(share) <= 0.8:
            raise ProposalError("REBALANCE_QUOTAS: max_family_share debe estar en [0.2, 0.8]")


def validate_session(
    proposals: Sequence[Proposal],
    snapshot: GardenSnapshot,
    limits: GardenerLimits,
) -> None:
    """Valida el conjunto de propuestas de una sesión, incluidos los límites
    acumulativos. Levanta ``ProposalError`` con el primer problema encontrado.
    """
    if len(proposals) > limits.max_proposals_per_session:
        raise ProposalError(
            f"{len(proposals)} propuestas superan el máximo de "
            f"{limits.max_proposals_per_session} por sesión"
        )

    for p in proposals:
        validate_proposal(p, snapshot, limits)

    # -- límites acumulativos --------------------------------------------- #
    retired: set[BotId] = set()
    for p in proposals:
        if p.kind is not ProposalKind.RETIRE:
            continue
        retired |= {str(b) for b in p.payload.get("bots", [])}
        lin = p.payload.get("lineage")
        if lin:
            retired |= {b for b in snapshot.alive_bot_ids if str(lin) in b}
    max_retire = int(snapshot.population_size * limits.max_retire_share)
    if len(retired) > max_retire:
        raise ProposalError(
            f"La sesión jubila {len(retired)} bots de una población de "
            f"{snapshot.population_size}; el máximo es {max_retire} "
            f"({limits.max_retire_share:.0%}). Un jardinero pesimista no puede "
            "vaciar el jardín en una sesión."
        )

    n_fuse = sum(1 for p in proposals if p.kind is ProposalKind.FUSE)
    if n_fuse > limits.max_fusions_per_session:
        raise ProposalError(
            f"{n_fuse} fusiones superan el máximo de {limits.max_fusions_per_session}"
        )

    n_graft = sum(
        int(p.payload.get("n_children", 1))
        for p in proposals
        if p.kind is ProposalKind.GRAFT
    )
    if n_graft > limits.max_grafts_per_session:
        raise ProposalError(
            f"{n_graft} injertos superan el máximo de {limits.max_grafts_per_session}"
        )

    n_seed = sum(
        int(p.payload.get("count", 0))
        for p in proposals
        if p.kind is ProposalKind.SEED_FAMILY
    )
    if n_seed > limits.max_seed_per_session:
        raise ProposalError(
            f"{n_seed} siembras superan el máximo de {limits.max_seed_per_session}"
        )

    # Un mismo parámetro no se ajusta dos veces en la misma sesión.
    tuned: set[str] = set()
    for p in proposals:
        if p.kind is ProposalKind.TUNE:
            param = str(p.payload["param"])
            if param in tuned:
                raise ProposalError(f"TUNE: '{param}' aparece dos veces en la sesión")
            tuned.add(param)


def parse_session(payload: str | Iterable[dict[str, Any]]) -> list[Proposal]:
    """Lee las propuestas de una sesión desde JSON o desde una lista de dicts."""
    data = json.loads(payload) if isinstance(payload, str) else list(payload)
    if isinstance(data, dict):
        data = data.get("proposals", [])
    return [Proposal.from_dict(d) for d in data]


__all__ = (
    "Proposal",
    "ProposalError",
    "GardenSnapshot",
    "GardenerLimits",
    "TUNABLE_PARAMS",
    "FORBIDDEN_PARAMS",
    "GRAFTABLE_BLOCKS",
    "PAYLOAD_SHAPES",
    "validate_proposal",
    "validate_session",
    "parse_session",
)
