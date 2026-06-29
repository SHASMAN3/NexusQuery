"""
api/main.py
-----------
FastAPI application factory.

Startup sequence:
  1. Configure LangSmith
  2. Build SQLAlchemy engine + ensure MySQL tables
  3. Build Motor client + ensure MongoDB indexes
  4. Mount static files + UI

Shutdown sequence:
  1. Dispose MySQL engine
  2. Close MongoDB client
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from api.middleware.request_id import RequestIDMiddleware
from api.routes import ask, health, ingest
from config.settings import get_settings
from db.models_sql import Base
from db.mongo_client import close_mongo_client, ensure_indexes, get_mongo_client
from db.mysql_client import build_engine, close_engine
from monitoring.langsmith_tracer import configure_langsmith

logger = logging.getLogger(__name__)
_cfg = get_settings()

# Project root — works regardless of working directory
BASE_DIR = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------------ #
# Lifespan
# ------------------------------------------------------------------ #

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Async lifespan context — replaces deprecated on_event handlers."""

    # --- Startup ---
    logger.info("Starting NexusQuery WebQA Agent v%s (%s)", _cfg.app_version, _cfg.environment)

    # LangSmith (no-op if key not set)
    configure_langsmith()

    # MySQL — create tables if they don't exist (idempotent)
    engine = build_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("MySQL tables verified")

    # MongoDB — ensure B-tree + text + vector search indexes
    get_mongo_client()
    await ensure_indexes()
    logger.info("MongoDB indexes verified")

    logger.info("NexusQuery startup complete — ready to serve requests")
    yield

    # --- Shutdown ---
    logger.info("Shutting down NexusQuery...")
    await close_engine()
    await close_mongo_client()
    logger.info("NexusQuery shutdown complete")


# ------------------------------------------------------------------ #
# App factory
# ------------------------------------------------------------------ #

def create_app() -> FastAPI:
    app = FastAPI(
        title=_cfg.app_name,
        version=_cfg.app_version,
        description=(
            "NexusQuery: Help Website Q&A Agent — "
            "RAG-powered question answering over indexed documentation."
        ),
        docs_url="/docs" if _cfg.debug else None,
        redoc_url="/redoc" if _cfg.debug else None,
        openapi_url="/openapi.json" if _cfg.debug else None,
        lifespan=lifespan,
    )

    # ---- Middleware ------------------------------------------------- #
    app.add_middleware(RequestIDMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],          # UI is served from the same origin;
        allow_credentials=True,       # '*' covers direct file:// opens too
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    # ---- Exception handlers ----------------------------------------- #
    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        request_id = getattr(request.state, "request_id", "unknown")
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "error": "Validation error",
                "detail": exc.errors(),
                "request_id": request_id,
            },
        )

    @app.exception_handler(Exception)
    async def generic_error_handler(request: Request, exc: Exception):
        request_id = getattr(request.state, "request_id", "unknown")
        logger.error(
            "Unhandled exception: request_id=%s err=%s", request_id, exc, exc_info=True
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"error": "Internal server error", "request_id": request_id},
        )

    # ---- API routes ------------------------------------------------- #
    app.include_router(ask.router,    prefix="/api/v1")
    app.include_router(ingest.router, prefix="/api/v1")
    app.include_router(health.router, prefix="/api/v1")

    # ---- UI routes -------------------------------------------------- #
    # Serve index.html at root  →  http://localhost:8080/
    index_file = BASE_DIR / "index.html"

    @app.get("/", include_in_schema=False)
    async def serve_ui():
        if index_file.exists():
            return FileResponse(str(index_file), media_type="text/html")
        return JSONResponse(
            status_code=404,
            content={"error": "UI not found. Place index.html in the project root."},
        )

    # Serve any other static assets from project root (CSS, JS, images, etc.)
    # Mounted AFTER API routes so /api/v1/* is never intercepted
    if BASE_DIR.exists():
        app.mount(
            "/static",
            StaticFiles(directory=str(BASE_DIR), html=False),
            name="static",
        )

    return app


app = create_app()