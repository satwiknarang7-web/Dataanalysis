"""Saved analyses, and sharing them by email."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from insight.db.models import Analysis, AnalysisShare, Dataset
from insight.engine.models import Filter
from insight.services.auth import EMAIL, CurrentUser, normalise_email
from insight.services.datasets import NotFound
from insight.services.plans import PlanLimit, plan_of


async def save(db: AsyncSession, user: CurrentUser, dataset: Dataset, title: str, filters: list[Filter]) -> Analysis:
    plan = plan_of(user.plan)
    if plan.max_saved is not None:
        count = await db.scalar(select(func.count()).select_from(Analysis).where(Analysis.owner_id == user.id))
        if (count or 0) >= plan.max_saved:
            raise PlanLimit(f"{plan.name} saves {plan.max_saved} analyses. Upgrade to Pro to save as many as you need.")
    analysis = Analysis(
        owner_id=user.id,
        dataset_id=dataset.id,
        title=(title.strip() or dataset.name)[:200],
        filters=[f.model_dump(mode="json") for f in filters],
    )
    db.add(analysis)
    await db.commit()
    return analysis


async def update_filters(db: AsyncSession, user: CurrentUser, analysis_id: UUID, filters: list[Filter]) -> Analysis:
    analysis = await _owned(db, user, analysis_id)
    analysis.filters = [f.model_dump(mode="json") for f in filters]
    await db.commit()
    return analysis


async def get(db: AsyncSession, user: CurrentUser, analysis_id: UUID) -> tuple[Analysis, bool]:
    """An analysis the user owns or that was shared with them, and whether they own it."""
    analysis = await db.get(Analysis, analysis_id)
    if analysis is None:
        raise NotFound(str(analysis_id))
    if analysis.owner_id == user.id:
        return analysis, True
    shared = await db.scalar(select(AnalysisShare).where(AnalysisShare.analysis_id == analysis_id, AnalysisShare.email == user.email))
    if shared is None:
        raise NotFound(str(analysis_id))
    return analysis, False


async def mine(db: AsyncSession, user: CurrentUser) -> list[Analysis]:
    rows = await db.scalars(select(Analysis).where(Analysis.owner_id == user.id).order_by(Analysis.updated_at.desc()))
    return list(rows)


async def shared_with_me(db: AsyncSession, user: CurrentUser) -> list[Analysis]:
    rows = await db.scalars(
        select(Analysis)
        .join(AnalysisShare, AnalysisShare.analysis_id == Analysis.id)
        .where(AnalysisShare.email == user.email)
        .order_by(Analysis.updated_at.desc())
    )
    return list(rows)


async def shares(db: AsyncSession, analysis_id: UUID) -> list[str]:
    rows = await db.scalars(select(AnalysisShare.email).where(AnalysisShare.analysis_id == analysis_id).order_by(AnalysisShare.created_at))
    return list(rows)


async def share(db: AsyncSession, user: CurrentUser, analysis_id: UUID, email: str) -> None:
    if not plan_of(user.plan).sharing:
        raise PlanLimit("Sharing is part of Pro. Upgrade to share analyses with your team.")
    analysis = await _owned(db, user, analysis_id)
    email = normalise_email(email)
    if not EMAIL.match(email):
        raise ValueError("Enter a valid email address.")
    if email == user.email:
        raise ValueError("That's your own address.")
    db.add(AnalysisShare(analysis_id=analysis.id, email=email))
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()  # already shared with them: nothing to do


async def unshare(db: AsyncSession, user: CurrentUser, analysis_id: UUID, email: str) -> None:
    await _owned(db, user, analysis_id)
    row = await db.scalar(
        select(AnalysisShare).where(AnalysisShare.analysis_id == analysis_id, AnalysisShare.email == normalise_email(email))
    )
    if row is not None:
        await db.delete(row)
        await db.commit()


async def delete(db: AsyncSession, user: CurrentUser, analysis_id: UUID) -> None:
    await db.delete(await _owned(db, user, analysis_id))
    await db.commit()


async def _owned(db: AsyncSession, user: CurrentUser, analysis_id: UUID) -> Analysis:
    analysis = await db.get(Analysis, analysis_id)
    if analysis is None or analysis.owner_id != user.id:
        raise NotFound(str(analysis_id))
    return analysis
