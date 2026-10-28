"""The hand-written t distribution against SciPy's (SciPy is a dev dependency only)."""

import numpy as np
import pytest

from insight.engine import stats

sps = pytest.importorskip("scipy.stats")


@pytest.mark.parametrize("df", [1, 2, 3, 5, 10, 29, 100, 1000])
@pytest.mark.parametrize("t", [0.0, 0.5, 1.96, 2.5, 4.0, 10.0])
def test_t_p_values(t, df):
    assert stats.t_two_sided_p(t, df) == pytest.approx(2 * sps.t.sf(t, df), rel=1e-7, abs=1e-12)


@pytest.mark.parametrize("df", [1, 2, 5, 10, 30, 200])
def test_t_critical(df):
    assert stats.t_critical(df) == pytest.approx(sps.t.ppf(0.975, df), rel=1e-7)


def test_tests_match_scipy():
    rng = np.random.default_rng(3)
    for _ in range(20):
        a, b = rng.normal(10, 2, rng.integers(5, 80)), rng.normal(10.6, 3, rng.integers(5, 80))
        assert stats.welch_p(a, b) == pytest.approx(sps.ttest_ind(a, b, equal_var=False).pvalue, rel=1e-6)
        x = rng.normal(size=60)
        y = 0.3 * x + rng.normal(size=60)
        r, p = stats.correlation(x, y)
        expected = sps.pearsonr(x, y)
        assert r == pytest.approx(expected[0], rel=1e-9) and p == pytest.approx(expected[1], rel=1e-6)
        ys = list(np.arange(12) * 0.2 + rng.normal(size=12))
        slope, p = stats.trend_test(ys)
        lr = sps.linregress(np.arange(12), ys)
        assert slope == pytest.approx(lr.slope) and p == pytest.approx(lr.pvalue, rel=1e-6)
