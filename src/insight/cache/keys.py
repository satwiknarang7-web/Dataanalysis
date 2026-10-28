"""Every cache key in one place, so their shapes and versions are easy to audit."""

from __future__ import annotations

import hashlib
import json

from insight.engine import ENGINE_VERSION

PREFIX = f"insight:v{ENGINE_VERSION}"


def digest(value: object) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def plan(content_hash: str) -> str:
    return f"{PREFIX}:plan:{content_hash}"


def dashboard(content_hash: str, filters: list[dict]) -> str:
    return f"{PREFIX}:dash:{content_hash}:{digest(filters)}"


def answer(content_hash: str, question: str, filters: list[dict]) -> str:
    return f"{PREFIX}:ask:{content_hash}:{digest([question.strip().lower(), filters])}"


def session(token_hash: str) -> str:
    return f"insight:session:{token_hash}"


def sign_in_attempts(email: str, ip: str) -> str:
    return f"insight:signin:{digest([email.lower(), ip])}"
