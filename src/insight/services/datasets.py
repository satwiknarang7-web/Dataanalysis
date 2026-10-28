"""Datasets: read a file once, clean it, store it, and load it back fast.

Read path: frame cache (memory) → Parquet blob (Postgres) → parse.
Write path: read → clean → Parquet → hash → store blob once → dataset row.
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import pandas as pd
from sqlalchemy import exists, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from insight.db.models import Analysis, AnalysisShare, Dataset, DatasetBlob, utcnow
from insight.engine.cleaning import CleaningReport, clean
from insight.engine.fields import read_dataset
from insight.engine.models import Reading
from insight.ingest.readers import read_table
from insight.services.auth import CurrentUser
from insight.services.plans import PlanLimit, plan_of
from insight.state import AppState

SAMPLES_DIR = Path(__file__).resolve().parents[1] / "samples"


@dataclass(frozen=True)
class Sample:
    key: str
    name: str
    blurb: str
    file: str


SAMPLES = {
    s.key: s
    for s in (
        Sample("retail", "Retail sales", "4,000 orders across regions, channels and categories, with returns.", "retail_sales.csv"),
        Sample("saas", "SaaS customers", "2,500 subscribers: plans, MRR, support tickets, NPS and churn.", "saas_customers.csv"),
        Sample("people", "People analytics", "1,200 employees: departments, levels, pay, satisfaction and attrition.", "people.csv"),
    )
}


class NotFound(LookupError):
    pass


@dataclass
class Prepared:
    df: pd.DataFrame
    report: CleaningReport
    reading: Reading
    parquet: bytes
    content_hash: str


def prepare(content: bytes, filename: str) -> Prepared:
    """The CPU-bound half of an upload; runs on a worker thread."""
    df, report = clean(read_table(content, filename))
    reading = read_dataset(df, report.percent_columns())
    buf = io.BytesIO()
    df.to_parquet(buf, index=False, compression="zstd")
    parquet = buf.getvalue()
    return Prepared(df, report, reading, parquet, hashlib.sha256(parquet).hexdigest())


async def ingest(state: AppState, db: AsyncSession, user: CurrentUser, content: bytes, filename: str, source: str, label: str) -> Dataset:
    plan = plan_of(user.plan)
    if len(content) > plan.max_upload_mb * 1024 * 1024:
        raise PlanLimit(f"Files up to {plan.max_upload_mb} MB are included on {plan.name}. Upgrade to Pro for 100 MB.")
    if plan.max_datasets is not None:
        count = await db.scalar(select(func.count()).select_from(Dataset).where(Dataset.owner_id == user.id))
        if (count or 0) >= plan.max_datasets:
            raise PlanLimit(f"{plan.name} holds {plan.max_datasets} datasets. Delete one, or upgrade to Pro for unlimited.")

    prepared = await state.run_cpu(prepare, content, filename)
    await _store_blob(db, prepared)
    dataset = Dataset(
        owner_id=user.id,
        name=_display_name(filename),
        source=source,
        source_label=label[:500],
        content_hash=prepared.content_hash,
        row_count=len(prepared.df),
        column_count=prepared.df.shape[1],
        size_bytes=len(content),
        report=prepared.report.model_dump(mode="json"),
        reading=prepared.reading.model_dump(mode="json"),
    )
    db.add(dataset)
    await db.commit()
    state.frames.put(prepared.content_hash, prepared.df)
    return dataset


async def _store_blob(db: AsyncSession, prepared: Prepared) -> None:
    """Store the rows once per content hash. Idempotent, so two identical uploads at once cannot collide."""
    insert = pg_insert if db.bind.dialect.name == "postgresql" else sqlite_insert
    stmt = insert(DatasetBlob).values(
        content_hash=prepared.content_hash, parquet=prepared.parquet, size_bytes=len(prepared.parquet), created_at=utcnow()
    )
    await db.execute(stmt.on_conflict_do_nothing(index_elements=["content_hash"]))


async def ingest_sample(state: AppState, db: AsyncSession, user: CurrentUser, key: str) -> Dataset:
    sample = SAMPLES.get(key)
    if sample is None:
        raise NotFound(key)
    content = (SAMPLES_DIR / sample.file).read_bytes()
    dataset = await ingest(state, db, user, content, sample.file, "sample", sample.name)
    dataset.name = sample.name
    await db.commit()
    return dataset


async def list_datasets(db: AsyncSession, user: CurrentUser) -> list[Dataset]:
    rows = await db.scalars(select(Dataset).where(Dataset.owner_id == user.id).order_by(Dataset.created_at.desc()))
    return list(rows)


async def get_dataset(db: AsyncSession, user: CurrentUser, dataset_id: UUID) -> Dataset:
    """A dataset the user owns, or one behind an analysis shared with them."""
    dataset = await db.get(Dataset, dataset_id)
    if dataset is None:
        raise NotFound(str(dataset_id))
    if dataset.owner_id == user.id:
        return dataset
    shared = await db.scalar(
        select(
            exists().where(
                Analysis.dataset_id == dataset_id,
                AnalysisShare.analysis_id == Analysis.id,
                AnalysisShare.email == user.email,
            )
        )
    )
    if not shared:
        raise NotFound(str(dataset_id))  # not "forbidden": do not reveal that it exists
    return dataset


async def load_frame(state: AppState, db: AsyncSession, dataset: Dataset) -> pd.DataFrame:
    df = state.frames.get(dataset.content_hash)
    if df is not None:
        return df
    blob = await db.get(DatasetBlob, dataset.content_hash)
    if blob is None:
        raise NotFound(dataset.content_hash)
    df = await state.run_cpu(pd.read_parquet, io.BytesIO(blob.parquet))
    state.frames.put(dataset.content_hash, df)
    return df


async def load_sample_frame(state: AppState, key: str) -> tuple[pd.DataFrame, CleaningReport, Reading, str]:
    """A sample for the public demo: computed once per process, never stored."""
    sample = SAMPLES.get(key)
    if sample is None:
        raise NotFound(key)
    content = (SAMPLES_DIR / sample.file).read_bytes()
    cache_key = "sample:" + hashlib.sha256(content).hexdigest()
    cached = await state.cache.get_json(cache_key)
    df = state.frames.get(cache_key)
    if cached is None or df is None:
        prepared = await state.run_cpu(prepare, content, sample.file)
        df = prepared.df
        state.frames.put(cache_key, df)
        cached = {"report": prepared.report.model_dump(mode="json"), "reading": prepared.reading.model_dump(mode="json")}
        await state.cache.set_json(cache_key, cached)
    return df, CleaningReport.model_validate(cached["report"]), Reading.model_validate(cached["reading"]), cache_key


async def delete_dataset(db: AsyncSession, user: CurrentUser, dataset_id: UUID) -> None:
    dataset = await db.get(Dataset, dataset_id)
    if dataset is None or dataset.owner_id != user.id:
        raise NotFound(str(dataset_id))
    content_hash = dataset.content_hash
    await db.delete(dataset)
    await db.flush()
    still_used = await db.scalar(select(exists().where(Dataset.content_hash == content_hash)))
    if not still_used:
        blob = await db.get(DatasetBlob, content_hash)
        if blob is not None:
            await db.delete(blob)
    await db.commit()


def _display_name(filename: str) -> str:
    """`q3_sales-final.csv` → "Q3 sales final"."""
    name = " ".join(Path(filename).stem.replace("_", " ").replace("-", " ").split())[:200]
    return name[:1].upper() + name[1:] if name else "Untitled"


def reading_of(dataset: Dataset) -> Reading:
    return Reading.model_validate(dataset.reading)


def report_of(dataset: Dataset) -> CleaningReport:
    return CleaningReport.model_validate(dataset.report)
