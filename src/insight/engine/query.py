"""The one way numbers are computed: a structured query run over the rows.

Every tile, headline number and finding is a `Spec`, not a SQL string, so a
filter re-runs the same spec and a chart can never disagree with the sentence
under it. "Top 9 + Other" is computed by relabelling the rest as Other and
grouping again, which is correct for averages and rates as well as sums.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from insight.engine.models import Dim, Filter, Grain, Measure, Spec
from insight.engine.stats import mean_range, wilson_range

BLANK = "(blank)"
OTHER = "Other"
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def bucket(s: pd.Series, grain: Grain) -> pd.Series:
    """A date column as period labels that sort in time order: 2024-03, 2024-Q1…"""
    if not pd.api.types.is_datetime64_any_dtype(s):
        return labels(s)
    if grain == "year":
        out = s.dt.strftime("%Y")
    elif grain == "quarter":
        out = s.dt.year.astype("Int64").astype(str) + "-Q" + s.dt.quarter.astype("Int64").astype(str)
    elif grain == "month":
        out = s.dt.strftime("%Y-%m")
    elif grain == "week":
        out = (s.dt.normalize() - pd.to_timedelta(s.dt.dayofweek, unit="D")).dt.strftime("%Y-%m-%d")
    elif grain == "day":
        out = s.dt.strftime("%Y-%m-%d")
    elif grain == "weekday":
        out = s.dt.dayofweek.map(lambda d: WEEKDAYS[int(d)] if pd.notna(d) else None)
    else:  # hour of day
        out = s.dt.strftime("%H:00")
    return out.astype("object").where(s.notna(), None)


def labels(s: pd.Series) -> pd.Series:
    """Any column as text labels; missing cells are named, not dropped."""
    if pd.api.types.is_datetime64_any_dtype(s):
        out = s.dt.strftime("%Y-%m-%d")
    else:
        out = s.astype("string")
    return out.astype("object").where(s.notna(), BLANK)


def dim_labels(df: pd.DataFrame, dim: Dim | Filter) -> pd.Series:
    s = df[dim.field]
    return bucket(s, dim.grain) if dim.grain else labels(s)


def apply_filters(df: pd.DataFrame, filters: list[Filter]) -> pd.DataFrame:
    mask = np.ones(len(df), dtype=bool)
    for f in filters:
        if f.field not in df.columns or not f.values:
            continue
        hit = dim_labels(df, f).isin(f.values).to_numpy()
        mask &= ~hit if f.exclude else hit
    return df.loc[mask]


def run(df: pd.DataFrame, spec: Spec) -> list[dict]:
    """Run a spec. Each row holds the group labels, each measure's value,
    `_n` (rows in the group) and, for averages and rates, a 95% range."""
    rows = apply_filters(df, spec.filters)
    keys = [f"__k{i}" for i in range(len(spec.by))]
    work = pd.DataFrame({k: dim_labels(rows, d).to_numpy() for k, d in zip(keys, spec.by, strict=False)}, index=rows.index)
    for k, d in zip(keys, spec.by, strict=False):
        if d.grain:  # a row with no date has no period: it is left out of a trend
            work = work.loc[work[k].notna()]

    if spec.other and spec.limit and keys:
        work = _fold_other(rows, work, keys[0], spec)

    out = _aggregate(rows.loc[work.index], work, keys, spec.measures)
    out = _sort(out, keys, spec)
    if spec.limit and not spec.other:
        out = out[: spec.limit]
    rename = dict(zip(keys, (d.field for d in spec.by), strict=False))
    return [{rename.get(k, k): _clean(v) for k, v in r.items()} for r in out]


def _fold_other(rows: pd.DataFrame, work: pd.DataFrame, key: str, spec: Spec) -> pd.DataFrame:
    ranked = _aggregate(rows.loc[work.index], work, [key], spec.measures[:1])
    ranked = _sort(ranked, [key], spec.model_copy(update={"sort": "value", "descending": True}))
    keep = {r[key] for r in ranked[: spec.limit]}
    if len(ranked) <= spec.limit + 1:
        return work
    work = work.copy()
    work[key] = work[key].where(work[key].isin(keep), OTHER)
    return work


def _aggregate(rows: pd.DataFrame, work: pd.DataFrame, keys: list[str], measures: list[Measure]) -> list[dict]:
    values = work.copy()
    for i, m in enumerate(measures):
        col = f"__m{i}"
        if m.type == "count":
            values[col] = 1.0
        elif m.type == "rate":
            s = rows[m.field]
            values[col] = (labels(s) == m.event).astype("float64").where(s.notna(), np.nan)
        else:
            values[col] = rows[m.field].astype("float64") if m.type == "agg" else rows[m.field]

    if keys:
        grouped = values.groupby(keys, sort=False, dropna=False)
        groups = [(k if isinstance(k, tuple) else (k,), g) for k, g in grouped]
    else:
        groups = [((), values)]

    out = []
    for key, g in groups:
        row: dict = dict(zip(keys, key, strict=False))
        row["_n"] = len(g)
        for i, m in enumerate(measures):
            col = g[f"__m{i}"]
            row[m.id] = _measure(col, m)
            if m.type == "rate":
                filled = col.dropna()
                row[f"_range:{m.id}"] = wilson_range(float(filled.sum()), len(filled))
            elif m.type == "agg" and m.agg == "avg":
                row[f"_range:{m.id}"] = mean_range(col.dropna().to_numpy())
        out.append(row)
    return out


def _measure(col: pd.Series, m: Measure) -> float | None:
    if m.type == "count":
        return float(len(col))
    if m.type == "distinct":
        return float(col.nunique())
    filled = col.dropna()
    if filled.empty:
        return None if m.type != "agg" or m.agg != "sum" else 0.0
    if m.type == "rate":
        return float(filled.mean())
    return float(getattr(filled, {"avg": "mean"}.get(m.agg or "sum", m.agg or "sum"))())


def _sort(out: list[dict], keys: list[str], spec: Spec) -> list[dict]:
    if not keys:
        return out
    first, mid = keys[0], spec.measures[0].id
    if spec.sort == "label":
        return sorted(out, key=lambda r: (str(r[first]) == BLANK, str(r[first])), reverse=False)
    if spec.sort == "order" and spec.order:
        rank = {v: i for i, v in enumerate(spec.order)}
        return sorted(out, key=lambda r: rank.get(r[first], len(rank)))

    def value_key(r: dict) -> tuple:
        v = r.get(mid)
        v = -math.inf if v is None else v
        # Other always sits last, whatever its size.
        return (r[first] != OTHER, v if spec.descending else -v)

    ranked = sorted(out, key=lambda r: str(r[first]))  # ties broken by label
    return sorted(ranked, key=value_key, reverse=True)


def _clean(v):
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    if isinstance(v, np.generic):
        return v.item()
    return v


def total(df: pd.DataFrame, m: Measure, filters: list[Filter] | None = None) -> float | None:
    return run(df, Spec(measures=[m], filters=filters or []))[0][m.id]
