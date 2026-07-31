"""Tests for excursion detection.

The claims worth pinning are not "it finds the spike" — any threshold does that.
They are the ones the design argued for against a simpler alternative: that a
sustained rise does not move its own baseline, that a null never silently ends a
run, that "we could not look" never renders as "we looked and found none", and
that a censored run is never called short.
"""

from __future__ import annotations

import pathlib
from collections.abc import Sequence
from datetime import datetime, timedelta

import polars as pl
import pytest

from twair.qc.outliers import (
    BASELINE_CELL,
    BOUNDARY_REASONS,
    MAD_TO_SIGMA,
    classify_boundaries,
    corroborate,
    delimit_runs,
    neighbour_edges,
    outliers_conf,
    score_hours,
    verdict_expr,
)

START = datetime(2015, 6, 1)


def _series(
    values: Sequence[float | None], *, station: str = "西屯", start: datetime = START
) -> pl.DataFrame:
    """Consecutive hours that all sit in one baseline cell.

    The two things a fixture has to do here pull against each other. Run
    delimitation needs **consecutive hours**, because a run continues only
    across a one-hour step. The baseline cell is (station, year, month,
    hour-of-day), so in real data one cell is one clock hour across a month —
    readings a day apart, never an hour apart.

    Both are satisfied by writing the cell columns explicitly instead of
    deriving them from the timestamp: the hours run consecutively for the run
    logic while every reading belongs to one cell for the statistic. That is
    the only place these tests depart from the shape of the real frame, and it
    departs in the direction that exercises more of the code, not less.
    """
    return pl.DataFrame(
        {
            "station_name": [station] * len(values),
            "ts_local": [start + timedelta(hours=i) for i in range(len(values))],
            "value": values,
        }
    ).with_columns(
        pl.lit(2015, dtype=pl.Int32).alias("obs_year"),
        pl.lit(6, dtype=pl.Int8).alias("obs_month"),
        pl.lit(9, dtype=pl.Int8).alias("obs_hour"),
    )


_hourly = _series


def _noise(n: int) -> list[float]:
    """An ordinary background with a real spread.

    A constant background has a MAD of exactly zero, which the module correctly
    refuses to divide by — so a fixture built from one identical value tests the
    withholding path and nothing else. 18/20/22 gives a median of 20 and a MAD
    of 2, deterministically.
    """
    return [(18.0, 20.0, 22.0)[i % 3] for i in range(n)]


class TestShippedConfig:
    def test_the_config_carries_every_threshold_the_module_reads(self) -> None:
        """A threshold in code rather than conf is a threshold with no record."""
        block = outliers_conf()

        assert set(block["spike"]) == {
            "max_duration_hours",
            "neighbor_radius_km",
            "min_neighbor_corroboration",
            "neighbor_lag_hours",
        }
        assert set(block["event"]) == {"min_stations", "min_zscore"}
        assert block["baseline"]["min_samples"] > 0
        assert block["null_shift_days"]

    def test_no_circular_measurand_is_on_the_list(self) -> None:
        """A median of bearings is meaningless; 359 and 1 are two degrees apart."""
        assert not {"WIND_DIREC", "WD_HR"} & set(outliers_conf()["pollutants"])


class TestBaseline:
    def test_a_sustained_rise_does_not_move_its_own_baseline(self) -> None:
        """The whole reason this is a climatology and not a rolling window.

        A centred rolling median inside a long excursion climbs with the data
        and the z collapses. Thirty ordinary readings plus a six-long block that
        is far above them must still score the block as extreme.
        """
        frame = _series(_noise(30) + [80.0] * 6)

        marked = score_hours(frame.lazy(), min_samples=20, min_zscore=3.0)

        raised = marked.filter(pl.col("value") == 80.0)
        assert raised["excursion"].to_list() == [True] * 6

    def test_a_reading_at_the_cell_median_has_a_z_of_zero(self) -> None:
        frame = _series([10.0, 20.0, 30.0] * 10)

        marked = score_hours(frame.lazy(), min_samples=20, min_zscore=3.0)

        middle = marked.filter(pl.col("value") == 20.0)
        assert middle["robust_z"].to_list() == [0.0] * 10

    def test_the_scale_is_a_true_mad_against_one_median(self) -> None:
        """Because the cell is static, every residual is taken against the same
        median — which is what a MAD is, and what a rolling spelling of it is not.
        """
        values = [10.0] * 15 + [30.0] * 15
        marked = score_hours(_series(values).lazy(), min_samples=20, min_zscore=3.0)

        # median 10 or 30 depending on tie-breaking; |dev| is 10 either way.
        assert marked["scale"].unique().to_list() == [10.0]

    def test_a_thin_cell_withholds_the_z_rather_than_inventing_one(self) -> None:
        marked = score_hours(_series(_noise(5)).lazy(), min_samples=20, min_zscore=3.0)

        assert marked["robust_z"].null_count() == 5
        assert marked["unscored_reason"].unique().to_list() == ["thin_cell"]

    def test_a_constant_cell_has_no_scale_so_its_z_is_null_not_infinite(self) -> None:
        marked = score_hours(_series([20.0] * 30).lazy(), min_samples=20, min_zscore=3.0)

        assert marked["robust_z"].null_count() == 30
        assert marked["unscored_reason"].unique().to_list() == ["zero_scale"]

    def test_a_withheld_z_keeps_its_deviation_in_native_units(self) -> None:
        """A reading far above a cell with no spread is a finding the z cannot say."""
        marked = score_hours(_series([20.0] * 30 + [920.0]).lazy(), min_samples=20, min_zscore=3.0)

        far = marked.filter(pl.col("value") == 920.0)
        assert far["robust_z"].to_list() == [None]
        assert far["deviation"].to_list() == [900.0]

    def test_an_untested_hour_is_null_not_false(self) -> None:
        """Two distinguishable nulls, at hour granularity.

        `excursion is None` means never tested; False means tested and ordinary.
        Collapsing them would let a run end on an hour nobody looked at.
        """
        marked = score_hours(_series(_noise(5)).lazy(), min_samples=20, min_zscore=3.0)

        assert marked["excursion"].to_list() == [None] * 5

    def test_the_z_uses_the_mad_to_sigma_constant(self) -> None:
        frame = _series([*_noise(30), 50.0])
        marked = score_hours(frame.lazy(), min_samples=20, min_zscore=3.0)

        row = marked.filter(pl.col("value") == 50.0).row(0, named=True)
        expected = MAD_TO_SIGMA * row["deviation"] / row["scale"]
        assert row["robust_z"] == pytest.approx(expected)


class TestBothTails:
    def test_a_reading_far_below_its_cell_is_an_excursion_marked_low(self) -> None:
        """The agency's largest invalidation category is enriched on this tail."""
        frame = _series([*_noise(30), -40.0])

        marked = score_hours(frame.lazy(), min_samples=20, min_zscore=3.0)

        low = marked.filter(pl.col("value") == -40.0)
        assert low["excursion"].to_list() == [True]
        assert low["direction"].to_list() == ["low"]

    def test_direction_is_null_where_the_hour_was_never_tested(self) -> None:
        marked = score_hours(_series(_noise(5)).lazy(), min_samples=20, min_zscore=3.0)

        assert marked["direction"].to_list() == [None] * 5


class TestRuns:
    def _marked(self, values: Sequence[float]) -> pl.DataFrame:
        return score_hours(_hourly(values).lazy(), min_samples=5, min_zscore=3.0)

    def test_a_run_of_consecutive_hours_is_one_run(self) -> None:
        runs = delimit_runs(self._marked(_noise(30) + [80.0] * 4 + _noise(10)))

        assert runs.height == 1
        assert runs["duration_hours"].to_list() == [4]

    def test_duration_is_the_span_plus_one_hour(self) -> None:
        """The invariant that makes duration comparable to the config threshold."""
        runs = delimit_runs(self._marked(_noise(30) + [80.0] * 4 + _noise(10)))

        row = runs.row(0, named=True)
        span = (row["t_end"] - row["t_start"]).total_seconds() / 3600
        assert row["duration_hours"] == span + 1

    def test_a_gap_in_the_hours_ends_the_run(self) -> None:
        """The mechanism by which a missing hour is never bridged.

        The frame holds only usable rows, so an agency-rejected hour and a
        null-valued hour are both already absent and both stop the run here, by
        one rule rather than two.
        """
        marked = self._marked(_noise(30) + [80.0] * 4)
        with_gap = marked.filter(pl.col("ts_local") != marked["ts_local"][32])

        runs = delimit_runs(with_gap)

        assert runs["duration_hours"].to_list() == [2, 1]

    def test_a_high_hour_and_a_low_hour_do_not_join_into_one_run(self) -> None:
        marked = self._marked([*_noise(30), 90.0, -50.0])

        runs = delimit_runs(marked)

        assert sorted(runs["direction"].to_list()) == ["high", "low"]
        assert runs["duration_hours"].to_list() == [1, 1]

    def test_the_at_peak_columns_come_from_the_same_hour(self) -> None:
        """Independent maxima would describe an hour that never happened."""
        marked = self._marked([*_noise(30), 80.0, 200.0])

        row = delimit_runs(marked).row(0, named=True)

        peak_hour = marked.filter(pl.col("value") == row["peak_value"]).row(0, named=True)
        assert row["peak_z"] == peak_hour["robust_z"]
        assert row["baseline_at_peak"] == peak_hour["baseline"]

    def test_an_empty_frame_yields_an_empty_run_table_with_the_schema(self) -> None:
        runs = delimit_runs(pl.DataFrame())

        assert runs.height == 0
        assert "duration_hours" in runs.columns


class TestBoundaries:
    def _parts(self, values: Sequence[float]) -> tuple[pl.DataFrame, pl.DataFrame]:
        present = _hourly(values).with_columns(
            pl.lit(True).alias("is_usable"), pl.lit(False).alias("is_rejected")
        )
        marked = score_hours(_hourly(values).lazy(), min_samples=5, min_zscore=3.0)
        return marked, present

    def test_a_run_that_ends_below_the_threshold_is_not_censored(self) -> None:
        marked, present = self._parts(_noise(30) + [80.0] * 2 + _noise(5))

        runs = classify_boundaries(delimit_runs(marked), marked, present)

        row = runs.row(0, named=True)
        assert (row["left_boundary"], row["right_boundary"]) == (
            "below_threshold",
            "below_threshold",
        )
        assert row["censored"] is False

    def test_a_run_ending_at_a_missing_hour_is_censored_and_says_why(self) -> None:
        """A null must never end a run silently — the reason ships as a column."""
        marked, present = self._parts(_noise(30) + [80.0] * 2 + _noise(5))
        cut = marked["ts_local"][32]
        marked = marked.filter(pl.col("ts_local") != cut)
        present = present.filter(pl.col("ts_local") != cut)

        runs = classify_boundaries(delimit_runs(marked), marked, present)

        row = runs.row(0, named=True)
        assert row["right_boundary"] == "absent"
        assert row["censored"] is True

    def test_an_hour_present_with_no_number_is_not_reported_as_absent(self) -> None:
        marked, present = self._parts(_noise(30) + [80.0] * 2 + _noise(5))
        cut = marked["ts_local"][32]
        marked = marked.filter(pl.col("ts_local") != cut)
        present = present.with_columns(
            pl.when(pl.col("ts_local") == cut).then(False).otherwise(True).alias("is_usable")
        )

        runs = classify_boundaries(delimit_runs(marked), marked, present)

        assert runs.row(0, named=True)["right_boundary"] == "value_null"

    def test_an_hour_the_agency_rejected_is_named_as_such(self) -> None:
        """The nearest thing to an independent check the module has.

        While this and a plain null shared one reason, the question "did the
        agency throw out the hour next door?" could not be asked at all.
        """
        marked, present = self._parts(_noise(30) + [80.0] * 2 + _noise(5))
        cut = marked["ts_local"][32]
        marked = marked.filter(pl.col("ts_local") != cut)
        present = present.with_columns(
            pl.when(pl.col("ts_local") == cut).then(False).otherwise(True).alias("is_usable"),
            pl.when(pl.col("ts_local") == cut).then(True).otherwise(False).alias("is_rejected"),
        )

        runs = classify_boundaries(delimit_runs(marked), marked, present)

        assert runs.row(0, named=True)["right_boundary"] == "agency_rejected"

    def test_a_run_cut_by_the_requested_span_says_so_rather_than_blaming_the_record(
        self,
    ) -> None:
        """One is a fact about the network; the other is a property of the command line."""
        marked, present = self._parts([*_noise(30), 80.0, 80.0])
        last = START + timedelta(hours=marked.height - 1)

        runs = classify_boundaries(
            delimit_runs(marked), marked, present, span=(START, last - timedelta(hours=1))
        )

        row = runs.row(0, named=True)
        assert row["right_boundary"] == "outside_requested_span"
        assert row["censored"] is True

    def test_a_run_at_the_end_of_the_record_is_a_series_edge(self) -> None:
        marked, present = self._parts(_noise(30) + [80.0] * 2)

        runs = classify_boundaries(delimit_runs(marked), marked, present)

        assert runs.row(0, named=True)["right_boundary"] == "series_edge"
        assert runs.row(0, named=True)["censored"] is True


class TestBoundaryVocabulary:
    def test_every_reason_the_classifier_emits_is_declared(self) -> None:
        """The comment above BOUNDARY_REASONS said five, then six, while the
        tuple held six and then eight. A count in prose rots; this does not."""
        emitted = {
            reason
            for line in (
                pathlib.Path("src/twair/qc/outliers.py").read_text(encoding="utf-8").splitlines()
            )
            if (stripped := line.strip()).startswith(".then(pl.lit(")
            and (reason := stripped[len(".then(pl.lit(") + 1 : -3]) in set(BOUNDARY_REASONS)
        }

        assert emitted, "no boundary reason literals found — has the chain moved?"
        assert emitted <= set(BOUNDARY_REASONS)

    def test_below_threshold_is_first_because_only_it_means_the_run_ended(self) -> None:
        assert BOUNDARY_REASONS[0] == "below_threshold"


class TestNeighbourEdges:
    GEO = pl.DataFrame(
        {
            "station_name": ["西屯", "忠明", "沙鹿", "馬公"],
            "lat": [24.162, 24.152, 24.225, 23.569],
            "lon": [120.616, 120.641, 120.569, 119.566],
        }
    )

    def test_stations_within_the_radius_become_edges(self) -> None:
        edges, _ = neighbour_edges(radius_km=20.0, geography=self.GEO)

        assert set(edges.filter(pl.col("station_name") == "西屯")["neighbor_name"]) == {
            "忠明",
            "沙鹿",
        }

    def test_a_station_out_of_range_of_everything_has_no_edge_but_is_ledgered(self) -> None:
        """Zero neighbours inside the radius is a measurement; it must be a 0."""
        _, ledger = neighbour_edges(radius_km=20.0, geography=self.GEO)

        offshore = ledger.filter(pl.col("station_name") == "馬公").row(0, named=True)
        assert offshore["n_neighbours"] == 0
        assert offshore["has_coordinates"] is True

    def test_no_station_is_its_own_neighbour(self) -> None:
        edges, _ = neighbour_edges(radius_km=500.0, geography=self.GEO)

        assert edges.filter(pl.col("station_name") == pl.col("neighbor_name")).height == 0

    def test_an_empty_register_is_refused_rather_than_returning_uncheckable(self) -> None:
        """`load_station_geo` degrades to an empty frame by design.

        Without this guard a missing conf/station_geo.yaml produces a complete,
        well-formed and entirely vacuous result in which nothing distinguishes
        "the register is missing" from "the network is unplaceable".
        """
        empty = pl.DataFrame(schema={"station_name": pl.Utf8, "lat": pl.Float64, "lon": pl.Float64})

        with pytest.raises(ValueError, match="station_geo"):
            neighbour_edges(radius_km=50.0, geography=empty)


class TestCorroborationCounts:
    def test_an_unplaceable_station_reports_null_neighbours_not_zero(self) -> None:
        """ "We could not look" must never render as "we looked and found none"."""
        marked = score_hours(
            _hourly(_noise(30) + [80.0] * 2, station="無座標站").lazy(),
            min_samples=5,
            min_zscore=3.0,
        )
        runs = delimit_runs(marked)
        edges = pl.DataFrame(
            schema={"station_name": pl.Utf8, "neighbor_name": pl.Utf8, "km": pl.Float64}
        )
        ledger = pl.DataFrame(
            {
                "station_name": ["西屯"],
                "n_neighbours": [0],
                "km_nearest_neighbour": [None],
                "has_coordinates": [True],
            },
            schema={
                "station_name": pl.Utf8,
                "n_neighbours": pl.UInt32,
                "km_nearest_neighbour": pl.Float64,
                "has_coordinates": pl.Boolean,
            },
        )

        out = corroborate(runs, marked, edges, ledger, lag_hours=1, null_shift_days=(14,))

        row = out.row(0, named=True)
        assert row["has_coordinates"] is False
        assert row["n_neighbors_placed"] is None
        assert row["n_neighbors_scoreable"] is None

    def test_a_placed_station_with_an_empty_radius_reports_zero(self) -> None:
        marked = score_hours(_hourly(_noise(30) + [80.0] * 2).lazy(), min_samples=5, min_zscore=3.0)
        runs = delimit_runs(marked)
        edges = pl.DataFrame(
            schema={"station_name": pl.Utf8, "neighbor_name": pl.Utf8, "km": pl.Float64}
        )
        ledger = pl.DataFrame(
            {
                "station_name": ["西屯"],
                "n_neighbours": [0],
                "km_nearest_neighbour": [None],
                "has_coordinates": [True],
            },
            schema={
                "station_name": pl.Utf8,
                "n_neighbours": pl.UInt32,
                "km_nearest_neighbour": pl.Float64,
                "has_coordinates": pl.Boolean,
            },
        )

        out = corroborate(runs, marked, edges, ledger, lag_hours=1, null_shift_days=(14,))

        row = out.row(0, named=True)
        assert row["has_coordinates"] is True
        assert row["n_neighbors_placed"] == 0


class TestNeighboursMustMoveTheSameWay:
    def test_a_neighbour_dipping_low_does_not_corroborate_a_high_run(self) -> None:
        """多站同步**上升**. A neighbour falling is not a neighbour rising.

        The bug this pins was invisible: the neighbour frame carried a
        `direction` column that nothing read, so any excursion at a neighbour
        counted, in either direction.
        """
        rising = _hourly([*_noise(30), 90.0, 90.0], station="西屯")
        falling = _hourly([*_noise(30), -50.0, -50.0], station="忠明")
        marked = score_hours(pl.concat([rising, falling]).lazy(), min_samples=5, min_zscore=3.0)
        edges = pl.DataFrame({"station_name": ["西屯"], "neighbor_name": ["忠明"], "km": [3.0]})
        ledger = pl.DataFrame(
            {
                "station_name": ["西屯", "忠明"],
                "n_neighbours": [1, 1],
                "km_nearest_neighbour": [3.0, 3.0],
                "has_coordinates": [True, True],
            }
        )
        runs = delimit_runs(marked).filter(pl.col("station_name") == "西屯")

        out = corroborate(runs, marked, edges, ledger, lag_hours=1, null_shift_days=())

        row = out.row(0, named=True)
        assert row["direction"] == "high"
        assert row["n_neighbors_scoreable"] == 1, "the neighbour was testable"
        assert row["n_neighbors_elevated"] == 0, "but it moved the other way"

    def test_a_neighbour_rising_with_it_does_corroborate(self) -> None:
        rising = _hourly([*_noise(30), 90.0, 90.0], station="西屯")
        also = _hourly([*_noise(30), 90.0, 90.0], station="忠明")
        marked = score_hours(pl.concat([rising, also]).lazy(), min_samples=5, min_zscore=3.0)
        edges = pl.DataFrame({"station_name": ["西屯"], "neighbor_name": ["忠明"], "km": [3.0]})
        ledger = pl.DataFrame(
            {
                "station_name": ["西屯", "忠明"],
                "n_neighbours": [1, 1],
                "km_nearest_neighbour": [3.0, 3.0],
                "has_coordinates": [True, True],
            }
        )
        runs = delimit_runs(marked).filter(pl.col("station_name") == "西屯")

        out = corroborate(runs, marked, edges, ledger, lag_hours=1, null_shift_days=())

        assert out.row(0, named=True)["n_neighbors_elevated"] == 1


class TestTheNullHasItsOwnDenominator:
    """The null is the module's defence against its own headline claim.

    A shift that lands outside the scanned span has no neighbour data to look
    at. Counting that as "the shifted calendar saw nothing" hands the largest
    excess to exactly the runs with no comparison behind them — measured on a
    single-year span before this was fixed, runs in the first 42 days had a
    shifted denominator of 0.036 neighbours against an observed 15.9.
    """

    def _parts(self) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
        marked = score_hours(
            _hourly([*_noise(30), 80.0, 80.0]).lazy(), min_samples=5, min_zscore=3.0
        )
        edges = pl.DataFrame(
            {"station_name": ["西屯"], "neighbor_name": ["忠明"], "km": [3.0]},
        )
        ledger = pl.DataFrame(
            {
                "station_name": ["西屯", "忠明"],
                "n_neighbours": [1, 1],
                "km_nearest_neighbour": [3.0, 3.0],
                "has_coordinates": [True, True],
            }
        )
        return marked, edges, ledger

    def test_a_shift_with_no_neighbour_data_is_not_counted_as_a_quiet_calendar(self) -> None:
        marked, edges, ledger = self._parts()
        runs = delimit_runs(marked)

        # The fixture holds one station only, so every shifted window falls on
        # nothing at all — which must read as "no null was computed".
        out = corroborate(runs, marked, edges, ledger, lag_hours=1, null_shift_days=(14, 28))

        row = out.row(0, named=True)
        assert row["n_null_shifts_measurable"] == 0
        assert row["n_neighbors_elevated_null"] is None
        assert row["corroboration_excess"] is None

    def test_the_null_denominator_ships_beside_the_null_count(self) -> None:
        """Two counts taken over different denominators are not comparable."""
        marked, edges, ledger = self._parts()

        out = corroborate(
            delimit_runs(marked), marked, edges, ledger, lag_hours=1, null_shift_days=(14,)
        )

        assert "n_neighbors_scoreable_null" in out.columns
        assert "n_neighbors_scoreable" in out.columns

    def test_the_shipped_shifts_run_in_both_directions(self) -> None:
        """A backwards-only null is undefined at the start of the span."""
        shifts = outliers_conf()["null_shift_days"]

        assert any(days < 0 for days in shifts)
        assert any(days > 0 for days in shifts)


class TestVerdictOrder:
    """The chain's order is the argument; each test pins one link of it."""

    def _run(self, **overrides: object) -> pl.DataFrame:
        row: dict[str, object] = {
            "direction": "high",
            "duration_hours": 1,
            "censored": False,
            "has_coordinates": True,
            "n_neighbors_scoreable": 5,
            "n_neighbors_elevated": 0,
        }
        row.update(overrides)
        frame = pl.DataFrame([row]).with_columns(
            pl.col("duration_hours").cast(pl.UInt32),
            pl.col("n_neighbors_scoreable").cast(pl.UInt32),
            pl.col("n_neighbors_elevated").cast(pl.UInt32),
        )
        verdict, reason = verdict_expr(
            min_stations=3, max_duration_hours=2, min_neighbor_corroboration=1
        )
        return frame.with_columns(verdict, reason)

    def test_no_coordinates_outranks_everything(self) -> None:
        out = self._run(has_coordinates=False, n_neighbors_elevated=9)

        assert out["verdict"][0] == "uncheckable"
        assert out["verdict_reason"][0] == "no_coordinates"

    def test_no_scoreable_neighbour_is_absence_of_evidence(self) -> None:
        """Not evidence of a lone spike — the distinction the gate exists for."""
        out = self._run(n_neighbors_scoreable=0)

        assert out["verdict"][0] == "uncheckable"
        assert out["verdict_reason"][0] == "too_few_scoreable_neighbours"

    def test_a_high_run_that_could_not_have_reached_an_episode_is_not_judged(self) -> None:
        """One scoreable neighbour cannot make three stations, so there was no
        alternative for the verdict to have chosen."""
        out = self._run(n_neighbors_scoreable=1, n_neighbors_elevated=0)

        assert out["verdict"][0] == "uncheckable"
        assert out["verdict_reason"][0] == "too_few_scoreable_neighbours"

    def test_a_low_run_keeps_the_bar_at_zero(self) -> None:
        """It was never eligible for the episode verdict, so nothing is unreachable."""
        out = self._run(direction="low", n_neighbors_scoreable=1, duration_hours=1)

        assert out["verdict"][0] == "uncorroborated_short_rise"

    def test_a_one_hour_rise_at_four_stations_is_still_an_episode(self) -> None:
        """Extent, not brevity, is the spec's discriminator."""
        out = self._run(duration_hours=1, n_neighbors_elevated=3)

        assert out["verdict"][0] == "regional_episode"

    def test_several_stations_reading_low_together_is_not_an_episode(self) -> None:
        """Below the local level is a statement about readings, not about the air."""
        out = self._run(direction="low", n_neighbors_elevated=9)

        assert out["verdict"][0] != "regional_episode"

    def test_a_censored_run_is_never_called_short(self) -> None:
        """ "<2h therefore suspicious" is exactly the claim censoring destroys."""
        out = self._run(duration_hours=1, censored=True)

        assert out["verdict"][0] == "uncheckable"
        assert out["verdict_reason"][0] == "duration_censored"

    def test_corroboration_still_counts_when_the_run_is_censored(self) -> None:
        """It holds however this station's own run was cut, so it is tested first."""
        out = self._run(censored=True, n_neighbors_elevated=5)

        assert out["verdict"][0] == "regional_episode"

    def test_short_and_alone_is_the_specs_suspect_case(self) -> None:
        out = self._run(duration_hours=2, n_neighbors_elevated=0)

        assert out["verdict"][0] == "uncorroborated_short_rise"

    def test_a_long_lone_excursion_is_named_but_not_explained(self) -> None:
        """The residual the spec does not describe keeps its own label."""
        out = self._run(duration_hours=9, n_neighbors_elevated=0)

        assert out["verdict"][0] == "uncorroborated_sustained_rise"


class TestTheLedgerSurvivesANewColumn:
    """The end-to-end shape nothing was testing.

    Adding `km_nearest_neighbour` to the ledger left the frame that carries the
    unplaceable stations one column short, and `run_outliers` raised ShapeError
    on the shipped configuration while all 655 tests passed — because every one
    of them called a helper and none of them called the function. The frame is
    now derived from the ledger's own schema, and this asserts that property
    rather than the column list, so the next column added is covered too.
    """

    GEO = pl.DataFrame(
        {
            "station_name": ["西屯", "忠明"],
            "lat": [24.162, 24.152],
            "lon": [120.616, 120.641],
        }
    )

    def test_an_unplaceable_station_can_be_appended_to_whatever_the_ledger_carries(
        self,
    ) -> None:
        _, ledger = neighbour_edges(radius_km=20.0, geography=self.GEO)
        measured = [c for c in ledger.columns if c not in ("station_name", "has_coordinates")]

        extra = (
            pl.DataFrame({"station_name": ["萬里"]}, schema={"station_name": pl.Utf8})
            .with_columns(
                *[pl.lit(None, dtype=ledger.schema[name]).alias(name) for name in measured],
                pl.lit(False).alias("has_coordinates"),
            )
            .select(ledger.columns)
        )

        combined = pl.concat([ledger, extra], how="vertical")

        assert combined.height == 3
        assert combined.schema == ledger.schema
        unplaceable = combined.filter(pl.col("station_name") == "萬里").row(0, named=True)
        for name in measured:
            assert unplaceable[name] is None, f"{name} must be null, not a measured zero"

    def test_the_ledger_carries_the_distance_that_explains_the_verdict_rate(self) -> None:
        """r = 0.76 between this and a station's uncorroborated share."""
        _, ledger = neighbour_edges(radius_km=20.0, geography=self.GEO)

        assert "km_nearest_neighbour" in ledger.columns
        assert ledger["km_nearest_neighbour"].min() > 0


class TestBaselineCell:
    def test_the_cell_carries_the_year_so_the_trend_is_not_an_episode(self) -> None:
        """Without obs_year the 44-year decline would flag the 1990s as one rise."""
        assert "obs_year" in BASELINE_CELL
        assert "obs_month" in BASELINE_CELL
        assert "obs_hour" in BASELINE_CELL
