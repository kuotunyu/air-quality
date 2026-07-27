"""Tests for M1 — the deliberate reproduction of the 2018 method.

The replication is only useful if it stays faithful to the original's choices,
including the wrong ones. These tests pin those choices so a later "improvement"
cannot quietly drift the baseline that every comparison rests on.
"""

from __future__ import annotations

from datetime import datetime

import polars as pl
import pytest

from twair.analysis.replication import (
    ORIGINAL_PERIOD,
    ORIGINAL_PREDICTORS,
    RESPONSE,
    load_expected,
    naive_monthly_panel,
)
from twair.qc.flags import Flag


def _store(tmp_path, rows):  # type: ignore[no-untyped-def]
    from twair.store.writer import write_observations

    frame = pl.DataFrame(
        {
            "station_name": [r[0] for r in rows],
            "pollutant": [r[1] for r in rows],
            "ts_local": [r[2] for r in rows],
            "value": [r[3] for r in rows],
            "flag": [r[4] for r in rows],
            "value_retained": [False] * len(rows),
            "generation": ["test"] * len(rows),
            "source_member": ["test.csv"] * len(rows),
        }
    )
    write_observations(frame, root=tmp_path)
    return tmp_path


def _full_month(station: str, year: int, month: int, values: dict[str, float]):  # type: ignore[no-untyped-def]
    rows = []
    for pollutant, value in values.items():
        for day in range(1, 4):
            rows.append(
                (station, pollutant, datetime(year, month, day, 0), value, Flag.VALID.value)
            )
    return rows


class TestExpectedValues:
    def test_the_reference_file_loads(self) -> None:
        assert load_expected()["study"]["n_observations"] == 7286

    def test_nitric_oxide_survives_yaml(self) -> None:
        """Unquoted `NO` becomes False — it bit this file too."""
        expected = load_expected()

        assert "NO" in expected["correlation_with_pm25"]
        assert expected["correlation_with_pm25"]["NO"] == pytest.approx(0.06395)

    def test_predictor_list_matches_the_module(self) -> None:
        assert tuple(load_expected()["study"]["predictors"]) == ORIGINAL_PREDICTORS

    def test_pm10_is_among_the_original_predictors(self) -> None:
        """The leak is deliberate here: M1 must reproduce it, not fix it."""
        assert "PM10" in ORIGINAL_PREDICTORS


class TestNaiveAggregation:
    def test_period_matches_the_original(self) -> None:
        assert ORIGINAL_PERIOD == (2010, 2017)

    def test_years_outside_the_window_are_excluded(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        values = dict.fromkeys([RESPONSE, *ORIGINAL_PREDICTORS], 1.0)
        rows = _full_month("二林", 2009, 6, values) + _full_month("二林", 2012, 6, values)
        root = _store(tmp_path, rows)

        panel = naive_monthly_panel(root)

        assert panel["month"].dt.year().unique().to_list() == [2012]

    def test_wind_direction_is_averaged_arithmetically(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """The original's error, reproduced on purpose: 350 and 10 average to 180."""
        values = dict.fromkeys([RESPONSE, *ORIGINAL_PREDICTORS], 1.0)
        rows = _full_month("二林", 2012, 6, values)
        rows = [r for r in rows if r[1] != "WD_HR"]
        rows += [
            ("二林", "WD_HR", datetime(2012, 6, 1, 0), 350.0, Flag.VALID.value),
            ("二林", "WD_HR", datetime(2012, 6, 1, 1), 10.0, Flag.VALID.value),
        ]
        root = _store(tmp_path, rows)

        panel = naive_monthly_panel(root)

        assert panel["WD_HR"].to_list() == [pytest.approx(180.0)], (
            "M1 must not use the circular mean — it exists to reproduce the original"
        )

    def test_sparse_months_are_kept_without_a_coverage_check(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """The original applied no threshold; a one-hour month counted the same."""
        values = dict.fromkeys([RESPONSE, *ORIGINAL_PREDICTORS], 5.0)
        rows = [
            ("二林", p, datetime(2012, 6, 1, 0), v, Flag.VALID.value) for p, v in values.items()
        ]
        root = _store(tmp_path, rows)

        panel = naive_monthly_panel(root)

        assert panel.height == 1
        assert panel[RESPONSE].to_list() == [5.0]

    def test_incomplete_rows_are_dropped(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """The published N implies complete cases across all thirteen variables."""
        values = dict.fromkeys([RESPONSE, *ORIGINAL_PREDICTORS], 1.0)
        complete = _full_month("二林", 2012, 6, values)
        # 關山 never reports SO2, so its month cannot be a complete case.
        partial = [r for r in _full_month("關山", 2012, 6, values) if r[1] != "SO2"]
        root = _store(tmp_path, complete + partial)

        panel = naive_monthly_panel(root)

        assert panel["station_name"].to_list() == ["二林"]

    def test_a_store_missing_a_variable_entirely_is_an_error(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """Distinct from an empty result: this input cannot support the analysis."""
        root = _store(tmp_path, _full_month("關山", 2012, 6, {RESPONSE: 20.0, "PM10": 40.0}))

        with pytest.raises(RuntimeError, match="lacks pollutants"):
            naive_monthly_panel(root)

    def test_rejected_readings_are_excluded_by_default(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        values = dict.fromkeys([RESPONSE, *ORIGINAL_PREDICTORS], 1.0)
        rows = _full_month("二林", 2012, 6, values)
        rows = [
            (r[0], r[1], r[2], r[3], Flag.INSTRUMENT_CHECK_INVALID.value) if r[1] == RESPONSE else r
            for r in rows
        ]
        # A second station keeps every pollutant present in the store, so the
        # inventory check passes and this exercises filtering, not absence.
        rows += _full_month("關山", 2012, 6, values)
        root = _store(tmp_path, rows)

        panel = naive_monthly_panel(root, valid_only=True)

        assert panel["station_name"].to_list() == ["關山"]

    def test_no_rain_hours_count_toward_the_rainfall_mean(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """NR is a measured zero. Excluding it inflated the mean tenfold."""
        values = dict.fromkeys([RESPONSE, *ORIGINAL_PREDICTORS], 1.0)
        rows = [r for r in _full_month("二林", 2012, 6, values) if r[1] != "RAINFALL"]
        rows += [
            ("二林", "RAINFALL", datetime(2012, 6, 1, 0), 4.0, Flag.VALID.value),
            ("二林", "RAINFALL", datetime(2012, 6, 1, 1), 0.0, Flag.NO_RAIN.value),
            ("二林", "RAINFALL", datetime(2012, 6, 1, 2), 0.0, Flag.NO_RAIN.value),
            ("二林", "RAINFALL", datetime(2012, 6, 1, 3), 0.0, Flag.NO_RAIN.value),
        ]
        root = _store(tmp_path, rows)

        panel = naive_monthly_panel(root)

        assert panel["RAINFALL"].to_list() == [pytest.approx(1.0)], "4mm over 4 hours"
