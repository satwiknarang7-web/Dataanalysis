"""Accounts and sign-in sessions.

Passwords are hashed with Argon2id. A session is a random token in an
HttpOnly cookie; only its SHA-256 is stored, so a leaked database cannot be
used to sign in. Looking a session up is cached briefly, since every page
needs it. Sign-in attempts are rate limited per email and address.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from insight.cache import keys
from insight.cache.backend import Cache
from insight.db.models import AuthSession, User

_hasher = PasswordHasher()
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAX_ATTEMPTS = 8
ATTEMPT_WINDOW = 15 * 60
SESSION_CACHE_TTL = 120


class AuthError(ValueError):
    pass


@dataclass(frozen=True)
class CurrentUser:
    id: UUID
    email: str
    name: str
    plan: str

    @property
    def display_name(self) -> str:
        return self.name or self.email.split("@")[0]

    @property
    def initials(self) -> str:
        parts = self.display_name.replace(".", " ").split()
        return "".join(p[0] for p in parts[:2]).upper() or "?"


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def normalise_email(email: str) -> str:
    return email.strip().lower()


def _current(user: User) -> CurrentUser:
    return CurrentUser(id=user.id, email=user.email, name=user.name, plan=user.plan)


async def sign_up(db: AsyncSession, email: str, password: str, name: str = "") -> User:
    email = normalise_email(email)
    if not EMAIL.match(email):
        raise AuthError("Enter a valid email address.")
    if len(password) < 10:
        raise AuthError("Use at least 10 characters for your password.")
    user = User(email=email, name=name.strip()[:120], password_hash=_hasher.hash(password))
    db.add(user)
    try:
        await db.commit()
    except IntegrityError as e:
        await db.rollback()
        raise AuthError("An account with this email already exists. Sign in instead.") from e
    return user


async def authenticate(db: AsyncSession, cache: Cache, email: str, password: str, ip: str) -> User:
    email = normalise_email(email)
    attempts = await cache.incr(keys.sign_in_attempts(email, ip), ATTEMPT_WINDOW)
    if attempts > MAX_ATTEMPTS:
        raise AuthError("Too many attempts. Wait a few minutes and try again.")
    user = await db.scalar(select(User).where(User.email == email))
    try:
        if user is None:
            _hasher.hash(password)  # same work either way, so timing does not reveal accounts
            raise VerifyMismatchError
        _hasher.verify(user.password_hash, password)
    except (VerifyMismatchError, InvalidHashError) as e:
        raise AuthError("That email and password don't match.") from e
    await cache.delete(keys.sign_in_attempts(email, ip))
    if _hasher.check_needs_rehash(user.password_hash):
        user.password_hash = _hasher.hash(password)
        await db.commit()
    return user


async def start_session(db: AsyncSession, user: User, days: int) -> str:
    token = secrets.token_urlsafe(32)
    db.add(AuthSession(token_hash=_token_hash(token), user_id=user.id, expires_at=datetime.now(UTC) + timedelta(days=days)))
    await db.commit()
    return token


async def user_for_token(db: AsyncSession, cache: Cache, token: str | None) -> CurrentUser | None:
    if not token:
        return None
    th = _token_hash(token)
    cached = await cache.get_json(keys.session(th))
    if cached:
        return CurrentUser(id=UUID(cached["id"]), email=cached["email"], name=cached["name"], plan=cached["plan"])
    row = await db.scalar(select(AuthSession).where(AuthSession.token_hash == th))
    if row is None or _aware(row.expires_at) < datetime.now(UTC):
        return None
    user = _current(row.user)
    await cache.set_json(
        keys.session(th), {"id": str(user.id), "email": user.email, "name": user.name, "plan": user.plan}, SESSION_CACHE_TTL
    )
    return user


async def end_session(db: AsyncSession, cache: Cache, token: str | None) -> None:
    if not token:
        return
    th = _token_hash(token)
    await db.execute(delete(AuthSession).where(AuthSession.token_hash == th))
    await db.commit()
    await cache.delete(keys.session(th))


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
