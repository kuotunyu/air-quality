"""Tests for no-rain zero handling.

The distinction encoded here is the whole point: no rain is zero millimetres,
but it is not pH zero.
"""

from __future__ import annotations

import polars as pl

from twair.qc.flags import Flag
from twair.qc.rainfall import (
    USABLE_FLAGS,
    apply_no_rain_zero,
    no_rain_zero_pollutants,
    usable,
)

CONFIG = {
    "pollutants": {
        "RAINFALL": {"no_rain_is_zero": True},
        "RAIN_INT": {"no_rain_is_zero": True},
        "PH_RAIN": {"no_rain_is_zero": False},
        "RAIN_COND": {"no_rain_is_zero": False},
        "PM2.5": {},
    }
}


def _frame(rows: list[tuple[str, float | None, str]]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "pollutant": [r[0] for r in rows],
            "value": [r[1] for r in rows],
            "flag": [r[2] for r in rows],
        }
    )


class TestNoRainIsZero:
    def test_rainfall_no_rain_becomes_zero(self) -> None:
        """90% of hourly rainfall cells are NR; excluding them inflates the mean tenfold."""
        out = apply_no_rain_zero(_frame([("RAINFALL", None, Flag.NO_RAIN.value)]), config=CONFIG)

        assert out["value"].to_list() == [0.0]

    def test_rain_intensity_also_becomes_zero(self) -> None:
        out = apply_no_rain_zero(_frame([("RAIN_INT", None, Flag.NO_RAIN.value)]), config=CONFIG)

        assert out["value"].to_list() == [0.0]

    def test_ph_of_rain_that_never_fell_stays_undefined(self) -> None:
        """pH 0 is strongly acidic — coercing dry weather to it invents acid rain."""
        out = apply_no_rain_zero(_frame([("PH_RAIN", None, Flag.NO_RAIN.value)]), config=CONFIG)

        assert out["value"].to_list() == [None]

    def test_conductivity_stays_undefined_too(self) -> None:
        out = apply_no_rain_zero(_frame([("RAIN_COND", None, Flag.NO_RAIN.value)]), config=CONFIG)

        assert out["value"].to_list() == [None]

    def test_the_flag_is_preserved_not_rewritten(self) -> None:
        """How the agency recorded it stays visible; only the value is filled."""
        out = apply_no_rain_zero(_frame([("RAINFALL", None, Flag.NO_RAIN.value)]), config=CONFIG)

        assert out["flag"].to_list() == [Flag.NO_RAIN.value]

    def test_existing_measurements_are_untouched(self) -> None:
        out = apply_no_rain_zero(_frame([("RAINFALL", 4.5, Flag.VALID.value)]), config=CONFIG)

        assert out["value"].to_list() == [4.5]

    def test_other_pollutants_are_untouched(self) -> None:
        out = apply_no_rain_zero(_frame([("PM2.5", None, Flag.MISSING.value)]), config=CONFIG)

        assert out["value"].to_list() == [None]

    def test_config_lists_only_the_zero_valued_pollutants(self) -> None:
        assert no_rain_zero_pollutants(CONFIG) == {"RAINFALL", "RAIN_INT"}


class TestUsablePredicate:
    def test_no_rain_counts_as_usable_once_it_has_a_value(self) -> None:
        frame = apply_no_rain_zero(
            _frame(
                [
                    ("RAINFALL", None, Flag.NO_RAIN.value),
                    ("RAINFALL", 4.5, Flag.VALID.value),
                    ("RAINFALL", None, Flag.INSTRUMENT_CHECK_INVALID.value),
                ]
            ),
            config=CONFIG,
        )

        kept = frame.filter(usable())

        assert kept.height == 2
        assert sorted(kept["value"].to_list()) == [0.0, 4.5]

    def test_no_rain_without_a_value_is_still_excluded(self) -> None:
        """PH_RAIN keeps its null, so the null check drops it."""
        frame = apply_no_rain_zero(_frame([("PH_RAIN", None, Flag.NO_RAIN.value)]), config=CONFIG)

        assert frame.filter(usable()).is_empty()

    def test_rejected_readings_are_never_usable(self) -> None:
        frame = _frame([("PM2.5", 20.0, Flag.INSTRUMENT_CHECK_INVALID.value)])

        assert frame.filter(usable()).is_empty()

    def test_usable_flags_are_exactly_valid_and_no_rain(self) -> None:
        assert set(USABLE_FLAGS) == {Flag.VALID.value, Flag.NO_RAIN.value}


class TestShippedConfig:
    def test_rainfall_is_configured_as_zero_valued(self) -> None:
        assert "RAINFALL" in no_rain_zero_pollutants()

    def test_acid_rain_measurements_are_not(self) -> None:
        zero_valued = no_rain_zero_pollutants()

        assert "PH_RAIN" not in zero_valued
        assert "RAIN_COND" not in zero_valued
