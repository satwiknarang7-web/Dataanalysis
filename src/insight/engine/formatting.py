"""How numbers and names are written for a reader."""

from __future__ import annotations

import math
import re

from insight.engine.models import Format, Measure

_ACRONYMS = {
    "usd",
    "eur",
    "gbp",
    "inr",
    "jpy",
    "gdp",
    "nps",
    "csat",
    "api",
    "id",
    "sla",
    "kpi",
    "roi",
    "aov",
    "mrr",
    "arr",
    "ltv",
    "cpc",
    "cpa",
    "cpm",
    "ctr",
    "sku",
    "url",
    "hr",
    "ai",
    "uk",
    "us",
    "eu",
    "crm",
    "erp",
    "vat",
    "gst",
    "ebitda",
    "yoy",
    "qa",
    "ip",
    "bmi",
    "kwh",
    "co2",
}


def name_words(name: str) -> str:
    """A column name as words, the way a reader says it: `orderDate` → "order date"."""
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", str(name))
    s = re.sub(r"[_\-./]+", " ", s)
    return re.sub(r"\s+", " ", s).strip().lower()


def label_of(name: str) -> str:
    """`units_sold` → "Units sold", keeping acronyms (USD, NPS) upper case."""
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", str(name))
    s = re.sub(r"[?!:*#]+$", "", s.strip())  # "Churned?" → "Churned"
    tokens = re.sub(r"[_\-]+", " ", s).split()
    if not tokens:
        return str(name)
    out = []
    for i, t in enumerate(tokens):
        low = t.lower()
        if re.fullmatch(r"[A-Z0-9]{2,}", t) or low in _ACRONYMS:
            out.append(t.upper())
        elif low == "pct":
            out.append("%")
        elif i == 0:
            out.append(t[:1].upper() + t[1:].lower())
        else:
            out.append(low)
    return " ".join(out)


def _compact(x: float) -> str:
    """1234567 → "1.23M": three significant figures past a thousand."""
    ax = abs(x)
    for size, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if ax >= size:
            v = x / size
            digits = 2 if abs(v) < 10 else 1 if abs(v) < 100 else 0
            return f"{v:.{digits}f}".rstrip("0").rstrip(".") + suffix
    if float(x).is_integer():
        return f"{int(x):,}"
    digits = 2 if ax < 10 else 1
    return f"{x:,.{digits}f}"


def format_value(x: float | None, fmt: Format = "number", percent_scale: int = 1) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "–"
    x = float(x)
    if fmt == "percent":
        pct = x * 100 if percent_scale == 1 else x
        return f"{pct:.1f}%".replace(".0%", "%")
    if fmt == "currency":
        sign = "-" if x < 0 else ""
        return f"{sign}${_compact(abs(x))}"
    return _compact(x)


def format_measure(x: float | None, m: Measure) -> str:
    return format_value(x, m.format, m.percent_scale)


def format_change(a: float, b: float, m: Measure) -> str:
    """The change from a to b.

    A percentage moves in points; anything else moves by a share of where it
    started, unless it started at zero.
    """
    if m.format == "percent" or m.type == "rate":
        scale = 100 if m.percent_scale == 1 else 1
        return f"{(b - a) * scale:+.1f} pts"
    if a == 0:
        return f"{'+' if b >= 0 else '−'}{format_measure(abs(b), m)}"
    pct = (b - a) / abs(a) * 100
    return f"{pct:+.0f}%" if abs(pct) >= 10 else f"{pct:+.1f}%"


def lower_label(label: str) -> str:
    """A label inside a sentence: "Revenue" → "revenue", but "NPS score" stays."""
    return label if label[:2].isupper() else label[:1].lower() + label[1:]


_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def period_label(key: str) -> str:
    """A sortable period key as a reader writes it: 2024-01 → Jan 2024, 2024-Q1 → Q1 2024."""
    if m := re.fullmatch(r"(\d{4})-(\d{2})", key):
        return f"{_MONTHS[int(m.group(2)) - 1]} {m.group(1)}"
    if m := re.fullmatch(r"(\d{4})-Q(\d)", key):
        return f"Q{m.group(2)} {m.group(1)}"
    if m := re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", key):
        return f"{int(m.group(3))} {_MONTHS[int(m.group(2)) - 1]} {m.group(1)}"
    return key


def plural(word: str) -> str:
    """category → categories, status → statuses, region → regions."""
    w = word.strip()
    if not w:
        return w
    head, _, last = w.rpartition(" ")
    low = last.lower()
    if low.endswith(("s", "x", "z", "ch", "sh")):
        last += "es"
    elif low.endswith("y") and len(low) > 1 and low[-2] not in "aeiou":
        last = last[:-1] + "ies"
    else:
        last += "s"
    return f"{head} {last}".strip()


def share(part: float, whole: float) -> str:
    return f"{part / whole * 100:.0f}%" if whole else "–"


def join_names(names: list[str]) -> str:
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def first_sentence(text: str) -> str:
    """The lead of a caption, for the brief: up to the first full stop."""
    m = re.search(r"^(.+?[.!?])(\s|$)", text.strip())
    return m.group(1) if m else text.strip()
