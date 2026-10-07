# lineage: REQ-GF-001 / implement.links_api, REQ-BF-002 / implement.alias_fix
"""Data access for links."""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from service.models import Link


class LinkRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_code(self, code: str) -> Link | None:
        """Exact match first (generated codes are case-sensitive), then the canonical alias.

        Aliases are stored lower-cased, so a lower-cased lookup restricted to custom links makes
        aliases case-insensitive without ever folding generated base62 codes (REQ-BF-002).
        """
        result = await self.session.execute(select(Link).where(Link.code == code))
        link = result.scalar_one_or_none()
        if link is None and code != code.lower():
            alias = await self.session.execute(
                select(Link).where(Link.code == code.lower(), Link.is_custom.is_(True))
            )
            link = alias.scalar_one_or_none()
        return link

    async def code_exists(self, code: str) -> bool:
        result = await self.session.execute(select(Link.id).where(Link.code == code))
        return result.first() is not None

    async def alias_taken(self, alias: str) -> bool:
        result = await self.session.execute(
            select(Link.id).where(func.lower(Link.code) == alias.lower())
        )
        return result.first() is not None

    async def get_by_idempotency_key(self, key: str) -> Link | None:
        result = await self.session.execute(select(Link).where(Link.idempotency_key == key))
        return result.scalar_one_or_none()

    async def add(self, link: Link) -> Link:
        self.session.add(link)
        await self.session.flush()
        return link
