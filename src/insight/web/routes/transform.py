"""Transform data: shape the rows with steps, previewed on the real data before they are kept.

Served for a user's dataset (/datasets/<id>/transform) and the public demo
(/demo/<sample>/transform), where steps can be previewed but not kept.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from insight.db.models import Dataset
from insight.engine.formulas import FUNCTION_HELP, FormulaError
from insight.engine.suggest import suggest
from insight.engine.transform_language import PhraseError, example_phrases, parse_phrase
from insight.engine.transforms import StepError, apply_steps, parse_step
from insight.services import datasets as datasets_service
from insight.services import shaping
from insight.services.auth import CurrentUser
from insight.services.insights import Source
from insight.state import AppState
from insight.web import step_forms
from insight.web.deps import SignInRequired, get_db, get_state, optional_user, templates
from insight.web.routes.datasets import dataset_source, demo_source
from insight.web.view import Workspace

router = APIRouter()
Scope = Literal["datasets", "demo"]
STEP_ERRORS = (StepError, FormulaError, PhraseError, ValidationError, ValueError)


@dataclass
class Shaping:
    src: Source
    ws: Workspace
    steps: list[dict]
    dataset: Dataset | None
    can_save: bool


async def _context(scope: str, ref: str, request: Request, state: AppState, db: AsyncSession, user: CurrentUser | None) -> Shaping:
    if scope == "demo":
        src, ws = await demo_source(state, ref)
        return Shaping(src, ws, [], None, False)
    if user is None:
        raise SignInRequired(request.url.path)
    try:
        dataset_id = UUID(ref)
    except ValueError as e:
        raise datasets_service.NotFound(ref) from e
    src, ws = await dataset_source(state, db, user, dataset_id)
    dataset = await datasets_service.get_dataset(db, user, dataset_id)
    return Shaping(src, ws, list(dataset.steps or []), dataset, dataset.owner_id == user.id)


def _error(e: Exception) -> str:
    if isinstance(e, ValidationError):
        first = e.errors()[0]
        where = ".".join(str(p) for p in first.get("loc", ()) if not isinstance(p, int))
        return f"That step is missing something{f' ({where})' if where else ''}."
    return str(e)


@router.get("/{scope}/{ref}/transform", response_class=HTMLResponse)
async def transform_page(
    request: Request,
    scope: Scope,
    ref: str,
    state: AppState = Depends(get_state),
    db: AsyncSession = Depends(get_db),
    user: CurrentUser | None = Depends(optional_user),
):
    ctx = await _context(scope, ref, request, state, db, user)
    base = await shaping.base_frame(state, db, ctx.dataset) if ctx.dataset else ctx.src.df
    _, results = await state.run_cpu(apply_steps, base, [parse_step(s) for s in ctx.steps])
    preview = await state.run_cpu(shaping.run_preview, base, ctx.steps, None)
    suggestions = await state.run_cpu(suggest, ctx.src.df, ctx.src.reading, ctx.steps)
    return templates.TemplateResponse(
        request,
        "pages/transform.html",
        {
            "user": user,
            "ws": ctx.ws,
            "tab": "transform",
            "steps": list(zip(ctx.steps, results, strict=True)),
            "can_save": ctx.can_save,
            "suggestions": suggestions,
            "examples": example_phrases(ctx.src.reading),
            "preview": preview,
            "pending": None,
            "primary": step_forms.PRIMARY,
            "more": step_forms.MORE,
            "labels": step_forms.KIND_LABELS,
            "base_rows": len(base),
        },
    )


@router.get("/{scope}/{ref}/transform/form", response_class=HTMLResponse)
async def step_form(
    request: Request,
    scope: Scope,
    ref: str,
    kind: str,
    state: AppState = Depends(get_state),
    db: AsyncSession = Depends(get_db),
    user: CurrentUser | None = Depends(optional_user),
):
    ctx = await _context(scope, ref, request, state, db, user)
    fields = ctx.src.reading.fields
    return templates.TemplateResponse(
        request,
        "partials/step_form.html",
        {
            "ws": ctx.ws,
            "kind": kind if kind in step_forms.KIND_LABELS else "filter",
            "labels": step_forms.KIND_LABELS,
            "fields": fields,
            "columns": [f.name for f in fields],
            "numbers": [f.name for f in fields if f.kind == "number"],
            "dates": [f.name for f in fields if f.kind == "date"],
            "texts": [f.name for f in fields if f.kind in ("text", "boolean")],
            "date_parts": step_forms.DATE_PARTS,
            "group_fns": step_forms.GROUP_FNS,
            "max_rules": step_forms.MAX_RULES,
            "max_aggregates": step_forms.MAX_AGGREGATES,
            "function_help": FUNCTION_HELP,
        },
    )


@router.post("/{scope}/{ref}/transform/preview", response_class=HTMLResponse)
async def preview_step(
    request: Request,
    scope: Scope,
    ref: str,
    state: AppState = Depends(get_state),
    db: AsyncSession = Depends(get_db),
    user: CurrentUser | None = Depends(optional_user),
):
    ctx = await _context(scope, ref, request, state, db, user)
    form = await request.form()
    columns = list(ctx.src.df.columns)
    try:
        if form.get("phrase"):
            step = parse_phrase(str(form["phrase"])[:300], columns).model_dump(mode="json")
        elif form.get("step"):
            step = parse_step(json.loads(str(form["step"]))).model_dump(mode="json")
        else:
            step = parse_step(step_forms.build(str(form.get("kind", "")), form, columns)).model_dump(mode="json")
        base = await shaping.base_frame(state, db, ctx.dataset) if ctx.dataset else ctx.src.df
        preview = await state.run_cpu(shaping.run_preview, base, ctx.steps, step)
    except STEP_ERRORS as e:
        return templates.TemplateResponse(request, "partials/step_preview.html", {"error": _error(e), "ws": ctx.ws, "preview": None})
    return templates.TemplateResponse(
        request,
        "partials/step_preview.html",
        {
            "ws": ctx.ws,
            "preview": preview,
            "pending": step,
            "pending_json": json.dumps(step),
            "sentence": parse_step(step).describe(),
            "can_save": ctx.can_save,
            "error": preview.error,
        },
    )


@router.post("/{scope}/{ref}/transform/steps")
async def add_step(
    request: Request,
    scope: Scope,
    ref: str,
    state: AppState = Depends(get_state),
    db: AsyncSession = Depends(get_db),
    user: CurrentUser | None = Depends(optional_user),
):
    ctx = await _context(scope, ref, request, state, db, user)
    if not ctx.can_save or ctx.dataset is None or user is None:
        return _toast(request, "Create a free account to keep steps on your own data.", 403)
    form = await request.form()
    try:
        step = parse_step(json.loads(str(form.get("step", "{}")))).model_dump(mode="json")
        await shaping.save_steps(state, db, user, ctx.dataset, [*ctx.steps, step])
    except STEP_ERRORS as e:
        return _toast(request, _error(e), 400)
    return Response(status_code=204, headers={"HX-Redirect": f"{ctx.ws.base}/transform"})


@router.post("/{scope}/{ref}/transform/steps/{index}/{action}")
async def edit_step(
    request: Request,
    scope: Scope,
    ref: str,
    index: int,
    action: Literal["toggle", "delete", "up", "down"],
    state: AppState = Depends(get_state),
    db: AsyncSession = Depends(get_db),
    user: CurrentUser | None = Depends(optional_user),
):
    ctx = await _context(scope, ref, request, state, db, user)
    if not ctx.can_save or ctx.dataset is None or user is None:
        return _toast(request, "Create a free account to keep steps on your own data.", 403)
    try:
        await shaping.save_steps(state, db, user, ctx.dataset, shaping.edited(ctx.steps, action, index))
    except STEP_ERRORS as e:
        return _toast(request, _error(e), 400)
    return Response(status_code=204, headers={"HX-Redirect": f"{ctx.ws.base}/transform"})


def _toast(request: Request, message: str, status: int) -> Response:
    return templates.TemplateResponse(request, "partials/toast.html", {"message": message, "upgrade": False}, status_code=status)
