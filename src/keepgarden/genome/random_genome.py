"""Muestreo de genomas nuevos: el operador SEED.

Un muestreo uniforme sobre el catálogo genera basura el 99 % de las veces. Este
módulo muestrea **sesgado por familia**, usando las plantillas de
``config/genes.yaml``, de forma que un bot recién sembrado al menos tenga la
forma de una idea con sentido.

El sesgo tiene tres piezas:

* **qué mira**: los pesos por familia eligen los indicadores;
* **hacia dónde mira**: cada familia tiene una *postura* (comprar fuerza,
  comprar debilidad, o esperar a que la volatilidad se comprima) que decide el
  sentido de todas las comparaciones;
* **cómo sale**: la salida es la imagen espejo de la entrada, o condiciones
  propias, mitad y mitad.

Todo lo aleatorio pasa por el ``Random`` inyectado: misma semilla, mismo jardín.
"""

from __future__ import annotations

import dataclasses
import itertools
import math
import random
from collections.abc import Sequence

from ..config import Config
from ..ids import new_bot_id, new_genome_id, new_lineage_id
from ..types import (
    BreedOperator,
    CompareOp,
    IdeaFamily,
    LogicOp,
    PriceField,
    SizingKind,
    StopKind,
    TakeProfitKind,
    TrailingKind,
)
from .catalog import SEEDABLE_FAMILIES, GeneCatalog, IndicatorSpec, ParamSpec, spec
from .schema import (
    Condition,
    FeatureGene,
    Genome,
    GenomeMeta,
    LogicNode,
    MarketSpec,
    Operand,
    RegimeGene,
    RiskGene,
    RuleNode,
    StopGene,
    TakeProfitGene,
    TrailingGene,
    genome_hash,
)
from .validate import GenomeInvalid, repair_genome, validate_genome

#: Hacia dónde mira cada familia al entrar.
#:
#: ``up`` compra fuerza (la señal salta cuando el indicador está alto o sube),
#: ``down`` compra debilidad, ``compression`` espera a que la volatilidad se
#: encoja. La salida es siempre la postura contraria.
STANCE: dict[IdeaFamily, str] = {
    IdeaFamily.TREND: "up",
    IdeaFamily.MOMENTUM: "up",
    IdeaFamily.BREAKOUT: "up",
    IdeaFamily.MICROSTRUCTURE: "up",
    IdeaFamily.MEAN_REVERSION: "down",
    IdeaFamily.VOLATILITY: "compression",
    IdeaFamily.HYBRID: "up",
}

#: Ventanas admitidas para los percentiles de las reglas.
RANK_WINDOWS: tuple[int, ...] = (50, 100, 200, 400)

#: Cuántas velas atrás miran RISING y FALLING.
TREND_LOOKBACKS: tuple[int, ...] = (1, 2, 3, 6, 12, 24)

#: Indicadores que sirven de filtro de régimen, por orden de preferencia.
REGIME_KINDS: tuple[str, ...] = ("ADX", "ATR_PCT", "REALIZED_VOL", "BB_WIDTH", "SLOPE")

_MOVING_AVERAGES = frozenset({"SMA", "EMA", "WMA"})

#: Polaridad de los indicadores de rango: un techo (+1) es lo que el precio
#: rompe al subir y un suelo (-1) lo que rompe al caer. Mezclarlos produce
#: reglas ciertas casi siempre —"el precio está por encima del mínimo de las
#: últimas 80 velas"— que parecen condiciones y no lo son.
POLARITY: dict[str, int] = {
    "DONCHIAN_HIGH": 1,
    "HIGHEST": 1,
    "DONCHIAN_LOW": -1,
    "LOWEST": -1,
}


# --------------------------------------------------------------------------- #
# Muestreo de parámetros                                                       #
# --------------------------------------------------------------------------- #


def _sample_param(p: ParamSpec, rng: random.Random) -> float:
    """Un valor dentro del rango del parámetro.

    Los periodos se muestrean log-uniformemente: entre 5 y 400 velas, la mitad
    uniforme caería casi siempre en periodos largos y el jardín nacería sin
    ninguna estrategia rápida.
    """
    if p.integer and p.high / max(p.low, 1.0) >= 8.0:
        value = math.exp(rng.uniform(math.log(max(p.low, 1.0)), math.log(p.high)))
        return p.clamp(round(value))
    if p.integer:
        return p.clamp(rng.randint(int(p.low), int(p.high)))
    return p.clamp(rng.uniform(p.low, p.high))


def _sample_line(s: IndicatorSpec, rng: random.Random, stance: str) -> float:
    """Qué línea de un indicador multi-salida mira el feature.

    En unas bandas, la postura decide: quien compra fuerza mira la banda
    superior y quien compra debilidad, la inferior. Mirar una banda al azar
    produce bots que dicen lo contrario de lo que su familia pretende.
    """
    if s.multi_output == ("upper", "middle", "lower"):
        if stance == "up":
            return 0.0
        return 2.0 if stance == "down" else float(rng.choice([0, 2]))
    return float(rng.choices([0, 1, 2], weights=[5, 1, 2])[0])


def _sample_params(s: IndicatorSpec, rng: random.Random, stance: str) -> dict[str, float]:
    params = {p.name: _sample_param(p, rng) for p in s.params}
    if "line" in params:
        line = s.param("line")
        assert line is not None
        params["line"] = line.clamp(_sample_line(s, rng, stance))
    if s.kind == "MACD":
        fast, slow = s.param("fast"), s.param("slow")
        assert fast is not None and slow is not None
        params["fast"] = fast.clamp(min(params["fast"], params["slow"]))
        params["slow"] = slow.clamp(max(params["fast"] * 2.0, params["slow"]))
        if params["fast"] >= params["slow"]:
            params["fast"], params["slow"] = fast.low, slow.high
    return params


# --------------------------------------------------------------------------- #
# Muestreo de features                                                         #
# --------------------------------------------------------------------------- #


def _weighted_kind(
    family: IdeaFamily, catalog: GeneCatalog, rng: random.Random, candidatos: Sequence[str]
) -> str:
    pesos = [catalog.weight(family, k) for k in candidatos]
    return rng.choices(list(candidatos), weights=pesos)[0]


def _sample_kinds(
    family: IdeaFamily, n: int, catalog: GeneCatalog, rng: random.Random
) -> list[str]:
    """Qué indicadores mira el bot, garantizando las categorías obligatorias."""
    tpl = catalog.templates[family]
    disponibles = catalog.kinds_for(family)
    kinds: list[str] = []

    for categoria in tpl.required_categories:
        candidatos = [k for k in disponibles if catalog.indicators[k].category == categoria]
        if candidatos:
            kinds.append(_weighted_kind(family, catalog, rng, candidatos))

    while len(kinds) < n:
        # Una media móvil sola no dice nada; dos hacen un cruce, que es la forma
        # más antigua y más sólida de expresar una tendencia.
        medias = [k for k in kinds if k in _MOVING_AVERAGES and kinds.count(k) == 1]
        if medias and rng.random() < 0.6:
            kinds.append(medias[0])
        else:
            kinds.append(_weighted_kind(family, catalog, rng, disponibles))

    return kinds[: max(n, len(tpl.required_categories))]


def _build_features(
    kinds: Sequence[str], rng: random.Random, stance: str
) -> tuple[FeatureGene, ...]:
    """Convierte una lista de indicadores en features con id único.

    Cuando un indicador aparece repetido, sus periodos se ordenan de menor a
    mayor: así ``ema_1`` es siempre la rápida y ``ema_2`` la lenta, y el
    muestreo de reglas puede construir un cruce sin volver a mirar los números.
    """
    por_kind: dict[str, list[dict[str, float]]] = {}
    for kind in kinds:
        por_kind.setdefault(kind, []).append(_sample_params(spec(kind), rng, stance))

    features: list[FeatureGene] = []
    for kind, grupo in por_kind.items():
        s = spec(kind)
        periodo = s.param("period")
        if len(grupo) > 1 and periodo is not None:
            grupo.sort(key=lambda p: p["period"])
            # Dos medias con el mismo periodo son la misma media: el cruce no
            # ocurriría jamás. Se separan al menos un 60 %.
            for anterior, siguiente in itertools.pairwise(grupo):
                minimo = anterior["period"] * 1.6
                if siguiente["period"] < minimo:
                    siguiente["period"] = periodo.clamp(minimo)
        for i, params in enumerate(grupo, start=1):
            source = rng.choice(list(s.sources)) if len(s.sources) > 1 else s.sources[0]
            features.append(
                FeatureGene(
                    id=f"{kind.lower()}_{i}", kind=kind, params=params, source=source
                )
            )
    return tuple(features)


# --------------------------------------------------------------------------- #
# Muestreo de reglas                                                           #
# --------------------------------------------------------------------------- #


def _partner(gene: FeatureGene, features: Sequence[FeatureGene]) -> FeatureGene | None:
    """La otra copia del mismo indicador, si la hay."""
    return next((f for f in features if f.kind == gene.kind and f.id != gene.id), None)


def _const_in(rango: tuple[float, float], rng: random.Random, upward: bool) -> float:
    """Una constante en la mitad alta o baja del rango del indicador.

    Sin pasarse hacia los extremos: un umbral en el 90 % del rango describe una
    condición que el mercado cumple una vez al año, y un bot así no llega a
    operar lo suficiente como para que nadie pueda juzgarlo.
    """
    low, high = rango
    span = high - low
    frac = rng.uniform(0.50, 0.78) if upward else rng.uniform(0.22, 0.50)
    return round(low + frac * span, 6)


def _leaf(
    gene: FeatureGene, features: Sequence[FeatureGene], rng: random.Random, *, upward: bool
) -> Condition:
    """Una condición con sentido sobre un feature, en el sentido pedido."""
    s = spec(gene.kind)
    opciones: list[tuple[str, float]] = [("rank", 2.0), ("trend", 1.0)]
    compañera = _partner(gene, features)
    if s.output == "price":
        # "el precio está por encima del mínimo de las últimas 80 velas" es
        # cierto casi siempre: comparar el precio contra un suelo sólo dice algo
        # cuando se rompe hacia abajo, y contra un techo, hacia arriba.
        if POLARITY.get(gene.kind, 0) in (0, 1 if upward else -1):
            opciones.append(("price_cmp", 3.0))
        if compañera is not None:
            opciones.append(("cross", 5.0))
    if s.value_range is not None:
        # Un umbral absoluto sólo significa algo si el indicador tiene una
        # escala fija. Sobre un ratio sin techo (el volumen relativo, por
        # ejemplo) el percentil dice lo mismo sin depender de la época.
        opciones.append(("const", {"bounded": 4.0, "unit": 2.5, "signed": 2.5}.get(s.output, 0.5)))
    if s.output == "unbounded":
        opciones = [(o, w * 2.0 if o == "rank" else w) for o, w in opciones]

    forma = rng.choices([o for o, _ in opciones], weights=[w for _, w in opciones])[0]

    if forma == "cross" and compañera is not None:
        rapida, lenta = sorted(
            (gene, compañera), key=lambda f: f.params.get("period", 0.0)
        )
        return Condition(
            CompareOp.CROSS_ABOVE if upward else CompareOp.CROSS_BELOW,
            Operand(ref=rapida.id),
            Operand(ref=lenta.id),
        )

    if forma == "price_cmp":
        return Condition(
            CompareOp.GT if upward else CompareOp.LT,
            Operand(price=PriceField.CLOSE),
            Operand(ref=gene.id),
        )

    if forma == "const" and s.value_range is not None:
        return Condition(
            CompareOp.GT if upward else CompareOp.LT,
            Operand(ref=gene.id),
            Operand(const=_const_in(s.value_range, rng, upward)),
        )

    if forma == "rank":
        umbral = rng.uniform(0.70, 0.95) if upward else rng.uniform(0.05, 0.30)
        return Condition(
            CompareOp.PCT_RANK_GT if upward else CompareOp.PCT_RANK_LT,
            Operand(ref=gene.id),
            Operand(const=round(umbral, 4)),
            lookback=rng.choice(RANK_WINDOWS),
        )

    return Condition(
        CompareOp.RISING if upward else CompareOp.FALLING,
        Operand(ref=gene.id),
        lookback=rng.choice(TREND_LOOKBACKS),
    )


def _mirror(node: RuleNode) -> RuleNode:
    """La imagen espejo de una regla: la misma idea al revés."""
    opuesto = {
        CompareOp.GT: CompareOp.LT,
        CompareOp.LT: CompareOp.GT,
        CompareOp.GTE: CompareOp.LTE,
        CompareOp.LTE: CompareOp.GTE,
        CompareOp.CROSS_ABOVE: CompareOp.CROSS_BELOW,
        CompareOp.CROSS_BELOW: CompareOp.CROSS_ABOVE,
        CompareOp.RISING: CompareOp.FALLING,
        CompareOp.FALLING: CompareOp.RISING,
        CompareOp.PCT_RANK_GT: CompareOp.PCT_RANK_LT,
        CompareOp.PCT_RANK_LT: CompareOp.PCT_RANK_GT,
    }
    if isinstance(node, Condition):
        return dataclasses.replace(node, op=opuesto.get(node.op, node.op))
    # De Morgan: lo contrario de "todo esto a la vez" es "algo de esto falla".
    contrario = LogicOp.OR if node.op is LogicOp.AND else LogicOp.AND
    return LogicNode(contrario, tuple(_mirror(c) for c in node.children))


def _combine(hojas: Sequence[Condition], op: LogicOp) -> RuleNode:
    return hojas[0] if len(hojas) == 1 else LogicNode(op, tuple(hojas))


def _sample_sin_repetir(
    opciones: list[FeatureGene], pesos: list[float], k: int, rng: random.Random
) -> list[FeatureGene]:
    """``rng.choices`` sin repetición: muestrea con pesos y va descartando."""
    elegidos: list[FeatureGene] = []
    restantes = list(zip(opciones, pesos))
    for _ in range(min(k, len(restantes))):
        gene = rng.choices([o for o, _ in restantes], weights=[w for _, w in restantes])[0]
        elegidos.append(gene)
        restantes = [(o, w) for o, w in restantes if o.id != gene.id]
    return elegidos


def random_rule(
    features: Sequence[FeatureGene],
    catalog: GeneCatalog,
    cfg: Config,
    rng: random.Random,
    *,
    max_leaves: int = 3,
    bias: str = "entry",
    upward: bool = True,
) -> RuleNode:
    """Muestrea un árbol de reglas con sentido sobre los features dados.

    Recibe los ``FeatureGene`` completos y no sólo sus ids: sin saber qué
    indicador hay detrás no se puede decidir con qué tiene sentido compararlo,
    que es justo lo que separa esto de un generador de ruido.

    Las entradas se combinan con ``AND`` (hay que cumplirlo todo) y las salidas
    con ``OR`` (basta con que algo falle): un bot al que cuesta salir se queda
    atrapado en cualquier vuelta del mercado.
    """
    if not features:
        raise GenomeInvalid("no hay features con los que construir una regla")
    n = max(1, min(max_leaves, cfg.evolution.max_rule_leaves, len(features)))
    deseada = 1 if upward else -1
    pesos = [1.0 if POLARITY.get(f.kind, 0) in (0, deseada) else 0.1 for f in features]
    elegidos = _sample_sin_repetir(list(features), pesos, rng.randint(1, n), rng)
    hojas = [_leaf(g, features, rng, upward=upward) for g in elegidos]
    return _combine(hojas, LogicOp.AND if bias == "entry" else LogicOp.OR)


# --------------------------------------------------------------------------- #
# Gen de riesgo                                                                #
# --------------------------------------------------------------------------- #


def _rango(block: dict | None, clave: str, por_defecto: tuple[float, float]) -> tuple[float, float]:
    if not block or clave not in block:
        return por_defecto
    value = block[clave]
    if isinstance(value, (int, float)):
        return (float(value), float(value))
    return (float(value[0]), float(value[1]))


def _uniforme(rng: random.Random, rango: tuple[float, float], *, decimales: int = 4) -> float:
    return round(rng.uniform(*rango), decimales)


def random_risk_gene(
    family: IdeaFamily, cfg: Config, catalog: GeneCatalog, rng: random.Random
) -> RiskGene:
    """Muestrea un gen de riesgo desde los priores de la familia.

    Los priores de ``config/genes.yaml`` son puntos de partida, no jaulas: la
    evolución los mueve después dentro de los límites de ``garden.yaml``. Si no
    hay priores (el catálogo de código no trae ninguno), se usan unos valores
    sensatos y se sigue adelante: el jardín tiene que poder sembrar sin
    configuración.
    """
    prior = catalog.risk_priors.get(family, {})

    stop_block = prior.get("stop") or {"kind": "ATR_MULT", "value": [1.5, 3.5]}
    stop_kind = StopKind(str(stop_block.get("kind", "ATR_MULT")))
    stop_value = (
        0.0 if stop_kind is StopKind.NONE
        else _uniforme(rng, _rango(stop_block, "value", (1.5, 3.5)))
    )

    tp_block = prior.get("take_profit") or {"kind": "NONE"}
    tp_kind = TakeProfitKind(str(tp_block.get("kind", "NONE")))
    tp_value = (
        0.0 if tp_kind is TakeProfitKind.NONE
        else _uniforme(rng, _rango(tp_block, "value", (1.5, 3.0)))
    )

    trail_block = prior.get("trailing") or {"kind": "NONE"}
    trail_kind = TrailingKind(str(trail_block.get("kind", "NONE")))
    trail_value = (
        0.0 if trail_kind is TrailingKind.NONE
        else _uniforme(rng, _rango(trail_block, "value", (2.0, 4.0)))
    )
    trail_r = (
        0.0 if trail_kind is TrailingKind.NONE
        else _uniforme(rng, _rango(trail_block, "activate_at_r", (0.5, 1.5)), decimales=2)
    )

    holding_low, holding_high = _rango(prior, "max_holding_bars", (120.0, 720.0))

    return RiskGene(
        sizing=SizingKind.ATR_RISK if stop_kind is StopKind.ATR_MULT else SizingKind.FIXED_FRACTION,
        risk_per_trade=round(
            rng.uniform(cfg.risk.min_risk_per_trade, cfg.risk.max_risk_per_trade), 5
        ),
        max_concurrent_positions=1,
        max_exposure=0.95,
        stop=StopGene(kind=stop_kind, value=stop_value),
        take_profit=TakeProfitGene(kind=tp_kind, value=tp_value),
        trailing=TrailingGene(kind=trail_kind, value=trail_value, activate_at_r=trail_r),
        max_holding_bars=int(rng.uniform(holding_low, holding_high)),
        cooldown_bars=rng.choice([0, 0, 0, 1, 2, 4]),
        allow_short=False,          # spot: no hay cortos que valgan
    )


# --------------------------------------------------------------------------- #
# El genoma                                                                    #
# --------------------------------------------------------------------------- #


def _ensure_atr(
    features: tuple[FeatureGene, ...], risk: RiskGene, rng: random.Random
) -> tuple[tuple[FeatureGene, ...], RiskGene]:
    """Si el riesgo se mide en ATR, el genoma necesita un ATR al que apuntar."""
    if risk.stop.kind is not StopKind.ATR_MULT and risk.sizing is not SizingKind.ATR_RISK:
        return features, risk
    existente = next((f for f in features if f.kind == "ATR"), None)
    if existente is None:
        params = _sample_params(spec("ATR"), rng, "up")
        existente = FeatureGene("atr_stop", "ATR", params, spec("ATR").sources[0])
        features = (*features, existente)
    return features, dataclasses.replace(
        risk, stop=dataclasses.replace(risk.stop, atr_ref=existente.id)
    )


def _regime_rule(
    features: Sequence[FeatureGene], rng: random.Random, *, upward: bool
) -> RuleNode | None:
    """Filtro de régimen sobre el indicador más apropiado que tenga el genoma."""
    candidatos = [f for f in features if f.kind in REGIME_KINDS]
    if not candidatos:
        return None
    preferencia = {k: i for i, k in enumerate(REGIME_KINDS)}
    gene = min(candidatos, key=lambda f: preferencia[f.kind])
    s = spec(gene.kind)
    if s.value_range is None:
        return Condition(
            CompareOp.PCT_RANK_GT if upward else CompareOp.PCT_RANK_LT,
            Operand(ref=gene.id),
            Operand(const=round(rng.uniform(0.5, 0.8) if upward else rng.uniform(0.2, 0.5), 3)),
            lookback=rng.choice(RANK_WINDOWS),
        )
    return Condition(
        CompareOp.GT if upward else CompareOp.LT,
        Operand(ref=gene.id),
        Operand(const=_const_in(s.value_range, rng, upward)),
    )


def _cover_unused(
    features: Sequence[FeatureGene],
    entry: RuleNode,
    exit_: RuleNode,
    regime: RuleNode | None,
    used: set[str],
    cfg: Config,
    rng: random.Random,
    *,
    upward: bool,
) -> RuleNode:
    """Mete en la salida los features que no ha usado nadie.

    Un feature huérfano lo podaría la reparación, y el bot nacería con menos
    sentidos de los que la plantilla de su familia pedía. Se cuelgan de la
    salida porque allí suman con ``OR`` y no hacen la entrada más difícil.
    """
    from .schema import rule_leaves

    sueltos = [f for f in features if f.id not in used]
    if not sueltos:
        return exit_
    hojas = list(rule_leaves(exit_))
    for gene in sueltos:
        if len(hojas) >= cfg.evolution.max_rule_leaves:
            break
        hojas.append(_leaf(gene, features, rng, upward=not upward))
    return _combine(hojas, LogicOp.OR)


def random_genome(
    family: IdeaFamily,
    market: MarketSpec,
    cfg: Config,
    catalog: GeneCatalog,
    rng: random.Random,
) -> Genome:
    """Muestrea un genoma nuevo de la familia dada.

    Elige cuántos indicadores mira y cuáles (con los pesos de la familia y sus
    categorías obligatorias), muestrea sus parámetros, construye la entrada en
    el sentido que marca la postura de la familia, la salida como imagen espejo
    o como condiciones propias, añade el filtro de régimen con la probabilidad
    de la plantilla y remata con un gen de riesgo sacado de los priores.

    Al final valida y repara. Si el genoma sale irreparable se reintenta hasta
    diez veces: es mucho más barato volver a muestrear que aflojar los límites.

    Todo lo aleatorio pasa por ``rng``: con la misma semilla, el mismo genoma.
    """
    from .schema import referenced_features

    tpl = catalog.templates[family]
    stance = STANCE.get(family, "up")
    entra_arriba = stance == "up"

    ultimo_error: Exception | None = None
    for _ in range(10):
        # La horquilla de la plantilla cuenta los indicadores que miran las
        # reglas. El ATR del stop y el filtro de régimen son aparte, y por eso
        # se les reservan dos huecos contra `max_features`.
        low, high = tpl.n_features
        techo = max(int(low), min(int(high), cfg.evolution.max_features - 2))
        n = rng.randint(int(low), techo)

        risk = random_risk_gene(family, cfg, catalog, rng)
        kinds = _sample_kinds(family, n, catalog, rng)

        quiere_regimen = rng.random() < tpl.prefers_regime
        if quiere_regimen and not any(k in REGIME_KINDS for k in kinds):
            kinds.append(_weighted_kind(family, catalog, rng, REGIME_KINDS))

        features = _build_features(kinds, rng, stance)
        features, risk = _ensure_atr(features, risk, rng)

        rule_features = [f for f in features if f.id != risk.stop.atr_ref] or list(features)

        entry = random_rule(
            rule_features, catalog, cfg, rng, max_leaves=2, bias="entry", upward=entra_arriba
        )
        if rng.random() < 0.5:
            exit_ = _mirror(entry)
        else:
            exit_ = random_rule(
                rule_features, catalog, cfg, rng, max_leaves=2, bias="exit",
                upward=not entra_arriba,
            )

        regime = None
        if quiere_regimen:
            regla = _regime_rule(features, rng, upward=entra_arriba)
            if regla is not None:
                regime = RegimeGene(enabled=True, rule=regla)

        usados = referenced_features(entry) | referenced_features(exit_)
        if regime is not None:
            usados |= referenced_features(regime.rule)
        if risk.stop.atr_ref:
            usados.add(risk.stop.atr_ref)
        exit_ = _cover_unused(
            features, entry, exit_, regime.rule if regime else None, usados, cfg, rng,
            upward=entra_arriba,
        )

        bot_id = new_bot_id(f"seed:{rng.getrandbits(64):016x}")
        genome = Genome(
            id=new_genome_id(bot_id),
            family=family,
            market=market,
            features=features,
            entry_long=entry,
            exit_long=exit_,
            risk=risk,
            regime=regime,
            meta=GenomeMeta(
                generation=0,
                operator=BreedOperator.SEED,
                root_lineage=new_lineage_id(bot_id, family.value),
            ),
        )

        try:
            genome = repair_genome(genome, cfg, catalog)
            validate_genome(genome, cfg, catalog, strict=True)
        except GenomeInvalid as exc:
            ultimo_error = exc
            continue
        return genome

    raise GenomeInvalid(
        f"no se ha conseguido sembrar un genoma válido de la familia {family} "
        f"en 10 intentos: {ultimo_error}"
    )


def random_population(
    size: int,
    market: MarketSpec,
    cfg: Config,
    catalog: GeneCatalog,
    rng: random.Random,
    *,
    families: tuple[IdeaFamily, ...] | None = None,
) -> list[Genome]:
    """Muestrea una población inicial equilibrada.

    Reparte ``size`` entre las familias sembrables de forma aproximadamente
    uniforme (ninguna puede superar ``cfg.evolution.max_family_share``), y
    rechaza genomas cuyo hash ya esté en la población: una población inicial con
    clones arranca con menos diversidad de la que aparenta.
    """
    elegibles = tuple(families or SEEDABLE_FAMILIES)
    if not elegibles or size <= 0:
        return []

    tope = (
        size
        if len(elegibles) == 1
        else max(1, math.ceil(size * cfg.evolution.max_family_share))
    )
    cuenta = dict.fromkeys(elegibles, 0)
    poblacion: list[Genome] = []
    hashes: set[str] = set()

    intentos = 0
    limite = max(200, size * 50)
    while len(poblacion) < size and intentos < limite:
        intentos += 1
        disponibles = [f for f in elegibles if cuenta[f] < tope]
        if not disponibles:
            break
        family = min(disponibles, key=lambda f: (cuenta[f], elegibles.index(f)))
        try:
            genome = random_genome(family, market, cfg, catalog, rng)
        except GenomeInvalid:
            continue
        huella = genome_hash(genome)
        if huella in hashes:
            continue
        hashes.add(huella)
        poblacion.append(genome)
        cuenta[family] += 1

    return poblacion


__all__ = (
    "RANK_WINDOWS",
    "REGIME_KINDS",
    "STANCE",
    "TREND_LOOKBACKS",
    "random_genome",
    "random_population",
    "random_risk_gene",
    "random_rule",
)
