"""LedgerForge application factory."""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from . import __version__
from .api import auth, books, masters, ops, periods, plugins, reports, slots, vouchers
from .auth import build_verifier, warn_if_disabled
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

    # --- authentication (issue #1) --------------------------------------
    # The production guard runs first (v0.16): a deployment that asks for
    # authentication (LF_REQUIRE_AUTH=true, set by docker-compose.prod.yml)
    # must never fall back to the anonymous-admin dev default — a missing
    # OIDC configuration is a boot failure, not a warning.
    if settings.require_auth and settings.auth_mode != "oidc":
        raise RuntimeError(
            "LF_REQUIRE_AUTH=true but LF_AUTH_MODE="
            f"{settings.auth_mode!r} — refusing to start as anonymous admin. "
            "Set LF_AUTH_MODE=oidc with LF_OIDC_ISSUER and LF_OIDC_AUDIENCE, "
            "or unset LF_REQUIRE_AUTH on a development machine."
        )
    warn_if_disabled()
    if settings.auth_mode == "oidc":
        if not settings.oidc_issuer or not settings.oidc_audience:
            raise RuntimeError("LF_AUTH_MODE=oidc requires LF_OIDC_ISSUER and LF_OIDC_AUDIENCE")
        app.state.token_verifier = build_verifier()
    else:
        app.state.token_verifier = None

    # CORS: in dev (auth disabled) allow everything for convenience; with
    # authentication on, the browser UI is same-origin (proxied /api), so no
    # cross-origin calls are allowed unless explicitly configured.
    if settings.auth_mode == "disabled":
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
        )
    elif settings.cors_origin_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origin_list,
            allow_methods=["*"],
            allow_headers=["Authorization", "Content-Type"],
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

    app.include_router(auth.router, prefix="/api/v1")
    app.include_router(books.router, prefix="/api/v1")
    app.include_router(plugins.router, prefix="/api/v1")
    app.include_router(slots.router, prefix="/api/v1")
    app.include_router(masters.router, prefix="/api/v1")
    app.include_router(vouchers.router, prefix="/api/v1")
    app.include_router(reports.router, prefix="/api/v1")
    app.include_router(periods.router, prefix="/api/v1")
    app.include_router(ops.router, prefix="/api/v1")

    @app.get("/health", tags=["meta"])
    def health():
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        # auth_mode rides along so a production monitor can alert on anything
        # other than "oidc" (docs/deployment.md — monitoring).
        return {"status": "ok", "version": __version__, "auth_mode": settings.auth_mode}

    return app


app = create_app()
