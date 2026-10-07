# lineage: REQ-AMBIGUOUS / implement.cache_backend (see runs/ for the decision trail)
"""Cache abstraction: bounded in-memory TTL LRU (single process) or Redis (shared)."""

import time
from collections import OrderedDict
from typing import Any, Protocol

import redis.asyncio as redis_asyncio


class Cache(Protocol):
    backend: str

    async def get(self, key: str) -> str | None: ...

    async def set(self, key: str, value: str, ttl_s: float) -> None: ...

    async def delete(self, *keys: str) -> None: ...

    async def ping(self) -> bool: ...

    async def close(self) -> None: ...


class MemoryCache:
    """LRU with per-entry TTL. Safe within one event loop (no awaits while mutating)."""

    backend = "memory"

    def __init__(self, max_entries: int = 10_000) -> None:
        self.max_entries = max_entries
        self._data: OrderedDict[str, tuple[float, str]] = OrderedDict()

    async def get(self, key: str) -> str | None:
        item = self._data.get(key)
        if item is None:
            return None
        expires, value = item
        if expires <= time.monotonic():
            del self._data[key]
            return None
        self._data.move_to_end(key)
        return value

    async def set(self, key: str, value: str, ttl_s: float) -> None:
        self._data[key] = (time.monotonic() + ttl_s, value)
        self._data.move_to_end(key)
        while len(self._data) > self.max_entries:
            self._data.popitem(last=False)

    async def delete(self, *keys: str) -> None:
        for key in keys:
            self._data.pop(key, None)

    async def ping(self) -> bool:
        return True

    async def close(self) -> None:
        self._data.clear()

    def __len__(self) -> int:
        return len(self._data)


class RedisCache:
    backend = "redis"

    def __init__(self, client: Any, prefix: str = "shortener:") -> None:
        self.client = client
        self.prefix = prefix

    async def get(self, key: str) -> str | None:
        value = await self.client.get(self.prefix + key)
        if value is None:
            return None
        return value.decode() if isinstance(value, bytes) else str(value)

    async def set(self, key: str, value: str, ttl_s: float) -> None:
        await self.client.set(self.prefix + key, value, px=max(int(ttl_s * 1000), 1))

    async def delete(self, *keys: str) -> None:
        if keys:
            await self.client.delete(*(self.prefix + k for k in keys))

    async def ping(self) -> bool:
        return bool(await self.client.ping())

    async def close(self) -> None:
        await self.client.aclose()


def redis_client(url: str) -> Any:
    return redis_asyncio.from_url(url, socket_timeout=0.5, socket_connect_timeout=0.5)


def build_cache(redis_url: str | None, *, max_entries: int = 10_000, client: Any = None) -> Cache:
    if redis_url or client is not None:
        return RedisCache(client if client is not None else redis_client(str(redis_url)))
    return MemoryCache(max_entries)
