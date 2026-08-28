"""Load the immutable primary inputs for spatial surface evaluation."""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from hashlib import sha256
from pathlib import Path
from typing import Any

import polars as pl

from twair.config import ConfigError, load_conf
from twair.ingest.station_inventory import station_inventory_generation

__all__ = [
    "FileIdentity",
    "SpatialSurfaceBaselineConfig",
    "SurfaceInputs",
    "load_spatial_surface_baseline_config",
    "load_surface_inputs",
]


_STATION_COLUMNS = ("station_name", "station_type_official", "lon", "lat")
_MONTHLY_COLUMNS = ("station_name", "pollutant", "month", "mean", "meets_threshold")


@dataclass(frozen=True, slots=True)
class SpatialSurfaceBaselineConfig:
    years: tuple[int, int]
    pollutant: str
    station_types: tuple[str, ...]
    excluded_stations: tuple[str, ...]
    min_train_stations: int
    buffer_radii_km: tuple[float, ...]
    spatial_folds: int
    projection_epsg: int
    methods: tuple[str, ...]
    idw_power: float
    minimum_distance_km: float
    bootstrap_draws: int
    seed: int
    comparison_method: str
    required_evaluations: tuple[str, ...]
    required_years: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class FileIdentity:
    path: Path
    bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class SurfaceInputs:
    stations: pl.DataFrame
    panel: pl.DataFrame
    input_files: tuple[FileIdentity, ...]
    inventory_generation_sha256: str


def _mapping(value: object, *, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{path} must be a mapping")
    return value


def _positive_int(value: object, *, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(f"{path} must be a positive integer")
    return value


def _positive_float(value: object, *, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{path} must be a positive finite number")
    converted = float(value)
    if not math.isfinite(converted) or converted <= 0:
        raise ConfigError(f"{path} must be a positive finite number")
    return converted


def _strings(value: object, *, path: str) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, str) or not item for item in value)
    ):
        raise ConfigError(f"{path} must be a non-empty list of strings")
    if len(value) != len(set(value)):
        raise ConfigError(f"{path} must not contain duplicates")
    return tuple(value)


def _years(value: object, *, path: str, exact_pair: bool) -> tuple[int, ...]:
    if not isinstance(value, list) or not value:
        raise ConfigError(f"{path} must be a non-empty list of years")
    converted = tuple(
        _positive_int(year, path=f"{path}[{index}]") for index, year in enumerate(value)
    )
    if len(set(converted)) != len(converted) or tuple(sorted(converted)) != converted:
        raise ConfigError(f"{path} must contain unique ascending years")
    if exact_pair and len(converted) != 2:
        raise ConfigError(f"{path} must contain exactly two years")
    return converted


def load_spatial_surface_baseline_config(
    config: dict[str, Any] | None = None,
) -> SpatialSurfaceBaselineConfig:
    raw = config if config is not None else load_conf("spatial_surface_baseline")
    if not isinstance(raw, dict):
        raise ConfigError("spatial_surface_baseline must be a mapping")
    schema_version = raw.get("schema_version")
    if schema_version != 1:
        raise ConfigError("spatial_surface_baseline.schema_version must be 1")
    analysis = _mapping(raw.get("analysis"), path="spatial_surface_baseline.analysis")
    validation = _mapping(raw.get("validation"), path="spatial_surface_baseline.validation")
    gate = _mapping(raw.get("gate"), path="spatial_surface_baseline.gate")
    years = _years(
        analysis.get("years"), path="spatial_surface_baseline.analysis.years", exact_pair=True
    )
    required_years = _years(
        gate.get("required_years"),
        path="spatial_surface_baseline.gate.required_years",
        exact_pair=False,
    )
    if not set(required_years).issubset(years):
        raise ConfigError(
            "spatial_surface_baseline.gate.required_years must be configured analysis years"
        )
    pollutant = analysis.get("pollutant")
    if not isinstance(pollutant, str) or not pollutant:
        raise ConfigError("spatial_surface_baseline.analysis.pollutant must be a non-empty string")
    buffer_radii_km = tuple(
        _positive_float(
            radius, path=f"spatial_surface_baseline.validation.buffer_radii_km[{index}]"
        )
        for index, radius in enumerate(
            _list(
                validation.get("buffer_radii_km"),
                path="spatial_surface_baseline.validation.buffer_radii_km",
            )
        )
    )
    if len(buffer_radii_km) != len(set(buffer_radii_km)):
        raise ConfigError(
            "spatial_surface_baseline.validation.buffer_radii_km must not contain duplicates"
        )
    methods = _strings(
        validation.get("methods"), path="spatial_surface_baseline.validation.methods"
    )
    comparison_method = gate.get("comparison_method")
    if not isinstance(comparison_method, str) or comparison_method not in methods:
        raise ConfigError(
            "spatial_surface_baseline.gate.comparison_method must be a configured method"
        )
    return SpatialSurfaceBaselineConfig(
        years=(years[0], years[1]),
        pollutant=pollutant,
        station_types=_strings(
            analysis.get("station_types"), path="spatial_surface_baseline.analysis.station_types"
        ),
        excluded_stations=_strings(
            analysis.get("excluded_stations"),
            path="spatial_surface_baseline.analysis.excluded_stations",
        ),
        min_train_stations=_positive_int(
            analysis.get("min_train_stations"),
            path="spatial_surface_baseline.analysis.min_train_stations",
        ),
        buffer_radii_km=buffer_radii_km,
        spatial_folds=_positive_int(
            validation.get("spatial_folds"),
            path="spatial_surface_baseline.validation.spatial_folds",
        ),
        projection_epsg=_positive_int(
            validation.get("projection_epsg"),
            path="spatial_surface_baseline.validation.projection_epsg",
        ),
        methods=methods,
        idw_power=_positive_float(
            validation.get("idw_power"), path="spatial_surface_baseline.validation.idw_power"
        ),
        minimum_distance_km=_positive_float(
            validation.get("minimum_distance_km"),
            path="spatial_surface_baseline.validation.minimum_distance_km",
        ),
        bootstrap_draws=_positive_int(
            validation.get("bootstrap_draws"),
            path="spatial_surface_baseline.validation.bootstrap_draws",
        ),
        seed=_positive_int(validation.get("seed"), path="spatial_surface_baseline.validation.seed"),
        comparison_method=comparison_method,
        required_evaluations=_strings(
            gate.get("required_evaluations"),
            path="spatial_surface_baseline.gate.required_evaluations",
        ),
        required_years=required_years,
    )


def _list(value: object, *, path: str) -> list[Any]:
    if not isinstance(value, list) or not value:
        raise ConfigError(f"{path} must be a non-empty list")
    return value


def _file_identity(path: Path) -> FileIdentity:
    if not path.is_file():
        raise FileNotFoundError(f"spatial surface input not found: {path}")
    payload = path.read_bytes()
    return FileIdentity(path=path, bytes=len(payload), sha256=sha256(payload).hexdigest())


def _require_columns(frame: pl.DataFrame, required: Iterable[str], *, label: str) -> None:
    missing = sorted(set(required) - set(frame.columns))
    if missing:
        raise RuntimeError(f"{label} is missing required column(s): {missing}")


def _validated_stations(
    frame: pl.DataFrame, config: SpatialSurfaceBaselineConfig
) -> tuple[pl.DataFrame, str]:
    _require_columns(frame, _STATION_COLUMNS, label="spatial surface station inventory")
    if frame.schema["station_type_official"] != pl.String:
        raise RuntimeError("spatial surface station types must use String values")
    generation = station_inventory_generation(frame)
    selected = frame.select(*_STATION_COLUMNS).filter(
        pl.col("station_type_official").is_in(config.station_types)
        & ~pl.col("station_name").is_in(config.excluded_stations)
    )
    if selected.is_empty():
        raise RuntimeError("spatial surface primary cohort has no selected stations")
    incomplete = selected.filter(pl.col("lon").is_null() | pl.col("lat").is_null())
    if not incomplete.is_empty():
        raise RuntimeError("spatial surface primary cohort requires complete station coordinates")
    return selected, generation.sha256


def _validated_monthly(
    frame: pl.DataFrame, config: SpatialSurfaceBaselineConfig, station_names: list[str]
) -> pl.DataFrame:
    _require_columns(frame, _MONTHLY_COLUMNS, label="spatial surface monthly table")
    if frame.schema["month"] != pl.Date:
        raise RuntimeError("spatial surface monthly months must use Date values")
    if frame.schema["meets_threshold"] != pl.Boolean:
        raise RuntimeError("spatial surface monthly meets_threshold must use Boolean values")
    if frame.schema["mean"] not in {pl.Float32, pl.Float64}:
        raise RuntimeError("spatial surface monthly means must use floating-point values")
    selected = frame.select(*_MONTHLY_COLUMNS).filter(
        (pl.col("pollutant") == config.pollutant)
        & pl.col("station_name").is_in(station_names)
        & pl.col("month").dt.year().is_between(config.years[0], config.years[1])
    )
    duplicates = selected.group_by("station_name", "month").len().filter(pl.col("len") > 1)
    if not duplicates.is_empty():
        raise RuntimeError("spatial surface monthly table has duplicate PM2.5 station-month keys")
    return selected.with_columns(pl.lit(True).alias("_source_row_present"))


def _calendar(config: SpatialSurfaceBaselineConfig) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "month": [
                date(year, month, 1)
                for year in range(config.years[0], config.years[1] + 1)
                for month in range(1, 13)
            ]
        },
        schema={"month": pl.Date},
    )


def load_surface_inputs(data_root: Path, config: SpatialSurfaceBaselineConfig) -> SurfaceInputs:
    station_path = data_root / "outputs" / "qc" / "stations.parquet"
    monthly_path = data_root / "processed" / "monthly" / "monthly.parquet"
    station_before = _file_identity(station_path)
    monthly_before = _file_identity(monthly_path)
    inventory = pl.read_parquet(station_path)
    monthly = pl.read_parquet(monthly_path)
    station_after = _file_identity(station_path)
    monthly_after = _file_identity(monthly_path)
    if station_before != station_after or monthly_before != monthly_after:
        raise RuntimeError("spatial surface input file changed while it was read")
    stations, inventory_generation_sha256 = _validated_stations(inventory, config)
    source = _validated_monthly(monthly, config, stations["station_name"].to_list())
    panel = (
        stations.join(_calendar(config), how="cross")
        .join(source, on=("station_name", "month"), how="left")
        .with_columns(
            pl.lit(config.pollutant).alias("pollutant"),
            pl.when(pl.col("_source_row_present").is_null())
            .then(pl.lit("source_row_absent"))
            .when(pl.col("mean").is_not_null() & ~pl.col("mean").is_finite())
            .then(pl.lit("invalid_non_finite"))
            .when(
                pl.col("meets_threshold")
                & pl.col("mean").is_not_null()
                & pl.col("mean").is_finite()
            )
            .then(pl.lit("observed"))
            .otherwise(pl.lit("withheld"))
            .alias("target_state"),
        )
        .drop("_source_row_present")
        .sort("station_name", "month")
    )
    return SurfaceInputs(
        stations=stations,
        panel=panel,
        input_files=(station_before, monthly_before),
        inventory_generation_sha256=inventory_generation_sha256,
    )
