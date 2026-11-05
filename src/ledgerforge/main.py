"""LedgerForge application factory."""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from . import __version__
from .api import masters, reports, vouchers
from .db import get_engine
from .errors import EngineError
from .plugins import PluginManager
from .settings import get_settings

logging.basicConfig(level=logging.INFO)


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="LedgerForge",
        version=__version__,
        description=(
            "Community-driven, keyboard-first, double-entry business management "
            "engine — inspired by TallyPrime. Financial laws: (1) debits must "
            "exactly equal credits, (2) history is append-only, (3) exact "
            "decimal arithmetic everywhere."
        ),
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    manager = PluginManager()
    try:
        manager.discover(extra_path=settings.plugin_path)
    except Exception:  # noqa: BLE001 — plugin boot failures must not kill the app
        logging.getLogger("ledgerforge").exception("plugin discovery failed")
    app.state.plugin_manager = manager

    @app.exception_handler(EngineError)
    async def engine_error_handler(request: Request, exc: EngineError):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    app.include_router(masters.router, prefix="/api/v1")
    app.include_router(vouchers.router, prefix="/api/v1")
    app.include_router(reports.router, prefix="/api/v1")

    @app.get("/health", tags=["meta"])
    def health():
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"status": "ok", "version": __version__}

    return app


app = create_app()
