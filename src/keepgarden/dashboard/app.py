"""Servidor del dashboard: FastAPI + uvicorn, en local.

    keepgarden dashboard   →   http://127.0.0.1:8756

Front sin build: HTML y JS vanilla, ECharts desde CDN. Esto se mira en local, no
necesita tooling.

CONTRATO — implementar en el hito 6.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..config import Config

if TYPE_CHECKING:  # pragma: no cover
    from fastapi import FastAPI


def create_app(cfg: Config) -> "FastAPI":
    """Construye la aplicación.

    * Monta ``dashboard/static`` en ``/``.
    * Registra los endpoints de ``DashboardAPI`` bajo ``/api``.
    * Abre la base en **sólo lectura**. Si el motor no está corriendo, el
      dashboard funciona igual mostrando el último estado: son procesos
      independientes y eso es deliberado.
    * Cachea en memoria las respuestas caras (grafo genealógico, matriz de
      correlación) invalidando por ``garden_meta.current_generation``.
    """
    raise NotImplementedError


def serve(cfg: Config, *, open_browser: bool | None = None) -> None:
    """Levanta uvicorn en ``cfg.dashboard.host:port``."""
    raise NotImplementedError


__all__ = ("create_app", "serve")
