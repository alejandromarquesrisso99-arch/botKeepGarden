"""Servidor del dashboard: FastAPI + uvicorn, en local.

    keepgarden dashboard   →   http://127.0.0.1:8756

Front sin build: HTML y JS vanilla, ECharts desde CDN. Esto se mira en local, no
necesita tooling.

El dashboard abre la base en **sólo lectura** y es un proceso independiente del
motor: si el jardín no está corriendo, se ve igual el último estado.
"""

from __future__ import annotations

import contextlib
import threading
import webbrowser
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..config import Config

if TYPE_CHECKING:  # pragma: no cover
    from fastapi import FastAPI

STATIC_DIR = Path(__file__).with_name("static")


class _GenerationCache:
    """Caché en memoria de las respuestas caras.

    Se invalida por ``garden_meta.current_generation``: mientras el motor no
    cierre una generación nueva, el pasado no cambia y volver a calcular el
    grafo genealógico o la matriz de correlación es tirar CPU.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._generation: int | None = None
        self._entries: dict[Any, Any] = {}

    def get(self, generation: int, key: Any, build: Callable[[], Any]) -> Any:
        with self._lock:
            if self._generation != generation:
                self._entries.clear()
                self._generation = generation
            if key in self._entries:
                return self._entries[key]
        valor = build()
        with self._lock:
            if self._generation == generation:
                self._entries[key] = valor
        return valor

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._generation = None


def create_app(cfg: Config) -> FastAPI:
    """Construye la aplicación.

    * Monta ``dashboard/static`` en ``/``.
    * Registra los endpoints de ``DashboardAPI`` bajo ``/api``.
    * Abre la base en **sólo lectura**. Si el motor no está corriendo, el
      dashboard funciona igual mostrando el último estado: son procesos
      independientes y eso es deliberado.
    * Cachea en memoria las respuestas caras (grafo genealógico, matriz de
      correlación) invalidando por ``garden_meta.current_generation``.
    """
    from fastapi import FastAPI, HTTPException, Query
    from fastapi.responses import FileResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles

    from ..storage.db import open_database
    from ..storage.repositories import Repositories
    from .api import DashboardAPI

    # Una conexión por hilo: FastAPI atiende los endpoints síncronos en un pool
    # y SQLite prohíbe usar una conexión desde un hilo que no es el suyo. Abrir
    # en sólo lectura es barato y los hilos del pool se reutilizan, así que esto
    # son un puñado de conexiones, no una por petición.
    local = threading.local()
    abiertas: list[Any] = []
    cerrojo = threading.Lock()

    def api_del_hilo() -> DashboardAPI:
        propia = getattr(local, "api", None)
        if propia is None:
            db = open_database(cfg.db_file, read_only=True, create=False)
            propia = DashboardAPI(cfg=cfg, repos=Repositories.open(db))
            local.api = propia
            with cerrojo:
                abiertas.append(db)
        return propia

    cache = _GenerationCache()

    app = FastAPI(title="botKeepGarden", docs_url="/api/docs", redoc_url=None)
    app.state.api_factory = api_del_hilo
    app.state.api = api_del_hilo()
    app.state.cache = cache

    @app.on_event("shutdown")
    def _cerrar() -> None:  # pragma: no cover - lo ejecuta uvicorn al salir
        with cerrojo:
            for db in abiertas:
                # Al apagar no hay nada que hacer con un cierre que falle.
                with contextlib.suppress(Exception):
                    db.close()
            abiertas.clear()

    def cacheado(key: Any, build: Callable[[DashboardAPI], Any]) -> Any:
        api = api_del_hilo()
        return cache.get(api.current_generation(), key, lambda: build(api))

    # -- portada ----------------------------------------------------------- #

    @app.get("/api/garden/summary")
    def garden_summary() -> Any:
        return api_del_hilo().garden_summary()

    @app.get("/api/garden/equity")
    def garden_equity(
        frm: int | None = Query(default=None, alias="from"),
        to: int | None = None,
    ) -> Any:
        return api_del_hilo().garden_equity(frm, to)

    # -- generaciones ------------------------------------------------------ #

    @app.get("/api/generations")
    def generations() -> Any:
        return cacheado("generations", lambda api: api.generations())

    @app.get("/api/generations/{n}")
    def generation(n: int) -> Any:
        try:
            return cacheado(("generation", n), lambda api: api.generation(n))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    # -- cría -------------------------------------------------------------- #

    @app.get("/api/breeding")
    def breeding() -> Any:
        return cacheado("breeding", lambda api: api.breeding())

    @app.get("/api/incubation/{n}")
    def incubation(n: int) -> Any:
        return cacheado(("incubation", n), lambda api: api.incubation(n))

    # -- bots -------------------------------------------------------------- #

    @app.get("/api/bots")
    def bots(
        status: str | None = None,
        family: str | None = None,
        lineage: str | None = None,
        limit: int = 200,
    ) -> Any:
        return api_del_hilo().bots(status=status, family=family, lineage=lineage, limit=limit)

    @app.get("/api/bots/{bot_id}")
    def bot(bot_id: str) -> Any:
        try:
            return api_del_hilo().bot(bot_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/bots/{bot_id}/equity")
    def bot_equity(bot_id: str) -> Any:
        return api_del_hilo().bot_equity(bot_id)

    @app.get("/api/bots/{bot_id}/trades")
    def bot_trades(bot_id: str, limit: int = 500) -> Any:
        return api_del_hilo().bot_trades(bot_id, limit)

    @app.get("/api/bots/{bot_id}/genome")
    def bot_genome(bot_id: str) -> Any:
        try:
            return api_del_hilo().bot_genome(bot_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    # -- genealogía -------------------------------------------------------- #

    @app.get("/api/lineage/graph")
    def lineage_graph(until_generation: int | None = None) -> Any:
        return cacheado(
            ("lineage", until_generation),
            lambda api: api.lineage_graph(until_generation),
        )

    # -- especies ---------------------------------------------------------- #

    @app.get("/api/species")
    def species() -> Any:
        return cacheado("species", lambda api: api.species())

    @app.get("/api/species/scatter")
    def species_scatter() -> Any:
        return cacheado("scatter", lambda api: api.species_scatter())

    @app.get("/api/species/correlation")
    def species_correlation() -> Any:
        return cacheado("correlation", lambda api: api.species_correlation())

    # -- jardinero --------------------------------------------------------- #

    @app.get("/api/journal")
    def journal(limit: int = 20) -> Any:
        return api_del_hilo().journal(limit)

    @app.get("/api/events")
    def events(type: str | None = None, limit: int = 200) -> Any:
        return api_del_hilo().events(type, limit)

    @app.get("/api/alerts")
    def alerts() -> Any:
        return api_del_hilo().alerts()

    # -- front ------------------------------------------------------------- #

    @app.get("/")
    def index() -> Any:
        return FileResponse(STATIC_DIR / "index.html")

    @app.exception_handler(KeyError)
    def _no_encontrado(_request: Any, exc: KeyError) -> JSONResponse:  # pragma: no cover
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app


def serve(cfg: Config, *, open_browser: bool | None = None) -> None:
    """Levanta uvicorn en ``cfg.dashboard.host:port``."""
    import uvicorn

    app = create_app(cfg)
    host = cfg.dashboard.host
    port = cfg.dashboard.port
    url = f"http://{host}:{port}"

    abrir = cfg.dashboard.open_browser if open_browser is None else open_browser
    if abrir:
        # Un hilo con retardo: si se abre antes de que uvicorn escuche, el
        # navegador enseña un error y hay que recargar a mano.
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    print(f"dashboard en {url}   (Ctrl+C para parar)")
    uvicorn.run(app, host=host, port=port, log_level="warning")


__all__ = ("STATIC_DIR", "create_app", "serve")
