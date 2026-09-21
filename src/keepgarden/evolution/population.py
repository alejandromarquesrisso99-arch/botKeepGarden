"""Orquestador de una generación: junta todas las piezas de la evolución.

Este módulo no inventa nada; llama a los demás en el orden correcto. Es el sitio
donde mirar para entender qué pasa cuando se cierra una generación.
"""

from __future__ import annotations

import random
import statistics
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Mapping, Sequence

from ..config import Config
from ..evaluation.fitness import (
    FitnessBreakdown,
    RobustScale,
    compute_fitness,
    pareto_front,
)
from ..evaluation.metrics import Metrics
from ..genome.catalog import SEEDABLE_FAMILIES, GeneCatalog
from ..genome.distance import mean_pairwise_distance, novelty
from ..genome.random_genome import random_genome
from ..genome.schema import Genome, MarketSpec
from ..genome.validate import GenomeInvalid
from ..ids import bot_id_of
from ..types import (
    AlertKind,
    BotId,
    BotStatus,
    BreedOperator,
    DeathCause,
    EventType,
    IdeaFamily,
)
from .crossover import crossover, pick_pair
from .fusion import fuse, select_fusion_candidates
from .lineage import ParentEdge, Pedigree
from .mutation import adaptive_rate_factor, mutate
from .selection import (
    blocked_families,
    plan_births,
    select_deaths,
    select_parents,
)
from .speciation import (
    Species,
    assign_quotas,
    detect_clones,
    family_shares,
    shared_fitness,
    speciate,
)

if TYPE_CHECKING:  # pragma: no cover
    from ..engine.incubator import Incubator
    from ..storage.db import Database
    from ..storage.repositories import Repositories

#: Cuántos intentos se hacen por cada nacimiento antes de rendirse con ese
#: operador. Un cruce puede salir irreparable y una mutación puede quedarse en
#: clon; insistir un poco es más barato que perder el hueco.
BREED_ATTEMPTS = 6


@dataclass(slots=True)
class GenerationOutcome:
    """Lo que pasó al cerrar una generación. Se persiste y se muestra."""

    generation: int
    births: list[BotId] = field(default_factory=list)
    deaths: dict[BotId, DeathCause] = field(default_factory=dict)
    fusions: list[BotId] = field(default_factory=list)
    discarded: int = 0
    n_species: int = 0
    genetic_diversity: float = 0.0
    fitness_best: float = 0.0
    fitness_median: float = 0.0
    alerts: list[str] = field(default_factory=list)

    @property
    def summary_line(self) -> str:
        return (
            f"gen {self.generation:>3}  "
            f"+{len(self.births):<3} -{len(self.deaths):<3} "
            f"fus {len(self.fusions):<2} desc {self.discarded:<4} "
            f"esp {self.n_species:<3} div {self.genetic_diversity:.3f}  "
            f"fit mediana {self.fitness_median:+.3f} mejor {self.fitness_best:+.3f}"
        )


@dataclass(slots=True)
class Population:
    """La población viva y las operaciones sobre ella."""

    cfg: Config
    catalog: GeneCatalog
    db: "Database"
    rng: random.Random
    repos: "Repositories | None" = None
    #: Escala congelada de la generación 0. Sin ella el fitness es relativo a
    #: los contemporáneos y su mediana vale 0 por construcción, así que no hay
    #: forma de saber si el jardín mejora o sólo se reordena.
    reference: dict[str, RobustScale] = field(default_factory=dict)
    #: Historia de la mediana de fitness, para la tasa de mutación adaptativa.
    fitness_history: list[float] = field(default_factory=list)
    _species: list[Species] = field(default_factory=list)
    #: Retornos de la última medición, por bot. Es lo que permite correlacionar
    #: candidatos a fusión cuando todavía no hay jardín vivo que haya dejado
    #: curvas de equity en la base.
    _returns: dict[BotId, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.repos is None:
            from ..storage.repositories import Repositories

            self.repos = Repositories.open(self.db)

    # -- consultas --------------------------------------------------------- #

    def alive_ids(self) -> list[BotId]:
        assert self.repos is not None
        return [row["bot_id"] for row in self.repos.bots.alive()]

    def alive_genomes(self) -> dict[BotId, Genome]:
        assert self.repos is not None
        return self.repos.bots.alive_genomes()

    # -- el ciclo ---------------------------------------------------------- #

    def evolve_generation(
        self,
        generation: int,
        incubator: "Incubator",
        *,
        window_metrics: Mapping[BotId, Metrics] | None = None,
    ) -> GenerationOutcome:
        """Los 10 pasos de docs/ARCHITECTURE.md §5, en orden:

        1. Cerrar la ventana y recoger trades y equity.
        2. ``evaluation.metrics.compute_metrics`` por bot.
        3. ``evaluation.fitness.compute_fitness`` + frente de Pareto.
        4. ``evolution.speciation.speciate``.
        5. ``shared_fitness`` y ``assign_quotas``.
        6. ``selection.select_parents``.
        7. Generar descendencia: ``mutate``, ``crossover``, ``fuse``, ``seed``
           según ``plan_births``.
        8. ``incubator.screen`` — sólo los que pasan nacen.
        9. ``selection.select_deaths`` y podar.
        10. Persistir generación, métricas, eventos y alertas.

        Todo lo aleatorio pasa por ``self.rng``: misma semilla, mismo jardín.

        ``window_metrics`` son las métricas de la ventana que se cierra. El
        jardín vivo las trae de sus propios trades (hito 5); la incubadora las
        calcula con walk-forward. Sin ellas se miden aquí contra la incubadora,
        que es lo que hace ``keepgarden incubate``.
        """
        assert self.repos is not None
        genomas = self.alive_genomes()
        outcome = GenerationOutcome(generation=generation)
        if not genomas:
            return outcome

        metricas = dict(window_metrics) if window_metrics else self._incubator_metrics(
            genomas, incubator
        )
        self._add_relational(genomas, metricas)

        edades = self._ages(generation)
        fitness = compute_fitness(
            metricas,
            self.cfg.fitness,
            ages=edades,
            complexities={b: g.complexity() for b, g in genomas.items()},
            reference=self.reference or None,
        )
        escalares = {b: f.total for b, f in fitness.items()}
        frente = pareto_front(
            {
                b: (metricas[b].sortino, metricas[b].ulcer_index, metricas[b].novelty)
                for b in genomas
                if fitness[b].is_defined
            },
            maximize=(True, False, True),
        )

        especies = speciate(genomas, self.cfg, self.catalog, previous=self._species)
        compartido = shared_fitness(escalares, especies)
        diversidad = mean_pairwise_distance(
            [genomas[b] for b in sorted(genomas)],
            self.cfg.speciation.distance_weights,
            self.catalog,
            members=genomas,
        )
        cuotas_familia = family_shares(genomas)

        # Los nacimientos de la generación se reparten primero entre especies
        # (quién se reproduce) y después entre operadores (cómo).
        nacimientos = self._birth_budget(len(genomas))
        assign_quotas(especies, nacimientos, escalares)
        plan = plan_births(
            {sp.species_id: sp.breeding_quota for sp in especies},
            self.cfg,
            diversity=diversidad,
            family_shares={str(k): v for k, v in cuotas_familia.items()},
            garden_drawdown=self._garden_drawdown(),
        )

        elite = self._elite(escalares)
        padres = select_parents(
            especies, compartido, {sp.species_id: sp.breeding_quota for sp in especies},
            self.cfg, self.rng, elite=elite,
        )

        candidatos, parentescos = self._breed(
            plan, padres, genomas, escalares, metricas, edades, cuotas_familia, generation
        )

        resultados = incubator.screen(
            candidatos,
            alive_genomes=list(genomas.values()),
            generation=generation,
            members=genomas,
            reference=self.reference or None,
        )
        aprobados = [r for r in resultados if r.passed]
        outcome.discarded = len(resultados) - len(aprobados)

        with self.db.transaction():
            outcome.births = self.admit(
                [(r.genome, parentescos.get(r.genome.id, ())) for r in aprobados],
                generation,
                fitness_by_genome={r.genome.id: r.fitness_incubator for r in aprobados},
            )
            outcome.fusions = [
                bot_id_of(r.genome.id) for r in aprobados if r.genome.is_ensemble
            ]

            muertes = select_deaths(
                escalares,
                edades,
                self._idle(generation),
                self._drawdowns(genomas),
                protected=set(elite) | self._gardener_protected(generation),
                pareto=frente,
                clones=detect_clones(genomas, self.cfg, self.catalog),
                cfg=self.cfg,
            )
            self.cull(muertes, generation)
            outcome.deaths = muertes

            self._persist(
                generation, genomas, metricas, fitness, especies, frente,
                diversidad, cuotas_familia, outcome,
            )

        definidos = [v for v in escalares.values() if v == v]
        outcome.n_species = len(especies)
        outcome.genetic_diversity = diversidad
        outcome.fitness_best = max(definidos) if definidos else 0.0
        outcome.fitness_median = (
            float(statistics.median(definidos)) if definidos else 0.0
        )
        outcome.alerts = self._check_alerts(generation, diversidad, cuotas_familia)
        self.fitness_history.append(outcome.fitness_median)
        self._species = especies
        return outcome

    # -- pasos sueltos ----------------------------------------------------- #

    def _birth_budget(self, poblacion: int) -> int:
        """Cuántos nacimientos caben esta generación.

        La cuota base de la configuración, recortada para no pasarse de
        ``max_population``: un jardín que crece sin techo acaba siendo lento y
        endogámico a la vez.
        """
        hueco = max(0, self.cfg.garden.max_population - poblacion)
        return min(self.cfg.garden.births_per_generation, hueco)

    def _garden_drawdown(self) -> float:
        assert self.repos is not None
        fila = self.repos.db.query_one(
            "SELECT garden_drawdown FROM garden_equity ORDER BY ts DESC LIMIT 1"
        )
        return float(fila["garden_drawdown"]) if fila else 0.0

    def _incubator_metrics(
        self, genomas: Mapping[BotId, Genome], incubator: "Incubator"
    ) -> dict[BotId, Metrics]:
        """Mide a los vivos con el walk-forward de la incubadora.

        Es lo que hace ``keepgarden incubate``: sin jardín vivo no hay ventana
        que cerrar, así que la evidencia sale del histórico. Se usa ``measure``
        y no ``screen`` porque a los que ya están vivos no hay que volver a
        juzgarlos con el listón de nacimiento, ni ensuciar ``incubation_runs``
        con una fila por bot y generación.
        """
        from ..engine.incubator import fold_metrics_to_metrics

        medidos = incubator.measure(list(genomas.values()), members=dict(genomas))
        self._returns = {
            bot_id_of(gid): run.valid_returns for gid, run in medidos.items()
        }
        return {
            bot_id_of(gid): fold_metrics_to_metrics(run.fold_metrics)
            for gid, run in medidos.items()
        }

    def _add_relational(
        self, genomas: Mapping[BotId, Genome], metricas: dict[BotId, Metrics]
    ) -> None:
        """Novedad y correlación con la población: sin ellas, el fitness premia
        a los bots que se parecen a todos los demás."""
        poblacion = [genomas[b] for b in sorted(genomas)]
        for bot, genoma in genomas.items():
            if bot in metricas:
                metricas[bot].novelty = novelty(
                    genoma, poblacion, self.cfg.speciation.distance_weights,
                    self.catalog, members=genomas,
                )

    def _elite(self, escalares: Mapping[BotId, float]) -> list[BotId]:
        definidos = [(b, f) for b, f in escalares.items() if f == f]
        definidos.sort(key=lambda par: (-par[1], par[0]))
        return [b for b, _ in definidos[: self.cfg.garden.elite_count]]

    def _ages(self, generation: int) -> dict[BotId, int]:
        assert self.repos is not None
        return {
            row["bot_id"]: max(0, generation - int(row["born_generation"]))
            for row in self.repos.bots.alive()
        }

    def _idle(self, generation: int) -> dict[BotId, int]:
        assert self.repos is not None
        return {
            row["bot_id"]: int(row["idle_generations"]) for row in self.repos.bots.alive()
        }

    def _drawdowns(self, genomas: Mapping[BotId, Genome]) -> dict[BotId, float]:
        assert self.repos is not None
        out: dict[BotId, float] = {}
        for row in self.repos.bots.alive():
            pico = float(row["peak_equity"]) or 1.0
            out[row["bot_id"]] = max(0.0, 1.0 - float(row["equity"]) / pico)
        return out

    def _gardener_protected(self, generation: int) -> set[BotId]:
        assert self.repos is not None
        return {
            row["bot_id"]
            for row in self.repos.bots.alive()
            if int(row["protected_until_gen"]) > generation
        }

    # -- reproducción ------------------------------------------------------ #

    def _breed(
        self,
        plan: Mapping[str, int],
        padres: Sequence[BotId],
        genomas: Mapping[BotId, Genome],
        escalares: Mapping[BotId, float],
        metricas: Mapping[BotId, Metrics],
        edades: Mapping[BotId, int],
        cuotas_familia: Mapping[IdeaFamily, float],
        generation: int,
    ) -> tuple[list[Genome], dict[str, tuple[ParentEdge, ...]]]:
        """Genera la descendencia del plan. Devuelve candidatos y parentescos."""
        assert self.repos is not None
        bloqueadas = blocked_families(
            {str(k): v for k, v in cuotas_familia.items()}, self.cfg
        )
        factor = adaptive_rate_factor(self.fitness_history, self.cfg)
        pedigri = Pedigree.from_edges(self.repos.lineage.all_edges())

        candidatos: list[Genome] = []
        parentescos: dict[str, tuple[ParentEdge, ...]] = {}

        def apunta(hijo: Genome, edges: Sequence[ParentEdge]) -> None:
            candidatos.append(hijo)
            parentescos[hijo.id] = tuple(edges)

        elegibles = [b for b in padres if genomas[b].family not in bloqueadas]

        for _ in range(int(plan.get(str(BreedOperator.MUTATE), 0))):
            if not elegibles:
                break
            padre = elegibles[self.rng.randrange(len(elegibles))]
            hijo = mutate(
                genomas[padre], self.cfg, self.catalog, self.rng, rate_factor=factor
            )
            apunta(
                hijo,
                [ParentEdge(padre, bot_id_of(hijo.id), BreedOperator.MUTATE)],
            )

        simples = [
            (genomas[b], escalares.get(b, 0.0))
            for b in elegibles
            if not genomas[b].is_ensemble and escalares.get(b, 0.0) == escalares.get(b, 0.0)
        ]
        for _ in range(int(plan.get(str(BreedOperator.CROSSOVER), 0))):
            for _ in range(BREED_ATTEMPTS):
                pareja = pick_pair(
                    simples, self.cfg, self.catalog, self.rng, pedigree=pedigri
                )
                if pareja is None:
                    break
                a, b = pareja
                hijo = crossover(a, b, self.cfg, self.catalog, self.rng)
                if hijo is None:
                    continue
                apunta(
                    hijo,
                    [
                        ParentEdge(
                            bot_id_of(a.id), bot_id_of(hijo.id), BreedOperator.CROSSOVER,
                            ordinal=0,
                        ),
                        ParentEdge(
                            bot_id_of(b.id), bot_id_of(hijo.id), BreedOperator.CROSSOVER,
                            ordinal=1,
                        ),
                    ],
                )
                break

        cupo_fusion = int(plan.get(str(BreedOperator.FUSION), 0))
        if cupo_fusion:
            grupos = select_fusion_candidates(
                list(genomas.values()),
                self._correlations(genomas),
                escalares,
                edades,
                self.cfg,
                self.rng,
            )
            for grupo in grupos[:cupo_fusion]:
                miembros = [genomas[b] for b in grupo if b in genomas]
                if len(miembros) < self.cfg.fusion.min_members:
                    continue
                try:
                    hijo = fuse(miembros, metricas, self.cfg, self.rng)
                except ValueError:
                    continue
                apunta(
                    hijo,
                    [
                        ParentEdge(
                            padre, bot_id_of(hijo.id), BreedOperator.FUSION,
                            weight=peso, ordinal=i,
                        )
                        for i, (padre, peso) in enumerate(
                            zip(grupo, hijo.ensemble.normalized_weights())  # type: ignore[union-attr]
                        )
                    ],
                )

        mercado = genomas[next(iter(sorted(genomas)))].market
        for _ in range(int(plan.get(str(BreedOperator.SEED), 0))):
            hijo = self._seed_one(mercado, bloqueadas)
            if hijo is not None:
                apunta(hijo, [])

        return candidatos, parentescos

    def _seed_one(
        self, market: MarketSpec, bloqueadas: set[IdeaFamily]
    ) -> Genome | None:
        disponibles = [f for f in SEEDABLE_FAMILIES if f not in bloqueadas]
        if not disponibles:
            disponibles = list(SEEDABLE_FAMILIES)
        for _ in range(BREED_ATTEMPTS):
            familia = disponibles[self.rng.randrange(len(disponibles))]
            try:
                return random_genome(familia, market, self.cfg, self.catalog, self.rng)
            except GenomeInvalid:
                continue
        return None

    def _correlations(
        self, genomas: Mapping[BotId, Genome]
    ) -> dict[tuple[BotId, BotId], float]:
        """Correlación entre curvas de equity de los vivos.

        La correlación es entre curvas y no entre genomas: dos genomas distintos
        pueden producir la misma curva, y fusionarlos no aporta nada.

        En el jardín vivo las curvas salen de la base. En la incubadora todavía
        no hay ninguna, así que se usan los retornos de las ventanas de
        validación de la última medición, que es la misma serie para todos y
        por tanto comparable.
        """
        assert self.repos is not None
        import numpy as np

        from ..evaluation.metrics import rolling_correlation, simple_returns

        curvas: dict[BotId, np.ndarray] = {}
        for bot in genomas:
            filas = self.repos.equity.curve(bot, max_points=2000)
            if len(filas) > 2:
                curvas[bot] = simple_returns(
                    np.array([float(f["equity"]) for f in filas], dtype="float64")
                )
            else:
                guardados = self._returns.get(bot)
                if guardados is not None and len(guardados) > 2:  # type: ignore[arg-type]
                    curvas[bot] = np.asarray(guardados, dtype="float64")
        ids = sorted(curvas)
        return {
            (a, b): rolling_correlation(curvas[a], curvas[b])
            for i, a in enumerate(ids)
            for b in ids[i + 1 :]
        }

    # -- alta y baja ------------------------------------------------------- #

    def admit(
        self,
        newborns: Sequence[tuple[Genome, Sequence[ParentEdge]]],
        generation: int,
        *,
        fitness_by_genome: Mapping[str, float] | None = None,
    ) -> list[BotId]:
        """Da de alta a los recién nacidos: bot, genoma, parentesco, cartera."""
        assert self.repos is not None
        fitness_by_genome = fitness_by_genome or {}
        nacidos: list[BotId] = []
        ahora = int(time.time() * 1000)
        for genoma, edges in newborns:
            genoma = genoma.with_meta(generation=generation)
            bot = self.repos.bots.create(
                genoma,
                generation=generation,
                initial_capital=self.cfg.garden.initial_capital_per_bot,
                parents=list(edges),
            )
            incubadora = fitness_by_genome.get(genoma.id)
            if incubadora is not None:
                self.repos.bots.set_fitness(
                    bot, incubator=incubadora, live=None, effective=incubadora
                )
            self.repos.events.log(
                EventType.BOT_BORN,
                f"nace {bot} por {genoma.meta.operator} ({genoma.family})",
                ts=ahora,
                generation=generation,
                bot_id=bot,
                payload={
                    "operator": str(genoma.meta.operator),
                    "family": str(genoma.family),
                    "parents": list(genoma.meta.parents),
                    "fitness_incubator": incubadora,
                },
            )
            nacidos.append(bot)
        return nacidos

    def cull(self, deaths: Mapping[BotId, DeathCause], generation: int) -> None:
        """Mata bots: cambia su estado y registra el evento.

        Un bot nunca se borra: cambia de estado y su historia completa
        permanece, que es lo que alimenta la genealogía. Las posiciones abiertas
        las cierra el motor del jardín vivo antes de llamar aquí; en la
        incubadora no hay ninguna que cerrar.
        """
        assert self.repos is not None
        ahora = int(time.time() * 1000)
        for bot, causa in deaths.items():
            self.repos.bots.set_status(
                bot, BotStatus.CULLED, generation=generation, cause=causa
            )
            self.repos.events.log(
                EventType.BOT_DIED,
                f"muere {bot} por {causa}",
                ts=ahora,
                generation=generation,
                bot_id=bot,
                severity="info",
                payload={"cause": str(causa)},
            )
        self._retire_orphan_ensembles(set(deaths), generation, ahora)

    def _retire_orphan_ensembles(
        self, muertos: set[BotId], generation: int, ahora: int
    ) -> None:
        """Un ensemble con menos de 2 miembros vivos se jubila.

        Redistribuir el peso de un miembro muerto entre los demás lo hace
        ``fusion.reweight_on_death``; aquí sólo se decide quién ya no puede
        seguir siendo un ensemble.
        """
        assert self.repos is not None
        from .fusion import reweight_on_death

        for bot, genoma in self.alive_genomes().items():
            if not genoma.is_ensemble or bot in muertos:
                continue
            actual = genoma
            for muerto in muertos & set(genoma.ensemble.members):  # type: ignore[union-attr]
                siguiente = reweight_on_death(actual, muerto)
                if siguiente is None:
                    self.repos.bots.set_status(
                        bot, BotStatus.RETIRED, generation=generation,
                        cause=DeathCause.ORPHAN_ENSEMBLE,
                    )
                    self.repos.events.log(
                        EventType.BOT_DIED,
                        f"se jubila la fusión {bot}: le quedan menos de 2 miembros",
                        ts=ahora, generation=generation, bot_id=bot,
                        payload={"cause": str(DeathCause.ORPHAN_ENSEMBLE)},
                    )
                    actual = None  # type: ignore[assignment]
                    break
                actual = siguiente
            if actual is not None and actual is not genoma:
                self.repos.bots.reweight_ensemble(bot, actual)
                self.repos.events.log(
                    EventType.FUSION_REWEIGHTED,
                    f"la fusión {bot} reparte el peso de sus miembros muertos",
                    ts=ahora, generation=generation, bot_id=bot,
                    payload={
                        "members": list(actual.ensemble.members),  # type: ignore[union-attr]
                        "weights": list(actual.ensemble.weights),  # type: ignore[union-attr]
                    },
                )

    # -- persistencia ------------------------------------------------------ #

    def _persist(
        self,
        generation: int,
        genomas: Mapping[BotId, Genome],
        metricas: Mapping[BotId, Metrics],
        fitness: Mapping[BotId, FitnessBreakdown],
        especies: Sequence[Species],
        frente: set[BotId],
        diversidad: float,
        cuotas_familia: Mapping[IdeaFamily, float],
        outcome: GenerationOutcome,
    ) -> None:
        assert self.repos is not None
        especie_de = {m: sp.species_id for sp in especies for m in sp.members}
        edades = self._ages(generation)
        elite = set(self._elite({b: f.total for b, f in fitness.items()}))

        orden = sorted(
            (b for b in genomas if fitness[b].is_defined),
            key=lambda b: -fitness[b].total,
        )
        rango = {b: i + 1 for i, b in enumerate(orden)}

        self.repos.generations.open(generation, 0)
        for bot, met in metricas.items():
            if bot not in genomas:
                continue
            fila = met.to_dict()
            fila["fitness"] = fitness[bot].total if fitness[bot].is_defined else None
            fila["fitness_rank"] = rango.get(bot)
            fila["on_pareto_front"] = int(bot in frente)
            self.repos.metrics.upsert(bot, generation, "incubator", fila)
            self.repos.bots.set_generation_stats(
                bot,
                generations_alive=edades.get(bot, 0),
                idle_generations=(0 if met.n_trades else self._idle(generation).get(bot, 0) + 1),
                total_trades=met.n_trades,
                species_id=especie_de.get(bot),
                is_elite=bot in elite,
                on_pareto_front=bot in frente,
            )

        self.repos.generations.record_species(generation, especies)

        definidos = [f.total for f in fitness.values() if f.is_defined]
        definidos.sort()

        def percentil(q: float) -> float | None:
            if not definidos:
                return None
            return definidos[min(len(definidos) - 1, int(len(definidos) * q))]

        self.repos.generations.close(
            generation,
            {
                "population_size": len(genomas),
                "births": len(outcome.births),
                "deaths": len(outcome.deaths),
                "fusions": len(outcome.fusions),
                "discarded": outcome.discarded,
                "n_species": len(especies),
                "oldest_bot_age": max(edades.values(), default=0),
                "fitness_best": definidos[-1] if definidos else None,
                "fitness_p75": percentil(0.75),
                "fitness_median": percentil(0.50),
                "fitness_p25": percentil(0.25),
                "fitness_worst": definidos[0] if definidos else None,
                "genetic_diversity": diversidad,
                "family_shares": {str(k): v for k, v in cuotas_familia.items()},
                "best_bot_id": orden[0] if orden else None,
            },
        )
        self.db.set_meta("current_generation", generation)

    def _check_alerts(
        self,
        generation: int,
        diversidad: float,
        cuotas_familia: Mapping[IdeaFamily, float],
    ) -> list[str]:
        """La diversidad cayendo es la alerta temprana más importante."""
        assert self.repos is not None
        ahora = int(time.time() * 1000)
        avisos: list[str] = []

        if diversidad < self.cfg.evolution.diversity_floor:
            self.repos.events.raise_alert(
                str(AlertKind.DIVERSITY_FLOOR), ts=ahora, generation=generation,
                value=diversidad, threshold=self.cfg.evolution.diversity_floor,
                detail="el jardín está convergiendo y va a dejar de descubrir",
            )
            avisos.append(f"diversidad {diversidad:.3f} bajo el suelo")
        else:
            self.repos.events.clear_alert(str(AlertKind.DIVERSITY_FLOOR), ahora)

        for familia, cuota in cuotas_familia.items():
            if cuota > self.cfg.evolution.max_family_share:
                self.repos.events.raise_alert(
                    str(AlertKind.FAMILY_QUOTA), ts=ahora, generation=generation,
                    value=cuota, threshold=self.cfg.evolution.max_family_share,
                    detail=f"{familia} ocupa el {cuota:.0%} de la población",
                )
                avisos.append(f"{familia} al {cuota:.0%}")
                break
        else:
            self.repos.events.clear_alert(str(AlertKind.FAMILY_QUOTA), ahora)

        return avisos


__all__ = ("BREED_ATTEMPTS", "GenerationOutcome", "Population")
