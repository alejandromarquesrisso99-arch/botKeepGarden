"""API de sólo lectura del dashboard.

Abre la base en modo ``ro``. El dashboard **nunca escribe** en el jardín.

Endpoints en docs/DASHBOARD.md §API, más los dos de la incubadora que se
añadieron en el hito 6 (ver docs/DECISIONS.md D-020): la cría es la mitad de
la evolución y hasta ahora sólo se veía el resultado, nunca la criba.

Todo lo que sale de aquí es JSON plano: la lógica vive separada del enrutado de
FastAPI para poder testearla sin levantar un servidor.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections import defaultdict
from dataclasses import asdict, dataclass
from typing import Any

from ..config import Config
from ..storage.db import Database
from ..storage.repositories import Repositories
from ..types import TIMEFRAME_MS

#: Estados que cuentan como "sigue en el jardín".
LIVE_STATES: frozenset[str] = frozenset({"ALIVE", "RETIRED"})

#: Ámbito de métricas preferido. El jardín vivo manda sobre la incubadora, pero
#: un jardín que sólo ha corrido cosechas de incubadora tiene que verse igual.
SCOPES: tuple[str, ...] = ("live", "incubator")

#: Tope de bots en el mapa genético. La matriz de distancias es O(n²) y cada par
#: cuesta decenas de microsegundos: con 600 vivos son seis segundos de espera
#: antes de ver nada. Un jardín en su tamaño de diseño (``max_population``, 120)
#: nunca lo toca; si lo toca, se proyectan los mejores y la vista lo dice.
MAX_SCATTER_POINTS: int = 250


def _row(fila: sqlite3.Row | None) -> dict[str, Any]:
    """Una fila de SQLite a dict JSON-serializable."""
    if fila is None:
        return {}
    # .keys() no es opcional: iterar una sqlite3.Row da sus valores, no sus
    # nombres de columna.
    return {k: fila[k] for k in fila.keys()}  # noqa: SIM118


def _rows(filas: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [_row(f) for f in filas]


def _loads(texto: Any) -> Any:
    """JSON guardado en una columna TEXT. Nunca revienta: el dashboard no puede
    caerse porque una fila antigua traiga basura."""
    if not texto:
        return None
    if isinstance(texto, (dict, list)):
        return texto
    try:
        return json.loads(texto)
    except (ValueError, TypeError):
        return None


def _clean(valor: Any) -> Any:
    """``NaN`` e ``inf`` no son JSON válido. Se mandan como ``null``."""
    if isinstance(valor, float) and not math.isfinite(valor):
        return None
    return valor


def _cleaned(d: dict[str, Any]) -> dict[str, Any]:
    return {k: _clean(v) for k, v in d.items()}


def reject_category(reason: str | None) -> str:
    """Agrupa el motivo de rechazo de la incubadora en una familia legible.

    Los motivos traen números ("sortino -2.60 por debajo del umbral 0.86") y
    como cadena cruda no se pueden contar: cada rechazo sería su propia
    categoría. Lo que interesa ver es *por qué* mueren los candidatos, no con
    qué decimal.
    """
    r = (reason or "").strip().lower()
    if not r:
        return "sin motivo"
    if r.startswith("clon"):
        return "clon de un vivo"
    if "opera poco" in r:
        return "opera poco"
    if "freno de drawdown" in r:
        return "freno de drawdown"
    if r.startswith("sortino"):
        return "sortino bajo"
    if r.startswith("drawdown"):
        return "drawdown alto"
    if "degrada" in r:
        return "se degrada fuera de muestra"
    return "otro"


@dataclass(slots=True)
class DashboardAPI:
    """Lógica de las respuestas, separada del enrutado de FastAPI para poder
    testearla sin levantar un servidor."""

    cfg: Config
    repos: Repositories

    # -- utilidades internas ------------------------------------------------ #

    @property
    def _db(self) -> Database:
        return self.repos.db

    def current_generation(self) -> int:
        """Generación vigente. Es también la clave de invalidación de la caché
        del servidor: mientras no cambie, el pasado no cambia."""
        meta = self._db.get_meta("current_generation")
        if meta is not None:
            return int(meta)
        ultima = self.repos.generations.latest()
        return int(ultima["generation"]) if ultima is not None else 0

    def _bots_table(self) -> list[dict[str, Any]]:
        """Todos los bots con el par de campos que sólo vive en ``genomes``."""
        return _rows(
            self._db.query(
                "SELECT b.*, g.is_ensemble, g.complexity, g.n_features, g.hash AS genome_hash "
                "FROM bots b JOIN genomes g ON g.genome_id = b.genome_id "
                "ORDER BY b.born_generation, b.bot_id"
            )
        )

    def _metrics_scope(self, generation: int) -> str:
        """El ámbito con datos para esta generación: vivo si lo hay, incubadora
        si el jardín todavía no ha corrido en vivo."""
        for scope in SCOPES:
            fila = self._db.query_one(
                "SELECT 1 FROM bot_metrics WHERE generation = ? AND scope = ? LIMIT 1",
                (int(generation), scope),
            )
            if fila is not None:
                return scope
        return "live"

    # -- portada ----------------------------------------------------------- #

    def garden_summary(self) -> dict[str, Any]:
        """Tarjetas de la portada: capital, alfa, población, especies,
        diversidad, generación, edad del jardín, alertas, sesión pendiente."""
        generacion = self.current_generation()
        vivos = _rows(self.repos.bots.alive())
        todos = self._db.query_one(
            "SELECT COUNT(*) AS n, SUM(status = 'CULLED') AS muertos, "
            "SUM(status = 'RETIRED') AS jubilados FROM bots"
        )
        ultima = _row(self.repos.generations.latest())
        capital = sum(float(b["equity"] or 0.0) for b in vivos)
        invertido = sum(float(b["initial_capital"] or 0.0) for b in vivos)

        familias: dict[str, int] = defaultdict(int)
        for b in vivos:
            familias[str(b["family"])] += 1

        mejores = sorted(
            (b for b in vivos if b.get("fitness_effective") is not None),
            key=lambda b: -float(b["fitness_effective"]),
        )[:5]

        pendientes = self._db.query_one(
            "SELECT COUNT(*) AS n FROM gardener_sessions WHERE closed_ts IS NULL"
        )
        nacimientos = self._db.query_one(
            "SELECT COUNT(*) AS n FROM bots WHERE born_generation > 0"
        )

        return {
            "generation": generacion,
            "created_at": self._db.get_meta("created_at"),
            "seed": self._db.get_meta("seed"),
            "symbol": self._db.get_meta("symbol") or self.cfg.primary_symbol,
            "timeframe": self._db.get_meta("timeframe") or self.cfg.market.timeframe,
            "venue": self._db.get_meta("venue") or self.cfg.market.venue,
            "status": self._db.get_meta("status"),
            "n_alive": len(vivos),
            "n_total": int(todos["n"] or 0),
            "n_dead": int(todos["muertos"] or 0),
            "n_retired": int(todos["jubilados"] or 0),
            "n_born": int(nacimientos["n"] or 0),
            "n_species": int(ultima.get("n_species") or 0),
            "genetic_diversity": _clean(ultima.get("genetic_diversity")),
            "diversity_floor": self.cfg.evolution.diversity_floor,
            "garden_equity": _clean(ultima.get("garden_equity")),
            "benchmark_equity": _clean(ultima.get("benchmark_equity")),
            "garden_alpha": _clean(ultima.get("garden_alpha")),
            "garden_drawdown": _clean(ultima.get("garden_drawdown")),
            "capital": capital,
            "capital_invested": invertido,
            "fitness_best": _clean(ultima.get("fitness_best")),
            "fitness_median": _clean(ultima.get("fitness_median")),
            "family_shares": _loads(ultima.get("family_shares")) or {},
            "families": dict(sorted(familias.items())),
            "top_bots": [
                {
                    "bot_id": b["bot_id"], "name": b["name"], "family": b["family"],
                    "born_generation": b["born_generation"],
                    "fitness": _clean(b["fitness_effective"]),
                    "total_trades": b["total_trades"],
                    "equity": _clean(b["equity"]),
                    "operator": b["operator"],
                }
                for b in mejores
            ],
            "alerts": self.alerts(),
            "pending_gardener_sessions": int(pendientes["n"] or 0),
            "has_live_data": self._db.query_one(
                "SELECT 1 FROM garden_equity LIMIT 1"
            ) is not None,
        }

    def garden_equity(self, frm: int | None = None, to: int | None = None) -> dict[str, Any]:
        """Curva del jardín contra el benchmark, decimada a
        ``cfg.dashboard.series_points`` con LTTB."""
        puntos = self.cfg.dashboard.series_points
        filas = self.repos.equity.garden_curve(max_points=puntos)
        serie = [
            f for f in filas
            if (frm is None or int(f["ts"]) >= frm) and (to is None or int(f["ts"]) <= to)
        ]
        return {
            "points": [
                {
                    "ts": int(f["ts"]),
                    "equity": _clean(f["equity"]),
                    "benchmark": _clean(f["benchmark_equity"]),
                }
                for f in serie
            ],
            "empty": not serie,
        }

    # -- generaciones ------------------------------------------------------ #

    def generations(self) -> list[dict[str, Any]]:
        """Una fila por generación con la demografía ya cruzada.

        Los nacimientos se reparten por operador y las muertes por causa: es lo
        que pinta las barras apiladas y lo que cuenta la historia de la cría.
        """
        nacimientos: dict[int, dict[str, int]] = defaultdict(dict)
        for f in self._db.query(
            "SELECT born_generation AS g, operator, COUNT(*) AS n FROM bots "
            "GROUP BY born_generation, operator"
        ):
            nacimientos[int(f["g"])][str(f["operator"])] = int(f["n"])

        muertes: dict[int, dict[str, int]] = defaultdict(dict)
        for f in self._db.query(
            "SELECT died_generation AS g, COALESCE(death_cause, 'DESCONOCIDA') AS causa, "
            "COUNT(*) AS n FROM bots WHERE died_generation IS NOT NULL "
            "GROUP BY died_generation, death_cause"
        ):
            muertes[int(f["g"])][str(f["causa"])] = int(f["n"])

        criba: dict[int, dict[str, int]] = {}
        for f in self._db.query(
            "SELECT generation AS g, COUNT(*) AS candidatos, "
            "SUM(passed) AS aprobados FROM incubation_runs GROUP BY generation"
        ):
            criba[int(f["g"])] = {
                "candidates": int(f["candidatos"] or 0),
                "passed": int(f["aprobados"] or 0),
            }

        salida: list[dict[str, Any]] = []
        for fila in self.repos.generations.all():
            g = int(fila["generation"])
            d = _cleaned(_row(fila))
            d["family_shares"] = _loads(fila["family_shares"]) or {}
            d["lineage_shares"] = _loads(fila["lineage_shares"]) or {}
            d["effective_params"] = _loads(fila["effective_params"]) or {}
            d["births_by_operator"] = nacimientos.get(g, {})
            d["deaths_by_cause"] = muertes.get(g, {})
            d["incubation"] = criba.get(g, {"candidates": 0, "passed": 0})
            salida.append(d)
        return salida

    def generation(self, n: int) -> dict[str, Any]:
        """El detalle de una generación: quién nació, quién murió, qué especies
        había y qué hizo la incubadora."""
        fila = self.repos.generations.get(n)
        if fila is None:
            raise KeyError(f"no existe la generación {n}")

        d = _cleaned(_row(fila))
        d["family_shares"] = _loads(fila["family_shares"]) or {}
        d["lineage_shares"] = _loads(fila["lineage_shares"]) or {}
        d["effective_params"] = _loads(fila["effective_params"]) or {}

        d["births"] = [
            {**_cleaned(_row(f)), "parents": self.repos.lineage.parents_of(f["bot_id"])}
            for f in self._db.query(
                "SELECT bot_id, name, family, operator, root_lineage, status, "
                "fitness_effective FROM bots WHERE born_generation = ? ORDER BY bot_id",
                (int(n),),
            )
        ]
        d["deaths"] = [
            _cleaned(_row(f))
            for f in self._db.query(
                "SELECT bot_id, name, family, death_cause, born_generation, "
                "fitness_effective FROM bots WHERE died_generation = ? ORDER BY bot_id",
                (int(n),),
            )
        ]
        d["species"] = _rows(self.repos.generations.species_of(n))

        scope = self._metrics_scope(n)
        d["metrics_scope"] = scope
        d["ranking"] = [
            _cleaned(_row(f)) for f in self.repos.metrics.for_generation(n, scope)
        ]
        nombres = {
            f["bot_id"]: f["name"]
            for f in self._db.query("SELECT bot_id, name FROM bots")
        }
        for r in d["ranking"]:
            r["name"] = nombres.get(r["bot_id"], r["bot_id"])

        d["incubation"] = self.incubation(n)
        d["events"] = [
            {**_row(f), "payload": _loads(f["payload"])}
            for f in self.repos.events.for_generation(n)
        ]
        return d

    # -- cría e incubadora -------------------------------------------------- #

    def incubation(self, generation: int) -> dict[str, Any]:
        """La criba de una generación: qué se concibió y qué llegó a nacer.

        Es la vista de la cría. Un jardín sano descarta mucho: ver *por qué*
        descarta es lo que dice si los operadores están produciendo ideas o
        clones."""
        filas = _rows(self.repos.incubation.for_generation(generation))
        por_categoria: dict[str, int] = defaultdict(int)
        por_operador: dict[str, dict[str, int]] = defaultdict(
            lambda: {"candidates": 0, "passed": 0}
        )
        for f in filas:
            f["parents"] = _loads(f["parents"]) or []
            f["fold_metrics"] = _loads(f["fold_metrics"]) or []
            f["reject_category"] = (
                None if f["passed"] else reject_category(f["reject_reason"])
            )
            op = str(f["operator"])
            por_operador[op]["candidates"] += 1
            por_operador[op]["passed"] += int(f["passed"])
            if not f["passed"]:
                por_categoria[reject_category(f["reject_reason"])] += 1

        aprobados = sum(1 for f in filas if f["passed"])
        return {
            "generation": int(generation),
            "candidates": len(filas),
            "passed": aprobados,
            "rejected": len(filas) - aprobados,
            "pass_rate": (aprobados / len(filas)) if filas else None,
            "threshold_used": next(
                (_clean(f["threshold_used"]) for f in filas if f["threshold_used"] is not None),
                None,
            ),
            "reject_reasons": dict(
                sorted(por_categoria.items(), key=lambda kv: -kv[1])
            ),
            "by_operator": {k: v for k, v in sorted(por_operador.items())},
            "runs": [_cleaned(f) for f in filas],
        }

    def breeding(self) -> dict[str, Any]:
        """El embudo de la cría a lo largo de todo el jardín.

        Concebidos → aprobados por la incubadora → nacidos → todavía vivos, por
        generación y por operador, más la tasa de supervivencia de cada
        operador. Responde a la pregunta del jardinero: ¿qué forma de criar
        está produciendo bots que duran?
        """
        generacion = self.current_generation()

        criba = _rows(
            self._db.query(
                "SELECT generation, operator, COUNT(*) AS candidatos, "
                "SUM(passed) AS aprobados FROM incubation_runs "
                "GROUP BY generation, operator ORDER BY generation, operator"
            )
        )
        nacidos = _rows(
            self._db.query(
                "SELECT born_generation AS generation, operator, COUNT(*) AS nacidos, "
                "SUM(status IN ('ALIVE','RETIRED')) AS vivos FROM bots "
                "GROUP BY born_generation, operator ORDER BY born_generation, operator"
            )
        )

        por_generacion: dict[int, dict[str, Any]] = {}
        for f in criba:
            g = por_generacion.setdefault(
                int(f["generation"]),
                {"generation": int(f["generation"]), "candidates": 0, "passed": 0,
                 "born": 0, "alive": 0, "by_operator": {}},
            )
            op = g["by_operator"].setdefault(
                str(f["operator"]),
                {"candidates": 0, "passed": 0, "born": 0, "alive": 0},
            )
            op["candidates"] += int(f["candidatos"] or 0)
            op["passed"] += int(f["aprobados"] or 0)
            g["candidates"] += int(f["candidatos"] or 0)
            g["passed"] += int(f["aprobados"] or 0)
        for f in nacidos:
            g = por_generacion.setdefault(
                int(f["generation"]),
                {"generation": int(f["generation"]), "candidates": 0, "passed": 0,
                 "born": 0, "alive": 0, "by_operator": {}},
            )
            op = g["by_operator"].setdefault(
                str(f["operator"]),
                {"candidates": 0, "passed": 0, "born": 0, "alive": 0},
            )
            op["born"] += int(f["nacidos"] or 0)
            op["alive"] += int(f["vivos"] or 0)
            g["born"] += int(f["nacidos"] or 0)
            g["alive"] += int(f["vivos"] or 0)

        motivos: dict[str, int] = defaultdict(int)
        for f in self._db.query(
            "SELECT reject_reason, COUNT(*) AS n FROM incubation_runs "
            "WHERE passed = 0 GROUP BY reject_reason"
        ):
            motivos[reject_category(f["reject_reason"])] += int(f["n"])

        totales = _rows(
            self._db.query(
                "SELECT operator, COUNT(*) AS nacidos, "
                "SUM(status IN ('ALIVE','RETIRED')) AS vivos, "
                "AVG(CASE WHEN died_generation IS NULL THEN ? - born_generation "
                "         ELSE died_generation - born_generation END) AS edad_media, "
                "AVG(fitness_effective) AS fitness_medio "
                "FROM bots GROUP BY operator ORDER BY operator",
                (int(generacion),),
            )
        )

        fusiones = _rows(
            self._db.query(
                "SELECT b.bot_id, b.name, b.born_generation, b.status, "
                "b.fitness_effective, COUNT(p.parent_id) AS n_padres "
                "FROM bots b JOIN parentage p ON p.child_id = b.bot_id "
                "WHERE p.operator = 'FUSION' GROUP BY b.bot_id "
                "ORDER BY b.born_generation DESC"
            )
        )

        prolificos = _rows(
            self._db.query(
                "SELECT b.bot_id, b.name, b.family, b.status, b.born_generation, "
                "COUNT(p.child_id) AS hijos FROM bots b "
                "JOIN parentage p ON p.parent_id = b.bot_id "
                "GROUP BY b.bot_id ORDER BY hijos DESC, b.born_generation LIMIT 12"
            )
        )

        return {
            "generation": generacion,
            "by_generation": [por_generacion[k] for k in sorted(por_generacion)],
            "reject_reasons": dict(sorted(motivos.items(), key=lambda kv: -kv[1])),
            "by_operator": [_cleaned(f) for f in totales],
            "survival_rates": self.repos.gardener.operator_success_rates(generacion),
            "fusions": [_cleaned(f) for f in fusiones],
            "most_prolific": [_cleaned(f) for f in prolificos],
            "shares": {
                "mutation": self.cfg.evolution.mutation_share,
                "crossover": self.cfg.evolution.crossover_share,
                "fusion": self.cfg.evolution.fusion_share,
                "seed": self.cfg.evolution.seed_share,
            },
        }

    # -- bots -------------------------------------------------------------- #

    def bots(self, *, status: str | None = None, family: str | None = None,
             lineage: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        if status:
            where.append("b.status = ?")
            params.append(status.upper())
        if family:
            where.append("b.family = ?")
            params.append(family.upper())
        if lineage:
            where.append("b.root_lineage = ?")
            params.append(lineage)
        filtro = f"WHERE {' AND '.join(where)}" if where else ""
        filas = self._db.query(
            f"SELECT b.*, g.is_ensemble FROM bots b "
            f"JOIN genomes g ON g.genome_id = b.genome_id {filtro} "
            f"ORDER BY b.fitness_effective DESC NULLS LAST, b.born_generation DESC "
            f"LIMIT ?",
            (*params, int(limit)),
        )
        return [_cleaned(_row(f)) for f in filas]

    def bot(self, bot_id: str) -> dict[str, Any]:
        """Ficha completa: cabecera, genoma legible y crudo, padres, hijos,
        métricas por generación e historia de eventos."""
        fila = self._db.query_one(
            "SELECT b.*, g.is_ensemble, g.complexity, g.n_features, g.max_depth, "
            "g.hash AS genome_hash, g.payload FROM bots b "
            "JOIN genomes g ON g.genome_id = b.genome_id WHERE b.bot_id = ?",
            (bot_id,),
        )
        if fila is None:
            raise KeyError(f"no hay ningún bot con id {bot_id!r}")

        d = _cleaned(_row(fila))
        d.pop("payload", None)
        d["genome"] = self.bot_genome(bot_id)

        def ficha(ids: list[str]) -> list[dict[str, Any]]:
            if not ids:
                return []
            huecos = ",".join("?" for _ in ids)
            return [
                _cleaned(_row(f))
                for f in self._db.query(
                    f"SELECT bot_id, name, family, status, born_generation, "
                    f"fitness_effective FROM bots WHERE bot_id IN ({huecos})",
                    tuple(ids),
                )
            ]

        aristas = _rows(
            self._db.query(
                "SELECT parent_id, operator, weight, ordinal FROM parentage "
                "WHERE child_id = ? ORDER BY ordinal",
                (bot_id,),
            )
        )
        padres = ficha([a["parent_id"] for a in aristas])
        por_id = {p["bot_id"]: p for p in padres}
        d["parents"] = [
            {**por_id.get(a["parent_id"], {"bot_id": a["parent_id"]}),
             "operator": a["operator"], "weight": _clean(a["weight"])}
            for a in aristas
        ]
        d["children"] = ficha(self.repos.lineage.children_of(bot_id))
        d["metrics"] = [_cleaned(_row(f)) for f in self.repos.metrics.history(bot_id)]
        d["events"] = [
            {**_row(f), "payload": _loads(f["payload"])}
            for f in self.repos.events.for_bot(bot_id)
        ]
        d["holdout"] = _cleaned(_row(self.repos.incubation.holdout_of(bot_id)))
        d["incubation"] = [
            {**_cleaned(_row(f)), "fold_metrics": _loads(f["fold_metrics"]) or [],
             "parents": _loads(f["parents"]) or []}
            for f in self._db.query(
                "SELECT * FROM incubation_runs WHERE genome_id = ? ORDER BY run_id",
                (fila["genome_id"],),
            )
        ]
        d["n_trades"] = int(
            (self._db.query_one(
                "SELECT COUNT(*) AS n FROM trades WHERE bot_id = ?", (bot_id,)
            ) or {"n": 0})["n"] or 0
        )
        return d

    def bot_equity(self, bot_id: str) -> dict[str, Any]:
        filas = self.repos.equity.curve(
            bot_id, max_points=self.cfg.dashboard.series_points
        )
        return {
            "bot_id": bot_id,
            "points": [
                {"ts": int(f["ts"]), "equity": _clean(f["equity"])} for f in filas
            ],
            "empty": not filas,
        }

    def bot_trades(self, bot_id: str, limit: int = 500) -> list[dict[str, Any]]:
        return [
            _cleaned(_row(f)) for f in self.repos.trades.for_bot(bot_id, limit=limit)
        ]

    def bot_genome(self, bot_id: str) -> dict[str, Any]:
        """Genoma en las dos formas: ``describe()`` legible y JSON crudo."""
        genoma = self.repos.bots.genome_of(bot_id)
        return {
            "id": genoma.id,
            "family": str(genoma.family),
            "is_ensemble": genoma.is_ensemble,
            "complexity": genoma.complexity(),
            "describe": genoma.describe(),
            "raw": genoma.to_dict(),
        }

    # -- genealogía -------------------------------------------------------- #

    def lineage_graph(self, until_generation: int | None = None) -> dict[str, Any]:
        """El DAG genealógico. ``until_generation`` es lo que alimenta el
        slider temporal: reproducir la evolución del jardín generación a
        generación. Ver ``evolution.lineage.build_graph``.

        Cada nodo lleva además ``status_at``: el estado que tenía el bot *en*
        esa generación, no el de hoy. Sin eso el slider enseñaría el pasado
        pintado con los muertos de después, que es justo lo contrario de
        reproducir la evolución.
        """
        from ..evolution.lineage import build_graph

        bots = self._bots_table()
        edges = self.repos.lineage.all_edges(until_generation)
        grafo = build_graph(
            bots,
            edges,
            until_generation=until_generation,
            node_limit=self.cfg.dashboard.graph_node_limit,
        )
        por_id = {b["bot_id"]: b for b in bots}
        corte = until_generation

        nodos: list[dict[str, Any]] = []
        for n in grafo.nodes:
            b = por_id.get(n.id, {})
            murio = b.get("died_generation")
            if corte is None:
                estado = str(b.get("status", n.status))
            elif murio is not None and int(murio) <= corte:
                # Ya había muerto: el estado con el que salió del jardín.
                estado = str(b.get("status", "CULLED"))
            else:
                estado = "ALIVE"
            nodos.append(
                {
                    **asdict(n),
                    "status_at": estado,
                    "operator": b.get("operator"),
                    "died_generation": murio,
                    "death_cause": b.get("death_cause"),
                    "is_elite": bool(b.get("is_elite")),
                    "fitness": _clean(n.fitness),
                    "is_ensemble": bool(b.get("is_ensemble", n.is_ensemble)),
                }
            )

        linajes = sorted({str(n["lineage"]) for n in nodos})
        return {
            "nodes": nodos,
            "edges": [asdict(e) for e in grafo.edges],
            "collapsed": grafo.collapsed,
            "lineages": linajes,
            "families": sorted({str(n["family"]) for n in nodos}),
            "generations": {
                "min": min((int(n["generation"]) for n in nodos), default=0),
                "max": self.current_generation(),
            },
            "until_generation": until_generation,
            "node_limit": self.cfg.dashboard.graph_node_limit,
        }

    # -- especies ---------------------------------------------------------- #

    def species(self) -> list[dict[str, Any]]:
        generacion = self.current_generation()
        filas = self.repos.generations.species_of(generacion)
        while not filas and generacion > 0:
            generacion -= 1
            filas = self.repos.generations.species_of(generacion)

        miembros: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for f in self._db.query(
            "SELECT bot_id, name, family, species_id, fitness_effective FROM bots "
            "WHERE species_id IS NOT NULL AND status = 'ALIVE'"
        ):
            miembros[str(f["species_id"])].append(_cleaned(_row(f)))

        nombres = {
            f["bot_id"]: f["name"] for f in self._db.query("SELECT bot_id, name FROM bots")
        }
        salida = []
        for f in filas:
            d = _cleaned(_row(f))
            d["representative_name"] = nombres.get(f["representative_id"])
            d["members"] = miembros.get(str(f["species_id"]), [])
            salida.append(d)
        return salida

    def species_scatter(self) -> dict[str, Any]:
        """Proyección 2D de la población por distancia genética (MDS clásico
        sobre la matriz de distancias; UMAP si algún día hace falta).

        Junto con ``species_correlation``, responde a la pregunta que importa:
        ¿este jardín tiene ideas distintas o cincuenta copias de la misma?"""
        import numpy as np

        from ..genome.catalog import load_catalog
        from ..genome.distance import distance_matrix

        genomas = self.repos.bots.alive_genomes()
        if len(genomas) < 2:
            return {"points": [], "empty": True, "truncated": False, "n_alive": len(genomas)}

        catalogo = load_catalog(self.cfg.path("config/genes.yaml"))
        vivos = len(genomas)
        ids = list(genomas)
        truncado = vivos > MAX_SCATTER_POINTS
        if truncado:
            # Los mejores por fitness, y a igualdad el id, para que la selección
            # sea la misma en dos ejecuciones iguales.
            ranking = {
                str(f["bot_id"]): (
                    -float(f["fitness_effective"]) if f["fitness_effective"] is not None
                    else float("inf")
                )
                for f in self._db.query(
                    "SELECT bot_id, fitness_effective FROM bots WHERE status = 'ALIVE'"
                )
            }
            ids.sort(key=lambda b: (ranking.get(b, float("inf")), b))
            ids = ids[:MAX_SCATTER_POINTS]
            genomas = {b: genomas[b] for b in ids}
        matriz = np.asarray(
            distance_matrix(
                [genomas[i] for i in ids],
                self.cfg.speciation.distance_weights,
                catalogo,
                members=genomas,
            ),
            dtype=float,
        )

        # MDS clásico: doble centrado de las distancias al cuadrado y los dos
        # autovectores dominantes. Sin dependencias nuevas.
        n = len(ids)
        cuadrados = matriz**2
        centrado = np.eye(n) - np.ones((n, n)) / n
        b = -0.5 * centrado @ cuadrados @ centrado
        autovalores, autovectores = np.linalg.eigh(b)
        orden = np.argsort(autovalores)[::-1][:2]
        escala = np.sqrt(np.clip(autovalores[orden], 0.0, None))
        coords = autovectores[:, orden] * escala

        filas = {
            f["bot_id"]: _row(f)
            for f in self._db.query(
                "SELECT bot_id, name, family, species_id, fitness_effective, "
                "born_generation, status FROM bots WHERE status = 'ALIVE'"
            )
        }
        positivos = float(np.clip(autovalores, 0.0, None).sum()) or 1.0
        return {
            "points": [
                {
                    "bot_id": bid,
                    "x": float(coords[i, 0]),
                    "y": float(coords[i, 1]),
                    "name": filas.get(bid, {}).get("name", bid),
                    "family": filas.get(bid, {}).get("family"),
                    "species_id": filas.get(bid, {}).get("species_id"),
                    "generation": filas.get(bid, {}).get("born_generation"),
                    "fitness": _clean(filas.get(bid, {}).get("fitness_effective")),
                }
                for i, bid in enumerate(ids)
            ],
            "explained": float(np.clip(autovalores[orden], 0.0, None).sum() / positivos),
            "mean_distance": float(matriz[np.triu_indices(n, 1)].mean()),
            "clone_threshold": self.cfg.speciation.clone_threshold,
            "species_threshold": self.cfg.speciation.species_threshold,
            "truncated": truncado,
            "n_alive": vivos,
            "empty": False,
        }

    def species_correlation(self) -> dict[str, Any]:
        """Heatmap de correlación entre curvas de equity de los bots vivos."""
        import numpy as np

        vivos = _rows(
            self._db.query(
                "SELECT bot_id, name, family FROM bots WHERE status = 'ALIVE' "
                "ORDER BY family, bot_id"
            )
        )
        series: dict[str, dict[int, float]] = {}
        for b in vivos:
            filas = self._db.query(
                "SELECT ts, equity FROM equity_snapshots WHERE bot_id = ? ORDER BY ts",
                (b["bot_id"],),
            )
            if filas:
                series[b["bot_id"]] = {int(f["ts"]): float(f["equity"]) for f in filas}

        if len(series) < 2:
            return {"bots": [], "matrix": [], "empty": True}

        comunes = sorted(set.intersection(*(set(s) for s in series.values())))
        if len(comunes) < 3:
            return {"bots": [], "matrix": [], "empty": True}

        ids = list(series)
        retornos = np.array(
            [
                np.diff(np.log(np.maximum([series[bid][t] for t in comunes], 1e-12)))
                for bid in ids
            ]
        )
        with np.errstate(invalid="ignore", divide="ignore"):
            matriz = np.corrcoef(retornos)
        matriz = np.nan_to_num(matriz, nan=0.0)
        por_id = {b["bot_id"]: b for b in vivos}
        return {
            "bots": [
                {"bot_id": bid, "name": por_id[bid]["name"], "family": por_id[bid]["family"]}
                for bid in ids
            ],
            "matrix": [[round(float(v), 4) for v in fila] for fila in matriz],
            "threshold": self.cfg.fitness.correlation_threshold,
            "empty": False,
        }

    # -- jardinero --------------------------------------------------------- #

    def journal(self, limit: int = 20) -> list[dict[str, Any]]:
        sesiones = _rows(self.repos.gardener.journal(limit))
        for s in sesiones:
            s["decisions"] = [
                {**_cleaned(_row(f)), "payload": _loads(f["payload"])}
                for f in self._db.query(
                    "SELECT * FROM gardener_decisions WHERE session_id = ? "
                    "ORDER BY decision_id",
                    (s["session_id"],),
                )
            ]
        return sesiones

    def events(self, type: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        if type:
            filas = self._db.query(
                "SELECT * FROM events WHERE type = ? ORDER BY event_id DESC LIMIT ?",
                (type.upper(), int(limit)),
            )
        else:
            filas = self._db.query(
                "SELECT * FROM events ORDER BY event_id DESC LIMIT ?", (int(limit),)
            )
        return [{**_row(f), "payload": _loads(f["payload"])} for f in filas]

    def alerts(self) -> list[dict[str, Any]]:
        return [_cleaned(_row(f)) for f in self.repos.events.open_alerts()]

    # -- salud del sistema -------------------------------------------------- #

    def health(self) -> dict[str, Any]:
        """Si el jardín está vivo, y si los datos con los que vive son buenos.

        Las tres preguntas que hay que poder responder de un vistazo antes de
        dejar esto corriendo treinta días: ¿sigue latiendo?, ¿le llegan las
        velas?, ¿va sobrado de tiempo entre velas?
        """
        import time as _time

        ahora = int(_time.time() * 1000)
        ultimo = self._db.get_meta("last_tick_ts")
        ultimo_ts = int(ultimo) if ultimo else None
        tf_ms = TIMEFRAME_MS.get(
            str(self._db.get_meta("timeframe") or self.cfg.market.timeframe), 3_600_000
        )
        retraso = (ahora - ultimo_ts) if ultimo_ts else None
        tick_ms = self._db.get_meta("last_tick_ms")
        latencia = self._db.get_meta("venue_latency_ms")

        series = _rows(
            self._db.query(
                "SELECT venue, symbol, timeframe, COUNT(*) AS huecos, "
                "SUM(n_missing) AS velas_perdidas, SUM(filled) AS rellenados "
                "FROM data_gaps GROUP BY venue, symbol, timeframe"
            )
        )
        anomalias = _rows(
            self._db.query(
                "SELECT kind, COUNT(*) AS n FROM data_anomalies GROUP BY kind ORDER BY n DESC"
            )
        )
        fallos = self._db.query_one(
            "SELECT COUNT(*) AS n FROM events WHERE type = 'CIRCUIT_BREAKER' AND ts > ?",
            (ahora - 86_400_000,),
        )
        copias = sorted(
            self.cfg.path(self.cfg.storage.db_path).parent.glob("*.db"),
            key=lambda r: r.name,
        )

        return {
            "status": self._db.get_meta("status") or "STOPPED",
            "generation": self.current_generation(),
            "last_tick_ts": ultimo_ts,
            "lag_ms": retraso,
            # Un jardín al día va por detrás menos de dos velas: la que acaba de
            # cerrar más el margen de asentamiento.
            "fresh": bool(retraso is not None and retraso < 2 * tf_ms),
            "timeframe_ms": tf_ms,
            "tick_ms": float(tick_ms) if tick_ms else None,
            "tick_budget_ms": tf_ms,
            "venue_latency_ms": float(latencia) if latencia else None,
            "venue_failures_24h": int((fallos or {"n": 0})["n"] or 0),
            "data_gaps": series,
            "data_anomalies": anomalias,
            "snapshots": [
                {"name": r.name, "size_mb": round(r.stat().st_size / 1e6, 2)}
                for r in copias
                if r.name != self.cfg.path(self.cfg.storage.db_path).name
            ][-10:],
            "alerts": self.alerts(),
            "symbols": self._symbols(),
        }

    def _symbols(self) -> list[dict[str, Any]]:
        """Los mercados del jardín y cuántos bots vivos opera cada uno."""
        return _rows(
            self._db.query(
                "SELECT g.symbol, COUNT(*) AS bots, "
                "SUM(b.status = 'ALIVE') AS vivos FROM bots b "
                "JOIN genomes g ON g.genome_id = b.genome_id "
                "GROUP BY g.symbol ORDER BY vivos DESC"
            )
        )


__all__ = ("LIVE_STATES", "MAX_SCATTER_POINTS", "DashboardAPI", "reject_category")
