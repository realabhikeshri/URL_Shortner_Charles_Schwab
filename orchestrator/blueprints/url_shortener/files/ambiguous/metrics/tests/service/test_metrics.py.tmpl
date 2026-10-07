# lineage: REQ-AMBIGUOUS / implement.metrics (see runs/ for the decision trail)
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from service.metrics import Metrics, MetricsMiddleware, router


def build() -> tuple[FastAPI, Metrics]:
    app = FastAPI()
    metrics = Metrics()
    app.state.metrics = metrics
    app.add_middleware(MetricsMiddleware, metrics=metrics)

    @app.get("/items/{item_id}")
    async def item(item_id: str) -> dict[str, str]:
        return {"id": item_id}

    app.include_router(router)
    return app, metrics


async def test_requests_are_counted_by_route_template() -> None:
    app, _ = build()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        for i in range(3):
            assert (await c.get(f"/items/{i}")).status_code == 200
        assert (await c.get("/nope")).status_code == 404
        body = (await c.get("/metrics")).text
    assert 'http_requests_total{method="GET",route="/items/{item_id}",status="200"} 3.0' in body
    assert 'route="unmatched",status="404"' in body
    assert "http_request_duration_seconds_bucket" in body
    assert "/items/0" not in body  # no per-id cardinality


async def test_registries_are_isolated_and_gauges_bind() -> None:
    a, b = Metrics(), Metrics()
    a.redirects.labels("ok").inc()
    a.bind_gauge(a.analytics_queue, lambda: 7)
    assert b"redirects_total" in a.render()
    assert b'redirects_total{outcome="ok"} 1.0' not in b.render()
    assert b"analytics_queue_depth 7.0" in a.render()
