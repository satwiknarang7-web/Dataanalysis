"""Everything a request needs that lives for the whole process."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from typing import TypeVar

import anyio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from insight.cache.backend import Cache
from insight.cache.frames import FrameCache
from insight.config import Settings

T = TypeVar("T")


@dataclass
class AppState:
    settings: Settings
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]
    cache: Cache
    frames: FrameCache
    compute_slots: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(4))

    async def run_cpu(self, fn: Callable[..., T], *args, **kwargs) -> T:
        """Run engine work on a worker thread, a bounded number at a time.

        pandas and numpy release the GIL for most of their work, so threads
        give real parallelism here; the semaphore stops a burst of uploads
        from starving the event loop's own threads.
        """
        async with self.compute_slots:
            return await anyio.to_thread.run_sync(partial(fn, *args, **kwargs))
