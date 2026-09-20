"""Validación estructural de genomas.

Un genoma inválido nunca debe llegar al motor. Esto se llama:

* tras cada operador evolutivo (mutación, cruce, fusión, injerto),
* al deserializar un genoma desde la base o desde un archivo,
* en los tests de fuzz, sobre miles de genomas aleatorios.

Validar es barato; reparar, casi igual de barato. Descartar descendencia rota
es lo caro, porque la evolución produce genomas mal formados a docenas en cada
generación y tirarlos es tirar la mayor parte de la exploración.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..config import Config
from ..types import (
    RANGE_COMPARE_OPS,
    UNARY_COMPARE_OPS,
    CompareOp,
    LogicOp,
    SizingKind,
    StopKind,
)
from .catalog import GeneCatalog, spec
from .schema import (
    Condition,
    FeatureGene,
    Genome,
    LogicNode,
    Operand,
    RiskGene,
    RuleNode,
    rule_depth,
    rule_leaves,
    walk_rules,
)

#: Operadores cuyo operando derecho es un percentil, no un valor del indicador.
_RANK_OPS = frozenset({CompareOp.PCT_RANK_GT, CompareOp.PCT_RANK_LT})

#: Marca de "esto es una constante", para distinguirla de "no se pudo resolver".
_CONST = "__const__"


class GenomeInvalid(ValueError):
    """El genoma viola una invariante estructural."""


@dataclass(slots=True)
class ValidationReport:
    ok: bool = True
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def fail(self, msg: str) -> None:
        self.ok = False
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)


# --------------------------------------------------------------------------- #
# Utilidades de lectura                                                        #
# --------------------------------------------------------------------------- #


def _operand_output(operand: Operand | None, genome: Genome) -> str | None:
    """Naturaleza de la salida de un operando, o ``None`` si no resuelve."""
    if operand is None:
        return None
    if operand.const is not None:
        return _CONST
    if operand.price is not None:
        return "price"
    gene = genome.feature(operand.ref or "")
    if gene is None:
        return None
    try:
        return spec(gene.kind).output
    except KeyError:
        return None


def _value_range(operand: Operand | None, genome: Genome) -> tuple[float, float] | None:
    if operand is None or operand.ref is None:
        return None
    gene = genome.feature(operand.ref)
    if gene is None:
        return None
    try:
        return spec(gene.kind).value_range
    except KeyError:
        return None


def _describe(operand: Operand | None) -> str:
    return operand.describe() if operand is not None else "—"


# --------------------------------------------------------------------------- #
# Validación                                                                   #
# --------------------------------------------------------------------------- #


def _check_features(genome: Genome, cfg: Config, catalog: GeneCatalog, r: ValidationReport) -> None:
    vistos: set[str] = set()
    for gene in genome.features:
        if gene.id in vistos:
            r.fail(f"el feature '{gene.id}' está definido dos veces")
        vistos.add(gene.id)

        if gene.kind not in catalog.indicators:
            r.fail(f"el feature '{gene.id}' usa el indicador '{gene.kind}', que no está en el catálogo")
            continue
        s = catalog.indicators[gene.kind]

        for p in s.params:
            if p.name not in gene.params:
                r.fail(f"{gene.id} ({gene.kind}): falta el parámetro '{p.name}'")
                continue
            value = float(gene.params[p.name])
            if not (p.low <= value <= p.high):
                r.fail(
                    f"{gene.id} ({gene.kind}): '{p.name}' vale {value:g} y debe estar "
                    f"entre {p.low:g} y {p.high:g}"
                )
        for name in gene.params:
            if s.param(name) is None:
                r.warn(f"{gene.id} ({gene.kind}): el parámetro '{name}' no existe y se ignorará")

        if gene.source not in s.sources:
            r.fail(
                f"{gene.id} ({gene.kind}): la fuente '{gene.source}' no está admitida "
                f"(válidas: {', '.join(str(x) for x in s.sources)})"
            )

        if gene.timeframe is not None and gene.timeframe not in cfg.market.context_timeframes:
            r.warn(
                f"{gene.id}: usa el timeframe de contexto '{gene.timeframe}', que no está "
                f"en market.context_timeframes"
            )

        _check_composite_params(gene, r)

    if len(genome.features) > cfg.evolution.max_features:
        r.fail(
            f"{len(genome.features)} features y el límite (evolution.max_features) "
            f"es {cfg.evolution.max_features}"
        )


def _check_composite_params(gene: FeatureGene, r: ValidationReport) -> None:
    """Coherencia entre parámetros del mismo indicador.

    Un MACD con la media rápida más lenta que la lenta compila sin quejarse y
    produce exactamente la señal contraria a la que su autor creía.
    """
    p = gene.params
    if gene.kind == "MACD" and float(p.get("fast", 0)) >= float(p.get("slow", 1)):
        r.fail(
            f"{gene.id} (MACD): 'fast' ({p.get('fast')}) debe ser menor que "
            f"'slow' ({p.get('slow')})"
        )
    if gene.kind in {"BBANDS", "BB_WIDTH"} and float(p.get("stdev", 1)) <= 0:
        r.fail(f"{gene.id} ({gene.kind}): 'stdev' debe ser positivo")
    if gene.kind in {"KELTNER", "SUPERTREND"} and float(p.get("multiplier", 1)) <= 0:
        r.fail(f"{gene.id} ({gene.kind}): 'multiplier' debe ser positivo")
    if gene.kind.startswith("DONCHIAN") and float(p.get("period", 2)) < 2:
        r.fail(f"{gene.id} ({gene.kind}): 'period' debe ser al menos 2")


def _check_references(genome: Genome, r: ValidationReport) -> None:
    conocidos = genome.feature_ids()
    for nombre, tree in genome.rule_trees():
        for leaf in rule_leaves(tree):
            for operand in (leaf.left, leaf.right, leaf.right2):
                if operand is not None and operand.ref is not None and operand.ref not in conocidos:
                    r.fail(f"{nombre}: referencia al feature '{operand.ref}', que no existe")

    atr_ref = genome.risk.stop.atr_ref
    if atr_ref and atr_ref not in conocidos:
        r.fail(f"risk.stop.atr_ref apunta a '{atr_ref}', que no existe")

    usados = genome.all_referenced_features()
    for gene in genome.features:
        if gene.id not in usados:
            r.warn(f"el feature '{gene.id}' no lo usa nadie: es complejidad que no paga")


def _check_shape(genome: Genome, cfg: Config, r: ValidationReport) -> None:
    for nombre, tree in genome.rule_trees():
        if tree is None:
            continue
        hojas = len(rule_leaves(tree))
        if hojas > cfg.evolution.max_rule_leaves:
            r.fail(
                f"{nombre}: {hojas} hojas y el límite (evolution.max_rule_leaves) "
                f"es {cfg.evolution.max_rule_leaves}"
            )
        profundidad = rule_depth(tree)
        if profundidad > cfg.evolution.max_rule_depth:
            r.fail(
                f"{nombre}: profundidad {profundidad} y el límite "
                f"(evolution.max_rule_depth) es {cfg.evolution.max_rule_depth}"
            )
        for node in walk_rules(tree):
            if isinstance(node, LogicNode) and node.op is not LogicOp.NOT and not node.children:
                r.fail(f"{nombre}: un nodo {node.op} sin hijos")


def _check_comparisons(
    genome: Genome, catalog: GeneCatalog, r: ValidationReport
) -> None:
    """Comparaciones con sentido: escalas compatibles y constantes en su rango.

    Comparar un RSI con una EMA no significa nada —viven en escalas distintas—
    y un umbral de 500 sobre un RSI es una condición que nunca se cumple. Las
    dos cosas producen bots que parecen válidos y no operan jamás.
    """
    for nombre, tree in genome.rule_trees():
        for leaf in rule_leaves(tree):
            izquierda = _operand_output(leaf.left, genome)
            if izquierda is None:
                continue                       # ya lo denunció _check_references

            if leaf.op in UNARY_COMPARE_OPS:
                if izquierda is _CONST:
                    r.fail(f"{nombre}: {leaf.op} sobre una constante no dice nada")
                continue

            derechas = [leaf.right] if leaf.op not in RANGE_COMPARE_OPS else [leaf.right, leaf.right2]
            for operand in derechas:
                salida = _operand_output(operand, genome)
                if salida is None:
                    continue

                if leaf.op in _RANK_OPS:
                    _check_rank_operand(nombre, operand, salida, r)
                    continue

                if izquierda is _CONST and salida is _CONST:
                    r.fail(f"{nombre}: comparar dos constantes no depende del mercado")
                elif salida is _CONST:
                    _check_constant(nombre, leaf.left, operand, genome, r)
                elif izquierda is _CONST:
                    _check_constant(nombre, operand, leaf.left, genome, r)
                else:
                    _check_same_scale(nombre, leaf, izquierda, salida, genome, catalog, r)


def _check_rank_operand(
    nombre: str, operand: Operand | None, salida: str, r: ValidationReport
) -> None:
    if operand is None or salida is not _CONST:
        return
    value = float(operand.const or 0.0)
    if not 0.0 <= value <= 1.0:
        r.fail(
            f"{nombre}: un percentil se compara contra un número entre 0 y 1, "
            f"y aquí vale {value:g}"
        )


def _check_constant(
    nombre: str,
    referencia: Operand | None,
    constante: Operand | None,
    genome: Genome,
    r: ValidationReport,
) -> None:
    if constante is None:
        return
    value = float(constante.const or 0.0)
    rango = _value_range(referencia, genome)
    if rango is None:
        if _operand_output(referencia, genome) == "price":
            r.warn(
                f"{nombre}: {_describe(referencia)} se compara con la constante {value:g}, "
                f"que depende de la escala del precio y no generaliza"
            )
        return
    low, high = rango
    if not low <= value <= high:
        r.fail(
            f"{nombre}: la constante {value:g} está fuera del rango de "
            f"{_describe(referencia)} ({low:g} a {high:g}): la condición nunca cambiaría"
        )


def _check_same_scale(
    nombre: str,
    leaf: Condition,
    izquierda: str,
    derecha: str,
    genome: Genome,
    catalog: GeneCatalog,
    r: ValidationReport,
) -> None:
    if izquierda != derecha:
        r.fail(
            f"{nombre}: no se pueden comparar {_describe(leaf.left)} ({izquierda}) y "
            f"{_describe(leaf.right)} ({derecha}): son escalas distintas"
        )
        return
    rango_izq = _value_range(leaf.left, genome)
    rango_der = _value_range(leaf.right, genome)
    if rango_izq and rango_der and (rango_izq[1] < rango_der[0] or rango_der[1] < rango_izq[0]):
        r.warn(
            f"{nombre}: {_describe(leaf.left)} y {_describe(leaf.right)} no solapan sus "
            f"rangos, así que la comparación siempre dará lo mismo"
        )
    if leaf.left.ref and leaf.right is not None and leaf.right.ref:
        izq = genome.feature(leaf.left.ref)
        der = genome.feature(leaf.right.ref)
        if izq and der and not catalog_is_comparable(catalog, izq.kind, der.kind):
            r.fail(f"{nombre}: el catálogo no admite comparar {izq.kind} con {der.kind}")


def catalog_is_comparable(catalog: GeneCatalog, a: str, b: str) -> bool:
    """``is_comparable`` del catálogo, tolerante con indicadores desconocidos."""
    if a not in catalog.indicators or b not in catalog.indicators:
        return False
    return catalog.indicators[a].output == catalog.indicators[b].output


def _check_risk(genome: Genome, cfg: Config, r: ValidationReport) -> None:
    risk = genome.risk
    if not cfg.risk.min_risk_per_trade <= risk.risk_per_trade <= cfg.risk.max_risk_per_trade:
        r.fail(
            f"risk.risk_per_trade vale {risk.risk_per_trade:g} y debe estar entre "
            f"{cfg.risk.min_risk_per_trade:g} y {cfg.risk.max_risk_per_trade:g}"
        )
    if risk.max_holding_bars <= 0:
        r.fail("risk.max_holding_bars debe ser mayor que 0")
    if not 0.0 < risk.max_exposure <= 1.0:
        r.fail("risk.max_exposure debe estar en (0, 1]")
    if risk.max_concurrent_positions < 1:
        r.fail("risk.max_concurrent_positions debe ser al menos 1")
    if risk.cooldown_bars < 0:
        r.fail("risk.cooldown_bars no puede ser negativo")

    if risk.stop.kind is StopKind.NONE:
        r.warn("sin stop: el bot sólo cierra por señal, tiempo o freno de drawdown")
    elif risk.stop.value <= 0:
        r.fail(f"risk.stop.value debe ser positivo con stop {risk.stop.kind}")
    if risk.stop.kind is StopKind.PERCENT and risk.stop.value >= 1.0:
        r.fail("un stop por porcentaje de 100 % o más nunca salta")

    necesita_atr = risk.stop.kind is StopKind.ATR_MULT or risk.sizing is SizingKind.ATR_RISK
    if necesita_atr and not risk.stop.atr_ref:
        r.warn("no hay feature de ATR declarado: el motor usará su ATR por defecto")


def _check_rules_present(genome: Genome, r: ValidationReport) -> None:
    if genome.entry_long is None:
        r.fail("un bot sin entry_long no puede abrir ninguna posición")
    if genome.exit_long is None:
        r.fail("un bot sin exit_long sólo saldría por stop o por tiempo")
    if genome.risk.allow_short:
        if genome.entry_short is None:
            r.fail("risk.allow_short está activo pero no hay entry_short: no abriría cortos")
        if genome.exit_short is None:
            r.fail("risk.allow_short está activo pero no hay exit_short")


def _check_ensemble(genome: Genome, cfg: Config, r: ValidationReport) -> None:
    ensemble = genome.ensemble
    assert ensemble is not None
    con_reglas = [n for n, t in genome.rule_trees() if t is not None]
    if con_reglas:
        r.fail(
            f"una fusión no tiene reglas propias, toma las de sus miembros, y "
            f"este genoma define {', '.join(con_reglas)}"
        )
    if ensemble.depth > cfg.fusion.max_depth:
        r.fail(
            f"la fusión tiene depth {ensemble.depth} y el máximo de anidamiento "
            f"(fusion.max_depth) es {cfg.fusion.max_depth}"
        )
    if not cfg.fusion.min_members <= len(ensemble.members) <= cfg.fusion.max_members:
        r.fail(
            f"la fusión tiene {len(ensemble.members)} miembros y deben ser entre "
            f"{cfg.fusion.min_members} y {cfg.fusion.max_members}"
        )
    if any(w < 0 for w in ensemble.weights):
        r.fail("los pesos de una fusión no pueden ser negativos")
    if not 0.0 < ensemble.threshold <= 1.0:
        r.fail("ensemble.threshold debe estar en (0, 1]")


def validate_genome(
    genome: Genome,
    cfg: Config,
    catalog: GeneCatalog,
    *,
    strict: bool = True,
) -> ValidationReport:
    """Comprueba todas las invariantes estructurales del genoma.

    Verifica ids únicos, referencias que resuelven, indicadores y parámetros
    dentro del catálogo, coherencia de parámetros compuestos, límites de forma,
    comparaciones con sentido, presencia de entrada y salida, riesgo dentro de
    los límites de la configuración, fusiones bien formadas y coherencia del
    filtro de régimen. Ver docs/GENOME.md.

    Args:
        strict: si ``True``, levanta ``GenomeInvalid`` cuando hay errores. Los
            *avisos* nunca levantan: describen genomas mejorables, no rotos.

    Returns:
        El informe. Si ``strict`` y hay errores, no retorna: levanta.
    """
    r = ValidationReport()

    _check_features(genome, cfg, catalog, r)
    _check_references(genome, r)
    _check_shape(genome, cfg, r)
    _check_comparisons(genome, catalog, r)
    _check_risk(genome, cfg, r)

    if genome.regime is not None and genome.regime.enabled and genome.regime.rule is None:
        r.fail("el filtro de régimen está activado pero no tiene regla")

    if genome.is_ensemble:
        _check_ensemble(genome, cfg, r)
    else:
        _check_rules_present(genome, r)

    if strict and not r.ok:
        raise GenomeInvalid(f"{genome.id} no es válido:\n  - " + "\n  - ".join(r.errors))
    return r


# --------------------------------------------------------------------------- #
# Reparación                                                                   #
# --------------------------------------------------------------------------- #


def _repair_features(
    genome: Genome, cfg: Config, catalog: GeneCatalog
) -> tuple[FeatureGene, ...]:
    """Features con kind conocido, ids únicos, parámetros en rango y fuente válida."""
    out: list[FeatureGene] = []
    vistos: set[str] = set()
    for gene in genome.features:
        if gene.kind not in catalog.indicators:
            continue
        s = catalog.indicators[gene.kind]

        nuevo_id = gene.id
        sufijo = 2
        while nuevo_id in vistos:
            nuevo_id = f"{gene.id}__{sufijo}"
            sufijo += 1
        vistos.add(nuevo_id)

        params = s.clamp_params(gene.params)
        if gene.kind == "MACD" and params.get("fast", 0) >= params.get("slow", 1):
            rapido = s.param("fast")
            lento = s.param("slow")
            assert rapido is not None and lento is not None
            params["fast"] = rapido.clamp(min(params["fast"], params["slow"]))
            params["slow"] = lento.clamp(max(params["fast"] + 1, params["slow"]))
            if params["fast"] >= params["slow"]:
                params["fast"], params["slow"] = rapido.low, lento.high

        source = gene.source if gene.source in s.sources else s.sources[0]
        out.append(
            FeatureGene(
                id=nuevo_id, kind=gene.kind, params=params, source=source,
                timeframe=gene.timeframe,
            )
        )
    return tuple(out)


def _substitute_or_drop(
    leaf: Condition, genome: Genome, catalog: GeneCatalog
) -> Condition | None:
    """Arregla una hoja con referencias rotas o escalas incompatibles.

    Primero intenta sustituir la referencia rota por un feature compatible: un
    hijo de un cruce suele traer una regla del otro padre y el feature que le
    falta tiene casi siempre un equivalente. Si no hay ninguno, la hoja se poda.
    """
    conocidos = {g.id: g for g in genome.features}

    def arreglar(operand: Operand | None, pareja: Operand | None) -> Operand | None:
        if operand is None or operand.ref is None or operand.ref in conocidos:
            return operand
        objetivo = _operand_output(pareja, genome)
        candidatos = sorted(conocidos)
        for fid in candidatos:
            salida = spec(conocidos[fid].kind).output
            if objetivo in (None, _CONST) or salida == objetivo:
                return Operand(ref=fid)
        return None

    left = arreglar(leaf.left, leaf.right)
    if left is None:
        return None
    right = arreglar(leaf.right, left) if leaf.right is not None else None
    if leaf.right is not None and right is None:
        return None
    right2 = arreglar(leaf.right2, left) if leaf.right2 is not None else None
    if leaf.right2 is not None and right2 is None:
        return None

    return Condition(op=leaf.op, left=left, right=right, right2=right2, lookback=leaf.lookback)


def _clamp_constants(leaf: Condition, genome: Genome) -> Condition:
    """Mete las constantes dentro del rango del indicador con el que se comparan."""

    def recortar(constante: Operand | None, referencia: Operand | None) -> Operand | None:
        if constante is None or constante.const is None:
            return constante
        value = float(constante.const)
        if leaf.op in _RANK_OPS:
            return Operand(const=min(max(value, 0.0), 1.0))
        rango = _value_range(referencia, genome)
        if rango is None:
            return constante
        return Operand(const=min(max(value, rango[0]), rango[1]))

    return dataclasses.replace(
        leaf,
        left=recortar(leaf.left, leaf.right) or leaf.left,
        right=recortar(leaf.right, leaf.left),
        right2=recortar(leaf.right2, leaf.left),
    )


def _map_leaves(
    node: RuleNode | None, fn: Callable[[Condition], Condition | None]
) -> RuleNode | None:
    """Aplica ``fn`` a cada hoja y simplifica el árbol con lo que sobreviva."""
    if node is None:
        return None
    if isinstance(node, Condition):
        return fn(node)
    hijos = [h for h in (_map_leaves(c, fn) for c in node.children) if h is not None]
    if not hijos:
        return None
    if node.op is LogicOp.NOT:
        return LogicNode(LogicOp.NOT, (hijos[0],))
    return hijos[0] if len(hijos) == 1 else LogicNode(node.op, tuple(hijos))


def _cap_depth(node: RuleNode | None, remaining: int) -> RuleNode | None:
    if node is None or isinstance(node, Condition):
        return node
    if remaining <= 1:
        hojas = rule_leaves(node)
        return hojas[0] if hojas else None
    hijos = [h for h in (_cap_depth(c, remaining - 1) for c in node.children) if h is not None]
    if not hijos:
        return None
    if node.op is LogicOp.NOT:
        return LogicNode(LogicOp.NOT, (hijos[0],))
    return hijos[0] if len(hijos) == 1 else LogicNode(node.op, tuple(hijos))


def _cap_leaves(node: RuleNode | None, budget: list[int]) -> RuleNode | None:
    if node is None:
        return None
    if isinstance(node, Condition):
        if budget[0] <= 0:
            return None
        budget[0] -= 1
        return node
    hijos = [h for h in (_cap_leaves(c, budget) for c in node.children) if h is not None]
    if not hijos:
        return None
    if node.op is LogicOp.NOT:
        return LogicNode(LogicOp.NOT, (hijos[0],))
    return hijos[0] if len(hijos) == 1 else LogicNode(node.op, tuple(hijos))


def _incompatible(leaf: Condition, genome: Genome, catalog: GeneCatalog) -> bool:
    if leaf.op in UNARY_COMPARE_OPS or leaf.op in _RANK_OPS:
        return False
    izquierda = _operand_output(leaf.left, genome)
    derecha = _operand_output(leaf.right, genome)
    if izquierda is None or derecha is None:
        return True
    if izquierda is _CONST and derecha is _CONST:
        return True
    if izquierda is _CONST or derecha is _CONST:
        return False
    if izquierda != derecha:
        return True
    if leaf.left.ref and leaf.right is not None and leaf.right.ref:
        izq = genome.feature(leaf.left.ref)
        der = genome.feature(leaf.right.ref)
        if izq and der:
            return not catalog_is_comparable(catalog, izq.kind, der.kind)
    return False


def _rewrite_trees(
    genome: Genome, fn: Callable[[RuleNode | None], RuleNode | None]
) -> Genome:
    cambios: dict[str, Any] = {
        "entry_long": fn(genome.entry_long),
        "exit_long": fn(genome.exit_long),
        "entry_short": fn(genome.entry_short),
        "exit_short": fn(genome.exit_short),
    }
    if genome.regime is not None:
        # Un régimen activado cuya regla se ha quedado en nada se apaga: es la
        # reparación honesta, porque un filtro sin regla no filtra nada.
        regla = fn(genome.regime.rule) if genome.regime.rule is not None else None
        cambios["regime"] = dataclasses.replace(
            genome.regime, rule=regla, enabled=genome.regime.enabled and regla is not None
        )
    return dataclasses.replace(genome, **cambios)


def repair_genome(genome: Genome, cfg: Config, catalog: GeneCatalog) -> Genome:
    """Intenta arreglar un genoma inválido en vez de descartarlo.

    Los operadores evolutivos producen genomas rotos constantemente (un cruce
    que hereda una regla que referencia un feature del otro padre, por ejemplo).
    Descartarlos sería desperdiciar la mayor parte de la descendencia. Reparar
    es más barato que volver a muestrear.

    En orden: features con kind conocido, ids únicos y parámetros recortados;
    referencias rotas sustituidas por un feature compatible o podadas;
    constantes metidas en su rango; hojas de escala incompatible podadas;
    profundidad y número de hojas recortados; features huérfanos podados; gen de
    riesgo recortado a los límites de ``cfg.risk``.

    Arrastrar features desde los padres —el primer recurso que menciona el
    diseño— lo hace el operador de cruce, que es quien tiene los padres
    delante; aquí ya no están.

    Raises:
        GenomeInvalid: si tras todo esto el genoma no tiene entrada o salida.
    """
    g = dataclasses.replace(genome, features=_repair_features(genome, cfg, catalog))

    if g.is_ensemble:
        ensemble = g.ensemble
        assert ensemble is not None
        g = _rewrite_trees(g, lambda _: None)
        return dataclasses.replace(
            g,
            features=(),
            ensemble=dataclasses.replace(
                ensemble,
                depth=min(ensemble.depth, cfg.fusion.max_depth),
                threshold=min(max(ensemble.threshold, 0.01), 1.0),
            ),
            risk=_repair_risk(g, cfg),
        )

    g = _rewrite_trees(g, lambda t: _map_leaves(t, lambda leaf: _substitute_or_drop(leaf, g, catalog)))
    g = _rewrite_trees(g, lambda t: _map_leaves(t, lambda leaf: _clamp_constants(leaf, g)))
    g = _rewrite_trees(
        g, lambda t: _map_leaves(t, lambda leaf: None if _incompatible(leaf, g, catalog) else leaf)
    )
    g = _rewrite_trees(g, lambda t: _cap_depth(t, cfg.evolution.max_rule_depth))
    g = _rewrite_trees(g, lambda t: _cap_leaves(t, [cfg.evolution.max_rule_leaves]))

    g = _prune_orphans(g)
    g = _cap_features(g, cfg, catalog)
    g = dataclasses.replace(g, risk=_repair_risk(g, cfg))

    if g.entry_long is None or g.exit_long is None:
        raise GenomeInvalid(
            f"{genome.id} es irreparable: tras podar lo roto no queda ni entrada ni salida"
        )
    if g.risk.allow_short and (g.entry_short is None or g.exit_short is None):
        g = dataclasses.replace(g, risk=dataclasses.replace(g.risk, allow_short=False))
    return g


def _prune_orphans(genome: Genome) -> Genome:
    usados = genome.all_referenced_features()
    return dataclasses.replace(
        genome, features=tuple(f for f in genome.features if f.id in usados)
    )


def _cap_features(genome: Genome, cfg: Config, catalog: GeneCatalog) -> Genome:
    """Deja como mucho ``max_features``, empezando por los menos usados."""
    limite = cfg.evolution.max_features
    if len(genome.features) <= limite:
        return genome

    cuenta: dict[str, int] = dict.fromkeys(genome.feature_ids(), 0)
    for _, tree in genome.rule_trees():
        for leaf in rule_leaves(tree):
            for operand in (leaf.left, leaf.right, leaf.right2):
                if operand is not None and operand.ref in cuenta:
                    cuenta[operand.ref] += 1
    if genome.risk.stop.atr_ref in cuenta:
        cuenta[genome.risk.stop.atr_ref] += 100      # el del stop no se toca

    orden = sorted(genome.features, key=lambda f: (-cuenta[f.id], f.id))
    conservados = {f.id for f in orden[:limite]}
    g = dataclasses.replace(
        genome, features=tuple(f for f in genome.features if f.id in conservados)
    )
    g = _rewrite_trees(
        g,
        lambda t: _map_leaves(
            t,
            lambda leaf: leaf
            if all(
                o is None or o.ref is None or o.ref in conservados
                for o in (leaf.left, leaf.right, leaf.right2)
            )
            else None,
        ),
    )
    return _prune_orphans(g)


def _repair_risk(genome: Genome, cfg: Config) -> RiskGene:
    risk = genome.risk
    conocidos = genome.feature_ids()
    stop = risk.stop
    if stop.atr_ref and stop.atr_ref not in conocidos:
        stop = dataclasses.replace(stop, atr_ref=None)
    if stop.kind is not StopKind.NONE and stop.value <= 0:
        stop = dataclasses.replace(stop, value=0.02 if stop.kind is StopKind.PERCENT else 2.0)
    if stop.kind is StopKind.PERCENT and stop.value >= 1.0:
        stop = dataclasses.replace(stop, value=0.5)

    return dataclasses.replace(
        risk,
        stop=stop,
        risk_per_trade=min(
            max(risk.risk_per_trade, cfg.risk.min_risk_per_trade), cfg.risk.max_risk_per_trade
        ),
        max_holding_bars=max(1, risk.max_holding_bars),
        max_exposure=min(max(risk.max_exposure, 0.01), 1.0),
        max_concurrent_positions=max(1, risk.max_concurrent_positions),
        cooldown_bars=max(0, risk.cooldown_bars),
    )


__all__ = ("GenomeInvalid", "ValidationReport", "repair_genome", "validate_genome")
