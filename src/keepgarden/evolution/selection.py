"""Selección y poda: quién se reproduce y quién muere."""

from __future__ import annotations

import math
import random
from typing import Mapping, Sequence

from ..config import Config
from ..types import BotId, BreedOperator, DeathCause, IdeaFamily
from .speciation import Species

#: Cuántas veces puede un mismo bot ser padre en una generación. Sin tope, el
#: jardín se llena de hijos de un solo bot en dos generaciones.
MAX_TIMES_AS_PARENT = 3

#: Operadores que reparten los nacimientos de una cosecha.
BREED_OPERATORS: tuple[BreedOperator, ...] = (
    BreedOperator.MUTATE,
    BreedOperator.CROSSOVER,
    BreedOperator.FUSION,
    BreedOperator.SEED,
)


def _defined(fitness: Mapping[BotId, float], bot: BotId) -> bool:
    valor = fitness.get(bot)
    return valor is not None and not math.isnan(valor)


def select_parents(
    species: Sequence[Species],
    fitness: Mapping[BotId, float],
    quotas: Mapping[str, int],
    cfg: Config,
    rng: random.Random,
    *,
    elite: Sequence[BotId] = (),
) -> list[BotId]:
    """Elige los padres de la siguiente cosecha.

    * **Elitismo**: los ``cfg.garden.elite_count`` mejores del jardín se
      reproducen seguro y están protegidos de la poda. Se pasan en ``elite``
      porque se calculan sobre el fitness **sin compartir**: ser el mejor del
      jardín no es lo mismo que ser el mejor de una especie pequeña.
    * **Torneo dentro de especie**: tamaño ``cfg.evolution.tournament_size``,
      gana el de mejor fitness compartido, que es el que se pasa en ``fitness``.
    * Un bot puede ser padre varias veces en la misma generación, pero no más de
      ``MAX_TIMES_AS_PARENT``.
    * Los bots con fitness indefinido (pocas operaciones) no son padres, pero
      tampoco mueren: se les da otra generación.
    """
    veces: dict[BotId, int] = {}
    padres: list[BotId] = []

    def apunta(bot: BotId) -> bool:
        if veces.get(bot, 0) >= MAX_TIMES_AS_PARENT:
            return False
        veces[bot] = veces.get(bot, 0) + 1
        padres.append(bot)
        return True

    for bot in elite:
        if _defined(fitness, bot):
            apunta(bot)

    tamaño = max(1, cfg.evolution.tournament_size)
    for sp in species:
        cupo = int(quotas.get(sp.species_id, 0))
        elegibles = [m for m in sp.members if _defined(fitness, m)]
        if not elegibles:
            continue
        for _ in range(cupo):
            disponibles = [
                m for m in elegibles if veces.get(m, 0) < MAX_TIMES_AS_PARENT
            ]
            if not disponibles:
                break
            torneo = [
                disponibles[rng.randrange(len(disponibles))]
                for _ in range(min(tamaño, len(disponibles)))
            ]
            apunta(max(torneo, key=lambda b: (fitness[b], b)))

    return padres


def select_deaths(
    fitness: Mapping[BotId, float],
    ages: Mapping[BotId, int],
    idle: Mapping[BotId, int],
    drawdowns: Mapping[BotId, float],
    protected: set[BotId],
    pareto: set[BotId],
    clones: Sequence[tuple[BotId, BotId]],
    cfg: Config,
) -> dict[BotId, DeathCause]:
    """Decide quién muere y por qué.

    Un bot muere si se cumple cualquiera (ver docs/EVOLUTION.md §Poda):

    * peor ``cull_fraction`` por fitness **y** edad >= ``min_age_generations``,
    * drawdown > ``risk.hard_max_drawdown`` (esto lo detecta antes el circuit
      breaker, pero se comprueba aquí también),
    * ``idle_generations >= idle_generations_limit``,
    * es clon de otro vivo con mejor fitness,
    * lo jubila el jardinero.

    Exentos: la élite, los bots del frente de Pareto, los protegidos por el
    jardinero —los tres llegan en ``protected`` y ``pareto``— y los que no
    llegan a ``min_age_generations``, salvo por drawdown, que no perdona a
    nadie.

    No deja la población por debajo de ``cfg.garden.min_population``: si la poda
    lo haría, se indulta empezando por los de mejor fitness entre los condenados.
    """
    poblacion = list(fitness)
    muertes: dict[BotId, DeathCause] = {}

    # El drawdown no perdona a nadie: ni élite, ni Pareto, ni recién nacidos.
    for bot in poblacion:
        if float(drawdowns.get(bot, 0.0)) > cfg.risk.hard_max_drawdown:
            muertes[bot] = DeathCause.DRAWDOWN_BREAKER

    exento = protected | pareto

    for bot in poblacion:
        if bot in muertes or bot in exento:
            continue
        if int(ages.get(bot, 0)) < cfg.garden.min_age_generations:
            continue
        if int(idle.get(bot, 0)) >= cfg.garden.idle_generations_limit:
            muertes[bot] = DeathCause.IDLE

    for a, b in clones:
        if a not in fitness or b not in fitness:
            continue
        peor, mejor = (a, b) if _peor(fitness, a, b) else (b, a)
        if peor in exento and mejor not in exento:
            peor = mejor
        if peor in muertes or peor in exento:
            continue
        if int(ages.get(peor, 0)) < cfg.garden.min_age_generations:
            continue
        muertes[peor] = DeathCause.CLONE

    juzgables = [
        b
        for b in poblacion
        if _defined(fitness, b)
        and b not in exento
        and int(ages.get(b, 0)) >= cfg.garden.min_age_generations
    ]
    cuantos = int(len(fitness) * cfg.garden.cull_fraction)
    if cuantos > 0 and juzgables:
        peores = sorted(juzgables, key=lambda b: (fitness[b], b))[:cuantos]
        for bot in peores:
            muertes.setdefault(bot, DeathCause.LOW_FITNESS)

    return _respect_floor(muertes, fitness, cfg)


def _peor(fitness: Mapping[BotId, float], a: BotId, b: BotId) -> bool:
    """¿Es ``a`` el peor del par? Un indefinido pierde contra uno definido."""
    fa, fb = fitness.get(a, float("nan")), fitness.get(b, float("nan"))
    if math.isnan(fa) and math.isnan(fb):
        return a > b
    if math.isnan(fa):
        return True
    if math.isnan(fb):
        return False
    return (fa, a) < (fb, b)


def _respect_floor(
    muertes: dict[BotId, DeathCause], fitness: Mapping[BotId, float], cfg: Config
) -> dict[BotId, DeathCause]:
    """Indulta por arriba hasta no bajar de ``garden.min_population``.

    El drawdown sigue sin perdonar: un bot que se ha desangrado no puede seguir
    vivo porque el censo quede corto. Para eso está la siembra.
    """
    supervivientes = len(fitness) - len(muertes)
    if supervivientes >= cfg.garden.min_population:
        return muertes

    indultables = sorted(
        (b for b, causa in muertes.items() if causa is not DeathCause.DRAWDOWN_BREAKER),
        key=lambda b: (-_orden(fitness, b), b),
    )
    faltan = cfg.garden.min_population - supervivientes
    for bot in indultables[:faltan]:
        muertes.pop(bot, None)
    return muertes


def _orden(fitness: Mapping[BotId, float], bot: BotId) -> float:
    valor = fitness.get(bot, float("nan"))
    return float("-inf") if math.isnan(valor) else float(valor)


def blocked_families(
    family_shares: Mapping[str, float], cfg: Config
) -> set[IdeaFamily]:
    """Familias que han pasado de su cuota y no pueden tener hijos esta vez."""
    tope = cfg.evolution.max_family_share
    out: set[IdeaFamily] = set()
    for familia, cuota in family_shares.items():
        if float(cuota) > tope:
            out.add(familia if isinstance(familia, IdeaFamily) else IdeaFamily(str(familia)))
    return out


def plan_births(
    quotas: Mapping[str, int],
    cfg: Config,
    *,
    diversity: float,
    family_shares: Mapping[str, float],
    garden_drawdown: float = 0.0,
) -> dict[str, int]:
    """Reparte los nacimientos entre operadores.

    Base: ``mutation_share``, ``crossover_share``, ``fusion_share``,
    ``seed_share``.

    Ajustes:

    * Si ``diversity < cfg.evolution.diversity_floor``, toda la cosecha pasa a
      ``SEED``: el jardín está convergiendo y hay que meter sangre nueva.
    * Si una familia supera ``max_family_share``, la mitad de lo que iba a
      mutación y cruce pasa a siembra. Mutar y cruzar reproduce las familias que
      ya están; sembrar es lo único que puede traer otra.
    * Si el drawdown del jardín supera ``risk.garden_max_drawdown``, se reducen
      los nacimientos a la mitad: cuando el ecosistema está sufriendo no es
      momento de poblarlo.
    """
    total = max(0, sum(int(v) for v in quotas.values()))
    if float(garden_drawdown) > cfg.risk.garden_max_drawdown:
        total //= 2
    if total == 0:
        return {str(op): 0 for op in BREED_OPERATORS}

    e = cfg.evolution
    if float(diversity) < e.diversity_floor:
        return {
            str(BreedOperator.SEED): total,
            **{str(op): 0 for op in BREED_OPERATORS if op is not BreedOperator.SEED},
        }

    cuotas = {
        BreedOperator.MUTATE: e.mutation_share,
        BreedOperator.CROSSOVER: e.crossover_share,
        BreedOperator.FUSION: e.fusion_share,
        BreedOperator.SEED: e.seed_share,
    }
    if blocked_families(family_shares, cfg):
        cedido = (cuotas[BreedOperator.MUTATE] + cuotas[BreedOperator.CROSSOVER]) / 2.0
        cuotas[BreedOperator.MUTATE] /= 2.0
        cuotas[BreedOperator.CROSSOVER] /= 2.0
        cuotas[BreedOperator.SEED] += cedido

    suma = sum(cuotas.values()) or 1.0
    exacto = {op: total * peso / suma for op, peso in cuotas.items()}
    plan = {op: int(valor) for op, valor in exacto.items()}

    restantes = total - sum(plan.values())
    for op in sorted(BREED_OPERATORS, key=lambda o: (-(exacto[o] % 1.0), str(o))):
        if restantes <= 0:
            break
        plan[op] += 1
        restantes -= 1

    return {str(op): plan[op] for op in BREED_OPERATORS}


__all__ = (
    "BREED_OPERATORS",
    "MAX_TIMES_AS_PARENT",
    "blocked_families",
    "select_parents",
    "select_deaths",
    "plan_births",
)
