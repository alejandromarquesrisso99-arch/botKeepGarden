"""Fixtures compartidas."""

from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from keepgarden.config import Config, load_config  # noqa: E402
from keepgarden.genome.catalog import DEFAULT_CATALOG, GeneCatalog  # noqa: E402
from keepgarden.genome.schema import (  # noqa: E402
    Condition,
    FeatureGene,
    Genome,
    GenomeMeta,
    LogicNode,
    MarketSpec,
    Operand,
    RegimeGene,
    RiskGene,
    StopGene,
    TakeProfitGene,
)
from keepgarden.types import (  # noqa: E402
    BreedOperator,
    CompareOp,
    IdeaFamily,
    LogicOp,
    PriceField,
    StopKind,
    TakeProfitKind,
)


@pytest.fixture(scope="session")
def project_root() -> Path:
    return ROOT


@pytest.fixture(scope="session")
def cfg() -> Config:
    return load_config(ROOT / "config" / "garden.yaml")


@pytest.fixture(scope="session")
def catalog() -> GeneCatalog:
    return DEFAULT_CATALOG


@pytest.fixture
def rng() -> random.Random:
    return random.Random(20260914)


@pytest.fixture
def trend_genome() -> Genome:
    """Un cruce de medias con filtro de ADX. Escrito a mano a propósito: es el
    genoma de oro contra el que se valida el compilador en el hito 2."""
    return Genome(
        id="gen_trend_demo",
        family=IdeaFamily.TREND,
        market=MarketSpec(venue="binance", symbol="BTC/USDT", timeframe="1h"),
        features=(
            FeatureGene("ema_fast", "EMA", {"period": 24}, PriceField.CLOSE),
            FeatureGene("ema_slow", "EMA", {"period": 120}, PriceField.CLOSE),
            FeatureGene("adx", "ADX", {"period": 14}, PriceField.CLOSE),
            FeatureGene("atr", "ATR", {"period": 14}, PriceField.HLC3),
        ),
        entry_long=LogicNode(
            LogicOp.AND,
            (
                Condition(CompareOp.CROSS_ABOVE, Operand(ref="ema_fast"), Operand(ref="ema_slow")),
                Condition(CompareOp.GT, Operand(ref="adx"), Operand(const=20.0)),
            ),
        ),
        exit_long=Condition(
            CompareOp.CROSS_BELOW, Operand(ref="ema_fast"), Operand(ref="ema_slow")
        ),
        risk=RiskGene(
            risk_per_trade=0.01,
            stop=StopGene(StopKind.ATR_MULT, 2.5, atr_ref="atr"),
            take_profit=TakeProfitGene(TakeProfitKind.NONE, 0.0),
            max_holding_bars=720,
        ),
        regime=RegimeGene(
            enabled=True,
            rule=Condition(CompareOp.GT, Operand(ref="adx"), Operand(const=18.0)),
        ),
        meta=GenomeMeta(
            born_at="2026-09-14T00:00:00Z",
            generation=0,
            operator=BreedOperator.SEED,
            root_lineage="lin_trend_demo",
        ),
    )


@pytest.fixture
def breakout_genome() -> Genome:
    """Ruptura de Donchian con compresión previa de volatilidad."""
    return Genome(
        id="gen_breakout_demo",
        family=IdeaFamily.BREAKOUT,
        market=MarketSpec(),
        features=(
            FeatureGene("dc_high", "DONCHIAN_HIGH", {"period": 55}, PriceField.HIGH),
            FeatureGene("dc_low", "DONCHIAN_LOW", {"period": 20}, PriceField.LOW),
            FeatureGene("bbw", "BB_WIDTH", {"period": 40, "stdev": 2.0}, PriceField.CLOSE),
            FeatureGene("atr", "ATR", {"period": 14}, PriceField.HLC3),
        ),
        entry_long=LogicNode(
            LogicOp.AND,
            (
                Condition(CompareOp.GT, Operand(price=PriceField.CLOSE), Operand(ref="dc_high")),
                Condition(CompareOp.PCT_RANK_LT, Operand(ref="bbw"), Operand(const=0.35)),
            ),
        ),
        exit_long=Condition(
            CompareOp.LT, Operand(price=PriceField.CLOSE), Operand(ref="dc_low")
        ),
        risk=RiskGene(
            risk_per_trade=0.012,
            stop=StopGene(StopKind.ATR_MULT, 2.0, atr_ref="atr"),
            take_profit=TakeProfitGene(TakeProfitKind.R_MULTIPLE, 3.0),
            max_holding_bars=480,
        ),
        meta=GenomeMeta(
            born_at="2026-09-14T00:00:00Z",
            generation=0,
            operator=BreedOperator.SEED,
            root_lineage="lin_breakout_demo",
        ),
    )


# --------------------------------------------------------------------------- #
# Velas sintéticas                                                             #
# --------------------------------------------------------------------------- #


def build_candles(
    n: int = 200,
    *,
    start_ts: int = 1_546_300_800_000,   # 2019-01-01T00:00:00Z
    timeframe: str = "1h",
    first_close: float = 100.0,
    step: float = 0.5,
    volume: float = 10.0,
):
    """Serie de velas determinista, continua y coherente.

    El cierre sube ``step`` por vela siguiendo una onda suave, de forma que los
    indicadores tengan algo que morder sin que aparezcan saltos que la
    validación marcaría como anomalía.
    """
    import math

    import numpy as np
    import pandas as pd

    from keepgarden.types import TIMEFRAME_MS

    tf_ms = TIMEFRAME_MS[timeframe]
    ts = np.arange(n, dtype="int64") * tf_ms + start_ts
    i = np.arange(n, dtype="float64")
    close = first_close + step * i + 3.0 * np.sin(i / (2.0 * math.pi))
    open_ = np.empty(n, dtype="float64")
    open_[0] = first_close
    open_[1:] = close[:-1]
    high = np.maximum(open_, close) * 1.002
    low = np.minimum(open_, close) * 0.998
    return pd.DataFrame(
        {
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": np.full(n, volume, dtype="float64"),
            "trades": np.full(n, 42.0, dtype="float64"),
        },
        index=pd.Index(ts, name="ts"),
    )


@pytest.fixture
def candles():
    """Fábrica de series de velas sintéticas."""
    return build_candles


def build_walk(
    n: int = 2000,
    *,
    start_ts: int = 1_546_300_800_000,
    timeframe: str = "1h",
    first_close: float = 20000.0,
    vol: float = 0.006,
    drift: float = 0.00005,
    seed: int = 20260920,
):
    """Un paseo aleatorio con forma de mercado: tendencias, vueltas y rangos.

    ``build_candles`` es una recta con una onda encima: sirve para comprobar
    fórmulas, pero en ella un cruce de medias ocurre una sola vez y una ruptura
    no ocurre nunca. Para preguntarle a un bot si opera hace falta una serie que
    suba, baje y se aburra, como la de verdad.
    """
    import numpy as np
    import pandas as pd

    from keepgarden.types import TIMEFRAME_MS

    rng = np.random.default_rng(seed)
    # Volatilidad cambiante: sin ella no hay regímenes que distinguir.
    escala = 1.0 + 0.6 * np.sin(np.arange(n) / 180.0)
    pasos = rng.normal(drift, vol, n) * escala
    close = first_close * np.exp(np.cumsum(pasos))
    open_ = np.empty(n)
    open_[0] = first_close
    open_[1:] = close[:-1]
    rango = np.abs(rng.normal(0.0, vol / 2.0, n)) * close
    high = np.maximum(open_, close) + rango
    low = np.minimum(open_, close) - rango
    ts = np.arange(n, dtype="int64") * TIMEFRAME_MS[timeframe] + start_ts
    return pd.DataFrame(
        {
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": np.abs(rng.normal(500.0, 150.0, n)),
            "trades": np.abs(rng.normal(300.0, 90.0, n)),
        },
        index=pd.Index(ts, name="ts"),
    )


@pytest.fixture
def walk():
    """Fábrica de series con forma de mercado."""
    return build_walk
