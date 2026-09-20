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

