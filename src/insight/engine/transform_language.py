"""A step typed as a sentence: "split City on the comma into Town and State".

Read with rules, no model: each rule is one shape people write, matched
against the real columns, so "group by region: total revenue, number of
orders" becomes a Group step over the columns this table actually has. A
sentence no rule can read is refused with examples, never guessed at.
"""

from __future__ import annotations

import math
import re

from insight.engine.formatting import label_of, lower_label, name_words
from insight.engine.formulas import Formula, FormulaError
from insight.engine.models import Reading
from insight.engine.transforms import DATE_PARTS, Step, parse_step

QUANTITY = re.compile(r"\b(qty|quantity|units|units sold|items|pieces)\b")


class PhraseError(ValueError):
    pass


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s).lower()).strip()


def match_column(text: str, columns: list[str]) -> str | None:
    """The column a phrase names: exactly, by its readable label, or by the one column it uniquely starts."""
    t = _norm(text.strip().strip("[]\"'“”"))
    if not t:
        return None
    for c in columns:
        if _norm(c) == t or _norm(label_of(c)) == t:
            return c
    words = set(t.split())
    for pattern in (
        lambda c: _norm(c).startswith(t + " ") or _norm(c) == t,
        lambda c: words <= set(_norm(c).split()) | set(_norm(label_of(c)).split()),
    ):
        hits = [c for c in columns if pattern(c)]
        if len(hits) == 1:
            return hits[0]
    singular = t[:-1] if t.endswith("s") else t
    hits = [c for c in columns if _norm(c) in (singular, singular + "s")]
    return hits[0] if len(hits) == 1 else None


def _column(text: str, columns: list[str]) -> str:
    c = match_column(text, columns)
    if not c:
        raise PhraseError(f"There is no column called “{text.strip()}”.")
    return c


def _columns(text: str, columns: list[str]) -> list[str]:
    parts = [p for p in re.split(r"\s*,\s*|\s+and\s+|\s*&\s*", text.strip()) if p]
    return [_column(p, columns) for p in parts]


def _literal(value: str) -> str:
    v = value.strip().strip("“”")
    if re.fullmatch(r"'[^']*'|\"[^\"]*\"", v):
        return "'" + v[1:-1].replace("'", "") + "'"
    if re.fullmatch(r"[-+]?[\d,]*\.?\d+%?", v):
        n = v.replace(",", "")
        return str(float(n[:-1]) / 100) if n.endswith("%") else n
    return "'" + v.replace("'", "") + "'"


# Word operators need spaces round them ("is North" is not "is no" + "rth"); symbols don't.
COMPARATORS = [
    (r"\s+(?:is\s+)?(?:greater than or equal to|at least|no less than)\s+|\s*(?:>=|≥)\s*", ">="),
    (r"\s+(?:is\s+)?(?:less than or equal to|at most|no more than)\s+|\s*(?:<=|≤)\s*", "<="),
    (r"\s+(?:is\s+)?(?:greater than|more than|over|above|higher than|exceeds)\s+|\s*>\s*", ">"),
    (r"\s+(?:is\s+)?(?:less than|under|below|lower than)\s+|\s*<(?!>)\s*", "<"),
    (r"\s+(?:is not|isn't|does not equal|doesn't equal|is not equal to|not equal to)\s+|\s*(?:!=|<>|≠)\s*", "<>"),
    (r"\s+(?:is|equals|is equal to)\s+|\s*==?\s*", "="),
]


def condition_formula(text: str, columns: list[str]) -> str:
    """A condition in words as a formula: "revenue is over 1,000 and region is North"."""
    text = text.strip().rstrip(".")
    try:  # already a formula
        Formula(text, columns)
        return text
    except FormulaError:
        pass
    if re.search(r"\bbetween\b", text):
        return _one_condition(text, columns)
    parts = re.split(r"\s+(and|or)\s+", text)
    out = [_one_condition(parts[0], columns)]
    for joiner, part in zip(parts[1::2], parts[2::2], strict=False):
        try:
            out.append(f"{joiner} {_one_condition(part, columns)}")
        except PhraseError:
            # "region is North or South": a bare value continues the comparison before it.
            prev = re.fullmatch(r"\[(.+)\] (=|in) (.+)", out[-1].removeprefix(f"{joiner} ").removeprefix("and ").removeprefix("or "))
            if joiner != "or" or not prev:
                raise
            values = prev[3].strip("[]")
            head = out[-1][: out[-1].index("[")]
            out[-1] = f"{head}[{prev[1]}] in [{values}, {_literal(part)}]"
    return " ".join(out)


def _one_condition(text: str, columns: list[str]) -> str:
    t = text.strip()
    if m := re.match(r"^(.+?)\s+(?:is|are)\s+(not\s+)?(?:blank|empty|missing|null)$", t, re.I):
        return f"{'not ' if m.group(2) else ''}ISBLANK([{_column(m.group(1), columns)}])"
    if m := re.match(r"^(.+?)\s+(?:is\s+)?between\s+(.+?)\s+and\s+(.+)$", t, re.I):
        c = _column(m.group(1), columns)
        return f"[{c}] >= {_literal(m.group(2))} and [{c}] <= {_literal(m.group(3))}"
    if m := re.match(r"^(.+?)\s+(not\s+)?contains?\s+(.+)$", t, re.I):
        return f"{'not ' if m.group(2) else ''}CONTAINS([{_column(m.group(1), columns)}], {_literal(m.group(3))})"
    if m := re.match(r"^(.+?)\s+(?:is\s+)?(not\s+)?(?:in|one of)\s+(.+)$", t, re.I):
        values = [v for v in re.split(r"\s*,\s*|\s+or\s+", m.group(3)) if v]
        return f"[{_column(m.group(1), columns)}] {'not in' if m.group(2) else 'in'} [{', '.join(_literal(v) for v in values)}]"
    named = None
    for pattern, op in COMPARATORS:
        if m := re.match(rf"^(.+?)(?:{pattern})(.+)$", t, re.I):
            column = match_column(m.group(1), columns)
            named = named or m.group(1).strip()
            if column:
                values = re.split(r"\s+or\s+", m.group(2))
                if op == "=" and len(values) > 1:
                    return f"[{column}] in [{', '.join(_literal(v) for v in values)}]"
                return f"[{column}] {op} {_literal(m.group(2))}"
    if named and len(named.split()) <= 4:
        raise PhraseError(f"There is no column called “{named}”.")
    raise PhraseError(f"I couldn't read the condition “{text}”. Try “revenue is over 1000” or “region is North”.")


AGG_WORDS = [
    (r"(?:total|sum of|sum)\s+(.+)", "sum"),
    (r"(?:average|avg|mean)\s+(.+)", "avg"),
    (r"(?:median)\s+(.+)", "median"),
    (r"(?:min|minimum|lowest|smallest)\s+(.+)", "min"),
    (r"(?:max|maximum|highest|largest)\s+(.+)", "max"),
    (r"(?:distinct|unique|number of different|number of unique)\s+(.+)", "count_distinct"),
    (r"(?:first)\s+(.+)", "first"),
]


def _aggregates(text: str, columns: list[str]) -> list[dict]:
    out = []
    for part in [p for p in re.split(r"\s*,\s*|\s+and\s+", text.strip()) if p]:
        p = part.strip()
        if re.fullmatch(r"(?:count|rows|row count|number of rows|how many|number of \w+|\w+ count)", p, re.I) and not match_column(
            re.sub(r"^number of ", "", p), columns
        ):
            out.append({"fn": "count"})
            continue
        for pattern, fn in AGG_WORDS:
            if m := re.fullmatch(pattern, p, re.I):
                out.append({"fn": fn, "column": _column(m.group(1), columns)})
                break
        else:
            if m := re.fullmatch(r"(?:count of|number of)\s+(.+)", p, re.I):
                out.append({"fn": "count", "column": _column(m.group(1), columns)})
            else:
                out.append({"fn": "sum", "column": _column(p, columns)})
    return out


SEPARATORS = {"comma": ",", "commas": ",", "space": " ", "spaces": " ", "dash": "-", "hyphen": "-", "slash": "/", "semicolon": ";",
              "pipe": "|", "colon": ":", "underscore": "_", "dot": ".", "period": "."}  # fmt: skip


def _separator(text: str) -> str:
    t = text.strip().strip("“”")
    if m := re.fullmatch(r"(?:the\s+)?(?:a\s+)?(\w+)", t, re.I):
        word = m.group(1).lower()
        if word in SEPARATORS:
            return SEPARATORS[word]
    if re.fullmatch(r"'[^']+'|\"[^\"]+\"", t):
        return t[1:-1]
    return t


DATE_WORDS = [
    (r"year and month|month and year|year-month", "year_month"),
    (r"year and quarter|quarter and year", "year_quarter"),
    (r"month name", "month_name"),
    (r"day of (?:the )?week|weekday|week day", "weekday"),
    (r"hour(?: of (?:the )?day)?", "hour"),
    (r"day(?: of (?:the )?month)?", "day"),
    (r"quarter", "quarter"),
    (r"month", "month"),
    (r"year", "year"),
    (r"date", "date"),
]


def _rules(columns: list[str]):
    c = lambda t: _column(t, columns)
    cs = lambda t: _columns(t, columns)
    return [
        (r"rename (.+?) (?:to|as) (.+)", lambda m: {"kind": "rename", "column": c(m[1]), "to": m[2].strip().strip("“”\"'")}),
        (r"(?:remove|drop|delete) (?:the )?(?:blank|empty) rows", lambda m: {"kind": "blanks", "columns": []}),
        (
            r"(?:remove|drop|delete) rows? (?:where|with|whose|if) (?:the )?(.+?) (?:is|are) (?:blank|empty|missing)",
            lambda m: {"kind": "blanks", "columns": cs(m[1])},
        ),
        (
            r"(?:remove|drop|delete|de-?dupe|dedup(?:licate)?) (?:the )?duplicate(?:s|d rows| rows)?(?: (?:by|on|using|based on) (.+))?",
            lambda m: {"kind": "dedupe", "columns": cs(m[1]) if m[1] else []},
        ),
        (
            r"(?:keep|only keep|filter(?: to)?|show only|only) (?:only )?(?:the )?rows? (?:where|with|whose|if|when) (.+)",
            lambda m: {"kind": "filter", "condition": condition_formula(m[1], columns)},
        ),
        (
            r"(?:remove|drop|delete|exclude) (?:the )?rows? (?:where|with|whose|if|when) (.+)",
            lambda m: {"kind": "filter", "condition": condition_formula(m[1], columns), "exclude": True},
        ),
        (
            r"(?:keep|take|show) (?:only )?(?:the )?(?:top|highest|largest) (\d+)(?: rows?)?(?: (?:by|on) (.+))?",
            lambda m: {"kind": "limit", "count": int(m[1]), "by": c(m[2]) if m[2] else "", "descending": True},
        ),
        (r"(?:keep|take|show) (?:only )?(?:the )?(?:first) (\d+)(?: rows?)?", lambda m: {"kind": "limit", "count": int(m[1])}),
        (
            r"(?:keep|take|show) (?:only )?(?:the )?(?:bottom|lowest|smallest) (\d+)(?: rows?)? (?:by|on) (.+)",
            lambda m: {"kind": "limit", "count": int(m[1]), "by": c(m[2]), "descending": False},
        ),
        (r"(?:sort|order)(?: (?:the )?rows)? by (.+)", lambda m: {"kind": "sort", "by": _sort_keys(m[1], columns)}),
        (
            r"(?:group|summari[sz]e|aggregate|roll up)(?: (?:the )?rows)? by (.+?)\s*(?::|,| with| showing| to get)\s*(.+)",
            lambda m: {"kind": "group", "by": cs(m[1]), "aggregates": _aggregates(m[2], columns)},
        ),
        (
            r"(?:group|summari[sz]e)(?: (?:the )?rows)? by (.+)",
            lambda m: {"kind": "group", "by": cs(m[1]), "aggregates": [{"fn": "count"}]},
        ),
        (
            r"split (.+?) (?:on|by|at) (.+?) into (.+)",
            lambda m: {"kind": "split", "column": c(m[1]), "separator": _separator(m[2]), "into": _names(m[3])},
        ),
        (r"split (.+?) (?:on|by|at) (.+)", lambda m: _split_default(c(m[1]), _separator(m[2]))),
        (
            r"(?:join|merge|combine|concatenate) (.+?) (?:into|as) (.+?)(?: (?:with|using|separated by) (.+))?",
            lambda m: {
                "kind": "merge",
                "columns": cs(m[1]),
                "name": m[2].strip().strip("“”\"'"),
                "separator": _separator(m[3]) if m[3] else " ",
            },
        ),
        (
            r"(?:fill|replace) (?:the )?(?:blanks?|empty cells|missing values?|gaps)(?: in (.+?))? with (?:the )?(.+?)(?: in (.+))?",
            lambda m: _fill(c(m[1] or m[3] or ""), m[2]),
        ),
        (
            r"replace (.+?) with (.+?) in (.+)",
            lambda m: {
                "kind": "replace",
                "column": c(m[3]),
                "find": m[1].strip().strip("“”\"'"),
                "replacement": m[2].strip().strip("“”\"'"),
            },
        ),
        (r"(?:trim|strip)(?: spaces)?(?: (?:in|from))? (.+)", lambda m: {"kind": "text", "column": c(m[1]), "op": "trim"}),
        (
            r"(?:upper ?case|capitali[sz]e all|make (.+?) upper ?case)(?: (.+))?",
            lambda m: {"kind": "text", "column": c(m[1] or m[2]), "op": "upper"},
        ),
        (r"(?:lower ?case)(?: (.+))?", lambda m: {"kind": "text", "column": c(m[1]), "op": "lower"}),
        (r"(?:proper ?case|title ?case|capitali[sz]e) (.+)", lambda m: {"kind": "text", "column": c(m[1]), "op": "proper"}),
        (
            r"(?:change|convert|read|treat|make) (.+?) (?:to|as|into) (?:a |an )?(numbers?|whole numbers?|integers?|text|dates?)",
            lambda m: {"kind": "retype", "column": c(m[1]), "to": _type_word(m[2])},
        ),
        (
            r"(?:band|bucket|bin|group) (.+?) (?:at|into|by|on|with edges?) ([\d.,\s]+(?:and [\d.]+)?)(?: (?:as|into|called) (.+))?",
            lambda m: _bucket(c(m[1]), m[2], m[3]),
        ),
        (
            r"(?:add|insert|create) (?:a |an )?(?:row )?(?:number|index|numbering)(?: column)?(?: (?:called|named|as) (.+))?",
            lambda m: {"kind": "index", "name": (m[1] or "Row number").strip()},
        ),
        (r"(?:turn|unpivot|melt) (.+?) into rows(?: (?:as|called) (.+?) and (.+))?", lambda m: _unpivot(m[1], m[2], m[3], columns)),
        (
            r"(?:pivot|spread) (.+?) (?:into|as) columns(?: of| showing| with)? (?:(total|sum of|average|count of|max|min) )?(.+)",
            lambda m: {"kind": "pivot", "column": c(m[1]), "measure": c(m[3]), "fn": _pivot_fn(m[2])},
        ),
        (r"(?:remove|drop|delete|hide)(?: the)? columns? (.+)", lambda m: {"kind": "drop", "columns": cs(m[1])}),
        (r"keep(?: only)?(?: the)? columns? (.+)", lambda m: {"kind": "keep", "columns": cs(m[1])}),
        (
            r"(?:add|create|make|new)(?: a| an)?(?: new)?(?: column)?(?: called| named)? (.+?)\s*[:,=]?\s*"
            r"if (.+?)(?:,? (?:else|otherwise) (.+))?",
            lambda m: _conditional(m[1], m[2], m[3], columns),
        ),
        (
            r"(?:add|create|make|new)(?: a| an)?(?: new)?(?: column)?(?: called| named)? (.+?)\s*(?:=|as|:) (.+)",
            lambda m: {"kind": "derive", "name": m[1].strip().strip("“”\"'"), "formula": _derive_formula(m[2], columns)},
        ),
        (r"(.+?) = (.+)", lambda m: {"kind": "derive", "name": m[1].strip().strip("[]“”\"'"), "formula": _derive_formula(m[2], columns)}),
        (
            r"(?:add |extract |take |get )?(?:the )?("
            + "|".join(p for p, _ in DATE_WORDS)
            + r") (?:of|from) (.+?)(?: (?:as|called) (.+))?",
            lambda m: _datepart(m[1], c(m[2]), m[3]),
        ),
        (r"(?:remove|drop|delete) (.+)", lambda m: {"kind": "drop", "columns": cs(m[1])}),
    ]


def parse_phrase(phrase: str, columns: list[str]) -> Step:
    text = re.sub(r"\s+", " ", phrase.strip().rstrip(".")).strip()
    if not text:
        raise PhraseError("Say what you want to change.")
    last_error: PhraseError | None = None
    for pattern, build in _rules(columns):
        m = re.fullmatch(pattern, text, re.I)
        if not m:
            continue
        try:
            return parse_step(build(m))
        except PhraseError as e:
            last_error = e
        except (ValueError, KeyError) as e:  # a rule that half-matched; another may read it
            last_error = PhraseError(str(e).split("\n")[0])
    if last_error:
        raise last_error
    raise PhraseError("I couldn't read that as a step. Try one of the examples, or use a form below.")


def _names(text: str) -> list[str]:
    return [n.strip().strip("“”\"'") for n in re.split(r"\s*,\s*|\s+and\s+", text.strip()) if n.strip()]


def _split_default(column: str, sep: str) -> dict:
    base = label_of(column)
    return {"kind": "split", "column": column, "separator": sep, "into": [f"{base} 1", f"{base} 2"]}


def _sort_keys(text: str, columns: list[str]) -> list[dict]:
    keys = []
    for part in re.split(r"\s*,\s*|\s+then\s+(?:by\s+)?", text.strip()):
        desc = bool(
            re.search(r"\b(desc(?:ending)?|high(?:est)? (?:to|first)|largest first|newest first|z to a|biggest first)\b", part, re.I)
        )
        name = re.sub(
            r"\b(desc(?:ending)?|asc(?:ending)?|high(?:est)? (?:to|first).*|low(?:est)? (?:to|first).*"
            r"|largest first|smallest first|newest first|oldest first|a to z|z to a|biggest first)\b.*",
            "",
            part,
            flags=re.I,
        )
        keys.append({"column": _column(name, columns), "descending": desc})
    return keys


def _fill(column: str, value: str) -> dict:
    v = value.strip().strip("“”\"'")
    methods = {
        **dict.fromkeys(("previous", "previous value", "value above", "last value"), "previous"),
        **dict.fromkeys(("average", "mean"), "average"),
        "median": "median",
        **dict.fromkeys(("zero", "0"), "zero"),
    }
    method = methods.get(v.lower(), "value")
    return {"kind": "fill", "column": column, "method": method, "value": v if method == "value" else ""}


def _type_word(word: str) -> str:
    w = word.lower()
    return (
        "integer"
        if w.startswith(("whole", "integer"))
        else "number"
        if w.startswith("number")
        else "date"
        if w.startswith("date")
        else "text"
    )


def _bucket(column: str, edges_text: str, name: str | None) -> dict:
    edges = [float(e) for e in re.findall(r"-?\d+(?:\.\d+)?", edges_text.replace(",", " "))]
    return {"kind": "bucket", "column": column, "edges": edges, "name": (name or f"{label_of(column)} band").strip()}


def _unpivot(cols_text: str, name: str | None, value: str | None, columns: list[str]) -> dict:
    if m := re.fullmatch(r"(?:columns? )?(.+?)\s*(?:\.\.|…|to|through|-)\s*(.+)", cols_text.strip(), re.I):
        a, b = match_column(m[1], columns), match_column(m[2], columns)
        if a and b and columns.index(a) <= columns.index(b):
            chosen = columns[columns.index(a) : columns.index(b) + 1]
            return {
                "kind": "unpivot",
                "columns": chosen,
                "name_column": (name or "Attribute").strip(),
                "value_column": (value or "Value").strip(),
            }
    return {
        "kind": "unpivot",
        "columns": _columns(cols_text, columns),
        "name_column": (name or "Attribute").strip(),
        "value_column": (value or "Value").strip(),
    }


def _pivot_fn(word: str | None) -> str:
    w = (word or "").lower()
    return (
        "avg" if w.startswith("average") else "count" if w.startswith("count") else "max" if w == "max" else "min" if w == "min" else "sum"
    )


def _derive_formula(text: str, columns: list[str]) -> str:
    t = text.strip()
    words = {"plus": "+", "minus": "-", "times": "*", "multiplied by": "*", "divided by": "/", "over": "/"}
    for word, op in words.items():
        t = re.sub(rf"\s+{word}\s+", f" {op} ", t, flags=re.I)
    # Column names written loosely ("revenue - cost of goods") become [bracketed] references.
    for c in sorted(columns, key=len, reverse=True):
        for spelling in {c, label_of(c), lower_label(label_of(c))}:
            if " " in spelling and spelling.lower() in t.lower() and f"[{c}]" not in t:
                t = re.sub(re.escape(spelling), f"[{c}]", t, flags=re.I)
    try:
        Formula(t, columns)
    except FormulaError as e:
        raise PhraseError(str(e)) from e
    return t


def _conditional(name: str, body: str, otherwise: str | None, columns: list[str]) -> dict:
    rules = []
    for chunk in re.split(r",?\s*\bif\b\s+|;\s*", "if " + body.strip()):
        if not chunk.strip():
            continue
        m = re.fullmatch(r"(.+?)\s*(?:,\s*)?then\s+(.+)", chunk.strip().rstrip(","), re.I)
        if not m:
            raise PhraseError("Write each rule as “if … then …”, for example “if revenue is over 1000 then High”.")
        rules.append({"when": condition_formula(m[1], columns), "then": m[2].strip().rstrip(",")})
    return {"kind": "conditional", "name": name.strip().strip("“”\"'"), "rules": rules, "otherwise": (otherwise or "").strip()}


def _datepart(word: str, column: str, name: str | None) -> dict:
    part = next(p for pattern, p in DATE_WORDS if re.fullmatch(pattern, word, re.I))
    return {
        "kind": "datepart",
        "column": column,
        "part": part,
        "name": (name or f"{label_of(column)} {DATE_PARTS[part].split(' (')[0].lower()}").strip(),
    }


def example_phrases(reading: Reading) -> list[str]:
    """Sentences written against this table, for the box's placeholder and hints."""
    f = reading.fields
    nums = [x for x in f if x.role == "measure"]
    totals = sorted((x for x in nums if x.agg == "sum"), key=lambda x: x.format != "currency") or nums
    dims = [x for x in f if x.role == "dimension" and x.top]
    dates = [x for x in f if x.kind == "date" and not x.origin]
    out: list[str] = []
    if dims:
        out.append(f"keep rows where {lower_label(dims[0].label)} is {dims[0].top[0][0]}")
    if dims and totals:
        out.append(f"group by {lower_label(dims[0].label)}: total {lower_label(totals[0].label)}, count")
    qty = next((x for x in nums if x.agg == "sum" and QUANTITY.search(name_words(x.name))), None)
    money = next((x for x in totals if x is not qty and x.format == "currency"), None) or next((x for x in totals if x is not qty), None)
    if qty and money:
        out.append(f"add {money.label} per unit = [{money.name}] / [{qty.name}]")
    if dates:
        out.append(f"month of {lower_label(dates[0].label)}")
    banded = next((x for x in nums if isinstance(x.min, int | float) and isinstance(x.max, int | float) and x.max > x.min), None)
    if banded:
        edges = _round_edges(float(banded.min), float(banded.max))
        if edges:
            out.append(f"band {lower_label(banded.label)} at {', '.join(edges)}")
    out.append("remove duplicates")
    return out


def _round_edges(lo: float, hi: float) -> list[str]:
    """Three round break points inside lo..hi, for an example that makes sense on this column."""
    step = (hi - lo) / 4
    if step <= 0:
        return []
    unit = 10 ** math.floor(math.log10(step))
    step = max(unit, round(step / unit) * unit)
    start = math.ceil(lo / step) * step
    edges = [start + i * step for i in range(4) if lo < start + i * step < hi][:3]
    return [f"{e:g}" for e in edges] if len(edges) >= 2 else []
