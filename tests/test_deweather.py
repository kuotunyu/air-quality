"""Tests for M4 — meteorological normalisation.

The method's whole claim is that it separates an emissions change from a
weather change. So the tests are built on series where that split is known by
construction: a trend put in by hand, and weather that either does or does not
conspire with it.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import polars as pl
import pytest

from twair.analysis import deweather
from twair.analysis.deweather import (
    TrendEstimate,
    _weather_share,
    _years_since_start,
    block_bootstrap_slope,
    monthly_means,
    theil_sen,
)


def _monthly(values: list[float], start: datetime = datetime(2006, 1, 1)) -> pl.DataFrame:
    months = [
        start.replace(year=start.year + i // 12, month=i % 12 + 1) for i in range(len(values))
    ]
    return pl.DataFrame({"month": months, "normalised": values}).with_columns(
        pl.col("month").cast(pl.Date)
    )


class TestTrendEstimation:
    def test_a_known_slope_is_recovered(self) -> None:
        frame = _monthly([30.0 - 1.2 * (i / 12) for i in range(240)])

        trend = theil_sen(_years_since_start(frame["month"]), frame["normalised"].to_numpy())

        assert trend.slope_per_year == pytest.approx(-1.2, abs=0.01)

    def test_the_slope_is_per_year_not_per_point(self) -> None:
        """240 monthly points spanning 20 years must not report a per-month slope."""
        frame = _monthly([100.0 - 5.0 * (i / 12) for i in range(240)])

        trend = theil_sen(_years_since_start(frame["month"]), frame["normalised"].to_numpy())

        assert trend.slope_per_year == pytest.approx(-5.0, abs=0.05)

    def test_a_flat_series_is_not_significant(self) -> None:
        rng = np.random.default_rng(0)
        frame = _monthly(list(20.0 + rng.normal(0, 2.0, 240)))

        trend = theil_sen(_years_since_start(frame["month"]), frame["normalised"].to_numpy())

        assert not trend.significant, "noise around a constant is not a trend"

    def test_too_few_points_raises_rather_than_returning_a_number(self) -> None:
        with pytest.raises(ValueError, match="at least 3"):
            theil_sen(np.array([0.0, 1.0]), np.array([1.0, 2.0]))


class TestHonestConfidenceInterval:
    def test_the_block_interval_is_wider_than_the_naive_one_on_correlated_data(self) -> None:
        """The reason the naive interval is not reported alone.

        A random walk has no trend, but its neighbouring values are almost
        identical. Theil-Sen's interval assumes independence and therefore
        understates the uncertainty badly.
        """
        rng = np.random.default_rng(1)
        walk = np.cumsum(rng.normal(0, 1.0, 240)) + 30.0
        frame = _monthly(list(walk))

        trend = theil_sen(_years_since_start(frame["month"]), frame["normalised"].to_numpy())

        assert trend.block_width > trend.naive_width

    def test_significance_uses_the_block_interval_not_the_naive_one(self) -> None:
        estimate = TrendEstimate(
            slope_per_year=-1.0,
            intercept=0.0,
            naive_low=-1.2,
            naive_high=-0.8,  # naive interval excludes zero
            block_low=-2.5,
            block_high=0.4,  # honest interval does not
            n=240,
        )

        assert not estimate.significant

    def test_the_bootstrap_keeps_blocks_contiguous(self) -> None:
        """A block of 1 would destroy the autocorrelation it exists to preserve."""
        rng = np.random.default_rng(2)
        x = np.arange(240, dtype=float) / 12
        y = np.cumsum(rng.normal(0, 1.0, 240))

        wide = block_bootstrap_slope(x, y, block=24, n_boot=200)
        narrow = block_bootstrap_slope(x, y, block=1, n_boot=200)

        assert (wide[1] - wide[0]) > (narrow[1] - narrow[0])


class TestWeatherShare:
    def test_weather_flattering_an_improvement_gives_a_positive_share(self) -> None:
        """Observed fell twice as fast as the emissions-only series."""
        assert _weather_share(-2.0, -1.0) == pytest.approx(0.5)

    def test_weather_working_against_an_improvement_gives_a_negative_share(self) -> None:
        """The underlying change was larger than it looked."""
        assert _weather_share(-1.0, -2.0) == pytest.approx(-1.0)

    def test_no_observed_trend_gives_no_share_rather_than_a_division_by_zero(self) -> None:
        assert np.isnan(_weather_share(0.0, -1.0))


class TestMonthlyAggregation:
    def test_hours_collapse_to_one_row_per_month(self) -> None:
        start = datetime(2010, 1, 1)
        series = pl.DataFrame(
            {
                "ts_local": [start + timedelta(hours=h) for h in range(24 * 70)],
                "observed": [20.0] * (24 * 70),
                "normalised": [18.0] * (24 * 70),
            }
        )

        monthly = monthly_means(series)

        assert monthly.height == 3, "70 days spans January, February and March"
        assert monthly["hours"].sum() == 24 * 70

    def test_the_first_month_is_year_zero(self) -> None:
        frame = _monthly([1.0] * 25)

        years = _years_since_start(frame["month"])

        assert years[0] == 0.0
        assert years[-1] == pytest.approx(2.0, abs=0.02)


class TestNormalisationContract:
    def test_holding_every_feature_fixed_is_refused(self) -> None:
        """Nothing would be normalised, so the result would be a plain fit."""
        frame = pl.DataFrame({"trend_days": [1.0, 2.0], "AMB_TEMP": [20.0, 21.0]})

        with pytest.raises(ValueError, match="every feature is held fixed"):
            deweather.normalise(
                model=None,
                frame=frame,
                features=("trend_days", "AMB_TEMP"),
                held_fixed=("trend_days", "AMB_TEMP"),
            )

    def test_the_trend_column_is_the_one_held_fixed_by_default(self) -> None:
        """Resampling the trend too would erase the signal being measured."""
        assert deweather.DEFAULT_HELD_FIXED == ("trend_days",)

    def test_chemistry_is_not_among_the_normalisation_features(self) -> None:
        """NOx and CO share sources with PM2.5.

        Including them would let the model attribute an emissions change to a
        covariate, which is exactly the change this analysis exists to isolate.
        """
        for excluded in ("NOx", "CO", "SO2", "O3", "PM10", "NO2"):
            assert excluded not in deweather.NORMALISE_FEATURES

    def test_resampled_rows_keep_realistic_weather_combinations(self) -> None:
        """One permutation for all resampled columns, not one per column.

        Drawing each column independently would manufacture conditions that
        never occur — 35 °C with a January morning's humidity.
        """
        rng = np.random.default_rng(0)
        n = 500
        # Temperature and humidity perfectly anti-correlated by construction.
        temp = rng.uniform(10, 35, n)
        frame = pl.DataFrame(
            {"AMB_TEMP": temp, "RH": 100.0 - 2 * temp, "trend_days": np.arange(n, dtype=float)}
        )

        captured: list[np.ndarray] = []

        class _Recorder:
            def predict(self, matrix: np.ndarray) -> np.ndarray:
                captured.append(matrix.copy())
                return np.zeros(matrix.shape[0])

        deweather.normalise(
            _Recorder(),
            frame,
            features=("AMB_TEMP", "RH", "trend_days"),
            held_fixed=("trend_days",),
            n_samples=1,
        )

        resampled = captured[0]
        assert np.allclose(resampled[:, 1], 100.0 - 2 * resampled[:, 0]), (
            "the temperature/humidity pairing must survive resampling"
        )
