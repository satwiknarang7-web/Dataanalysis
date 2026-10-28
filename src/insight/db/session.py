"""The connection to Postgres (Neon in production).

Neon specifics handled here:
- TLS is required, so asyncpg is told to use it.
- The pooled endpoint is PgBouncer in transaction mode, which cannot keep
  prepared statements between transactions: asyncpg's statement cache is off.
- Neon suspends idle computes and drops their connections, so every
  connection is pinged on checkout and recycled after five minutes.
- On serverless platforms (Vercel) there is no local pool at all: each
  request takes a connection from Neon's pooler and gives it back.
"""

from __future__ import annotations

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from insight.config import Settings
from insight.db.models import Base


def make_engine(settings: Settings) -> AsyncEngine:
    if not settings.is_postgres:
        engine = create_async_engine(settings.async_database_url)
        event.listen(engine.sync_engine, "connect", _sqlite_foreign_keys)
        return engine
    url = settings.async_database_url
    connect_args: dict = {} if settings.is_local_database else {"ssl": "require"}
    if settings.uses_pgbouncer:
        connect_args["statement_cache_size"] = 0  # asyncpg's own cache
        url += ("&" if "?" in url else "?") + "prepared_statement_cache_size=0"  # SQLAlchemy's
    if settings.serverless:
        # Instances are short-lived and many: Neon's PgBouncer does the pooling, so hold nothing open here.
        return create_async_engine(url, poolclass=NullPool, connect_args=connect_args)
    return create_async_engine(
        url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=True,
        pool_recycle=300,
        connect_args=connect_args,
    )


def _sqlite_foreign_keys(dbapi_connection, _record) -> None:
    """SQLite (dev and tests) enforces foreign keys only when asked; Postgres always does."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def create_tables(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
