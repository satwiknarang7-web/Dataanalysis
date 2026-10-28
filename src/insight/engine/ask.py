"""A question in plain words, read into a dashboard tile.

"Revenue by region", "average price by category in 2025", "how has churn
changed over time", "top 10 products by revenue", "price vs rating",
"distribution of salary". Read against the table's own fields and measures, so
a question about a column the table does not have is refused by name rather
than answered with something else.
"""

from __future__ import annotations

import re

import pandas as pd

from insight.engine.formatting import lower_label, plural
from insight.engine.measures import agg_measure, count_measure, distinct_measure
from insight.engine.models import FieldInfo, Filter, Grain, Measure, Reading, Spec, Tile
from insight.engine.planner import breakdown_tile, distribution_tile, ranking_tile, relationship_tile, trend_tile

STOP = {
    "the",
    "a",
    "an",
    "of",
    "by",
    "per",
    "for",
    "in",
    "on",
    "and",
    "vs",
    "versus",
    "what",
    "is",
    "are",
    "how",
    "which",
    "show",
    "me",
    "my",
    "with",
    "to",
    "over",
    "across",
    "each",
    "does",
    "do",
    "did",
    "has",
    "have",
    "between",
    "total",
    "average",
    "avg",
    "mean",
    "sum",
    "number",
    "count",
    "many",
    "much",
    "top",
    "bottom",
    "highest",
    "lowest",
    "most",
    "least",
}
GRAIN_WORDS: list[tuple[str, Grain]] = [
    (r"\b(daily|per day|by day|each day)\b", "day"),
    (r"\b(weekly|per week|by week|each week)\b", "week"),
    (r"\b(monthly|per month|by month|each month)\b", "month"),
    (r"\b(quarterly|per quarter|by quarter)\b", "quarter"),
    (r"\b(yearly|annual|annually|per year|by year|each year)\b", "year"),
    (r"\b(day of week|weekday|by weekday)\b", "weekday"),
    (r"\b(hour of day|time of day|by hour)\b", "hour"),
]


class AskError(ValueError):
    """The question could not be read against this table; the message says why."""


def _norm(s: str) -> str:
    s = re.sub(r"[_\-./]+", " ", str(s).lower())
    s = re.sub(r"[^a-z0-9%\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _mention(q: str, name: str) -> float:
    """How well a field's name is mentioned in the question (0 = not at all)."""
    n = _norm(name)
    if not n:
        return 0
    if re.search(rf"\b{re.escape(n)}s?\b", q):
        return len(n) + 10
    words = [w for w in n.split() if len(w) > 2 and w not in STOP]
    if not words:
        return 0
    hits = [w for w in words if re.search(rf"\b{re.escape(w)}s?\b", q) or (w.endswith("s") and re.search(rf"\b{re.escape(w[:-1])}\b", q))]
    if len(hits) == len(words):
        return len(" ".join(hits)) + 5
    return len(" ".join(hits)) if len(hits) / len(words) >= 0.5 else 0


def _names(q: str, f: FieldInfo) -> bool:
    """The question names this field in full, not just one of its words."""
    return _mention(q, f.label) >= len(_norm(f.label)) + 5


def _best(q: str, fields: list[FieldInfo]) -> list[FieldInfo]:
    scored = [(max(_mention(q, f.label), _mention(q, f.name)), f) for f in fields]
    return [f for s, f in sorted(scored, key=lambda x: -x[0]) if s > 0]


def interpret(question: str, reading: Reading, measures: list[Measure], df: pd.DataFrame) -> tuple[Tile, Measure | None]:
    """Read a question into a tile. Returns the tile and any measure made up for it."""
    q = _norm(question)
    if not q:
        raise AskError("Type a question about your data.")
    dims = [f for f in reading.fields if f.role in ("dimension", "id")]
    times = [f for f in reading.fields if f.role == "time" and (not f.origin or _names(q, f))]
    times.sort(key=lambda f: f.name != reading.time)
    nums = reading.by_role("measure")

    filters, used = _value_filters(q, dims, df)
    if (year := re.search(r"\b(in|for|during)\s+((19|20)\d{2})\b", q)) and times:
        t = times[0]
        filters.append(Filter(field=t.name, values=[year.group(2)], grain=None if t.time_unit else "year"))

    by_num = _best(q, nums)
    # An id column ("order id") only when named in full: "orders" is the count, not a split by order.
    by_dim = [d for d in _best(q, dims) if d.name not in used and (d.role != "id" or _names(q, d))]
    wants_avg = bool(re.search(r"\b(average|avg|mean|typical)\b", q))
    wants_median = bool(re.search(r"\bmedian\b", q))
    wants_sum = bool(re.search(r"\b(total|sum|overall)\b", q))
    wants_count = bool(re.search(r"\b(how many|number of|count of|count)\b", q))
    wants_distinct = bool(re.search(r"\b(unique|distinct|different)\b", q))

    if re.search(r"\b(vs|versus|against|relationship|correlat\w*|related|affect\w*)\b", q) and len(by_num) >= 2:
        a, b = by_num[:2]
        y, x = (a, b) if q.find(_norm(a.label)) < q.find(_norm(b.label)) else (b, a)
        return relationship_tile(x, y, filters), None
    if re.search(r"\b(distribution|spread|histogram|range of)\b", q) and by_num:
        return distribution_tile(by_num[0], filters), None

    measure = _pick_measure(q, by_num, by_dim, measures, reading, wants_avg, wants_median, wants_sum, wants_count, wants_distinct)
    if measure is None:
        named = ", ".join([w for w in q.split() if len(w) > 3 and w not in STOP][:3]) or question
        options = ", ".join(m.label for m in measures[:5])
        raise AskError(f'There is no number matching "{named}" in this table. Try one of: {options}.')
    adhoc = measure if all(m.id != measure.id for m in measures) else None
    by_dim = [d for d in by_dim if d.name != measure.field]  # "returned rate" names its own column

    grain = next((g for pattern, g in GRAIN_WORDS if re.search(pattern, q)), None)
    wants_time = (
        grain is not None
        or bool(re.search(r"\b(over time|trend\w*|changed?|growth|grown|history|timeline)\b", q))
        or any(_names(q, t) for t in times)
    )
    limit_m = re.search(r"\b(top|bottom|best|worst|highest|lowest)\s+(\d+)\b", q)
    ascending = bool(re.search(r"\b(bottom|worst|lowest|least)\b", q))

    if wants_time and times:
        t = next((x for x in times if _names(q, x)), times[0])
        series = by_dim[0] if by_dim and by_dim[0].distinct <= 12 else None
        tile = trend_tile(df, measure, t, series, filters)
        if grain and tile.spec and t.time_unit is None:
            tile.spec.by[0].grain = grain
        return tile, adhoc

    if by_dim:
        d = by_dim[0]
        if limit_m or d.distinct > 30 or d.role == "id":
            limit = min(50, int(limit_m.group(2))) if limit_m else 10
            return ranking_tile(measure, d, limit, not ascending, filters), adhoc
        tile = breakdown_tile(measure, d, filters)
        if re.search(r"\b(share|split|mix|breakdown|proportion)\b", q) and measure.additive:
            tile.viz = "donut"
        return tile, adhoc

    return Tile(
        id=f"ask:kpi:{measure.id}", kind="kpi", title=measure.label, viz="kpi", spec=Spec(measures=[measure], filters=filters)
    ), adhoc


def _value_filters(q: str, dims: list[FieldInfo], df: pd.DataFrame) -> tuple[list[Filter], set[str]]:
    """A category value named in the question: "in Europe", "for Pro"."""
    filters: list[Filter] = []
    used: set[str] = set()
    for d in dims:
        if d.distinct > 200:
            continue
        values = df[d.name].dropna().astype(str).unique()
        for v in values:
            nv = _norm(v)
            if len(nv) >= 2 and re.search(rf"\b(in|for|from|where|only|of|at)\s+(the\s+)?{re.escape(nv)}\b", q):
                filters.append(Filter(field=d.name, values=[str(v)]))
                used.add(d.name)
    return filters, used


def _pick_measure(q, by_num, by_dim, measures, reading, wants_avg, wants_median, wants_sum, wants_count, wants_distinct) -> Measure | None:
    f = by_num[0] if by_num else None
    if f and (wants_avg or wants_median or wants_sum):
        agg = "median" if wants_median else "avg" if wants_avg else "sum"
        return next((m for m in measures if m.type == "agg" and m.field == f.name and m.agg == agg), None) or agg_measure(f, agg)
    named = [m for m in measures if m.type == "rate" and _mention(q, m.label)]
    if named:
        return named[0]
    if f:
        return next((m for m in measures if m.type == "agg" and m.field == f.name), None) or agg_measure(f)
    if wants_count and wants_distinct and by_dim:
        return distinct_measure(by_dim[0])
    noun = plural(lower_label(reading.row_noun))
    if wants_count or re.search(rf"\b(rows|records|{re.escape(noun)})\b", q):
        return next((m for m in measures if m.type == "count"), count_measure(reading))
    return None


def example_questions(reading: Reading, measures: list[Measure]) -> list[str]:
    """Questions worth asking of this table, for the Ask page's suggestions."""
    dims = [f for f in reading.fields if f.role == "dimension" and f.distinct <= 30]
    m = measures[:3]
    out: list[str] = []
    if m and dims:
        out.append(f"{m[0].label} by {lower_label(dims[0].label)}")
    if m and reading.time:
        out.append(f"{m[0].label} over time")
    rate = next((x for x in measures if x.type == "rate"), None)
    if rate and len(dims) > 1:
        out.append(f"{rate.label} by {lower_label(dims[1].label)}")
    big = next((f for f in reading.fields if f.role in ("dimension", "id") and 12 < f.distinct <= reading.row_count / 4), None)
    if m and big:
        out.append(f"Top 10 {plural(lower_label(big.label))} by {lower_label(m[0].label)}")
    nums = reading.by_role("measure")
    if len(nums) >= 2:
        out.append(f"{nums[0].label} vs {lower_label(nums[1].label)}")
    if nums:
        out.append(f"Distribution of {lower_label(nums[0].label)}")
    return list(dict.fromkeys(out))[:6]
