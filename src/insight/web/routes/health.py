"""Liveness and readiness, for the load balancer and the platform."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text

from insight.state import AppState
from insight.web.deps import get_state

router = APIRouter()


@router.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(state: AppState = Depends(get_state)) -> JSONResponse:
    checks = {"database": "ok", "cache": "ok"}
    try:
        async with state.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as e:  # report, don't raise: this endpoint's job is to say what is down
        checks["database"] = f"error: {type(e).__name__}"
    await state.cache.set_json("insight:ready", 1, ttl=10)
    if await state.cache.get_json("insight:ready") != 1:
        checks["cache"] = "degraded"
    ok = checks["database"] == "ok"
    return JSONResponse({"status": "ok" if ok else "unavailable", **checks}, status_code=200 if ok else 503)
