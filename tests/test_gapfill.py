"""Tests for gap filling.

Replacing missing values with data from a nearby station is the method under test
and said so in one clause. The point of this module is not to do that better;
it is to make the same choice measurable. So what gets pinned here is mostly
what the strategies must *not* do: overwrite a real reading, bridge a gap
longer than configured, borrow from a station that is far away or uncorrelated,
or fill without leaving a mark.

Fixtures are shaped like the wide station-hour frame the modelling code uses,
because a fixture that does not match the real frame tests a shape nobody has.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import polars as pl
import pytest

from twair.qc.gapfill import STRATEGIES, fill, imputed_column

START = datetime(2020, 1, 1)


def _hours(n: int, start: datetime = START) -> list[datetime]:
    return [start + timedelta(hours=i) for i in range(n)]


def _frame(
    station: str = "測站甲",
    values: list[float | None] | None = None,
    *,
    start: datetime = START,
    column: str = "PM2.5",
) -> pl.DataFrame:
    values = values if values is not None else [10.0] * 24
    return pl.DataFrame(
        {
            "station_name": [station] * len(values),
            "ts_local": _hours(len(values), start),
            column: values,
        },
        schema_overrides={column: pl.Float64},
    )


class TestTheDefaultInventsNothing:
    def test_the_shipped_strategy_leaves_every_null_where_it_was(self) -> None:
        """`none` is the default in conf/qc.yaml and must stay a real no-op."""
        frame = _frame(values=[10.0, None, 12.0, None])

        out, report = fill(frame, columns=["PM2.5"], strategy="none")

        assert out["PM2.5"].to_list() == [10.0, None, 12.0, None]
        assert report.total_filled == 0
        assert not out[imputed_column("PM2.5")].any()

    def test_a_rate_over_nothing_missing_is_undefined_not_zero(self) -> None:
        """Zero would read as "did nothing"; the truth is "nothing to do"."""
        _, report = fill(_frame(values=[10.0] * 24), columns=["PM2.5"], strategy="none")

        assert report.total_missing == 0
        assert report.fill_rate is None


class TestNothingOverwritesARealReading:
    @pytest.mark.parametrize("strategy", STRATEGIES)
    def test_filling_never_moves_a_value_that_was_already_there(self, strategy: str) -> None:
        """The one invariant every strategy shares, and the reason `_mark`
        derives its flag by comparison rather than trusting each implementation
        to report honestly."""
        values: list[float | None] = [float(i) for i in range(48)]
        values[10] = None
        values[11] = None
        frame = _frame(values=values)

        out, _ = fill(
            frame,
            columns=["PM2.5"],
            strategy=strategy,  # type: ignore[arg-type]
            geography=pl.DataFrame({"station_name": ["測站甲"], "lat": [25.0], "lon": [121.0]}),
        )

        before = frame["PM2.5"].to_list()
        after = out.sort("ts_local")["PM2.5"].to_list()
        for i, (old, new) in enumerate(zip(before, after, strict=True)):
            if old is not None:
                assert new == pytest.approx(old), f"{strategy} moved hour {i}"

    @pytest.mark.parametrize("strategy", STRATEGIES)
    def test_every_invented_cell_is_marked(self, strategy: str) -> None:
        values: list[float | None] = [float(i) for i in range(48)]
        values[20] = None
        frame = _frame(values=values)

        out, report = fill(
            frame,
            columns=["PM2.5"],
            strategy=strategy,  # type: ignore[arg-type]
            geography=pl.DataFrame({"station_name": ["測站甲"], "lat": [25.0], "lon": [121.0]}),
        )
        out = out.sort("ts_local")

        marked = out[imputed_column("PM2.5")].sum()
        assert marked == report.filled["PM2.5"]
        for row in out.iter_rows(named=True):
            if row[imputed_column("PM2.5")]:
                assert row["PM2.5"] is not None, "a cell marked imputed must carry a value"


class TestInterpolationRespectsTheGapBound:
    def test_a_short_gap_is_bridged(self) -> None:
        frame = _frame(values=[10.0, None, None, 16.0])

        out, report = fill(frame, columns=["PM2.5"], strategy="interpolate")

        assert out.sort("ts_local")["PM2.5"].to_list() == [10.0, 12.0, 14.0, 16.0]
        assert report.filled["PM2.5"] == 2

    def test_a_gap_longer_than_the_bound_is_left_alone(self) -> None:
        """Three hours between two readings is a defensible straight line.
        Six is an invention, and the configured bound is what says so."""
        values: list[float | None] = [10.0, None, None, None, None, None, None, 20.0]
        frame = _frame(values=values)

        out, report = fill(frame, columns=["PM2.5"], strategy="interpolate")

        assert report.filled["PM2.5"] == 0
        assert out.sort("ts_local")["PM2.5"].to_list() == values

    def test_the_bound_comes_from_config_not_from_a_constant(self) -> None:
        frame = _frame(values=[10.0, None, None, None, None, 20.0])
        conf = {"gapfill": {"strategies": {"interpolate": {"max_gap_hours": 4}}}}

        out, report = fill(frame, columns=["PM2.5"], strategy="interpolate", config=conf)

        assert report.filled["PM2.5"] == 4
        assert out.sort("ts_local")["PM2.5"].to_list() == [10.0, 12.0, 14.0, 16.0, 18.0, 20.0]

    def test_an_absent_hour_counts_toward_the_gap_the_same_as_a_null_one(self) -> None:
        """The trap the lag features already document, one module over.

        A three-day outage usually arrives as *missing rows*, not as null
        values. Measuring gap length on the rows present would call these two
        readings adjacent and draw a line across the outage.
        """
        early = _frame(values=[10.0], start=START)
        late = _frame(values=[20.0], start=START + timedelta(hours=10))
        frame = pl.concat([early, late])

        out, report = fill(frame, columns=["PM2.5"], strategy="interpolate")

        assert report.filled["PM2.5"] == 0
        assert out.height == 2, "row count is preserved; the dense index is internal"

    def test_leading_and_trailing_nulls_are_not_extrapolated(self) -> None:
        """Interpolation is between known points. Beyond them it would be a
        forecast wearing a fill's clothing."""
        frame = _frame(values=[None, 10.0, 11.0, None])

        out, report = fill(frame, columns=["PM2.5"], strategy="interpolate")

        assert report.filled["PM2.5"] == 0
        assert out.sort("ts_local")["PM2.5"].to_list() == [None, 10.0, 11.0, None]


class TestTheNeighbourStrategyIsTheOriginalMethod:
    @staticmethod
    def _two_stations(
        target: list[float | None], donor: list[float | None]
    ) -> tuple[pl.DataFrame, pl.DataFrame]:
        frame = pl.concat([_frame("目標站", target), _frame("捐助站", donor)], how="vertical")
        geo = pl.DataFrame(
            {
                "station_name": ["目標站", "捐助站"],
                "lat": [25.0, 25.05],  # ~5.5 km apart
                "lon": [121.0, 121.0],
            }
        )
        return frame, geo

    def test_a_close_correlated_station_donates(self) -> None:
        donor: list[float | None] = [float(i % 20 + 5) for i in range(48)]
        target: list[float | None] = [v * 2.0 for v in donor if v is not None]
        target[30] = None
        frame, geo = self._two_stations(target, donor)

        out, report = fill(frame, columns=["PM2.5"], strategy="neighbor", geography=geo)

        assert report.filled["PM2.5"] == 1
        filled_value = out.filter(
            (pl.col("station_name") == "目標站") & pl.col(imputed_column("PM2.5"))
        )["PM2.5"].item()
        assert filled_value == pytest.approx(donor[30] * 2.0, abs=1e-6)

    def test_a_station_beyond_the_distance_limit_does_not_donate(self) -> None:
        donor: list[float | None] = [float(i % 20 + 5) for i in range(48)]
        target: list[float | None] = [v * 2.0 for v in donor if v is not None]
        target[30] = None
        frame, _ = self._two_stations(target, donor)
        far = pl.DataFrame(
            {
                "station_name": ["目標站", "捐助站"],
                "lat": [25.0, 26.0],  # ~111 km
                "lon": [121.0, 121.0],
            }
        )

        _, report = fill(frame, columns=["PM2.5"], strategy="neighbor", geography=far)

        assert report.filled["PM2.5"] == 0

    def test_a_close_but_uncorrelated_station_does_not_donate(self) -> None:
        """Proximity is not similarity. A roadside site next to a park site is
        close and measures something else."""
        donor: list[float | None] = [float((i * 7) % 23) for i in range(48)]
        target: list[float | None] = [float((i * 13) % 19) for i in range(48)]
        target[30] = None
        frame, geo = self._two_stations(target, donor)

        _, report = fill(frame, columns=["PM2.5"], strategy="neighbor", geography=geo)

        assert report.filled["PM2.5"] == 0

    def test_a_station_with_no_coordinates_is_reported_rather_than_passed_over(self) -> None:
        """Six stations in this archive have decades of observations and no
        entry in the MOENV register. Silently not serving them would make the
        fill rate look like a property of the method."""
        values: list[float | None] = [float(i) for i in range(48)]
        values[10] = None
        frame = _frame("無座標站", values)
        geo = pl.DataFrame(
            {"station_name": ["別站"], "lat": [25.0], "lon": [121.0]},
        )

        _, report = fill(frame, columns=["PM2.5"], strategy="neighbor", geography=geo)

        assert any("no coordinates" in note for note in report.notes)
        assert any("無座標站" in note for note in report.notes)


class TestTheReportCanBeChecked:
    def test_the_parameters_travel_with_the_counts(self) -> None:
        """A fill rate without its configuration cannot be verified."""
        frame = _frame(values=[10.0, None, 12.0])

        _, report = fill(frame, columns=["PM2.5"], strategy="interpolate")

        assert report.params.get("max_gap_hours") == 3

    def test_the_part_the_strategy_could_not_reach_is_kept(self) -> None:
        values: list[float | None] = [10.0, None, 12.0, None, None, None, None, None, 20.0]
        frame = _frame(values=values)

        _, report = fill(frame, columns=["PM2.5"], strategy="interpolate")

        assert report.missing_before["PM2.5"] == 6
        assert report.filled["PM2.5"] == 1
        assert report.total_missing - report.total_filled == 5

    def test_an_unknown_strategy_is_refused(self) -> None:
        with pytest.raises(ValueError, match="unknown strategy"):
            fill(_frame(), columns=["PM2.5"], strategy="magic")  # type: ignore[arg-type]

    def test_a_column_that_is_not_there_is_refused(self) -> None:
        with pytest.raises(ValueError, match="columns not in frame"):
            fill(_frame(), columns=["NO2"], strategy="none")


class TestTheStationBoundaryIsNotAGap:
    """The run id is built with an inner `shift(1)` that is not partitioned.

    That looks wrong: the first row of station B is compared against the last
    row of station A, so a station boundary can register as a null-ness flip
    that never happened. It is safe — `cum_sum` is partitioned and the length is
    taken over `[station, run_id]` — but "it is safe" was an argument until
    these ran, and the same shape one module over in `features/lags.py` was
    NOT safe and had been wrong for eleven years.
    """

    @staticmethod
    def _two(a: list[float | None], b: list[float | None]) -> pl.DataFrame:
        return pl.concat([_frame(station="甲", values=a), _frame(station="乙", values=b)])

    def test_two_short_gaps_either_side_of_a_boundary_stay_two_short_gaps(self) -> None:
        """Both are bridgeable. Merged into one run of four they would not be."""
        frame = self._two([10.0, None, None, 16.0], [20.0, None, None, 26.0])

        out, report = fill(frame, columns=["PM2.5"], strategy="interpolate")

        assert report.filled["PM2.5"] == 4
        assert out.sort("station_name", "ts_local")["PM2.5"].to_list() == [
            20.0,
            22.0,
            24.0,
            26.0,  # 乙
            10.0,
            12.0,
            14.0,
            16.0,  # 甲
        ]

    def test_a_gap_at_the_end_of_one_station_is_not_joined_to_the_next(self) -> None:
        """甲 trails off into nulls and 乙 opens with them.

        Counted as one run that is six hours long, both would be refused. They
        are two runs of two and three, and only the three-hour one is at the
        bound.
        """
        frame = self._two([10.0, None, None], [None, None, None, 26.0])

        out, report = fill(frame, columns=["PM2.5"], strategy="interpolate")

        # 甲's trailing nulls have no right-hand anchor, so nothing to
        # interpolate between; 乙's leading nulls have no left-hand one either.
        # What matters is that neither was refused for being part of a six-hour
        # run — the reason they stay null is that they are at the edges.
        assert report.filled["PM2.5"] == 0
        assert out.height == 7

    def test_a_long_gap_in_one_station_does_not_veto_a_short_one_in_another(self) -> None:
        """The failure a merged run id would actually produce."""
        frame = self._two(
            [10.0, None, None, None, None, None, None, 20.0],
            [30.0, None, None, 36.0, 36.0, 36.0, 36.0, 36.0],
        )

        out, report = fill(frame, columns=["PM2.5"], strategy="interpolate")

        assert report.filled["PM2.5"] == 2, "乙's two-hour gap was refused"
        assert out.filter(pl.col("station_name") == "甲")["PM2.5"].null_count() == 6
