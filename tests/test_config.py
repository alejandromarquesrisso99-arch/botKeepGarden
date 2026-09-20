"""La configuración debe fallar ruidosamente, nunca en silencio."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from keepgarden.config import Config, ConfigError, load_config, validate_config


def _write(tmp_path: Path, raw: dict) -> Path:
    root = tmp_path / "proj"
    (root / "config").mkdir(parents=True)
    p = root / "config" / "garden.yaml"
    p.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return p


def test_carga_la_config_del_proyecto(cfg: Config) -> None:
    assert cfg.primary_symbol == "BTC/USDT"
    assert cfg.market.timeframe == "1h"
    assert cfg.garden.ticks_per_generation == 168


def test_live_esta_bloqueado(tmp_path: Path) -> None:
    p = _write(tmp_path, {"execution": {"mode": "live"}})
    with pytest.raises(ConfigError, match="paper"):
        load_config(p)


def test_clave_desconocida_falla(tmp_path: Path) -> None:
    p = _write(tmp_path, {"garden": {"targt_population": 50}})
    with pytest.raises(ConfigError, match="desconocidas"):
        load_config(p)


def test_seccion_desconocida_falla(tmp_path: Path) -> None:
    p = _write(tmp_path, {"jardin": {}})
    with pytest.raises(ConfigError, match="Secciones desconocidas"):
        load_config(p)


def test_cuotas_de_operadores_deben_sumar_uno(cfg: Config) -> None:
    import copy

    bad = copy.deepcopy(cfg)
    bad.evolution.fusion_share = 0.5
    with pytest.raises(ConfigError, match="sumar 1.0"):
        validate_config(bad)


def test_comision_cero_esta_prohibida(cfg: Config) -> None:
    import copy

    bad = copy.deepcopy(cfg)
    bad.frictions.taker_fee_bps = 0.0
    with pytest.raises(ConfigError, match="comisiones"):
        validate_config(bad)


def test_holdout_no_puede_ser_cero(cfg: Config) -> None:
    import copy

    bad = copy.deepcopy(cfg)
    bad.incubator.holdout_bars = 0
    with pytest.raises(ConfigError, match="holdout"):
        validate_config(bad)


def test_umbral_de_clon_menor_que_el_de_especie(cfg: Config) -> None:
    import copy

    bad = copy.deepcopy(cfg)
    bad.speciation.clone_threshold = 0.9
    with pytest.raises(ConfigError, match="clone_threshold"):
        validate_config(bad)


def test_poblacion_coherente(cfg: Config) -> None:
    import copy

    bad = copy.deepcopy(cfg)
    bad.garden.min_population = 200
    with pytest.raises(ConfigError):
        validate_config(bad)
