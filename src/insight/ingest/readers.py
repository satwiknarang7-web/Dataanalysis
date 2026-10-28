"""Turn the bytes of a file into a table.

Text formats are read as strings, so cleaning (not the parser) decides what
each value is. Binary formats (Excel, Parquet) keep their types.
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import PurePath

import pandas as pd

from insight.ingest.parquet import read_parquet

SUPPORTED = {".csv", ".tsv", ".txt", ".xlsx", ".xlsm", ".xls", ".json", ".ndjson", ".jsonl", ".parquet"}
MAX_ROWS = 2_000_000


class UnreadableFile(ValueError):
    """The file could not be read as a table; the message says why, in words."""


def read_table(content: bytes, filename: str) -> pd.DataFrame:
    ext = PurePath(filename.lower()).suffix
    if ext in {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".docx", ".pptx"}:
        raise UnreadableFile("Photos, PDFs and documents aren't read. Export the table as CSV or Excel first.")
    if ext not in SUPPORTED:
        raise UnreadableFile(f"{ext or 'This kind of'} file isn't supported. Use CSV, Excel, JSON or Parquet.")
    if not content:
        raise UnreadableFile("The file is empty.")
    try:
        if ext in {".xlsx", ".xlsm", ".xls"}:
            df = _excel(content)
        elif ext == ".parquet":
            df = read_parquet(content)
        elif ext in {".json", ".ndjson", ".jsonl"}:
            df = _json(content)
        else:
            df = _delimited(content)
    except UnreadableFile:
        raise
    except Exception as e:  # parsers raise many types; the reader needs one message
        raise UnreadableFile(f"The file couldn't be read as a table ({type(e).__name__}).") from e
    if df.empty or df.shape[1] == 0:
        raise UnreadableFile("No rows were found in the file.")
    if len(df) > MAX_ROWS:
        raise UnreadableFile(f"The file has {len(df):,} rows; the limit is {MAX_ROWS:,}.")
    return df


def _decode(content: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-16") if content[:2] in (b"\xff\xfe", b"\xfe\xff") else ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise UnreadableFile("The file's text encoding couldn't be read.")


def _delimited(content: bytes) -> pd.DataFrame:
    text = _decode(content)
    lines = text.splitlines()
    sample = "\n".join(lines[:50])
    try:
        delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        delimiter = "\t" if sample.count("\t") > sample.count(",") else ","
    skip = _title_rows(lines[:30], delimiter)
    return pd.read_csv(
        io.StringIO(text),
        sep=delimiter,
        dtype=str,
        keep_default_na=False,
        skiprows=skip,
        on_bad_lines="skip",
        engine="python",
        skipinitialspace=True,
    )


def _title_rows(lines: list[str], delimiter: str) -> int:
    """A title and a stamp above the header ("Sales report", "Exported 2024-01-05") are cut."""
    widths = [len(next(csv.reader([ln], delimiter=delimiter), [])) for ln in lines if ln.strip()]
    if not widths:
        return 0
    full = max(set(widths), key=widths.count)
    for i, ln in enumerate(lines):
        cells = next(csv.reader([ln], delimiter=delimiter), [])
        if len(cells) >= full and sum(bool(c.strip()) for c in cells) >= max(1, full // 2):
            return i
    return 0


def _excel(content: bytes) -> pd.DataFrame:
    sheets = pd.read_excel(io.BytesIO(content), sheet_name=None)
    filled = {name: df.dropna(how="all").dropna(axis=1, how="all") for name, df in sheets.items()}
    filled = {k: v for k, v in filled.items() if not v.empty}
    if not filled:
        raise UnreadableFile("Every sheet in the workbook is empty.")
    # The sheet with the most cells is the data; the rest are usually notes or pivots.
    return max(filled.values(), key=lambda d: d.size)


def _json(content: bytes) -> pd.DataFrame:
    text = _decode(content).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = [json.loads(line) for line in text.splitlines() if line.strip()]
    if isinstance(data, dict):
        # {"data": [...]} or {"results": [...]}: the first list of records inside.
        data = next((v for v in data.values() if isinstance(v, list) and v and isinstance(v[0], dict)), [data])
    if not isinstance(data, list):
        raise UnreadableFile("The JSON doesn't hold a list of records.")
    return pd.json_normalize(data).map(_json_cell)


def _json_cell(v: object) -> str | None:
    if v is None or (isinstance(v, float) and v != v):
        return None
    if isinstance(v, (list, dict)):
        return json.dumps(v)
    return str(v)
