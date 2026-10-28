"""Settings, read once from the environment (or a .env file).

Every variable is described in .env.example.
"""

from __future__ import annotations

from functools import lru_cache
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore", populate_by_name=True)

    environment: str = Field("development", description="development | production | test")
    app_name: str = "Insight"
    base_url: str = "http://localhost:8000"
    contact_email: str = "sales@example.com"

    # Neon: paste the connection string from the Neon console (the pooled one, "-pooler" in the host).
    database_url: str = "sqlite+aiosqlite:///./insight.db"
    # Neon's direct (non-pooled) endpoint, for Alembic: DDL is best run outside PgBouncer. Defaults to DATABASE_URL.
    # DATABASE_URL_UNPOOLED is the name Vercel's Neon integration gives it.
    migration_database_url: str = Field("", validation_alias=AliasChoices("MIGRATION_DATABASE_URL", "DATABASE_URL_UNPOOLED"))
    db_pool_size: int = 5
    db_max_overflow: int = 10
    auto_create_tables: bool = Field(True, description="Create tables on start-up (dev). Use Alembic in production.")

    # Redis (Upstash, Redis Cloud, local…). Empty: an in-process cache is used instead.
    # KV_URL is the name Vercel's Upstash integration gives it.
    redis_url: str = Field("", validation_alias=AliasChoices("REDIS_URL", "KV_URL"))
    cache_ttl_seconds: int = 60 * 60 * 24
    frame_cache_items: int = 12

    max_concurrent_analyses: int = 4
    session_days: int = 30

    # Serverless (Vercel sets VERCEL=1): no local connection pool, and uploads capped at the platform's request limit.
    serverless: bool = Field(False, validation_alias=AliasChoices("SERVERLESS", "VERCEL"))
    # The largest request body the platform accepts. Vercel Functions take 4.5 MB, so 4 MB of file plus the form.
    platform_upload_limit_mb: float | None = None

    @property
    def secure_cookies(self) -> bool:
        """Cookies only over HTTPS: in production, and on serverless platforms, which serve every deployment over HTTPS."""
        return self.is_production or self.serverless

    @property
    def upload_cap_mb(self) -> float | None:
        if self.platform_upload_limit_mb:
            return self.platform_upload_limit_mb
        return 4.0 if self.serverless else None

    def upload_limit_mb(self, plan_limit_mb: float) -> float:
        """What a user can upload: their plan's limit, or the platform's if that is lower."""
        cap = self.upload_cap_mb
        return min(plan_limit_mb, cap) if cap else plan_limit_mb

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def async_database_url(self) -> str:
        """The URL in SQLAlchemy's async form, without libpq-only query options."""
        url = self.database_url
        for prefix in ("postgres://", "postgresql://"):
            if url.startswith(prefix):
                url = "postgresql+asyncpg://" + url[len(prefix) :]
        if not url.startswith("postgresql+asyncpg://"):
            return url
        parts = urlsplit(url)
        query = [(k, v) for k, v in parse_qsl(parts.query) if k not in {"sslmode", "channel_binding"}]
        return urlunsplit(parts._replace(query=urlencode(query)))

    @property
    def is_postgres(self) -> bool:
        return self.async_database_url.startswith("postgresql+asyncpg://")

    @property
    def is_local_database(self) -> bool:
        """A database on this machine or in docker compose: no TLS needed."""
        return any(h in self.database_url for h in ("@localhost", "@127.0.0.1", "@db:", "@postgres:"))

    @property
    def uses_pgbouncer(self) -> bool:
        """Neon's pooled endpoint runs PgBouncer in transaction mode."""
        return "-pooler" in self.database_url


@lru_cache
def get_settings() -> Settings:
    return Settings()
