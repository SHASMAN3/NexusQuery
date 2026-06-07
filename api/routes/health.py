"""
api/routes/health.py
---------------------
GET /health  — liveness + readiness check (pings MySQL and MongoDB)
GET /metrics — in-process counters snapshot
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from api.middleware.schemas import HealthResponse
from config.settings import get_settings
from db.mongo_client import ping_mongo
from db.mysql_client import ping_mysql
from monitoring.metrics import get_metrics

router = APIRouter()
_cfg = get_settings()


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Health check",
    tags=["Ops"],
)
async def health() -> HealthResponse:
    mysql_ok = await ping_mysql()
    mongo_ok = await ping_mongo()
    checks = {
        "mysql": "ok" if mysql_ok else "error",
        "mongodb": "ok" if mongo_ok else "error",
    }
    overall = "healthy" if all(v == "ok" for v in checks.values()) else "degraded"
    return HealthResponse(
        status=overall,
        version=_cfg.app_version,
        environment=_cfg.environment,
        checks=checks,
    )


@router.get(
    "/metrics",
    summary="In-process metrics snapshot",
    tags=["Ops"],
)
async def metrics() -> JSONResponse:
    return JSONResponse(content=get_metrics().snapshot())