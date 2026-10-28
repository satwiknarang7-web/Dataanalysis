"""Cleaning decides what a value *is*, and says so.

Every decision made here is one the reader can see on the Cleaning report and
disagree with: which columns became numbers or dates, which spellings were
folded into one, what personal data was redacted, which cells were dropped.

The work is vectorised over each column's *distinct* values (a column of a
million rows usually has a few thousand), then mapped back onto the rows.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import ClassVar

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field

from insight.engine.formatting import name_words

MISSING_TOKENS = {"", "na", "n/a", "nan", "null", "none", "nil", "-", "--", "?", "#n/a", "(blank)"}
REDACTED = re.compile(r"^\[?\s*redacted[\w\s-]*\]?$", re.I)
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# A phone number written as one: separators between the groups, or a leading +.
PHONE = re.compile(r"(?<!\w)(?:\+\d{1,3}[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?!\w)|(?<!\w)\+\d{7,14}(?!\w)")
PHONE_NAME = re.compile(r"\b(phone|mobile|cell|tel|telephone|whatsapp|fax|contact no|contact number)\b")
BOOL_YES = {"yes", "y", "true", "t"}
BOOL_NO = {"no", "n", "false", "f"}
SUFFIX = {"k": 1e3, "m": 1e6, "mn": 1e6, "b": 1e9, "bn": 1e9}
# A number as people write it: (1,234.50), $3M, 12%, -0.5, 2.5bn.
NUMBER = re.compile(
    r"^(?P<open>\()?\s*(?P<sign>[+-])?\s*[$€£₹¥]?\s*(?P<sign2>[+-])?"
    r"(?P<num>(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|\.\d+)(?:[eE](?P<exp>[+-]?\d+))?"
    r"\s*(?P<suffix>k|m|mn|b|bn)?\s*(?P<pct>%)?\s*(?P<close>\))?$",
    re.I,
)
EURO_NUMBER = re.compile(r"^-?\d{1,3}(?:\.\d{3})*,\d+$|^-?\d+,\d+$")
MAX_CATEGORY_LEVELS = 500

ISO_FORMATS = ["%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y/%m/%d", "%Y-%m"]
WORD_FORMATS = [
    "%d %b %Y",
    "%d %B %Y",
    "%d-%b-%Y",
    "%d-%b-%y",
    "%b %d, %Y",
    "%B %d, %Y",
    "%b %d %Y",
    "%d.%m.%Y",
    "%B %Y",
    "%b %Y",
    "%b-%y",
    "%b-%Y",
]
SLASH_MDY = ["%m/%d/%Y", "%m/%d/%y", "%m/%d/%Y %H:%M", "%m/%d/%Y %H:%M:%S", "%m-%d-%Y"]
SLASH_DMY = ["%d/%m/%Y", "%d/%m/%y", "%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S", "%d-%m-%Y"]
SLASH = re.compile(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})")


class ColumnReport(BaseModel):
    name: str
    kind: str
    missing: int = 0
    changed: int = 0
    guessed: bool = False
    unit: str | None = None
    notes: list[str] = Field(default_factory=list)


class Check(BaseModel):
    key: str
    title: str
    count: int
    detail: list[str] = Field(default_factory=list)


class Merge(BaseModel):
    column: str
    into: str
    spellings: list[str]


class CleaningReport(BaseModel):
    rows_in: int
    rows_out: int
    columns_in: int
    columns: list[ColumnReport]
    checks: list[Check]
    merges: list[Merge]
    duplicate_rows: int = 0

    @property
    def verdict(self) -> str:
        # Reading text as numbers or dates is how a CSV is read, not a fix.
        changed = [c for c in self.checks if c.count and c.key not in ("numbers", "dates")]
        guessed = [c.name for c in self.columns if c.guessed]
        if not changed and not guessed:
            return "Read cleanly: nothing needed fixing."
        parts = [f"{len(changed)} kind{'s' if len(changed) != 1 else ''} of fix applied"] if changed else []
        if guessed:
            parts.append(f"{len(guessed)} column{'s' if len(guessed) != 1 else ''} read with a guess")
        return "; ".join(parts) + "."

    def percent_columns(self) -> set[str]:
        return {c.name for c in self.columns if c.unit == "%"}


class _Checks:
    """Counts each kind of change, with a few examples of each."""

    TITLES: ClassVar[dict[str, str]] = {
        "headers": "Header cells renamed",
        "empty_columns": "Empty columns removed",
        "empty_rows": "Empty rows removed",
        "missing_tokens": "Placeholders read as missing",
        "redacted": "Values redacted before the file arrived",
        "pii": "Personal data redacted",
        "numbers": "Text read as numbers",
        "stray": "Stray values in a number or date column set missing",
        "dates": "Text read as dates",
        "booleans": "Yes/no spellings made one",
        "categories": "Spellings of one category made one",
    }

    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()
        self.detail: dict[str, list[str]] = {}

    def add(self, key: str, n: int, example: str | None = None) -> None:
        if n <= 0:
            return
        self.counts[key] += n
        if example:
            ex = self.detail.setdefault(key, [])
            if len(ex) < 6:
                ex.append(example)

    def report(self) -> list[Check]:
        return [Check(key=k, title=t, count=self.counts[k], detail=self.detail.get(k, [])) for k, t in self.TITLES.items()]


def clean(raw: pd.DataFrame) -> tuple[pd.DataFrame, CleaningReport]:
    """Clean a freshly read table. Returns the cleaned frame and what was done."""
    checks = _Checks()
    rows_in, columns_in = len(raw), raw.shape[1]
    df = raw.copy()
    df.columns = _fix_headers(list(df.columns), checks)

    # Text columns: blank and placeholder cells become missing first, so an
    # empty column is found whatever it was filled with.
    for name in df.columns:
        if _is_text(df[name]):
            df[name] = _normalise_missing(df[name], name, checks)

    empty_cols = [c for c in df.columns if df[c].isna().all()]
    if empty_cols:
        checks.add("empty_columns", len(empty_cols), ", ".join(map(str, empty_cols[:6])))
        df = df.drop(columns=empty_cols)
    empty_rows = df.isna().all(axis=1)
    checks.add("empty_rows", int(empty_rows.sum()))
    df = df.loc[~empty_rows].reset_index(drop=True)

    columns: list[ColumnReport] = []
    merges: list[Merge] = []
    for name in df.columns:
        series, col = _clean_column(df[name], str(name), checks, merges)
        df[name] = series
        col.missing = int(series.isna().sum())
        columns.append(col)

    report = CleaningReport(
        rows_in=rows_in,
        rows_out=len(df),
        columns_in=columns_in,
        columns=columns,
        checks=checks.report(),
        merges=merges,
        duplicate_rows=int(df.duplicated().sum()) if len(df) else 0,
    )
    return df, report


def _is_text(s: pd.Series) -> bool:
    return pd.api.types.is_object_dtype(s) or pd.api.types.is_string_dtype(s)


def _fix_headers(names: list, checks: _Checks) -> list[str]:
    out: list[str] = []
    seen: Counter[str] = Counter()
    for i, raw in enumerate(names):
        name = "" if raw is None or (isinstance(raw, float) and np.isnan(raw)) else str(raw).strip()
        if not name or re.fullmatch(r"Unnamed: \d+", name):
            name = f"Column {i + 1}"
            checks.add("headers", 1, f"column {i + 1} had no name")
        base = name
        seen[base] += 1
        if seen[base] > 1:
            name = f"{base} ({seen[base]})"
            checks.add("headers", 1, f"a second '{base}' became '{name}'")
        out.append(name)
    return out


def _normalise_missing(s: pd.Series, name: str, checks: _Checks) -> pd.Series:
    s = s.astype("object").where(s.notna(), None)
    text = s.map(lambda v: v.strip() if isinstance(v, str) else v)
    lowered = text.map(lambda v: v.lower() if isinstance(v, str) else v)
    placeholder = lowered.isin(MISSING_TOKENS) & text.notna()
    redacted = text.map(lambda v: isinstance(v, str) and bool(REDACTED.match(v)))
    checks.add("missing_tokens", int((placeholder & (text != "")).sum()), f"{name}")
    checks.add("redacted", int(redacted.sum()), f"{name}")
    return text.mask(placeholder | redacted, None)


def _clean_column(s: pd.Series, name: str, checks: _Checks, merges: list[Merge]) -> tuple[pd.Series, ColumnReport]:
    if pd.api.types.is_bool_dtype(s):
        return s.map({True: "Yes", False: "No"}).astype("object"), ColumnReport(name=name, kind="boolean")
    if pd.api.types.is_numeric_dtype(s):
        return as_number_dtype(s.astype("float64")), ColumnReport(name=name, kind="number")
    if pd.api.types.is_datetime64_any_dtype(s):
        if getattr(s.dt, "tz", None) is not None:
            s = s.dt.tz_convert("UTC").dt.tz_localize(None)
        return s.astype("datetime64[ns]"), ColumnReport(name=name, kind="date")

    s = s.astype("object").map(lambda v: None if v is None or (not isinstance(v, str) and pd.isna(v)) else str(v))
    col = ColumnReport(name=name, kind="text")
    s = _redact(s, name, col, checks)
    values = s.dropna()
    if values.empty:
        return s, col
    uniques = pd.Series(values.unique())

    if not PHONE_NAME.search(name_words(name)):
        numbers = _read_numbers(uniques, col)
        if numbers is not None:
            mapped = s.map(numbers)
            stray = int((s.notna() & mapped.isna()).sum())
            checks.add("numbers", int(mapped.notna().sum()), f"{name}")
            checks.add("stray", stray, f"{name}: {stray} cell(s)")
            col.kind, col.changed = "number", int(mapped.notna().sum())
            if stray:
                col.notes.append(f"{stray} value(s) that were not numbers set missing")
            return as_number_dtype(mapped.astype("float64")), col

    dates = _read_dates(uniques, col)
    if dates is not None:
        mapped = pd.to_datetime(s.map(dates), errors="coerce")
        stray = int((s.notna() & mapped.isna()).sum())
        checks.add("dates", int(mapped.notna().sum()), f"{name}")
        checks.add("stray", stray, f"{name}: {stray} cell(s)")
        col.kind, col.changed = "date", int(mapped.notna().sum())
        return mapped.astype("datetime64[ns]"), col

    lowered = set(uniques.str.lower())
    if lowered <= (BOOL_YES | BOOL_NO) and lowered & BOOL_YES and lowered & BOOL_NO:
        mapped = s.map(lambda v: None if v is None else "Yes" if v.lower() in BOOL_YES else "No")
        changed = int((mapped != s).sum())
        checks.add("booleans", changed, f"{name}: {', '.join(sorted(uniques)[:4])} → Yes / No")
        col.kind, col.changed = "boolean", changed
        return mapped, col

    return _unify_categories(s, uniques, name, col, checks, merges), col


def _redact(s: pd.Series, name: str, col: ColumnReport, checks: _Checks) -> pd.Series:
    """Emails anywhere; phone numbers written as one, or in a column named for phones."""
    phone_column = bool(PHONE_NAME.search(name_words(name)))

    def scrub(v: str | None) -> str | None:
        if v is None:
            return None
        out = EMAIL.sub("[email]", v)
        if phone_column and re.search(r"\d{6,}", re.sub(r"[\s().+-]", "", out)):
            return "[phone]"
        return PHONE.sub("[phone]", out)

    uniques = s.dropna().unique()
    changed = {v: scrub(v) for v in uniques}
    changed = {k: v for k, v in changed.items() if k != v}
    if not changed:
        return s
    n = int(s.isin(list(changed)).sum())
    checks.add("pii", n, f"{name}: {n} cell(s)")
    col.notes.append(f"{n} cell(s) of personal data redacted")
    col.changed += n
    return s.map(lambda v: changed.get(v, v))


def _parse_number(text: str, european: bool) -> tuple[float | None, bool, bool]:
    """A number as written, with whether it used a scale letter and a percent sign."""
    t = text.strip()
    if european and EURO_NUMBER.match(t):
        t = t.replace(".", "").replace(",", ".")
    m = NUMBER.match(t)
    if not m or bool(m.group("open")) != bool(m.group("close")):
        return None, False, False
    x = float(m.group("num").replace(",", ""))
    if m.group("exp"):
        x *= 10 ** int(m.group("exp"))
    suffix = (m.group("suffix") or "").lower()
    if suffix:
        x *= SUFFIX[suffix]
    if m.group("open") or "-" in ((m.group("sign") or "") + (m.group("sign2") or "")):
        x = -x
    return x, bool(suffix), bool(m.group("pct"))


def _read_numbers(uniques: pd.Series, col: ColumnReport) -> dict[str, float] | None:
    """Read a text column as numbers when nearly every value is one.

    Leading zeros (00123) mark an identifier, which stays text; scale letters
    are honoured only when most values are plain numbers, so a column of
    sizes (5M, XL) stays a column of sizes.
    """
    sample = uniques.head(5000)
    if (sample.str.match(r"^0\d+$")).mean() > 0.2:
        return None
    european = sample.map(lambda v: bool(EURO_NUMBER.match(v.strip()))).mean() > 0.5
    parsed = {v: _parse_number(v, european) for v in uniques}
    ok = {v: p for v, p in parsed.items() if p[0] is not None}
    if len(ok) < 0.95 * len(uniques):
        return None
    suffixed = sum(1 for p in ok.values() if p[1])
    if suffixed and suffixed > 0.5 * len(ok) and len(ok) < 0.99 * len(uniques):
        return None
    if sum(1 for p in ok.values() if p[2]) > 0.5 * len(ok):
        col.unit = "%"
    if european:
        col.notes.append("decimal commas read as decimal points (1.234,5 → 1234.5)")
    if suffixed:
        col.notes.append("scale letters read (1.2K → 1200)")
    return {v: p[0] for v, p in ok.items()}


def _read_dates(uniques: pd.Series, col: ColumnReport) -> dict[str, pd.Timestamp] | None:
    """Read a text column as dates when 90% of values are one, in UTC."""
    if uniques.str.fullmatch(r"\d+(\.\d+)?").mean() > 0.5:
        return None  # numbers, not dates: 2024 is a year column, read as a number
    parsed = pd.to_datetime(uniques, format="ISO8601", errors="coerce", utc=True)
    out = pd.Series(parsed.values, index=uniques.values)

    slashed = uniques[uniques.str.match(SLASH)]
    formats = list(ISO_FORMATS) + list(WORD_FORMATS)
    if len(slashed):
        parts = slashed.str.extract(SLASH).astype(int)
        day_first = (parts[0] > 12).any() and not (parts[1] > 12).any()
        month_first = (parts[1] > 12).any() and not (parts[0] > 12).any()
        formats += SLASH_DMY if day_first else SLASH_MDY
        if not day_first and not month_first:
            col.guessed = True
            col.notes.append("dates like 03/04 could be read either way: read as month/day")

    for fmt in formats:
        missing = out.isna()
        if not missing.any():
            break
        todo = pd.Series(out.index[missing.values])
        got = pd.to_datetime(todo, format=fmt, errors="coerce", utc=True)
        out.loc[todo[got.notna()].values] = got[got.notna()].values

    if out.notna().mean() < 0.9:
        return None
    out = out.dropna()
    return {k: _naive_utc(pd.Timestamp(v)) for k, v in out.items()}


def _naive_utc(t: pd.Timestamp) -> pd.Timestamp:
    return t if t.tzinfo is None else t.tz_convert("UTC").tz_localize(None)


def as_number_dtype(s: pd.Series) -> pd.Series:
    """Whole numbers as nullable integers, so 2024 stays "2024", not "2024.0"."""
    filled = s.dropna()
    if len(filled) and np.all(np.isfinite(filled)) and np.all(filled == np.round(filled)) and filled.abs().max() < 2**53:
        return s.round().astype("Int64")
    return s


def _unify_categories(s: pd.Series, uniques: pd.Series, name: str, col: ColumnReport, checks: _Checks, merges: list[Merge]) -> pd.Series:
    """`North`, `north` and ` NORTH ` become the most frequent spelling.

    Never in a column with more distinct values than a category can have: an
    id column folded by case would merge things that are different.
    """
    if len(uniques) > MAX_CATEGORY_LEVELS:
        return s
    counts = s.value_counts()
    groups: dict[str, list[str]] = {}
    for v in counts.index:
        groups.setdefault(re.sub(r"\s+", " ", v).strip().casefold(), []).append(v)
    mapping: dict[str, str] = {}
    for spellings in groups.values():
        if len(spellings) > 1:
            into = spellings[0]  # value_counts is most frequent first
            mapping.update({v: into for v in spellings[1:]})
            merges.append(Merge(column=name, into=into, spellings=spellings[1:]))
    if not mapping:
        return s
    n = int(s.isin(list(mapping)).sum())
    checks.add("categories", n, f"{name}: {len(mapping)} spelling(s)")
    col.changed += n
    col.notes.append(f"{len(mapping)} spelling(s) made one")
    return s.map(lambda v: mapping.get(v, v))
