"""Independently verify one immutable spatial-covariate readiness generation."""

from __future__ import annotations

import argparse
import json
import math
import re
import stat
from collections.abc import Sequence
from datetime import date, datetime
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any

import numpy as np
import polars as pl

from twair.analysis.spatial_covariate_readiness import (
    COVARIATE_READINESS_EVALUATIONS,
    COVARIATE_READINESS_LIMITATIONS,
    COVARIATE_READINESS_MEMBER_NAMES,
    COVARIATE_READINESS_METHODS,
    COVARIATE_READINESS_TABLE_ORDER,
    COVARIATE_READINESS_TABLE_SCHEMAS,
)
from twair.analysis.spatial_surface_baseline import (
    SPATIAL_BASELINE_MEMBER_NAMES,
    SPATIAL_BASELINE_TABLE_SCHEMAS,
)

_SCHEMA_VERSION = 1
_BASELINE_GENERATION = "620b7ba088906611c191d0f371b5405f8096059cefc488306b6849b64588ef0f"
_INVENTORY_GENERATION = "58e00bb5ab951c9afd1a95e9e98aacdab4e90762e32904ca6d79d198efe6d788"
_TARGET_KEY = (
    "evaluation",
    "training_period",
    "train_year",
    "target_year",
    "month",
    "target_station",
)
_CELL_KEY = ("evaluation", "training_period", "train_year", "target_year")
_FOLD_COLUMNS = tuple(COVARIATE_READINESS_TABLE_SCHEMAS["folds"])
_TABLE_NAMES = tuple(COVARIATE_READINESS_TABLE_SCHEMAS)
_CANDIDATES = tuple(method for method in COVARIATE_READINESS_METHODS if method != "idw2")
_IDENTITY_SCOPE = (
    "float-bearing output hashes and generation identity record one run; "
    "they are not cross-hardware identities"
)
_RULE = (
    "complete predictions and median station MAE delta < 0 versus idw2 "
    "in 2024/2025 same-year 20/40 km and all 2024-to-2025 joint cells"
)
_MANIFEST_KEYS = {
    "schema_version",
    "analysis",
    "config",
    "inputs",
    "baseline_generation_sha256",
    "station_inventory_generation_sha256",
    "tables",
    "gate",
    "claim_boundary",
    "identity_scope",
    "git_sha",
    "git_dirty",
    "members",
    "generated_at",
    "complete",
    "generation_sha256",
}
_GIT_SHA = re.compile(r"[0-9a-f]{40}")
_FAILURE_TYPE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_BASELINE_MEMBERS = (
    "stations.parquet",
    "panel.parquet",
    "support.parquet",
    "folds.parquet",
)


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


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")


def _strict_json(payload: bytes) -> object:
    return json.loads(payload.decode("utf-8"), parse_constant=_reject_json_constant)


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


def _file_identity(path: Path) -> dict[str, object]:
    payload = path.read_bytes()
    return {"bytes": len(payload), "sha256": sha256(payload).hexdigest()}


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


def _normalized_table(name: str, frame: pl.DataFrame) -> pl.DataFrame:
    schema = COVARIATE_READINESS_TABLE_SCHEMAS[name]
    return (
        frame.select(*schema)
        .cast(pl.Schema(schema), strict=True)
        .sort(*COVARIATE_READINESS_TABLE_ORDER[name])
    )


def _table_identity(name: str, frame: pl.DataFrame) -> dict[str, object]:
    normalized = _normalized_table(name, frame)
    payload = _canonical_json_bytes(
        {
            "schema": [[column, str(dtype)] for column, dtype in normalized.schema.items()],
            "order": list(COVARIATE_READINESS_TABLE_ORDER[name]),
            "rows": [[_table_scalar(value) for value in row] for row in normalized.iter_rows()],
        }
    )
    return {
        "rows": normalized.height,
        "bytes": len(payload),
        "sha256": sha256(payload).hexdigest(),
        "schema": {column: str(dtype) for column, dtype in normalized.schema.items()},
        "order": list(COVARIATE_READINESS_TABLE_ORDER[name]),
    }


def _target_key(row: dict[str, Any]) -> tuple[object, ...]:
    return tuple(row[column] for column in _TARGET_KEY)


def _cell(row: dict[str, Any]) -> tuple[str, str, int, int]:
    return (
        str(row["evaluation"]),
        str(row["training_period"]),
        int(row["train_year"]),
        int(row["target_year"]),
    )


def _values_equal(left: object, right: object) -> bool:
    if left is None or right is None:
        return left is right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        if math.isnan(float(left)) or math.isnan(float(right)):
            return math.isnan(float(left)) and math.isnan(float(right))
        return math.isclose(float(left), float(right), rel_tol=1e-12, abs_tol=1e-12)
    if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        return len(left) == len(right) and all(
            _values_equal(left_value, right_value)
            for left_value, right_value in zip(left, right, strict=True)
        )
    return left == right


def _frames_equal(left: pl.DataFrame, right: pl.DataFrame) -> bool:
    if left.columns != right.columns or left.height != right.height:
        return False
    return all(
        _values_equal(left_value, right_value)
        for left_row, right_row in zip(left.rows(), right.rows(), strict=True)
        for left_value, right_value in zip(left_row, right_row, strict=True)
    )


def _required_cells() -> set[tuple[str, str, int, int]]:
    same_year = {
        (evaluation, "same_year", year, year)
        for evaluation in COVARIATE_READINESS_EVALUATIONS
        for year in (2024, 2025)
    }
    forward = {
        (evaluation, "2024_to_2025", 2024, 2025) for evaluation in COVARIATE_READINESS_EVALUATIONS
    }
    return same_year | forward


def _config_problems(manifest: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    config = manifest.get("config")
    if not isinstance(config, dict):
        return ["manifest config is not an object"]
    expected = {
        "schema_version": 1,
        "analysis": {
            "years": [2024, 2025],
            "baseline_generation_sha256": _BASELINE_GENERATION,
            "station_inventory_generation_sha256": _INVENTORY_GENERATION,
            "minimum_train_stations": 8,
        },
        "methods": {
            "comparator": "idw2",
            "candidates": ["covariate_gbm", "covariate_gbm_idw2"],
            "idw_power": 2.0,
            "minimum_distance_km": 0.1,
            "model": {
                "n_estimators": 200,
                "learning_rate": 0.05,
                "num_leaves": 31,
                "min_child_samples": 10,
                "n_jobs": 1,
                "seed": 20260811,
            },
        },
        "validation": {
            "evaluations": list(COVARIATE_READINESS_EVALUATIONS),
            "bootstrap_draws": 9999,
            "bootstrap_seed": 20260828,
        },
    }
    if config != expected:
        problems.append("normalized config differs from the frozen readiness domain")
    if manifest.get("schema_version") != _SCHEMA_VERSION:
        problems.append("manifest schema version differs from the frozen readiness schema")
    if manifest.get("analysis") != "spatial_covariate_readiness":
        problems.append("manifest analysis name differs from the readiness contract")
    if manifest.get("baseline_generation_sha256") != _BASELINE_GENERATION:
        problems.append("baseline generation differs from the reviewed generation")
    if manifest.get("station_inventory_generation_sha256") != _INVENTORY_GENERATION:
        problems.append("station inventory generation differs from the reviewed generation")
    if isinstance(config.get("analysis"), dict):
        config_analysis = config["analysis"]
        if manifest.get("baseline_generation_sha256") != config_analysis.get(
            "baseline_generation_sha256"
        ):
            problems.append("baseline generation does not match normalized config")
        if manifest.get("station_inventory_generation_sha256") != config_analysis.get(
            "station_inventory_generation_sha256"
        ):
            problems.append("station inventory generation does not match normalized config")
    return problems


def _input_problems(directory: Path, manifest: dict[str, Any]) -> list[str]:
    inputs = manifest.get("inputs")
    if not isinstance(inputs, list) or not inputs:
        return ["bound input role inventory is missing"]
    if len(directory.parents) < 4:
        return ["bound input identity root cannot be resolved from generation path"]
    data_root = directory.parents[3]
    problems: list[str] = []
    expected_paths = {
        f"outputs/spatial_surface_baseline/generations/{_BASELINE_GENERATION}/manifest.json",
        *(
            f"outputs/spatial_surface_baseline/generations/{_BASELINE_GENERATION}/{member}"
            for member in _BASELINE_MEMBERS
        ),
        *(
            "interim/era5/generations/"
            f"{_INVENTORY_GENERATION}/year={year}/era5_station_hour.parquet"
            for year in (2023, 2024, 2025)
        ),
        *(
            f"outputs/m8_satellite/generations/{_INVENTORY_GENERATION}/year={year}/panel.parquet"
            for year in (2024, 2025)
        ),
    }
    observed_paths = {
        str(item.get("path")) for item in inputs if isinstance(item, dict) and "path" in item
    }
    if len(inputs) != len(expected_paths) or observed_paths != expected_paths:
        problems.append("bound input role inventory differs from the exact ten-file contract")
    seen: set[str] = set()
    for item in inputs:
        if not isinstance(item, dict) or set(item) != {"path", "bytes", "sha256"}:
            problems.append("bound input identity has an invalid shape")
            continue
        relative_value = item.get("path")
        if not isinstance(relative_value, str):
            problems.append("bound input identity path is not a string")
            continue
        relative = PurePosixPath(relative_value)
        if relative.is_absolute() or ".." in relative.parts or relative_value in seen:
            problems.append("bound input identity path is unsafe or duplicate")
            continue
        seen.add(relative_value)
        candidate = data_root.joinpath(*relative.parts)
        try:
            resolved = candidate.resolve(strict=True)
            root = data_root.resolve(strict=True)
            resolved.relative_to(root)
            observed = _file_identity(resolved)
        except (OSError, ValueError):
            problems.append(f"bound input identity is unreadable: {relative_value}")
            continue
        if _is_link_or_reparse(candidate) or not candidate.is_file():
            problems.append(f"bound input identity is linked or not a file: {relative_value}")
        if observed != {"bytes": item.get("bytes"), "sha256": item.get("sha256")}:
            problems.append(f"bound input identity differs from bytes on disk: {relative_value}")
    if inputs != sorted(
        inputs, key=lambda item: str(item.get("path")) if isinstance(item, dict) else ""
    ):
        problems.append("bound input identities are not sorted by path")
    return problems


def _baseline_authority_problems(
    directory: Path, generation_frames: dict[str, pl.DataFrame]
) -> list[str]:
    """Compare the emitted cohort and fold authorization with the frozen baseline."""
    data_root = directory.parents[3]
    baseline = (
        data_root / "outputs" / "spatial_surface_baseline" / "generations" / _BASELINE_GENERATION
    )
    manifest_path = baseline / "manifest.json"
    problems: list[str] = []
    try:
        manifest_payload = manifest_path.read_bytes()
        manifest_value = _strict_json(manifest_payload)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return ["frozen baseline manifest is unreadable or contains non-finite JSON"]
    if not isinstance(manifest_value, dict):
        return ["frozen baseline manifest is not a JSON object"]
    baseline_manifest: dict[str, Any] = manifest_value
    if (
        baseline_manifest.get("complete") is not True
        or baseline_manifest.get("generation_sha256") != _BASELINE_GENERATION
        or baseline_manifest.get("inventory_generation_sha256") != _INVENTORY_GENERATION
    ):
        problems.append("frozen baseline manifest identity differs from the reviewed inputs")
    members = baseline_manifest.get("members")
    if not isinstance(members, dict) or set(members) != set(SPATIAL_BASELINE_MEMBER_NAMES[:-1]):
        problems.append("frozen baseline member identity inventory differs from the contract")
        members = {}
    baseline_frames: dict[str, pl.DataFrame] = {}
    for member in _BASELINE_MEMBERS:
        name = member.removesuffix(".parquet")
        path = baseline / member
        try:
            observed_identity = _file_identity(path)
            frame = pl.read_parquet(path)
        except (OSError, pl.exceptions.PolarsError):
            problems.append(f"frozen baseline member is unreadable: {member}")
            continue
        if members.get(member) != observed_identity:
            problems.append(f"frozen baseline member identity differs: {member}")
        expected_schema = SPATIAL_BASELINE_TABLE_SCHEMAS[name]
        if frame.schema != pl.Schema(expected_schema):
            problems.append(f"frozen baseline {name} schema differs from its contract")
            continue
        baseline_frames[name] = frame
    if set(baseline_frames) != {"stations", "panel", "support", "folds"}:
        return problems
    baseline_stations = baseline_frames["stations"].sort("station_name")
    baseline_panel = baseline_frames["panel"].sort("station_name", "month")
    generation_stations = generation_frames["stations"].sort("station_name")
    generation_panel = generation_frames["panel"].sort("station_name", "month")
    if not _frames_equal(baseline_stations, generation_stations) or not _frames_equal(
        baseline_panel, generation_panel
    ):
        problems.append("generation stations or panel differ from the frozen baseline cohort")
    support_by_station = {
        str(row["station_name"]): row for row in baseline_frames["support"].to_dicts()
    }
    for row in generation_frames["covariates"].to_dicts():
        support = support_by_station.get(str(row["station_name"]))
        if support is None or not (
            _values_equal(row["x_m"], support["x_m"]) and _values_equal(row["y_m"], support["y_m"])
        ):
            problems.append("generation projected coordinates differ from frozen baseline support")
            break
    authorization: dict[tuple[str, str], set[str]] = {}
    clusters: dict[str, set[int]] = {}
    for row in baseline_frames["folds"].to_dicts():
        key = (str(row["evaluation"]), str(row["target_station"]))
        authorization.setdefault(key, set()).update(str(value) for value in row["train_stations"])
        clusters.setdefault(str(row["target_station"]), set()).add(int(row["target_cluster"]))
    if any(len(values) != 1 for values in clusters.values()):
        problems.append("frozen baseline target cluster membership is inconsistent")
    for row in generation_frames["folds"].to_dicts():
        key = (str(row["evaluation"]), str(row["target_station"]))
        expected = authorization.get(key)
        observed = {str(value) for value in row["train_stations"]}
        cluster = clusters.get(str(row["target_station"]), set())
        if expected is None or observed != expected:
            problems.append("generation fold differs from baseline training authorization")
            break
        if cluster != {int(row["target_cluster"])}:
            problems.append("generation target cluster differs from the frozen baseline fold")
            break
    return problems


def _table_contract_problems(
    frames: dict[str, pl.DataFrame], manifest: dict[str, Any]
) -> list[str]:
    problems: list[str] = []
    tables = manifest.get("tables")
    if not isinstance(tables, dict) or set(tables) != set(_TABLE_NAMES):
        return ["manifest table inventory differs from the seven-table contract"]
    for name, frame in frames.items():
        schema = COVARIATE_READINESS_TABLE_SCHEMAS[name]
        if frame.schema != pl.Schema(schema):
            problems.append(f"{name} schema differs from the authoritative schema")
            continue
        try:
            identity = _table_identity(name, frame)
        except (TypeError, ValueError, pl.exceptions.PolarsError):
            problems.append(f"{name} logical table identity cannot be recomputed")
            continue
        if tables.get(name) != identity:
            problems.append(f"{name} logical table identity differs from manifest")
    return problems


def _manifest_envelope_problems(manifest: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    if set(manifest) != _MANIFEST_KEYS:
        problems.append("manifest envelope has missing or unexpected top-level fields")
    git_sha = manifest.get("git_sha")
    if not isinstance(git_sha, str) or _GIT_SHA.fullmatch(git_sha) is None:
        problems.append("manifest envelope git_sha is not a full lowercase Git SHA")
    if not isinstance(manifest.get("git_dirty"), bool):
        problems.append("manifest envelope git_dirty is not Boolean")
    generated_at = manifest.get("generated_at")
    try:
        timestamp = datetime.fromisoformat(generated_at) if isinstance(generated_at, str) else None
    except ValueError:
        timestamp = None
    if timestamp is None or timestamp.tzinfo is None or timestamp.utcoffset() is None:
        problems.append("manifest envelope generated_at is not a timezone-aware ISO timestamp")
    return problems


def _authoritative_problems(frames: dict[str, pl.DataFrame], minimum: int) -> list[str]:
    problems: list[str] = []
    stations = frames["stations"]
    panel = frames["panel"]
    covariates = frames["covariates"]
    folds = frames["folds"]
    predictions = frames["predictions"]
    station_rows = stations.to_dicts()
    station_names = {str(row["station_name"]) for row in station_rows}
    if len(station_names) != stations.height:
        problems.append("station authoritative keys are duplicate")
    panel_rows = panel.to_dicts()
    panel_keys = [(str(row["station_name"]), row["month"]) for row in panel_rows]
    if len(panel_keys) != len(set(panel_keys)):
        problems.append("panel authoritative keys are duplicate")
    if {station for station, _month in panel_keys} != station_names:
        problems.append("panel station domain differs from authoritative stations")
    station_by_name = {str(row["station_name"]): row for row in station_rows}
    panel_by_key: dict[tuple[str, date], dict[str, Any]] = {}
    for row in panel_rows:
        key = (str(row["station_name"]), row["month"])
        panel_by_key[key] = row
        station_record = station_by_name.get(key[0])
        if station_record is not None and any(
            not _values_equal(row[column], station_record[column])
            for column in ("station_type_official", "lon", "lat")
        ):
            problems.append("panel station metadata differs from authoritative stations")
        if row["pollutant"] != "PM2.5" or key[1].year not in {2024, 2025}:
            problems.append("panel target domain differs from the frozen years and pollutant")
        observed = row["mean"]
        valid_observed = (
            row["target_state"] == "observed"
            and row["meets_threshold"] is True
            and observed is not None
            and math.isfinite(float(observed))
        )
        valid_withheld = (
            row["target_state"] == "withheld"
            and row["meets_threshold"] is False
            and observed is None
        )
        if not (valid_observed or valid_withheld):
            problems.append("panel target state disagrees with authoritative observation")
    covariate_rows = covariates.to_dicts()
    covariate_keys = [(str(row["station_name"]), row["month"]) for row in covariate_rows]
    if len(covariate_keys) != len(set(covariate_keys)) or set(covariate_keys) != set(panel_keys):
        problems.append("covariate authoritative keys differ from the panel")
    covariate_by_key = {(str(row["station_name"]), row["month"]): row for row in covariate_rows}
    for key, row in covariate_by_key.items():
        target = panel_by_key.get(key)
        if target is not None and any(
            not _values_equal(left, right)
            for left, right in (
                (row["target_state"], target["target_state"]),
                (row["PM2.5"], target["mean"]),
                (row["lon"], target["lon"]),
                (row["lat"], target["lat"]),
            )
        ):
            problems.append("covariate target values differ from the authoritative panel")
    fold_rows = folds.to_dicts()
    fold_keys = [_target_key(row) for row in fold_rows]
    if len(fold_keys) != len(set(fold_keys)):
        problems.append("authoritative fold keys are duplicate")
    expected_fold_keys: set[tuple[object, ...]] = set()
    for station, month in panel_keys:
        for evaluation in COVARIATE_READINESS_EVALUATIONS:
            expected_fold_keys.add(
                (evaluation, "same_year", month.year, month.year, month, station)
            )
            if month.year == 2025:
                expected_fold_keys.add((evaluation, "2024_to_2025", 2024, 2025, month, station))
    if set(fold_keys) != expected_fold_keys:
        problems.append("authoritative fold grid differs from panel targets and required cells")
    fold_by_key = {_target_key(row): row for row in fold_rows}
    for row in fold_rows:
        _validate_fold_row(
            row,
            panel_by_key=panel_by_key,
            covariates=covariate_rows,
            station_names=station_names,
            minimum=minimum,
            problems=problems,
        )
    prediction_rows = predictions.to_dicts()
    actual_prediction_keys = [(_target_key(row), str(row["method"])) for row in prediction_rows]
    expected_prediction_keys = {
        (key, method) for key in expected_fold_keys for method in COVARIATE_READINESS_METHODS
    }
    if len(actual_prediction_keys) != len(set(actual_prediction_keys)):
        problems.append("prediction keys contain a duplicate method target key")
    if set(actual_prediction_keys) != expected_prediction_keys:
        problems.append("prediction key grid differs from authoritative folds and methods")
    for row in prediction_rows:
        fold = fold_by_key.get(_target_key(row))
        if fold is None:
            continue
        if any(not _values_equal(row[column], fold[column]) for column in _FOLD_COLUMNS):
            problems.append("prediction row does not inherit its authoritative fold")
            continue
        _validate_prediction_row(row, problems)
    return problems


def _validate_fold_row(
    row: dict[str, Any],
    *,
    panel_by_key: dict[tuple[str, date], dict[str, Any]],
    covariates: list[dict[str, Any]],
    station_names: set[str],
    minimum: int,
    problems: list[str],
) -> None:
    station = str(row["target_station"])
    month = row["month"]
    target = panel_by_key.get((station, month))
    if target is None:
        return
    if any(
        not _values_equal(left, right)
        for left, right in (
            (row["target_state"], target["target_state"]),
            (row["observed"], target["mean"]),
            (row["target_year"], month.year),
        )
    ):
        problems.append("fold target metadata differs from the authoritative panel")
    period = str(row["training_period"])
    if period == "2024_to_2025":
        if int(row["train_year"]) != 2024 or int(row["target_year"]) != 2025:
            problems.append("forward training year declares or uses 2025 training truth")
    elif period == "same_year":
        if int(row["train_year"]) != int(row["target_year"]):
            problems.append("same-year fold training year differs from target year")
    else:
        problems.append("fold training period is outside the frozen domain")
    train_stations = [str(value) for value in row["train_stations"]]
    if train_stations != sorted(set(train_stations)) or not set(train_stations) <= station_names:
        problems.append("fold training-station membership is invalid")
    if station in train_stations:
        problems.append("held-station isolation is violated by target in training stations")
    if int(row["n_train_stations"]) != len(train_stations):
        problems.append("fold training-station denominator differs from membership")
    train_year = int(row["train_year"])
    model_rows = [
        candidate
        for candidate in covariates
        if candidate["station_name"] in train_stations
        and candidate["month"].year == train_year
        and candidate["target_state"] == "observed"
    ]
    source_month = date(train_year, month.month, 1)
    same_month_rows = [candidate for candidate in model_rows if candidate["month"] == source_month]
    if int(row["n_model_train_rows"]) != len(model_rows):
        problems.append("fold model-training denominator differs from observed training truth")
    if int(row["n_same_month_train_rows"]) != len(same_month_rows):
        problems.append("fold same-month denominator differs from observed training truth")
    if target["target_state"] == "withheld":
        expected_state = "unscored_target_withheld"
        expected_reason = "target_state=withheld"
    elif len(train_stations) < minimum:
        expected_state = "unscored_insufficient_train"
        expected_reason = (
            f"n_train_stations={len(train_stations)} is below minimum_train_stations={minimum}"
        )
    else:
        expected_state = "eligible"
        expected_reason = None
    if row["fold_state"] != expected_state or row["fold_reason"] != expected_reason:
        problems.append("fold state differs from target state and training denominator")


def _validate_prediction_row(row: dict[str, Any], problems: list[str]) -> None:
    predicted = row["predicted"]
    error = row["error"]
    if row["fold_state"] != "eligible":
        if (
            row["prediction_state"] != row["fold_state"]
            or row["failure_type"] is not None
            or predicted is not None
            or error is not None
        ):
            problems.append("withheld target or other unscored fold was changed to scored")
            problems.append("prediction failure contract differs from the fold-derived state")
        return
    if row["prediction_state"] == "scored":
        observed = row["observed"]
        try:
            valid = (
                predicted is not None
                and observed is not None
                and error is not None
                and math.isfinite(float(predicted))
                and math.isfinite(float(observed))
                and math.isfinite(float(error))
                and math.isclose(
                    float(error),
                    float(predicted) - float(observed),
                    rel_tol=1e-12,
                    abs_tol=1e-12,
                )
                and row["failure_type"] is None
            )
        except (TypeError, ValueError):
            valid = False
        if not valid:
            problems.append("prediction error arithmetic differs from predicted minus observed")
        return
    valid_failures = {
        "idw2": {"duplicate_coordinate", "non_finite_prediction"},
        "covariate_gbm": {
            "estimator_failed",
            "wrong_prediction_length",
            "non_finite_prediction",
        },
        "covariate_gbm_idw2": {
            "duplicate_coordinate",
            "estimator_failed",
            "wrong_prediction_length",
            "non_finite_prediction",
            "insufficient_residual_stations",
        },
    }
    state = str(row["prediction_state"])
    failure_type = row["failure_type"]
    valid_failure_type = (
        state == "estimator_failed"
        and isinstance(failure_type, str)
        and _FAILURE_TYPE.fullmatch(failure_type) is not None
    ) or (state != "estimator_failed" and failure_type is None)
    if (
        state not in valid_failures.get(str(row["method"]), set())
        or not valid_failure_type
        or predicted is not None
        or error is not None
    ):
        problems.append("prediction failure contract has an arbitrary state, type, or value")


def _finite_prediction(row: dict[str, Any] | None) -> bool:
    if row is None:
        return False
    try:
        return (
            row["prediction_state"] == "scored"
            and row["predicted"] is not None
            and row["error"] is not None
            and math.isfinite(float(row["predicted"]))
            and math.isfinite(float(row["error"]))
        )
    except (TypeError, ValueError):
        return False


def _recompute_scores(folds: pl.DataFrame, predictions: pl.DataFrame) -> pl.DataFrame:
    fold_rows = folds.to_dicts()
    prediction_rows = predictions.to_dicts()
    by_method_cell: dict[tuple[tuple[str, str, int, int], str], list[dict[str, Any]]] = {}
    for row in prediction_rows:
        by_method_cell.setdefault((_cell(row), str(row["method"])), []).append(row)
    rows: list[dict[str, object]] = []
    for cell in sorted(_required_cells()):
        cell_folds = [row for row in fold_rows if _cell(row) == cell]
        full_keys = {_target_key(row) for row in cell_folds}
        eligible_keys = {_target_key(row) for row in cell_folds if row["fold_state"] == "eligible"}
        intended_stations = {
            str(row["target_station"]) for row in cell_folds if row["fold_state"] == "eligible"
        }
        for method in COVARIATE_READINESS_METHODS:
            method_rows = by_method_cell.get((cell, method), [])
            method_keys = {_target_key(row) for row in method_rows}
            eligible_rows = [row for row in method_rows if _target_key(row) in eligible_keys]
            scored_rows = [row for row in eligible_rows if _finite_prediction(row)]
            if method_keys != full_keys:
                score_state = "missing_intended_predictions"
            elif not eligible_keys:
                score_state = "no_eligible_targets"
            elif len(scored_rows) != len(eligible_keys):
                score_state = "incomplete_predictions"
            else:
                score_state = "complete"
            mae: float | None = None
            rmse: float | None = None
            bias: float | None = None
            if score_state != "missing_intended_predictions" and scored_rows:
                station_metrics: list[tuple[float, float, float]] = []
                for station in sorted({str(row["target_station"]) for row in scored_rows}):
                    errors = np.asarray(
                        [
                            float(row["error"])
                            for row in scored_rows
                            if row["target_station"] == station
                        ],
                        dtype=float,
                    )
                    station_metrics.append(
                        (
                            float(np.abs(errors).mean()),
                            float(np.sqrt(np.square(errors).mean())),
                            float(errors.mean()),
                        )
                    )
                mae = float(np.mean([metric[0] for metric in station_metrics]))
                rmse = float(np.mean([metric[1] for metric in station_metrics]))
                bias = float(np.mean([metric[2] for metric in station_metrics]))
            evaluation, training_period, train_year, target_year = cell
            rows.append(
                {
                    "evaluation": evaluation,
                    "training_period": training_period,
                    "train_year": train_year,
                    "target_year": target_year,
                    "method": method,
                    "n_intended": len(eligible_keys),
                    "n_scored": len(scored_rows),
                    "n_failed": len(eligible_keys) - len(scored_rows),
                    "n_stations_intended": len(intended_stations),
                    "n_stations_scored": len({str(row["target_station"]) for row in scored_rows}),
                    "station_clustered_mae": mae,
                    "station_clustered_rmse": rmse,
                    "station_clustered_bias": bias,
                    "score_state": score_state,
                }
            )
    return pl.DataFrame(rows, schema=COVARIATE_READINESS_TABLE_SCHEMAS["scores"]).sort(
        *_CELL_KEY, "method"
    )


def _bootstrap(values: list[float]) -> tuple[float, float, float]:
    array = np.asarray(values, dtype=float)
    generator = np.random.default_rng(20260828)
    samples = array[generator.integers(0, array.size, size=(9999, array.size))]
    lower, upper = np.percentile(np.median(samples, axis=1), [2.5, 97.5])
    return float(np.median(array)), float(lower), float(upper)


def _recompute_deltas(folds: pl.DataFrame, predictions: pl.DataFrame) -> pl.DataFrame:
    fold_rows = folds.to_dicts()
    prediction_rows = predictions.to_dicts()
    by_method = {
        method: {_target_key(row): row for row in prediction_rows if row["method"] == method}
        for method in COVARIATE_READINESS_METHODS
    }
    rows: list[dict[str, object]] = []
    for method in _CANDIDATES:
        for cell in sorted(_required_cells()):
            keys = [
                _target_key(row)
                for row in fold_rows
                if _cell(row) == cell and row["fold_state"] == "eligible"
            ]
            comparator = by_method["idw2"]
            candidate = by_method[method]
            if not keys:
                n_stations = 0
                median = lower = upper = None
                state = "no_eligible_targets"
            elif not all(
                _finite_prediction(comparator.get(key)) and _finite_prediction(candidate.get(key))
                for key in keys
            ):
                n_stations = 0
                median = lower = upper = None
                state = "incomplete_predictions"
            else:
                station_deltas: list[float] = []
                for station in sorted({str(key[-1]) for key in keys}):
                    station_keys = [key for key in keys if key[-1] == station]
                    candidate_mae = float(
                        np.mean([abs(float(candidate[key]["error"])) for key in station_keys])
                    )
                    comparator_mae = float(
                        np.mean([abs(float(comparator[key]["error"])) for key in station_keys])
                    )
                    station_deltas.append(candidate_mae - comparator_mae)
                median, lower, upper = _bootstrap(station_deltas)
                n_stations = len(station_deltas)
                state = "complete"
            evaluation, training_period, train_year, target_year = cell
            rows.append(
                {
                    "evaluation": evaluation,
                    "training_period": training_period,
                    "train_year": train_year,
                    "target_year": target_year,
                    "method": method,
                    "comparison_method": "idw2",
                    "n_stations": n_stations,
                    "median_station_mae_delta": median,
                    "lower_2_5": lower,
                    "upper_97_5": upper,
                    "paired_state": state,
                }
            )
    return pl.DataFrame(rows, schema=COVARIATE_READINESS_TABLE_SCHEMAS["paired_deltas"]).sort(
        *_CELL_KEY, "method"
    )


def _complete_score(row: dict[str, Any]) -> bool:
    metrics = (
        row["station_clustered_mae"],
        row["station_clustered_rmse"],
        row["station_clustered_bias"],
    )
    return (
        row["score_state"] == "complete"
        and int(row["n_intended"]) > 0
        and int(row["n_intended"]) == int(row["n_scored"])
        and int(row["n_failed"]) == 0
        and int(row["n_stations_intended"]) > 0
        and int(row["n_stations_intended"]) == int(row["n_stations_scored"])
        and all(value is not None and math.isfinite(float(value)) for value in metrics)
    )


def _recompute_gate(scores: pl.DataFrame, deltas: pl.DataFrame) -> dict[str, object]:
    score_rows = {(*_cell(row), str(row["method"])): row for row in scores.to_dicts()}
    delta_rows = {(*_cell(row), str(row["method"])): row for row in deltas.to_dicts()}
    improvement_cells = {
        (evaluation, "same_year", year, year)
        for evaluation in ("buffer_20km", "buffer_40km")
        for year in (2024, 2025)
    } | {(evaluation, "2024_to_2025", 2024, 2025) for evaluation in COVARIATE_READINESS_EVALUATIONS}
    cluster_cells = {("spatial_cluster", "same_year", year, year) for year in (2024, 2025)}
    qualifying: list[str] = []
    for method in _CANDIDATES:
        pair_complete = True
        for cell in improvement_cells:
            candidate = score_rows[(*cell, method)]
            comparator = score_rows[(*cell, "idw2")]
            delta = delta_rows[(*cell, method)]
            if not (
                _complete_score(candidate)
                and _complete_score(comparator)
                and candidate["n_intended"] == comparator["n_intended"]
                and candidate["n_stations_intended"] == comparator["n_stations_intended"]
                and delta["comparison_method"] == "idw2"
                and delta["paired_state"] == "complete"
                and int(delta["n_stations"]) == int(candidate["n_stations_intended"])
                and delta["median_station_mae_delta"] is not None
                and delta["lower_2_5"] is not None
                and delta["upper_97_5"] is not None
                and all(
                    math.isfinite(float(delta[field]))
                    for field in ("median_station_mae_delta", "lower_2_5", "upper_97_5")
                )
                and float(delta["median_station_mae_delta"]) < 0
            ):
                pair_complete = False
        cluster_complete = all(
            _complete_score(score_rows[(*cell, method)])
            and _complete_score(score_rows[(*cell, "idw2")])
            and score_rows[(*cell, method)]["n_intended"]
            == score_rows[(*cell, "idw2")]["n_intended"]
            and score_rows[(*cell, method)]["n_stations_intended"]
            == score_rows[(*cell, "idw2")]["n_stations_intended"]
            for cell in cluster_cells
        )
        if pair_complete and cluster_complete:
            qualifying.append(method)
    return {
        "state": "go" if qualifying else "stop",
        "qualifying_methods": sorted(qualifying),
        "required_improvement_cells": 7,
        "rule": _RULE,
        "limitations": list(COVARIATE_READINESS_LIMITATIONS),
    }


def _semantic_problems(
    frames: dict[str, pl.DataFrame], manifest: dict[str, Any], summary: dict[str, Any]
) -> list[str]:
    problems: list[str] = []
    analysis = manifest["config"]["analysis"]
    minimum = int(analysis["minimum_train_stations"])
    problems.extend(_authoritative_problems(frames, minimum))
    recomputed_scores = _recompute_scores(frames["folds"], frames["predictions"])
    if not _frames_equal(recomputed_scores, frames["scores"]):
        problems.append("persisted scores differ from independently recomputed scores")
    recomputed_deltas = _recompute_deltas(frames["folds"], frames["predictions"])
    if not _frames_equal(recomputed_deltas, frames["paired_deltas"]):
        problems.append(
            "persisted paired deltas differ from independently recomputed paired deltas"
        )
    gate = _recompute_gate(recomputed_scores, recomputed_deltas)
    if manifest.get("gate") != gate or summary.get("gate") != gate:
        problems.append("persisted gate verdict differs from independently recomputed gate verdict")
    claim_boundary = {
        "feeds_web": False,
        "limitations": list(COVARIATE_READINESS_LIMITATIONS),
    }
    if manifest.get("claim_boundary") != claim_boundary or any(
        summary.get(key) != value for key, value in claim_boundary.items()
    ):
        problems.append("claim boundary lacks required limitations or feeds_web=false")
    expected_summary = {
        "analysis": "spatial_covariate_readiness",
        "baseline_generation_sha256": _BASELINE_GENERATION,
        "station_inventory_generation_sha256": _INVENTORY_GENERATION,
        "output_rows": {name: frame.height for name, frame in frames.items()},
        "gate": gate,
        **claim_boundary,
    }
    if summary != expected_summary:
        problems.append("summary differs from independently recomputed run record")
    return problems


def verify_generation(path: Path) -> list[str]:
    """Return every independently detected problem for one generation directory."""
    directory = path.absolute()
    problems: list[str] = []
    try:
        resolved = directory.resolve(strict=True)
        entries = tuple(directory.iterdir())
    except OSError as exc:
        return [f"generation directory is unreadable: {type(exc).__name__}"]
    if _is_link_or_reparse(directory) or not directory.is_dir() or resolved != directory:
        return ["generation directory is linked, reparse-point, or outside"]
    if directory.parent.name != "generations":
        problems.append("generation directory is outside the immutable generations layout")
    names = {entry.name for entry in entries}
    if names != set(COVARIATE_READINESS_MEMBER_NAMES):
        problems.append("generation member inventory differs from the exact nine-file contract")
    for entry in entries:
        try:
            resolved_entry = entry.resolve(strict=True)
            links = entry.stat().st_nlink
        except OSError:
            problems.append(f"generation member is unreadable: {entry.name}")
            continue
        if (
            _is_link_or_reparse(entry)
            or not entry.is_file()
            or resolved_entry.parent != directory
            or links != 1
        ):
            problems.append(f"generation member is linked or reparse-point: {entry.name}")
    manifest_path = directory / "manifest.json"
    if not manifest_path.is_file():
        return problems
    try:
        manifest_payload = manifest_path.read_bytes()
        manifest_value = _strict_json(manifest_payload)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        problems.append(f"manifest is unreadable or contains non-finite JSON: {type(exc).__name__}")
        return problems
    if not isinstance(manifest_value, dict):
        return [*problems, "manifest is not a JSON object"]
    manifest: dict[str, Any] = manifest_value
    try:
        canonical_manifest = _canonical_json_bytes(manifest)
    except (TypeError, ValueError) as exc:
        problems.append(f"manifest contains malformed or non-finite JSON: {type(exc).__name__}")
        return problems
    if manifest_payload != canonical_manifest:
        problems.append("manifest is not canonical JSON")
    problems.extend(_manifest_envelope_problems(manifest))
    if manifest.get("complete") is not True:
        problems.append("manifest is not marked complete")
    generation_sha = manifest.get("generation_sha256")
    identity = {
        key: value
        for key, value in manifest.items()
        if key not in {"generated_at", "complete", "generation_sha256"}
    }
    try:
        recomputed_generation = _canonical_hash(identity)
    except (TypeError, ValueError) as exc:
        problems.append(f"manifest identity cannot be recomputed: {type(exc).__name__}")
        return problems
    if generation_sha != recomputed_generation or directory.name != recomputed_generation:
        problems.append("generation directory identity differs from manifest identity")
    problems.extend(_config_problems(manifest))
    if manifest.get("identity_scope") != _IDENTITY_SCOPE:
        problems.append("manifest identity scope differs from the run-record contract")
    members = manifest.get("members")
    expected_member_names = set(COVARIATE_READINESS_MEMBER_NAMES[:-1])
    if not isinstance(members, dict) or set(members) != expected_member_names:
        problems.append("manifest member hash inventory differs from the exact contract")
    else:
        for name in COVARIATE_READINESS_MEMBER_NAMES[:-1]:
            member = directory / name
            if not member.is_file():
                continue
            try:
                observed = _file_identity(member)
            except OSError:
                problems.append(f"generation member hash is unreadable: {name}")
                continue
            if members.get(name) != observed:
                problems.append(f"generation member hash differs from manifest: {name}")
    problems.extend(_input_problems(directory, manifest))
    frames: dict[str, pl.DataFrame] = {}
    for name in _TABLE_NAMES:
        member = directory / f"{name}.parquet"
        if not member.is_file():
            continue
        try:
            frames[name] = pl.read_parquet(member)
        except (OSError, pl.exceptions.PolarsError):
            problems.append(f"{name} Parquet member is unreadable")
    if set(frames) != set(_TABLE_NAMES):
        return problems
    problems.extend(_table_contract_problems(frames, manifest))
    try:
        problems.extend(_baseline_authority_problems(directory, frames))
    except (KeyError, TypeError, ValueError, OverflowError, pl.exceptions.PolarsError) as exc:
        problems.append(f"frozen baseline comparison cannot be completed: {type(exc).__name__}")
    summary_path = directory / "summary.json"
    if not summary_path.is_file():
        return problems
    try:
        summary_payload = summary_path.read_bytes()
        summary_value = _strict_json(summary_payload)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        problems.append(f"summary is unreadable or contains non-finite JSON: {type(exc).__name__}")
        return problems
    if not isinstance(summary_value, dict):
        return [*problems, "summary is not a JSON object"]
    summary: dict[str, Any] = summary_value
    try:
        canonical_summary = _canonical_json_bytes(summary)
    except (TypeError, ValueError) as exc:
        problems.append(f"summary contains malformed or non-finite JSON: {type(exc).__name__}")
        return problems
    if summary_payload != canonical_summary:
        problems.append("summary is not canonical JSON")
    try:
        problems.extend(_semantic_problems(frames, manifest, summary))
    except (KeyError, TypeError, ValueError, OverflowError, pl.exceptions.PolarsError) as exc:
        problems.append(f"semantic verification cannot be completed: {type(exc).__name__}")
    return problems


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Independently verify one spatial-covariate readiness generation."
    )
    parser.add_argument("generation", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    problems = verify_generation(args.generation)
    if problems:
        for problem in problems:
            print(problem)
        return 1
    print(f"PASS {args.generation.absolute().name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
