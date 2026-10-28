"""What each chart means, in one or two plain sentences.

A caption claims a pattern ("rose", "highest", "goes with") only when the test
in stats.py backs it; otherwise it says plainly that nothing stands out and is
marked `flat`, so it is never promoted to a key finding.
"""

from __future__ import annotations

import numpy as np

from insight.engine.formatting import format_change, format_measure, format_value, period_label, plural, share
from insight.engine.formatting import lower_label as _lower
from insight.engine.models import Caption, FieldInfo, Measure
from insight.engine.query import OTHER
from insight.engine.stats import SIGNIFICANT, concentration, correlation, standout_means, standout_rates, trend_test

ENDS = 3  # a trend compares the average of its first and last few periods, not two noisy points


def trend(points: list[tuple[str, float]], m: Measure, grain: str) -> Caption:
    if len(points) < 3:
        return Caption(text=f"Only {len(points)} {plural(grain)} of data: too few to call a trend.", tone="flat")
    labels = [period_label(k) for k, _ in points]
    ys = [y for _, y in points]
    slope, p = trend_test(list(ys))
    lo, hi = min(ys), max(ys)
    if p >= SIGNIFICANT:
        return Caption(
            text=(
                f"No clear trend in {_lower(m.label)} across {len(ys)} {plural(grain)}: "
                f"it moved between {format_measure(lo, m)} and {format_measure(hi, m)}."
            ),
            headline=f"{m.label} held steady",
            tone="flat",
            score=0.12,
        )
    k = ENDS if len(ys) >= 4 * ENDS else 1
    start, end = float(np.mean(ys[:k])), float(np.mean(ys[-k:]))
    direction = "rose" if slope > 0 else "fell"
    change = format_change(start, end, m).lstrip("+-")
    span = (
        f"from {labels[0]} to {labels[-1]}" if k == 1 else f"between the first and last {k} {plural(grain)} ({labels[0]} to {labels[-1]})"
    )
    text = f"{m.label} {direction} {change} {span}"
    peak = int(np.argmax(ys))
    if slope > 0 and peak < len(ys) - 1:
        text += f", peaking at {format_measure(ys[peak], m)} in {labels[peak]}"
    rel = abs(end - start) / (abs(start) or 1)
    return Caption(text=text + ".", headline=f"{m.label} {direction} {change}", tone="finding", score=0.5 + 0.5 * min(1.0, rel))


def series_trend(series: list[dict], m: Measure, dim: FieldInfo) -> Caption:
    """Lines by group: which grew fastest, from the first to the last periods."""
    growth = []
    for s in series:
        ys = [y for y in s["y"] if y is not None]
        if len(ys) >= 2 * ENDS:
            a, b = np.mean(ys[:ENDS]), np.mean(ys[-ENDS:])
            if a > 0:
                growth.append((float((b - a) / a), s["name"]))
    if len(growth) < 2:
        return Caption(text=f"{m.label} over time, one line per {_lower(dim.label)}.", tone="neutral", score=0.2)
    growth.sort(reverse=True)
    (g_hi, top), (g_lo, bottom) = growth[0], growth[-1]
    if g_hi - g_lo < 0.15:
        return Caption(
            text=f"Every {_lower(dim.label)} moved roughly together ({g_lo:+.0%} to {g_hi:+.0%}).",
            headline=f"{plural(dim.label)} moved together",
            tone="flat",
            score=0.15,
        )
    return Caption(
        text=f"{top} grew fastest ({g_hi:+.0%}), while {bottom} changed least ({g_lo:+.0%}).",
        headline=f"{top} grew fastest",
        tone="neutral",
        score=0.35,
    )


def _name(value: str, dim: FieldInfo) -> str:
    """A level as a reader says it: 'Yes' in Returned is 'returned orders'-ish, so name the column."""
    if dim.kind == "boolean" or value in ("Yes", "No"):
        return f"where {_lower(dim.label)} is {value}"
    return f"for {value}"


def breakdown(data: list[dict], m: Measure, dim: FieldInfo, samples: dict[str, np.ndarray] | None) -> Caption:
    """A measure split by a category: who leads, and is the gap real?"""
    pts = [(str(r[dim.name]), r[m.id], r) for r in data if r.get(m.id) is not None]
    named = [p for p in pts if p[0] != OTHER]
    if len(named) < 2:
        return Caption(text=f"Only one {_lower(dim.label)} has data.", tone="flat")

    if m.additive and all(v >= 0 for _, v, _ in pts):
        return _additive_split(pts, named, m, dim)
    if m.type == "rate":
        p, idx = standout_rates([(r[m.id] * r["_n"], int(r["_n"])) for _, _, r in named])
    else:
        p, idx = standout_means([samples.get(name, np.array([])) if samples else np.array([]) for name, _, _ in named])
    if len(named) == 2:
        return _two_groups(named, m, dim, p)
    return _average_split(named, m, dim, p, idx)


def _additive_split(pts: list, named: list, m: Measure, dim: FieldInfo) -> Caption:
    whole = sum(v for _, v, _ in pts)
    (top, top_v, _), (second, second_v, _) = named[0], named[1]
    conc = concentration([v for _, v, _ in pts])
    verb = "brings in" if m.format == "currency" else "accounts for"
    if conc >= 0.15 or top_v >= 1.5 * whole / len(pts):
        text = (
            f"{top} {verb} {share(top_v, whole)} of {_lower(m.label)} "
            f"({format_measure(top_v, m)} of {format_measure(whole, m)}), "
            f"followed by {second} at {share(second_v, whole)}."
        )
        return Caption(
            text=text,
            headline=f"{top} {verb} {share(top_v, whole)} of {_lower(m.label)}",
            tone="finding",
            score=0.3 + 0.6 * conc,
        )
    lo = min(named, key=lambda x: x[1])
    return Caption(
        text=(
            f"{m.label} is spread fairly evenly across {len(pts)} {plural(_lower(dim.label))}, "
            f"from {format_measure(lo[1], m)} ({lo[0]}) to {format_measure(top_v, m)} ({top})."
        ),
        headline=f"{m.label} is even across {plural(_lower(dim.label))}",
        tone="flat",
        score=0.1,
    )


def _two_groups(named: list, m: Measure, dim: FieldInfo, p: float) -> Caption:
    (a, va, _), (b, vb, _) = sorted(named, key=lambda x: -x[1])
    if p < SIGNIFICANT:
        rel = abs(va - vb) / (abs(vb) or 1)
        return Caption(
            text=(
                f"{m.label} is {format_measure(va, m)} {_name(a, dim)}, "
                f"against {format_measure(vb, m)} {_name(b, dim)} ({format_change(vb, va, m).lstrip('+')} higher)."
            ),
            headline=f"{m.label} is higher {_name(a, dim)}",
            tone="finding",
            score=0.35 + 0.6 * min(1.0, rel * 2),
        )
    return Caption(
        text=f"{m.label} barely differs {_name(a, dim)} ({format_measure(va, m)}) and {_name(b, dim)} ({format_measure(vb, m)}).",
        headline=f"No real gap by {_lower(dim.label)}",
        tone="flat",
        score=0.08,
    )


def _average_split(named: list, m: Measure, dim: FieldInfo, p: float, idx: int) -> Caption:
    hi = max(named, key=lambda x: x[1])
    lo = min(named, key=lambda x: x[1])
    groups = plural(_lower(dim.label))
    if p < SIGNIFICANT and idx >= 0:
        name, value, _ = named[idx]
        rest = [r for i, r in enumerate(named) if i != idx]
        weights = np.array([r[2]["_n"] for r in rest], dtype=float)
        rest_v = float(np.average([r[1] for r in rest], weights=weights)) if weights.sum() else 0.0
        word = "highest" if value >= rest_v else "lowest"
        text = (
            f"{name} has the {word} {_lower(m.label)} at {format_measure(value, m)}, "
            f"against {format_measure(rest_v, m)} across the other {groups}."
        )
        rel = abs(value - rest_v) / (abs(rest_v) or 1)
        return Caption(
            text=text,
            headline=f"{name} has the {word} {_lower(m.label)}",
            tone="finding",
            score=0.35 + 0.6 * min(1.0, rel * 2),
        )
    return Caption(
        text=(
            f"{m.label} ranges from {format_measure(lo[1], m)} ({lo[0]}) to {format_measure(hi[1], m)} ({hi[0]}), "
            f"but the gap is within what chance alone would give."
        ),
        headline=f"No {_lower(dim.label)} stands out",
        tone="flat",
        score=0.08,
    )


def ranking(data: list[dict], m: Measure, dim: FieldInfo, whole: float | None) -> Caption:
    pts = [(str(r[dim.name]), r[m.id]) for r in data if r.get(m.id) is not None]
    if not pts:
        return Caption(text="Nothing to rank.", tone="flat")
    top, v = pts[0]
    text = f"{top} leads at {format_measure(v, m)}"
    if m.additive and whole:
        listed = sum(x for _, x in pts)
        text += f"; these {len(pts)} make up {share(listed, whole)} of the total"
    return Caption(text=text + ".", headline=f"{top} leads on {_lower(m.label)}", tone="neutral", score=0.3)


def relationship(x: np.ndarray, y: np.ndarray, fx: FieldInfo, fy: FieldInfo) -> Caption:
    r, p = correlation(x, y)
    if p < 0.01 and abs(r) >= 0.3:
        strength = "strongly" if abs(r) >= 0.7 else "clearly" if abs(r) >= 0.5 else "moderately"
        direction = "higher" if r > 0 else "lower"
        return Caption(
            text=f"More {_lower(fx.label)} {strength} goes with {direction} {_lower(fy.label)} (correlation {r:.2f}).",
            headline=f"More {_lower(fx.label)}, {direction} {_lower(fy.label)}",
            tone="finding",
            score=min(1.0, abs(r)),
        )
    return Caption(
        text=f"No clear link between {_lower(fx.label)} and {_lower(fy.label)} (correlation {r:.2f}).",
        headline="No clear link",
        tone="flat",
        score=0.05,
    )


def distribution(values: np.ndarray, f: FieldInfo) -> Caption:
    if len(values) < 10:
        return Caption(text="Too few values to describe the spread.", tone="flat")
    p25, med, p75 = np.percentile(values, [25, 50, 75])
    fmt = lambda v: format_value(v, f.format, f.percent_scale)
    text = f"The middle half of {_lower(f.label)} runs from {fmt(p25)} to {fmt(p75)}, around a median of {fmt(med)}."
    sd = float(np.std(values))
    if sd and (np.mean(values) - med) / sd > 0.2:
        text += f" A long tail reaches {fmt(values.max())}."
    return Caption(text=text, headline=f"Typical {_lower(f.label)}: {fmt(med)}", tone="neutral", score=0.2)
