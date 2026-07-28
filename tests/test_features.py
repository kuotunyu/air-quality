"""Tests for feature engineering.

The wind tests are the ones that matter most: they encode the difference
between a bearing treated as a number and a bearing treated as a direction.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

import polars as pl
import pytest

from twair.features.chem import add_chem_features
from twair.features.met import add_wind_features
from twair.features.temporal import (
    TREND_ORIGIN,
    add_temporal_features,
    cyclic_encoding,
)


def _wind(direction: float, speed: float = 1.0) -> dict[str, Any]:
    frame = add_wind_features(pl.DataFrame({"WD_HR": [direction], "WS_HR": [speed]}))
    return frame.row(0, named=True)


class TestWindDirectionEncoding:
    def test_north_and_just_below_north_are_adjacent(self) -> None:
        """1° and 359° differ by 2°, but as raw numbers they look 358 apart."""
        a, b = _wind(1.0), _wind(359.0)

        raw_gap = abs(1.0 - 359.0)
        encoded_gap = math.hypot(a["wd_sin"] - b["wd_sin"], a["wd_cos"] - b["wd_cos"])

        assert raw_gap == 358.0
        assert encoded_gap < 0.05

    def test_opposite_directions_are_far_apart(self) -> None:
        north, south = _wind(0.0), _wind(180.0)

        gap = math.hypot(north["wd_sin"] - south["wd_sin"], north["wd_cos"] - south["wd_cos"])

        assert gap == pytest.approx(2.0)

    def test_encoding_lies_on_the_unit_circle(self) -> None:
        for bearing in (0.0, 45.0, 137.0, 270.0, 359.9):
            row = _wind(bearing)
            assert row["wd_sin"] ** 2 + row["wd_cos"] ** 2 == pytest.approx(1.0)

    def test_cardinal_bearings(self) -> None:
        assert _wind(0.0)["wd_cos"] == pytest.approx(1.0)
        assert _wind(90.0)["wd_sin"] == pytest.approx(1.0)
        assert _wind(180.0)["wd_cos"] == pytest.approx(-1.0)
        assert _wind(270.0)["wd_sin"] == pytest.approx(-1.0)


class TestWindVector:
    def test_a_northerly_moves_air_southward(self) -> None:
        """0° means the wind comes *from* the north, so v must be negative."""
        row = _wind(0.0, speed=5.0)

        assert row["v"] == pytest.approx(-5.0)
        assert row["u"] == pytest.approx(0.0, abs=1e-9)

    def test_a_westerly_moves_air_eastward(self) -> None:
        row = _wind(270.0, speed=3.0)

        assert row["u"] == pytest.approx(3.0)
        assert row["v"] == pytest.approx(0.0, abs=1e-9)

    def test_vector_magnitude_equals_speed(self) -> None:
        row = _wind(137.0, speed=7.5)

        assert math.hypot(row["u"], row["v"]) == pytest.approx(7.5)

    def test_speed_scales_the_vector_but_not_the_bearing(self) -> None:
        """A 1 m/s and a 15 m/s northerly share a bearing but not a transport."""
        light, strong = _wind(45.0, 1.0), _wind(45.0, 15.0)

        assert light["wd_sin"] == pytest.approx(strong["wd_sin"])
        assert abs(strong["u"]) > abs(light["u"]) * 10

    def test_calm_wind_has_no_vector(self) -> None:
        row = _wind(180.0, speed=0.0)

        assert row["u"] == pytest.approx(0.0)
        assert row["v"] == pytest.approx(0.0)

    def test_raw_bearing_is_retained(self) -> None:
        """M3 needs it to show what using it directly produced."""
        assert "WD_HR" in add_wind_features(pl.DataFrame({"WD_HR": [90.0], "WS_HR": [1.0]})).columns

    def test_missing_inputs_are_reported(self) -> None:
        with pytest.raises(KeyError, match="WS_HR"):
            add_wind_features(pl.DataFrame({"WD_HR": [90.0]}))


class TestTemporalFeatures:
    def _frame(self, moments: list[datetime]) -> pl.DataFrame:
        return add_temporal_features(pl.DataFrame({"ts_local": moments}))

    def test_midnight_and_23h_are_adjacent(self) -> None:
        rows = self._frame([datetime(2015, 6, 1, 23), datetime(2015, 6, 2, 0)]).to_dicts()

        gap = math.hypot(
            rows[0]["hour_sin"] - rows[1]["hour_sin"],
            rows[0]["hour_cos"] - rows[1]["hour_cos"],
        )

        assert gap < 0.3, "the daily cycle must not break at midnight"

    def test_hour_encoding_is_on_the_unit_circle(self) -> None:
        for row in self._frame([datetime(2015, 6, 1, h) for h in range(24)]).to_dicts():
            assert row["hour_sin"] ** 2 + row["hour_cos"] ** 2 == pytest.approx(1.0)

    def test_year_end_and_year_start_are_adjacent(self) -> None:
        rows = self._frame([datetime(2015, 12, 31), datetime(2016, 1, 1)]).to_dicts()

        gap = math.hypot(
            rows[0]["doy_sin"] - rows[1]["doy_sin"],
            rows[0]["doy_cos"] - rows[1]["doy_cos"],
        )

        assert gap < 0.05, "the seasonal cycle must not break at new year"

    def test_weekend_flag(self) -> None:
        rows = self._frame(
            [datetime(2015, 6, 5), datetime(2015, 6, 6), datetime(2015, 6, 8)]
        ).to_dicts()

        assert [r["is_weekend"] for r in rows] == [False, True, False]

    def test_trend_is_measured_from_a_fixed_origin(self) -> None:
        """Fixed so models fitted on different subsets stay comparable."""
        rows = self._frame([datetime(1982, 1, 1), datetime(1983, 1, 1)]).to_dicts()

        assert rows[0]["trend_days"] == 0
        assert rows[1]["trend_days"] == 365
        assert TREND_ORIGIN.year == 1982

    def test_trend_increases_monotonically(self) -> None:
        rows = self._frame([datetime(y, 1, 1) for y in (1990, 2000, 2010)]).to_dicts()
        values = [r["trend_days"] for r in rows]

        assert values == sorted(values)

    def test_cyclic_encoding_is_reusable(self) -> None:
        frame = pl.DataFrame({"x": [0, 6, 12, 18]})

        out = frame.select(*cyclic_encoding(pl.col("x"), 24.0, "q"))

        assert out["q_sin"][0] == pytest.approx(0.0, abs=1e-9)
        assert out["q_sin"][1] == pytest.approx(1.0)

    def test_missing_timestamp_is_reported(self) -> None:
        with pytest.raises(KeyError, match="ts_local"):
            add_temporal_features(pl.DataFrame({"other": [1]}))


class TestChemFeatures:
    def test_total_oxidant_is_the_sum(self) -> None:
        out = add_chem_features(pl.DataFrame({"O3": [30.0], "NO2": [12.0]}))

        assert out["ox"].to_list() == [pytest.approx(42.0)]

    def test_fine_fraction_ratio(self) -> None:
        out = add_chem_features(pl.DataFrame({"PM2.5": [20.0], "PM10": [50.0]}))

        assert out["pm_ratio"].to_list() == [pytest.approx(0.4)]

    def test_ratio_is_null_when_the_denominator_vanishes(self) -> None:
        """Dividing by near-zero produces a number that would dominate a model."""
        out = add_chem_features(pl.DataFrame({"PM2.5": [20.0], "PM10": [0.0]}))

        assert out["pm_ratio"].to_list() == [None]

    def test_photochemical_age_ratio(self) -> None:
        out = add_chem_features(pl.DataFrame({"NO2": [8.0], "NOx": [10.0]}))

        assert out["no2_nox_ratio"].to_list() == [pytest.approx(0.8)]

    def test_features_are_skipped_when_inputs_are_absent(self) -> None:
        """A station measuring a reduced set still gets whatever is computable."""
        out = add_chem_features(pl.DataFrame({"O3": [30.0], "NO2": [12.0]}))

        assert "ox" in out.columns
        assert "pm_ratio" not in out.columns

    def test_no_features_leaves_the_frame_untouched(self) -> None:
        frame = pl.DataFrame({"AMB_TEMP": [25.0]})

        assert add_chem_features(frame).columns == ["AMB_TEMP"]
