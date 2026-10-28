"""The tests behind every word like "rose", "highest" or "goes with".

A caption only claims a pattern when one of these says it is unlikely to be
noise. Each test returns a p-value; the planner reads it, never the wording.
"""

from __future__ import annotations

import math

import numpy as np
from scipy import stats as sps

SIGNIFICANT = 0.05


def wilson_range(hits: float, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """The 95% range of a rate, honest near 0% and 100%."""
    if n <= 0:
        return None
    p = hits / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def mean_range(values: np.ndarray) -> tuple[float, float] | None:
    """The 95% range of an average (t interval)."""
    n = len(values)
    if n < 2:
        return None
    m, sd = float(np.mean(values)), float(np.std(values, ddof=1))
    half = float(sps.t.ppf(0.975, n - 1)) * sd / math.sqrt(n)
    return (m - half, m + half)


def trend_test(ys: list[float]) -> tuple[float, float]:
    """Slope per period and its p-value, by least squares on the period index."""
    y = np.asarray(ys, dtype=float)
    if len(y) < 3 or np.all(y == y[0]):
        return 0.0, 1.0
    res = sps.linregress(np.arange(len(y)), y)
    return float(res.slope), float(res.pvalue)


def standout_rates(groups: list[tuple[float, int]]) -> tuple[float, int]:
    """Does any one group's rate stand out from the rest?

    Each group is tested against all the others together, and the smallest p
    is multiplied by the number of groups, since it was picked for standing
    out (Bonferroni). Returns (p, index of the group).
    """
    gs = [(h, n) for h, n in groups if n > 0]
    if len(gs) < 2:
        return 1.0, -1
    H, N = sum(h for h, _ in gs), sum(n for _, n in gs)
    pool = H / N
    best = (1.0, -1)
    for i, (h, n) in enumerate(gs):
        n2 = N - n
        se = math.sqrt(pool * (1 - pool) * (1 / n + 1 / n2)) if n2 else 0
        if not se:
            continue
        z = abs(h / n - (H - h) / n2) / se
        p = math.erfc(z / math.sqrt(2))
        if p < best[0]:
            best = (p, i)
    return min(1.0, best[0] * len(gs)), best[1]


def standout_means(samples: list[np.ndarray]) -> tuple[float, int]:
    """The same question for averages: each group against the rest, Welch's t."""
    gs = [s for s in samples if len(s) >= 2]
    if len(gs) < 2:
        return 1.0, -1
    best = (1.0, -1)
    for i, s in enumerate(gs):
        rest = np.concatenate([g for j, g in enumerate(gs) if j != i])
        if np.std(s) == 0 and np.std(rest) == 0:
            p = 0.0 if np.mean(s) != np.mean(rest) else 1.0
        else:
            p = float(sps.ttest_ind(s, rest, equal_var=False).pvalue)
        if not math.isnan(p) and p < best[0]:
            best = (p, i)
    return min(1.0, best[0] * len(gs)), best[1]


def correlation(x: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    """Pearson's r and its p-value over the rows where both are present."""
    ok = ~(np.isnan(x) | np.isnan(y))
    x, y = x[ok], y[ok]
    if len(x) < 10 or np.std(x) == 0 or np.std(y) == 0:
        return 0.0, 1.0
    r, p = sps.pearsonr(x, y)
    return float(r), float(p)


def concentration(values: list[float]) -> float:
    """How far the largest group's share is above an even split: 0 = even, 1 = all in one."""
    vals = [v for v in values if v is not None and v > 0]
    if len(vals) < 2:
        return 0.0
    k, top = len(vals), max(vals) / sum(vals)
    return (top - 1 / k) / (1 - 1 / k)
