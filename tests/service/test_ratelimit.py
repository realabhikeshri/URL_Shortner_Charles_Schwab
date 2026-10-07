# lineage: REQ-AMBIGUOUS / implement.rate_limiter (see runs/ for the decision trail)
import asyncio

import fakeredis
import pytest

from service.ratelimit import MemoryRateLimiter, RateLimitedError, RedisRateLimiter, enforce


@pytest.fixture(params=["memory", "redis"])
def limiter_factory(request: pytest.FixtureRequest):  # type: ignore[no-untyped-def]
    def make(capacity: int, rate: float):  # type: ignore[no-untyped-def]
        if request.param == "memory":
            return MemoryRateLimiter(capacity, rate)
        return RedisRateLimiter(fakeredis.FakeAsyncRedis(), capacity, rate)

    return make


async def test_bucket_allows_burst_then_limits(limiter_factory) -> None:  # type: ignore[no-untyped-def]
    limiter = limiter_factory(3, 0.5)
    results = [await limiter.hit("client") for _ in range(4)]
    assert [r.allowed for r in results] == [True, True, True, False]
    assert results[0].remaining == 2
    blocked = results[-1]
    assert blocked.retry_after_s > 0
    assert blocked.headers()["Retry-After"] == "2"
    assert blocked.headers()["X-RateLimit-Limit"] == "3"


async def test_buckets_are_independent_per_key(limiter_factory) -> None:  # type: ignore[no-untyped-def]
    limiter = limiter_factory(1, 0.1)
    assert (await limiter.hit("a")).allowed
    assert (await limiter.hit("b")).allowed
    assert not (await limiter.hit("a")).allowed


async def test_tokens_refill_over_time(limiter_factory) -> None:  # type: ignore[no-untyped-def]
    limiter = limiter_factory(1, 20.0)
    assert (await limiter.hit("k")).allowed
    assert not (await limiter.hit("k")).allowed
    await asyncio.sleep(0.1)
    assert (await limiter.hit("k")).allowed


async def test_enforce_raises_429_with_headers() -> None:
    limiter = MemoryRateLimiter(1, 0.01)
    await enforce(limiter, "x")
    with pytest.raises(RateLimitedError) as exc:
        await enforce(limiter, "x")
    assert exc.value.status_code == 429
    assert "Retry-After" in exc.value.headers


async def test_memory_limiter_bounds_its_key_space() -> None:
    limiter = MemoryRateLimiter(5, 1.0, max_keys=10)
    for i in range(50):
        await limiter.hit(f"k{i}")
    assert len(limiter._buckets) == 10
