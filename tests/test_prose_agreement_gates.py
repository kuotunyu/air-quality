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

import json
from pathlib import Path

import pytest
from scripts import check_chapter_titles as chapter_titles
from scripts import check_cjk_spacing as cjk_spacing
from scripts import check_publication_structure as publication_structure
from scripts import check_published_detection as detection
from scripts import check_published_forecast as forecast
from scripts import check_published_headline as headline
from scripts import check_published_sarima as sarima
from scripts import check_published_site_prose as site_prose
from scripts import check_published_spatial as spatial
from scripts import check_term_first_use as term_first_use


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


class TestAGateMustNotCrashWhileReporting:
    """Three gates document a relative path and crashed on one.

    `python scripts/check_cjk_spacing.py web/dist` is the usage line in that
    file's own docstring, and it raised `ValueError` from
    `page.relative_to(ROOT)` — because `ROOT` is absolute and the argument was
    not. CI never saw it: it passes no argument, so the default absolute path is
    used every time.

    Losing the report is worse than the path being ugly, which is why the
    fallback prints the path as given rather than raising.
    """

    @pytest.mark.parametrize(
        "gate",
        [cjk_spacing, chapter_titles, publication_structure, term_first_use],
    )
    def test_a_path_outside_the_repository_is_printed_not_raised(
        self, gate: object, tmp_path: Path
    ) -> None:
        outside = tmp_path / "dist" / "index.html"

        assert gate.shown(outside) == outside.as_posix()  # type: ignore[attr-defined]

    @pytest.mark.parametrize(
        "gate",
        [cjk_spacing, chapter_titles, publication_structure, term_first_use],
    )
    def test_a_path_inside_the_repository_stays_repo_relative(self, gate: object) -> None:
        inside = gate.ROOT / "web" / "dist" / "index.html"  # type: ignore[attr-defined]

        assert gate.shown(inside) == "web/dist/index.html"  # type: ignore[attr-defined]


class TestTheM2TableIsReadFromTheReport:
    """`docs/working-rules.md` states three of M2's figures and nothing read them.

    Verified by mutation on 2026-08-24, before this existed: setting the tree
    pair to 0.999 / 0.111 left `check_published_headline`,
    `check_published_site_prose` and `check_published_spatial` all green. The
    generator was fixed the same day; the static copies had no source at all.

    Truth comes from `reports/01-core.md`'s own rolling table because no payload
    carries these — `pitfalls.json` has the `full` feature set but not
    `full_raw_wind`, and nothing carries the persistence baseline.
    """

    ROLLING = """### 滾動原點驗證（訓練用過去、測試用未來）

| model | feature_set | rmse | mae | r2 | f1 | splits |
|---|---|---|---|---|---|---|
| persistence | - | 5.4046 | 3.7326 | 0.8995 | 0.8577 | 3 |
| lightgbm | full_raw_wind | 11.8386 | 8.8616 | 0.5371 | 0.6538 | 3 |
| lightgbm | full | 12.0159 | 9.0602 | 0.5238 | 0.6476 | 3 |

### 留一測站（空間泛化）

| model | feature_set | rmse | mae | r2 | f1 | splits |
|---|---|---|---|---|---|---|
| persistence | - | 9.9999 | 9.9999 | 0.1111 | 0.1111 | 3 |
"""

    def _report(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str) -> None:
        path = tmp_path / "01-core.md"
        path.write_text(text, encoding="utf-8")
        monkeypatch.setattr(headline, "CORE_REPORT", path)

    def test_the_rows_are_read_by_model_and_feature_set(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._report(tmp_path, monkeypatch, self.ROLLING)

        assert headline.m2_rolling_r2() == {
            "persistence/-": 0.8995,
            "lightgbm/full_raw_wind": 0.5371,
            "lightgbm/full": 0.5238,
        }

    def test_the_divider_is_not_a_row(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        r"""`|---|---|---|` has no spaces in it, so a `\S+` for the first column
        matched the whole line and produced a key of dashes carrying the
        persistence row's `mae` as its value."""
        self._report(tmp_path, monkeypatch, self.ROLLING)

        keys = headline.m2_rolling_r2()

        assert not [k for k in keys if set(k) <= {"-", "/"}]
        assert 3.7326 not in keys.values(), "that is a mae, not an r2"

    def test_only_the_rolling_section_is_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The leave-one-station table repeats every model name with different
        numbers. An unscoped row pattern reads whichever comes first — the
        mistake `retention_truth()` made against the yearly-validity table."""
        self._report(tmp_path, monkeypatch, self.ROLLING)

        assert headline.m2_rolling_r2()["persistence/-"] == 0.8995

    def test_a_missing_section_is_refused_rather_than_read_as_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._report(tmp_path, monkeypatch, "## M2\n\nnothing here\n")

        with pytest.raises(SystemExit, match="no '### 滾動原點驗證' section"):
            headline.m2_rolling_r2()

    def test_a_section_with_no_rows_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An empty result would make every claim below it raise a KeyError
        rather than report, and a gate that finds nothing must not pass."""
        self._report(tmp_path, monkeypatch, "### 滾動原點驗證\n\n_M2 尚未執行。_\n")

        with pytest.raises(SystemExit, match="no readable rows"):
            headline.m2_rolling_r2()

    @pytest.mark.parametrize(
        ("what", "pattern", "quoted"),
        [
            ("tree R2 with raw bearing", r"raw bearing \(R²\s*([\d.]+)\)", "0.999"),
            ("tree R2 with sin/cos", r"with sin/cos \(([\d.]+)\)", "0.111"),
            ("persistence baseline R2", r"reaches R²\s*([\d.]+)", "0.777"),
        ],
    )
    def test_a_drifted_figure_is_a_disagreement(self, what: str, pattern: str, quoted: str) -> None:
        sentence = (
            f"with the raw bearing (R² {quoted}) than with sin/cos ({quoted}), "
            f"and persistence reaches R² {quoted} against 0.524"
        )
        claim = headline.Claim(what, pattern, 0.5371, 3)

        problems = claim.check("working-rules.md", sentence)

        assert len(problems) == 1
        assert "0.5371" in problems[0]

    def test_the_published_figures_agree_with_the_report(self) -> None:
        """The live check, on the committed files, so this cannot pass while the
        real pair has drifted."""
        rolling = headline.m2_rolling_r2()
        text = (headline.SOURCES["working-rules.md"]).read_text(encoding="utf-8")

        for what, pattern, key in (
            (
                "tree R2 with raw bearing",
                r"raw bearing \(R²\s*([\d.]+)\)",
                "lightgbm/full_raw_wind",
            ),
            ("tree R2 with sin/cos", r"with sin/cos \(([\d.]+)\)", "lightgbm/full"),
            ("persistence baseline R2", r"reaches R²\s*([\d.]+)", "persistence/-"),
        ):
            assert (
                headline.Claim(what, pattern, rolling[key], 3).check("working-rules.md", text) == []
            )


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


class TestThePairingIsTheArgument:
    """D8's closing paragraph is two lists that only mean something together.

    「前金 −23.4%、左營 −18.5%、古亭 −9.9%」 is what a headline would print, and
    「−0.24、−0.52、−0.12」 is the same three stations in units of their own
    placebo spread. The point is that the first list looks like a finding and the
    second says it is not. Six numbers typed by hand: a re-run could move the
    percentages and leave the z-scores, and both lists would still read as
    plausible while no longer describing the same stations.

    `analysis/causal.py` computed `effect_pct` and `z_against_placebo` all along
    and `story.py` dropped them in a two-column projection, so the fix was to
    carry them and interpolate.
    """

    @staticmethod
    def claim_for(what: str) -> site_prose.Claim:
        for claim, _, _ in site_prose.CLAIMS:
            if claim.component == "ChapterDetection.astro" and claim.what == what:
                return claim
        raise AssertionError(f"no shipped claim for {what}")

    def test_the_payload_carries_a_percentage_and_a_z_for_all_three(self) -> None:
        """If an export ever drops them again, this says so — rather than the
        page throwing at build time with the reason buried in a stack trace."""
        truth = site_prose.detection_truth()

        # Exact, not a superset: a key vanishing is the failure this holds
        # against, so it is re-stated when one is added rather than loosened.
        assert set(truth) == {
            "前金_pct",
            "左營_pct",
            "古亭_pct",
            "前金_z",
            "左營_z",
            "古亭_z",
            "古亭_drop",
        }
        # Every effect is a fall, and the drop the caveat quotes is its magnitude.
        assert all(value < 0 for key, value in truth.items() if key != "古亭_drop")
        # A magnitude, so the prose can say 「下降 X」 without a sign. Pinning the
        # value here would be the defect this whole file exists to remove.
        assert truth["古亭_drop"] > 0

    def test_a_missing_station_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        payload = {
            "events": [
                {
                    "event": "COVID-19 全國三級警戒",
                    "kind": "window",
                    "station_effects": [
                        {"station": "前金", "effect": -1.94, "effect_pct": -23.42, "z": -0.242}
                    ],
                }
            ]
        }
        path = tmp_path / "detection-limit.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        monkeypatch.setattr(site_prose, "DETECTION", path)

        with pytest.raises(SystemExit) as excinfo:
            site_prose.detection_truth()

        assert "左營" in str(excinfo.value)

    def test_a_station_without_the_two_fields_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The exact regression this work removed: the projection carries the
        station and its effect, and silently not the two the prose needs."""
        payload = {
            "events": [
                {
                    "event": "COVID-19 全國三級警戒",
                    "kind": "window",
                    "station_effects": [
                        {"station": name, "effect": -1.0} for name in ("前金", "左營", "古亭")
                    ],
                }
            ]
        }
        path = tmp_path / "detection-limit.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        monkeypatch.setattr(site_prose, "DETECTION", path)

        with pytest.raises(SystemExit) as excinfo:
            site_prose.detection_truth()

        assert "effect_pct" in str(excinfo.value) or "z" in str(excinfo.value)

    def test_a_hand_typed_percentage_that_drifted_is_reported(self) -> None:
        claim = self.claim_for("urban lockdown percentage")

        problems = claim.check("封城期間的都會測站：前金 −22.1%、左營 −18.5%、古亭 −9.9%。", -23.42)

        assert len(problems) == 1
        assert "says -22.1" in problems[0]

    def test_the_shipped_interpolation_passes(self) -> None:
        claim = self.claim_for("urban lockdown percentage")
        source = (site_prose.COMPONENTS / "ChapterDetection.astro").read_text(encoding="utf-8")

        assert claim.check(site_prose.flat(source), -23.42) == []

    def test_a_hand_typed_z_that_drifted_is_reported(self) -> None:
        claim = self.claim_for("urban lockdown z score")

        problems = claim.check("z 分數是 <strong>−0.99、−0.52、−0.12</strong>——全部", -0.242)

        assert len(problems) == 1
        assert "says -0.99" in problems[0]

    def test_the_shipped_z_interpolation_passes(self) -> None:
        claim = self.claim_for("urban lockdown z score")
        source = (site_prose.COMPONENTS / "ChapterDetection.astro").read_text(encoding="utf-8")

        assert claim.check(site_prose.flat(source), -0.242) == []


class TestTruthCanLiveInARegeneratedReport:
    """Not every figure has a JSON payload behind it, and that is not a reason
    to leave it unwatched.

    `ChapterMethods` quotes two things the site's payloads do not carry: the
    LightGBM R² for raw-bearing against sin/cos encoding, and the per-year
    invalid-value retention rates. Both *are* produced — the first is a row of
    the rolling-origin table in `reports/01-core.md`, the second a row of the
    retention table in `docs/data-quality.md`, whose own header says it is
    generated by `uv run twair qc report`.

    A chapter cannot interpolate from Markdown, so these stay typed. The gate is
    what makes a typed number trustworthy, which is exactly what the five older
    gates do for `docs/*.md`.

    Worth keeping straight: `docs/archive-formats.md` says the retention rates
    are 「沒有守護」 because *verifying they are correct* needs 16 GB of raw
    archives CI can never have. Checking that the chapter still agrees with the
    generated report is a different claim, and it needs only what is committed.
    """

    @staticmethod
    def claim_for(what: str) -> site_prose.Claim:
        for claim, _, _ in site_prose.CLAIMS:
            if claim.component == "ChapterMethods.astro" and claim.what == what:
                return claim
        raise AssertionError(f"no shipped claim for {what}")

    def test_the_core_report_still_states_both_encodings(self) -> None:
        truth = site_prose.core_truth()

        assert set(truth) == {"r2_raw_wind", "r2_sin_cos"}
        # The chapter's point is that the raw bearing does slightly *better*.
        assert truth["r2_raw_wind"] > truth["r2_sin_cos"]

    def test_a_core_report_without_the_table_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        report = tmp_path / "01-core.md"
        report.write_text("這份報告改寫過，不再有滾動原點表。\n", encoding="utf-8")
        monkeypatch.setattr(site_prose, "CORE_REPORT", report)

        with pytest.raises(SystemExit) as excinfo:
            site_prose.core_truth()

        assert "full_raw_wind" in str(excinfo.value)

    def test_the_retention_table_still_states_all_three_years(self) -> None:
        truth = site_prose.retention_truth()

        assert set(truth) == {"1997", "1998", "2001"}
        assert truth["1997"] == 0.0
        assert truth["2001"] == 0.0

    def test_a_retention_table_missing_a_year_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        report = tmp_path / "data-quality.md"
        report.write_text(
            "## 無效值的測值保留率（格式世代差異）\n\n"
            "| 年份 | 世代 | 總數 | 保留 | 比例 |\n"
            "| 1997 | legacy_csv_big5 | 481336 | 2 | 0.0 |\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(site_prose, "QUALITY_REPORT", report)

        with pytest.raises(SystemExit) as excinfo:
            site_prose.retention_truth()

        assert "1998" in str(excinfo.value)

    def test_a_report_without_the_retention_section_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        report = tmp_path / "data-quality.md"
        report.write_text("## 別的章節\n\n| 1997 | x | 1 | 1 | 0.9 |\n", encoding="utf-8")
        monkeypatch.setattr(site_prose, "QUALITY_REPORT", report)

        with pytest.raises(SystemExit) as excinfo:
            site_prose.retention_truth()

        assert "保留率" in str(excinfo.value)

    def test_the_parse_does_not_read_a_year_out_of_a_different_table(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The bug this scoping exists for, kept.

        `data-quality.md` carries several tables with a year column. An unscoped
        row pattern read 1997 as 0.8325 out of the yearly-validity table — it
        matched, and returned the wrong number, which sends someone to correct a
        chapter that was right. Failing to match would have been safer; reading
        the correct table is better still.
        """
        report = tmp_path / "data-quality.md"
        report.write_text(
            "## 逐年有效值比例\n\n"
            "| 年份 | 世代 | 總數 | 有效 | 比例 |\n"
            "| 1997 | legacy_csv_big5 | 481336 | 400000 | 0.8325 |\n"
            "| 1998 | legacy_csv_big5 | 492509 | 416000 | 0.8452 |\n"
            "| 2001 | legacy_csv_big5 | 487873 | 409000 | 0.8399 |\n\n"
            "## 無效值的測值保留率（格式世代差異）\n\n"
            "| 年份 | 世代 | 總數 | 保留 | 比例 |\n"
            "| 1997 | legacy_csv_big5 | 481336 | 2 | 0.0 |\n"
            "| 1998 | legacy_csv_big5 | 492509 | 167618 | 0.3403 |\n"
            "| 2001 | legacy_csv_big5 | 487873 | 1 | 0.0 |\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(site_prose, "QUALITY_REPORT", report)

        assert site_prose.retention_truth() == {"1997": 0.0, "1998": 0.3403, "2001": 0.0}

    def test_a_drifted_tree_r_squared_is_reported(self) -> None:
        claim = self.claim_for("tree r2 with the raw bearing")

        problems = claim.check("（R² 0.499 對 0.524）——樹可以反覆切分同一個變數，", 0.5371)

        assert len(problems) == 1
        assert "says 0.499" in problems[0]

    def test_the_shipped_tree_r_squared_agrees(self) -> None:
        claim = self.claim_for("tree r2 with the raw bearing")
        source = (site_prose.COMPONENTS / "ChapterMethods.astro").read_text(encoding="utf-8")

        assert claim.check(site_prose.flat(source), 0.5371) == []

    def test_a_drifted_retention_rate_is_reported(self) -> None:
        claim = self.claim_for("1998 retention rate")

        problems = claim.check("1997 與 2001 年的保留率是 0.000，1998 年 0.500，", 0.3403)

        assert len(problems) == 1
        assert "says 0.5" in problems[0]

    def test_the_shipped_retention_rates_agree(self) -> None:
        source = site_prose.flat(
            (site_prose.COMPONENTS / "ChapterMethods.astro").read_text(encoding="utf-8")
        )

        assert self.claim_for("1998 retention rate").check(source, 0.3403) == []
        assert self.claim_for("1997 and 2001 retention rate").check(source, 0.0) == []


class TestTheLimitSentenceIsFourNumbers:
    """D8's 「測不到」 aside compares two ranges, and all four ends were typed.

    「噪音底線是 2.5–3.5 μg/m³，而待測的效應量是 0.5–1.6 μg/m³。噪音底線高於訊號。」
    The conclusion rests on the first range sitting above the second, so a re-run
    that moved either could leave a sentence whose numbers no longer support the
    claim printed immediately after them.

    Both derive from the payload. The prose says 「這些日曆窗口」, which is exactly
    the `kind == "window"` events — the trend-break event has a placebo sd of
    0.66 and would drag the floor's lower end well below what the sentence says,
    so the restriction is load-bearing rather than incidental.
    """

    @staticmethod
    def claim_for(what: str) -> site_prose.Claim:
        for claim, _, _ in site_prose.CLAIMS:
            if claim.component == "ChapterDetection.astro" and claim.what == what:
                return claim
        raise AssertionError(f"no shipped claim for {what}")

    def test_the_window_events_bound_both_ranges(self) -> None:
        truth = site_prose.limit_truth()

        assert set(truth) == {"sd_lo", "sd_hi", "effect_lo", "effect_hi"}
        # The whole point of the aside: the floor sits above the signal.
        assert truth["sd_lo"] > truth["effect_hi"]

    def test_the_trend_break_event_is_excluded(self) -> None:
        """Its placebo sd is 0.66. Including it would put the floor's lower end
        near 0.7, and the sentence — which says 2.5 — would then be wrong while
        the gate agreed with it."""
        truth = site_prose.limit_truth()

        assert truth["sd_lo"] > 1.0

    def test_a_payload_with_no_window_event_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "detection-limit.json"
        path.write_text(
            json.dumps({"events": [{"event": "x", "kind": "trend_break"}]}), encoding="utf-8"
        )
        monkeypatch.setattr(site_prose, "DETECTION", path)

        with pytest.raises(SystemExit) as excinfo:
            site_prose.limit_truth()

        assert "window" in str(excinfo.value)

    def test_a_drifted_noise_floor_is_reported(self) -> None:
        problems = self.claim_for("noise floor low end").check(
            "這個方法在這些日曆窗口的噪音底線是 1.9–3.5 μg/m³，", 2.503
        )

        assert len(problems) == 1
        assert "says 1.9" in problems[0]

    def test_a_drifted_effect_size_is_reported(self) -> None:
        problems = self.claim_for("effect size high end").check(
            "而待測的效應量是 0.5–2.9 μg/m³。", 1.612
        )

        assert len(problems) == 1
        assert "says 2.9" in problems[0]

    def test_the_shipped_sentence_agrees_on_all_four(self) -> None:
        source = site_prose.flat(
            (site_prose.COMPONENTS / "ChapterDetection.astro").read_text(encoding="utf-8")
        )
        truth = site_prose.limit_truth()

        for what, key in (
            ("noise floor low end", "sd_lo"),
            ("noise floor high end", "sd_hi"),
            ("effect size low end", "effect_lo"),
            ("effect size high end", "effect_hi"),
        ):
            assert self.claim_for(what).check(source, truth[key]) == [], what

    def test_the_conclusion_is_checked_as_a_relation_not_as_four_cells(self) -> None:
        """Every end can be corrected faithfully after a re-run while the
        ordering they support quietly stops holding."""
        holds = {"sd_lo": 2.503, "sd_hi": 3.512, "effect_lo": 0.494, "effect_hi": 1.612}
        broken = {**holds, "effect_hi": 2.9}

        assert site_prose.check_the_floor_still_sits_above_the_signal(holds) == []

        problems = site_prose.check_the_floor_still_sits_above_the_signal(broken)
        assert len(problems) == 1
        assert "噪音底線高於訊號" in problems[0]

    def test_the_real_payload_still_supports_the_conclusion(self) -> None:
        assert (
            site_prose.check_the_floor_still_sits_above_the_signal(site_prose.limit_truth()) == []
        )


class TestASentenceCanBeWrongAboutItsOwnCorrectNumbers:
    """The health chapter named the wrong two assumptions for eight months.

    Both figures in 「{last_range[0]}% 還是 {last_range[1]}%」 were interpolated and
    both were right. The clause after them said the difference was 「把 2.4 還是
    5.9 μg/m³ 當作比較基準——這兩個數字是同一份 published TMREL 區間的兩端」, and
    that was false: `analysis/health.py` builds the range as min/max across every
    counterfactual, so its upper end is the zero-exposure assumption, which
    `conf/health.yaml` calls almost certainly wrong at the bottom of the range.
    2.4 gives 7.7%, beside a sentence standing next to 9.4%.

    **No prose-agreement gate could have caught it**, because nothing disagreed
    with anything — the numbers matched the payload and only the sentence about
    them was untrue. It was found by working out what a different claim should be
    anchored to, and the repair was to derive the description rather than correct
    it, so the sentence follows the range instead of asserting something beside
    it.

    What a gate *can* hold is that the description stays derived. These tests do
    that, and check the arithmetic the fix depends on.
    """

    @staticmethod
    def claim_for(what: str) -> site_prose.Claim:
        for claim, _, _ in site_prose.CLAIMS:
            if claim.component == "ChapterHealth.astro" and claim.what == what:
                return claim
        raise AssertionError(f"no shipped claim for {what}")

    def test_the_range_ends_still_resolve_to_two_counterfactuals(self) -> None:
        truth = site_prose.health_truth()

        assert set(truth) == {
            "lower_counterfactual",
            "upper_counterfactual",
            "first_range_width",
            "last_range_width",
        }
        # The narrower counterfactual gives the smaller attributable fraction, so
        # the range's first end is the higher concentration.
        assert truth["lower_counterfactual"] > truth["upper_counterfactual"]

    def test_a_range_end_matching_no_counterfactual_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The build throws on this too. Both are wanted: the page must not
        render prose about an assumption nobody made, and CI must not need a
        browser to find out."""
        payload = {
            "years": [2025],
            "headline": {"last_year": 2025, "last_range": [0.052, 0.4242]},
            "series": [
                {"name": "gbd_high", "value": 5.9, "paf": [0.052]},
                {"name": "zero", "value": 0.0, "paf": [0.0941]},
            ],
        }
        path = tmp_path / "health.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        monkeypatch.setattr(site_prose, "HEALTH", path)

        with pytest.raises(SystemExit) as excinfo:
            site_prose.health_truth()

        assert "0.4242" in str(excinfo.value)

    def test_a_hand_typed_counterfactual_is_reported(self) -> None:
        """The regression itself: someone types the value back in."""
        claim = self.claim_for("first counterfactual named")

        problems = claim.check("唯一的差別是把 2.4 還是 0 μg/m³ 當作比較基準", 5.9)

        assert len(problems) == 1
        assert "says 2.4" in problems[0]

    def test_the_shipped_sentence_derives_both(self) -> None:
        claim = self.claim_for("first counterfactual named")
        source = site_prose.flat(
            (site_prose.COMPONENTS / "ChapterHealth.astro").read_text(encoding="utf-8")
        )

        assert claim.check(source, 5.9) == []


class TestTheLastTwoTypedFiguresAreDerivedNow:
    """The two the sweep left, closed the same way as the rest.

    `ChapterDetection` used 「下降 0.96」 as an illustrative drop while 古亭's own
    effect sat in the payload beside it at exactly that value — the one figure on
    the site whose number was in reach of the prose retyping it.
    `ChapterHealth` hedged 「大約 3.6 到 4.2 個百分點」 for a width that is the
    arithmetic of two ranges the same chapter already prints; the hedge went with
    the hand-typing, because a derived number does not need one.

    What these hold is that both stay derived. A literal typed back in is caught
    by the pattern; the accepted form names the expression, so interpolating a
    different variable does not pass either.
    """

    @staticmethod
    def claim_for(component: str, what: str) -> site_prose.Claim:
        for claim, _, _ in site_prose.CLAIMS:
            if claim.component == component and claim.what == what:
                return claim
        raise AssertionError(f"no shipped claim for {component} / {what}")

    def test_a_typed_illustrative_drop_that_drifted_is_reported(self) -> None:
        """A literal typed back in, against a re-run that moved 古亭's effect."""
        claim = self.claim_for("ChapterDetection.astro", "illustrative drop")

        problems = claim.check("所以看到某站「下降 0.55」時，真正該問的是", 0.96)

        assert len(problems) == 1
        assert "says 0.55" in problems[0]

    def test_deleting_the_caveat_is_not_the_same_as_deriving_it(self) -> None:
        """The sentence carries the chapter's point — that a small drop looks
        like a finding until you see the placebo mean. Its absence must not read
        as a fix."""
        claim = self.claim_for("ChapterDetection.astro", "illustrative drop")

        problems = claim.check("這一段不再拿任何一站當例子。", 0.96)

        assert len(problems) == 1
        assert "no longer matches" in problems[0]

    def test_the_shipped_drop_is_derived(self) -> None:
        claim = self.claim_for("ChapterDetection.astro", "illustrative drop")
        source = site_prose.flat(
            (site_prose.COMPONENTS / "ChapterDetection.astro").read_text(encoding="utf-8")
        )

        assert claim.check(source, 0.96) == []

    def test_a_typed_interval_width_is_reported(self) -> None:
        claim = self.claim_for("ChapterHealth.astro", "headline range width")

        problems = claim.check("絕對寬度幾乎沒動（大約 3.6 到 4.2 個百分點）", 3.64)

        assert len(problems) == 1

    def test_the_shipped_widths_are_derived(self) -> None:
        claim = self.claim_for("ChapterHealth.astro", "headline range width")
        source = site_prose.flat(
            (site_prose.COMPONENTS / "ChapterHealth.astro").read_text(encoding="utf-8")
        )

        assert claim.check(source, 3.64) == []

    def test_the_widths_come_from_the_ranges_the_chapter_prints(self) -> None:
        """The arithmetic itself, so the claim above is anchored to something."""
        truth = site_prose.health_truth()

        assert truth["first_range_width"] > 0
        assert truth["last_range_width"] > truth["first_range_width"]
