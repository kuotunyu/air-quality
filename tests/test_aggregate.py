"""Tests for daily and monthly aggregation.

The circular-mean tests are the important ones: they encode the single
statistical error the M1 baseline reproduces on purpose.
"""

from __future__ import annotations

import math
from datetime import date, datetime
from pathlib import Path

import polars as pl
import pytest

from twair.store.aggregate import (
    aggregate_daily,
    aggregate_monthly,
    circular_mean_expr,
    circular_resultant_expr,
)

POLLUTANT_CONFIG = {
    "pollutants": {
        "PM2.5": {},
        "WD_HR": {"circular": True},
    }
}
QC_CONFIG = {
    "aggregation": {
        "hourly_to_daily": {"min_valid_hours": 16},
        "daily_to_monthly": {"min_valid_days_ratio": 0.75},
    }
}


def _mean(values: list[float]) -> float:
    frame = pl.DataFrame({"value": values})
    return frame.select(circular_mean_expr().alias("m"))["m"][0]  # type: ignore[no-any-return]


def _resultant(values: list[float]) -> float:
    frame = pl.DataFrame({"value": values})
    return frame.select(circular_resultant_expr().alias("r"))["r"][0]  # type: ignore[no-any-return]


class TestCircularMean:
    """Wind direction is circular; the arithmetic mean is simply wrong."""

    def test_the_wraparound_case_the_2018_project_got_wrong(self) -> None:
        """350° and 10° average to due north, not due south."""
        arithmetic = (350 + 10) / 2

        assert arithmetic == 180.0, "this is what a plain arithmetic mean gives"
        assert _mean([350.0, 10.0]) == pytest.approx(0.0, abs=1e-6)

    def test_identical_directions_are_preserved(self) -> None:
        assert _mean([90.0, 90.0, 90.0]) == pytest.approx(90.0)

    def test_non_wrapping_values_match_the_arithmetic_mean(self) -> None:
        assert _mean([80.0, 100.0]) == pytest.approx(90.0)

    def test_result_stays_within_zero_to_360(self) -> None:
        for values in ([355.0, 5.0], [270.0, 90.0001], [1.0, 359.0]):
            assert 0.0 <= _mean(values) < 360.0

    def test_opposing_directions_have_no_meaningful_mean(self) -> None:
        """The resultant length exposes what a bare mean would hide."""
        assert _resultant([0.0, 180.0]) == pytest.approx(0.0, abs=1e-9)

    def test_consistent_directions_have_resultant_one(self) -> None:
        assert _resultant([45.0, 45.0, 45.0]) == pytest.approx(1.0)

    def test_resultant_is_between_zero_and_one(self) -> None:
        assert 0.0 <= _resultant([10.0, 80.0, 200.0]) <= 1.0

    def test_resultant_matches_the_closed_form(self) -> None:
        values = [10.0, 20.0, 30.0]
        radians = [math.radians(v) for v in values]
        expected = math.hypot(
            sum(math.sin(r) for r in radians) / len(radians),
            sum(math.cos(r) for r in radians) / len(radians),
        )

        assert _resultant(values) == pytest.approx(expected)


def _hourly(rows: list[tuple[str, str, datetime, float]]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "station_name": [r[0] for r in rows],
            "pollutant": [r[1] for r in rows],
            "ts_local": [r[2] for r in rows],
            "value": [r[3] for r in rows],
            "flag": ["valid"] * len(rows),
        }
    )


def _write_store(tmp_path: Path, frame: pl.DataFrame) -> Path:
    """Persist a minimal store the aggregation functions can scan."""
    from twair.store.writer import write_observations

    complete = frame.with_columns(
        pl.lit(False).alias("value_retained"),
        pl.lit("test").alias("generation"),
        pl.lit("test.csv").alias("source_member"),
    )
    write_observations(complete, root=tmp_path)
    return tmp_path


class TestDailyAggregation:
    def test_sparse_days_lose_their_mean_but_keep_their_counts(self, tmp_path: Path) -> None:
        """The baseline averages regardless of how many hours contributed."""
        rows = [("二林", "PM2.5", datetime(2015, 6, 1, h), 20.0 + h) for h in range(5)]
        root = _write_store(tmp_path, _hourly(rows))

        daily = aggregate_daily(root, config=POLLUTANT_CONFIG, qc_config=QC_CONFIG)

        row = daily.to_dicts()[0]
        assert row["n_valid"] == 5
        assert row["mean"] is None, "5 of 24 hours must not yield a daily mean"
        assert row["meets_threshold"] is False

    def test_well_covered_days_produce_a_mean(self, tmp_path: Path) -> None:
        rows = [("二林", "PM2.5", datetime(2015, 6, 1, h), 20.0) for h in range(20)]
        root = _write_store(tmp_path, _hourly(rows))

        daily = aggregate_daily(root, config=POLLUTANT_CONFIG, qc_config=QC_CONFIG)

        row = daily.to_dicts()[0]
        assert row["mean"] == pytest.approx(20.0)
        assert row["meets_threshold"] is True
        assert row["coverage_ratio"] == pytest.approx(20 / 24)

    def test_wind_direction_uses_the_circular_mean(self, tmp_path: Path) -> None:
        values = [350.0, 10.0] * 10  # 20 hours straddling north
        rows = [("二林", "WD_HR", datetime(2015, 6, 1, h), v) for h, v in enumerate(values)]
        root = _write_store(tmp_path, _hourly(rows))

        daily = aggregate_daily(root, config=POLLUTANT_CONFIG, qc_config=QC_CONFIG)

        row = daily.to_dicts()[0]
        assert row["is_circular"] is True
        assert row["mean"] == pytest.approx(0.0, abs=1e-6)
        assert row["mean"] != pytest.approx(180.0)

    def test_circular_rows_carry_a_consistency_score(self, tmp_path: Path) -> None:
        rows = [("二林", "WD_HR", datetime(2015, 6, 1, h), 45.0) for h in range(20)]
        root = _write_store(tmp_path, _hourly(rows))

        daily = aggregate_daily(root, config=POLLUTANT_CONFIG, qc_config=QC_CONFIG)

        assert daily.to_dicts()[0]["consistency"] == pytest.approx(1.0)

    def test_linear_rows_have_no_consistency_score(self, tmp_path: Path) -> None:
        rows = [("二林", "PM2.5", datetime(2015, 6, 1, h), 20.0) for h in range(20)]
        root = _write_store(tmp_path, _hourly(rows))

        daily = aggregate_daily(root, config=POLLUTANT_CONFIG, qc_config=QC_CONFIG)

        assert daily.to_dicts()[0]["consistency"] is None


class TestMonthlyAggregation:
    def _daily(self, days: int, *, pollutant: str = "PM2.5", value: float = 20.0) -> pl.DataFrame:
        return pl.DataFrame(
            {
                "station_name": ["二林"] * days,
                "pollutant": [pollutant] * days,
                "date": [date(2015, 6, d + 1) for d in range(days)],
                "mean": [value] * days,
                "min": [value] * days,
                "max": [value] * days,
                "n_valid": [24] * days,
                "consistency": [None] * days,
                "coverage_ratio": [1.0] * days,
                "is_circular": [pollutant == "WD_HR"] * days,
                "meets_threshold": [True] * days,
            }
        )

    def test_sparse_months_lose_their_mean(self) -> None:
        monthly = aggregate_monthly(self._daily(10), config=POLLUTANT_CONFIG, qc_config=QC_CONFIG)

        row = monthly.to_dicts()[0]
        assert row["n_days"] == 10
        assert row["mean"] is None, "10 of 30 days must not yield a monthly mean"

    def test_well_covered_months_produce_a_mean(self) -> None:
        monthly = aggregate_monthly(self._daily(28), config=POLLUTANT_CONFIG, qc_config=QC_CONFIG)

        row = monthly.to_dicts()[0]
        assert row["mean"] == pytest.approx(20.0)
        assert row["meets_threshold"] is True

    def test_days_that_failed_their_own_threshold_are_excluded(self) -> None:
        daily = self._daily(28).with_columns(
            pl.Series("meets_threshold", [True] * 20 + [False] * 8)
        )

        monthly = aggregate_monthly(daily, config=POLLUTANT_CONFIG, qc_config=QC_CONFIG)

        assert monthly.to_dicts()[0]["n_days"] == 20

    def test_monthly_wind_direction_is_also_circular(self) -> None:
        daily = self._daily(28, pollutant="WD_HR")
        daily = daily.with_columns(
            pl.Series("mean", [350.0, 10.0] * 14),
            pl.Series("is_circular", [True] * 28),
        )

        monthly = aggregate_monthly(daily, config=POLLUTANT_CONFIG, qc_config=QC_CONFIG)

        row = monthly.to_dicts()[0]
        assert row["mean"] == pytest.approx(0.0, abs=1e-6)
        assert row["consistency"] is not None


def test_build_aggregates_creates_its_output_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A full aggregation must not run to completion and then die on the write.

    processed_dir resolves a path without creating it, so the daily/monthly
    directories have to be made before writing.
    """
    from twair.store import aggregate as agg

    store = tmp_path / "store"
    rows = [("二林", "PM2.5", datetime(2015, 6, 1, h), 20.0) for h in range(20)]
    _write_store(store, _hourly(rows))

    processed = tmp_path / "processed"
    monkeypatch.setattr(agg, "processed_dir", lambda table=None: processed / table)

    tables = agg.build_aggregates(store)

    assert (processed / "daily" / "daily.parquet").exists()
    assert (processed / "monthly" / "monthly.parquet").exists()
    assert set(tables) == {"daily", "monthly"}
