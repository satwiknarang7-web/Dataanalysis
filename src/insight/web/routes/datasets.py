"""The pages about one table: Dashboard, Ask, Data and Cleaning report.

The same pages serve a user's dataset (/datasets/<id>/…) and the public demo
(/demo/<sample>/…): each route only resolves its rows into a `Source` and a
`Workspace`, and the shared page functions below do the rest.
"""

from __future__ import annotations

import io
from uuid import UUID

import pandas as pd
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from insight.engine.ask import AskError
from insight.engine.measures import find_measure
from insight.engine.sql_console import SqlError, run_sql
from insight.services import datasets as datasets_service
from insight.services import insights
from insight.services import library as library_service
from insight.services.auth import CurrentUser
from insight.services.datasets import SAMPLES, NotFound
from insight.services.insights import Source
from insight.services.plans import PlanLimit, plan_of
from insight.state import AppState
from insight.web import view
from insight.web.deps import get_db, get_state, optional_user, require_user, templates
from insight.web.view import Workspace

router = APIRouter()
PAGE_SIZE = 50


# ── Resolving the rows ──────────────────────────────────────────────────────


async def dataset_source(state: AppState, db: AsyncSession, user: CurrentUser, dataset_id: UUID) -> tuple[Source, Workspace]:
    ds = await datasets_service.get_dataset(db, user, dataset_id)
    df = await datasets_service.load_frame(state, db, ds)
    src = Source(
        name=ds.name, df=df, reading=datasets_service.reading_of(ds), report=datasets_service.report_of(ds), content_hash=ds.content_hash
    )
    ws = Workspace(
        name=ds.name, base=f"/datasets/{ds.id}", rows=ds.row_count, columns=ds.column_count, dataset_id=ds.id, owner=ds.owner_id == user.id
    )
    return src, ws


async def demo_source(state: AppState, key: str) -> tuple[Source, Workspace]:
    sample = SAMPLES.get(key)
    if sample is None:
        raise NotFound(key)
    df, report, reading, content_hash = await datasets_service.load_sample_frame(state, key)
    src = Source(name=sample.name, df=df, reading=reading, report=report, content_hash=content_hash)
    return src, Workspace(name=sample.name, base=f"/demo/{key}", rows=len(df), columns=df.shape[1], demo=True, owner=False)


# ── Shared page functions ──────────────────────────────────────────────────


async def dashboard_page(
    request: Request, state: AppState, src: Source, ws: Workspace, user: CurrentUser | None, saved_filters=None
) -> Response:
    options = insights.filter_options(src)
    params = list(request.query_params.multi_items())
    filters = (
        insights.parse_filters(params, options) if any(k.startswith("f.") for k, _ in params) or saved_filters is None else saved_filters
    )
    board = await insights.dashboard_for(state, src, filters)
    plan = await insights.plan_for(state, src)
    selected = {f"f.{f.field}" + (f"@{f.grain}" if f.grain else ""): set(f.values) for f in filters}
    ctx = {
        "user": user,
        "ws": ws,
        "board": board,
        "plan": plan,
        "options": options,
        "selected": selected,
        "measure_of": lambda tile: find_measure(plan.measures, tile.spec.measures[0].id) if tile.spec else None,
        "tab": "dashboard",
    }
    template = "partials/board.html" if request.headers.get("hx-target") == "board" else "pages/dashboard.html"
    return templates.TemplateResponse(request, template, ctx)


async def ask_page(request: Request, state: AppState, src: Source, ws: Workspace, user: CurrentUser | None) -> Response:
    examples = await insights.examples_for(state, src)
    can_sql = ws.demo or (user is not None and plan_of(user.plan).sql_console)
    columns = [f for f in src.reading.fields]
    return templates.TemplateResponse(
        request, "pages/ask.html", {"user": user, "ws": ws, "examples": examples, "can_sql": can_sql, "columns": columns, "tab": "ask"}
    )


async def ask_answer(request: Request, state: AppState, src: Source, question: str) -> Response:
    question = question.strip()[:300]
    try:
        tile = await insights.answer(state, src, question, [])
        ctx = {"question": question, "t": tile, "error": None}
    except AskError as e:
        ctx = {"question": question, "t": None, "error": str(e)}
    plan = await insights.plan_for(state, src)
    ctx["measure"] = find_measure(plan.measures, ctx["t"].tile.spec.measures[0].id) if ctx["t"] and ctx["t"].tile.spec else None
    return templates.TemplateResponse(request, "partials/answer.html", ctx)


async def sql_answer(request: Request, state: AppState, src: Source, ws: Workspace, user: CurrentUser | None, sql: str) -> Response:
    if not (ws.demo or (user and plan_of(user.plan).sql_console)):
        return templates.TemplateResponse(request, "partials/sql_result.html", {"error": "The SQL console is part of Pro.", "result": None})
    try:
        result = await state.run_cpu(run_sql, src.df, sql[:5000])
        ctx = {"result": result, "error": None}
    except SqlError as e:
        ctx = {"result": None, "error": str(e)}
    return templates.TemplateResponse(request, "partials/sql_result.html", ctx)


async def data_page(request: Request, src: Source, ws: Workspace, user: CurrentUser | None) -> Response:
    q = request.query_params.get("q", "").strip()[:100]
    page = max(1, int(request.query_params.get("page", "1") or 1))
    df = src.df
    if q:
        text_cols = [c for c in df.columns if not pd.api.types.is_numeric_dtype(df[c]) and not pd.api.types.is_datetime64_any_dtype(df[c])]
        mask = pd.Series(False, index=df.index)
        for c in text_cols:
            mask |= df[c].astype("string").str.contains(q, case=False, regex=False, na=False)
        df = df.loc[mask]
    pages = max(1, -(-len(df) // PAGE_SIZE))
    page = min(page, pages)
    chunk = df.iloc[(page - 1) * PAGE_SIZE : page * PAGE_SIZE]
    fields = {f.name: f for f in src.reading.fields}
    rows = [[_cell(v) for v in row] for row in chunk.itertuples(index=False, name=None)]
    ctx = {
        "user": user,
        "ws": ws,
        "columns": [fields[c] for c in df.columns],
        "rows": rows,
        "page": page,
        "pages": pages,
        "matches": len(df),
        "q": q,
        "tab": "data",
    }
    template = "partials/data_table.html" if request.headers.get("hx-target") == "data-table" else "pages/data.html"
    return templates.TemplateResponse(request, template, ctx)


async def quality_page(request: Request, src: Source, ws: Workspace, user: CurrentUser | None) -> Response:
    report = src.report
    fields = {f.name: f for f in src.reading.fields}
    ctx = {
        "user": user,
        "ws": ws,
        "report": report,
        "fields": fields,
        "checks": [c for c in report.checks if c.count],
        "quiet": [c.title for c in report.checks if not c.count],
        "roles": view.ROLE_LABELS,
        "kinds": view.KIND_LABELS,
        "tab": "quality",
    }
    return templates.TemplateResponse(request, "pages/quality.html", ctx)


def export_csv(src: Source) -> Response:
    buf = io.StringIO()
    src.df.to_csv(buf, index=False)
    name = "".join(ch if ch.isalnum() or ch in "-_ " else "_" for ch in src.name).strip() or "data"
    return StreamingResponse(
        iter([buf.getvalue()]), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{name} (cleaned).csv"'}
    )


def _cell(v) -> str:
    if v is None or (isinstance(v, float) and v != v) or v is pd.NA or v is pd.NaT:
        return ""
    if isinstance(v, pd.Timestamp):
        return v.strftime("%Y-%m-%d %H:%M") if (v.hour or v.minute) else v.strftime("%Y-%m-%d")
    if isinstance(v, float):
        return f"{v:,.4f}".rstrip("0").rstrip(".")
    return str(v)


# ── A user's datasets ───────────────────────────────────────────────────────


@router.get("/datasets/{dataset_id}")
async def dataset_root(dataset_id: UUID) -> RedirectResponse:
    return RedirectResponse(f"/datasets/{dataset_id}/dashboard", status_code=303)


@router.get("/datasets/{dataset_id}/dashboard", response_class=HTMLResponse)
async def dataset_dashboard(
    request: Request,
    dataset_id: UUID,
    state: AppState = Depends(get_state),
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_user),
):
    src, ws = await dataset_source(state, db, user, dataset_id)
    return await dashboard_page(request, state, src, ws, user)


@router.get("/datasets/{dataset_id}/ask", response_class=HTMLResponse)
async def dataset_ask(
    request: Request,
    dataset_id: UUID,
    state: AppState = Depends(get_state),
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_user),
):
    src, ws = await dataset_source(state, db, user, dataset_id)
    return await ask_page(request, state, src, ws, user)


@router.post("/datasets/{dataset_id}/ask", response_class=HTMLResponse)
async def dataset_ask_answer(
    request: Request,
    dataset_id: UUID,
    question: str = Form(""),
    state: AppState = Depends(get_state),
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_user),
):
    src, _ = await dataset_source(state, db, user, dataset_id)
    return await ask_answer(request, state, src, question)


@router.post("/datasets/{dataset_id}/sql", response_class=HTMLResponse)
async def dataset_sql(
    request: Request,
    dataset_id: UUID,
    sql: str = Form(""),
    state: AppState = Depends(get_state),
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_user),
):
    src, ws = await dataset_source(state, db, user, dataset_id)
    return await sql_answer(request, state, src, ws, user, sql)


@router.get("/datasets/{dataset_id}/data", response_class=HTMLResponse)
async def dataset_data(
    request: Request,
    dataset_id: UUID,
    state: AppState = Depends(get_state),
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_user),
):
    src, ws = await dataset_source(state, db, user, dataset_id)
    return await data_page(request, src, ws, user)


@router.get("/datasets/{dataset_id}/quality", response_class=HTMLResponse)
async def dataset_quality(
    request: Request,
    dataset_id: UUID,
    state: AppState = Depends(get_state),
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_user),
):
    src, ws = await dataset_source(state, db, user, dataset_id)
    return await quality_page(request, src, ws, user)


@router.get("/datasets/{dataset_id}/export.csv")
async def dataset_export(
    dataset_id: UUID, state: AppState = Depends(get_state), db: AsyncSession = Depends(get_db), user: CurrentUser = Depends(require_user)
):
    src, _ = await dataset_source(state, db, user, dataset_id)
    return export_csv(src)


@router.post("/datasets/{dataset_id}/delete")
async def dataset_delete(dataset_id: UUID, db: AsyncSession = Depends(get_db), user: CurrentUser = Depends(require_user)):
    await datasets_service.delete_dataset(db, user, dataset_id)
    return RedirectResponse("/home", status_code=303)


@router.post("/datasets/{dataset_id}/save")
async def dataset_save(
    request: Request,
    dataset_id: UUID,
    title: str = Form(""),
    state: AppState = Depends(get_state),
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(require_user),
):
    src, _ = await dataset_source(state, db, user, dataset_id)
    form = await request.form()
    filters = insights.parse_filters([(k, str(v)) for k, v in form.multi_items()], insights.filter_options(src))
    ds = await datasets_service.get_dataset(db, user, dataset_id)
    try:
        analysis = await library_service.save(db, user, ds, title, filters)
    except PlanLimit as e:
        return templates.TemplateResponse(request, "partials/toast.html", {"message": str(e), "upgrade": True}, status_code=402)
    return Response(status_code=204, headers={"HX-Redirect": f"/analyses/{analysis.id}"})


# ── The public demo ────────────────────────────────────────────────────────


@router.get("/demo/{key}")
async def demo_root(key: str) -> RedirectResponse:
    return RedirectResponse(f"/demo/{key}/dashboard", status_code=303)


@router.get("/demo/{key}/dashboard", response_class=HTMLResponse)
async def demo_dashboard(
    request: Request, key: str, state: AppState = Depends(get_state), user: CurrentUser | None = Depends(optional_user)
):
    src, ws = await demo_source(state, key)
    return await dashboard_page(request, state, src, ws, user)


@router.get("/demo/{key}/ask", response_class=HTMLResponse)
async def demo_ask(request: Request, key: str, state: AppState = Depends(get_state), user: CurrentUser | None = Depends(optional_user)):
    src, ws = await demo_source(state, key)
    return await ask_page(request, state, src, ws, user)


@router.post("/demo/{key}/ask", response_class=HTMLResponse)
async def demo_ask_answer(request: Request, key: str, question: str = Form(""), state: AppState = Depends(get_state)):
    src, _ = await demo_source(state, key)
    return await ask_answer(request, state, src, question)


@router.post("/demo/{key}/sql", response_class=HTMLResponse)
async def demo_sql(
    request: Request, key: str, sql: str = Form(""), state: AppState = Depends(get_state), user: CurrentUser | None = Depends(optional_user)
):
    src, ws = await demo_source(state, key)
    return await sql_answer(request, state, src, ws, user, sql)


@router.get("/demo/{key}/data", response_class=HTMLResponse)
async def demo_data(request: Request, key: str, state: AppState = Depends(get_state), user: CurrentUser | None = Depends(optional_user)):
    src, ws = await demo_source(state, key)
    return await data_page(request, src, ws, user)


@router.get("/demo/{key}/quality", response_class=HTMLResponse)
async def demo_quality(request: Request, key: str, state: AppState = Depends(get_state), user: CurrentUser | None = Depends(optional_user)):
    src, ws = await demo_source(state, key)
    return await quality_page(request, src, ws, user)


@router.get("/demo/{key}/export.csv")
async def demo_export(key: str, state: AppState = Depends(get_state)):
    src, _ = await demo_source(state, key)
    return export_csv(src)
