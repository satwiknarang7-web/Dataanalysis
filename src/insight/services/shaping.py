"""Shaping a dataset: preview a step on the real rows, then keep it.

The cleaned upload is kept as the base. Every change to the steps re-runs
them all from the base and stores the result as a new content-addressed
table; the dataset then points at it. Boards, answers and caches are keyed by
that hash, so they follow the shaped rows with nothing to invalidate.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

import pandas as pd
from sqlalchemy.ext.asyncio import AsyncSession

from insight.db.models import Dataset
from insight.engine.fields import read_dataset
from insight.engine.transforms import MAX_STEPS, STEPS, StepError, StepResult, apply_steps, parse_step
from insight.ingest.parquet import to_parquet
from insight.services import datasets as datasets_service
from insight.services.auth import CurrentUser
from insight.services.datasets import NotFound
from insight.state import AppState

PREVIEW_ROWS = 12


@dataclass
class Preview:
    columns: list[str]
    rows: list[list]
    total_rows: int
    results: list[StepResult]
    before_rows: int
    before_columns: int
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    error: str | None = None


def run_preview(base: pd.DataFrame, steps: list[dict], pending: dict | None) -> Preview:
    """The table after the saved steps and, if given, one more; with what that step changed."""
    parsed = STEPS.validate_python(steps)
    before, results = apply_steps(base, parsed)
    if pending is None:
        return Preview(list(before.columns), _rows(before), len(before), results, len(before), before.shape[1])
    step = parse_step(pending)
    after, last = apply_steps(before, [step])
    r = last[0]
    return Preview(list(after.columns), _rows(after), len(after), results + last, len(before), before.shape[1], r.added, r.removed, r.error)


def _rows(df: pd.DataFrame) -> list[list]:
    out = []
    for row in df.head(PREVIEW_ROWS).itertuples(index=False, name=None):
        out.append(["" if v is None or (not isinstance(v, str) and pd.isna(v)) else _cell(v) for v in row])
    return out


def _cell(v) -> str:
    if isinstance(v, pd.Timestamp):
        return v.strftime("%Y-%m-%d") if not (v.hour or v.minute) else v.strftime("%Y-%m-%d %H:%M")
    if isinstance(v, float):
        return f"{v:,.4f}".rstrip("0").rstrip(".")
    return str(v)


async def base_frame(state: AppState, db: AsyncSession, dataset: Dataset) -> pd.DataFrame:
    return await datasets_service.load_blob_frame(state, db, dataset.base_hash or dataset.content_hash)


async def owned(db: AsyncSession, user: CurrentUser, dataset: Dataset) -> Dataset:
    if dataset.owner_id != user.id:
        raise NotFound(str(dataset.id))  # shared viewers can read a board, not reshape its rows
    return dataset


def _shape(base: pd.DataFrame, steps: list[dict], percent: set[str]):
    shaped, results = apply_steps(base, STEPS.validate_python(steps))
    parquet = to_parquet(shaped)
    return shaped, results, parquet, hashlib.sha256(parquet).hexdigest(), read_dataset(shaped, percent & set(shaped.columns))


async def save_steps(state: AppState, db: AsyncSession, user: CurrentUser, dataset: Dataset, steps: list[dict]) -> list[StepResult]:
    """Replace the dataset's steps, re-run them from the base and point the dataset at the result."""
    await owned(db, user, dataset)
    if len(steps) > MAX_STEPS:
        raise StepError(f"A table can have up to {MAX_STEPS} steps.")
    steps = [s.model_dump(mode="json") for s in STEPS.validate_python(steps)]  # normalised, and refused if malformed
    base = await base_frame(state, db, dataset)
    base_hash = dataset.base_hash or dataset.content_hash
    percent = datasets_service.report_of(dataset).percent_columns()
    shaped, results, parquet, content_hash, reading = await state.run_cpu(_shape, base, steps, percent)
    if shaped.empty or shaped.shape[1] == 0:
        raise StepError("Those steps leave no rows. Switch one off or change it.")

    old = dataset.content_hash
    if steps:
        await datasets_service.store_blob(db, content_hash, parquet)
        dataset.base_hash, dataset.content_hash = base_hash, content_hash
    else:  # no steps: back to the cleaned upload itself
        dataset.base_hash, dataset.content_hash = None, base_hash
        reading = read_dataset(base, percent)
        shaped = base
    dataset.steps = steps
    dataset.reading = reading.model_dump(mode="json")
    dataset.row_count, dataset.column_count = len(shaped), shaped.shape[1]
    await db.flush()
    await datasets_service.release_blobs(db, {old} - {dataset.content_hash, base_hash})
    await db.commit()
    state.frames.put(dataset.content_hash, shaped)
    return results


def edited(steps: list[dict], action: str, index: int) -> list[dict]:
    """The step list after toggling, removing or moving one step."""
    steps = [dict(s) for s in steps]
    if not 0 <= index < len(steps):
        raise StepError("That step is no longer there.")
    if action == "toggle":
        steps[index]["enabled"] = not steps[index].get("enabled", True)
    elif action == "delete":
        steps.pop(index)
    elif action == "up" and index > 0:
        steps[index - 1], steps[index] = steps[index], steps[index - 1]
    elif action == "down" and index < len(steps) - 1:
        steps[index + 1], steps[index] = steps[index], steps[index + 1]
    return steps
