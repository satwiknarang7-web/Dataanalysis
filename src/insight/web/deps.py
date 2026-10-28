"""Request-scoped dependencies: the app state, a database session, the user."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from insight.services import auth
from insight.services.auth import CurrentUser
from insight.services.plans import plan_of
from insight.state import AppState
from insight.web import view

SESSION_COOKIE = "insight_session"

STATIC_DIR = Path(__file__).parent / "static"
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.globals.update(plan_of=plan_of, view=view, asset_v=view.asset_version(STATIC_DIR))
templates.env.filters.update(view.FILTERS)


class SignInRequired(Exception):
    def __init__(self, next_url: str) -> None:
        self.next_url = next_url


def get_state(request: Request) -> AppState:
    return request.app.state.insight


async def get_db(state: AppState = Depends(get_state)) -> AsyncIterator[AsyncSession]:
    async with state.sessions() as session:
        yield session


async def optional_user(request: Request, state: AppState = Depends(get_state), db: AsyncSession = Depends(get_db)) -> CurrentUser | None:
    user = await auth.user_for_token(db, state.cache, request.cookies.get(SESSION_COOKIE))
    request.state.user = user
    return user


async def require_user(request: Request, user: CurrentUser | None = Depends(optional_user)) -> CurrentUser:
    if user is None:
        target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        raise SignInRequired(quote(target, safe="/?=&"))
    return user


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    return forwarded.split(",")[0].strip() or (request.client.host if request.client else "unknown")
