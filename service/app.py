# lineage: REQ-GF-001 / implement.foundation, REQ-BF-002 / implement.analytics,
#          REQ-AMBIGUOUS / implement.enterprise_wiring
"""Application factory."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from service import __version__
from service.analytics import ClickRecorder
from service.api import health, links, redirect, stats
from service.api.middleware import (
    BodySizeLimitMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
)
from service.cache import Cache, build_cache, redis_client
from service.config import Settings, get_settings
from service.db import Database, migrate
from service.errors import install_error_handlers
from service.metrics import Metrics, MetricsMiddleware
from service.metrics import router as metrics_router
from service.observability import configure_logging, get_logger
from service.ratelimit import MemoryRateLimiter, RateLimiter, RedisRateLimiter

log = get_logger("app")


def build_limiters(
    settings: Settings, redis: Any | None
) -> tuple[RateLimiter | None, RateLimiter | None]:
    if not settings.rate_limit_enabled:
        return None, None

    def make(capacity: int, refill_per_s: float) -> RateLimiter:
        if redis is not None:
            return RedisRateLimiter(redis, capacity, refill_per_s)
        return MemoryRateLimiter(capacity, refill_per_s)

    return (
        make(settings.rate_limit_write_capacity, settings.rate_limit_write_refill_per_s),
        make(settings.rate_limit_redirect_capacity, settings.rate_limit_redirect_refill_per_s),
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(settings.log_level, json_output=settings.log_json)
        if settings.db_auto_migrate:
            await migrate(settings.database_url)
        app.state.db = Database(settings.database_url, pool_size=settings.db_pool_size)
        redis = redis_client(settings.redis_url) if settings.redis_url else None
        cache: Cache | None = None
        if settings.cache_enabled:
            cache = build_cache(None, max_entries=settings.cache_max_entries, client=redis)
        app.state.cache = cache
        app.state.write_limiter, app.state.redirect_limiter = build_limiters(settings, redis)
        app.state.recorder = None
        if settings.analytics_enabled:
            app.state.recorder = ClickRecorder(
                app.state.db.sessionmaker,
                secret=settings.analytics_salt_secret.get_secret_value(),
                queue_size=settings.analytics_queue_size,
                batch_size=settings.analytics_batch_size,
                retention_days=settings.analytics_retention_days,
                purge_interval_s=settings.analytics_purge_interval_s,
            )
            await app.state.recorder.start()
            recorder = app.state.recorder
            metrics.bind_gauge(metrics.analytics_dropped, lambda: float(recorder.dropped))
            metrics.bind_gauge(metrics.analytics_queue, lambda: float(recorder.queue.qsize()))
        log.info(
            "startup complete",
            extra={"environment": settings.environment, "cache": cache.backend if cache else None},
        )
        try:
            yield
        finally:
            if app.state.recorder is not None:
                await app.state.recorder.stop()
            if cache is not None:
                await cache.close()
            elif redis is not None:
                await redis.aclose()
            await app.state.db.dispose()

    app = FastAPI(
        title="URL Shortener",
        version=__version__,
        lifespan=lifespan,
        openapi_url="/api/v1/openapi.json",
    )
    app.state.settings = settings
    metrics = Metrics()
    app.state.metrics = metrics
    install_error_handlers(app)
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_methods=["GET", "POST", "PATCH", "DELETE"],
            allow_headers=["Content-Type", "Idempotency-Key", "X-Request-ID"],
        )
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_body_bytes)
    app.add_middleware(SecurityHeadersMiddleware, hsts=settings.hsts_enabled)
    if settings.metrics_enabled:
        app.add_middleware(MetricsMiddleware, metrics=metrics)
    app.add_middleware(RequestContextMiddleware)

    app.include_router(health.router)
    if settings.metrics_enabled:
        app.include_router(metrics_router)
    app.include_router(links.router)
    app.include_router(stats.router)
    app.include_router(redirect.router)  # catch-all /{code}: keep last
    return app
