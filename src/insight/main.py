"""Entry point: `insight` (or `uvicorn insight.main:app`)."""

from __future__ import annotations

import os

import uvicorn

from insight.web.app import create_app

app = create_app()


def run() -> None:
    uvicorn.run(
        "insight.main:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    run()
