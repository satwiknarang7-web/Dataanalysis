"""The application: wiring, lifespan, middleware and error pages."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.gzip import GZipMiddleware

from insight.cache.backend import make_cache
from insight.cache.frames import FrameCache
from insight.config import Settings, get_settings
from insight.db.session import create_tables, make_engine, make_sessionmaker
from insight.services.datasets import NotFound
from insight.state import AppState
from insight.web.deps import STATIC_DIR, SignInRequired, templates
from insight.web.routes import analyses, auth, datasets, health, public, workspace
from insight.web.security import SecurityMiddleware

log = logging.getLogger("insight")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = make_engine(settings)
        if settings.auto_create_tables:
            await create_tables(engine)
        app.state.insight = AppState(
            settings=settings,
            engine=engine,
            sessions=make_sessionmaker(engine),
            cache=make_cache(settings.redis_url, settings.cache_ttl_seconds),
            frames=FrameCache(settings.frame_cache_items),
            compute_slots=asyncio.Semaphore(settings.max_concurrent_analyses),
        )
        log.info("Insight started (%s, cache: %s)", settings.environment, "redis" if settings.redis_url else "memory")
        yield
        await app.state.insight.cache.backend.close()
        await engine.dispose()

    templates.env.globals["contact_email"] = settings.contact_email
    app = FastAPI(title=settings.app_name, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.add_middleware(SecurityMiddleware, production=settings.is_production)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    for router in (public.router, auth.router, workspace.router, datasets.router, analyses.router, health.router):
        app.include_router(router)

    @app.exception_handler(SignInRequired)
    async def sign_in_required(request: Request, exc: SignInRequired) -> Response:
        if request.headers.get("hx-request"):
            return Response(status_code=401, headers={"HX-Redirect": f"/signin?next={exc.next_url}"})
        return RedirectResponse(f"/signin?next={exc.next_url}", status_code=303)

    @app.exception_handler(NotFound)
    async def not_found(request: Request, exc: NotFound) -> HTMLResponse:
        return _error(request, 404, "We couldn't find that", "It may have been deleted, or it isn't shared with you.")

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> HTMLResponse:
        if exc.status_code == 404:
            return _error(request, 404, "Page not found", "The link may be old, or mistyped.")
        return _error(request, exc.status_code, "Something went wrong", str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def bad_request(request: Request, exc: RequestValidationError) -> HTMLResponse:
        return _error(request, 400, "That request wasn't quite right", "Go back and try again.")

    @app.exception_handler(Exception)
    async def crash(request: Request, exc: Exception) -> HTMLResponse:
        log.exception("Unhandled error on %s", request.url.path)
        return _error(request, 500, "Something went wrong on our side", "It has been logged. Try again in a moment.")

    return app


def _error(request: Request, status: int, title: str, message: str) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "pages/error.html",
        {"status": status, "title": title, "message": message, "user": getattr(request.state, "user", None)},
        status_code=status,
    )
