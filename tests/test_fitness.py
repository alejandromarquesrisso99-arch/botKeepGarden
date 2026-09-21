"""Fitness: normalización robusta, penalizaciones, Pareto y fitness efectivo.

Todas las cifras esperadas salen de docs/METRICS.md §Fitness. Si un número de
aquí cambia, es que ha cambiado el contrato, no el código.
"""

from __future__ import annotations

import math

import pytest

from keepgarden.config import FitnessConfig
from keepgarden.evaluation.fitness import (
    compute_fitness,
    effective_fitness,
    pareto_front,
    robust_reference,
    robust_z,
)
from keepgarden.evaluation.metrics import Metrics


def m(**kw) -> Metrics:
    """Métricas con lo mínimo para ser evaluables, más lo que se pida."""
    base = dict(n_trades=40, sortino=1.0, calmar=1.0, profit_factor=1.5, consistency=0.5)
    base.update(kw)
    return Metrics(**base)


# --------------------------------------------------------------------------- #
# z-score robusto                                                              #
# --------------------------------------------------------------------------- #


def test_z_robusto_usa_mediana_y_mad() -> None:
    z = robust_z([1.0, 2.0, 3.0, 4.0, 5.0])
    assert z[2] == pytest.approx(0.0)          # la mediana va al cero
    assert z[3] == pytest.approx(-z[1])        # simétrico alrededor de ella


def test_z_robusto_no_lo_arrastra_un_outlier() -> None:
    """Siempre hay un bot con un Sharpe absurdo por haber hecho tres
    operaciones. La media y la desviación lo siguen; la mediana y el MAD no."""
    normal = robust_z([1.0, 2.0, 3.0, 4.0, 5.0])
    con_outlier = robust_z([1.0, 2.0, 3.0, 4.0, 1000.0])
    assert con_outlier[:4] == pytest.approx(normal[:4])


def test_z_robusto_se_recorta() -> None:
    z = robust_z([1.0, 2.0, 3.0, 4.0, 1000.0], clip=3.0)
    assert max(z) == pytest.approx(3.0)
    assert min(z) >= -3.0


def test_z_robusto_con_mad_cero_devuelve_ceros() -> None:
    """Todos iguales no es división por cero ni NaN: es que nadie destaca."""
    assert robust_z([7.0, 7.0, 7.0]) == [0.0, 0.0, 0.0]


def test_z_robusto_con_lista_vacia_o_de_uno() -> None:
    assert robust_z([]) == []
    assert robust_z([3.0]) == [0.0]


# --------------------------------------------------------------------------- #
# El escalar                                                                   #
# --------------------------------------------------------------------------- #


def test_menos_operaciones_que_el_minimo_es_indefinido_no_bajo() -> None:
    """Matar a un bot por no haber operado todavía es matar a los selectivos."""
    cfg = FitnessConfig(min_trades=10)
    out = compute_fitness({"a": m(n_trades=3), "b": m(n_trades=40)}, cfg)
    assert not out["a"].is_defined
    assert "operaciones" in out["a"].undefined_reason
    assert math.isnan(out["a"].total)
    assert out["b"].is_defined


def test_los_indefinidos_no_contaminan_la_normalizacion() -> None:
    """La escala la fijan los bots con evidencia; los demás sólo esperan."""
    cfg = FitnessConfig()
    poblacion = {f"b{i}": m(sortino=float(i)) for i in range(1, 6)}
    solos = compute_fitness(dict(poblacion), cfg)
    con_mudos = compute_fitness(
        {**poblacion, "mudo": m(n_trades=0, sortino=99.0)}, cfg
    )
    for bot in poblacion:
        assert con_mudos[bot].total == pytest.approx(solos[bot].total)


def test_mas_sortino_es_mas_fitness() -> None:
    cfg = FitnessConfig()
    out = compute_fitness(
        {"peor": m(sortino=0.2), "medio": m(sortino=1.0), "mejor": m(sortino=3.0)}, cfg
    )
    assert out["mejor"].total > out["medio"].total > out["peor"].total


def test_el_ulcer_entra_con_signo_negativo() -> None:
    """Castiga los drawdowns largos: más ulcer no puede ser más fitness."""
    cfg = FitnessConfig()
    out = compute_fitness(
        {"liso": m(ulcer_index=0.01), "medio": m(ulcer_index=0.1), "feo": m(ulcer_index=0.4)},
        cfg,
    )
    assert out["liso"].total > out["medio"].total > out["feo"].total


def test_la_penalizacion_por_complejidad_es_la_de_la_doc() -> None:
    """Con la población entera idéntica, la base es 0 y sólo queda el castigo."""
    cfg = FitnessConfig()
    poblacion = {"simple": m(), "barroco": m()}
    out = compute_fitness(
        poblacion, cfg, complexities={"simple": 4, "barroco": 12},
        ages={"simple": 0, "barroco": 0},
    )
    assert out["simple"].penalties.get("complexity", 0.0) == pytest.approx(0.0)
    assert out["barroco"].penalties["complexity"] == pytest.approx(0.02 * (12 - 4))
    assert out["simple"].total - out["barroco"].total == pytest.approx(0.16)


def test_la_penalizacion_por_correlacion_empieza_en_el_umbral() -> None:
    cfg = FitnessConfig()
    out = compute_fitness(
        {"solo": m(corr_to_population=0.5), "gregario": m(corr_to_population=0.9)}, cfg
    )
    assert out["solo"].penalties.get("correlation", 0.0) == pytest.approx(0.0)
    assert out["gregario"].penalties["correlation"] == pytest.approx(0.30 * (0.9 - 0.6))


def test_la_penalizacion_por_comisiones_empieza_en_el_umbral() -> None:
    cfg = FitnessConfig()
    out = compute_fitness({"barato": m(fee_drag=0.2), "caro": m(fee_drag=0.8)}, cfg)
    assert out["barato"].penalties.get("fees", 0.0) == pytest.approx(0.0)
    assert out["caro"].penalties["fees"] == pytest.approx(0.25 * (0.8 - 0.30))


def test_el_bono_de_edad_satura() -> None:
    """Premia haber sobrevivido, que es la única evidencia no falsificable, pero
    sin convertir la veteranía en un trono."""
    cfg = FitnessConfig()
    out = compute_fitness(
        {"cria": m(), "veterano": m(), "abuelo": m()},
        cfg,
        ages={"cria": 0, "veterano": 8, "abuelo": 40},
    )
    assert out["cria"].bonuses.get("age", 0.0) == pytest.approx(0.0)
    assert out["veterano"].bonuses["age"] == pytest.approx(0.05)
    assert out["abuelo"].bonuses["age"] == pytest.approx(0.05)


def test_el_desglose_suma_el_total() -> None:
    """Que el fitness sea explicable no es cosmético: es lo que permite al
    jardinero diagnosticar por qué un bot está donde está."""
    cfg = FitnessConfig()
    out = compute_fitness(
        {
            "a": m(sortino=2.0, fee_drag=0.6, corr_to_population=0.8, novelty=0.5),
            "b": m(sortino=0.5, turnover=200.0),
            "c": m(sortino=1.2),
        },
        cfg,
        ages={"a": 5, "b": 1, "c": 12},
        complexities={"a": 9, "b": 4, "c": 6},
    )
    for f in out.values():
        esperado = f.base - sum(f.penalties.values()) + sum(f.bonuses.values())
        assert f.total == pytest.approx(esperado)


def test_ninguna_metrica_infinita_produce_un_fitness_infinito() -> None:
    cfg = FitnessConfig()
    out = compute_fitness(
        {"loco": m(sortino=1e18, calmar=-1e18), "cuerdo": m(), "otro": m(sortino=0.5)}, cfg
    )
    for f in out.values():
        assert math.isfinite(f.total)


def test_poblacion_vacia_no_revienta() -> None:
    assert compute_fitness({}, FitnessConfig()) == {}


# --------------------------------------------------------------------------- #
# Referencia congelada                                                         #
# --------------------------------------------------------------------------- #


def test_con_referencia_fija_el_fitness_es_comparable_entre_generaciones() -> None:
    """Sin referencia, la mediana del jardín es 0 por construcción y no se puede
    saber si el jardín está mejorando o sólo reordenándose."""
    cfg = FitnessConfig()
    gen0 = {f"b{i}": m(sortino=v) for i, v in enumerate([0.2, 0.4, 0.6, 0.8, 1.0])}
    gen9 = {f"c{i}": m(sortino=v) for i, v in enumerate([1.8, 2.0, 2.2, 2.4, 2.6])}
    ref = robust_reference(gen0, cfg)

    medios_gen0 = sorted(f.total for f in compute_fitness(gen0, cfg, reference=ref).values())
    medios_gen9 = sorted(f.total for f in compute_fitness(gen9, cfg, reference=ref).values())
    assert medios_gen9[2] > medios_gen0[2]

    # Sin referencia, las dos generaciones son indistinguibles.
    sueltos0 = sorted(f.total for f in compute_fitness(gen0, cfg).values())
    sueltos9 = sorted(f.total for f in compute_fitness(gen9, cfg).values())
    assert sueltos0[2] == pytest.approx(sueltos9[2])


# --------------------------------------------------------------------------- #
# Fitness efectivo                                                             #
# --------------------------------------------------------------------------- #


def test_con_menos_de_dos_generaciones_manda_la_incubadora() -> None:
    cfg = FitnessConfig()
    assert effective_fitness(1.5, -9.0, 0, cfg) == pytest.approx(1.5)
    assert effective_fitness(1.5, -9.0, 1, cfg) == pytest.approx(1.5)


def test_a_partir_de_dos_generaciones_manda_la_realidad() -> None:
    """El backtest sirve para llegar a la puerta; después manda el jardín."""
    cfg = FitnessConfig()
    assert effective_fitness(1.0, 2.0, 2, cfg) == pytest.approx(0.3 * 1.0 + 0.7 * 2.0)


def test_fitness_efectivo_sin_datos() -> None:
    cfg = FitnessConfig()
    assert effective_fitness(None, None, 5, cfg) is None
    assert effective_fitness(None, 2.0, 5, cfg) == pytest.approx(2.0)
    assert effective_fitness(1.0, None, 5, cfg) == pytest.approx(1.0)


# --------------------------------------------------------------------------- #
# Frente de Pareto                                                             #
# --------------------------------------------------------------------------- #


def test_pareto_sobre_sortino_ulcer_y_novedad() -> None:
    # (sortino ↑, ulcer ↓, novelty ↑)
    objetivos = {
        "domina":    (2.0, 0.1, 0.5),
        "dominado":  (1.0, 0.2, 0.4),
        "raro":      (0.3, 0.3, 0.9),
        "plano":     (1.5, 0.05, 0.1),
    }
    frente = pareto_front(objetivos, maximize=(True, False, True))
    assert "domina" in frente
    assert "raro" in frente       # nadie le gana en novedad
    assert "plano" in frente      # nadie le gana en ulcer
    assert "dominado" not in frente


def test_pareto_protege_a_los_raros() -> None:
    """Es lo que mantiene vivos a los que aún no han demostrado su valor, y de
    los que suelen salir los saltos."""
    objetivos = {f"gris{i}": (1.0, 0.2, 0.1) for i in range(10)}
    objetivos["raro"] = (0.1, 0.9, 0.99)
    assert "raro" in pareto_front(objetivos, maximize=(True, False, True))


def test_pareto_con_empates_los_mantiene_a_todos() -> None:
    objetivos = {"a": (1.0, 0.2), "b": (1.0, 0.2)}
    assert pareto_front(objetivos, maximize=(True, False)) == {"a", "b"}


def test_pareto_vacio() -> None:
    assert pareto_front({}, maximize=(True,)) == set()
