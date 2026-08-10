"""Immutable station-inventory identities shared by satellite data sources."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

import polars as pl

from twair.ingest.station_meta import TAIWAN_BOUNDS
from twair.paths import interim_dir

__all__ = [
    "StationInventoryGeneration",
    "maiac_generation_ledger_path",
    "maiac_generation_result_dir",
    "satellite_generation_dir",
    "station_inventory_generation",
    "validate_generation_sha256",
]

_GENERATION_PATTERN = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class StationInventoryGeneration:
    stations: pl.DataFrame
    stations_total: int
    stations_with_coordinates: int
    stations_without_coordinates: int
    sha256: str


def station_inventory_generation(stations: pl.DataFrame) -> StationInventoryGeneration:
    required = {"station_name", "lon", "lat"}
    missing = required - set(stations.columns)
    if missing:
        raise RuntimeError(f"station table is missing {sorted(missing)}")

    try:
        normalized = stations.select(
            pl.col("station_name").cast(pl.String),
            pl.col("lon").cast(pl.Float64),
            pl.col("lat").cast(pl.Float64),
        )
    except (pl.exceptions.InvalidOperationError, pl.exceptions.ComputeError) as exc:
        raise RuntimeError(
            "station names and coordinates must use canonical scalar values"
        ) from exc

    invalid_names = normalized.filter(
        pl.col("station_name").is_null() | (pl.col("station_name").str.strip_chars() == "")
    )
    if not invalid_names.is_empty():
        raise RuntimeError("station names must be non-empty strings")
    duplicates = normalized.group_by("station_name").len().filter(pl.col("len") > 1)
    if not duplicates.is_empty():
        raise RuntimeError(f"station names are not unique: {duplicates['station_name'].to_list()}")

    partial = normalized.filter(pl.col("lon").is_null() != pl.col("lat").is_null())
    if not partial.is_empty():
        raise RuntimeError(
            "station longitude and latitude must be both present or both null: "
            f"{partial['station_name'].to_list()}"
        )
    non_finite = normalized.filter(
        (pl.col("lon").is_not_null() & ~pl.col("lon").is_finite())
        | (pl.col("lat").is_not_null() & ~pl.col("lat").is_finite())
    )
    if not non_finite.is_empty():
        raise RuntimeError(
            f"station coordinates are not finite: {non_finite['station_name'].to_list()}"
        )

    placed = normalized.filter(pl.col("lon").is_not_null())
    outside = placed.filter(
        ~pl.col("lon").is_between(TAIWAN_BOUNDS["lon_min"], TAIWAN_BOUNDS["lon_max"])
        | ~pl.col("lat").is_between(TAIWAN_BOUNDS["lat_min"], TAIWAN_BOUNDS["lat_max"])
    )
    if not outside.is_empty():
        raise RuntimeError(
            f"station coordinates are outside Taiwan: {outside['station_name'].to_list()}"
        )
    if placed.is_empty():
        raise RuntimeError("no stations have coordinates")

    placed = placed.sort("station_name")
    payload = json.dumps(
        placed.to_dicts(),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    placed_count = placed.height
    return StationInventoryGeneration(
        stations=placed,
        stations_total=normalized.height,
        stations_with_coordinates=placed_count,
        stations_without_coordinates=normalized.height - placed_count,
        sha256=sha256(payload).hexdigest(),
    )


def validate_generation_sha256(value: str) -> str:
    if not isinstance(value, str) or _GENERATION_PATTERN.fullmatch(value) is None:
        raise ValueError("generation must be a full 64-character lowercase SHA-256")
    return value


def _validated_year(year: int) -> int:
    if isinstance(year, bool) or not isinstance(year, int) or year <= 0:
        raise ValueError("generation year must be a positive integer")
    return year


def satellite_generation_dir(year: int, generation_sha256: str) -> Path:
    year = _validated_year(year)
    generation = validate_generation_sha256(generation_sha256)
    return interim_dir("satellite") / "generations" / generation / f"year={year}"


def maiac_generation_ledger_path(year: int, generation_sha256: str) -> Path:
    year = _validated_year(year)
    generation = validate_generation_sha256(generation_sha256)
    return interim_dir("maiac") / "generations" / generation / f"year={year}" / "export-ledger.json"


def maiac_generation_result_dir(year: int, generation_sha256: str) -> Path:
    return maiac_generation_ledger_path(year, generation_sha256).parent / "result"
