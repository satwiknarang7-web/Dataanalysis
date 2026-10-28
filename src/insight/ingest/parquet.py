"""Parquet in and out, through DuckDB.

DuckDB is already here for the SQL console, so it also does Parquet: pyarrow
would add ~170 MB to a serverless bundle for the same two functions. DuckDB
reads and writes files, not buffers, so each call goes through a private
temporary directory (/tmp is writable on every platform we deploy to).
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import duckdb
import pandas as pd

from insight.engine.cleaning import as_number_dtype


def to_parquet(df: pd.DataFrame) -> bytes:
    """A table as zstd-compressed Parquet bytes."""
    with tempfile.TemporaryDirectory(prefix="insight-") as tmp:
        path = Path(tmp) / "table.parquet"
        con = duckdb.connect(":memory:")
        try:
            con.register("t", df)
            con.execute("COPY t TO ? (FORMAT parquet, COMPRESSION zstd)", [str(path)])
        finally:
            con.close()
        return path.read_bytes()


def read_parquet(content: bytes) -> pd.DataFrame:
    """Parquet bytes as a table, with whole-number columns restored as nullable integers."""
    with tempfile.TemporaryDirectory(prefix="insight-") as tmp:
        path = Path(tmp) / "table.parquet"
        path.write_bytes(content)
        con = duckdb.connect(":memory:")
        try:
            df = con.execute("SELECT * FROM read_parquet(?)", [str(path)]).df()
        finally:
            con.close()
    for name in df.columns:
        s = df[name]
        if pd.api.types.is_float_dtype(s) or pd.api.types.is_integer_dtype(s):
            df[name] = as_number_dtype(s.astype("float64"))
        elif pd.api.types.is_datetime64_any_dtype(s):
            if getattr(s.dt, "tz", None) is not None:
                s = s.dt.tz_convert("UTC").dt.tz_localize(None)
            df[name] = s.astype("datetime64[ns]")
        elif pd.api.types.is_object_dtype(s) or pd.api.types.is_string_dtype(s):
            df[name] = s.astype("object").where(s.notna(), None)
    return df
