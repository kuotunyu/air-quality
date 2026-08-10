"""One immutable identity keeps new satellite sources on the same station generation."""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from twair.ingest.station_inventory import (
    maiac_generation_ledger_path,
    maiac_generation_result_dir,
    satellite_generation_dir,
    station_inventory_generation,
    validate_generation_sha256,
)


def station_rows() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "station_name": ["Beta", "Unplaced", "Alpha"],
            "lon": [120.0, None, 121.0],
            "lat": [24.0, None, 25.0],
            "unused": [2, 3, 1],
        }
    )


def test_row_and_column_order_cannot_change_the_generation_identity() -> None:
    first = station_inventory_generation(station_rows())
    reordered = station_rows().select("unused", "lat", "station_name", "lon").reverse()
    second = station_inventory_generation(reordered)
    payload = [
        {"station_name": "Alpha", "lon": 121.0, "lat": 25.0},
        {"station_name": "Beta", "lon": 120.0, "lat": 24.0},
    ]
    expected = sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    assert first.sha256 == expected
    assert second.sha256 == expected
    assert first.stations.to_dicts() == payload


def test_unresolved_coordinates_are_counted_without_being_filled() -> None:
    rows = [
        {
            "station_name": f"Placed {index:02d}",
            "lon": 120.0 + index / 100,
            "lat": 23.0 + index / 100,
        }
        for index in range(77)
    ]
    rows.extend(
        {"station_name": f"Unresolved {index}", "lon": None, "lat": None} for index in range(5)
    )
    source = pl.DataFrame(rows)
    before = source.clone()

    generation = station_inventory_generation(source)

    assert generation.stations_total == 82
    assert generation.stations_with_coordinates == 77
    assert generation.stations_without_coordinates == 5
    assert generation.stations.height == 77
    assert source.equals(before)
    assert (
        source.filter(pl.col("station_name").str.starts_with("Unresolved"))["lon"].null_count() == 5
    )


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        (
            pl.DataFrame({"station_name": ["A", "A"], "lon": [121.0, 121.1], "lat": [25.0, 25.1]}),
            "station names are not unique",
        ),
        (
            pl.DataFrame({"station_name": ["A"], "lon": [float("nan")], "lat": [25.0]}),
            "coordinates are not finite",
        ),
        (
            pl.DataFrame({"station_name": ["A"], "lon": [0.0], "lat": [0.0]}),
            "outside Taiwan",
        ),
        (
            pl.DataFrame({"station_name": ["A"], "lon": [121.0], "lat": [None]}),
            "both present or both null",
        ),
    ],
)
def test_an_ambiguous_station_inventory_cannot_name_a_generation(
    rows: pl.DataFrame, message: str
) -> None:
    with pytest.raises(RuntimeError, match=message):
        station_inventory_generation(rows)


@pytest.mark.parametrize(
    "value",
    ["", "abc", "A" * 64, "0" * 63, "0" * 65, "../" + "0" * 64],
)
def test_only_a_full_lowercase_sha256_can_select_a_generation(value: str) -> None:
    with pytest.raises(ValueError, match="64-character lowercase SHA-256"):
        validate_generation_sha256(value)


def test_both_sources_share_one_full_hash_but_keep_separate_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("TWAIR_DATA_DIR", str(tmp_path))
    generation = station_inventory_generation(station_rows())

    assert satellite_generation_dir(2025, generation.sha256) == (
        tmp_path / "interim" / "satellite" / "generations" / generation.sha256 / "year=2025"
    )
    assert maiac_generation_ledger_path(2025, generation.sha256) == (
        tmp_path
        / "interim"
        / "maiac"
        / "generations"
        / generation.sha256
        / "year=2025"
        / "export-ledger.json"
    )
    assert maiac_generation_result_dir(2025, generation.sha256) == (
        tmp_path / "interim" / "maiac" / "generations" / generation.sha256 / "year=2025" / "result"
    )


@pytest.mark.parametrize("year", [True, 0, -1, 2025.0])
def test_a_generation_path_requires_a_positive_integer_year(year: Any) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        satellite_generation_dir(year, "0" * 64)
