"""El informe de generación: lo que el jardinero lee antes de decidir.

Estructura completa en docs/GARDENER_PROTOCOL.md §El informe. El orden de las
nueve secciones no es casual: va de lo general a lo concreto y termina con las
decisiones anteriores y su resultado, que es lo que impide que el jardinero
repita el mismo consejo cada mes.

El informe tiene que bastar para decidir **sin mirar la base de datos**. Si hace
falta consultar algo que no está aquí, es un fallo del informe.
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..config import Config
from ..storage.repositories import Repositories

#: Cuántos bots se describen enteros en la sección 5.
TOP_BOTS = 10

#: Pares más correlacionados que se enseñan en la sección 4.
TOP_PAIRS = 5


def _fecha(ts: Any) -> str:
    try:
        return datetime.fromtimestamp(int(ts) / 1000, UTC).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return "—"


def _num(valor: Any, decimales: int = 3, signo: bool = False) -> str:
    if valor is None:
        return "—"
    try:
        v = float(valor)
    except (TypeError, ValueError):
        return str(valor)
    if not math.isfinite(v):
        return "—"
    return f"{v:+.{decimales}f}" if signo else f"{v:.{decimales}f}"


def _pct(valor: Any, decimales: int = 1) -> str:
    if valor is None:
        return "—"
    try:
        v = float(valor)
    except (TypeError, ValueError):
        return str(valor)
    return "—" if not math.isfinite(v) else f"{100 * v:.{decimales}f} %"


def _tabla(cabeceras: list[str], filas: list[list[str]]) -> str:
    """Una tabla Markdown. Sin filas, una línea honesta en vez de una tabla vacía."""
    if not filas:
        return "_(nada que mostrar)_\n"
    cuerpo = "\n".join("| " + " | ".join(f) + " |" for f in filas)
    return (
        "| " + " | ".join(cabeceras) + " |\n"
        "|" + "|".join("---" for _ in cabeceras) + "|\n"
        + cuerpo + "\n"
    )


@dataclass(slots=True)
class ReportBuilder:
    cfg: Config
    repos: Repositories

    # -- utilidades --------------------------------------------------------- #

    @property
    def _db(self):  # type: ignore[no-untyped-def]
        return self.repos.db

    def _scope(self, generation: int) -> str:
        """El jardín vivo manda; si no ha corrido, la evidencia es la incubadora."""
        fila = self._db.query_one(
            "SELECT 1 FROM bot_metrics WHERE generation = ? AND scope = 'live' LIMIT 1",
            (int(generation),),
        )
        return "live" if fila is not None else "incubator"

    def _resolve(self, generation: int | str) -> int:
        if isinstance(generation, str) and generation.lower() in {"latest", "ultima", "última"}:
            fila = self.repos.generations.latest()
            if fila is None:
                raise KeyError("el jardín no tiene ninguna generación cerrada todavía")
            return int(fila["generation"])
        return int(generation)

    # -- el informe --------------------------------------------------------- #

    def build(self, generation: int) -> str:
        """Genera el informe en Markdown."""
        generation = self._resolve(generation)
        fila = self.repos.generations.get(generation)
        if fila is None:
            raise KeyError(f"no existe la generación {generation}")

        partes = [
            self._cabecera(generation, fila),
            self._demografia(generation, fila),
            self._rendimiento(generation, fila),
            self._diversidad(generation, fila),
            self._mejores(generation),
            self._muertos(generation),
            self._que_ha_funcionado(generation),
            self._alertas(),
            self._decisiones_anteriores(generation),
            self._como_responder(),
        ]
        return "\n".join(partes)

    def write(self, generation: int) -> Path:
        """Escribe el informe en ``state/reports/gen_<n>.md`` y devuelve la ruta."""
        generation = self._resolve(generation)
        destino = self.cfg.path(self.cfg.gardener.report_dir)
        destino.mkdir(parents=True, exist_ok=True)
        ruta = destino / f"gen_{generation}.md"
        ruta.write_text(self.build(generation), encoding="utf-8")
        return ruta

    # -- 1. resumen --------------------------------------------------------- #

    def _cabecera(self, generation: int, fila: sqlite3.Row) -> str:
        from .journal import Journal

        vivos = self.repos.bots.count_alive()
        alfa = fila["garden_alpha"]
        diversidad = fila["genetic_diversity"]
        titular = self._titular(generation, fila)

        lineas = [
            f"# Jardín · generación {generation}",
            "",
            f"_{_fecha(fila['ended_ts'] or fila['started_ts'])} UTC · "
            f"{self._db.get_meta('symbol') or self.cfg.primary_symbol} "
            f"{self._db.get_meta('timeframe') or self.cfg.market.timeframe} · "
            f"semilla {self._db.get_meta('seed') or self.cfg.seed}_",
            "",
            "## 1. Resumen",
            "",
            f"**{titular}**",
            "",
            f"{vivos} bots vivos · {fila['n_species'] or 0} especies · "
            f"diversidad {_num(diversidad)} (suelo {self.cfg.evolution.diversity_floor}) · "
            f"alfa {_pct(alfa) if alfa is not None else '—'} · "
            f"fitness mediana {_num(fila['fitness_median'], signo=True)}",
            "",
        ]
        digest = Journal(self.repos).context_digest(generation)
        if digest:
            lineas += ["> " + digest.replace("\n", "\n> "), ""]
        return "\n".join(lineas)

    def _titular(self, generation: int, fila: sqlite3.Row) -> str:
        """El cambio más importante de la generación, en una línea."""
        anterior = self.repos.generations.get(generation - 1)
        if fila["genetic_diversity"] is not None and (
            float(fila["genetic_diversity"]) < self.cfg.evolution.diversity_floor
        ):
            return "El jardín ha bajado del suelo de diversidad: está convergiendo."
        if anterior is not None and None not in (
            fila["fitness_median"], anterior["fitness_median"]
        ):
            delta = float(fila["fitness_median"]) - float(anterior["fitness_median"])
            if abs(delta) >= 0.1:
                direccion = "sube" if delta > 0 else "baja"
                return (
                    f"La mediana de fitness {direccion} "
                    f"{abs(delta):.2f} respecto a la generación anterior."
                )
        if int(fila["deaths"] or 0) > int(fila["births"] or 0):
            return (
                f"Mueren más de los que nacen ({fila['deaths']} frente a "
                f"{fila['births']}): la población se está encogiendo."
            )
        if int(fila["births"] or 0) == 0:
            return "Ningún candidato ha pasado la incubadora esta generación."
        return "El jardín sigue su curso sin cambios de régimen."

    # -- 2. demografía ------------------------------------------------------ #

    def _demografia(self, generation: int, fila: sqlite3.Row) -> str:
        por_operador = self._db.query(
            "SELECT operator, COUNT(*) AS n FROM bots WHERE born_generation = ? "
            "GROUP BY operator ORDER BY n DESC",
            (generation,),
        )
        criba = self._db.query_one(
            "SELECT COUNT(*) AS candidatos, SUM(passed) AS aprobados "
            "FROM incubation_runs WHERE generation = ?",
            (generation,),
        )
        edades = self._db.query_one(
            "SELECT AVG(? - born_generation) AS media, MAX(? - born_generation) AS maxima "
            "FROM bots WHERE status = 'ALIVE'",
            (generation, generation),
        )
        return "\n".join(
            [
                "## 2. Demografía",
                "",
                f"- Población: **{fila['population_size'] or 0}** "
                f"(objetivo {self.cfg.garden.target_population}, "
                f"máximo {self.cfg.garden.max_population})",
                f"- Nacimientos: **{fila['births'] or 0}** "
                + (
                    "("
                    + ", ".join(f"{r['operator']} {r['n']}" for r in por_operador)
                    + ")"
                    if por_operador
                    else ""
                ),
                f"- Muertes: **{fila['deaths'] or 0}** · fusiones: {fila['fusions'] or 0}",
                f"- Incubadora: {int((criba or {'candidatos': 0})['candidatos'] or 0)} "
                f"candidatos, {int((criba or {'aprobados': 0})['aprobados'] or 0)} aprobados "
                f"({fila['discarded'] or 0} descartados)",
                f"- Especies: {fila['n_species'] or 0} · edad media "
                f"{_num((edades or {'media': None})['media'], 1)} generaciones · "
                f"el más viejo lleva {int((edades or {'maxima': 0})['maxima'] or 0)}",
                "",
            ]
        )

    # -- 3. rendimiento ----------------------------------------------------- #

    def _rendimiento(self, generation: int, fila: sqlite3.Row) -> str:
        scope = self._scope(generation)
        metricas = self.repos.metrics.for_generation(generation, scope)
        definidas = [m for m in metricas if m["fitness"] is not None]
        sin_juicio = len(metricas) - len(definidas)

        lineas = [
            "## 3. Rendimiento",
            "",
            f"Capital del jardín: **{_num(fila['garden_equity'], 2)}** · "
            f"benchmark (buy & hold con las mismas entradas de capital): "
            f"{_num(fila['benchmark_equity'], 2)} · "
            f"alfa **{_pct(fila['garden_alpha']) if fila['garden_alpha'] is not None else '—'}** · "
            f"drawdown {_pct(fila['garden_drawdown'])}",
            "",
            f"Distribución de fitness (ámbito `{scope}`): "
            f"peor {_num(fila['fitness_worst'], signo=True)} · "
            f"p25 {_num(fila['fitness_p25'], signo=True)} · "
            f"**mediana {_num(fila['fitness_median'], signo=True)}** · "
            f"p75 {_num(fila['fitness_p75'], signo=True)} · "
            f"mejor {_num(fila['fitness_best'], signo=True)}",
            "",
        ]
        if sin_juicio:
            lineas += [
                f"> {sin_juicio} de {len(metricas)} bots se quedan sin fitness definido "
                f"por operar menos de {self.cfg.fitness.min_trades} veces en la ventana. "
                "Sin evidencia no hay juicio: la selección les da otra generación.",
                "",
            ]
        return "\n".join(lineas)

    # -- 4. diversidad ------------------------------------------------------ #

    def _diversidad(self, generation: int, fila: sqlite3.Row) -> str:
        familias = json.loads(fila["family_shares"]) if fila["family_shares"] else {}
        linajes = self._db.query(
            "SELECT root_lineage, COUNT(*) AS n FROM bots WHERE status = 'ALIVE' "
            "GROUP BY root_lineage ORDER BY n DESC LIMIT 6",
            (),
        )
        vivos = self.repos.bots.count_alive() or 1
        pares, media, maxima = self._correlaciones()

        lineas = [
            "## 4. Diversidad",
            "",
            f"Distancia genética media: **{_num(fila['genetic_diversity'])}** "
            f"(suelo {self.cfg.evolution.diversity_floor})",
            "",
            "**Por familia de ideas**",
            "",
            _tabla(
                ["Familia", "Cuota"],
                [
                    [f, _pct(c)]
                    for f, c in sorted(familias.items(), key=lambda kv: -kv[1])
                ],
            ),
            "**Por linaje raíz** (los 6 mayores)",
            "",
            _tabla(
                ["Linaje", "Bots", "Cuota"],
                [
                    [str(r["root_lineage"]), str(r["n"]), _pct(int(r["n"]) / vivos)]
                    for r in linajes
                ],
            ),
        ]
        if pares:
            lineas += [
                f"**Correlación entre curvas de capital**: media {_num(media)}, "
                f"máxima {_num(maxima)} "
                f"(umbral de penalización {self.cfg.fitness.correlation_threshold})",
                "",
                _tabla(
                    ["Par", "Correlación"],
                    [[f"{a} · {b}", _num(c)] for a, b, c in pares],
                ),
            ]
        else:
            lineas += [
                "_Sin curvas de capital suficientes para correlacionar: "
                "el jardín vivo todavía no ha dejado historia._",
                "",
            ]
        return "\n".join(lineas)

    def _correlaciones(self) -> tuple[list[tuple[str, str, float]], float, float]:
        """Los pares más correlacionados de la población viva."""
        import numpy as np

        from ..evaluation.metrics import rolling_correlation, simple_returns

        curvas: dict[str, Any] = {}
        nombres: dict[str, str] = {}
        for fila in self.repos.bots.alive():
            puntos = self.repos.equity.curve(fila["bot_id"], max_points=500)
            if len(puntos) > 10:
                curvas[fila["bot_id"]] = simple_returns(
                    np.array([float(p["equity"]) for p in puntos], dtype="float64")
                )
                nombres[fila["bot_id"]] = str(fila["name"])
        ids = sorted(curvas)
        if len(ids) < 2:
            return [], 0.0, 0.0
        valores: list[tuple[str, str, float]] = []
        for i, a in enumerate(ids):
            for b in ids[i + 1 :]:
                c = rolling_correlation(curvas[a], curvas[b])
                if math.isfinite(c):
                    valores.append((nombres[a], nombres[b], float(c)))
        if not valores:
            return [], 0.0, 0.0
        media = sum(abs(v[2]) for v in valores) / len(valores)
        maxima = max(v[2] for v in valores)
        valores.sort(key=lambda v: -v[2])
        return valores[:TOP_PAIRS], media, maxima

    # -- 5. los mejores ----------------------------------------------------- #

    def _mejores(self, generation: int) -> str:
        scope = self._scope(generation)
        filas = self._db.query(
            "SELECT b.bot_id, b.name, b.family, b.operator, b.born_generation, "
            "b.root_lineage, b.fitness_effective, b.total_trades, b.equity, "
            "m.sortino, m.max_drawdown, m.n_trades, m.fee_drag, m.novelty, "
            "m.corr_to_population, m.total_return "
            "FROM bots b LEFT JOIN bot_metrics m "
            "  ON m.bot_id = b.bot_id AND m.generation = ? AND m.scope = ? "
            "WHERE b.status = 'ALIVE' "
            "ORDER BY b.fitness_effective DESC NULLS LAST LIMIT ?",
            (generation, scope, TOP_BOTS),
        )
        if not filas:
            return "## 5. Los mejores\n\n_El jardín no tiene bots vivos._\n"

        bloques = [
            "## 5. Los mejores",
            "",
            _tabla(
                ["#", "Bot", "Familia", "Operador", "Edad", "Fitness", "Sortino",
                 "DD", "Ops", "Novedad"],
                [
                    [
                        str(i + 1), str(f["name"]), str(f["family"]), str(f["operator"]),
                        f"{generation - int(f['born_generation'])} gen",
                        _num(f["fitness_effective"], signo=True),
                        _num(f["sortino"], 2), _pct(f["max_drawdown"]),
                        str(f["n_trades"] if f["n_trades"] is not None else f["total_trades"]),
                        _num(f["novelty"], 2),
                    ]
                    for i, f in enumerate(filas)
                ],
            ),
            "### Genomas, en legible",
            "",
        ]
        for f in filas[:TOP_BOTS]:
            genoma = self.repos.bots.genome_of(f["bot_id"])
            padres = self.repos.lineage.parents_of(f["bot_id"])
            bloques += [
                f"**{f['name']}** (`{f['bot_id']}`) · linaje `{f['root_lineage']}` · "
                f"nacido en la generación {f['born_generation']} por {f['operator']}"
                + (f" de {', '.join(padres)}" if padres else " (fundador)"),
                "",
                "```",
                genoma.describe(),
                "```",
                "",
            ]
        return "\n".join(bloques)

    # -- 6. los muertos ----------------------------------------------------- #

    def _muertos(self, generation: int) -> str:
        filas = self._db.query(
            "SELECT name, family, operator, born_generation, death_cause, "
            "total_trades, fitness_effective FROM bots WHERE died_generation = ? "
            "ORDER BY name",
            (generation,),
        )
        causas = self._db.query(
            "SELECT death_cause, COUNT(*) AS n FROM bots WHERE died_generation = ? "
            "GROUP BY death_cause ORDER BY n DESC",
            (generation,),
        )
        resumen = (
            " · ".join(f"{r['death_cause']}: {r['n']}" for r in causas)
            if causas else "nadie ha muerto esta generación"
        )
        return "\n".join(
            [
                "## 6. Los muertos",
                "",
                resumen,
                "",
                _tabla(
                    ["Bot", "Familia", "Nació", "Vivió", "Ops", "Fitness", "Causa"],
                    [
                        [
                            str(f["name"]), str(f["family"]), str(f["born_generation"]),
                            f"{generation - int(f['born_generation'])} gen",
                            str(f["total_trades"]),
                            _num(f["fitness_effective"], signo=True),
                            str(f["death_cause"]),
                        ]
                        for f in filas
                    ],
                ),
            ]
        )

    # -- 7. qué ha funcionado ----------------------------------------------- #

    def _que_ha_funcionado(self, generation: int) -> str:
        tasas = self.repos.gardener.operator_success_rates(generation)
        por_operador = self._db.query(
            "SELECT operator, COUNT(*) AS nacidos, "
            "SUM(status IN ('ALIVE','RETIRED')) AS vivos, "
            "AVG(fitness_effective) AS fitness "
            "FROM bots WHERE born_generation <= ? GROUP BY operator ORDER BY operator",
            (generation,),
        )
        por_familia = self._db.query(
            "SELECT family, COUNT(*) AS nacidos, "
            "SUM(status IN ('ALIVE','RETIRED')) AS vivos, "
            "AVG(fitness_effective) AS fitness "
            "FROM bots WHERE born_generation <= ? GROUP BY family ORDER BY family",
            (generation,),
        )
        return "\n".join(
            [
                "## 7. Qué ha funcionado",
                "",
                "Supervivencia = fracción de hijos que aguantan 3 generaciones. "
                "Es la métrica que dice si la evolución descubre o sólo hace ruido.",
                "",
                "**Por operador**",
                "",
                _tabla(
                    ["Operador", "Nacidos", "Vivos", "Supervivencia 3 gen", "Fitness medio"],
                    [
                        [
                            str(r["operator"]), str(r["nacidos"]), str(r["vivos"] or 0),
                            _pct(tasas[str(r["operator"])], 0)
                            if str(r["operator"]) in tasas else "—",
                            _num(r["fitness"], signo=True),
                        ]
                        for r in por_operador
                    ],
                ),
                "**Por familia de ideas**",
                "",
                _tabla(
                    ["Familia", "Nacidos", "Vivos", "Fitness medio"],
                    [
                        [
                            str(r["family"]), str(r["nacidos"]), str(r["vivos"] or 0),
                            _num(r["fitness"], signo=True),
                        ]
                        for r in por_familia
                    ],
                ),
            ]
        )

    # -- 8. alertas --------------------------------------------------------- #

    def _alertas(self) -> str:
        abiertas = self.repos.events.open_alerts()
        if not abiertas:
            return "## 8. Alertas\n\nNinguna abierta.\n"
        return "\n".join(
            [
                "## 8. Alertas",
                "",
                _tabla(
                    ["Alerta", "Valor", "Umbral", "Desde", "Detalle"],
                    [
                        [
                            str(a["kind"]), _num(a["value"]), _num(a["threshold"]),
                            f"gen {a['raised_gen']}", str(a["detail"] or ""),
                        ]
                        for a in abiertas
                    ],
                ),
            ]
        )

    # -- 9. decisiones anteriores ------------------------------------------- #

    def _decisiones_anteriores(self, generation: int) -> str:
        filas = self._db.query(
            "SELECT d.*, s.opened_ts FROM gardener_decisions d "
            "JOIN gardener_sessions s ON s.session_id = d.session_id "
            "ORDER BY d.decision_id DESC LIMIT 20"
        )
        if not filas:
            return (
                "## 9. Decisiones anteriores\n\n"
                "_Primera sesión: no hay nada que revisar todavía._\n"
            )
        return "\n".join(
            [
                "## 9. Decisiones anteriores y su resultado",
                "",
                "Léete a ti mismo antes de proponer: aquí está lo que dijiste que "
                "iba a pasar y lo que pasó.",
                "",
                _tabla(
                    ["Gen", "Propuesta", "Objetivo", "Estado", "Esperabas", "Pasó", "Veredicto"],
                    [
                        [
                            str(f["generation"]), str(f["kind"]), str(f["target"] or "—"),
                            str(f["status"]),
                            str(f["expected_effect"])[:70],
                            str(f["observed_effect"] or "—")[:70],
                            str(f["verdict"] or "pendiente"),
                        ]
                        for f in filas
                    ],
                ),
            ]
        )

    def _como_responder(self) -> str:
        return "\n".join(
            [
                "## Cómo responder",
                "",
                "Escribe las propuestas en un JSON y aplícalas:",
                "",
                "```powershell",
                "keepgarden gardener apply --file propuestas.json --dry-run   # sólo valida",
                "keepgarden gardener apply --file propuestas.json --journal \"...\"",
                "```",
                "",
                "Cada propuesta necesita `rationale`, `expected_effect` y "
                "`review_in_generations`. Los límites están en "
                "docs/GARDENER_PROTOCOL.md §Límites: si uno se viola, esa propuesta "
                "se rechaza con el motivo y las demás siguen su curso.",
                "",
            ]
        )

    # -- validación --------------------------------------------------------- #

    def snapshot_for_validation(self, generation: int) -> Any:
        """Construye el ``GardenSnapshot`` que valida las propuestas."""
        from ..config import current_params
        from .proposals import GardenSnapshot

        generation = self._resolve(generation)
        vivos = self.repos.bots.alive()
        overrides = json.loads(self._db.get_meta("gardener_overrides") or "{}")
        params = current_params(self.cfg)
        params.update({k: float(v) for k, v in overrides.items()})
        return GardenSnapshot(
            generation=generation,
            alive_bot_ids=frozenset(str(f["bot_id"]) for f in vivos),
            lineages=frozenset(str(f["root_lineage"]) for f in vivos),
            population_size=len(vivos),
            current_params=params,
            bot_ages={
                str(f["bot_id"]): max(0, generation - int(f["born_generation"]))
                for f in vivos
            },
        )


__all__ = ("TOP_BOTS", "TOP_PAIRS", "ReportBuilder")
