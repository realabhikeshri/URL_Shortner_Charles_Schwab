# lineage: REQ-GF-001 / test.integration (see runs/ for the decision trail)
"""End-to-end behaviour through the HTTP API against a migrated SQLite database."""

import asyncio
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import create_engine, inspect

from service import links as links_module
from service.db import ALEMBIC_INI
from service.models import Link


async def test_full_link_lifecycle(client: AsyncClient) -> None:
    created = await client.post(
        "/api/v1/links", json={"url": "https://example.com/launch", "custom_alias": "launch"}
    )
    assert created.status_code == 201
    code = created.json()["code"]
    assert (await client.get(f"/{code}")).headers["location"] == "https://example.com/launch"
    assert (await client.patch(f"/api/v1/links/{code}", json={"disabled": True})).status_code == 200
    assert (await client.get(f"/{code}")).status_code == 410
    assert (await client.delete(f"/api/v1/links/{code}")).status_code == 204
    assert (await client.get(f"/api/v1/links/{code}")).status_code == 404


async def test_generated_codes_are_case_sensitive(app: FastAPI, client: AsyncClient) -> None:
    """Generated base62 codes are case-sensitive: 'AbC1234' and 'abc1234' are different links."""
    async with app.state.db.session() as session:
        session.add(Link(code="AbC1234", target_url="https://example.com/upper", tags=[]))
        await session.commit()
    resp = await client.get("/AbC1234")
    assert resp.status_code == 302
    assert resp.headers["location"] == "https://example.com/upper"
    assert (await client.get("/abc1234")).status_code == 404


async def test_code_collision_retries_then_succeeds(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = await client.post("/api/v1/links", json={"url": "https://e.com/1"})
    taken = first.json()["code"]
    sequence = iter([taken, taken, "Fresh01", "Fresh02", "Fresh03"])
    monkeypatch.setattr(links_module, "generate_code", lambda length: next(sequence))
    second = await client.post("/api/v1/links", json={"url": "https://e.com/2"})
    assert second.status_code == 201
    assert second.json()["code"] == "Fresh01"


async def test_code_space_exhaustion_returns_409(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    taken = (await client.post("/api/v1/links", json={"url": "https://e.com/1"})).json()["code"]
    monkeypatch.setattr(links_module, "generate_code", lambda length: taken)
    resp = await client.post("/api/v1/links", json={"url": "https://e.com/2"})
    assert resp.status_code == 409
    assert "unique code" in resp.json()["error"]["message"]


async def test_concurrent_creates_get_distinct_codes(client: AsyncClient) -> None:
    responses = await asyncio.gather(
        *(client.post("/api/v1/links", json={"url": f"https://e.com/{i}"}) for i in range(20))
    )
    assert all(r.status_code == 201 for r in responses)
    assert len({r.json()["code"] for r in responses}) == 20


async def test_concurrent_idempotent_requests_create_one_link(client: AsyncClient) -> None:
    headers = {"Idempotency-Key": "same-key"}
    responses = await asyncio.gather(
        *(
            client.post("/api/v1/links", json={"url": "https://e.com/i"}, headers=headers)
            for _ in range(5)
        )
    )
    assert {r.status_code for r in responses} == {201}
    assert len({r.json()["code"] for r in responses}) == 1


async def test_alias_conflict_is_case_insensitive(client: AsyncClient) -> None:
    assert (
        await client.post("/api/v1/links", json={"url": "https://e.com", "custom_alias": "promo"})
    ).status_code == 201
    clash = await client.post(
        "/api/v1/links", json={"url": "https://e.com", "custom_alias": "PROMO"}
    )
    assert clash.status_code == 409


def test_migrations_upgrade_and_downgrade_cleanly(tmp_path: Path) -> None:
    db = tmp_path / "m.db"
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db}")
    command.upgrade(cfg, "head")
    sync = create_engine(f"sqlite:///{db}")
    assert "links" in inspect(sync).get_table_names()
    command.downgrade(cfg, "base")
    assert "links" not in inspect(sync).get_table_names()
    command.upgrade(cfg, "head")
    assert "links" in inspect(sync).get_table_names()
    sync.dispose()
