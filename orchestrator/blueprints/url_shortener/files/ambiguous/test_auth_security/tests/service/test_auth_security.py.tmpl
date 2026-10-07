# lineage: REQ-AMBIGUOUS / test.auth_security (see runs/ for the decision trail)
"""Abuse cases for authentication and authorization."""

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from service.config import Settings
from tests.service.conftest import authed, issue_key


async def test_only_the_owner_can_modify_a_link(app: FastAPI, client: AsyncClient) -> None:
    code = (await client.post("/api/v1/links", json={"url": "https://e.com/own"})).json()["code"]
    async with authed(app, await issue_key(app, "intruder")) as intruder:
        patch = await intruder.patch(f"/api/v1/links/{code}", json={"disabled": True})
        assert patch.status_code == 403
        assert patch.json()["error"]["code"] == "forbidden"
        assert (await intruder.delete(f"/api/v1/links/{code}")).status_code == 403
        assert (await intruder.get(f"/{code}")).status_code == 302  # reads stay public
    assert (await client.patch(f"/api/v1/links/{code}", json={"disabled": True})).status_code == 200


@pytest.mark.parametrize(
    "header",
    [
        "",
        " ",
        "usk_",
        "usk__",
        "Bearer x",
        "usk_12345678_short",
        "usk_1234567g_" + "a" * 43,
        "x" * 5000,
        "usk_12345678_" + "é" * 30,
    ],
)
async def test_malformed_keys_are_rejected_cleanly(anon_client: AsyncClient, header: str) -> None:
    resp = await anon_client.post(
        "/api/v1/links", json={"url": "https://e.com"}, headers={"X-API-Key": header.encode()}
    )
    assert resp.status_code == 401


async def test_idempotency_keys_are_scoped_per_owner(app: FastAPI, client: AsyncClient) -> None:
    body = {"url": "https://e.com/idem"}
    first = await client.post("/api/v1/links", json=body, headers={"Idempotency-Key": "shared"})
    assert first.status_code == 201
    async with authed(app, await issue_key(app, "other")) as other:
        clash = await other.post("/api/v1/links", json=body, headers={"Idempotency-Key": "shared"})
    assert clash.status_code == 409  # never replays another owner's link


async def test_rate_limits_are_per_api_key(settings: Settings) -> None:
    from tests.service.test_enterprise import started

    async with started(
        settings, rate_limit_write_capacity=1, rate_limit_write_refill_per_s=0.001
    ) as (app, a):
        assert (await a.post("/api/v1/links", json={"url": "https://e.com/1"})).status_code == 201
        assert (await a.post("/api/v1/links", json={"url": "https://e.com/2"})).status_code == 429
        async with authed(app, await issue_key(app, "b"), "http://t") as b:
            assert (
                await b.post("/api/v1/links", json={"url": "https://e.com/3"})
            ).status_code == 201


async def test_presented_keys_never_reach_logs(
    anon_client: AsyncClient, client: AsyncClient, api_key: str, capfd: pytest.CaptureFixture[str]
) -> None:
    wrong = api_key[:-4] + "zzzz"
    await anon_client.post(
        "/api/v1/links", json={"url": "https://e.com"}, headers={"X-API-Key": wrong}
    )
    await client.post("/api/v1/links", json={"url": "https://e.com/ok"})
    out, err = capfd.readouterr()
    assert api_key not in out + err
    assert wrong not in out + err
