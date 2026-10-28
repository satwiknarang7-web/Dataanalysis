"""The numbers a business talks about, named from the reading of a table."""

from __future__ import annotations

import re

from insight.engine.formatting import label_of, lower_label, name_words, plural
from insight.engine.models import FieldInfo, Measure, Reading

# A rise in these is bad news: the KPI's change is coloured the other way.
BAD_UP = re.compile(
    r"\b(cost|costs|expense|expenses|spend|refund|refunds|return|returns|returned|churn|churned|cancel\w*|"
    r"defect\w*|error\w*|fail\w*|late|delay\w*|latency|tickets|complaints|discount|attrition|left|exit\w*|fraud|debt|loss|losses)\b"
)


def _good_up(*names: str) -> bool:
    return not any(BAD_UP.search(name_words(n)) for n in names if n)


COUNT_ID = "count"


def count_measure(reading: Reading) -> Measure:
    return Measure(id=COUNT_ID, label=plural(label_of(reading.row_noun)), type="count", additive=True)


def agg_measure(f: FieldInfo, agg: str | None = None) -> Measure:
    agg = agg or f.agg or "sum"
    prefix = {"sum": "", "avg": "Average ", "median": "Median ", "min": "Lowest ", "max": "Highest "}[agg]
    label = f.label if not prefix else prefix + lower_label(f.label)
    return Measure(
        id=f"{agg}:{f.name}",
        label=label,
        type="agg",
        field=f.name,
        agg=agg,  # type: ignore[arg-type]
        format=f.format,
        percent_scale=f.percent_scale,
        additive=agg == "sum",
        good_up=_good_up(f.name),
    )


def rate_measure(f: FieldInfo) -> Measure:
    event = f.outcome or "Yes"
    if f.kind == "boolean" or event.lower() in {"yes", "true", "y"}:
        label = f"{f.label} rate"
    else:
        label = f"{event} rate"
    return Measure(
        id=f"rate:{f.name}={event}", label=label, type="rate", field=f.name, event=event, format="percent", good_up=_good_up(f.name, event)
    )


def distinct_measure(f: FieldInfo) -> Measure:
    return Measure(id=f"distinct:{f.name}", label=f"Distinct {lower_label(f.label)}", type="distinct", field=f.name)


def build_measures(reading: Reading) -> list[Measure]:
    """Every measure the planner may chart, most useful first."""
    measures: list[Measure] = []
    numeric = reading.by_role("measure")
    # Money that adds up first, then other sums, then averages.
    numeric.sort(key=lambda f: (f.agg != "sum", f.format != "currency"))
    measures.extend(agg_measure(f) for f in numeric)
    measures.extend(rate_measure(f) for f in reading.fields if f.role == "dimension" and f.outcome)
    measures.append(count_measure(reading))
    return measures


def primary_measure(measures: list[Measure]) -> Measure:
    """What the board is built around: money that adds up, else any sum, a rate, the count."""
    for test in (
        lambda m: m.type == "agg" and m.agg == "sum" and m.format == "currency",
        lambda m: m.type == "agg" and m.agg == "sum",
        lambda m: m.type == "rate",
        lambda m: m.type == "agg",
    ):
        found = next((m for m in measures if test(m)), None)
        if found:
            return found
    return next(m for m in measures if m.id == COUNT_ID)


def find_measure(measures: list[Measure], measure_id: str) -> Measure | None:
    return next((m for m in measures if m.id == measure_id), None)
