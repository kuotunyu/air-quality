"""Validate the reviewed 2025 micro-sensor panel before its annual audit."""

from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import sys
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import duckdb
import polars as pl

from twair.config import ConfigError, load_conf
from twair.ingest.micro_sensor_observations import (
    OBSERVATION_OUTPUT_SCHEMA,
    load_micro_sensor_observation_generation,
)
from twair.ingest.micro_sensors import load_catalog_generation
from twair.ingest.station_meta import TAIWAN_BOUNDS, resolve_station_geo
from twair.net import sha256_file
from twair.paths import data_root as configured_data_root
from twair.paths import outputs_dir
from twair.provenance import git_state
from twair.store.schema import PARTITION_SCHEMA

_SHA256 = re.compile(r"[0-9a-f]{64}")
_YEAR = 2025
_PARSED_DAYS = 322
_ABSENT_DAYS = 43
_ANALYSIS_FIELDS = {
    "threads",
    "memory_limit_gb",
    "minimum_active_months",
    "minimum_trio_dates",
    "minimum_trio_hours",
    "distance_bands_km",
    "extreme_ranges",
}
_VARIABLES = ("pm25", "humidity", "temperature")
_VARIABLE_METRICS = (
    "source_rows",
    "null_value_rows",
    "null_timestamp_rows",
    "distinct_timestamps",
    "observed_hours",
    "duplicate_timestamp_groups",
    "extreme_rows",
)
ANNUAL_DEVICE_DAY_SCHEMA: tuple[tuple[str, pl.DataType | type[pl.DataType]], ...] = (
    ("date", pl.Date),
    ("device_id", pl.String),
    *(
        (f"{variable}_{metric}", pl.Int64)
        for variable in _VARIABLES
        for metric in _VARIABLE_METRICS
    ),
    ("trio_observed_hours", pl.Int64),
    ("coordinate_source_rows", pl.Int64),
    ("coordinate_null_rows", pl.Int64),
    ("coordinate_invalid_rows", pl.Int64),
    ("coordinate_positions", pl.Int64),
    ("lon_min", pl.Float64),
    ("lon_max", pl.Float64),
    ("lat_min", pl.Float64),
    ("lat_max", pl.Float64),
    ("spatial_state", pl.String),
    ("station_name", pl.String),
    ("distance_km", pl.Float64),
    ("ground_present_trio_hours", pl.Int64),
    ("ground_eligible_trio_hours", pl.Int64),
    ("ground_present_ineligible_trio_hours", pl.Int64),
    ("ground_absent_trio_hours", pl.Int64),
)
ANNUAL_DEVICE_COHORT_SCHEMA: tuple[tuple[str, pl.DataType | type[pl.DataType]], ...] = (
    ("device_id", pl.String),
    ("active_dates", pl.Int64),
    ("active_months", pl.Int64),
    ("trio_dates", pl.Int64),
    *(
        (f"{variable}_{metric}", pl.Int64)
        for variable in _VARIABLES
        for metric in _VARIABLE_METRICS
    ),
    ("trio_observed_hours", pl.Int64),
    ("coordinate_source_rows", pl.Int64),
    ("coordinate_null_rows", pl.Int64),
    ("coordinate_invalid_rows", pl.Int64),
    ("lon_min", pl.Float64),
    ("lon_max", pl.Float64),
    ("lat_min", pl.Float64),
    ("lat_max", pl.Float64),
    ("spatial_state", pl.String),
    ("station_name", pl.String),
    ("distance_km", pl.Float64),
    ("ground_present_trio_hours", pl.Int64),
    ("ground_eligible_trio_hours", pl.Int64),
    ("ground_present_ineligible_trio_hours", pl.Int64),
    ("ground_absent_trio_hours", pl.Int64),
    ("exclusion_reason", pl.String),
)
ANNUAL_COHORT_THRESHOLD_SCHEMA: tuple[tuple[str, pl.DataType | type[pl.DataType]], ...] = (
    ("minimum_active_months", pl.Int64),
    ("minimum_trio_dates", pl.Int64),
    ("minimum_trio_hours", pl.Int64),
    ("distance_km", pl.Float64),
    ("devices", pl.Int64),
)
ANNUAL_EXCLUSION_SCHEMA: tuple[tuple[str, pl.DataType | type[pl.DataType]], ...] = (
    ("reason", pl.String),
    ("devices", pl.Int64),
)
ANNUAL_CALENDAR_SCHEMA: tuple[tuple[str, pl.DataType | type[pl.DataType]], ...] = (
    ("date", pl.Date),
    ("state", pl.String),
    ("catalog_generation_sha256", pl.String),
    ("parsed_generation_sha256", pl.String),
)
_RESULT_MEMBERS = (
    "calendar_coverage",
    "device_days",
    "device_cohorts",
    "cohort_thresholds",
    "exclusions",
)
_CLAIM_BOUNDARY = {
    "calibration_fitted": False,
    "bias_estimated": False,
    "fusion_performed": False,
    "satellite_acquired": False,
    "values_imputed": False,
    "nearest_reference_is_colocated_ground_truth": False,
    "high_resolution_pm25_created": False,
}


@dataclass(frozen=True, slots=True)
class AnnualParsedGeneration:
    date: date
    generation_sha256: str


@dataclass(frozen=True, slots=True)
class AnnualMicroSensorAuditConfig:
    threads: int
    memory_limit_gb: int
    minimum_active_months: tuple[int, ...]
    minimum_trio_dates: tuple[int, ...]
    minimum_trio_hours: tuple[int, ...]
    distance_bands_km: tuple[float, ...]
    extreme_ranges: tuple[tuple[str, tuple[float, float]], ...]


@dataclass(frozen=True, slots=True)
class AnnualMicroSensorPanelConfig:
    year: int
    catalog_generations: tuple[tuple[str, str], ...]
    parsed_generations: tuple[AnnualParsedGeneration, ...]
    catalogue_absent_dates: tuple[date, ...]
    analysis: AnnualMicroSensorAuditConfig

    @property
    def parsed_dates(self) -> tuple[date, ...]:
        return tuple(record.date for record in self.parsed_generations)


@dataclass(frozen=True, slots=True)
class AnnualMicroSensorReadinessResult:
    calendar_coverage: pl.DataFrame
    device_days: pl.DataFrame | tuple[Path, ...]
    device_cohorts: pl.DataFrame
    cohort_thresholds: pl.DataFrame
    exclusions: pl.DataFrame
    summary: dict[str, Any]
    manifest: dict[str, Any]


def _mapping(value: object, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ConfigError(f"{label} must be a mapping with string keys")
    return value


def _exact_keys(value: dict[str, Any], expected: set[str], *, label: str) -> None:
    unknown = sorted(set(value) - expected)
    missing = sorted(expected - set(value))
    if unknown:
        raise ConfigError(f"{label} has unknown field(s): {unknown}")
    if missing:
        raise ConfigError(f"{label} is missing field(s): {missing}")


def _identity(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ConfigError(f"{label} must be a 64-character lowercase SHA-256")
    return value


def _iso_date(value: object, *, label: str) -> date:
    if not isinstance(value, str):
        raise ConfigError(f"{label} must be an ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ConfigError(f"{label} must be an ISO date") from exc
    if parsed.year != _YEAR:
        raise ConfigError(f"{label} must be in {_YEAR}")
    return parsed


def _integer_sequence(value: object, *, label: str) -> tuple[int, ...]:
    if not isinstance(value, list) or any(
        isinstance(item, bool) or not isinstance(item, int) for item in value
    ):
        raise ConfigError(f"{label} must contain integers")
    return tuple(value)


def _number_sequence(value: object, *, label: str) -> tuple[float, ...]:
    if not isinstance(value, list):
        raise ConfigError(f"{label} must contain finite numbers")
    converted: list[float] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise ConfigError(f"{label} must contain finite numbers")
        number = float(item)
        if not math.isfinite(number):
            raise ConfigError(f"{label} must contain finite numbers")
        converted.append(number)
    return tuple(converted)


def _load_analysis(value: object) -> AnnualMicroSensorAuditConfig:
    raw = _mapping(value, label="micro_sensor_annual_panel.analysis")
    _exact_keys(raw, _ANALYSIS_FIELDS, label="micro_sensor_annual_panel.analysis")
    threads = raw["threads"]
    if isinstance(threads, bool) or not isinstance(threads, int) or threads != 1:
        raise ConfigError("micro_sensor_annual_panel.analysis.threads must be one")
    memory = raw["memory_limit_gb"]
    if isinstance(memory, bool) or not isinstance(memory, int) or memory != 6:
        raise ConfigError("micro_sensor_annual_panel.analysis.memory_limit_gb must be six")
    months = _integer_sequence(raw["minimum_active_months"], label="analysis.minimum_active_months")
    dates = _integer_sequence(raw["minimum_trio_dates"], label="analysis.minimum_trio_dates")
    hours = _integer_sequence(raw["minimum_trio_hours"], label="analysis.minimum_trio_hours")
    distances = _number_sequence(raw["distance_bands_km"], label="analysis.distance_bands_km")
    ranges_raw = _mapping(raw["extreme_ranges"], label="analysis.extreme_ranges")
    _exact_keys(ranges_raw, set(_VARIABLES), label="analysis.extreme_ranges")
    extreme_ranges: list[tuple[str, tuple[float, float]]] = []
    for variable in sorted(_VARIABLES):
        bounds = _number_sequence(
            ranges_raw[variable],
            label=f"analysis.extreme_ranges.{variable}",
        )
        if len(bounds) != 2 or bounds[0] >= bounds[1]:
            raise ConfigError(f"analysis.extreme_ranges.{variable} must increase")
        extreme_ranges.append((variable, (bounds[0], bounds[1])))
    if months != (3, 6, 9, 12):
        raise ConfigError("micro_sensor_annual_panel.analysis.minimum_active_months changed")
    if dates != (30, 90, 180, 270):
        raise ConfigError("micro_sensor_annual_panel.analysis.minimum_trio_dates changed")
    if hours != (360, 1080, 2160, 4320):
        raise ConfigError("micro_sensor_annual_panel.analysis.minimum_trio_hours changed")
    if distances != (0.5, 1.0, 2.0, 5.0, 10.0):
        raise ConfigError("micro_sensor_annual_panel.analysis.distance_bands_km changed")
    return AnnualMicroSensorAuditConfig(
        threads=1,
        memory_limit_gb=6,
        minimum_active_months=months,
        minimum_trio_dates=dates,
        minimum_trio_hours=hours,
        distance_bands_km=distances,
        extreme_ranges=tuple(extreme_ranges),
    )


def load_annual_micro_sensor_panel_config(
    config: dict[str, Any] | None = None,
) -> AnnualMicroSensorPanelConfig:
    raw = (
        config
        if config is not None
        else _mapping(load_conf("micro_sensor_annual_panel"), label="micro_sensor_annual_panel")
    )
    _exact_keys(
        raw,
        {
            "schema_version",
            "year",
            "catalog_generations",
            "parsed_generations",
            "catalogue_absent_dates",
            "analysis",
        },
        label="micro_sensor_annual_panel",
    )
    if isinstance(raw["schema_version"], bool) or raw["schema_version"] != 1:
        raise ConfigError("micro_sensor_annual_panel.schema_version must be one")
    if isinstance(raw["year"], bool) or raw["year"] != _YEAR:
        raise ConfigError(f"micro_sensor_annual_panel.year must be {_YEAR}")

    catalogs = _mapping(raw["catalog_generations"], label="catalog_generations")
    expected_months = {f"{_YEAR}{month:02d}" for month in range(1, 13)}
    if set(catalogs) != expected_months:
        raise ConfigError("catalog_generations must contain the twelve 2025 months")
    catalog_generations = tuple(
        (month, _identity(catalogs[month], label=f"catalog_generations.{month}"))
        for month in sorted(catalogs)
    )

    records_raw = raw["parsed_generations"]
    if not isinstance(records_raw, list):
        raise ConfigError("parsed_generations must be a list")
    records: list[AnnualParsedGeneration] = []
    for index, item in enumerate(records_raw):
        record = _mapping(item, label=f"parsed_generations[{index}]")
        _exact_keys(
            record,
            {"date", "generation_sha256"},
            label=f"parsed_generations[{index}]",
        )
        records.append(
            AnnualParsedGeneration(
                date=_iso_date(record["date"], label=f"parsed_generations[{index}].date"),
                generation_sha256=_identity(
                    record["generation_sha256"],
                    label=f"parsed_generations[{index}].generation_sha256",
                ),
            )
        )
    parsed_generations = tuple(records)
    parsed_dates = tuple(record.date for record in parsed_generations)
    parsed_ids = tuple(record.generation_sha256 for record in parsed_generations)
    if (
        len(parsed_generations) != _PARSED_DAYS
        or parsed_dates != tuple(sorted(set(parsed_dates)))
        or len(set(parsed_ids)) != _PARSED_DAYS
    ):
        raise ConfigError(
            "parsed_generations must contain 322 unique and sorted dates and identities"
        )

    absent_raw = raw["catalogue_absent_dates"]
    if not isinstance(absent_raw, list):
        raise ConfigError("catalogue_absent_dates must be a list")
    absent = tuple(
        _iso_date(value, label=f"catalogue_absent_dates[{index}]")
        for index, value in enumerate(absent_raw)
    )
    if len(absent) != _ABSENT_DAYS or absent != tuple(sorted(set(absent))):
        raise ConfigError("catalogue_absent_dates must contain 43 unique and sorted dates")

    calendar = {date(_YEAR, 1, 1) + timedelta(days=index) for index in range(365)}
    if set(parsed_dates).intersection(absent) or set(parsed_dates).union(absent) != calendar:
        raise ConfigError("parsed and absent dates must form the complete 2025 calendar partition")

    return AnnualMicroSensorPanelConfig(
        year=_YEAR,
        catalog_generations=catalog_generations,
        parsed_generations=parsed_generations,
        catalogue_absent_dates=absent,
        analysis=_load_analysis(raw["analysis"]),
    )


def _file_identity(path: Path) -> dict[str, object]:
    if not path.is_file():
        raise FileNotFoundError(f"annual micro-sensor input not found: {path}")
    return {
        "path": path.as_posix(),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
            default=str,
        )
        + "\n"
    ).encode()


def _hash_value(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{label} is unreadable") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} must be an object")
    return value


def _sql_path(path: Path) -> str:
    return path.as_posix().replace("'", "''")


def _validate_member_schema(path: Path, *, variable: str) -> None:
    try:
        observed = pl.read_parquet_schema(path)
    except (OSError, pl.exceptions.PolarsError) as exc:
        raise RuntimeError(f"annual micro-sensor {variable} member is unreadable") from exc
    if observed != dict(OBSERVATION_OUTPUT_SCHEMA):
        raise RuntimeError(f"annual micro-sensor {variable} member schema changed")


def _validate_member_rows(
    connection: duckdb.DuckDBPyConnection,
    *,
    variable: str,
    day: date,
) -> None:
    next_day = day + timedelta(days=1)
    counts = connection.execute(
        f"""
        SELECT
            count(*) FILTER (
                WHERE variable IS NULL OR variable != ?
            )::BIGINT AS wrong_variable,
            count(*) FILTER (
                WHERE device_id IS NULL OR trim(device_id) = ''
            )::BIGINT AS invalid_device,
            count(*) FILTER (
                WHERE ts_local IS NOT NULL
                  AND (ts_local < ?::TIMESTAMP OR ts_local >= ?::TIMESTAMP)
            )::BIGINT AS outside_day,
            count(*) FILTER (
                WHERE (value IS NOT NULL AND NOT isfinite(value))
                   OR (lon IS NOT NULL AND NOT isfinite(lon))
                   OR (lat IS NOT NULL AND NOT isfinite(lat))
            )::BIGINT AS nonfinite_numeric,
            count(*) FILTER (
                WHERE (coordinate_wgs84_valid IS NULL)
                    != (lon IS NULL OR lat IS NULL)
                   OR (coordinate_wgs84_valid IS TRUE AND (
                        lon IS NULL OR lat IS NULL
                        OR lon NOT BETWEEN -180 AND 180
                        OR lat NOT BETWEEN -90 AND 90
                   ))
                   OR (coordinate_wgs84_valid IS FALSE AND (
                        lon IS NULL OR lat IS NULL
                        OR (lon BETWEEN -180 AND 180 AND lat BETWEEN -90 AND 90)
                   ))
            )::BIGINT AS inconsistent_coordinate
        FROM {variable}_input
        """,
        (variable, day.isoformat(), next_day.isoformat()),
    ).fetchone()
    if counts is None:
        raise RuntimeError(f"annual micro-sensor {variable} validation returned no result")
    wrong_variable, invalid_device, outside_day, nonfinite_numeric, inconsistent_coordinate = (
        int(value) for value in counts
    )
    if wrong_variable:
        raise RuntimeError(f"annual micro-sensor {variable} variable identity changed")
    if invalid_device:
        raise RuntimeError(f"annual micro-sensor {variable} device identity is invalid")
    if outside_day:
        raise RuntimeError(f"annual micro-sensor {variable} timestamp outside {day.isoformat()}")
    if nonfinite_numeric:
        raise RuntimeError(f"annual micro-sensor {variable} has a non-finite numeric value")
    if inconsistent_coordinate:
        raise RuntimeError(f"annual micro-sensor {variable} coordinate state is inconsistent")


def _create_variable_tables(
    connection: duckdb.DuckDBPyConnection,
    *,
    variable: str,
    extreme_range: tuple[float, float],
) -> None:
    minimum, maximum = extreme_range
    connection.execute(
        f"""
        CREATE TEMP TABLE {variable}_device AS
        WITH duplicate_groups AS (
            SELECT device_id, count(*)::BIGINT AS duplicate_timestamp_groups
            FROM (
                SELECT device_id, ts_local
                FROM {variable}_input
                WHERE ts_local IS NOT NULL
                GROUP BY device_id, ts_local
                HAVING count(*) > 1
            ) groups
            GROUP BY device_id
        ), aggregated AS (
            SELECT device_id,
                   count(*)::BIGINT AS source_rows,
                   count(*) FILTER (WHERE value IS NULL)::BIGINT AS null_value_rows,
                   count(*) FILTER (WHERE ts_local IS NULL)::BIGINT AS null_timestamp_rows,
                   count(DISTINCT ts_local)::BIGINT AS distinct_timestamps,
                   count(DISTINCT date_trunc('hour', ts_local)) FILTER (
                       WHERE ts_local IS NOT NULL AND value IS NOT NULL
                   )::BIGINT AS observed_hours,
                   count(*) FILTER (
                       WHERE value IS NOT NULL AND (value < {minimum} OR value > {maximum})
                   )::BIGINT AS extreme_rows
            FROM {variable}_input
            GROUP BY device_id
        )
        SELECT aggregated.*,
               coalesce(duplicate_groups.duplicate_timestamp_groups, 0)::BIGINT
                   AS duplicate_timestamp_groups
        FROM aggregated LEFT JOIN duplicate_groups USING (device_id)
        """
    )
    connection.execute(
        f"""
        CREATE TEMP TABLE {variable}_hours AS
        SELECT DISTINCT device_id, date_trunc('hour', ts_local) AS hour
        FROM {variable}_input
        WHERE ts_local IS NOT NULL AND value IS NOT NULL
        """
    )


def _haversine(lon_1: str, lat_1: str, lon_2: str, lat_2: str) -> str:
    component = (
        f"pow(sin(radians(({lat_2}) - ({lat_1})) / 2), 2)"
        f" + cos(radians({lat_1})) * cos(radians({lat_2}))"
        f" * pow(sin(radians(({lon_2}) - ({lon_1})) / 2), 2)"
    )
    return f"2 * 6371.0088 * asin(sqrt(least(1.0, greatest(0.0, {component}))))"


def _validate_geography(geography: pl.DataFrame) -> None:
    expected = {"station_name": pl.String, "lon": pl.Float64, "lat": pl.Float64}
    if not set(expected).issubset(geography.columns) or any(
        geography.schema[name] != dtype for name, dtype in expected.items()
    ):
        raise RuntimeError("annual micro-sensor reviewed geography schema changed")
    selected = geography.select(*expected)
    invalid = selected.filter(
        pl.col("station_name").is_null()
        | (pl.col("station_name").str.strip_chars() == "")
        | pl.col("lon").is_null()
        | pl.col("lat").is_null()
        | ~pl.col("lon").is_finite()
        | ~pl.col("lat").is_finite()
        | ~pl.col("lon").is_between(-180, 180, closed="both")
        | ~pl.col("lat").is_between(-90, 90, closed="both")
    )
    if invalid.height or selected["station_name"].n_unique() != selected.height:
        raise RuntimeError("annual micro-sensor reviewed geography is invalid")


def _validate_ground_schema(path: Path) -> None:
    try:
        observed = pl.read_parquet_schema(path)
    except (OSError, pl.exceptions.PolarsError) as exc:
        raise RuntimeError("annual micro-sensor ground member is unreadable") from exc
    if set(observed) != set(PARTITION_SCHEMA) or any(
        observed[name] != expected for name, expected in PARTITION_SCHEMA.items()
    ):
        raise RuntimeError("annual micro-sensor ground member schema changed")


def _create_context_tables(
    connection: duckdb.DuckDBPyConnection,
    *,
    day: date,
    geography: pl.DataFrame | None,
    ground_path: Path | None,
) -> None:
    bounds = TAIWAN_BOUNDS
    connection.execute(
        f"""
        CREATE TEMP TABLE daily_coordinates AS
        SELECT device_id,
               count(*)::BIGINT AS source_rows,
               count(*) FILTER (WHERE lon IS NULL OR lat IS NULL)::BIGINT AS null_rows,
               count(*) FILTER (WHERE coordinate_wgs84_valid IS FALSE)::BIGINT
                   AS invalid_rows,
               count(DISTINCT struct_pack(lon := lon, lat := lat)) FILTER (
                   WHERE coordinate_wgs84_valid IS TRUE
               )::BIGINT AS positions,
               min(lon) FILTER (WHERE coordinate_wgs84_valid IS TRUE)::DOUBLE AS lon_min,
               max(lon) FILTER (WHERE coordinate_wgs84_valid IS TRUE)::DOUBLE AS lon_max,
               min(lat) FILTER (WHERE coordinate_wgs84_valid IS TRUE)::DOUBLE AS lat_min,
               max(lat) FILTER (WHERE coordinate_wgs84_valid IS TRUE)::DOUBLE AS lat_max,
               CASE
                   WHEN count(*) FILTER (WHERE lon IS NULL OR lat IS NULL) > 0
                     OR count(*) FILTER (WHERE coordinate_wgs84_valid IS FALSE) > 0
                       THEN 'invalid_or_null_coordinate'
                   WHEN count(DISTINCT struct_pack(lon := lon, lat := lat)) FILTER (
                       WHERE coordinate_wgs84_valid IS TRUE
                   ) != 1 THEN 'moving_coordinate'
                   WHEN min(lon) FILTER (WHERE coordinate_wgs84_valid IS TRUE)
                            NOT BETWEEN {bounds["lon_min"]} AND {bounds["lon_max"]}
                     OR min(lat) FILTER (WHERE coordinate_wgs84_valid IS TRUE)
                            NOT BETWEEN {bounds["lat_min"]} AND {bounds["lat_max"]}
                       THEN 'outside_taiwan'
                   ELSE 'eligible'
               END AS spatial_state
        FROM pm25_input GROUP BY device_id
        """
    )
    if geography is None or ground_path is None:
        connection.execute(
            """
            CREATE TEMP TABLE nearest_reference AS
            SELECT CAST(NULL AS VARCHAR) AS device_id,
                   CAST(NULL AS VARCHAR) AS station_name,
                   CAST(NULL AS DOUBLE) AS distance_km WHERE FALSE;
            CREATE TEMP TABLE ground_counts AS
            SELECT CAST(NULL AS VARCHAR) AS device_id,
                   0::BIGINT AS present_hours, 0::BIGINT AS eligible_hours,
                   0::BIGINT AS present_ineligible_hours, 0::BIGINT AS absent_hours
            WHERE FALSE;
            """
        )
        return

    _validate_geography(geography)
    _validate_ground_schema(ground_path)
    connection.register(
        "reference_stations", geography.select("station_name", "lon", "lat").to_arrow()
    )
    distance = _haversine(
        "coordinates.lon_min", "coordinates.lat_min", "stations.lon", "stations.lat"
    )
    connection.execute(
        f"""
        CREATE TEMP TABLE nearest_reference AS
        SELECT device_id, station_name, distance_km FROM (
            SELECT coordinates.device_id, stations.station_name,
                   {distance}::DOUBLE AS distance_km,
                   row_number() OVER (
                       PARTITION BY coordinates.device_id
                       ORDER BY {distance}, stations.station_name
                   ) AS rank
            FROM daily_coordinates coordinates CROSS JOIN reference_stations stations
            WHERE coordinates.spatial_state = 'eligible'
        ) ranked WHERE rank = 1
        """
    )
    next_day = day + timedelta(days=1)
    connection.execute(
        f"""
        CREATE TEMP TABLE ground_pm25 AS
        SELECT CAST(station_name AS VARCHAR) AS station_name, ts_local AS hour,
               value::DOUBLE AS ground_pm25, CAST(flag AS VARCHAR) AS ground_flag
        FROM read_parquet('{_sql_path(ground_path)}')
        WHERE CAST(pollutant AS VARCHAR) = 'PM2.5'
          AND ts_local >= TIMESTAMP '{day.isoformat()} 00:00:00'
          AND ts_local < TIMESTAMP '{next_day.isoformat()} 00:00:00'
        """
    )
    invalid = connection.execute(
        """
        SELECT
            count(*) FILTER (WHERE ground_pm25 IS NOT NULL AND NOT isfinite(ground_pm25)),
            count(*) FILTER (WHERE station_name IS NULL OR trim(station_name) = ''),
            (SELECT count(*) FROM (
                SELECT station_name, hour FROM ground_pm25 GROUP BY ALL HAVING count(*) > 1
            ))
        FROM ground_pm25
        """
    ).fetchone()
    if invalid is None or any(int(value) > 0 for value in invalid):
        raise RuntimeError("annual micro-sensor ground PM2.5 member is invalid")
    connection.execute(
        """
        CREATE TEMP TABLE ground_counts AS
        WITH trio AS (
            SELECT pm25.device_id, pm25.hour
            FROM pm25_hours pm25
            INNER JOIN humidity_hours humidity USING (device_id, hour)
            INNER JOIN temperature_hours temperature USING (device_id, hour)
        )
        SELECT trio.device_id,
               count(*) FILTER (WHERE ground.hour IS NOT NULL)::BIGINT AS present_hours,
               count(*) FILTER (
                   WHERE ground.hour IS NOT NULL AND ground.ground_flag = 'valid'
                     AND ground.ground_pm25 IS NOT NULL
               )::BIGINT AS eligible_hours,
               count(*) FILTER (
                   WHERE ground.hour IS NOT NULL AND NOT coalesce(
                       ground.ground_flag = 'valid' AND ground.ground_pm25 IS NOT NULL,
                       false
                   )
               )::BIGINT AS present_ineligible_hours,
               count(*) FILTER (WHERE ground.hour IS NULL)::BIGINT AS absent_hours
        FROM trio
        INNER JOIN nearest_reference nearest USING (device_id)
        LEFT JOIN ground_pm25 ground
          ON ground.station_name = nearest.station_name AND ground.hour = trio.hour
        GROUP BY trio.device_id
        """
    )


def _device_day_query(day: date) -> str:
    metrics = ",\n".join(
        f"coalesce({variable}.{metric}, 0)::BIGINT AS {variable}_{metric}"
        for variable in _VARIABLES
        for metric in _VARIABLE_METRICS
    )
    return f"""
        WITH devices AS (
            SELECT device_id FROM pm25_device
            UNION SELECT device_id FROM humidity_device
            UNION SELECT device_id FROM temperature_device
        ), trio AS (
            SELECT pm25.device_id, count(*)::BIGINT AS observed_hours
            FROM pm25_hours pm25
            INNER JOIN humidity_hours humidity USING (device_id, hour)
            INNER JOIN temperature_hours temperature USING (device_id, hour)
            GROUP BY pm25.device_id
        )
        SELECT DATE '{day.isoformat()}' AS date, devices.device_id,
               {metrics},
               coalesce(trio.observed_hours, 0)::BIGINT AS trio_observed_hours,
               coalesce(coordinates.source_rows, 0)::BIGINT AS coordinate_source_rows,
               coalesce(coordinates.null_rows, 0)::BIGINT AS coordinate_null_rows,
               coalesce(coordinates.invalid_rows, 0)::BIGINT AS coordinate_invalid_rows,
               coalesce(coordinates.positions, 0)::BIGINT AS coordinate_positions,
               coordinates.lon_min, coordinates.lon_max,
               coordinates.lat_min, coordinates.lat_max,
               coalesce(coordinates.spatial_state, 'missing_pm25_coordinate') AS spatial_state,
               nearest.station_name, nearest.distance_km,
               coalesce(ground.present_hours, 0)::BIGINT AS ground_present_trio_hours,
               coalesce(ground.eligible_hours, 0)::BIGINT AS ground_eligible_trio_hours,
               coalesce(ground.present_ineligible_hours, 0)::BIGINT
                   AS ground_present_ineligible_trio_hours,
               coalesce(ground.absent_hours, 0)::BIGINT AS ground_absent_trio_hours
        FROM devices
        LEFT JOIN pm25_device pm25 USING (device_id)
        LEFT JOIN humidity_device humidity USING (device_id)
        LEFT JOIN temperature_device temperature USING (device_id)
        LEFT JOIN trio USING (device_id)
        LEFT JOIN daily_coordinates coordinates USING (device_id)
        LEFT JOIN nearest_reference nearest USING (device_id)
        LEFT JOIN ground_counts ground USING (device_id)
        ORDER BY devices.device_id
    """


def aggregate_micro_sensor_day(
    *,
    day: date,
    member_paths: dict[str, Path],
    geography: pl.DataFrame | None = None,
    ground_path: Path | None = None,
    config: AnnualMicroSensorAuditConfig,
    temp_dir: Path,
) -> pl.DataFrame:
    if set(member_paths) != set(_VARIABLES):
        raise RuntimeError("annual micro-sensor aggregation requires all three parsed variables")
    if config.threads != 1 or config.memory_limit_gb != 6:
        raise RuntimeError("annual micro-sensor aggregation resource limits changed")
    if (geography is None) != (ground_path is None):
        raise RuntimeError("annual micro-sensor geography and ground inputs must be paired")
    ordered_paths = tuple(member_paths[variable] for variable in _VARIABLES)
    identity_paths = (*ordered_paths, *((ground_path,) if ground_path is not None else ()))
    before = tuple(_file_identity(path) for path in identity_paths)
    extreme_ranges = dict(config.extreme_ranges)
    for variable, path in zip(_VARIABLES, ordered_paths, strict=True):
        _validate_member_schema(path, variable=variable)

    temp_dir.mkdir(parents=True, exist_ok=False)
    connection = duckdb.connect()
    try:
        connection.execute(f"SET threads={config.threads}")
        connection.execute(f"SET memory_limit='{config.memory_limit_gb}GB'")
        connection.execute(f"SET temp_directory='{_sql_path(temp_dir)}'")
        connection.execute("SET preserve_insertion_order=false")
        for variable, path in zip(_VARIABLES, ordered_paths, strict=True):
            connection.execute(
                f"CREATE TEMP VIEW {variable}_input AS "
                f"SELECT * FROM read_parquet('{_sql_path(path)}')"
            )
            _validate_member_rows(connection, variable=variable, day=day)
            _create_variable_tables(
                connection,
                variable=variable,
                extreme_range=extreme_ranges[variable],
            )
        _create_context_tables(
            connection,
            day=day,
            geography=geography,
            ground_path=ground_path,
        )
        result = pl.DataFrame(connection.execute(_device_day_query(day)).to_arrow_table())
    finally:
        connection.close()
        shutil.rmtree(temp_dir, ignore_errors=True)

    after = tuple(_file_identity(path) for path in identity_paths)
    if before != after:
        raise RuntimeError("an annual micro-sensor input changed while it was read")
    try:
        return result.cast(dict(ANNUAL_DEVICE_DAY_SCHEMA), strict=True).select(
            *(name for name, _ in ANNUAL_DEVICE_DAY_SCHEMA)
        )
    except (pl.exceptions.PolarsError, TypeError) as exc:
        raise RuntimeError("annual micro-sensor device-day schema changed") from exc


def _validate_device_day_frame(frame: pl.DataFrame, *, day: date | None = None) -> None:
    if frame.schema != dict(ANNUAL_DEVICE_DAY_SCHEMA):
        raise RuntimeError("annual micro-sensor device-day schema changed")
    if (
        day is not None
        and frame.height
        and (frame["date"].null_count() or set(frame["date"].unique().to_list()) != {day})
    ):
        raise RuntimeError("annual micro-sensor device-day date changed")
    if frame["date"].null_count() or frame["device_id"].null_count():
        raise RuntimeError("annual micro-sensor device-day identities changed")
    if day is not None and frame["device_id"].n_unique() != frame.height:
        raise RuntimeError("annual micro-sensor device-day identities changed")
    integer_columns = [name for name, dtype in ANNUAL_DEVICE_DAY_SCHEMA if dtype == pl.Int64]
    if any(frame[column].null_count() or (frame[column] < 0).any() for column in integer_columns):
        raise RuntimeError("annual micro-sensor device-day counts must be nonnegative")
    nonfinite = frame.select(
        pl.any_horizontal(
            pl.col(column).is_not_null() & ~pl.col(column).is_finite()
            for column in ("lon_min", "lon_max", "lat_min", "lat_max", "distance_km")
        ).sum()
    ).item()
    if nonfinite:
        raise RuntimeError("annual micro-sensor device-day coordinates must be finite or null")


def _checkpoint_directory(checkpoint_root: Path, day: date) -> Path:
    return checkpoint_root / day.isoformat()


def _checkpoint_contract(
    *,
    day: date,
    parsed_generation_sha256: str,
    input_files: tuple[dict[str, object], ...],
    panel_sha256: str,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "annual_micro_sensor_device_day",
        "day": day.isoformat(),
        "parsed_generation_sha256": _identity(
            parsed_generation_sha256,
            label="annual device-day parsed generation",
        ),
        "panel_sha256": _identity(panel_sha256, label="annual device-day panel"),
        "input_files": [dict(item) for item in input_files],
    }


def load_annual_device_day_checkpoint(
    *,
    day: date,
    parsed_generation_sha256: str,
    input_files: tuple[dict[str, object], ...],
    panel_sha256: str,
    checkpoint_root: Path,
) -> pl.DataFrame:
    directory = _checkpoint_directory(checkpoint_root, day)
    manifest_path = directory / "manifest.json"
    member_path = directory / "device_days.parquet"
    if not directory.is_dir():
        raise FileNotFoundError(f"annual micro-sensor checkpoint is missing: {day}")
    manifest = _read_json(manifest_path, label="annual micro-sensor checkpoint manifest")
    contract = _checkpoint_contract(
        day=day,
        parsed_generation_sha256=parsed_generation_sha256,
        input_files=input_files,
        panel_sha256=panel_sha256,
    )
    if any(manifest.get(key) != value for key, value in contract.items()):
        raise RuntimeError("annual micro-sensor checkpoint input identity changed")
    member = manifest.get("member")
    if not isinstance(member, dict):
        raise RuntimeError("annual micro-sensor checkpoint member identity is missing")
    observed = _file_identity(member_path)
    expected_member = {
        "path": "device_days.parquet",
        "bytes": observed["bytes"],
        "sha256": observed["sha256"],
    }
    if member != expected_member:
        raise RuntimeError("annual micro-sensor checkpoint checksum changed")
    try:
        frame = pl.read_parquet(member_path)
    except (OSError, pl.exceptions.PolarsError) as exc:
        raise RuntimeError("annual micro-sensor checkpoint member is unreadable") from exc
    _validate_device_day_frame(frame, day=day)
    if manifest.get("rows") != frame.height:
        raise RuntimeError("annual micro-sensor checkpoint row count changed")
    return frame


def write_annual_device_day_checkpoint(
    frame: pl.DataFrame,
    *,
    day: date,
    parsed_generation_sha256: str,
    input_files: tuple[dict[str, object], ...],
    panel_sha256: str,
    checkpoint_root: Path,
) -> Path:
    _validate_device_day_frame(frame, day=day)
    destination = _checkpoint_directory(checkpoint_root, day)
    if destination.exists():
        load_annual_device_day_checkpoint(
            day=day,
            parsed_generation_sha256=parsed_generation_sha256,
            input_files=input_files,
            panel_sha256=panel_sha256,
            checkpoint_root=checkpoint_root,
        )
        return destination / "device_days.parquet"
    checkpoint_root.mkdir(parents=True, exist_ok=True)
    for residue in checkpoint_root.glob(f".{day.isoformat()}.staging-*"):
        shutil.rmtree(residue)
    staged = checkpoint_root / f".{day.isoformat()}.staging-{uuid4().hex}"
    staged.mkdir()
    try:
        member_path = staged / "device_days.parquet"
        frame.write_parquet(member_path)
        identity = _file_identity(member_path)
        manifest = {
            **_checkpoint_contract(
                day=day,
                parsed_generation_sha256=parsed_generation_sha256,
                input_files=input_files,
                panel_sha256=panel_sha256,
            ),
            "rows": frame.height,
            "member": {
                "path": "device_days.parquet",
                "bytes": identity["bytes"],
                "sha256": identity["sha256"],
            },
            "complete": True,
        }
        _write_json(staged / "manifest.json", manifest)
        staged.replace(destination)
    finally:
        if staged.exists():
            shutil.rmtree(staged)
    return destination / "device_days.parquet"


def _annual_checkpoint_evidence(directory: Path, *, day: date) -> dict[str, object]:
    manifest_path = directory / "manifest.json"
    member_path = directory / "device_days.parquet"
    manifest = _read_json(manifest_path, label="annual micro-sensor checkpoint manifest")
    rows = manifest.get("rows")
    if not isinstance(rows, int) or rows < 0:
        raise RuntimeError("annual micro-sensor checkpoint row count changed")
    manifest_identity = _file_identity(manifest_path)
    member_identity = _file_identity(member_path)
    return {
        "date": day.isoformat(),
        "rows": rows,
        "manifest_bytes": manifest_identity["bytes"],
        "manifest_sha256": manifest_identity["sha256"],
        "member_bytes": member_identity["bytes"],
        "member_sha256": member_identity["sha256"],
    }


def _cohort_query() -> str:
    metric_sums = ",\n".join(
        f"sum({variable}_{metric})::BIGINT AS {variable}_{metric}"
        for variable in _VARIABLES
        for metric in _VARIABLE_METRICS
    )
    return f"""
        WITH aggregated AS (
            SELECT device_id,
                   count(DISTINCT date)::BIGINT AS active_dates,
                   count(DISTINCT strftime(date, '%Y-%m'))::BIGINT AS active_months,
                   count(DISTINCT date) FILTER (WHERE trio_observed_hours > 0)::BIGINT
                       AS trio_dates,
                   {metric_sums},
                   sum(trio_observed_hours)::BIGINT AS trio_observed_hours,
                   sum(coordinate_source_rows)::BIGINT AS coordinate_source_rows,
                   sum(coordinate_null_rows)::BIGINT AS coordinate_null_rows,
                   sum(coordinate_invalid_rows)::BIGINT AS coordinate_invalid_rows,
                   min(lon_min)::DOUBLE AS lon_min, max(lon_max)::DOUBLE AS lon_max,
                   min(lat_min)::DOUBLE AS lat_min, max(lat_max)::DOUBLE AS lat_max,
                   max(coordinate_positions)::BIGINT AS maximum_daily_positions,
                   count(DISTINCT station_name)::BIGINT AS reference_stations,
                   min(station_name) AS station_name,
                   max(distance_km)::DOUBLE AS distance_km,
                   count(*) FILTER (WHERE spatial_state = 'outside_taiwan')::BIGINT
                       AS outside_dates,
                   sum(ground_present_trio_hours)::BIGINT AS ground_present_trio_hours,
                   sum(ground_eligible_trio_hours)::BIGINT AS ground_eligible_trio_hours,
                   sum(ground_present_ineligible_trio_hours)::BIGINT
                       AS ground_present_ineligible_trio_hours,
                   sum(ground_absent_trio_hours)::BIGINT AS ground_absent_trio_hours
            FROM device_days GROUP BY device_id
        ), classified AS (
            SELECT *, CASE
                WHEN coordinate_source_rows = 0 THEN 'missing_pm25_coordinate'
                WHEN coordinate_null_rows > 0 OR coordinate_invalid_rows > 0
                    THEN 'invalid_or_null_coordinate'
                WHEN maximum_daily_positions != 1 OR lon_min != lon_max OR lat_min != lat_max
                    THEN 'moving_coordinate'
                WHEN outside_dates > 0 THEN 'outside_taiwan'
                WHEN reference_stations != 1 THEN 'reference_station_changed'
                ELSE 'eligible'
            END AS spatial_state
            FROM aggregated
        )
        SELECT * EXCLUDE (maximum_daily_positions, reference_stations, outside_dates),
               CASE
                   WHEN spatial_state != 'eligible' THEN spatial_state
                   WHEN active_months < 3 THEN 'fewer_than_3_active_months'
                   WHEN trio_dates < 30 THEN 'fewer_than_30_trio_dates'
                   WHEN trio_observed_hours < 360 THEN 'fewer_than_360_trio_hours'
                   WHEN distance_km IS NULL THEN 'missing_reference_station'
                   WHEN distance_km > 10 THEN 'beyond_10km'
                   ELSE 'eligible_at_relaxed_threshold'
               END AS exclusion_reason
        FROM classified ORDER BY device_id
    """


def _summarize_registered_device_days(
    connection: duckdb.DuckDBPyConnection,
    *,
    config: AnnualMicroSensorAuditConfig,
) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    cohorts = (
        pl.DataFrame(connection.execute(_cohort_query()).to_arrow_table())
        .cast(dict(ANNUAL_DEVICE_COHORT_SCHEMA), strict=True)
        .select(*(name for name, _ in ANNUAL_DEVICE_COHORT_SCHEMA))
    )
    month_values = ",".join(f"({value})" for value in config.minimum_active_months)
    date_values = ",".join(f"({value})" for value in config.minimum_trio_dates)
    hour_values = ",".join(f"({value})" for value in config.minimum_trio_hours)
    distance_values = ",".join(f"({value})" for value in config.distance_bands_km)
    connection.register("cohorts", cohorts.to_arrow())
    thresholds = pl.DataFrame(
        connection.execute(
            f"""
            WITH months(value) AS (VALUES {month_values}),
                 dates(value) AS (VALUES {date_values}),
                 hours(value) AS (VALUES {hour_values}),
                 distances(value) AS (VALUES {distance_values})
            SELECT months.value::BIGINT AS minimum_active_months,
                   dates.value::BIGINT AS minimum_trio_dates,
                   hours.value::BIGINT AS minimum_trio_hours,
                   distances.value::DOUBLE AS distance_km,
                   count(*) FILTER (
                       WHERE spatial_state = 'eligible'
                         AND active_months >= months.value
                         AND trio_dates >= dates.value
                         AND trio_observed_hours >= hours.value
                         AND cohorts.distance_km <= distances.value
                   )::BIGINT AS devices
            FROM months CROSS JOIN dates CROSS JOIN hours CROSS JOIN distances
            CROSS JOIN cohorts
            GROUP BY ALL ORDER BY ALL
            """
        ).to_arrow_table()
    ).cast(dict(ANNUAL_COHORT_THRESHOLD_SCHEMA), strict=True)
    exclusions = pl.DataFrame(
        connection.execute(
            """
            SELECT exclusion_reason AS reason, count(*)::BIGINT AS devices
            FROM cohorts GROUP BY exclusion_reason ORDER BY exclusion_reason
            """
        ).to_arrow_table()
    ).cast(dict(ANNUAL_EXCLUSION_SCHEMA), strict=True)
    return cohorts, thresholds, exclusions


def summarize_annual_micro_sensor_cohorts(
    device_days: pl.DataFrame,
    *,
    config: AnnualMicroSensorAuditConfig,
) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    _validate_device_day_frame(device_days)
    duplicates = device_days.group_by("device_id", "date").len().filter(pl.col("len") > 1)
    if duplicates.height:
        raise RuntimeError("annual micro-sensor device-day keys are duplicated")
    if device_days.filter(pl.col("date").dt.year() != _YEAR).height:
        raise RuntimeError("annual micro-sensor device-day date is outside 2025")
    connection = duckdb.connect()
    connection.execute(f"SET threads={config.threads}")
    connection.execute(f"SET memory_limit='{config.memory_limit_gb}GB'")
    try:
        connection.register("device_days", device_days.to_arrow())
        return _summarize_registered_device_days(connection, config=config)
    finally:
        connection.close()


def _summarize_annual_micro_sensor_checkpoint_paths(
    checkpoint_paths: tuple[Path, ...],
    *,
    config: AnnualMicroSensorAuditConfig,
    temp_dir: Path,
) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame, int]:
    if not checkpoint_paths:
        raise RuntimeError("annual micro-sensor checkpoints are missing")
    for path in checkpoint_paths:
        if pl.read_parquet_schema(path) != dict(ANNUAL_DEVICE_DAY_SCHEMA):
            raise RuntimeError("annual micro-sensor checkpoint member schema changed")
    path_list = ", ".join(f"'{_sql_path(path)}'" for path in checkpoint_paths)
    temp_dir.mkdir(parents=True, exist_ok=False)
    connection = duckdb.connect()
    try:
        connection.execute(f"SET threads={config.threads}")
        connection.execute(f"SET memory_limit='{config.memory_limit_gb}GB'")
        connection.execute(f"SET temp_directory='{_sql_path(temp_dir)}'")
        connection.execute("SET preserve_insertion_order=false")
        connection.execute(
            f"CREATE TEMP VIEW device_days AS SELECT * FROM read_parquet([{path_list}])"
        )
        counted = connection.execute("SELECT count(*) FROM device_days").fetchone()
        if counted is None:
            raise RuntimeError("annual micro-sensor checkpoint reduction returned no count")
        rows = int(counted[0])
        cohorts, thresholds, exclusions = _summarize_registered_device_days(
            connection,
            config=config,
        )
        return cohorts, thresholds, exclusions, rows
    finally:
        connection.close()
        shutil.rmtree(temp_dir, ignore_errors=True)


def _write_annual_device_days_from_checkpoints(
    checkpoint_paths: tuple[Path, ...],
    *,
    destination: Path,
    config: AnnualMicroSensorAuditConfig,
    temp_dir: Path,
) -> int:
    if not checkpoint_paths:
        raise RuntimeError("annual micro-sensor checkpoints are missing")
    path_list = ", ".join(f"'{_sql_path(path)}'" for path in checkpoint_paths)
    temp_dir.mkdir(parents=True, exist_ok=False)
    connection = duckdb.connect()
    try:
        connection.execute(f"SET threads={config.threads}")
        connection.execute(f"SET memory_limit='{config.memory_limit_gb}GB'")
        connection.execute(f"SET temp_directory='{_sql_path(temp_dir)}'")
        connection.execute("SET preserve_insertion_order=false")
        copied = connection.execute(
            f"""
            COPY (
                SELECT * FROM read_parquet([{path_list}]) ORDER BY date, device_id
            ) TO '{_sql_path(destination)}' (FORMAT PARQUET)
            """
        ).fetchone()
        if copied is None:
            raise RuntimeError("annual micro-sensor device-day materialization returned no count")
        return int(copied[0])
    finally:
        connection.close()
        shutil.rmtree(temp_dir, ignore_errors=True)


def _calendar_coverage(panel: AnnualMicroSensorPanelConfig) -> pl.DataFrame:
    parsed = {record.date: record.generation_sha256 for record in panel.parsed_generations}
    absent = set(panel.catalogue_absent_dates)
    catalogs = dict(panel.catalog_generations)
    rows = []
    for index in range(365):
        observed_day = date(panel.year, 1, 1) + timedelta(days=index)
        rows.append(
            {
                "date": observed_day,
                "state": "catalogue_absent" if observed_day in absent else "complete",
                "catalog_generation_sha256": catalogs[observed_day.strftime("%Y%m")],
                "parsed_generation_sha256": parsed.get(observed_day),
            }
        )
    return pl.DataFrame(rows, schema=dict(ANNUAL_CALENDAR_SCHEMA))


def _portable_identity(path: Path, *, root: Path) -> dict[str, object]:
    identity = _file_identity(path)
    try:
        portable = path.relative_to(root).as_posix()
    except ValueError as exc:
        raise RuntimeError(f"annual micro-sensor input is outside the data root: {path}") from exc
    return {**identity, "path": portable}


def _geography_identity(geography: pl.DataFrame) -> str:
    fields = (
        "station_name",
        "lon",
        "lat",
        "geo_source",
        "geo_source_record_namespace",
        "geo_source_record_id",
    )
    if not set(fields).issubset(geography.columns):
        raise RuntimeError("annual micro-sensor geography provenance columns are missing")
    return _hash_value(geography.select(*fields).sort("station_name").to_dicts())


@contextmanager
def _exclusive_run_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = path.open("a+b")
    acquired = False
    try:
        if path.stat().st_size == 0:
            lock_file.write(b"\0")
            lock_file.flush()
        lock_file.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError("another annual micro-sensor readiness run is active") from None
        acquired = True
        yield
    finally:
        try:
            if acquired:
                lock_file.seek(0)
                if sys.platform == "win32":
                    import msvcrt

                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            lock_file.close()


def _load_reviewed_physical_inputs(
    *,
    root: Path,
    panel: AnnualMicroSensorPanelConfig,
) -> tuple[pl.DataFrame, list[dict[str, object]]]:
    catalog_raw = root / "raw" / "micro_sensors" / "catalog" / "generations"
    catalog_interim = root / "interim" / "micro_sensors" / "catalog" / "generations"
    for month, generation in panel.catalog_generations:
        loaded = load_catalog_generation(
            generation,
            raw_root=catalog_raw,
            interim_root=catalog_interim,
        )
        archive_catalog = loaded.manifest.get("archive_catalog")
        if not isinstance(archive_catalog, dict) or archive_catalog.get("month") != month:
            raise RuntimeError("annual micro-sensor catalog month identity changed")
    geography = resolve_station_geo()
    ground_files = [
        root
        / "processed"
        / "observations"
        / f"year={panel.year}"
        / f"month={month:02d}"
        / "part-0.parquet"
        for month in range(1, 13)
    ]
    ground_identities = [_portable_identity(path, root=root) for path in ground_files]
    return geography, ground_identities


def _prepare_annual_micro_sensor_readiness(
    *,
    data_root: Path | None = None,
    panel: AnnualMicroSensorPanelConfig | None = None,
    generated_at: str | None = None,
    _lock_held: bool = False,
) -> AnnualMicroSensorReadinessResult:
    root = data_root or configured_data_root()
    if root.resolve() != configured_data_root().resolve():
        raise RuntimeError("annual micro-sensor data_root must match the configured data root")
    selected = panel or load_annual_micro_sensor_panel_config()
    panel_sha256 = _hash_value(asdict(selected))
    checkpoint_root = root / "interim" / "micro_sensor_annual_readiness" / panel_sha256 / "days"
    parsed_root = root / "interim" / "micro_sensors" / "observations" / "generations"
    geography, ground_inputs_before = _load_reviewed_physical_inputs(root=root, panel=selected)
    geography_sha256 = _geography_identity(geography)
    checkpoint_paths: list[Path] = []
    checkpoint_inventory: list[dict[str, object]] = []
    checkpoint_run: list[dict[str, str]] = []
    parsed_inputs: list[dict[str, object]] = []
    lock_context = (
        nullcontext() if _lock_held else _exclusive_run_lock(checkpoint_root.parent / ".run.lock")
    )
    with lock_context:
        for record in selected.parsed_generations:
            loaded = load_micro_sensor_observation_generation(
                record.generation_sha256,
                interim_observation_root=parsed_root,
            )
            if loaded.manifest.get("date") != record.date.isoformat():
                raise RuntimeError("annual micro-sensor parsed generation date changed")
            member_paths = {
                variable: loaded.directory / f"{variable}.parquet" for variable in _VARIABLES
            }
            ground_path = (
                root
                / "processed"
                / "observations"
                / f"year={selected.year}"
                / f"month={record.date.month:02d}"
                / "part-0.parquet"
            )
            input_paths = (*member_paths.values(), ground_path)
            input_files = tuple(_portable_identity(path, root=root) for path in input_paths)
            parsed_inputs.append(
                {
                    "date": record.date.isoformat(),
                    "generation_sha256": record.generation_sha256,
                    "input_files": list(input_files[:3]),
                }
            )
            checkpoint_state = "reused"
            try:
                load_annual_device_day_checkpoint(
                    day=record.date,
                    parsed_generation_sha256=record.generation_sha256,
                    input_files=input_files,
                    panel_sha256=panel_sha256,
                    checkpoint_root=checkpoint_root,
                )
            except FileNotFoundError:
                checkpoint_state = "created"
                frame = aggregate_micro_sensor_day(
                    day=record.date,
                    member_paths=member_paths,
                    geography=geography,
                    ground_path=ground_path,
                    config=selected.analysis,
                    temp_dir=checkpoint_root.parent / f".duckdb-{record.date.isoformat()}",
                )
                after = tuple(_portable_identity(path, root=root) for path in input_paths)
                if input_files != after:
                    raise RuntimeError(
                        "annual micro-sensor inputs changed before checkpointing"
                    ) from None
                write_annual_device_day_checkpoint(
                    frame,
                    day=record.date,
                    parsed_generation_sha256=record.generation_sha256,
                    input_files=input_files,
                    panel_sha256=panel_sha256,
                    checkpoint_root=checkpoint_root,
                )
            checkpoint_paths.append(
                _checkpoint_directory(checkpoint_root, record.date) / "device_days.parquet"
            )
            checkpoint_inventory.append(
                _annual_checkpoint_evidence(
                    _checkpoint_directory(checkpoint_root, record.date),
                    day=record.date,
                )
            )
            checkpoint_run.append({"date": record.date.isoformat(), "state": checkpoint_state})

        cohorts, thresholds, exclusions, device_day_rows = (
            _summarize_annual_micro_sensor_checkpoint_paths(
                tuple(checkpoint_paths),
                config=selected.analysis,
                temp_dir=checkpoint_root.parent / ".annual-duckdb",
            )
        )

    ground_inputs_after = [
        _portable_identity(root / str(item["path"]), root=root) for item in ground_inputs_before
    ]
    if ground_inputs_before != ground_inputs_after:
        raise RuntimeError("annual micro-sensor ground inputs changed during analysis")
    if _geography_identity(resolve_station_geo()) != geography_sha256:
        raise RuntimeError("annual micro-sensor geography changed during analysis")
    calendar = _calendar_coverage(selected)
    output_rows = {
        "calendar_coverage": calendar.height,
        "device_days": device_day_rows,
        "device_cohorts": cohorts.height,
        "cohort_thresholds": thresholds.height,
        "exclusions": exclusions.height,
    }
    summary = {
        "calendar": {
            "complete_dates": len(selected.parsed_generations),
            "catalogue_absent_dates": len(selected.catalogue_absent_dates),
        },
        "devices": cohorts.height,
        "threshold_grid_rows": thresholds.height,
        "output_rows": output_rows,
    }
    measured_sha, measured_dirty = git_state()
    inputs = {
        "panel_sha256": panel_sha256,
        "catalog_generations": dict(selected.catalog_generations),
        "parsed_generations": parsed_inputs,
        "ground_files": ground_inputs_before,
        "reviewed_geography_sha256": geography_sha256,
    }
    identity_payload = {
        "schema_version": 1,
        "analysis": "annual_micro_sensor_readiness",
        "config": asdict(selected.analysis),
        "inputs": inputs,
        "checkpoint_inventory": checkpoint_inventory,
        "claim_boundary": _CLAIM_BOUNDARY,
    }
    manifest = {
        **identity_payload,
        "complete": True,
        "generated_at": generated_at or datetime.now(UTC).isoformat(timespec="seconds"),
        "output_rows": output_rows,
        "generation_sha256": _hash_value(identity_payload),
        "git_sha": measured_sha,
        "git_dirty": measured_dirty,
        "checkpoint_run": checkpoint_run,
    }
    return AnnualMicroSensorReadinessResult(
        calendar_coverage=calendar,
        device_days=tuple(checkpoint_paths),
        device_cohorts=cohorts,
        cohort_thresholds=thresholds,
        exclusions=exclusions,
        summary=summary,
        manifest=manifest,
    )


def annual_micro_sensor_readiness_dir(*, generation_sha256: str) -> Path:
    generation = _identity(generation_sha256, label="annual micro-sensor output generation")
    return outputs_dir("micro_sensor_annual_readiness") / "generations" / generation


def _final_generation_identity(manifest: dict[str, Any]) -> dict[str, object]:
    fields = (
        "schema_version",
        "analysis",
        "config",
        "inputs",
        "checkpoint_inventory",
        "claim_boundary",
        "output_rows",
        "members",
        "summary_file",
    )
    missing = [field for field in fields if field not in manifest]
    if missing:
        raise RuntimeError(f"annual micro-sensor generation identity is missing: {missing}")
    return {field: manifest[field] for field in fields}


def _validate_annual_result(result: AnnualMicroSensorReadinessResult) -> None:
    expected_schemas = {
        "calendar_coverage": ANNUAL_CALENDAR_SCHEMA,
        "device_days": ANNUAL_DEVICE_DAY_SCHEMA,
        "device_cohorts": ANNUAL_DEVICE_COHORT_SCHEMA,
        "cohort_thresholds": ANNUAL_COHORT_THRESHOLD_SCHEMA,
        "exclusions": ANNUAL_EXCLUSION_SCHEMA,
    }
    for name, schema in expected_schemas.items():
        value = getattr(result, name)
        if name == "device_days" and isinstance(value, tuple):
            if not value or any(pl.read_parquet_schema(path) != dict(schema) for path in value):
                raise RuntimeError(f"annual micro-sensor {name} schema changed")
        elif not isinstance(value, pl.DataFrame) or value.schema != dict(schema):
            raise RuntimeError(f"annual micro-sensor {name} schema changed")
    declared_rows = result.summary.get("output_rows")
    if not isinstance(declared_rows, dict):
        raise RuntimeError("annual micro-sensor output row counts changed")
    rows = {
        name: (
            declared_rows.get(name)
            if name == "device_days" and isinstance(result.device_days, tuple)
            else getattr(result, name).height
        )
        for name in _RESULT_MEMBERS
    }
    if not isinstance(rows["device_days"], int) or rows["device_days"] < 0:
        raise RuntimeError("annual micro-sensor output row counts changed")
    if result.manifest.get("output_rows") != rows or result.summary.get("output_rows") != rows:
        raise RuntimeError("annual micro-sensor output row counts changed")
    if result.manifest.get("claim_boundary") != _CLAIM_BOUNDARY:
        raise RuntimeError("annual micro-sensor claim boundary changed")
    try:
        json.dumps(result.summary, allow_nan=False)
        json.dumps(result.manifest, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("annual micro-sensor metadata is not finite JSON") from exc


def write_annual_micro_sensor_readiness_result(
    result: AnnualMicroSensorReadinessResult,
    *,
    destination: Path | None = None,
    config: AnnualMicroSensorAuditConfig | None = None,
) -> dict[str, Path]:
    _validate_annual_result(result)
    if destination is not None and destination.exists():
        raise RuntimeError(f"annual micro-sensor output already exists: {destination}")
    parent = (
        destination.parent
        if destination is not None
        else outputs_dir("micro_sensor_annual_readiness") / "generations"
    )
    parent.mkdir(parents=True, exist_ok=True)
    staged = parent / f".annual-readiness.staging-{uuid4().hex}"
    staged.mkdir()
    try:
        members: dict[str, dict[str, object]] = {}
        for name in _RESULT_MEMBERS:
            path = staged / f"{name}.parquet"
            value = getattr(result, name)
            if name == "device_days" and isinstance(value, tuple):
                if config is None:
                    raise RuntimeError("annual micro-sensor path-backed output requires config")
                rows = _write_annual_device_days_from_checkpoints(
                    value,
                    destination=path,
                    config=config,
                    temp_dir=staged / ".device-days-duckdb",
                )
                if rows != result.summary["output_rows"]["device_days"]:
                    raise RuntimeError("annual micro-sensor device-day row count changed")
            else:
                value.write_parquet(path)
            identity = _file_identity(path)
            members[name] = {
                "path": path.name,
                "bytes": identity["bytes"],
                "sha256": identity["sha256"],
            }
        _write_json(staged / "summary.json", result.summary)
        summary_identity = _file_identity(staged / "summary.json")
        manifest = {
            **result.manifest,
            "members": members,
            "summary_file": {
                "path": "summary.json",
                "bytes": summary_identity["bytes"],
                "sha256": summary_identity["sha256"],
            },
        }
        manifest["generation_sha256"] = _hash_value(_final_generation_identity(manifest))
        out = destination or annual_micro_sensor_readiness_dir(
            generation_sha256=str(manifest["generation_sha256"])
        )
        if out.exists():
            raise RuntimeError(f"annual micro-sensor output already exists: {out}")
        _write_json(staged / "manifest.json", manifest)
        staged.replace(out)
    finally:
        if staged.exists():
            shutil.rmtree(staged)
    return {
        **{name: out / f"{name}.parquet" for name in _RESULT_MEMBERS},
        "summary": out / "summary.json",
        "manifest": out / "manifest.json",
    }


def run_and_write_annual_micro_sensor_readiness(
    *,
    data_root: Path | None = None,
    panel: AnnualMicroSensorPanelConfig | None = None,
    generated_at: str | None = None,
    destination: Path | None = None,
) -> tuple[AnnualMicroSensorReadinessResult, dict[str, Path]]:
    root = data_root or configured_data_root()
    if root.resolve() != configured_data_root().resolve():
        raise RuntimeError("annual micro-sensor data_root must match the configured data root")
    selected = panel or load_annual_micro_sensor_panel_config()
    panel_sha256 = _hash_value(asdict(selected))
    lock_path = root / "interim" / "micro_sensor_annual_readiness" / panel_sha256 / ".run.lock"
    with _exclusive_run_lock(lock_path):
        result = _prepare_annual_micro_sensor_readiness(
            data_root=root,
            panel=selected,
            generated_at=generated_at,
            _lock_held=True,
        )
        written = write_annual_micro_sensor_readiness_result(
            result,
            destination=destination,
            config=selected.analysis,
        )
        finalized_manifest = _read_json(
            written["manifest"],
            label="published annual micro-sensor manifest",
        )
        finalized = AnnualMicroSensorReadinessResult(
            calendar_coverage=result.calendar_coverage,
            device_days=result.device_days,
            device_cohorts=result.device_cohorts,
            cohort_thresholds=result.cohort_thresholds,
            exclusions=result.exclusions,
            summary=result.summary,
            manifest=finalized_manifest,
        )
    return finalized, written
