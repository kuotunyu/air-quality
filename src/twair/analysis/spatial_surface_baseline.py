"""Load the immutable primary inputs for spatial surface evaluation."""

from __future__ import annotations

import json
import math
import os
import shutil
import stat
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

import geopandas as gpd
import numpy as np
import polars as pl
from pykrige.ok import OrdinaryKriging
from sklearn.cluster import KMeans

from twair.config import ConfigError, load_conf
from twair.ingest.station_inventory import station_inventory_generation
from twair.paths import outputs_dir
from twair.provenance import git_state

__all__ = [
    "SPATIAL_BASELINE_LIMITATIONS",
    "SPATIAL_BASELINE_MEMBER_NAMES",
    "SPATIAL_BASELINE_SCHEMA_VERSION",
    "SPATIAL_BASELINE_TABLE_ORDER",
    "SPATIAL_BASELINE_TABLE_SCHEMAS",
    "FileIdentity",
    "Prediction",
    "SpatialSurfaceBaselineConfig",
    "SpatialSurfaceBaselineResult",
    "SurfaceInputs",
    "assign_spatial_clusters",
    "bootstrap_station_delta",
    "build_fold_ledger",
    "build_station_support",
    "decide_baseline_gate",
    "evaluate_baselines",
    "load_spatial_surface_baseline_config",
    "load_surface_inputs",
    "paired_method_deltas",
    "predict_target",
    "run_spatial_surface_baseline",
    "score_predictions",
    "write_spatial_surface_baseline_result",
]


SPATIAL_BASELINE_SCHEMA_VERSION = 1
SPATIAL_BASELINE_LIMITATIONS = (
    "baseline readiness gate only; no concentration surface was generated",
    "passing permits covariate-model design, not publication of a map",
    "no population-weighted or personal-exposure result",
)
SPATIAL_BASELINE_MEMBER_NAMES = (
    "stations.parquet",
    "panel.parquet",
    "support.parquet",
    "folds.parquet",
    "predictions.parquet",
    "scores.parquet",
    "paired_deltas.parquet",
    "summary.json",
    "manifest.json",
)
_STATION_COLUMNS = ("station_name", "station_type_official", "lon", "lat")
_MONTHLY_COLUMNS = ("station_name", "pollutant", "month", "mean", "meets_threshold")
_SUPPORTED_METHODS = (
    "station_mean",
    "nearest",
    "idw2",
    "kriging_spherical",
    "kriging_hole_effect",
)
_PREDICTION_SCHEMA: dict[str, pl.DataType | type[pl.DataType]] = {
    "evaluation": pl.String,
    "fold_id": pl.String,
    "year": pl.Int64,
    "month": pl.Date,
    "target_station": pl.String,
    "target_cluster": pl.Int64,
    "target_state": pl.String,
    "observed": pl.Float64,
    "train_stations": pl.List(pl.String),
    "n_train": pl.Int64,
    "fold_state": pl.String,
    "fold_reason": pl.String,
    "method": pl.String,
    "predicted": pl.Float64,
    "kriging_sd": pl.Float64,
    "prediction_state": pl.String,
    "failure_type": pl.String,
    "error": pl.Float64,
}
_SCORE_SCHEMA: dict[str, pl.DataType | type[pl.DataType]] = {
    "evaluation": pl.String,
    "year": pl.Int64,
    "method": pl.String,
    "n_intended": pl.Int64,
    "n_scored": pl.Int64,
    "n_failed": pl.Int64,
    "n_stations_intended": pl.Int64,
    "n_stations_scored": pl.Int64,
    "station_clustered_mae": pl.Float64,
    "station_clustered_rmse": pl.Float64,
    "station_clustered_bias": pl.Float64,
    "score_state": pl.String,
}
_PAIRED_DELTA_SCHEMA: dict[str, pl.DataType | type[pl.DataType]] = {
    "evaluation": pl.String,
    "year": pl.Int64,
    "method": pl.String,
    "comparison_method": pl.String,
    "n_stations": pl.Int64,
    "median_station_mae_delta": pl.Float64,
    "lower_2_5": pl.Float64,
    "upper_97_5": pl.Float64,
    "paired_state": pl.String,
}
_TARGET_KEY_COLUMNS = ("evaluation", "fold_id", "year", "month", "target_station")
_STATION_SCHEMA: dict[str, pl.DataType | type[pl.DataType]] = {
    "station_name": pl.String,
    "station_type_official": pl.String,
    "lon": pl.Float64,
    "lat": pl.Float64,
}
_PANEL_SCHEMA: dict[str, pl.DataType | type[pl.DataType]] = {
    **_STATION_SCHEMA,
    "month": pl.Date,
    "pollutant": pl.String,
    "mean": pl.Float64,
    "meets_threshold": pl.Boolean,
    "target_state": pl.String,
}
_SUPPORT_SCHEMA: dict[str, pl.DataType | type[pl.DataType]] = {
    "station_name": pl.String,
    "nearest_station": pl.String,
    "nearest_station_km": pl.Float64,
    "stations_within_20km": pl.Int64,
    "stations_within_40km": pl.Int64,
    "x_m": pl.Float64,
    "y_m": pl.Float64,
}
_FOLD_SCHEMA: dict[str, pl.DataType | type[pl.DataType]] = {
    "evaluation": pl.String,
    "fold_id": pl.String,
    "year": pl.Int64,
    "month": pl.Date,
    "target_station": pl.String,
    "target_cluster": pl.Int64,
    "target_state": pl.String,
    "observed": pl.Float64,
    "train_stations": pl.List(pl.String),
    "n_train": pl.Int64,
    "fold_state": pl.String,
    "fold_reason": pl.String,
}
SPATIAL_BASELINE_TABLE_SCHEMAS = {
    "stations": _STATION_SCHEMA,
    "panel": _PANEL_SCHEMA,
    "support": _SUPPORT_SCHEMA,
    "folds": _FOLD_SCHEMA,
    "predictions": _PREDICTION_SCHEMA,
    "scores": _SCORE_SCHEMA,
    "paired_deltas": _PAIRED_DELTA_SCHEMA,
}
SPATIAL_BASELINE_TABLE_ORDER = {
    "stations": ("station_name",),
    "panel": ("station_name", "month"),
    "support": ("station_name",),
    "folds": ("evaluation", "year", "month", "target_station"),
    "predictions": (*_TARGET_KEY_COLUMNS, "method"),
    "scores": ("evaluation", "year", "method"),
    "paired_deltas": ("evaluation", "year", "method"),
}


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


@dataclass(frozen=True, slots=True)
class Prediction:
    value: float | None
    kriging_sd: float | None
    state: str
    failure_type: str | None


@dataclass(frozen=True, slots=True)
class SpatialSurfaceBaselineResult:
    stations: pl.DataFrame
    panel: pl.DataFrame
    support: pl.DataFrame
    folds: pl.DataFrame
    predictions: pl.DataFrame
    scores: pl.DataFrame
    paired_deltas: pl.DataFrame
    summary: dict[str, Any]
    manifest: dict[str, Any]


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
    if methods != _SUPPORTED_METHODS:
        raise ConfigError(
            "spatial_surface_baseline.validation.methods must be the fixed supported methods"
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
    normalized = frame.select("station_name", "station_type_official").join(
        generation.stations, on="station_name", how="left"
    )
    selected = normalized.filter(
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


def build_station_support(
    stations: pl.DataFrame, config: SpatialSurfaceBaselineConfig
) -> pl.DataFrame:
    """Project station coordinates and measure their local support."""
    ordered = stations.sort("station_name")
    points = gpd.GeoDataFrame(
        ordered.to_pandas(),
        geometry=gpd.points_from_xy(ordered["lon"].to_list(), ordered["lat"].to_list()),
        crs="EPSG:4326",
    ).to_crs(epsg=config.projection_epsg)
    x = points.geometry.x.to_numpy()
    y = points.geometry.y.to_numpy()
    station_names = ordered["station_name"].to_list()
    rows: list[dict[str, object]] = []
    for index, station_name in enumerate(station_names):
        distances_km = np.hypot(x - x[index], y - y[index]) / 1000
        candidates = [
            (float(distance), str(name))
            for candidate_index, (distance, name) in enumerate(
                zip(distances_km, station_names, strict=True)
            )
            if candidate_index != index
        ]
        nearest_distance, nearest_station = min(candidates, key=lambda item: (item[0], item[1]))
        rows.append(
            {
                "station_name": station_name,
                "nearest_station": nearest_station,
                "nearest_station_km": nearest_distance,
                "stations_within_20km": sum(distance <= 20 for distance, _ in candidates),
                "stations_within_40km": sum(distance <= 40 for distance, _ in candidates),
                "x_m": float(x[index]),
                "y_m": float(y[index]),
            }
        )
    return pl.DataFrame(rows).sort("station_name")


def assign_spatial_clusters(
    stations: pl.DataFrame, config: SpatialSurfaceBaselineConfig
) -> pl.DataFrame:
    """Assign deterministic projected-coordinate clusters to every station."""
    support = build_station_support(stations, config)
    coordinates = support.select("x_m", "y_m").to_numpy()
    standardized = (coordinates - coordinates.mean(axis=0)) / coordinates.std(axis=0)
    labels = KMeans(
        n_clusters=config.spatial_folds,
        random_state=config.seed,
        n_init=20,
    ).fit_predict(standardized)
    cluster_centroids = {
        int(label): (
            float(coordinates[labels == label, 0].mean()),
            float(coordinates[labels == label, 1].mean()),
        )
        for label in set(labels)
    }
    canonical_labels = {
        label: cluster_index
        for cluster_index, (label, _) in enumerate(
            sorted(cluster_centroids.items(), key=lambda item: item[1])
        )
    }
    return support.with_columns(
        pl.Series("spatial_cluster", [canonical_labels[int(label)] for label in labels])
    ).sort("station_name")


def build_fold_ledger(inputs: SurfaceInputs, config: SpatialSurfaceBaselineConfig) -> pl.DataFrame:
    """Materialize target-held-out training sets for each evaluation family."""
    clusters = assign_spatial_clusters(inputs.stations, config)
    cluster_by_station = dict(
        zip(
            clusters["station_name"].to_list(),
            clusters["spatial_cluster"].to_list(),
            strict=True,
        )
    )
    coordinates_by_station = dict(
        zip(
            clusters["station_name"].to_list(),
            zip(clusters["x_m"].to_list(), clusters["y_m"].to_list(), strict=True),
            strict=True,
        )
    )
    observed_by_month: dict[date, list[str]] = {}
    for station_name, month in (
        inputs.panel.filter(pl.col("target_state") == "observed")
        .select("station_name", "month")
        .iter_rows()
    ):
        observed_by_month.setdefault(month, []).append(station_name)
    rows: list[dict[str, object]] = []
    for station_name, month, target_state, observed in (
        inputs.panel.select("station_name", "month", "target_state", "mean")
        .sort("month", "station_name")
        .iter_rows()
    ):
        target_cluster = cluster_by_station[station_name]
        observed_stations = observed_by_month.get(month, [])
        target_x, target_y = coordinates_by_station[station_name]
        evaluations: tuple[tuple[str, str, list[str]], ...] = (
            (
                "buffer_20km",
                f"buffer_20km:{station_name}",
                [
                    candidate
                    for candidate in observed_stations
                    if candidate != station_name
                    and np.hypot(
                        coordinates_by_station[candidate][0] - target_x,
                        coordinates_by_station[candidate][1] - target_y,
                    )
                    / 1000
                    > 20
                ],
            ),
            (
                "buffer_40km",
                f"buffer_40km:{station_name}",
                [
                    candidate
                    for candidate in observed_stations
                    if candidate != station_name
                    and np.hypot(
                        coordinates_by_station[candidate][0] - target_x,
                        coordinates_by_station[candidate][1] - target_y,
                    )
                    / 1000
                    > 40
                ],
            ),
            (
                "spatial_cluster",
                f"spatial_cluster:{target_cluster}",
                [
                    candidate
                    for candidate in observed_stations
                    if cluster_by_station[candidate] != target_cluster
                ],
            ),
        )
        for evaluation, fold_id, train_stations in evaluations:
            ordered_train_stations = sorted(train_stations)
            n_train = len(ordered_train_stations)
            if target_state != "observed":
                fold_state = "unscored_target_withheld"
                fold_reason: str | None = f"target_state={target_state}"
            elif n_train < config.min_train_stations:
                fold_state = "unscored_insufficient_train"
                fold_reason = (
                    f"n_train={n_train} is below min_train_stations={config.min_train_stations}"
                )
            else:
                fold_state = "eligible"
                fold_reason = None
            rows.append(
                {
                    "evaluation": evaluation,
                    "fold_id": fold_id,
                    "year": month.year,
                    "month": month,
                    "target_station": station_name,
                    "target_cluster": target_cluster,
                    "target_state": target_state,
                    "observed": observed,
                    "train_stations": ordered_train_stations,
                    "n_train": n_train,
                    "fold_state": fold_state,
                    "fold_reason": fold_reason,
                }
            )
    return pl.DataFrame(rows).sort("evaluation", "year", "month", "target_station")


def _projected_distances_km(
    train: pl.DataFrame,
    target: dict[str, Any],
    config: SpatialSurfaceBaselineConfig,
) -> np.ndarray:
    target_lat = float(target["lat"])
    target_lon = float(target["lon"])
    points = gpd.GeoDataFrame(
        {
            "lon": [*train["lon"].to_list(), target_lon],
            "lat": [*train["lat"].to_list(), target_lat],
        },
        geometry=gpd.points_from_xy(
            [*train["lon"].to_list(), target_lon],
            [*train["lat"].to_list(), target_lat],
        ),
        crs="EPSG:4326",
    ).to_crs(epsg=config.projection_epsg)
    x = points.geometry.x.to_numpy()
    y = points.geometry.y.to_numpy()
    return np.asarray(np.hypot(x[:-1] - x[-1], y[:-1] - y[-1]) / 1000, dtype=float)


def _finite_prediction(value: float) -> Prediction:
    if math.isfinite(value):
        return Prediction(value=value, kriging_sd=None, state="scored", failure_type=None)
    return Prediction(value=None, kriging_sd=None, state="non_finite_prediction", failure_type=None)


def predict_target(
    train: pl.DataFrame,
    target: dict[str, Any],
    method: str,
    config: SpatialSurfaceBaselineConfig,
) -> Prediction:
    if method not in _SUPPORTED_METHODS or method not in config.methods:
        raise ValueError(f"spatial surface method is not supported: {method}")
    distances_km = _projected_distances_km(train, target, config)
    if np.any(distances_km == 0.0):
        return Prediction(
            value=None,
            kriging_sd=None,
            state="duplicate_coordinate",
            failure_type=None,
        )
    values = np.asarray(train["mean"].to_numpy(), dtype=float)
    if method == "station_mean":
        return _finite_prediction(float(values.mean()))
    if method == "nearest":
        return _finite_prediction(float(values[np.argmin(distances_km)]))
    if method == "idw2":
        weights = 1.0 / np.maximum(distances_km, config.minimum_distance_km) ** config.idw_power
        return _finite_prediction(float(weights @ values / weights.sum()))
    if method == "kriging_spherical":
        variogram_model = "spherical"
    elif method == "kriging_hole_effect":
        variogram_model = "hole-effect"
    else:
        raise ValueError(f"spatial surface method is not supported: {method}")
    try:
        model = OrdinaryKriging(
            np.asarray(train["lon"].to_numpy(), dtype=float),
            np.asarray(train["lat"].to_numpy(), dtype=float),
            values,
            variogram_model=variogram_model,
            nlags=8,
            coordinates_type="geographic",
            enable_plotting=False,
        )
        predicted, variance = model.execute(
            "points",
            np.array([float(target["lon"])]),
            np.array([float(target["lat"])]),
        )
        value = float(np.asarray(predicted).ravel()[0])
        variance_value = float(np.asarray(variance).ravel()[0])
        if not math.isfinite(value) or not math.isfinite(variance_value):
            return Prediction(
                value=None,
                kriging_sd=None,
                state="non_finite_prediction",
                failure_type=None,
            )
        return Prediction(
            value=value,
            kriging_sd=float(math.sqrt(max(variance_value, 0.0))),
            state="scored",
            failure_type=None,
        )
    except Exception as error:
        return Prediction(
            value=None,
            kriging_sd=None,
            state="estimator_failed",
            failure_type=type(error).__name__,
        )


def evaluate_baselines(
    inputs: SurfaceInputs,
    folds: pl.DataFrame,
    config: SpatialSurfaceBaselineConfig,
) -> pl.DataFrame:
    rows: list[dict[str, object]] = []
    for fold in folds.iter_rows(named=True):
        target_station = str(fold["target_station"])
        fold_state = str(fold["fold_state"])
        if fold_state == "eligible":
            train_stations = [str(station) for station in fold["train_stations"]]
            train = (
                inputs.panel.filter(
                    (pl.col("month") == fold["month"])
                    & pl.col("station_name").is_in(train_stations)
                )
                .join(inputs.stations.select("station_name", "lat", "lon"), on="station_name")
                .sort("station_name")
            )
            if train.height != int(fold["n_train"]):
                raise RuntimeError("spatial surface fold training count does not match its ledger")
            if target_station in set(train["station_name"].to_list()):
                raise RuntimeError("spatial surface fold training set includes its target")
            if not train.filter(pl.col("target_state") != "observed").is_empty():
                raise RuntimeError("spatial surface fold training rows must be observed")
            coordinates = inputs.stations.filter(pl.col("station_name") == target_station)
            if coordinates.height != 1:
                raise RuntimeError("spatial surface fold target station is not uniquely available")
            target = {
                "lat": coordinates.item(0, "lat"),
                "lon": coordinates.item(0, "lon"),
            }
        for method in config.methods:
            if fold_state == "eligible":
                outcome = predict_target(train, target, method, config)
            else:
                outcome = Prediction(
                    value=None,
                    kriging_sd=None,
                    state=fold_state,
                    failure_type=None,
                )
            observed = fold["observed"]
            rows.append(
                {
                    **fold,
                    "method": method,
                    "predicted": outcome.value,
                    "kriging_sd": outcome.kriging_sd,
                    "prediction_state": outcome.state,
                    "failure_type": outcome.failure_type,
                    "error": (
                        outcome.value - float(observed)
                        if outcome.state == "scored" and outcome.value is not None
                        else None
                    ),
                }
            )
    return pl.DataFrame(rows, schema=_PREDICTION_SCHEMA)


def _prediction_key(row: dict[str, Any]) -> tuple[object, ...]:
    return tuple(row[column] for column in _TARGET_KEY_COLUMNS)


def _require_prediction_columns(predictions: pl.DataFrame) -> None:
    _require_columns(
        predictions,
        (*_TARGET_KEY_COLUMNS, "fold_state", "method", "prediction_state", "error"),
        label="spatial surface predictions",
    )


def _finite_error(row: dict[str, Any]) -> bool:
    value = row["error"]
    return row["prediction_state"] == "scored" and value is not None and math.isfinite(float(value))


def score_predictions(
    predictions: pl.DataFrame, config: SpatialSurfaceBaselineConfig
) -> pl.DataFrame:
    """Summarize eligible predictions with equal weight for every target station."""
    _require_prediction_columns(predictions)
    rows = list(predictions.iter_rows(named=True))
    for method in config.methods:
        method_keys = [_prediction_key(row) for row in rows if row["method"] == method]
        if len(method_keys) != len(set(method_keys)):
            raise RuntimeError("spatial surface predictions contain duplicate method target keys")
    cells = sorted({(str(row["evaluation"]), int(row["year"])) for row in rows})
    score_rows: list[dict[str, object]] = []
    for evaluation, year in cells:
        cell_rows = [
            row for row in rows if row["evaluation"] == evaluation and int(row["year"]) == year
        ]
        expected_keys = {
            _prediction_key(row) for row in cell_rows if row["fold_state"] == "eligible"
        }
        for method in config.methods:
            method_rows = [row for row in cell_rows if row["method"] == method]
            intended_rows = [row for row in method_rows if row["fold_state"] == "eligible"]
            intended_keys = {_prediction_key(row) for row in intended_rows}
            scored_rows = [row for row in intended_rows if _finite_error(row)]
            n_intended = len(expected_keys)
            n_scored = len(scored_rows)
            n_failed = n_intended - n_scored
            n_stations_intended = len(
                {
                    str(row["target_station"])
                    for row in cell_rows
                    if _prediction_key(row) in expected_keys
                }
            )
            n_stations_scored = len({str(row["target_station"]) for row in scored_rows})
            if not expected_keys:
                score_state = "no_eligible_targets"
            elif intended_keys != expected_keys:
                score_state = "missing_intended_predictions"
            elif n_failed:
                score_state = "incomplete_predictions"
            else:
                score_state = "complete"
            mae: float | None = None
            rmse: float | None = None
            bias: float | None = None
            if score_state != "missing_intended_predictions" and scored_rows:
                per_station: list[tuple[float, float, float]] = []
                for station in sorted({str(row["target_station"]) for row in scored_rows}):
                    errors = np.asarray(
                        [
                            float(row["error"])
                            for row in scored_rows
                            if row["target_station"] == station
                        ],
                        dtype=float,
                    )
                    per_station.append(
                        (
                            float(np.abs(errors).mean()),
                            float(np.sqrt(np.square(errors).mean())),
                            float(errors.mean()),
                        )
                    )
                mae = float(np.mean([summary[0] for summary in per_station]))
                rmse = float(np.mean([summary[1] for summary in per_station]))
                bias = float(np.mean([summary[2] for summary in per_station]))
            score_rows.append(
                {
                    "evaluation": evaluation,
                    "year": year,
                    "method": method,
                    "n_intended": n_intended,
                    "n_scored": n_scored,
                    "n_failed": n_failed,
                    "n_stations_intended": n_stations_intended,
                    "n_stations_scored": n_stations_scored,
                    "station_clustered_mae": mae,
                    "station_clustered_rmse": rmse,
                    "station_clustered_bias": bias,
                    "score_state": score_state,
                }
            )
    return pl.DataFrame(score_rows, schema=_SCORE_SCHEMA).sort("evaluation", "year", "method")


def bootstrap_station_delta(
    station_deltas: np.ndarray, *, draws: int, seed: int
) -> tuple[float, float, float]:
    """Bootstrap the station-weighted median error difference deterministically."""
    values = np.asarray(station_deltas, dtype=float)
    if values.ndim != 1 or values.size == 0 or not np.isfinite(values).all():
        raise ValueError("station deltas must be a non-empty finite one-dimensional array")
    if draws <= 0:
        raise ValueError("bootstrap draws must be positive")
    generator = np.random.default_rng(seed)
    samples = values[generator.integers(0, values.size, size=(draws, values.size))]
    quantiles = np.percentile(np.median(samples, axis=1), [2.5, 97.5])
    return float(np.median(values)), float(quantiles[0]), float(quantiles[1])


def paired_method_deltas(
    predictions: pl.DataFrame, config: SpatialSurfaceBaselineConfig
) -> pl.DataFrame:
    """Compare each method with the configured baseline on identical target keys."""
    _require_prediction_columns(predictions)
    rows = list(predictions.iter_rows(named=True))
    by_method = {
        method: [row for row in rows if row["method"] == method] for method in config.methods
    }
    baseline_rows = by_method[config.comparison_method]
    baseline_keys = [_prediction_key(row) for row in baseline_rows]
    if len(baseline_keys) != len(set(baseline_keys)):
        raise RuntimeError("spatial surface paired predictions contain duplicate target keys")
    baseline_by_key = {_prediction_key(row): row for row in baseline_rows}
    paired_rows: list[dict[str, object]] = []
    for method in config.methods:
        if method == config.comparison_method:
            continue
        candidate_rows = by_method[method]
        candidate_keys = [_prediction_key(row) for row in candidate_rows]
        if len(candidate_keys) != len(set(candidate_keys)):
            raise RuntimeError("spatial surface paired predictions contain duplicate target keys")
        candidate_by_key = {_prediction_key(row): row for row in candidate_rows}
        if set(candidate_by_key) != set(baseline_by_key):
            raise RuntimeError("spatial surface paired predictions have unequal target keys")
        for key in baseline_by_key:
            if candidate_by_key[key]["fold_state"] != baseline_by_key[key]["fold_state"]:
                raise RuntimeError("spatial surface paired predictions have mismatched eligibility")
        cells = sorted({(str(row["evaluation"]), int(row["year"])) for row in baseline_rows})
        for evaluation, year in cells:
            keys = [
                key
                for key, baseline in baseline_by_key.items()
                if baseline["evaluation"] == evaluation
                and int(baseline["year"]) == year
                and baseline["fold_state"] == "eligible"
            ]
            if not keys:
                paired_state = "no_eligible_targets"
                n_stations = 0
                median = lower = upper = None
            elif not all(
                _finite_error(baseline_by_key[key]) and _finite_error(candidate_by_key[key])
                for key in keys
            ):
                paired_state = "incomplete_predictions"
                n_stations = 0
                median = lower = upper = None
            else:
                station_deltas: list[float] = []
                for station in sorted(
                    {str(baseline_by_key[key]["target_station"]) for key in keys}
                ):
                    station_keys = [
                        key for key in keys if baseline_by_key[key]["target_station"] == station
                    ]
                    candidate_mae = float(
                        np.mean(
                            [abs(float(candidate_by_key[key]["error"])) for key in station_keys]
                        )
                    )
                    baseline_mae = float(
                        np.mean([abs(float(baseline_by_key[key]["error"])) for key in station_keys])
                    )
                    station_deltas.append(candidate_mae - baseline_mae)
                median, lower, upper = bootstrap_station_delta(
                    np.asarray(station_deltas), draws=config.bootstrap_draws, seed=config.seed
                )
                paired_state = "complete"
                n_stations = len(station_deltas)
            paired_rows.append(
                {
                    "evaluation": evaluation,
                    "year": year,
                    "method": method,
                    "comparison_method": config.comparison_method,
                    "n_stations": n_stations,
                    "median_station_mae_delta": median,
                    "lower_2_5": lower,
                    "upper_97_5": upper,
                    "paired_state": paired_state,
                }
            )
    return pl.DataFrame(paired_rows, schema=_PAIRED_DELTA_SCHEMA).sort(
        "evaluation", "year", "method"
    )


def _single_gate_row(
    frame: pl.DataFrame, *, evaluation: str, year: int, method: str
) -> dict[str, Any] | None:
    matches = frame.filter(
        (pl.col("evaluation") == evaluation)
        & (pl.col("year") == year)
        & (pl.col("method") == method)
    )
    if matches.height != 1:
        return None
    return matches.row(0, named=True)


def _complete_score_row(row: dict[str, Any] | None) -> bool:
    if row is None:
        return False
    try:
        counts_are_complete = (
            int(row["n_intended"]) > 0
            and int(row["n_intended"]) == int(row["n_scored"])
            and int(row["n_failed"]) == 0
            and int(row["n_stations_intended"]) > 0
            and int(row["n_stations_intended"]) == int(row["n_stations_scored"])
        )
        metrics_are_finite = all(
            value is not None and math.isfinite(float(value))
            for value in (
                row["station_clustered_mae"],
                row["station_clustered_rmse"],
                row["station_clustered_bias"],
            )
        )
    except (KeyError, TypeError, ValueError):
        return False
    return row["score_state"] == "complete" and counts_are_complete and metrics_are_finite


def decide_baseline_gate(
    scores: pl.DataFrame, deltas: pl.DataFrame, config: SpatialSurfaceBaselineConfig
) -> dict[str, Any]:
    """Apply the preregistered complete-prediction baseline readiness rule."""
    _require_columns(scores, _SCORE_SCHEMA, label="spatial surface scores")
    _require_columns(deltas, _PAIRED_DELTA_SCHEMA, label="spatial surface paired deltas")
    qualifying: list[str] = []
    primary_evaluations = ("buffer_20km", "buffer_40km")
    for method in config.methods:
        if method == config.comparison_method:
            continue
        primary_complete = True
        for evaluation in primary_evaluations:
            for year in config.required_years:
                score = _single_gate_row(scores, evaluation=evaluation, year=year, method=method)
                baseline_score = _single_gate_row(
                    scores,
                    evaluation=evaluation,
                    year=year,
                    method=config.comparison_method,
                )
                delta = _single_gate_row(deltas, evaluation=evaluation, year=year, method=method)
                if (
                    score is None
                    or baseline_score is None
                    or delta is None
                    or not _complete_score_row(score)
                    or not _complete_score_row(baseline_score)
                ):
                    primary_complete = False
                    continue
                if (
                    int(score["n_intended"]) != int(baseline_score["n_intended"])
                    or int(score["n_stations_intended"])
                    != int(baseline_score["n_stations_intended"])
                    or delta["comparison_method"] != config.comparison_method
                    or int(delta["n_stations"]) != int(score["n_stations_intended"])
                    or delta["paired_state"] != "complete"
                    or delta["median_station_mae_delta"] is None
                    or not math.isfinite(float(delta["median_station_mae_delta"]))
                    or float(delta["median_station_mae_delta"]) >= 0
                ):
                    primary_complete = False
        cluster_complete = True
        for year in config.required_years:
            cluster_score = _single_gate_row(
                scores, evaluation="spatial_cluster", year=year, method=method
            )
            if not _complete_score_row(cluster_score):
                cluster_complete = False
        if primary_complete and cluster_complete:
            qualifying.append(method)
    return {
        "state": "go" if qualifying else "stop",
        "qualifying_methods": sorted(qualifying),
        "required_cells": 4,
        "rule": "complete predictions and median station MAE delta < 0 in 2024/2025 at 20/40 km",
        "limitations": list(SPATIAL_BASELINE_LIMITATIONS),
    }


def _normalized_config(config: SpatialSurfaceBaselineConfig) -> dict[str, object]:
    return {
        "schema_version": SPATIAL_BASELINE_SCHEMA_VERSION,
        "analysis": {
            "years": list(config.years),
            "pollutant": config.pollutant,
            "station_types": list(config.station_types),
            "excluded_stations": list(config.excluded_stations),
            "min_train_stations": config.min_train_stations,
        },
        "validation": {
            "buffer_radii_km": list(config.buffer_radii_km),
            "spatial_folds": config.spatial_folds,
            "projection_epsg": config.projection_epsg,
            "methods": list(config.methods),
            "idw_power": config.idw_power,
            "minimum_distance_km": config.minimum_distance_km,
            "bootstrap_draws": config.bootstrap_draws,
            "seed": config.seed,
        },
        "gate": {
            "comparison_method": config.comparison_method,
            "required_evaluations": list(config.required_evaluations),
            "required_years": list(config.required_years),
        },
    }


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _canonical_hash(value: object) -> str:
    return sha256(_canonical_json_bytes(value)).hexdigest()


def _normalized_table(name: str, frame: pl.DataFrame) -> pl.DataFrame:
    schema = SPATIAL_BASELINE_TABLE_SCHEMAS[name]
    if set(frame.columns) != set(schema):
        raise RuntimeError(f"spatial surface {name} columns changed")
    return (
        frame.select(*schema)
        .cast(pl.Schema(schema), strict=True)
        .sort(*SPATIAL_BASELINE_TABLE_ORDER[name])
    )


def _table_identity(name: str, frame: pl.DataFrame) -> dict[str, object]:
    normalized = _normalized_table(name, frame)
    payload = _canonical_json_bytes(
        {
            "schema": [[column, str(dtype)] for column, dtype in normalized.schema.items()],
            "order": list(SPATIAL_BASELINE_TABLE_ORDER[name]),
            "rows": [[_table_scalar(value) for value in row] for row in normalized.iter_rows()],
        }
    )
    return {
        "rows": normalized.height,
        "bytes": len(payload),
        "sha256": sha256(payload).hexdigest(),
        "schema": {column: str(dtype) for column, dtype in normalized.schema.items()},
        "order": list(SPATIAL_BASELINE_TABLE_ORDER[name]),
    }


def _table_scalar(value: object) -> object:
    if isinstance(value, date):
        return {"date": value.isoformat()}
    if isinstance(value, float) and not math.isfinite(value):
        if math.isnan(value):
            return {"float": "nan"}
        return {"float": "positive_infinity" if value > 0 else "negative_infinity"}
    if isinstance(value, (list, tuple)):
        return [_table_scalar(item) for item in value]
    return value


def _result_frames(result: SpatialSurfaceBaselineResult) -> dict[str, pl.DataFrame]:
    return {
        name: _normalized_table(name, getattr(result, name))
        for name in SPATIAL_BASELINE_TABLE_SCHEMAS
    }


def _manifest_identity(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in manifest.items()
        if key not in {"generated_at", "complete", "generation_sha256"}
    }


def _input_manifest(
    inputs: SurfaceInputs,
    *,
    data_root: Path,
) -> list[dict[str, object]]:
    root = data_root.absolute()
    identities: list[dict[str, object]] = []
    for identity in inputs.input_files:
        path = identity.path.absolute()
        try:
            relative = path.relative_to(root)
        except ValueError as exc:
            raise RuntimeError("spatial surface input is outside data root") from exc
        identities.append(
            {
                "path": relative.as_posix(),
                "bytes": identity.bytes,
                "sha256": identity.sha256,
            }
        )
    return sorted(identities, key=lambda value: str(value["path"]))


def run_spatial_surface_baseline(
    *,
    data_root: Path,
    config: SpatialSurfaceBaselineConfig | None = None,
    generated_at: str | None = None,
) -> SpatialSurfaceBaselineResult:
    reviewed = config if config is not None else load_spatial_surface_baseline_config()
    inputs = load_surface_inputs(data_root, reviewed)
    support = build_station_support(inputs.stations, reviewed)
    folds = build_fold_ledger(inputs, reviewed)
    predictions = evaluate_baselines(inputs, folds, reviewed)
    scores = score_predictions(predictions, reviewed)
    paired_deltas = paired_method_deltas(predictions, reviewed)
    gate = decide_baseline_gate(scores, paired_deltas, reviewed)
    frames = {
        "stations": inputs.stations,
        "panel": inputs.panel,
        "support": support,
        "folds": folds,
        "predictions": predictions,
        "scores": scores,
        "paired_deltas": paired_deltas,
    }
    normalized = {name: _normalized_table(name, frame) for name, frame in frames.items()}
    tables = {name: _table_identity(name, frame) for name, frame in normalized.items()}
    claim_boundary = {
        "feeds_web": False,
        "limitations": list(SPATIAL_BASELINE_LIMITATIONS),
    }
    summary: dict[str, Any] = {
        "analysis": "spatial_surface_baseline",
        "inventory_generation_sha256": inputs.inventory_generation_sha256,
        "output_rows": {name: frame.height for name, frame in normalized.items()},
        "gate": gate,
        **claim_boundary,
    }
    git_sha, git_dirty = git_state()
    identity: dict[str, Any] = {
        "schema_version": SPATIAL_BASELINE_SCHEMA_VERSION,
        "analysis": "spatial_surface_baseline",
        "config": _normalized_config(reviewed),
        "inputs": _input_manifest(inputs, data_root=data_root),
        "inventory_generation_sha256": inputs.inventory_generation_sha256,
        "tables": tables,
        "gate": gate,
        "claim_boundary": claim_boundary,
        "git_sha": git_sha,
        "git_dirty": git_dirty,
    }
    manifest = {
        **identity,
        "generated_at": generated_at
        if generated_at is not None
        else datetime.now(UTC).isoformat(timespec="seconds"),
        "complete": False,
        "generation_sha256": _canonical_hash(identity),
    }
    return SpatialSurfaceBaselineResult(
        stations=normalized["stations"],
        panel=normalized["panel"],
        support=normalized["support"],
        folds=normalized["folds"],
        predictions=normalized["predictions"],
        scores=normalized["scores"],
        paired_deltas=normalized["paired_deltas"],
        summary=summary,
        manifest=manifest,
    )


def _path_exists(path: Path) -> bool:
    return os.path.lexists(path)


def _is_link_or_reparse(path: Path) -> bool:
    try:
        attributes = int(getattr(path.lstat(), "st_file_attributes", 0))
    except OSError:
        return True
    is_junction = getattr(path, "is_junction", None)
    return (
        path.is_symlink()
        or (is_junction is not None and is_junction())
        or bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    )


def _validated_directory(path: Path, *, label: str) -> Path:
    absolute = path.absolute()
    try:
        resolved = absolute.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError(f"{label} is unreadable") from exc
    if _is_link_or_reparse(absolute) or not absolute.is_dir() or resolved != absolute:
        raise RuntimeError(f"{label} is linked, reparse-point, or outside")
    return absolute


def _ensure_directory(path: Path, *, label: str) -> Path:
    if _path_exists(path):
        return _validated_directory(path, label=label)
    path.mkdir()
    return _validated_directory(path, label=label)


def _validate_generation_inventory(directory: Path, *, label: str) -> tuple[Path, ...]:
    validated = _validated_directory(directory, label=label)
    try:
        entries = tuple(validated.iterdir())
    except OSError as exc:
        raise RuntimeError(f"{label} is unreadable") from exc
    if {entry.name for entry in entries} != set(SPATIAL_BASELINE_MEMBER_NAMES):
        raise RuntimeError(f"{label} has an unexpected member inventory")
    for entry in entries:
        try:
            resolved = entry.resolve(strict=True)
            links = entry.stat().st_nlink
        except OSError as exc:
            raise RuntimeError(f"{label} member is unreadable: {entry.name}") from exc
        if (
            _is_link_or_reparse(entry)
            or not entry.is_file()
            or resolved.parent != validated
            or links != 1
        ):
            raise RuntimeError(f"{label} member is linked or reparse-point: {entry.name}")
    return entries


def _observed_file_identity(path: Path) -> dict[str, object]:
    payload = path.read_bytes()
    return {"bytes": len(payload), "sha256": sha256(payload).hexdigest()}


def _write_canonical_json(path: Path, value: object) -> None:
    path.write_bytes(_canonical_json_bytes(value))


def _written_paths(directory: Path) -> dict[str, Path]:
    return {
        **{name: directory / f"{name}.parquet" for name in SPATIAL_BASELINE_TABLE_SCHEMAS},
        "summary": directory / "summary.json",
        "manifest": directory / "manifest.json",
    }


def _validate_existing_generation(
    destination: Path,
    *,
    staged: Path,
    expected_identity: dict[str, Any],
) -> dict[str, Path]:
    try:
        _validate_generation_inventory(destination, label="existing generation")
        manifest_path = destination / "manifest.json"
        payload = manifest_path.read_bytes()
        manifest = json.loads(payload.decode("utf-8"))
        if not isinstance(manifest, dict) or payload != _canonical_json_bytes(manifest):
            raise RuntimeError("manifest is not canonical")
        if (
            manifest.get("complete") is not True
            or manifest.get("generation_sha256") != destination.name
            or _manifest_identity(manifest) != expected_identity
            or _canonical_hash(expected_identity) != destination.name
        ):
            raise RuntimeError("manifest identity differs")
        members = manifest.get("members")
        if not isinstance(members, dict) or set(members) != set(SPATIAL_BASELINE_MEMBER_NAMES[:-1]):
            raise RuntimeError("manifest member identities differ")
        for name in SPATIAL_BASELINE_MEMBER_NAMES[:-1]:
            existing = destination / name
            if _observed_file_identity(existing) != members[name]:
                raise RuntimeError(f"member checksum differs: {name}")
            if existing.read_bytes() != (staged / name).read_bytes():
                raise RuntimeError(f"member bytes differ: {name}")
    except (KeyError, OSError, UnicodeError, json.JSONDecodeError, RuntimeError) as exc:
        raise RuntimeError(f"existing generation is not reusable: {exc}") from exc
    return _written_paths(destination)


def _remove_invocation_staging(staged: Path, *, generations: Path, token: str) -> None:
    if not _path_exists(staged):
        return
    if (
        staged.name != f".spatial-surface-baseline.staging-{token}"
        or staged.absolute().parent != generations
    ):
        return
    try:
        validated = _validated_directory(staged, label="spatial surface staging directory")
    except RuntimeError:
        return
    if validated.parent == generations:
        shutil.rmtree(validated)


def write_spatial_surface_baseline_result(
    result: SpatialSurfaceBaselineResult,
    *,
    output_root: Path | None = None,
) -> dict[str, Path]:
    frames = _result_frames(result)
    expected_tables = {name: _table_identity(name, frame) for name, frame in frames.items()}
    provisional_identity = _manifest_identity(result.manifest)
    if (
        result.manifest.get("complete") is not False
        or result.manifest.get("generation_sha256") != _canonical_hash(provisional_identity)
        or provisional_identity.get("tables") != expected_tables
    ):
        raise RuntimeError("spatial surface prepared result identity changed")
    selected_root = (
        output_root.absolute()
        if output_root is not None
        else outputs_dir("spatial_surface_baseline").absolute()
    )
    if not _path_exists(selected_root):
        selected_root.mkdir(parents=True)
    root = _validated_directory(selected_root, label="spatial surface output root")
    generations = _ensure_directory(
        root / "generations", label="spatial surface generations directory"
    )
    token = uuid4().hex
    staged = generations / f".spatial-surface-baseline.staging-{token}"
    staged.mkdir()
    _validated_directory(staged, label="spatial surface staging directory")
    try:
        for name, frame in frames.items():
            frame.write_parquet(staged / f"{name}.parquet")
        _write_canonical_json(staged / "summary.json", result.summary)
        members = {
            name: _observed_file_identity(staged / name)
            for name in SPATIAL_BASELINE_MEMBER_NAMES[:-1]
        }
        final_identity = {**provisional_identity, "members": members}
        generation = _canonical_hash(final_identity)
        destination = generations / generation
        manifest = {
            **final_identity,
            "generated_at": result.manifest["generated_at"],
            "complete": True,
            "generation_sha256": generation,
        }
        _write_canonical_json(staged / "manifest.json", manifest)
        _validate_generation_inventory(staged, label="spatial surface staging generation")
        if _path_exists(destination):
            return _validate_existing_generation(
                destination,
                staged=staged,
                expected_identity=final_identity,
            )
        try:
            staged.replace(destination)
        except FileExistsError:
            return _validate_existing_generation(
                destination,
                staged=staged,
                expected_identity=final_identity,
            )
        _validate_generation_inventory(destination, label="spatial surface final generation")
        return _written_paths(destination)
    finally:
        _remove_invocation_staging(staged, generations=generations, token=token)
