"""Cache, link fetching, readers and settings."""

import asyncio

import pytest

from insight.cache.backend import Cache, MemoryBackend
from insight.config import Settings
from insight.ingest.links import LinkError, fetch, rewrite
from insight.ingest.readers import UnreadableFile, read_table


async def test_cache_computes_once_under_concurrency():
    cache = Cache(MemoryBackend(), default_ttl=60)
    calls = 0

    async def compute():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return {"v": 1}

    results = await asyncio.gather(*(cache.get_or_compute("k", compute) for _ in range(10)))
    assert calls == 1 and all(r == {"v": 1} for r in results)


async def test_cache_counter_and_expiry():
    backend = MemoryBackend()
    assert [await backend.incr("n", 60) for _ in range(3)] == [1, 2, 3]
    await backend.set("gone", b"x", ttl=-1)
    assert await backend.get("gone") is None


def test_links_are_rewritten_to_their_data():
    assert rewrite("https://docs.google.com/spreadsheets/d/abc123/edit#gid=42") == (
        "https://docs.google.com/spreadsheets/d/abc123/export?format=csv&gid=42"
    )
    assert rewrite("https://github.com/o/r/blob/main/data.csv") == "https://raw.githubusercontent.com/o/r/main/data.csv"


@pytest.mark.parametrize(
    "url",
    ["http://127.0.0.1/x.csv", "http://localhost/x.csv", "http://10.0.0.5/x.csv", "file:///etc/passwd", "http://169.254.169.254/latest"],
)
async def test_private_and_internal_links_are_refused(url):
    with pytest.raises(LinkError):
        await fetch(url)


def test_readers():
    assert read_table(b"Sales report\nExported today\na,b\n1,2\n3,4\n", "x.csv").columns.tolist() == ["a", "b"]
    assert read_table(b"a;b\n1;2\n", "x.csv").shape == (1, 2)
    assert read_table(b'[{"a": 1, "b": {"c": 2}}]', "x.json").columns.tolist() == ["a", "b.c"]
    with pytest.raises(UnreadableFile, match="PDF"):
        read_table(b"%PDF", "report.pdf")
    with pytest.raises(UnreadableFile):
        read_table(b"", "x.csv")


def test_neon_urls_are_made_async_and_pooled():
    s = Settings(database_url="postgresql://u:p@ep-cool-1-pooler.eu-central-1.aws.neon.tech/db?sslmode=require&channel_binding=require")
    assert s.async_database_url == "postgresql+asyncpg://u:p@ep-cool-1-pooler.eu-central-1.aws.neon.tech/db"
    assert s.is_postgres and s.uses_pgbouncer
    assert Settings(database_url="sqlite+aiosqlite:///./x.db").async_database_url == "sqlite+aiosqlite:///./x.db"
