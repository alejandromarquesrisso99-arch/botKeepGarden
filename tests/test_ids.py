"""Identidad de los bots."""

from __future__ import annotations

from keepgarden.ids import bot_name, label, new_bot_id, new_genome_id, short, unique_names


def test_el_id_sembrado_es_determinista() -> None:
    assert new_bot_id("semilla") == new_bot_id("semilla")
    assert new_bot_id("semilla") != new_bot_id("otra")


def test_el_id_aleatorio_no_se_repite() -> None:
    assert len({new_bot_id() for _ in range(500)}) == 500


def test_el_nombre_es_determinista() -> None:
    bid = new_bot_id("x")
    assert bot_name(bid) == bot_name(bid)
    assert bid.split("_")[1][:4] == short(bid)
    assert short(bid) in label(bid)


def test_el_genoma_comparte_identidad_con_el_bot() -> None:
    bid = new_bot_id("y")
    assert new_genome_id(bid).replace("gen_", "") == bid.replace("bot_", "")


def test_los_nombres_de_un_conjunto_son_unicos() -> None:
    ids = [new_bot_id(f"s{i}") for i in range(400)]
    nombres = unique_names(ids)
    assert len(set(nombres.values())) == len(ids)
