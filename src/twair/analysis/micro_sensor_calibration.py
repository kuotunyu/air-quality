"""Measure whether the reviewed micro-sensor panel is ready for calibration tests."""

from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import duckdb
import polars as pl

from twair.config import ConfigError, load_conf
from twair.ingest.micro_sensor_observations import load_micro_sensor_observation_generation
from twair.ingest.micro_sensors import load_observation_generation
from twair.ingest.station_meta import TAIWAN_BOUNDS, resolve_station_geo
from twair.net import sha256_file
from twair.paths import data_root as configured_data_root
from twair.paths import outputs_dir
from twair.provenance import git_state

_SHA256 = re.compile(r"[0-9a-f]{64}")
_VARIABLES = ("pm25", "humidity", "temperature")
_RESULT_MEMBERS = (
    "device_links",
    "hourly_pairs",
    "coverage",
    "exclusions",
    "fold_coverage",
    "satellite_context",
)
_CLAIM_BOUNDARY = {
    "calibration_fitted": False,
    "fusion_performed": False,
    "values_imputed": False,
    "duplicate_timestamps_repaired": False,
    "satellite_is_micro_location": False,
    "satellite_context": "nearest reviewed reference-station month only",
}
_RESULT_SCHEMAS: dict[str, dict[str, Any]] = {
    "device_links": {
        "device_id": pl.String,
        "source_rows": pl.Int64,
        "invalid_or_null_coordinate_rows": pl.Int64,
        "positions": pl.Int64,
        "lon": pl.Float64,
        "lat": pl.Float64,
        "spatial_state": pl.String,
        "station_name": pl.String,
        "distance_km": pl.Float64,
        "geo_source": pl.String,
        "geo_source_record_namespace": pl.String,
        "geo_source_record_id": pl.String,
    },
    "hourly_pairs": {
        "device_id": pl.String,
        "hour": pl.Datetime("us"),
        "pm25_source_rows": pl.Int64,
        "pm25_distinct_timestamps": pl.Int64,
        "pm25_mean": pl.Float64,
        "pm25_extreme_source_rows": pl.Int64,
        "humidity_source_rows": pl.Int64,
        "humidity_distinct_timestamps": pl.Int64,
        "humidity_mean": pl.Float64,
        "humidity_extreme_source_rows": pl.Int64,
        "temperature_source_rows": pl.Int64,
        "temperature_distinct_timestamps": pl.Int64,
        "temperature_mean": pl.Float64,
        "temperature_extreme_source_rows": pl.Int64,
        "station_name": pl.String,
        "distance_km": pl.Float64,
        "ground_row_present": pl.Boolean,
        "ground_flag": pl.String,
        "ground_pm25": pl.Float64,
        "ground_eligible": pl.Boolean,
        "eligibility_reason": pl.String,
    },
    "coverage": {
        "radius_km": pl.Float64,
        "minimum_rows": pl.Int64,
        "pm25_device_hours": pl.Int64,
        "devices": pl.Int64,
        "eligible_pairs": pl.Int64,
        "eligible_reference_stations": pl.Int64,
        "eligible_dates": pl.Int64,
    },
    "exclusions": {
        "eligibility_reason": pl.String,
        "rows": pl.Int64,
        "devices": pl.Int64,
    },
    "fold_coverage": {
        "fold_kind": pl.String,
        "fold": pl.String,
        "rows": pl.Int64,
        "devices": pl.Int64,
        "reference_stations": pl.Int64,
    },
    "satellite_context": {
        "station_name": pl.String,
        "source": pl.String,
        "satellite_value": pl.Float64,
        "satellite_observed": pl.Boolean,
        "pair_observed": pl.Boolean,
        "satellite_unit": pl.String,
        "collection_id": pl.String,
        "band": pl.String,
        "sample_scale_m": pl.Int32,
        "linked_devices": pl.Int64,
    },
}


@dataclass(frozen=True, slots=True)
class MicroSensorPanelConfig:
    catalog_generation_sha256: str
    parsed_generations: tuple[str, ...]
    satellite_generation_sha256: str
    satellite_year: int


@dataclass(frozen=True, slots=True)
class MicroSensorReadinessConfig:
    coverage_thresholds: tuple[int, ...]
    primary_minimum_rows: int
    distance_bands_km: tuple[float, ...]
    primary_distance_km: float
    threads: int
    extreme_ranges: dict[str, tuple[float, float]]


@dataclass(frozen=True, slots=True)
class MicroSensorReadinessResult:
    device_links: pl.DataFrame
    hourly_pairs: pl.DataFrame
    coverage: pl.DataFrame
    exclusions: pl.DataFrame
    fold_coverage: pl.DataFrame
    satellite_context: pl.DataFrame
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


def load_micro_sensor_panel_config(
    config: dict[str, Any] | None = None,
) -> MicroSensorPanelConfig:
    raw = (
        config
        if config is not None
        else _mapping(load_conf("micro_sensor_january_panel"), label="panel")
    )
    _exact_keys(
        raw,
        {
            "schema_version",
            "catalog_generation_sha256",
            "parsed_generations",
            "satellite_generation_sha256",
            "satellite_year",
        },
        label="micro_sensor_january_panel",
    )
    if isinstance(raw["schema_version"], bool) or raw["schema_version"] != 1:
        raise ConfigError("micro_sensor_january_panel.schema_version must be one")
    generations = raw["parsed_generations"]
    if not isinstance(generations, list):
        raise ConfigError("micro_sensor_january_panel.parsed_generations must be a list")
    parsed = tuple(
        _identity(item, label="micro_sensor_january_panel.parsed_generations")
        for item in generations
    )
    if len(parsed) != 25 or len(set(parsed)) != 25:
        raise ConfigError("micro_sensor_january_panel must contain 25 unique parsed generations")
    if (
        isinstance(raw["satellite_year"], bool)
        or not isinstance(raw["satellite_year"], int)
        or raw["satellite_year"] != 2025
    ):
        raise ConfigError("micro_sensor_january_panel.satellite_year must be 2025")
    return MicroSensorPanelConfig(
        catalog_generation_sha256=_identity(
            raw["catalog_generation_sha256"],
            label="micro_sensor_january_panel.catalog_generation_sha256",
        ),
        parsed_generations=parsed,
        satellite_generation_sha256=_identity(
            raw["satellite_generation_sha256"],
            label="micro_sensor_january_panel.satellite_generation_sha256",
        ),
        satellite_year=2025,
    )


def _number(value: object, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{label} must be a finite number")
    converted = float(value)
    if not math.isfinite(converted):
        raise ConfigError(f"{label} must be a finite number")
    return converted


def load_micro_sensor_readiness_config(
    config: dict[str, Any] | None = None,
) -> MicroSensorReadinessConfig:
    root = (
        config
        if config is not None
        else _mapping(load_conf("micro_sensors"), label="micro_sensors")
    )
    group = _mapping(root.get("analysis"), label="micro_sensors.analysis")
    expected = {
        "coverage_thresholds",
        "primary_minimum_rows",
        "distance_bands_km",
        "primary_distance_km",
        "threads",
        "extreme_ranges",
    }
    _exact_keys(group, expected, label="micro_sensors.analysis")
    thresholds_raw = group["coverage_thresholds"]
    radii_raw = group["distance_bands_km"]
    if not isinstance(thresholds_raw, list) or any(
        isinstance(value, bool) or not isinstance(value, int) for value in thresholds_raw
    ):
        raise ConfigError("micro_sensors.analysis.coverage_thresholds must contain integers")
    thresholds = tuple(thresholds_raw)
    if thresholds != (15, 30, 45, 60):
        raise ConfigError("micro_sensors.analysis.coverage_thresholds must be [15, 30, 45, 60]")
    if not isinstance(radii_raw, list):
        raise ConfigError("micro_sensors.analysis.distance_bands_km must be a list")
    radii = tuple(_number(value, label="distance_bands_km") for value in radii_raw)
    if radii != (0.5, 1.0, 2.0, 3.0, 5.0, 10.0):
        raise ConfigError("micro_sensors.analysis.distance_bands_km changed")
    if (
        isinstance(group["primary_minimum_rows"], bool)
        or not isinstance(group["primary_minimum_rows"], int)
        or group["primary_minimum_rows"] != 45
    ):
        raise ConfigError("micro_sensors.analysis.primary_minimum_rows must be 45")
    if _number(group["primary_distance_km"], label="primary_distance_km") != 1.0:
        raise ConfigError("micro_sensors.analysis.primary_distance_km must be 1")
    if (
        isinstance(group["threads"], bool)
        or not isinstance(group["threads"], int)
        or group["threads"] != 1
    ):
        raise ConfigError("micro_sensors.analysis.threads must be one")
    extreme_raw = _mapping(group["extreme_ranges"], label="extreme_ranges")
    if set(extreme_raw) != set(_VARIABLES):
        raise ConfigError("micro_sensors.analysis.extreme_ranges must name three variables")
    extreme: dict[str, tuple[float, float]] = {}
    for variable in _VARIABLES:
        limits = _mapping(extreme_raw[variable], label=f"extreme_ranges.{variable}")
        _exact_keys(limits, {"minimum", "maximum"}, label=f"extreme_ranges.{variable}")
        pair = (
            _number(limits["minimum"], label=f"extreme_ranges.{variable}.minimum"),
            _number(limits["maximum"], label=f"extreme_ranges.{variable}.maximum"),
        )
        if pair[0] >= pair[1]:
            raise ConfigError(f"extreme_ranges.{variable} minimum must be below maximum")
        extreme[variable] = pair
    expected_extreme = {
        "pm25": (0.0, 1000.0),
        "humidity": (0.0, 100.0),
        "temperature": (-100.0, 100.0),
    }
    if extreme != expected_extreme:
        raise ConfigError("micro_sensors.analysis.extreme_ranges changed")
    return MicroSensorReadinessConfig(
        coverage_thresholds=thresholds,
        primary_minimum_rows=45,
        distance_bands_km=radii,
        primary_distance_km=1.0,
        threads=1,
        extreme_ranges=extreme,
    )


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


def _file_identity(path: Path, *, relative_to: Path | None = None) -> dict[str, object]:
    if not path.is_file():
        raise FileNotFoundError(f"micro-sensor readiness input not found: {path}")
    identity_path = (
        path.relative_to(relative_to).as_posix()
        if relative_to is not None and path.is_relative_to(relative_to)
        else path.as_posix()
    )
    return {"path": identity_path, "bytes": path.stat().st_size, "sha256": sha256_file(path)}


def _sql_path(path: Path) -> str:
    return path.as_posix().replace("'", "''")


def _file_list(paths: tuple[Path, ...]) -> str:
    return "[" + ",".join(f"'{_sql_path(path)}'" for path in paths) + "]"


def _haversine(lon_1: str, lat_1: str, lon_2: str, lat_2: str) -> str:
    component = (
        f"pow(sin(radians(({lat_2}) - ({lat_1})) / 2), 2)"
        f" + cos(radians({lat_1})) * cos(radians({lat_2}))"
        f" * pow(sin(radians(({lon_2}) - ({lon_1})) / 2), 2)"
    )
    return f"2 * 6371.0088 * asin(sqrt(least(1.0, greatest(0.0, {component}))))"


def _hourly_sql(paths: tuple[Path, ...], *, variable: str, limits: tuple[float, float]) -> str:
    return f"""
        SELECT device_id, date_trunc('hour', ts_local) AS hour,
               count(*)::BIGINT AS source_rows,
               count(DISTINCT ts_local)::BIGINT AS distinct_timestamps,
               avg(value)::DOUBLE AS mean_value,
               count(*) FILTER (WHERE value < {limits[0]} OR value > {limits[1]})::BIGINT
                   AS extreme_source_rows
        FROM read_parquet({_file_list(paths)})
        GROUP BY device_id, hour
    """


def _frame(connection: duckdb.DuckDBPyConnection, query: str) -> pl.DataFrame:
    return pl.DataFrame(connection.execute(query).to_arrow_table())


def _validate_prepare_inputs(
    micro_paths: dict[str, tuple[Path, ...]],
    geography: pl.DataFrame,
    ground_path: Path,
    satellite_path: Path,
    identity_root: Path | None,
) -> list[dict[str, object]]:
    if set(micro_paths) != set(_VARIABLES) or any(not paths for paths in micro_paths.values()):
        raise RuntimeError("micro-sensor readiness requires all three parsed variables")
    required_geo = {
        "station_name",
        "lon",
        "lat",
        "geo_source",
        "geo_source_record_namespace",
        "geo_source_record_id",
    }
    if not required_geo.issubset(geography.columns):
        raise RuntimeError("reviewed station geography is missing provenance columns")
    identities = [
        _file_identity(path, relative_to=identity_root)
        for variable in _VARIABLES
        for path in micro_paths[variable]
    ]
    identities.extend(
        (
            _file_identity(ground_path, relative_to=identity_root),
            _file_identity(satellite_path, relative_to=identity_root),
        )
    )
    _validate_satellite_context(satellite_path)
    return identities


def _validate_satellite_context(path: Path) -> None:
    try:
        frame = pl.read_parquet(path)
    except (OSError, pl.exceptions.PolarsError) as exc:
        raise RuntimeError(
            f"micro-sensor readiness satellite context is unreadable: {path}"
        ) from exc
    required = {
        "source",
        "station_name",
        "month",
        "satellite_value",
        "satellite_observed",
        "pair_observed",
        "satellite_unit",
        "collection_id",
        "band",
        "sample_scale_m",
    }
    missing = sorted(required - set(frame.columns))
    if missing:
        raise RuntimeError(f"satellite context is missing required column(s): {missing}")
    if frame.schema["month"] != pl.Date:
        raise RuntimeError("satellite context month must use Date values")
    if (
        frame.schema["satellite_observed"] != pl.Boolean
        or frame.schema["pair_observed"] != pl.Boolean
    ):
        raise RuntimeError("satellite context observation states must be Boolean")
    if not frame.schema["satellite_value"].is_float():
        raise RuntimeError("satellite context values must be floating point")
    if not frame.schema["sample_scale_m"].is_integer():
        raise RuntimeError("satellite context sample scales must be integers")
    text_columns = ("source", "station_name", "satellite_unit", "collection_id", "band")
    if any(frame.schema[column] != pl.String for column in text_columns):
        raise RuntimeError("satellite context labels must be strings")
    selected = frame.filter(pl.col("month") == date(2025, 1, 1))
    if selected.is_empty():
        raise RuntimeError("satellite context has no January 2025 station-month rows")
    for column in text_columns:
        if selected.filter(
            pl.col(column).is_null() | (pl.col(column).str.strip_chars() == "")
        ).height:
            raise RuntimeError(f"satellite context {column} must be non-empty")
    if selected.filter(
        pl.col("satellite_observed").is_null() | pl.col("pair_observed").is_null()
    ).height:
        raise RuntimeError("satellite context observation states must not be null")
    if selected.filter(
        pl.col("satellite_value").is_not_null() & ~pl.col("satellite_value").is_finite()
    ).height:
        raise RuntimeError("satellite context values must be finite or null")
    if selected.filter(
        pl.col("satellite_observed") != pl.col("satellite_value").is_not_null()
    ).height:
        raise RuntimeError("satellite context observed state disagrees with its value")
    if selected.filter(pl.col("pair_observed") & ~pl.col("satellite_observed")).height:
        raise RuntimeError("satellite context cannot pair an absent satellite value")
    if selected.filter(pl.col("sample_scale_m").is_null() | (pl.col("sample_scale_m") <= 0)).height:
        raise RuntimeError("satellite context sample scales must be positive")
    duplicates = (
        selected.group_by("source", "station_name", "month").len().filter(pl.col("len") > 1)
    )
    if not duplicates.is_empty():
        raise RuntimeError("satellite context has duplicate source-station-month rows")


def prepare_micro_sensor_calibration_readiness(
    *,
    micro_paths: dict[str, tuple[Path, ...]],
    geography: pl.DataFrame,
    ground_path: Path,
    satellite_path: Path,
    config: MicroSensorReadinessConfig,
    temp_dir: Path,
    input_identity: dict[str, Any],
    identity_root: Path | None = None,
    generated_at: str | None = None,
    git_sha: str | None = None,
    git_dirty: bool | None = None,
) -> MicroSensorReadinessResult:
    before = _validate_prepare_inputs(
        micro_paths,
        geography,
        ground_path,
        satellite_path,
        identity_root,
    )
    temp_dir.mkdir(parents=True, exist_ok=False)
    connection = duckdb.connect()
    connection.execute(f"SET threads={config.threads}")
    connection.execute("SET memory_limit='6GB'")
    connection.execute(f"SET temp_directory='{_sql_path(temp_dir)}'")
    connection.execute("SET preserve_insertion_order=false")
    try:
        connection.register("reference_stations", geography.to_arrow())
        connection.execute(
            f"""
            CREATE TEMP TABLE device_locations AS
            SELECT device_id, count(*)::BIGINT AS source_rows,
                   count(*) FILTER (WHERE lon IS NULL OR lat IS NULL
                       OR coordinate_wgs84_valid IS DISTINCT FROM TRUE)::BIGINT
                       AS invalid_or_null_coordinate_rows,
                   count(DISTINCT struct_pack(lon := lon, lat := lat))::BIGINT AS positions,
                   min(lon)::DOUBLE AS lon, min(lat)::DOUBLE AS lat
            FROM read_parquet({_file_list(micro_paths["pm25"])})
            GROUP BY device_id
            """
        )
        connection.execute(
            """
            ALTER TABLE device_locations ADD COLUMN spatial_state VARCHAR;
            UPDATE device_locations SET spatial_state = CASE
                WHEN invalid_or_null_coordinate_rows > 0 THEN 'invalid_or_null_coordinate'
                WHEN positions > 1 THEN 'moving_coordinate'
                WHEN NOT (lon BETWEEN {lon_min} AND {lon_max}
                          AND lat BETWEEN {lat_min} AND {lat_max}) THEN 'outside_taiwan'
                ELSE 'eligible' END;
            """.format(**TAIWAN_BOUNDS)
        )
        distance = _haversine("d.lon", "d.lat", "r.lon", "r.lat")
        connection.execute(
            f"""
            CREATE TEMP TABLE nearest_reference AS
            SELECT * EXCLUDE (rank) FROM (
                SELECT d.device_id, r.station_name,
                       r.geo_source, r.geo_source_record_namespace,
                       r.geo_source_record_id, {distance}::DOUBLE AS distance_km,
                       row_number() OVER (PARTITION BY d.device_id
                           ORDER BY {distance}, r.station_name) AS rank
                FROM device_locations d CROSS JOIN reference_stations r
                WHERE d.spatial_state = 'eligible'
            ) WHERE rank = 1
            """
        )
        device_links = _frame(
            connection,
            """
            SELECT d.device_id, d.source_rows, d.invalid_or_null_coordinate_rows,
                   d.positions, d.lon, d.lat, d.spatial_state,
                   n.station_name, n.distance_km, n.geo_source,
                   n.geo_source_record_namespace, n.geo_source_record_id
            FROM device_locations d LEFT JOIN nearest_reference n USING (device_id)
            ORDER BY d.device_id
            """,
        )
        for variable in _VARIABLES:
            connection.execute(
                f"CREATE TEMP TABLE {variable}_hourly AS "
                + _hourly_sql(
                    micro_paths[variable],
                    variable=variable,
                    limits=config.extreme_ranges[variable],
                )
            )
        connection.execute(
            """
            CREATE TEMP TABLE micro_hourly AS
            SELECT p.device_id, p.hour,
                   p.source_rows AS pm25_source_rows,
                   p.distinct_timestamps AS pm25_distinct_timestamps,
                   p.mean_value AS pm25_mean,
                   p.extreme_source_rows AS pm25_extreme_source_rows,
                   h.source_rows AS humidity_source_rows,
                   h.distinct_timestamps AS humidity_distinct_timestamps,
                   h.mean_value AS humidity_mean,
                   h.extreme_source_rows AS humidity_extreme_source_rows,
                   t.source_rows AS temperature_source_rows,
                   t.distinct_timestamps AS temperature_distinct_timestamps,
                   t.mean_value AS temperature_mean,
                   t.extreme_source_rows AS temperature_extreme_source_rows
            FROM pm25_hourly p
            LEFT JOIN humidity_hourly h USING (device_id, hour)
            LEFT JOIN temperature_hourly t USING (device_id, hour)
            """
        )
        connection.execute(
            f"""
            CREATE TEMP TABLE ground_pm25 AS
            SELECT CAST(station_name AS VARCHAR) AS station_name, ts_local AS hour,
                   value::DOUBLE AS ground_pm25, CAST(flag AS VARCHAR) AS ground_flag
            FROM read_parquet('{_sql_path(ground_path)}')
            WHERE CAST(pollutant AS VARCHAR) = 'PM2.5'
              AND ts_local >= TIMESTAMP '2025-01-01 00:00:00'
              AND ts_local < TIMESTAMP '2025-02-01 00:00:00'
            """
        )
        nonfinite_ground = connection.execute(
            """SELECT count(*) FROM ground_pm25
               WHERE ground_pm25 IS NOT NULL AND NOT isfinite(ground_pm25)"""
        ).fetchone()
        if nonfinite_ground is None or int(nonfinite_ground[0]) > 0:
            raise RuntimeError("ground PM2.5 values must be finite or null")
        duplicate_ground = connection.execute(
            """SELECT count(*) FROM (
                SELECT station_name, hour FROM ground_pm25 GROUP BY ALL HAVING count(*) > 1
            )"""
        ).fetchone()
        if duplicate_ground is None or int(duplicate_ground[0]) > 0:
            raise RuntimeError("ground PM2.5 has duplicate station-hours")
        connection.execute(
            """
            CREATE TEMP TABLE all_pairs AS
            SELECT m.*, n.station_name, n.distance_km,
                   g.hour IS NOT NULL AS ground_row_present,
                   g.ground_flag, g.ground_pm25,
                   CASE WHEN g.ground_flag = 'valid' AND g.ground_pm25 IS NOT NULL
                        THEN TRUE ELSE FALSE END AS ground_eligible
            FROM micro_hourly m INNER JOIN nearest_reference n USING (device_id)
            LEFT JOIN ground_pm25 g ON g.station_name = n.station_name AND g.hour = m.hour
            """
        )
        reason = f"""CASE
            WHEN humidity_source_rows IS NULL THEN 'missing_humidity'
            WHEN temperature_source_rows IS NULL THEN 'missing_temperature'
            WHEN pm25_mean IS NULL THEN 'missing_pm25_value'
            WHEN humidity_mean IS NULL THEN 'missing_humidity_value'
            WHEN temperature_mean IS NULL THEN 'missing_temperature_value'
            WHEN pm25_source_rows < {config.primary_minimum_rows} THEN 'insufficient_pm25_rows'
            WHEN humidity_source_rows < {config.primary_minimum_rows} THEN 'insufficient_humidity_rows'
            WHEN temperature_source_rows < {config.primary_minimum_rows} THEN 'insufficient_temperature_rows'
            WHEN pm25_source_rows != pm25_distinct_timestamps THEN 'duplicate_pm25_timestamp'
            WHEN humidity_source_rows != humidity_distinct_timestamps THEN 'duplicate_humidity_timestamp'
            WHEN temperature_source_rows != temperature_distinct_timestamps THEN 'duplicate_temperature_timestamp'
            WHEN pm25_extreme_source_rows > 0 THEN 'extreme_pm25'
            WHEN humidity_extreme_source_rows > 0 THEN 'extreme_humidity'
            WHEN temperature_extreme_source_rows > 0 THEN 'extreme_temperature'
            WHEN NOT ground_row_present THEN 'ground_absent'
            WHEN NOT ground_eligible THEN 'ground_present_but_ineligible'
            ELSE 'eligible' END"""
        connection.execute(
            f"""
            CREATE TEMP TABLE primary_pairs AS
            SELECT *, {reason} AS eligibility_reason
            FROM all_pairs WHERE distance_km <= {config.primary_distance_km}
            """
        )
        hourly_pairs = _frame(connection, "SELECT * FROM primary_pairs ORDER BY device_id, hour")
        values = ",".join(f"({radius})" for radius in config.distance_bands_km)
        thresholds = ",".join(f"({threshold})" for threshold in config.coverage_thresholds)
        coverage = _frame(
            connection,
            f"""
            WITH radii(radius_km) AS (VALUES {values}),
                 thresholds(minimum_rows) AS (VALUES {thresholds}),
                 classified AS (
                    SELECT r.radius_km, t.minimum_rows, p.*,
                    CASE
                        WHEN humidity_source_rows IS NULL OR temperature_source_rows IS NULL THEN FALSE
                        WHEN pm25_mean IS NULL OR humidity_mean IS NULL
                          OR temperature_mean IS NULL THEN FALSE
                        WHEN pm25_source_rows < t.minimum_rows
                          OR humidity_source_rows < t.minimum_rows
                          OR temperature_source_rows < t.minimum_rows THEN FALSE
                        WHEN pm25_source_rows != pm25_distinct_timestamps
                          OR humidity_source_rows != humidity_distinct_timestamps
                          OR temperature_source_rows != temperature_distinct_timestamps THEN FALSE
                        WHEN pm25_extreme_source_rows > 0 OR humidity_extreme_source_rows > 0
                          OR temperature_extreme_source_rows > 0 THEN FALSE
                        WHEN NOT ground_eligible THEN FALSE ELSE TRUE END AS eligible_pair
                    FROM all_pairs p CROSS JOIN radii r CROSS JOIN thresholds t
                    WHERE p.distance_km <= r.radius_km
                 )
            SELECT radius_km::DOUBLE AS radius_km, minimum_rows::BIGINT AS minimum_rows,
                   count(*)::BIGINT AS pm25_device_hours,
                   count(DISTINCT device_id)::BIGINT AS devices,
                   count(*) FILTER (WHERE eligible_pair)::BIGINT AS eligible_pairs,
                   count(DISTINCT station_name) FILTER (WHERE eligible_pair)::BIGINT
                       AS eligible_reference_stations,
                   count(DISTINCT CAST(hour AS DATE)) FILTER (WHERE eligible_pair)::BIGINT
                       AS eligible_dates
            FROM classified GROUP BY radius_km, minimum_rows ORDER BY radius_km, minimum_rows
            """,
        )
        exclusions = _frame(
            connection,
            """
            SELECT eligibility_reason, count(*)::BIGINT AS rows,
                   count(DISTINCT device_id)::BIGINT AS devices
            FROM primary_pairs GROUP BY eligibility_reason ORDER BY eligibility_reason
            """,
        )
        fold_coverage = _frame(
            connection,
            """
            WITH eligible AS (SELECT * FROM primary_pairs WHERE eligibility_reason = 'eligible'),
            dates AS (
                SELECT 'date' AS fold_kind, CAST(hour AS DATE)::VARCHAR AS fold,
                       count(*)::BIGINT AS rows, count(DISTINCT device_id)::BIGINT AS devices,
                       count(DISTINCT station_name)::BIGINT AS reference_stations
                FROM eligible GROUP BY CAST(hour AS DATE)
            ), stations AS (
                SELECT 'station' AS fold_kind, station_name AS fold,
                       count(*)::BIGINT AS rows, count(DISTINCT device_id)::BIGINT AS devices,
                       1::BIGINT AS reference_stations
                FROM eligible GROUP BY station_name
            )
            SELECT * FROM dates UNION ALL SELECT * FROM stations ORDER BY fold_kind, fold
            """,
        )
        connection.execute(
            f"""
            CREATE TEMP VIEW satellite AS
            SELECT * FROM read_parquet('{_sql_path(satellite_path)}')
            WHERE month = DATE '2025-01-01'
            """
        )
        satellite_context = _frame(
            connection,
            """
            WITH links AS (
                SELECT station_name, count(DISTINCT device_id)::BIGINT AS linked_devices
                FROM primary_pairs WHERE eligibility_reason = 'eligible' GROUP BY station_name
            )
            SELECT s.station_name, s.source, s.satellite_value, s.satellite_observed,
                   s.pair_observed, s.satellite_unit, s.collection_id, s.band,
                   s.sample_scale_m::INTEGER AS sample_scale_m, l.linked_devices
            FROM satellite s INNER JOIN links l USING (station_name)
            ORDER BY s.source, s.station_name
            """,
        )
    finally:
        connection.close()
        shutil.rmtree(temp_dir, ignore_errors=True)
    after = _validate_prepare_inputs(
        micro_paths,
        geography,
        ground_path,
        satellite_path,
        identity_root,
    )
    if before != after:
        raise RuntimeError("a micro-sensor readiness input changed while it was read")
    output_rows = {
        "device_links": device_links.height,
        "hourly_pairs": hourly_pairs.height,
        "coverage": coverage.height,
        "exclusions": exclusions.height,
        "fold_coverage": fold_coverage.height,
        "satellite_context": satellite_context.height,
    }
    primary_rows = hourly_pairs.filter(pl.col("eligibility_reason") == "eligible").height
    summary = {
        "primary": {
            "distance_km": config.primary_distance_km,
            "minimum_rows": config.primary_minimum_rows,
            "eligible_pairs": primary_rows,
        },
        "output_rows": output_rows,
    }
    measured_sha, measured_dirty = git_state()
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "analysis": "micro_sensor_calibration_readiness",
        "complete": True,
        "generated_at": generated_at or datetime.now(UTC).isoformat(timespec="seconds"),
        "panel_dates": len(input_identity.get("parsed_generations", [])),
        "config": asdict(config),
        "config_sha256": _hash_value(asdict(config)),
        "inputs": input_identity,
        "input_files": before,
        "output_rows": output_rows,
        "claim_boundary": dict(_CLAIM_BOUNDARY),
        "git_sha": measured_sha if git_sha is None else git_sha,
        "git_dirty": measured_dirty if git_dirty is None else git_dirty,
    }
    identity_payload = {
        key: manifest[key]
        for key in (
            "schema_version",
            "analysis",
            "config_sha256",
            "inputs",
            "input_files",
            "output_rows",
            "claim_boundary",
        )
    }
    manifest["output_identity_sha256"] = _hash_value(identity_payload)
    return MicroSensorReadinessResult(
        device_links=device_links,
        hourly_pairs=hourly_pairs,
        coverage=coverage,
        exclusions=exclusions,
        fold_coverage=fold_coverage,
        satellite_context=satellite_context,
        summary=summary,
        manifest=manifest,
    )


def _validated_panel_days(
    panel: MicroSensorPanelConfig,
    *,
    parsed_root: Path,
) -> tuple[list[date], dict[str, tuple[Path, ...]], list[dict[str, Any]]]:
    dates: list[date] = []
    paths: dict[str, list[Path]] = {variable: [] for variable in _VARIABLES}
    manifests: list[dict[str, Any]] = []
    for generation in panel.parsed_generations:
        written = load_micro_sensor_observation_generation(
            generation,
            interim_observation_root=parsed_root,
        )
        raw_date = written.manifest.get("date")
        if not isinstance(raw_date, str):
            raise RuntimeError("micro-sensor panel generation date is invalid")
        try:
            parsed_date = date.fromisoformat(raw_date)
        except ValueError as exc:
            raise RuntimeError("micro-sensor panel generation date is invalid") from exc
        dates.append(parsed_date)
        manifests.append(written.manifest)
        for variable in _VARIABLES:
            paths[variable].append(written.directory / f"{variable}.parquet")
    expected_dates = [date(2025, 1, 1) + timedelta(days=index) for index in range(25)]
    if dates != expected_dates or len(set(dates)) != 25:
        raise RuntimeError(
            "micro-sensor panel dates must be unique, sorted 2025-01-01 through 2025-01-25"
        )
    return dates, {key: tuple(value) for key, value in paths.items()}, manifests


def _stable_json(path: Path) -> tuple[dict[str, Any], dict[str, object]]:
    before = _file_identity(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"micro-sensor readiness manifest is unreadable: {path}") from exc
    after = _file_identity(path)
    if before != after:
        raise RuntimeError(f"micro-sensor readiness manifest changed while read: {path}")
    if not isinstance(value, dict):
        raise RuntimeError(f"micro-sensor readiness manifest must be an object: {path}")
    return value, before


def run_micro_sensor_calibration_readiness(
    *,
    data_root: Path | None = None,
    panel: MicroSensorPanelConfig | None = None,
    config: MicroSensorReadinessConfig | None = None,
    generated_at: str | None = None,
) -> MicroSensorReadinessResult:
    root = data_root or configured_data_root()
    if root.resolve() != configured_data_root().resolve():
        raise RuntimeError("micro-sensor readiness data_root must match the configured data root")
    selected_panel = panel or load_micro_sensor_panel_config()
    selected_config = config or load_micro_sensor_readiness_config()
    parsed_root = root / "interim" / "micro_sensors" / "observations" / "generations"
    dates, paths, manifests = _validated_panel_days(selected_panel, parsed_root=parsed_root)
    raw_generations: list[str] = []
    for day, parsed_manifest in zip(dates, manifests, strict=True):
        raw_generation = parsed_manifest.get("raw_observation_generation_sha256")
        if not isinstance(raw_generation, str):
            raise RuntimeError("micro-sensor panel raw generation identity is invalid")
        raw = load_observation_generation(raw_generation)
        if (
            raw.manifest.get("catalog_generation_sha256")
            != selected_panel.catalog_generation_sha256
            or raw.manifest.get("date") != day.isoformat()
            or raw.manifest.get("members") != parsed_manifest.get("raw_members")
        ):
            raise RuntimeError("micro-sensor panel raw request identity changed")
        raw_generations.append(raw_generation)
    ground_path = root / "processed" / "observations" / "year=2025" / "month=01" / "part-0.parquet"
    satellite_dir = (
        root
        / "outputs"
        / "m8_satellite"
        / "generations"
        / selected_panel.satellite_generation_sha256
        / f"year={selected_panel.satellite_year}"
    )
    satellite_path = satellite_dir / "panel.parquet"
    satellite_manifest, satellite_manifest_file = _stable_json(satellite_dir / "manifest.json")
    if (
        satellite_manifest.get("analysis") != "m8_satellite_association"
        or satellite_manifest.get("year") != selected_panel.satellite_year
        or satellite_manifest.get("inventory_generation_sha256")
        != selected_panel.satellite_generation_sha256
    ):
        raise RuntimeError("micro-sensor readiness satellite context identity changed")
    geography = resolve_station_geo()
    geography_fields = [
        "station_name",
        "lon",
        "lat",
        "geo_source",
        "geo_source_record_namespace",
        "geo_source_record_id",
    ]
    geography = geography.select(geography_fields)
    geography_identity = _hash_value(geography.sort("station_name").to_dicts())
    portable_satellite_manifest_file = {
        **satellite_manifest_file,
        "path": (satellite_dir / "manifest.json").relative_to(root).as_posix(),
    }
    input_identity = {
        "catalog_generation_sha256": selected_panel.catalog_generation_sha256,
        "panel_config_sha256": _hash_value(asdict(selected_panel)),
        "parsed_generations": [
            {
                "date": day.isoformat(),
                "generation_sha256": generation,
                "raw_generation_sha256": raw_generation,
            }
            for day, generation, raw_generation in zip(
                dates,
                selected_panel.parsed_generations,
                raw_generations,
                strict=True,
            )
        ],
        "reviewed_geography_sha256": geography_identity,
        "satellite_generation_sha256": selected_panel.satellite_generation_sha256,
        "satellite_manifest": portable_satellite_manifest_file,
    }
    temporary = root / "interim" / f".micro-sensor-readiness-{uuid4().hex}"
    result = prepare_micro_sensor_calibration_readiness(
        micro_paths=paths,
        geography=geography,
        ground_path=ground_path,
        satellite_path=satellite_path,
        config=selected_config,
        temp_dir=temporary,
        input_identity=input_identity,
        identity_root=root,
        generated_at=generated_at,
    )
    if _file_identity(satellite_dir / "manifest.json") != satellite_manifest_file:
        raise RuntimeError("micro-sensor readiness satellite manifest changed during analysis")
    if (
        _hash_value(resolve_station_geo().select(geography_fields).sort("station_name").to_dicts())
        != geography_identity
    ):
        raise RuntimeError("reviewed station geography changed during analysis")
    return result


def micro_sensor_calibration_readiness_dir(*, identity: str) -> Path:
    if _SHA256.fullmatch(identity) is None:
        raise ValueError("micro-sensor readiness output identity must be a SHA-256")
    return outputs_dir("micro_sensor_calibration_readiness") / "generations" / identity


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _validate_result(result: MicroSensorReadinessResult) -> None:
    manifest = result.manifest
    if (
        manifest.get("schema_version") != 1
        or manifest.get("analysis") != "micro_sensor_calibration_readiness"
        or manifest.get("complete") is not True
        or manifest.get("panel_dates") != 25
    ):
        raise RuntimeError("micro-sensor readiness manifest contract is invalid")
    if manifest.get("claim_boundary") != _CLAIM_BOUNDARY:
        raise RuntimeError("micro-sensor readiness claim boundary changed")
    manifest_config = manifest.get("config")
    if not isinstance(manifest_config, dict) or manifest.get("config_sha256") != _hash_value(
        manifest_config
    ):
        raise RuntimeError("micro-sensor readiness config identity changed")
    rows = {name: getattr(result, name).height for name in _RESULT_MEMBERS}
    if manifest.get("output_rows") != rows or result.summary.get("output_rows") != rows:
        raise RuntimeError("micro-sensor readiness result row counts are inconsistent")
    for name, expected in _RESULT_SCHEMAS.items():
        if dict(getattr(result, name).schema) != expected:
            raise RuntimeError(f"micro-sensor readiness {name} schema changed")
    primary = result.summary.get("primary")
    expected_primary = {
        "distance_km": manifest_config.get("primary_distance_km"),
        "minimum_rows": manifest_config.get("primary_minimum_rows"),
        "eligible_pairs": result.hourly_pairs.filter(
            pl.col("eligibility_reason") == "eligible"
        ).height,
    }
    if primary != expected_primary:
        raise RuntimeError("micro-sensor readiness primary summary is inconsistent")
    try:
        identity_payload = {
            key: manifest[key]
            for key in (
                "schema_version",
                "analysis",
                "config_sha256",
                "inputs",
                "input_files",
                "output_rows",
                "claim_boundary",
            )
        }
    except KeyError as exc:
        raise RuntimeError("micro-sensor readiness manifest identity is incomplete") from exc
    if manifest.get("output_identity_sha256") != _hash_value(identity_payload):
        raise RuntimeError("micro-sensor readiness output identity changed")
    try:
        json.dumps(manifest, ensure_ascii=False, allow_nan=False)
        json.dumps(result.summary, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("micro-sensor readiness metadata is not finite JSON") from exc


def _recover_swap(destination: Path) -> None:
    parent = destination.parent
    if not parent.exists():
        return
    backups = sorted(parent.glob(f".{destination.name}.backup-*"))
    stages = sorted(parent.glob(f".{destination.name}.staging-*"))
    if len(backups) > 1 or len(stages) > 1:
        raise RuntimeError(
            f"multiple interrupted micro-sensor readiness swaps beside {destination}"
        )
    if destination.exists() and backups and stages:
        raise RuntimeError(
            f"ambiguous interrupted micro-sensor readiness swap beside {destination}"
        )
    if not destination.exists() and backups:
        backups[0].replace(destination)
        backups = []
    for staged in stages:
        shutil.rmtree(staged)
    if destination.exists() and backups:
        shutil.rmtree(backups[0])


def write_micro_sensor_calibration_readiness_result(
    result: MicroSensorReadinessResult,
    *,
    destination: Path | None = None,
) -> dict[str, Path]:
    _validate_result(result)
    raw_identity = result.manifest.get("output_identity_sha256")
    if not isinstance(raw_identity, str):
        raise RuntimeError("micro-sensor readiness result has no output identity")
    out = destination or micro_sensor_calibration_readiness_dir(identity=raw_identity)
    _recover_swap(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    token = uuid4().hex
    staged = out.with_name(f".{out.name}.staging-{token}")
    backup = out.with_name(f".{out.name}.backup-{token}")
    staged.mkdir()
    had_existing = out.exists()
    try:
        for name in _RESULT_MEMBERS:
            getattr(result, name).write_parquet(staged / f"{name}.parquet")
        _write_json(staged / "summary.json", result.summary)
        _write_json(staged / "manifest.json", result.manifest)
        try:
            if had_existing:
                out.replace(backup)
            staged.replace(out)
        except BaseException:
            if had_existing and backup.exists() and not out.exists():
                backup.replace(out)
            raise
        if backup.exists():
            shutil.rmtree(backup)
    finally:
        if staged.exists():
            shutil.rmtree(staged)
    return {
        **{name: out / f"{name}.parquet" for name in _RESULT_MEMBERS},
        "summary": out / "summary.json",
        "manifest": out / "manifest.json",
    }
