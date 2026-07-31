"""Tests for flatline detection.

The claims worth pinning are the ones a naive implementation gets wrong: that a
gap in the record ends a run rather than joining two readings across it, that
"the agency rejected none of this" is distinguishable from "there was nothing
here to reject", and that the calibration curve is built from every run rather
than only the ones above the threshold it is supposed to justify.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

import polars as pl
import pytest

from twair.qc.stuck import calibration_curve, stuck_conf, stuck_runs

START = datetime(2003, 6, 1)


def _frame(
    values: Sequence[float | None],
    *,
    flags: Sequence[str] | None = None,
    station: str = "西屯",
    pollutant: str = "PM10",
    hours: Sequence[int] | None = None,
    generation: str = "legacy_csv_big5",
) -> pl.DataFrame:
    """Consecutive hourly readings unless `hours` says otherwise."""
    offsets = list(hours) if hours is not None else list(range(len(values)))
    return pl.DataFrame(
        {
            "station_name": [station] * len(values),
            "pollutant": [pollutant] * len(values),
            "ts_local": [START + timedelta(hours=h) for h in offsets],
            "value": list(values),
            "flag": list(flags) if flags is not None else ["valid"] * len(values),
            "generation": [generation] * len(values),
        }
    )


class TestShippedConfig:
    def test_the_threshold_lives_in_config_with_the_measurands(self) -> None:
        block = stuck_conf()

        assert block["min_length_hours"] >= 1
        assert block["pollutants"]

    def test_no_circular_measurand_is_scanned(self) -> None:
        """A repeated bearing is not the same phenomenon as a repeated concentration."""
        assert not {"WIND_DIREC", "WD_HR"} & set(stuck_conf()["pollutants"])


class TestRunDelimitation:
    def test_an_unchanging_reading_becomes_one_run(self) -> None:
        runs = stuck_runs(_frame([12.0] * 8), min_length_hours=6)

        assert runs.height == 1
        assert runs["length_hours"].to_list() == [8]

    def test_a_change_ends_the_run(self) -> None:
        runs = stuck_runs(_frame([12.0] * 6 + [13.0] * 6), min_length_hours=6)

        assert sorted(runs["length_hours"].to_list()) == [6, 6]

    def test_a_gap_ends_the_run_rather_than_joining_across_it(self) -> None:
        """Two identical readings either side of an outage are two readings.

        Joining them would manufacture exactly the thing being looked for — the
        longest flatlines in the record would all be the longest outages.
        """
        hours = [*range(6), *range(200, 206)]
        runs = stuck_runs(_frame([12.0] * 12, hours=hours), min_length_hours=6)

        assert sorted(runs["length_hours"].to_list()) == [6, 6]

    def test_a_run_shorter_than_the_threshold_is_not_returned(self) -> None:
        assert stuck_runs(_frame([12.0] * 5), min_length_hours=6).is_empty()

    def test_runs_are_split_per_station_and_measurand(self) -> None:
        frame = pl.concat(
            [
                _frame([12.0] * 6, station="西屯"),
                _frame([12.0] * 6, station="忠明"),
                _frame([12.0] * 6, pollutant="SO2"),
            ]
        )

        runs = stuck_runs(frame, min_length_hours=6)

        assert runs.height == 3
        assert set(runs["length_hours"].to_list()) == {6}

    def test_the_span_matches_the_length(self) -> None:
        runs = stuck_runs(_frame([12.0] * 9), min_length_hours=6)

        row = runs.row(0, named=True)
        span = (row["t_end"] - row["t_start"]).total_seconds() / 3600
        assert row["length_hours"] == span + 1

    def test_an_empty_frame_yields_the_schema_not_a_crash(self) -> None:
        runs = stuck_runs(pl.DataFrame(), min_length_hours=6)

        assert runs.height == 0
        assert "length_hours" in runs.columns


class TestAgencyAgreement:
    def test_the_share_the_agency_rejected_is_carried(self) -> None:
        flags = ["valid"] * 3 + ["instrument_check_invalid"] * 3
        runs = stuck_runs(_frame([12.0] * 6, flags=flags), min_length_hours=6)

        assert runs["agency_rejected"].to_list() == [pytest.approx(0.5)]

    def test_a_flatline_the_agency_accepted_reads_zero_not_null(self) -> None:
        """Zero here is a measurement: every hour was on the record and kept."""
        runs = stuck_runs(_frame([12.0] * 6), min_length_hours=6)

        assert runs["agency_rejected"].to_list() == [0.0]

    def test_the_modern_era_reads_null_rather_than_zero(self) -> None:
        """From 2018 a rejected reading carries no number.

        It therefore never enters a run, and every modern run would otherwise
        come out at exactly 0.0 — "the agency accepted all of this" and "no
        rejected hour could have been seen" rendering as the same number.
        """
        runs = stuck_runs(_frame([12.0] * 6, generation="modern_csv_utf8"), min_length_hours=6)

        assert runs["agency_rejected"].to_list() == [None]


class TestZeroAndNegative:
    def test_a_flatline_at_zero_is_marked_apart(self) -> None:
        """The data says zero behaves oppositely: an isolated zero is rejected
        67% of the time and the rate falls as the run lengthens."""
        runs = stuck_runs(_frame([0.0] * 6), min_length_hours=6)

        assert runs["at_zero"].to_list() == [True]

    def test_a_negative_flatline_is_marked_so_a_sentinel_can_be_told_apart(self) -> None:
        """`-99` is a documented legacy no-data marker, not a frozen sensor."""
        runs = stuck_runs(_frame([-99.0] * 6), min_length_hours=6)

        assert runs["negative"].to_list() == [True]
        assert runs["value"].to_list() == [-99.0]


class TestCalibration:
    def test_the_curve_covers_lengths_below_the_threshold(self) -> None:
        """A threshold justified only by data above itself is not justified.

        The flat part of the curve — where a repeat is ordinary — is the whole
        reason the threshold sits where it does, and it is entirely below it.
        """
        every = stuck_runs(_frame([12.0, 13.0, 14.0, 15.0] + [16.0] * 8), min_length_hours=1)

        curve = calibration_curve(every)

        assert "01" in curve["bucket"].to_list()
        assert "06-08" in curve["bucket"].to_list()

    def test_zero_and_non_zero_are_reported_apart(self) -> None:
        frame = pl.concat([_frame([0.0] * 6), _frame([12.0] * 6, station="忠明")])

        curve = calibration_curve(stuck_runs(frame, min_length_hours=1))

        assert set(curve["at_zero"].to_list()) == {True, False}

    def test_an_empty_frame_yields_the_curve_schema(self) -> None:
        curve = calibration_curve(stuck_runs(pl.DataFrame(), min_length_hours=1))

        assert curve.height == 0
        assert "agency_rejected" in curve.columns
