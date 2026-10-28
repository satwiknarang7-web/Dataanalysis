"""What each plan includes: the one definition, checked on every request."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Plan:
    key: str
    name: str
    price: str
    tagline: str
    max_upload_mb: int
    max_datasets: int | None
    max_saved: int | None
    sharing: bool
    sql_console: bool
    features: tuple[str, ...]


FREE = Plan(
    key="free",
    name="Starter",
    price="$0",
    tagline="For trying Insight on your own data.",
    max_upload_mb=10,
    max_datasets=5,
    max_saved=3,
    sharing=False,
    sql_console=False,
    features=(
        "Automatic dashboards with plain-English findings",
        "Cleaning report for every file",
        "Ask questions in plain words",
        "5 datasets, files up to 10 MB",
        "3 saved analyses",
    ),
)

PRO = Plan(
    key="pro",
    name="Pro",
    price="$29",
    tagline="For analysts and teams who report every week.",
    max_upload_mb=100,
    max_datasets=None,
    max_saved=None,
    sharing=True,
    sql_console=True,
    features=(
        "Everything in Starter",
        "Unlimited datasets, files up to 100 MB",
        "Unlimited saved analyses",
        "Share analyses with your team",
        "SQL console over every dataset",
        "Priority support",
    ),
)

PLANS = {p.key: p for p in (FREE, PRO)}


def plan_of(key: str | None) -> Plan:
    return PLANS.get(key or "free", FREE)


class PlanLimit(Exception):
    """An action the account's plan does not include; the message says how to get it."""
