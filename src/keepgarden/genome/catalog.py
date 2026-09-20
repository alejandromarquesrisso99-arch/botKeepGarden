"""Catálogo de genes: qué indicadores existen y qué parámetros admiten.

Separación de responsabilidades:

* **Este módulo** es la fuente de verdad de *qué se puede hacer*: los
  indicadores implementados, sus parámetros, sus rangos válidos, su periodo de
  calentamiento y su categoría. Ampliarlo requiere implementar el indicador en
  ``data/indicators.py``.
* **``config/genes.yaml``** es la fuente de verdad de *qué se prefiere*: qué
  indicadores muestrea cada familia de ideas y con qué peso. Se puede tocar sin
  escribir código, y es lo que el jardinero ajusta con ``REBALANCE_QUOTAS``.

Una mutación nunca puede producir un genoma fuera de este catálogo.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Mapping

from ..types import IdeaFamily, PriceField

Category = Literal["trend", "oscillator", "volatility", "range", "volume", "derived"]

#: Naturaleza de la salida de un indicador. Determina con qué se le puede
#: comparar: un indicador ``price`` se compara con el precio o con otro
#: ``price``; un ``unit`` (0..1) con constantes en ese rango; un ``bounded``
#: (p.ej. RSI 0..100) con constantes en su rango declarado.
OutputKind = Literal["price", "bounded", "unit", "unbounded", "signed"]


@dataclass(frozen=True, slots=True)
class ParamSpec:
    """Rango válido de un parámetro de indicador."""

    name: str
    low: float
    high: float
    integer: bool = True
    #: Paso de rejilla para discretizar al comparar genomas (distancia genética).
    bin_size: float = 1.0

    def clamp(self, value: float) -> float:
        v = min(max(float(value), self.low), self.high)
        return float(int(round(v))) if self.integer else round(v, 6)

    def binned(self, value: float) -> float:
        """Valor discretizado, para detectar genomas equivalentes."""
        return round(round(value / self.bin_size) * self.bin_size, 6)


@dataclass(frozen=True, slots=True)
class IndicatorSpec:
    """Definición de un indicador del catálogo."""

    kind: str
    category: Category
    output: OutputKind
    params: tuple[ParamSpec, ...] = ()
    #: Campos de vela admitidos como ``source``.
    sources: tuple[PriceField, ...] = (PriceField.CLOSE,)
    #: Rango de valores típico, usado para muestrear constantes en las reglas.
    value_range: tuple[float, float] | None = None
    #: Cuántas velas necesita antes de dar un valor válido, en función de los
    #: parámetros. Se expresa como múltiplo del parámetro principal.
    warmup_factor: float = 3.0
    warmup_param: str = "period"
    #: Si True, el indicador produce varias series y hay que elegir una con el
    #: parámetro ``line`` (p.ej. MACD: macd / signal / hist).
    multi_output: tuple[str, ...] = ()
    doc: str = ""

    def param(self, name: str) -> ParamSpec | None:
        for p in self.params:
            if p.name == name:
                return p
        return None

    def warmup_bars(self, params: Mapping[str, float]) -> int:
        base = float(params.get(self.warmup_param, 14))
        return max(2, int(round(base * self.warmup_factor)))

    def clamp_params(self, params: Mapping[str, float]) -> dict[str, float]:
        out: dict[str, float] = {}
        for spec in self.params:
            out[spec.name] = spec.clamp(params.get(spec.name, (spec.low + spec.high) / 2))
        return out


def _p(name: str, low: float, high: float, integer: bool = True, bin_size: float = 1.0) -> ParamSpec:
    return ParamSpec(name=name, low=low, high=high, integer=integer, bin_size=bin_size)


OHLC = (PriceField.CLOSE, PriceField.HIGH, PriceField.LOW, PriceField.HLC3, PriceField.OHLC4)

# --------------------------------------------------------------------------- #
# El catálogo                                                                  #
# --------------------------------------------------------------------------- #

INDICATORS: dict[str, IndicatorSpec] = {
    # -- tendencia ---------------------------------------------------------- #
    "SMA": IndicatorSpec(
        "SMA", "trend", "price", (_p("period", 5, 400, bin_size=5),), OHLC,
        doc="Media móvil simple.",
    ),
    "EMA": IndicatorSpec(
        "EMA", "trend", "price", (_p("period", 5, 400, bin_size=5),), OHLC,
        doc="Media móvil exponencial.",
    ),
    "WMA": IndicatorSpec(
        "WMA", "trend", "price", (_p("period", 5, 300, bin_size=5),), OHLC,
        doc="Media móvil ponderada linealmente.",
    ),
    "MACD": IndicatorSpec(
        "MACD", "trend", "signed",
        (_p("fast", 5, 60, bin_size=2), _p("slow", 15, 200, bin_size=5), _p("signal", 3, 40)),
        (PriceField.CLOSE,), warmup_param="slow", warmup_factor=3.0,
        multi_output=("macd", "signal", "hist"),
        doc="Convergencia/divergencia de medias. Elegir línea con el parámetro 'line'.",
    ),
    "ADX": IndicatorSpec(
        "ADX", "trend", "bounded", (_p("period", 7, 60),), (PriceField.CLOSE,),
        value_range=(0.0, 60.0), warmup_factor=4.0,
        doc="Fuerza de la tendencia, sin dirección. 0-100, en la práctica 0-60.",
    ),
    "SUPERTREND": IndicatorSpec(
        "SUPERTREND", "trend", "price",
        (_p("period", 5, 60), _p("multiplier", 1.0, 6.0, integer=False, bin_size=0.5)),
        (PriceField.CLOSE,),
        doc="Banda de tendencia basada en ATR.",
    ),
    "SLOPE": IndicatorSpec(
        "SLOPE", "trend", "signed", (_p("period", 5, 200, bin_size=5),), OHLC,
        value_range=(-0.02, 0.02),
        doc="Pendiente de la regresión lineal, normalizada por precio.",
    ),
    # -- osciladores -------------------------------------------------------- #
    "RSI": IndicatorSpec(
        "RSI", "oscillator", "bounded", (_p("period", 3, 60),), (PriceField.CLOSE,),
        value_range=(0.0, 100.0),
        doc="Índice de fuerza relativa.",
    ),
    "STOCH": IndicatorSpec(
        "STOCH", "oscillator", "bounded",
        (_p("period", 5, 60), _p("smooth", 1, 10)), (PriceField.CLOSE,),
        value_range=(0.0, 100.0),
        doc="Estocástico %K suavizado.",
    ),
    "CCI": IndicatorSpec(
        "CCI", "oscillator", "signed", (_p("period", 5, 100, bin_size=5),), (PriceField.HLC3,),
        value_range=(-250.0, 250.0),
        doc="Commodity Channel Index.",
    ),
    "WILLR": IndicatorSpec(
        "WILLR", "oscillator", "bounded", (_p("period", 5, 100, bin_size=5),), (PriceField.CLOSE,),
        value_range=(-100.0, 0.0),
        doc="Williams %R.",
    ),
    "ROC": IndicatorSpec(
        "ROC", "oscillator", "signed", (_p("period", 2, 300, bin_size=5),), (PriceField.CLOSE,),
        value_range=(-0.4, 0.4),
        doc="Tasa de cambio en fracción sobre N velas.",
    ),
    "ZSCORE": IndicatorSpec(
        "ZSCORE", "oscillator", "signed", (_p("period", 10, 300, bin_size=10),), OHLC,
        value_range=(-4.0, 4.0),
        doc="Z-score del precio respecto a su media móvil.",
    ),
    # -- volatilidad -------------------------------------------------------- #
    "ATR": IndicatorSpec(
        "ATR", "volatility", "price", (_p("period", 5, 100, bin_size=5),), (PriceField.HLC3,),
        doc="Rango verdadero medio, en unidades de precio.",
    ),
    "ATR_PCT": IndicatorSpec(
        "ATR_PCT", "volatility", "unit", (_p("period", 5, 100, bin_size=5),), (PriceField.HLC3,),
        value_range=(0.0, 0.15),
        doc="ATR dividido por el precio. Comparable entre activos y épocas.",
    ),
    "BBANDS": IndicatorSpec(
        "BBANDS", "volatility", "price",
        (_p("period", 10, 200, bin_size=5), _p("stdev", 1.0, 4.0, integer=False, bin_size=0.25)),
        (PriceField.CLOSE,), multi_output=("upper", "middle", "lower"),
        doc="Bandas de Bollinger. Elegir banda con el parámetro 'line'.",
    ),
    "BB_WIDTH": IndicatorSpec(
        "BB_WIDTH", "volatility", "unit",
        (_p("period", 10, 200, bin_size=5), _p("stdev", 1.0, 4.0, integer=False, bin_size=0.25)),
        (PriceField.CLOSE,), value_range=(0.0, 0.3),
        doc="Anchura de las bandas relativa al precio. Detecta compresión.",
    ),
    "KELTNER": IndicatorSpec(
        "KELTNER", "volatility", "price",
        (_p("period", 10, 120, bin_size=5), _p("multiplier", 0.5, 4.0, integer=False, bin_size=0.25)),
        (PriceField.CLOSE,), multi_output=("upper", "middle", "lower"),
        doc="Canal de Keltner basado en ATR.",
    ),
    "REALIZED_VOL": IndicatorSpec(
        "REALIZED_VOL", "volatility", "unit", (_p("period", 10, 300, bin_size=10),), (PriceField.CLOSE,),
        value_range=(0.0, 3.0),
        doc="Volatilidad realizada anualizada de los retornos logarítmicos.",
    ),
    # -- rango -------------------------------------------------------------- #
    "DONCHIAN_HIGH": IndicatorSpec(
        "DONCHIAN_HIGH", "range", "price", (_p("period", 5, 300, bin_size=5),), (PriceField.HIGH,),
        warmup_factor=1.2,
        doc="Máximo de las últimas N velas, EXCLUYENDO la actual.",
    ),
    "DONCHIAN_LOW": IndicatorSpec(
        "DONCHIAN_LOW", "range", "price", (_p("period", 5, 300, bin_size=5),), (PriceField.LOW,),
        warmup_factor=1.2,
        doc="Mínimo de las últimas N velas, EXCLUYENDO la actual.",
    ),
    "HIGHEST": IndicatorSpec(
        "HIGHEST", "range", "price", (_p("period", 3, 300, bin_size=5),), OHLC, warmup_factor=1.2,
    ),
    "LOWEST": IndicatorSpec(
        "LOWEST", "range", "price", (_p("period", 3, 300, bin_size=5),), OHLC, warmup_factor=1.2,
    ),
    # -- volumen ------------------------------------------------------------ #
    "OBV": IndicatorSpec(
        "OBV", "volume", "unbounded", (), (PriceField.CLOSE,), warmup_factor=1.0,
        doc="On-balance volume acumulado.",
    ),
    "VWAP": IndicatorSpec(
        "VWAP", "volume", "price", (_p("period", 12, 720, bin_size=12),), (PriceField.HLC3,),
        doc="Precio medio ponderado por volumen, sobre ventana móvil.",
    ),
    "VOL_RATIO": IndicatorSpec(
        "VOL_RATIO", "volume", "unbounded", (_p("period", 10, 300, bin_size=10),), (PriceField.VOLUME,),
        value_range=(0.0, 5.0),
        doc="Volumen actual dividido por su media móvil.",
    ),
    "MFI": IndicatorSpec(
        "MFI", "volume", "bounded", (_p("period", 5, 60),), (PriceField.HLC3,),
        value_range=(0.0, 100.0),
        doc="Money Flow Index.",
    ),
    # -- derivados ---------------------------------------------------------- #
    "PCT_CHANGE": IndicatorSpec(
        "PCT_CHANGE", "derived", "signed", (_p("period", 1, 200, bin_size=5),), OHLC,
        value_range=(-0.3, 0.3), warmup_factor=1.2,
        doc="Cambio porcentual sobre N velas.",
    ),
    "PCT_RANK": IndicatorSpec(
        "PCT_RANK", "derived", "unit",
        (_p("period", 20, 1000, bin_size=20),), OHLC, value_range=(0.0, 1.0),
        warmup_factor=1.1,
        doc="Percentil del valor actual dentro de su ventana. Sin escala: generaliza bien.",
    ),
}

#: Indicadores agrupados por categoría, para SWAP_INDICATOR.
BY_CATEGORY: dict[Category, tuple[str, ...]] = {}
for _kind, _spec in INDICATORS.items():
    BY_CATEGORY.setdefault(_spec.category, ())  # type: ignore[arg-type]
    BY_CATEGORY[_spec.category] = BY_CATEGORY[_spec.category] + (_kind,)  # type: ignore[index]


def spec(kind: str) -> IndicatorSpec:
    """Devuelve la especificación de un indicador o levanta KeyError claro."""
    try:
        return INDICATORS[kind]
    except KeyError as exc:  # pragma: no cover - mensaje
        raise KeyError(
            f"Indicador '{kind}' no está en el catálogo. "
            f"Disponibles: {', '.join(sorted(INDICATORS))}"
        ) from exc


def is_comparable(a: str, b: str) -> bool:
    """¿Tiene sentido comparar dos indicadores entre sí?

    Comparar un RSI con una EMA no significa nada: viven en escalas distintas.
    El muestreo de reglas usa esto para no generar basura.
    """
    ka, kb = spec(a).output, spec(b).output
    if ka == kb:
        return True
    return {ka, kb} == {"price", "price"}


# --------------------------------------------------------------------------- #
# Plantillas por familia                                                       #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class FamilyTemplate:
    """Sesgo de muestreo de una familia de ideas.

    ``preferred`` son los indicadores que el muestreo elige con más
    probabilidad; ``required_categories`` obliga a que el genoma tenga al menos
    un indicador de cada una de esas categorías, para que un bot de la familia
    tenga de verdad la forma de su familia.
    """

    family: IdeaFamily
    preferred: tuple[str, ...]
    required_categories: tuple[Category, ...] = ()
    n_features: tuple[int, int] = (2, 4)
    prefers_regime: float = 0.3
    doc: str = ""


DEFAULT_TEMPLATES: dict[IdeaFamily, FamilyTemplate] = {
    IdeaFamily.TREND: FamilyTemplate(
        IdeaFamily.TREND,
        preferred=("EMA", "SMA", "MACD", "ADX", "SUPERTREND", "SLOPE", "ATR"),
        required_categories=("trend",),
        n_features=(2, 4),
        prefers_regime=0.4,
        doc="Seguir la dirección dominante. Pocas operaciones, colas largas.",
    ),
    IdeaFamily.MEAN_REVERSION: FamilyTemplate(
        IdeaFamily.MEAN_REVERSION,
        preferred=("RSI", "ZSCORE", "BBANDS", "KELTNER", "STOCH", "CCI", "WILLR", "ATR"),
        required_categories=("oscillator",),
        n_features=(2, 4),
        prefers_regime=0.6,
        doc="Apostar a la vuelta al centro. Muere en tendencias: el filtro de régimen es casi obligatorio.",
    ),
    IdeaFamily.BREAKOUT: FamilyTemplate(
        IdeaFamily.BREAKOUT,
        preferred=("DONCHIAN_HIGH", "DONCHIAN_LOW", "HIGHEST", "LOWEST", "ATR", "BB_WIDTH", "VOL_RATIO"),
        required_categories=("range",),
        n_features=(2, 4),
        prefers_regime=0.3,
        doc="Entrar al romper un rango. La compresión previa de volatilidad es la mejor señal.",
    ),
    IdeaFamily.MOMENTUM: FamilyTemplate(
        IdeaFamily.MOMENTUM,
        preferred=("ROC", "RSI", "PCT_CHANGE", "PCT_RANK", "SLOPE", "MACD"),
        required_categories=("oscillator",),
        n_features=(2, 3),
        prefers_regime=0.3,
    ),
    IdeaFamily.VOLATILITY: FamilyTemplate(
        IdeaFamily.VOLATILITY,
        preferred=("ATR_PCT", "BB_WIDTH", "REALIZED_VOL", "KELTNER", "PCT_RANK"),
        required_categories=("volatility",),
        n_features=(2, 4),
        prefers_regime=0.2,
        doc="Operar el régimen, no la dirección.",
    ),
    IdeaFamily.MICROSTRUCTURE: FamilyTemplate(
        IdeaFamily.MICROSTRUCTURE,
        preferred=("OBV", "VWAP", "VOL_RATIO", "MFI", "PCT_CHANGE"),
        required_categories=("volume",),
        n_features=(2, 4),
        prefers_regime=0.3,
    ),
    IdeaFamily.HYBRID: FamilyTemplate(
        IdeaFamily.HYBRID,
        preferred=tuple(INDICATORS),
        required_categories=(),
        n_features=(2, 5),
        prefers_regime=0.4,
        doc="Resultado de cruzar familias distintas. No se siembra directamente.",
    ),
}

#: Familias que el sembrador puede usar. HYBRID sólo nace de un cruce.
SEEDABLE_FAMILIES: tuple[IdeaFamily, ...] = tuple(
    f for f in DEFAULT_TEMPLATES if f is not IdeaFamily.HYBRID
)


@dataclass(slots=True)
class GeneCatalog:
    """Catálogo efectivo: el de código, más el sesgo de ``config/genes.yaml``."""

    indicators: dict[str, IndicatorSpec] = field(default_factory=lambda: dict(INDICATORS))
    templates: dict[IdeaFamily, FamilyTemplate] = field(
        default_factory=lambda: dict(DEFAULT_TEMPLATES)
    )
    #: Pesos de muestreo por (familia, indicador). 1.0 si no se especifica.
    weights: dict[tuple[IdeaFamily, str], float] = field(default_factory=dict)

    def weight(self, family: IdeaFamily, kind: str) -> float:
        if kind not in self.indicators:
            return 0.0
        base = self.weights.get((family, kind))
        if base is not None:
            return base
        tpl = self.templates.get(family)
        if tpl and kind in tpl.preferred:
            return 3.0
        return 0.35

    def kinds_for(self, family: IdeaFamily) -> list[str]:
        return [k for k in self.indicators if self.weight(family, k) > 0]


DEFAULT_CATALOG = GeneCatalog()

__all__ = (
    "BY_CATEGORY",
    "DEFAULT_CATALOG",
    "DEFAULT_TEMPLATES",
    "INDICATORS",
    "SEEDABLE_FAMILIES",
    "FamilyTemplate",
    "GeneCatalog",
    "IndicatorSpec",
    "ParamSpec",
    "is_comparable",
    "spec",
)
