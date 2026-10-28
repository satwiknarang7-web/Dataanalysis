"""The tests behind every word like "rose", "highest" or "goes with".

A caption only claims a pattern when one of these says it is unlikely to be
noise. Each test returns a p-value; the planner reads it, never the wording.

Student's t distribution is computed here (through the regularised incomplete
beta function) rather than taken from SciPy, which would add ~140 MB to a
serverless bundle for four functions. tests/test_stats.py holds every value
to SciPy's where SciPy is installed.
"""

from __future__ import annotations

import math
from functools import lru_cache

import numpy as np

SIGNIFICANT = 0.05


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the incomplete beta function (modified Lentz)."""
    tiny, eps = 1e-300, 3e-16
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 300):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return h


def incomplete_beta(x: float, a: float, b: float) -> float:
    """The regularised incomplete beta function I_x(a, b)."""
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    front = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x))
    if x < (a + 1) / (a + b + 2):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def t_two_sided_p(t: float, df: float) -> float:
    """P(|T| >= |t|) for Student's t with df degrees of freedom."""
    if df <= 0 or math.isnan(t):
        return 1.0
    if math.isinf(t):
        return 0.0
    return incomplete_beta(df / (df + t * t), df / 2.0, 0.5)


@lru_cache(maxsize=512)
def t_critical(df: int, two_sided_alpha: float = 0.05) -> float:
    """The t value with two_sided_alpha in the tails, by bisection on the p-value."""
    lo, hi = 0.0, 1.0
    while t_two_sided_p(hi, df) > two_sided_alpha:
        hi *= 2
    for _ in range(80):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if t_two_sided_p(mid, df) > two_sided_alpha else (lo, mid)
    return (lo + hi) / 2


def welch_p(a: np.ndarray, b: np.ndarray) -> float:
    """Two-sided p-value of Welch's t test (unequal variances)."""
    na, nb = len(a), len(b)
    va, vb = float(np.var(a, ddof=1)) / na, float(np.var(b, ddof=1)) / nb
    se2 = va + vb
    if se2 == 0:
        return 0.0 if np.mean(a) != np.mean(b) else 1.0
    t = (float(np.mean(a)) - float(np.mean(b))) / math.sqrt(se2)
    df = se2 * se2 / ((va * va) / (na - 1) + (vb * vb) / (nb - 1))
    return t_two_sided_p(t, df)


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
    half = t_critical(n - 1) * sd / math.sqrt(n)
    return (m - half, m + half)


def trend_test(ys: list[float]) -> tuple[float, float]:
    """Slope per period and its p-value, by least squares on the period index."""
    y = np.asarray(ys, dtype=float)
    if len(y) < 3 or np.all(y == y[0]):
        return 0.0, 1.0
    x = np.arange(len(y), dtype=float)
    xc, yc = x - x.mean(), y - y.mean()
    slope = float((xc @ yc) / (xc @ xc))
    resid = yc - slope * xc
    df = len(y) - 2
    se = math.sqrt(float(resid @ resid) / df / float(xc @ xc))
    if se == 0:
        return slope, 0.0
    return slope, t_two_sided_p(slope / se, df)


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
        p = welch_p(s, rest)
        if not math.isnan(p) and p < best[0]:
            best = (p, i)
    return min(1.0, best[0] * len(gs)), best[1]


def correlation(x: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    """Pearson's r and its p-value over the rows where both are present."""
    ok = ~(np.isnan(x) | np.isnan(y))
    x, y = x[ok], y[ok]
    if len(x) < 10 or np.std(x) == 0 or np.std(y) == 0:
        return 0.0, 1.0
    r = float(np.clip(np.corrcoef(x, y)[0, 1], -1.0, 1.0))
    df = len(x) - 2
    if abs(r) >= 1:
        return r, 0.0
    return r, t_two_sided_p(r * math.sqrt(df / (1 - r * r)), df)


def concentration(values: list[float]) -> float:
    """How far the largest group's share is above an even split: 0 = even, 1 = all in one."""
    vals = [v for v in values if v is not None and v > 0]
    if len(vals) < 2:
        return 0.0
    k, top = len(vals), max(vals) / sum(vals)
    return (top - 1 / k) / (1 - 1 / k)
