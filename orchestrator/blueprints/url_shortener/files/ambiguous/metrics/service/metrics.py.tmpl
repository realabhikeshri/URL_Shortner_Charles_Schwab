# lineage: REQ-AMBIGUOUS / implement.metrics (see runs/ for the decision trail)
"""Prometheus metrics with a per-app registry (no global state, isolated in tests)."""

import time
from collections.abc import Callable

from fastapi import APIRouter, Request, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from starlette.types import ASGIApp, Message, Receive, Scope, Send

LATENCY_BUCKETS = (0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5)


class Metrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.requests = Counter(
            "http_requests_total",
            "HTTP requests by route template, method and status",
            ["method", "route", "status"],
            registry=self.registry,
        )
        self.latency = Histogram(
            "http_request_duration_seconds",
            "HTTP request latency by route template",
            ["route"],
            buckets=LATENCY_BUCKETS,
            registry=self.registry,
        )
        self.redirects = Counter(
            "redirects_total", "Redirect outcomes", ["outcome"], registry=self.registry
        )
        self.cache = Counter(
            "link_cache_requests_total",
            "Redirect cache lookups",
            ["result"],
            registry=self.registry,
        )
        self.rate_limited = Counter(
            "rate_limited_total",
            "Requests rejected by rate limiting",
            ["scope"],
            registry=self.registry,
        )
        self.analytics_dropped = Gauge(
            "analytics_clicks_dropped",
            "Clicks dropped because the queue was full",
            registry=self.registry,
        )
        self.analytics_queue = Gauge(
            "analytics_queue_depth", "Clicks waiting to be written", registry=self.registry
        )

    def bind_gauge(self, gauge: Gauge, fn: Callable[[], float]) -> None:
        gauge.set_function(fn)

    def render(self) -> bytes:
        return generate_latest(self.registry)


class MetricsMiddleware:
    """Records request count/latency labelled by the *route template* (bounded cardinality)."""

    def __init__(self, app: ASGIApp, *, metrics: Metrics) -> None:
        self.app = app
        self.metrics = metrics

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        status = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            route = getattr(scope.get("route"), "path", "unmatched")
            self.metrics.requests.labels(scope["method"], route, str(status)).inc()
            self.metrics.latency.labels(route).observe(time.perf_counter() - started)


router = APIRouter(tags=["observability"])


@router.get("/metrics", include_in_schema=False)
async def metrics_endpoint(request: Request) -> Response:
    metrics: Metrics = request.app.state.metrics
    return Response(metrics.render(), media_type=CONTENT_TYPE_LATEST)
