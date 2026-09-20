"""Los límites del jardinero existen para que no pueda destruir el jardín."""

from __future__ import annotations

import pytest

from keepgarden.gardener.proposals import (
    GardenerLimits,
    GardenSnapshot,
    Proposal,
    ProposalError,
    validate_session,
)


@pytest.fixture
def snap() -> GardenSnapshot:
    bots = frozenset(f"bot_{i:02d}" for i in range(60))
    return GardenSnapshot(
        generation=28,
        alive_bot_ids=bots,
        lineages=frozenset({"lin_breakout_a", "lin_trend_b"}),
        population_size=60,
        current_params={"evolution.mutation_rate": 0.35, "garden.cull_fraction": 0.20},
        bot_ages={b: 5 for b in bots},
    )


@pytest.fixture
def limits() -> GardenerLimits:
    return GardenerLimits()


def _p(kind: str, payload: dict, **kw) -> Proposal:
    base = {
        "kind": kind,
        "payload": payload,
        "rationale": "porque sí, con motivo",
        "expected_effect": "sortino mediano sube 0.2",
        "review_in_generations": 3,
    }
    base.update(kw)
    return Proposal.from_dict(base)


def test_propuesta_valida(snap, limits) -> None:
    validate_session([_p("FUSE", {"members": ["bot_01", "bot_02"]})], snap, limits)


def test_faltan_campos_obligatorios() -> None:
    with pytest.raises(ProposalError, match="obligatorios"):
        Proposal.from_dict({"kind": "NOTE", "payload": {}})


def test_expected_effect_vacio_se_rechaza(snap, limits) -> None:
    with pytest.raises(ProposalError, match="expected_effect"):
        validate_session([_p("NOTE", {}, expected_effect="  ")], snap, limits)


def test_parametro_prohibido(snap, limits) -> None:
    with pytest.raises(ProposalError, match="fuera del alcance"):
        validate_session([_p("TUNE", {"param": "execution.mode", "value": 1})], snap, limits)


def test_holdout_intocable(snap, limits) -> None:
    with pytest.raises(ProposalError, match="fuera del alcance"):
        validate_session([_p("TUNE", {"param": "incubator.holdout_bars", "value": 0})], snap, limits)


def test_salto_de_parametro_demasiado_grande(snap, limits) -> None:
    with pytest.raises(ProposalError, match="supera el máximo"):
        validate_session(
            [_p("TUNE", {"param": "evolution.mutation_rate", "value": 0.9})], snap, limits
        )


def test_salto_pequeno_se_admite(snap, limits) -> None:
    validate_session(
        [_p("TUNE", {"param": "evolution.mutation_rate", "value": 0.45})], snap, limits
    )


def test_mismo_parametro_dos_veces(snap, limits) -> None:
    ps = [
        _p("TUNE", {"param": "evolution.mutation_rate", "value": 0.40}),
        _p("TUNE", {"param": "evolution.mutation_rate", "value": 0.42}),
    ]
    with pytest.raises(ProposalError, match="dos veces"):
        validate_session(ps, snap, limits)


def test_no_puede_vaciar_el_jardin(snap, limits) -> None:
    victimas = [f"bot_{i:02d}" for i in range(30)]
    with pytest.raises(ProposalError, match="vaciar el jardín"):
        validate_session([_p("RETIRE", {"bots": victimas})], snap, limits)


def test_jubilacion_moderada_se_admite(snap, limits) -> None:
    validate_session([_p("RETIRE", {"bots": ["bot_01", "bot_02"]})], snap, limits)


def test_no_se_siembran_hibridos(snap, limits) -> None:
    with pytest.raises(ProposalError, match="HYBRID"):
        validate_session([_p("SEED_FAMILY", {"family": "HYBRID", "count": 3})], snap, limits)


def test_fusion_con_bot_muerto(snap, limits) -> None:
    with pytest.raises(ProposalError, match="no vivos"):
        validate_session([_p("FUSE", {"members": ["bot_01", "bot_999"]})], snap, limits)


def test_injerto_de_bloque_inexistente(snap, limits) -> None:
    with pytest.raises(ProposalError, match="no injertable"):
        validate_session([_p("GRAFT", {"block": "alma", "into": "bot_01"})], snap, limits)


def test_payload_con_clave_desconocida(snap, limits) -> None:
    with pytest.raises(ProposalError, match="desconocidas"):
        validate_session([_p("PROTECT", {"bots": ["bot_01"], "generations": 2, "x": 1})], snap, limits)


def test_demasiadas_propuestas(snap, limits) -> None:
    ps = [_p("NOTE", {"text": f"n{i}"}) for i in range(limits.max_proposals_per_session + 1)]
    with pytest.raises(ProposalError, match="superan el máximo"):
        validate_session(ps, snap, limits)
