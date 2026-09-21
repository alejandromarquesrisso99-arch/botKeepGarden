"""Cruce por bloques entre dos padres.

Los bloques son: ``features``, ``entry_long``, ``exit_long``, ``entry_short``,
``exit_short``, ``risk``, ``regime``. Para cada uno se elige el del padre A o el
del B, sesgado hacia el de mejor fitness.

Cruzar por bits no tiene sentido aquí: un árbol de reglas partido por la mitad
no es un árbol. Por bloques, en cambio, el hijo hereda ideas completas.
"""

from __future__ import annotations

import dataclasses
import random

from ..config import Config
from ..genome.catalog import GeneCatalog
from ..genome.distance import genome_distance
from ..genome.schema import (
    Condition,
    FeatureGene,
    Genome,
    LogicNode,
    Operand,
    RuleNode,
    referenced_features,
)
from ..genome.validate import GenomeInvalid, repair_genome, validate_genome
from ..ids import bot_id_of, new_bot_id, new_genome_id
from ..types import BreedOperator, IdeaFamily

BLOCKS: tuple[str, ...] = (
    "features",
    "entry_long",
    "exit_long",
    "entry_short",
    "exit_short",
    "risk",
    "regime",
)

#: Bloques que son árboles de reglas y por tanto pueden traerse features
#: prestados del padre del que vienen.
RULE_BLOCKS: tuple[str, ...] = ("entry_long", "exit_long", "entry_short", "exit_short")

#: Endogamia máxima tolerada entre dos padres. Por encima, cruzar es repetir.
#:
#: El número está calibrado sobre el coeficiente de ``Pedigree``, que es un
#: Jaccard de antepasados: hermanos enteros dan 0.5, medio hermanos 0.33,
#: padre e hijo 0.5 y primos 0.2. El corte en 0.3 deja pasar a los primos y
#: frena a los hermanos, que es exactamente la línea que pide
#: docs/EVOLUTION.md: la endogamia colapsa la diversidad incluso con la
#: especiación activa, porque los hermanos se cruzan generación tras generación.
MAX_INBREEDING = 0.3


# --------------------------------------------------------------------------- #
# Reparación de referencias                                                    #
# --------------------------------------------------------------------------- #


def _same_definition(a: FeatureGene, b: FeatureGene) -> bool:
    """¿Son el mismo sensor, aunque se llamen distinto?"""
    return (
        a.kind == b.kind
        and a.source == b.source
        and a.timeframe == b.timeframe
        and a.params == b.params
    )


def _unique_id(base: str, usados: set[str]) -> str:
    i = 2
    while f"{base}_x{i}" in usados:
        i += 1
    return f"{base}_x{i}"


def _rewrite(tree: RuleNode | None, mapping: dict[str, str]) -> RuleNode | None:
    """Reescribe las referencias de un árbol según el renombrado."""
    if tree is None or not mapping:
        return tree

    def operando(o: Operand | None) -> Operand | None:
        if o is not None and o.ref is not None and o.ref in mapping:
            return Operand(ref=mapping[o.ref])
        return o

    def visita(node: RuleNode) -> RuleNode:
        if isinstance(node, Condition):
            return dataclasses.replace(
                node,
                left=operando(node.left) or node.left,
                right=operando(node.right),
                right2=operando(node.right2),
            )
        return LogicNode(node.op, tuple(visita(c) for c in node.children))

    return visita(tree)


def _adopt(
    tree: RuleNode | None,
    origen: Genome,
    pool: dict[str, FeatureGene],
) -> RuleNode | None:
    """Arrastra al hijo los features que este árbol necesita.

    Es el 80 % del trabajo del cruce. Si ``entry_long`` viene de A y referencia
    un feature que sólo existía en A, ese feature se viene con la regla. Si el
    id ya está ocupado por un sensor distinto, el recién llegado se renombra y
    su regla se reescribe: dos cosas distintas no pueden llamarse igual.
    """
    if tree is None:
        return None
    mapping: dict[str, str] = {}
    for ref in sorted(referenced_features(tree)):
        gene = origen.feature(ref)
        if gene is None:
            continue                      # referencia rota: la poda la reparación
        alojado = pool.get(ref)
        if alojado is None:
            pool[ref] = gene
        elif not _same_definition(alojado, gene):
            gemelo = next(
                (fid for fid, g in pool.items() if _same_definition(g, gene)), None
            )
            if gemelo is None:
                gemelo = _unique_id(ref, set(pool))
                pool[gemelo] = dataclasses.replace(gene, id=gemelo)
            mapping[ref] = gemelo
    return _rewrite(tree, mapping)


# --------------------------------------------------------------------------- #
# El operador                                                                  #
# --------------------------------------------------------------------------- #


def crossover(
    parent_a: Genome,
    parent_b: Genome,
    cfg: Config,
    catalog: GeneCatalog,
    rng: random.Random,
    *,
    a_is_better: bool = True,
) -> Genome | None:
    """Cruza dos padres y devuelve un hijo válido.

    1. Comprueba que ``distance(a, b) > cfg.evolution.crossover_min_distance``.
       Cruzar dos casi-clones gasta un nacimiento sin explorar nada.
    2. Para cada bloque, elige A o B con probabilidad
       ``fitness_bias_to_better_parent`` a favor del mejor.
    3. **Repara**: arrastra los features que las reglas heredadas referencian,
       renombra los ids duplicados con distinta definición reescribiendo sus
       referencias, poda los huérfanos y recorta a ``max_features``,
       ``max_rule_leaves`` y ``max_rule_depth``.
    4. La familia del hijo: la común si ambos padres coinciden; ``HYBRID`` si
       no, guardando las familias de origen en ``meta.parent_families`` para
       poder rastrear qué mezclas funcionan.
    5. Valida. Si el hijo no es reparable devuelve ``None``, y que el llamador
       reintente con otra pareja.

    Los ensembles no se cruzan: un ensemble no tiene reglas propias que heredar,
    y partirlo por bloques daría un hijo con miembros de uno y riesgo de otro
    sin que eso signifique nada. Para combinar ensembles está la fusión.
    """
    if parent_a.is_ensemble or parent_b.is_ensemble:
        return None
    weights = cfg.speciation.distance_weights
    if genome_distance(parent_a, parent_b, weights, catalog) <= cfg.evolution.crossover_min_distance:
        return None

    sesgo = cfg.evolution.fitness_bias_to_better_parent
    mejor, peor = (parent_a, parent_b) if a_is_better else (parent_b, parent_a)

    def elige() -> Genome:
        return mejor if rng.random() < sesgo else peor

    origen = {block: elige() for block in BLOCKS}

    pool: dict[str, FeatureGene] = {f.id: f for f in origen["features"].features}
    arboles: dict[str, RuleNode | None] = {}
    for block in RULE_BLOCKS:
        arboles[block] = _adopt(getattr(origen[block], block), origen[block], pool)

    fuente_regimen = origen["regime"]
    regimen = None
    if fuente_regimen.regime is not None:
        regla = _adopt(fuente_regimen.regime.rule, fuente_regimen, pool)
        regimen = dataclasses.replace(
            fuente_regimen.regime, rule=regla, enabled=fuente_regimen.regime.enabled and regla is not None
        )

    risk = origen["risk"].risk
    if risk.stop.atr_ref:
        prestado = origen["risk"].feature(risk.stop.atr_ref)
        if prestado is not None and risk.stop.atr_ref not in pool:
            pool[risk.stop.atr_ref] = prestado

    familia = (
        parent_a.family if parent_a.family is parent_b.family else IdeaFamily.HYBRID
    )
    bot_id = new_bot_id(f"cross:{parent_a.id}:{parent_b.id}:{rng.getrandbits(64):016x}")

    hijo = Genome(
        id=new_genome_id(bot_id),
        family=familia,
        market=parent_a.market,
        features=tuple(pool[fid] for fid in sorted(pool)),
        entry_long=arboles["entry_long"],
        exit_long=arboles["exit_long"],
        entry_short=arboles["entry_short"],
        exit_short=arboles["exit_short"],
        risk=risk,
        regime=regimen,
        meta=dataclasses.replace(
            mejor.meta,
            parents=(parent_a.id, parent_b.id),
            operator=BreedOperator.CROSSOVER,
            parent_families=(parent_a.family, parent_b.family),
            gardener_note="",
        ),
    )

    try:
        hijo = repair_genome(hijo, cfg, catalog)
        validate_genome(hijo, cfg, catalog, strict=True)
    except GenomeInvalid:
        return None
    return hijo


def pick_pair(
    candidates: list[tuple[Genome, float]],
    cfg: Config,
    catalog: GeneCatalog,
    rng: random.Random,
    *,
    pedigree: object | None = None,
) -> tuple[Genome, Genome] | None:
    """Elige dos padres compatibles.

    Filtra por distancia genética mínima y, si se pasa el pedigrí, por
    coeficiente de endogamia (``lineage.Pedigree.inbreeding_coefficient``): la
    endogamia colapsa la diversidad incluso con especiación activa, porque los
    hermanos se cruzan una generación tras otra.

    Devuelve la pareja con el mejor primero, que es lo que espera el sesgo de
    ``crossover``.
    """
    elegibles = [(g, f) for g, f in candidates if not g.is_ensemble]
    if len(elegibles) < 2:
        return None

    weights = cfg.speciation.distance_weights
    minima = cfg.evolution.crossover_min_distance
    orden = sorted(elegibles, key=lambda par: (-par[1], par[0].id))

    for _ in range(12):
        # Sesgo hacia los mejores sin excluir al resto: el rango pondera, no
        # decide. Un cruce sólo entre élites converge en tres generaciones.
        pesos = [1.0 / (i + 1) for i in range(len(orden))]
        primero = rng.choices(range(len(orden)), weights=pesos)[0]
        ga, fa = orden[primero]

        compatibles = []
        for i, (gb, fb) in enumerate(orden):
            if i == primero:
                continue
            if genome_distance(ga, gb, weights, catalog) <= minima:
                continue
            if pedigree is not None and _too_inbred(
                pedigree, bot_id_of(ga.id), bot_id_of(gb.id)
            ):
                continue
            compatibles.append((i, gb, fb))
        if not compatibles:
            continue
        pesos_b = [1.0 / (i + 1) for i, _, _ in compatibles]
        _, gb, fb = rng.choices(compatibles, weights=pesos_b)[0]
        return (ga, gb) if fa >= fb else (gb, ga)

    return None


def _too_inbred(pedigree: object, a: str, b: str) -> bool:
    coeficiente = getattr(pedigree, "inbreeding_coefficient", None)
    if coeficiente is None:
        return False
    return float(coeficiente(a, b)) > MAX_INBREEDING


__all__ = ("BLOCKS", "MAX_INBREEDING", "RULE_BLOCKS", "crossover", "pick_pair")
