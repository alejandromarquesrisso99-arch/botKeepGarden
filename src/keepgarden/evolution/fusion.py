"""Fusión: el operador que define este proyecto.

Dos estrategias mediocres pero descorrelacionadas, combinadas, producen una
curva con menos drawdown que cualquiera de las dos. Es la única comida gratis
que hay en esto, y el sistema la busca de forma explícita.

Reglas importantes (docs/EVOLUTION.md §FUSION):

* Los padres **siguen vivos**. La fusión no los consume, así se puede medir
  directamente si aporta: el hijo y sus padres corren en paralelo sobre los
  mismos datos.
* Un ensemble puede fusionarse otra vez, hasta ``fusion.max_depth`` niveles.
* Si un padre muere, el ensemble redistribuye su peso. Con menos de 2 miembros
  vivos, se jubila.
"""

from __future__ import annotations

import dataclasses
import random
import statistics
from typing import Mapping, Sequence

from ..config import Config
from ..genome.schema import EnsembleGene, Genome, GenomeMeta
from ..ids import bot_id_of, new_bot_id, new_genome_id
from ..types import BotId, BreedOperator, CombineMode, IdeaFamily

#: Peso mínimo de un miembro, para que un Sortino negativo no produzca pesos
#: negativos ni deje a un padre con voz cero. Un miembro que no aporta debe
#: pesar poco, no votar al revés.
MIN_WEIGHT = 1e-3


def _corr(correlations: Mapping[tuple[BotId, BotId], float], a: BotId, b: BotId) -> float:
    """Correlación entre dos curvas, mirando el par en los dos órdenes.

    Cuando no se conoce se devuelve 1.0 —máxima— y no 0: fusionar a ciegas dos
    bots que podrían ser el mismo es justo lo que la fusión intenta evitar.
    """
    if a == b:
        return 1.0
    valor = correlations.get((a, b))
    if valor is None:
        valor = correlations.get((b, a))
    return 1.0 if valor is None else float(valor)


def _depth_of(genome: Genome) -> int:
    return genome.ensemble.depth if genome.ensemble is not None else 0


def select_fusion_candidates(
    alive: Sequence[Genome],
    correlations: Mapping[tuple[BotId, BotId], float],
    fitness: Mapping[BotId, float],
    ages: Mapping[BotId, int],
    cfg: Config,
    rng: random.Random,
) -> list[tuple[BotId, ...]]:
    """Encuentra grupos de padres que merezcan fusionarse.

    Criterios:

    * Todos vivos, con al menos ``fusion.min_parent_generations`` generaciones
      de historial **en vivo** (no basta con un buen backtest).
    * Correlación entre sus curvas de equity por debajo de
      ``fusion.max_parent_correlation``. La correlación es entre curvas, no
      entre genomas: dos genomas distintos pueden producir la misma curva y
      fusionarlos no aporta nada.
    * Fitness por encima de la mediana.
    * Profundidad de ensemble resultante ``<= fusion.max_depth``.

    Devuelve grupos de 2 a ``fusion.max_members``, priorizando los de
    correlación más baja: es donde está el beneficio.
    """
    f = cfg.fusion
    por_bot = {bot_id_of(g.id): g for g in alive}

    definidos = [v for v in fitness.values() if v == v]
    mediana = float(statistics.median(definidos)) if definidos else 0.0

    elegibles = [
        bot
        for bot, genoma in sorted(por_bot.items())
        if int(ages.get(bot, 0)) >= f.min_parent_generations
        and fitness.get(bot, float("nan")) == fitness.get(bot, float("nan"))
        and fitness.get(bot, float("-inf")) >= mediana
        and _depth_of(genoma) + 1 <= f.max_depth
    ]
    if len(elegibles) < max(2, f.min_members):
        return []

    parejas = [
        (_corr(correlations, a, b), a, b)
        for i, a in enumerate(elegibles)
        for b in elegibles[i + 1 :]
        if _corr(correlations, a, b) < f.max_parent_correlation
    ]
    parejas.sort(key=lambda t: (t[0], t[1], t[2]))

    usados: set[BotId] = set()
    grupos: list[tuple[BotId, ...]] = []
    for _, a, b in parejas:
        if a in usados or b in usados:
            continue
        grupo = [a, b]
        # Se agranda mientras haya alguien descorrelacionado con TODO el grupo:
        # basta un miembro correlacionado con otro para que la diversificación
        # que se buscaba deje de existir.
        for candidato in elegibles:
            if len(grupo) >= f.max_members:
                break
            if candidato in usados or candidato in grupo:
                continue
            if all(
                _corr(correlations, candidato, m) < f.max_parent_correlation for m in grupo
            ):
                grupo.append(candidato)
        usados.update(grupo)
        grupos.append(tuple(grupo))
    return grupos


def fuse(
    members: Sequence[Genome],
    metrics: Mapping[BotId, object],
    cfg: Config,
    rng: random.Random,
) -> Genome:
    """Crea el genoma ensemble.

    * Pesos iniciales proporcionales al Sortino de cada padre en la ventana,
      normalizados.
    * ``combine`` por defecto ``cfg.fusion.default_combine``; el jardinero
      puede proponer otro con ``FUSE``.
    * El gen de riesgo del hijo **es propio**: se hereda del padre con mejor
      Calmar. Un ensemble hereda señales, pero gestiona su propio riesgo.
    * La familia es ``HYBRID`` si los padres difieren, la común si coinciden.
    * ``ensemble.depth = 1 + max(depth de los miembros que sean ensembles)``.
    """
    if len(members) < cfg.fusion.min_members:
        raise ValueError(
            f"una fusión necesita al menos {cfg.fusion.min_members} miembros, "
            f"se han pasado {len(members)}"
        )

    ids = tuple(bot_id_of(g.id) for g in members)
    sortinos = [max(MIN_WEIGHT, float(_metric(metrics, b, "sortino"))) for b in ids]
    total = sum(sortinos) or float(len(sortinos))
    pesos = tuple(round(s / total, 6) for s in sortinos)

    mejor_calmar = max(
        range(len(ids)), key=lambda i: (float(_metric(metrics, ids[i], "calmar")), ids[i])
    )
    risk = members[mejor_calmar].risk

    familias = {g.family for g in members}
    familia = familias.pop() if len(familias) == 1 else IdeaFamily.HYBRID

    profundidad = 1 + max((_depth_of(g) for g in members), default=0)
    if profundidad > cfg.fusion.max_depth:
        raise ValueError(
            f"la fusión daría profundidad {profundidad}, por encima de "
            f"fusion.max_depth={cfg.fusion.max_depth}: los ensembles de "
            f"ensembles degeneran en promedios de todo el jardín"
        )

    bot_id = new_bot_id(f"fuse:{'+'.join(ids)}:{rng.getrandbits(64):016x}")
    raiz = members[mejor_calmar].meta.root_lineage
    return Genome(
        id=new_genome_id(bot_id),
        family=familia,
        market=members[0].market,
        features=(),
        risk=risk,
        ensemble=EnsembleGene(
            members=ids,
            weights=pesos,
            combine=CombineMode(str(cfg.fusion.default_combine)),
            threshold=float(cfg.fusion.default_threshold),
            depth=profundidad,
        ),
        meta=GenomeMeta(
            parents=ids,
            operator=BreedOperator.FUSION,
            root_lineage=raiz,
            parent_families=tuple(g.family for g in members),
        ),
    )


def _metric(metrics: Mapping[BotId, object], bot: BotId, name: str) -> float:
    met = metrics.get(bot)
    valor = getattr(met, name, 0.0) if met is not None else 0.0
    try:
        valor = float(valor)
    except (TypeError, ValueError):
        return 0.0
    return valor if valor == valor else 0.0


def reweight_on_death(ensemble: Genome, dead_member: BotId) -> Genome | None:
    """Redistribuye el peso de un miembro muerto entre los vivos.

    Devuelve ``None`` si quedan menos de 2 miembros vivos: entonces el ensemble
    se jubila con causa ``ORPHAN_ENSEMBLE``. El evento ``FUSION_REWEIGHTED`` lo
    registra quien llama, que es quien tiene la base delante.
    """
    gen = ensemble.ensemble
    if gen is None:
        return ensemble
    if dead_member not in gen.members:
        return ensemble

    supervivientes = [
        (m, w) for m, w in zip(gen.members, gen.weights) if m != dead_member
    ]
    if len(supervivientes) < 2:
        return None

    total = sum(w for _, w in supervivientes)
    if total <= 0:
        pesos = tuple(1.0 / len(supervivientes) for _ in supervivientes)
    else:
        pesos = tuple(round(w / total, 6) for _, w in supervivientes)

    return dataclasses.replace(
        ensemble,
        ensemble=dataclasses.replace(
            gen, members=tuple(m for m, _ in supervivientes), weights=pesos
        ),
    )


def combine_signals(
    signals: Sequence[int], weights: Sequence[float], combine: str, threshold: float
) -> int:
    """Combina las señales de los miembros en la del ensemble.

    Se combina la **posición deseada** (dentro/fuera), no los eventos de
    entrada: los eventos de miembros distintos rara vez coinciden en la misma
    vela, y combinarlos daría un ensemble que casi nunca opera.

    * ``VOTE``: mayoría simple.
    * ``WEIGHTED``: suma ponderada contra ``threshold``.
    * ``UNANIMOUS``: todos de acuerdo. Muy selectivo, pocas operaciones.
    * ``ANY``: cualquiera dispara. Muy activo, cuidado con el ``fee_drag``.
    """
    valores = [1 if int(s) > 0 else 0 for s in signals]
    if not valores:
        return 0

    modo = CombineMode(str(combine))
    if modo is CombineMode.UNANIMOUS:
        return int(all(valores))
    if modo is CombineMode.ANY:
        return int(any(valores))
    if modo is CombineMode.VOTE:
        return int(sum(valores) / len(valores) > 0.5)

    pesos = [float(w) for w in weights[: len(valores)]]
    if len(pesos) < len(valores) or sum(pesos) <= 0:
        pesos = [1.0 / len(valores)] * len(valores)
    else:
        suma = sum(pesos)
        pesos = [w / suma for w in pesos]
    return int(sum(w * v for w, v in zip(pesos, valores)) >= float(threshold))


__all__ = (
    "MIN_WEIGHT",
    "select_fusion_candidates",
    "fuse",
    "reweight_on_death",
    "combine_signals",
)
