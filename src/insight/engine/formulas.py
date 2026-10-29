"""Formulas over a table's columns: `[Revenue] - [Cost]`, `[Region] = 'North' and [Units] > 5`.

A formula is how a person writes a derived column, a row filter or an if/else
rule. It is parsed into Python's own expression tree and then walked by a
small evaluator that knows only what is listed here: arithmetic, comparisons,
and/or/not, a handful of functions, numbers, quoted text and column names.
Anything else (attributes, subscripts, lambdas, comprehensions, any name that
is not a column or a listed function) is refused before evaluation, so a
formula can never run code.

Spreadsheet spellings are accepted: `=` for equality, `<>` for not-equal,
AND/OR/NOT in capitals, and columns in [square brackets] (needed when a name
has spaces) or bare (when it is a single word).
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable

import numpy as np
import pandas as pd

MAX_LENGTH = 500


class FormulaError(ValueError):
    """The formula cannot be read or refers to something that isn't there; the message says which."""


def _days(a: pd.Series, b: pd.Series) -> pd.Series:
    return (pd.to_datetime(a) - pd.to_datetime(b)).dt.total_seconds() / 86400


def _num(s):
    return pd.to_numeric(s, errors="coerce") if isinstance(s, pd.Series) else s


def _text(s):
    return s.astype("string") if isinstance(s, pd.Series) else str(s)


FUNCTIONS: dict[str, Callable] = {
    "round": lambda x, digits=0: _num(x).round(int(digits)) if isinstance(x, pd.Series) else round(x, int(digits)),
    "abs": lambda x: _num(x).abs() if isinstance(x, pd.Series) else abs(x),
    "min": lambda *xs: pd.concat([_num(x) if isinstance(x, pd.Series) else pd.Series(x, index=_index(xs)) for x in xs], axis=1).min(axis=1),
    "max": lambda *xs: pd.concat([_num(x) if isinstance(x, pd.Series) else pd.Series(x, index=_index(xs)) for x in xs], axis=1).max(axis=1),
    "lower": lambda x: _text(x).str.lower() if isinstance(x, pd.Series) else str(x).lower(),
    "upper": lambda x: _text(x).str.upper() if isinstance(x, pd.Series) else str(x).upper(),
    "trim": lambda x: _text(x).str.strip() if isinstance(x, pd.Series) else str(x).strip(),
    "len": lambda x: _text(x).str.len() if isinstance(x, pd.Series) else len(str(x)),
    "contains": lambda x, part: _text(x).str.contains(str(part), case=False, regex=False).fillna(False),
    "startswith": lambda x, part: _text(x).str.lower().str.startswith(str(part).lower()).fillna(False),
    "coalesce": lambda x, fallback: x.where(x.notna(), fallback) if isinstance(x, pd.Series) else x,
    "isblank": lambda x: x.isna() if isinstance(x, pd.Series) else x is None,
    "year": lambda x: pd.to_datetime(x).dt.year.astype("Int64"),
    "month": lambda x: pd.to_datetime(x).dt.month.astype("Int64"),
    "day": lambda x: pd.to_datetime(x).dt.day.astype("Int64"),
    "weekday": lambda x: pd.to_datetime(x).dt.day_name().str[:3],
    "days": _days,
    "today": lambda: pd.Timestamp.now(tz="UTC").tz_localize(None).normalize(),
}
FUNCTION_HELP = (
    "ROUND, ABS, MIN, MAX, LOWER, UPPER, TRIM, LEN, CONTAINS, STARTSWITH, COALESCE, ISBLANK, "
    "YEAR, MONTH, DAY, WEEKDAY, DAYS(end, start), TODAY()"
)


def _index(values) -> pd.Index:
    for v in values:
        if isinstance(v, pd.Series):
            return v.index
    return pd.RangeIndex(1)


_ALLOWED = (
    ast.Expression, ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare, ast.Call, ast.Name, ast.Load, ast.Constant,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod, ast.Pow, ast.FloorDiv, ast.USub, ast.UAdd, ast.Not, ast.And, ast.Or,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn, ast.List, ast.Tuple,
)  # fmt: skip


class Formula:
    """A parsed formula, checked against a table's columns, ready to evaluate."""

    def __init__(self, text: str, columns: list[str]) -> None:
        self.text = text.strip()
        if not self.text:
            raise FormulaError("Write a formula, for example [Revenue] - [Cost].")
        if len(self.text) > MAX_LENGTH:
            raise FormulaError("That formula is too long.")
        self.columns = list(columns)
        source, self._refs = _prepare(self.text, self.columns)
        try:
            self._tree = ast.parse(source, mode="eval")
        except SyntaxError as e:
            raise FormulaError(f"The formula couldn't be read near “{_near(self.text, e)}”.") from e
        if isinstance(self._tree.body, ast.Tuple):
            raise FormulaError("Separate thousands without commas (2000, not 2,000), or put lists in [brackets].")
        for node in ast.walk(self._tree):
            if not isinstance(node, _ALLOWED):
                raise FormulaError("Formulas can use columns, numbers, quoted text, + − × ÷, comparisons, and/or/not and functions.")
            if isinstance(node, ast.Call) and not (isinstance(node.func, ast.Name) and node.func.id in FUNCTIONS):
                raise FormulaError(f"Unknown function. You can use {FUNCTION_HELP}.")
            if isinstance(node, ast.Name) and node.id not in self._refs and node.id not in FUNCTIONS:
                raise FormulaError(f"There is no column called “{node.id}”. Put names with spaces in [square brackets].")

    @property
    def used_columns(self) -> list[str]:
        return list(dict.fromkeys(self._refs.values()))

    def evaluate(self, df: pd.DataFrame):
        try:
            with np.errstate(all="ignore"):
                return self._eval(self._tree.body, df)
        except FormulaError:
            raise
        except Exception as e:  # type errors between columns, a bad date…: one message for the reader
            raise FormulaError(f"The formula couldn't be worked out on these rows ({type(e).__name__}).") from e

    def _eval(self, node: ast.AST, df: pd.DataFrame):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            return df[self._refs[node.id]]
        if isinstance(node, ast.List | ast.Tuple):
            return [self._eval(e, df) for e in node.elts]
        if isinstance(node, ast.UnaryOp):
            v = self._eval(node.operand, df)
            if isinstance(node.op, ast.Not):
                return ~_bool(v) if isinstance(v, pd.Series) else not v
            v = _num(v)
            return -v if isinstance(node.op, ast.USub) else v
        if isinstance(node, ast.BinOp):
            a, b = self._eval(node.left, df), self._eval(node.right, df)
            if isinstance(node.op, ast.Add) and (_is_text(a) or _is_text(b)):
                return _text(a) + _text(b)
            a, b = _num(a), _num(b)
            ops = {ast.Add: lambda: a + b, ast.Sub: lambda: a - b, ast.Mult: lambda: a * b, ast.Div: lambda: _divide(a, b),
                   ast.Mod: lambda: a % b, ast.Pow: lambda: a**b, ast.FloorDiv: lambda: a // b}  # fmt: skip
            return ops[type(node.op)]()
        if isinstance(node, ast.BoolOp):
            values = [_bool(self._eval(v, df)) for v in node.values]
            out = values[0]
            for v in values[1:]:
                out = (out & v) if isinstance(node.op, ast.And) else (out | v)
            return out
        if isinstance(node, ast.Compare):
            left = self._eval(node.left, df)
            result = None
            for op, right_node in zip(node.ops, node.comparators, strict=True):
                right = self._eval(right_node, df)
                part = _compare(left, op, right)
                result = part if result is None else (result & part)
                left = right
            return result
        if isinstance(node, ast.Call):
            args = [self._eval(a, df) for a in node.args]
            return FUNCTIONS[node.func.id](*args)  # type: ignore[union-attr]
        raise FormulaError("That part of the formula isn't supported.")


def _prepare(text: str, columns: list[str]) -> tuple[str, dict[str, str]]:
    """Rewrite spreadsheet spellings into Python, and every column into a safe placeholder name."""
    refs: dict[str, str] = {}
    lookup = {c.lower(): c for c in columns}
    by_words = {re.sub(r"[^a-z0-9]+", "_", c.lower()).strip("_"): c for c in columns}
    out: list[str] = []
    i = 0
    # [Column name]; a bracket that opens with a quote or a digit is a list: [Region] in ['North', 'East'].
    for m in re.finditer(r"\[(?!\s*['\"\d-])([^\]]+)\]|'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"|[A-Za-z_][A-Za-z0-9_]*", text):
        out.append(_operators(text[i : m.start()]))
        token = m.group(0)
        if m.group(1) is not None:  # [Column name]
            name = lookup.get(m.group(1).strip().lower())
            if name is None:
                raise FormulaError(f"There is no column called “{m.group(1).strip()}”.")
            out.append(_ref(refs, name))
        elif token[0] in "'\"":
            out.append(token)
        else:
            low = token.lower()
            if low in {"and", "or", "not", "in"}:
                out.append(f" {low} ")
            elif low in {"true", "false"}:
                out.append("True" if low == "true" else "False")
            elif low in FUNCTIONS and text[m.end() :].lstrip().startswith("("):
                out.append(low)
            elif low in lookup:
                out.append(_ref(refs, lookup[low]))
            elif low in by_words:
                out.append(_ref(refs, by_words[low]))
            else:
                out.append(token)  # an unknown name: refused, by name, once parsed
        i = m.end()
    out.append(_operators(text[i:]))
    return "".join(out).strip(), refs


def _ref(refs: dict[str, str], column: str) -> str:
    for key, value in refs.items():
        if value == column:
            return key
    key = f"__c{len(refs)}"
    refs[key] = column
    return key


def _operators(s: str) -> str:
    s = re.sub(r"(?<![\w.,])(\d{1,3}(?:,\d{3})+)(?![\d,])", lambda m: m.group(1).replace(",", ""), s)  # 2,000 is two thousand
    s = s.replace("<>", "!=").replace("≠", "!=").replace("≥", ">=").replace("≤", "<=").replace("×", "*").replace("÷", "/")
    s = s.replace("^", "**")
    return re.sub(r"(?<![<>=!])=(?!=)", "==", s)


def _near(text: str, e: SyntaxError) -> str:
    offset = max(0, (e.offset or 1) - 1)
    return text[max(0, offset - 10) : offset + 10].strip() or text[:20]


def _is_text(v) -> bool:
    if isinstance(v, str):
        return True
    return isinstance(v, pd.Series) and (pd.api.types.is_object_dtype(v) or pd.api.types.is_string_dtype(v))


def _bool(v):
    if isinstance(v, pd.Series):
        return v.fillna(False).astype(bool)
    return bool(v)


def _divide(a, b):
    if isinstance(b, pd.Series):
        return a / b.where(b != 0)
    return a / b if b else np.nan


def _compare(a, op: ast.cmpop, b):
    if isinstance(op, ast.In | ast.NotIn):
        values = b if isinstance(b, list) else [b]
        if _is_text(a) and all(isinstance(v, str) for v in values):  # without regard to case, like =
            a, values = (a.astype("string").str.lower() if isinstance(a, pd.Series) else a.lower()), [v.lower() for v in values]
        hit = a.isin(values).fillna(False).astype(bool) if isinstance(a, pd.Series) else a in values
        return ~hit if isinstance(op, ast.NotIn) else hit
    text = isinstance(a, str) or isinstance(b, str)
    if text:  # compare text without regard to case: [Region] = 'north' finds North
        a = a.astype("string").str.lower() if isinstance(a, pd.Series) else str(a).lower()
        b = b.astype("string").str.lower() if isinstance(b, pd.Series) else str(b).lower()
    elif not (_is_date(a) or _is_date(b)):
        a, b = _num(a), _num(b)
    else:
        a = pd.to_datetime(a) if not isinstance(a, pd.Series) or not _is_date(a) else a
        b = pd.to_datetime(b) if not isinstance(b, pd.Series) or not _is_date(b) else b
    result = {ast.Eq: lambda: a == b, ast.NotEq: lambda: a != b, ast.Lt: lambda: a < b, ast.LtE: lambda: a <= b,
              ast.Gt: lambda: a > b, ast.GtE: lambda: a >= b}[type(op)]()  # fmt: skip
    return result.fillna(False).astype(bool) if isinstance(result, pd.Series) else result


def _is_date(v) -> bool:
    return (isinstance(v, pd.Series) and pd.api.types.is_datetime64_any_dtype(v)) or isinstance(v, pd.Timestamp)
