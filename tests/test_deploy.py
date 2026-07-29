"""Tests for the Space bundle.

A deployment bundle fails differently from analysis code. It does not crash —
it ships, loads, and returns plausible numbers computed from the wrong columns.
The feature order is the whole risk: LightGBM takes a bare array and cannot
tell that column 12 is humidity now rather than wind speed.

So the checks here are about the contract between what was trained and what
gets loaded, plus the failure that already happened once — a declared station
producing no rows and the bundle shipping five where the README said six.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import polars as pl
import pytest

from twair.models.deploy import DEMO_STATIONS, BundleReport, build_climatology


def _hours(n: int, station: str = "站", start: datetime = datetime(2020, 1, 1)) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "station_name": [station] * n,
            "ts_local": [start + timedelta(hours=i) for i in range(n)],
            "PM2.5": [float(i % 24) for i in range(n)],
        }
    )


class TestClimatology:
    def test_it_averages_by_station_month_and_hour(self) -> None:
        """The baseline that ignores today has to be keyed on all three."""
        table = build_climatology(_hours(24 * 40))

        assert set(table.columns) == {"station_name", "month", "hour", "climatology"}
        assert table["hour"].n_unique() == 24

    def test_the_same_hour_of_the_day_gets_the_same_value(self) -> None:
        """The fixture repeats a 24-hour cycle, so each hour has one answer."""
        table = build_climatology(_hours(24 * 40)).filter(pl.col("month") == 1)

        for row in table.iter_rows(named=True):
            assert row["climatology"] == pytest.approx(float(row["hour"]))

    def test_stations_do_not_share_a_climatology(self) -> None:
        a = _hours(24 * 10, station="甲")
        b = _hours(24 * 10, station="乙").with_columns(pl.col("PM2.5") + 100)

        table = build_climatology(pl.concat([a, b]))

        means = {
            name: part["climatology"].mean()
            for (name,), part in table.group_by("station_name", maintain_order=True)
        }
        assert means["乙"] - means["甲"] == pytest.approx(100.0)

    def test_it_is_horizon_free(self) -> None:
        """Built from the observed column, so one table serves every horizon.

        The climatological value for 3pm in March is the same number whether it
        is being predicted 1 hour ahead or 48. Keying it by horizon would mean
        four tables that must agree.
        """
        table = build_climatology(_hours(24 * 30))

        assert "horizon" not in table.columns


class TestBundleContract:
    def test_the_demo_stations_are_the_ones_the_copy_describes(self) -> None:
        """The Space README names each station and why it is there.

        陽明 was dropped because it has no anemometer, and the bundle silently
        shipped five stations while the copy still described six. Whatever this
        tuple says, the prose has to match it.
        """
        assert len(DEMO_STATIONS) == len(set(DEMO_STATIONS))
        assert "陽明" not in DEMO_STATIONS, "no WS_HR/WD_HR — every row dies on drop_nulls"

    def test_the_deployed_model_is_the_backtested_one(self) -> None:
        """The Space's claim is that it runs the model M9 measured.

        Asserting that two dicts hold equal values would only prove they agree
        today. There is one dict; this checks deploy still reaches for it rather
        than growing a copy.
        """
        from twair.models import deploy, forecast

        assert deploy.MODEL_PARAMS is forecast.MODEL_PARAMS

    def test_the_report_counts_rows_the_model_saw(self) -> None:
        """Not the hourly index, which barely moves when the data does.

        The first version printed the span of the complete hourly index — 507,480
        either way — so swapping a station changed nothing visible.
        """
        report = BundleReport(
            horizons=(1, 24),
            stations=DEMO_STATIONS,
            train_rows={1: 400_000, 24: 399_000},
            demo_rows=24_754,
            features=38,
            bytes=14_000_000,
        )

        assert "h1=400,000" in report.summary()
        assert "h24=399,000" in report.summary()

    def test_the_summary_survives_rich_markup(self) -> None:
        """It is printed through rich, which eats anything inside brackets.

        The first version wrapped the counts in `[...]` and rich dropped them,
        so the line read "fitted on  rows" and looked like an empty dict.
        """
        report = BundleReport(
            horizons=(1,),
            stations=DEMO_STATIONS,
            train_rows={1: 400_000},
            demo_rows=1,
            features=38,
            bytes=1,
        )

        assert "[" not in report.summary()
