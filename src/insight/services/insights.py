"""Dashboards and answers, computed once and served from the cache.

The plan (what to show) is computed once per dataset. Each board (the plan
under a set of filters) and each answer is cached under the dataset's content
hash, so the second viewer of any board gets it without touching pandas.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from insight.cache import keys
from insight.engine.ask import AskError, example_questions, interpret
from insight.engine.cleaning import CleaningReport
from insight.engine.models import Dashboard, Filter, Plan, Reading, RenderedTile
from insight.engine.planner import build_plan
from insight.engine.query import dim_labels
from insight.engine.render import render_dashboard, render_tile
from insight.state import AppState

MAX_FILTER_VALUES = 40
MAX_FILTERS = 4


@dataclass
class Source:
    """The rows under a board, wherever they came from (a dataset or a demo sample)."""

    name: str
    df: pd.DataFrame
    reading: Reading
    report: CleaningReport
    content_hash: str


@dataclass
class FilterOption:
    field: str
    label: str
    grain: str | None
    values: list[str]

    @property
    def param(self) -> str:
        return f"f.{self.field}" + (f"@{self.grain}" if self.grain else "")


async def plan_for(state: AppState, src: Source) -> Plan:
    async def compute() -> dict:
        plan = await state.run_cpu(build_plan, src.df, src.reading)
        return plan.model_dump(mode="json")

    return Plan.model_validate(await state.cache.get_or_compute(keys.plan(src.content_hash), compute))


async def dashboard_for(state: AppState, src: Source, filters: list[Filter]) -> Dashboard:
    plan = await plan_for(state, src)
    payload = [f.model_dump(mode="json") for f in filters]

    async def compute() -> dict:
        board = await state.run_cpu(render_dashboard, src.df, plan, src.reading, filters)
        return board.model_dump(mode="json")

    return Dashboard.model_validate(await state.cache.get_or_compute(keys.dashboard(src.content_hash, payload), compute))


async def answer(state: AppState, src: Source, question: str, filters: list[Filter]) -> RenderedTile:
    """Raises AskError with a reader-facing message when the question can't be read."""
    plan = await plan_for(state, src)
    payload = [f.model_dump(mode="json") for f in filters]

    def compute_sync() -> dict:
        tile, _ = interpret(question, src.reading, plan.measures, src.df)
        return render_tile(src.df, tile, src.reading, filters).model_dump(mode="json")

    async def compute() -> dict:
        try:
            return await state.run_cpu(compute_sync)
        except AskError as e:
            return {"error": str(e)}

    result = await state.cache.get_or_compute(keys.answer(src.content_hash, question, payload), compute)
    if "error" in result:
        raise AskError(result["error"])
    return RenderedTile.model_validate(result)


async def examples_for(state: AppState, src: Source) -> list[str]:
    plan = await plan_for(state, src)
    return example_questions(src.reading, plan.measures)


def filter_options(src: Source) -> list[FilterOption]:
    """The few splits worth filtering a whole board by, and a year picker."""
    out: list[FilterOption] = []
    r = src.reading
    if r.time:
        tf = r.field(r.time)
        grain = None if tf.time_unit else "year"
        years = sorted(dim_labels(src.df, Filter(field=tf.name, values=[], grain=grain)).dropna().unique().tolist())
        if 1 < len(years) <= MAX_FILTER_VALUES:
            out.append(FilterOption(field=tf.name, label="Year", grain=grain, values=[str(y) for y in years]))
    dims = [f for f in r.fields if f.role == "dimension" and 2 <= f.distinct <= MAX_FILTER_VALUES]
    dims.sort(key=lambda f: (f.outcome is not None, f.distinct))
    for f in dims[: MAX_FILTERS - len(out)]:
        values = f.order or sorted(v for v in src.df[f.name].dropna().astype(str).unique())
        out.append(FilterOption(field=f.name, label=f.label, grain=None, values=list(values)))
    return out


def parse_filters(params: list[tuple[str, str]], options: list[FilterOption]) -> list[Filter]:
    """`f.region=North&f.region=South&f.order_date@year=2025` → filters, only on offered fields."""
    allowed = {o.param: o for o in options}
    chosen: dict[str, list[str]] = {}
    for key, value in params:
        if key in allowed and value and value in allowed[key].values:
            chosen.setdefault(key, []).append(value)
    return [Filter(field=allowed[k].field, grain=allowed[k].grain, values=v) for k, v in chosen.items()]  # type: ignore[arg-type]
