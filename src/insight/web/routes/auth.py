"""Sign up, sign in, sign out."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from insight.services import auth
from insight.services.auth import AuthError, CurrentUser
from insight.state import AppState
from insight.web.deps import SESSION_COOKIE, client_ip, get_db, get_state, optional_user, templates

router = APIRouter()


def _safe_next(target: str | None) -> str:
    """Only a path on this site: never an open redirect to another one."""
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return "/home"


def _signed_in(state: AppState, token: str, target: str) -> Response:
    response = RedirectResponse(_safe_next(target), status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=state.settings.session_days * 86400,
        httponly=True,
        secure=state.settings.is_production,
        samesite="lax",
        path="/",
    )
    return response


@router.get("/signin", response_class=HTMLResponse)
async def signin_page(request: Request, next: str = "/home", user: CurrentUser | None = Depends(optional_user)):
    if user:
        return RedirectResponse(_safe_next(next), status_code=303)
    return templates.TemplateResponse(request, "pages/signin.html", {"next": _safe_next(next), "error": None, "email": ""})


@router.post("/signin", response_class=HTMLResponse)
async def signin(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
    next: str = Form("/home"),
    state: AppState = Depends(get_state),
    db: AsyncSession = Depends(get_db),
):
    try:
        user = await auth.authenticate(db, state.cache, email, password, client_ip(request))
    except AuthError as e:
        ctx = {"next": _safe_next(next), "error": str(e), "email": email}
        return templates.TemplateResponse(request, "pages/signin.html", ctx, status_code=400)
    return _signed_in(state, await auth.start_session(db, user, state.settings.session_days), next)


@router.get("/signup", response_class=HTMLResponse)
async def signup_page(request: Request, next: str = "/home", user: CurrentUser | None = Depends(optional_user)):
    if user:
        return RedirectResponse("/home", status_code=303)
    return templates.TemplateResponse(request, "pages/signup.html", {"next": _safe_next(next), "error": None, "email": "", "name": ""})


@router.post("/signup", response_class=HTMLResponse)
async def signup(
    request: Request,
    name: str = Form(""),
    email: str = Form(""),
    password: str = Form(""),
    next: str = Form("/home"),
    state: AppState = Depends(get_state),
    db: AsyncSession = Depends(get_db),
):
    try:
        user = await auth.sign_up(db, email, password, name)
    except AuthError as e:
        ctx = {"next": _safe_next(next), "error": str(e), "email": email, "name": name}
        return templates.TemplateResponse(request, "pages/signup.html", ctx, status_code=400)
    return _signed_in(state, await auth.start_session(db, user, state.settings.session_days), next)


@router.post("/signout")
async def signout(request: Request, state: AppState = Depends(get_state), db: AsyncSession = Depends(get_db)):
    await auth.end_session(db, state.cache, request.cookies.get(SESSION_COOKIE))
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response
