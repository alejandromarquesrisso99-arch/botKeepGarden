"""Fusión: el operador que define este proyecto.

Dos estrategias mediocres pero descorrelacionadas, combinadas, producen una
curva con menos drawdown que cualquiera de las dos. Es la única comida gratis
que hay en esto, y el sistema la busca de forma explícita.

Reglas importantes (docs/EVOLUTION.md §FUSION):

* Los padres **siguen vivos**. La fusión no los consume, así se puede medir
  directamente si aporta: el hijo y sus padres corren en paralelo sobre los
  mismos datos.
* Un ensemble puede fusionarse otra vez, hasta ``fusion.max_depth`` niveles.
* Si un padre muere, el ensemble redistribuye su peso. Con menos de 2 miembros
  vivos, se jubila.

CONTRATO — implementar en el hito 4.
"""

from __future__ import annotations

import random
from typing import Mapping, Sequence

from ..config import Config
from ..genome.schema import Genome
from ..types import BotId


def select_fusion_candidates(
    alive: Sequence[Genome],
    correlations: Mapping[tuple[BotId, BotId], float],
    fitness: Mapping[BotId, float],
    ages: Mapping[BotId, int],
    cfg: Config,
    rng: random.Random,
) -> list[tuple[BotId, ...]]:
    """Encuentra grupos de padres que merezcan fusionarse.

    Criterios:

    * Todos vivos, con al menos ``fusion.min_parent_generations`` generaciones
      de historial **en vivo** (no basta con un buen backtest).
    * Correlación entre sus curvas de equity por debajo de
      ``fusion.max_parent_correlation``. La correlación es entre curvas, no
      entre genomas: dos genomas distintos pueden producir la misma curva y
      fusionarlos no aporta nada.
    * Fitness por encima de la mediana.
    * Profundidad de ensemble resultante ``<= fusion.max_depth``.

    Devuelve grupos de 2 a ``fusion.max_members``, priorizando los de
    correlación más baja: es donde está el beneficio.
    """
    raise NotImplementedError


def fuse(
    members: Sequence[Genome],
    metrics: Mapping[BotId, object],
    cfg: Config,
    rng: random.Random,
) -> Genome:
    """Crea el genoma ensemble.

    * Pesos iniciales proporcionales al Sortino de cada padre en la ventana,
      normalizados.
    * ``combine`` por defecto ``cfg.fusion.default_combine``; el jardinero
      puede proponer otro con ``FUSE``.
    * El gen de riesgo del hijo **es propio**: se hereda del padre con mejor
      Calmar. Un ensemble hereda señales, pero gestiona su propio riesgo.
    * La familia es ``HYBRID`` si los padres difieren, la común si coinciden.
    * ``ensemble.depth = 1 + max(depth de los miembros que sean ensembles)``.
    """
    raise NotImplementedError


def reweight_on_death(ensemble: Genome, dead_member: BotId) -> Genome | None:
    """Redistribuye el peso de un miembro muerto entre los vivos.

    Devuelve ``None`` si quedan menos de 2 miembros vivos: entonces el ensemble
    se jubila con causa ``ORPHAN_ENSEMBLE``. Registra el evento
    ``FUSION_REWEIGHTED``.
    """
    raise NotImplementedError


def combine_signals(
    signals: Sequence[int], weights: Sequence[float], combine: str, threshold: float
) -> int:
    """Combina las señales de los miembros en la del ensemble.

    Se combina la **posición deseada** (dentro/fuera), no los eventos de
    entrada: los eventos de miembros distintos rara vez coinciden en la misma
    vela, y combinarlos daría un ensemble que casi nunca opera.

    * ``VOTE``: mayoría simple.
    * ``WEIGHTED``: suma ponderada contra ``threshold``.
    * ``UNANIMOUS``: todos de acuerdo. Muy selectivo, pocas operaciones.
    * ``ANY``: cualquiera dispara. Muy activo, cuidado con el ``fee_drag``.
    """
    raise NotImplementedError


__all__ = ("select_fusion_candidates", "fuse", "reweight_on_death", "combine_signals")
