"""What each column is, and what the table is.

The first thing an analyst does with an unfamiliar table is read it: which
column is the date, which are the things you split by, which numbers you add
up and which you average, which is an id nobody charts. Everything the planner
decides rests on these answers. They come from the values first and the names
second: a name only breaks a tie the values cannot settle.
"""

from __future__ import annotations

import re

import numpy as np
import pandas as pd

from insight.engine.formatting import label_of, name_words
from insight.engine.models import FieldInfo, Reading

AVERAGED = re.compile(
    r"\b(rate|ratio|pct|percent|percentage|share|avg|average|mean|median|score|rating|index|price|cost per|per|age|"
    r"temp|temperature|humidity|pressure|lat|latitude|lon|lng|longitude|margin|probability|prob|grade|rank|ranking|"
    r"level|tenure|duration|days|hours|minutes|seconds|years|speed|weight|height|length|width|size|density|yield|"
    r"capacity|salary|wage|income|nps|csat|satisfaction|growth|latency|ms|cpu|memory|utilisation|utilization)\b"
)
ADDITIVE = re.compile(
    r"\b(revenue|sales|amount|total|sum|profit|cost|costs|spend|spent|expense|expenses|budget|qty|quantity|units|"
    r"volume|count|orders|sessions|visits|clicks|impressions|conversions|signups|downloads|installs|leads|deals|"
    r"calls|tickets|bookings|transactions|value|gmv|net|gross|tax|fees|refund|refunds|payments|paid|balance|"
    r"population|emissions|hires|stock|inventory|views|users|customers|items|points|goals|wins|losses|energy|"
    r"consumption|usage|shipped|received|sold)\b"
)
MONEY = re.compile(
    r"\b(revenue|sales|amount|price|cost|costs|spend|spent|expense|expenses|budget|profit|salary|wage|income|fee|"
    r"fees|tax|gmv|payment|payments|paid|refund|refunds|usd|eur|gbp|inr|dollars?|euros?|pounds?|rupees?|charge|"
    r"charges|balance|value|mrr|arr|ltv|aov)\b"
)
PERCENT = re.compile(r"(\bpct\b|\bpercent|\bpercentage\b|%|\brate\b|\bshare\b|\bmargin\b|\bprobability\b|\bgrowth\b)")
ID_NAME = re.compile(r"(^|\b)(id|uuid|guid|key|code|number|no|num|ref|reference|sku|serial|hash|email|phone|url|isbn)\b")
ID_HEAD = re.compile(r"(^|\s)(id|uuid|guid|key|code|number|no|num|ref|reference|sku|serial|hash|phone|isbn|zip|zipcode|postcode)$|^id\s")
QUANTITY_NAME = re.compile(r"\b(number|num|no|count)\s+of\b|\b(num|no)\s+[a-z]+s\b")
YEAR_NAME = re.compile(r"\b(year|yr|fiscal year|fy)\b")
# When each thing began (born, founded, hired, signed up): a date to filter by, not the axis a trend runs along.
ORIGIN = re.compile(
    r"\b(birth|births|born|dob|birthday|birthdate|founded|founding|established|incorporated|built|manufactured|"
    r"hire|hired|joined|joining|signup|signed up|sign up|registered|registration|onboarded|enrolled)\b"
)
OUTCOME_NAME = re.compile(
    r"\b(churn|churned|converted|conversion|default|defaulted|survived|fraud|won|success|approved|cancelled|"
    r"canceled|attrition|outcome|target|clicked|purchased|returned|retained|left|exited|hired|passed|failed|"
    r"late|delayed|bounced|refunded|rejected|accepted|completed|resolved|renewed|upgraded|is [a-z]+|has [a-z]+)\b"
)
PROBLEM = re.compile(
    r"\b(fault|error|fail|failed|failure|critical|defect|reject|rejected|late|breach|cancel|cancelled|canceled|"
    r"refund|refunded|return|returned|churn|churned|lost|dropped|abandoned|fraud|overdue|default|denied|declined)\b",
    re.I,
)
YES = re.compile(r"^(y|yes|true|active|won|success|churned|converted|retained|accepted|approved|hired|passed|enrolled)$", re.I)
NO = re.compile(r"^(n|no|false|inactive|lost|failure|failed|rejected|declined|denied)$", re.I)

LEVEL_SCALES = [
    ["very low", "low", "medium", "moderate", "high", "very high"],
    ["none", "low", "medium", "high", "critical"],
    ["small", "medium", "large", "extra large"],
    ["xs", "s", "m", "l", "xl", "xxl"],
    ["very poor", "poor", "fair", "average", "good", "very good", "excellent"],
    ["strongly disagree", "disagree", "neutral", "agree", "strongly agree"],
    ["very dissatisfied", "dissatisfied", "neutral", "satisfied", "very satisfied"],
    ["never", "rarely", "sometimes", "often", "always"],
    ["bronze", "silver", "gold", "platinum"],
    ["junior", "mid", "senior", "lead", "principal"],
    ["beginner", "intermediate", "advanced", "expert"],
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"],
    ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"],
    ["q1", "q2", "q3", "q4"],
    ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
    ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"],
]


def read_dataset(df: pd.DataFrame, percent_columns: set[str] | None = None) -> Reading:
    """Read every column, then the table: its time axis and what a row is."""
    percent_columns = percent_columns or set()
    fields = [_role(_profile(df[c], str(c)), str(c) in percent_columns) for c in df.columns]
    times = [f for f in fields if f.role == "time" and not f.origin]
    # The time axis is the real date with the most rows; a year column only when there is no date.
    times.sort(key=lambda f: (f.time_unit is None, f.filled), reverse=True)
    ids = [f for f in fields if f.role == "id" and f.distinct >= 0.95 * len(df)]
    noun = _row_noun(ids[0].name) if ids else "row"
    return Reading(row_count=len(df), fields=fields, time=times[0].name if times else None, row_noun=noun)


def _profile(s: pd.Series, name: str) -> FieldInfo:
    filled = s.dropna()
    counts = filled.astype(str).value_counts()
    info = dict(
        name=name,
        label=label_of(name),
        filled=len(filled),
        distinct=len(counts),
        top=[(str(k), int(v)) for k, v in counts.head(12).items()],
    )
    if pd.api.types.is_datetime64_any_dtype(s):
        kind = "date"
        if len(filled):
            info.update(min=filled.min().isoformat(), max=filled.max().isoformat())
    elif pd.api.types.is_numeric_dtype(s):
        kind = "number"
        if len(filled):
            values = filled.astype("float64")
            info.update(
                min=float(values.min()),
                max=float(values.max()),
                integer=bool(np.all(values == np.round(values))),
            )
    elif len(counts) and set(counts.index) <= {"Yes", "No"}:
        kind = "boolean"
    else:
        kind = "text"
    return FieldInfo(kind=kind, role="dimension", **info)


def _role(f: FieldInfo, percent_hint: bool) -> FieldInfo:
    w = name_words(f.name)
    if not f.filled:
        return f.model_copy(update=dict(role="ignore", why="empty"))
    if f.distinct == 1:
        return f.model_copy(update=dict(role="ignore", why="one value on every row"))
    if f.kind == "date":
        origin = bool(ORIGIN.search(w))
        why = "when each one began, not a timeline" if origin else "dates"
        return f.model_copy(update=dict(role="time", origin=origin, why=why))
    if f.kind == "boolean":
        return f.model_copy(update=dict(role="dimension", outcome="Yes", why="yes / no"))
    if f.kind == "number":
        return _number_role(f, w, percent_hint)
    return _text_role(f, w)


def _number_role(f: FieldInfo, w: str, percent_hint: bool) -> FieldInfo:
    lo, hi = float(f.min), float(f.max)  # type: ignore[arg-type]
    additive_name = bool(ADDITIVE.search(w))
    # Whole numbers named like an id label things; they do not count them.
    if f.integer and ID_NAME.search(w) and not additive_name and f.distinct >= 0.9 * f.filled:
        return f.model_copy(update=dict(role="id", why="unique whole numbers named like an id"))
    if f.integer and ID_HEAD.search(w) and not QUANTITY_NAME.search(w) and not additive_name:
        if f.distinct > 60:
            return f.model_copy(update=dict(role="id", why="codes that label things, not count them"))
        return f.model_copy(update=dict(role="dimension", why="a few codes: groups, not a quantity"))
    if f.integer and lo >= 1900 and hi <= 2100 and (YEAR_NAME.search(w) or (f.distinct <= 60 and not additive_name)):
        return f.model_copy(update=dict(role="time", time_unit="year", why="years"))
    if f.integer and re.search(r"\bhour\b", w) and lo >= 0 and hi <= 23:
        return f.model_copy(update=dict(role="dimension", ordinal=True, why="an hour of the day"))
    if f.integer and re.search(r"\b(rank|ranking|tier|position|seq|sequence)\b", w) and f.distinct <= 50:
        return f.model_copy(update=dict(role="dimension", ordinal=True, why="a rank"))

    small_scale = f.integer and f.distinct <= 11 and hi - lo <= 10 and lo >= 0
    count_name = additive_name and not AVERAGED.search(w)
    money = bool(MONEY.search(w))
    if (small_scale and not count_name) or AVERAGED.search(w) or PERCENT.search(w) or percent_hint:
        agg = "avg"
    elif additive_name:
        agg = "sum"
    elif (lo < 0 and not money) or (not f.integer and (not money or 0 <= lo <= hi <= 1)):
        agg = "avg"
    else:
        agg = "sum"

    fmt, scale = ("currency" if money and not (small_scale and not count_name) else "number"), 1
    if percent_hint or PERCENT.search(w) or (not f.integer and 0 <= lo <= hi <= 1 and re.search(r"\b(ratio|share|rate)\b", w)):
        fmt, scale = "percent", (1 if hi <= 1.0001 and not percent_hint else 100)
    return f.model_copy(
        update=dict(
            role="measure",
            agg=agg,
            format=fmt,
            percent_scale=scale,
            ordinal=small_scale and not count_name,
            why="a number that adds up across rows" if agg == "sum" else "a number to average, not add",
        )
    )


def _text_role(f: FieldInfo, w: str) -> FieldInfo:
    levels = [v for v, _ in f.top]
    if levels and sum(bool(re.match(r"^https?://\S+$", v)) for v in levels) >= 0.8 * len(levels):
        return f.model_copy(update=dict(role="id", why="web addresses"))
    avg_len = float(np.mean([len(v) for v in levels])) if levels else 0.0
    id_like = f.distinct >= max(20, 0.9 * f.filled)
    if id_like and avg_len > 40:
        return f.model_copy(update=dict(role="text", why="free text"))
    if id_like:
        return f.model_copy(update=dict(role="id", why="a different value on almost every row"))
    if ID_NAME.search(w) and f.distinct > 20 and not re.search(r"\b(name|type|category|status)\b", w):
        return f.model_copy(update=dict(role="id", why="named like an id"))
    if avg_len > 60 and f.distinct > 30:
        return f.model_copy(update=dict(role="text", why="free text"))

    # A status whose rarer level is a problem (Refund among Sale) is an outcome.
    if 2 <= f.distinct <= 3:
        for value, n in f.top:
            if PROBLEM.search(value) and n < 0.5 * f.filled:
                return f.model_copy(update=dict(role="dimension", outcome=value, why=f"how often it is {value}"))
    if f.distinct == 2:
        positive = positive_level(levels, w)
        if positive:
            return f.model_copy(update=dict(role="dimension", outcome=positive, why="two levels: an outcome"))
    order = level_order(levels) if 3 <= f.distinct <= 16 and len(levels) == f.distinct else None
    if order:
        return f.model_copy(update=dict(role="dimension", ordinal=True, order=order, why="levels with an order"))
    return f.model_copy(update=dict(role="dimension", why=f"{f.distinct} categories"))


def positive_level(levels: list[str], words: str = "") -> str | None:
    """Which of a two-level column's values is the event (churned, converted…)."""
    yes = next((v for v in levels if YES.match(v.strip())), None)
    no = next((v for v in levels if NO.match(v.strip())), None)
    if yes:
        return yes
    if no:
        return next((v for v in levels if v != no), None)
    if OUTCOME_NAME.search(words):
        stems = [t[:5] for t in words.split() if len(t) > 3]
        return next((v for v in levels if any(s in v.lower() for s in stems)), None)
    return None


def level_order(levels: list[str]) -> list[str] | None:
    """Levels in their own order — Low, Medium, High; Under 1, 1–3, 5+ — or None."""
    low = {v: v.strip().lower() for v in levels}
    for scale in LEVEL_SCALES:
        if all(x in scale for x in low.values()):
            return sorted(levels, key=lambda v: scale.index(low[v]))
    keys = {v: _range_key(v) for v in levels}
    if all(k is not None for k in keys.values()):
        return sorted(levels, key=lambda v: keys[v])  # type: ignore[arg-type, return-value]
    return None


def _range_key(level: str) -> float | None:
    v = level.strip().lower().replace(",", "")
    num = r"[$£€]?(-?\d+(?:\.\d+)?)"
    if m := re.match(rf"^(?:under|below|less than|up to|<|<=)\s*{num}", v):
        return float(m.group(1)) - 0.5
    if m := re.match(rf"^(?:over|above|more than|>|>=)\s*{num}", v):
        return float(m.group(1)) + 0.5
    if m := re.match(rf"^{num}\s*\+", v):
        return float(m.group(1)) + 0.5
    if m := re.match(rf"^{num}", v):
        return float(m.group(1))
    return None


def _row_noun(id_name: str) -> str:
    w = name_words(id_name)
    w = re.sub(r"\b(id|uuid|guid|key|code|number|no|num|ref)\b", "", w).strip()
    return w.split()[-1] if w else "row"
