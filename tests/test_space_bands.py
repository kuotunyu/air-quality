"""The Space's prediction interval, which nothing checked until it shipped twice.

`spaces/forecast/` has no CI coverage of any kind — no gate, no site-quality, no
type check. On 2026-08-23 it produced two defects inside two hours, both of them
found by rebuilding the bundle and calling `forecast()` on a real row rather than
by reading anything:

* the 48-hour band came out **narrower** than the 24-hour one, which reads as
  「more certain further out」;
* the 48-hour lower bound rendered as **−1.2 μg/m³**, a negative concentration.

Neither is visible in the manifest or in the source. `model_value - half` is an
entirely ordinary line.

`app.py` loads a manifest, a demo slice and four boosters at import time, and the
bundle those come from is untracked, so CI can never import it. `bands.py` exists
so the part that decides what a reader is told takes numbers and returns strings.
These are the tests that could not be written before.
"""

from __future__ import annotations

import sys

import pytest

from twair.paths import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "spaces" / "forecast"))

from bands import band_notes, interval_cell


class TestAConcentrationCannotBeNegative:
    """The defect: −1.2 μg/m³ shown to a demo visitor.

    A conformal band is symmetric by construction, so near the floor its lower
    end goes through zero. Chapter 9's own example queries include 「PM2.5 大於
    PM10 的比率（物理上不可能）」 — this project treats impossible values as
    findings, and would have published one.
    """

    def test_the_lower_end_is_floored_at_zero(self) -> None:
        cell, _ = interval_cell(8.2, 9.4)

        assert cell.startswith("0.0–")
        assert "-" not in cell.split("–")[0]

    def test_the_untruncated_end_comes_back_for_reporting(self) -> None:
        """Clamping silently would advertise a coverage the interval no longer
        has, so the caller is given the number it has to report."""
        _, raw_low = interval_cell(8.2, 9.4)

        assert raw_low == pytest.approx(-1.2)

    def test_a_band_clear_of_the_floor_is_untouched(self) -> None:
        cell, raw_low = interval_cell(30.0, 9.4)

        assert cell == "20.6–39.4"
        assert raw_low == pytest.approx(20.6)

    def test_the_truncation_is_stated_when_it_happens(self) -> None:
        notes = band_notes(
            horizon=48,
            half_width=9.4,
            nominal=0.8,
            calibration_rows=40987,
            raw_low=-1.2,
        )

        truncation = [n for n in notes if "已截在 0" in n]
        assert len(truncation) == 1
        assert "-1.2" in truncation[0]
        assert "少一些" in truncation[0], "it must say the coverage is reduced"

    def test_nothing_is_said_when_nothing_was_truncated(self) -> None:
        notes = band_notes(
            horizon=24,
            half_width=9.6,
            nominal=0.8,
            calibration_rows=41074,
            raw_low=0.7,
        )

        assert not [n for n in notes if "已截在 0" in n]


class TestALongerHorizonWithANarrowerBand:
    """The other defect, and it is a property of the data rather than a slip.

    At 48 hours the calibration residuals have a tighter core and a heavier tail
    than at 24 — p50 4.82 against 5.37, p80 9.41 against 9.56, then p90 12.79
    against 12.27. The 80th percentile genuinely crosses over, so the band a
    reader sees inverts while the uncertainty does not.

    Raising the level to 90% would have made the ordering monotonic and the
    display tidy. That is loosening a measurement to make a picture agree with an
    expectation, which this repository's own gate comments refuse by name. The
    page states the crossover instead.
    """

    def test_the_crossover_is_reported(self) -> None:
        notes = band_notes(
            horizon=24,
            half_width=9.56,
            nominal=0.8,
            calibration_rows=41074,
            raw_low=0.7,
            percentiles={"90": 12.27},
            longer_horizons={48: (9.41, {"90": 12.79})},
        )

        crossover = [n for n in notes if "**窄**" in n]
        assert len(crossover) == 1
        assert "48 小時" in crossover[0]
        assert "不是模型在更遠處更確定" in crossover[0]

    def test_it_names_the_tail_that_does_not_cross(self) -> None:
        """Saying only 「the band is narrower」 leaves the reader where they
        started. The p90 pair is the evidence that the ordering inverts at one
        percentile and not at the next."""
        notes = band_notes(
            horizon=24,
            half_width=9.56,
            nominal=0.8,
            calibration_rows=41074,
            raw_low=0.7,
            percentiles={"90": 12.27},
            longer_horizons={48: (9.41, {"90": 12.79})},
        )

        crossover = next(n for n in notes if "**窄**" in n)
        assert "12.3" in crossover and "12.8" in crossover

    def test_a_monotonic_ordering_says_nothing(self) -> None:
        """The note has to disappear by itself if the data stops inverting —
        otherwise it is a hard-coded caveat, not a reported property."""
        notes = band_notes(
            horizon=24,
            half_width=9.56,
            nominal=0.8,
            calibration_rows=41074,
            raw_low=0.7,
            longer_horizons={48: (11.95, {"90": 14.0})},
        )

        assert not [n for n in notes if "**窄**" in n]

    def test_the_longest_horizon_has_nothing_to_compare_against(self) -> None:
        notes = band_notes(
            horizon=48,
            half_width=9.41,
            nominal=0.8,
            calibration_rows=40987,
            raw_low=-1.2,
            longer_horizons={},
        )

        assert not [n for n in notes if "**窄**" in n]

    def test_the_nearest_inverting_horizon_is_the_one_named(self) -> None:
        """With more than one narrower horizon further out, the closest is the
        useful comparison — it is the one a reader would reach for next."""
        notes = band_notes(
            horizon=6,
            half_width=12.0,
            nominal=0.8,
            calibration_rows=1000,
            raw_low=1.0,
            longer_horizons={24: (11.0, {"90": 15.0}), 48: (10.0, {"90": 16.0})},
        )

        crossover = next(n for n in notes if "**窄**" in n)
        assert "24 小時" in crossover
        assert "48 小時" not in crossover


class TestTheLevelIsAskedForRatherThanGuaranteed:
    """Every band carries this, whatever else is true of it.

    The backtest measured 76.4%–84.6% against a nominal 80%, with exactly one
    split below nominal at every horizon and the same split each time. The Space
    calibrates on the tail of its own training period and has no held-out data
    left to measure against, so a page showing an 80% band without saying that is
    printing a level nothing checked.
    """

    @pytest.mark.parametrize("raw_low", [-1.2, 0.7])
    @pytest.mark.parametrize("longer", [None, {48: (9.41, {"90": 12.79})}])
    def test_the_caveat_is_always_last_and_always_present(
        self, raw_low: float, longer: dict[int, tuple[float, dict[str, float]]] | None
    ) -> None:
        notes = band_notes(
            horizon=24,
            half_width=9.56,
            nominal=0.8,
            calibration_rows=41074,
            raw_low=raw_low,
            percentiles={"90": 12.27},
            longer_horizons=longer,
        )

        assert "要求的，不是保證的" in notes[-1]
        assert "四個分割中有一個" in notes[-1]

    def test_the_first_note_states_the_level_and_where_it_came_from(self) -> None:
        notes = band_notes(
            horizon=24,
            half_width=9.56,
            nominal=0.8,
            calibration_rows=41074,
            raw_low=0.7,
        )

        assert "80%" in notes[0]
        assert "±9.6" in notes[0]
        assert "41,074" in notes[0], "the calibration size is what the level rests on"
