"""Mutación: variación sobre un solo padre.

Tabla de mutaciones y pesos en docs/EVOLUTION.md §MUTATE.
"""

from __future__ import annotations

import dataclasses
import random

from ..config import Config
from ..genome.catalog import BY_CATEGORY, GeneCatalog, IndicatorSpec, ParamSpec, spec
from ..genome.distance import genome_distance
from ..genome.random_genome import (
    REGIME_KINDS,
    STANCE,
    _regime_rule,
    _sample_params,
    random_rule,
)
from ..genome.schema import (
    Condition,
    FeatureGene,
    Genome,
    LogicNode,
    Operand,
    RegimeGene,
    RuleNode,
    StopGene,
    TakeProfitGene,
    TrailingGene,
    rule_leaves,
)
from ..genome.validate import GenomeInvalid, repair_genome, validate_genome
from ..ids import new_bot_id, new_genome_id
from ..types import (
    BreedOperator,
    CompareOp,
    LogicOp,
    MutationKind,
    StopKind,
    TakeProfitKind,
    TrailingKind,
)

#: Pesos por defecto. La suma no necesita ser 1: se normaliza.
MUTATION_WEIGHTS: dict[MutationKind, float] = {
    MutationKind.TWEAK_PARAM: 0.35,
    MutationKind.TWEAK_THRESHOLD: 0.20,
    MutationKind.TWEAK_RISK: 0.15,
    MutationKind.ADD_CONDITION: 0.10,
    MutationKind.DROP_CONDITION: 0.10,
    MutationKind.SWAP_INDICATOR: 0.05,
    MutationKind.TOGGLE_REGIME: 0.03,
    MutationKind.TOGGLE_SHORT: 0.02,
}

#: Anchura del paso gaussiano, como fracción del rango del parámetro.
STEP_SIGMA = 0.15

#: Operadores cuyo operando derecho es un percentil en [0, 1] y no un valor del
#: indicador: su umbral se mueve en esa escala, no en la del feature.
_RANK_OPS = frozenset({CompareOp.PCT_RANK_GT, CompareOp.PCT_RANK_LT})

#: Bloques de reglas sobre los que se puede añadir o quitar una condición.
_RULE_BLOCKS: tuple[str, ...] = ("entry_long", "exit_long", "entry_short", "exit_short")

#: Tope de mutaciones extra que se aplican para que el hijo deje de parecerse
#: demasiado a su padre. Ver docs/DECISIONS.md D-020.
MAX_EXTRA_MUTATIONS = 4


# --------------------------------------------------------------------------- #
# Utilidades de árbol                                                          #
# --------------------------------------------------------------------------- #


def _blocks_with_rules(genome: Genome) -> list[str]:
    return [b for b in _RULE_BLOCKS if getattr(genome, b) is not None]


def _replace_node(tree: RuleNode | None, old: RuleNode, new: RuleNode | None) -> RuleNode | None:
    """Sustituye un nodo del árbol por otro, o lo quita si ``new`` es None.

    La comparación es por identidad y no por igualdad: dos hojas idénticas de un
    mismo árbol son nodos distintos, y cambiarlas las dos cuando sólo se pidió
    una convertiría una mutación puntual en otra cosa.
    """
    if tree is None:
        return None
    if tree is old:
        return new
    if isinstance(tree, Condition):
        return tree
    hijos = [_replace_node(c, old, new) for c in tree.children]
    vivos = tuple(c for c in hijos if c is not None)
    if not vivos:
        return None
    if tree.op is LogicOp.NOT:
        return LogicNode(LogicOp.NOT, vivos[:1])
    if len(vivos) == 1:
        return vivos[0]
    return LogicNode(tree.op, vivos)


def _all_leaves(genome: Genome) -> list[tuple[str, Condition]]:
    """Todas las hojas del genoma con el bloque en el que viven."""
    out: list[tuple[str, Condition]] = []
    for name, tree in genome.rule_trees():
        out.extend((name, leaf) for leaf in rule_leaves(tree))
    return out


def _set_tree(genome: Genome, block: str, tree: RuleNode | None) -> Genome:
    if block == "regime":
        if genome.regime is None:
            return genome
        return dataclasses.replace(
            genome,
            regime=dataclasses.replace(
                genome.regime, rule=tree, enabled=genome.regime.enabled and tree is not None
            ),
        )
    return dataclasses.replace(genome, **{block: tree})


def _tree_of(genome: Genome, block: str) -> RuleNode | None:
    if block == "regime":
        return genome.regime.rule if genome.regime is not None else None
    return getattr(genome, block)


# --------------------------------------------------------------------------- #
# Las mutaciones                                                               #
# --------------------------------------------------------------------------- #


def _gaussian_step(value: float, p: ParamSpec, rng: random.Random) -> float:
    sigma = max((p.high - p.low) * STEP_SIGMA, 1e-9)
    return p.clamp(rng.gauss(float(value), sigma))


def _coherent(s: IndicatorSpec, params: dict[str, float]) -> dict[str, float]:
    """Arregla las dependencias entre parámetros de un mismo indicador."""
    if s.kind == "MACD":
        fast, slow = s.param("fast"), s.param("slow")
        assert fast is not None and slow is not None
        if params.get("fast", 0.0) >= params.get("slow", 1.0):
            params["slow"] = slow.clamp(max(params["fast"] * 2.0, params["slow"]))
            if params["fast"] >= params["slow"]:
                params["fast"] = fast.clamp(params["slow"] / 2.0)
    return params


def _tweak_param(genome: Genome, cfg: Config, catalog: GeneCatalog, rng: random.Random) -> Genome:
    """Mueve un parámetro de indicador dentro de su rango."""
    candidatos = [f for f in genome.features if spec(f.kind).params]
    if not candidatos:
        return genome
    gene = rng.choice(candidatos)
    s = spec(gene.kind)
    # ``line`` elige qué salida de un indicador múltiple se mira: moverla no es
    # afinar un parámetro, es cambiar de sentido. Eso lo hace SWAP_INDICATOR.
    movibles = [p for p in s.params if p.name != "line" and p.name in gene.params]
    if not movibles:
        return genome
    p = rng.choice(movibles)
    params = dict(gene.params)
    params[p.name] = _gaussian_step(params[p.name], p, rng)
    nuevo = dataclasses.replace(gene, params=_coherent(s, params))
    return dataclasses.replace(
        genome,
        features=tuple(nuevo if f.id == gene.id else f for f in genome.features),
    )


def _const_range(leaf: Condition, genome: Genome) -> tuple[float, float] | None:
    """En qué escala vive la constante de una hoja."""
    if leaf.op in _RANK_OPS:
        return (0.02, 0.98)
    if leaf.left.ref is not None:
        gene = genome.feature(leaf.left.ref)
        if gene is not None:
            return spec(gene.kind).value_range
    return None


def _tweak_threshold(genome: Genome, cfg: Config, catalog: GeneCatalog, rng: random.Random) -> Genome:
    """Mueve una constante de una regla dentro de su escala."""
    candidatas = [
        (block, leaf)
        for block, leaf in _all_leaves(genome)
        if leaf.right is not None and leaf.right.const is not None
    ]
    if not candidatas:
        return genome
    block, leaf = rng.choice(candidatas)
    rango = _const_range(leaf, genome)
    actual = float(leaf.right.const)      # type: ignore[arg-type]
    if rango is None:
        # Sin escala declarada, un paso relativo es lo único honesto.
        nuevo = actual * rng.gauss(1.0, STEP_SIGMA)
    else:
        low, high = rango
        nuevo = min(max(rng.gauss(actual, (high - low) * STEP_SIGMA), low), high)
    nueva_hoja = dataclasses.replace(leaf, right=Operand(const=round(float(nuevo), 6)))
    return _set_tree(genome, block, _replace_node(_tree_of(genome, block), leaf, nueva_hoja))


def _tweak_risk(genome: Genome, cfg: Config, catalog: GeneCatalog, rng: random.Random) -> Genome:
    """Cambia stop, take profit, trailing o el riesgo por operación."""
    risk = genome.risk
    opcion = rng.choice(["risk_per_trade", "stop", "take_profit", "trailing", "holding"])

    if opcion == "risk_per_trade":
        low, high = cfg.risk.min_risk_per_trade, cfg.risk.max_risk_per_trade
        valor = min(max(rng.gauss(risk.risk_per_trade, (high - low) * STEP_SIGMA), low), high)
        return dataclasses.replace(genome, risk=dataclasses.replace(risk, risk_per_trade=valor))

    if opcion == "holding":
        valor = max(4, int(rng.gauss(risk.max_holding_bars, risk.max_holding_bars * 0.25)))
        return dataclasses.replace(
            genome, risk=dataclasses.replace(risk, max_holding_bars=min(valor, 2000))
        )

    if opcion == "stop":
        if risk.stop.kind is StopKind.NONE:
            nuevo = StopGene(kind=StopKind.ATR_MULT, value=round(rng.uniform(1.5, 3.5), 3))
        else:
            tope = 0.5 if risk.stop.kind is StopKind.PERCENT else 8.0
            valor = min(max(rng.gauss(risk.stop.value, tope * STEP_SIGMA), tope * 0.02), tope)
            nuevo = dataclasses.replace(risk.stop, value=round(valor, 4))
        return dataclasses.replace(genome, risk=dataclasses.replace(risk, stop=nuevo))

    if opcion == "take_profit":
        tp = risk.take_profit
        if tp.kind is TakeProfitKind.NONE:
            nuevo = TakeProfitGene(
                kind=rng.choice([TakeProfitKind.R_MULTIPLE, TakeProfitKind.ATR_MULT]),
                value=round(rng.uniform(1.5, 4.0), 3),
            )
        elif rng.random() < 0.25:
            nuevo = TakeProfitGene(TakeProfitKind.NONE, 0.0)
        else:
            nuevo = dataclasses.replace(
                tp, value=round(max(0.2, rng.gauss(tp.value, 8.0 * STEP_SIGMA)), 4)
            )
        return dataclasses.replace(genome, risk=dataclasses.replace(risk, take_profit=nuevo))

    tr = risk.trailing
    if tr.kind is TrailingKind.NONE:
        nuevo = TrailingGene(
            kind=rng.choice([TrailingKind.ATR_MULT, TrailingKind.PERCENT]),
            value=round(rng.uniform(1.5, 4.0), 3),
            activate_at_r=round(rng.uniform(0.0, 1.5), 2),
        )
    elif rng.random() < 0.25:
        nuevo = TrailingGene(TrailingKind.NONE, 0.0, 0.0)
    else:
        nuevo = dataclasses.replace(
            tr,
            value=round(max(0.2, rng.gauss(tr.value, 8.0 * STEP_SIGMA)), 4),
            activate_at_r=round(max(0.0, rng.gauss(tr.activate_at_r, 0.3)), 2),
        )
    return dataclasses.replace(genome, risk=dataclasses.replace(risk, trailing=nuevo))


def _new_feature(genome: Genome, cfg: Config, catalog: GeneCatalog, rng: random.Random) -> FeatureGene | None:
    """Un sentido nuevo para el genoma, sesgado por su familia."""
    if len(genome.features) >= cfg.evolution.max_features:
        return None
    disponibles = catalog.kinds_for(genome.family) or list(catalog.indicators)
    pesos = [catalog.weight(genome.family, k) for k in disponibles]
    kind = rng.choices(disponibles, weights=pesos)[0]
    usados = genome.feature_ids()
    i = 1
    while f"{kind.lower()}_{i}" in usados:
        i += 1
    s = spec(kind)
    stance = STANCE.get(genome.family, "up")
    return FeatureGene(
        id=f"{kind.lower()}_{i}",
        kind=kind,
        params=_sample_params(s, rng, stance),
        source=rng.choice(list(s.sources)) if len(s.sources) > 1 else s.sources[0],
    )


def _add_condition(genome: Genome, cfg: Config, catalog: GeneCatalog, rng: random.Random) -> Genome:
    """Añade una hoja con sentido, y el feature que necesite si no lo hay."""
    bloques = _blocks_with_rules(genome)
    if not bloques:
        return genome
    block = rng.choice(bloques)
    arbol = _tree_of(genome, block)
    if arbol is None or len(rule_leaves(arbol)) >= cfg.evolution.max_rule_leaves:
        return genome

    features = list(genome.features)
    if rng.random() < 0.35:
        nuevo = _new_feature(genome, cfg, catalog, rng)
        if nuevo is not None:
            features.append(nuevo)
            genome = dataclasses.replace(genome, features=tuple(features))

    entrada = block.startswith("entry")
    arriba = STANCE.get(genome.family, "up") == "up"
    upward = arriba if entrada else not arriba
    if block.endswith("short"):
        upward = not upward

    try:
        hoja = random_rule(
            features, catalog, cfg, rng, max_leaves=1,
            bias="entry" if entrada else "exit", upward=upward,
        )
    except GenomeInvalid:
        return genome

    op = LogicOp.AND if entrada else LogicOp.OR
    if isinstance(arbol, LogicNode) and arbol.op is op:
        nuevo_arbol: RuleNode = LogicNode(op, (*arbol.children, hoja))
    else:
        nuevo_arbol = LogicNode(op, (arbol, hoja))
    return _set_tree(genome, block, nuevo_arbol)


def _drop_condition(genome: Genome, cfg: Config, catalog: GeneCatalog, rng: random.Random) -> Genome:
    """Quita una hoja, siempre que el bloque no se quede sin ninguna."""
    candidatas = [
        (block, leaf)
        for block, leaf in _all_leaves(genome)
        if len(rule_leaves(_tree_of(genome, block))) > 1
    ]
    if not candidatas:
        return genome
    block, leaf = rng.choice(candidatas)
    return _set_tree(genome, block, _replace_node(_tree_of(genome, block), leaf, None))


def _swap_indicator(genome: Genome, cfg: Config, catalog: GeneCatalog, rng: random.Random) -> Genome:
    """Sustituye un indicador por otro de la misma categoría.

    La misma categoría y no cualquiera: cambiar un RSI por una EMA no es una
    variación de la idea, es otra idea, y además deja todas las comparaciones
    del genoma midiendo escalas que no se pueden comparar.
    """
    candidatos = [f for f in genome.features if f.id != genome.risk.stop.atr_ref]
    if not candidatos:
        return genome
    gene = rng.choice(candidatos)
    categoria = spec(gene.kind).category
    alternativas = [
        k for k in BY_CATEGORY.get(categoria, ()) if k != gene.kind and k in catalog.indicators
    ]
    if not alternativas:
        return genome

    kind = rng.choices(
        alternativas, weights=[catalog.weight(genome.family, k) for k in alternativas]
    )[0]
    s = spec(kind)
    usados = genome.feature_ids() - {gene.id}
    i = 1
    while f"{kind.lower()}_{i}" in usados:
        i += 1
    nuevo_id = f"{kind.lower()}_{i}"
    nuevo = FeatureGene(
        id=nuevo_id,
        kind=kind,
        params=_sample_params(s, rng, STANCE.get(genome.family, "up")),
        source=gene.source if gene.source in s.sources else s.sources[0],
    )

    g = dataclasses.replace(
        genome,
        features=tuple(nuevo if f.id == gene.id else f for f in genome.features),
    )
    return _rename_ref(g, gene.id, nuevo_id)


def _rename_ref(genome: Genome, viejo: str, nuevo: str) -> Genome:
    """Reescribe todas las referencias a un feature renombrado."""

    def operando(o: Operand | None) -> Operand | None:
        return Operand(ref=nuevo) if o is not None and o.ref == viejo else o

    def visita(node: RuleNode | None) -> RuleNode | None:
        if node is None:
            return None
        if isinstance(node, Condition):
            return dataclasses.replace(
                node,
                left=operando(node.left) or node.left,
                right=operando(node.right),
                right2=operando(node.right2),
            )
        hijos = tuple(c for c in (visita(h) for h in node.children) if c is not None)
        return LogicNode(node.op, hijos) if hijos else None

    cambios: dict[str, object] = {b: visita(getattr(genome, b)) for b in _RULE_BLOCKS}
    if genome.regime is not None:
        cambios["regime"] = dataclasses.replace(genome.regime, rule=visita(genome.regime.rule))
    if genome.risk.stop.atr_ref == viejo:
        cambios["risk"] = dataclasses.replace(
            genome.risk, stop=dataclasses.replace(genome.risk.stop, atr_ref=nuevo)
        )
    return dataclasses.replace(genome, **cambios)  # type: ignore[arg-type]


def _toggle_regime(genome: Genome, cfg: Config, catalog: GeneCatalog, rng: random.Random) -> Genome:
    """Activa o desactiva el filtro de régimen."""
    if genome.regime is not None and genome.regime.enabled:
        return dataclasses.replace(
            genome, regime=dataclasses.replace(genome.regime, enabled=False)
        )
    if genome.regime is not None and genome.regime.rule is not None:
        return dataclasses.replace(
            genome, regime=dataclasses.replace(genome.regime, enabled=True)
        )

    features = list(genome.features)
    if not any(f.kind in REGIME_KINDS for f in features):
        nuevo = _new_feature(genome, cfg, catalog, rng)
        if nuevo is None:
            return genome
        kind = rng.choice(REGIME_KINDS)
        s = spec(kind)
        nuevo = dataclasses.replace(
            nuevo,
            id=f"{kind.lower()}_reg",
            kind=kind,
            params=_sample_params(s, rng, STANCE.get(genome.family, "up")),
            source=s.sources[0],
        )
        if nuevo.id in genome.feature_ids():
            return genome
        features.append(nuevo)

    regla = _regime_rule(features, rng, upward=STANCE.get(genome.family, "up") == "up")
    if regla is None:
        return genome
    return dataclasses.replace(
        genome, features=tuple(features), regime=RegimeGene(enabled=True, rule=regla)
    )


def _toggle_short(genome: Genome, cfg: Config, catalog: GeneCatalog, rng: random.Random) -> Genome:
    """Permite o prohíbe cortos.

    En spot no hay cortos que valgan, así que esto sólo puede apagarlos: un bot
    que los tuviera encendidos por herencia los pierde, y nadie los enciende.
    Ver docs/DECISIONS.md D-001.
    """
    if not genome.risk.allow_short:
        return genome
    return dataclasses.replace(
        genome, risk=dataclasses.replace(genome.risk, allow_short=False)
    )


_MUTATORS = {
    MutationKind.TWEAK_PARAM: _tweak_param,
    MutationKind.TWEAK_THRESHOLD: _tweak_threshold,
    MutationKind.TWEAK_RISK: _tweak_risk,
    MutationKind.ADD_CONDITION: _add_condition,
    MutationKind.DROP_CONDITION: _drop_condition,
    MutationKind.SWAP_INDICATOR: _swap_indicator,
    MutationKind.TOGGLE_REGIME: _toggle_regime,
    MutationKind.TOGGLE_SHORT: _toggle_short,
}


# --------------------------------------------------------------------------- #
# El operador                                                                  #
# --------------------------------------------------------------------------- #


def _pick_kinds(n: int, rng: random.Random) -> list[MutationKind]:
    tipos = list(MUTATION_WEIGHTS)
    pesos = [MUTATION_WEIGHTS[k] for k in tipos]
    return rng.choices(tipos, weights=pesos, k=n)


def mutate(
    parent: Genome,
    cfg: Config,
    catalog: GeneCatalog,
    rng: random.Random,
    *,
    rate_factor: float = 1.0,
    n_mutations: int | None = None,
) -> Genome:
    """Produce un hijo mutado.

    Aplica entre 1 y 3 mutaciones (``cfg.evolution.mutations_per_child``)
    elegidas por ``MUTATION_WEIGHTS``, valida y repara. El hijo es un genoma
    nuevo con id nuevo y ``meta.operator = MUTATE``: el padre no cambia. Un bot
    nunca muta en sitio, porque entonces la genealogía dejaría de significar
    nada.

    ``rate_factor`` viene de ``adaptive_rate_factor`` y estira o encoge ese
    número de mutaciones.

    Detalles de cada mutación:

    * ``TWEAK_PARAM``: paso gaussiano con σ = 15 % del rango del ``ParamSpec``,
      recortado al rango. Mantiene las coherencias (en MACD, ``fast < slow``).
    * ``TWEAK_THRESHOLD``: mueve una constante dentro del ``value_range`` del
      indicador con el que se compara.
    * ``TWEAK_RISK``: uno de stop / take profit / trailing / ``risk_per_trade``,
      dentro de los límites de ``cfg.risk``.
    * ``ADD_CONDITION``: añade una hoja con sentido si no se supera
      ``max_rule_leaves``; si el feature necesario no existe, se añade también.
    * ``DROP_CONDITION``: quita una hoja si queda al menos una; la reparación
      poda después los features que quedan huérfanos.
    * ``SWAP_INDICATOR``: sustituye por otro de la **misma categoría**
      (``catalog.BY_CATEGORY``) y reescribe las referencias.
    * ``TOGGLE_REGIME`` / ``TOGGLE_SHORT``: activan o desactivan.

    Además, el hijo sigue mutando mientras se parezca demasiado al padre: una
    sola mutación puntual cae casi siempre por debajo de ``clone_threshold``, y
    la incubadora lo rechazaría como clon sin llegar a probarlo. Ver
    docs/DECISIONS.md D-020.

    Si tras 5 intentos no se consigue un hijo válido y distinto del padre,
    devuelve el padre con id nuevo (una mutación neutra) en vez de fallar.
    """
    low, high = (cfg.evolution.mutations_per_child + [1, 3])[:2]
    factor = max(0.1, float(rate_factor))
    weights = cfg.speciation.distance_weights

    for _ in range(5):
        n = n_mutations if n_mutations is not None else rng.randint(int(low), int(high))
        n = max(1, min(12, int(round(n * factor))))

        hijo = parent
        for kind in _pick_kinds(n, rng):
            hijo = _MUTATORS[kind](hijo, cfg, catalog, rng)

        extra = 0
        while extra < MAX_EXTRA_MUTATIONS:
            try:
                candidato = repair_genome(hijo, cfg, catalog)
            except GenomeInvalid:
                break
            if genome_distance(parent, candidato, weights, catalog) >= cfg.speciation.clone_threshold:
                hijo = candidato
                break
            hijo = _MUTATORS[_pick_kinds(1, rng)[0]](hijo, cfg, catalog, rng)
            extra += 1

        try:
            hijo = repair_genome(hijo, cfg, catalog)
            validate_genome(hijo, cfg, catalog, strict=True)
        except GenomeInvalid:
            continue
        if hijo.to_dict(include_meta=False) == parent.to_dict(include_meta=False):
            continue
        return _as_child(hijo, parent, rng)

    return _as_child(parent, parent, rng)


def _as_child(genome: Genome, parent: Genome, rng: random.Random) -> Genome:
    """Le da identidad propia y pedigrí al hijo. El padre no se toca.

    El id sale del ``Random`` inyectado y no de nada del proceso: dos
    ejecuciones con la misma semilla tienen que producir el mismo jardín hasta
    en los identificadores.
    """
    bot_id = new_bot_id(f"mut:{parent.id}:{rng.getrandbits(64):016x}")
    return dataclasses.replace(
        genome,
        id=new_genome_id(bot_id),
        meta=dataclasses.replace(
            parent.meta,
            parents=(parent.id,),
            operator=BreedOperator.MUTATE,
            parent_families=(parent.family,),
            gardener_note="",
        ),
    )


def adaptive_rate_factor(median_fitness_history: list[float], cfg: Config) -> float:
    """Tasa de mutación adaptativa.

    Si la mediana de fitness lleva ``stagnation_generations`` sin mejorar, sube
    hasta ``mutation_rate_max_factor``; si mejora rápido, baja hasta
    ``mutation_rate_min_factor``. Evita los dos fallos clásicos de estos
    sistemas: la convergencia prematura y el ruido perpetuo.
    """
    e = cfg.evolution
    historia = [float(v) for v in median_fitness_history if v == v]
    if len(historia) < 2:
        return 1.0

    ventana = max(1, int(e.stagnation_generations))
    k = min(len(historia) - 1, ventana)
    delta = historia[-1] - historia[-1 - k]

    if delta > 0:
        #: Mejora "rápida" de referencia: un cuarto de desviación de fitness en
        #: la ventana de estancamiento. Por encima, no hace falta agitar nada.
        fuerte = 0.25
        avance = min(1.0, delta / fuerte)
        return float(1.0 - (1.0 - e.mutation_rate_min_factor) * avance)

    racha = 0
    for anterior, siguiente in zip(reversed(historia[:-1]), reversed(historia)):
        if siguiente > anterior:
            break
        racha += 1
    return float(1.0 + (e.mutation_rate_max_factor - 1.0) * min(1.0, racha / ventana))


__all__ = (
    "MAX_EXTRA_MUTATIONS",
    "MUTATION_WEIGHTS",
    "STEP_SIGMA",
    "mutate",
    "adaptive_rate_factor",
)
