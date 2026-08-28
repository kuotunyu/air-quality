"""Freeze the reviewed inputs for the spatial covariate readiness gate."""

from __future__ import annotations

import math
import re
from calendar import monthrange
from dataclasses import dataclass
from datetime import date
from hashlib import sha256
from pathlib import Path
from typing import Any

import polars as pl

from twair.analysis.era5_value import ModelConfig
from twair.analysis.spatial_surface_baseline import (
    SPATIAL_BASELINE_TABLE_ORDER,
    SPATIAL_BASELINE_TABLE_SCHEMAS,
)
from twair.config import ConfigError, load_conf

__all__ = [
    "COVARIATE_READINESS_EVALUATIONS",
    "COVARIATE_READINESS_METHODS",
    "CovariateReadinessConfig",
    "FrozenInputs",
    "InputFile",
    "aggregate_era5_monthly",
    "assemble_covariates",
    "load_frozen_inputs",
    "load_spatial_covariate_readiness_config",
    "pivot_satellite_monthly",
]


COVARIATE_READINESS_METHODS = ("idw2", "covariate_gbm", "covariate_gbm_idw2")
COVARIATE_READINESS_EVALUATIONS = ("buffer_20km", "buffer_40km", "spatial_cluster")
_BASELINE_MEMBER_NAMES = ("stations.parquet", "panel.parquet", "support.parquet", "folds.parquet")
_REVIEWED_STATION_COUNT = 59
_REVIEWED_PANEL_KEY_COUNT = 1416
_REVIEWED_OBSERVED_COUNT = 1415
_REVIEWED_WITHHELD_KEY = ("新營", date(2025, 5, 1))
_SHA256 = re.compile(r"[0-9a-f]{64}")
_EXPECTED_MODEL = ModelConfig(
    n_estimators=200,
    learning_rate=0.05,
    num_leaves=31,
    min_child_samples=10,
    n_jobs=1,
    seed=20260811,
)
_ERA5_SOURCE_COLUMNS = (
    "blh_m",
    "u10_m_s",
    "v10_m_s",
    "t2m_k",
    "d2m_k",
    "sp_pa",
)
_ERA5_MONTHLY_SCHEMA: dict[str, pl.DataType | type[pl.DataType]] = {
    "station_name": pl.String,
    "month": pl.Date,
    "n_hours": pl.Int64,
    "era5_blh_mean_m": pl.Float64,
    "era5_u10_mean_m_s": pl.Float64,
    "era5_v10_mean_m_s": pl.Float64,
    "era5_wind_speed_mean_m_s": pl.Float64,
    "era5_t2m_mean_k": pl.Float64,
    "era5_dewpoint_depression_mean_k": pl.Float64,
    "era5_sp_mean_pa": pl.Float64,
}
_SATELLITE_SOURCES = ("maiac_aod", "s5p_no2", "s5p_so2")
_SATELLITE_LONG_COLUMNS = (
    "source",
    "station_name",
    "month",
    "satellite_value",
    "ground_value",
    "satellite_observed",
    "ground_row_present",
    "ground_meets_threshold",
    "ground_observed",
    "ground_withheld",
    "pair_observed",
)
_SATELLITE_MONTHLY_SCHEMA: dict[str, pl.DataType | type[pl.DataType]] = {
    "station_name": pl.String,
    "month": pl.Date,
    "ground_value": pl.Float64,
    "ground_row_present": pl.Boolean,
    "ground_meets_threshold": pl.Boolean,
    "ground_observed": pl.Boolean,
    "ground_withheld": pl.Boolean,
    "maiac_aod": pl.Float64,
    "s5p_no2": pl.Float64,
    "s5p_so2": pl.Float64,
}
_COVARIATE_SCHEMA: dict[str, pl.DataType | type[pl.DataType]] = {
    "station_name": pl.String,
    "month": pl.Date,
    "target_state": pl.String,
    "PM2.5": pl.Float64,
    "lon": pl.Float64,
    "lat": pl.Float64,
    "x_m": pl.Float64,
    "y_m": pl.Float64,
    "month_sin": pl.Float64,
    "month_cos": pl.Float64,
    **{
        name: dtype
        for name, dtype in _ERA5_MONTHLY_SCHEMA.items()
        if name not in {"station_name", "month", "n_hours"}
    },
    "maiac_aod": pl.Float64,
    "s5p_no2": pl.Float64,
    "s5p_so2": pl.Float64,
}


@dataclass(frozen=True, slots=True)
class CovariateReadinessConfig:
    years: tuple[int, int]
    baseline_generation_sha256: str
    station_inventory_generation_sha256: str
    minimum_train_stations: int
    methods: tuple[str, str, str]
    comparator: str
    idw_power: float
    minimum_distance_km: float
    model: ModelConfig
    evaluations: tuple[str, str, str]
    bootstrap_draws: int
    bootstrap_seed: int


@dataclass(frozen=True, slots=True)
class InputFile:
    path: Path
    bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class FrozenInputs:
    stations: pl.DataFrame
    panel: pl.DataFrame
    support: pl.DataFrame
    baseline_folds: pl.DataFrame
    input_files: tuple[InputFile, ...]
    baseline_generation_sha256: str
    station_inventory_generation_sha256: str


def aggregate_era5_monthly(frame: pl.DataFrame, *, years: tuple[int, int]) -> pl.DataFrame:
    """Aggregate complete station-hour ERA5 rows by Asia/Taipei calendar month."""
    required = ("station_name", "ts_utc", "grid_lat", "grid_lon", *_ERA5_SOURCE_COLUMNS)
    missing = set(required).difference(frame.columns)
    if missing:
        raise RuntimeError(f"ERA5 station-hour frame is missing columns: {sorted(missing)}")
    if frame.is_empty():
        raise RuntimeError("ERA5 station-hour frame is empty")
    if frame.schema["ts_utc"] != pl.Datetime(time_zone="UTC"):
        raise RuntimeError("ERA5 station-hour timestamps must use UTC Datetime values")
    if frame.filter(
        pl.col("station_name").is_null() | (pl.col("station_name").str.strip_chars() == "")
    ).height:
        raise RuntimeError("ERA5 station-hour frame has an empty station name")
    if frame.group_by("station_name", "ts_utc").len().filter(pl.col("len") > 1).height:
        raise RuntimeError("ERA5 station-hour keys are duplicate")

    numeric_columns = ("grid_lat", "grid_lon", *_ERA5_SOURCE_COLUMNS)
    invalid = frame.filter(
        pl.any_horizontal(
            *(
                pl.col(name).is_null() | ~pl.col(name).cast(pl.Float64, strict=False).is_finite()
                for name in numeric_columns
            )
        )
    )
    if not invalid.is_empty():
        raise RuntimeError("ERA5 station-hour frame has a null or non-finite source value")
    if (
        frame.group_by("station_name")
        .agg(
            pl.col("grid_lat").n_unique().alias("latitudes"),
            pl.col("grid_lon").n_unique().alias("longitudes"),
        )
        .filter((pl.col("latitudes") != 1) | (pl.col("longitudes") != 1))
        .height
    ):
        raise RuntimeError("ERA5 station-hour frame has inconsistent station grid coordinates")

    local = frame.with_columns(
        pl.col("ts_utc")
        .dt.convert_time_zone("Asia/Taipei")
        .dt.replace_time_zone(None)
        .alias("ts_local")
    ).with_columns(pl.col("ts_local").dt.truncate("1mo").cast(pl.Date).alias("month"))
    if local.filter(~pl.col("month").dt.year().is_in(years)).height:
        raise RuntimeError("ERA5 station-hour frame has a local month outside the configured years")

    monthly = local.group_by("station_name", "month").agg(
        pl.len().cast(pl.Int64).alias("n_hours"),
        pl.col("blh_m").cast(pl.Float64).mean().alias("era5_blh_mean_m"),
        pl.col("u10_m_s").cast(pl.Float64).mean().alias("era5_u10_mean_m_s"),
        pl.col("v10_m_s").cast(pl.Float64).mean().alias("era5_v10_mean_m_s"),
        (pl.col("u10_m_s").cast(pl.Float64).pow(2) + pl.col("v10_m_s").cast(pl.Float64).pow(2))
        .sqrt()
        .mean()
        .alias("era5_wind_speed_mean_m_s"),
        pl.col("t2m_k").cast(pl.Float64).mean().alias("era5_t2m_mean_k"),
        (pl.col("t2m_k").cast(pl.Float64) - pl.col("d2m_k").cast(pl.Float64))
        .mean()
        .alias("era5_dewpoint_depression_mean_k"),
        pl.col("sp_pa").cast(pl.Float64).mean().alias("era5_sp_mean_pa"),
    )
    incomplete = [
        row
        for row in monthly.iter_rows(named=True)
        if row["n_hours"] != monthrange(row["month"].year, row["month"].month)[1] * 24
    ]
    if incomplete:
        raise RuntimeError(
            "ERA5 station-hour frame does not contain complete local calendar months"
        )
    return (
        monthly.select(*_ERA5_MONTHLY_SCHEMA)
        .cast(pl.Schema(_ERA5_MONTHLY_SCHEMA), strict=True)
        .sort("station_name", "month")
    )


def _optional_finite(value: object, *, label: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RuntimeError(f"{label} must be a finite number or null")
    converted = float(value)
    if not math.isfinite(converted):
        raise RuntimeError(f"{label} must be a finite number or null")
    return converted


def _required_columns(frame: pl.DataFrame, columns: tuple[str, ...], *, label: str) -> None:
    missing = set(columns).difference(frame.columns)
    if missing:
        raise RuntimeError(f"{label} is missing columns: {sorted(missing)}")


def _required_bool(value: object, *, label: str) -> bool:
    if not isinstance(value, bool):
        raise RuntimeError(f"{label} must be Boolean")
    return value


def pivot_satellite_monthly(frame: pl.DataFrame) -> pl.DataFrame:
    """Pivot one complete set of M8 source rows per station-month without filling nulls."""
    _required_columns(frame, _SATELLITE_LONG_COLUMNS, label="satellite panel")
    selected = frame.select(*_SATELLITE_LONG_COLUMNS)
    if selected.schema["month"] != pl.Date:
        raise RuntimeError("satellite panel month must use Date values")
    if selected.is_empty():
        raise RuntimeError("satellite panel is empty")
    if selected.filter(
        pl.col("station_name").is_null()
        | (pl.col("station_name").cast(pl.String).str.strip_chars() == "")
    ).height:
        raise RuntimeError("satellite panel has an empty station name")
    sources = set(selected["source"].to_list())
    if sources.difference(_SATELLITE_SOURCES):
        raise RuntimeError("satellite panel has an unexpected source name")
    if set(_SATELLITE_SOURCES).difference(sources):
        raise RuntimeError("satellite panel lacks a complete source set")
    if selected.group_by("source", "station_name", "month").len().filter(pl.col("len") > 1).height:
        raise RuntimeError("satellite panel has duplicate source station-month keys")

    rows: list[dict[str, object]] = []
    for key, group in selected.group_by("station_name", "month", maintain_order=True):
        station_name, month = key
        if group.height != len(_SATELLITE_SOURCES) or set(group["source"].to_list()) != set(
            _SATELLITE_SOURCES
        ):
            raise RuntimeError("satellite panel lacks a complete source set")
        ground_states: list[tuple[float | None, bool, bool | None, bool, bool]] = []
        values: dict[str, float | None] = {}
        for item in group.iter_rows(named=True):
            source = str(item["source"])
            satellite = _optional_finite(item["satellite_value"], label=f"{source} satellite value")
            satellite_observed = _required_bool(
                item["satellite_observed"], label=f"{source} satellite observed flag"
            )
            if satellite_observed != (satellite is not None):
                raise RuntimeError("satellite observed flags disagree with satellite nulls")
            ground = _optional_finite(item["ground_value"], label="satellite ground value")
            present = _required_bool(
                item["ground_row_present"], label="satellite ground row-present flag"
            )
            threshold = item["ground_meets_threshold"]
            if threshold is not None and not isinstance(threshold, bool):
                raise RuntimeError("satellite ground threshold flag must be Boolean or null")
            observed = _required_bool(
                item["ground_observed"], label="satellite ground observed flag"
            )
            withheld = _required_bool(
                item["ground_withheld"], label="satellite ground withheld flag"
            )
            if not present:
                if ground is not None or threshold is not None or observed or withheld:
                    raise RuntimeError(
                        "satellite ground state flags disagree with the ground value"
                    )
            elif ground is None:
                if threshold is not False or observed or not withheld:
                    raise RuntimeError(
                        "satellite ground state flags disagree with the ground value"
                    )
            elif threshold is not True or not observed or withheld:
                raise RuntimeError("satellite ground state flags disagree with the ground value")
            pair_observed = _required_bool(
                item["pair_observed"], label="satellite pair observed flag"
            )
            if pair_observed != (satellite_observed and observed):
                raise RuntimeError(
                    "satellite pair observed flags disagree with source and ground states"
                )
            values[source] = satellite
            ground_states.append((ground, present, threshold, observed, withheld))
        if len(set(ground_states)) != 1:
            raise RuntimeError("satellite ground state differs across source rows")
        ground, present, threshold, observed, withheld = ground_states[0]
        rows.append(
            {
                "station_name": station_name,
                "month": month,
                "ground_value": ground,
                "ground_row_present": present,
                "ground_meets_threshold": threshold,
                "ground_observed": observed,
                "ground_withheld": withheld,
                **values,
            }
        )
    return pl.DataFrame(rows, schema=_SATELLITE_MONTHLY_SCHEMA).sort("station_name", "month")


def _require_exact_key_grid(
    authoritative: pl.DataFrame, candidate: pl.DataFrame, *, label: str
) -> None:
    key = ("station_name", "month")
    if candidate.group_by(*key).len().filter(pl.col("len") > 1).height:
        raise RuntimeError(f"{label} has duplicate station-month keys")
    expected = authoritative.select(*key)
    observed = candidate.select(*key)
    if (
        expected.height != observed.height
        or not expected.join(observed, on=list(key), how="anti").is_empty()
        or not observed.join(expected, on=list(key), how="anti").is_empty()
    ):
        raise RuntimeError(f"{label} keys do not match the authoritative panel")


def _external_frames(inputs: FrozenInputs) -> tuple[pl.DataFrame, pl.DataFrame]:
    era5_paths = [path.path for path in inputs.input_files if "era5" in path.path.parts]
    satellite_paths = [
        path.path for path in inputs.input_files if "m8_satellite" in path.path.parts
    ]
    if not era5_paths or not satellite_paths:
        raise RuntimeError("spatial covariate external input paths are missing")
    try:
        era5 = pl.concat([pl.read_parquet(path) for path in era5_paths], how="vertical_relaxed")
        satellite = pl.concat(
            [pl.read_parquet(path) for path in satellite_paths], how="vertical_relaxed"
        )
    except (OSError, pl.exceptions.PolarsError) as exc:
        raise RuntimeError("spatial covariate external input is unreadable") from exc
    return era5, satellite


def _local_year_rows(frame: pl.DataFrame, *, years: tuple[int, int]) -> pl.DataFrame:
    _required_columns(frame, ("ts_utc",), label="ERA5 station-hour frame")
    if frame.schema["ts_utc"] != pl.Datetime(time_zone="UTC"):
        raise RuntimeError("ERA5 station-hour timestamps must use UTC Datetime values")
    return (
        frame.with_columns(
            pl.col("ts_utc")
            .dt.convert_time_zone("Asia/Taipei")
            .dt.replace_time_zone(None)
            .dt.year()
            .alias("_local_year")
        )
        .filter(pl.col("_local_year").is_in(years))
        .drop("_local_year")
    )


def assemble_covariates(inputs: FrozenInputs, config: CovariateReadinessConfig) -> pl.DataFrame:
    """Join the frozen target panel to exact spatial, weather, and satellite features."""
    panel = inputs.panel.select("station_name", "month", "mean", "target_state", "lon", "lat")
    _require_exact_key_grid(panel, panel, label="authoritative panel")
    if set(panel["target_state"].to_list()) != {"observed", "withheld"}:
        raise RuntimeError("authoritative panel target states changed")
    observed = panel.filter(pl.col("target_state") == "observed")
    withheld = panel.filter(pl.col("target_state") == "withheld")
    if (
        observed.filter(pl.col("mean").is_null() | ~pl.col("mean").is_finite()).height
        or withheld.filter(pl.col("mean").is_not_null()).height
    ):
        raise RuntimeError("authoritative panel target values changed")
    support = inputs.support.select("station_name", "x_m", "y_m")
    if support.group_by("station_name").len().filter(pl.col("len") > 1).height:
        raise RuntimeError("support coordinates have duplicate station names")
    if set(support["station_name"].to_list()) != set(panel["station_name"].to_list()):
        raise RuntimeError("support coordinates do not match the authoritative station set")
    if support.filter(
        pl.any_horizontal(
            *(
                pl.col(name).is_null() | ~pl.col(name).cast(pl.Float64).is_finite()
                for name in ("x_m", "y_m")
            )
        )
    ).height:
        raise RuntimeError("support coordinates contain a non-finite value")

    era5_raw, satellite_raw = _external_frames(inputs)
    era5 = aggregate_era5_monthly(
        _local_year_rows(era5_raw, years=config.years), years=config.years
    )
    satellite = pivot_satellite_monthly(satellite_raw)
    _require_exact_key_grid(panel, era5, label="ERA5 monthly")
    _require_exact_key_grid(panel, satellite, label="satellite")
    expected_ground = panel.select("station_name", "month", "mean", "target_state").rename(
        {"mean": "ground_value"}
    )
    compared_ground = satellite.select(
        "station_name",
        "month",
        "ground_value",
        "ground_row_present",
        "ground_meets_threshold",
        "ground_observed",
        "ground_withheld",
    ).join(expected_ground, on=["station_name", "month"], how="inner")
    if compared_ground.filter(
        ~pl.col("ground_row_present")
        | (pl.col("ground_meets_threshold") != (pl.col("target_state") == "observed"))
        | (pl.col("ground_observed") != (pl.col("target_state") == "observed"))
        | (pl.col("ground_withheld") != (pl.col("target_state") == "withheld"))
        | (pl.col("ground_value") != pl.col("ground_value_right"))
        | (pl.col("ground_value").is_null() != pl.col("ground_value_right").is_null())
    ).height:
        raise RuntimeError("satellite ground state does not match the authoritative panel")

    result = (
        panel.join(support, on="station_name", how="left")
        .join(era5.drop("n_hours"), on=["station_name", "month"], how="left")
        .join(
            satellite.select("station_name", "month", *_SATELLITE_SOURCES),
            on=["station_name", "month"],
            how="left",
        )
        .with_columns(
            pl.col("mean").alias("PM2.5"),
            ((pl.col("month").dt.month() * (2.0 * math.pi / 12.0)).sin()).alias("month_sin"),
            ((pl.col("month").dt.month() * (2.0 * math.pi / 12.0)).cos()).alias("month_cos"),
        )
        .select(*_COVARIATE_SCHEMA)
        .cast(pl.Schema(_COVARIATE_SCHEMA), strict=True)
        .sort("station_name", "month")
    )
    required_finite = tuple(
        name
        for name in _COVARIATE_SCHEMA
        if name not in {"station_name", "month", "target_state", "PM2.5", *_SATELLITE_SOURCES}
    )
    if result.filter(
        pl.any_horizontal(
            *(pl.col(name).is_null() | ~pl.col(name).is_finite() for name in required_finite)
        )
    ).height:
        raise RuntimeError("assembled covariates contain a non-finite required feature")
    return result


def _mapping(value: object, *, path: str, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{path} must be a mapping")
    if set(value) != keys:
        raise ConfigError(f"{path} must contain exactly {sorted(keys)}")
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


def _identity(value: object, *, path: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ConfigError(f"{path} must be a lowercase SHA-256")
    return value


def _strings(value: object, *, path: str) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or any(not isinstance(item, str) or not item for item in value)
        or len(value) != len(set(value))
    ):
        raise ConfigError(f"{path} must be a unique list of non-empty strings")
    return tuple(value)


def load_spatial_covariate_readiness_config(
    config: dict[str, Any] | None = None,
) -> CovariateReadinessConfig:
    """Load the exact, reviewed readiness configuration without allowing drift."""
    raw = config if config is not None else load_conf("spatial_covariate_readiness")
    root = "spatial_covariate_readiness"
    if not isinstance(raw, dict):
        raise ConfigError(f"{root} must be a mapping")
    if set(raw) != {"schema_version", "analysis", "methods", "validation"}:
        raise ConfigError(
            f"{root} must contain exactly ['analysis', 'methods', 'schema_version', 'validation']"
        )
    if raw.get("schema_version") != 1:
        raise ConfigError(f"{root}.schema_version must be 1")

    analysis = _mapping(
        raw.get("analysis"),
        path=f"{root}.analysis",
        keys={
            "years",
            "baseline_generation_sha256",
            "station_inventory_generation_sha256",
            "minimum_train_stations",
        },
    )
    methods = _mapping(
        raw.get("methods"),
        path=f"{root}.methods",
        keys={"comparator", "candidates", "idw_power", "minimum_distance_km", "model"},
    )
    validation = _mapping(
        raw.get("validation"),
        path=f"{root}.validation",
        keys={"evaluations", "bootstrap_draws", "bootstrap_seed"},
    )
    years = analysis.get("years")
    if years != [2024, 2025]:
        raise ConfigError(f"{root}.analysis.years must be [2024, 2025]")

    model_raw = _mapping(
        methods.get("model"),
        path=f"{root}.methods.model",
        keys={"n_estimators", "learning_rate", "num_leaves", "min_child_samples", "n_jobs", "seed"},
    )
    model = ModelConfig(
        n_estimators=_positive_int(
            model_raw.get("n_estimators"), path=f"{root}.methods.model.n_estimators"
        ),
        learning_rate=_positive_float(
            model_raw.get("learning_rate"), path=f"{root}.methods.model.learning_rate"
        ),
        num_leaves=_positive_int(
            model_raw.get("num_leaves"), path=f"{root}.methods.model.num_leaves"
        ),
        min_child_samples=_positive_int(
            model_raw.get("min_child_samples"), path=f"{root}.methods.model.min_child_samples"
        ),
        n_jobs=_positive_int(model_raw.get("n_jobs"), path=f"{root}.methods.model.n_jobs"),
        seed=_positive_int(model_raw.get("seed"), path=f"{root}.methods.model.seed"),
    )
    for field in (
        "n_estimators",
        "learning_rate",
        "num_leaves",
        "min_child_samples",
        "n_jobs",
        "seed",
    ):
        if getattr(model, field) != getattr(_EXPECTED_MODEL, field):
            raise ConfigError(f"{root}.methods.model.{field} must preserve the fixed serial model")

    candidates = _strings(methods.get("candidates"), path=f"{root}.methods.candidates")
    if methods.get("comparator") != "idw2":
        raise ConfigError(f"{root}.methods.comparator must be idw2")
    if (methods.get("comparator"), *candidates) != COVARIATE_READINESS_METHODS:
        raise ConfigError(f"{root}.methods.candidates must preserve the fixed method domain")
    idw_power = _positive_float(methods.get("idw_power"), path=f"{root}.methods.idw_power")
    if idw_power != 2.0:
        raise ConfigError(f"{root}.methods.idw_power must be 2.0")
    minimum_distance_km = _positive_float(
        methods.get("minimum_distance_km"), path=f"{root}.methods.minimum_distance_km"
    )
    if minimum_distance_km != 0.1:
        raise ConfigError(f"{root}.methods.minimum_distance_km must be 0.1")

    evaluations = _strings(validation.get("evaluations"), path=f"{root}.validation.evaluations")
    if evaluations != COVARIATE_READINESS_EVALUATIONS:
        raise ConfigError(
            f"{root}.validation.evaluations must preserve the fixed evaluation domain"
        )
    bootstrap_draws = _positive_int(
        validation.get("bootstrap_draws"), path=f"{root}.validation.bootstrap_draws"
    )
    if bootstrap_draws != 9999:
        raise ConfigError(f"{root}.validation.bootstrap_draws must be 9999")
    bootstrap_seed = _positive_int(
        validation.get("bootstrap_seed"), path=f"{root}.validation.bootstrap_seed"
    )
    if bootstrap_seed != 20260828:
        raise ConfigError(f"{root}.validation.bootstrap_seed must be 20260828")
    minimum_train_stations = _positive_int(
        analysis.get("minimum_train_stations"), path=f"{root}.analysis.minimum_train_stations"
    )
    if minimum_train_stations != 8:
        raise ConfigError(f"{root}.analysis.minimum_train_stations must be 8")

    return CovariateReadinessConfig(
        years=(2024, 2025),
        baseline_generation_sha256=_identity(
            analysis.get("baseline_generation_sha256"),
            path=f"{root}.analysis.baseline_generation_sha256",
        ),
        station_inventory_generation_sha256=_identity(
            analysis.get("station_inventory_generation_sha256"),
            path=f"{root}.analysis.station_inventory_generation_sha256",
        ),
        minimum_train_stations=minimum_train_stations,
        methods=(
            COVARIATE_READINESS_METHODS[0],
            COVARIATE_READINESS_METHODS[1],
            COVARIATE_READINESS_METHODS[2],
        ),
        comparator="idw2",
        idw_power=idw_power,
        minimum_distance_km=minimum_distance_km,
        model=model,
        evaluations=(
            COVARIATE_READINESS_EVALUATIONS[0],
            COVARIATE_READINESS_EVALUATIONS[1],
            COVARIATE_READINESS_EVALUATIONS[2],
        ),
        bootstrap_draws=bootstrap_draws,
        bootstrap_seed=bootstrap_seed,
    )


def _input_file(path: Path, *, label: str) -> InputFile:
    if not path.is_file():
        raise RuntimeError(f"{label} is missing: {path}")
    payload = path.read_bytes()
    return InputFile(path=path, bytes=len(payload), sha256=sha256(payload).hexdigest())


def _read_json_stable(path: Path, *, label: str) -> tuple[dict[str, Any], InputFile]:
    before = _input_file(path, label=label)
    try:
        import json

        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise RuntimeError(f"{label} is unreadable: {path}") from exc
    after = _input_file(path, label=label)
    if before != after:
        raise RuntimeError(f"{label} changed while it was read: {path}")
    if not isinstance(raw, dict):
        raise RuntimeError(f"{label} must be a JSON object")
    return raw, before


def _read_parquet_stable(path: Path, *, label: str) -> tuple[pl.DataFrame, InputFile]:
    before = _input_file(path, label=label)
    try:
        frame = pl.read_parquet(path)
    except (OSError, pl.exceptions.PolarsError) as exc:
        raise RuntimeError(f"{label} is unreadable: {path}") from exc
    after = _input_file(path, label=label)
    if before != after:
        raise RuntimeError(f"{label} changed while it was read: {path}")
    return frame, before


def _within_data_root(path: Path, *, data_root: Path, label: str) -> Path:
    root = data_root.resolve()
    resolved = path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise RuntimeError(f"{label} is outside data root") from exc
    return resolved


def _require_exact_schema(frame: pl.DataFrame, *, name: str) -> pl.DataFrame:
    schema = SPATIAL_BASELINE_TABLE_SCHEMAS[name]
    if dict(frame.schema) != schema:
        raise RuntimeError(f"spatial covariate baseline {name} schema changed")
    return (
        frame.select(*schema)
        .cast(pl.Schema(schema), strict=True)
        .sort(*SPATIAL_BASELINE_TABLE_ORDER[name])
    )


def _unique(frame: pl.DataFrame, columns: tuple[str, ...], *, label: str) -> None:
    duplicates = frame.group_by(*columns).len().filter(pl.col("len") > 1)
    if not duplicates.is_empty():
        raise RuntimeError(f"spatial covariate baseline has duplicate {label}")


def _validate_baseline_tables(
    stations: pl.DataFrame,
    panel: pl.DataFrame,
    support: pl.DataFrame,
    folds: pl.DataFrame,
) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    selected_stations = _require_exact_schema(stations, name="stations")
    selected_panel = _require_exact_schema(panel, name="panel")
    selected_support = _require_exact_schema(support, name="support")
    selected_folds = _require_exact_schema(folds, name="folds")

    _unique(selected_stations, ("station_name",), label="station names")
    _unique(selected_support, ("station_name",), label="support station names")
    _unique(selected_panel, ("station_name", "month"), label="station-month keys")
    _unique(
        selected_folds,
        ("evaluation", "year", "month", "target_station"),
        label="fold target keys",
    )
    if selected_stations.height != _REVIEWED_STATION_COUNT:
        raise RuntimeError("spatial covariate baseline station count changed")
    if selected_panel.height != _REVIEWED_PANEL_KEY_COUNT:
        raise RuntimeError("spatial covariate baseline panel key count changed")
    station_names = set(selected_stations["station_name"].to_list())
    support_names = set(selected_support["station_name"].to_list())
    panel_names = set(selected_panel["station_name"].to_list())
    if not station_names or None in station_names or support_names != station_names:
        raise RuntimeError("spatial covariate baseline station set does not match support")
    if panel_names != station_names:
        raise RuntimeError(
            "spatial covariate baseline panel station set does not match stations/support"
        )
    if not set(selected_folds["target_station"].to_list()).issubset(station_names):
        raise RuntimeError("spatial covariate baseline fold station set does not match stations")

    target_states = set(selected_panel["target_state"].to_list())
    if target_states != {"observed", "withheld"}:
        raise RuntimeError("spatial covariate baseline target states must be observed and withheld")
    observed = selected_panel.filter(pl.col("target_state") == "observed")
    withheld = selected_panel.filter(pl.col("target_state") == "withheld")
    if withheld.height != 1:
        raise RuntimeError("spatial covariate baseline withheld count changed")
    if observed.height != _REVIEWED_OBSERVED_COUNT:
        raise RuntimeError("spatial covariate baseline observed count changed")
    if (
        observed.filter(
            pl.col("mean").is_null() | ~pl.col("mean").is_finite() | ~pl.col("meets_threshold")
        ).height
        or withheld.filter(pl.col("mean").is_not_null() | pl.col("meets_threshold")).height
    ):
        raise RuntimeError("spatial covariate baseline target-state values changed")
    if withheld.select("station_name", "month", "mean").rows() != [(*_REVIEWED_WITHHELD_KEY, None)]:
        raise RuntimeError("spatial covariate baseline withheld identity changed")
    expected_target_states = pl.DataFrame(
        {"evaluation": list(COVARIATE_READINESS_EVALUATIONS)}, schema={"evaluation": pl.String}
    ).join(
        selected_panel.select("station_name", "month", "target_state").rename(
            {"station_name": "target_station"}
        ),
        how="cross",
    )
    fold_target_states = selected_folds.select(
        "evaluation", "target_station", "month", "target_state"
    )
    target_state_key = ("evaluation", "target_station", "month", "target_state")
    if (
        expected_target_states.height != fold_target_states.height
        or not expected_target_states.join(
            fold_target_states, on=target_state_key, how="anti"
        ).is_empty()
        or not fold_target_states.join(
            expected_target_states, on=target_state_key, how="anti"
        ).is_empty()
    ):
        raise RuntimeError("spatial covariate baseline target-state counts or fold keys changed")
    return selected_stations, selected_panel, selected_support, selected_folds


def _validate_manifest_member_identities(
    manifest: dict[str, Any], identities: list[InputFile]
) -> None:
    members = manifest.get("members")
    if not isinstance(members, dict):
        raise RuntimeError("spatial covariate baseline manifest member identities are missing")
    if tuple(identity.path.name for identity in identities) != _BASELINE_MEMBER_NAMES:
        raise RuntimeError("spatial covariate baseline member paths changed")
    for identity in identities:
        expected = members.get(identity.path.name)
        if expected != {"bytes": identity.bytes, "sha256": identity.sha256}:
            raise RuntimeError(
                f"spatial covariate baseline manifest member identity changed: {identity.path.name}"
            )


def load_frozen_inputs(data_root: Path, config: CovariateReadinessConfig) -> FrozenInputs:
    """Load only reviewed baseline tables and freeze their external input identities."""
    root = data_root.resolve()
    baseline_directory = _within_data_root(
        root
        / "outputs"
        / "spatial_surface_baseline"
        / "generations"
        / config.baseline_generation_sha256,
        data_root=root,
        label="spatial covariate baseline generation",
    )
    if not baseline_directory.is_dir():
        raise RuntimeError(
            f"spatial covariate baseline generation is missing: {config.baseline_generation_sha256}"
        )

    manifest, manifest_file = _read_json_stable(
        baseline_directory / "manifest.json", label="spatial covariate baseline manifest"
    )
    if manifest.get("complete") is not True:
        raise RuntimeError("spatial covariate baseline manifest is not complete")
    if manifest.get("generation_sha256") != config.baseline_generation_sha256:
        raise RuntimeError("spatial covariate baseline manifest generation does not match config")
    if manifest.get("inventory_generation_sha256") != config.station_inventory_generation_sha256:
        raise RuntimeError(
            "spatial covariate baseline station inventory generation does not match config"
        )

    files: list[InputFile] = [manifest_file]
    frames: dict[str, pl.DataFrame] = {}
    for name in ("stations", "panel", "support", "folds"):
        path = _within_data_root(
            baseline_directory / f"{name}.parquet",
            data_root=root,
            label=f"spatial covariate baseline {name}",
        )
        frame, identity = _read_parquet_stable(path, label=f"spatial covariate baseline {name}")
        frames[name] = frame
        files.append(identity)
    _validate_manifest_member_identities(manifest, files[1:])
    stations, panel, support, baseline_folds = _validate_baseline_tables(
        frames["stations"], frames["panel"], frames["support"], frames["folds"]
    )

    external_paths = [
        root
        / "interim"
        / "era5"
        / "generations"
        / config.station_inventory_generation_sha256
        / f"year={year}"
        / "era5_station_hour.parquet"
        for year in (2023, 2024, 2025)
    ] + [
        root
        / "outputs"
        / "m8_satellite"
        / "generations"
        / config.station_inventory_generation_sha256
        / f"year={year}"
        / "panel.parquet"
        for year in (2024, 2025)
    ]
    for path in external_paths:
        resolved = _within_data_root(path, data_root=root, label="spatial covariate external input")
        files.append(_input_file(resolved, label="spatial covariate external input"))

    return FrozenInputs(
        stations=stations,
        panel=panel,
        support=support,
        baseline_folds=baseline_folds,
        input_files=tuple(files),
        baseline_generation_sha256=config.baseline_generation_sha256,
        station_inventory_generation_sha256=config.station_inventory_generation_sha256,
    )
