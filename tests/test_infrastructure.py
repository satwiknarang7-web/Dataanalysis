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


def test_parquet_round_trip_keeps_types():
    import pandas as pd

    from insight.engine.cleaning import clean
    from insight.ingest.parquet import read_parquet, to_parquet
    from tests.conftest import frame

    df, _ = clean(
        frame({"d": ["2024-01-05", None, "2024-03-01"], "year": ["2021", "2022", None], "x": ["1.5", "2", "3"], "t": ["a", None, "c"]})
    )
    back = read_parquet(to_parquet(df))
    assert str(back["year"].dtype) == "Int64" and back["year"].tolist()[:2] == [2021, 2022] and pd.isna(back["year"][2])
    assert back["d"].dt.strftime("%Y-%m-%d").tolist()[0] == "2024-01-05" and pd.isna(back["d"][1])
    assert back["x"].tolist() == [1.5, 2.0, 3.0]
    assert back["t"].tolist() == ["a", None, "c"]


def test_vercel_integration_variable_names(monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("KV_URL", "rediss://default:x@cache.upstash.io:6379")
    monkeypatch.setenv("DATABASE_URL_UNPOOLED", "postgresql://u:p@ep-1.neon.tech/db?sslmode=require")
    s = Settings(_env_file=None)
    assert s.serverless and s.redis_url.startswith("rediss://") and s.migration_database_url.startswith("postgresql://")
    assert s.upload_limit_mb(100) == 4 and s.upload_limit_mb(2) == 2
    assert Settings(_env_file=None, serverless=False).upload_limit_mb(100) == 100


def test_serverless_engine_holds_no_pool():
    from sqlalchemy.pool import NullPool

    from insight.db.session import make_engine

    s = Settings(_env_file=None, serverless=True, database_url="postgresql://u:p@ep-1-pooler.neon.tech/db?sslmode=require")
    engine = make_engine(s)
    assert isinstance(engine.pool, NullPool)
    assert "prepared_statement_cache_size=0" in str(engine.url)


async def test_uploads_over_the_platform_cap_are_refused(client):
    from tests.conftest import sign_up

    await sign_up(client)
    client.app.state.insight.settings.platform_upload_limit_mb = 0.001
    r = await client.post(
        "/upload", files={"file": ("big.csv", b"a,b\n" + b"1,2\n" * 1000, "text/csv")}, headers={"x-requested-with": "fetch"}
    )
    assert r.status_code == 400 and "most that can be uploaded" in r.json()["error"]
