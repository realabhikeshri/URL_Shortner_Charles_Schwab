# lineage: REQ-GF-001 / implement.codes (see runs/ for the decision trail)
"""Short-code generation (base62) and custom-alias validation.

Capacity: 62**7 ~= 3.52e12 codes. With n links stored, a fresh random code collides with
probability n / 62**7 (1e9 links -> 2.8e-4). Generation retries up to ``max_attempts`` times,
so an exhausted retry budget needs (n / 62**7) ** attempts: ~1.7e-18 at 1e9 links with 5 tries.
The database unique constraint remains the final arbiter for races between writers.
"""

import re
import secrets
import string

ALPHABET = string.digits + string.ascii_uppercase + string.ascii_lowercase
BASE = len(ALPHABET)
_INDEX = {ch: i for i, ch in enumerate(ALPHABET)}

ALIAS_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,31}$")
RESERVED = frozenset(
    {
        "api",
        "healthz",
        "readyz",
        "metrics",
        "docs",
        "redoc",
        "openapi.json",
        "static",
        "admin",
        "login",
        "favicon.ico",
        "robots.txt",
    }
)


class AliasError(ValueError):
    """The requested custom alias is not acceptable."""


def encode(number: int) -> str:
    if number < 0:
        raise ValueError("cannot encode negative numbers")
    if number == 0:
        return ALPHABET[0]
    digits = []
    while number:
        number, rem = divmod(number, BASE)
        digits.append(ALPHABET[rem])
    return "".join(reversed(digits))


def decode(code: str) -> int:
    if not code:
        raise ValueError("empty code")
    number = 0
    for ch in code:
        if ch not in _INDEX:
            raise ValueError(f"invalid base62 character {ch!r}")
        number = number * BASE + _INDEX[ch]
    return number


def generate_code(length: int = 7) -> str:
    """Uniformly random base62 code of exactly ``length`` characters (CSPRNG)."""
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


def collision_probability(existing: int, length: int = 7) -> float:
    """Probability that one freshly generated code hits an existing one."""
    return existing / float(BASE**length)


def normalize_alias(alias: str) -> str:
    """Validate a custom alias and return its canonical (lower-case) form."""
    candidate = alias.strip()
    if not ALIAS_PATTERN.match(candidate):
        raise AliasError(
            "alias must be 3-32 characters of letters, digits, '-' or '_' "
            "and start with a letter or digit"
        )
    canonical = candidate.lower()
    if canonical in RESERVED:
        raise AliasError(f"alias '{candidate}' is reserved")
    return canonical
