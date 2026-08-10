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

from twair import panels
from twair.viz import story


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

        options = panels.balanced_panel_options(annual)

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

        options = panels.balanced_panel_options(annual)

        # 2000: 1 station x 5 years = 5.  2003: 3 stations x 2 years = 6.
        assert panels.choose_balanced_start(options) == 2003

    def test_a_tie_goes_to_the_longer_record(self) -> None:
        options = pl.DataFrame(
            {
                "start_year": [2000, 2010],
                "n_stations": [2, 4],
                "n_years": [20, 10],
                "station_years": [40, 40],
            }
        )

        assert panels.choose_balanced_start(options) == 2000

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
        self, daily: Callable[[pl.DataFrame], None], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rows = _year_of_days("已停用", 2003, 40.0, days=365) + _year_of_days(
            "已停用", 2004, 40.0, days=50
        )
        daily(_daily_frame(rows))
        # The station register is a local artefact of `twair stations` and is
        # gitignored, so reaching the real one made this the only test in the
        # suite that could not run on a clean checkout. Which year the card
        # describes does not depend on the register, so it is stubbed the same
        # way the cigarette test already stubs it.
        monkeypatch.setattr(story, "_stations", lambda: pl.DataFrame({"station_name": ["已停用"]}))

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


class TestTheImputationPayload:
    """Pitfall 07's payload carries an editorial choice that a sort would undo.

    Gap-length buckets are physical: 1h, 2-3h, 4-12h, 13-48h, >48h. Sorted as
    strings, ">48h" comes first and "13-48h" lands between "1h" and "2-3h",
    which turns the chart's whole shape — an error that does or does not grow
    with gap length — into noise. The order therefore ships in the payload
    rather than being left to the front end to guess.
    """

    def _payload(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        import json

        source = tmp_path / "m11_imputation"
        source.mkdir()
        pl.DataFrame(
            {
                "gap_bucket": [">48h", "1h", "2-3h"],
                "gaps": [453, 40406, 10214],
                "hours": [84972, 40406, 23053],
                "share_of_gaps": [0.0081, 0.7251, 0.1833],
                "share_of_missing_hours": [0.4345, 0.2066, 0.1179],
                "stations": [78, 78, 78],
                "period": ["2010-2017"] * 3,
            }
        ).write_parquet(source / "gap_distribution.parquet")
        pl.DataFrame(
            {
                "strategy": ["neighbor", "neighbor", "neighbor"],
                "gap_bucket": ["all", ">48h", "1h"],
                "hidden": [40679, 40679, 40679],
                "recovered": [28367, 1823, 11312],
                "recovery_rate": [0.6973, None, None],
                "mae": [7.41, 8.96, 7.31],
                "rmse": [10.6, 12.34, 10.39],
                "bias": [-0.16, None, None],
                "n": [28367, 1823, 11312],
                "stations": [12, 12, 12],
            }
        ).write_parquet(source / "reconstruction.parquet")

        monkeypatch.setattr(story, "outputs_dir", lambda name: tmp_path / name)
        written = story._export_imputation(tmp_path)
        assert written
        return json.loads(written[0].read_text(encoding="utf-8"))  # type: ignore[no-any-return]

    def test_the_gap_buckets_ship_in_physical_order(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        payload = self._payload(tmp_path, monkeypatch)

        assert payload["buckets"] == ["1h", "2-3h", "4-12h", "13-48h", ">48h"]
        assert [row["gap_bucket"] for row in payload["distribution"]] == ["1h", "2-3h", ">48h"]

    def test_counts_are_integers_not_rounded_floats(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A station count written as 78.0 reads as an estimate."""
        payload = self._payload(tmp_path, monkeypatch)

        assert payload["stations_measured"] == 78
        assert isinstance(payload["stations_measured"], int)
        assert isinstance(payload["hidden"], int)

    def test_the_withheld_number_is_named_rather_than_omitted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A reader who does not find the downstream R² should be told it was
        attempted, not left to assume nobody looked."""
        payload = self._payload(tmp_path, monkeypatch)

        assert "downstream_r2" in payload["not_reported"]


class TestTheDeweatheredSeries:
    """Chapter 1's second line, and the reason it is built from M4's own output.

    The temptation is to plot the normalised series against chapter 1's
    existing trend. That would compare a 74-station fit against a 68-station
    daily-aggregate panel, and part of the gap between the two lines would be
    the station sets rather than the weather — which is the exact confound
    chapter 1's *first* correction exists to remove.
    """

    @staticmethod
    def _monthly(rows: list[dict[str, Any]]) -> pl.DataFrame:
        return pl.DataFrame(
            rows,
            schema={
                "station_name": pl.Utf8,
                "month": pl.Date,
                "observed": pl.Float64,
                "normalised": pl.Float64,
            },
        )

    @staticmethod
    def _months(
        station: str, year: int, observed: float, normalised: float, *, months: int = 12
    ) -> list[dict[str, Any]]:
        return [
            {
                "station_name": station,
                "month": date(year, m, 1),
                "observed": observed,
                "normalised": normalised,
            }
            for m in range(1, months + 1)
        ]

    def _run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, frame: pl.DataFrame
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        source = tmp_path / "m4_deweather"
        source.mkdir()
        frame.write_parquet(source / "monthly.parquet")
        monkeypatch.setattr(story, "outputs_dir", lambda name: tmp_path / name)
        return story._deweather_series()

    def test_both_lines_are_averaged_over_the_same_stations(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rows = (
            self._months("長期站", 2020, 30.0, 25.0)
            + self._months("長期站", 2021, 20.0, 24.0)
            # Present in 2021 only, so it must not enter the panel at all —
            # otherwise 2021 averages two stations against 2020's one.
            + self._months("新站", 2021, 5.0, 5.0)
        )

        series, panel = self._run(tmp_path, monkeypatch, self._monthly(rows))

        assert panel["stations"] == ["長期站"]
        assert [row["year"] for row in series] == [2020, 2021]
        assert series[1]["observed"] == pytest.approx(20.0), "新站 must not drag 2021 down"
        assert series[1]["normalised"] == pytest.approx(24.0)

    def test_a_year_missing_months_does_not_count_as_a_year(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """M4's first year holds about eight months; a partial year averaged
        against full ones is a seasonal artefact wearing a trend's clothing."""
        rows = (
            self._months("站", 2019, 40.0, 40.0, months=8)
            + self._months("站", 2020, 30.0, 25.0)
            + self._months("站", 2021, 20.0, 24.0)
        )

        _, panel = self._run(tmp_path, monkeypatch, self._monthly(rows))

        assert panel["balanced_since"] == 2020

    def test_the_start_year_maximises_station_years(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same rule chapter 1 uses. One station over three years is three
        station-years; three stations over two years is six."""
        rows = list(self._months("老站", 2019, 30.0, 30.0))
        for year in (2020, 2021):
            for name in ("老站", "新A", "新B"):
                rows += self._months(name, year, 20.0, 20.0)

        _, panel = self._run(tmp_path, monkeypatch, self._monthly(rows))

        assert panel["balanced_since"] == 2020
        assert panel["n_stations"] == 3
        assert panel["station_years"] == 6

    def test_the_weather_share_of_the_fall_is_derived_from_the_two_falls(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rows = self._months("站", 2020, 30.0, 30.0) + self._months("站", 2021, 10.0, 20.0)

        _, panel = self._run(tmp_path, monkeypatch, self._monthly(rows))

        assert panel["observed_fall"] == pytest.approx(20.0)
        assert panel["normalised_fall"] == pytest.approx(10.0)
        assert panel["weather_share_of_fall"] == pytest.approx(0.5)

    def test_an_absent_monthly_output_degrades_rather_than_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The chapter falls back to the slope figures; the export still runs."""
        monkeypatch.setattr(story, "outputs_dir", lambda name: tmp_path / name)

        series, panel = story._deweather_series()

        assert series == []
        assert panel == {}


class TestTheSourcesPayload:
    def _payload(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        import json

        source = tmp_path / "m7_sources"
        source.mkdir()
        pl.DataFrame(
            {
                "station_name": ["低風站", "中風站", "高風站"],
                "threshold": [20.0, 30.0, 40.0],
                "calm_fraction": [0.1, 0.2, 0.3],
                "resultant": [0.4, 0.5, 0.6],
                "peak_sector": [0, 30, 60],
                "peak_speed": ["0.5-1.5", "2.5-4", "8+"],
                "percentile": [75.0, 75.0, 75.0],
                "n_suppressed_bins": [1, 2, 3],
            }
        ).write_parquet(source / "summary.parquet")
        pl.DataFrame(
            {
                "station_name": ["低風站", "中風站", "高風站"],
                "sector": [0, 30, 60],
                "speed_bin": ["0.5-1.5", "2.5-4", "8+"],
                "probability": [0.2, 0.3, 0.4],
                "n": [20, 30, 40],
            }
        ).write_parquet(source / "grid.parquet")
        monkeypatch.setattr(story, "outputs_dir", lambda name: tmp_path / name)
        monkeypatch.setattr(story, "_stations", lambda: pl.DataFrame({"station_name": []}))

        written = story._export_sources(tmp_path)

        assert written
        payload = json.loads(written[0].read_text(encoding="utf-8"))
        assert isinstance(payload, dict)
        return payload

    def test_peak_speed_classes_name_observed_wind_groups(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        payload = self._payload(tmp_path, monkeypatch)

        assert {name: row["wind_peak_class"] for name, row in payload["stations"].items()} == {
            "低風站": "low_wind_peak",
            "中風站": "mid_wind_peak",
            "高風站": "high_wind_peak",
        }

    def test_the_sources_payload_names_wind_patterns_without_attribution(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        payload = self._payload(tmp_path, monkeypatch)

        assert set(payload["wind_peak_counts"]) == {
            "low_wind_peak",
            "mid_wind_peak",
            "high_wind_peak",
        }
        assert "signature_counts" not in payload
        assert all("wind_peak_class" in row for row in payload["stations"].values())
        assert all("signature" not in row for row in payload["stations"].values())
        assert sum(payload["wind_peak_counts"].values()) == len(payload["stations"])
        assert payload["explains"] == "給定風從某方位、以某風速吹來，該小時濃度落在高值區的機率。"
        for term in ("來源地", "距離", "來源身分", "貢獻", "化學成分", "軌跡", "擴散", "排放清冊"):
            assert term in payload["cannot_say"]


class TestPlainTextPayloadClaimBoundaries:
    """The site prints these strings verbatim; nothing renders them.

    Three payload strings carried `**emphasis**` written by habit, and the site
    showed the asterisks to the reader. Emphasis belongs in the component,
    which has real markup. This walks every exported payload rather than
    naming the three, because the next one will be written by habit too. The
    detection payload also supplies prose that must describe observational
    contrasts without turning them into identified causal effects.
    """

    def test_verbatim_payload_prose_preserves_plain_text_and_detection_claims(self) -> None:
        import json

        from twair.viz.export import web_data_dir

        story_root = web_data_dir() / "story"
        offenders: list[str] = []
        for path in sorted(story_root.glob("*.json")):
            text = path.read_text(encoding="utf-8")
            if "**" in text or "__" in json.dumps(json.loads(text), ensure_ascii=False):
                offenders.append(path.name)

        assert not offenders, f"markdown emphasis in payload prose: {offenders}"

        detection = json.loads((story_root / "detection-limit.json").read_text(encoding="utf-8"))
        window = detection["method"]["window"]
        assert "事件日曆窗口內「觀測值減去模型預測值」的差額" in window
        assert "不等同於已識別的因果效應" in window
        assert "8 站" in detection["spatial_check"]["why"]
        assert "全部落在安慰劑散布之內" in detection["spatial_check"]["why"]
        assert "效應該出現的地方" not in detection["spatial_check"]["why"]
        assert "真的停止燃煤" not in detection["spatial_check"]["why"]


class TestTheSarimaPayload:
    """D10's payload has to hold two things apart that a reader will want to mix.

    Chapter 5 prints these numbers a few paragraphs below the LightGBM ones, and
    they are not comparable: different stations, different origins, different
    rows. The payload therefore ships the warning as a field rather than trusting
    the component to remember, and the sign convention on `margin` has to be
    unambiguous because the chapter renders it as a percentage either way.
    """

    def _payload(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        import json

        source = tmp_path / "m12_sarima"
        source.mkdir()
        # One horizon SARIMA loses and one it wins, so the sign convention is
        # exercised in both directions.
        pl.DataFrame(
            {
                "station_name": ["古亭"] * 6,
                "split": ["rolling_1"] * 6,
                "horizon": [1, 1, 1, 24, 24, 24],
                "method": ["sarima", "persistence", "climatology"] * 2,
                "n": [100] * 6,
                "rmse": [5.0, 4.0, 9.0, 8.0, 9.0, 10.0],
                "mae": [3.0, 2.0, 7.0, 6.0, 7.0, 8.0],
                "r2": [0.5, 0.6, 0.1, 0.3, 0.2, 0.1],
            }
        ).write_parquet(source / "scores.parquet")
        pl.DataFrame(
            {
                "station_name": ["古亭"],
                "split": ["rolling_1"],
                "train_points": [8760],
                "train_observed": [8294],
                "fit_seconds": [28.1],
                "converged": [True],
                "aic": [49830.0],
            }
        ).write_parquet(source / "fits.parquet")
        pl.DataFrame(
            {
                "points": [1000],
                "auto_seconds": [11.14],
                "fixed_seconds": [0.58],
                "search_multiple": [19.1],
            }
        ).write_parquet(source / "selection_cost.parquet")

        monkeypatch.setattr(story, "outputs_dir", lambda name: tmp_path / name)
        written = story._export_sarima(tmp_path)
        assert written
        return json.loads(written[0].read_text(encoding="utf-8"))  # type: ignore[no-any-return]

    def test_margin_is_positive_only_when_sarima_wins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        payload = self._payload(tmp_path, monkeypatch)
        by_horizon = {row["horizon"]: row for row in payload["horizons"]}

        # 5.0 against a best baseline of 4.0 is a loss of 25%.
        assert by_horizon[1]["margin"] == pytest.approx(-0.25)
        # 8.0 against a best baseline of 9.0 is a win of 11.1%.
        assert by_horizon[24]["margin"] == pytest.approx(0.111, abs=5e-4)

    def test_the_best_baseline_is_the_lowest_error_not_the_first_listed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """At 24h climatology is worse than persistence, so persistence is the bar."""
        payload = self._payload(tmp_path, monkeypatch)
        by_horizon = {row["horizon"]: row for row in payload["horizons"]}

        assert by_horizon[1]["best_baseline"] == "persistence"
        assert by_horizon[24]["best_baseline"] == "persistence"

    def test_the_non_comparability_warning_ships_with_the_numbers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        payload = self._payload(tmp_path, monkeypatch)

        assert "LightGBM" in payload["not_comparable"]
        assert payload["no_lightgbm"]

    def test_the_payload_does_not_restate_the_headline_the_chapter_supplies(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The site printed the same sentence twice in a row on the first run."""
        payload = self._payload(tmp_path, monkeypatch)

        assert not payload["verdict"].startswith("跳過 SARIMA")
        assert not payload["not_comparable"].startswith("這張表不能")

    def test_convergence_is_reported_rather_than_assumed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        payload = self._payload(tmp_path, monkeypatch)

        assert payload["fits"]["total"] == 1
        assert payload["fits"]["converged"] == 1
        assert isinstance(payload["fits"]["median_observed"], int)
