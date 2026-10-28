"""The shared cache: Redis when configured, an in-process TTL cache otherwise.

Cache-aside throughout. Keys are content-addressed (the dataset's hash, the
engine version, a hash of the filters), so an entry can never go stale: new
rows or new rules make a new key, and old ones simply expire.

The cache is an optimisation, never a dependency: if Redis is unreachable, a
read is a miss and a write is dropped, and the request is computed as usual.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from cachetools import TTLCache

log = logging.getLogger(__name__)


class CacheBackend(Protocol):
    async def get(self, key: str) -> bytes | None: ...
    async def set(self, key: str, value: bytes, ttl: int) -> None: ...
    async def delete(self, key: str) -> None: ...
    async def incr(self, key: str, ttl: int) -> int: ...
    async def close(self) -> None: ...


class MemoryBackend:
    """For development, tests and single-process deployments."""

    def __init__(self, maxsize: int = 2048, ttl: int = 3600) -> None:
        self._data: TTLCache[str, tuple[float, bytes]] = TTLCache(maxsize=maxsize, ttl=ttl)

    async def get(self, key: str) -> bytes | None:
        item = self._data.get(key)
        if item is None or item[0] < time.monotonic():
            return None
        return item[1]

    async def set(self, key: str, value: bytes, ttl: int) -> None:
        self._data[key] = (time.monotonic() + ttl, value)

    async def delete(self, key: str) -> None:
        self._data.pop(key, None)

    async def incr(self, key: str, ttl: int) -> int:
        current = await self.get(key)
        n = int(current or 0) + 1
        expires = self._data[key][0] if current is not None else time.monotonic() + ttl
        self._data[key] = (expires, str(n).encode())
        return n

    async def close(self) -> None:
        self._data.clear()


class RedisBackend:
    def __init__(self, url: str) -> None:
        from redis.asyncio import Redis

        self._redis = Redis.from_url(url, socket_timeout=1.5, socket_connect_timeout=1.5, health_check_interval=30)

    async def get(self, key: str) -> bytes | None:
        try:
            return await self._redis.get(key)
        except Exception as e:  # any Redis failure degrades to a miss
            log.warning("cache get failed: %s", e)
            return None

    async def set(self, key: str, value: bytes, ttl: int) -> None:
        try:
            await self._redis.set(key, value, ex=ttl)
        except Exception as e:
            log.warning("cache set failed: %s", e)

    async def delete(self, key: str) -> None:
        try:
            await self._redis.delete(key)
        except Exception as e:
            log.warning("cache delete failed: %s", e)

    async def incr(self, key: str, ttl: int) -> int:
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.incr(key)
                pipe.expire(key, ttl, nx=True)
                n, _ = await pipe.execute()
            return int(n)
        except Exception as e:
            log.warning("cache incr failed: %s", e)
            return 0  # fail open: a cache outage must not lock people out

    async def close(self) -> None:
        await self._redis.aclose()


class Cache:
    """JSON values with single-flight computation.

    `get_or_compute` lets only one coroutine per process compute a missing
    key; the others wait for its result instead of repeating the work (a
    dashboard opened by a team at once is computed once).
    """

    def __init__(self, backend: CacheBackend, default_ttl: int) -> None:
        self.backend = backend
        self.default_ttl = default_ttl
        self._locks: dict[str, asyncio.Lock] = {}

    async def get_json(self, key: str) -> Any | None:
        raw = await self.backend.get(key)
        return None if raw is None else json.loads(raw)

    async def set_json(self, key: str, value: Any, ttl: int | None = None) -> None:
        await self.backend.set(key, json.dumps(value, separators=(",", ":"), default=str).encode(), ttl or self.default_ttl)

    async def delete(self, key: str) -> None:
        await self.backend.delete(key)

    async def incr(self, key: str, ttl: int) -> int:
        return await self.backend.incr(key, ttl)

    async def get_or_compute(self, key: str, compute: Callable[[], Awaitable[Any]], ttl: int | None = None) -> Any:
        hit = await self.get_json(key)
        if hit is not None:
            return hit
        lock = self._locks.setdefault(key, asyncio.Lock())
        try:
            async with lock:
                hit = await self.get_json(key)
                if hit is not None:
                    return hit
                value = await compute()
                await self.set_json(key, value, ttl)
                return value
        finally:
            if not lock.locked():
                self._locks.pop(key, None)


def make_cache(redis_url: str, default_ttl: int) -> Cache:
    backend: CacheBackend = RedisBackend(redis_url) if redis_url else MemoryBackend(ttl=default_ttl)
    return Cache(backend, default_ttl)
