"""Tests for the D11 sensitivity analysis.

The comparison this module produces is only worth reading if the masking is
honest, so that is what gets pinned: the hidden runs have to be the shape real
gaps are, the recorded length has to belong to the cell it is attached to, and
hiding must never destroy the truth it is scored against.

Real missingness is clustered. An evaluation that hides isolated cells hands
linear interpolation a series of one-hour gaps it cannot fail, and then reports
that linear interpolation is excellent.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import polars as pl
import pytest

from twair.analysis.imputation import (
    bucket_expr,
    gap_runs,
    mask_like_reality,
    score_strategies,
)

START = datetime(2020, 1, 1)


def _panel(stations: int = 2, hours: int = 500) -> pl.DataFrame:
    rows = []
    for s in range(stations):
        name = f"站{s}"
        for i in range(hours):
            rows.append(
                {
                    "station_name": name,
                    "ts_local": START + timedelta(hours=i),
                    # A smooth-ish series so interpolation has something to do.
                    "PM2.5": 20.0 + 10.0 * np.sin(i / 12.0) + (i % 7),
                }
            )
    return pl.DataFrame(rows).sort("station_name", "ts_local")


class TestGapShape:
    def test_a_run_of_missing_hours_is_counted_once_with_its_length(self) -> None:
        frame = _panel(stations=1, hours=20).with_columns(
            pl.when(
                pl.col("ts_local").is_between(
                    START + timedelta(hours=5), START + timedelta(hours=8)
                )
            )
            .then(None)
            .otherwise(pl.col("PM2.5"))
            .alias("PM2.5")
        )

        runs = gap_runs(frame)

        assert runs.height == 1
        assert runs["length"][0] == 4

    def test_runs_do_not_merge_across_stations(self) -> None:
        """Station A ending on a gap and station B starting on one are two
        gaps, not a single eight-hour outage that never happened."""
        frame = _panel(stations=2, hours=10)
        last_a = frame.filter(pl.col("station_name") == "站0")["ts_local"].max()
        first_b = frame.filter(pl.col("station_name") == "站1")["ts_local"].min()
        # Both stations span the same hours, so the condition has to name the
        # station as well — otherwise this hides four cells and tests nothing.
        frame = frame.with_columns(
            pl.when(
                ((pl.col("station_name") == "站0") & (pl.col("ts_local") == last_a))
                | ((pl.col("station_name") == "站1") & (pl.col("ts_local") == first_b))
            )
            .then(None)
            .otherwise(pl.col("PM2.5"))
            .alias("PM2.5")
        )

        runs = gap_runs(frame)

        assert runs.height == 2
        assert runs["length"].to_list() == [1, 1]


class TestTheMaskingIsHonest:
    def test_the_recorded_length_belongs_to_the_cell_it_is_attached_to(self) -> None:
        """The bug this exists for.

        Collecting run lengths in acceptance order and zipping them against
        `flatnonzero(hidden)` — which is in row order — mislabels every cell as
        soon as one run is placed earlier in the frame than a run accepted
        before it. It is invisible in the pooled score and shows up only per
        gap length, where it had linear interpolation apparently reconstructing
        48-hour gaps its own configuration forbids it to touch.
        """
        panel = mask_like_reality(_panel(), fraction=0.1, seed=7, lengths=[1, 5, 24])

        hidden = panel.truth.sort("station_name", "ts_local").with_columns(
            # Contiguous hidden cells within a station form one run.
            (
                (pl.col("ts_local").diff().dt.total_hours() != 1)
                | (pl.col("station_name") != pl.col("station_name").shift(1))
            )
            .fill_null(value=True)
            .cum_sum()
            .alias("run")
        )
        measured = hidden.group_by("run").agg(
            pl.len().alias("actual"), pl.col("gap_length").unique().alias("recorded")
        )

        for row in measured.iter_rows(named=True):
            assert len(row["recorded"]) == 1, "one run must carry one length"
            assert row["recorded"][0] == row["actual"], (
                f"run of {row['actual']}h is labelled {row['recorded'][0]}h"
            )

    def test_only_known_readings_are_hidden(self) -> None:
        """Hiding a value that was already missing gives nothing to score."""
        panel = mask_like_reality(_panel(), fraction=0.05, seed=1)

        assert panel.truth["true_value"].null_count() == 0
        assert panel.n_hidden > 0

    def test_the_hidden_values_are_actually_gone_from_the_frame(self) -> None:
        panel = mask_like_reality(_panel(), fraction=0.05, seed=2)

        remaining = panel.frame.join(
            panel.truth.select("station_name", "ts_local"),
            on=["station_name", "ts_local"],
            how="inner",
        )
        assert remaining["PM2.5"].null_count() == remaining.height

    def test_the_lengths_hidden_come_from_the_distribution_asked_for(self) -> None:
        panel = mask_like_reality(_panel(), fraction=0.1, seed=3, lengths=[6])

        assert set(panel.truth["gap_length"].unique().to_list()) == {6}

    def test_a_fraction_that_cannot_hide_one_reading_is_refused(self) -> None:
        with pytest.raises(ValueError, match="too small"):
            mask_like_reality(_panel(stations=1, hours=30), fraction=1e-9)


class TestScoring:
    def test_the_default_strategy_recovers_nothing_and_says_so(self) -> None:
        panel = mask_like_reality(_panel(), fraction=0.05, seed=4, lengths=[3])

        scores = score_strategies(panel, strategies=("none", "interpolate"))
        none_row = scores.filter((pl.col("strategy") == "none") & (pl.col("gap_bucket") == "all"))

        assert none_row["recovered"][0] == 0
        assert none_row["mae"][0] is None, "an error over zero reconstructions is undefined"

    def test_interpolation_never_scores_a_gap_longer_than_its_own_limit(self) -> None:
        """The property the mislabelling hid. `max_gap_hours` is 3, so every
        bucket above it must be empty for this strategy."""
        panel = mask_like_reality(_panel(), fraction=0.15, seed=5, lengths=[2, 30])

        scores = score_strategies(panel, strategies=("interpolate",))
        long_buckets = scores.filter(
            (pl.col("gap_bucket").is_in(["4-12h", "13-48h", ">48h"])) & (pl.col("n") > 0)
        )

        assert long_buckets.is_empty(), f"filled beyond the bound: {long_buckets}"


class TestBuckets:
    @pytest.mark.parametrize(
        ("length", "expected"),
        [
            (1, "1h"),
            (2, "2-3h"),
            (3, "2-3h"),
            (4, "4-12h"),
            (12, "4-12h"),
            (48, "13-48h"),
            (72, ">48h"),
        ],
    )
    def test_a_gap_lands_in_the_bucket_that_brackets_it(self, length: int, expected: str) -> None:
        """The 3-hour boundary matters: it is where the configured
        interpolation limit falls, so it must not be inside a bucket."""
        frame = pl.DataFrame({"length": [length]})

        assert frame.select(bucket_expr())["gap_bucket"][0] == expected
