# lineage: REQ-AMBIGUOUS / implement.rate_limiter (see runs/ for the decision trail)
"""Token-bucket rate limiting (in-memory per process, or Redis via an atomic Lua script)."""

import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Protocol

from service.errors import AppError


class RateLimitedError(AppError):
    status_code = 429
    code = "rate_limited"


@dataclass(frozen=True)
class Decision:
    allowed: bool
    limit: int
    remaining: int
    retry_after_s: float

    def headers(self) -> dict[str, str]:
        headers = {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(self.remaining),
        }
        if not self.allowed:
            headers["Retry-After"] = str(max(1, round(self.retry_after_s + 0.5)))
        return headers


class RateLimiter(Protocol):
    capacity: int

    async def hit(self, key: str) -> Decision: ...


def _decide(tokens: float, capacity: int, refill_per_s: float, allowed: bool) -> Decision:
    retry = 0.0 if allowed else (1 - tokens) / refill_per_s
    return Decision(allowed, capacity, int(tokens), retry)


class MemoryRateLimiter:
    def __init__(self, capacity: int, refill_per_s: float, *, max_keys: int = 100_000) -> None:
        self.capacity = capacity
        self.refill_per_s = refill_per_s
        self.max_keys = max_keys
        self._buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()

    async def hit(self, key: str) -> Decision:
        now = time.monotonic()
        tokens, updated = self._buckets.get(key, (float(self.capacity), now))
        tokens = min(float(self.capacity), tokens + (now - updated) * self.refill_per_s)
        allowed = tokens >= 1
        if allowed:
            tokens -= 1
        self._buckets[key] = (tokens, now)
        self._buckets.move_to_end(key)
        while len(self._buckets) > self.max_keys:
            self._buckets.popitem(last=False)
        return _decide(tokens, self.capacity, self.refill_per_s, allowed)


BUCKET_SCRIPT_LUA = """
local capacity = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local ttl = tonumber(ARGV[4])
local data = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts = tonumber(data[2])
if tokens == nil then
  tokens = capacity
  ts = now
end
tokens = math.min(capacity, tokens + math.max(0, now - ts) * rate)
local allowed = 0
if tokens >= 1 then
  tokens = tokens - 1
  allowed = 1
end
redis.call('HSET', KEYS[1], 'tokens', tostring(tokens), 'ts', tostring(now))
redis.call('EXPIRE', KEYS[1], ttl)
return {allowed, tostring(tokens)}
"""


class RedisRateLimiter:
    """Shared across replicas; the Lua script makes read-modify-write atomic."""

    def __init__(
        self, client: Any, capacity: int, refill_per_s: float, prefix: str = "rl:"
    ) -> None:
        self.client = client
        self.capacity = capacity
        self.refill_per_s = refill_per_s
        self.prefix = prefix
        self._script = client.register_script(BUCKET_SCRIPT_LUA)
        self._ttl = max(int(capacity / refill_per_s) + 1, 1)

    async def hit(self, key: str) -> Decision:
        allowed, tokens = await self._script(
            keys=[self.prefix + key],
            args=[self.capacity, self.refill_per_s, time.time(), self._ttl],
        )
        tokens_f = float(tokens.decode() if isinstance(tokens, bytes) else tokens)
        return _decide(tokens_f, self.capacity, self.refill_per_s, bool(int(allowed)))


async def enforce(limiter: RateLimiter, key: str) -> Decision:
    decision = await limiter.hit(key)
    if not decision.allowed:
        raise RateLimitedError("rate limit exceeded; retry later", headers=decision.headers())
    return decision
