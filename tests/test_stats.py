import math

from orchestrator.stats import median_mad, mean_ci95


def test_median_mad_basic():
    med, mad = median_mad([1.0, 2.0, 3.0, 4.0, 5.0])
    assert med == 3.0
    assert mad == 1.0


def test_median_mad_empty():
    med, mad = median_mad([])
    assert math.isnan(med) and math.isnan(mad)


def test_mean_ci95_known():
    mean, lo, hi = mean_ci95([10.0, 12.0, 11.0, 13.0, 9.0])
    assert abs(mean - 11.0) < 1e-9
    # 95% CI for n=5, sd ≈ 1.58, se ≈ 0.707, t(4) ≈ 2.776 → half-width ≈ 1.96
    assert lo < mean < hi
    assert abs((hi - lo) - 2 * 1.964) < 0.5


def test_mean_ci95_singleton():
    mean, lo, hi = mean_ci95([7.5])
    assert mean == lo == hi == 7.5


def test_mean_ci95_empty():
    mean, lo, hi = mean_ci95([])
    assert math.isnan(mean) and math.isnan(lo) and math.isnan(hi)
