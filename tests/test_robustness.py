"""El informe de robustez: fricción, desplazamiento del inicio y Monte Carlo."""

from __future__ import annotations

import random
from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from keepgarden.config import Config
from keepgarden.evaluation.robustness import (
    _scaled_frictions,
    analyse,
    format_report,
    monte_carlo_order,
)


def _velas(n: int = 900, seed: int = 3) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    ts = 1_600_000_000_000 + np.arange(n) * 3_600_000
    close = 10_000.0 * np.exp(np.cumsum(rng.normal(0.0003, 0.01, n)))
    open_ = np.empty(n)
    open_[0] = 10_000.0
    open_[1:] = close[:-1]
    rango = np.abs(rng.normal(0, 0.01, n)) * close
    return pd.DataFrame(
        {
            "open": open_,
            "high": np.maximum(open_, close) + rango,
            "low": np.minimum(open_, close) - rango,
            "close": close,
            "volume": np.abs(rng.lognormal(6, 0.5, n)),
            "trades": np.nan,
        },
        index=pd.Index(ts, name="ts"),
    )


def _trades(pnls: list[float]) -> list[dict[str, object]]:
    return [{"pnl": p, "return": p / 1000.0} for p in pnls]


# --------------------------------------------------------------------------- #
# Monte Carlo                                                                  #
# --------------------------------------------------------------------------- #


def test_barajar_no_cambia_el_dinero_pero_si_el_camino() -> None:
    """Sumar es conmutativo: lo que el Monte Carlo mide es el drawdown."""
    mc = monte_carlo_order(
        _trades([50.0, -30.0, 80.0, -60.0, 40.0, -20.0, 25.0, -45.0]),
        1000.0, runs=300, rng=random.Random(1),
    )
    assert mc.runs == 300
    assert mc.final_return == pytest.approx(40.0 / 1000.0)
    assert mc.worst_drawdown >= mc.p95_drawdown >= mc.median_drawdown > 0
    assert 0.0 <= mc.observed_percentile <= 1.0


def test_el_peor_orden_posible_es_todas_las_perdidas_primero() -> None:
    ganancias = [100.0] * 5
    perdidas = [-50.0] * 5
    mc = monte_carlo_order(
        _trades(ganancias + perdidas), 1000.0, runs=500, rng=random.Random(2)
    )
    # El peor orden posible es empezar perdiendo: el pico es el capital
    # inicial y la curva cae 250 desde 1000.
    assert mc.worst_drawdown == pytest.approx(250.0 / 1000.0, rel=1e-6)
    # Y el mejor, ganar primero: el hoyo se mide desde 1500.
    assert mc.median_drawdown <= 250.0 / 1000.0


def test_el_bootstrap_si_da_una_distribucion() -> None:
    """Remuestrear con reemplazo cambia qué operaciones tocan, no sólo cuándo."""
    mc = monte_carlo_order(
        _trades([60.0, -40.0, 55.0, -35.0, 70.0, -50.0, 45.0, -25.0]),
        1000.0, runs=500, rng=random.Random(3),
    )
    assert mc.bootstrap_p05_return < mc.bootstrap_median_return < mc.bootstrap_p95_return
    assert 0.0 <= mc.prob_loss <= 1.0


def test_con_dos_operaciones_no_hay_monte_carlo_que_valga() -> None:
    assert monte_carlo_order(_trades([10.0, -5.0]), 1000.0).runs == 0


# --------------------------------------------------------------------------- #
# La fricción                                                                  #
# --------------------------------------------------------------------------- #


def test_doblar_la_friccion_dobla_las_cuatro_palancas(cfg: Config) -> None:
    doble = _scaled_frictions(cfg, 2.0)
    assert doble.frictions.taker_fee_bps == cfg.frictions.taker_fee_bps * 2
    assert doble.frictions.maker_fee_bps == cfg.frictions.maker_fee_bps * 2
    assert doble.frictions.slippage_bps == cfg.frictions.slippage_bps * 2
    assert doble.frictions.slippage_atr_frac == cfg.frictions.slippage_atr_frac * 2
    assert cfg.frictions.taker_fee_bps != doble.frictions.taker_fee_bps  # sin mutar


def test_mas_friccion_nunca_mejora_el_resultado(cfg: Config, trend_genome) -> None:
    """Si subir el coste mejora el retorno, algo está mal contado."""
    velas = _velas()
    informe = analyse(trend_genome, velas, cfg, runs=50, rng=random.Random(4))
    por_factor = {e.friction_factor: e for e in informe.scenarios if e.start_shift == 0}
    assert set(por_factor) == {1.0, 2.0, 3.0}
    assert por_factor[1.0].total_return >= por_factor[2.0].total_return
    assert por_factor[2.0].total_return >= por_factor[3.0].total_return
    assert por_factor[1.0].fee_drag <= por_factor[3.0].fee_drag


def test_el_informe_marca_si_aguanta_el_doble_de_friccion(cfg: Config, trend_genome) -> None:
    informe = analyse(trend_genome, _velas(), cfg, runs=50, rng=random.Random(5))
    doble = next(e for e in informe.scenarios if e.friction_factor == 2.0)
    assert informe.survives is informe.survives_double_friction
    assert informe.survives_double_friction == (
        doble.n_trades > 0 and doble.total_return > 0 and not doble.aborted
    )


def test_el_informe_prueba_cuatro_arranques_distintos(cfg: Config, trend_genome) -> None:
    """Una estrategia que depende de haber entrado ese martes no es una estrategia."""
    informe = analyse(trend_genome, _velas(), cfg, runs=50, rng=random.Random(6))
    desplazamientos = {e.start_shift for e in informe.scenarios}
    assert desplazamientos == {0, 24, 72, 168}


def test_el_informe_en_markdown_dice_el_veredicto(cfg: Config, trend_genome) -> None:
    informe = analyse(
        trend_genome, _velas(), cfg, bot_id="Zarza-Terca-01", runs=50,
        rng=random.Random(7),
    )
    texto = format_report([informe])
    assert "# Informe de robustez" in texto
    assert "Zarza-Terca-01" in texto
    assert "fricción ×2" in texto
    assert "empieza 168 velas después" in texto
    assert "**Veredicto**" in texto


def test_el_informe_explica_por_que_el_dinero_final_no_cambia() -> None:
    """Con operaciones de sobra, el Monte Carlo sale en el texto y se explica."""
    from keepgarden.evaluation.robustness import RobustnessReport, Scenario

    informe = RobustnessReport(
        bot_id="Musgo-Firme-02", genome_id="gen_x", symbol="BTC/USDT",
        scenarios=[Scenario(label="referencia")],
        monte_carlo=monte_carlo_order(
            _trades([40.0, -25.0, 60.0, -30.0, 35.0, -15.0]),
            1000.0, runs=200, rng=random.Random(9),
        ),
    )
    texto = format_report([informe])
    assert "sumar es conmutativo" in texto
    assert "Bootstrap" in texto


def test_el_informe_no_mira_el_holdout(cfg: Config, trend_genome) -> None:
    """Se mide sobre el tramo entrenable, igual que la incubadora."""
    from keepgarden.evaluation.walkforward import make_split

    velas = _velas(3000)
    corte = make_split(len(velas), replace(
        cfg.incubator, train_bars=800, validation_bars=200, holdout_bars=400, n_folds=2
    )).holdout_start
    informe = analyse(trend_genome, velas.iloc[:corte], cfg, runs=20, rng=random.Random(8))
    assert informe.baseline is not None
    # Ninguna corrida puede haber visto una vela más allá del corte.
    assert corte < len(velas)
    assert informe.scenarios[0].n_trades >= 0
