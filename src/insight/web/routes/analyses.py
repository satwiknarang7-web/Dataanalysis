"""Saved analyses: open, update, share, delete."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from insight.engine.models import Filter
from insight.services import insights
from insight.services import library as library_service
from insight.services.auth import CurrentUser
from insight.services.plans import PlanLimit
from insight.state import AppState
from insight.web.deps import get_db, get_state, require_user, templates
from insight.web.routes.datasets import dashboard_page, dataset_source

router = APIRouter(prefix="/analyses")


@router.get("/{analysis_id}", response_class=HTMLResponse)
async def open_analysis(
    request: Request,
    analysis_id: UUID,
    state: AppState = Depends(get_state),
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_user),
):
    analysis, owner = await library_service.get(db, user, analysis_id)
    src, ws = await dataset_source(state, db, user, analysis.dataset_id)
    ws.analysis_id, ws.analysis_title, ws.owner = analysis.id, analysis.title, owner
    ws.shares = await library_service.shares(db, analysis.id) if owner else []
    saved = [Filter.model_validate(f) for f in analysis.filters]
    return await dashboard_page(request, state, src, ws, user, saved_filters=saved)


@router.post("/{analysis_id}/filters")
async def save_filters(
    request: Request,
    analysis_id: UUID,
    state: AppState = Depends(get_state),
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_user),
):
    analysis, _ = await library_service.get(db, user, analysis_id)
    src, _ = await dataset_source(state, db, user, analysis.dataset_id)
    form = await request.form()
    filters = insights.parse_filters([(k, str(v)) for k, v in form.multi_items()], insights.filter_options(src))
    await library_service.update_filters(db, user, analysis_id, filters)
    return templates.TemplateResponse(
        request, "partials/toast.html", {"message": "Saved. Everyone with access sees these filters.", "upgrade": False}
    )


@router.post("/{analysis_id}/share", response_class=HTMLResponse)
async def share(
    request: Request,
    analysis_id: UUID,
    email: str = Form(""),
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_user),
):
    error = None
    try:
        await library_service.share(db, user, analysis_id, email)
    except (PlanLimit, ValueError) as e:
        error = str(e)
    emails = await library_service.shares(db, analysis_id)
    return templates.TemplateResponse(request, "partials/share_list.html", {"emails": emails, "error": error, "analysis_id": analysis_id})


@router.post("/{analysis_id}/unshare", response_class=HTMLResponse)
async def unshare(
    request: Request,
    analysis_id: UUID,
    email: str = Form(""),
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_user),
):
    await library_service.unshare(db, user, analysis_id, email)
    emails = await library_service.shares(db, analysis_id)
    return templates.TemplateResponse(request, "partials/share_list.html", {"emails": emails, "error": None, "analysis_id": analysis_id})


@router.post("/{analysis_id}/delete")
async def delete(analysis_id: UUID, db: AsyncSession = Depends(get_db), user: CurrentUser = Depends(require_user)) -> Response:
    await library_service.delete(db, user, analysis_id)
    return RedirectResponse("/library", status_code=303)
