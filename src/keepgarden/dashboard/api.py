"""API de sólo lectura del dashboard.

Abre la base en modo ``ro``. El dashboard **nunca escribe** en el jardín.

Endpoints en docs/DASHBOARD.md §API.

CONTRATO — implementar en el hito 6.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..config import Config
from ..storage.repositories import Repositories


@dataclass(slots=True)
class DashboardAPI:
    """Lógica de las respuestas, separada del enrutado de FastAPI para poder
    testearla sin levantar un servidor."""

    cfg: Config
    repos: Repositories

    # -- portada ----------------------------------------------------------- #

    def garden_summary(self) -> dict[str, Any]:
        """Tarjetas de la portada: capital, alfa, población, especies,
        diversidad, generación, edad del jardín, alertas, sesión pendiente."""
        raise NotImplementedError

    def garden_equity(self, frm: int | None = None, to: int | None = None) -> dict[str, Any]:
        """Curva del jardín contra el benchmark, decimada a
        ``cfg.dashboard.series_points`` con LTTB."""
        raise NotImplementedError

    # -- generaciones ------------------------------------------------------ #

    def generations(self) -> list[dict[str, Any]]: ...
    def generation(self, n: int) -> dict[str, Any]: ...

    # -- bots -------------------------------------------------------------- #

    def bots(self, *, status: str | None = None, family: str | None = None,
             lineage: str | None = None, limit: int = 200) -> list[dict[str, Any]]: ...

    def bot(self, bot_id: str) -> dict[str, Any]:
        """Ficha completa: cabecera, genoma legible y crudo, padres, hijos,
        métricas por generación e historia de eventos."""
        raise NotImplementedError

    def bot_equity(self, bot_id: str) -> dict[str, Any]: ...
    def bot_trades(self, bot_id: str, limit: int = 500) -> list[dict[str, Any]]: ...
    def bot_genome(self, bot_id: str) -> dict[str, Any]:
        """Genoma en las dos formas: ``describe()`` legible y JSON crudo."""
        ...

    # -- genealogía -------------------------------------------------------- #

    def lineage_graph(self, until_generation: int | None = None) -> dict[str, Any]:
        """El DAG genealógico. ``until_generation`` es lo que alimenta el
        slider temporal: reproducir la evolución del jardín generación a
        generación. Ver ``evolution.lineage.build_graph``."""
        raise NotImplementedError

    # -- especies ---------------------------------------------------------- #

    def species(self) -> list[dict[str, Any]]: ...

    def species_scatter(self) -> dict[str, Any]:
        """Proyección 2D de la población por distancia genética (MDS clásico
        sobre la matriz de distancias; UMAP si algún día hace falta).

        Junto con ``species_correlation``, responde a la pregunta que importa:
        ¿este jardín tiene ideas distintas o cincuenta copias de la misma?"""
        raise NotImplementedError

    def species_correlation(self) -> dict[str, Any]:
        """Heatmap de correlación entre curvas de equity de los bots vivos."""
        raise NotImplementedError

    # -- jardinero --------------------------------------------------------- #

    def journal(self, limit: int = 20) -> list[dict[str, Any]]: ...
    def events(self, type: str | None = None, limit: int = 200) -> list[dict[str, Any]]: ...
    def alerts(self) -> list[dict[str, Any]]: ...


__all__ = ("DashboardAPI",)
