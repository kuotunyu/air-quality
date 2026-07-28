"""Tests for wind-direction sentinel handling."""

from __future__ import annotations

from datetime import datetime

import polars as pl

from twair.qc.flags import Flag
from twair.qc.sentinels import (
    apply_sentinels,
    sentinel_columns,
    sentinel_report,
)

CONFIG = {
    "sentinels": {"wind_direction": {888: "calm", 999: "instrument_fault"}},
    "pollutants": {
        "WD_HR": {"sentinel_set": "wind_direction"},
        "WIND_DIREC": {"sentinel_set": "wind_direction"},
        "PM2.5": {},
    },
}


def _frame(rows: list[tuple[str, float | None, str]]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "ts_local": [datetime(2010, 1, 1, i) for i in range(len(rows))],
            "pollutant": [r[0] for r in rows],
            "value": [r[1] for r in rows],
            "flag": [r[2] for r in rows],
        }
    )


def test_config_maps_only_circular_pollutants() -> None:
    mapping = sentinel_columns(CONFIG)

    assert set(mapping) == {"WD_HR", "WIND_DIREC"}
    assert mapping["WD_HR"][888.0] is Flag.CALM
    assert mapping["WD_HR"][999.0] is Flag.INSTRUMENT_FAULT


def test_the_shipped_config_uses_the_definition_the_data_supports() -> None:
    """999 is calm and 888 is a direction the vane could not resolve.

    The two official ReadMe editions contradict each other — 2017 says
    888=calm/999=fault, 2001 says 888=variable/999=calm — and neither was
    written at the time the data was collected. The tie was broken by
    measurement: across 1993-2004, hours flagged 999 have a **median wind
    speed of exactly 0.00 m/s** (81.3% below 0.5) against 0.43 m/s for 888 and
    1.84 m/s for ordinary bearings. A broken direction sensor has no reason to
    coincide with a still anemometer.

    Since these codes appear only in 1993-2004, the 2001 edition governs every
    occurrence in the store. This test guards the mapping in the shipped
    config, which the fixtures above deliberately do not use.
    """
    mapping = sentinel_columns()

    assert mapping["WD_HR"][999.0] is Flag.CALM, "999 = 靜風, per the wind-speed evidence"
    assert mapping["WD_HR"][888.0] is Flag.VARIABLE_DIRECTION, "888 = 風向不定"
    assert mapping["WIND_DIREC"] == mapping["WD_HR"], "both wind channels share the convention"


def test_sentinels_become_null_with_a_flag() -> None:
    frame = _frame(
        [
            ("WD_HR", 888.0, Flag.VALID.value),
            ("WD_HR", 999.0, Flag.VALID.value),
            ("WD_HR", 180.0, Flag.VALID.value),
        ]
    )

    out = apply_sentinels(frame, config=CONFIG)

    assert out["value"].to_list() == [None, None, 180.0]
    assert out["flag"].to_list() == [
        Flag.CALM.value,
        Flag.INSTRUMENT_FAULT.value,
        Flag.VALID.value,
    ]


def test_same_numbers_are_untouched_for_other_pollutants() -> None:
    """888 µg/m3 of PM2.5 would be extreme but is a real measurement."""
    frame = _frame([("PM2.5", 888.0, Flag.VALID.value)])

    out = apply_sentinels(frame, config=CONFIG)

    assert out["value"].to_list() == [888.0]
    assert out["flag"].to_list() == [Flag.VALID.value]


def test_agency_rejection_outranks_our_inference() -> None:
    """A cell the agency already rejected keeps its official flag."""
    frame = _frame([("WD_HR", 888.0, Flag.INSTRUMENT_CHECK_INVALID.value)])

    out = apply_sentinels(frame, config=CONFIG)

    assert out["flag"].to_list() == [Flag.INSTRUMENT_CHECK_INVALID.value]


def test_report_counts_by_year_and_flag() -> None:
    frame = apply_sentinels(
        _frame(
            [
                ("WD_HR", 888.0, Flag.VALID.value),
                ("WD_HR", 888.0, Flag.VALID.value),
                ("WD_HR", 999.0, Flag.VALID.value),
                ("WD_HR", 45.0, Flag.VALID.value),
            ]
        ),
        config=CONFIG,
    )

    report = sentinel_report(frame)

    counts = {(r["flag"], r["n"]) for r in report.to_dicts()}
    assert counts == {(Flag.CALM.value, 2), (Flag.INSTRUMENT_FAULT.value, 1)}
    assert report["year"].unique().to_list() == [2010]


def test_report_is_empty_when_no_sentinels_present() -> None:
    frame = _frame([("WD_HR", 45.0, Flag.VALID.value)])

    report = sentinel_report(apply_sentinels(frame, config=CONFIG))

    assert report.is_empty()
