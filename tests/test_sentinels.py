"""Tests for wind-direction sentinel handling."""

from __future__ import annotations

from datetime import datetime

import polars as pl

from twair.qc.flags import Flag
from twair.qc.sentinels import apply_sentinels, sentinel_columns, sentinel_report

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
