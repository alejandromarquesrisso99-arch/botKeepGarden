"""Walk-forward anclado con embargo y holdout intocable.

    |------ TRAIN ------|--emb--|-- VALID --|--emb--|-- TRAIN+1 ...
                                                             ...  |-- HOLDOUT --|
                                                                   (intocable)

CONTRATO — implementar en el hito 4.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from ..config import IncubatorConfig


@dataclass(frozen=True, slots=True)
class Fold:
    """Un pliegue de walk-forward, en índices de vela."""

    index: int
    train_start: int
    train_end: int
    valid_start: int
    valid_end: int

    @property
    def train_bars(self) -> int:
        return self.train_end - self.train_start

    @property
    def valid_bars(self) -> int:
        return self.valid_end - self.valid_start


@dataclass(frozen=True, slots=True)
class Split:
    """Partición completa de la serie."""

    folds: tuple[Fold, ...]
    holdout_start: int
    holdout_end: int
    total_bars: int

    def is_in_holdout(self, index: int) -> bool:
        return self.holdout_start <= index < self.holdout_end


def make_split(n_bars: int, cfg: IncubatorConfig) -> Split:
    """Construye la partición.

    1. Reservar las últimas ``holdout_bars`` velas como holdout. Intocables.
    2. Sobre el resto, construir ``n_folds`` pliegues anclados: el train empieza
       siempre al principio y crece; la validación es la ventana siguiente.
    3. Insertar ``embargo_bars`` entre train y validación, y entre la última
       validación y el holdout. El embargo evita fugas por indicadores con
       ventana larga: sin él, una EMA(400) del final del train ya "sabe" cosas
       del principio de la validación.

    Debe fallar con un error claro si no hay datos suficientes, en vez de
    devolver pliegues degenerados de 3 velas.
    """
    raise NotImplementedError


def aggregate_folds(fold_metrics: Sequence[dict[str, float]], key: str) -> float:
    """Agrega una métrica entre pliegues usando la **mediana**.

    La mediana, no la media: castiga a los bots que dependen de un único periodo
    afortunado, que es exactamente lo que queremos filtrar.
    """
    raise NotImplementedError


def oos_decay(train_value: float, valid_value: float, *, eps: float = 1e-9) -> float:
    """Degradación fuera de muestra: ``1 - valid / max(train, eps)``.

    0 = generaliza perfecto. 1 = todo lo que funcionaba en train desaparece
    fuera. Por encima de ``max_oos_decay`` el genoma no nace.
    """
    raise NotImplementedError


__all__ = ("Fold", "Split", "make_split", "aggregate_folds", "oos_decay")
