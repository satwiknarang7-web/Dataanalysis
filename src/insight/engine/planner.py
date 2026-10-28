"""Decide what the board shows, the way an analyst would.

List the analyses worth considering for this shape of table, compute each one
over every row, score it by what the data actually shows, and keep a balanced
set: the trend first, then the splits that say the most, never two charts
that tell the same story.
"""

from __future__ import annotations

from collections import Counter
from itertools import combinations

import pandas as pd

from insight.engine.formatting import lower_label
from insight.engine.measures import build_measures, primary_measure
from insight.engine.models import Dim, FieldInfo, Measure, Plan, Reading, RenderedTile, Spec, Tile
from insight.engine.render import choose_grain, render_tile

BUDGET = 8
MIN_TILES = 4
MAX_SPLIT_LEVELS = 30
BREAKDOWN_LIMIT = 10


def build_plan(df: pd.DataFrame, reading: Reading) -> Plan:
    measures = build_measures(reading)
    primary = primary_measure(measures)
    candidates = [render_tile(df, t, reading) for t in _candidates(df, reading, measures, primary)]
    return Plan(
        primary=primary.id,
        kpis=_kpis(reading, measures, primary),
        tiles=_select(candidates),
        measures=measures,
    )


def _split_dims(reading: Reading) -> list[FieldInfo]:
    return [f for f in reading.fields if f.role == "dimension" and 2 <= f.distinct <= MAX_SPLIT_LEVELS]


def _rank_dims(reading: Reading) -> list[FieldInfo]:
    """Things that recur (products, customers), not a value on nearly every row."""
    rows = reading.row_count
    return [
        f
        for f in reading.fields
        if f.role in ("dimension", "id") and f.distinct > MAX_SPLIT_LEVELS and f.distinct <= rows / 2 and f.kind == "text"
    ]


def breakdown_tile(m: Measure, d: FieldInfo, filters=None) -> Tile:
    ordered = bool(d.order) or d.ordinal
    long_labels = any(len(v) > 14 for v, _ in d.top)
    viz = "bar" if ordered else "hbar" if (d.distinct > 6 or long_labels) else "bar"
    if m.additive and not ordered and d.distinct <= 4:
        viz = "donut"
    spec = Spec(
        measures=[m],
        by=[Dim(field=d.name)],
        filters=filters or [],
        sort="order" if d.order else "label" if d.ordinal else "value",
        order=d.order,
        limit=None if ordered else BREAKDOWN_LIMIT,
        other=not ordered and d.distinct > BREAKDOWN_LIMIT + 1,
    )
    return Tile(id=f"breakdown:{m.id}:{d.name}", kind="breakdown", title=f"{m.label} by {lower_label(d.label)}", viz=viz, spec=spec)


def trend_tile(df: pd.DataFrame, m: Measure, time: FieldInfo, series: FieldInfo | None = None, filters=None) -> Tile:
    grain = None if time.time_unit == "year" else choose_grain(df, time.name)
    by = [Dim(field=time.name, grain=grain)]
    if series:
        by.append(Dim(field=series.name))
    title = f"{m.label}{f' by {lower_label(series.label)}' if series else ''} over time"
    viz = "area" if m.additive and not series else "line"
    return Tile(
        id=f"trend:{m.id}:{time.name}{f':{series.name}' if series else ''}",
        kind="trend",
        title=title,
        viz=viz,
        spec=Spec(measures=[m], by=by, sort="label", filters=filters or []),
    )


def ranking_tile(m: Measure, d: FieldInfo, limit: int = 10, descending: bool = True, filters=None) -> Tile:
    word = "Top" if descending else "Bottom"
    return Tile(
        id=f"table:{m.id}:{d.name}:{limit}:{int(descending)}",
        kind="table",
        title=f"{word} {limit} {lower_label(d.label)} by {lower_label(m.label)}",
        viz="table",
        spec=Spec(measures=[m], by=[Dim(field=d.name)], limit=limit, descending=descending, filters=filters or []),
    )


def relationship_tile(fx: FieldInfo, fy: FieldInfo, filters=None) -> Tile:
    return Tile(
        id=f"relationship:{fx.name}:{fy.name}",
        kind="relationship",
        title=f"{fy.label} against {lower_label(fx.label)}",
        viz="scatter",
        x=fx.name,
        y=fy.name,
        filters=filters or [],
    )


def distribution_tile(f: FieldInfo, filters=None) -> Tile:
    return Tile(
        id=f"distribution:{f.name}",
        kind="distribution",
        title=f"Spread of {lower_label(f.label)}",
        viz="histogram",
        field=f.name,
        filters=filters or [],
    )


def _kpis(reading: Reading, measures: list[Measure], primary: Measure) -> list[Tile]:
    chosen = [primary]
    for test in (
        lambda m: m.type == "agg" and m.agg == "sum",
        lambda m: m.type == "rate",
        lambda m: m.type == "count",
        lambda m: m.type == "agg" and m.agg == "avg",
    ):
        for m in measures:
            if len(chosen) < 4 and m not in chosen and test(m):
                chosen.append(m)
                break
    return [Tile(id=f"kpi:{m.id}", kind="kpi", title=m.label, viz="kpi", spec=Spec(measures=[m])) for m in chosen]


def _candidates(df: pd.DataFrame, reading: Reading, measures: list[Measure], primary: Measure) -> list[Tile]:
    tiles: list[Tile] = []
    # Outcome rates (churn, returns) are what a table is usually watched for: they come first.
    rates = [m for m in measures if m is not primary and m.type == "rate"]
    aggs = [m for m in measures if m is not primary and m.type == "agg"]
    focus = [primary, *(rates + aggs)[:3]]
    dims = _split_dims(reading)
    time = reading.field(reading.time) if reading.time else None

    if time:
        tiles.extend(trend_tile(df, m, time) for m in focus[:2])
        small = next((d for d in dims if d.distinct <= 6 and d.name != primary.field), None)
        if small:
            tiles.append(trend_tile(df, primary, time, small))

    for m in focus:
        for d in dims:
            if m.type == "rate" and d.name == m.field:
                continue
            if m.additive and d.outcome:
                continue  # "Revenue by returned" restates the rate; the rate's own tiles say it better
            tiles.append(breakdown_tile(m, d))

    for d in _rank_dims(reading)[:2]:
        tiles.append(ranking_tile(primary, d))

    numeric = [f for f in reading.by_role("measure")][:6]
    for fx, fy in combinations(numeric, 2):
        tiles.append(relationship_tile(fx, fy))

    spread = next((f for f in numeric if f.agg == "avg"), numeric[0] if numeric else None)
    if spread:
        tiles.append(distribution_tile(spread))
    return tiles


def _select(cands: list[RenderedTile]) -> list[Tile]:
    """A balanced board: the strongest tiles, at most two per split, one per story."""
    chosen: list[RenderedTile] = []
    per_dim: Counter[str] = Counter()
    per_kind: Counter[str] = Counter()

    def dim_of(t: Tile) -> str:
        if t.spec and t.spec.by:
            return t.spec.by[-1].field if t.kind == "trend" else t.spec.by[0].field
        return t.x or t.field or ""

    def take(c: RenderedTile) -> None:
        chosen.append(c)
        per_dim[dim_of(c.tile)] += 1
        per_kind[c.tile.kind] += 1

    trends = [c for c in cands if c.tile.kind == "trend"]
    if trends:
        take(trends[0])  # the primary measure over time always leads
    for c in sorted(cands, key=lambda c: -c.caption.score):
        if len(chosen) >= BUDGET or c in chosen:
            continue
        weak = c.caption.tone == "flat" or c.caption.score < 0.12
        if weak and len(chosen) >= MIN_TILES:
            continue
        if per_dim[dim_of(c.tile)] >= 2 or (c.tile.kind in ("relationship", "distribution", "table") and per_kind[c.tile.kind] >= 2):
            continue
        # A twin: the same split already shown for a total that moves with it
        # (revenue and units by region, led by the same region).
        if c.tile.kind == "breakdown" and any(_twins(o, c) for o in chosen):
            continue
        take(c)

    order = {"trend": 0, "breakdown": 1, "table": 2, "relationship": 3, "distribution": 4}
    # The primary measure's own trend always leads; split trends follow it.
    lead = chosen[0] if chosen else None
    chosen.sort(key=lambda c: (order[c.tile.kind], c is not lead, -c.caption.score))
    return [c.tile for c in chosen]


def _twins(a: RenderedTile, b: RenderedTile) -> bool:
    if a.tile.kind != "breakdown" or b.tile.kind != "breakdown":
        return False
    sa, sb = a.tile.spec, b.tile.spec
    assert sa is not None and sb is not None
    if sa.by[0].field != sb.by[0].field or not (sa.measures[0].additive and sb.measures[0].additive):
        return False
    return bool(a.data["labels"]) and a.data["labels"][:2] == b.data["labels"][:2]
