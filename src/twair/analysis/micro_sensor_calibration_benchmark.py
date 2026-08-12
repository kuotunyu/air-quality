"""Benchmark grouped January prediction without claiming validated calibration."""

from __future__ import annotations

import json
import math
import re
import shutil
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

import numpy as np
import polars as pl

from twair.analysis import micro_sensor_calibration as readiness
from twair.analysis.era5_robustness import assign_station_folds
from twair.analysis.era5_value import InputFile, ModelConfig
from twair.config import ConfigError, load_conf
from twair.ingest.station_meta import resolve_station_geo
from twair.models.evaluate import evaluate_predictions
from twair.net import sha256_file
from twair.paths import data_root as configured_data_root
from twair.paths import outputs_dir
from twair.provenance import git_state
from twair.scalars import as_float

__all__ = [
    "BENCHMARK_FEATURE_SETS",
    "CLAIM_BOUNDARY",
    "MicroSensorBenchmarkConfig",
    "MicroSensorBenchmarkEvaluation",
    "MicroSensorBenchmarkResult",
    "PreparedMicroSensorBenchmark",
    "ReadinessRows",
    "evaluate_micro_sensor_benchmark",
    "load_micro_sensor_benchmark_config",
    "load_micro_sensor_readiness_rows",
    "micro_sensor_benchmark_deltas",
    "prepare_micro_sensor_benchmark",
    "run_micro_sensor_calibration_benchmark",
    "score_micro_sensor_predictions",
    "write_micro_sensor_calibration_benchmark_result",
]


_SHA256 = re.compile(r"[0-9a-f]{64}")
_MEMBERS = tuple(readiness._RESULT_SCHEMAS)
_EXPECTED_FILES = {
    "manifest.json",
    "summary.json",
    *(f"{name}.parquet" for name in _MEMBERS),
}
_CONFIG_KEYS = {
    "readiness_generation_sha256",
    "date_folds",
    "station_folds",
    "feature_sets",
    "model",
}
_MODEL_KEYS = {
    "n_estimators",
    "learning_rate",
    "num_leaves",
    "min_child_samples",
    "n_jobs",
    "seed",
}
_ELIGIBLE_VALUE_COLUMNS = (
    "pm25_mean",
    "humidity_mean",
    "temperature_mean",
    "ground_pm25",
    "distance_km",
)

BENCHMARK_FEATURE_SETS: dict[str, tuple[str, ...]] = {
    "raw_micro": (),
    "micro_only": ("pm25_mean",),
    "micro_weather": ("pm25_mean", "humidity_mean", "temperature_mean"),
}
CLAIM_BOUNDARY: dict[str, object] = {
    "predictive_benchmark_only": True,
    "validated_calibration": False,
    "sensor_fusion": False,
    "causal_analysis": False,
    "satellite_feature_used": False,
    "seasonal_validation": False,
    "high_resolution_field": False,
    "values_imputed": False,
    "panel": "2025-01-01 through 2025-01-25 only",
}

_FOLD_MEMBERSHIP_SCHEMA: dict[str, Any] = {
    "fold_kind": pl.String,
    "group": pl.String,
    "fold": pl.String,
    "fold_index": pl.Int64,
    "airzone_official": pl.String,
}
_FOLDS_SCHEMA: dict[str, Any] = {
    "evaluation": pl.String,
    "fold": pl.String,
    "fold_index": pl.Int64,
    "n_train": pl.Int64,
    "n_test": pl.Int64,
    "train_devices": pl.Int64,
    "test_devices": pl.Int64,
    "train_stations": pl.Int64,
    "test_stations": pl.Int64,
    "train_dates": pl.Int64,
    "test_dates": pl.Int64,
    "train_membership_sha256": pl.String,
    "test_membership_sha256": pl.String,
}
_PREDICTIONS_SCHEMA: dict[str, Any] = {
    "evaluation": pl.String,
    "fold": pl.String,
    "fold_index": pl.Int64,
    "device_id": pl.String,
    "hour": pl.Datetime("us"),
    "date": pl.Date,
    "station_name": pl.String,
    "distance_km": pl.Float64,
    "pm25_mean": pl.Float64,
    "truth": pl.Float64,
    "raw_micro": pl.Float64,
    "micro_only": pl.Float64,
    "micro_weather": pl.Float64,
}
_SCORES_SCHEMA: dict[str, Any] = {
    "evaluation": pl.String,
    "fold": pl.String,
    "fold_index": pl.Int64,
    "evaluation_unit": pl.String,
    "feature_set": pl.String,
    "n": pl.Int64,
    "rmse": pl.Float64,
    "mae": pl.Float64,
    "r2": pl.Float64,
    "bias": pl.Float64,
}
_DELTAS_SCHEMA: dict[str, Any] = {
    "evaluation": pl.String,
    "fold": pl.String,
    "fold_index": pl.Int64,
    "evaluation_unit": pl.String,
    "comparison": pl.String,
    "candidate": pl.String,
    "reference": pl.String,
    "n": pl.Int64,
    "rmse_delta": pl.Float64,
    "mae_delta": pl.Float64,
    "r2_delta": pl.Float64,
    "bias_delta": pl.Float64,
    "abs_bias_delta": pl.Float64,
    "rmse_improved": pl.Boolean,
    "mae_improved": pl.Boolean,
    "r2_improved": pl.Boolean,
    "abs_bias_improved": pl.Boolean,
}
_COMPARISONS = (
    ("micro_only", "raw_micro", "micro_only_minus_raw_micro"),
    ("micro_weather", "raw_micro", "micro_weather_minus_raw_micro"),
    ("micro_weather", "micro_only", "micro_weather_minus_micro_only"),
)


@dataclass(frozen=True, slots=True)
class MicroSensorBenchmarkConfig:
    readiness_generation_sha256: str
    date_folds: int
    station_folds: int
    feature_sets: dict[str, tuple[str, ...]]
    model: ModelConfig


@dataclass(frozen=True, slots=True)
class ReadinessRows:
    generation_sha256: str
    rows: pl.DataFrame
    manifest: dict[str, Any]
    summary: dict[str, Any]
    input_files: tuple[InputFile, ...]


@dataclass(frozen=True, slots=True)
class PreparedMicroSensorBenchmark:
    rows: pl.DataFrame
    fold_membership: pl.DataFrame
    source_rows: int
    readiness_generation_sha256: str
    readiness_input_files: tuple[InputFile, ...]
    geography_sha256: str


@dataclass(frozen=True, slots=True)
class MicroSensorBenchmarkEvaluation:
    folds: pl.DataFrame
    predictions: pl.DataFrame


@dataclass(frozen=True, slots=True)
class MicroSensorBenchmarkResult:
    fold_membership: pl.DataFrame
    folds: pl.DataFrame
    predictions: pl.DataFrame
    scores: pl.DataFrame
    deltas: pl.DataFrame
    summary: dict[str, Any]
    manifest: dict[str, Any]


def _mapping(value: object, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{label} must be a mapping")
    return value


def _exact_keys(value: dict[str, Any], expected: set[str], *, label: str) -> None:
    unknown = set(value) - expected
    missing = expected - set(value)
    if unknown:
        raise ConfigError(f"{label} has unknown field(s) {sorted(unknown)}")
    if missing:
        raise ConfigError(f"{label} is missing field(s) {sorted(missing)}")


def _positive_int(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(f"{label} must be a positive integer")
    return value


def _positive_float(value: object, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{label} must be a positive finite number")
    converted = float(value)
    if converted <= 0 or not math.isfinite(converted):
        raise ConfigError(f"{label} must be a positive finite number")
    return converted


def _identity(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ConfigError(f"{label} must be a lowercase SHA-256")
    return value


def load_micro_sensor_benchmark_config(
    config: dict[str, Any] | None = None,
) -> MicroSensorBenchmarkConfig:
    raw = config if config is not None else load_conf("micro_sensor_calibration")
    _exact_keys(raw, {"analysis"}, label="micro_sensor_calibration")
    group = _mapping(raw["analysis"], label="micro_sensor_calibration.analysis")
    _exact_keys(group, _CONFIG_KEYS, label="micro_sensor_calibration.analysis")

    date_folds = _positive_int(
        group["date_folds"], label="micro_sensor_calibration.analysis.date_folds"
    )
    if date_folds != 25:
        raise ConfigError("micro_sensor_calibration.analysis.date_folds must be 25")
    station_folds = _positive_int(
        group["station_folds"], label="micro_sensor_calibration.analysis.station_folds"
    )
    if station_folds != 10:
        raise ConfigError("micro_sensor_calibration.analysis.station_folds must be 10")

    feature_sets = _mapping(
        group["feature_sets"], label="micro_sensor_calibration.analysis.feature_sets"
    )
    parsed_feature_sets: dict[str, tuple[str, ...]] = {}
    for name, raw_features in feature_sets.items():
        if (
            not isinstance(name, str)
            or not isinstance(raw_features, list)
            or any(not isinstance(feature, str) for feature in raw_features)
        ):
            raise ConfigError(
                "micro_sensor_calibration.analysis.feature_sets must map names to feature lists"
            )
        parsed_feature_sets[name] = tuple(raw_features)
    if parsed_feature_sets != BENCHMARK_FEATURE_SETS:
        raise ConfigError("micro_sensor_calibration.analysis.feature_sets changed")

    raw_model = _mapping(group["model"], label="micro_sensor_calibration.analysis.model")
    _exact_keys(raw_model, _MODEL_KEYS, label="micro_sensor_calibration.analysis.model")
    model = ModelConfig(
        n_estimators=_positive_int(
            raw_model["n_estimators"],
            label="micro_sensor_calibration.analysis.model.n_estimators",
        ),
        learning_rate=_positive_float(
            raw_model["learning_rate"],
            label="micro_sensor_calibration.analysis.model.learning_rate",
        ),
        num_leaves=_positive_int(
            raw_model["num_leaves"], label="micro_sensor_calibration.analysis.model.num_leaves"
        ),
        min_child_samples=_positive_int(
            raw_model["min_child_samples"],
            label="micro_sensor_calibration.analysis.model.min_child_samples",
        ),
        n_jobs=_positive_int(
            raw_model["n_jobs"], label="micro_sensor_calibration.analysis.model.n_jobs"
        ),
        seed=_positive_int(raw_model["seed"], label="micro_sensor_calibration.analysis.model.seed"),
    )
    expected_model = ModelConfig(200, 0.05, 31, 10, 1, 20260812)
    if model.n_jobs != 1:
        raise ConfigError("micro-sensor benchmark models must run with n_jobs=1")
    if model != expected_model:
        raise ConfigError("micro_sensor_calibration.analysis.model changed")
    return MicroSensorBenchmarkConfig(
        readiness_generation_sha256=_identity(
            group["readiness_generation_sha256"],
            label="micro_sensor_calibration.analysis.readiness_generation_sha256",
        ),
        date_folds=date_folds,
        station_folds=station_folds,
        feature_sets=parsed_feature_sets,
        model=model,
    )


def _file_identity(path: Path) -> InputFile:
    if not path.is_file():
        raise RuntimeError(f"micro-sensor readiness member is missing: {path.name}")
    return InputFile(path=path, bytes=path.stat().st_size, sha256=sha256_file(path))


def _read_json_stable(path: Path) -> tuple[dict[str, Any], InputFile]:
    before = _file_identity(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"micro-sensor readiness JSON is unreadable: {path.name}") from exc
    after = _file_identity(path)
    if before != after:
        raise RuntimeError(f"micro-sensor readiness member changed while read: {path.name}")
    if not isinstance(value, dict):
        raise RuntimeError(f"micro-sensor readiness JSON must be an object: {path.name}")
    return value, before


def _read_parquet_stable(path: Path) -> tuple[pl.DataFrame, InputFile]:
    before = _file_identity(path)
    try:
        frame = pl.read_parquet(path)
    except (OSError, pl.exceptions.PolarsError) as exc:
        raise RuntimeError(f"micro-sensor readiness Parquet is unreadable: {path.name}") from exc
    after = _file_identity(path)
    if before != after:
        raise RuntimeError(f"micro-sensor readiness member changed while read: {path.name}")
    return frame, before


def _hash_json(value: object) -> str:
    return sha256(
        (
            json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
            + "\n"
        ).encode("utf-8")
    ).hexdigest()


def load_micro_sensor_readiness_rows(
    *,
    generation_sha256: str,
    data_root: Path | None = None,
) -> ReadinessRows:
    if _SHA256.fullmatch(generation_sha256) is None:
        raise ValueError("micro-sensor readiness generation must be a lowercase SHA-256")
    root = data_root or configured_data_root()
    directory = (
        root / "outputs" / "micro_sensor_calibration_readiness" / "generations" / generation_sha256
    )
    if not directory.is_dir():
        raise RuntimeError(f"micro-sensor readiness generation is missing: {generation_sha256}")
    observed_files = {path.name for path in directory.iterdir() if path.is_file()}
    if observed_files != _EXPECTED_FILES:
        missing = sorted(_EXPECTED_FILES - observed_files)
        extra = sorted(observed_files - _EXPECTED_FILES)
        raise RuntimeError(
            f"micro-sensor readiness member inventory changed; missing={missing}, extra={extra}"
        )

    manifest, manifest_file = _read_json_stable(directory / "manifest.json")
    summary, summary_file = _read_json_stable(directory / "summary.json")
    if (
        manifest.get("schema_version") != 1
        or manifest.get("analysis") != "micro_sensor_calibration_readiness"
        or manifest.get("complete") is not True
        or manifest.get("panel_dates") != 25
        or manifest.get("output_identity_sha256") != generation_sha256
        or manifest.get("claim_boundary") != readiness._CLAIM_BOUNDARY
    ):
        raise RuntimeError("micro-sensor readiness manifest contract changed")
    manifest_config = manifest.get("config")
    if not isinstance(manifest_config, dict) or manifest.get(
        "config_sha256"
    ) != readiness._hash_value(manifest_config):
        raise RuntimeError("micro-sensor readiness config identity changed")
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
    if readiness._hash_value(identity_payload) != generation_sha256:
        raise RuntimeError("micro-sensor readiness generation identity changed")

    frames: dict[str, pl.DataFrame] = {}
    member_files: list[InputFile] = []
    for name in _MEMBERS:
        frame, member_file = _read_parquet_stable(directory / f"{name}.parquet")
        if dict(frame.schema) != readiness._RESULT_SCHEMAS[name]:
            raise RuntimeError(f"micro-sensor readiness {name} schema changed")
        frames[name] = frame
        member_files.append(member_file)
    output_rows = {name: frame.height for name, frame in frames.items()}
    if manifest.get("output_rows") != output_rows or summary.get("output_rows") != output_rows:
        raise RuntimeError("micro-sensor readiness output row counts changed")
    return ReadinessRows(
        generation_sha256=generation_sha256,
        rows=frames["hourly_pairs"],
        manifest=manifest,
        summary=summary,
        input_files=tuple(
            sorted((manifest_file, summary_file, *member_files), key=lambda item: item.path.name)
        ),
    )


def _validate_eligible_values(rows: pl.DataFrame) -> None:
    invalid = rows.filter(
        pl.any_horizontal(
            *(
                pl.col(column).is_null() | ~pl.col(column).is_finite()
                for column in _ELIGIBLE_VALUE_COLUMNS
            )
        )
        | pl.col("device_id").is_null()
        | pl.col("station_name").is_null()
        | pl.col("hour").is_null()
    )
    if not invalid.is_empty():
        raise RuntimeError("micro-sensor benchmark eligible values contain null or non-finite data")


def prepare_micro_sensor_benchmark(
    readiness_rows: ReadinessRows,
    geography: pl.DataFrame,
    config: MicroSensorBenchmarkConfig,
) -> PreparedMicroSensorBenchmark:
    if readiness_rows.generation_sha256 != config.readiness_generation_sha256:
        raise RuntimeError("micro-sensor benchmark readiness generation changed")
    source = readiness_rows.rows
    duplicated = source.group_by("device_id", "hour").len().filter(pl.col("len") > 1)
    if not duplicated.is_empty():
        raise RuntimeError("micro-sensor readiness device-hour keys are duplicated")
    if source["eligibility_reason"].null_count() != 0:
        raise RuntimeError("micro-sensor readiness eligibility reasons contain null")
    eligible = source.filter(pl.col("eligibility_reason") == "eligible")
    if eligible.is_empty():
        raise RuntimeError("micro-sensor benchmark has no eligible rows")
    _validate_eligible_values(eligible)
    if not eligible["ground_eligible"].all():
        raise RuntimeError("micro-sensor benchmark eligible rows changed ground eligibility")

    required_geo = {"station_name", "airzone_official"}
    missing_geo_columns = required_geo - set(geography.columns)
    if missing_geo_columns:
        raise RuntimeError(
            f"micro-sensor station metadata is missing {sorted(missing_geo_columns)}"
        )
    selected_geo = geography.select("station_name", "airzone_official")
    if selected_geo["station_name"].n_unique() != selected_geo.height:
        raise RuntimeError("micro-sensor station metadata has duplicated station names")
    expected_stations = eligible.select("station_name").unique()
    absent = expected_stations.join(
        selected_geo.select("station_name"), on="station_name", how="anti"
    )
    if not absent.is_empty():
        raise RuntimeError("micro-sensor station metadata is absent for an eligible station")
    selected_geo = selected_geo.join(expected_stations, on="station_name", how="semi")
    station_membership = assign_station_folds(selected_geo, fold_count=config.station_folds)

    dates = eligible.select(pl.col("hour").dt.date().alias("date")).unique().sort("date")
    if dates.height != config.date_folds:
        raise RuntimeError("micro-sensor benchmark observed date count changed")
    expected_dates = [date(2025, 1, 1 + index) for index in range(25)]
    if dates["date"].to_list() != expected_dates:
        raise RuntimeError("micro-sensor benchmark dates must be 2025-01-01 through 2025-01-25")
    date_membership = pl.DataFrame(
        {
            "fold_kind": ["held_date"] * dates.height,
            "group": [value.isoformat() for value in dates["date"].to_list()],
            "fold": [f"held_date_{value.isoformat()}" for value in dates["date"].to_list()],
            "fold_index": list(range(dates.height)),
            "airzone_official": [None] * dates.height,
        },
        schema_overrides={"fold_index": pl.Int64, "airzone_official": pl.String},
    )
    station_fold_membership = station_membership.select(
        pl.lit("held_station").alias("fold_kind"),
        pl.col("station_name").alias("group"),
        pl.concat_str(
            pl.lit("held_station_"),
            pl.col("station_fold").cast(pl.String).str.pad_start(2, "0"),
        ).alias("fold"),
        pl.col("station_fold").cast(pl.Int64).alias("fold_index"),
        pl.col("airzone_official"),
    )
    fold_membership = pl.concat(
        [date_membership, station_fold_membership], how="vertical_relaxed"
    ).sort("fold_kind", "fold_index", "group")

    prepared = (
        eligible.with_columns(pl.col("hour").dt.date().alias("date"))
        .join(
            station_membership.select("station_name", "station_fold"),
            on="station_name",
            how="left",
            validate="m:1",
        )
        .sort("device_id", "hour")
    )
    if prepared.height != eligible.height or prepared["station_fold"].null_count() != 0:
        raise RuntimeError("micro-sensor benchmark fold join changed eligible rows")
    geography_identity = _hash_json(selected_geo.sort("station_name").to_dicts())
    return PreparedMicroSensorBenchmark(
        rows=prepared,
        fold_membership=fold_membership,
        source_rows=source.height,
        readiness_generation_sha256=readiness_rows.generation_sha256,
        readiness_input_files=readiness_rows.input_files,
        geography_sha256=geography_identity,
    )


def _membership_sha256(rows: pl.DataFrame) -> str:
    keys = rows.select("device_id", "hour").sort("device_id", "hour")
    return _hash_json(keys.to_dicts())


def _fit_predict(
    train: pl.DataFrame,
    test: pl.DataFrame,
    features: tuple[str, ...],
    model: ModelConfig,
) -> np.ndarray:
    import lightgbm as lgb

    estimator = lgb.LGBMRegressor(
        n_estimators=model.n_estimators,
        learning_rate=model.learning_rate,
        num_leaves=model.num_leaves,
        min_child_samples=model.min_child_samples,
        subsample=0.8,
        subsample_freq=1,
        colsample_bytree=0.8,
        random_state=model.seed,
        n_jobs=model.n_jobs,
        verbose=-1,
    )
    estimator.fit(train.select(features).to_numpy(), train["ground_pm25"].to_numpy())
    return np.asarray(estimator.predict(test.select(features).to_numpy()), dtype=float)


def _fold_description(
    *,
    evaluation: str,
    fold: str,
    fold_index: int,
    train: pl.DataFrame,
    test: pl.DataFrame,
) -> dict[str, object]:
    return {
        "evaluation": evaluation,
        "fold": fold,
        "fold_index": fold_index,
        "n_train": train.height,
        "n_test": test.height,
        "train_devices": train["device_id"].n_unique(),
        "test_devices": test["device_id"].n_unique(),
        "train_stations": train["station_name"].n_unique(),
        "test_stations": test["station_name"].n_unique(),
        "train_dates": train["date"].n_unique(),
        "test_dates": test["date"].n_unique(),
        "train_membership_sha256": _membership_sha256(train),
        "test_membership_sha256": _membership_sha256(test),
    }


def _prediction_rows(
    *,
    evaluation: str,
    fold: str,
    fold_index: int,
    train: pl.DataFrame,
    test: pl.DataFrame,
    config: MicroSensorBenchmarkConfig,
) -> pl.DataFrame:
    if train.is_empty() or test.is_empty():
        raise RuntimeError(f"micro-sensor benchmark fold {fold} has no train or test rows")
    train_keys = set(train.select("device_id", "hour").iter_rows())
    test_keys = set(test.select("device_id", "hour").iter_rows())
    if train_keys & test_keys:
        raise RuntimeError(f"micro-sensor benchmark fold {fold} leaks train rows into test")
    if evaluation == "held_date" and set(train["date"].to_list()) & set(test["date"].to_list()):
        raise RuntimeError(f"micro-sensor benchmark fold {fold} leaks held dates")
    if evaluation == "held_station" and set(train["station_name"].to_list()) & set(
        test["station_name"].to_list()
    ):
        raise RuntimeError(f"micro-sensor benchmark fold {fold} leaks held stations")

    predictions: dict[str, np.ndarray] = {
        "raw_micro": np.asarray(test["pm25_mean"].to_numpy(), dtype=float)
    }
    for feature_set in ("micro_only", "micro_weather"):
        prediction = _fit_predict(
            train,
            test,
            config.feature_sets[feature_set],
            config.model,
        )
        predictions[feature_set] = np.asarray(prediction, dtype=float)
    for name, values in predictions.items():
        if values.shape != (test.height,) or not np.isfinite(values).all():
            raise RuntimeError(
                f"micro-sensor benchmark {name} predictions are missing or non-finite in {fold}"
            )

    return (
        test.select(
            "device_id",
            "hour",
            "date",
            "station_name",
            "distance_km",
            "pm25_mean",
            pl.col("ground_pm25").alias("truth"),
        )
        .with_columns(
            pl.lit(evaluation).alias("evaluation"),
            pl.lit(fold).alias("fold"),
            pl.lit(fold_index, dtype=pl.Int64).alias("fold_index"),
            *(pl.Series(name, values, dtype=pl.Float64) for name, values in predictions.items()),
        )
        .select(*_PREDICTIONS_SCHEMA)
        .sort("device_id", "hour")
    )


def evaluate_micro_sensor_benchmark(
    prepared: PreparedMicroSensorBenchmark,
    config: MicroSensorBenchmarkConfig,
) -> MicroSensorBenchmarkEvaluation:
    if config.feature_sets != BENCHMARK_FEATURE_SETS or config.model.n_jobs != 1:
        raise RuntimeError("micro-sensor benchmark model contract changed")
    rows = prepared.rows
    fold_rows: list[dict[str, object]] = []
    prediction_frames: list[pl.DataFrame] = []
    date_groups = prepared.fold_membership.filter(pl.col("fold_kind") == "held_date")
    for item in date_groups.iter_rows(named=True):
        held_date = date.fromisoformat(str(item["group"]))
        test = rows.filter(pl.col("date") == held_date)
        train = rows.filter(pl.col("date") != held_date)
        fold = str(item["fold"])
        fold_index = int(item["fold_index"])
        fold_rows.append(
            _fold_description(
                evaluation="held_date",
                fold=fold,
                fold_index=fold_index,
                train=train,
                test=test,
            )
        )
        prediction_frames.append(
            _prediction_rows(
                evaluation="held_date",
                fold=fold,
                fold_index=fold_index,
                train=train,
                test=test,
                config=config,
            )
        )

    station_groups = (
        prepared.fold_membership.filter(pl.col("fold_kind") == "held_station")
        .select("fold", "fold_index")
        .unique()
        .sort("fold_index")
    )
    for item in station_groups.iter_rows(named=True):
        fold_index = int(item["fold_index"])
        test = rows.filter(pl.col("station_fold") == fold_index)
        train = rows.filter(pl.col("station_fold") != fold_index)
        fold = str(item["fold"])
        fold_rows.append(
            _fold_description(
                evaluation="held_station",
                fold=fold,
                fold_index=fold_index,
                train=train,
                test=test,
            )
        )
        prediction_frames.append(
            _prediction_rows(
                evaluation="held_station",
                fold=fold,
                fold_index=fold_index,
                train=train,
                test=test,
                config=config,
            )
        )
    folds = pl.DataFrame(fold_rows, schema=_FOLDS_SCHEMA).sort("evaluation", "fold_index")
    predictions = pl.concat(prediction_frames).sort("evaluation", "fold_index", "device_id", "hour")
    tested = predictions.group_by("evaluation", "device_id", "hour").len()
    if tested.height != rows.height * 2 or tested["len"].unique().to_list() != [1]:
        raise RuntimeError("micro-sensor benchmark did not test every source row exactly once")
    return MicroSensorBenchmarkEvaluation(folds=folds, predictions=predictions)


def _score_frame(
    frame: pl.DataFrame,
    *,
    evaluation: str,
    fold: str,
    fold_index: int,
    evaluation_unit: str,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    truth = frame["truth"].to_numpy()
    if not np.isfinite(truth).all() or float(np.var(truth)) == 0.0:
        raise RuntimeError(
            f"micro-sensor benchmark {evaluation}/{fold}/{evaluation_unit} has a constant target"
        )
    for feature_set in BENCHMARK_FEATURE_SETS:
        prediction = frame[feature_set].to_numpy()
        if not np.isfinite(prediction).all():
            raise RuntimeError("micro-sensor benchmark predictions contain non-finite values")
        metrics = evaluate_predictions(truth, prediction, exceedance_threshold=None)
        bias = float(np.mean(prediction - truth))
        values = (metrics.rmse, metrics.mae, metrics.r2, bias)
        if metrics.n != frame.height or not all(math.isfinite(value) for value in values):
            raise RuntimeError("micro-sensor benchmark metrics are missing or non-finite")
        rows.append(
            {
                "evaluation": evaluation,
                "fold": fold,
                "fold_index": fold_index,
                "evaluation_unit": evaluation_unit,
                "feature_set": feature_set,
                "n": metrics.n,
                "rmse": metrics.rmse,
                "mae": metrics.mae,
                "r2": metrics.r2,
                "bias": bias,
            }
        )
    return rows


def score_micro_sensor_predictions(predictions: pl.DataFrame) -> pl.DataFrame:
    required = {
        "evaluation",
        "fold",
        "fold_index",
        "device_id",
        "hour",
        "station_name",
        "truth",
        *BENCHMARK_FEATURE_SETS,
    }
    missing = required - set(predictions.columns)
    if missing:
        raise RuntimeError(f"micro-sensor benchmark predictions are missing {sorted(missing)}")
    duplicated = (
        predictions.group_by("evaluation", "device_id", "hour").len().filter(pl.col("len") > 1)
    )
    if not duplicated.is_empty():
        raise RuntimeError("micro-sensor benchmark prediction keys are duplicated")
    rows: list[dict[str, object]] = []
    for group in predictions.partition_by("evaluation", "fold", "fold_index", maintain_order=True):
        evaluation = str(group["evaluation"][0])
        fold = str(group["fold"][0])
        fold_index = int(group["fold_index"][0])
        rows.extend(
            _score_frame(
                group,
                evaluation=evaluation,
                fold=fold,
                fold_index=fold_index,
                evaluation_unit="device_hour",
            )
        )
        station_hour = group.group_by("station_name", "hour").agg(
            pl.col("truth").n_unique().alias("truth_values"),
            pl.col("truth").first(),
            *(pl.col(name).mean() for name in BENCHMARK_FEATURE_SETS),
        )
        if station_hour.filter(pl.col("truth_values") != 1).height:
            raise RuntimeError("micro-sensor benchmark station-hour ground targets disagree")
        rows.extend(
            _score_frame(
                station_hour,
                evaluation=evaluation,
                fold=fold,
                fold_index=fold_index,
                evaluation_unit="reference_station_hour",
            )
        )
    return pl.DataFrame(rows, schema=_SCORES_SCHEMA).sort(
        "evaluation", "fold_index", "evaluation_unit", "feature_set"
    )


def micro_sensor_benchmark_deltas(scores: pl.DataFrame) -> pl.DataFrame:
    if dict(scores.schema) != _SCORES_SCHEMA:
        raise RuntimeError("micro-sensor benchmark score schema changed")
    rows: list[dict[str, object]] = []
    group_columns = ("evaluation", "fold", "fold_index", "evaluation_unit")
    for group in scores.partition_by(*group_columns, maintain_order=True):
        identity = {column: group[column][0] for column in group_columns}
        indexed = {str(row["feature_set"]): row for row in group.iter_rows(named=True)}
        if set(indexed) != set(BENCHMARK_FEATURE_SETS):
            raise RuntimeError(f"micro-sensor benchmark scores are incomplete for {identity}")
        for candidate_name, reference_name, comparison in _COMPARISONS:
            candidate = indexed[candidate_name]
            reference = indexed[reference_name]
            if candidate["n"] != reference["n"]:
                raise RuntimeError(f"micro-sensor benchmark scores are not paired for {identity}")
            rmse_delta = float(candidate["rmse"]) - float(reference["rmse"])
            mae_delta = float(candidate["mae"]) - float(reference["mae"])
            r2_delta = float(candidate["r2"]) - float(reference["r2"])
            bias_delta = float(candidate["bias"]) - float(reference["bias"])
            abs_bias_delta = abs(float(candidate["bias"])) - abs(float(reference["bias"]))
            rows.append(
                {
                    **identity,
                    "comparison": comparison,
                    "candidate": candidate_name,
                    "reference": reference_name,
                    "n": int(candidate["n"]),
                    "rmse_delta": rmse_delta,
                    "mae_delta": mae_delta,
                    "r2_delta": r2_delta,
                    "bias_delta": bias_delta,
                    "abs_bias_delta": abs_bias_delta,
                    "rmse_improved": rmse_delta < 0,
                    "mae_improved": mae_delta < 0,
                    "r2_improved": r2_delta > 0,
                    "abs_bias_improved": abs_bias_delta < 0,
                }
            )
    return pl.DataFrame(rows, schema=_DELTAS_SCHEMA).sort(
        "evaluation", "fold_index", "evaluation_unit", "comparison"
    )


def _summary(
    prepared: PreparedMicroSensorBenchmark,
    evaluation: MicroSensorBenchmarkEvaluation,
    scores: pl.DataFrame,
    deltas: pl.DataFrame,
) -> dict[str, Any]:
    comparisons: dict[str, Any] = {}
    for key in deltas.select("evaluation", "evaluation_unit", "comparison").unique().iter_rows():
        evaluation_name, evaluation_unit, comparison = (str(value) for value in key)
        subset = deltas.filter(
            (pl.col("evaluation") == evaluation_name)
            & (pl.col("evaluation_unit") == evaluation_unit)
            & (pl.col("comparison") == comparison)
        )
        comparisons[f"{evaluation_name}/{evaluation_unit}/{comparison}"] = {
            "folds": subset.height,
            "median_rmse_delta": as_float(subset["rmse_delta"].median()),
            "median_mae_delta": as_float(subset["mae_delta"].median()),
            "median_r2_delta": as_float(subset["r2_delta"].median()),
            "median_abs_bias_delta": as_float(subset["abs_bias_delta"].median()),
        }
    return {
        "source_rows": prepared.source_rows,
        "eligible_rows": prepared.rows.height,
        "devices": prepared.rows["device_id"].n_unique(),
        "reference_stations": prepared.rows["station_name"].n_unique(),
        "dates": prepared.rows["date"].n_unique(),
        "folds": evaluation.folds.height,
        "prediction_rows": evaluation.predictions.height,
        "score_rows": scores.height,
        "delta_rows": deltas.height,
        "comparisons": comparisons,
    }


def _rehash_inputs(files: tuple[InputFile, ...]) -> tuple[InputFile, ...]:
    return tuple(_file_identity(item.path) for item in files)


def run_micro_sensor_calibration_benchmark(
    *,
    data_root: Path | None = None,
    config: MicroSensorBenchmarkConfig | None = None,
    geography: pl.DataFrame | None = None,
    generated_at: str | None = None,
    git_sha: str | None = None,
    git_dirty: bool | None = None,
) -> MicroSensorBenchmarkResult:
    root = data_root or configured_data_root()
    selected = config or load_micro_sensor_benchmark_config()
    loaded = load_micro_sensor_readiness_rows(
        data_root=root,
        generation_sha256=selected.readiness_generation_sha256,
    )
    selected_geography = geography if geography is not None else resolve_station_geo()
    prepared = prepare_micro_sensor_benchmark(loaded, selected_geography, selected)
    before_files = prepared.readiness_input_files
    before_geography = prepared.geography_sha256
    evaluation = evaluate_micro_sensor_benchmark(prepared, selected)
    scores = score_micro_sensor_predictions(evaluation.predictions)
    deltas = micro_sensor_benchmark_deltas(scores)
    after_files = _rehash_inputs(before_files)
    if before_files != after_files:
        raise RuntimeError("micro-sensor readiness input changed during benchmark analysis")
    post_geography = selected_geography if geography is not None else resolve_station_geo()
    after_geography = _hash_json(
        post_geography.select("station_name", "airzone_official")
        .join(prepared.rows.select("station_name").unique(), on="station_name", how="semi")
        .sort("station_name")
        .to_dicts()
    )
    if before_geography != after_geography:
        raise RuntimeError("reviewed station geography changed during benchmark analysis")

    measured_sha, measured_dirty = git_state()
    resolved_sha = git_sha if git_sha is not None else measured_sha
    resolved_dirty = git_dirty if git_dirty is not None else measured_dirty
    config_payload = asdict(selected)
    output_rows = {
        "fold_membership": prepared.fold_membership.height,
        "folds": evaluation.folds.height,
        "predictions": evaluation.predictions.height,
        "scores": scores.height,
        "deltas": deltas.height,
    }
    summary = _summary(prepared, evaluation, scores, deltas)
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "analysis": "micro_sensor_calibration_benchmark",
        "complete": True,
        "generated_at": generated_at or datetime.now(UTC).isoformat(timespec="seconds"),
        "readiness_generation_sha256": selected.readiness_generation_sha256,
        "config": config_payload,
        "config_sha256": _hash_json(config_payload),
        "inputs": [
            {
                "path": item.path.relative_to(root).as_posix(),
                "bytes": item.bytes,
                "sha256": item.sha256,
            }
            for item in before_files
        ],
        "reviewed_station_airzone_sha256": prepared.geography_sha256,
        "output_rows": output_rows,
        "claim_boundary": CLAIM_BOUNDARY,
        "git_sha": resolved_sha,
        "git_dirty": resolved_dirty,
    }
    identity_payload = {
        key: manifest[key]
        for key in (
            "schema_version",
            "analysis",
            "readiness_generation_sha256",
            "config_sha256",
            "inputs",
            "reviewed_station_airzone_sha256",
            "output_rows",
            "claim_boundary",
            "git_sha",
            "git_dirty",
        )
    }
    manifest["output_identity_sha256"] = _hash_json(identity_payload)
    return MicroSensorBenchmarkResult(
        fold_membership=prepared.fold_membership,
        folds=evaluation.folds,
        predictions=evaluation.predictions,
        scores=scores,
        deltas=deltas,
        summary=summary,
        manifest=manifest,
    )


def micro_sensor_calibration_benchmark_dir(*, identity: str) -> Path:
    if _SHA256.fullmatch(identity) is None:
        raise ValueError("micro-sensor benchmark identity must be a lowercase SHA-256")
    return outputs_dir("micro_sensor_calibration_benchmark") / "generations" / identity


def _validate_result(result: MicroSensorBenchmarkResult) -> None:
    members = {
        "fold_membership": result.fold_membership,
        "folds": result.folds,
        "predictions": result.predictions,
        "scores": result.scores,
        "deltas": result.deltas,
    }
    expected_schemas = {
        "fold_membership": _FOLD_MEMBERSHIP_SCHEMA,
        "folds": _FOLDS_SCHEMA,
        "predictions": _PREDICTIONS_SCHEMA,
        "scores": _SCORES_SCHEMA,
        "deltas": _DELTAS_SCHEMA,
    }
    for name, frame in members.items():
        if dict(frame.schema) != expected_schemas[name]:
            raise RuntimeError(f"micro-sensor benchmark {name} schema changed")
    rows = {name: frame.height for name, frame in members.items()}
    manifest = result.manifest
    if (
        manifest.get("schema_version") != 1
        or manifest.get("analysis") != "micro_sensor_calibration_benchmark"
        or manifest.get("complete") is not True
        or manifest.get("output_rows") != rows
        or manifest.get("claim_boundary") != CLAIM_BOUNDARY
    ):
        raise RuntimeError("micro-sensor benchmark manifest contract changed")
    config = manifest.get("config")
    if not isinstance(config, dict) or manifest.get("config_sha256") != _hash_json(config):
        raise RuntimeError("micro-sensor benchmark config identity changed")
    identity_payload = {
        key: manifest[key]
        for key in (
            "schema_version",
            "analysis",
            "readiness_generation_sha256",
            "config_sha256",
            "inputs",
            "reviewed_station_airzone_sha256",
            "output_rows",
            "claim_boundary",
            "git_sha",
            "git_dirty",
        )
    }
    if manifest.get("output_identity_sha256") != _hash_json(identity_payload):
        raise RuntimeError("micro-sensor benchmark output identity changed")
    if result.summary.get("prediction_rows") != result.predictions.height:
        raise RuntimeError("micro-sensor benchmark summary row counts changed")
    try:
        json.dumps(result.summary, ensure_ascii=False, allow_nan=False)
        json.dumps(manifest, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("micro-sensor benchmark metadata is not finite JSON") from exc


def _recover_swap(destination: Path) -> None:
    parent = destination.parent
    if not parent.exists():
        return
    backups = sorted(parent.glob(f".{destination.name}.backup-*"))
    stages = sorted(parent.glob(f".{destination.name}.staging-*"))
    if len(backups) > 1 or len(stages) > 1:
        raise RuntimeError(f"multiple interrupted benchmark swaps beside {destination}")
    if destination.exists() and backups and stages:
        raise RuntimeError(f"ambiguous interrupted benchmark swap beside {destination}")
    if not destination.exists() and backups:
        backups[0].replace(destination)
        backups = []
    for staged in stages:
        shutil.rmtree(staged)
    if destination.exists() and backups:
        shutil.rmtree(backups[0])


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def write_micro_sensor_calibration_benchmark_result(
    result: MicroSensorBenchmarkResult,
    *,
    destination: Path | None = None,
) -> dict[str, Path]:
    _validate_result(result)
    identity = result.manifest.get("output_identity_sha256")
    if not isinstance(identity, str):
        raise RuntimeError("micro-sensor benchmark has no output identity")
    out = destination or micro_sensor_calibration_benchmark_dir(identity=identity)
    _recover_swap(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    token = uuid4().hex
    staged = out.with_name(f".{out.name}.staging-{token}")
    backup = out.with_name(f".{out.name}.backup-{token}")
    staged.mkdir()
    had_existing = out.exists()
    try:
        result.fold_membership.write_parquet(staged / "fold_membership.parquet")
        result.folds.write_parquet(staged / "folds.parquet")
        result.predictions.write_parquet(staged / "predictions.parquet")
        result.scores.write_parquet(staged / "scores.parquet")
        result.deltas.write_parquet(staged / "deltas.parquet")
        _write_json(staged / "summary.json", result.summary)
        _write_json(staged / "manifest.json", result.manifest)
        try:
            if had_existing:
                out.replace(backup)
            staged.replace(out)
        except BaseException:
            if backup.exists():
                if out.exists():
                    shutil.rmtree(out)
                backup.replace(out)
            elif not had_existing and out.exists():
                shutil.rmtree(out)
            raise
        if backup.exists():
            shutil.rmtree(backup)
    finally:
        if staged.exists():
            shutil.rmtree(staged)
    return {
        "fold_membership": out / "fold_membership.parquet",
        "folds": out / "folds.parquet",
        "predictions": out / "predictions.parquet",
        "scores": out / "scores.parquet",
        "deltas": out / "deltas.parquet",
        "summary": out / "summary.json",
        "manifest": out / "manifest.json",
    }
