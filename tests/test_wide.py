"""Tests for the wide (station-hour) modelling view."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import polars as pl

from twair.qc.flags import Flag  # type: ignore[import-untyped]
from twair.store.wide import (  # type: ignore[import-untyped]
    modelling_columns,
    valid_only,
    wide_year,
)


def _store(tmp_path: Path, rows: list[tuple[str, str, datetime, float | None, str]]) -> Path:
    from twair.store.writer import write_observations  # type: ignore[import-untyped]

    frame = pl.DataFrame(
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
    )
    write_observations(frame, root=tmp_path)
    return tmp_path


TS = datetime(2015, 6, 1, 0)


def test_pivot_produces_one_row_per_station_hour(tmp_path: Path) -> None:
    root = _store(
        tmp_path,
        [
            ("二林", "PM2.5", TS, 20.0, Flag.VALID.value),
            ("二林", "PM10", TS, 45.0, Flag.VALID.value),
            ("二林", "O3", TS, 30.0, Flag.VALID.value),
        ],
    )

    wide = wide_year(2015, root)

    assert wide.height == 1
    assert wide.row(0, named=True)["PM2.5"] == 20.0
    assert wide.row(0, named=True)["PM10"] == 45.0


def test_flag_columns_accompany_each_pollutant(tmp_path: Path) -> None:
    """Distinguishing "absent" from "rejected" is the point of keeping flags."""
    root = _store(
        tmp_path,
        [
            ("二林", "PM2.5", TS, None, Flag.INSTRUMENT_CHECK_INVALID.value),
            ("二林", "O3", TS, None, Flag.MISSING.value),
        ],
    )

    wide = wide_year(2015, root)
    row = wide.row(0, named=True)

    assert row["PM2.5"] is None
    assert row["O3"] is None
    assert row["PM2.5_flag"] == Flag.INSTRUMENT_CHECK_INVALID.value
    assert row["O3_flag"] == Flag.MISSING.value


def test_pm10_is_excluded_from_default_predictors(tmp_path: Path) -> None:
    """PM2.5 is a subset of PM10; using one to predict the other is leakage."""
    root = _store(
        tmp_path,
        [
            ("二林", "PM2.5", TS, 20.0, Flag.VALID.value),
            ("二林", "PM10", TS, 45.0, Flag.VALID.value),
            ("二林", "O3", TS, 30.0, Flag.VALID.value),
        ],
    )

    columns = modelling_columns(wide_year(2015, root))

    assert "PM10" not in columns
    assert "PM2.5" not in columns, "the response variable is not a predictor"
    assert "O3" in columns


def test_modelling_columns_drop_flag_columns() -> None:
    frame = pl.DataFrame(
        {"station_name": ["a"], "ts_local": [TS], "O3": [1.0], "O3_flag": ["valid"]}
    )

    assert modelling_columns(frame) == ["O3"]


def test_valid_only_nulls_rejected_readings() -> None:
    frame = pl.DataFrame(
        {
            "PM2.5": [20.0, 30.0],
            "PM2.5_flag": [Flag.VALID.value, Flag.INSTRUMENT_CHECK_INVALID.value],
        }
    )

    out = valid_only(frame, "PM2.5")

    assert out["PM2.5"].to_list() == [20.0, None]


def test_valid_only_is_a_no_op_without_a_flag_column() -> None:
    frame = pl.DataFrame({"PM2.5": [20.0]})

    assert valid_only(frame, "PM2.5")["PM2.5"].to_list() == [20.0]


def test_missing_year_returns_empty(tmp_path: Path) -> None:
    root = _store(tmp_path, [("二林", "PM2.5", TS, 20.0, Flag.VALID.value)])

    assert wide_year(1999, root).is_empty()
