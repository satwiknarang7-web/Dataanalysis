"""Shaping steps worth offering for this table, each with its reason.

Rules over the values the table actually holds, no model: profit from revenue
and cost, the days between a start and an end date, a "City, ST" column split
in two, a year's worth of month columns turned into rows, a number in bands.
A step that only adds a column is marked `additive`: it changes nothing a
chart already shows, so it is safe to accept with one click.
"""

from __future__ import annotations

import re

import numpy as np
import pandas as pd
from pydantic import BaseModel

from insight.engine.formatting import label_of, lower_label, name_words
from insight.engine.models import Reading
from insight.engine.transforms import ADDITIVE_KINDS, parse_step

REVENUE = re.compile(r"\b(revenue|sales|turnover|income|gmv)\b")
COST = re.compile(r"\b(cost|costs|cogs|expense|expenses|spend)\b")
PRICE = re.compile(r"\b(unit price|price|rate)\b")
QUANTITY = re.compile(r"\b(qty|quantity|units|units sold|count)\b")
START = re.compile(
    r"\b(start|started|begin|created|opened|order|ordered|signup|signed|joined|hire|hired|admit|admitted|submitted|requested)\b"
)
END = re.compile(
    r"\b(end|ended|finish|closed|resolved|ship|shipped|delivered|delivery|churn|churned|cancel|cancelled|left|exit|discharge|discharged|completed|paid|due)\b"
)
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
BANDABLE = re.compile(r"\b(age|tenure|score|years|experience|income|salary|size|duration|days)\b")


class Suggestion(BaseModel):
    step: dict
    sentence: str
    reason: str
    additive: bool


def suggest(df: pd.DataFrame, reading: Reading, existing: list[dict] | None = None) -> list[Suggestion]:
    existing = existing or []
    taken = {s.get("name") for s in existing} | set(df.columns)
    out: list[Suggestion] = []

    def add(step: dict, reason: str) -> None:
        if step.get("name") in taken or any(_same(step, e) for e in existing):
            return
        try:
            parsed = parse_step(step)
            parsed.apply(df.head(200))
        except Exception:  # a suggestion that would not run is not offered
            return
        taken.add(step.get("name"))
        out.append(Suggestion(step=step, sentence=parsed.describe(), reason=reason, additive=step["kind"] in ADDITIVE_KINDS))

    numbers = [f for f in reading.fields if f.role == "measure"]
    words = {f.name: name_words(f.name) for f in reading.fields}

    revenue = next((f for f in numbers if REVENUE.search(words[f.name])), None)
    cost = next((f for f in numbers if COST.search(words[f.name]) and f is not revenue), None)
    if revenue and cost and not any(re.search(r"\b(profit|margin)\b", w) for w in words.values()):
        add(
            {"kind": "derive", "name": "Profit", "formula": f"[{revenue.name}] - [{cost.name}]"},
            f"{revenue.label} and {lower_label(cost.label)} are both here, so profit can be worked out row by row.",
        )
        add(
            {"kind": "derive", "name": "Margin", "formula": f"([{revenue.name}] - [{cost.name}]) / [{revenue.name}]"},
            "Profit as a share of revenue, comparable across rows of any size.",
        )

    price = next((f for f in numbers if PRICE.search(words[f.name])), None)
    qty = next((f for f in numbers if QUANTITY.search(words[f.name]) and f is not price), None)
    if price and qty and not revenue:
        add(
            {"kind": "derive", "name": "Sales", "formula": f"[{price.name}] * [{qty.name}]"},
            f"{price.label} × {lower_label(qty.label)} gives what each row was worth.",
        )

    dates = [f for f in reading.fields if f.kind == "date"]
    for start in dates:
        for end in dates:
            if start is end or not START.search(words[start.name]) or not END.search(words[end.name]):
                continue
            both = df[[start.name, end.name]].dropna()
            if len(both) and (both[end.name] >= both[start.name]).mean() > 0.9:
                end_word = END.search(words[end.name]).group(1)
                add(
                    {"kind": "derive", "name": f"Days to {end_word}", "formula": f"DAYS([{end.name}], [{start.name}])"},
                    f"Each row has both {lower_label(start.label)} and {lower_label(end.label)}; "
                    "the gap between them is usually what's managed.",
                )

    for f in reading.fields:
        if f.kind != "text" or f.role not in ("dimension", "id") or f.distinct < 5:
            continue
        values = df[f.name].dropna().astype(str).head(2000)
        if len(values) and values.str.fullmatch(r"[^,]+,\s*[^,]+").mean() > 0.8:
            first, second = _split_names(f.label, values)
            add(
                {"kind": "split", "column": f.name, "separator": ",", "into": [first, second]},
                f"Most values of {lower_label(f.label)} are two things joined by a comma, like “{values.iloc[0]}”.",
            )

    periods = [f.name for f in numbers if _period_name(f.name)]
    if len(periods) >= 3:
        kind = _period_name(periods[0])
        add(
            {"kind": "unpivot", "columns": periods, "name_column": kind, "value_column": "Value"},
            f"{len(periods)} columns are one {kind.lower()} each; as rows, they can be charted over time and filtered.",
        )

    for f in numbers:
        if f.agg == "avg" and BANDABLE.search(words[f.name]) and f.distinct > 12:
            edges = _nice_edges(pd.to_numeric(df[f.name], errors="coerce").dropna().to_numpy())
            if edges:
                add(
                    {"kind": "bucket", "name": f"{f.label} band", "column": f.name, "edges": edges},
                    f"{f.label} has {f.distinct} different values; in a few bands it splits the board like a category.",
                )
    return out[:6]


def _same(a: dict, b: dict) -> bool:
    keys = ("kind", "formula", "column", "columns")
    return all(a.get(k) == b.get(k) for k in keys)


def _split_names(label: str, values: pd.Series) -> tuple[str, str]:
    if re.search(r"\b(location|place|city|address)\b", label.lower()):
        return "City", "Region"
    if re.search(r"\bname\b", label.lower()):
        return "Last name", "First name"
    return f"{label} 1", f"{label} 2"


def _period_name(name: str) -> str | None:
    w = name.strip().lower()
    if w[:3] in MONTHS and len(w) <= 9 and re.fullmatch(r"[a-z]+(\s*\d{2,4})?", w):
        return "Month"
    if re.fullmatch(r"q[1-4](\s*\d{2,4})?", w):
        return "Quarter"
    if re.fullmatch(r"(fy\s*)?(19|20)\d{2}", w):
        return "Year"
    return None


def _nice_edges(values: np.ndarray) -> list[float]:
    if len(values) < 20:
        return []
    raw = np.percentile(values, [25, 50, 75])
    span = float(values.max() - values.min()) or 1.0
    step = 10 ** np.floor(np.log10(span / 4))
    edges = sorted({float(round(v / step) * step) for v in raw})
    return [e for e in edges if values.min() < e < values.max()]


__all__ = ["Suggestion", "label_of", "suggest"]
