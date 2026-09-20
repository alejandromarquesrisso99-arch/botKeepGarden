"""El genoma debe serializar y volver idéntico, siempre."""

from __future__ import annotations

import json

import pytest

from keepgarden.genome.schema import (
    Condition,
    Genome,
    LogicNode,
    Operand,
    canonical_json,
    genome_hash,
    referenced_features,
    rule_depth,
    rule_leaves,
)
from keepgarden.genome.serialize import dump_genome_str, load_genome_str
from keepgarden.types import CompareOp, LogicOp


def test_roundtrip_exacto(trend_genome: Genome) -> None:
    payload = dump_genome_str(trend_genome)
    back = load_genome_str(payload)
    assert back.to_dict() == trend_genome.to_dict()


def test_roundtrip_de_ensembles(breakout_genome: Genome) -> None:
    d = breakout_genome.to_dict()
    assert Genome.from_dict(d).to_dict() == d


def test_json_canonico_es_estable(trend_genome: Genome) -> None:
    a = canonical_json(trend_genome)
    b = canonical_json(load_genome_str(dump_genome_str(trend_genome)))
    assert a == b


def test_hash_ignora_meta_e_id(trend_genome: Genome) -> None:
    otro = trend_genome.with_meta(generation=99, gardener_note="lo que sea")
    assert genome_hash(otro) == genome_hash(trend_genome)


def test_hash_cambia_con_la_estructura(trend_genome: Genome) -> None:
    import dataclasses

    mutado = dataclasses.replace(
        trend_genome,
        features=tuple(
            dataclasses.replace(f, params={"period": 999}) if f.id == "ema_fast" else f
            for f in trend_genome.features
        ),
    )
    assert genome_hash(mutado) != genome_hash(trend_genome)


def test_operand_exige_exactamente_un_campo() -> None:
    with pytest.raises(ValueError):
        Operand()
    with pytest.raises(ValueError):
        Operand(ref="x", const=1.0)


def test_condicion_unaria_rechaza_right() -> None:
    with pytest.raises(ValueError):
        Condition(CompareOp.RISING, Operand(ref="x"), Operand(const=1.0))


def test_condicion_binaria_exige_right() -> None:
    with pytest.raises(ValueError):
        Condition(CompareOp.GT, Operand(ref="x"))


def test_between_exige_right2() -> None:
    with pytest.raises(ValueError):
        Condition(CompareOp.BETWEEN, Operand(ref="x"), Operand(const=1.0))


def test_not_admite_un_solo_hijo() -> None:
    hoja = Condition(CompareOp.GT, Operand(ref="x"), Operand(const=1.0))
    with pytest.raises(ValueError):
        LogicNode(LogicOp.NOT, (hoja, hoja))


def test_referencias_y_forma(trend_genome: Genome) -> None:
    refs = referenced_features(trend_genome.entry_long)
    assert refs == {"ema_fast", "ema_slow", "adx"}
    assert rule_depth(trend_genome.entry_long) == 2
    assert len(rule_leaves(trend_genome.entry_long)) == 2
    # toda referencia del genoma resuelve a un feature existente
    assert trend_genome.all_referenced_features() <= trend_genome.feature_ids()


def test_complejidad_cuenta_features_y_hojas(trend_genome: Genome) -> None:
    # 4 features + 2 hojas de entrada + 1 de salida + 1 de régimen
    assert trend_genome.complexity() == 8


def test_describe_es_legible(trend_genome: Genome) -> None:
    texto = trend_genome.describe()
    assert "EMA" in texto
    assert "cruza por encima de" in texto
    assert "riesgo:" in texto


def test_el_genoma_es_json_serializable(trend_genome: Genome) -> None:
    json.loads(dump_genome_str(trend_genome))
