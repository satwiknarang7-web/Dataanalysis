"""Get data (upload, link, samples) and the Library."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from insight.ingest.links import LinkError, fetch
from insight.ingest.readers import UnreadableFile
from insight.services import datasets as datasets_service
from insight.services import library as library_service
from insight.services.auth import CurrentUser
from insight.services.datasets import SAMPLES
from insight.services.plans import PlanLimit, plan_of
from insight.state import AppState
from insight.web.deps import get_db, get_state, require_user, templates

router = APIRouter()
READ_CHUNK = 1024 * 1024


async def _home(request: Request, db: AsyncSession, user: CurrentUser, error: str | None = None, status: int = 200) -> Response:
    recent = await datasets_service.list_datasets(db, user)
    return templates.TemplateResponse(
        request,
        "pages/home.html",
        {"user": user, "recent": recent, "samples": SAMPLES.values(), "error": error, "plan": plan_of(user.plan), "tab": "home"},
        status_code=status,
    )


def _wants_json(request: Request) -> bool:
    return request.headers.get("x-requested-with") == "fetch"


def _done(request: Request, url: str) -> Response:
    return JSONResponse({"redirect": url}) if _wants_json(request) else RedirectResponse(url, status_code=303)


async def _failed(request: Request, db: AsyncSession, user: CurrentUser, message: str) -> Response:
    if _wants_json(request):
        return JSONResponse({"error": message}, status_code=400)
    return await _home(request, db, user, message, status=400)


@router.get("/home", response_class=HTMLResponse)
async def home(request: Request, db: AsyncSession = Depends(get_db), user: CurrentUser = Depends(require_user)):
    return await _home(request, db, user)


@router.post("/upload")
async def upload(
    request: Request,
    file: UploadFile,
    state: AppState = Depends(get_state),
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_user),
):
    limit = plan_of(user.plan).max_upload_mb * 1024 * 1024
    content = bytearray()
    while chunk := await file.read(READ_CHUNK):
        content.extend(chunk)
        if len(content) > limit:
            return await _failed(request, db, user, f"That file is over {plan_of(user.plan).max_upload_mb} MB, the limit on your plan.")
    try:
        ds = await datasets_service.ingest(state, db, user, bytes(content), file.filename or "upload.csv", "file", file.filename or "")
    except (UnreadableFile, PlanLimit) as e:
        return await _failed(request, db, user, str(e))
    return _done(request, f"/datasets/{ds.id}/dashboard")


@router.post("/link")
async def from_link(
    request: Request,
    url: str = Form(""),
    state: AppState = Depends(get_state),
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_user),
):
    try:
        content, filename = await fetch(url)
        ds = await datasets_service.ingest(state, db, user, content, filename, "link", url)
    except (LinkError, UnreadableFile, PlanLimit) as e:
        return await _failed(request, db, user, str(e))
    return _done(request, f"/datasets/{ds.id}/dashboard")


@router.post("/samples/{key}")
async def from_sample(
    request: Request,
    key: str,
    state: AppState = Depends(get_state),
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_user),
):
    try:
        ds = await datasets_service.ingest_sample(state, db, user, key)
    except PlanLimit as e:
        return await _failed(request, db, user, str(e))
    return _done(request, f"/datasets/{ds.id}/dashboard")


@router.get("/library", response_class=HTMLResponse)
async def library(request: Request, db: AsyncSession = Depends(get_db), user: CurrentUser = Depends(require_user)):
    mine = await library_service.mine(db, user)
    shared = await library_service.shared_with_me(db, user)
    return templates.TemplateResponse(
        request, "pages/library.html", {"user": user, "mine": mine, "shared": shared, "plan": plan_of(user.plan), "tab": "library"}
    )
