# lineage: REQ-BF-002 / implement.analytics (see runs/ for the decision trail)
"""Click analytics: recorded off the redirect hot path, without storing personal data.

* The redirect handler calls :meth:`ClickRecorder.record`, which only does ``put_nowait`` on a
  bounded queue. It never awaits I/O and never raises; on overflow the click is dropped and
  counted. A background task drains the queue and writes clicks in batches.
* Unique visitors: ``sha256(HMAC(secret, day) || ip || user_agent)``. The salt rotates daily, so
  hashes cannot be joined across days and the raw IP / user agent are never persisted.
* Retention: clicks older than ``analytics_retention_days`` are purged periodically.
"""

import asyncio
import contextlib
import hashlib
import hmac
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from service.models import Click
from service.observability import get_logger

log = get_logger("analytics")

_UA_RULES: Sequence[tuple[str, str]] = (
    ("bot", "bot"),
    ("crawler", "bot"),
    ("spider", "bot"),
    ("edg/", "Edge"),
    ("opr/", "Opera"),
    ("firefox/", "Firefox"),
    ("chrome/", "Chrome"),
    ("safari/", "Safari"),
    ("curl/", "curl"),
    ("python-httpx", "httpx"),
    ("python-requests", "requests"),
)


@dataclass(frozen=True)
class ClickEvent:
    link_id: int
    occurred_at: datetime
    ip: str
    user_agent: str
    referrer: str | None


def daily_salt(secret: str, day: date) -> bytes:
    return hmac.new(secret.encode(), day.isoformat().encode(), hashlib.sha256).digest()


def visitor_hash(secret: str, day: date, ip: str, user_agent: str) -> str:
    material = daily_salt(secret, day) + f"{ip}|{user_agent}".encode()
    return hashlib.sha256(material).hexdigest()


def ua_family(user_agent: str) -> str:
    lowered = user_agent.lower()
    for needle, family in _UA_RULES:
        if needle in lowered:
            return family
    return "other" if user_agent else "unknown"


def referrer_host(referrer: str | None) -> str | None:
    if not referrer:
        return None
    host = urlsplit(referrer).hostname
    return host.lower()[:255] if host else None


class ClickRecorder:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        secret: str,
        queue_size: int = 10_000,
        batch_size: int = 200,
        retention_days: int = 90,
        purge_interval_s: float = 3600.0,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.secret = secret
        self.batch_size = batch_size
        self.retention_days = retention_days
        self.purge_interval_s = purge_interval_s
        self.queue: asyncio.Queue[ClickEvent] = asyncio.Queue(maxsize=queue_size)
        self.recorded = 0
        self.dropped = 0
        self.failed = 0
        self._tasks: list[asyncio.Task[None]] = []

    def record(self, event: ClickEvent) -> bool:
        """Enqueue without blocking. Returns False (and counts a drop) when the queue is full."""
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            self.dropped += 1
            return False
        return True

    async def start(self) -> None:
        await self._purge_once()  # enforce retention at startup, before accepting clicks
        self._tasks = [
            asyncio.create_task(self._writer(), name="click-writer"),
            asyncio.create_task(self._purger(), name="click-purger"),
        ]

    async def stop(self) -> None:
        await self.flush()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks = []

    async def flush(self) -> None:
        """Wait until every queued click has been written (or failed)."""
        await self.queue.join()

    async def _writer(self) -> None:
        while True:
            batch = [await self.queue.get()]
            while len(batch) < self.batch_size and not self.queue.empty():
                batch.append(self.queue.get_nowait())
            try:
                await self._persist(batch)
                self.recorded += len(batch)
            except Exception as exc:
                self.failed += len(batch)
                log.warning(
                    "click batch failed", extra={"batch": len(batch), "error": type(exc).__name__}
                )
            finally:
                for _ in batch:
                    self.queue.task_done()

    async def _persist(self, batch: list[ClickEvent]) -> None:
        async with self.sessionmaker() as session:
            for e in batch:
                day = e.occurred_at.astimezone(UTC).date()
                session.add(
                    Click(
                        link_id=e.link_id,
                        occurred_at=e.occurred_at,
                        day=day,
                        visitor_hash=visitor_hash(self.secret, day, e.ip, e.user_agent),
                        referrer_host=referrer_host(e.referrer),
                        ua_family=ua_family(e.user_agent),
                    )
                )
            await session.commit()

    async def _purger(self) -> None:
        while True:
            await asyncio.sleep(self.purge_interval_s)
            await self._purge_once()

    async def _purge_once(self) -> None:
        try:
            removed = await self.purge_expired()
            if removed:
                log.info("purged expired clicks", extra={"removed": removed})
        except Exception as exc:
            log.warning("click purge failed", extra={"error": type(exc).__name__})

    async def purge_expired(self, now: datetime | None = None) -> int:
        cutoff = (now or datetime.now(UTC)) - timedelta(days=self.retention_days)
        async with self.sessionmaker() as session:
            result = await session.execute(delete(Click).where(Click.occurred_at < cutoff))
            await session.commit()
            return int(getattr(result, "rowcount", 0) or 0)


async def link_stats(session: AsyncSession, link_id: int, *, days: int) -> dict[str, Any]:
    since = datetime.now(UTC).date() - timedelta(days=days - 1)
    scope = (Click.link_id == link_id, Click.day >= since)
    per_day = (
        await session.execute(
            select(
                Click.day,
                func.count(Click.id),
                func.count(func.distinct(Click.visitor_hash)),
            )
            .where(*scope)
            .group_by(Click.day)
            .order_by(Click.day)
        )
    ).all()
    referrers = (
        await session.execute(
            select(Click.referrer_host, func.count(Click.id))
            .where(*scope, Click.referrer_host.is_not(None))
            .group_by(Click.referrer_host)
            .order_by(func.count(Click.id).desc(), Click.referrer_host)
            .limit(10)
        )
    ).all()
    agents = (
        await session.execute(
            select(Click.ua_family, func.count(Click.id))
            .where(*scope)
            .group_by(Click.ua_family)
            .order_by(func.count(Click.id).desc(), Click.ua_family)
            .limit(10)
        )
    ).all()
    return {
        "window_days": days,
        "total_clicks": sum(int(c) for _, c, _ in per_day),
        "unique_visitors": sum(int(u) for _, _, u in per_day),
        "clicks_by_day": [
            {"day": d.isoformat(), "clicks": int(c), "unique_visitors": int(u)}
            for d, c, u in per_day
        ],
        "top_referrers": [{"host": h, "clicks": int(c)} for h, c in referrers],
        "top_user_agents": [{"family": f, "clicks": int(c)} for f, c in agents],
    }
