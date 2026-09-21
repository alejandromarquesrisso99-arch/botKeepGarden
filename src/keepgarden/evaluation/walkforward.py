"""Walk-forward con embargo y holdout intocable.

    |------ TRAIN ------|--emb--|-- VALID --|
                 |------ TRAIN+1 ------|--emb--|-- VALID+1 --|
                                                      ...  |--emb--|-- HOLDOUT --|
                                                                     (intocable)

La ventana de entrenamiento **desliza**: mide ``incubator.train_bars`` velas y
avanza un pliegue cada vez. Ver docs/DECISIONS.md D-021.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Sequence

from ..config import IncubatorConfig


class NotEnoughData(ValueError):
    """No hay velas suficientes para construir una partición honesta."""


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

    @property
    def touchable_end(self) -> int:
        """Primer índice que ya no se puede mirar durante la selección.

        Todo el código de la incubadora corta por aquí. El embargo entre la
        última validación y el holdout queda dentro de este tramo: son velas que
        nadie evalúa, pero que tampoco hay que esconder, porque su papel es
        justamente separar.
        """
        return self.holdout_start


def make_split(n_bars: int, cfg: IncubatorConfig) -> Split:
    """Construye la partición.

    1. Reservar las últimas ``holdout_bars`` velas como holdout. Intocables.
    2. Sobre el resto, construir ``n_folds`` pliegues: cada uno tiene una
       ventana de train de ``train_bars`` velas y, justo después, la validación.
    3. Insertar ``embargo_bars`` entre train y validación, y entre la última
       validación y el holdout. El embargo evita fugas por indicadores con
       ventana larga: sin él, una EMA(400) del final del train ya "sabe" cosas
       del principio de la validación.

    Las ventanas de validación se **reparten por todo el histórico evaluable**,
    de la más antigua posible a la más reciente. Amontonarlas contra el final
    parece atractivo —son las velas que más se parecen al mercado de mañana—
    pero entonces los cinco pliegues caen en el mismo régimen, y exigir la
    mediana de los cinco deja de significar "funciona en mercados distintos"
    para significar "funciona en éste". Sobre BTC/USDT eso equivalía a exigir
    que un bot sólo de largos ganara dinero en un año y medio de caídas: no
    nacía nadie. Ver docs/DECISIONS.md D-021.

    El train **desliza** en vez de acumularse desde el origen. Aquí no se ajusta
    ningún parámetro —el genoma llega hecho— y la única función del tramo de
    train es dar una referencia con la que comparar la validación. Una
    referencia de seis años contra una validación de tres meses no compara
    nada: mezcla regímenes que ya no existen y, de paso, garantiza que el freno
    de drawdown salte antes de llegar a los últimos pliegues. Ver
    docs/DECISIONS.md D-021.

    Raises:
        NotEnoughData: si no hay histórico para todos los pliegues. Es preferible
            a devolver pliegues degenerados de tres velas, que pasarían por
            evaluación y no medirían nada.
    """
    n_bars = int(n_bars)
    n_folds = max(1, int(cfg.n_folds))
    valid = int(cfg.validation_bars)
    embargo = int(cfg.embargo_bars)
    holdout = int(cfg.holdout_bars)

    if valid <= 0 or holdout <= 0 or embargo <= 0:
        raise NotEnoughData(
            "incubator.validation_bars, embargo_bars y holdout_bars deben ser > 0"
        )

    holdout_start = n_bars - holdout
    # El embargo previo al holdout se descuenta del tramo evaluable: la última
    # validación no puede pegarse al holdout más de lo que se pega al train.
    evaluable_end = holdout_start - embargo
    necesario = int(cfg.train_bars) + embargo + n_folds * valid + embargo + holdout
    if holdout_start <= 0 or evaluable_end <= 0 or n_bars < necesario:
        raise NotEnoughData(
            f"hacen falta al menos {necesario} velas para {n_folds} pliegues de "
            f"{valid} con {cfg.train_bars} de train, {embargo} de embargo y "
            f"{holdout} de holdout; hay {n_bars}"
        )

    # El primer pliegue acaba tan pronto como su train, su embargo y su
    # validación caben; el último, en el borde del holdout. Los de en medio se
    # reparten a pasos iguales entre esos dos.
    primero = int(cfg.train_bars) + embargo + valid
    paso_total = evaluable_end - primero

    folds: list[Fold] = []
    for i in range(n_folds):
        valid_end = primero + (i * paso_total) // max(1, n_folds - 1)
        valid_start = valid_end - valid
        train_end = valid_start - embargo
        if train_end < int(cfg.train_bars):
            raise NotEnoughData(
                f"el pliegue {i} se quedaría con {train_end} velas de train, por "
                f"debajo del mínimo de {cfg.train_bars}"
            )
        folds.append(
            Fold(
                index=i,
                train_start=train_end - int(cfg.train_bars),
                train_end=train_end,
                valid_start=valid_start,
                valid_end=valid_end,
            )
        )

    return Split(
        folds=tuple(folds),
        holdout_start=holdout_start,
        holdout_end=n_bars,
        total_bars=n_bars,
    )


def aggregate_folds(fold_metrics: Sequence[dict[str, float]], key: str) -> float:
    """Agrega una métrica entre pliegues usando la **mediana**.

    La mediana, no la media: castiga a los bots que dependen de un único periodo
    afortunado, que es exactamente lo que queremos filtrar.
    """
    valores = [
        float(m[key])
        for m in fold_metrics
        if key in m and m[key] is not None and float(m[key]) == float(m[key])
    ]
    if not valores:
        return 0.0
    return float(statistics.median(valores))


def oos_decay(train_value: float, valid_value: float, *, eps: float = 1e-9) -> float:
    """Degradación fuera de muestra: ``1 - valid / max(train, eps)``.

    0 = generaliza perfecto. 1 = todo lo que funcionaba en train desaparece
    fuera. Por encima de ``max_oos_decay`` el genoma no nace.

    Un train no positivo no se premia por serlo: si dentro de muestra el bot ya
    no ganaba nada, la degradación es total y no hay nada que generalizar.
    """
    train = float(train_value)
    valid = float(valid_value)
    if train <= eps:
        return 0.0 if valid >= train else 1.0
    return float(min(1.0, max(-1.0, 1.0 - valid / train)))


__all__ = (
    "Fold",
    "NotEnoughData",
    "Split",
    "make_split",
    "aggregate_folds",
    "oos_decay",
)
