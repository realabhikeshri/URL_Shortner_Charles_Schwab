# lineage: REQ-BF-002 / implement.expiry (see runs/ for the decision trail)
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import update

from service.models import Link


def future(hours: int = 1) -> str:
    return (datetime.now(UTC) + timedelta(hours=hours)).isoformat()


async def test_expires_at_round_trips_and_link_works_until_then(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/links", json={"url": "https://e.com/t", "expires_at": future()}
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["expires_at"] is not None
    assert body["is_expired"] is False
    assert (await client.get(f"/{body['code']}")).status_code == 302


async def test_expired_link_returns_410(app: FastAPI, client: AsyncClient) -> None:
    code = (
        await client.post("/api/v1/links", json={"url": "https://e.com/x", "expires_at": future()})
    ).json()["code"]
    async with app.state.db.session() as session:
        await session.execute(
            update(Link)
            .where(Link.code == code)
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await session.commit()
    resp = await client.get(f"/{code}")
    assert resp.status_code == 410
    assert "expired" in resp.json()["error"]["message"]
    meta = await client.get(f"/api/v1/links/{code}")
    assert meta.status_code == 200
    assert meta.json()["is_expired"] is True


async def test_invalid_expiry_values_are_rejected(client: AsyncClient) -> None:
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    assert (
        await client.post("/api/v1/links", json={"url": "https://e.com", "expires_at": past})
    ).status_code == 422
    naive = datetime.now().replace(microsecond=0).isoformat()
    assert (
        await client.post("/api/v1/links", json={"url": "https://e.com", "expires_at": naive})
    ).status_code == 422


async def test_patch_sets_and_clears_expiry(client: AsyncClient) -> None:
    code = (await client.post("/api/v1/links", json={"url": "https://e.com"})).json()["code"]
    set_resp = await client.patch(f"/api/v1/links/{code}", json={"expires_at": future(5)})
    assert set_resp.json()["expires_at"] is not None
    cleared = await client.patch(f"/api/v1/links/{code}", json={"expires_at": None})
    assert cleared.json()["expires_at"] is None
    assert (await client.get(f"/{code}")).status_code == 302
