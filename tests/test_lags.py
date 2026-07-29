"""Tests for lag construction — the place forecasting code leaks.

The trick throughout: the series carries its own timestamp as its value, hour
0 holding 0.0, hour 1 holding 1.0 and so on. Any shift in the wrong direction
then shows up as an exact arithmetic fact rather than as a suspiciously good
score, which is the only form in which leakage is easy to catch.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import polars as pl
import pytest

from twair.features.lags import (
    add_lag_features,
    add_target,
    complete_hourly_index,
    lag_feature_names,
)


def _clock(
    n: int = 400, station: str = "站", start: datetime = datetime(2020, 1, 1)
) -> pl.DataFrame:
    """A series whose value at each hour *is* the hour index."""
    return pl.DataFrame(
        {
            "station_name": [station] * n,
            "ts_local": [start + timedelta(hours=i) for i in range(n)],
            "PM2.5": [float(i) for i in range(n)],
        }
    )


class TestNoLookAhead:
    def test_lag1_is_the_current_row_not_the_next_one(self) -> None:
        """`lag_1` is the most recent observation a forecaster would have."""
        out = add_lag_features(_clock(), column="PM2.5")
        row = out.filter(pl.col("PM2.5") == 100.0)

        assert row["PM2.5_lag1"][0] == 100.0

    def test_every_lag_points_backwards(self) -> None:
        out = add_lag_features(_clock(), column="PM2.5")
        row = out.filter(pl.col("PM2.5") == 200.0)

        for k in (1, 2, 3, 6, 12, 24, 48, 168):
            assert row[f"PM2.5_lag{k}"][0] == 200.0 - (k - 1), f"lag{k} looked forward"

    def test_the_target_points_forwards_by_exactly_the_horizon(self) -> None:
        out = add_target(_clock(), column="PM2.5", horizon=24)
        row = out.filter(pl.col("PM2.5") == 100.0)

        assert row["target"][0] == 124.0

    def test_no_feature_ever_equals_the_target(self) -> None:
        """The whole safety property, stated as one assertion.

        With features shifted backwards and the target forwards, a feature that
        equals the target would mean the two shifts collided — the exact bug
        that produces a spectacular score and a useless model.
        """
        out = add_target(
            add_lag_features(_clock(600), column="PM2.5"), column="PM2.5", horizon=6
        ).drop_nulls()

        features = [c for c in out.columns if c.startswith("PM2.5_lag")]
        for name in features:
            assert (out[name] != out["target"]).all(), f"{name} equals the target"

    def test_rolling_means_end_at_the_current_row(self) -> None:
        out = add_lag_features(_clock(), column="PM2.5", windows=(3,))
        row = out.filter(pl.col("PM2.5") == 100.0)

        # Hours 98, 99, 100 — never 101.
        assert row["PM2.5_mean3"][0] == pytest.approx(99.0)

    def test_a_longer_horizon_moves_the_target_further(self) -> None:
        near = add_target(_clock(), column="PM2.5", horizon=1)
        far = add_target(_clock(), column="PM2.5", horizon=48)

        assert near.filter(pl.col("PM2.5") == 50.0)["target"][0] == 51.0
        assert far.filter(pl.col("PM2.5") == 50.0)["target"][0] == 98.0


class TestGaps:
    def test_a_lag_does_not_step_over_a_missing_hour(self) -> None:
        """After an outage, `lag_1` must be null rather than pre-outage data.

        Without the complete index, a three-day gap hands the model the value
        from before it and calls that "an hour ago".
        """
        frame = _clock(200)
        with_gap = frame.filter(~pl.col("PM2.5").is_between(100, 172))

        filled = complete_hourly_index(with_gap)
        out = add_lag_features(filled, column="PM2.5")

        just_after = out.filter(pl.col("PM2.5") == 173.0)
        assert just_after["PM2.5_lag2"][0] is None, "lag reached across the gap"

    def test_the_complete_index_inserts_the_missing_hours(self) -> None:
        frame = _clock(100).filter(~pl.col("PM2.5").is_between(40, 49))

        filled = complete_hourly_index(frame)

        assert filled.height == 100
        assert filled["PM2.5"].null_count() == 10

    def test_stations_get_their_own_index(self) -> None:
        a = _clock(50, station="甲")
        b = _clock(30, station="乙")

        filled = complete_hourly_index(pl.concat([a, b]))

        assert filled.filter(pl.col("station_name") == "甲").height == 50
        assert filled.filter(pl.col("station_name") == "乙").height == 30


class TestPerStation:
    def test_a_lag_never_crosses_a_station_boundary(self) -> None:
        """Two stations concatenated must not bleed into each other."""
        a = _clock(200, station="甲")
        b = _clock(200, station="乙", start=datetime(2020, 1, 1))

        out = add_lag_features(pl.concat([a, b]), column="PM2.5")
        first_of_b = out.filter((pl.col("station_name") == "乙") & (pl.col("PM2.5") == 0.0))

        assert first_of_b["PM2.5_lag2"][0] is None, "read the previous station's tail"

    def test_the_target_never_crosses_a_station_boundary(self) -> None:
        a = _clock(100, station="甲")
        b = _clock(100, station="乙")

        out = add_target(pl.concat([a, b]), column="PM2.5", horizon=3)
        last_of_a = out.filter((pl.col("station_name") == "甲") & (pl.col("PM2.5") == 99.0))

        assert last_of_a["target"][0] is None


class TestContract:
    def test_the_declared_feature_names_match_what_is_produced(self) -> None:
        """A name list that drifts from the builder trains on the wrong columns."""
        out = add_lag_features(_clock(), column="PM2.5")

        for name in lag_feature_names("PM2.5"):
            assert name in out.columns, f"{name} was declared but not produced"

    def test_a_zero_horizon_is_refused(self) -> None:
        with pytest.raises(ValueError, match="at least 1 hour"):
            add_target(_clock(), column="PM2.5", horizon=0)

    def test_an_absent_column_fails_loudly(self) -> None:
        with pytest.raises(KeyError, match="NOx"):
            add_lag_features(_clock(), column="NOx")
