"""Muestreo de genomas nuevos: el operador SEED.

El test que manda es ``test_mil_genomas_validan_y_compilan``: es el criterio de
aceptación del hito 2. Un sembrador que produce basura el 99 % de las veces
condena a la incubadora a gastar toda su potencia tirando bots.
"""

from __future__ import annotations

import random

import pytest

from conftest import build_walk
from keepgarden.data.indicators import IndicatorCache
from keepgarden.genome.catalog import (
    DEFAULT_CATALOG,
    SEEDABLE_FAMILIES,
    load_catalog,
    spec,
)
from keepgarden.genome.compile import compile_genome
from keepgarden.genome.random_genome import (
    random_genome,
    random_population,
    random_risk_gene,
)
from keepgarden.genome.schema import MarketSpec, genome_hash, rule_leaves
from keepgarden.genome.serialize import dump_genome_str, load_genome_str
from keepgarden.genome.validate import validate_genome
from keepgarden.types import IdeaFamily, Signal


@pytest.fixture(scope="session")
def catalogo(project_root):
    return load_catalog(project_root / "config" / "genes.yaml")


@pytest.fixture
def market() -> MarketSpec:
    return MarketSpec(venue="binance", symbol="BTC/USDT", timeframe="1h")


def sembrar(family, market, cfg, catalogo, semilla: int = 1):
    return random_genome(family, market, cfg, catalogo, random.Random(semilla))


# --------------------------------------------------------------------------- #
# El criterio de aceptación                                                    #
# --------------------------------------------------------------------------- #


def test_mil_genomas_validan_y_compilan(market, cfg, catalogo) -> None:
    """Criterio de aceptación del hito 2."""
    rng = random.Random(20260920)
    velas = build_walk(1500)
    cache = IndicatorCache(candles=velas)

    con_entrada = 0
    for i in range(1000):
        family = SEEDABLE_FAMILIES[i % len(SEEDABLE_FAMILIES)]
        genome = random_genome(family, market, cfg, catalogo, rng)

        r = validate_genome(genome, cfg, catalogo, strict=False)
        assert r.ok, f"genoma {i} ({family}) inválido: {r.errors}\n{genome.describe()}"

        compilado = compile_genome(genome, velas, cache)
        assert len(compilado.codes) == len(velas)
        if (compilado.signals() == Signal.ENTER_LONG).any():
            con_entrada += 1

    # No todos tienen que disparar en dos meses de mercado, pero si casi ninguno
    # lo hace es que el muestreo está produciendo reglas imposibles.
    assert con_entrada > 600, f"sólo {con_entrada} de 1000 genomas llegan a entrar"


def test_los_genomas_sembrados_se_serializan_ida_y_vuelta(market, cfg, catalogo) -> None:
    rng = random.Random(7)
    for family in SEEDABLE_FAMILIES:
        g = random_genome(family, market, cfg, catalogo, rng)
        assert load_genome_str(dump_genome_str(g)).to_dict() == g.to_dict()


# --------------------------------------------------------------------------- #
# Determinismo                                                                 #
# --------------------------------------------------------------------------- #


def test_la_misma_semilla_da_el_mismo_genoma(market, cfg, catalogo) -> None:
    a = sembrar(IdeaFamily.TREND, market, cfg, catalogo, 42)
    b = sembrar(IdeaFamily.TREND, market, cfg, catalogo, 42)
    assert a.to_dict(include_meta=False) == b.to_dict(include_meta=False)


def test_semillas_distintas_dan_genomas_distintos(market, cfg, catalogo) -> None:
    hashes = {
        genome_hash(sembrar(IdeaFamily.TREND, market, cfg, catalogo, s)) for s in range(30)
    }
    assert len(hashes) > 20, "el muestreo se repite demasiado"


def test_la_misma_semilla_da_la_misma_poblacion(market, cfg, catalogo) -> None:
    a = random_population(20, market, cfg, catalogo, random.Random(3))
    b = random_population(20, market, cfg, catalogo, random.Random(3))
    assert [g.to_dict(include_meta=False) for g in a] == [
        g.to_dict(include_meta=False) for g in b
    ]


# --------------------------------------------------------------------------- #
# Forma del genoma sembrado                                                    #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("family", SEEDABLE_FAMILIES)
def test_cada_familia_tiene_su_forma(family, market, cfg, catalogo) -> None:
    rng = random.Random(11)
    for _ in range(25):
        g = random_genome(family, market, cfg, catalogo, rng)
        assert g.family is family
        assert g.market == market
        assert g.meta.operator.value == "SEED"

        tpl = catalogo.templates[family]
        categorias = {spec(f.kind).category for f in g.features}
        for requerida in tpl.required_categories:
            assert requerida in categorias, f"{family} sin indicador de {requerida}"


@pytest.mark.parametrize("family", SEEDABLE_FAMILIES)
def test_el_numero_de_features_respeta_la_plantilla(family, market, cfg, catalogo) -> None:
    rng = random.Random(5)
    low, high = catalogo.templates[family].n_features
    for _ in range(25):
        g = random_genome(family, market, cfg, catalogo, rng)
        # la horquilla cuenta los indicadores de las reglas; el ATR del stop y
        # el filtro de régimen van aparte
        assert low <= len(g.features) <= min(high + 2, cfg.evolution.max_features)


def test_todo_feature_sembrado_se_usa(market, cfg, catalogo) -> None:
    rng = random.Random(9)
    for family in SEEDABLE_FAMILIES:
        for _ in range(20):
            g = random_genome(family, market, cfg, catalogo, rng)
            assert g.feature_ids() == g.all_referenced_features()


def test_hay_entrada_y_salida_y_no_son_la_misma(market, cfg, catalogo) -> None:
    rng = random.Random(13)
    distintas = 0
    for _ in range(60):
        g = random_genome(IdeaFamily.TREND, market, cfg, catalogo, rng)
        assert g.entry_long is not None and g.exit_long is not None
        if g.entry_long.to_dict() != g.exit_long.to_dict():
            distintas += 1
    assert distintas == 60, "la salida nunca debe ser idéntica a la entrada"


def test_en_spot_no_se_siembran_cortos(market, cfg, catalogo) -> None:
    rng = random.Random(17)
    for family in SEEDABLE_FAMILIES:
        for _ in range(10):
            g = random_genome(family, market, cfg, catalogo, rng)
            assert g.risk.allow_short is False
            assert g.entry_short is None and g.exit_short is None


def test_las_reglas_caben_en_los_limites(market, cfg, catalogo) -> None:
    rng = random.Random(19)
    for family in SEEDABLE_FAMILIES:
        for _ in range(20):
            g = random_genome(family, market, cfg, catalogo, rng)
            assert len(g.features) <= cfg.evolution.max_features
            assert g.max_rule_depth() <= cfg.evolution.max_rule_depth
            for _, tree in g.rule_trees():
                assert len(rule_leaves(tree)) <= cfg.evolution.max_rule_leaves


def test_los_parametros_caen_dentro_del_catalogo(market, cfg, catalogo) -> None:
    rng = random.Random(23)
    for family in SEEDABLE_FAMILIES:
        for _ in range(20):
            g = random_genome(family, market, cfg, catalogo, rng)
            for gene in g.features:
                s = spec(gene.kind)
                assert set(gene.params) == {p.name for p in s.params}
                for p in s.params:
                    assert p.low <= gene.params[p.name] <= p.high


def test_el_filtro_de_regimen_aparece_segun_la_plantilla(market, cfg, catalogo) -> None:
    rng = random.Random(29)
    con_regimen = sum(
        1
        for _ in range(200)
        if (g := random_genome(IdeaFamily.MEAN_REVERSION, market, cfg, catalogo, rng)).regime
        is not None
        and g.regime.enabled
    )
    esperado = catalogo.templates[IdeaFamily.MEAN_REVERSION].prefers_regime
    assert abs(con_regimen / 200 - esperado) < 0.15


# --------------------------------------------------------------------------- #
# Gen de riesgo                                                                #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("family", SEEDABLE_FAMILIES)
def test_el_riesgo_respeta_los_limites_de_la_config(family, cfg, catalogo) -> None:
    rng = random.Random(31)
    for _ in range(50):
        risk = random_risk_gene(family, cfg, catalogo, rng)
        assert cfg.risk.min_risk_per_trade <= risk.risk_per_trade <= cfg.risk.max_risk_per_trade
        assert risk.max_holding_bars > 0
        assert 0.0 < risk.max_exposure <= 1.0
        assert risk.allow_short is False


def test_el_riesgo_sigue_los_priores_de_la_familia(cfg, catalogo) -> None:
    rng = random.Random(37)
    priors = catalogo.risk_priors[IdeaFamily.TREND]
    low, high = priors["stop"]["value"]
    for _ in range(50):
        risk = random_risk_gene(IdeaFamily.TREND, cfg, catalogo, rng)
        assert str(risk.stop.kind) == priors["stop"]["kind"]
        assert low <= risk.stop.value <= high


def test_el_riesgo_funciona_sin_priores(cfg) -> None:
    """El catálogo de código no trae priores: sembrar sin genes.yaml debe valer."""
    rng = random.Random(41)
    risk = random_risk_gene(IdeaFamily.TREND, cfg, DEFAULT_CATALOG, rng)
    assert cfg.risk.min_risk_per_trade <= risk.risk_per_trade <= cfg.risk.max_risk_per_trade


# --------------------------------------------------------------------------- #
# Población inicial                                                            #
# --------------------------------------------------------------------------- #


def test_la_poblacion_tiene_el_tamano_pedido(market, cfg, catalogo) -> None:
    poblacion = random_population(60, market, cfg, catalogo, random.Random(2))
    assert len(poblacion) == 60


def test_la_poblacion_no_tiene_clones(market, cfg, catalogo) -> None:
    poblacion = random_population(60, market, cfg, catalogo, random.Random(4))
    assert len({genome_hash(g) for g in poblacion}) == 60


def test_la_poblacion_reparte_entre_familias(market, cfg, catalogo) -> None:
    poblacion = random_population(60, market, cfg, catalogo, random.Random(6))
    cuenta: dict[IdeaFamily, int] = {}
    for g in poblacion:
        cuenta[g.family] = cuenta.get(g.family, 0) + 1
    assert set(cuenta) == set(SEEDABLE_FAMILIES)
    for family, n in cuenta.items():
        assert n / 60 <= cfg.evolution.max_family_share + 1e-9, family


def test_una_poblacion_pequena_no_se_atasca(market, cfg, catalogo) -> None:
    assert len(random_population(3, market, cfg, catalogo, random.Random(8))) == 3


def test_se_puede_pedir_una_sola_familia(market, cfg, catalogo) -> None:
    poblacion = random_population(
        12, market, cfg, catalogo, random.Random(10), families=(IdeaFamily.BREAKOUT,)
    )
    assert {g.family for g in poblacion} == {IdeaFamily.BREAKOUT}


def test_toda_la_poblacion_valida(market, cfg, catalogo) -> None:
    for g in random_population(40, market, cfg, catalogo, random.Random(12)):
        assert validate_genome(g, cfg, catalogo, strict=False).ok


def test_la_poblacion_opera_de_verdad(market, cfg, catalogo) -> None:
    """Un jardín cuya siembra no dispara ninguna señal no es un jardín."""
    velas = build_walk(2000)
    cache = IndicatorCache(candles=velas)
    poblacion = random_population(40, market, cfg, catalogo, random.Random(14))
    activos = [
        g for g in poblacion
        if (compile_genome(g, velas, cache).signals() == Signal.ENTER_LONG).any()
    ]
    assert len(activos) >= 20
