"""Shaping the rows: formulas, steps, plain-English phrases, suggestions, and the Transform page end to end."""

import json

import pandas as pd
import pytest

from insight.engine.formulas import Formula, FormulaError
from insight.engine.suggest import suggest
from insight.engine.transform_language import PhraseError, example_phrases, parse_phrase
from insight.engine.transforms import apply_steps, parse_step
from tests.conftest import cleaned, sign_up
from tests.test_web import open_sample


@pytest.fixture(scope="module")
def retail():
    df, _, reading = cleaned("retail_sales.csv")
    return df, reading


# ── Formulas ────────────────────────────────────────────────────────────────


def test_formula_arithmetic_and_functions(retail):
    df, _ = retail
    profit = Formula("[revenue] - [cost]", list(df.columns)).evaluate(df)
    assert profit.iloc[0] == pytest.approx(df["revenue"].iloc[0] - df["cost"].iloc[0])
    days = Formula("DAYS([ship_date], [order_date])", list(df.columns)).evaluate(df)
    assert (days >= 0).all()
    assert set(Formula("ROUND(revenue / units_sold, 2)", list(df.columns)).used_columns) == {"revenue", "units_sold"}


def test_formula_conditions_read_like_a_spreadsheet(retail):
    df, _ = retail
    cols = list(df.columns)
    big = Formula("revenue > 2,000 and region = 'north'", cols).evaluate(df)  # thousands commas, any case
    expected = (df["revenue"] > 2000) & (df["region"] == "North")
    assert int(big.sum()) == int(expected.sum()) > 0
    listed = Formula("region in ['North', 'South']", cols).evaluate(df)
    assert int(listed.sum()) == int(df["region"].isin(["North", "South"]).sum())


@pytest.mark.parametrize(
    "text",
    [
        "__import__('os').system('id')",
        "revenue.__class__",
        "[x for x in range(9)]",
        "open('/etc/passwd')",
        "lambda: 1",
        "nope + 1",
        "1, 2",
    ],
)
def test_formula_rejects_anything_but_expressions_over_columns(retail, text):
    df, _ = retail
    with pytest.raises(FormulaError):
        Formula(text, list(df.columns)).evaluate(df)


# ── Steps ───────────────────────────────────────────────────────────────────


def test_steps_run_in_order_and_report_what_changed(retail):
    df, _ = retail
    steps = [
        parse_step({"kind": "derive", "name": "Profit", "formula": "[revenue] - [cost]"}),
        parse_step({"kind": "filter", "condition": "region = 'North'"}),
        parse_step({"kind": "group", "by": ["channel"], "aggregates": [{"fn": "sum", "column": "Profit"}, {"fn": "count"}]}),
        parse_step({"kind": "sort", "by": [{"column": "Total Profit", "descending": True}]}),
    ]
    out, results = apply_steps(df, steps)
    assert [r.error for r in results] == [None] * 4
    assert results[0].added == ["Profit"]
    assert results[1].rows == int((df["region"] == "North").sum())
    assert list(out.columns) == ["channel", "Total Profit", "Rows"]
    assert out["Total Profit"].is_monotonic_decreasing
    north = df[df["region"] == "North"]
    assert out["Rows"].sum() == len(north)


def test_a_failing_step_is_skipped_not_fatal(retail):
    df, _ = retail
    steps = [
        parse_step({"kind": "rename", "column": "region", "to": "Area"}),
        parse_step({"kind": "filter", "condition": "[region] = 'North'"}),  # region was renamed
        parse_step({"kind": "dedupe", "columns": []}),
    ]
    out, results = apply_steps(df, steps)
    assert results[1].error and results[0].error is None and results[2].error is None
    assert "Area" in out.columns and len(out) == len(df)


def test_disabled_steps_do_nothing(retail):
    df, _ = retail
    out, results = apply_steps(df, [parse_step({"kind": "limit", "count": 5, "enabled": False})])
    assert len(out) == len(df) and not results[0].enabled


def test_shape_changing_steps():
    wide = pd.DataFrame({"store": ["A", "B"], "Jan": [1.0, 2.0], "Feb": [3.0, 4.0], "Mar": [5.0, 6.0]})
    long, _ = apply_steps(wide, [parse_step({"kind": "unpivot", "columns": ["Jan", "Feb", "Mar"], "name_column": "Month"})])
    assert len(long) == 6 and set(long.columns) == {"store", "Month", "Value"}
    back, _ = apply_steps(long, [parse_step({"kind": "pivot", "column": "Month", "measure": "Value", "fn": "sum"})])
    assert len(back) == 2 and back.loc[back["store"] == "B", "Mar"].item() == 6

    people = pd.DataFrame({"place": ["Austin, TX", "Boston, MA", None], "age": [21.0, 45.0, 67.0]})
    steps = [
        parse_step({"kind": "split", "column": "place", "separator": ",", "into": ["City", "State"]}),
        parse_step({"kind": "bucket", "column": "age", "edges": [30, 60], "name": "Age band"}),
        parse_step({"kind": "conditional", "name": "Senior", "rules": [{"when": "age >= 65", "then": "Yes"}], "otherwise": "No"}),
    ]
    out, results = apply_steps(people, steps)
    assert [r.error for r in results] == [None] * 3
    assert out["State"].tolist()[:2] == ["TX", "MA"]
    assert out["Age band"].tolist() == ["Under 30", "30–60", "60+"]
    assert out["Senior"].tolist() == ["No", "No", "Yes"]


def test_steps_describe_themselves():
    step = parse_step({"kind": "replace", "column": "region", "find": "N", "replacement": "North", "mode": "value"})
    assert step.describe() == "Replace “N” with “North” in “region”"


# ── Plain English ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("phrase", "kind", "check"),
    [
        ("keep rows where region is North or South", "filter", lambda s: "in" in s["condition"]),
        ("remove rows where revenue < 100", "filter", lambda s: s["exclude"]),
        ("remove duplicates", "dedupe", lambda s: True),
        ("group by region with total revenue", "group", lambda s: s["by"] == ["region"]),
        ("sort by revenue descending", "sort", lambda s: s["by"][0]["descending"]),
        ("rename region to Area", "rename", lambda s: s["to"] == "Area"),
        ("month of order date", "datepart", lambda s: s["part"] == "month"),
        ("add Profit = revenue - cost", "derive", lambda s: s["name"] == "Profit"),
    ],
)
def test_phrases_become_steps(retail, phrase, kind, check):
    df, _ = retail
    step = parse_phrase(phrase, list(df.columns)).model_dump(mode="json")
    assert step["kind"] == kind and check(step)
    apply_steps(df, [parse_step(step)])


def test_phrases_name_what_they_could_not_find(retail):
    df, _ = retail
    with pytest.raises(PhraseError, match="no column called “customer”"):
        parse_phrase("keep rows where customer is Ana", list(df.columns))
    with pytest.raises(PhraseError):
        parse_phrase("make it pop", list(df.columns))


def test_example_phrases_all_parse(retail):
    df, reading = retail
    examples = example_phrases(reading)
    assert len(examples) >= 4
    for e in examples:
        _, results = apply_steps(df, [parse_phrase(e, list(df.columns))])
        assert results[0].error is None, e


# ── Suggestions ─────────────────────────────────────────────────────────────


def test_suggestions_fit_the_table(retail):
    df, reading = retail
    found = {s.step.get("name"): s for s in suggest(df, reading)}
    assert {"Profit", "Margin", "Days to ship"} <= set(found)
    assert all(s.additive for s in found.values())
    taken = suggest(df, reading, [found["Profit"].step])
    assert "Profit" not in {s.step.get("name") for s in taken}


# ── The page ────────────────────────────────────────────────────────────────


async def test_transform_page_preview_add_and_edit(client):
    await sign_up(client)
    board = await open_sample(client)
    base = board.rsplit("/", 1)[0]

    page = await client.get(f"{base}/transform")
    assert page.status_code == 200 and "Applied steps" in page.text and "Profit" in page.text
    for kind in ("filter", "derive", "conditional", "group", "split", "pivot", "unpivot"):
        form = await client.get(f"{base}/transform/form", params={"kind": kind})
        assert form.status_code == 200 and 'name="kind"' in form.text

    r = await client.post(f"{base}/transform/preview", data={"phrase": "keep rows where region is North"})
    assert "Add step" in r.text and "4,000 → " in r.text
    r = await client.post(f"{base}/transform/preview", data={"phrase": "make it pop"})
    assert "alert error" in r.text
    r = await client.post(f"{base}/transform/preview", data={"kind": "derive", "name": "Profit", "formula": "[revenue] - [cost]"})
    assert "+ Profit" in r.text

    step = {"kind": "derive", "name": "Profit", "formula": "[revenue] - [cost]"}
    r = await client.post(f"{base}/transform/steps", data={"step": json.dumps(step)})
    assert r.status_code == 204 and r.headers["hx-redirect"] == f"{base}/transform"
    r = await client.post(f"{base}/transform/steps", data={"step": json.dumps({"kind": "filter", "condition": "region = 'North'"})})
    assert r.status_code == 204

    north = await client.get(board, headers={"hx-target": "board"})
    assert "4,000 rows" not in north.text  # the board reads the shaped rows
    assert "Profit" in (await client.get(f"{base}/data")).text

    assert (await client.post(f"{base}/transform/steps/1/toggle")).status_code == 204
    assert "4,000 rows" in (await client.get(board, headers={"hx-target": "board"})).text
    assert (await client.post(f"{base}/transform/steps/1/up")).status_code == 204
    assert (await client.post(f"{base}/transform/steps/0/delete")).status_code == 204
    page = await client.get(f"{base}/transform")
    assert page.text.count('class="step-item') == 1

    bad = await client.post(f"{base}/transform/steps", data={"step": json.dumps({"kind": "derive", "name": "X"})})
    assert bad.status_code == 400


async def test_demo_previews_but_does_not_keep_steps(client):
    page = await client.get("/demo/retail/transform")
    assert page.status_code == 200 and "Use your own data" in page.text
    r = await client.post("/demo/retail/transform/preview", data={"phrase": "remove duplicates"})
    assert r.status_code == 200 and "Add step" not in r.text
    r = await client.post("/demo/retail/transform/steps", data={"step": json.dumps({"kind": "dedupe"})})
    assert r.status_code == 403


async def test_other_people_cannot_shape_my_data(client):
    await sign_up(client, "ana@example.com")
    board = await open_sample(client)
    base = board.rsplit("/", 1)[0]
    client.cookies.clear()
    await sign_up(client, "bo@example.com")
    assert (await client.get(f"{base}/transform")).status_code == 404
    assert (await client.post(f"{base}/transform/steps", data={"step": json.dumps({"kind": "dedupe"})})).status_code == 404
