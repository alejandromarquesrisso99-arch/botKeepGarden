"""Distancia genética entre genomas.

Es la base de la especiación, de la detección de clones y del scatter del
dashboard. Ver docs/GENOME.md §Distancia genética.
"""

from __future__ import annotations

import math
from typing import Mapping, Sequence

from ..types import BotId, CompareOp
from .catalog import GeneCatalog
from .schema import Condition, FeatureGene, Genome, Operand, RiskGene, rule_leaves

#: Rangos con los que se normaliza cada campo del gen de riesgo antes de medir.
#: Son fijos y no vienen de la configuración a propósito: la distancia entre dos
#: genomas no puede cambiar porque alguien toque ``garden.yaml``, o el histórico
#: de especies dejaría de significar lo mismo de una generación a otra.
RISK_SCALES: dict[str, tuple[float, float]] = {
    "risk_per_trade": (0.0, 0.05),
    "max_exposure": (0.0, 1.0),
    "stop_value_atr": (0.0, 8.0),
    "stop_value_pct": (0.0, 0.5),
    "take_profit_value": (0.0, 8.0),
    "trailing_value": (0.0, 8.0),
    "max_holding_bars": (0.0, 1000.0),
    "cooldown_bars": (0.0, 24.0),
}

#: Distancia mínima entre un ensemble y un bot simple. Son organismos de
#: naturaleza distinta —uno tiene reglas y el otro tiene miembros— y meterlos en
#: la misma especie mezclaría cosas que no se pueden comparar.
ENSEMBLE_FLOOR = 0.5

#: Cuánto del término de features pesa *qué mira* el bot frente a *con qué
#: ajustes lo mira*. Ver docs/DECISIONS.md D-019: sin la mitad gruesa, dos
#: cruces de medias con periodos distintos son especies distintas y la
#: especiación deja de hacer lo único que se le pide, que es impedir que el
#: jardín entero acabe siendo variaciones de la misma EMA.
KIND_SHARE = 0.5

#: Operadores que son la imagen espejo el uno del otro. Se normalizan al mismo
#: símbolo porque "la rápida cruza hacia arriba a la lenta" y su contraria son
#: el mismo gen: lo que las separa es el bloque donde viven, y una salida es por
#: construcción el espejo de su entrada en casi todos los genomas sembrados.
_MIRROR: dict[CompareOp, CompareOp] = {
    CompareOp.LT: CompareOp.GT,
    CompareOp.LTE: CompareOp.GTE,
    CompareOp.CROSS_BELOW: CompareOp.CROSS_ABOVE,
    CompareOp.FALLING: CompareOp.RISING,
    CompareOp.PCT_RANK_LT: CompareOp.PCT_RANK_GT,
}


# --------------------------------------------------------------------------- #
# Piezas                                                                       #
# --------------------------------------------------------------------------- #


def _jaccard(a: set[object], b: set[object]) -> float:
    """Distancia de Jaccard. Dos conjuntos vacíos están a distancia 0."""
    if not a and not b:
        return 0.0
    return 1.0 - len(a & b) / len(a | b)


def _binned_feature(gene: FeatureGene, catalog: GeneCatalog) -> tuple[object, ...]:
    """Un feature reducido a su gen: el indicador y sus parámetros en rejilla.

    Discretizar es esencial: EMA(21) y EMA(22) son el mismo gen a efectos
    evolutivos, y sin la rejilla una población entera parecería diversa por
    decimales que el mercado no distingue.
    """
    s = catalog.indicators.get(gene.kind)
    params: list[tuple[str, float]] = []
    for name, value in sorted(gene.params.items()):
        p = s.param(name) if s is not None else None
        params.append((name, p.binned(value) if p is not None else round(float(value), 6)))
    return (gene.kind, str(gene.source), gene.timeframe or "", tuple(params))


def _feature_set(genome: Genome, catalog: GeneCatalog) -> set[object]:
    return {_binned_feature(f, catalog) for f in genome.features}


def _kind_set(genome: Genome) -> set[object]:
    """Qué mira el bot, sin mirar con qué ajustes."""
    return {f.kind for f in genome.features}


def _features_distance(a: Genome, b: Genome, catalog: GeneCatalog) -> float:
    """Distancia entre los sentidos de dos bots.

    Dos términos: *qué* indicadores mira y *cómo* los ha afinado. Con sólo el
    segundo, dos cruces de medias con periodos distintos no comparten nada y
    acaban en especies separadas, que es justo lo contrario de lo que la
    especiación tiene que conseguir.
    """
    grueso = _jaccard(_kind_set(a), _kind_set(b))
    fino = _jaccard(_feature_set(a, catalog), _feature_set(b, catalog))
    return KIND_SHARE * grueso + (1.0 - KIND_SHARE) * fino


def _binned_const(value: float) -> float:
    """Una constante en rejilla, para que mover un umbral se note.

    Sin esto, ``TWEAK_THRESHOLD`` —una de cada cinco mutaciones— produciría
    hijos a distancia exactamente cero de su padre, y el sistema los mataría a
    todos como clones sin haberlos mirado.
    """
    v = float(value)
    return round(v, 3) if abs(v) < 10.0 else round(v, 1)


def _operand_token(operand: Operand | None, genome: Genome) -> str:
    """Con qué se compara, ignorando el id local, que es arbitrario."""
    if operand is None:
        return ""
    if operand.const is not None:
        return f"const:{_binned_const(operand.const)}"
    if operand.price is not None:
        return f"price:{operand.price}"
    gene = genome.feature(operand.ref or "")
    return f"kind:{gene.kind}" if gene is not None else "?"


def _normalized_leaf(leaf: Condition, genome: Genome) -> tuple[object, ...]:
    """``(op, kind_izq, kind_o_const_der)``, con los espejos unificados."""
    return (
        str(_MIRROR.get(leaf.op, leaf.op)),
        _operand_token(leaf.left, genome),
        _operand_token(leaf.right, genome),
    )


def _rule_set(genome: Genome) -> set[object]:
    out: set[object] = set()
    for _, tree in genome.rule_trees():
        for leaf in rule_leaves(tree):
            out.add(_normalized_leaf(leaf, genome))
    return out


def _scaled(value: float, key: str) -> float:
    low, high = RISK_SCALES[key]
    span = high - low if high > low else 1.0
    return min(max((float(value) - low) / span, 0.0), 1.0)


def _risk_numbers(risk: RiskGene) -> tuple[float, ...]:
    """Los campos numéricos del gen de riesgo, cada uno escalado a [0, 1].

    El ``value`` del stop se escala con el rango de su propio tipo: comparar un
    stop del 3 % con uno de tres veces el ATR sería comparar magnitudes
    distintas, y la diferencia de tipo ya la recoge la parte categórica.
    """
    stop_key = "stop_value_pct" if str(risk.stop.kind) == "PERCENT" else "stop_value_atr"
    return (
        _scaled(risk.risk_per_trade, "risk_per_trade"),
        _scaled(risk.max_exposure, "max_exposure"),
        _scaled(risk.stop.value, stop_key),
        _scaled(risk.take_profit.value, "take_profit_value"),
        _scaled(risk.trailing.value, "trailing_value"),
        _scaled(risk.max_holding_bars, "max_holding_bars"),
        _scaled(risk.cooldown_bars, "cooldown_bars"),
        1.0 if risk.allow_short else 0.0,
    )


def _risk_categories(risk: RiskGene) -> tuple[str, ...]:
    return (
        str(risk.sizing),
        str(risk.stop.kind),
        str(risk.take_profit.kind),
        str(risk.trailing.kind),
    )


def _risk_distance(a: RiskGene, b: RiskGene) -> float:
    """Euclídea normalizada entre dos genes de riesgo, en [0, 1]."""
    cuadrados = [(x - y) ** 2 for x, y in zip(_risk_numbers(a), _risk_numbers(b))]
    cuadrados += [
        0.0 if x == y else 1.0
        for x, y in zip(_risk_categories(a), _risk_categories(b))
    ]
    return math.sqrt(sum(cuadrados) / len(cuadrados))


def _ensemble_distance(
    a: Genome,
    b: Genome,
    weights: Mapping[str, float],
    catalog: GeneCatalog,
    members: Mapping[BotId, Genome] | None,
) -> float:
    """Distancia de señal entre dos fusiones: sus miembros y, si se conocen, sus
    genomas. Dos ensembles con miembros distintos pero equivalentes no son dos
    ideas distintas, y sin mirar dentro lo parecerían."""
    ea, eb = a.ensemble, b.ensemble
    assert ea is not None and eb is not None
    por_miembros = _jaccard(set(ea.members), set(eb.members))
    if not members:
        return por_miembros

    distancias: list[float] = []
    for ma in ea.members:
        ga = members.get(ma)
        if ga is None:
            continue
        for mb in eb.members:
            gb = members.get(mb)
            if gb is not None:
                distancias.append(genome_distance(ga, gb, weights, catalog, members=members))
    if not distancias:
        return por_miembros
    return 0.5 * por_miembros + 0.5 * (sum(distancias) / len(distancias))


# --------------------------------------------------------------------------- #
# La distancia                                                                 #
# --------------------------------------------------------------------------- #


def genome_distance(
    a: Genome,
    b: Genome,
    weights: Mapping[str, float],
    catalog: GeneCatalog,
    *,
    members: Mapping[BotId, Genome] | None = None,
) -> float:
    """Distancia en [0, 1]. 0 = mismo organismo, 1 = nada en común.

    Combinación ponderada de cuatro términos (los pesos vienen de
    ``cfg.speciation.distance_weights`` y suman 1):

    * ``features``: Jaccard sobre el conjunto de ``(kind, params discretizados
      con ParamSpec.binned)``. Discretizar es esencial: EMA(21) y EMA(22) son el
      mismo gen a efectos evolutivos.
    * ``rules``: Jaccard sobre las hojas normalizadas de todos los árboles. Una
      hoja se normaliza a ``(op, kind_del_feature_izq, kind_o_const_der)``,
      ignorando los ids locales, que son arbitrarios.
    * ``risk``: distancia euclídea normalizada entre los genes de riesgo, con
      cada campo escalado a [0, 1] por su rango válido.
    * ``family``: 0 si coinciden, 1 si no.

    Para ensembles la señal se mide sobre el conjunto de miembros (Jaccard de
    ``members``), combinado con la distancia media entre los genomas de los
    miembros cuando se pasan en ``members``. Un ensemble y un bot simple siempre
    distan al menos ``ENSEMBLE_FLOOR``.

    Es simétrica y ``genome_distance(a, a) == 0``.
    """
    if a is b or a.id == b.id:
        return 0.0

    w_features = float(weights.get("features", 0.0))
    w_rules = float(weights.get("rules", 0.0))
    w_risk = float(weights.get("risk", 0.0))
    w_family = float(weights.get("family", 0.0))
    total = w_features + w_rules + w_risk + w_family or 1.0

    d_risk = _risk_distance(a.risk, b.risk)
    d_family = 0.0 if a.family is b.family else 1.0

    if a.is_ensemble or b.is_ensemble:
        if a.is_ensemble != b.is_ensemble:
            # Naturalezas distintas: el suelo manda y lo único que puede
            # separarlos todavía más es el riesgo, que un ensemble sí tiene
            # propio, y la familia.
            resto = (w_risk * d_risk + w_family * d_family) / total
            return min(1.0, ENSEMBLE_FLOOR + (1.0 - ENSEMBLE_FLOOR) * resto)
        d_señal = _ensemble_distance(a, b, weights, catalog, members)
        d = ((w_features + w_rules) * d_señal + w_risk * d_risk + w_family * d_family) / total
        return min(max(d, 0.0), 1.0)

    d = (
        w_features * _features_distance(a, b, catalog)
        + w_rules * _jaccard(_rule_set(a), _rule_set(b))
        + w_risk * d_risk
        + w_family * d_family
    ) / total
    return min(max(d, 0.0), 1.0)


def distance_matrix(
    genomes: Sequence[Genome],
    weights: Mapping[str, float],
    catalog: GeneCatalog,
    *,
    members: Mapping[BotId, Genome] | None = None,
) -> list[list[float]]:
    """Matriz simétrica de distancias. Calcula sólo el triángulo superior."""
    n = len(genomes)
    out = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            d = genome_distance(genomes[i], genomes[j], weights, catalog, members=members)
            out[i][j] = out[j][i] = d
    return out


def mean_pairwise_distance(
    genomes: Sequence[Genome],
    weights: Mapping[str, float],
    catalog: GeneCatalog,
    *,
    members: Mapping[BotId, Genome] | None = None,
) -> float:
    """Diversidad genética del jardín: la media del triángulo superior.

    Es la métrica de alerta temprana más importante del sistema: si cae de forma
    sostenida, el jardín está convergiendo y va a dejar de descubrir.
    """
    n = len(genomes)
    if n < 2:
        return 0.0
    suma = 0.0
    for i in range(n):
        for j in range(i + 1, n):
            suma += genome_distance(genomes[i], genomes[j], weights, catalog, members=members)
    return suma / (n * (n - 1) / 2.0)


def nearest_neighbours(
    target: Genome,
    population: Sequence[Genome],
    weights: Mapping[str, float],
    catalog: GeneCatalog,
    k: int = 10,
    *,
    members: Mapping[BotId, Genome] | None = None,
) -> list[tuple[str, float]]:
    """Los ``k`` genomas vivos más cercanos. Alimenta la métrica ``novelty``."""
    distancias = [
        (g.id, genome_distance(target, g, weights, catalog, members=members))
        for g in population
        if g.id != target.id
    ]
    distancias.sort(key=lambda par: (par[1], par[0]))
    return distancias[: max(0, k)]


def novelty(
    target: Genome,
    population: Sequence[Genome],
    weights: Mapping[str, float],
    catalog: GeneCatalog,
    k: int = 10,
    *,
    members: Mapping[BotId, Genome] | None = None,
) -> float:
    """Distancia media a los ``k`` vecinos más cercanos.

    Un bot es novedoso cuando nadie a su alrededor se le parece, no cuando está
    lejos del promedio: medirlo contra los vecinos es lo que hace que un nicho
    pequeño y raro siga contando como novedad aunque el centro del jardín se
    haya movido de sitio.
    """
    vecinos = nearest_neighbours(target, population, weights, catalog, k, members=members)
    if not vecinos:
        return 0.0
    return sum(d for _, d in vecinos) / len(vecinos)


__all__ = (
    "ENSEMBLE_FLOOR",
    "KIND_SHARE",
    "RISK_SCALES",
    "genome_distance",
    "distance_matrix",
    "mean_pairwise_distance",
    "nearest_neighbours",
    "novelty",
)
