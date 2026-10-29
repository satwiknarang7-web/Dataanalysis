"""Generate the sample datasets offered on the Get data page.

Deterministic (seeded), with real patterns planted in them so a first-time
visitor sees what the product finds:

    python scripts/make_samples.py
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

OUT = Path(__file__).resolve().parents[1] / "src" / "insight" / "samples"


def retail_sales(rng: np.random.Generator) -> pd.DataFrame:
    n = 4000
    dates = pd.to_datetime("2024-01-01") + pd.to_timedelta(rng.integers(0, 730, n), unit="D")
    region = rng.choice(["North", "South", "East", "West"], n, p=[0.4, 0.25, 0.2, 0.15])
    category = rng.choice(["Electronics", "Home", "Clothing", "Sports", "Beauty"], n)
    channel = rng.choice(["Online", "Store", "Partner"], n, p=[0.5, 0.35, 0.15])
    base = {"Electronics": 420, "Home": 180, "Clothing": 75, "Sports": 120, "Beauty": 45}
    units = rng.integers(1, 8, n)
    growth = 1 + (dates - dates.min()).days / 730 * 0.35  # sales grow ~35% over two years
    price = np.array([base[c] for c in category]) * rng.lognormal(0, 0.25, n)
    revenue = np.round(price * units * growth, 2)
    discount = np.round(np.where(channel == "Partner", rng.uniform(0.1, 0.25, n), rng.uniform(0, 0.12, n)), 3)
    returned = np.where(rng.random(n) < np.where(category == "Clothing", 0.18, 0.06), "Yes", "No")
    # Drawn from their own generator, so adding them left every other sample value as it was.
    extra = np.random.default_rng(7)
    cost_share = np.array([{"Electronics": 0.78, "Home": 0.62, "Clothing": 0.48, "Sports": 0.58, "Beauty": 0.42}[c] for c in category])
    cost = np.round(revenue * cost_share * extra.lognormal(0, 0.06, n), 2)
    ship_days = extra.integers(1, 5, n) + np.where(channel == "Partner", extra.integers(2, 6, n), 0)
    shipped = dates + pd.to_timedelta(ship_days, unit="D")
    return pd.DataFrame(
        {
            "order_id": [f"ORD-{100000 + i}" for i in range(n)],
            "order_date": dates.strftime("%Y-%m-%d"),
            "ship_date": shipped.strftime("%Y-%m-%d"),
            "region": region,
            "product_category": category,
            "channel": channel,
            "units_sold": units,
            "revenue": revenue,
            "cost": cost,
            "discount_rate": discount,
            "returned": returned,
        }
    ).sort_values("order_date")


def saas_customers(rng: np.random.Generator) -> pd.DataFrame:
    n = 2500
    plan = rng.choice(["Starter", "Growth", "Enterprise"], n, p=[0.55, 0.33, 0.12])
    mrr = np.round(np.array([{"Starter": 49, "Growth": 199, "Enterprise": 1200}[p] for p in plan]) * rng.lognormal(0, 0.2, n), 2)
    tenure = rng.integers(1, 48, n)
    tickets = rng.poisson(np.where(plan == "Starter", 3, 1.5))
    nps = np.clip(np.round(rng.normal(8 - tickets * 0.4, 1.5)), 0, 10).astype(int)
    p_churn = 1 / (1 + np.exp(-(-2.2 + 0.35 * tickets - 0.04 * tenure + (plan == "Starter") * 0.8)))
    churned = np.where(rng.random(n) < p_churn, "Churned", "Active")
    signup = pd.to_datetime("2022-01-01") + pd.to_timedelta(rng.integers(0, 1000, n), unit="D")
    return pd.DataFrame(
        {
            "customer_id": [f"C{10000 + i}" for i in range(n)],
            "signup_date": signup.strftime("%Y-%m-%d"),
            "plan": plan,
            "country": rng.choice(["United States", "United Kingdom", "Germany", "India", "Brazil", "Canada"], n),
            "mrr": mrr,
            "tenure_months": tenure,
            "support_tickets": tickets,
            "nps_score": nps,
            "status": churned,
        }
    )


def people(rng: np.random.Generator) -> pd.DataFrame:
    n = 1200
    dept = rng.choice(["Engineering", "Sales", "Marketing", "Operations", "Finance", "Support"], n, p=[0.3, 0.2, 0.1, 0.15, 0.1, 0.15])
    level = rng.choice(["Junior", "Mid", "Senior", "Lead"], n, p=[0.35, 0.35, 0.2, 0.1])
    lvl = {"Junior": 0, "Mid": 1, "Senior": 2, "Lead": 3}
    base = {"Engineering": 95000, "Sales": 70000, "Marketing": 72000, "Operations": 60000, "Finance": 80000, "Support": 52000}
    salary = np.round([base[d] * (1 + 0.28 * lvl[lv]) * rng.lognormal(0, 0.08) for d, lv in zip(dept, level, strict=True)], -2)
    satisfaction = np.clip(np.round(rng.normal(3.6, 0.8, n) - (dept == "Support") * 0.6), 1, 5).astype(int)
    left = np.where(rng.random(n) < 0.08 + (satisfaction <= 2) * 0.25, "Yes", "No")
    hired = pd.to_datetime("2016-01-01") + pd.to_timedelta(rng.integers(0, 3000, n), unit="D")
    return pd.DataFrame(
        {
            "employee_id": [f"E{5000 + i}" for i in range(n)],
            "hire_date": hired.strftime("%d %b %Y"),
            "department": dept,
            "level": level,
            "office": rng.choice(["London", "New York", "Bangalore", "Berlin"], n),
            "salary": salary,
            "satisfaction": satisfaction,
            "training_hours": rng.integers(0, 80, n),
            "left_company": left,
        }
    )


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(42)
    retail_sales(rng).to_csv(OUT / "retail_sales.csv", index=False)
    saas_customers(rng).to_csv(OUT / "saas_customers.csv", index=False)
    people(rng).to_csv(OUT / "people.csv", index=False)
    print(f"Wrote samples to {OUT}")


if __name__ == "__main__":
    main()
