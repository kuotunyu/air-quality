"""Tests for M12 — SARIMA, the model the 2018 project set aside.

The thing worth testing here is not whether SARIMA forecasts well. It is
whether the *evaluation* is honest: that a split tests on its own span and not
on data a later split trains on, that the baseline it is compared against never
sees the test period, and that a forecast is only scored where a truth exists.
Each of those, done wrong, produces a number that looks fine and means nothing.
"""

from __future__ import annotations

import numpy as np
import polars as pl

from twair.models.sarima import (
    TRAIN_WINDOW_HOURS,
    _climatology,
    _scores,
    rolling_sarima,
)


def _hourly(n: int, start: str = "2020-01-01") -> pl.Series:
    return pl.datetime_range(
        pl.lit(start).str.to_datetime(),
        pl.lit(start).str.to_datetime() + pl.duration(hours=n - 1),
        interval="1h",
        eager=True,
    )


def _seasonal(n: int, *, seed: int = 0) -> np.ndarray:
    """A daily cycle plus noise — enough structure for SARIMAX to fit quickly."""
    rng = np.random.default_rng(seed)
    hours = np.arange(n)
    return 20.0 + 6.0 * np.sin(2 * np.pi * hours / 24) + rng.normal(0, 1.5, n)


class TestSplitBoundary:
    """The defect that made the first run cost twice what it should.

    `rolling_sarima` used to loop to the end of the *series* rather than to the
    end of the split, so the first of three splits forecast straight across the
    hours the second and third splits use as training data. Every method was
    still scored on identical rows, so the head-to-head stayed fair — which is
    exactly why it survived a reading and had to be caught by measurement.
    """

    def test_no_origin_falls_outside_the_split(self) -> None:
        values = _seasonal(1600)
        train_end, test_end = 900, 1100

        forecasts, fit = rolling_sarima(
            values, train_end=train_end, test_end=test_end, horizons=(1, 6), stride=40
        )

        assert fit is not None
        origins = [origin for origin, _ in forecasts[1]]
        assert origins, "expected at least one origin inside the split"
        assert min(origins) >= train_end
        assert max(origins) < test_end

    def test_a_later_split_does_not_reuse_an_earlier_ones_origins(self) -> None:
        values = _seasonal(2000)

        first, _ = rolling_sarima(
            values, train_end=800, test_end=1200, horizons=(1,), stride=50
        )
        second, _ = rolling_sarima(
            values, train_end=1200, test_end=1600, horizons=(1,), stride=50
        )

        assert not set(o for o, _ in first[1]) & set(o for o, _ in second[1])

    def test_the_test_span_never_runs_past_the_series(self) -> None:
        values = _seasonal(1200)

        forecasts, _ = rolling_sarima(
            # A caller asking for more span than exists must not read past the end.
            values,
            train_end=800,
            test_end=10_000,
            horizons=(1, 48),
            stride=60,
        )

        assert all(origin + 48 < values.size for origin, _ in forecasts[48])


class TestTrainingWindow:
    def test_training_uses_only_the_trailing_window(self) -> None:
        # Everything before the window is NaN, so a fit that reached back
        # further would have almost nothing observed and bail out.
        values = _seasonal(TRAIN_WINDOW_HOURS + 1500)
        values[: TRAIN_WINDOW_HOURS // 2] = np.nan
        train_end = TRAIN_WINDOW_HOURS + 800

        _, fit = rolling_sarima(
            values, train_end=train_end, test_end=train_end + 300, horizons=(1,), stride=150
        )

        assert fit is not None
        assert fit.train_points == TRAIN_WINDOW_HOURS

    def test_too_little_observed_history_returns_nothing_rather_than_a_bad_fit(self) -> None:
        values = np.full(2000, np.nan)
        values[:100] = _seasonal(100)

        forecasts, fit = rolling_sarima(
            values, train_end=1000, test_end=1500, horizons=(1,), stride=100
        )

        assert fit is None
        assert forecasts == {}


class TestClimatologyBaseline:
    """The bar SARIMA has to clear. If it can see the test period, the bar is
    fake and every skill number computed against it is meaningless."""

    def test_the_baseline_cannot_see_the_test_period(self) -> None:
        n = 24 * 40
        frame = pl.DataFrame(
            {
                "ts_local": _hourly(n),
                # The training half sits at 10, the test half at 1000. A
                # baseline fitted on everything would be dragged upward.
                "PM2.5": np.concatenate([np.full(n // 2, 10.0), np.full(n // 2, 1000.0)]),
            }
        )

        climatology = _climatology(frame, train_end=n // 2)

        assert np.allclose(climatology, 10.0)

    def test_hours_absent_from_training_fall_back_to_the_overall_mean(self) -> None:
        n = 48
        frame = pl.DataFrame(
            {"ts_local": _hourly(n), "PM2.5": np.concatenate([np.full(24, 8.0), np.full(24, 99.0)])}
        )

        # Only the first day trains, so every month-hour pair is present; make
        # one unlearnable by nulling it and check the fallback is the training
        # mean rather than a null propagated into the scores.
        frame = frame.with_columns(
            pl.when(pl.arange(0, n) == 5).then(None).otherwise(pl.col("PM2.5")).alias("PM2.5")
        )
        climatology = _climatology(frame, train_end=24)

        assert np.isfinite(climatology).all()
        assert climatology[5] == 8.0


class TestScores:
    def test_a_perfect_forecast_scores_zero_error_and_unit_r2(self) -> None:
        truth = np.array([1.0, 5.0, 3.0, 9.0])

        assert _scores(truth, truth) == {"rmse": 0.0, "mae": 0.0, "r2": 1.0}

    def test_predicting_the_mean_scores_zero_r2(self) -> None:
        truth = np.array([1.0, 2.0, 3.0, 4.0])

        scored = _scores(truth, np.full(4, truth.mean()))

        assert abs(scored["r2"]) < 1e-12

    def test_a_constant_truth_gives_no_r2_rather_than_dividing_by_zero(self) -> None:
        truth = np.full(5, 7.0)

        assert np.isnan(_scores(truth, np.full(5, 6.0))["r2"])
