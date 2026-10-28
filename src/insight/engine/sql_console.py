"""A read-only SQL console over one table, in an isolated DuckDB.

Each query gets a fresh in-memory database that can see exactly one table,
`data`, and nothing else: external access (files, URLs, extensions) is turned
off and the configuration locked before the user's SQL runs. Only a single
SELECT is accepted, results are capped, and a query that runs too long is
interrupted.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

import duckdb
import pandas as pd

MAX_ROWS = 500
TIMEOUT_SECONDS = 10


class SqlError(ValueError):
    pass


@dataclass
class SqlResult:
    columns: list[str]
    rows: list[list]
    truncated: bool


def run_sql(df: pd.DataFrame, sql: str, max_rows: int = MAX_ROWS, timeout: float = TIMEOUT_SECONDS) -> SqlResult:
    sql = sql.strip().rstrip(";").strip()
    if not sql:
        raise SqlError("Write a query, for example: SELECT * FROM data LIMIT 10")
    con = duckdb.connect(":memory:")
    try:
        con.register("data", df)
        con.execute("SET enable_external_access = false")
        con.execute("SET autoinstall_known_extensions = false")
        con.execute("SET autoload_known_extensions = false")
        con.execute("SET lock_configuration = true")
        try:
            statements = con.extract_statements(sql)
        except duckdb.Error as e:
            raise SqlError(_message(e)) from e
        if len(statements) != 1 or statements[0].type != duckdb.StatementType.SELECT:
            raise SqlError("Only a single SELECT query can be run here.")

        timer = threading.Timer(timeout, con.interrupt)
        timer.start()
        try:
            cursor = con.execute(sql)
            columns = [d[0] for d in cursor.description or []]
            rows = cursor.fetchmany(max_rows + 1)
        except duckdb.InterruptException as e:
            raise SqlError(f"The query took longer than {timeout:g} seconds and was stopped.") from e
        except duckdb.Error as e:
            raise SqlError(_message(e)) from e
        finally:
            timer.cancel()
        return SqlResult(columns=columns, rows=[list(map(_cell, r)) for r in rows[:max_rows]], truncated=len(rows) > max_rows)
    finally:
        con.close()


def _cell(v):
    if v is None or isinstance(v, (int, float, str, bool)):
        return v
    return str(v)


def _message(e: Exception) -> str:
    return str(e).split("\n")[0][:300]
