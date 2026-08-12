"""Test monthly reference-station satellite context without calling it fusion."""

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
from twair.analysis import micro_sensor_calibration_benchmark as benchmark
from twair.analysis.era5_value import InputFile
from twair.config import ConfigError, load_conf
from twair.net import sha256_file
from twair.paths import data_root as configured_data_root
from twair.paths import outputs_dir
from twair.provenance import git_state
from twair.scalars import as_float

__all__ = [
    "CLAIM_BOUNDARY",
    "COMPARISONS",
    "FEATURE_SETS",
    "LEARNED_FEATURE_SETS",
    "SATELLITE_SOURCES",
    "MicroSensorSatelliteInputs",
    "MicroSensorSatelliteValueConfig",
    "MicroSensorSatelliteValueEvaluation",
    "MicroSensorSatelliteValueResult",
    "PreparedMicroSensorSatelliteValue",
    "SatelliteContextModelConfig",
    "evaluate_micro_sensor_satellite_value",
    "load_micro_sensor_satellite_inputs",
    "load_micro_sensor_satellite_value_config",
    "micro_sensor_satellite_value_deltas",
    "prepare_micro_sensor_satellite_value",
    "run_micro_sensor_satellite_value",
    "score_micro_sensor_satellite_predictions",
    "write_micro_sensor_satellite_value_result",
]


_SHA256 = re.compile(r"[0-9a-f]{64}")
SATELLITE_SOURCES = ("maiac_aod", "s5p_no2", "s5p_so2")
FEATURE_SETS: dict[str, tuple[str, ...]] = {
    "raw_micro": (),
    "micro_only": ("pm25_mean",),
    "micro_weather": ("pm25_mean", "humidity_mean", "temperature_mean"),
    "micro_satellite": ("pm25_mean", *SATELLITE_SOURCES),
    "micro_weather_satellite": (
        "pm25_mean",
        "humidity_mean",
        "temperature_mean",
        *SATELLITE_SOURCES,
    ),
}
LEARNED_FEATURE_SETS = {
    name: features for name, features in FEATURE_SETS.items() if name != "raw_micro"
}
COMPARISONS = (
    ("micro_only", "raw_micro", "micro_only_minus_raw_micro"),
    ("micro_weather", "raw_micro", "micro_weather_minus_raw_micro"),
    ("micro_satellite", "raw_micro", "micro_satellite_minus_raw_micro"),
    (
        "micro_weather_satellite",
        "raw_micro",
        "micro_weather_satellite_minus_raw_micro",
    ),
    ("micro_weather", "micro_only", "micro_weather_minus_micro_only"),
    ("micro_satellite", "micro_only", "micro_satellite_minus_micro_only"),
    (
        "micro_weather_satellite",
        "micro_weather",
        "micro_weather_satellite_minus_micro_weather",
    ),
    (
        "micro_weather_satellite",
        "micro_satellite",
        "micro_weather_satellite_minus_micro_satellite",
    ),
)
CLAIM_BOUNDARY: dict[str, object] = {
    "predictive_value_only": True,
    "held_station_primary": True,
    "satellite_context_is_reference_station_month": True,
    "satellite_context_is_micro_sensor_location": False,
    "validated_calibration": False,
    "sensor_fusion": False,
    "causal_analysis": False,
    "source_attribution": False,
    "spatial_concentration_field": False,
    "seasonal_or_drift_validation": False,
    "future_transfer": False,
    "values_imputed": False,
    "panel": "2025-01-01 through 2025-01-25 only",
}

_CONFIG_KEYS = {
    "readiness_generation_sha256",
    "benchmark_generation_sha256",
    "input_sha256",
    "date_folds",
    "station_folds",
    "satellite_sources",
    "feature_sets",
    "comparisons",
    "model",
}
_INPUT_KEYS = {
    "readiness_manifest",
    "hourly_pairs",
    "satellite_context",
    "benchmark_manifest",
    "fold_membership",
}
_MODEL_KEYS = {
    "n_estimators",
    "learning_rate",
    "num_leaves",
    "min_child_samples",
    "subsample",
    "subsample_freq",
    "colsample_bytree",
    "n_jobs",
    "seed",
}
_INPUT_VALUE_COLUMNS = (
    "pm25_mean",
    "humidity_mean",
    "temperature_mean",
    "distance_km",
    "ground_pm25",
)

_COVERAGE_SCHEMA: dict[str, Any] = {
    "source": pl.String,
    "station_rows": pl.Int64,
    "observed_stations": pl.Int64,
    "unobserved_stations": pl.Int64,
    "linked_devices_sum": pl.Int64,
}
_EXCLUSIONS_SCHEMA: dict[str, Any] = {
    "station_name": pl.String,
    "source_rows": pl.Int64,
    "devices": pl.Int64,
    "missing_sources": pl.List(pl.String),
    "reason": pl.String,
}
_FOLDS_SCHEMA: dict[str, Any] = dict(benchmark._FOLDS_SCHEMA)
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
    "humidity_mean": pl.Float64,
    "temperature_mean": pl.Float64,
    "maiac_aod": pl.Float64,
    "s5p_no2": pl.Float64,
    "s5p_so2": pl.Float64,
    "truth": pl.Float64,
    "raw_micro": pl.Float64,
    "micro_only": pl.Float64,
    "micro_weather": pl.Float64,
    "micro_satellite": pl.Float64,
    "micro_weather_satellite": pl.Float64,
}
_SCORES_SCHEMA: dict[str, Any] = dict(benchmark._SCORES_SCHEMA)
_DELTAS_SCHEMA: dict[str, Any] = dict(benchmark._DELTAS_SCHEMA)


@dataclass(frozen=True, slots=True)
class SatelliteContextModelConfig:
    n_estimators: int
    learning_rate: float
    num_leaves: int
    min_child_samples: int
    subsample: float
    subsample_freq: int
    colsample_bytree: float
    n_jobs: int
    seed: int


@dataclass(frozen=True, slots=True)
class MicroSensorSatelliteValueConfig:
    readiness_generation_sha256: str
    benchmark_generation_sha256: str
    input_sha256: dict[str, str]
    date_folds: int
    station_folds: int
    satellite_sources: tuple[str, ...]
    feature_sets: dict[str, tuple[str, ...]]
    comparisons: tuple[tuple[str, str, str], ...]
    model: SatelliteContextModelConfig


@dataclass(frozen=True, slots=True)
class MicroSensorSatelliteInputs:
    hourly_pairs: pl.DataFrame
    satellite_context: pl.DataFrame
    fold_membership: pl.DataFrame
    input_files: tuple[InputFile, ...]


@dataclass(frozen=True, slots=True)
class PreparedMicroSensorSatelliteValue:
    rows: pl.DataFrame
    coverage: pl.DataFrame
    exclusions: pl.DataFrame
    held_dates: pl.DataFrame
    held_stations: pl.DataFrame
    source_rows: int
    input_files: tuple[InputFile, ...]


@dataclass(frozen=True, slots=True)
class MicroSensorSatelliteValueEvaluation:
    folds: pl.DataFrame
    predictions: pl.DataFrame


@dataclass(frozen=True, slots=True)
class MicroSensorSatelliteValueResult:
    coverage: pl.DataFrame
    exclusions: pl.DataFrame
    folds: pl.DataFrame
    predictions: pl.DataFrame
    scores: pl.DataFrame
    deltas: pl.DataFrame
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
        raise ConfigError(f"{label} has unknown field(s) {unknown}")
    if missing:
        raise ConfigError(f"{label} is missing field(s) {missing}")


def _positive_int(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(f"{label} must be a positive integer")
    return value


def _positive_float(value: object, *, label: str, at_most_one: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{label} must be a positive finite number")
    result = float(value)
    if not math.isfinite(result) or result <= 0 or (at_most_one and result > 1):
        raise ConfigError(f"{label} must be a positive finite number")
    return result


def _identity(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ConfigError(f"{label} must be a lowercase SHA-256")
    return value


def _string_list(value: object, *, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ConfigError(f"{label} must be a list of strings")
    return tuple(value)


def load_micro_sensor_satellite_value_config(
    config: dict[str, Any] | None = None,
) -> MicroSensorSatelliteValueConfig:
    raw = config if config is not None else load_conf("micro_sensor_satellite_value")
    _exact_keys(raw, {"analysis"}, label="micro_sensor_satellite_value")
    group = _mapping(raw["analysis"], label="micro_sensor_satellite_value.analysis")
    _exact_keys(group, _CONFIG_KEYS, label="micro_sensor_satellite_value.analysis")

    date_folds = _positive_int(
        group["date_folds"], label="micro_sensor_satellite_value.analysis.date_folds"
    )
    station_folds = _positive_int(
        group["station_folds"], label="micro_sensor_satellite_value.analysis.station_folds"
    )
    if date_folds != 25:
        raise ConfigError("micro_sensor_satellite_value.analysis.date_folds must be 25")
    if station_folds != 10:
        raise ConfigError("micro_sensor_satellite_value.analysis.station_folds must be 10")
    sources = _string_list(
        group["satellite_sources"],
        label="micro_sensor_satellite_value.analysis.satellite_sources",
    )
    if sources != SATELLITE_SOURCES:
        raise ConfigError("micro_sensor_satellite_value.analysis.satellite_sources changed")

    raw_input_sha256 = _mapping(
        group["input_sha256"], label="micro_sensor_satellite_value.analysis.input_sha256"
    )
    _exact_keys(
        raw_input_sha256,
        _INPUT_KEYS,
        label="micro_sensor_satellite_value.analysis.input_sha256",
    )
    input_sha256 = {
        name: _identity(
            value,
            label=f"micro_sensor_satellite_value.analysis.input_sha256.{name}",
        )
        for name, value in raw_input_sha256.items()
    }

    raw_feature_sets = _mapping(
        group["feature_sets"], label="micro_sensor_satellite_value.analysis.feature_sets"
    )
    feature_sets = {
        name: _string_list(
            value,
            label=f"micro_sensor_satellite_value.analysis.feature_sets.{name}",
        )
        for name, value in raw_feature_sets.items()
    }
    if feature_sets != FEATURE_SETS:
        raise ConfigError("micro_sensor_satellite_value.analysis.feature_sets changed")

    raw_comparisons = group["comparisons"]
    if not isinstance(raw_comparisons, list):
        raise ConfigError("micro_sensor_satellite_value.analysis.comparisons must be a list")
    parsed_comparisons: list[tuple[str, str, str]] = []
    for index, value in enumerate(raw_comparisons):
        item = _mapping(value, label=f"micro_sensor_satellite_value.analysis.comparisons[{index}]")
        _exact_keys(
            item,
            {"candidate", "reference", "name"},
            label=f"micro_sensor_satellite_value.analysis.comparisons[{index}]",
        )
        if any(not isinstance(item[key], str) for key in ("candidate", "reference", "name")):
            raise ConfigError("micro_sensor_satellite_value.analysis.comparisons changed")
        parsed_comparisons.append((item["candidate"], item["reference"], item["name"]))
    comparisons = tuple(parsed_comparisons)
    if comparisons != COMPARISONS:
        raise ConfigError("micro_sensor_satellite_value.analysis.comparisons changed")

    raw_model = _mapping(group["model"], label="micro_sensor_satellite_value.analysis.model")
    _exact_keys(raw_model, _MODEL_KEYS, label="micro_sensor_satellite_value.analysis.model")
    model = SatelliteContextModelConfig(
        n_estimators=_positive_int(
            raw_model["n_estimators"],
            label="micro_sensor_satellite_value.analysis.model.n_estimators",
        ),
        learning_rate=_positive_float(
            raw_model["learning_rate"],
            label="micro_sensor_satellite_value.analysis.model.learning_rate",
        ),
        num_leaves=_positive_int(
            raw_model["num_leaves"],
            label="micro_sensor_satellite_value.analysis.model.num_leaves",
        ),
        min_child_samples=_positive_int(
            raw_model["min_child_samples"],
            label="micro_sensor_satellite_value.analysis.model.min_child_samples",
        ),
        subsample=_positive_float(
            raw_model["subsample"],
            label="micro_sensor_satellite_value.analysis.model.subsample",
            at_most_one=True,
        ),
        subsample_freq=_positive_int(
            raw_model["subsample_freq"],
            label="micro_sensor_satellite_value.analysis.model.subsample_freq",
        ),
        colsample_bytree=_positive_float(
            raw_model["colsample_bytree"],
            label="micro_sensor_satellite_value.analysis.model.colsample_bytree",
            at_most_one=True,
        ),
        n_jobs=_positive_int(
            raw_model["n_jobs"], label="micro_sensor_satellite_value.analysis.model.n_jobs"
        ),
        seed=_positive_int(
            raw_model["seed"], label="micro_sensor_satellite_value.analysis.model.seed"
        ),
    )
    expected_model = SatelliteContextModelConfig(200, 0.05, 31, 10, 0.8, 1, 0.8, 1, 20260812)
    if model.n_jobs != 1:
        raise ConfigError("micro-sensor satellite-value models must run with n_jobs=1")
    if model != expected_model:
        raise ConfigError("micro_sensor_satellite_value.analysis.model changed")
    return MicroSensorSatelliteValueConfig(
        readiness_generation_sha256=_identity(
            group["readiness_generation_sha256"],
            label="micro_sensor_satellite_value.analysis.readiness_generation_sha256",
        ),
        benchmark_generation_sha256=_identity(
            group["benchmark_generation_sha256"],
            label="micro_sensor_satellite_value.analysis.benchmark_generation_sha256",
        ),
        input_sha256=input_sha256,
        date_folds=date_folds,
        station_folds=station_folds,
        satellite_sources=sources,
        feature_sets=feature_sets,
        comparisons=comparisons,
        model=model,
    )


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


def _file_identity(path: Path) -> InputFile:
    if not path.is_file():
        raise RuntimeError(f"micro-sensor satellite-value input is missing: {path}")
    return InputFile(path=path, bytes=path.stat().st_size, sha256=sha256_file(path))


def _read_json_stable(path: Path) -> tuple[dict[str, Any], InputFile]:
    before = _file_identity(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"micro-sensor satellite-value JSON is unreadable: {path.name}") from exc
    after = _file_identity(path)
    if before != after:
        raise RuntimeError(f"micro-sensor satellite-value input changed while read: {path.name}")
    if not isinstance(value, dict):
        raise RuntimeError(f"micro-sensor satellite-value JSON must be an object: {path.name}")
    return value, before


def _read_parquet_stable(path: Path) -> tuple[pl.DataFrame, InputFile]:
    before = _file_identity(path)
    try:
        frame = pl.read_parquet(path)
    except (OSError, pl.exceptions.PolarsError) as exc:
        raise RuntimeError(
            f"micro-sensor satellite-value Parquet is unreadable: {path.name}"
        ) from exc
    after = _file_identity(path)
    if before != after:
        raise RuntimeError(f"micro-sensor satellite-value input changed while read: {path.name}")
    return frame, before


def _validate_readiness_manifest(manifest: dict[str, Any], expected_identity: str) -> None:
    if (
        manifest.get("schema_version") != 1
        or manifest.get("analysis") != "micro_sensor_calibration_readiness"
        or manifest.get("complete") is not True
        or manifest.get("panel_dates") != 25
        or manifest.get("output_identity_sha256") != expected_identity
        or manifest.get("claim_boundary") != readiness._CLAIM_BOUNDARY
    ):
        raise RuntimeError("micro-sensor readiness manifest contract changed")
    config = manifest.get("config")
    if not isinstance(config, dict) or manifest.get("config_sha256") != readiness._hash_value(
        config
    ):
        raise RuntimeError("micro-sensor readiness config identity changed")
    try:
        payload = {
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
    if readiness._hash_value(payload) != expected_identity:
        raise RuntimeError("micro-sensor readiness generation identity changed")


def _validate_benchmark_manifest(
    manifest: dict[str, Any],
    expected_identity: str,
    readiness_identity: str,
) -> None:
    if (
        manifest.get("schema_version") != 1
        or manifest.get("analysis") != "micro_sensor_calibration_benchmark"
        or manifest.get("complete") is not True
        or manifest.get("output_identity_sha256") != expected_identity
        or manifest.get("readiness_generation_sha256") != readiness_identity
        or manifest.get("claim_boundary") != benchmark.CLAIM_BOUNDARY
    ):
        raise RuntimeError("grouped benchmark readiness generation or manifest contract changed")
    config = manifest.get("config")
    if not isinstance(config, dict) or manifest.get("config_sha256") != benchmark._hash_json(
        config
    ):
        raise RuntimeError("grouped benchmark config identity changed")
    try:
        payload = {
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
    except KeyError as exc:
        raise RuntimeError("grouped benchmark manifest identity is incomplete") from exc
    if benchmark._hash_json(payload) != expected_identity:
        raise RuntimeError("grouped benchmark generation identity changed")


def _eligible_rows(rows: pl.DataFrame, config: MicroSensorSatelliteValueConfig) -> pl.DataFrame:
    if dict(rows.schema) != readiness._RESULT_SCHEMAS["hourly_pairs"]:
        raise RuntimeError("micro-sensor readiness hourly_pairs schema changed")
    duplicated = rows.group_by("device_id", "hour").len().filter(pl.col("len") > 1)
    if not duplicated.is_empty():
        raise RuntimeError("micro-sensor readiness has duplicate device-hour keys")
    if rows["eligibility_reason"].null_count() != 0:
        raise RuntimeError("micro-sensor readiness eligibility reasons contain null")
    eligible = rows.filter(pl.col("eligibility_reason") == "eligible")
    if eligible.is_empty():
        raise RuntimeError("micro-sensor satellite-value has no eligible source rows")
    invalid = eligible.filter(
        pl.any_horizontal(
            *(
                pl.col(column).is_null() | ~pl.col(column).is_finite()
                for column in _INPUT_VALUE_COLUMNS
            )
        )
        | pl.col("device_id").is_null()
        | pl.col("hour").is_null()
        | pl.col("station_name").is_null()
        | pl.col("ground_eligible").is_null()
        | ~pl.col("ground_eligible")
    )
    if not invalid.is_empty():
        raise RuntimeError("eligible micro-sensor values contain null, non-finite or invalid state")
    eligible = eligible.with_columns(pl.col("hour").dt.date().alias("date"))
    dates = eligible.select("date").unique().sort("date")["date"].to_list()
    expected_dates = [date(2025, 1, 1 + index) for index in range(config.date_folds)]
    if dates != expected_dates:
        raise RuntimeError("eligible micro-sensor dates changed from 2025-01-01 through 2025-01-25")
    return eligible


def _validate_satellite_context(
    satellite: pl.DataFrame,
    eligible: pl.DataFrame,
    config: MicroSensorSatelliteValueConfig,
) -> None:
    if dict(satellite.schema) != readiness._RESULT_SCHEMAS["satellite_context"]:
        raise RuntimeError("micro-sensor readiness satellite_context schema changed")
    duplicated = satellite.group_by("station_name", "source").len().filter(pl.col("len") > 1)
    if not duplicated.is_empty():
        raise RuntimeError("satellite context has duplicate station-source keys")
    if set(satellite["source"].unique()) != set(config.satellite_sources):
        raise RuntimeError("satellite context source inventory changed")
    eligible_stations = set(eligible["station_name"].unique())
    satellite_stations = set(satellite["station_name"].unique())
    counts = satellite.group_by("station_name").agg(pl.col("source").n_unique().alias("sources"))
    if satellite_stations != eligible_stations or counts["sources"].unique().to_list() != [
        len(config.satellite_sources)
    ]:
        raise RuntimeError("satellite context does not cover every eligible reference station")
    contradictory = satellite.filter(
        pl.col("satellite_observed").is_null()
        | pl.col("pair_observed").is_null()
        | pl.col("linked_devices").is_null()
        | (pl.col("linked_devices") < 0)
        | (pl.col("satellite_observed") & pl.col("satellite_value").is_null())
        | (~pl.col("satellite_observed") & pl.col("satellite_value").is_not_null())
        | (pl.col("satellite_value").is_not_null() & ~pl.col("satellite_value").is_finite())
    )
    if not contradictory.is_empty():
        raise RuntimeError("satellite observation flags and values disagree")


def _validate_fold_membership(
    membership: pl.DataFrame,
    eligible: pl.DataFrame,
    config: MicroSensorSatelliteValueConfig,
) -> None:
    if dict(membership.schema) != benchmark._FOLD_MEMBERSHIP_SCHEMA:
        raise RuntimeError("grouped benchmark fold_membership schema changed")
    if set(membership["fold_kind"].unique()) != {"held_date", "held_station"}:
        raise RuntimeError("grouped benchmark fold kinds changed")
    if membership.select("fold_kind", "group").is_duplicated().any():
        raise RuntimeError("grouped benchmark fold groups are duplicated")
    core = ("fold_kind", "group", "fold", "fold_index")
    if any(membership[column].null_count() for column in core):
        raise RuntimeError("grouped benchmark fold membership contains null")
    held_dates = membership.filter(pl.col("fold_kind") == "held_date").sort("fold_index")
    expected_dates = eligible.select("date").unique().sort("date")["date"].cast(pl.String).to_list()
    if (
        held_dates.height != config.date_folds
        or held_dates["group"].to_list() != expected_dates
        or held_dates["fold_index"].to_list() != list(range(config.date_folds))
    ):
        raise RuntimeError("grouped benchmark held-date folds changed")
    held_stations = membership.filter(pl.col("fold_kind") == "held_station")
    expected_stations = set(eligible["station_name"].unique())
    if set(held_stations["group"]) != expected_stations:
        raise RuntimeError("an eligible reference station is absent from grouped folds")
    if (
        held_stations["fold_index"].n_unique() != config.station_folds
        or sorted(held_stations["fold_index"].unique().to_list())
        != list(range(config.station_folds))
        or held_stations.group_by("fold_index")
        .agg(pl.col("fold").n_unique().alias("names"))["names"]
        .unique()
        .to_list()
        != [1]
    ):
        raise RuntimeError("grouped benchmark held-station folds changed")


def load_micro_sensor_satellite_inputs(
    *,
    data_root: Path | None = None,
    config: MicroSensorSatelliteValueConfig | None = None,
) -> MicroSensorSatelliteInputs:
    root = data_root or configured_data_root()
    selected = config or load_micro_sensor_satellite_value_config()
    readiness_dir = (
        root
        / "outputs"
        / "micro_sensor_calibration_readiness"
        / "generations"
        / selected.readiness_generation_sha256
    )
    benchmark_dir = (
        root
        / "outputs"
        / "micro_sensor_calibration_benchmark"
        / "generations"
        / selected.benchmark_generation_sha256
    )
    readiness_manifest, readiness_file = _read_json_stable(readiness_dir / "manifest.json")
    benchmark_manifest, benchmark_file = _read_json_stable(benchmark_dir / "manifest.json")
    _validate_readiness_manifest(readiness_manifest, selected.readiness_generation_sha256)
    _validate_benchmark_manifest(
        benchmark_manifest,
        selected.benchmark_generation_sha256,
        selected.readiness_generation_sha256,
    )
    hourly, hourly_file = _read_parquet_stable(readiness_dir / "hourly_pairs.parquet")
    satellite, satellite_file = _read_parquet_stable(readiness_dir / "satellite_context.parquet")
    membership, membership_file = _read_parquet_stable(benchmark_dir / "fold_membership.parquet")
    observed_input_sha256 = {
        "readiness_manifest": readiness_file.sha256,
        "hourly_pairs": hourly_file.sha256,
        "satellite_context": satellite_file.sha256,
        "benchmark_manifest": benchmark_file.sha256,
        "fold_membership": membership_file.sha256,
    }
    if observed_input_sha256 != selected.input_sha256:
        raise RuntimeError("micro-sensor satellite-value immutable input identity changed")
    readiness_rows = readiness_manifest.get("output_rows")
    benchmark_rows = benchmark_manifest.get("output_rows")
    if (
        not isinstance(readiness_rows, dict)
        or readiness_rows.get("hourly_pairs") != hourly.height
        or readiness_rows.get("satellite_context") != satellite.height
    ):
        raise RuntimeError("micro-sensor readiness output row counts changed")
    if (
        not isinstance(benchmark_rows, dict)
        or benchmark_rows.get("fold_membership") != membership.height
    ):
        raise RuntimeError("grouped benchmark fold row count changed")
    eligible = _eligible_rows(hourly, selected)
    _validate_satellite_context(satellite, eligible, selected)
    _validate_fold_membership(membership, eligible, selected)
    return MicroSensorSatelliteInputs(
        hourly_pairs=hourly,
        satellite_context=satellite,
        fold_membership=membership,
        input_files=tuple(
            sorted(
                (readiness_file, hourly_file, satellite_file, benchmark_file, membership_file),
                key=lambda item: item.path.as_posix(),
            )
        ),
    )


def _coverage(satellite: pl.DataFrame) -> pl.DataFrame:
    return (
        satellite.group_by("source")
        .agg(
            pl.len().cast(pl.Int64).alias("station_rows"),
            pl.col("satellite_observed").sum().cast(pl.Int64).alias("observed_stations"),
            (~pl.col("satellite_observed")).sum().cast(pl.Int64).alias("unobserved_stations"),
            pl.col("linked_devices").sum().cast(pl.Int64).alias("linked_devices_sum"),
        )
        .select(*_COVERAGE_SCHEMA)
        .sort("source")
    )


def _complete_satellite_stations(satellite: pl.DataFrame) -> pl.DataFrame:
    observed = satellite.filter(pl.col("satellite_observed")).select(
        "station_name", "source", "satellite_value"
    )
    complete_names = (
        observed.group_by("station_name")
        .agg(pl.col("source").n_unique().alias("sources"))
        .filter(pl.col("sources") == len(SATELLITE_SOURCES))
        .select("station_name")
    )
    return (
        observed.join(complete_names, on="station_name", how="semi")
        .pivot(on="source", index="station_name", values="satellite_value")
        .select("station_name", *SATELLITE_SOURCES)
        .sort("station_name")
    )


def _exclusions(
    eligible: pl.DataFrame,
    satellite: pl.DataFrame,
    complete: pl.DataFrame,
) -> pl.DataFrame:
    excluded = eligible.join(complete.select("station_name"), on="station_name", how="anti")
    if excluded.is_empty():
        return pl.DataFrame(schema=_EXCLUSIONS_SCHEMA)
    rows: list[dict[str, object]] = []
    for station in sorted(excluded["station_name"].unique().to_list()):
        station_rows = excluded.filter(pl.col("station_name") == station)
        missing = (
            satellite.filter((pl.col("station_name") == station) & ~pl.col("satellite_observed"))[
                "source"
            ]
            .sort()
            .to_list()
        )
        if not missing:
            raise RuntimeError("an excluded station has no explicit unobserved satellite source")
        rows.append(
            {
                "station_name": station,
                "source_rows": station_rows.height,
                "devices": station_rows["device_id"].n_unique(),
                "missing_sources": missing,
                "reason": "one or more satellite sources unobserved",
            }
        )
    return pl.DataFrame(rows, schema=_EXCLUSIONS_SCHEMA).sort("station_name")


def prepare_micro_sensor_satellite_value(
    inputs: MicroSensorSatelliteInputs,
    config: MicroSensorSatelliteValueConfig,
) -> PreparedMicroSensorSatelliteValue:
    eligible = _eligible_rows(inputs.hourly_pairs, config)
    _validate_satellite_context(inputs.satellite_context, eligible, config)
    _validate_fold_membership(inputs.fold_membership, eligible, config)
    complete = _complete_satellite_stations(inputs.satellite_context)
    coverage = _coverage(inputs.satellite_context)
    exclusions = _exclusions(eligible, inputs.satellite_context, complete)
    cohort = eligible.join(complete, on="station_name", how="inner", validate="m:1")
    if cohort.is_empty():
        raise RuntimeError("no satellite-complete micro-sensor rows remain")
    invalid = cohort.filter(
        pl.any_horizontal(
            *(
                pl.col(column).is_null() | ~pl.col(column).is_finite()
                for column in (*_INPUT_VALUE_COLUMNS, *SATELLITE_SOURCES)
            )
        )
    )
    if not invalid.is_empty():
        raise RuntimeError("satellite-complete cohort contains null or non-finite values")
    held_dates = inputs.fold_membership.filter(pl.col("fold_kind") == "held_date").sort(
        "fold_index"
    )
    held_stations = inputs.fold_membership.filter(pl.col("fold_kind") == "held_station").sort(
        "fold_index", "group"
    )
    station_mapping = held_stations.select(
        pl.col("group").alias("station_name"),
        pl.col("fold_index").alias("station_fold"),
    )
    absent = (
        cohort.select("station_name")
        .unique()
        .join(station_mapping.select("station_name"), on="station_name", how="anti")
    )
    if not absent.is_empty():
        raise RuntimeError("a satellite-complete station is absent from grouped folds")
    cohort = cohort.join(station_mapping, on="station_name", how="left", validate="m:1").sort(
        "device_id", "hour"
    )
    fold_counts = cohort.group_by("station_fold").agg(
        pl.col("station_name").n_unique().alias("stations")
    )
    minimum_stations = fold_counts["stations"].min()
    if (
        cohort["station_fold"].null_count()
        or fold_counts.height != config.station_folds
        or not isinstance(minimum_stations, int)
        or minimum_stations < 1
    ):
        raise RuntimeError("satellite-complete held-station fold coverage changed")
    return PreparedMicroSensorSatelliteValue(
        rows=cohort,
        coverage=coverage,
        exclusions=exclusions,
        held_dates=held_dates,
        held_stations=held_stations,
        source_rows=eligible.height,
        input_files=inputs.input_files,
    )


def _membership_sha256(rows: pl.DataFrame) -> str:
    return _hash_json(rows.select("device_id", "hour").sort("device_id", "hour").to_dicts())


def _fit_predict(
    train: pl.DataFrame,
    test: pl.DataFrame,
    features: tuple[str, ...],
    model: SatelliteContextModelConfig,
) -> np.ndarray:
    import lightgbm as lgb

    estimator = lgb.LGBMRegressor(
        n_estimators=model.n_estimators,
        learning_rate=model.learning_rate,
        num_leaves=model.num_leaves,
        min_child_samples=model.min_child_samples,
        subsample=model.subsample,
        subsample_freq=model.subsample_freq,
        colsample_bytree=model.colsample_bytree,
        random_state=model.seed,
        n_jobs=model.n_jobs,
        verbose=-1,
    )
    estimator.fit(train.select(features).to_numpy(), train["ground_pm25"].to_numpy())
    return np.asarray(estimator.predict(test.select(features).to_numpy()), dtype=float)


def _evaluate_fold(
    *,
    evaluation: str,
    fold: str,
    fold_index: int,
    train: pl.DataFrame,
    test: pl.DataFrame,
    config: MicroSensorSatelliteValueConfig,
) -> tuple[dict[str, object], pl.DataFrame]:
    if train.is_empty() or test.is_empty():
        raise RuntimeError(f"micro-sensor satellite-value fold {fold} has no train or test rows")
    train_keys = set(train.select("device_id", "hour").iter_rows())
    test_keys = set(test.select("device_id", "hour").iter_rows())
    if train_keys & test_keys:
        raise RuntimeError(f"micro-sensor satellite-value fold {fold} leaks source rows")
    if evaluation == "held_date" and set(train["date"]) & set(test["date"]):
        raise RuntimeError(f"micro-sensor satellite-value fold {fold} leaks dates")
    if evaluation == "held_station" and set(train["station_name"]) & set(test["station_name"]):
        raise RuntimeError(f"micro-sensor satellite-value fold {fold} leaks stations")
    predictions: dict[str, np.ndarray] = {
        "raw_micro": np.asarray(test["pm25_mean"].to_numpy(), dtype=float)
    }
    for name, features in LEARNED_FEATURE_SETS.items():
        prediction = _fit_predict(train, test, features, config.model)
        if prediction.shape != (test.height,) or not np.isfinite(prediction).all():
            raise RuntimeError(
                f"micro-sensor satellite-value {name} predictions are missing or non-finite"
            )
        predictions[name] = prediction
    frame = (
        test.select(
            "device_id",
            "hour",
            "date",
            "station_name",
            "distance_km",
            "pm25_mean",
            "humidity_mean",
            "temperature_mean",
            *SATELLITE_SOURCES,
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
    description = {
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
    return description, frame


def evaluate_micro_sensor_satellite_value(
    prepared: PreparedMicroSensorSatelliteValue,
    config: MicroSensorSatelliteValueConfig,
) -> MicroSensorSatelliteValueEvaluation:
    fold_rows: list[dict[str, object]] = []
    prediction_frames: list[pl.DataFrame] = []
    for item in prepared.held_dates.iter_rows(named=True):
        held_date = date.fromisoformat(str(item["group"]))
        description, frame = _evaluate_fold(
            evaluation="held_date",
            fold=str(item["fold"]),
            fold_index=int(item["fold_index"]),
            train=prepared.rows.filter(pl.col("date") != held_date),
            test=prepared.rows.filter(pl.col("date") == held_date),
            config=config,
        )
        fold_rows.append(description)
        prediction_frames.append(frame)
    station_folds = prepared.held_stations.select("fold", "fold_index").unique().sort("fold_index")
    for item in station_folds.iter_rows(named=True):
        fold_index = int(item["fold_index"])
        description, frame = _evaluate_fold(
            evaluation="held_station",
            fold=str(item["fold"]),
            fold_index=fold_index,
            train=prepared.rows.filter(pl.col("station_fold") != fold_index),
            test=prepared.rows.filter(pl.col("station_fold") == fold_index),
            config=config,
        )
        fold_rows.append(description)
        prediction_frames.append(frame)
    folds = pl.DataFrame(fold_rows, schema=_FOLDS_SCHEMA).sort("evaluation", "fold_index")
    predictions = pl.concat(prediction_frames).sort("evaluation", "fold_index", "device_id", "hour")
    tested = predictions.group_by("evaluation", "device_id", "hour").len()
    if tested.height != prepared.rows.height * 2 or tested["len"].unique().to_list() != [1]:
        raise RuntimeError("micro-sensor satellite-value did not test each cohort row once")
    return MicroSensorSatelliteValueEvaluation(folds=folds, predictions=predictions)


def _metric_values(truth: np.ndarray, prediction: np.ndarray) -> dict[str, float]:
    if truth.shape != prediction.shape or truth.size == 0:
        raise RuntimeError("micro-sensor satellite-value metric arrays are empty or unpaired")
    if not np.isfinite(truth).all() or not np.isfinite(prediction).all():
        raise RuntimeError("micro-sensor satellite-value metric arrays contain non-finite values")
    residual = prediction - truth
    sst = float(np.sum((truth - float(np.mean(truth))) ** 2))
    if sst == 0:
        raise RuntimeError("micro-sensor satellite-value metric target is constant")
    return {
        "rmse": float(np.sqrt(np.mean(residual**2))),
        "mae": float(np.mean(np.abs(residual))),
        "r2": 1.0 - float(np.sum(residual**2)) / sst,
        "bias": float(np.mean(residual)),
    }


def score_micro_sensor_satellite_predictions(predictions: pl.DataFrame) -> pl.DataFrame:
    if dict(predictions.schema) != _PREDICTIONS_SCHEMA:
        raise RuntimeError("micro-sensor satellite-value predictions schema changed")
    score_rows: list[dict[str, object]] = []
    for group in predictions.partition_by("evaluation", "fold", "fold_index", maintain_order=True):
        identity = {
            "evaluation": str(group["evaluation"][0]),
            "fold": str(group["fold"][0]),
            "fold_index": int(group["fold_index"][0]),
        }
        station_hour = group.group_by("station_name", "hour").agg(
            pl.col("truth").n_unique().alias("truth_values"),
            pl.col("truth").first(),
            *(pl.col(name).mean() for name in FEATURE_SETS),
        )
        if station_hour.filter(pl.col("truth_values") != 1).height:
            raise RuntimeError("micro-sensor satellite-value ground targets disagree")
        for evaluation_unit, frame in (
            ("device_hour", group),
            ("reference_station_hour", station_hour),
        ):
            truth = np.asarray(frame["truth"].to_numpy(), dtype=float)
            for feature_set in FEATURE_SETS:
                score_rows.append(
                    {
                        **identity,
                        "evaluation_unit": evaluation_unit,
                        "feature_set": feature_set,
                        "n": frame.height,
                        **_metric_values(
                            truth,
                            np.asarray(frame[feature_set].to_numpy(), dtype=float),
                        ),
                    }
                )
    return pl.DataFrame(score_rows, schema=_SCORES_SCHEMA).sort(
        "evaluation", "fold_index", "evaluation_unit", "feature_set"
    )


def micro_sensor_satellite_value_deltas(
    scores: pl.DataFrame,
    config: MicroSensorSatelliteValueConfig,
) -> pl.DataFrame:
    if dict(scores.schema) != _SCORES_SCHEMA:
        raise RuntimeError("micro-sensor satellite-value scores schema changed")
    rows: list[dict[str, object]] = []
    columns = ("evaluation", "fold", "fold_index", "evaluation_unit")
    for group in scores.partition_by(*columns, maintain_order=True):
        indexed = {str(row["feature_set"]): row for row in group.iter_rows(named=True)}
        if set(indexed) != set(config.feature_sets):
            raise RuntimeError("micro-sensor satellite-value scores omit a feature set")
        identity = {column: group[column][0] for column in columns}
        for candidate_name, reference_name, comparison in config.comparisons:
            candidate = indexed[candidate_name]
            reference = indexed[reference_name]
            if candidate["n"] != reference["n"]:
                raise RuntimeError("micro-sensor satellite-value comparison is not paired")
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
    prepared: PreparedMicroSensorSatelliteValue,
    evaluation: MicroSensorSatelliteValueEvaluation,
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
            "rmse_improved_folds": int(subset["rmse_improved"].sum()),
            "r2_improved_folds": int(subset["r2_improved"].sum()),
        }
    return {
        "source_rows": prepared.source_rows,
        "cohort_rows": prepared.rows.height,
        "excluded_rows": int(prepared.exclusions["source_rows"].sum())
        if not prepared.exclusions.is_empty()
        else 0,
        "devices": prepared.rows["device_id"].n_unique(),
        "reference_stations": prepared.rows["station_name"].n_unique(),
        "dates": prepared.rows["date"].n_unique(),
        "folds": evaluation.folds.height,
        "prediction_rows": evaluation.predictions.height,
        "score_rows": scores.height,
        "delta_rows": deltas.height,
        "comparisons": dict(sorted(comparisons.items())),
    }


def _rehash_inputs(files: tuple[InputFile, ...]) -> tuple[InputFile, ...]:
    return tuple(_file_identity(item.path) for item in files)


def run_micro_sensor_satellite_value(
    *,
    data_root: Path | None = None,
    config: MicroSensorSatelliteValueConfig | None = None,
    generated_at: str | None = None,
    git_sha: str | None = None,
    git_dirty: bool | None = None,
) -> MicroSensorSatelliteValueResult:
    root = data_root or configured_data_root()
    selected = config or load_micro_sensor_satellite_value_config()
    inputs = load_micro_sensor_satellite_inputs(data_root=root, config=selected)
    prepared = prepare_micro_sensor_satellite_value(inputs, selected)
    before_files = prepared.input_files
    evaluation = evaluate_micro_sensor_satellite_value(prepared, selected)
    scores = score_micro_sensor_satellite_predictions(evaluation.predictions)
    deltas = micro_sensor_satellite_value_deltas(scores, selected)
    if before_files != _rehash_inputs(before_files):
        raise RuntimeError("micro-sensor satellite-value input changed during analysis")
    measured_sha, measured_dirty = git_state()
    resolved_sha = git_sha if git_sha is not None else measured_sha
    resolved_dirty = git_dirty if git_dirty is not None else measured_dirty
    config_payload = asdict(selected)
    output_rows = {
        "coverage": prepared.coverage.height,
        "exclusions": prepared.exclusions.height,
        "folds": evaluation.folds.height,
        "predictions": evaluation.predictions.height,
        "scores": scores.height,
        "deltas": deltas.height,
    }
    summary = _summary(prepared, evaluation, scores, deltas)
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "analysis": "micro_sensor_satellite_value",
        "complete": True,
        "generated_at": generated_at or datetime.now(UTC).isoformat(timespec="seconds"),
        "readiness_generation_sha256": selected.readiness_generation_sha256,
        "benchmark_generation_sha256": selected.benchmark_generation_sha256,
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
            "benchmark_generation_sha256",
            "config_sha256",
            "inputs",
            "output_rows",
            "claim_boundary",
            "git_sha",
            "git_dirty",
        )
    }
    manifest["output_identity_sha256"] = _hash_json(identity_payload)
    return MicroSensorSatelliteValueResult(
        coverage=prepared.coverage,
        exclusions=prepared.exclusions,
        folds=evaluation.folds,
        predictions=evaluation.predictions,
        scores=scores,
        deltas=deltas,
        summary=summary,
        manifest=manifest,
    )


def micro_sensor_satellite_value_dir(*, identity: str) -> Path:
    if _SHA256.fullmatch(identity) is None:
        raise ValueError("micro-sensor satellite-value identity must be a lowercase SHA-256")
    return outputs_dir("micro_sensor_satellite_value") / "generations" / identity


def _validate_result(result: MicroSensorSatelliteValueResult) -> None:
    members = {
        "coverage": result.coverage,
        "exclusions": result.exclusions,
        "folds": result.folds,
        "predictions": result.predictions,
        "scores": result.scores,
        "deltas": result.deltas,
    }
    schemas = {
        "coverage": _COVERAGE_SCHEMA,
        "exclusions": _EXCLUSIONS_SCHEMA,
        "folds": _FOLDS_SCHEMA,
        "predictions": _PREDICTIONS_SCHEMA,
        "scores": _SCORES_SCHEMA,
        "deltas": _DELTAS_SCHEMA,
    }
    for name, frame in members.items():
        if dict(frame.schema) != schemas[name]:
            raise RuntimeError(f"micro-sensor satellite-value {name} schema changed")
    rows = {name: frame.height for name, frame in members.items()}
    manifest = result.manifest
    if (
        manifest.get("schema_version") != 1
        or manifest.get("analysis") != "micro_sensor_satellite_value"
        or manifest.get("complete") is not True
        or manifest.get("output_rows") != rows
        or manifest.get("claim_boundary") != CLAIM_BOUNDARY
    ):
        raise RuntimeError("micro-sensor satellite-value manifest contract changed")
    config = manifest.get("config")
    if not isinstance(config, dict) or manifest.get("config_sha256") != _hash_json(config):
        raise RuntimeError("micro-sensor satellite-value config identity changed")
    identity_payload = {
        key: manifest[key]
        for key in (
            "schema_version",
            "analysis",
            "readiness_generation_sha256",
            "benchmark_generation_sha256",
            "config_sha256",
            "inputs",
            "output_rows",
            "claim_boundary",
            "git_sha",
            "git_dirty",
        )
    }
    if manifest.get("output_identity_sha256") != _hash_json(identity_payload):
        raise RuntimeError("micro-sensor satellite-value output identity changed")
    if (
        result.summary.get("cohort_rows") != result.predictions.height // 2
        or result.summary.get("prediction_rows") != result.predictions.height
    ):
        raise RuntimeError("micro-sensor satellite-value summary row counts changed")
    try:
        json.dumps(result.summary, ensure_ascii=False, allow_nan=False)
        json.dumps(manifest, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("micro-sensor satellite-value metadata is not finite JSON") from exc


def _recover_swap(destination: Path) -> None:
    parent = destination.parent
    if not parent.exists():
        return
    backups = sorted(parent.glob(f".{destination.name}.backup-*"))
    stages = sorted(parent.glob(f".{destination.name}.staging-*"))
    if len(backups) > 1 or len(stages) > 1:
        raise RuntimeError(f"multiple interrupted satellite-value swaps beside {destination}")
    if destination.exists() and backups and stages:
        raise RuntimeError(f"ambiguous interrupted satellite-value swap beside {destination}")
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


def write_micro_sensor_satellite_value_result(
    result: MicroSensorSatelliteValueResult,
    *,
    destination: Path | None = None,
) -> dict[str, Path]:
    _validate_result(result)
    identity = result.manifest.get("output_identity_sha256")
    if not isinstance(identity, str):
        raise RuntimeError("micro-sensor satellite-value has no output identity")
    out = destination or micro_sensor_satellite_value_dir(identity=identity)
    _recover_swap(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    token = uuid4().hex
    staged = out.with_name(f".{out.name}.staging-{token}")
    backup = out.with_name(f".{out.name}.backup-{token}")
    staged.mkdir()
    had_existing = out.exists()
    try:
        result.coverage.write_parquet(staged / "coverage.parquet")
        result.exclusions.write_parquet(staged / "exclusions.parquet")
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
        "coverage": out / "coverage.parquet",
        "exclusions": out / "exclusions.parquet",
        "folds": out / "folds.parquet",
        "predictions": out / "predictions.parquet",
        "scores": out / "scores.parquet",
        "deltas": out / "deltas.parquet",
        "summary": out / "summary.json",
        "manifest": out / "manifest.json",
    }
