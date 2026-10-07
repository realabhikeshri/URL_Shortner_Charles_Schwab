# lineage: REQ-GF-001 / implement.links_api, REQ-BF-002 / implement.expiry,
#          REQ-AMBIGUOUS / implement.enterprise_wiring, implement.api_key_auth
"""Link use cases: create (idempotent), resolve, read, update, soft-delete."""

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from service.auth import ForbiddenError
from service.cache import Cache
from service.config import Settings
from service.db import utcnow
from service.domain.codes import AliasError, generate_code, normalize_alias
from service.domain.urls import UrlError, normalize_url
from service.errors import ConflictError, GoneError, InvalidInputError, NotFoundError
from service.metrics import Metrics
from service.models import Link
from service.observability import get_logger
from service.repository import LinkRepository
from service.schemas import LinkCreate, LinkOut, LinkUpdate

log = get_logger("links")


def is_expired(link: Link, now: datetime | None = None) -> bool:
    return link.expires_at is not None and link.expires_at <= (now or utcnow())


def validate_expiry(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise InvalidInputError("expires_at must include a timezone offset")
    if value <= utcnow():
        raise InvalidInputError("expires_at must be in the future")
    return value


MISSING = "__missing__"


@dataclass(frozen=True)
class Resolved:
    """What the redirect path needs, small enough to cache."""

    id: int
    code: str
    target_url: str
    is_custom: bool
    expires_at: datetime | None
    gone: bool

    @classmethod
    def from_link(cls, link: Link) -> "Resolved":
        return cls(
            id=link.id,
            code=link.code,
            target_url=link.target_url,
            is_custom=link.is_custom,
            expires_at=link.expires_at,
            gone=link.deleted_at is not None or link.disabled,
        )

    def to_json(self) -> str:
        data = asdict(self)
        data["expires_at"] = self.expires_at.isoformat() if self.expires_at else None
        return json.dumps(data)

    @classmethod
    def from_json(cls, raw: str) -> "Resolved":
        data: dict[str, Any] = json.loads(raw)
        expires = data.get("expires_at")
        data["expires_at"] = datetime.fromisoformat(expires) if expires else None
        return cls(**data)


def fingerprint(payload: LinkCreate) -> str:
    canonical = json.dumps(payload.model_dump(mode="json"), sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


class LinkService:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        *,
        cache: Cache | None = None,
        metrics: Metrics | None = None,
    ) -> None:
        self.session = session
        self.settings = settings
        self.repo = LinkRepository(session)
        self.cache = cache
        self.metrics = metrics

    def to_out(self, link: Link) -> LinkOut:
        return LinkOut(
            code=link.code,
            short_url=f"{self.settings.base_url}/{link.code}",
            target_url=link.target_url,
            is_custom=link.is_custom,
            tags=list(link.tags or []),
            disabled=link.disabled,
            created_at=link.created_at,
            updated_at=link.updated_at,
            expires_at=link.expires_at,
            is_expired=is_expired(link),
        )

    async def create(
        self,
        payload: LinkCreate,
        idempotency_key: str | None = None,
        *,
        owner_key_id: int | None = None,
    ) -> tuple[Link, bool]:
        """Return ``(link, created)``; ``created`` is False for an idempotent replay."""
        try:
            target = normalize_url(
                payload.url,
                max_length=self.settings.max_url_length,
                allow_private=self.settings.allow_private_hosts,
            )
        except UrlError as exc:
            raise InvalidInputError(str(exc)) from exc
        expires_at = validate_expiry(payload.expires_at)
        digest = fingerprint(payload)
        if idempotency_key:
            replay = await self._replay(idempotency_key, digest, owner_key_id)
            if replay is not None:
                return replay, False

        if payload.custom_alias is not None:
            try:
                alias = normalize_alias(payload.custom_alias)
            except AliasError as exc:
                raise InvalidInputError(str(exc)) from exc
            if await self.repo.alias_taken(alias):
                raise ConflictError(f"alias '{payload.custom_alias}' is already taken")
            candidates = [alias]
        else:
            candidates = [
                generate_code(self.settings.code_length)
                for _ in range(self.settings.code_max_attempts)
            ]

        for code in candidates:
            if payload.custom_alias is None and await self.repo.code_exists(code):
                log.info("code collision, retrying", extra={"code_length": len(code)})
                continue
            link = Link(
                code=code,
                target_url=target,
                is_custom=payload.custom_alias is not None,
                tags=list(payload.tags),
                expires_at=expires_at,
                owner_key_id=owner_key_id,
                idempotency_key=idempotency_key,
                request_fingerprint=digest if idempotency_key else None,
            )
            try:
                await self.repo.add(link)
                await self.session.commit()
            except IntegrityError:
                await self.session.rollback()
                if idempotency_key:
                    replay = await self._replay(idempotency_key, digest, owner_key_id)
                    if replay is not None:
                        return replay, False
                if payload.custom_alias is not None:
                    raise ConflictError(
                        f"alias '{payload.custom_alias}' is already taken"
                    ) from None
                continue
            log.info("link created", extra={"code": link.code, "custom": link.is_custom})
            await self._invalidate(link.code)  # clears a cached "missing" marker
            return link, True
        raise ConflictError("could not allocate a unique code; please retry")

    async def _replay(self, key: str, digest: str, owner_key_id: int | None) -> Link | None:
        existing = await self.repo.get_by_idempotency_key(key)
        if existing is None:
            return None
        if existing.owner_key_id != owner_key_id or existing.request_fingerprint != digest:
            raise ConflictError("Idempotency-Key was already used with a different request body")
        return existing

    # ------------------------------------------------------------------ redirect hot path

    async def _cache_get(self, key: str) -> str | None:
        if self.cache is None:
            return None
        try:
            return await self.cache.get(key)
        except Exception as exc:  # a cache outage degrades to the database, never to an error
            log.warning("cache read failed", extra={"error": type(exc).__name__})
            return None

    async def _cache_set(self, key: str, value: str, ttl_s: float) -> None:
        if self.cache is None:
            return
        try:
            await self.cache.set(key, value, ttl_s)
        except Exception as exc:
            log.warning("cache write failed", extra={"error": type(exc).__name__})

    async def _invalidate(self, code: str) -> None:
        if self.cache is None:
            return
        try:
            await self.cache.delete(f"link:{code}")
        except Exception as exc:
            log.warning("cache invalidation failed", extra={"error": type(exc).__name__})

    def _count(self, result: str) -> None:
        if self.metrics is not None and self.cache is not None:
            self.metrics.cache.labels(result).inc()

    async def _lookup(self, code: str) -> Resolved | None:
        """Read-through cache keyed by canonical code; aliases resolve case-insensitively."""
        raw = await self._cache_get(f"link:{code}")
        if raw is None and code != code.lower():
            alias_raw = await self._cache_get(f"link:{code.lower()}")
            if (
                alias_raw is not None
                and alias_raw != MISSING
                and Resolved.from_json(alias_raw).is_custom
            ):
                raw = alias_raw
        if raw == MISSING:
            self._count("hit")
            return None
        if raw is not None:
            self._count("hit")
            return Resolved.from_json(raw)
        self._count("miss")
        link = await self.repo.get_by_code(code)
        if link is None:
            if code == code.lower():  # only canonical misses are cached (see ADR-013)
                await self._cache_set(f"link:{code}", MISSING, self.settings.cache_negative_ttl_s)
            return None
        resolved = Resolved.from_link(link)
        await self._cache_set(f"link:{link.code}", resolved.to_json(), self.settings.cache_ttl_s)
        return resolved

    async def resolve(self, code: str) -> Resolved:
        """Look up a link for redirecting; 404 for unknown, 410 for disabled/deleted/expired."""
        resolved = await self._lookup(code)
        if resolved is None:
            raise NotFoundError(f"no link with code '{code}'")
        if resolved.gone:
            raise GoneError(f"link '{code}' is no longer available")
        if resolved.expires_at is not None and resolved.expires_at <= utcnow():
            raise GoneError(f"link '{code}' has expired")
        return resolved

    async def get(self, code: str) -> Link:
        link = await self.repo.get_by_code(code)
        if link is None or link.deleted_at is not None:
            raise NotFoundError(f"no link with code '{code}'")
        return link

    def _authorize(self, link: Link, actor_key_id: int | None) -> None:
        """Only the creating key may change a link (links created before auth have no owner)."""
        if not self.settings.auth_enabled or link.owner_key_id is None:
            return
        if actor_key_id != link.owner_key_id:
            raise ForbiddenError("only the API key that created this link may modify it")

    async def update(
        self, code: str, patch: LinkUpdate, *, actor_key_id: int | None = None
    ) -> Link:
        link = await self.get(code)
        self._authorize(link, actor_key_id)
        if patch.disabled is not None:
            link.disabled = patch.disabled
        if "expires_at" in patch.model_fields_set:
            link.expires_at = validate_expiry(patch.expires_at)
        link.updated_at = utcnow()
        await self.session.commit()
        await self._invalidate(link.code)
        return link

    async def delete(self, code: str, *, actor_key_id: int | None = None) -> None:
        link = await self.get(code)
        self._authorize(link, actor_key_id)
        link.deleted_at = utcnow()
        link.updated_at = link.deleted_at
        await self.session.commit()
        await self._invalidate(link.code)
        log.info("link deleted", extra={"code": code})
