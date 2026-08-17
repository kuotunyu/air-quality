"""Tests for the out-of-sample evaluation harness.

In-sample-only reporting is the failure mode here, so these tests are as much about
pinning what "out of sample" means as about arithmetic.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import polars as pl
import pytest

from twair.models.evaluate import (
    climatology_baseline,
    evaluate_predictions,
    leave_one_station_out,
    leave_one_year_out,
    persistence_baseline,
    rolling_origin,
)


def _series(stations: list[str], start: datetime, hours: int, value: float = 20.0) -> pl.DataFrame:
    rows = []
    for station in stations:
        for h in range(hours):
            rows.append((station, start + timedelta(hours=h), value + h % 5))
    return pl.DataFrame(
        {
            "station_name": [r[0] for r in rows],
            "ts_local": [r[1] for r in rows],
            "PM2.5": [float(r[2]) for r in rows],
        }
    )


class TestMetrics:
    def test_perfect_prediction(self) -> None:
        m = evaluate_predictions(np.array([1.0, 2.0, 3.0]), np.array([1.0, 2.0, 3.0]))

        assert m.rmse == pytest.approx(0.0)
        assert m.r2 == pytest.approx(1.0)

    def test_rmse_and_mae_differ_under_a_single_large_error(self) -> None:
        m = evaluate_predictions(np.array([0.0, 0.0, 0.0, 0.0]), np.array([0.0, 0.0, 0.0, 4.0]))

        assert m.mae == pytest.approx(1.0)
        assert m.rmse == pytest.approx(2.0)

    def test_predicting_the_mean_gives_zero_r2(self) -> None:
        truth = np.array([1.0, 2.0, 3.0, 4.0])

        m = evaluate_predictions(truth, np.full(4, truth.mean()))

        assert m.r2 == pytest.approx(0.0)

    def test_worse_than_the_mean_gives_negative_r2(self) -> None:
        m = evaluate_predictions(np.array([1.0, 2.0, 3.0]), np.array([10.0, 10.0, 10.0]))

        assert m.r2 < 0

    def test_missing_values_are_excluded_not_imputed(self) -> None:
        m = evaluate_predictions(np.array([1.0, np.nan, 3.0]), np.array([1.0, 2.0, 3.0]))

        assert m.n == 2
        assert m.rmse == pytest.approx(0.0)

    def test_no_overlapping_observations_yields_nan_not_a_crash(self) -> None:
        m = evaluate_predictions(np.array([np.nan]), np.array([1.0]))

        assert m.n == 0
        assert np.isnan(m.rmse)

    def test_exceedance_f1_rewards_catching_the_bad_days(self) -> None:
        """Flagging the dangerous hour matters more than the exact value."""
        truth = np.array([10.0, 50.0, 12.0, 60.0])

        caught = evaluate_predictions(truth, np.array([11.0, 45.0, 13.0, 55.0]))
        missed = evaluate_predictions(truth, np.array([11.0, 20.0, 13.0, 22.0]))

        assert caught.exceedance_f1 == pytest.approx(1.0)
        assert missed.exceedance_f1 == pytest.approx(0.0)

    def test_a_low_rmse_model_can_still_miss_every_exceedance(self) -> None:
        """Rare events are diluted by the easy hours, so RMSE hides them.

        Fifteen quiet hours predicted perfectly and one exceedance missed by
        6 µg/m³ looks excellent on RMSE and R², and is useless for the only
        hour a reader cared about.
        """
        truth = np.array([10.0] * 15 + [40.0])
        prediction = np.array([10.0] * 15 + [34.0])

        m = evaluate_predictions(truth, prediction)

        assert m.rmse == pytest.approx(1.5)
        assert m.r2 > 0.95
        assert m.exceedance_f1 == pytest.approx(0.0)


class TestRollingOrigin:
    def test_training_data_always_precedes_test_data(self) -> None:
        """Ordinary k-fold would leak the future; this is the point of the split."""
        frame = _series(["A"], datetime(2015, 1, 1), 200)

        for split in rolling_origin(frame, n_splits=4):
            assert split.train["ts_local"].max() <= split.test["ts_local"].min()

    def test_training_window_expands(self) -> None:
        frame = _series(["A"], datetime(2015, 1, 1), 200)

        sizes = [s.train.height for s in rolling_origin(frame, n_splits=4)]

        assert sizes == sorted(sizes)
        assert len(set(sizes)) > 1

    def test_every_split_has_data_on_both_sides(self) -> None:
        frame = _series(["A"], datetime(2015, 1, 1), 200)

        for split in rolling_origin(frame, n_splits=4):
            assert split.train.height > 0
            assert split.test.height > 0

    def test_too_few_rows_yields_nothing_rather_than_a_bad_split(self) -> None:
        frame = _series(["A"], datetime(2015, 1, 1), 2)

        assert list(rolling_origin(frame, n_splits=5)) == []


class TestLeaveOneStationOut:
    def test_the_held_out_station_is_absent_from_training(self) -> None:
        frame = _series(["A", "B", "C"], datetime(2015, 1, 1), 10)

        for split in leave_one_station_out(frame):
            held = split.test["station_name"].unique().to_list()
            assert len(held) == 1
            assert held[0] not in split.train["station_name"].unique().to_list()

    def test_one_split_per_station(self) -> None:
        frame = _series(["A", "B", "C"], datetime(2015, 1, 1), 10)

        assert len(list(leave_one_station_out(frame))) == 3


class TestLeaveOneYearOut:
    def test_the_held_out_year_is_absent_from_training(self) -> None:
        frame = pl.concat([_series(["A"], datetime(y, 1, 1), 24) for y in (2014, 2015, 2016)])

        for split in leave_one_year_out(frame):
            years = split.train["ts_local"].dt.year().unique().to_list()
            held = split.test["ts_local"].dt.year().unique().to_list()
            assert len(held) == 1
            assert held[0] not in years


class TestBaselines:
    def test_persistence_repeats_the_previous_hour(self) -> None:
        frame = pl.DataFrame(
            {
                "station_name": ["A"] * 3,
                "ts_local": [datetime(2015, 1, 1, h) for h in range(3)],
                "PM2.5": [10.0, 20.0, 30.0],
            }
        )

        assert persistence_baseline(frame).to_list() == [None, 10.0, 20.0]

    def test_persistence_does_not_leak_across_stations(self) -> None:
        frame = pl.DataFrame(
            {
                "station_name": ["A", "A", "B", "B"],
                "ts_local": [datetime(2015, 1, 1, h) for h in (0, 1, 0, 1)],
                "PM2.5": [10.0, 20.0, 100.0, 200.0],
            }
        )

        predictions = persistence_baseline(frame).to_list()

        assert predictions == [None, 10.0, None, 100.0], "each station starts fresh"

    def test_climatology_predicts_the_training_mean_for_that_slot(self) -> None:
        train = pl.DataFrame(
            {
                "station_name": ["A", "A"],
                "ts_local": [datetime(2015, 6, 1, 8), datetime(2015, 6, 2, 8)],
                "PM2.5": [10.0, 30.0],
            }
        )
        test = pl.DataFrame(
            {
                "station_name": ["A"],
                "ts_local": [datetime(2016, 6, 3, 8)],
                "PM2.5": [99.0],
            }
        )

        assert climatology_baseline(train, test).to_list() == [pytest.approx(20.0)]

    def test_climatology_falls_back_when_the_slot_is_unseen(self) -> None:
        train = pl.DataFrame(
            {
                "station_name": ["A"],
                "ts_local": [datetime(2015, 6, 1, 8)],
                "PM2.5": [10.0],
            }
        )
        test = pl.DataFrame(
            {
                "station_name": ["Z"],
                "ts_local": [datetime(2016, 12, 3, 22)],
                "PM2.5": [99.0],
            }
        )

        assert climatology_baseline(train, test).to_list() == [pytest.approx(10.0)]

    def test_climatology_uses_only_training_data(self) -> None:
        """A baseline that peeked at the test set would not be a baseline."""
        train = pl.DataFrame(
            {
                "station_name": ["A"],
                "ts_local": [datetime(2015, 6, 1, 8)],
                "PM2.5": [10.0],
            }
        )
        test = pl.DataFrame(
            {
                "station_name": ["A"],
                "ts_local": [datetime(2016, 6, 1, 8)],
                "PM2.5": [500.0],
            }
        )

        assert climatology_baseline(train, test).to_list() == [pytest.approx(10.0)]
