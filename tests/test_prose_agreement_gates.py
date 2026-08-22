"""The prose-agreement gates, tested on the failure paths that were broken.

`check_published_spatial.py` and `check_published_headline.py` compare numbers
retyped into prose against the committed story payloads. Both reported zero
disagreements against the real files while unable to catch the drift they exist
for, which is why these tests exist and why they are all about failing.

Three defects, each found only by running a gate against deliberately stale
input:

* integer counts were compared with a tolerance — `places=0` set the slack to
  `10**-0 == 1.0`, so 60 stations against 61 agreed, and off-by-one on a station
  count is the whole point;
* a pattern that stopped matching returned no problems, so rewording a sentence
  switched off its own check with no output at all;
* a greedy separator matched and read the **wrong** number, reporting a
  disagreement that looked entirely real.

**None of these was a discovery.** `check_published_forecast.py` predates all of
them and already compared at the printed precision and already reported a table
that matched nothing. The later gates were written beside a working example
without carrying its properties across, which makes them regressions rather than
new problems — so it is tested here too, and cannot lose what the others had to
be taught.

A gate verified only against correct files is a gate whose failure path has
never run. These tests are that failure path, kept.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from scripts import check_published_detection as detection
from scripts import check_published_forecast as forecast
from scripts import check_published_headline as headline
from scripts import check_published_sarima as sarima
from scripts import check_published_site_prose as site_prose
from scripts import check_published_spatial as spatial


class TestIntegersCompareExactly:
    """The defect that made the spatial gate ornamental on its first run."""

    @pytest.mark.parametrize("gate", [spatial, headline, sarima, detection, site_prose])
    def test_an_off_by_one_count_is_a_disagreement(self, gate: object) -> None:
        # `places=None` means "an exact integer". A tolerance here is what let
        # 60 stations agree with 61.
        assert not gate.agrees(60.0, 61.0, None)  # type: ignore[attr-defined]
        assert gate.agrees(61.0, 61.0, None)  # type: ignore[attr-defined]

    @pytest.mark.parametrize("gate", [spatial, headline, sarima, detection, site_prose])
    def test_a_rounded_decimal_still_gets_its_last_place(self, gate: object) -> None:
        """The payload rounds on export, so three decimals of 0.1555 may
        honestly print either way. Real drift is further off than that."""
        assert gate.agrees(0.156, 0.1555, 3)  # type: ignore[attr-defined]
        assert gate.agrees(0.155, 0.1555, 3)  # type: ignore[attr-defined]
        assert not gate.agrees(0.157, 0.1555, 3)  # type: ignore[attr-defined]


class TestSilenceIsNotAPass:
    """A reworded sentence must not switch off its own check."""

    def test_a_missing_lisa_sentence_is_reported(self) -> None:
        payload = {"lisa": {"stations": 61, "significant_raw": 7, "significant_bh": 0}}

        problems = spatial.check_lisa("這段散文完全沒有提到那個統計量。", "methodology.md", payload)

        assert len(problems) == 1
        assert "no LISA sentence matched" in problems[0]

    def test_a_present_lisa_sentence_is_compared(self) -> None:
        payload = {"lisa": {"stations": 61, "significant_raw": 7, "significant_bh": 0}}
        stale = "**LISA**：60 站中 raw 顯著 8 站、**BH 後 0 站**——相依是場性質。"

        problems = spatial.check_lisa(stale, "methodology.md", payload)

        assert any("LISA stations" in p and "60" in p for p in problems)
        assert any("LISA raw significant" in p and "8" in p for p in problems)

    def test_a_headline_claim_that_stops_matching_is_reported(self) -> None:
        claim = headline.Claim("national fall", r"降了\s*(\d+)%", 60.32, 0)

        problems = claim.check("README.md", "這句話換了寫法，不再提那個百分比。")

        assert len(problems) == 1
        assert "no longer matches" in problems[0]


class TestTypographyIsNotADisagreement:
    def test_a_unicode_minus_parses_as_a_negative_number(self) -> None:
        """U+2212 reads better in a table than a hyphen and does not parse as
        one. `float()` raised on it until the headline gate normalised it."""
        claim = headline.Claim("median slope", r"中位數\s*\|\s*\*{0,2}(−?-?[\d.]+)", -1.24, 2)

        assert claim.check("methodology.md", "| 觀測斜率中位數 | **−1.24** μg/m³/年 |") == []

    def test_the_spatial_gate_reads_the_same_sign(self) -> None:
        assert spatial.num("−0.230") == pytest.approx(-0.230)
        assert spatial.num("**+0.156**") == pytest.approx(0.156)


class TestASeparatorMustNotEatItsNeighbour:
    """The SARIMA gate matched and read the wrong number before it read none.

    A greedy `.{0,3}` between 「秒」 and the observation count swallowed 「、8,」
    and captured 612 out of 8,612. The gate then reported 「says 612, payload has
    8612」 — a disagreement that looks entirely real and would send someone to
    edit a document that was correct. Failing to match is safer than that.
    """

    def test_the_fits_sentence_reads_a_thousands_separated_count(self) -> None:
        sentence = "**18/18 次擬合全部收斂**，\n中位數 11 秒、8,612 個有效觀測點。"
        payload = {
            "fits": {"converged": 18, "total": 18, "median_seconds": 11.0, "median_observed": 8612}
        }

        assert sarima.check_fits(sentence, payload) == []

    def test_the_same_sentence_wrapped_differently_still_matches(self) -> None:
        """Prose here is hard-wrapped, so a newline can land anywhere in a
        clause. The first version used `[^\\n]` and called the sentence missing."""
        payload = {
            "fits": {"converged": 18, "total": 18, "median_seconds": 11.0, "median_observed": 8612}
        }

        one_line = "**18/18 次擬合全部收斂**，中位數 11 秒、8,612 個有效觀測點。"

        assert sarima.check_fits(one_line, payload) == []

    def test_a_missing_fits_sentence_is_reported(self) -> None:
        payload = {
            "fits": {"converged": 18, "total": 18, "median_seconds": 11.0, "median_observed": 8612}
        }

        problems = sarima.check_fits("這段沒有提到擬合。", payload)

        assert len(problems) == 1
        assert "次擬合全部收斂" in problems[0]


class TestAConclusionCanOutliveTheNumbersUnderIt:
    """D8's table can be corrected cell by cell and stop meaning what it says.

    「測不到，不是等於零」 rests on every event passing fewer stations than chance
    predicts. A re-run that pushed one above its expectation could be copied
    faithfully into the table — every cell agreeing — while the sentence beneath
    became false. So the relation is checked against the payload, not the prose.
    """

    @staticmethod
    def _event(passed: float, expected: float) -> dict[str, object]:
        return {
            "event": "COVID-19 全國三級警戒",
            "n_credible": passed,
            "n_expected_by_chance": expected,
        }

    def test_passing_fewer_than_chance_is_what_the_sentence_claims(self) -> None:
        assert detection.check_the_reading_still_holds([self._event(1, 3.3)]) == []

    def test_passing_more_than_chance_is_reported_even_if_the_table_is_correct(self) -> None:
        problems = detection.check_the_reading_still_holds([self._event(4, 3.3)])

        assert len(problems) == 1
        assert "低於機率預期" in problems[0]

    def test_matching_chance_exactly_is_not_below_it(self) -> None:
        assert detection.check_the_reading_still_holds([self._event(3.3, 3.3)])


class TestTheGateThatAlreadyHadThisRight:
    """`check_published_forecast.py` predates the other four and had both
    properties from the start: it compares at the printed precision rather than
    with a tolerance, and it reports a table that matched nothing.

    The defects in the later gates were therefore **regressions**, not
    discoveries — a working example sat beside them and its properties were not
    carried over. Tested here so the oldest gate cannot lose what the newer ones
    had to be taught.
    """

    def test_it_compares_at_the_printed_precision(self) -> None:
        assert forecast.compare("f", 1, "r2", 0.5061, 0.5064, 3) is None
        assert forecast.compare("f", 1, "r2", 0.506, 0.512, 3) is not None

    def test_a_table_that_matched_nothing_is_reported(self) -> None:
        expected = {1: {"model_r2": 0.5, "skill_persistence": 0.1}}

        problems = forecast.check_table(
            "forecast.py docstring",
            forecast._MD_ROW,
            "this prose has no table in it",
            expected,
            fields=("model_r2", "skill_persistence"),
        )

        assert any("no rows matched at all" in p for p in problems)

    def test_a_unicode_minus_is_read_as_negative(self) -> None:
        assert forecast.num("−0.262") == pytest.approx(-0.262)


class TestControlTableAgreement:
    @staticmethod
    def _payload(mean_i: float, months: int) -> dict[str, object]:
        return {
            "controls": [
                {
                    "control": "pooled",
                    "params": 13,
                    "mean_i": mean_i,
                    "months_significant_bh": months,
                    "months_scored": 96,
                }
            ]
        }

    def test_a_stale_significant_month_count_is_caught(self) -> None:
        row = "| 合併式（未分層） | 13 | +0.156 | 55/96 |"

        problems = spatial.check_control_table(row, self._payload(0.1555, 54))

        assert any("BH significant months" in p for p in problems)

    def test_a_matching_row_reports_nothing(self) -> None:
        row = "| 合併式（未分層） | 13 | +0.156 | 54/96 |"

        assert spatial.check_control_table(row, self._payload(0.1555, 54)) == []

    def test_a_table_that_matched_nothing_says_so(self) -> None:
        """Renaming every control would otherwise pass in silence."""
        problems = spatial.check_control_table("no table here", self._payload(0.1555, 54))

        assert any("no control rows matched" in p for p in problems)


class TestTheWebsiteIsAProseSurfaceToo:
    """The sixth gate reads `.astro`, which the other five never did.

    All five compare `docs/*.md` against payloads, so the chapters readers
    actually open were unwatched. `ChapterSpatial.astro` was still saying the raw
    island-wide correlation is 0.73 — the value `working-rules.md` cites as this
    project's canonical drift case, corrected everywhere except the website.
    """

    def test_a_chapter_that_stops_stating_its_number_is_reported(self) -> None:
        claim = site_prose.Claim("ChapterSpatial.astro", "raw correlation", r"相關\s*([\d.]+)", 2)

        problems = claim.check("這一段換了寫法，不再給那個數字。", 0.782)

        assert len(problems) == 1
        assert "no longer matches" in problems[0]

    def test_the_drift_that_was_live_is_a_disagreement(self) -> None:
        claim = site_prose.Claim("ChapterSpatial.astro", "raw correlation", r"相關\s*([\d.]+)", 2)

        problems = claim.check("生料月均值全島相關 0.73——那是共用的冬季高峰。", 0.782)

        assert len(problems) == 1
        assert "says 0.73" in problems[0] and "0.782" in problems[0]

    def test_rounding_the_source_down_to_two_places_is_not_drift(self) -> None:
        """0.006 printed at two decimals is honestly 0.01. Failing that would
        get this gate switched off inside a week — and it is the reason the
        anomaly correlation is *not* on the defect list beside the raw one."""
        claim = site_prose.Claim("ChapterSpatial.astro", "anomaly", r"掉到\s*([\d.]+)", 2)

        assert claim.check("平均相關掉到 0.01，剩下的才是偏離。", 0.006) == []

    def test_a_sentence_that_now_reads_its_number_from_the_payload_passes(self) -> None:
        """The fix this gate exists to produce. `ChapterMethods.astro` had
        `pooledRetained` in scope four lines above the sentence that retyped its
        value; once the sentence interpolates, the number cannot drift and needs
        no comparison."""
        claim = site_prose.Claim(
            "ChapterMethods.astro",
            "month share",
            r"月份解釋\s*([\d.]+)%",
            1,
            without_a_literal=r"月份解釋\s*\{[^{}]*\}%",
        )

        assert claim.check("逐時變異中，月份解釋 {n(pooledRetained * 100, 1)}%，", 20.298) == []

    def test_deleting_the_sentence_is_still_reported(self) -> None:
        """Interpolating and deleting look identical to a pattern that only
        knows the literal form, so the without_a_literal shape has to be named
        explicitly rather than treated as "anything but a number"."""
        claim = site_prose.Claim(
            "ChapterMethods.astro",
            "month share",
            r"月份解釋\s*([\d.]+)%",
            1,
            without_a_literal=r"月份解釋\s*\{[^{}]*\}%",
        )

        problems = claim.check("這一段不再談變異分解。", 20.298)

        assert len(problems) == 1
        assert "no longer matches" in problems[0]

    def test_a_sentence_that_hands_its_figure_to_the_report_passes(self) -> None:
        """The other accepted fix. `docs/methodology.md` stopped quoting this
        project's canonical drift case and named the file that recomputes it;
        the chapter now does the same. A figure nobody retypes cannot drift."""
        claim = site_prose.Claim(
            "ChapterSpatial.astro",
            "raw island correlation",
            r"生料月均值全島相關\s*([\d.]+)",
            2,
            without_a_literal=r"生料月均值在全島高度相關.*?reports/03-spatial\.md",
        )
        fixed = (
            "生料月均值在全島高度相關——那是共用的冬季高峰，不是分區的性質。"
            "兩個值每次執行重算，現值印在 reports/03-spatial.md。"
        )

        assert claim.check(fixed, 0.782) == []

    def test_deleting_the_caveat_is_not_the_same_as_fixing_it(self) -> None:
        """The caveat is load-bearing — it is why the anomaly step exists. A
        pattern that accepted "no number present" would rate its removal and its
        correction identically, so the shape of the sentence is required."""
        claim = site_prose.Claim(
            "ChapterSpatial.astro",
            "raw island correlation",
            r"生料月均值全島相關\s*([\d.]+)",
            2,
            without_a_literal=r"生料月均值在全島高度相關.*?reports/03-spatial\.md",
        )

        problems = claim.check("這一節直接談分群結果，不再解釋為什麼要減島均。", 0.782)

        assert len(problems) == 1
        assert "no longer matches" in problems[0]

    def test_a_stale_literal_still_fails_after_the_delegation_shape_exists(self) -> None:
        """Adding an accepted alternative must not weaken the original check."""
        claim = site_prose.Claim(
            "ChapterSpatial.astro",
            "raw island correlation",
            r"生料月均值全島相關\s*([\d.]+)",
            2,
            without_a_literal=r"生料月均值在全島高度相關.*?reports/03-spatial\.md",
        )

        problems = claim.check("生料月均值全島相關 0.73——那是共用的冬季高峰。", 0.782)

        assert len(problems) == 1
        assert "says 0.73" in problems[0]

    def test_a_measurand_count_is_compared_exactly(self) -> None:
        """`places=None`. Twenty against twenty-one is the drift, not slack."""
        claim = site_prose.Claim("Explorer.astro", "measurands", r"完整的\s*(\d+)\s*個測項", None)

        assert claim.check("完整的 21 個測項共 54.6 MB。", 21.0) == []
        assert claim.check("完整的 20 個測項共 54.6 MB。", 21.0)

    def test_a_report_that_stops_stating_its_truth_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The truth side needs the same protection as the prose side.

        `reports/03-spatial.md` regenerates every run, so a reworded template
        could stop printing the figure the chapter is checked against. Comparing
        prose to a value that was never found is worse than not checking: it
        would compare against whatever the parse happened to yield.
        """
        reworded = tmp_path / "03-spatial.md"
        reworded.write_text("這份報告改寫過，不再印出那兩個相關值。\n", encoding="utf-8")
        monkeypatch.setattr(site_prose, "SPATIAL_REPORT", reworded)

        with pytest.raises(SystemExit) as excinfo:
            site_prose.spatial_truth()

        assert "no longer states raw_correlation" in str(excinfo.value)

    def test_the_real_report_still_states_all_four(self) -> None:
        """And the same parse against the committed report, so a rewording that
        happens for good reasons is caught here rather than in CI."""
        truth = site_prose.spatial_truth()

        assert set(truth) == {
            "raw_correlation",
            "anomaly_correlation",
            "spacing_min",
            "spacing_max",
        }
        assert truth["raw_correlation"] > truth["anomaly_correlation"]

    def test_a_bound_on_two_without_a_literal_numbers_is_checked_as_a_relation(self) -> None:
        """`ChapterTrend.astro` reads both weather shares from the payload and
        then types a bound on their difference. Both numbers can stay correct
        through a re-run while the bound stops holding — the same shape as D8's
        「低於機率預期」, which is why that one is checked as a relation too."""
        sentence = "與 43.4% 相差不到 1.5 個百分點。"

        assert site_prose.check_the_two_aggregations_still_agree(sentence, 1.2) == []

    def test_a_bound_that_stopped_holding_is_reported(self) -> None:
        sentence = "與 43.4% 相差不到 1.5 個百分點。"

        problems = site_prose.check_the_two_aggregations_still_agree(sentence, 2.4)

        assert len(problems) == 1
        assert "1.5" in problems[0] and "2.4" in problems[0]

    def test_a_bound_exactly_met_is_not_exceeded(self) -> None:
        sentence = "與 43.4% 相差不到 1.5 個百分點。"

        assert site_prose.check_the_two_aggregations_still_agree(sentence, 1.5) == []

    def test_a_deleted_bound_is_reported(self) -> None:
        problems = site_prose.check_the_two_aggregations_still_agree("這段不再比較兩種聚合。", 1.2)

        assert len(problems) == 1
        assert "no longer stated" in problems[0]

    def test_the_gate_refuses_to_pass_for_reading_no_chapter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Renaming every chapter would otherwise print three zeroes and exit 0
        — the defect `check_like_ci.py` and `check_internal_links.py` both
        shipped with."""
        monkeypatch.setattr(site_prose, "COMPONENTS", site_prose.REPO_ROOT / "no-such-directory")

        with pytest.raises(SystemExit) as excinfo:
            site_prose.main()

        assert "refusing to report success" in str(excinfo.value)


class TestAnInterpolationMustBeTheRightOne:
    """`{[^{}]*}` accepted any interpolation, including the wrong variable.

    The gate's contract is 「this number cannot drift」, and an interpolation of
    the wrong payload field satisfies that while showing something false. The
    controls caption is the case that proved it matters: it read 「0 到 0.18」
    against `max(mean_i_hi)` = 0.1752 and passed on tolerance alone, while the
    chart's axis actually ends at 0.1752 x 1.12 = 0.196224 and its outermost
    labelled tick is 0.15. 0.18 was none of the three.

    So each interpolated claim now names the expression it must contain. These
    tests run against the shipped `CLAIMS`, not a local copy, because a rule
    that only exists in a test is not the rule the gate applies.
    """

    @staticmethod
    def claim_for(component: str, what: str) -> site_prose.Claim:
        for claim, _, _ in site_prose.CLAIMS:
            if claim.component == component and claim.what == what:
                return claim
        raise AssertionError(f"no shipped claim for {component} / {what}")

    CONTROLS_AXIS = 0.1752 * 1.12

    def test_the_stale_literal_fails_against_the_real_axis_end(self) -> None:
        claim = self.claim_for("ChapterSpatial.astro", "controls bar extent")

        problems = claim.check("這裡是 0 到 0.18 的五種控制", self.CONTROLS_AXIS)

        assert len(problems) == 1
        assert "says 0.18" in problems[0]

    def test_the_correct_expression_passes(self) -> None:
        claim = self.claim_for("ChapterSpatial.astro", "controls bar extent")

        assert claim.check("這裡是 0 到 {n(maxI, 2)} 的五種控制", self.CONTROLS_AXIS) == []

    def test_the_controls_caption_may_not_borrow_the_correlogram_variable(self) -> None:
        """`maxAbs` is the other chart's axis. Interpolating it here cannot
        drift and would still be wrong — which is the whole point."""
        claim = self.claim_for("ChapterSpatial.astro", "controls bar extent")

        problems = claim.check("這裡是 0 到 {n(maxAbs, 2)} 的五種控制", self.CONTROLS_AXIS)

        assert len(problems) == 1
        assert "no longer matches" in problems[0]

    def test_the_correlogram_caption_may_not_borrow_the_controls_variable(self) -> None:
        claim = self.claim_for("ChapterSpatial.astro", "correlogram scale")

        problems = claim.check("上圖是 ±{n(maxI, 2)} 的距離分帶", 0.31855)

        assert len(problems) == 1
        assert "no longer matches" in problems[0]

    def test_the_correlogram_caption_accepts_only_its_own(self) -> None:
        claim = self.claim_for("ChapterSpatial.astro", "correlogram scale")

        assert claim.check("上圖是 ±{n(maxAbs, 2)} 的距離分帶", 0.31855) == []

    def test_the_two_methods_shares_may_not_swap_variables(self) -> None:
        """They read different rows of the same table, so swapping them yields
        two sentences that are each individually plausible and both wrong."""
        month = self.claim_for("ChapterMethods.astro", "month share of variance")
        station_month = self.claim_for("ChapterMethods.astro", "station x month share")

        swapped_month = month.check("月份解釋 {n(retained * 100, 1)}%", 20.298)
        swapped_sm = station_month.check(
            "「測站 × 月份」解釋 {n(pooledRetained * 100, 1)}%", 40.267
        )

        assert len(swapped_month) == 1
        assert len(swapped_sm) == 1
        assert all("no longer matches" in p for p in swapped_month + swapped_sm)

    def test_the_two_methods_shares_accept_their_own(self) -> None:
        month = self.claim_for("ChapterMethods.astro", "month share of variance")
        station_month = self.claim_for("ChapterMethods.astro", "station x month share")

        assert month.check("月份解釋 {n(pooledRetained * 100, 1)}%", 20.298) == []
        assert station_month.check("「測站 × 月份」解釋 {n(retained * 100, 1)}%", 40.267) == []

    def test_whitespace_inside_the_expression_is_not_a_disagreement(self) -> None:
        """Prettier may reflow `{n(maxI, 2)}` to `{n(maxI,2)}`. That is
        formatting, not a change of variable."""
        claim = self.claim_for("ChapterSpatial.astro", "controls bar extent")

        assert claim.check("這裡是 0 到 {n(maxI,2)} 的五種控制", self.CONTROLS_AXIS) == []
