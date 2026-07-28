"""Tests for the chapter payloads.

These carry editorial choices — which threshold, which baseline, which
stations — and the tests are written as claims about those choices, because a
wrong one produces a chart that is confidently misleading rather than obviously
broken.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from twair.viz import story  # type: ignore[import-untyped]


def _daily_frame(rows: list[dict[str, Any]]) -> pl.DataFrame:
    return pl.DataFrame(
        rows,
        schema={
            "station_name": pl.Utf8,
            "pollutant": pl.Utf8,
            "date": pl.Date,
            "mean": pl.Float64,
        },
    )


def _year_of_days(
    station: str, year: int, value: float, *, days: int = 365
) -> list[dict[str, Any]]:
    start = date(year, 1, 1)
    return [
        {
            "station_name": station,
            "pollutant": "PM2.5",
            "date": start + timedelta(days=i),
            "mean": value,
        }
        for i in range(days)
    ]


@pytest.fixture
def daily(monkeypatch: pytest.MonkeyPatch) -> Callable[[pl.DataFrame], None]:
    """Redirect ``_daily`` to an in-memory frame."""

    def _install(frame: pl.DataFrame) -> None:
        monkeypatch.setattr(
            story,
            "_daily",
            lambda pollutant, root=None: frame.lazy().filter(pl.col("pollutant") == pollutant),
        )

    return _install


class TestComparability:
    def test_a_year_with_thin_coverage_is_marked_not_dropped(
        self, daily: Callable[[pl.DataFrame], None]
    ) -> None:
        """The sparse year still exists; it just does not get to be compared."""
        daily(_daily_frame(_year_of_days("三重", 2020, 20.0, days=100)))

        annual = story.annual_by_station("PM2.5")

        assert annual.height == 1
        assert annual["comparable"][0] is False
        assert annual["days_with_data"][0] == 100

    def test_coverage_uses_the_real_length_of_a_leap_year(
        self, daily: Callable[[pl.DataFrame], None]
    ) -> None:
        daily(_daily_frame(_year_of_days("三重", 2020, 20.0, days=366)))

        annual = story.annual_by_station("PM2.5")

        assert annual["coverage"][0] == pytest.approx(1.0)

    def test_an_incomparable_station_is_not_ranked(
        self, daily: Callable[[pl.DataFrame], None]
    ) -> None:
        """A station reporting 100 days does not get to be 'cleanest in Taiwan'."""
        daily(
            _daily_frame(
                _year_of_days("常態站", 2020, 20.0, days=366)
                + _year_of_days("稀疏站", 2020, 1.0, days=100)
            )
        )

        annual = story.annual_by_station("PM2.5").sort("station_name")
        sparse = annual.filter(pl.col("station_name") == "稀疏站")

        assert sparse["rank"][0] is None
        assert annual.filter(pl.col("station_name") == "常態站")["rank"][0] == 1


class TestExceedances:
    def test_counts_travel_with_their_denominator(
        self, daily: Callable[[pl.DataFrame], None]
    ) -> None:
        """A raw count is uninterpretable without the days it was counted over."""
        daily(_daily_frame(_year_of_days("三重", 2021, 40.0, days=365)))

        annual = story.annual_by_station("PM2.5")

        assert annual["days_over_taiwan"][0] == 365, "40 > 35"
        assert annual["days_over_who"][0] == 365, "40 > 15"
        assert annual["share_over_taiwan"][0] == pytest.approx(1.0)

    def test_the_who_threshold_is_stricter_than_taiwan_s(
        self, daily: Callable[[pl.DataFrame], None]
    ) -> None:
        daily(_daily_frame(_year_of_days("三重", 2021, 20.0, days=365)))

        annual = story.annual_by_station("PM2.5")

        assert annual["days_over_who"][0] == 365, "20 > WHO's 15"
        assert annual["days_over_taiwan"][0] == 0, "20 < Taiwan's 35"


class TestBalancedPanel:
    def test_balancing_over_the_whole_record_can_collapse_to_almost_nothing(self) -> None:
        """The trap this machinery exists to avoid.

        One station with a long record and many with short ones: balancing
        back to the start keeps only the one, and its 'national trend' is the
        trend at a single place.
        """
        annual = pl.DataFrame(
            {
                "station_name": ["老站"] * 5 + ["新站A", "新站A", "新站B", "新站B"],
                "year": [2000, 2001, 2002, 2003, 2004, 2003, 2004, 2003, 2004],
                "annual_mean": [30.0] * 9,
            }
        )

        options = story.balanced_panel_options(annual)

        assert options.filter(pl.col("start_year") == 2000)["n_stations"][0] == 1
        assert options.filter(pl.col("start_year") == 2003)["n_stations"][0] == 3

    def test_the_chosen_window_maximises_station_years(self) -> None:
        annual = pl.DataFrame(
            {
                "station_name": ["老站"] * 5 + ["新站A", "新站A", "新站B", "新站B"],
                "year": [2000, 2001, 2002, 2003, 2004, 2003, 2004, 2003, 2004],
                "annual_mean": [30.0] * 9,
            }
        )

        options = story.balanced_panel_options(annual)

        # 2000: 1 station x 5 years = 5.  2003: 3 stations x 2 years = 6.
        assert story.choose_balanced_start(options) == 2003

    def test_a_tie_goes_to_the_longer_record(self) -> None:
        options = pl.DataFrame(
            {
                "start_year": [2000, 2010],
                "n_stations": [2, 4],
                "n_years": [20, 10],
                "station_years": [40, 40],
            }
        )

        assert story.choose_balanced_start(options) == 2000

    def test_the_balanced_series_is_null_before_the_window_starts(
        self, daily: Callable[[pl.DataFrame], None]
    ) -> None:
        # 2020 as the start gives 1 station x 2 years = 2 station-years;
        # 2021 gives 3 x 1 = 3, so the rule picks 2021 and 2020 loses its
        # balanced value.
        rows = (
            _year_of_days("老站", 2020, 30.0, days=366)
            + _year_of_days("老站", 2021, 30.0, days=365)
            + _year_of_days("新站A", 2021, 10.0, days=365)
            + _year_of_days("新站B", 2021, 10.0, days=365)
        )
        daily(_daily_frame(rows))

        series, panel = story.national_trend("PM2.5")

        assert panel["balanced_since"] == 2021
        before = series.filter(pl.col("year") == 2020)
        assert before["balanced"][0] is None
        assert before["all_stations"][0] == pytest.approx(30.0)

    def test_the_panel_reports_which_stations_it_used(
        self, daily: Callable[[pl.DataFrame], None]
    ) -> None:
        """A number whose membership is not stated cannot be checked."""
        rows = _year_of_days("老站", 2020, 30.0, days=366) + _year_of_days(
            "老站", 2021, 20.0, days=365
        )
        daily(_daily_frame(rows))

        _, panel = story.national_trend("PM2.5")

        assert panel["balanced_stations"] == ["老站"]
        assert panel["options"], "the alternatives are published alongside the choice"


class TestStationCards:
    def test_a_closed_station_gets_the_card_for_its_last_good_year(
        self, daily: Callable[[pl.DataFrame], None]
    ) -> None:
        rows = _year_of_days("已停用", 2003, 40.0, days=365) + _year_of_days(
            "已停用", 2004, 40.0, days=50
        )
        daily(_daily_frame(rows))

        cards = story.station_cards("PM2.5")

        assert len(cards) == 1
        assert cards[0]["year"] == 2003, "2004 had 50 days and is not comparable"

    def test_the_cigarette_figure_ships_with_its_caveat(
        self,
        daily: Callable[[pl.DataFrame], None],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The analogy is a popularisation and must never travel bare."""
        daily(_daily_frame(_year_of_days("三重", 2020, 22.0, days=366)))
        monkeypatch.setattr(story, "_stations", lambda: pl.DataFrame({"station_name": ["三重"]}))
        monkeypatch.setattr(story, "_export_pitfalls", lambda root: [])
        monkeypatch.setattr(story, "_export_replication", lambda root: [])

        import json

        story.export_story(tmp_path)
        payload = json.loads(
            (tmp_path / "story" / "station-cards.json").read_text(encoding="utf-8")
        )

        assert payload["cards"][0]["cigarettes_per_day"] == pytest.approx(1.0)
        assert "劑量反應模型" in payload["cigarette_caveat"]

    def test_a_station_with_no_comparable_year_yields_no_card(
        self, daily: Callable[[pl.DataFrame], None]
    ) -> None:
        daily(_daily_frame(_year_of_days("三重", 2020, 20.0, days=30)))

        assert story.station_cards("PM2.5") == []
