"""Tests for the chapter payloads.

These carry editorial choices — which threshold, which baseline, which
stations — and the tests are written as claims about those choices, because a
wrong one produces a chart that is confidently misleading rather than obviously
broken.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from twair import panels
from twair.paths import REPO_ROOT
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

    def test_station_cards_and_payload_do_not_publish_a_cigarette_analogy(
        self,
        daily: Callable[[pl.DataFrame], None],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        daily(_daily_frame(_year_of_days("三重", 2020, 22.0, days=366)))
        monkeypatch.setattr(story, "_stations", lambda: pl.DataFrame({"station_name": ["三重"]}))
        monkeypatch.setattr(story, "_export_pitfalls", lambda root: [])

        import json

        story.export_story(tmp_path)
        payload = json.loads(
            (tmp_path / "story" / "station-cards.json").read_text(encoding="utf-8")
        )

        forbidden = {
            "cigarettes" + "_per_day",
            "cigarette" + "_equivalent_ugm3",
            "cigarette" + "_caveat",
        }
        assert forbidden.isdisjoint(payload)
        assert forbidden.isdisjoint(payload["cards"][0])

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


class TestThePersistenceBaselineQuotesM2:
    """The last typed measurement in this payload, and it had already drifted.

    The sentence said the baseline's R² was 0.900 while `reports/01-core.md`'s
    own generated table — same repository, same run — said 0.8995, which is 0.899
    at the precision the sentence prints. One computed artefact and one typed
    one, disagreeing about one number, with the website showing the typed one.

    The figures belong to M2, so the exporter asks M2 rather than carrying a
    copy. That is a cross-module read, and it is the same relation every other
    exporter in the file already has with the module it describes.
    """

    # The three splits M2 actually produced, and the four feature sets beside
    # them. Written out here because `data/outputs/` is gitignored: the first
    # version of these tests read the real file, passed on the machine that had
    # run M2, and failed in CI — the trap the short contributor rules name, walked into while
    # fixing a defect of the same family.
    M2_ROLLING = (
        ("persistence", "-", (0.914856, 0.903149, 0.880364)),
        ("lightgbm", "full", (0.516, 0.534477, 0.520950)),
        ("lightgbm", "full_raw_wind", (0.530, 0.541, 0.540324)),
        ("lightgbm", "full_with_pm10", (0.778801, 0.782477, 0.754259)),
        ("climatology", "-", (0.239743, 0.064776, 0.072773)),
    )

    @classmethod
    def with_m2(cls, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Point the exporter at a scores frame in the real schema."""
        rows = [
            {
                "feature_set": feature_set,
                "model": model,
                "split_kind": "rolling",
                "split": f"rolling_{i + 1}",
                "n": 1027243,
                "rmse": 5.4,
                "mae": 3.7,
                "r2": r2,
                "exceedance_f1": 0.85,
            }
            for model, feature_set, values in cls.M2_ROLLING
            for i, r2 in enumerate(values)
        ]
        directory = tmp_path / "m2_drivers"
        directory.mkdir(parents=True)
        pl.DataFrame(rows).write_parquet(directory / "scores.parquet")
        monkeypatch.setattr(story, "outputs_dir", lambda module: tmp_path / module)

    def test_it_states_the_mean_across_the_splits(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """0.914856, 0.903149 and 0.880364 average to 0.899456 — 0.899, not the
        0.900 the sentence used to carry."""
        self.with_m2(tmp_path, monkeypatch)

        why = story._persistence_baseline_why()

        assert "R² 是 0.899" in why
        assert "0.900" not in why

    def test_the_explanatory_model_is_the_headline_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`full_with_pm10` scores higher and is the leakage case chapter 8
        prices; `full_raw_wind` is the encoding diagnostic beside it. Taking a
        maximum over feature sets would quote one of those instead."""
        self.with_m2(tmp_path, monkeypatch)

        why = story._persistence_baseline_why()

        assert "所有解釋性模型的 0.524" in why
        assert "0.772" not in why and "0.537" not in why

    def test_a_missing_m2_says_less_rather_than_inventing_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A claim about a measurement that does not exist is worse than an
        unquantified one."""
        monkeypatch.setattr(story, "outputs_dir", lambda module: tmp_path / module)

        why = story._persistence_baseline_why()

        assert "Phase 2 量到" not in why
        assert "門檻" in why
        assert not any(ch.isdigit() for ch in why.replace("PM2.5", ""))

    def test_the_published_sentence_is_one_this_builder_can_produce(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The committed payload against the code that now generates it.

        Written against a frame rather than against `data/outputs/`, so it holds
        in a clean checkout. It is the M2 figures that matter, not the file: the
        means above are the ones that produced the published sentence.
        """
        self.with_m2(tmp_path, monkeypatch)
        payload = json.loads(
            (REPO_ROOT / "web" / "public" / "data" / "story" / "forecast.json").read_text(
                encoding="utf-8"
            )
        )
        persistence = next(b for b in payload["baselines"] if b["name"] == "persistence")

        assert persistence["why"] == story._persistence_baseline_why()


class TestChapterSixReadsItsOwnTable:
    """Four paragraphs that used to retype the table sitting beside them.

    Every measured figure in chapter 6's 「how to read this」 rows — the two R²
    values, the four skill values, the two climatology skills, the worst split,
    the widest spread and the three narrower ones — was a literal in the same
    function that computes them.

    A mutation showed what that was worth: moving the numbers in the payload's
    prose while leaving the chart data untouched passed
    `check_published_forecast` and `check_published_site_prose`, and failed
    `check_publication_structure` with 「forecast reading row text changed」. That
    third gate pins the text against being *edited*, not against being *wrong* —
    a re-run moving the real values would leave the literals alone, so the
    payload and the pinned copy would agree and both would be stale.

    The proof that the rewrite is faithful is
    `test_the_rows_reproduce_the_published_prose_exactly`: the derived rows equal
    the committed payload byte for byte, so this commit changed no sentence.
    """

    @staticmethod
    def horizons() -> list[dict[str, Any]]:
        payload = json.loads(
            (REPO_ROOT / "web" / "public" / "data" / "story" / "forecast.json").read_text(
                encoding="utf-8"
            )
        )
        return list(payload["horizons"])

    def test_the_rows_reproduce_the_published_prose_exactly(self) -> None:
        payload = json.loads(
            (REPO_ROOT / "web" / "public" / "data" / "story" / "forecast.json").read_text(
                encoding="utf-8"
            )
        )

        assert story._forecast_reading(payload["horizons"]) == payload["reading"]

    def test_the_least_stable_horizon_is_the_widest_one_not_the_named_one(self) -> None:
        """The chapter says 6 hours because 6 hours has the widest spread across
        splits. Said the other way round it would be a caption for a claim the
        data had stopped making."""
        rows = self.horizons()
        for row in rows:
            base = 0.30 if row["horizon"] == 24 else 0.20
            row["per_split"] = [
                {**s, "skill_persistence": base if i else 0.0}
                for i, s in enumerate(row["per_split"])
            ]

        built = story._forecast_reading(rows)

        assert "24 小時仍是最不穩的期距" in built[3]["claim"]
        assert "6 小時仍是最不穩" not in built[3]["claim"]

    def test_the_multiple_follows_the_two_r_squareds(self) -> None:
        rows = self.horizons()
        rows[0]["model_r2"], rows[-1]["model_r2"] = 0.8, 0.4

        assert "R² 掉兩倍的同時" in story._forecast_reading(rows)[0]["claim"]

    def test_the_multiple_keeps_the_register_of_a_characterisation(self) -> None:
        """Measurements are Arabic here and characterisations are not. Deriving
        the value and printing it as `3` would be correct and would change the
        voice of the sentence."""
        assert "R² 掉三倍" in story._forecast_reading(self.horizons())[0]["claim"]

    def test_a_negative_split_is_not_described_as_absent(self) -> None:
        """「現在這張表沒有負的格子」 is a claim about the current table."""
        rows = self.horizons()
        rows[1]["per_split"][0]["skill_persistence"] = -0.02

        built = story._forecast_reading(rows)[2]["detail"]

        assert "仍有負的格子" in built
        assert "沒有負的格子" not in built
        assert "最差是 −0.020" in built

    def test_the_first_backtest_stays_where_it_is(self) -> None:
        """−0.111 and the demo figures record a past state, which is the case
        `docs/working-rules.md` exempts by name. Deriving them would destroy the
        record and gain nothing."""
        rows = self.horizons()
        rows[1]["per_split"][0]["skill_persistence"] = 0.5

        text = " ".join(r["detail"] for r in story._forecast_reading(rows))

        assert "第一次回測時" in text and "−0.111" in text
        assert "−0.043" in text and "+0.256" in text
        assert "前 167 小時" in text

    def test_the_cell_count_is_counted(self) -> None:
        """「16 個格子」 is four horizons times four splits, and it was typed."""
        rows = self.horizons()
        for row in rows:
            row["per_split"] = row["per_split"][:2]

        assert "8 個「期距 × 分割」格子" in story._forecast_reading(rows)[2]["detail"]

    def test_a_withheld_figure_refuses_rather_than_printing_none(self) -> None:
        rows = self.horizons()
        rows[0]["model_r2"] = None

        with pytest.raises(ValueError, match="has no model_r2"):
            story._forecast_reading(rows)

    def test_no_horizons_is_not_an_empty_sentence(self) -> None:
        with pytest.raises(ValueError, match="no forecast horizons"):
            story._forecast_reading([])

    @pytest.mark.parametrize(("value", "expected"), [(0.08, "+0.080"), (-0.111, "−0.111")])
    def test_a_signed_figure_uses_the_minus_the_prose_uses(
        self, value: float, expected: str
    ) -> None:
        """U+2212, not a hyphen — matching the chart labels it sits beside."""
        assert story._signed(value) == expected


class TestChapterOnesBoundaryParagraph:
    """The sentence a reader meets first, and the two things wrong with it.

    `0.445` was typed into this caveat two lines below the expression that
    computes the same median, which is the exact pattern six prose gates exist
    to catch. Nothing compared them: the headline gate would have caught a
    drifted median, but only because `docs/methodology.md` retypes it too, so a
    fix there alone would have left the website saying the old figure with every
    gate green.

    「一半以上」 is a second claim hiding in the same sentence. It is true because
    the median sits below 0.5, and a re-run that pushed it above would make the
    sentence false while every number in it stayed correct.
    """

    def test_the_number_is_the_one_it_was_given(self) -> None:
        assert "0.512" in story._deweather_caveat(0.512)
        assert "0.445" not in story._deweather_caveat(0.512)

    def test_a_median_below_a_half_leaves_most_variance_unexplained(self) -> None:
        assert "有一半以上不是本地氣象能解釋的" in story._deweather_caveat(0.445)

    def test_a_median_above_a_half_says_the_opposite(self) -> None:
        """The claim follows the value. Swapping only the quantifier would give
        「有不到一半不是……」, a double negative that reads as a typo."""
        caveat = story._deweather_caveat(0.6)

        assert "本地氣象能解釋逐時變異的一半以上" in caveat
        assert "有一半以上不是" not in caveat

    def test_exactly_a_half_is_not_more_than_a_half(self) -> None:
        assert "有一半以上不是" not in story._deweather_caveat(0.5)

    def test_a_withheld_median_claims_no_proportion(self) -> None:
        """A missing aggregate is reported, never invented — and a caveat that
        named a figure it did not have would be the worst place to break that."""
        caveat = story._deweather_caveat(None)

        assert "這一次沒有量到" in caveat
        assert "一半以上" not in caveat
        assert "R² 中位數 " not in caveat.replace("的 holdout R² 中位數，", "")

    @pytest.mark.parametrize("median", [0.445, 0.512, 0.6])
    def test_both_borrowed_words_are_explained_where_they_appear(self, median: float) -> None:
        """Chapter 1 is the homepage's primary path, and `holdout` appears once
        on the whole site. `scripts/check_term_first_use.py` holds these two
        anchors against the built page."""
        caveat = story._deweather_caveat(median)

        assert "把一段資料留著不給模型學" in caveat
        assert "解釋掉的變異比例" in caveat

    def test_a_whole_number_median_does_not_render_as_a_float(self) -> None:
        """`:g` rather than `str`, so a median of 1.0 reads as 1 and not 1.0."""
        assert "中位數 1——" in story._deweather_caveat(1.0)


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
