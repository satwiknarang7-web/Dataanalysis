"""The analysis engine: read a table, plan a dashboard, compute and caption it.

Pure Python over pandas, with no I/O, so it is easy to test and to cache.
Bump ENGINE_VERSION whenever a change would alter a computed result: every
cache key includes it, so a deploy never serves a figure from older rules.
"""

ENGINE_VERSION = "1"
