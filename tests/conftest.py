from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pandas as pd
import pytest

from insight.config import Settings
from insight.db.models import Base
from insight.engine.cleaning import clean
from insight.engine.fields import read_dataset
from insight.web.app import create_app

SAMPLES = Path(__file__).resolve().parents[1] / "src" / "insight" / "samples"


def frame(rows: dict) -> pd.DataFrame:
    """A table as a reader would hand it to cleaning: every cell a string."""
    return pd.DataFrame({k: [None if v is None else str(v) for v in vs] for k, vs in rows.items()})


def cleaned(name: str):
    raw = pd.read_csv(SAMPLES / name, dtype=str, keep_default_na=False)
    df, report = clean(raw)
    return df, report, read_dataset(df, report.percent_columns())


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """SQLite and the memory cache by default; set TEST_DATABASE_URL / TEST_REDIS_URL to run on Postgres and Redis."""
    return Settings(
        environment="test",
        database_url=os.environ.get("TEST_DATABASE_URL") or f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
        redis_url=os.environ.get("TEST_REDIS_URL", ""),
    )


@pytest.fixture
async def client(settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        state = app.state.insight
        async with state.engine.begin() as conn:  # every test starts from empty tables
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        if settings.redis_url:
            await state.cache.backend._redis.flushdb()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver", follow_redirects=False) as c:
            c.app = app  # type: ignore[attr-defined]
            yield c


async def sign_up(client: httpx.AsyncClient, email: str = "ana@example.com", password: str = "correct horse battery") -> None:
    r = await client.post("/signup", data={"name": "Ana", "email": email, "password": password, "next": "/home"})
    assert r.status_code == 303, r.text
