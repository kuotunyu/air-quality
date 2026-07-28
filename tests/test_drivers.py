"""Tests for M2 — the hourly redo.

These pin the design choices that separate M2 from the 2018 method. Each one
was a defect in the original, so a regression here would quietly restore it.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path

import polars as pl
import pytest

from twair.analysis.drivers import (
    FEATURE_SETS,
    POLLUTANTS,
    TARGET,
    build_modelling_frame,
)
from twair.qc.flags import Flag


def _store(tmp_path: Path, hours: int = 48, stations: Sequence[str] = ("二林", "關山")) -> Path:
    from twair.store.writer import write_observations

    start = datetime(2015, 6, 1)
    rows = []
    for station in stations:
        for h in range(hours):
            ts = start + timedelta(hours=h)
            for i, pollutant in enumerate(POLLUTANTS):
                rows.append((station, pollutant, ts, 10.0 + i + (h % 7), Flag.VALID.value))

    write_observations(
        pl.DataFrame(
            {
                "station_name": [r[0] for r in rows],
                "pollutant": [r[1] for r in rows],
                "ts_local": [r[2] for r in rows],
                "value": [r[3] for r in rows],
                "flag": [r[4] for r in rows],
                "value_retained": [False] * len(rows),
                "generation": ["test"] * len(rows),
                "source_member": ["test.csv"] * len(rows),
            }
        ),
        root=tmp_path,
    )
    return tmp_path


class TestFeatureSets:
    def test_pm10_is_absent_from_the_honest_specification(self) -> None:
        """PM2.5 is a subset of PM10; predicting one from the other is a leak."""
        assert "PM10" not in FEATURE_SETS["full"]

    def test_a_leaking_set_exists_only_to_price_the_leak(self) -> None:
        full = set(FEATURE_SETS["full"])
        leaking = set(FEATURE_SETS["full_with_pm10"])

        assert leaking - full == {"PM10"}, "the two must differ by PM10 and nothing else"

    def test_no_no2_and_nox_are_never_all_used_together(self) -> None:
        """NO + NO2 = NOx exactly. All three makes the design singular."""
        for name, features in FEATURE_SETS.items():
            present = {"NO", "NO2", "NOx"} & set(features)
            assert len(present) <= 1, f"{name} carries collinear nitrogen terms: {present}"

    def test_the_honest_set_uses_encoded_wind_not_the_bearing(self) -> None:
        full = set(FEATURE_SETS["full"])

        assert "WD_HR" not in full
        assert {"wd_sin", "wd_cos", "u", "v"} <= full

    def test_a_raw_wind_set_is_kept_for_the_m3_contrast(self) -> None:
        raw = set(FEATURE_SETS["full_raw_wind"])

        assert "WD_HR" in raw
        assert "wd_sin" not in raw

    def test_the_honest_set_carries_time_of_day(self) -> None:
        """Monthly averaging erased the daily cycle; it is restored here."""
        assert {"hour_sin", "hour_cos"} <= set(FEATURE_SETS["full"])

    def test_the_target_is_never_a_predictor(self) -> None:
        for name, features in FEATURE_SETS.items():
            assert TARGET not in features, f"{name} predicts the target from itself"


class TestModellingFrame:
    def test_frame_is_hourly_not_aggregated(self, tmp_path: Path) -> None:
        """The single biggest difference from the original."""
        root = _store(tmp_path, hours=48)

        frame = build_modelling_frame(root, period=(2015, 2015))

        assert frame.height == 96, "2 stations x 48 hours"
        assert frame["ts_local"].dt.hour().n_unique() > 1

    def test_every_declared_feature_exists(self, tmp_path: Path) -> None:
        root = _store(tmp_path)

        frame = build_modelling_frame(root, period=(2015, 2015))

        for name, features in FEATURE_SETS.items():
            missing = [f for f in features if f not in frame.columns]
            assert not missing, f"{name} needs {missing}"

    def test_rows_without_the_target_are_dropped(self, tmp_path: Path) -> None:
        from twair.store.writer import write_observations

        root = tmp_path
        write_observations(
            pl.DataFrame(
                {
                    "station_name": ["二林"] * 2,
                    "pollutant": ["O3", "NO2"],
                    "ts_local": [datetime(2015, 6, 1, 0)] * 2,
                    "value": [30.0, 12.0],
                    "flag": [Flag.VALID.value] * 2,
                    "value_retained": [False] * 2,
                    "generation": ["test"] * 2,
                    "source_member": ["t.csv"] * 2,
                }
            ),
            root=root,
        )

        assert build_modelling_frame(root, period=(2015, 2015)).is_empty()

    def test_period_defaults_to_the_original_window(self, tmp_path: Path) -> None:
        """M1 and M2 must be compared on the same years, not different data."""
        import inspect

        default = inspect.signature(build_modelling_frame).parameters["period"].default

        assert default == (2010, 2017)

    def test_station_filter(self, tmp_path: Path) -> None:
        root = _store(tmp_path)

        frame = build_modelling_frame(root, period=(2015, 2015), stations=["二林"])

        assert frame["station_name"].unique().to_list() == ["二林"]

    def test_chemistry_ratios_are_computed(self, tmp_path: Path) -> None:
        root = _store(tmp_path)

        frame = build_modelling_frame(root, period=(2015, 2015))

        assert frame["ox"].null_count() == 0
        assert frame["no2_nox_ratio"].null_count() == 0

    def test_wind_features_are_finite(self, tmp_path: Path) -> None:
        root = _store(tmp_path)

        frame = build_modelling_frame(root, period=(2015, 2015))

        for column in ("wd_sin", "wd_cos", "u", "v"):
            assert frame[column].is_finite().all()

    def test_pm_ratio_is_available_even_though_pm10_is_not_a_predictor(
        self, tmp_path: Path
    ) -> None:
        """The ratio is a source fingerprint, not a leak."""
        root = _store(tmp_path)

        frame = build_modelling_frame(root, period=(2015, 2015))

        assert "pm_ratio" in frame.columns
        assert "pm_ratio" not in FEATURE_SETS["full"], "not a predictor by default either"


def test_unknown_feature_set_is_rejected() -> None:
    with pytest.raises(KeyError):
        FEATURE_SETS["does_not_exist"]
