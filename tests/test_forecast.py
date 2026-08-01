"""Tests for M9 — forecasting scored against persistence.

The metric is the subject here. A model can post a magnificent R² and still be
worse than a one-line rule, and these check that the reporting makes that
visible rather than hiding it behind a number that looks good.
"""

from __future__ import annotations

import numpy as np
import polars as pl
import pytest

from twair.models.forecast import ForecastScore, skill_score, summarise_scores


def _score(**overrides: object) -> ForecastScore:
    base = {
        "horizon": 24,
        "split": "rolling_1",
        "n": 1000,
        "stations": 74,
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


def _splits(*skills: float, horizon: int = 6) -> pl.DataFrame:
    """Four splits at one horizon, differing only in skill."""
    return pl.DataFrame(
        [
            {
                **_score(horizon=horizon, split=f"rolling_{i}", skill_vs_persistence=s).as_dict(),
            }
            for i, s in enumerate(skills, start=1)
        ]
    )


class TestTheMeanDoesNotHideALosingSplit:
    """The real backtest's first summary said "4/4 beat persistence" while
    `rolling_1` sat at -0.111 at six hours. A mean over splits hides a losing
    split exactly the way an R² hides a losing model — one level up, same
    mistake, so it gets the same treatment."""

    def test_the_worst_split_survives_the_aggregation(self) -> None:
        summary = summarise_scores(_splits(-0.111, 0.271, 0.298, 0.303))

        assert summary["skill_vs_persistence"][0] == pytest.approx(0.19025)
        assert summary["skill_worst_split"][0] == pytest.approx(-0.111)

    def test_a_positive_mean_still_reports_the_losing_split(self) -> None:
        summary = summarise_scores(_splits(-0.111, 0.271, 0.298, 0.303))

        assert summary["skill_vs_persistence"][0] > 0, "the mean looks fine"
        assert summary["splits_not_beating_persistence"][0] == 1, "and one split did not"

    def test_all_splits_winning_counts_none(self) -> None:
        summary = summarise_scores(_splits(0.131, 0.188, 0.179, 0.201))

        assert summary["splits_not_beating_persistence"][0] == 0
        assert summary["skill_worst_split"][0] == pytest.approx(0.131)

    def test_the_climatology_baseline_is_summarised_too(self) -> None:
        """Skill against persistence rises with horizon while skill against
        climatology falls. Reporting only the first reads as "better the
        further out you go", which is the opposite of what happens."""
        summary = summarise_scores(_splits(0.243))

        assert "skill_vs_climatology" in summary.columns
        assert "skill_vs_climatology_worst" in summary.columns

    def test_horizons_stay_separate_rows(self) -> None:
        both = pl.concat([_splits(0.175, horizon=1), _splits(0.243, horizon=48)])

        summary = summarise_scores(both)

        assert summary["horizon"].to_list() == [1, 48]


class TestTheStationCountTravels:
    """Chapter 8 said 「74 站」 as a literal, and nothing regenerated it.

    The count moves whenever the feature frame does — it moved when the
    station-boundary leak in `features/lags.py` was fixed and a station's first
    167 hours stopped being filled with the previous station's week. A number
    written into prose beside numbers that are all derived is the one that goes
    quietly wrong.
    """

    def test_the_summary_carries_the_widest_split_not_the_sum(self) -> None:
        """The same stations recur across splits; summing them counts 74 four times."""
        scores = pl.DataFrame(
            [
                _score(split="rolling_1", stations=70).as_dict(),
                _score(split="rolling_2", stations=74).as_dict(),
                _score(split="rolling_3", stations=73).as_dict(),
            ]
        )

        summary = summarise_scores(scores)

        assert summary["stations"].to_list() == [74]

    def test_each_horizon_reports_its_own_count(self) -> None:
        """A longer horizon drops more rows and can drop a whole station with them."""
        scores = pl.DataFrame(
            [
                _score(horizon=1, stations=74).as_dict(),
                _score(horizon=48, stations=71).as_dict(),
            ]
        )

        summary = summarise_scores(scores).sort("horizon")

        assert summary["stations"].to_list() == [74, 71]
