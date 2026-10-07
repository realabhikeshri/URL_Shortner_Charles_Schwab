# lineage: REQ-AMBIGUOUS / implement.cache_backend (see runs/ for the decision trail)
import asyncio

import fakeredis
import pytest

from service.cache import Cache, MemoryCache, RedisCache, build_cache


@pytest.fixture(params=["memory", "redis"])
async def cache(request: pytest.FixtureRequest) -> Cache:
    if request.param == "memory":
        return MemoryCache(max_entries=3)
    return build_cache(None, client=fakeredis.FakeAsyncRedis())


async def test_set_get_delete(cache: Cache) -> None:
    assert await cache.get("k") is None
    await cache.set("k", "v", 10)
    assert await cache.get("k") == "v"
    await cache.delete("k", "missing")
    assert await cache.get("k") is None
    assert await cache.ping()


async def test_entries_expire(cache: Cache) -> None:
    await cache.set("short", "v", 0.05)
    await asyncio.sleep(0.12)
    assert await cache.get("short") is None


async def test_memory_cache_evicts_least_recently_used() -> None:
    cache = MemoryCache(max_entries=2)
    await cache.set("a", "1", 60)
    await cache.set("b", "2", 60)
    assert await cache.get("a") == "1"  # a is now most recent
    await cache.set("c", "3", 60)
    assert await cache.get("b") is None
    assert await cache.get("a") == "1"
    assert len(cache) == 2
    await cache.close()
    assert len(cache) == 0


async def test_redis_cache_uses_prefix_and_closes() -> None:
    client = fakeredis.FakeAsyncRedis()
    cache = RedisCache(client, prefix="p:")
    await cache.set("x", "y", 5)
    assert await client.get("p:x") == b"y"
    await cache.close()


def test_build_cache_selects_backend() -> None:
    assert build_cache(None).backend == "memory"
    assert build_cache("redis://localhost:6399/0").backend == "redis"
