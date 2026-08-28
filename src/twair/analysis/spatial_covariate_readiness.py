"""Freeze the reviewed inputs for the spatial covariate readiness gate."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
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
    "load_frozen_inputs",
    "load_spatial_covariate_readiness_config",
]


COVARIATE_READINESS_METHODS = ("idw2", "covariate_gbm", "covariate_gbm_idw2")
COVARIATE_READINESS_EVALUATIONS = ("buffer_20km", "buffer_40km", "spatial_cluster")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_EXPECTED_MODEL = ModelConfig(
    n_estimators=200,
    learning_rate=0.05,
    num_leaves=31,
    min_child_samples=10,
    n_jobs=1,
    seed=20260811,
)


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
    if (
        observed.filter(
            pl.col("mean").is_null() | ~pl.col("mean").is_finite() | ~pl.col("meets_threshold")
        ).height
        or withheld.filter(pl.col("mean").is_not_null() | pl.col("meets_threshold")).height
    ):
        raise RuntimeError("spatial covariate baseline target-state values changed")
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
