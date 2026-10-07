# lineage: REQ-BF-002 / implement.analytics (see runs/ for the decision trail)
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select

from service.analytics import ClickEvent, ClickRecorder, referrer_host, ua_family, visitor_hash
from service.models import Click, Link

CHROME = "Mozilla/5.0 (Macintosh) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36"


def test_visitor_hash_is_daily_salted_and_secret_dependent() -> None:
    day = date(2026, 10, 7)
    a = visitor_hash("s1", day, "203.0.113.7", CHROME)
    assert a == visitor_hash("s1", day, "203.0.113.7", CHROME)
    assert a != visitor_hash("s1", day + timedelta(days=1), "203.0.113.7", CHROME)
    assert a != visitor_hash("s2", day, "203.0.113.7", CHROME)
    assert a != visitor_hash("s1", day, "203.0.113.8", CHROME)
    assert len(a) == 64


@pytest.mark.parametrize(
    ("agent", "family"),
    [
        (CHROME, "Chrome"),
        ("Mozilla/5.0 (X11; Linux) Gecko/20100101 Firefox/131.0", "Firefox"),
        ("Mozilla/5.0 (iPhone) AppleWebKit/605.1.15 Version/18.0 Mobile Safari/604.1", "Safari"),
        ("Mozilla/5.0 Chrome/129.0 Safari/537.36 Edg/129.0", "Edge"),
        ("Googlebot/2.1 (+http://www.google.com/bot.html)", "bot"),
        ("curl/8.7.1", "curl"),
        ("something-custom", "other"),
        ("", "unknown"),
    ],
)
def test_user_agent_families(agent: str, family: str) -> None:
    assert ua_family(agent) == family


def test_referrer_host_extraction() -> None:
    assert referrer_host("https://News.Example.com/a?b=1") == "news.example.com"
    assert referrer_host(None) is None
    assert referrer_host("not a url") is None


def event(link_id: int, *, when: datetime | None = None) -> ClickEvent:
    return ClickEvent(
        link_id=link_id,
        occurred_at=when or datetime.now(UTC),
        ip="203.0.113.7",
        user_agent=CHROME,
        referrer="https://ref.example/x",
    )


async def new_link_id(app: FastAPI, client: AsyncClient) -> int:
    code = (await client.post("/api/v1/links", json={"url": "https://e.com/a"})).json()["code"]
    async with app.state.db.session() as session:
        return int((await session.execute(select(Link.id).where(Link.code == code))).scalar_one())


async def test_no_raw_pii_is_persisted(app: FastAPI, client: AsyncClient) -> None:
    code = (await client.post("/api/v1/links", json={"url": "https://e.com/pii"})).json()["code"]
    resp = await client.get(f"/{code}", headers={"User-Agent": CHROME + " UniqueMarker/9.9"})
    assert resp.status_code == 302
    await app.state.recorder.flush()
    async with app.state.db.session() as session:
        rows = (await session.execute(select(Click))).scalars().all()
    assert len(rows) == 1
    stored = " ".join(str(v) for r in rows for v in vars(r).values())
    assert "127.0.0.1" not in stored  # the test client's address
    assert "UniqueMarker" not in stored
    db_file = Path(app.state.settings.database_url.split("///", 1)[1])
    assert b"UniqueMarker" not in db_file.read_bytes()


async def test_recorder_drops_when_queue_is_full(app: FastAPI) -> None:
    recorder = ClickRecorder(app.state.db.sessionmaker, secret="s", queue_size=2)
    assert recorder.record(event(1))
    assert recorder.record(event(1))
    assert not recorder.record(event(1))
    assert recorder.dropped == 1


async def test_recorder_writes_batches_and_counts_failures(
    app: FastAPI, client: AsyncClient
) -> None:
    link_id = await new_link_id(app, client)
    recorder = ClickRecorder(app.state.db.sessionmaker, secret="s", batch_size=3)
    await recorder.start()
    for _ in range(7):
        recorder.record(event(link_id))
    await recorder.flush()
    assert recorder.recorded == 7

    async def broken(batch: list[ClickEvent]) -> None:
        raise RuntimeError("db down")

    recorder._persist = broken  # type: ignore[method-assign]
    recorder.record(event(link_id))
    await recorder.flush()
    assert recorder.failed == 1
    await recorder.stop()


async def test_retention_purge_removes_old_clicks(app: FastAPI, client: AsyncClient) -> None:
    link_id = await new_link_id(app, client)
    recorder = ClickRecorder(app.state.db.sessionmaker, secret="s", retention_days=30)
    await recorder.start()
    now = datetime.now(UTC)
    recorder.record(event(link_id, when=now - timedelta(days=45)))
    recorder.record(event(link_id, when=now - timedelta(days=1)))
    await recorder.flush()
    assert await recorder.purge_expired(now) == 1
    async with app.state.db.session() as session:
        remaining = (
            (await session.execute(select(Click).where(Click.link_id == link_id))).scalars().all()
        )
    assert len(remaining) == 1
    await recorder.stop()
