"""Tests for M5 — event studies and the placebos that police them.

The failure this module is built against is not a crash. It is an estimate of
-0.9 µg/m³ that looks like a policy working, when the same procedure returns
-0.7 in every year the policy did not exist. So most of these tests are about
the placebo machinery refusing to let that through.
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from twair.analysis import causal
from twair.analysis.causal import EventEffect, _attach_placebos, build_daily_frame, counterfactual


def _effect(**overrides: object) -> EventEffect:
    base = {
        "event": "e",
        "station": "s",
        "n_days": 69,
        "observed_mean": 10.0,
        "predicted_mean": 11.0,
        "effect": -1.0,
        "effect_pct": -9.0,
        "ci_low": -1.5,
        "ci_high": -0.5,
        "holdout_rmse": 5.0,
        "placebo_mean": float("nan"),
        "placebo_sd": float("nan"),
        "placebo_n": 0,
        "z_against_placebo": float("nan"),
    }
    return EventEffect(**{**base, **overrides})  # type: ignore[arg-type]


def _synthetic_station(
    *,
    effect_window: tuple[date, date] | None = None,
    effect_size: float = 0.0,
    years: tuple[int, int] = (2010, 2024),
    seed: int = 0,
) -> pl.DataFrame:
    """A station whose true event effect is known because it was put in by hand.

    PM2.5 is a seasonal cycle plus a wind term plus noise. If `effect_window` is
    given, `effect_size` is added inside it and nowhere else.
    """
    rng = np.random.default_rng(seed)
    start = date(years[0], 1, 1)
    n = (date(years[1], 12, 31) - start).days + 1
    days = [start + timedelta(days=i) for i in range(n)]

    doy = np.array([d.timetuple().tm_yday for d in days], dtype=float)
    wind = rng.uniform(0.5, 5.0, n)
    seasonal = 20.0 + 8.0 * np.cos(2 * np.pi * doy / 365.25)
    pm = seasonal - 2.0 * wind + rng.normal(0, 3.0, n)

    if effect_window is not None:
        lo, hi = effect_window
        inside = np.array([lo <= d <= hi for d in days])
        pm = pm + inside * effect_size

    frame = pl.DataFrame(
        {
            "station_name": ["合成站"] * n,
            "date": days,
            "PM2.5": pm,
            "AMB_TEMP": 25.0 - 5.0 * np.cos(2 * np.pi * doy / 365.25),
            "RH": rng.uniform(60, 90, n),
            "RAINFALL": rng.exponential(2.0, n),
            "WS_HR": wind,
            "WD_HR": rng.uniform(0, 360, n),
        }
    ).with_columns(pl.col("date").cast(pl.Date))

    radians = pl.col("WD_HR") * (np.pi / 180.0)
    doy_expr = pl.col("date").dt.ordinal_day()
    dow_expr = pl.col("date").dt.weekday()
    return frame.with_columns(
        radians.sin().alias("wd_sin"),
        radians.cos().alias("wd_cos"),
        (doy_expr * (2 * np.pi / 365.25)).sin().alias("doy_sin"),
        (doy_expr * (2 * np.pi / 365.25)).cos().alias("doy_cos"),
        (dow_expr * (2 * np.pi / 7.0)).sin().alias("dow_sin"),
        (dow_expr * (2 * np.pi / 7.0)).cos().alias("dow_cos"),
        (pl.col("date") - pl.lit(date(1982, 1, 1))).dt.total_days().alias("trend_days"),
    )


class TestRecoveringAKnownEffect:
    def test_a_large_injected_drop_is_recovered(self) -> None:
        window = (date(2021, 5, 19), date(2021, 7, 26))
        frame = _synthetic_station(effect_window=window, effect_size=-8.0)

        result = counterfactual(
            frame, event_name="injected", station="合成站", start=window[0], end=window[1]
        )

        assert result is not None
        assert result.effect == pytest.approx(-8.0, abs=1.5)

    def test_no_injected_effect_yields_approximately_zero(self) -> None:
        frame = _synthetic_station(effect_size=0.0)

        result = counterfactual(
            frame,
            event_name="nothing",
            station="合成站",
            start=date(2021, 5, 19),
            end=date(2021, 7, 26),
        )

        assert result is not None
        assert abs(result.effect) < 2.0, "a quiet window must not manufacture an effect"


class TestPlaceboGate:
    def test_an_effect_inside_the_placebo_spread_is_not_credible(self) -> None:
        """The whole point. A precise estimate is not a correct one."""
        result = _attach_placebos(_effect(effect=-1.0), [-0.8, -0.6, -1.2, -0.4, -0.9])

        assert result.placebo_n == 5
        assert not result.credible

    def test_an_effect_far_outside_the_placebo_spread_is_credible(self) -> None:
        result = _attach_placebos(_effect(effect=-9.0), [-0.8, -0.6, -1.2, -0.4, -0.9])

        assert result.credible

    def test_a_tight_confidence_interval_does_not_make_an_effect_credible(self) -> None:
        """A model that misfits every May misfits this May precisely too."""
        result = _attach_placebos(
            _effect(effect=-1.0, ci_low=-1.05, ci_high=-0.95), [-0.9, -1.1, -1.0, -0.95]
        )

        assert result.ci_high < 0, "the interval excludes zero"
        assert not result.credible, "but the placebos find the same thing every year"

    def test_too_few_placebos_leaves_the_verdict_unknown_not_true(self) -> None:
        result = _attach_placebos(_effect(effect=-9.0), [-0.5, -0.6])

        assert result.placebo_n == 2
        assert np.isnan(result.z_against_placebo)
        assert not result.credible, "unchecked must not read as confirmed"

    def test_identical_placebos_do_not_divide_by_zero(self) -> None:
        result = _attach_placebos(_effect(effect=-1.0), [-0.5, -0.5, -0.5, -0.5])

        assert np.isnan(result.z_against_placebo)
        assert not result.credible


class TestTrendBreak:
    def _decelerating(self, station: str = "合成站") -> pl.DataFrame:
        """A decline that flattens smoothly, with no break anywhere.

        This is what diminishing returns look like, and it is the shape that
        makes a naive breakpoint test fire at every date you hand it.
        """
        months = [date(2006, 1, 1) + timedelta(days=30 * i) for i in range(240)]
        years = np.arange(240) / 12.0
        # Concave: steep early, flat late. No discontinuity.
        values = 30.0 - 12.0 * np.sqrt(years)
        return pl.DataFrame(
            {
                "station_name": [station] * 240,
                "month": months,
                "normalised": values,
                "observed": values,
            }
        ).with_columns(pl.col("month").cast(pl.Date))

    def test_a_smoothly_decelerating_decline_shows_no_break(self) -> None:
        """The correctness property that a zero-centred z-score gets wrong.

        Every candidate break date in a concave series yields a positive delta,
        so comparing delta against zero reports a break everywhere. Centring on
        the placebo mean is what makes this return "nothing here".
        """
        monthly = self._decelerating()

        result = causal.trend_break(
            monthly, event_name="none", station="合成站", when=date(2018, 8, 1)
        )

        assert result is not None
        assert result.delta > 0, "the slope really does flatten — that is the trap"
        assert result.placebo_mean > 0, "and it flattens at every other date too"
        assert not result.credible, "so this date is not special"

    def test_a_real_slope_change_is_detected(self) -> None:
        months = [date(2006, 1, 1) + timedelta(days=30 * i) for i in range(240)]
        years = np.arange(240) / 12.0
        break_at = 12.5  # roughly mid-2018
        values = np.where(
            years < break_at, 30.0 - 0.3 * years, 30.0 - 0.3 * break_at - 3.0 * (years - break_at)
        )
        monthly = pl.DataFrame(
            {
                "station_name": ["合成站"] * 240,
                "month": months,
                "normalised": values,
                "observed": values,
            }
        ).with_columns(pl.col("month").cast(pl.Date))

        result = causal.trend_break(
            monthly, event_name="real", station="合成站", when=date(2018, 8, 1)
        )

        assert result is not None
        assert result.delta < -2.0, "the decline steepened sharply"
        assert result.credible

    def test_too_short_a_series_yields_nothing(self) -> None:
        monthly = self._decelerating().head(20)

        assert (
            causal.trend_break(monthly, event_name="e", station="合成站", when=date(2018, 8, 1))
            is None
        )


class TestWindowHandling:
    def test_the_buffer_keeps_the_run_up_out_of_training(self) -> None:
        """Behaviour changed before the announcement and after the lifting.

        Training on those days would teach the model that they are normal, and
        the contrast would shrink toward nothing.
        """
        assert causal.DEFAULT_BUFFER_DAYS >= 14

    def test_a_window_shorter_than_two_weeks_is_refused(self) -> None:
        frame = _synthetic_station()

        result = counterfactual(
            frame,
            event_name="tiny",
            station="合成站",
            start=date(2021, 5, 19),
            end=date(2021, 5, 25),
        )

        assert result is None

    def test_an_unknown_station_yields_nothing_rather_than_raising(self) -> None:
        frame = _synthetic_station()

        assert (
            counterfactual(
                frame,
                event_name="e",
                station="不存在的站",
                start=date(2021, 5, 19),
                end=date(2021, 7, 26),
            )
            is None
        )


class TestFeatureSet:
    def test_chemistry_is_excluded_from_the_counterfactual(self) -> None:
        """NOx and CO fall during a lockdown too.

        Letting the model see them would explain the effect away using its own
        consequence, and return approximately zero for any real event.
        """
        for excluded in ("NOx", "CO", "SO2", "NO2", "O3", "PM10"):
            assert excluded not in causal.DAILY_FEATURES

    def test_wind_direction_enters_as_sin_and_cos(self) -> None:
        assert "wd_sin" in causal.DAILY_FEATURES
        assert "wd_cos" in causal.DAILY_FEATURES
        assert "WD_HR" not in causal.DAILY_FEATURES


class TestDailyFrame:
    def test_a_missing_daily_aggregate_is_reported_not_invented(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        with pytest.raises(FileNotFoundError, match="twair aggregate"):
            build_daily_frame(tmp_path)
