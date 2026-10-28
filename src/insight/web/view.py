"""Small presentation helpers used by the templates."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from insight.engine.models import Measure, RenderedTile

ROLE_LABELS = {
    "time": "Timeline",
    "measure": "Number",
    "dimension": "Category",
    "id": "Identifier",
    "text": "Free text",
    "ignore": "Not used",
}
KIND_LABELS = {"number": "Number", "date": "Date", "boolean": "Yes / No", "text": "Text"}


@dataclass
class Workspace:
    """Which rows a page is about, and where its tabs live."""

    name: str
    base: str  # /datasets/<id> or /demo/<key>
    rows: int
    columns: int
    demo: bool = False
    dataset_id: UUID | None = None
    analysis_id: UUID | None = None
    analysis_title: str | None = None
    owner: bool = True
    shares: list[str] = field(default_factory=list)

    @property
    def board_url(self) -> str:
        return f"/analyses/{self.analysis_id}" if self.analysis_id else f"{self.base}/dashboard"


def asset_version(static_dir: Path) -> str:
    """A short hash of our own CSS and JS, so their URLs change exactly when they do and can be cached for good."""
    digest = hashlib.sha256()
    for path in sorted(static_dir.glob("css/*.css")) + sorted(static_dir.glob("js/*.js")):
        digest.update(path.read_bytes())
    return digest.hexdigest()[:10]


def sparkline(values: list[float | None], width: int = 120, height: int = 32) -> dict | None:
    """An SVG path for a KPI's sparkline, drawn on the server: crisp and free of JS."""
    ys = [v for v in values if v is not None]
    if len(ys) < 2:
        return None
    lo, hi = min(ys), max(ys)
    span = (hi - lo) or 1
    step = width / (len(ys) - 1)
    pts = [(i * step, height - 2 - (y - lo) / span * (height - 4)) for i, y in enumerate(ys)]
    line = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    area = f"{line} L{width},{height} L0,{height} Z"
    return {"line": line, "area": area, "width": width, "height": height, "last": pts[-1]}


def change_tone(change: str | None, m: Measure | None) -> str:
    """good | bad | flat, for colouring a KPI's change by what it means."""
    if not change or m is None:
        return "flat"
    sign = change.strip()[:1]
    if change.strip().startswith(("+0.0", "-0.0", "+0%", "-0%")) or sign not in "+-−":
        return "flat"
    up = sign == "+"
    return "good" if up == m.good_up else "bad"


def chart_payload(t: RenderedTile) -> str:
    return json.dumps({"viz": t.tile.viz, "kind": t.tile.kind, **t.data}, separators=(",", ":"), default=str)


def ago(dt: datetime | None) -> str:
    if dt is None:
        return ""
    dt = dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    s = (datetime.now(UTC) - dt).total_seconds()
    for limit, size, word in ((60, 1, "just now"), (3600, 60, "minute"), (86400, 3600, "hour"), (86400 * 30, 86400, "day")):
        if s < limit:
            if size == 1:
                return word
            n = int(s // size)
            return f"{n} {word}{'s' if n != 1 else ''} ago"
    return dt.strftime("%d %b %Y")


def filesize(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"


def number(n: int | float) -> str:
    return f"{n:,.0f}"


FILTERS = {"ago": ago, "filesize": filesize, "number": number}
