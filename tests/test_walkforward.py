"""Particiones de walk-forward: embargo, anclaje y holdout intocable."""

from __future__ import annotations

import dataclasses

import pytest

from keepgarden.config import IncubatorConfig
from keepgarden.evaluation.walkforward import (
    NotEnoughData,
    aggregate_folds,
    make_split,
    oos_decay,
)


@pytest.fixture
def inc() -> IncubatorConfig:
    """Una configuración pequeña, para poder razonar los índices a mano."""
    return IncubatorConfig(
        n_folds=3,
        train_bars=100,
        validation_bars=50,
        embargo_bars=10,
        holdout_bars=80,
    )


def test_el_holdout_son_las_ultimas_velas(inc: IncubatorConfig) -> None:
    split = make_split(1000, inc)
    assert split.holdout_start == 1000 - inc.holdout_bars
    assert split.holdout_end == 1000
    assert split.is_in_holdout(999)
    assert not split.is_in_holdout(split.holdout_start - 1)


def test_ningun_pliegue_toca_el_holdout(inc: IncubatorConfig) -> None:
    split = make_split(1000, inc)
    for fold in split.folds:
        assert fold.valid_end <= split.holdout_start
        assert not split.is_in_holdout(fold.valid_end - 1)
        assert not split.is_in_holdout(fold.train_end - 1)


def test_hay_embargo_entre_train_y_validacion(inc: IncubatorConfig) -> None:
    split = make_split(1000, inc)
    for fold in split.folds:
        assert fold.valid_start - fold.train_end == inc.embargo_bars


def test_hay_embargo_antes_del_holdout(inc: IncubatorConfig) -> None:
    """Sin él, un indicador de ventana larga llega al holdout desde la última
    validación y el único juez honesto del sistema deja de serlo."""
    split = make_split(1000, inc)
    ultima = split.folds[-1]
    assert split.holdout_start - ultima.valid_end >= inc.embargo_bars


def test_el_train_desliza_con_tamano_fijo(inc: IncubatorConfig) -> None:
    """Ver DECISIONS D-021: aquí no se ajusta ningún parámetro, así que el train
    sólo sirve de referencia contra la que comparar la validación, y una
    referencia tiene que medir lo mismo en todos los pliegues."""
    split = make_split(1000, inc)
    assert all(f.train_bars == inc.train_bars for f in split.folds)
    inicios = [f.train_start for f in split.folds]
    assert inicios == sorted(inicios)
    assert len(set(inicios)) == len(inicios)
    assert all(f.train_start >= 0 for f in split.folds)


def test_cada_train_termina_justo_antes_de_su_validacion(inc: IncubatorConfig) -> None:
    split = make_split(1000, inc)
    for fold in split.folds:
        assert fold.train_end < fold.valid_start
        assert fold.train_start < fold.train_end


def test_las_validaciones_no_se_solapan(inc: IncubatorConfig) -> None:
    split = make_split(1000, inc)
    for anterior, siguiente in zip(split.folds, split.folds[1:]):
        assert siguiente.valid_start >= anterior.valid_end
    assert all(f.valid_bars == inc.validation_bars for f in split.folds)


def test_las_validaciones_recorren_todo_el_historico(inc: IncubatorConfig) -> None:
    """Amontonarlas al final haría que los cinco pliegues cayeran en el mismo
    régimen, y la mediana dejaría de significar 'funciona en mercados
    distintos' para significar 'funciona en éste'. Ver DECISIONS D-021."""
    split = make_split(3000, inc)
    primera, ultima = split.folds[0], split.folds[-1]
    assert primera.valid_end == inc.train_bars + inc.embargo_bars + inc.validation_bars
    assert ultima.valid_end == split.holdout_start - inc.embargo_bars
    recorrido = ultima.valid_end - primera.valid_start
    assert recorrido > 0.6 * (3000 - inc.holdout_bars)


def test_la_ultima_validacion_llega_hasta_donde_puede(inc: IncubatorConfig) -> None:
    """Y la última sí valida sobre las velas más recientes que se pueden
    mirar, que son las que más se parecen al mercado de mañana."""
    split = make_split(1000, inc)
    assert split.folds[-1].valid_end == split.holdout_start - inc.embargo_bars


def test_el_numero_de_pliegues_es_el_pedido(inc: IncubatorConfig) -> None:
    assert len(make_split(1000, inc).folds) == inc.n_folds
    assert [f.index for f in make_split(1000, inc).folds] == [0, 1, 2]


def test_sin_datos_suficientes_falla_en_vez_de_degenerar(inc: IncubatorConfig) -> None:
    with pytest.raises(NotEnoughData):
        make_split(200, inc)


def test_el_minimo_de_train_se_respeta(inc: IncubatorConfig) -> None:
    justo = inc.train_bars + inc.embargo_bars * 2 + inc.n_folds * inc.validation_bars
    justo += inc.holdout_bars
    split = make_split(justo, inc)
    assert split.folds[0].train_bars >= inc.train_bars
    with pytest.raises(NotEnoughData):
        make_split(justo - 1, inc)


def test_la_configuracion_real_parte_el_historico_de_btc(cfg) -> None:
    """Siete años de velas de 1h dan de sobra para los cinco pliegues reales."""
    split = make_split(67_000, cfg.incubator)
    assert len(split.folds) == cfg.incubator.n_folds
    assert split.folds[0].train_bars >= cfg.incubator.train_bars
    assert split.holdout_start == 67_000 - cfg.incubator.holdout_bars


def test_agregar_usa_la_mediana_y_no_la_media() -> None:
    """Un pliegue afortunado no puede arrastrar al resto: es justo el bot que la
    incubadora tiene que filtrar."""
    folds = [{"sortino": 0.1}, {"sortino": 0.2}, {"sortino": 0.3}, {"sortino": 40.0}]
    assert aggregate_folds(folds, "sortino") == pytest.approx(0.25)


def test_agregar_ignora_pliegues_sin_la_metrica() -> None:
    assert aggregate_folds([{"sortino": 1.0}, {}], "sortino") == pytest.approx(1.0)
    assert aggregate_folds([], "sortino") == 0.0
    assert aggregate_folds([{"sortino": float("nan")}], "sortino") == 0.0


def test_degradacion_fuera_de_muestra() -> None:
    assert oos_decay(2.0, 2.0) == pytest.approx(0.0)
    assert oos_decay(2.0, 1.0) == pytest.approx(0.5)
    assert oos_decay(2.0, 0.0) == pytest.approx(1.0)
    assert oos_decay(1.0, 2.0) == pytest.approx(-1.0)


def test_degradacion_con_train_inutil_no_premia() -> None:
    """Un bot que tampoco ganaba dentro de muestra no ha generalizado nada."""
    assert oos_decay(0.0, -1.0) == pytest.approx(1.0)
    assert oos_decay(-1.0, -2.0) == pytest.approx(1.0)
    assert oos_decay(0.0, 0.5) == pytest.approx(0.0)


def test_la_particion_es_determinista(inc: IncubatorConfig) -> None:
    assert make_split(1000, inc) == make_split(1000, inc)


def test_holdout_cero_esta_prohibido(inc: IncubatorConfig) -> None:
    """Sin holdout no hay forma honesta de saber si un bot generaliza."""
    with pytest.raises(NotEnoughData):
        make_split(1000, dataclasses.replace(inc, holdout_bars=0))
