"""Validación y reparación de genomas.

La reparación importa tanto como la validación: los operadores evolutivos
producen genomas rotos constantemente (un cruce que hereda una regla que
referencia un feature del otro padre). Si se descartaran, se tiraría la mayor
parte de la descendencia en cada generación.
"""

from __future__ import annotations

import dataclasses
import random

import pytest

from keepgarden.genome.catalog import DEFAULT_CATALOG
from keepgarden.genome.schema import (
    Condition,
    EnsembleGene,
    FeatureGene,
    Genome,
    GenomeMeta,
    LogicNode,
    MarketSpec,
    Operand,
    RegimeGene,
    RiskGene,
    StopGene,
    rule_leaves,
)
from keepgarden.genome.validate import (
    GenomeInvalid,
    repair_genome,
    validate_genome,
)
from keepgarden.types import (
    BreedOperator,
    CompareOp,
    IdeaFamily,
    LogicOp,
    PriceField,
    StopKind,
)


def con(genome: Genome, **cambios) -> Genome:
    return dataclasses.replace(genome, **cambios)


def informe(genome: Genome, cfg):
    return validate_genome(genome, cfg, DEFAULT_CATALOG, strict=False)


@pytest.fixture
def simple() -> Genome:
    """El genoma mínimo que pasa la validación."""
    return Genome(
        id="gen_simple",
        family=IdeaFamily.TREND,
        market=MarketSpec(),
        features=(
            FeatureGene("rapida", "EMA", {"period": 20}, PriceField.CLOSE),
            FeatureGene("lenta", "EMA", {"period": 60}, PriceField.CLOSE),
        ),
        entry_long=Condition(CompareOp.CROSS_ABOVE, Operand(ref="rapida"), Operand(ref="lenta")),
        exit_long=Condition(CompareOp.CROSS_BELOW, Operand(ref="rapida"), Operand(ref="lenta")),
        risk=RiskGene(risk_per_trade=0.01, stop=StopGene(StopKind.PERCENT, 0.03)),
        meta=GenomeMeta(operator=BreedOperator.SEED),
    )


# --------------------------------------------------------------------------- #
# Lo que debe pasar                                                            #
# --------------------------------------------------------------------------- #


def test_el_genoma_simple_vale(simple, cfg) -> None:
    assert informe(simple, cfg).ok


def test_los_genomas_de_ejemplo_valen(trend_genome, breakout_genome, cfg) -> None:
    for g in (trend_genome, breakout_genome):
        r = informe(g, cfg)
        assert r.ok, f"{g.id}: {r.errors}"


def test_los_genomas_de_examples_valen(project_root, cfg) -> None:
    from keepgarden.genome.serialize import load_genome

    for path in sorted((project_root / "examples").glob("*.json")):
        g = load_genome(path)
        if g.is_ensemble:
            continue                      # sus miembros no existen fuera del jardín
        r = informe(g, cfg)
        assert r.ok, f"{path.name}: {r.errors}"


# --------------------------------------------------------------------------- #
# Referencias y features                                                       #
# --------------------------------------------------------------------------- #


def test_ids_de_feature_duplicados(simple, cfg) -> None:
    roto = con(simple, features=(*simple.features, simple.features[0]))
    r = informe(roto, cfg)
    assert not r.ok
    assert any("rapida" in e for e in r.errors)


def test_referencia_que_no_resuelve(simple, cfg) -> None:
    roto = con(
        simple,
        entry_long=Condition(CompareOp.GT, Operand(ref="fantasma"), Operand(ref="lenta")),
    )
    r = informe(roto, cfg)
    assert not r.ok
    assert any("fantasma" in e for e in r.errors)


def test_el_atr_del_stop_tambien_debe_existir(simple, cfg) -> None:
    roto = con(simple, risk=RiskGene(stop=StopGene(StopKind.ATR_MULT, 2.0, atr_ref="no_hay")))
    r = informe(roto, cfg)
    assert not r.ok
    assert any("no_hay" in e for e in r.errors)


def test_un_feature_sin_usar_es_solo_un_aviso(simple, cfg) -> None:
    con_sobra = con(
        simple, features=(*simple.features, FeatureGene("huerfano", "RSI", {"period": 14}))
    )
    r = informe(con_sobra, cfg)
    assert r.ok
    assert any("huerfano" in w for w in r.warnings)


# --------------------------------------------------------------------------- #
# Catálogo y parámetros                                                        #
# --------------------------------------------------------------------------- #


def test_indicador_fuera_del_catalogo(simple, cfg) -> None:
    roto = con(simple, features=(FeatureGene("x", "TELEPATIA", {"period": 5}),
                                 *simple.features[1:]))
    r = informe(roto, cfg)
    assert not r.ok
    assert any("TELEPATIA" in e for e in r.errors)


def test_parametro_fuera_de_rango(simple, cfg) -> None:
    roto = con(simple, features=(FeatureGene("rapida", "EMA", {"period": 5000}),
                                 *simple.features[1:]))
    r = informe(roto, cfg)
    assert not r.ok
    assert any("period" in e for e in r.errors)


def test_macd_con_la_rapida_mas_lenta_que_la_lenta(simple, cfg) -> None:
    roto = con(
        simple,
        features=(
            FeatureGene("rapida", "MACD", {"fast": 50, "slow": 20, "signal": 9, "line": 0}),
            *simple.features[1:],
        ),
    )
    r = informe(roto, cfg)
    assert not r.ok
    assert any("fast" in e for e in r.errors)


def test_fuente_no_admitida_por_el_indicador(simple, cfg) -> None:
    roto = con(
        simple,
        features=(FeatureGene("rapida", "EMA", {"period": 20}, PriceField.VOLUME),
                  *simple.features[1:]),
    )
    assert not informe(roto, cfg).ok


# --------------------------------------------------------------------------- #
# Límites de forma                                                             #
# --------------------------------------------------------------------------- #


def test_demasiados_features(simple, cfg) -> None:
    muchos = tuple(
        FeatureGene(f"f{i}", "EMA", {"period": 10 + i}) for i in range(cfg.evolution.max_features + 2)
    )
    roto = con(
        simple,
        features=muchos,
        entry_long=Condition(CompareOp.GT, Operand(ref="f0"), Operand(ref="f1")),
        exit_long=Condition(CompareOp.LT, Operand(ref="f0"), Operand(ref="f1")),
        risk=RiskGene(stop=StopGene(StopKind.PERCENT, 0.03)),
    )
    r = informe(roto, cfg)
    assert not r.ok
    assert any("max_features" in e for e in r.errors)


def test_demasiadas_hojas_en_un_arbol(simple, cfg) -> None:
    hojas = tuple(
        Condition(CompareOp.GT, Operand(ref="rapida"), Operand(ref="lenta"))
        for _ in range(cfg.evolution.max_rule_leaves + 1)
    )
    roto = con(simple, entry_long=LogicNode(LogicOp.AND, hojas))
    r = informe(roto, cfg)
    assert not r.ok
    assert any("hojas" in e for e in r.errors)


def test_arbol_demasiado_profundo(simple, cfg) -> None:
    nodo = Condition(CompareOp.GT, Operand(ref="rapida"), Operand(ref="lenta"))
    for _ in range(cfg.evolution.max_rule_depth + 1):
        nodo = LogicNode(LogicOp.NOT, (nodo,))
    r = informe(con(simple, entry_long=nodo), cfg)
    assert not r.ok
    assert any("profundidad" in e for e in r.errors)


# --------------------------------------------------------------------------- #
# Comparaciones con sentido                                                    #
# --------------------------------------------------------------------------- #


def test_no_se_compara_un_rsi_con_una_media(simple, cfg) -> None:
    roto = con(
        simple,
        features=(FeatureGene("rsi", "RSI", {"period": 14}), *simple.features),
        entry_long=Condition(CompareOp.GT, Operand(ref="rsi"), Operand(ref="rapida")),
    )
    r = informe(roto, cfg)
    assert not r.ok
    assert any("comparar" in e.lower() for e in r.errors)


def test_constante_fuera_del_rango_del_indicador(simple, cfg) -> None:
    roto = con(
        simple,
        features=(FeatureGene("rsi", "RSI", {"period": 14}), *simple.features),
        entry_long=Condition(CompareOp.GT, Operand(ref="rsi"), Operand(const=500.0)),
    )
    r = informe(roto, cfg)
    assert not r.ok
    assert any("500" in e for e in r.errors)


def test_el_percentil_se_compara_contra_cero_uno(simple, cfg) -> None:
    bueno = con(
        simple,
        features=(FeatureGene("rsi", "RSI", {"period": 14}), *simple.features),
        entry_long=Condition(
            CompareOp.PCT_RANK_LT, Operand(ref="rsi"), Operand(const=0.2), lookback=100
        ),
    )
    assert informe(bueno, cfg).ok

    malo = con(bueno, entry_long=Condition(
        CompareOp.PCT_RANK_LT, Operand(ref="rsi"), Operand(const=40.0), lookback=100
    ))
    assert not informe(malo, cfg).ok


def test_comparar_un_precio_con_una_media_si_vale(simple, cfg) -> None:
    bueno = con(
        simple,
        entry_long=Condition(CompareOp.GT, Operand(price=PriceField.CLOSE), Operand(ref="lenta")),
    )
    assert informe(bueno, cfg).ok


def test_comparar_un_precio_con_un_oscilador_no(simple, cfg) -> None:
    roto = con(
        simple,
        features=(FeatureGene("rsi", "RSI", {"period": 14}), *simple.features),
        entry_long=Condition(CompareOp.GT, Operand(price=PriceField.CLOSE), Operand(ref="rsi")),
    )
    assert not informe(roto, cfg).ok


# --------------------------------------------------------------------------- #
# Entradas, salidas, riesgo, régimen y fusión                                  #
# --------------------------------------------------------------------------- #


def test_un_bot_sin_entrada_no_es_un_bot(simple, cfg) -> None:
    assert not informe(con(simple, entry_long=None), cfg).ok


def test_un_bot_sin_salida_tampoco(simple, cfg) -> None:
    assert not informe(con(simple, exit_long=None), cfg).ok


def test_si_permite_cortos_necesita_reglas_de_corto(simple, cfg) -> None:
    roto = con(simple, risk=dataclasses.replace(simple.risk, allow_short=True))
    r = informe(roto, cfg)
    assert not r.ok
    assert any("corto" in e.lower() for e in r.errors)


@pytest.mark.parametrize(
    "cambio",
    [
        {"risk_per_trade": 0.5},
        {"risk_per_trade": 0.0},
        {"max_holding_bars": 0},
        {"max_exposure": 1.5},
        {"max_concurrent_positions": 0},
    ],
)
def test_riesgo_fuera_de_limites(simple, cfg, cambio) -> None:
    roto = con(simple, risk=dataclasses.replace(simple.risk, **cambio))
    assert not informe(roto, cfg).ok


def test_regimen_activado_sin_regla(simple, cfg) -> None:
    roto = con(simple, regime=RegimeGene(enabled=True, rule=None))
    r = informe(roto, cfg)
    assert not r.ok
    assert any("régimen" in e or "regimen" in e for e in r.errors)


def test_una_fusion_no_tiene_reglas_propias(simple, cfg) -> None:
    roto = con(
        simple,
        ensemble=EnsembleGene(members=("a", "b"), weights=(0.5, 0.5)),
    )
    r = informe(roto, cfg)
    assert not r.ok
    assert any("fusión" in e or "fusion" in e for e in r.errors)


def test_una_fusion_demasiado_anidada(simple, cfg) -> None:
    roto = Genome(
        id="gen_fusion",
        family=IdeaFamily.HYBRID,
        ensemble=EnsembleGene(
            members=("a", "b"), weights=(0.5, 0.5), depth=cfg.fusion.max_depth + 1
        ),
        meta=GenomeMeta(operator=BreedOperator.FUSION),
    )
    r = informe(roto, cfg)
    assert not r.ok
    assert any("depth" in e or "anidamiento" in e for e in r.errors)


def test_una_fusion_bien_formada_vale(cfg) -> None:
    bueno = Genome(
        id="gen_fusion",
        family=IdeaFamily.HYBRID,
        ensemble=EnsembleGene(members=("a", "b", "c"), weights=(0.5, 0.3, 0.2)),
        meta=GenomeMeta(operator=BreedOperator.FUSION),
    )
    r = informe(bueno, cfg)
    assert r.ok, r.errors


def test_demasiados_miembros_en_la_fusion(cfg) -> None:
    miembros = tuple(f"bot_{i}" for i in range(cfg.fusion.max_members + 1))
    roto = Genome(
        id="gen_fusion",
        family=IdeaFamily.HYBRID,
        ensemble=EnsembleGene(members=miembros, weights=tuple(1.0 for _ in miembros)),
        meta=GenomeMeta(operator=BreedOperator.FUSION),
    )
    assert not informe(roto, cfg).ok


# --------------------------------------------------------------------------- #
# Modo estricto                                                                #
# --------------------------------------------------------------------------- #


def test_el_modo_estricto_levanta(simple, cfg) -> None:
    roto = con(simple, entry_long=None)
    with pytest.raises(GenomeInvalid):
        validate_genome(roto, cfg, DEFAULT_CATALOG, strict=True)


def test_el_modo_estricto_no_se_queja_de_un_aviso(simple, cfg) -> None:
    con_sobra = con(
        simple, features=(*simple.features, FeatureGene("huerfano", "RSI", {"period": 14}))
    )
    assert validate_genome(con_sobra, cfg, DEFAULT_CATALOG, strict=True).ok


# --------------------------------------------------------------------------- #
# Reparación                                                                   #
# --------------------------------------------------------------------------- #


def reparado_vale(genome: Genome, cfg) -> Genome:
    arreglado = repair_genome(genome, cfg, DEFAULT_CATALOG)
    r = validate_genome(arreglado, cfg, DEFAULT_CATALOG, strict=False)
    assert r.ok, f"la reparación dejó errores: {r.errors}"
    return arreglado


def test_reparar_una_referencia_rota(simple, cfg) -> None:
    roto = con(
        simple,
        entry_long=LogicNode(
            LogicOp.AND,
            (
                Condition(CompareOp.CROSS_ABOVE, Operand(ref="rapida"), Operand(ref="lenta")),
                Condition(CompareOp.GT, Operand(ref="fantasma"), Operand(const=1.0)),
            ),
        ),
    )
    arreglado = reparado_vale(roto, cfg)
    assert "fantasma" not in str(arreglado.to_dict())


def test_reparar_parametros_fuera_de_rango(simple, cfg) -> None:
    roto = con(simple, features=(FeatureGene("rapida", "EMA", {"period": 99999}),
                                 *simple.features[1:]))
    arreglado = reparado_vale(roto, cfg)
    assert arreglado.feature("rapida").params["period"] == 400


def test_reparar_ids_duplicados(simple, cfg) -> None:
    roto = con(simple, features=(*simple.features, FeatureGene("rapida", "RSI", {"period": 14})))
    arreglado = reparado_vale(roto, cfg)
    assert len(arreglado.feature_ids()) == len(arreglado.features)


def test_reparar_poda_los_huerfanos(simple, cfg) -> None:
    con_sobra = con(
        simple, features=(*simple.features, FeatureGene("huerfano", "RSI", {"period": 14}))
    )
    arreglado = repair_genome(con_sobra, cfg, DEFAULT_CATALOG)
    assert "huerfano" not in arreglado.feature_ids()


def test_reparar_recorta_las_hojas_de_mas(simple, cfg) -> None:
    hojas = tuple(
        Condition(CompareOp.GT, Operand(ref="rapida"), Operand(ref="lenta"))
        for _ in range(cfg.evolution.max_rule_leaves + 3)
    )
    arreglado = reparado_vale(con(simple, entry_long=LogicNode(LogicOp.AND, hojas)), cfg)
    assert len(rule_leaves(arreglado.entry_long)) <= cfg.evolution.max_rule_leaves


def test_reparar_recorta_la_profundidad(simple, cfg) -> None:
    nodo = Condition(CompareOp.GT, Operand(ref="rapida"), Operand(ref="lenta"))
    for _ in range(cfg.evolution.max_rule_depth + 3):
        nodo = LogicNode(LogicOp.AND, (nodo, nodo))
    arreglado = reparado_vale(con(simple, entry_long=nodo), cfg)
    assert arreglado.max_rule_depth() <= cfg.evolution.max_rule_depth


def test_reparar_recorta_el_riesgo(simple, cfg) -> None:
    roto = con(simple, risk=dataclasses.replace(simple.risk, risk_per_trade=0.9))
    arreglado = reparado_vale(roto, cfg)
    assert arreglado.risk.risk_per_trade <= cfg.risk.max_risk_per_trade


def test_reparar_recorta_los_features_de_mas(simple, cfg) -> None:
    muchos = (*simple.features, *(
        FeatureGene(f"extra{i}", "RSI", {"period": 10 + i})
        for i in range(cfg.evolution.max_features + 3)
    ))
    arreglado = reparado_vale(con(simple, features=muchos), cfg)
    assert len(arreglado.features) <= cfg.evolution.max_features


def test_un_genoma_sin_arreglo_posible_se_levanta(simple, cfg) -> None:
    irreparable = con(simple, entry_long=None, exit_long=None)
    with pytest.raises(GenomeInvalid):
        repair_genome(irreparable, cfg, DEFAULT_CATALOG)


def test_reparar_algo_sano_no_lo_cambia(simple, cfg) -> None:
    arreglado = repair_genome(simple, cfg, DEFAULT_CATALOG)
    assert arreglado.to_dict() == simple.to_dict()


def test_fuzz_de_reparacion(simple, cfg) -> None:
    """Genomas rotos a propósito: o se reparan, o se rechazan con GenomeInvalid.
    Lo que no puede pasar es que la reparación devuelva algo que no valida."""
    rng = random.Random(20260920)
    reparados = 0
    for _ in range(300):
        roto = _romper(simple, rng)
        try:
            arreglado = repair_genome(roto, cfg, DEFAULT_CATALOG)
        except GenomeInvalid:
            continue
        r = validate_genome(arreglado, cfg, DEFAULT_CATALOG, strict=False)
        assert r.ok, f"reparación incompleta: {r.errors}\n{roto.to_dict()}"
        reparados += 1
    assert reparados > 150, "la reparación está tirando demasiada descendencia"


def _romper(genome: Genome, rng: random.Random) -> Genome:
    """Rompe un genoma sano de una de las formas en que lo hace la evolución."""
    daño = rng.randrange(8)
    if daño == 0:
        return con(genome, entry_long=Condition(
            CompareOp.GT, Operand(ref=f"fantasma{rng.randrange(9)}"), Operand(const=1.0)
        ))
    if daño == 1:
        return con(genome, features=(*genome.features, genome.features[0]))
    if daño == 2:
        return con(genome, features=(
            FeatureGene("rapida", "EMA", {"period": rng.choice([-5, 0, 99999])}),
            *genome.features[1:],
        ))
    if daño == 3:
        hojas = tuple(
            Condition(CompareOp.GT, Operand(ref="rapida"), Operand(ref="lenta"))
            for _ in range(rng.randrange(7, 12))
        )
        return con(genome, entry_long=LogicNode(LogicOp.AND, hojas))
    if daño == 4:
        return con(genome, risk=dataclasses.replace(
            genome.risk, risk_per_trade=rng.choice([0.0, 0.9, -1.0]),
            max_holding_bars=rng.choice([0, -5, 240]),
        ))
    if daño == 5:
        return con(genome, features=(
            FeatureGene("rapida", "TELEPATIA", {"period": 20}), *genome.features[1:]
        ))
    if daño == 6:
        return con(genome, features=(
            *genome.features,
            *(FeatureGene(f"x{i}", "RSI", {"period": 10 + i}) for i in range(rng.randrange(4, 9))),
        ))
    return con(genome, regime=RegimeGene(enabled=True, rule=None))
