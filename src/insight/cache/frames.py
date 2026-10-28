"""Parsed tables kept in memory, so a dataset is read from the database once.

A DataFrame cannot go through Redis cheaply, so this is an in-process LRU,
bounded by item count. It sits in front of Postgres: a miss reads the Parquet
blob and parses it, a hit costs nothing. Entries are keyed by content hash,
so they never need invalidating.
"""

from __future__ import annotations

import threading
from collections import OrderedDict

import pandas as pd


class FrameCache:
    def __init__(self, max_items: int) -> None:
        self._items: OrderedDict[str, pd.DataFrame] = OrderedDict()
        self._max = max_items
        self._lock = threading.Lock()

    def get(self, key: str) -> pd.DataFrame | None:
        with self._lock:
            df = self._items.get(key)
            if df is not None:
                self._items.move_to_end(key)
            return df

    def put(self, key: str, df: pd.DataFrame) -> None:
        with self._lock:
            self._items[key] = df
            self._items.move_to_end(key)
            while len(self._items) > self._max:
                self._items.popitem(last=False)

    def __len__(self) -> int:
        return len(self._items)
