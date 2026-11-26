"""
FastAPI application factory.

Serves the JSON API and the WebSocket stream. If the frontend has been built
(`frontend/dist`), it is served from the same origin too, so a production run
is a single process on a single port.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .api import (
    ai_routes, autonomy_routes, business_routes, config_routes, core_routes, skills_routes, mcp_routes, document_routes,
    email_routes, setup_routes, countdown_routes, everyday_routes,
    data_routes,
    dev_routes, intelligence_routes, os_routes, personal_routes,
    work_routes,
)
from .api.deps import SESSION_COOKIE
from .config import settings
from .container import container
from .core.logs import setup_logging

logger = logging.getLogger("jarvis.app")

FRONTEND_DIST = settings.ROOT / "frontend" / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging(settings.LOG_LEVEL, settings.DATA_DIR / "logs")
    # Plain ASCII: the Windows console is cp1252 by default and mangles the rest.
    logger.info("=" * 62)
    logger.info("  J A R V I S   -   personal AI command centre")
    logger.info("=" * 62)
    try:
        await container.start()
    except Exception:
        logger.exception("startup failed")
        raise
    try:
        yield
    finally:
        await container.stop()


def create_app() -> FastAPI:
    app = FastAPI(
        title="JARVIS",
        description="Local-first personal AI command centre.",
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Jarvis-Token"],
    )

    # ------------------------------------------------------------------
    @app.middleware("http")
    async def attach_auto_session(request: Request, call_next):
        """In local auth mode a loopback request without a token is issued one.
        Hand it back so the browser can reuse it instead of minting a new
        session on every single request."""
        response = await call_next(request)
        token = getattr(request.state, "issued_token", None)
        if token:
            response.set_cookie(
                SESSION_COOKIE, token, httponly=True, samesite="lax",
                max_age=settings.SESSION_HOURS * 3600,
            )
            response.headers["X-Jarvis-Token"] = token
        return response

    # ------------------------------------------------------------------
    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        logger.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content={"detail": f"{type(exc).__name__}: {exc}", "path": request.url.path},
        )

    # ------------------------------------------------------------------
    for module in (core_routes, ai_routes, work_routes, data_routes, config_routes,
                   intelligence_routes, os_routes, dev_routes,
                   personal_routes, business_routes, autonomy_routes, skills_routes, mcp_routes, document_routes,
                   email_routes, setup_routes, countdown_routes, everyday_routes):
        app.include_router(module.router, prefix="/api")

    # ------------------------------------------------------------------
    if FRONTEND_DIST.exists():
        assets = FRONTEND_DIST / "assets"
        if assets.exists():
            app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        async def spa(full_path: str):
            candidate = FRONTEND_DIST / full_path
            if full_path and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(FRONTEND_DIST / "index.html")

        logger.info("serving the built frontend from %s", FRONTEND_DIST)
    else:
        @app.get("/", include_in_schema=False)
        async def root():
            return {
                "service": "JARVIS backend",
                "status": "online" if container.started else "starting",
                "ui": f"http://localhost:{settings.FRONTEND_PORT}",
                "docs": "/api/docs",
                "note": "The frontend dev server runs separately on port "
                        f"{settings.FRONTEND_PORT}. Run `npm run dev` in frontend/, "
                        "or `npm run build` to have this server host it.",
            }

    return app


app = create_app()
