"""The Transform data forms: which fields each kind of step asks for, and the step they build.

Each form is the same few controls (a column picker, a text box, a choice)
arranged per kind. `build` turns a submitted form back into a step dict, which
transforms.parse_step then validates like any other.
"""

from __future__ import annotations

import re

from starlette.datastructures import FormData

from insight.engine.transform_language import condition_formula
from insight.engine.transforms import AGG_LABELS, DATE_PARTS, KIND_LABELS

# The common jobs first, the rest under "More". Each: (kind, one-line hint).
PRIMARY = [
    ("filter", "Keep or remove rows that match a condition"),
    ("derive", "A new column from a formula"),
    ("conditional", "If … then … else, as a new column"),
    ("group", "One row per group, with totals or averages"),
    ("rename", "Give a column a clearer name"),
    ("split", "“City, ST” into two columns"),
]
MORE = [
    "datepart",
    "bucket",
    "retype",
    "replace",
    "fill",
    "text",
    "merge",
    "dedupe",
    "blanks",
    "sort",
    "limit",
    "drop",
    "keep",
    "unpivot",
    "pivot",
    "index",
]

GROUP_FNS = list(AGG_LABELS.items())
MAX_RULES = 4
MAX_AGGREGATES = 5


def _list(form: FormData, key: str) -> list[str]:
    return [v for v in (str(x) for x in form.getlist(key)) if v]


def _names(text: str) -> list[str]:
    return [n.strip() for n in re.split(r"\s*,\s*", text or "") if n.strip()]


def build(kind: str, form: FormData, columns: list[str]) -> dict:
    f = lambda key, default="": str(form.get(key, default) or default).strip()
    if kind == "rename":
        return {"kind": kind, "column": f("column"), "to": f("to")}
    if kind in ("drop", "keep", "blanks", "dedupe"):
        return {"kind": kind, "columns": _list(form, "columns")}
    if kind == "derive":
        return {"kind": kind, "name": f("name"), "formula": f("formula")}
    if kind == "retype":
        return {"kind": kind, "column": f("column"), "to": f("to", "number")}
    if kind == "filter":
        return {"kind": kind, "condition": condition_formula(f("condition"), columns), "exclude": f("mode") == "remove"}
    if kind == "conditional":
        rules = [
            {"when": condition_formula(f(f"when_{i}"), columns), "then": f(f"then_{i}")}
            for i in range(1, MAX_RULES + 1)
            if f(f"when_{i}") and f(f"then_{i}")
        ]
        target = f("target", "new")
        return {
            "kind": kind,
            "name": f("name") if target == "new" else "",
            "column": f("column") if target == "column" else "",
            "rules": rules,
            "otherwise": f("otherwise"),
        }
    if kind == "bucket":
        edges = [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", f("edges").replace(",", " "))]
        return {"kind": kind, "column": f("column"), "edges": edges, "labels": _names(f("labels")), "name": f("name")}
    if kind == "datepart":
        return {"kind": kind, "column": f("column"), "part": f("part", "month"), "name": f("name")}
    if kind == "split":
        sep = form.get("separator") or ","
        return {
            "kind": kind,
            "column": f("column"),
            "separator": str(sep),
            "into": _names(f("into")),
            "drop_original": bool(form.get("drop_original")),
        }
    if kind == "merge":
        return {"kind": kind, "columns": _list(form, "columns"), "separator": str(form.get("separator", " ")), "name": f("name")}
    if kind == "text":
        return {"kind": kind, "column": f("column"), "op": f("op", "trim")}
    if kind == "replace":
        return {"kind": kind, "column": f("column"), "find": f("find"), "replacement": f("replacement"), "mode": f("mode", "value")}
    if kind == "fill":
        return {"kind": kind, "column": f("column"), "method": f("method", "value"), "value": f("value")}
    if kind == "sort":
        return {"kind": kind, "by": [{"column": f("column"), "descending": f("direction") == "desc"}]}
    if kind == "limit":
        return {"kind": kind, "count": int(f("count", "10") or 10), "by": f("by"), "descending": f("direction", "desc") == "desc"}
    if kind == "group":
        aggs = []
        for i in range(1, MAX_AGGREGATES + 1):
            fn = f(f"fn_{i}")
            if fn and (fn == "count" or f(f"column_{i}")):
                aggs.append({"fn": fn, "column": f(f"column_{i}") if fn != "count" else ""})
        return {"kind": kind, "by": _list(form, "by"), "aggregates": aggs}
    if kind == "unpivot":
        return {
            "kind": kind,
            "columns": _list(form, "columns"),
            "name_column": f("name_column", "Attribute"),
            "value_column": f("value_column", "Value"),
        }
    if kind == "pivot":
        return {"kind": kind, "column": f("column"), "measure": f("measure"), "fn": f("fn", "sum")}
    if kind == "index":
        return {"kind": kind, "name": f("name", "Row number")}
    raise ValueError("Choose what kind of step to add.")


__all__ = ["DATE_PARTS", "GROUP_FNS", "KIND_LABELS", "MAX_AGGREGATES", "MAX_RULES", "MORE", "PRIMARY", "build"]
