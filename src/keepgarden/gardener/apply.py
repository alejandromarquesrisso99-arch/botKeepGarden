"""Aplicación de las propuestas del jardinero.

Valida contra los límites, aplica lo que pase, registra todo —lo aplicado y lo
rechazado, con su motivo— y programa la revisión del efecto.

Dos cosas que no son detalles:

* Lo que el jardinero cría entra por la puerta de todos: un injerto, una
  siembra o una fusión suyas pasan por la incubadora como cualquier candidato.
  Si no pasan el filtro, no nacen.
* Un ``TUNE`` no toca ``config/garden.yaml``. Los ajustes del jardinero viven
  en ``garden_meta`` y son reversibles.
"""

from __future__ import annotations

import json
import random
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from ..config import Config, apply_overrides
from ..genome.schema import FeatureGene, Genome, LogicNode, MarketSpec, rule_from_dict
from ..ids import bot_id_of
from ..storage.repositories import Repositories
from ..types import (
    BotStatus,
    BreedOperator,
    DeathCause,
    EventType,
    IdeaFamily,
    LogicOp,
    ProposalKind,
    ProposalStatus,
)
from .proposals import Proposal, ProposalError, validate_proposal, validate_session

#: Clave de ``garden_meta`` donde viven los ajustes efectivos del jardinero.
OVERRIDES_KEY = "gardener_overrides"

#: Clave de ``garden_meta`` con los pesos de siembra por familia.
FAMILY_WEIGHTS_KEY = "gardener_family_weights"

#: Cambio mínimo para que una revisión diga que algo pasó. Por debajo es ruido.
REVIEW_EPSILON = 0.05


@dataclass(slots=True)
class ApplyResult:
    applied: list[Proposal] = field(default_factory=list)
    rejected: list[tuple[Proposal, str]] = field(default_factory=list)
    created_bots: list[str] = field(default_factory=list)
    session_id: int = 0


@dataclass(slots=True)
class ProposalApplier:
    cfg: Config
    repos: Repositories

    #: Velas con las que criba la incubadora. Si no se pasan, se cargan de la
    #: caché: el jardinero no puede crear bots sin someterlos al mismo filtro.
    candles: Any = None
    report_path: str = ""

    @property
    def _db(self):  # type: ignore[no-untyped-def]
        return self.repos.db

    # -- sesión ------------------------------------------------------------- #

    def apply(self, proposals: Sequence[Proposal], generation: int) -> ApplyResult:
        """Valida y aplica una sesión completa.

        Una propuesta que viola un límite se rechaza con el motivo y **las
        demás siguen su curso**: un error del jardinero no invalida la sesión
        entera. Los límites acumulativos (cuántas jubilaciones, cuántas
        fusiones) sí tumban la sesión: son precisamente el freno contra una
        sesión entera mal calibrada.
        """
        resultado = ApplyResult()
        validas, invalidas = self.check(proposals, generation)

        resultado.session_id = self.repos.gardener.open_session(
            generation, trigger="manual", report_path=self.report_path
        )
        ahora = int(time.time() * 1000)

        for propuesta, motivo in invalidas:
            propuesta.status = ProposalStatus.REJECTED
            propuesta.rejection_reason = motivo
            resultado.rejected.append((propuesta, motivo))

        for propuesta in validas:
            try:
                nacidos = self._apply_one(propuesta, generation)
            except (ProposalError, ValueError) as exc:
                propuesta.status = ProposalStatus.REJECTED
                propuesta.rejection_reason = str(exc)
                resultado.rejected.append((propuesta, str(exc)))
            else:
                propuesta.status = ProposalStatus.APPLIED
                resultado.applied.append(propuesta)
                resultado.created_bots.extend(nacidos)

        for propuesta in (*resultado.applied, *(p for p, _ in resultado.rejected)):
            self.repos.gardener.record_decision(resultado.session_id, propuesta)
            self.repos.events.log(
                EventType.GARDENER_PROPOSAL,
                f"{propuesta.kind} {propuesta.status}"
                + (f": {propuesta.rejection_reason}" if propuesta.rejection_reason else ""),
                ts=ahora,
                generation=generation,
                severity="info" if propuesta.status is ProposalStatus.APPLIED else "warn",
                payload=propuesta.to_dict(),
            )

        self.repos.events.log(
            EventType.GARDENER_SESSION,
            f"sesión de jardinero en la generación {generation}: "
            f"{len(resultado.applied)} aplicadas, {len(resultado.rejected)} rechazadas",
            ts=ahora,
            generation=generation,
            payload={
                "session_id": resultado.session_id,
                "created_bots": resultado.created_bots,
            },
        )
        return resultado

    def check(
        self, proposals: Sequence[Proposal], generation: int
    ) -> tuple[list[Proposal], list[tuple[Proposal, str]]]:
        """Separa lo que se puede aplicar de lo que no, sin aplicar nada.

        Los dos tipos de límite se comportan distinto a propósito:

        * Un límite **individual** (un parámetro prohibido, un bot que no está
          vivo) rechaza esa propuesta y deja pasar las demás. Una errata no
          puede tirar por tierra una sesión entera de diagnóstico.
        * Un límite **acumulativo** (jubilar a más del 20 % de la población,
          demasiadas fusiones) tumba la sesión completa: es exactamente el
          freno contra una sesión mal calibrada, y aplicar media sesión sería
          peor que no aplicar ninguna.
        """
        from .report import ReportBuilder

        snapshot = ReportBuilder(cfg=self.cfg, repos=self.repos).snapshot_for_validation(
            generation
        )
        limites = self._limits()

        validas: list[Proposal] = []
        invalidas: list[tuple[Proposal, str]] = []
        for propuesta in proposals:
            try:
                validate_proposal(propuesta, snapshot, limites)
            except ProposalError as exc:
                invalidas.append((propuesta, str(exc)))
            else:
                validas.append(propuesta)

        validate_session(validas, snapshot, limites)
        return validas, invalidas

    def _limits(self):  # type: ignore[no-untyped-def]
        from .proposals import GardenerLimits

        return GardenerLimits(
            max_retire_share=self.cfg.gardener.max_retire_share,
            max_tune_delta=self.cfg.gardener.max_tune_delta,
        )

    def _apply_one(self, propuesta: Proposal, generation: int) -> list[str]:
        """Aplica una propuesta ya validada. Devuelve los bots que haya creado."""
        aplicar = {
            ProposalKind.GRAFT: self._graft,
            ProposalKind.SEED_FAMILY: self._seed_family,
            ProposalKind.FUSE: self._fuse,
            ProposalKind.RETIRE: self._retire,
            ProposalKind.PROTECT: self._protect,
            ProposalKind.TUNE: self._tune,
            ProposalKind.ADD_GENE: self._add_gene,
            ProposalKind.REBALANCE_QUOTAS: self._rebalance,
            ProposalKind.NOTE: lambda _p, _g: [],
        }[propuesta.kind]
        return aplicar(propuesta, generation)

    # -- cría dirigida ------------------------------------------------------ #

    def _market(self) -> MarketSpec:
        return MarketSpec(
            venue=self.cfg.market.venue,
            symbol=self._db.get_meta("symbol") or self.cfg.primary_symbol,
            timeframe=self._db.get_meta("timeframe") or self.cfg.market.timeframe,
        )

    def _incubate(
        self,
        candidatos: Sequence[tuple[Genome, Sequence[Any]]],
        generation: int,
        operador: BreedOperator,
    ) -> list[str]:
        """La puerta común: lo que el jardinero cría pasa la misma criba.

        Devuelve los bots que han nacido. Los que no pasan quedan registrados
        en ``incubation_runs`` como cualquier otro descarte.
        """
        from ..engine.incubator import Incubator
        from ..evolution.population import Population

        if not candidatos:
            return []
        velas = self._candles()
        incubadora = Incubator(
            cfg=self.cfg, candles=velas, repo=self.repos.incubation
        )
        vivos = self.repos.bots.alive_genomes()
        genomas = [g.with_meta(operator=operador, generation=generation) for g, _ in candidatos]
        resultados = incubadora.screen(
            genomas, alive_genomes=list(vivos.values()),
            generation=generation, members=vivos,
        )
        aprobados = {r.genome.id: r for r in resultados if r.passed}
        if not aprobados:
            return []

        poblacion = Population(
            cfg=self.cfg, catalog=self._catalog(), db=self._db,
            rng=random.Random(self.cfg.seed + generation), repos=self.repos,
        )
        edges = {g.id: list(e) for g, e in candidatos}
        return poblacion.admit(
            [(r.genome, edges.get(r.genome.id, ())) for r in aprobados.values()],
            generation,
            fitness_by_genome={gid: r.fitness_incubator for gid, r in aprobados.items()},
        )

    def _candles(self):  # type: ignore[no-untyped-def]
        if self.candles is not None:
            return self.candles
        from ..data.store import CandleStore, SeriesKey

        market = self._market()
        store = CandleStore(cache_dir=self.cfg.path(self.cfg.storage.cache_dir))
        velas = store.load(SeriesKey(market.venue, market.symbol, market.timeframe))
        if velas.empty:
            raise ProposalError(
                "no hay velas en caché: sin ellas la incubadora no puede cribar "
                "lo que propones, y nada puede nacer sin pasar por ella"
            )
        self.candles = velas
        return velas

    def _catalog(self):  # type: ignore[no-untyped-def]
        from ..genome.catalog import load_catalog

        return load_catalog(self.cfg.path("config/genes.yaml"))

    def _graft(self, propuesta: Proposal, generation: int) -> list[str]:
        """Injerta un bloque en un bot o en un linaje y cría a los hijos."""
        from ..evolution.lineage import ParentEdge
        from ..genome.validate import GenomeInvalid, validate_genome

        payload = propuesta.payload
        objetivo = str(payload["into"])
        padres = self._targets(objetivo)
        if not padres:
            raise ProposalError(f"GRAFT: '{objetivo}' no tiene bots vivos a los que injertar")

        n_hijos = int(payload.get("n_children", 1))
        candidatos: list[tuple[Genome, list[ParentEdge]]] = []
        for i in range(n_hijos):
            padre_id = padres[i % len(padres)]
            padre = self.repos.bots.genome_of(padre_id)
            hijo = self._graft_into(padre, payload)
            try:
                validate_genome(hijo, self.cfg, self._catalog())
            except GenomeInvalid as exc:
                raise ProposalError(
                    f"GRAFT: el injerto produce un genoma inválido: {exc}"
                ) from exc
            candidatos.append(
                (
                    hijo,
                    [ParentEdge(padre_id, bot_id_of(hijo.id), BreedOperator.GRAFT, 1.0, 0)],
                )
            )
        return self._incubate(candidatos, generation, BreedOperator.GRAFT)

    def _targets(self, objetivo: str) -> list[str]:
        vivos = self.repos.bots.alive()
        directos = [f["bot_id"] for f in vivos if f["bot_id"] == objetivo]
        if directos:
            return directos
        return [f["bot_id"] for f in vivos if f["root_lineage"] == objetivo]

    def _graft_into(self, padre: Genome, payload: dict[str, Any]) -> Genome:
        """Construye el hijo injertando el bloque pedido.

        El jardinero describe el bloque con el mismo esquema que el genoma, así
        que lo que escribe se valida exactamente igual que lo que produce el
        motor. No puede escribir un genoma entero: sólo una pieza.
        """
        from dataclasses import replace as _replace

        from ..ids import new_bot_id, new_genome_id

        bloque = str(payload["block"])
        nuevo_id = new_genome_id(new_bot_id(f"graft:{padre.id}:{json.dumps(payload, sort_keys=True)}"))
        hijo = _replace(padre, id=nuevo_id)

        if bloque == "risk":
            datos = dict(payload.get("risk") or {})
            if not datos:
                raise ProposalError("GRAFT de 'risk' necesita la clave 'risk' con los campos")
            base = padre.risk.to_dict()
            base.update(datos)
            hijo = _replace(hijo, risk=type(padre.risk).from_dict(base))
        else:
            features = list(hijo.features)
            if payload.get("feature"):
                features.append(FeatureGene.from_dict(dict(payload["feature"])))
                hijo = _replace(hijo, features=tuple(features))
            if bloque == "feature" and not payload.get("rule"):
                raise ProposalError(
                    "GRAFT de 'feature' necesita también 'rule': un indicador que "
                    "ninguna regla mira no cambia nada"
                )
            if payload.get("rule"):
                destino = "entry_long" if bloque == "feature" else bloque
                if destino == "regime":
                    raise ProposalError(
                        "GRAFT sobre 'regime' todavía no está soportado: "
                        "propón el filtro como condición de entrada"
                    )
                regla = rule_from_dict(dict(payload["rule"]))
                actual = getattr(hijo, destino, None)
                combinada = (
                    regla if actual is None else LogicNode(LogicOp.AND, (actual, regla))
                )
                hijo = _replace(hijo, **{destino: combinada})

        return hijo.with_meta(
            operator=BreedOperator.GRAFT,
            parents=(padre.id,),
            root_lineage=padre.meta.root_lineage,
        )

    def _seed_family(self, propuesta: Proposal, generation: int) -> list[str]:
        from ..genome.random_genome import random_genome
        from ..genome.validate import GenomeInvalid

        familia = IdeaFamily(str(propuesta.payload["family"]))
        cuantos = int(propuesta.payload["count"])
        rng = random.Random(self.cfg.seed + generation * 1000 + cuantos)
        catalogo = self._catalog()
        mercado = self._market()

        candidatos = []
        for _ in range(cuantos):
            for _intento in range(6):
                try:
                    candidatos.append(
                        (random_genome(familia, mercado, self.cfg, catalogo, rng), ())
                    )
                    break
                except GenomeInvalid:
                    continue
        return self._incubate(candidatos, generation, BreedOperator.SEED)

    def _fuse(self, propuesta: Proposal, generation: int) -> list[str]:
        from ..evolution.fusion import fuse
        from ..evolution.lineage import ParentEdge
        from ..types import CombineMode

        miembros = [str(m) for m in propuesta.payload["members"]]
        genomas = [self.repos.bots.genome_of(m) for m in miembros]
        metricas = self._last_metrics(miembros)
        hijo = fuse(genomas, metricas, self.cfg, random.Random(self.cfg.seed + generation))

        combine = propuesta.payload.get("combine")
        pesos = propuesta.payload.get("weights")
        umbral = propuesta.payload.get("threshold")
        if hijo.ensemble is not None and (combine or pesos or umbral is not None):
            from dataclasses import replace as _replace

            ensemble = hijo.ensemble
            if combine:
                ensemble = _replace(ensemble, combine=CombineMode(str(combine)))
            if pesos:
                ensemble = _replace(ensemble, weights=tuple(float(w) for w in pesos))
            if umbral is not None:
                ensemble = _replace(ensemble, threshold=float(umbral))
            hijo = _replace(hijo, ensemble=ensemble)

        edges = [
            ParentEdge(m, bot_id_of(hijo.id), BreedOperator.FUSION, 1.0, i)
            for i, m in enumerate(miembros)
        ]
        return self._incubate([(hijo, edges)], generation, BreedOperator.FUSION)

    def _last_metrics(self, bots: Sequence[str]) -> dict[str, Any]:
        """Las últimas métricas conocidas de unos bots, para pesar la fusión."""
        from ..evaluation.metrics import Metrics

        salida: dict[str, Metrics] = {}
        for bot in bots:
            fila = self._db.query_one(
                "SELECT * FROM bot_metrics WHERE bot_id = ? "
                "ORDER BY generation DESC LIMIT 1",
                (bot,),
            )
            m = Metrics()
            if fila is not None:
                for campo in ("sortino", "calmar", "max_drawdown", "n_trades"):
                    if fila[campo] is not None:
                        setattr(m, campo, fila[campo])
            salida[bot] = m
        return salida

    # -- poda y protección --------------------------------------------------- #

    def _retire(self, propuesta: Proposal, generation: int) -> list[str]:
        bots = [str(b) for b in propuesta.payload.get("bots", [])]
        linaje = propuesta.payload.get("lineage")
        if linaje:
            bots += [
                f["bot_id"] for f in self.repos.bots.alive()
                if f["root_lineage"] == str(linaje)
            ]
        ahora = int(time.time() * 1000)
        for bot in dict.fromkeys(bots):
            self._close_open_positions(bot, generation)
            self.repos.bots.set_status(
                bot, BotStatus.RETIRED, generation=generation, cause=DeathCause.GARDENER
            )
            self.repos.events.log(
                EventType.BOT_DIED,
                f"{bot} jubilado por el jardinero: {propuesta.payload.get('reason', '')}",
                ts=ahora, generation=generation, bot_id=bot,
                payload={"cause": str(DeathCause.GARDENER)},
            )
        return []

    def _close_open_positions(self, bot_id: str, generation: int) -> None:
        """Un jubilado no se queda con posiciones abiertas colgando.

        Se cierran al último cierre conocido y con su comisión, que es lo que
        habría pasado si el motor lo hubiera podado.
        """
        abiertas = self.repos.trades.open_positions(bot_id)
        if not abiertas:
            return
        velas = self._candles()
        precio = float(velas["close"].iloc[-1])
        comision = self.cfg.frictions.taker_fee_bps / 10_000.0
        fila = self.repos.bots.get(bot_id)
        caja = float(fila["cash"]) if fila else 0.0
        for t in abiertas:
            cantidad = float(t["amount"])
            direccion = 1.0 if str(t["side"]) == "LONG" else -1.0
            bruto = (precio - float(t["open_price"])) * cantidad * direccion
            fee = precio * cantidad * comision
            caja += (precio * cantidad * direccion) - fee
            self.repos.trades.close_trade(
                int(t["trade_id"]), close_ts=int(time.time() * 1000), close_price=precio,
                exit_kind="EXIT_FORCED", holding_bars=None, pnl_gross=bruto,
                pnl_net=bruto - fee, fees=fee,
                return_pct=(bruto - fee) / max(1e-9, float(t["open_price"]) * cantidad),
                r_multiple=None,
            )
        if fila is not None:
            self.repos.bots.update_equity(bot_id, caja, caja, float(fila["peak_equity"]))

    def _protect(self, propuesta: Proposal, generation: int) -> list[str]:
        hasta = generation + int(propuesta.payload["generations"])
        ahora = int(time.time() * 1000)
        for bot in [str(b) for b in propuesta.payload["bots"]]:
            self.repos.bots.protect(bot, hasta)
            self.repos.events.log(
                EventType.BOT_PROTECTED,
                f"{bot} protegido de la poda hasta la generación {hasta}",
                ts=ahora, generation=generation, bot_id=bot,
                payload={"until": hasta, "rationale": propuesta.rationale},
            )
        return []

    # -- ajustes ------------------------------------------------------------- #

    def overrides(self) -> dict[str, float]:
        """Ajustes del jardinero vigentes."""
        return json.loads(self._db.get_meta(OVERRIDES_KEY) or "{}")

    def _tune(self, propuesta: Proposal, generation: int) -> list[str]:
        ajustes = self.overrides()
        ajustes[str(propuesta.payload["param"])] = float(propuesta.payload["value"])
        self._db.set_meta(OVERRIDES_KEY, json.dumps(ajustes, sort_keys=True))
        return []

    def _rebalance(self, propuesta: Proposal, generation: int) -> list[str]:
        payload = propuesta.payload
        if payload.get("max_family_share") is not None:
            ajustes = self.overrides()
            ajustes["evolution.max_family_share"] = float(payload["max_family_share"])
            self._db.set_meta(OVERRIDES_KEY, json.dumps(ajustes, sort_keys=True))
        if payload.get("family_weights"):
            pesos = {
                str(IdeaFamily(str(k))): float(v)
                for k, v in dict(payload["family_weights"]).items()
            }
            self._db.set_meta(FAMILY_WEIGHTS_KEY, json.dumps(pesos, sort_keys=True))
        return []

    def _add_gene(self, propuesta: Proposal, generation: int) -> list[str]:
        """Un gen nuevo requiere código: esto deja la tarea escrita, no lo aplica."""
        destino = self.cfg.path(self.cfg.gardener.report_dir)
        destino.mkdir(parents=True, exist_ok=True)
        tareas = destino / "tareas_ingenieria.md"
        with tareas.open("a", encoding="utf-8") as f:
            f.write(
                f"\n## Generación {generation} · ADD_GENE "
                f"`{propuesta.payload['name']}`\n\n"
                f"- Categoría: {propuesta.payload['category']}\n"
                f"- Descripción: {propuesta.payload['description']}\n"
                f"- Fórmula: {propuesta.payload.get('formula', '—')}\n"
                f"- Parámetros: {propuesta.payload.get('params', '—')}\n"
                f"- Por qué: {propuesta.rationale}\n"
                f"- Qué se espera: {propuesta.expected_effect}\n"
            )
        self.repos.events.log(
            EventType.GARDENER_PROPOSAL,
            f"ADD_GENE '{propuesta.payload['name']}' requiere código: tarea en {tareas.name}",
            ts=int(time.time() * 1000), generation=generation, severity="warn",
            payload=propuesta.to_dict(),
        )
        return []

    # -- revisión ------------------------------------------------------------ #

    def review_due(self, generation: int) -> int:
        """Revisa las decisiones cuya generación de revisión ha llegado.

        Mide el efecto real y lo compara con el esperado. Como
        ``expected_effect`` es prosa —y tiene que serlo, es el razonamiento del
        jardinero—, lo que se mide es el efecto que ese tipo de propuesta debe
        producir: diversidad para una siembra, fitness mediana para un ajuste,
        supervivencia para una protección. El veredicto es una señal, no un
        juicio final; el jardinero lee el número y saca sus conclusiones.
        """
        pendientes = self.repos.gardener.pending_reviews(generation)
        for decision in pendientes:
            observado, veredicto = self._measure(decision, generation)
            self.repos.gardener.record_review(
                int(decision["decision_id"]), generation=generation,
                observed=observado, verdict=veredicto,
            )
        return len(pendientes)

    def _measure(self, decision: Any, generation: int) -> tuple[str, str]:
        antes = self.repos.generations.get(int(decision["generation"]))
        ahora = self.repos.generations.get(generation)
        kind = ProposalKind(str(decision["kind"]))

        if kind in (ProposalKind.NOTE, ProposalKind.ADD_GENE):
            return ("Sin efecto mecánico: no cambia el jardín por sí sola.", "no_effect")

        if kind is ProposalKind.PROTECT:
            objetivos = json.loads(decision["payload"] or "{}").get("bots", [])
            vivos = [
                b for b in objetivos
                if (fila := self.repos.bots.get(str(b))) is not None
                and str(fila["status"]) == "ALIVE"
            ]
            if not objetivos:
                return ("La propuesta no nombraba bots.", "no_effect")
            texto = f"{len(vivos)} de {len(objetivos)} protegidos siguen vivos."
            return (texto, "worked" if len(vivos) == len(objetivos) else "backfired")

        if antes is None or ahora is None:
            return ("No hay generaciones que comparar.", "no_effect")

        campo = (
            "genetic_diversity"
            if kind in (ProposalKind.SEED_FAMILY, ProposalKind.GRAFT, ProposalKind.FUSE,
                        ProposalKind.REBALANCE_QUOTAS)
            else "fitness_median"
        )
        viejo, nuevo = antes[campo], ahora[campo]
        if viejo is None or nuevo is None:
            return (f"{campo} sin valor en una de las dos generaciones.", "no_effect")

        delta = float(nuevo) - float(viejo)
        nombre = "diversidad" if campo == "genetic_diversity" else "fitness mediana"
        texto = (
            f"{nombre}: {float(viejo):+.3f} → {float(nuevo):+.3f} ({delta:+.3f}) "
            f"entre las generaciones {decision['generation']} y {generation}."
        )
        if abs(delta) < REVIEW_EPSILON:
            return (texto, "no_effect")
        return (texto, "worked" if delta > 0 else "backfired")


def effective_config(cfg: Config, db: Any) -> Config:
    """La config con los ajustes del jardinero aplicados.

    La usan el jardín vivo y la incubadora al arrancar: si el jardinero subió
    la tasa de mutación, el motor tiene que correr con ella sin que nadie haya
    tocado el YAML.
    """
    try:
        ajustes = json.loads(db.get_meta(OVERRIDES_KEY) or "{}")
    except (ValueError, TypeError):
        return cfg
    return apply_overrides(cfg, ajustes)


__all__ = (
    "FAMILY_WEIGHTS_KEY",
    "OVERRIDES_KEY",
    "REVIEW_EPSILON",
    "ApplyResult",
    "ProposalApplier",
    "effective_config",
)
