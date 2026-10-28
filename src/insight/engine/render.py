"""Compute a tile (or the whole board) over the rows, under any filters.

The planner decides *what* to show once; this runs it, as often as the reader
filters, and re-captions every chart over the rows it now stands on, so a
sentence is never one written over other rows.
"""

from __future__ import annotations

import itertools

import numpy as np
import pandas as pd

from insight.engine import captions
from insight.engine.formatting import first_sentence, format_change, format_measure, format_value, period_label
from insight.engine.measures import find_measure
from insight.engine.models import (
    Caption,
    Dashboard,
    Dim,
    Filter,
    Finding,
    Grain,
    Measure,
    Plan,
    Reading,
    RenderedTile,
    Spec,
    Tile,
)
from insight.engine.query import apply_filters, dim_labels, run

SCATTER_POINTS = 1500


def choose_grain(df: pd.DataFrame, field: str) -> Grain:
    s = df[field].dropna()
    if s.empty or not pd.api.types.is_datetime64_any_dtype(s):
        return "year"
    span = (s.max() - s.min()).days
    if span <= 45:
        return "day"
    if span <= 200:
        return "week"
    if span <= 365 * 4:
        return "month"
    if span <= 365 * 10:
        return "quarter"
    return "year"


def render_tile(df: pd.DataFrame, tile: Tile, reading: Reading, extra: list[Filter] | None = None) -> RenderedTile:
    extra = extra or []
    if tile.kind == "relationship":
        return _relationship(df, tile, reading, extra)
    if tile.kind == "distribution":
        return _distribution(df, tile, reading, extra)
    assert tile.spec is not None
    spec = tile.spec.model_copy(update={"filters": [*tile.spec.filters, *extra]})
    if tile.kind == "kpi":
        return _kpi(df, tile, spec, reading)
    data = run(df, spec)
    m = spec.measures[0]
    if tile.kind == "trend":
        return _trend(df, tile, spec, data, m, reading)
    dim = reading.field(spec.by[0].field)
    if tile.kind == "table":
        whole = run(df, Spec(measures=[m], filters=spec.filters))[0][m.id]
        cap = captions.ranking(data, m, dim, whole)
    else:
        cap = captions.breakdown(data, m, dim, _samples(df, spec, m))
    chart = {
        "labels": [str(r[dim.name]) for r in data],
        "values": [r.get(m.id) for r in data],
        "text": [format_measure(r.get(m.id), m) for r in data],
        "ranges": [r.get(f"_range:{m.id}") for r in data],
        "counts": [r["_n"] for r in data],
        "measure": m.label,
        "dimension": dim.label,
        "format": m.format,
        "percent_scale": m.percent_scale,
    }
    return RenderedTile(tile=tile, data=chart, caption=cap)


def _samples(df: pd.DataFrame, spec: Spec, m: Measure) -> dict[str, np.ndarray] | None:
    """Each group's raw values, for the test of whether an average stands out."""
    if m.type != "agg" or not m.field:
        return None
    rows = apply_filters(df, spec.filters)
    keys = dim_labels(rows, spec.by[0])
    values = rows[m.field].astype("float64")
    return {str(k): g.dropna().to_numpy() for k, g in values.groupby(keys.to_numpy())}


def _trend(df: pd.DataFrame, tile: Tile, spec: Spec, data: list[dict], m: Measure, reading: Reading) -> RenderedTile:
    time = spec.by[0]
    x = sorted({str(r[time.field]) for r in data})
    if len(spec.by) > 1:
        series_dim = spec.by[1].field
        names = sorted({str(r[series_dim]) for r in data})
        lookup = {(str(r[time.field]), str(r[series_dim])): r.get(m.id) for r in data}
        series = [{"name": n, "y": [lookup.get((t, n)) for t in x]} for n in names]
        complete = set(_complete_periods(df, spec, x))
        trimmed = [{"name": s["name"], "y": [y for t, y in zip(x, s["y"], strict=False) if t in complete]} for s in series]
        cap = captions.series_trend(trimmed, m, reading.field(series_dim))
    else:
        series = [{"name": m.label, "y": [r.get(m.id) for r in data]}]
        complete = set(_complete_periods(df, spec, x))
        points = [(str(r[time.field]), r[m.id]) for r in data if r.get(m.id) is not None and str(r[time.field]) in complete]
        cap = captions.trend(points, m, time.grain or "year")
    chart = {"x": [period_label(t) for t in x], "series": series, "measure": m.label, "format": m.format, "percent_scale": m.percent_scale}
    return RenderedTile(tile=tile, data=chart, caption=cap)


def _complete_periods(df: pd.DataFrame, spec: Spec, periods: list[str]) -> list[str]:
    """The periods to judge a change on: the first and last only if they are whole.

    A file that stops on the 9th of a month has a last month a third the size
    of the rest; read as a fall, it would be the board's headline.
    """
    time = spec.by[0]
    if not time.grain or time.grain in ("weekday", "hour") or len(periods) < 3:
        return periods
    rows = apply_filters(df, spec.filters)
    dates = rows[time.field].dropna()
    if not pd.api.types.is_datetime64_any_dtype(dates) or dates.empty:
        return periods
    days = dates.dt.normalize().groupby(dim_labels(rows.loc[dates.index], time).to_numpy()).nunique()
    typical = float(days.median())
    keep = list(periods)
    for edge in (0, -1):
        if keep and days.get(keep[edge], 0) < 0.6 * typical:
            keep.pop(edge)
    return keep


def _kpi(df: pd.DataFrame, tile: Tile, spec: Spec, reading: Reading) -> RenderedTile:
    m = spec.measures[0]
    value = run(df, spec)[0][m.id]
    out = RenderedTile(tile=tile, data={}, caption=Caption(text=""), value=format_measure(value, m))
    if not reading.time:
        return out
    tf = reading.field(reading.time)
    grain: Grain | None = None if tf.time_unit == "year" else choose_grain(df, reading.time)
    if grain == "day":
        grain = "week"
    by_period = spec.model_copy(update={"by": [Dim(field=reading.time, grain=grain)], "sort": "label"})
    points = [p for p in run(df, by_period) if p.get(m.id) is not None]
    out.data = {"spark": [p[m.id] for p in points], "x": [period_label(str(p[reading.time])) for p in points]}
    complete = set(_complete_periods(df, by_period, [str(p[reading.time]) for p in points]))
    whole = [p for p in points if str(p[reading.time]) in complete]
    if len(whole) >= 2:
        a, b = whole[-2][m.id], whole[-1][m.id]
        out.change = format_change(a, b, m)
        out.caption = Caption(text=f"vs {period_label(str(whole[-2][reading.time]))} ({format_measure(a, m)})")
    return out


def _relationship(df: pd.DataFrame, tile: Tile, reading: Reading, extra: list[Filter]) -> RenderedTile:
    rows = apply_filters(df, [*tile.filters, *extra])
    fx, fy = reading.field(tile.x), reading.field(tile.y)  # type: ignore[arg-type]
    x = rows[fx.name].astype("float64").to_numpy()
    y = rows[fy.name].astype("float64").to_numpy()
    cap = captions.relationship(x, y, fx, fy)
    ok = ~(np.isnan(x) | np.isnan(y))
    x, y = x[ok], y[ok]
    if len(x) > SCATTER_POINTS:
        pick = np.random.default_rng(7).choice(len(x), SCATTER_POINTS, replace=False)
        x, y = x[pick], y[pick]
    chart = {"x": x.tolist(), "y": y.tolist(), "x_label": fx.label, "y_label": fy.label}
    return RenderedTile(tile=tile, data=chart, caption=cap)


def _distribution(df: pd.DataFrame, tile: Tile, reading: Reading, extra: list[Filter]) -> RenderedTile:
    rows = apply_filters(df, [*tile.filters, *extra])
    f = reading.field(tile.field)  # type: ignore[arg-type]
    values = rows[f.name].astype("float64").dropna().to_numpy()
    cap = captions.distribution(values, f)
    if len(values):
        counts, edges = np.histogram(values, bins=min(30, max(5, int(np.sqrt(len(values))))))
        mids = ((edges[:-1] + edges[1:]) / 2).tolist()
        labels = [
            f"{format_value(a, f.format, f.percent_scale)} – {format_value(b, f.format, f.percent_scale)}"
            for a, b in itertools.pairwise(edges)
        ]
    else:
        counts, mids, labels = np.array([]), [], []
    chart = {
        "x": mids,
        "counts": counts.tolist(),
        "labels": labels,
        "measure": f.label,
        "format": f.format,
        "percent_scale": f.percent_scale,
    }
    return RenderedTile(tile=tile, data=chart, caption=cap)


def render_dashboard(df: pd.DataFrame, plan: Plan, reading: Reading, filters: list[Filter] | None = None) -> Dashboard:
    filters = [f for f in (filters or []) if f.values]
    rows = len(apply_filters(df, filters))
    kpis = [render_tile(df, t, reading, filters) for t in plan.kpis]
    tiles = [render_tile(df, t, reading, filters) for t in plan.tiles] if rows else []
    found = sorted((t for t in tiles if t.caption.tone == "finding"), key=lambda t: -t.caption.score)
    findings = [Finding(tile_id=t.tile.id, brief=first_sentence(t.caption.text), text=t.caption.text) for t in found[:5]]
    primary = find_measure(plan.measures, plan.primary)
    lead = next((t for t in found if t.tile.spec and t.tile.spec.measures[0].id == plan.primary), found[0] if found else None)
    if lead:
        headline = lead.caption.headline
    elif primary and kpis:
        headline = f"{primary.label}: {kpis[0].value}"
    else:
        headline = f"{rows:,} rows"
    return Dashboard(headline=headline, kpis=kpis, tiles=tiles, findings=findings, rows=rows, filters=filters)
