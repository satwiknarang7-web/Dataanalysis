"""Security headers on every response, and a same-origin check on every write.

The session cookie is SameSite=Lax, so other sites cannot send it with a
POST; checking the Origin header as well closes the gap for older browsers.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

CSP = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
        "font-src 'self' https://fonts.gstatic.com",
        "img-src 'self' data: blob:",
        "connect-src 'self'",
        "frame-ancestors 'none'",
        "base-uri 'self'",
        "form-action 'self'",
    ]
)


class SecurityMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, production: bool) -> None:
        super().__init__(app)
        self.production = production

    async def dispatch(self, request: Request, call_next) -> Response:
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and not self._same_origin(request):
            return PlainTextResponse("Cross-site request refused.", status_code=403)
        response = await call_next(request)
        if request.url.path.startswith("/static/") and response.status_code == 200:
            response.headers["Cache-Control"] = _static_cache(request)
        response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if self.production:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response

    @staticmethod
    def _same_origin(request: Request) -> bool:
        origin = request.headers.get("origin") or request.headers.get("referer")
        if not origin:
            return True  # non-browser clients send neither; the cookie rules still apply
        host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
        return urlsplit(origin).netloc == host


def _static_cache(request: Request) -> str:
    """Versioned assets (a content hash in the URL, or a version in the vendor file name) never change: cache them for a year.

    `public` lets a CDN (Vercel's, Cloudflare…) serve them without calling the app again.
    """
    if "v" in request.query_params or request.url.path.startswith("/static/vendor/"):
        return "public, max-age=31536000, immutable"
    return "public, max-age=3600"
