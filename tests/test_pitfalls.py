"""Tests for M3 — the paired methodological demonstrations.

Each function claims something about the 2018 method. These tests check that
the demonstration actually demonstrates it, on data where the answer is known
in advance. A demonstration that would look convincing whatever the truth is
worth nothing.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

import polars as pl
import pytest

from twair.analysis.pitfalls import (
    collinearity_instability,
    diurnal_cycle_lost_to_monthly_means,
    normality_remedy_does_not_work_on_its_own_numbers,
    normality_test_fallacy,
    wind_direction_linearisation,
)
from twair.qc.flags import Flag


def _store(tmp_path, rows):  # type: ignore[no-untyped-def]
    from twair.store.writer import write_observations

    write_observations(
        pl.DataFrame(
            {
                "station_name": [r[0] for r in rows],
                "pollutant": [r[1] for r in rows],
                "ts_local": [r[2] for r in rows],
                "value": [float(r[3]) for r in rows],
                "flag": [Flag.VALID.value] * len(rows),
                "value_retained": [False] * len(rows),
                "generation": ["test"] * len(rows),
                "source_member": ["t.csv"] * len(rows),
            }
        ),
        root=tmp_path,
    )
    return tmp_path


class TestNormalityFallacy:
    def test_genuinely_normal_data_passes_at_every_sample_size(self) -> None:
        """If the test tracked normality alone, this is what should happen."""
        result = normality_test_fallacy()
        normal = result.filter(pl.col("data") == "truly_normal")

        assert not normal["rejected_at_0.05"].any()

    def test_mildly_skewed_data_passes_when_small_and_fails_when_large(self) -> None:
        """Same distribution throughout — only N changes, and the verdict flips."""
        result = normality_test_fallacy().filter(pl.col("data") == "slightly_skewed")

        smallest = result.sort("n").row(0, named=True)
        largest = result.sort("n").row(-1, named=True)

        assert smallest["rejected_at_0.05"] is False
        assert largest["rejected_at_0.05"] is True

    def test_the_skew_is_present_at_every_size(self) -> None:
        """The data are non-normal throughout; only the test's verdict moves."""
        skewed = normality_test_fallacy().filter(pl.col("data") == "slightly_skewed")

        assert (skewed["skewness"] > 0.3).all()

    def test_lowering_the_threshold_does_not_rescue_the_large_sample(self) -> None:
        """The original's stated remedy, applied to a case where truth is known."""
        largest = (
            normality_test_fallacy()
            .filter(pl.col("data") == "slightly_skewed")
            .sort("n")
            .row(-1, named=True)
        )

        assert largest["rejected_at_0.01"] is True

    def test_result_is_reproducible(self) -> None:
        assert normality_test_fallacy(seed=7).equals(normality_test_fallacy(seed=7))


class TestNormalityRemedyAgainstThePublishedTable:
    def test_every_published_variable_is_rejected_at_both_thresholds(self) -> None:
        """p = .000 is below 0.01 as surely as below 0.05."""
        table = normality_remedy_does_not_work_on_its_own_numbers()

        assert table["rejected_at_0.05"].all()
        assert table["rejected_at_0.01"].all()

    def test_the_report_claim_is_contradicted_by_its_own_numbers(self) -> None:
        table = normality_remedy_does_not_work_on_its_own_numbers()

        contradicted = table.filter(
            pl.col("report_claims_not_rejected_at_0.01") & pl.col("rejected_at_0.01")
        )

        assert contradicted.height == table.height

    def test_all_thirteen_variables_are_covered(self) -> None:
        assert normality_remedy_does_not_work_on_its_own_numbers().height == 13


class TestDiurnalCycle:
    def test_monthly_averaging_destroys_a_daily_cycle(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """A pure daily sine averages to a constant over whole months."""
        start = datetime(2010, 1, 1)
        rows = [
            (
                "二林",
                "PM2.5",
                start + timedelta(hours=h),
                30.0 + 20.0 * math.sin(2 * math.pi * h / 24),
            )
            for h in range(24 * 365)
        ]
        root = _store(tmp_path, rows)

        result = diurnal_cycle_lost_to_monthly_means(root, period=(2010, 2010))

        retained = result["variance"].filter(pl.col("scale") == "monthly_mean")[
            "variance_retained"
        ][0]
        assert retained < 0.01, "a daily cycle must not survive monthly averaging"

    def test_the_diurnal_profile_recovers_the_cycle(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        start = datetime(2010, 1, 1)
        rows = [
            (
                "二林",
                "PM2.5",
                start + timedelta(hours=h),
                30.0 + 20.0 * math.sin(2 * math.pi * h / 24),
            )
            for h in range(24 * 60)
        ]
        root = _store(tmp_path, rows)

        diurnal = diurnal_cycle_lost_to_monthly_means(root, period=(2010, 2010))["diurnal"]

        assert diurnal.height == 24
        spread = diurnal["mean"].max() - diurnal["mean"].min()
        assert spread == pytest.approx(40.0, rel=0.05), "peak-to-trough of the input"


class TestWindLinearisation:
    def test_a_strong_directional_effect_yields_a_tiny_linear_correlation(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """Pollution from the north only: large sector effect, no linear signal.

        Bearings near 0 and near 360 are the same direction, so a linear
        correlation sees the high values at both ends of its scale and reports
        almost nothing.
        """
        start = datetime(2010, 1, 1)
        rows = []
        for h in range(24 * 200):
            bearing = (h * 7) % 360
            # High when the wind comes from the north, i.e. near 0 or 360.
            northerly = min(bearing, 360 - bearing) < 45
            pm = 60.0 if northerly else 15.0
            ts = start + timedelta(hours=h)
            rows += [
                ("二林", "WD_HR", ts, float(bearing)),
                ("二林", "WS_HR", ts, 2.0),
                ("二林", "PM2.5", ts, pm),
            ]
        root = _store(tmp_path, rows)

        result = wind_direction_linearisation(root, period=(2010, 2010))
        summary = dict(zip(result["summary"]["measure"], result["summary"]["value"], strict=True))

        assert abs(summary["pearson_r_with_raw_bearing"]) < 0.15, "linear method sees nothing"
        assert summary["sector_mean_range"] > 40.0, "sector view sees the whole effect"

    def test_sectors_cover_the_compass(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        start = datetime(2010, 1, 1)
        rows = []
        for h in range(24 * 200):
            ts = start + timedelta(hours=h)
            rows += [
                ("二林", "WD_HR", ts, float((h * 7) % 360)),
                ("二林", "WS_HR", ts, 2.0),
                ("二林", "PM2.5", ts, 20.0),
            ]
        root = _store(tmp_path, rows)

        by_sector = wind_direction_linearisation(root, period=(2010, 2010))["by_sector"]

        assert by_sector["sector"].to_list() == list(range(0, 360, 30))

    def test_a_bearing_of_exactly_360_lands_in_the_north_sector(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """360 and 0 are one direction, and must share one bin.

        `bearing // 30 * 30` gives 360 its own thirteenth sector. On the real
        store that split 3,341 readings away from the 285,527 at due north —
        the wraparound error this module was written to expose, committed
        inside it.
        """
        start = datetime(2010, 1, 1)
        rows = []
        for h in range(500):
            ts = start + timedelta(hours=h)
            rows += [
                ("二林", "WD_HR", ts, 360.0 if h % 2 else 0.0),
                ("二林", "WS_HR", ts, 2.0),
                ("二林", "PM2.5", ts, 20.0),
            ]
        root = _store(tmp_path, rows)

        by_sector = wind_direction_linearisation(root, period=(2010, 2010))["by_sector"]

        assert by_sector["sector"].to_list() == [0]
        assert by_sector["n"][0] == 500


class TestCollinearityInstability:
    def _store_with_identity(self, tmp_path, n: int = 3000):  # type: ignore[no-untyped-def]
        import numpy as np

        rng = np.random.default_rng(0)
        start = datetime(2010, 1, 1)
        no = rng.uniform(1, 30, n)
        no2 = rng.uniform(1, 40, n)
        o3 = rng.uniform(5, 60, n)
        pm = 10 + 0.3 * no2 + 0.2 * o3 + rng.normal(0, 3, n)

        rows = []
        for i in range(n):
            ts = start + timedelta(hours=i)
            rows += [
                ("二林", "NO", ts, no[i]),
                ("二林", "NO2", ts, no2[i]),
                ("二林", "NOx", ts, no[i] + no2[i]),  # the identity
                ("二林", "O3", ts, o3[i]),
                ("二林", "PM2.5", ts, pm[i]),
            ]
        return _store(tmp_path, rows)

    def test_the_identity_holds_in_the_data(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        root = self._store_with_identity(tmp_path)

        result = collinearity_instability(root, period=(2010, 2010), n_bootstrap=15)
        errors = dict(
            zip(
                result["identity_error"]["statistic"],
                result["identity_error"]["e"],
                strict=True,
            )
        )

        assert errors["mean"] < 1e-6, "NO + NO2 = NOx by construction here"

    def test_collinear_coefficients_swing_and_the_identified_one_does_not(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """The instability the original read as a finding about nitrogen."""
        root = self._store_with_identity(tmp_path)

        stability = collinearity_instability(root, period=(2010, 2010), n_bootstrap=25)["stability"]
        cv = dict(zip(stability["term"], stability["coefficient_of_variation"], strict=True))

        assert cv["O3"] < 0.1, "a well-identified predictor is stable across resamples"
        assert max(cv["NO"], cv["NO2"], cv["NOx"]) > cv["O3"] * 5

    def test_predictions_stay_good_while_coefficients_wander(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """The fit is fine; only the interpretation of individual terms is not."""
        root = self._store_with_identity(tmp_path)

        r2 = collinearity_instability(root, period=(2010, 2010), n_bootstrap=15)["r_squared"]
        values = dict(zip(r2["measure"], r2["value"], strict=True))

        assert values["r2_mean"] > 0.5
        assert values["r2_sd"] < 0.1, "predictions are stable even as coefficients are not"


class TestLeakagePrice:
    def test_reads_the_m2_scores_rather_than_refitting(self, tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        """The number quoted must be the one the comparison produced."""
        from twair.analysis import pitfalls

        outputs = tmp_path / "m2_drivers"
        outputs.mkdir(parents=True)
        pl.DataFrame(
            {
                "model": ["lightgbm"] * 2,
                "feature_set": ["full", "full_with_pm10"],
                "split_kind": ["rolling"] * 2,
                "split": ["rolling_1"] * 2,
                "n": [100, 100],
                "rmse": [14.0, 9.0],
                "mae": [10.0, 7.0],
                "r2": [0.4, 0.8],
                "exceedance_f1": [0.6, 0.85],
            }
        ).write_parquet(outputs / "scores.parquet")
        monkeypatch.setattr(pitfalls, "outputs_dir", lambda m=None: tmp_path / m, raising=False)
        monkeypatch.setattr("twair.paths.outputs_dir", lambda m=None: tmp_path / m)

        result = pitfalls.pm10_leakage_price()
        share = result.filter(pl.col("feature_set") == "leak_share_of_r2")["r2"][0]

        assert share == pytest.approx(0.5), "half the leaking model's R² comes from PM10"

    def test_missing_m2_output_is_reported_clearly(self, tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setattr("twair.paths.outputs_dir", lambda m=None: tmp_path / m)

        from twair.analysis.pitfalls import pm10_leakage_price

        with pytest.raises(FileNotFoundError, match="run_m2"):
            pm10_leakage_price()


class TestAllPitfallsRunner:
    def test_the_four_self_contained_demonstrations_survive_missing_m2(
        self, tmp_path, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        """One demonstration failing must not take the others with it."""
        monkeypatch.setattr("twair.paths.outputs_dir", lambda m=None: tmp_path / m)

        start = datetime(2010, 1, 1)
        rows = []
        for h in range(24 * 40):
            ts = start + timedelta(hours=h)
            rows += [
                ("二林", "PM2.5", ts, 20.0 + (h % 24)),
                ("二林", "WD_HR", ts, float((h * 11) % 360)),
                ("二林", "WS_HR", ts, 2.0),
                ("二林", "NO", ts, 5.0 + (h % 3)),
                ("二林", "NO2", ts, 12.0 + (h % 5)),
                ("二林", "NOx", ts, 17.0 + (h % 3) + (h % 5)),
                ("二林", "O3", ts, 30.0 + (h % 7)),
            ]
        root = _store(tmp_path / "store", rows)

        from twair.analysis.pitfalls import run_all_pitfalls

        tables = run_all_pitfalls(root, period=(2010, 2010))

        assert "normality.by_sample_size" in tables
        assert "diurnal.variance" in tables
        assert "wind.summary" in tables
        assert "collinearity.stability" in tables
        assert "leakage.price" not in tables, "no M2 output, so it is skipped not faked"
