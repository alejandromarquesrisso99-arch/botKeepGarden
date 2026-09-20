"""Compilación del genoma a señales.

El genoma es datos; aquí se convierte en una serie temporal de señales sobre un
DataFrame de velas. La compilación es **cerrada**: sólo puede producir
combinaciones del catálogo, nunca código arbitrario.

Convenios que el resto del motor da por supuestos:

* una señal por vela, y las salidas mandan sobre las entradas;
* nada antes del calentamiento: ni una señal;
* un ``NaN`` nunca es verdad — una regla sin datos suficientes no dispara.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

import numpy as np
import pandas as pd

from ..types import (
    RANGE_COMPARE_OPS,
    UNARY_COMPARE_OPS,
    BotId,
    CombineMode,
    CompareOp,
    LogicOp,
    Signal,
    Timeframe,
)
from .schema import Condition, Genome, LogicNode, Operand, RuleNode, rule_leaves
from .validate import GenomeInvalid

#: Ventana del percentil cuando la condición no declara ``lookback``.
#: Con ventana 1 el percentil valdría siempre 1 y la regla sería una constante
#: disfrazada, así que se usa una ventana larga por defecto.
DEFAULT_PCT_RANK_WINDOW = 200

#: Operadores que necesitan la vela anterior.
_CROSS_OPS = frozenset({CompareOp.CROSS_ABOVE, CompareOp.CROSS_BELOW})

#: Operadores que trabajan sobre el percentil del operando izquierdo.
_RANK_OPS = frozenset({CompareOp.PCT_RANK_GT, CompareOp.PCT_RANK_LT})

#: Códigos internos de señal. El motor trabaja con enteros; la enumeración es
#: para las personas.
CODE_TO_SIGNAL: tuple[Signal, ...] = (
    Signal.NONE,
    Signal.ENTER_LONG,
    Signal.EXIT_LONG,
    Signal.ENTER_SHORT,
    Signal.EXIT_SHORT,
)
SIGNAL_TO_CODE: dict[Signal, int] = {s: i for i, s in enumerate(CODE_TO_SIGNAL)}


class FeatureStore(Protocol):
    """Fuente de indicadores ya calculados, compartida por toda la población.

    Si 40 bots piden ``EMA(21)`` sobre el mismo símbolo, se calcula una vez.
    Implementado por ``data.indicators.IndicatorCache``.
    """

    def get(self, kind: str, params: Mapping[str, float], source: str,
            timeframe: Timeframe | None = None) -> np.ndarray: ...

    def warmup_bars(self, kind: str, params: Mapping[str, float],
                    timeframe: Timeframe | None = None) -> int: ...


# --------------------------------------------------------------------------- #
# Evaluación de reglas                                                         #
# --------------------------------------------------------------------------- #


def _shift(x: np.ndarray, k: int = 1) -> np.ndarray:
    """``out[t] = x[t - k]``. Nunca al revés: eso sería mirar al futuro."""
    out = np.full(len(x), np.nan, dtype="float64")
    if 0 < k < len(x):
        out[k:] = x[:-k]
    return out


def _shift_bool(x: np.ndarray, k: int = 1) -> np.ndarray:
    out = np.zeros(len(x), dtype=bool)
    if 0 < k < len(x):
        out[k:] = x[:-k]
    return out


def _operand(
    operand: Operand | None, arrays: Mapping[str, np.ndarray], candles: pd.DataFrame
) -> np.ndarray:
    """Resuelve un operando a una serie alineada con las velas."""
    from ..data.indicators import source_series

    n = len(candles)
    if operand is None:
        return np.full(n, np.nan, dtype="float64")
    if operand.ref is not None:
        try:
            return np.asarray(arrays[operand.ref], dtype="float64")
        except KeyError:
            raise KeyError(
                f"la regla referencia el feature '{operand.ref}', que el genoma no define. "
                f"Definidos: {sorted(arrays)}"
            ) from None
    if operand.price is not None:
        return np.asarray(source_series(candles, operand.price), dtype="float64")
    return np.full(n, float(operand.const or 0.0), dtype="float64")


def _compare(op: CompareOp, left: np.ndarray, right: np.ndarray) -> np.ndarray:
    with np.errstate(invalid="ignore"):
        if op in (CompareOp.GT, CompareOp.PCT_RANK_GT):
            return left > right
        if op is CompareOp.GTE:
            return left >= right
        if op in (CompareOp.LT, CompareOp.PCT_RANK_LT):
            return left < right
        if op is CompareOp.LTE:
            return left <= right
    raise ValueError(f"operador de comparación no soportado: {op}")


def _rank_window(cond: Condition) -> int:
    return cond.lookback if cond.lookback > 1 else DEFAULT_PCT_RANK_WINDOW


def _eval_condition(
    cond: Condition, arrays: Mapping[str, np.ndarray], candles: pd.DataFrame
) -> np.ndarray:
    from ..data.indicators import pct_rank

    left = _operand(cond.left, arrays, candles)
    valid = np.isfinite(left)

    if cond.op in UNARY_COMPARE_OPS:
        previous = _shift(left, max(1, cond.lookback))
        valid &= np.isfinite(previous)
        with np.errstate(invalid="ignore"):
            out = left > previous if cond.op is CompareOp.RISING else left < previous
        return out & valid

    if cond.op in RANGE_COMPARE_OPS:
        low = _operand(cond.right, arrays, candles)
        high = _operand(cond.right2, arrays, candles)
        valid &= np.isfinite(low) & np.isfinite(high)
        with np.errstate(invalid="ignore"):
            out = (left >= low) & (left <= high)
        return out & valid

    right = _operand(cond.right, arrays, candles)

    if cond.op in _RANK_OPS:
        ranked = pct_rank(left, _rank_window(cond))
        valid = np.isfinite(ranked) & np.isfinite(right)
        return _compare(cond.op, ranked, right) & valid

    valid &= np.isfinite(right)

    if cond.op in _CROSS_OPS:
        prev_left = _shift(left)
        prev_right = _shift(right)
        prev_valid = np.isfinite(prev_left) & np.isfinite(prev_right)
        with np.errstate(invalid="ignore"):
            if cond.op is CompareOp.CROSS_ABOVE:
                out = (left > right) & (prev_left <= prev_right)
            else:
                out = (left < right) & (prev_left >= prev_right)
        return out & valid & prev_valid

    return _compare(cond.op, left, right) & valid


def eval_rule(
    node: RuleNode | None,
    arrays: Mapping[str, np.ndarray],
    candles: pd.DataFrame,
) -> np.ndarray:
    """Evalúa un árbol de reglas a una máscara booleana por vela.

    Semántica de los operadores no obvios:

    * ``CROSS_ABOVE``: ``left[t] > right[t] and left[t-1] <= right[t-1]``.
    * ``RISING(lookback=k)``: ``left[t] > left[t-k]``.
    * ``PCT_RANK_GT``: percentil del valor dentro de su ventana > constante.
      La ventana es el ``lookback`` de la condición, o
      ``DEFAULT_PCT_RANK_WINDOW`` si no declara ninguno.
    * ``BETWEEN``: ``right <= left <= right2``.

    Las primeras velas, donde no hay suficiente historia, devuelven ``False``,
    nunca ``NaN`` propagado como verdadero.

    Un árbol ``None`` devuelve todo ``False`` (no hay señal), salvo en el filtro
    de régimen, donde ``None`` significa "sin filtro" y lo gestiona el llamador.
    """
    n = len(candles)
    if node is None:
        return np.zeros(n, dtype=bool)

    if isinstance(node, LogicNode):
        hijos = [eval_rule(child, arrays, candles) for child in node.children]
        if node.op is LogicOp.NOT:
            return ~hijos[0]
        if node.op is LogicOp.AND:
            return np.logical_and.reduce(hijos) if hijos else np.zeros(n, dtype=bool)
        return np.logical_or.reduce(hijos) if hijos else np.zeros(n, dtype=bool)

    return _eval_condition(node, arrays, candles)


# --------------------------------------------------------------------------- #
# Genoma compilado                                                             #
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class CompiledGenome:
    """Genoma listo para evaluar.

    Attributes:
        genome: El genoma de origen.
        warmup_bars: Velas necesarias antes de que las señales sean válidas.
            El motor no deja operar al bot hasta pasarlas.
        feature_arrays: Series de cada feature, alineadas al índice de velas.
        codes: La señal de cada vela como entero. Es lo que consume el motor;
            ``signals()`` es la misma información para leerla con los ojos.
    """

    genome: Genome
    warmup_bars: int
    feature_arrays: dict[str, np.ndarray]
    codes: np.ndarray
    _signals: np.ndarray | None = field(default=None, repr=False)

    def signals(self) -> np.ndarray:
        """Array de ``Signal`` por vela, ya con el filtro de régimen aplicado."""
        if self._signals is None:
            out = np.empty(len(self.codes), dtype=object)
            for code, signal in enumerate(CODE_TO_SIGNAL):
                out[self.codes == code] = signal
            self._signals = out
        return self._signals

    def signal_at(self, i: int) -> Signal:
        """Señal en la vela ``i``. Sólo puede leer datos hasta ``i``."""
        return CODE_TO_SIGNAL[int(self.codes[i])]

    @property
    def n_signals(self) -> int:
        """Cuántas velas traen señal. Un bot con cero nunca operará."""
        return int((self.codes != 0).sum())


# --------------------------------------------------------------------------- #
# Compilación                                                                  #
# --------------------------------------------------------------------------- #


def _rule_lookback(genome: Genome) -> int:
    """Velas extra que las reglas necesitan por detrás de sus features."""
    need = 0
    for _, tree in genome.rule_trees():
        for leaf in rule_leaves(tree):
            if leaf.op in UNARY_COMPARE_OPS:
                need = max(need, leaf.lookback)
            elif leaf.op in _CROSS_OPS:
                need = max(need, 1)
            elif leaf.op in _RANK_OPS:
                need = max(need, _rank_window(leaf))
    return need


def _first_valid(values: np.ndarray) -> int:
    finite = np.isfinite(values)
    return int(np.argmax(finite)) if finite.any() else len(values)


def _resolve_features(
    genome: Genome, candles: pd.DataFrame, features: FeatureStore
) -> tuple[dict[str, np.ndarray], int]:
    """Calcula los features del genoma y cuánto hay que esperar a que sirvan.

    El calentamiento no se cree sólo lo que declara el catálogo: se queda con lo
    peor entre eso y la primera vela en la que el indicador da de verdad un
    número. Un feature de contexto diario arrastra el suyo en velas horarias.
    """
    arrays: dict[str, np.ndarray] = {}
    warmup = 0
    for gene in genome.features:
        values = np.asarray(
            features.get(gene.kind, gene.params, str(gene.source), gene.timeframe),
            dtype="float64",
        )
        if len(values) != len(candles):
            raise GenomeInvalid(
                f"el feature '{gene.id}' ({gene.kind}) devolvió {len(values)} valores "
                f"para {len(candles)} velas"
            )
        arrays[gene.id] = values
        declarado = int(features.warmup_bars(gene.kind, gene.params, gene.timeframe))
        warmup = max(warmup, declarado, _first_valid(values) + 1)
    return arrays, warmup


def _position_from_codes(codes: np.ndarray) -> np.ndarray:
    """Posición deseada (dentro/fuera) a partir de los eventos de un miembro."""
    state = np.full(len(codes), np.nan, dtype="float64")
    state[codes == SIGNAL_TO_CODE[Signal.ENTER_LONG]] = 1.0
    state[codes == SIGNAL_TO_CODE[Signal.EXIT_LONG]] = 0.0
    return pd.Series(state).ffill().fillna(0.0).to_numpy()


def _combine_positions(
    positions: list[np.ndarray], weights: tuple[float, ...], mode: CombineMode, threshold: float
) -> np.ndarray:
    stacked = np.vstack(positions)
    if mode is CombineMode.UNANIMOUS:
        return stacked.min(axis=0) >= 1.0
    if mode is CombineMode.ANY:
        return stacked.max(axis=0) >= 1.0
    if mode is CombineMode.VOTE:
        return stacked.mean(axis=0) > 0.5
    pesos = np.asarray(weights, dtype="float64").reshape(-1, 1)
    return (stacked * pesos).sum(axis=0) >= threshold


def _compile_ensemble(
    genome: Genome,
    candles: pd.DataFrame,
    features: FeatureStore,
    members: Mapping[BotId, Genome] | None,
) -> CompiledGenome:
    ensemble = genome.ensemble
    assert ensemble is not None
    if not members:
        raise GenomeInvalid(
            f"{genome.id} es una fusión de {list(ensemble.members)} y no se han "
            f"pasado los genomas de sus miembros a compile_genome"
        )

    posiciones: list[np.ndarray] = []
    warmup = 0
    for member_id in ensemble.members:
        member = members.get(member_id)
        if member is None:
            raise GenomeInvalid(
                f"{genome.id} es una fusión y falta el genoma del miembro {member_id!r}"
            )
        compilado = compile_genome(member, candles, features, members=members)
        posiciones.append(_position_from_codes(compilado.codes))
        warmup = max(warmup, compilado.warmup_bars)

    dentro = _combine_positions(
        posiciones, ensemble.normalized_weights(), ensemble.combine, ensemble.threshold
    )
    dentro[:warmup] = False

    codes = np.zeros(len(candles), dtype="int8")
    antes = _shift_bool(dentro)
    codes[dentro & ~antes] = SIGNAL_TO_CODE[Signal.ENTER_LONG]
    codes[~dentro & antes] = SIGNAL_TO_CODE[Signal.EXIT_LONG]
    return CompiledGenome(genome=genome, warmup_bars=warmup, feature_arrays={}, codes=codes)


def compile_genome(
    genome: Genome,
    candles: pd.DataFrame,
    features: FeatureStore,
    *,
    members: Mapping[BotId, Genome] | None = None,
) -> CompiledGenome:
    """Compila un genoma contra una serie de velas.

    Pasos:

    1. Resolver cada ``FeatureGene`` contra el ``FeatureStore``. Los features
       con ``timeframe`` distinto del operativo se alinean **hacia atrás**:
       en la vela de 1h de las 13:00, la diaria disponible es la de ayer. Un
       ``reindex(method="ffill")`` con el cierre del timeframe superior
       desplazado una barra. Esto es la fuente número uno de look-ahead: hay un
       test dedicado.
    2. Calcular ``warmup_bars`` como el máximo de los calentamientos de sus
       features (más el ``lookback`` máximo de las reglas).
    3. Evaluar los árboles de forma vectorizada produciendo máscaras booleanas.
    4. Componer las señales con prioridad: salidas antes que entradas; si el
       filtro de régimen está activo y es falso, no hay entrada (las salidas
       siempre se permiten: un régimen adverso no debe atrapar una posición).

    Para un ensemble, ``compile_genome`` compila cada miembro y combina sus
    señales según ``ensemble.combine``. La combinación se hace sobre la señal
    *deseada de posición* (dentro/fuera), no sobre eventos, porque los eventos
    de entrada de miembros distintos rara vez coinciden en la misma vela. Los
    genomas de los miembros se pasan en ``members``.

    Raises:
        GenomeInvalid: si el genoma no se puede compilar.
    """
    if genome.is_ensemble:
        return _compile_ensemble(genome, candles, features, members)

    arrays, warmup = _resolve_features(genome, candles, features)
    warmup = min(len(candles), warmup + _rule_lookback(genome))

    regime = genome.regime
    if regime is not None and regime.enabled and regime.rule is not None:
        permitido = eval_rule(regime.rule, arrays, candles)
    else:
        permitido = np.ones(len(candles), dtype=bool)

    entrar_largo = eval_rule(genome.entry_long, arrays, candles) & permitido
    salir_largo = eval_rule(genome.exit_long, arrays, candles)
    if genome.risk.allow_short:
        entrar_corto = eval_rule(genome.entry_short, arrays, candles) & permitido
        salir_corto = eval_rule(genome.exit_short, arrays, candles)
    else:
        entrar_corto = salir_corto = np.zeros(len(candles), dtype=bool)

    # El orden de las asignaciones ES la prioridad: lo último escrito gana, y
    # salir siempre gana a entrar.
    codes = np.zeros(len(candles), dtype="int8")
    codes[entrar_corto] = SIGNAL_TO_CODE[Signal.ENTER_SHORT]
    codes[entrar_largo] = SIGNAL_TO_CODE[Signal.ENTER_LONG]
    codes[salir_corto] = SIGNAL_TO_CODE[Signal.EXIT_SHORT]
    codes[salir_largo] = SIGNAL_TO_CODE[Signal.EXIT_LONG]
    codes[:warmup] = 0

    return CompiledGenome(
        genome=genome, warmup_bars=warmup, feature_arrays=arrays, codes=codes
    )


__all__ = (
    "CODE_TO_SIGNAL",
    "DEFAULT_PCT_RANK_WINDOW",
    "SIGNAL_TO_CODE",
    "CompiledGenome",
    "FeatureStore",
    "compile_genome",
    "eval_rule",
)
