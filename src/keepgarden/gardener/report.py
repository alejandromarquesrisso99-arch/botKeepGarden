"""El informe de generación: lo que el jardinero lee antes de decidir.

Estructura completa en docs/GARDENER_PROTOCOL.md §El informe. El orden de las
nueve secciones no es casual: va de lo general a lo concreto y termina con las
decisiones anteriores y su resultado, que es lo que impide que el jardinero
repita el mismo consejo cada mes.

CONTRATO — implementar en el hito 7.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..config import Config
from ..storage.repositories import Repositories


@dataclass(slots=True)
class ReportBuilder:
    cfg: Config
    repos: Repositories

    def build(self, generation: int) -> str:
        """Genera el informe en Markdown.

        Secciones, en este orden:

        1. Resumen de una línea: estado del jardín y el cambio más importante.
        2. Demografía.
        3. Rendimiento contra benchmark y distribución de fitness.
        4. Diversidad: distancia media, reparto por familia y por linaje,
           matriz de correlación resumida.
        5. Los 10 mejores, con **el genoma en forma legible**
           (``Genome.describe()``), métricas, edad, padres y operador de origen.
        6. Los muertos de la generación y por qué murieron.
        7. Qué ha funcionado: tasa de éxito por operador y por familia, medida
           como fracción de hijos que sobreviven 3 generaciones.
        8. Alertas activas.
        9. Decisiones anteriores y su resultado medido frente al esperado.

        El informe debe ser suficiente para decidir **sin mirar la base de
        datos**. Si hace falta consultar algo que no está aquí, es un fallo del
        informe, no del jardinero.
        """
        raise NotImplementedError

    def write(self, generation: int) -> Path:
        """Escribe el informe en ``state/reports/gen_<n>.md`` y devuelve la ruta."""
        raise NotImplementedError

    def snapshot_for_validation(self, generation: int) -> object:
        """Construye el ``GardenSnapshot`` que valida las propuestas."""
        raise NotImplementedError


__all__ = ("ReportBuilder",)
