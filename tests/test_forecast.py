"""Tests for M9 — forecasting scored against persistence.

The metric is the subject here. A model can post a magnificent R² and still be
worse than a one-line rule, and these check that the reporting makes that
visible rather than hiding it behind a number that looks good.
"""

from __future__ import annotations

import numpy as np
import pytest

from twair.models.forecast import ForecastScore, skill_score


def _score(**overrides: object) -> ForecastScore:
    base = {
        "horizon": 24,
        "split": "rolling_1",
        "n": 1000,
        "model_rmse": 8.0,
        "persistence_rmse": 9.0,
        "climatology_rmse": 12.0,
        "model_r2": 0.5,
        "skill_vs_persistence": 0.21,
        "skill_vs_climatology": 0.55,
    }
    return ForecastScore(**{**base, **overrides})  # type: ignore[arg-type]


class TestSkillScore:
    def test_matching_the_baseline_scores_zero(self) -> None:
        errors = np.array([1.0, -2.0, 0.5, 3.0])

        assert skill_score(errors, errors) == pytest.approx(0.0)

    def test_halving_the_squared_error_scores_one_half(self) -> None:
        baseline = np.array([2.0, 2.0, 2.0, 2.0])
        model = baseline / np.sqrt(2)

        assert skill_score(model, baseline) == pytest.approx(0.5)

    def test_a_perfect_model_scores_one(self) -> None:
        assert skill_score(np.zeros(10), np.ones(10)) == pytest.approx(1.0)

    def test_losing_to_the_baseline_scores_negative(self) -> None:
        """The result the headline metric exists to make impossible to hide."""
        assert skill_score(np.full(10, 2.0), np.full(10, 1.0)) < 0

    def test_a_flawless_baseline_is_undefined_rather_than_infinite(self) -> None:
        assert np.isnan(skill_score(np.ones(5), np.zeros(5)))


class TestWhatTheHeadlineNumberSays:
    def test_a_high_r2_does_not_mean_the_model_helped(self) -> None:
        """The exact confusion this module is built around.

        An hour-ahead PM2.5 forecast scores R² 0.87 because PM2.5 an hour out
        is very predictable — by anyone, including a rule that says "the same".
        Only skill distinguishes the model from the rule.
        """
        impressive_but_useless = _score(model_r2=0.87, skill_vs_persistence=-0.05)

        assert impressive_but_useless.model_r2 > 0.85
        assert not impressive_but_useless.beats_persistence

    def test_a_low_r2_can_still_be_a_real_improvement(self) -> None:
        modest_but_useful = _score(model_r2=0.32, skill_vs_persistence=0.18)

        assert modest_but_useful.model_r2 < 0.4
        assert modest_but_useful.beats_persistence

    def test_the_verdict_travels_with_every_row(self) -> None:
        """A reader should not have to compute the comparison themselves."""
        assert "beats_persistence" in _score().as_dict()
        assert "skill_vs_persistence" in _score().as_dict()

    def test_exactly_matching_persistence_does_not_count_as_beating_it(self) -> None:
        assert not _score(skill_vs_persistence=0.0).beats_persistence


class TestHorizons:
    def test_the_default_horizons_span_useful_to_hopeless(self) -> None:
        from twair.models.forecast import HORIZONS

        assert 1 in HORIZONS, "where persistence is hardest to beat"
        assert 24 in HORIZONS, "where a forecast starts being useful to a person"
        assert max(HORIZONS) >= 48

    def test_scores_are_keyed_by_horizon_so_they_cannot_be_averaged_away(self) -> None:
        """Collapsing horizons would hide that R² and skill move oppositely."""
        assert "horizon" in _score().as_dict()
