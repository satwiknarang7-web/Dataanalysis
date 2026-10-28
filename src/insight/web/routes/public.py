"""The pages anyone can see: the landing page and pricing."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from insight.services.auth import CurrentUser
from insight.services.datasets import SAMPLES
from insight.services.plans import FREE, PRO
from insight.web.deps import optional_user, templates

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def landing(request: Request, user: CurrentUser | None = Depends(optional_user)):
    return templates.TemplateResponse(request, "pages/landing.html", {"user": user, "samples": SAMPLES.values(), "plans": (FREE, PRO)})


@router.get("/pricing", response_class=HTMLResponse)
async def pricing(request: Request, user: CurrentUser | None = Depends(optional_user)):
    return templates.TemplateResponse(request, "pages/pricing.html", {"user": user, "plans": (FREE, PRO)})
