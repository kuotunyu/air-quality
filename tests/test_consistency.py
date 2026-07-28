"""Tests for range and physical-consistency checks."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import polars as pl
import pytest

from twair.qc.consistency import (
    check_consistency,
    check_ranges,
    range_report,
)
from twair.qc.flags import Flag

CONFIG = {
    "pollutants": {
        "PM2.5": {"valid_range": [0, 1000]},
        "RH": {"valid_range": [0, 100]},
        "AMB_TEMP": {"valid_range": [-10, 50]},
    }
}


def _obs(rows: list[tuple[str, str, float | None, str]]) -> pl.DataFrame:
    """rows of (station, pollutant, value, flag)."""
    return pl.DataFrame(
        {
            "station_name": [r[0] for r in rows],
            "pollutant": [r[1] for r in rows],
            "value": [r[2] for r in rows],
            "flag": [r[3] for r in rows],
            "ts_local": [datetime(2015, 6, 1, i % 24) for i in range(len(rows))],
        }
    )


class TestRanges:
    def test_values_inside_the_domain_stay_valid(self) -> None:
        frame = _obs([("二林", "RH", 65.0, Flag.VALID.value)])

        out = check_ranges(frame, config=CONFIG)

        assert out["flag"].to_list() == [Flag.VALID.value]

    def test_impossible_humidity_is_flagged(self) -> None:
        frame = _obs([("二林", "RH", 140.0, Flag.VALID.value)])

        out = check_ranges(frame, config=CONFIG)

        assert out["flag"].to_list() == [Flag.OUT_OF_RANGE.value]

    def test_the_outlier_value_is_kept_for_inspection(self) -> None:
        """A PM2.5 of 1500 may be a fault or a dust storm — that is analysis, not parsing."""
        frame = _obs([("二林", "PM2.5", 1500.0, Flag.VALID.value)])

        out = check_ranges(frame, config=CONFIG)

        assert out["value"].to_list() == [1500.0]

    def test_negative_temperature_within_domain_is_allowed(self) -> None:
        frame = _obs([("鞍部", "AMB_TEMP", -5.0, Flag.VALID.value)])

        out = check_ranges(frame, config=CONFIG)

        assert out["flag"].to_list() == [Flag.VALID.value]

    def test_agency_rejection_is_not_overwritten(self) -> None:
        frame = _obs([("二林", "RH", 140.0, Flag.INSTRUMENT_CHECK_INVALID.value)])

        out = check_ranges(frame, config=CONFIG)

        assert out["flag"].to_list() == [Flag.INSTRUMENT_CHECK_INVALID.value]

    def test_unknown_pollutant_has_no_bounds_and_is_left_alone(self) -> None:
        frame = _obs([("二林", "UVB", 99999.0, Flag.VALID.value)])

        out = check_ranges(frame, config=CONFIG)

        assert out["flag"].to_list() == [Flag.VALID.value]

    def test_report_counts_by_year_and_pollutant(self) -> None:
        frame = check_ranges(
            _obs(
                [
                    ("二林", "RH", 140.0, Flag.VALID.value),
                    ("二林", "RH", 150.0, Flag.VALID.value),
                    ("二林", "RH", 60.0, Flag.VALID.value),
                ]
            ),
            config=CONFIG,
        )

        report = range_report(frame)

        assert report.to_dicts() == [{"year": 2015, "pollutant": "RH", "n": 2}]


class TestConsistency:
    def _paired(self, pm25: float, pm10: float) -> pl.DataFrame:
        return pl.DataFrame(
            {
                "station_name": ["二林", "二林"],
                "pollutant": ["PM2.5", "PM10"],
                "value": [pm25, pm10],
                "flag": [Flag.VALID.value] * 2,
                "ts_local": [datetime(2015, 6, 1, 0)] * 2,
            }
        )

    def test_pm25_above_pm10_is_a_violation(self) -> None:
        """PM2.5 is a subset of PM10 by definition, so this cannot happen physically."""
        report = check_consistency(self._paired(pm25=60.0, pm10=40.0))

        row = report.filter(pl.col("rule") == "pm_subset").to_dicts()[0]
        assert row["checked"] == 1
        assert row["violations"] == 1
        assert row["violation_rate"] == 1.0

    def test_pm25_below_pm10_holds(self) -> None:
        report = check_consistency(self._paired(pm25=20.0, pm10=45.0))

        row = report.filter(pl.col("rule") == "pm_subset").to_dicts()[0]
        assert row["violations"] == 0

    def test_nox_identity_tolerates_rounding(self) -> None:
        frame = pl.DataFrame(
            {
                "station_name": ["二林"] * 3,
                "pollutant": ["NO", "NO2", "NOx"],
                "value": [4.0, 12.0, 16.05],
                "flag": [Flag.VALID.value] * 3,
                "ts_local": [datetime(2015, 6, 1, 0)] * 3,
            }
        )

        report = check_consistency(frame)

        assert report.filter(pl.col("rule") == "nox_identity")["violations"].to_list() == [0]

    def test_rules_are_skipped_when_inputs_are_absent(self) -> None:
        """A station that never measured PM10 must not manufacture violations."""
        frame = _obs([("關山", "PM2.5", 20.0, Flag.VALID.value)])

        report = check_consistency(frame)

        assert "pm_subset" not in report["rule"].to_list()

    def test_invalid_readings_are_excluded_from_the_check(self) -> None:
        frame = self._paired(pm25=60.0, pm10=40.0).with_columns(
            pl.Series("flag", [Flag.VALID.value, Flag.INSTRUMENT_CHECK_INVALID.value])
        )

        report = check_consistency(frame)

        assert report.is_empty(), "cannot compare against a reading the agency rejected"

    def test_empty_input_returns_empty_report(self) -> None:
        empty = _obs([]).clear()

        assert check_consistency(empty).is_empty()


class TestBuildSummaryMerge:
    """Rebuilding one year must not erase the record of the others."""

    def test_existing_years_survive_a_single_year_rebuild(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import polars as pl

        from twair import build as build_module

        outputs = tmp_path / "build"
        outputs.mkdir(parents=True)
        monkeypatch.setattr(build_module, "outputs_dir", lambda name=None: tmp_path / name)

        first = [build_module.YearResult(year=y, rows=100, stations=1) for y in (2010, 2011)]
        build_module._report(first)

        rebuilt = build_module.YearResult(year=2011, rows=999, stations=2)
        build_module._report([rebuilt])

        summary = pl.read_csv(outputs / "year_summary.csv").sort("year")
        assert summary["year"].to_list() == [2010, 2011]
        assert summary.filter(pl.col("year") == 2011)["rows"].item() == 999
        assert summary.filter(pl.col("year") == 2010)["rows"].item() == 100
