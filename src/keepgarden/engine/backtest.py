"""Backtest de un genoma sobre un rango de velas.

Es la unidad de trabajo de la incubadora y el comando ``keepgarden backtest``.
Debe ser puro y sin estado: mismo genoma + mismas velas = mismo resultado,
siempre, y ejecutable en un proceso worker sin nada compartido.

CONTRATO — implementar en el hito 3.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..config import Config
from ..genome.schema import Genome

if TYPE_CHECKING:  # pragma: no cover
    import numpy as np
    import pandas as pd


@dataclass(slots=True)
class BacktestResult:
    """Resultado crudo. Las métricas las calcula ``evaluation.metrics``."""

    genome_id: str
    start_ts: int
    end_ts: int
    n_bars: int
    trades: list[dict[str, object]] = field(default_factory=list)
    equity_curve: "np.ndarray | None" = None
    equity_ts: "np.ndarray | None" = None
    final_equity: float = 0.0
    total_fees: float = 0.0
    warmup_bars: int = 0
    aborted_reason: str = ""


def run_backtest(
    genome: Genome,
    candles: "pd.DataFrame",
    cfg: Config,
    *,
    features: object | None = None,
    initial_capital: float | None = None,
) -> BacktestResult:
    """Corre un genoma sobre un rango de velas.

    Secuencia por vela ``t``, idéntica a la del jardín vivo para que el backtest
    y el vivo no puedan divergir:

    1. Comprobar salidas de las posiciones abiertas con ``high``/``low`` de
       ``t`` (convención pesimista de ``engine.broker``).
    2. Evaluar la señal del genoma con datos cerrados hasta ``t``.
    3. Encolar la orden resultante.
    4. Rellenar las órdenes encoladas en la apertura de ``t+1``.
    5. Revalorar y anotar equity.

    Durante el calentamiento (``warmup_bars``) no se opera.

    Si la serie atraviesa un hueco largo de datos, cerrar posiciones al inicio
    del hueco y no reabrir hasta después (docs/DATA.md).

    Debe cumplir el test de no-look-ahead: correrlo sobre ``velas[0:n]`` da
    exactamente los mismos trades que correrlo sobre ``velas[0:n+500]``
    truncado a ``n``.
    """
    raise NotImplementedError


def run_batch(
    genomes: list[Genome], candles: "pd.DataFrame", cfg: Config, *, workers: int = 0
) -> list[BacktestResult]:
    """Corre muchos genomas en paralelo con ``multiprocessing``.

    Cada worker recibe el genoma serializado y las velas; devuelve el resultado.
    Sin estado compartido: es lo que hace la paralelización trivial y correcta.
    ``workers=0`` usa ``cpu_count() - 1``.
    """
    raise NotImplementedError


__all__ = ("BacktestResult", "run_backtest", "run_batch")
