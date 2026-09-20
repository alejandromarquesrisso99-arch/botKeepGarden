"""El reloj del jardín: el paso del tiempo.

Dos modos:

* **Vivo**: espera al cierre de cada vela de 1h y pide la nueva al venue.
* **Acelerado** (``--dry-run --speed N``): recorre histórico como si fuera vivo,
  sin esperas. Es lo que permite validar el hito 5 en minutos en vez de meses, y
  lo que usan los tests del bucle completo.

CONTRATO — implementar en el hito 5.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterator

from ..config import Config
from ..types import Timestamp

if TYPE_CHECKING:  # pragma: no cover
    import pandas as pd


@dataclass(frozen=True, slots=True)
class Tick:
    """Una vela cerrada lista para procesar."""

    ts: Timestamp
    generation: int
    index: int                 # posición en la serie histórica
    is_catchup: bool = False   # recuperando velas perdidas tras una caída
    closes_generation: bool = False


@dataclass(slots=True)
class MarketClock:
    """Produce ticks, en vivo o acelerados."""

    cfg: Config
    start_generation: int = 0
    last_processed_ts: Timestamp | None = None

    def ticks(self) -> Iterator[Tick]:
        """Itera ticks indefinidamente (modo vivo).

        Bucle esperado:

        1. Dormir hasta ``próxima hora en punto + settle_delay_seconds``.
        2. Pedir las últimas K velas cerradas al venue.
        3. Descartar las que ya estén procesadas (``ts <= last_processed_ts``).
        4. Si quedan varias, estuvimos caídos: emitirlas en orden marcadas como
           ``is_catchup``, hasta ``execution.max_catchup_candles``.
        5. Marcar ``closes_generation`` cuando el contador de ticks de la
           generación llega a ``garden.ticks_per_generation``.

        Ante fallo del venue: reintentar con backoff. Tras
        ``venue_failure_threshold`` fallos seguidos, emitir el evento
        ``CIRCUIT_BREAKER`` y poner el jardín en ``DEGRADED`` — sigue
        registrando, no opera.
        """
        raise NotImplementedError

    def replay(self, candles: "pd.DataFrame", speed: float = 0.0) -> Iterator[Tick]:
        """Recorre histórico como si fuera vivo.

        ``speed`` 0 significa lo más rápido posible; 1000 significa 1000 velas
        por segundo de reloj real (útil para ver el dashboard moverse).

        Debe producir exactamente la misma secuencia de decisiones que el modo
        vivo sobre los mismos datos. Si no, el dry-run no sirve para validar
        nada.
        """
        raise NotImplementedError

    def next_candle_open(self, now_ms: Timestamp) -> Timestamp:
        """Timestamp de apertura de la siguiente vela del timeframe operativo."""
        raise NotImplementedError

    def generation_of(self, ts: Timestamp) -> int:
        """Generación a la que pertenece una vela, contando desde el arranque."""
        raise NotImplementedError


__all__ = ("Tick", "MarketClock")
