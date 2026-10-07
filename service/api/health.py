# lineage: REQ-GF-001 / implement.foundation, REQ-AMBIGUOUS / implement.enterprise_wiring
"""Liveness and readiness probes."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from service.api.deps import get_database
from service.db import Database
from service.observability import get_logger

router = APIRouter(tags=["health"])
log = get_logger("health")


@router.get("/healthz", summary="Liveness probe")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz", summary="Readiness probe (database and cache reachable)")
async def readyz(request: Request, db: Annotated[Database, Depends(get_database)]) -> JSONResponse:
    checks: dict[str, str] = {}
    try:
        await db.ping()
        checks["database"] = "ok"
    except Exception as exc:
        log.warning(
            "readiness check failed", extra={"check": "database", "error": type(exc).__name__}
        )
        checks["database"] = "unavailable"
    cache = getattr(request.app.state, "cache", None)
    if cache is not None:
        try:
            checks[f"cache:{cache.backend}"] = "ok" if await cache.ping() else "unavailable"
        except Exception as exc:
            log.warning(
                "readiness check failed", extra={"check": "cache", "error": type(exc).__name__}
            )
            checks[f"cache:{cache.backend}"] = "unavailable"
    ready = all(v == "ok" for v in checks.values())
    return JSONResponse(
        {"status": "ready" if ready else "not_ready", "checks": checks},
        status_code=200 if ready else 503,
    )
