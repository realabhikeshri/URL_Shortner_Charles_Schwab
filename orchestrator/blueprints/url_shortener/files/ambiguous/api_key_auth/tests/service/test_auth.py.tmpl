# lineage: REQ-AMBIGUOUS / implement.api_key_auth (see runs/ for the decision trail)
import hashlib
import os

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select

from service import cli
from service.auth import authenticate, create_api_key, generate_key, revoke_api_key
from service.config import Settings
from service.models import ApiKey


async def test_write_endpoints_require_a_valid_key(app: FastAPI, anon_client: AsyncClient) -> None:
    missing = await anon_client.post("/api/v1/links", json={"url": "https://e.com"})
    assert missing.status_code == 401
    assert missing.json()["error"]["code"] == "unauthorized"
    assert missing.headers["WWW-Authenticate"].startswith("ApiKey")
    for bad in ("garbage", "usk_zzzzzzzz_" + "a" * 43, generate_key().key):
        resp = await anon_client.post(
            "/api/v1/links", json={"url": "https://e.com"}, headers={"X-API-Key": bad}
        )
        assert resp.status_code == 401, bad
    async with app.state.db.session() as session:
        revoked = await create_api_key(session, "old")
        assert await revoke_api_key(session, revoked.split("_")[1])
        assert not await revoke_api_key(session, revoked.split("_")[1])
    resp = await anon_client.post(
        "/api/v1/links", json={"url": "https://e.com"}, headers={"X-API-Key": revoked}
    )
    assert resp.status_code == 401
    assert (
        await anon_client.patch("/api/v1/links/abc", json={"disabled": True})
    ).status_code == 401
    assert (await anon_client.delete("/api/v1/links/abc")).status_code == 401


async def test_read_endpoints_and_redirect_stay_public(
    client: AsyncClient, anon_client: AsyncClient
) -> None:
    code = (await client.post("/api/v1/links", json={"url": "https://e.com/pub"})).json()["code"]
    assert (await anon_client.get(f"/{code}")).status_code == 302
    assert (await anon_client.get(f"/api/v1/links/{code}")).status_code == 200
    assert (await anon_client.get(f"/api/v1/links/{code}/stats")).status_code == 200


async def test_keys_are_hashed_at_rest(app: FastAPI, api_key: str) -> None:
    async with app.state.db.session() as session:
        rows = (await session.execute(select(ApiKey))).scalars().all()
        assert len(rows) == 1
        row = rows[0]
        assert row.key_hash == hashlib.sha256(api_key.encode()).hexdigest()
        assert api_key not in " ".join(str(v) for v in vars(row).values())
        assert api_key.split("_")[1] == row.prefix
        assert (await authenticate(session, api_key)).id == row.id


async def test_cli_creates_lists_and_revokes_keys(
    settings: Settings,
    app: FastAPI,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SHORTENER_DATABASE_URL", settings.database_url)
    assert os.environ["SHORTENER_DATABASE_URL"] == settings.database_url
    assert await _cli(["create-key", "--name", "ops"]) == 0
    key = capsys.readouterr().out.strip()
    assert key.startswith("usk_")
    assert await _cli(["list-keys"]) == 0
    listing = capsys.readouterr().out
    assert "ops\tactive" in listing
    assert key not in listing
    assert await _cli(["revoke-key", key.split("_")[1]]) == 0
    assert await _cli(["revoke-key", "ffffffff"]) == 1
    async with app.state.db.session() as session:
        with pytest.raises(Exception, match="revoked"):
            await authenticate(session, key)


async def _cli(argv: list[str]) -> int:
    import asyncio

    return await asyncio.to_thread(cli.main, argv)


async def test_auth_can_be_disabled(settings: Settings) -> None:
    from tests.service.test_enterprise import started

    async with started(settings, auth_enabled=False) as (_app, c):
        assert "X-API-Key" not in c.headers
        assert (
            await c.post("/api/v1/links", json={"url": "https://e.com/open"})
        ).status_code == 201
