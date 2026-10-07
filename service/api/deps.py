# lineage: REQ-GF-001 / implement.foundation,
#          REQ-AMBIGUOUS / implement.enterprise_wiring, implement.api_key_auth
"""FastAPI dependencies shared by the routers."""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from service.auth import authenticate
from service.config import Settings
from service.db import Database
from service.links import LinkService
from service.models import ApiKey
from service.ratelimit import RateLimitedError, RateLimiter, enforce


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_database(request: Request) -> Database:
    database: Database = request.app.state.db
    return database


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with get_database(request).session() as session:
        yield session


async def get_link_service(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> LinkService:
    state = request.app.state
    return LinkService(
        session,
        settings,
        cache=getattr(state, "cache", None),
        metrics=getattr(state, "metrics", None),
    )


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


async def _limit(request: Request, limiter: RateLimiter | None, key: str, scope: str) -> None:
    if limiter is None:
        return
    try:
        await enforce(limiter, key)
    except RateLimitedError:
        metrics = getattr(request.app.state, "metrics", None)
        if metrics is not None:
            metrics.rate_limited.labels(scope).inc()
        raise


async def require_api_key(
    request: Request, session: Annotated[AsyncSession, Depends(get_session)]
) -> ApiKey | None:
    """Authenticate X-API-Key (None when auth is disabled by configuration)."""
    if not get_settings(request).auth_enabled:
        return None
    return await authenticate(session, request.headers.get("X-API-Key"))


async def limit_writes(
    request: Request, key: Annotated[ApiKey | None, Depends(require_api_key)]
) -> None:
    """Writes are limited per API key (per client address when auth is disabled)."""
    bucket = f"w:key:{key.id}" if key is not None else f"w:{client_ip(request)}"
    await _limit(request, getattr(request.app.state, "write_limiter", None), bucket, "write")


async def limit_redirects(request: Request) -> None:
    await _limit(
        request,
        getattr(request.app.state, "redirect_limiter", None),
        f"r:{client_ip(request)}",
        "redirect",
    )
