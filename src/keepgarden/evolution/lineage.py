"""Genealogía: pedigrí, linajes y el grafo que pinta el dashboard.

Este módulo es puro: no toca la base ni el motor. Recibe aristas
(padre → hijo) y responde preguntas sobre ellas. Así se puede testear con
grafos de juguete y así el dashboard puede usarlo sin arrastrar dependencias.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
from typing import Iterable, Mapping, Sequence

from ..types import BotId, BreedOperator, LineageId


@dataclass(frozen=True, slots=True)
class ParentEdge:
    """Una arista de la genealogía."""

    parent: BotId
    child: BotId
    operator: BreedOperator
    weight: float = 1.0
    ordinal: int = 0


@dataclass(slots=True)
class Pedigree:
    """Índice bidireccional de la genealogía completa del jardín."""

    edges: list[ParentEdge] = field(default_factory=list)
    _children: dict[BotId, list[ParentEdge]] = field(default_factory=lambda: defaultdict(list))
    _parents: dict[BotId, list[ParentEdge]] = field(default_factory=lambda: defaultdict(list))

    # -- construcción ------------------------------------------------------ #

    @staticmethod
    def from_edges(edges: Iterable[ParentEdge]) -> "Pedigree":
        ped = Pedigree()
        for e in edges:
            ped.add(e)
        return ped

    def add(self, edge: ParentEdge) -> None:
        self.edges.append(edge)
        self._children[edge.parent].append(edge)
        self._parents[edge.child].append(edge)

    # -- consultas directas ------------------------------------------------ #

    def parents_of(self, bot: BotId) -> list[BotId]:
        return [e.parent for e in sorted(self._parents.get(bot, []), key=lambda e: e.ordinal)]

    def children_of(self, bot: BotId) -> list[BotId]:
        return [e.child for e in self._children.get(bot, [])]

    def is_founder(self, bot: BotId) -> bool:
        """Un fundador no tiene padres: nació por SEED."""
        return not self._parents.get(bot)

    def parent_edges(self, bot: BotId) -> list[ParentEdge]:
        return list(self._parents.get(bot, []))

    # -- recorridos -------------------------------------------------------- #

    def ancestors(self, bot: BotId, *, max_depth: int | None = None) -> set[BotId]:
        """Todos los antepasados. Un bot fusionado tiene muchos."""
        seen: set[BotId] = set()
        queue: deque[tuple[BotId, int]] = deque((p, 1) for p in self.parents_of(bot))
        while queue:
            cur, depth = queue.popleft()
            if cur in seen:
                continue
            seen.add(cur)
            if max_depth is not None and depth >= max_depth:
                continue
            for p in self.parents_of(cur):
                if p not in seen:
                    queue.append((p, depth + 1))
        return seen

    def descendants(self, bot: BotId, *, max_depth: int | None = None) -> set[BotId]:
        seen: set[BotId] = set()
        queue: deque[tuple[BotId, int]] = deque((c, 1) for c in self.children_of(bot))
        while queue:
            cur, depth = queue.popleft()
            if cur in seen:
                continue
            seen.add(cur)
            if max_depth is not None and depth >= max_depth:
                continue
            for c in self.children_of(cur):
                if c not in seen:
                    queue.append((c, depth + 1))
        return seen

    def founders_of(self, bot: BotId) -> set[BotId]:
        """Los fundadores de los que desciende. Normalmente uno, varios si hubo fusión."""
        anc = self.ancestors(bot)
        founders = {a for a in anc if self.is_founder(a)}
        return founders or ({bot} if self.is_founder(bot) else set())

    def generation_depth(self, bot: BotId) -> int:
        """Saltos hasta el fundador más lejano. Profundidad evolutiva real."""
        if self.is_founder(bot):
            return 0
        return 1 + max((self.generation_depth(p) for p in self.parents_of(bot)), default=-1)

    def co_parents_of(self, bot: BotId) -> set[BotId]:
        """Los bots con los que ``bot`` ha engendrado algún hijo directo.

        En un jardín donde el cruce y la fusión toman dos padres, haber
        producido descendencia junto a alguien es una forma de parentesco que no
        aparece en el árbol de antepasados: dos fundadores sin relación de
        sangre quedan emparentados en cuanto tienen un hijo común.
        """
        out: set[BotId] = set()
        for child in self.children_of(bot):
            out.update(p for p in self.parents_of(child) if p != bot)
        return out

    def related(self, a: BotId, b: BotId) -> bool:
        """¿Hay parentesco entre los dos?

        Lo hay si comparten algún antepasado, si uno desciende del otro o si han
        engendrado un hijo juntos. Compartir un descendiente lejano no cuenta:
        en un jardín con fusiones casi todo acaba confluyendo aguas abajo, y una
        definición así declararía emparentado a todo el mundo.
        """
        if a == b:
            return True
        anc_a = self.ancestors(a) | {a}
        anc_b = self.ancestors(b) | {b}
        if anc_a & anc_b:
            return True
        return b in self.co_parents_of(a)

    def inbreeding_coefficient(self, a: BotId, b: BotId) -> float:
        """Solapamiento de antepasados, en [0, 1] (Jaccard).

        La selección la usa para evitar cruzar hermanos una generación tras
        otra: la endogamia colapsa la diversidad incluso con especiación.
        """
        anc_a = self.ancestors(a) | {a}
        anc_b = self.ancestors(b) | {b}
        union = anc_a | anc_b
        if not union:
            return 0.0
        return len(anc_a & anc_b) / len(union)

    # -- métricas de linaje ------------------------------------------------ #

    def lineage_sizes(self, lineage_of: Mapping[BotId, LineageId]) -> dict[LineageId, int]:
        out: dict[LineageId, int] = defaultdict(int)
        for bot, lin in lineage_of.items():
            out[lin] += 1
        return dict(out)

    def fusion_nodes(self) -> set[BotId]:
        """Bots nacidos de una fusión: los que tienen más de un padre FUSION."""
        return {
            child
            for child, edges in self._parents.items()
            if any(e.operator is BreedOperator.FUSION for e in edges)
        }

    def most_prolific(self, n: int = 10) -> list[tuple[BotId, int]]:
        """Los bots con más descendencia directa. Quién está poblando el jardín."""
        counts = [(bot, len(edges)) for bot, edges in self._children.items()]
        counts.sort(key=lambda t: t[1], reverse=True)
        return counts[:n]


# --------------------------------------------------------------------------- #
# Grafo para el dashboard                                                      #
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class GraphNode:
    id: BotId
    name: str
    family: str
    lineage: LineageId
    generation: int
    status: str
    fitness: float | None
    is_ensemble: bool
    depth: int


@dataclass(slots=True)
class GraphEdge:
    source: BotId
    target: BotId
    operator: str
    weight: float


@dataclass(slots=True)
class LineageGraph:
    """Lo que consume ``/api/lineage/graph``."""

    nodes: list[GraphNode] = field(default_factory=list)
    edges: list[GraphEdge] = field(default_factory=list)
    collapsed: list[dict[str, object]] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        # asdict y no __dict__: los nodos son dataclasses con slots y no tienen
        # diccionario de instancia.
        return {
            "nodes": [asdict(n) for n in self.nodes],
            "edges": [asdict(e) for e in self.edges],
            "collapsed": self.collapsed,
        }


def build_graph(
    bots: Sequence[Mapping[str, object]],
    edges: Sequence[ParentEdge],
    *,
    until_generation: int | None = None,
    node_limit: int = 2000,
) -> LineageGraph:
    """Construye el grafo genealógico para el dashboard.

    Args:
        bots: filas de la tabla ``bots`` (o dicts equivalentes) con al menos
            ``bot_id``, ``name``, ``family``, ``root_lineage``,
            ``born_generation``, ``status``, ``fitness_effective``.
        edges: aristas de parentesco.
        until_generation: si se da, el grafo se corta ahí. Es lo que alimenta
            el slider temporal: reproducir el jardín generación a generación.
        node_limit: por encima de este número se colapsan los linajes extintos
            en un nodo-resumen por (linaje, generación).

    Returns:
        El grafo, con ``collapsed`` describiendo los grupos plegados.
    """
    rows = [
        b for b in bots
        if until_generation is None or int(b["born_generation"]) <= until_generation  # type: ignore[arg-type]
    ]
    ped = Pedigree.from_edges(edges)
    keep_ids = {str(b["bot_id"]) for b in rows}

    nodes: list[GraphNode] = []
    collapsed: list[dict[str, object]] = []

    if len(rows) <= node_limit:
        selected = rows
    else:
        # Se conservan enteros los bots vivos, la élite y todo lo reciente; los
        # linajes extintos antiguos se pliegan en un nodo por (linaje, gen).
        alive = [b for b in rows if str(b["status"]) in {"ALIVE", "RETIRED"}]
        recent_cut = max((int(b["born_generation"]) for b in rows), default=0) - 10
        recent = [b for b in rows if int(b["born_generation"]) > recent_cut]
        selected_map = {str(b["bot_id"]): b for b in (*alive, *recent)}
        buckets: dict[tuple[str, int], list[Mapping[str, object]]] = defaultdict(list)
        for b in rows:
            if str(b["bot_id"]) not in selected_map:
                buckets[(str(b["root_lineage"]), int(b["born_generation"]))].append(b)
        for (lin, gen), group in buckets.items():
            collapsed.append(
                {
                    "id": f"grp_{lin}_{gen}",
                    "lineage": lin,
                    "generation": gen,
                    "count": len(group),
                    "members": [str(b["bot_id"]) for b in group],
                }
            )
        selected = list(selected_map.values())
        keep_ids = set(selected_map)

    for b in selected:
        bid = str(b["bot_id"])
        nodes.append(
            GraphNode(
                id=bid,
                name=str(b.get("name", bid)),
                family=str(b["family"]),
                lineage=str(b["root_lineage"]),
                generation=int(b["born_generation"]),
                status=str(b["status"]),
                fitness=(
                    float(b["fitness_effective"])  # type: ignore[arg-type]
                    if b.get("fitness_effective") is not None
                    else None
                ),
                is_ensemble=bool(b.get("is_ensemble", False)),
                depth=ped.generation_depth(bid),
            )
        )

    graph_edges = [
        GraphEdge(e.parent, e.child, str(e.operator), e.weight)
        for e in edges
        if e.parent in keep_ids and e.child in keep_ids
    ]

    return LineageGraph(nodes=nodes, edges=graph_edges, collapsed=collapsed)


__all__ = (
    "ParentEdge",
    "Pedigree",
    "GraphNode",
    "GraphEdge",
    "LineageGraph",
    "build_graph",
)
