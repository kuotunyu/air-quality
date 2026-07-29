"""Tests for M10 — the attributable fraction and what it depends on.

Two things are being defended here.

The arithmetic, because an exposure-response function is short enough to look
obviously right and still be wrong in a direction nobody checks — a missing
clip turns clean air into a protective effect, and a sign error would go
unnoticed because the output is a plausible small number either way.

And the provenance gate. A coefficient with no citation cannot be told apart
from one that was invented, and this project has already caught five fabricated
sources in one delegated batch.
"""

from __future__ import annotations

import math

import polars as pl
import pytest

from twair.analysis.health import (
    attributable_fraction,
    load_counterfactuals,
    load_response_functions,
    paf_expr,
    relative_risk,
    sensitivity_grid,
)

# ln(1.08)/10 — the shipped central estimate, spelled out so a change to the
# config shows up here as a deliberate edit rather than a silent drift.
BETA_1_08 = math.log(1.08) / 10.0


def _function(**overrides: object) -> dict[str, object]:
    base = {
        "rr_per_10": 1.08,
        "rr_per_10_low": 1.06,
        "rr_per_10_high": 1.09,
        "outcome": "all-cause mortality, adults",
        "source": "A real paper",
        "source_url": "https://doi.org/10.1000/example",
    }
    return {**base, **overrides}


class TestTheArithmetic:
    def test_exposure_at_the_counterfactual_attributes_nothing(self) -> None:
        assert attributable_fraction(5.0, beta=BETA_1_08, counterfactual=5.0) == 0.0

    def test_ten_above_the_counterfactual_gives_the_published_relative_risk(self) -> None:
        """The whole coefficient, checked against the number it came from."""
        rr = relative_risk(15.0, beta=BETA_1_08, counterfactual=5.0)

        assert rr == pytest.approx(1.08)

    def test_clean_air_is_not_protective(self) -> None:
        """The clip, which is the one place a sign error would hide.

        Without it a station below the counterfactual returns RR < 1, and the
        attributable fraction goes negative — reading as air pollution having
        saved lives, in a table where every other row is a burden.
        """
        assert attributable_fraction(1.0, beta=BETA_1_08, counterfactual=5.9) == 0.0
        assert relative_risk(0.0, beta=BETA_1_08, counterfactual=2.4) == 1.0

    def test_the_fraction_rises_with_concentration(self) -> None:
        low = attributable_fraction(10.0, beta=BETA_1_08, counterfactual=2.4)
        high = attributable_fraction(30.0, beta=BETA_1_08, counterfactual=2.4)

        assert 0 < low < high < 1

    def test_a_higher_counterfactual_attributes_less(self) -> None:
        """The finding the chapter is built on, as an assertion.

        Both counterfactuals are endpoints of one published TMREL range, and
        the choice between them moves the answer substantially.
        """
        lenient = attributable_fraction(13.0, beta=BETA_1_08, counterfactual=5.9)
        strict = attributable_fraction(13.0, beta=BETA_1_08, counterfactual=2.4)

        assert strict > lenient
        assert (strict - lenient) / strict > 0.25, "the assumption should move it a lot"

    def test_the_fraction_never_reaches_one(self) -> None:
        """(RR-1)/RR is bounded below 1 for any finite exposure."""
        assert attributable_fraction(500.0, beta=BETA_1_08, counterfactual=0.0) < 1.0

    def test_cleaner_air_makes_the_assumption_matter_more(self) -> None:
        """M10's actual finding, as a property rather than a measurement.

        The excess over the counterfactual shrinks toward the counterfactual,
        so the gap between two candidate counterfactuals takes up more of what
        is left. Measured on the real panel it goes from 17% of the estimate in
        2006 to 45% in 2025; here it is checked as the monotone relationship
        that produces it, which cannot drift with a data refresh.
        """

        def relative_spread(concentration: float) -> float:
            strict = attributable_fraction(concentration, beta=BETA_1_08, counterfactual=2.4)
            lenient = attributable_fraction(concentration, beta=BETA_1_08, counterfactual=5.9)
            return (strict - lenient) / strict

        dirty = relative_spread(32.0)
        clean = relative_spread(13.0)

        assert clean > dirty, "the assumption should take a bigger share as air improves"
        assert dirty < 0.25 < clean


class TestTheExpressionAgreesWithTheFormula:
    """A fast path that drifts from the readable one has bitten this repo."""

    @pytest.mark.parametrize("concentration", [0.0, 2.4, 5.0, 13.2, 28.0, 45.0])
    def test_they_agree_at_every_concentration(self, concentration: float) -> None:
        frame = pl.DataFrame({"annual_mean": [concentration]})

        vectorised = frame.select(
            paf_expr("annual_mean", beta=BETA_1_08, counterfactual=2.4)
        ).item()
        scalar = attributable_fraction(concentration, beta=BETA_1_08, counterfactual=2.4)

        assert vectorised == pytest.approx(scalar)


class TestProvenanceGate:
    def test_a_function_without_a_source_is_refused(self) -> None:
        conf = {"response_functions": {"invented": _function(source="")}}

        with pytest.raises(ValueError, match="no source"):
            load_response_functions(conf)

    def test_a_source_that_is_not_a_url_is_refused(self) -> None:
        conf = {"response_functions": {"vague": _function(source_url="see the literature")}}

        with pytest.raises(ValueError, match="not a URL"):
            load_response_functions(conf)

    def test_an_interval_that_excludes_its_own_central_estimate_is_refused(self) -> None:
        conf = {"response_functions": {"wrong": _function(rr_per_10=1.20)}}

        with pytest.raises(ValueError, match="outside its own"):
            load_response_functions(conf)

    def test_a_counterfactual_without_a_rationale_is_refused(self) -> None:
        """The rationale is the point — an unexplained threshold is a guess."""
        conf = {"counterfactuals": {"mystery": {"value": 4.0}}}

        with pytest.raises(ValueError, match="no rationale"):
            load_counterfactuals(conf)

    def test_a_negative_counterfactual_is_refused(self) -> None:
        conf = {"counterfactuals": {"impossible": {"value": -1.0, "why": "typo"}}}

        with pytest.raises(ValueError, match="negative"):
            load_counterfactuals(conf)

    def test_the_shipped_config_passes_its_own_gate(self) -> None:
        functions = load_response_functions()
        counterfactuals = load_counterfactuals()

        assert functions, "conf/health.yaml declares no response function"
        assert len(counterfactuals) >= 2, "a sensitivity analysis needs alternatives"
        for function in functions.values():
            assert function.source_url.startswith("http")

    def test_the_shipped_config_carries_no_death_counts(self) -> None:
        """M10 reports fractions. A count would need a population this
        repository does not have, and the population term would dominate the
        error while looking like the solid part of the sentence."""
        from twair.config import load_conf

        text = str(load_conf("health")).lower()
        assert "mortality_rate" not in text
        assert "population" not in text


class TestSensitivityGrid:
    def test_every_combination_appears(self) -> None:
        annual = pl.DataFrame(
            {"station_name": ["甲", "乙"], "year": [2024, 2024], "annual_mean": [13.0, 20.0]}
        )
        functions = load_response_functions({"response_functions": {"f": _function()}})
        counterfactuals = load_counterfactuals(
            {
                "counterfactuals": {
                    "a": {"value": 2.4, "why": "low end"},
                    "b": {"value": 5.9, "why": "high end"},
                }
            }
        )

        grid = sensitivity_grid(annual, functions=functions, counterfactuals=counterfactuals)

        # 2 stations x 1 function x 3 bounds x 2 counterfactuals
        assert grid.height == 12
        assert set(grid["bound"].unique()) == {"central", "low", "high"}

    def test_the_grid_keeps_the_assumption_beside_the_number(self) -> None:
        """A PAF without its counterfactual is not interpretable."""
        annual = pl.DataFrame({"station_name": ["甲"], "year": [2024], "annual_mean": [13.0]})
        functions = load_response_functions({"response_functions": {"f": _function()}})
        counterfactuals = load_counterfactuals(
            {"counterfactuals": {"a": {"value": 2.4, "why": "low end"}}}
        )

        grid = sensitivity_grid(annual, functions=functions, counterfactuals=counterfactuals)

        for column in ("function", "bound", "counterfactual", "counterfactual_ugm3"):
            assert column in grid.columns
