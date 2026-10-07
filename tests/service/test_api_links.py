# lineage: REQ-GF-001 / implement.links_api, REQ-AMBIGUOUS / implement.api_key_auth
from httpx import AsyncClient

from service.config import Settings


async def create(client: AsyncClient, **body: object) -> dict[str, object]:
    resp = await client.post("/api/v1/links", json={"url": "https://example.com/a", **body})
    assert resp.status_code == 201, resp.text
    return resp.json()  # type: ignore[no-any-return]


async def test_create_returns_201_with_short_url(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/links", json={"url": "https://Example.com/docs", "tags": ["launch"]}
    )
    assert resp.status_code == 201
    body = resp.json()
    assert len(body["code"]) == 7
    assert body["short_url"] == f"http://sho.rt/{body['code']}"
    assert body["target_url"] == "https://example.com/docs"
    assert body["tags"] == ["launch"]
    assert body["created_at"]
    assert resp.headers["Location"] == f"/api/v1/links/{body['code']}"


async def test_redirect_follows_with_302_and_no_store(client: AsyncClient) -> None:
    link = await create(client)
    resp = await client.get(f"/{link['code']}")
    assert resp.status_code == 302
    assert resp.headers["location"] == "https://example.com/a"
    assert resp.headers["cache-control"] == "private, no-store"


async def test_unknown_code_is_404(client: AsyncClient) -> None:
    resp = await client.get("/doesnotexist")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


async def test_invalid_urls_are_rejected_with_422(client: AsyncClient) -> None:
    for url in ("javascript:alert(1)", "http://127.0.0.1/", "not a url"):
        resp = await client.post("/api/v1/links", json={"url": url})
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "invalid_input"


async def test_schema_validation_errors_use_envelope(client: AsyncClient) -> None:
    resp = await client.post("/api/v1/links", json={"url": "https://e.com", "surprise": 1})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"
    bad_tags = await client.post("/api/v1/links", json={"url": "https://e.com", "tags": ["a b"]})
    assert bad_tags.status_code == 422


async def test_custom_alias_and_conflict(client: AsyncClient) -> None:
    link = await create(client, custom_alias="launch-2026")
    assert link["code"] == "launch-2026"
    assert link["is_custom"] is True
    dup = await client.post(
        "/api/v1/links", json={"url": "https://e.com", "custom_alias": "launch-2026"}
    )
    assert dup.status_code == 409
    reserved = await client.post(
        "/api/v1/links", json={"url": "https://e.com", "custom_alias": "api"}
    )
    assert reserved.status_code == 422


async def test_idempotency_key_replays_and_detects_misuse(client: AsyncClient) -> None:
    headers = {"Idempotency-Key": "req-42"}
    first = await client.post("/api/v1/links", json={"url": "https://e.com/x"}, headers=headers)
    again = await client.post("/api/v1/links", json={"url": "https://e.com/x"}, headers=headers)
    assert first.status_code == again.status_code == 201
    assert first.json()["code"] == again.json()["code"]
    assert again.headers["Idempotent-Replayed"] == "true"
    misuse = await client.post("/api/v1/links", json={"url": "https://e.com/y"}, headers=headers)
    assert misuse.status_code == 409


async def test_metadata_patch_and_soft_delete(client: AsyncClient) -> None:
    link = await create(client)
    code = link["code"]
    meta = await client.get(f"/api/v1/links/{code}")
    assert meta.status_code == 200
    assert meta.json()["disabled"] is False

    disabled = await client.patch(f"/api/v1/links/{code}", json={"disabled": True})
    assert disabled.json()["disabled"] is True
    assert (await client.get(f"/{code}")).status_code == 410
    await client.patch(f"/api/v1/links/{code}", json={"disabled": False})
    assert (await client.get(f"/{code}")).status_code == 302

    empty = await client.patch(f"/api/v1/links/{code}", json={})
    assert empty.status_code == 422

    assert (await client.delete(f"/api/v1/links/{code}")).status_code == 204
    assert (await client.get(f"/api/v1/links/{code}")).status_code == 404
    assert (await client.get(f"/{code}")).status_code == 410
    assert (await client.delete(f"/api/v1/links/{code}")).status_code == 404


async def test_redirect_status_is_configurable(settings: Settings) -> None:
    from service.app import create_app
    from tests.service.conftest import authed, issue_key

    app = create_app(settings.model_copy(update={"redirect_status_code": 307}))
    async with app.router.lifespan_context(app), authed(app, await issue_key(app), "http://t") as c:
        code = (await c.post("/api/v1/links", json={"url": "https://e.com"})).json()["code"]
        assert (await c.get(f"/{code}")).status_code == 307
