"""El reloj del jardín: el paso del tiempo.

Dos modos:

* **Vivo**: espera al cierre de cada vela de 1h y pide la nueva al venue.
* **Acelerado** (``--dry-run --speed N``): recorre histórico como si fuera vivo,
  sin esperas. Es lo que permite validar el hito 5 en minutos en vez de meses, y
  lo que usan los tests del bucle completo.

El reloj no toca la base ni el venue: recibe por parámetro cómo pedir velas,
cómo dormir y qué hora es. Así el modo vivo se puede probar entero sin red y sin
esperar una hora por tick, que es la única forma de que esté probado.
"""

from __future__ import annotations

import time as _time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..config import Config
from ..types import TIMEFRAME_MS, Timestamp

if TYPE_CHECKING:  # pragma: no cover
    import pandas as pd

#: Espera entre reintentos tras un fallo del venue, en segundos. Crece con los
#: fallos seguidos y se corta ahí: más allá no se gana nada y se pierde una vela.
BACKOFF_SECONDS: tuple[float, ...] = (5.0, 15.0, 30.0, 60.0, 120.0)


def _now_ms() -> Timestamp:
    return int(_time.time() * 1000)


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

    #: Cómo pedir velas nuevas en modo vivo. Recibe el último ts procesado y
    #: devuelve pares ``(ts, índice en la serie del jardín)`` ya persistidos y
    #: ordenados. Lo inyecta el runner, que es quien tiene venue y caché.
    fetch: Callable[[Timestamp | None], Sequence[tuple[Timestamp, int]]] | None = None
    #: Inyectables para poder probar el modo vivo sin red y sin esperas.
    sleep: Callable[[float], None] = _time.sleep
    now: Callable[[], Timestamp] = _now_ms
    #: Se llama con (fallos seguidos, excepción) cada vez que el venue falla.
    #: El runner lo usa para registrar el evento y degradar el jardín.
    on_venue_failure: Callable[[int, BaseException], None] | None = None

    #: Ticks ya procesados de la generación en curso. Al reanudar tras una
    #: caída lo rellena el runner: si no, la generación se cerraría tarde.
    ticks_in_generation: int = 0
    _generations_done: int = field(default=0, repr=False)

    # -- utilidades -------------------------------------------------------- #

    @property
    def timeframe_ms(self) -> int:
        return TIMEFRAME_MS[str(self.cfg.market.timeframe)]

    def next_candle_open(self, now_ms: Timestamp) -> Timestamp:
        """Timestamp de apertura de la siguiente vela del timeframe operativo."""
        tf = self.timeframe_ms
        return (int(now_ms) // tf + 1) * tf

    def generation_of(self, ts: Timestamp) -> int:
        """Generación a la que pertenece una vela, contando desde el arranque.

        Cuenta velas, no ticks procesados: si el jardín estuvo caído, las velas
        perdidas siguen perteneciendo a su generación. Con huecos en los datos
        manda el contador de ``ticks_in_generation``, que es el que usan
        ``replay`` y ``ticks``.
        """
        if self.last_processed_ts is None:
            return self.start_generation + 1
        transcurridas = (int(ts) - int(self.last_processed_ts)) // self.timeframe_ms
        pasadas = (self.ticks_in_generation + max(0, transcurridas - 1)) // (
            self.cfg.garden.ticks_per_generation
        )
        return self.start_generation + 1 + self._generations_done + pasadas

    def _emit(self, ts: Timestamp, index: int, *, is_catchup: bool = False) -> Tick:
        """Construye el tick y avanza los contadores de generación."""
        self.ticks_in_generation += 1
        cierra = self.ticks_in_generation >= self.cfg.garden.ticks_per_generation
        tick = Tick(
            ts=int(ts),
            generation=self.start_generation + 1 + self._generations_done,
            index=int(index),
            is_catchup=is_catchup,
            closes_generation=cierra,
        )
        if cierra:
            self.ticks_in_generation = 0
            self._generations_done += 1
        self.last_processed_ts = int(ts)
        return tick

    # -- histórico acelerado ------------------------------------------------ #

    def replay(self, candles: pd.DataFrame, speed: float = 0.0) -> Iterator[Tick]:
        """Recorre histórico como si fuera vivo.

        ``speed`` 0 significa lo más rápido posible; 1000 significa 1000 velas
        por segundo de reloj real (útil para ver el dashboard moverse).

        Produce exactamente la misma secuencia de decisiones que el modo vivo
        sobre los mismos datos: el runner no distingue de dónde viene el tick.
        """
        pausa = 1.0 / speed if speed and speed > 0 else 0.0
        arranque = self.last_processed_ts
        for i, ts in enumerate(candles.index):
            momento = int(ts)
            if arranque is not None and momento <= arranque:
                continue      # ya procesada antes de la caída
            yield self._emit(momento, i)
            if pausa:
                self.sleep(pausa)

    # -- vivo --------------------------------------------------------------- #

    def ticks(self) -> Iterator[Tick]:
        """Itera ticks indefinidamente (modo vivo).

        1. Duerme hasta la próxima vela + ``settle_delay_seconds``.
        2. Pide las velas cerradas que falten (``fetch``).
        3. Descarta las ya procesadas.
        4. Si quedan varias, estuvimos caídos: las emite en orden marcadas como
           ``is_catchup``, hasta ``execution.max_catchup_candles``.
        5. Marca ``closes_generation`` cada ``garden.ticks_per_generation``.

        Ante fallo del venue reintenta con backoff y avisa por
        ``on_venue_failure``; pasado ``venue_failure_threshold`` el runner es
        quien decide degradar el jardín. El reloj no se para: un venue caído no
        puede dejar el jardín sin reloj, sólo sin velas.
        """
        if self.fetch is None:
            raise RuntimeError(
                "el reloj en vivo necesita un 'fetch' que le traiga las velas nuevas"
            )
        fallos = 0
        while True:
            objetivo = (
                self.next_candle_open(self.now())
                + self.cfg.execution.settle_delay_seconds * 1000
            )
            self.sleep(max(0.0, (objetivo - self.now()) / 1000.0))

            try:
                nuevas = list(self.fetch(self.last_processed_ts))
            except Exception as exc:  # cualquier fallo del venue, sin excepciones
                fallos += 1
                if self.on_venue_failure is not None:
                    self.on_venue_failure(fallos, exc)
                self.sleep(BACKOFF_SECONDS[min(fallos - 1, len(BACKOFF_SECONDS) - 1)])
                continue
            fallos = 0

            pendientes = [
                (ts, i) for ts, i in nuevas
                if self.last_processed_ts is None or int(ts) > int(self.last_processed_ts)
            ]
            recuperando = len(pendientes) > 1
            for ts, index in pendientes[: self.cfg.execution.max_catchup_candles]:
                yield self._emit(ts, index, is_catchup=recuperando)


__all__ = ("BACKOFF_SECONDS", "MarketClock", "Tick")
