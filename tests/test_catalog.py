"""Integridad del catálogo de genes."""

from __future__ import annotations

import pytest
import yaml

from keepgarden.genome.catalog import (
    BY_CATEGORY,
    DEFAULT_TEMPLATES,
    INDICATORS,
    SEEDABLE_FAMILIES,
    GeneCatalog,
    spec,
)
from keepgarden.types import IdeaFamily


def test_todos_los_indicadores_tienen_categoria_conocida() -> None:
    validas = {"trend", "oscillator", "volatility", "range", "volume", "derived"}
    for kind, s in INDICATORS.items():
        assert s.category in validas, kind
        assert s.kind == kind


def test_los_rangos_de_parametros_tienen_sentido() -> None:
    for kind, s in INDICATORS.items():
        for p in s.params:
            assert p.low < p.high, f"{kind}.{p.name}"
            assert p.bin_size > 0, f"{kind}.{p.name}"


def test_el_recorte_respeta_los_limites() -> None:
    p = spec("RSI").param("period")
    assert p is not None
    assert p.clamp(-10) == p.low
    assert p.clamp(9999) == p.high
    assert p.clamp(14) == 14


def test_toda_familia_sembrable_tiene_plantilla() -> None:
    for fam in SEEDABLE_FAMILIES:
        assert fam in DEFAULT_TEMPLATES
        tpl = DEFAULT_TEMPLATES[fam]
        assert tpl.preferred, fam
        for kind in tpl.preferred:
            assert kind in INDICATORS, f"{fam} prefiere {kind}, que no existe"


def test_hybrid_no_es_sembrable() -> None:
    assert IdeaFamily.HYBRID not in SEEDABLE_FAMILIES


def test_las_categorias_requeridas_son_alcanzables() -> None:
    """Si una familia exige una categoría, alguno de sus preferidos debe tenerla."""
    for fam, tpl in DEFAULT_TEMPLATES.items():
        for cat in tpl.required_categories:
            disponibles = [k for k in tpl.preferred if INDICATORS[k].category == cat]
            assert disponibles, f"{fam} exige {cat} pero ninguno de sus preferidos lo es"


def test_genes_yaml_solo_referencia_indicadores_existentes(project_root) -> None:
    raw = yaml.safe_load((project_root / "config" / "genes.yaml").read_text(encoding="utf-8"))
    for fam, block in raw["families"].items():
        IdeaFamily(fam)  # familia válida
        for kind in block.get("weights", {}):
            assert kind in INDICATORS, f"genes.yaml: {fam} referencia '{kind}', que no existe"
        for cat in block.get("required_categories", []):
            assert cat in BY_CATEGORY, f"genes.yaml: categoría '{cat}' desconocida"


def test_pesos_del_catalogo(catalog: GeneCatalog) -> None:
    assert catalog.weight(IdeaFamily.TREND, "EMA") > catalog.weight(IdeaFamily.TREND, "MFI")
    assert catalog.weight(IdeaFamily.TREND, "NO_EXISTE") == 0.0


def test_indicador_desconocido_da_error_util() -> None:
    with pytest.raises(KeyError, match="no está en el catálogo"):
        spec("TELEPATIA")
