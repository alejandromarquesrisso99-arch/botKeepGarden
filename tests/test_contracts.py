"""Invariantes de forma que todo módulo debe cumplir, esté implementado o no.

Comprueba que cada módulo importa, declara ``__all__`` coherente y documenta lo
que expone. Un contrato sin docstring no es un contrato: es un hueco, y quien lo
implemente después no tendrá forma de saber qué se esperaba.

``CONTRACT_MODULES`` es además el inventario vivo de lo que falta: al
implementar un contrato, muévelo a ``IMPLEMENTED_MODULES`` y actualiza la lista
de estado de CLAUDE.md.
"""

from __future__ import annotations

import importlib
import inspect

import pytest

CONTRACT_MODULES = [
    "keepgarden.engine.clock",
    "keepgarden.engine.runner",
    "keepgarden.gardener.report",
    "keepgarden.gardener.apply",
    "keepgarden.gardener.journal",
    "keepgarden.dashboard.api",
    "keepgarden.dashboard.app",
]

IMPLEMENTED_MODULES = [
    "keepgarden.types",
    "keepgarden.config",
    "keepgarden.ids",
    "keepgarden.cli",
    "keepgarden.genome.schema",
    "keepgarden.genome.catalog",
    "keepgarden.genome.serialize",
    "keepgarden.evolution.lineage",
    "keepgarden.gardener.proposals",
    "keepgarden.data.candles",
    "keepgarden.data.sources",
    "keepgarden.data.store",
    "keepgarden.data.backfill",
    "keepgarden.data.indicators",
    "keepgarden.genome.compile",
    "keepgarden.genome.validate",
    "keepgarden.genome.random_genome",
    "keepgarden.engine.broker",
    "keepgarden.engine.portfolio",
    "keepgarden.engine.backtest",
    "keepgarden.evaluation.metrics",
    "keepgarden.genome.distance",
    "keepgarden.engine.incubator",
    "keepgarden.evaluation.fitness",
    "keepgarden.evaluation.walkforward",
    "keepgarden.evolution.mutation",
    "keepgarden.evolution.crossover",
    "keepgarden.evolution.fusion",
    "keepgarden.evolution.selection",
    "keepgarden.evolution.speciation",
    "keepgarden.evolution.population",
    "keepgarden.storage.db",
    "keepgarden.storage.repositories",
]


@pytest.mark.parametrize("name", CONTRACT_MODULES + IMPLEMENTED_MODULES)
def test_todo_modulo_importa(name: str) -> None:
    importlib.import_module(name)


@pytest.mark.parametrize("name", CONTRACT_MODULES + IMPLEMENTED_MODULES)
def test_todo_modulo_declara_all(name: str) -> None:
    mod = importlib.import_module(name)
    assert hasattr(mod, "__all__"), f"{name} debe declarar __all__"
    for symbol in mod.__all__:
        assert hasattr(mod, symbol), f"{name}.__all__ menciona '{symbol}', que no existe"


@pytest.mark.parametrize("name", CONTRACT_MODULES)
def test_todo_contrato_esta_documentado(name: str) -> None:
    """Un contrato sin docstring no es un contrato: es un hueco."""
    mod = importlib.import_module(name)
    assert mod.__doc__, f"{name} necesita docstring de módulo"
    for symbol in mod.__all__:
        obj = getattr(mod, symbol)
        if inspect.isfunction(obj) or inspect.isclass(obj):
            assert obj.__doc__, f"{name}.{symbol} necesita docstring"
