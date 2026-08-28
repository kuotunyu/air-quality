"""Freeze the reviewed inputs for the spatial covariate readiness gate."""

from __future__ import annotations

import math
import re
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from twair.analysis.era5_value import ModelConfig
from twair.analysis.spatial_surface_baseline import (
    SPATIAL_BASELINE_TABLE_ORDER,
    SPATIAL_BASELINE_TABLE_SCHEMAS,
    load_spatial_surface_baseline_config,
    predict_target,
)
from twair.config import ConfigError, load_conf

__all__ = [
    "COVARIATE_MODEL_FEATURES",
    "COVARIATE_READINESS_EVALUATIONS",
    "COVARIATE_READINESS_METHODS",
    "CovariatePrediction",
    "CovariateReadinessConfig",
    "FrozenInputs",
    "InputFile",
    "aggregate_era5_monthly",
    "assemble_covariates",
    "build_covariate_fold_ledger",
    "fit_covariate_model",
    "load_frozen_inputs",
    "load_spatial_covariate_readiness_config",
    "pivot_satellite_monthly",
    "predict_readiness_methods",
]


COVARIATE_READINESS_METHODS = ("idw2", "covariate_gbm", "covariate_gbm_idw2")
COVARIATE_READINESS_EVALUATIONS = ("buffer_20km", "buffer_40km", "spatial_cluster")
COVARIATE_MODEL_FEATURES = (
    "x_m",
    "y_m",
    "month_sin",
    "month_cos",
    "era5_blh_mean_m",
    "era5_u10_mean_m_s",
    "era5_v10_mean_m_s",
    "era5_wind_speed_mean_m_s",
    "era5_t2m_mean_k",
    "era5_dewpoint_depression_mean_k",
    "era5_sp_mean_pa",
    "maiac_aod",
    "s5p_no2",
    "s5p_so2",
)
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
_COVARIATE_LEDGER_SCHEMA: dict[str, pl.DataType | type[pl.DataType]] = {
    "evaluation": pl.String,
    "training_period": pl.String,
    "train_year": pl.Int64,
    "target_year": pl.Int64,
    "month": pl.Date,
    "target_station": pl.String,
    "target_cluster": pl.Int64,
    "target_state": pl.String,
    "observed": pl.Float64,
    "train_stations": pl.List(pl.String),
    "n_train_stations": pl.Int64,
    "n_model_train_rows": pl.Int64,
    "n_same_month_train_rows": pl.Int64,
    "fold_state": pl.String,
    "fold_reason": pl.String,
}
_COVARIATE_TARGET_KEY = (
    "evaluation",
    "training_period",
    "train_year",
    "target_year",
    "month",
    "target_station",
)
_COVARIATE_PREDICTION_SCHEMA: dict[str, pl.DataType | type[pl.DataType]] = {
    **_COVARIATE_LEDGER_SCHEMA,
    "method": pl.String,
    "predicted": pl.Float64,
    "prediction_state": pl.String,
    "failure_type": pl.String,
    "error": pl.Float64,
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


@dataclass(frozen=True, slots=True)
class CovariatePrediction:
    value: float | None
    state: str
    failure_type: str | None = None


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
    for (_, month), group in local.group_by("station_name", "month", maintain_order=True):
        expected_hours = monthrange(month.year, month.month)[1] * 24
        expected_timestamps = [
            datetime(month.year, month.month, 1) + timedelta(hours=offset)
            for offset in range(expected_hours)
        ]
        actual_timestamps = group["ts_local"].unique().sort().to_list()
        if actual_timestamps != expected_timestamps:
            raise RuntimeError(
                "ERA5 station-hour frame does not contain complete local calendar hours"
            )

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


def _station_coordinates(
    covariates: pl.DataFrame, support: pl.DataFrame
) -> dict[str, tuple[float, float]]:
    _required_columns(support, ("station_name", "x_m", "y_m"), label="baseline support")
    if support.group_by("station_name").len().filter(pl.col("len") > 1).height:
        raise RuntimeError("baseline support has duplicate station coordinates")
    station_names = set(covariates["station_name"].to_list())
    if set(support["station_name"].to_list()) != station_names:
        raise RuntimeError("baseline support coordinate station set changed")
    if covariates.filter(
        pl.any_horizontal(
            *(
                pl.col(name).is_null() | ~pl.col(name).cast(pl.Float64, strict=False).is_finite()
                for name in ("x_m", "y_m")
            )
        )
    ).height:
        raise RuntimeError("covariates contain a non-finite station coordinate")
    if support.filter(
        pl.any_horizontal(
            *(
                pl.col(name).is_null() | ~pl.col(name).cast(pl.Float64, strict=False).is_finite()
                for name in ("x_m", "y_m")
            )
        )
    ).height:
        raise RuntimeError("baseline support contains a non-finite coordinate")
    covariate_coordinates = covariates.select("station_name", "x_m", "y_m").unique()
    if covariate_coordinates.height != len(station_names):
        raise RuntimeError("covariate station coordinates change across months")
    compared = covariate_coordinates.join(
        support.select("station_name", "x_m", "y_m"),
        on="station_name",
        how="inner",
        suffix="_support",
    )
    if compared.filter(
        pl.col("x_m").ne_missing(pl.col("x_m_support"))
        | pl.col("y_m").ne_missing(pl.col("y_m_support"))
    ).height:
        raise RuntimeError("covariate coordinates do not match frozen baseline support coordinates")
    return {
        str(station): (float(x_m), float(y_m))
        for station, x_m, y_m in support.select("station_name", "x_m", "y_m").iter_rows()
    }


def _baseline_cluster_mapping(baseline_folds: pl.DataFrame) -> dict[str, int]:
    cluster_counts = baseline_folds.group_by("target_station").agg(
        pl.col("target_cluster").n_unique().alias("clusters")
    )
    if cluster_counts.filter(pl.col("clusters") != 1).height:
        raise RuntimeError("baseline cluster membership changes across authoritative folds")
    return {
        str(station): int(cluster)
        for station, cluster in baseline_folds.select("target_station", "target_cluster")
        .unique()
        .iter_rows()
    }


def _allowed_stations(
    *,
    evaluation: str,
    target_station: str,
    station_names: set[str],
    coordinates: dict[str, tuple[float, float]],
    clusters: dict[str, int],
) -> list[str]:
    if evaluation == "spatial_cluster":
        return sorted(
            station for station in station_names if clusters[station] != clusters[target_station]
        )
    radii = {"buffer_20km": 20.0, "buffer_40km": 40.0}
    if evaluation not in radii:
        raise RuntimeError(f"baseline fold has unexpected evaluation: {evaluation}")
    target_x, target_y = coordinates[target_station]
    radius_km = radii[evaluation]
    return sorted(
        station
        for station in station_names
        if station != target_station
        and math.hypot(
            coordinates[station][0] - target_x,
            coordinates[station][1] - target_y,
        )
        / 1000
        > radius_km
    )


def _validate_baseline_fold_membership(
    covariates: pl.DataFrame,
    baseline_folds: pl.DataFrame,
    *,
    coordinates: dict[str, tuple[float, float]],
    clusters: dict[str, int],
    evaluations: tuple[str, str, str],
) -> dict[tuple[str, str], list[str]]:
    required = (
        "evaluation",
        "year",
        "month",
        "target_station",
        "target_cluster",
        "target_state",
        "observed",
        "train_stations",
        "n_train",
    )
    _required_columns(baseline_folds, required, label="baseline folds")
    baseline_key = ("evaluation", "month", "target_station")
    if baseline_folds.group_by(*baseline_key).len().filter(pl.col("len") != 1).height:
        raise RuntimeError("baseline folds have duplicate target keys")
    station_names = set(covariates["station_name"].to_list())
    if set(clusters) != station_names:
        raise RuntimeError("baseline cluster membership station set changed")
    if set(baseline_folds["evaluation"].to_list()) != set(evaluations):
        raise RuntimeError("baseline fold evaluation set changed")
    expected_keys = pl.concat(
        [
            covariates.select(
                pl.lit(evaluation).alias("evaluation"),
                "month",
                pl.col("station_name").alias("target_station"),
            )
            for evaluation in evaluations
        ]
    )
    actual_keys = baseline_folds.select(*baseline_key)
    if (
        expected_keys.height != actual_keys.height
        or not expected_keys.join(actual_keys, on=list(baseline_key), how="anti").is_empty()
        or not actual_keys.join(expected_keys, on=list(baseline_key), how="anti").is_empty()
    ):
        raise RuntimeError("baseline fold target keys changed")
    target_rows = covariates.select("station_name", "month", "target_state", "PM2.5").rename(
        {"station_name": "target_station"}
    )
    compared = baseline_folds.select(
        "evaluation",
        "month",
        "year",
        "target_station",
        "target_state",
        "observed",
        "target_cluster",
    ).join(target_rows, on=["target_station", "month"], how="inner", suffix="_covariate")
    if compared.filter(
        (pl.col("year") != pl.col("month").dt.year())
        | (pl.col("target_state") != pl.col("target_state_covariate"))
        | (pl.col("observed") != pl.col("PM2.5"))
        | (pl.col("observed").is_null() != pl.col("PM2.5").is_null())
        | (
            pl.col("target_cluster")
            != pl.col("target_station").replace_strict(clusters, return_dtype=pl.Int64)
        )
    ).height:
        raise RuntimeError("baseline fold target or cluster membership changed")

    observed_by_month = {
        month: set(group["station_name"].to_list())
        for (month,), group in covariates.filter(pl.col("target_state") == "observed").group_by(
            "month", maintain_order=True
        )
    }
    allowed_by_fold: dict[tuple[str, str], list[str]] = {}
    for evaluation in evaluations:
        for target_station in sorted(station_names):
            allowed_by_fold[(evaluation, target_station)] = _allowed_stations(
                evaluation=evaluation,
                target_station=target_station,
                station_names=station_names,
                coordinates=coordinates,
                clusters=clusters,
            )
    for fold in baseline_folds.iter_rows(named=True):
        evaluation = str(fold["evaluation"])
        target_station = str(fold["target_station"])
        month = fold["month"]
        declared = [str(station) for station in fold["train_stations"]]
        expected = sorted(
            set(allowed_by_fold[(evaluation, target_station)]) & observed_by_month[month]
        )
        if declared != expected or int(fold["n_train"]) != len(expected):
            raise RuntimeError("baseline fold train station membership changed")
    return allowed_by_fold


def build_covariate_fold_ledger(
    covariates: pl.DataFrame,
    support: pl.DataFrame,
    baseline_folds: pl.DataFrame,
    config: CovariateReadinessConfig,
) -> pl.DataFrame:
    """Build same-year and forward ledgers without admitting held-location truth."""
    _required_columns(covariates, tuple(_COVARIATE_SCHEMA), label="covariates")
    key = ("station_name", "month")
    if covariates.group_by(*key).len().filter(pl.col("len") != 1).height:
        raise RuntimeError("covariates have duplicate target keys")
    if covariates.schema["month"] != pl.Date:
        raise RuntimeError("covariate months must use Date values")
    if set(covariates["month"].dt.year().to_list()) != set(config.years):
        raise RuntimeError("covariate years do not match the configured analysis years")
    coordinates = _station_coordinates(covariates, support)
    clusters = _baseline_cluster_mapping(baseline_folds)
    allowed_by_fold = _validate_baseline_fold_membership(
        covariates,
        baseline_folds,
        coordinates=coordinates,
        clusters=clusters,
        evaluations=config.evaluations,
    )

    rows: list[dict[str, object]] = []
    for target in covariates.sort("month", "station_name").iter_rows(named=True):
        target_station = str(target["station_name"])
        target_month = target["month"]
        target_year = target_month.year
        periods: list[tuple[str, int]] = [("same_year", target_year)]
        if target_year == config.years[1]:
            periods.append((f"{config.years[0]}_to_{config.years[1]}", config.years[0]))
        for evaluation in config.evaluations:
            train_stations = allowed_by_fold[(evaluation, target_station)]
            for training_period, train_year in periods:
                model_train = covariates.filter(
                    (pl.col("month").dt.year() == train_year)
                    & pl.col("station_name").is_in(train_stations)
                    & (pl.col("target_state") == "observed")
                )
                source_month = date(train_year, target_month.month, 1)
                same_month_train = model_train.filter(pl.col("month") == source_month)
                if target["target_state"] != "observed":
                    fold_state = "unscored_target_withheld"
                    fold_reason: str | None = f"target_state={target['target_state']}"
                elif len(train_stations) < config.minimum_train_stations:
                    fold_state = "unscored_insufficient_train"
                    fold_reason = (
                        f"n_train_stations={len(train_stations)} is below "
                        f"minimum_train_stations={config.minimum_train_stations}"
                    )
                else:
                    fold_state = "eligible"
                    fold_reason = None
                rows.append(
                    {
                        "evaluation": evaluation,
                        "training_period": training_period,
                        "train_year": train_year,
                        "target_year": target_year,
                        "month": target_month,
                        "target_station": target_station,
                        "target_cluster": clusters[target_station],
                        "target_state": target["target_state"],
                        "observed": target["PM2.5"],
                        "train_stations": train_stations,
                        "n_train_stations": len(train_stations),
                        "n_model_train_rows": model_train.height,
                        "n_same_month_train_rows": same_month_train.height,
                        "fold_state": fold_state,
                        "fold_reason": fold_reason,
                    }
                )
    ledger = pl.DataFrame(rows, schema=_COVARIATE_LEDGER_SCHEMA).sort(*_COVARIATE_TARGET_KEY)
    if ledger.select(*_COVARIATE_TARGET_KEY).n_unique() != ledger.height:
        raise RuntimeError("covariate fold ledger target keys are duplicate")
    return ledger


def _model_matrix(frame: pl.DataFrame, *, label: str) -> np.ndarray:
    _required_columns(frame, COVARIATE_MODEL_FEATURES, label=label)
    non_satellite = tuple(
        feature for feature in COVARIATE_MODEL_FEATURES if feature not in _SATELLITE_SOURCES
    )
    if frame.filter(
        pl.any_horizontal(
            *(
                pl.col(feature).is_null()
                | ~pl.col(feature).cast(pl.Float64, strict=False).is_finite()
                for feature in non_satellite
            )
        )
    ).height:
        raise RuntimeError(f"{label} has a non-finite required model feature")
    if frame.filter(
        pl.any_horizontal(
            *(
                pl.col(feature).is_not_null()
                & ~pl.col(feature).cast(pl.Float64, strict=False).is_finite()
                for feature in _SATELLITE_SOURCES
            )
        )
    ).height:
        raise RuntimeError(f"{label} has a non-finite satellite feature")
    boundary = frame.select(
        *(
            pl.col(feature).cast(pl.Float64).fill_null(float("nan")).alias(feature)
            if feature in _SATELLITE_SOURCES
            else pl.col(feature).cast(pl.Float64)
            for feature in COVARIATE_MODEL_FEATURES
        )
    )
    return np.asarray(boundary.to_numpy(), dtype=float)


def fit_covariate_model(
    train: pl.DataFrame,
    predict: pl.DataFrame,
    config: CovariateReadinessConfig,
) -> np.ndarray:
    """Fit the frozen LightGBM once and predict the supplied ordered rows."""
    if train.is_empty():
        raise RuntimeError("covariate model training frame is empty")
    _required_columns(train, ("PM2.5",), label="covariate model training frame")
    if train.filter(pl.col("PM2.5").is_null() | ~pl.col("PM2.5").is_finite()).height:
        raise RuntimeError("covariate model training truth is non-finite")
    train_matrix = _model_matrix(train, label="covariate model training frame")
    prediction_matrix = _model_matrix(predict, label="covariate model prediction frame")
    model = config.model
    estimator = _lgbm_regressor(
        n_estimators=model.n_estimators,
        learning_rate=model.learning_rate,
        num_leaves=model.num_leaves,
        min_child_samples=model.min_child_samples,
        subsample=0.8,
        subsample_freq=1,
        colsample_bytree=0.8,
        n_jobs=model.n_jobs,
        random_state=model.seed,
        verbose=-1,
    )
    estimator.fit(train_matrix, np.asarray(train["PM2.5"].to_numpy(), dtype=float))
    return np.asarray(estimator.predict(prediction_matrix), dtype=float)


def _lgbm_regressor(
    *,
    n_estimators: int,
    learning_rate: float,
    num_leaves: int,
    min_child_samples: int,
    subsample: float,
    subsample_freq: int,
    colsample_bytree: float,
    n_jobs: int,
    random_state: int,
    verbose: int,
) -> Any:
    try:
        from lightgbm import LGBMRegressor
    except ModuleNotFoundError as error:
        raise RuntimeError(
            "spatial covariate model fitting requires the optional ml dependency"
        ) from error
    return LGBMRegressor(
        n_estimators=n_estimators,
        learning_rate=learning_rate,
        num_leaves=num_leaves,
        min_child_samples=min_child_samples,
        subsample=subsample,
        subsample_freq=subsample_freq,
        colsample_bytree=colsample_bytree,
        n_jobs=n_jobs,
        random_state=random_state,
        verbose=verbose,
    )


def _failed_prediction(state: str, failure_type: str | None = None) -> CovariatePrediction:
    return CovariatePrediction(value=None, state=state, failure_type=failure_type)


def _finite_covariate_prediction(value: float) -> CovariatePrediction:
    if math.isfinite(value):
        return CovariatePrediction(value=value, state="scored")
    return _failed_prediction("non_finite_prediction")


def _idw_prediction(
    train: pl.DataFrame,
    target: dict[str, Any],
    *,
    baseline_config: Any,
) -> CovariatePrediction:
    outcome = predict_target(train, target, "idw2", baseline_config)
    return CovariatePrediction(
        value=outcome.value,
        state=outcome.state,
        failure_type=outcome.failure_type,
    )


def _prediction_groups(ledger: pl.DataFrame) -> list[pl.DataFrame]:
    group_columns = (
        "evaluation",
        "training_period",
        "target_station",
        "target_year",
    )
    return ledger.sort(*_COVARIATE_TARGET_KEY).partition_by(
        list(group_columns), maintain_order=True
    )


def _validate_prediction_ledger(ledger: pl.DataFrame, config: CovariateReadinessConfig) -> None:
    forward_period = f"{config.years[0]}_to_{config.years[1]}"
    invalid_period = ledger.filter(
        ~pl.col("training_period").is_in(["same_year", forward_period])
        | (pl.col("target_year") != pl.col("month").dt.year())
        | (
            (pl.col("training_period") == "same_year")
            & (pl.col("train_year") != pl.col("target_year"))
        )
        | (
            (pl.col("training_period") == forward_period)
            & (
                (pl.col("train_year") != config.years[0])
                | (pl.col("target_year") != config.years[1])
            )
        )
    )
    if not invalid_period.is_empty():
        raise RuntimeError("covariate fold ledger training period metadata is invalid")
    if not set(ledger["evaluation"].to_list()).issubset(config.evaluations):
        raise RuntimeError("covariate fold ledger evaluation metadata is invalid")
    if ledger.filter(
        ~pl.col("target_state").is_in(["observed", "withheld"])
        | (
            (pl.col("target_state") == "observed")
            & (pl.col("observed").is_null() | ~pl.col("observed").is_finite())
        )
        | ((pl.col("target_state") == "withheld") & pl.col("observed").is_not_null())
    ).height:
        raise RuntimeError("covariate fold ledger target metadata is invalid")


def predict_readiness_methods(
    covariates: pl.DataFrame,
    ledger: pl.DataFrame,
    config: CovariateReadinessConfig,
) -> pl.DataFrame:
    """Emit the fixed comparator and two candidate states for every ledger target."""
    _required_columns(covariates, tuple(_COVARIATE_SCHEMA), label="covariates")
    _required_columns(ledger, tuple(_COVARIATE_LEDGER_SCHEMA), label="covariate fold ledger")
    if ledger.select(*_COVARIATE_TARGET_KEY).n_unique() != ledger.height:
        raise RuntimeError("covariate fold ledger has duplicate target keys")
    _validate_prediction_ledger(ledger, config)
    baseline_config = load_spatial_surface_baseline_config()
    if (
        baseline_config.idw_power != config.idw_power
        or baseline_config.minimum_distance_km != config.minimum_distance_km
    ):
        raise RuntimeError("covariate and baseline IDW2 settings differ")

    rows: list[dict[str, object]] = []
    for group in _prediction_groups(ledger):
        first = group.row(0, named=True)
        target_station = str(first["target_station"])
        train_year = int(first["train_year"])
        train_station_lists = {tuple(row) for row in group["train_stations"].to_list()}
        if len(train_station_lists) != 1:
            raise RuntimeError("covariate fold ledger changes train stations within a model fit")
        train_stations = list(next(iter(train_station_lists)))
        if target_station in train_stations:
            raise RuntimeError("covariate model train station membership includes its target")
        train = covariates.filter(
            (pl.col("month").dt.year() == train_year)
            & pl.col("station_name").is_in(train_stations)
            & (pl.col("target_state") == "observed")
        ).sort("station_name", "month")
        if train.filter(pl.col("station_name") == target_station).height:
            raise RuntimeError("covariate model training rows include the target station")
        expected_model_counts = set(group["n_model_train_rows"].to_list())
        if expected_model_counts != {train.height}:
            raise RuntimeError("covariate model training rows differ from the authoritative ledger")
        targets = (
            group.select("month", "target_station")
            .join(
                covariates,
                left_on=["target_station", "month"],
                right_on=["station_name", "month"],
                how="left",
            )
            .rename({"target_station": "station_name"})
            .sort("month")
        )
        if targets.height != group.height or targets["x_m"].null_count():
            raise RuntimeError("covariate prediction targets differ from the authoritative ledger")
        eligible = group.filter(pl.col("fold_state") == "eligible")
        group_failure: CovariatePrediction | None = None
        model_values: np.ndarray | None = None
        if not eligible.is_empty():
            combined = pl.concat([train, targets.select(covariates.columns)], how="vertical")
            try:
                returned = np.asarray(fit_covariate_model(train, combined, config), dtype=float)
            except Exception as error:
                group_failure = _failed_prediction("estimator_failed", type(error).__name__)
            else:
                if returned.ndim != 1 or returned.shape[0] != combined.height:
                    group_failure = _failed_prediction("wrong_prediction_length")
                else:
                    model_values = returned
        target_by_month = {row["month"]: row for row in targets.iter_rows(named=True)}
        group_by_month = {row["month"]: row for row in group.iter_rows(named=True)}
        for month in sorted(group_by_month):
            fold = group_by_month[month]
            target = target_by_month[month]
            fold_state = str(fold["fold_state"])
            outcomes: dict[str, CovariatePrediction] = {}
            if fold_state != "eligible":
                outcomes = {
                    method: _failed_prediction(fold_state) for method in COVARIATE_READINESS_METHODS
                }
            else:
                source_month = date(train_year, month.month, 1)
                source_train = train.filter(pl.col("month") == source_month).select(
                    "station_name", "lon", "lat", pl.col("PM2.5").alias("mean")
                )
                if source_train.height != int(fold["n_same_month_train_rows"]):
                    raise RuntimeError(
                        "same-month comparator rows differ from the authoritative ledger"
                    )
                target_coordinates = {"lon": target["lon"], "lat": target["lat"]}
                outcomes["idw2"] = _idw_prediction(
                    source_train,
                    target_coordinates,
                    baseline_config=baseline_config,
                )
                if group_failure is not None or model_values is None:
                    candidate_failure = group_failure or _failed_prediction("estimator_failed")
                    outcomes["covariate_gbm"] = candidate_failure
                    outcomes["covariate_gbm_idw2"] = candidate_failure
                else:
                    target_index = train.height + targets["month"].to_list().index(month)
                    trend = _finite_covariate_prediction(float(model_values[target_index]))
                    outcomes["covariate_gbm"] = trend
                    if trend.state != "scored" or trend.value is None:
                        outcomes["covariate_gbm_idw2"] = trend
                    elif source_train.height < config.minimum_train_stations:
                        outcomes["covariate_gbm_idw2"] = _failed_prediction(
                            "insufficient_residual_stations"
                        )
                    else:
                        train_with_predictions = train.with_columns(
                            pl.Series("_fitted", model_values[: train.height])
                        ).with_columns((pl.col("PM2.5") - pl.col("_fitted")).alias("_residual"))
                        residual_train = train_with_predictions.filter(
                            pl.col("month") == source_month
                        ).select(
                            "station_name",
                            "lon",
                            "lat",
                            pl.col("_residual").alias("mean"),
                        )
                        if residual_train.filter(~pl.col("mean").is_finite()).height:
                            residual = _failed_prediction("non_finite_prediction")
                        else:
                            residual = _idw_prediction(
                                residual_train,
                                target_coordinates,
                                baseline_config=baseline_config,
                            )
                        if residual.state == "scored" and residual.value is not None:
                            outcomes["covariate_gbm_idw2"] = _finite_covariate_prediction(
                                trend.value + residual.value
                            )
                        else:
                            outcomes["covariate_gbm_idw2"] = residual
            for method in config.methods:
                outcome = outcomes[method]
                observed = fold["observed"]
                rows.append(
                    {
                        **fold,
                        "method": method,
                        "predicted": outcome.value,
                        "prediction_state": outcome.state,
                        "failure_type": outcome.failure_type,
                        "error": (
                            outcome.value - float(observed)
                            if outcome.state == "scored"
                            and outcome.value is not None
                            and observed is not None
                            else None
                        ),
                    }
                )
    return pl.DataFrame(rows, schema=_COVARIATE_PREDICTION_SCHEMA).sort(
        *_COVARIATE_TARGET_KEY, "method"
    )


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
