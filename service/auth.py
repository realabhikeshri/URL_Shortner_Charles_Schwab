# lineage: REQ-AMBIGUOUS / implement.api_key_auth (see runs/ for the decision trail)
"""API-key authentication for write endpoints.

Keys look like ``usk_<8 hex prefix>_<43 char secret>`` (256 bits of entropy). Only the SHA-256 of
the full key is stored; the prefix is an indexed lookup handle. A slow KDF is unnecessary for keys
with this much entropy (unlike passwords). Comparison uses ``hmac.compare_digest`` and a hash is
computed even for unknown prefixes so failures take similar time.
"""

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from service.db import utcnow
from service.errors import AppError
from service.models import ApiKey

SCHEME = "usk"
_KEY_RE = re.compile(r"^usk_([0-9a-f]{8})_([A-Za-z0-9_-]{20,64})$")
_DUMMY_HASH = hashlib.sha256(b"dummy").hexdigest()


class UnauthorizedError(AppError):
    status_code = 401
    code = "unauthorized"

    def __init__(self, message: str = "a valid X-API-Key header is required") -> None:
        super().__init__(message, headers={"WWW-Authenticate": 'ApiKey realm="url-shortener"'})


class ForbiddenError(AppError):
    status_code = 403
    code = "forbidden"


@dataclass(frozen=True)
class IssuedKey:
    key: str
    prefix: str
    key_hash: str


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def generate_key() -> IssuedKey:
    prefix = secrets.token_hex(4)
    key = f"{SCHEME}_{prefix}_{secrets.token_urlsafe(32)}"
    return IssuedKey(key=key, prefix=prefix, key_hash=hash_key(key))


async def create_api_key(session: AsyncSession, name: str) -> str:
    """Persist a new key and return the plaintext (shown once, never stored)."""
    issued = generate_key()
    session.add(ApiKey(name=name, prefix=issued.prefix, key_hash=issued.key_hash))
    await session.commit()
    return issued.key


async def revoke_api_key(session: AsyncSession, prefix: str) -> bool:
    key = (
        await session.execute(select(ApiKey).where(ApiKey.prefix == prefix))
    ).scalar_one_or_none()
    if key is None or key.revoked_at is not None:
        return False
    key.revoked_at = utcnow()
    await session.commit()
    return True


async def authenticate(session: AsyncSession, presented: str | None) -> ApiKey:
    if not presented:
        raise UnauthorizedError()
    match = _KEY_RE.match(presented.strip())
    if match is None:
        raise UnauthorizedError("malformed API key")
    record = (
        await session.execute(select(ApiKey).where(ApiKey.prefix == match.group(1)))
    ).scalar_one_or_none()
    expected = record.key_hash if record is not None else _DUMMY_HASH
    valid = hmac.compare_digest(hash_key(presented.strip()), expected)
    if record is None or not valid or record.revoked_at is not None:
        raise UnauthorizedError("invalid or revoked API key")
    return record
