"""Load the immutable primary inputs for spatial surface evaluation."""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from hashlib import sha256
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import polars as pl
from pykrige.ok import OrdinaryKriging
from sklearn.cluster import KMeans

from twair.config import ConfigError, load_conf
from twair.ingest.station_inventory import station_inventory_generation

__all__ = [
    "FileIdentity",
    "Prediction",
    "SpatialSurfaceBaselineConfig",
    "SurfaceInputs",
    "assign_spatial_clusters",
    "build_fold_ledger",
    "build_station_support",
    "evaluate_baselines",
    "load_spatial_surface_baseline_config",
    "load_surface_inputs",
    "predict_target",
]


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
