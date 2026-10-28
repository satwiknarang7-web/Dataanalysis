"""Alembic, run with the app's own engine settings (TLS and PgBouncer rules for Neon)."""

from __future__ import annotations

import asyncio

from alembic import context
from sqlalchemy.engine import Connection

from insight.config import get_settings
from insight.db.models import Base
from insight.db.session import make_engine

target_metadata = Base.metadata


def run_offline() -> None:
    context.configure(url=get_settings().async_database_url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def _migrate(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_online() -> None:
    engine = make_engine(get_settings())
    async with engine.connect() as connection:
        await connection.run_sync(_migrate)
    await engine.dispose()


if context.is_offline_mode():
    run_offline()
else:
    asyncio.run(run_online())
