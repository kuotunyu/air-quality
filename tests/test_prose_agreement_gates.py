"""The prose-agreement gates, tested on the failure paths that were broken.

`check_published_spatial.py` and `check_published_headline.py` compare numbers
retyped into prose against the committed story payloads. Both reported zero
disagreements against the real files while unable to catch the drift they exist
for, which is why these tests exist and why they are all about failing.

Two defects, both found only by running the gates against deliberately stale
input:

* integer counts were compared with a tolerance — `places=0` set the slack to
  `10**-0 == 1.0`, so 60 stations against 61 agreed, and off-by-one on a station
  count is the whole point;
* a pattern that stopped matching returned no problems, so rewording a sentence
  switched off its own check with no output at all.

A gate verified only against correct files is a gate whose failure path has
never run. These tests are that failure path, kept.
"""

from __future__ import annotations

import pytest
from scripts import check_published_headline as headline
from scripts import check_published_sarima as sarima
from scripts import check_published_spatial as spatial


class TestIntegersCompareExactly:
    """The defect that made the spatial gate ornamental on its first run."""

    @pytest.mark.parametrize("gate", [spatial, headline, sarima])
    def test_an_off_by_one_count_is_a_disagreement(self, gate: object) -> None:
        # `places=None` means "an exact integer". A tolerance here is what let
        # 60 stations agree with 61.
        assert not gate.agrees(60.0, 61.0, None)  # type: ignore[attr-defined]
        assert gate.agrees(61.0, 61.0, None)  # type: ignore[attr-defined]

    @pytest.mark.parametrize("gate", [spatial, headline, sarima])
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
