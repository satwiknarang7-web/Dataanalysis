"""Shaping the data before anything is analysed: what Power Query does.

Cleaning decides what a value *is*. This is the layer above it: renaming and
dropping columns, deriving one from others, splitting "City, ST", pulling the
month out of a date, banding a number, replacing values, removing duplicates,
grouping a ledger to one row per customer, turning twelve month columns into
rows. Things the file cannot say and only the person reading it knows.

Every step is data (a pydantic model), not code: it can be stored, shown as a
sentence, switched off, reordered, and checked against the table's columns
before it runs. Formulas go through `formulas.Formula`, which refuses anything
that isn't arithmetic over columns.
"""

from __future__ import annotations

import re
from itertools import pairwise
from typing import Annotated, Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field, TypeAdapter

from insight.engine.cleaning import as_number_dtype
from insight.engine.formatting import format_value, join_names
from insight.engine.formulas import Formula, FormulaError

MAX_STEPS = 60


class StepError(ValueError):
    """The step can't run on this table; the message says why, in words."""


class _Step(BaseModel):
    enabled: bool = True
    note: str = ""  # why it was suggested, for steps the app proposed


def _q(name: str) -> str:
    return f"“{name}”"


def _need(df: pd.DataFrame, *columns: str) -> None:
    missing = [c for c in columns if c not in df.columns]
    if missing:
        raise StepError(f"There is no column {join_names([_q(c) for c in missing])} at this point.")


def _new_name(df: pd.DataFrame, name: str) -> str:
    name = name.strip()
    if not name:
        raise StepError("Give the new column a name.")
    if len(name) > 80:
        raise StepError("Keep column names under 80 characters.")
    return name


def _value(text: str | float | int | None):
    """A value typed into a box: a number when it reads as one, text otherwise."""
    if text is None or isinstance(text, int | float):
        return text
    t = str(text).strip()
    if re.fullmatch(r"[-+]?\d+(\.\d+)?", t.replace(",", "")):
        n = float(t.replace(",", ""))
        return int(n) if n.is_integer() else n
    return t


class Rename(_Step):
    kind: Literal["rename"] = "rename"
    column: str
    to: str

    def describe(self) -> str:
        return f"Rename {_q(self.column)} to {_q(self.to)}"

    def apply(self, df):
        _need(df, self.column)
        to = _new_name(df, self.to)
        if to != self.column and to in df.columns:
            raise StepError(f"There is already a column called {_q(to)}.")
        return df.rename(columns={self.column: to})


class Drop(_Step):
    kind: Literal["drop"] = "drop"
    columns: list[str]

    def describe(self) -> str:
        return f"Remove {join_names([_q(c) for c in self.columns])}"

    def apply(self, df):
        _need(df, *self.columns)
        if len(self.columns) >= df.shape[1]:
            raise StepError("That would remove every column.")
        return df.drop(columns=self.columns)


class Keep(_Step):
    kind: Literal["keep"] = "keep"
    columns: list[str]

    def describe(self) -> str:
        return f"Keep only {join_names([_q(c) for c in self.columns])}"

    def apply(self, df):
        _need(df, *self.columns)
        if not self.columns:
            raise StepError("Choose at least one column to keep.")
        return df[self.columns]


class Derive(_Step):
    kind: Literal["derive"] = "derive"
    name: str
    formula: str

    def describe(self) -> str:
        return f"Add {_q(self.name)} = {self.formula}"

    def apply(self, df):
        name = _new_name(df, self.name)
        result = _formula(self.formula, df).evaluate(df)
        out = df.copy()
        out[name] = _column(result, df)
        return out


class Retype(_Step):
    kind: Literal["retype"] = "retype"
    column: str
    to: Literal["number", "integer", "text", "date"]

    def describe(self) -> str:
        return f"Read {_q(self.column)} as {dict(number='numbers', integer='whole numbers', text='text', date='dates')[self.to]}"

    def apply(self, df):
        _need(df, self.column)
        s = df[self.column]
        out = df.copy()
        if self.to in ("number", "integer"):
            text = s.astype("string").str.replace(r"[,$€£₹¥%\s]", "", regex=True)
            num = pd.to_numeric(text, errors="coerce")
            out[self.column] = num.round().astype("Int64") if self.to == "integer" else as_number_dtype(num.astype("float64"))
        elif self.to == "date":
            parsed = pd.to_datetime(s.astype("string"), errors="coerce", format="mixed", utc=True)
            out[self.column] = parsed.dt.tz_localize(None).astype("datetime64[ns]")
        else:
            out[self.column] = _as_text(s)
        return out


class FilterRows(_Step):
    kind: Literal["filter"] = "filter"
    condition: str
    exclude: bool = False  # remove the rows that match, rather than keep them

    def describe(self) -> str:
        return f"{'Remove' if self.exclude else 'Keep'} rows where {self.condition}"

    def apply(self, df):
        mask = _mask(_formula(self.condition, df).evaluate(df), df)
        return df.loc[~mask if self.exclude else mask].reset_index(drop=True)


class Rule(BaseModel):
    when: str
    then: str


class Conditional(_Step):
    kind: Literal["conditional"] = "conditional"
    name: str = ""  # a new column; leave empty to rewrite `column` in place
    column: str = ""
    rules: list[Rule]
    otherwise: str = ""

    def describe(self) -> str:
        first = self.rules[0] if self.rules else Rule(when="…", then="…")
        more = f" (+{len(self.rules) - 1} more)" if len(self.rules) > 1 else ""
        target = _q(self.name) if self.name else f"{_q(self.column)} in place"
        tail = f", otherwise {self.otherwise}" if self.otherwise else ""
        return f"If {first.when} then {first.then}{more}{tail} → {target}"

    def apply(self, df):
        if not self.rules:
            raise StepError("Add at least one rule: when … then ….")
        if self.column:
            _need(df, self.column)
        target = _new_name(df, self.name) if self.name else self.column
        if not target:
            raise StepError("Name the new column, or choose a column to rewrite.")
        conditions = [_mask(_formula(r.when, df).evaluate(df), df).to_numpy() for r in self.rules]
        choices = [_operand(r.then, df) for r in self.rules]
        fallback = _operand(self.otherwise, df) if self.otherwise else (df[self.column] if self.column else None)
        result = pd.Series(_select(conditions, choices, fallback, len(df)), index=df.index)
        out = df.copy()
        out[target] = _column(result, df)
        return out


class Bucket(_Step):
    kind: Literal["bucket"] = "bucket"
    name: str
    column: str
    edges: list[float]
    labels: list[str] = Field(default_factory=list)

    def describe(self) -> str:
        return f"Band {_q(self.column)} at {', '.join(f'{e:g}' for e in self.edges)} → {_q(self.name)}"

    def apply(self, df):
        _need(df, self.column)
        edges = sorted(set(self.edges))
        if not edges:
            raise StepError("Give at least one edge, for example 18, 35, 60.")
        labels = self.labels or _band_labels(edges)
        if len(labels) != len(edges) + 1:
            raise StepError(f"{len(edges)} edges make {len(edges) + 1} bands, so give {len(edges) + 1} labels (or none).")
        values = pd.to_numeric(df[self.column], errors="coerce")
        bins = [-np.inf, *edges, np.inf]
        out = df.copy()
        out[_new_name(df, self.name)] = pd.cut(values, bins=bins, labels=labels, right=False).astype("object").where(values.notna(), None)
        return out


DATE_PARTS = {
    "year": "Year",
    "quarter": "Quarter (Q1–Q4)",
    "year_quarter": "Year and quarter",
    "month": "Month (1–12)",
    "month_name": "Month name",
    "year_month": "Year and month",
    "day": "Day of month",
    "weekday": "Day of week",
    "hour": "Hour of day",
    "date": "Date only",
}


class DatePart(_Step):
    kind: Literal["datepart"] = "datepart"
    name: str
    column: str
    part: Literal["year", "quarter", "year_quarter", "month", "month_name", "year_month", "day", "weekday", "hour", "date"]

    def describe(self) -> str:
        return f"Add {_q(self.name)}: the {DATE_PARTS[self.part].lower()} of {_q(self.column)}"

    def apply(self, df):
        _need(df, self.column)
        d = pd.to_datetime(df[self.column], errors="coerce")
        part = {
            "year": lambda: d.dt.year.astype("Int64"),
            "quarter": lambda: "Q" + d.dt.quarter.astype("Int64").astype("string"),
            "year_quarter": lambda: d.dt.year.astype("Int64").astype("string") + "-Q" + d.dt.quarter.astype("Int64").astype("string"),
            "month": lambda: d.dt.month.astype("Int64"),
            "month_name": lambda: d.dt.strftime("%b"),
            "year_month": lambda: d.dt.strftime("%Y-%m"),
            "day": lambda: d.dt.day.astype("Int64"),
            "weekday": lambda: d.dt.strftime("%a"),
            "hour": lambda: d.dt.hour.astype("Int64"),
            "date": lambda: d.dt.normalize(),
        }[self.part]()
        out = df.copy()
        out[_new_name(df, self.name)] = _column(part.where(d.notna(), None) if self.part != "date" else part, df)
        return out


class Split(_Step):
    kind: Literal["split"] = "split"
    column: str
    separator: str
    into: list[str]
    drop_original: bool = False

    def describe(self) -> str:
        return f"Split {_q(self.column)} on “{self.separator}” into {join_names([_q(n) for n in self.into])}"

    def apply(self, df):
        _need(df, self.column)
        if not self.separator:
            raise StepError("Say what to split on, for example a comma.")
        if len(self.into) < 2:
            raise StepError("Name at least two new columns.")
        parts = _as_text(df[self.column]).str.split(self.separator, n=len(self.into) - 1, regex=False, expand=True)
        out = df.copy()
        for i, name in enumerate(self.into):
            col = parts[i] if i in parts.columns else pd.Series(None, index=df.index)
            out[_new_name(df, name)] = _column(col.astype("object").map(lambda v: v.strip() if isinstance(v, str) else None), df)
        return out.drop(columns=[self.column]) if self.drop_original and self.column not in self.into else out


class Merge(_Step):
    kind: Literal["merge"] = "merge"
    name: str
    columns: list[str]
    separator: str = " "

    def describe(self) -> str:
        return f"Join {join_names([_q(c) for c in self.columns])} with “{self.separator}” → {_q(self.name)}"

    def apply(self, df):
        _need(df, *self.columns)
        if len(self.columns) < 2:
            raise StepError("Choose at least two columns to join.")
        texts = [_as_text(df[c]).fillna("") for c in self.columns]
        joined = texts[0]
        for t in texts[1:]:
            joined = joined + self.separator + t
        out = df.copy()
        out[_new_name(df, self.name)] = joined.str.strip(self.separator.strip() or None).astype("object")
        return out


class Text(_Step):
    kind: Literal["text"] = "text"
    column: str
    op: Literal["trim", "upper", "lower", "proper"]

    def describe(self) -> str:
        return f"{dict(trim='Trim spaces in', upper='UPPER CASE', lower='lower case', proper='Proper Case')[self.op]} {_q(self.column)}"

    def apply(self, df):
        _need(df, self.column)
        s = _as_text(df[self.column])
        s = {"trim": s.str.strip, "upper": s.str.upper, "lower": s.str.lower, "proper": s.str.title}[self.op]()
        out = df.copy()
        out[self.column] = s.astype("object").where(s.notna(), None)
        return out


class Replace(_Step):
    kind: Literal["replace"] = "replace"
    column: str
    find: str
    replacement: str = ""
    mode: Literal["value", "text"] = "value"  # whole cell, or text inside the cell

    def describe(self) -> str:
        inside = " inside" if self.mode == "text" else ""
        return f"Replace “{self.find}” with “{self.replacement}”{inside or ' in'} {_q(self.column)}"

    def apply(self, df):
        _need(df, self.column)
        out = df.copy()
        s = df[self.column]
        if self.mode == "text":
            out[self.column] = _as_text(s).str.replace(self.find, self.replacement, regex=False).astype("object")
        else:
            find, repl = _value(self.find), _value(self.replacement)
            target = s.astype("string").str.strip().str.lower() == str(self.find).strip().lower()
            if pd.api.types.is_numeric_dtype(s) and isinstance(find, int | float):
                target = s == find
            out[self.column] = s.astype("object").where(~target.fillna(False), repl if self.replacement != "" else None)
            if pd.api.types.is_numeric_dtype(s):
                out[self.column] = _column(pd.to_numeric(out[self.column], errors="coerce"), df)
        return out


class Fill(_Step):
    kind: Literal["fill"] = "fill"
    column: str
    value: str = ""
    method: Literal["value", "previous", "average", "median", "zero"] = "value"

    def describe(self) -> str:
        how = {"value": f"“{self.value}”", "previous": "the value above", "average": "the average", "median": "the median", "zero": "0"}[
            self.method
        ]
        return f"Fill blanks in {_q(self.column)} with {how}"

    def apply(self, df):
        _need(df, self.column)
        s = df[self.column]
        out = df.copy()
        if self.method == "previous":
            out[self.column] = s.ffill()
        elif self.method in ("average", "median", "zero"):
            num = pd.to_numeric(s, errors="coerce")
            fill = 0 if self.method == "zero" else (num.mean() if self.method == "average" else num.median())
            out[self.column] = _column(num.fillna(fill), df)
        else:
            v = _value(self.value)
            out[self.column] = (
                _column(pd.to_numeric(s, errors="coerce").fillna(v), df)
                if pd.api.types.is_numeric_dtype(s) and isinstance(v, int | float)
                else s.astype("object").where(s.notna(), v)
            )
        return out


class Blanks(_Step):
    kind: Literal["blanks"] = "blanks"
    columns: list[str] = Field(default_factory=list)

    def describe(self) -> str:
        return (
            f"Remove rows with a blank {join_names([_q(c) for c in self.columns])}"
            if self.columns
            else "Remove rows that are entirely blank"
        )

    def apply(self, df):
        _need(df, *self.columns)
        kept = df.dropna(subset=self.columns, how="any") if self.columns else df.dropna(how="all")
        return kept.reset_index(drop=True)


class Dedupe(_Step):
    kind: Literal["dedupe"] = "dedupe"
    columns: list[str] = Field(default_factory=list)

    def describe(self) -> str:
        return (
            f"Remove duplicate {join_names([_q(c) for c in self.columns])}, keeping the first" if self.columns else "Remove duplicate rows"
        )

    def apply(self, df):
        _need(df, *self.columns)
        return df.drop_duplicates(subset=self.columns or None).reset_index(drop=True)


class SortKey(BaseModel):
    column: str
    descending: bool = False


class Sort(_Step):
    kind: Literal["sort"] = "sort"
    by: list[SortKey]

    def describe(self) -> str:
        return "Sort by " + join_names([f"{_q(k.column)} {'high to low' if k.descending else 'low to high'}" for k in self.by])

    def apply(self, df):
        _need(df, *[k.column for k in self.by])
        if not self.by:
            raise StepError("Choose a column to sort by.")
        return df.sort_values([k.column for k in self.by], ascending=[not k.descending for k in self.by], na_position="last").reset_index(
            drop=True
        )


class Limit(_Step):
    kind: Literal["limit"] = "limit"
    count: int
    by: str = ""
    descending: bool = True

    def describe(self) -> str:
        if self.by:
            return f"Keep the {'top' if self.descending else 'bottom'} {self.count:,} rows by {_q(self.by)}"
        return f"Keep the first {self.count:,} rows"

    def apply(self, df):
        if self.count < 1:
            raise StepError("Keep at least one row.")
        if self.by:
            _need(df, self.by)
            df = df.sort_values(self.by, ascending=not self.descending, na_position="last")
        return df.head(self.count).reset_index(drop=True)


AGG_LABELS = {
    "sum": "Total",
    "avg": "Average",
    "min": "Lowest",
    "max": "Highest",
    "count": "Count",
    "count_distinct": "Distinct",
    "median": "Median",
    "first": "First",
}


class Aggregate(BaseModel):
    fn: Literal["sum", "avg", "min", "max", "count", "count_distinct", "median", "first"]
    column: str = ""
    name: str = ""

    def output_name(self) -> str:
        if self.name:
            return self.name
        return "Rows" if self.fn == "count" and not self.column else f"{AGG_LABELS[self.fn]} {self.column}".strip()


class Group(_Step):
    kind: Literal["group"] = "group"
    by: list[str]
    aggregates: list[Aggregate]

    def describe(self) -> str:
        what = join_names(
            [
                "the number of rows" if a.fn == "count" and not a.column else f"{AGG_LABELS[a.fn].lower()} {a.column}"
                for a in self.aggregates
            ]
        )
        return f"Group by {join_names([_q(c) for c in self.by])}: {what}"

    def apply(self, df):
        _need(df, *self.by, *[a.column for a in self.aggregates if a.column])
        if not self.by:
            raise StepError("Choose at least one column to group by.")
        if not self.aggregates:
            raise StepError("Choose at least one thing to work out per group, for example a count.")
        grouped = df.groupby(self.by, dropna=False, sort=True)
        result = grouped.size().rename("__n").to_frame()
        for a in self.aggregates:
            name = a.output_name()
            if a.fn == "count":
                result[name] = grouped[a.column].count() if a.column else grouped.size()
            elif a.fn == "count_distinct":
                result[name] = grouped[a.column].nunique()
            elif a.fn == "first":
                result[name] = grouped[a.column].first()
            else:
                num = pd.to_numeric(df[a.column], errors="coerce")
                result[name] = num.groupby([df[c] for c in self.by], dropna=False).agg({"avg": "mean"}.get(a.fn, a.fn))
        out = result.drop(columns="__n").reset_index()
        for c in out.columns:
            if pd.api.types.is_float_dtype(out[c]):
                out[c] = as_number_dtype(out[c])
        return out


class Unpivot(_Step):
    kind: Literal["unpivot"] = "unpivot"
    columns: list[str]
    name_column: str = "Attribute"
    value_column: str = "Value"

    def describe(self) -> str:
        cols = join_names([_q(c) for c in self.columns[:3]]) + (f" and {len(self.columns) - 3} more" if len(self.columns) > 3 else "")
        return f"Turn {cols} into rows: {_q(self.name_column)} and {_q(self.value_column)}"

    def apply(self, df):
        _need(df, *self.columns)
        if len(self.columns) < 2:
            raise StepError("Choose at least two columns to turn into rows.")
        keep = [c for c in df.columns if c not in self.columns]
        out = df.melt(
            id_vars=keep, value_vars=self.columns, var_name=_new_name(df, self.name_column), value_name=_new_name(df, self.value_column)
        )
        if all(pd.api.types.is_numeric_dtype(df[c]) for c in self.columns):
            out[self.value_column] = as_number_dtype(pd.to_numeric(out[self.value_column], errors="coerce").astype("float64"))
        return out


class Pivot(_Step):
    kind: Literal["pivot"] = "pivot"
    column: str
    measure: str
    fn: Literal["sum", "avg", "min", "max", "count"] = "sum"

    def describe(self) -> str:
        return f"Turn each {_q(self.column)} into a column of {AGG_LABELS[self.fn].lower()} {_q(self.measure)}"

    def apply(self, df):
        _need(df, self.column, self.measure)
        levels = df[self.column].dropna().unique()
        if len(levels) > 50:
            raise StepError(f"{_q(self.column)} has {len(levels)} values; pivoting makes a column for each, so pick one with 50 or fewer.")
        # Rows are the label columns (text, dates, yes/no); other numbers would make every row unique.
        keep = [c for c in df.columns if c not in (self.column, self.measure) and not pd.api.types.is_numeric_dtype(df[c])]
        values = pd.to_numeric(df[self.measure], errors="coerce") if self.fn != "count" else df[self.measure]
        keys = [df[c] for c in keep] + [df[self.column].astype("string")]
        grouped = values.groupby(keys, dropna=False, sort=True)
        agg = grouped.count() if self.fn == "count" else grouped.agg({"avg": "mean"}.get(self.fn, self.fn))
        out = agg.unstack(level=-1)
        out.columns = [str(c) for c in out.columns]
        out = out.reset_index() if keep else out.reset_index(drop=True)
        for c in out.columns:
            if pd.api.types.is_float_dtype(out[c]):
                out[c] = as_number_dtype(out[c])
        return out


class Index(_Step):
    kind: Literal["index"] = "index"
    name: str = "Row number"

    def describe(self) -> str:
        return f"Add a row number, {_q(self.name)}"

    def apply(self, df):
        out = df.copy()
        out[_new_name(df, self.name)] = pd.array(range(1, len(df) + 1), dtype="Int64")
        return out


Step = Annotated[
    Rename
    | Drop
    | Keep
    | Derive
    | Retype
    | FilterRows
    | Conditional
    | Bucket
    | DatePart
    | Split
    | Merge
    | Text
    | Replace
    | Fill
    | Blanks
    | Dedupe
    | Sort
    | Limit
    | Group
    | Unpivot
    | Pivot
    | Index,
    Field(discriminator="kind"),
]
STEP = TypeAdapter(Step)
STEPS = TypeAdapter(list[Step])

# Steps that only add columns: they change nothing a chart already drew, so a suggestion of this kind is safe to apply as is.
ADDITIVE_KINDS = {"derive", "conditional", "bucket", "datepart", "split", "merge", "index"}

KIND_LABELS = {
    "rename": "Rename a column",
    "drop": "Remove columns",
    "keep": "Keep only some columns",
    "derive": "Formula column",
    "retype": "Change a column's type",
    "filter": "Filter rows",
    "conditional": "If / else column",
    "bucket": "Bands of a number",
    "datepart": "Part of a date",
    "split": "Split a column",
    "merge": "Join columns",
    "text": "Tidy text",
    "replace": "Replace values",
    "fill": "Fill blanks",
    "blanks": "Remove blank rows",
    "dedupe": "Remove duplicates",
    "sort": "Sort rows",
    "limit": "Keep top rows",
    "group": "Group and summarise",
    "unpivot": "Columns into rows",
    "pivot": "Rows into columns",
    "index": "Row number",
}


class StepResult(BaseModel):
    sentence: str
    enabled: bool
    rows: int | None = None
    columns: int | None = None
    added: list[str] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)
    error: str | None = None


def parse_step(data: dict) -> Step:
    return STEP.validate_python(data)


def apply_steps(df: pd.DataFrame, steps: list) -> tuple[pd.DataFrame, list[StepResult]]:
    """Run the steps in order. A step that fails is reported and skipped; the rest still run."""
    if len(steps) > MAX_STEPS:
        raise StepError(f"A table can have up to {MAX_STEPS} steps.")
    results: list[StepResult] = []
    for step in steps:
        result = StepResult(sentence=step.describe(), enabled=step.enabled)
        if step.enabled:
            before = list(df.columns)
            try:
                df = step.apply(df)
                result.added = [c for c in df.columns if c not in before]
                result.removed = [c for c in before if c not in df.columns]
            except (StepError, FormulaError) as e:
                result.error = str(e)
            result.rows, result.columns = len(df), df.shape[1]
        results.append(result)
    return df, results


# ── Helpers ─────────────────────────────────────────────────────────────


def _formula(text: str, df: pd.DataFrame) -> Formula:
    try:
        return Formula(text, list(df.columns))
    except FormulaError as e:
        raise StepError(str(e)) from e


def _mask(result, df: pd.DataFrame) -> pd.Series:
    if not isinstance(result, pd.Series):
        return pd.Series(bool(result), index=df.index)
    if not pd.api.types.is_bool_dtype(result):
        raise StepError("That condition doesn't say yes or no for each row; compare it, for example [Revenue] > 100.")
    return result.fillna(False).astype(bool)


def _operand(text: str, df: pd.DataFrame):
    """A rule's result: a quoted or plain value, or a formula over columns."""
    t = text.strip()
    if re.fullmatch(r"'[^']*'|\"[^\"]*\"", t):
        return t[1:-1]
    if re.fullmatch(r"[-+]?\d+(\.\d+)?", t):
        return _value(t)
    try:
        return Formula(t, list(df.columns)).evaluate(df)
    except FormulaError:
        return t  # plain words: High, Low, Churned


def _select(conditions, choices, fallback, n):
    out = pd.Series([None] * n, dtype="object")
    filled = np.zeros(n, dtype=bool)
    for cond, choice in zip(conditions, choices, strict=True):
        take = cond & ~filled
        values = choice.to_numpy() if isinstance(choice, pd.Series) else np.full(n, choice, dtype=object)
        out[take] = values[take]
        filled |= take
    if fallback is not None:
        values = fallback.to_numpy() if isinstance(fallback, pd.Series) else np.full(n, fallback, dtype=object)
        out[~filled] = values[~filled]
    return out.to_numpy()


def _column(values, df: pd.DataFrame) -> pd.Series:
    """A result as a tidy column: numbers as numbers (whole ones as integers), text as text."""
    s = values if isinstance(values, pd.Series) else pd.Series(values, index=df.index)
    if pd.api.types.is_bool_dtype(s):
        return s.map({True: "Yes", False: "No"}).astype("object")
    if pd.api.types.is_numeric_dtype(s):
        return as_number_dtype(s.astype("float64").replace([np.inf, -np.inf], np.nan))
    if pd.api.types.is_datetime64_any_dtype(s):
        return s
    if s.dropna().map(lambda v: isinstance(v, int | float) and not isinstance(v, bool)).all() and s.notna().any():
        return as_number_dtype(pd.to_numeric(s, errors="coerce").astype("float64"))
    return s.astype("object").where(s.notna(), None)


def _as_text(s: pd.Series) -> pd.Series:
    if pd.api.types.is_datetime64_any_dtype(s):
        return s.dt.strftime("%Y-%m-%d").astype("string")
    return s.astype("string")


def _band_labels(edges: list[float]) -> list[str]:
    fmt = lambda v: format_value(v)
    labels = [f"Under {fmt(edges[0])}"]
    labels += [f"{fmt(a)}–{fmt(b)}" for a, b in pairwise(edges)]
    labels.append(f"{fmt(edges[-1])}+")
    return labels
