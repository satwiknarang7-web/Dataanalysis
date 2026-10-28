"""Reading, querying, planning and asking, over the bundled samples and small tables."""

import numpy as np
import pandas as pd
import pytest

from insight.engine.ask import AskError, interpret
from insight.engine.cleaning import clean
from insight.engine.fields import read_dataset
from insight.engine.measures import agg_measure, build_measures, rate_measure
from insight.engine.models import Dim, Filter, Spec
from insight.engine.planner import build_plan
from insight.engine.query import OTHER, run
from insight.engine.render import render_dashboard
from insight.engine.sql_console import SqlError, run_sql
from insight.engine.stats import standout_means, standout_rates, trend_test
from tests.conftest import cleaned, frame


# ── Reading ─────────────────────────────────────────────────────────────
def test_roles_of_the_retail_sample():
    _, _, r = cleaned("retail_sales.csv")
    roles = {f.name: (f.role, f.agg) for f in r.fields}
    assert roles["order_id"][0] == "id"
    assert roles["order_date"][0] == "time"
    assert roles["revenue"] == ("measure", "sum")
    assert roles["discount_rate"] == ("measure", "avg")
    assert roles["region"][0] == "dimension"
    assert r.time == "order_date"
    assert r.field("returned").outcome == "Yes"
    assert r.field("revenue").format == "currency"


def test_hire_and_signup_dates_are_not_the_timeline():
    _, _, people = cleaned("people.csv")
    assert people.field("hire_date").origin and people.time is None


def test_ordered_levels_and_outcomes():
    df, _ = clean(frame({"level": ["Low", "High", "Medium", "Low"] * 5, "status": ["Active"] * 15 + ["Churned"] * 5}))
    r = read_dataset(df)
    assert r.field("level").order == ["Low", "Medium", "High"]
    assert r.field("status").outcome == "Churned"


def test_whole_numbers_named_like_ids_are_not_added_up():
    df, _ = clean(frame({"customer_id": [str(i) for i in range(100)], "amount": [str(i) for i in range(100)]}))
    r = read_dataset(df)
    assert r.field("customer_id").role == "id"
    assert r.field("amount").role == "measure"


# ── Query ───────────────────────────────────────────────────────────────
@pytest.fixture
def small():
    df, _ = clean(frame({"g": list("aabbbc"), "v": ["1", "3", "10", "20", "30", "100"], "flag": ["Yes", "No", "Yes", "Yes", "No", "No"]}))
    return df, read_dataset(df)


def test_sums_averages_and_rates_by_group(small):
    df, r = small
    s, a, rate = agg_measure(r.field("v"), "sum"), agg_measure(r.field("v"), "avg"), rate_measure(r.field("flag"))
    rows = {row["g"]: row for row in run(df, Spec(measures=[s, a, rate], by=[Dim(field="g")]))}
    assert rows["a"][s.id] == 4 and rows["b"][s.id] == 60 and rows["c"][s.id] == 100
    assert rows["b"][a.id] == 20
    assert rows["b"][rate.id] == pytest.approx(2 / 3)
    assert rows["b"]["_n"] == 3


def test_other_is_folded_correctly_for_averages(small):
    df, r = small
    a = agg_measure(r.field("v"), "avg")
    rows = run(df, Spec(measures=[a], by=[Dim(field="g")], limit=1, other=True))
    other = next(x for x in rows if x["g"] == OTHER)
    assert other[a.id] == pytest.approx((1 + 3 + 10 + 20 + 30) / 5)  # the average of the rows, not of the groups' averages
    assert rows[-1]["g"] == OTHER


def test_filters(small):
    df, r = small
    s = agg_measure(r.field("v"), "sum")
    assert run(df, Spec(measures=[s], filters=[Filter(field="g", values=["a", "c"])]))[0][s.id] == 104
    assert run(df, Spec(measures=[s], filters=[Filter(field="g", values=["a"], exclude=True)]))[0][s.id] == 160


def test_time_buckets():
    df, _ = clean(frame({"d": ["2024-01-03", "2024-01-30", "2024-04-02"], "v": ["1", "2", "3"]}))
    r = read_dataset(df)
    m = agg_measure(r.field("v"), "sum")
    rows = run(df, Spec(measures=[m], by=[Dim(field="d", grain="quarter")], sort="label"))
    assert [(x["d"], x[m.id]) for x in rows] == [("2024-Q1", 3), ("2024-Q2", 3)]


# ── Stats ───────────────────────────────────────────────────────────────
def test_tests_find_real_effects_and_ignore_noise():
    rng = np.random.default_rng(1)
    assert standout_means([rng.normal(10, 1, 200), rng.normal(10, 1, 200), rng.normal(12, 1, 200)])[0] < 0.001
    false_alarms = sum(standout_means([rng.normal(10, 2, 50) for _ in range(5)])[0] < 0.05 for _ in range(200))
    assert false_alarms <= 20  # 5% plus slack
    assert standout_rates([(10, 100), (11, 100), (40, 100)])[0] < 0.001
    assert trend_test(list(range(12)))[1] < 0.001
    assert trend_test([5, 5.1, 4.9, 5, 5.1, 4.9])[1] > 0.05


# ── Planning ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("name", ["retail_sales.csv", "saas_customers.csv", "people.csv"])
def test_every_sample_gets_a_board_with_findings(name):
    df, _, r = cleaned(name)
    plan = build_plan(df, r)
    board = render_dashboard(df, plan, r)
    assert 4 <= len(board.tiles) <= 8
    assert board.findings and board.headline
    assert all(k.value for k in board.kpis)


def test_planted_patterns_are_found():
    df, _, r = cleaned("retail_sales.csv")
    board = render_dashboard(df, build_plan(df, r), r)
    text = " ".join(f.text for f in board.findings)
    assert "Clothing has the highest returned rate" in text
    assert "Revenue rose" in board.headline


def test_noise_gives_no_findings():
    rng = np.random.default_rng(7)
    n = 600
    df, _ = clean(
        pd.DataFrame(
            {
                "group": rng.choice(["a", "b", "c", "d"], n).astype(str),
                "score": rng.normal(50, 10, n).round(2).astype(str),
                "rating": rng.normal(3, 1, n).round(2).astype(str),
            }
        )
    )
    r = read_dataset(df)
    board = render_dashboard(df, build_plan(df, r), r)
    assert not [f for f in board.findings if "group" in f.text.lower()]


def test_filters_recaption_every_chart():
    df, _, r = cleaned("retail_sales.csv")
    plan = build_plan(df, r)
    board = render_dashboard(df, plan, r, [Filter(field="region", values=["North"])])
    assert board.rows == int((df["region"] == "North").sum())


# ── Ask ─────────────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def retail():
    df, _, r = cleaned("retail_sales.csv")
    return df, r, build_measures(r)


@pytest.mark.parametrize(
    ("question", "kind"),
    [
        ("revenue by region", "breakdown"),
        ("how has revenue changed monthly", "trend"),
        ("top 5 product category by units sold", "table"),
        ("revenue vs units sold", "relationship"),
        ("distribution of revenue", "distribution"),
        ("how many orders for North", "kpi"),
        ("returned rate by channel", "breakdown"),
    ],
)
def test_questions_become_tiles(retail, question, kind):
    df, r, ms = retail
    tile, _ = interpret(question, r, ms, df)
    assert tile.kind == kind


def test_question_filters_and_dimensions(retail):
    df, r, ms = retail
    tile, _ = interpret("returned rate by channel in 2025", r, ms, df)
    assert tile.spec.by[0].field == "channel"
    assert tile.spec.filters[0].values == ["2025"]


def test_questions_about_missing_columns_are_refused_by_name(retail):
    df, r, ms = retail
    with pytest.raises(AskError, match="profit"):
        interpret("profit by region", r, ms, df)


# ── SQL console ─────────────────────────────────────────────────────────
def test_sql_console_reads_the_table(retail):
    df, _, _ = retail
    result = run_sql(df, "SELECT region, count(*) AS n FROM data GROUP BY 1 ORDER BY 2 DESC")
    assert result.columns == ["region", "n"] and len(result.rows) == 4


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM read_csv('/etc/passwd')",
        "COPY data TO '/tmp/out.csv'",
        "SELECT 1; SELECT 2",
        "INSTALL httpfs",
        "SET enable_external_access = true",
        "ATTACH '/tmp/x.db'",
    ],
)
def test_sql_console_refuses_anything_but_one_select(retail, sql):
    df, _, _ = retail
    with pytest.raises(SqlError):
        run_sql(df, sql)
