"""Independently verify one immutable spatial-surface baseline generation."""

from __future__ import annotations

import argparse
import json
import math
import stat
from collections.abc import Sequence
from datetime import date
from hashlib import sha256
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from twair.analysis.spatial_surface_baseline import (
    SPATIAL_BASELINE_LIMITATIONS,
    SPATIAL_BASELINE_MEMBER_NAMES,
    SPATIAL_BASELINE_SCHEMA_VERSION,
    SPATIAL_BASELINE_TABLE_ORDER,
    SPATIAL_BASELINE_TABLE_SCHEMAS,
)

_VOLATILE_MANIFEST_FIELDS = {"generated_at", "complete", "generation_sha256"}
_TARGET_KEYS = ("evaluation", "fold_id", "year", "month", "target_station")
_EXPECTED_METHODS = (
    "station_mean",
    "nearest",
    "idw2",
    "kriging_spherical",
    "kriging_hole_effect",
)
_FOLD_COLUMNS = tuple(SPATIAL_BASELINE_TABLE_SCHEMAS["folds"])
_TABLE_KEYS = {
    "stations": ("station_name",),
    "panel": ("station_name", "month"),
    "support": ("station_name",),
    "folds": (*_TARGET_KEYS,),
    "predictions": (*_TARGET_KEYS, "method"),
    "scores": ("evaluation", "year", "method"),
    "paired_deltas": ("evaluation", "year", "method"),
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


def _table_identity(name: str, frame: pl.DataFrame) -> dict[str, object]:
    normalized = frame.select(*SPATIAL_BASELINE_TABLE_SCHEMAS[name]).cast(
        pl.Schema(SPATIAL_BASELINE_TABLE_SCHEMAS[name]), strict=True
    )
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


def _duplicates(frame: pl.DataFrame, keys: tuple[str, ...]) -> bool:
    return not frame.group_by(*keys).len().filter(pl.col("len") > 1).is_empty()


def _prediction_key(row: dict[str, Any]) -> tuple[object, ...]:
    return tuple(row[column] for column in _TARGET_KEYS)


def _finite_error(row: dict[str, Any]) -> bool:
    value = row["error"]
    return row["prediction_state"] == "scored" and value is not None and math.isfinite(float(value))


def _recompute_scores(
    predictions: pl.DataFrame,
    folds: pl.DataFrame,
    methods: tuple[str, ...],
) -> pl.DataFrame:
    rows = list(predictions.iter_rows(named=True))
    fold_rows = list(folds.iter_rows(named=True))
    cells = sorted({(str(row["evaluation"]), int(row["year"])) for row in fold_rows})
    score_rows: list[dict[str, object]] = []
    for evaluation, year in cells:
        cell_folds = [
            row for row in fold_rows if row["evaluation"] == evaluation and int(row["year"]) == year
        ]
        expected_keys = {
            _prediction_key(row) for row in cell_folds if row["fold_state"] == "eligible"
        }
        for method in methods:
            method_rows = [
                row
                for row in rows
                if row["evaluation"] == evaluation
                and int(row["year"]) == year
                and row["method"] == method
            ]
            intended_rows = [row for row in method_rows if _prediction_key(row) in expected_keys]
            intended_keys = {_prediction_key(row) for row in intended_rows}
            scored_rows = [row for row in intended_rows if _finite_error(row)]
            n_intended = len(expected_keys)
            n_scored = len(scored_rows)
            n_failed = n_intended - n_scored
            n_stations_intended = len(
                {
                    str(row["target_station"])
                    for row in cell_folds
                    if row["fold_state"] == "eligible"
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
                station_summaries: list[tuple[float, float, float]] = []
                for station in sorted({str(row["target_station"]) for row in scored_rows}):
                    errors = np.asarray(
                        [
                            float(row["error"])
                            for row in scored_rows
                            if row["target_station"] == station
                        ],
                        dtype=float,
                    )
                    station_summaries.append(
                        (
                            float(np.abs(errors).mean()),
                            float(np.sqrt(np.square(errors).mean())),
                            float(errors.mean()),
                        )
                    )
                mae = float(np.mean([value[0] for value in station_summaries]))
                rmse = float(np.mean([value[1] for value in station_summaries]))
                bias = float(np.mean([value[2] for value in station_summaries]))
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
    return pl.DataFrame(score_rows, schema=SPATIAL_BASELINE_TABLE_SCHEMAS["scores"]).sort(
        *SPATIAL_BASELINE_TABLE_ORDER["scores"]
    )


def _bootstrap(values: list[float], *, draws: int, seed: int) -> tuple[float, float, float]:
    array = np.asarray(values, dtype=float)
    generator = np.random.default_rng(seed)
    samples = array[generator.integers(0, array.size, size=(draws, array.size))]
    lower, upper = np.percentile(np.median(samples, axis=1), [2.5, 97.5])
    return float(np.median(array)), float(lower), float(upper)


def _recompute_deltas(
    predictions: pl.DataFrame,
    folds: pl.DataFrame,
    methods: tuple[str, ...],
    *,
    comparison_method: str,
    draws: int,
    seed: int,
) -> pl.DataFrame:
    rows = list(predictions.iter_rows(named=True))
    fold_rows = list(folds.iter_rows(named=True))
    by_method = {method: [row for row in rows if row["method"] == method] for method in methods}
    baseline_by_key = {_prediction_key(row): row for row in by_method[comparison_method]}
    delta_rows: list[dict[str, object]] = []
    cells = sorted({(str(row["evaluation"]), int(row["year"])) for row in fold_rows})
    for method in methods:
        if method == comparison_method:
            continue
        candidate_by_key = {_prediction_key(row): row for row in by_method[method]}
        for evaluation, year in cells:
            keys = [
                _prediction_key(row)
                for row in fold_rows
                if row["evaluation"] == evaluation
                and int(row["year"]) == year
                and row["fold_state"] == "eligible"
            ]
            if not keys:
                state = "no_eligible_targets"
                n_stations = 0
                median = lower = upper = None
            elif set(candidate_by_key) != set(baseline_by_key) or not all(
                _finite_error(baseline_by_key[key]) and _finite_error(candidate_by_key.get(key, {}))
                for key in keys
            ):
                state = "incomplete_predictions"
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
                median, lower, upper = _bootstrap(station_deltas, draws=draws, seed=seed)
                state = "complete"
                n_stations = len(station_deltas)
            delta_rows.append(
                {
                    "evaluation": evaluation,
                    "year": year,
                    "method": method,
                    "comparison_method": comparison_method,
                    "n_stations": n_stations,
                    "median_station_mae_delta": median,
                    "lower_2_5": lower,
                    "upper_97_5": upper,
                    "paired_state": state,
                }
            )
    return pl.DataFrame(delta_rows, schema=SPATIAL_BASELINE_TABLE_SCHEMAS["paired_deltas"]).sort(
        *SPATIAL_BASELINE_TABLE_ORDER["paired_deltas"]
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


def _prediction_grid_problems(
    folds: pl.DataFrame,
    predictions: pl.DataFrame,
    methods: tuple[str, ...],
) -> list[str]:
    problems: list[str] = []
    fold_rows = list(folds.iter_rows(named=True))
    prediction_rows = list(predictions.iter_rows(named=True))
    fold_by_key = {_prediction_key(row): row for row in fold_rows}
    expected = {(key, method) for key in fold_by_key for method in methods}
    actual = [(_prediction_key(row), str(row["method"])) for row in prediction_rows]
    if len(actual) != len(set(actual)):
        problems.append("prediction grid contains duplicate rows")
    if set(actual) != expected:
        problems.append("prediction grid differs from authoritative folds and exact methods")
    if {str(row["method"]) for row in prediction_rows} - set(methods):
        problems.append("prediction grid contains methods outside the exact method domain")
    for row in prediction_rows:
        fold = fold_by_key.get(_prediction_key(row))
        if fold is None:
            continue
        if any(not _values_equal(row[column], fold[column]) for column in _FOLD_COLUMNS):
            problems.append("prediction row differs from its authoritative fold")
            break
    return problems


def _frames_equal(left: pl.DataFrame, right: pl.DataFrame) -> bool:
    if left.columns != right.columns or left.height != right.height:
        return False
    return all(
        _values_equal(left_value, right_value)
        for left_row, right_row in zip(left.rows(), right.rows(), strict=True)
        for left_value, right_value in zip(left_row, right_row, strict=True)
    )


def _single_row(
    frame: pl.DataFrame, *, evaluation: str, year: int, method: str
) -> dict[str, Any] | None:
    selected = frame.filter(
        (pl.col("evaluation") == evaluation)
        & (pl.col("year") == year)
        & (pl.col("method") == method)
    )
    return selected.row(0, named=True) if selected.height == 1 else None


def _complete_score(row: dict[str, Any] | None) -> bool:
    if row is None:
        return False
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


def _recompute_gate(
    scores: pl.DataFrame,
    deltas: pl.DataFrame,
    *,
    methods: tuple[str, ...],
    comparison_method: str,
    required_years: tuple[int, ...],
) -> dict[str, Any]:
    qualifying: list[str] = []
    for method in methods:
        if method == comparison_method:
            continue
        primary_complete = True
        for evaluation in ("buffer_20km", "buffer_40km"):
            for year in required_years:
                score = _single_row(scores, evaluation=evaluation, year=year, method=method)
                baseline = _single_row(
                    scores,
                    evaluation=evaluation,
                    year=year,
                    method=comparison_method,
                )
                delta = _single_row(deltas, evaluation=evaluation, year=year, method=method)
                if (
                    score is None
                    or baseline is None
                    or delta is None
                    or not _complete_score(score)
                    or not _complete_score(baseline)
                ):
                    primary_complete = False
                    continue
                if (
                    int(score["n_intended"]) != int(baseline["n_intended"])
                    or int(score["n_stations_intended"]) != int(baseline["n_stations_intended"])
                    or delta["comparison_method"] != comparison_method
                    or int(delta["n_stations"]) != int(score["n_stations_intended"])
                    or delta["paired_state"] != "complete"
                    or delta["median_station_mae_delta"] is None
                    or not math.isfinite(float(delta["median_station_mae_delta"]))
                    or float(delta["median_station_mae_delta"]) >= 0
                ):
                    primary_complete = False
        cluster_complete = all(
            _complete_score(
                _single_row(scores, evaluation="spatial_cluster", year=year, method=method)
            )
            for year in required_years
        )
        if primary_complete and cluster_complete:
            qualifying.append(method)
    return {
        "state": "go" if qualifying else "stop",
        "qualifying_methods": sorted(qualifying),
        "required_cells": 4,
        "rule": "complete predictions and median station MAE delta < 0 in 2024/2025 at 20/40 km",
        "limitations": list(SPATIAL_BASELINE_LIMITATIONS),
    }


def _manifest_config(
    manifest: dict[str, Any],
) -> tuple[tuple[str, ...], str, int, int, tuple[int, ...]]:
    config = manifest["config"]
    validation = config["validation"]
    gate = config["gate"]
    methods = tuple(str(value) for value in validation["methods"])
    comparison = str(gate["comparison_method"])
    return (
        methods,
        comparison,
        int(validation["bootstrap_draws"]),
        int(validation["seed"]),
        tuple(int(value) for value in gate["required_years"]),
    )


def verify_generation(path: Path) -> list[str]:
    problems: list[str] = []
    directory = path.absolute()
    try:
        resolved = directory.resolve(strict=True)
        entries = tuple(directory.iterdir())
    except OSError as exc:
        return [f"generation directory is unreadable: {type(exc).__name__}"]
    if _is_link_or_reparse(directory) or not directory.is_dir() or resolved != directory:
        return ["generation directory is linked, reparse-point, or outside"]
    names = {entry.name for entry in entries}
    expected_names = set(SPATIAL_BASELINE_MEMBER_NAMES)
    if names != expected_names:
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
        manifest = json.loads(manifest_payload.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        problems.append(f"manifest is unreadable: {type(exc).__name__}")
        return problems
    if not isinstance(manifest, dict):
        problems.append("manifest is not a JSON object")
        return problems
    try:
        if manifest_payload != _canonical_json_bytes(manifest):
            problems.append("manifest JSON is not canonical")
    except (TypeError, ValueError):
        problems.append("manifest JSON contains non-canonical values")
        return problems
    if (
        manifest.get("schema_version") != SPATIAL_BASELINE_SCHEMA_VERSION
        or manifest.get("analysis") != "spatial_surface_baseline"
        or manifest.get("complete") is not True
    ):
        problems.append("manifest contract is incomplete or has the wrong schema")
    identity = {
        key: value for key, value in manifest.items() if key not in _VOLATILE_MANIFEST_FIELDS
    }
    try:
        recomputed_generation = _canonical_hash(identity)
    except (TypeError, ValueError):
        recomputed_generation = ""
    if manifest.get("generation_sha256") != recomputed_generation:
        problems.append("manifest generation identity does not match its canonical payload")
    if directory.name != manifest.get("generation_sha256"):
        problems.append("generation directory name does not match the manifest identity")
    claim_boundary = manifest.get("claim_boundary")
    if claim_boundary != {
        "feeds_web": False,
        "limitations": list(SPATIAL_BASELINE_LIMITATIONS),
    }:
        problems.append("claim boundary must retain feeds_web=false and required limitations")
    members = manifest.get("members")
    if not isinstance(members, dict) or set(members) != set(SPATIAL_BASELINE_MEMBER_NAMES[:-1]):
        problems.append("manifest member identities differ from the exact persisted member set")
        members = {}
    for name in SPATIAL_BASELINE_MEMBER_NAMES[:-1]:
        member = directory / name
        if not member.is_file() or _is_link_or_reparse(member):
            continue
        try:
            if _file_identity(member) != members.get(name):
                problems.append(f"{name} checksum differs from manifest")
        except OSError:
            problems.append(f"{name} checksum cannot be read")
    summary: object = None
    summary_path = directory / "summary.json"
    if summary_path.is_file():
        try:
            summary_payload = summary_path.read_bytes()
            summary = json.loads(summary_payload.decode("utf-8"))
            if summary_payload != _canonical_json_bytes(summary):
                problems.append("summary JSON is not canonical")
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            problems.append("summary JSON is unreadable")
    tables: dict[str, pl.DataFrame] = {}
    recorded_tables = manifest.get("tables")
    if not isinstance(recorded_tables, dict) or set(recorded_tables) != set(
        SPATIAL_BASELINE_TABLE_SCHEMAS
    ):
        problems.append("manifest table identities differ from the required tables")
        recorded_tables = {}
    for name, expected_schema in SPATIAL_BASELINE_TABLE_SCHEMAS.items():
        member = directory / f"{name}.parquet"
        if not member.is_file() or _is_link_or_reparse(member):
            continue
        try:
            frame = pl.read_parquet(member)
        except (OSError, pl.exceptions.PolarsError):
            problems.append(f"{name} table is unreadable")
            continue
        tables[name] = frame
        if frame.schema != expected_schema:
            problems.append(f"{name} schema differs from the shared schema")
            continue
        keys = _TABLE_KEYS[name]
        if _duplicates(frame, keys):
            problems.append(f"{name} contains duplicate table keys")
        order_keys = SPATIAL_BASELINE_TABLE_ORDER[name]
        if not _frames_equal(
            frame.select(*order_keys), frame.sort(*order_keys).select(*order_keys)
        ):
            problems.append(f"{name} deterministic row order changed")
        declaration = recorded_tables.get(name)
        if not isinstance(declaration, dict):
            continue
        if frame.height != declaration.get("rows"):
            problems.append(f"{name} row count differs from manifest")
        try:
            observed_identity = _table_identity(name, frame)
        except (OSError, pl.exceptions.PolarsError, TypeError, ValueError):
            problems.append(f"{name} canonical table identity cannot be computed")
            continue
        if observed_identity != declaration:
            problems.append(f"{name} canonical table identity differs from manifest")
    predictions = tables.get("predictions")
    folds = tables.get("folds")
    scores = tables.get("scores")
    deltas = tables.get("paired_deltas")
    if predictions is not None:
        for row in predictions.iter_rows(named=True):
            if row["prediction_state"] == "scored":
                predicted = row["predicted"]
                observed = row["observed"]
                error = row["error"]
                if (
                    predicted is None
                    or observed is None
                    or error is None
                    or not all(
                        math.isfinite(float(value)) for value in (predicted, observed, error)
                    )
                    or not math.isclose(
                        float(error),
                        float(predicted) - float(observed),
                        rel_tol=1e-12,
                        abs_tol=1e-12,
                    )
                ):
                    problems.append("prediction error does not equal predicted - observed")
                    break
            elif row["predicted"] is not None or row["error"] is not None:
                problems.append("unscored prediction retains a predicted value or error")
                break
    if predictions is not None and folds is not None:
        problems.extend(_prediction_grid_problems(folds, predictions, _EXPECTED_METHODS))
    if predictions is not None and folds is not None and scores is not None and deltas is not None:
        try:
            configured_methods, comparison, draws, seed, required_years = _manifest_config(manifest)
            if configured_methods != _EXPECTED_METHODS:
                problems.append("config must retain the exact five configured methods")
            methods = _EXPECTED_METHODS
            expected_scores = _recompute_scores(predictions, folds, methods)
            expected_deltas = _recompute_deltas(
                predictions,
                folds,
                methods,
                comparison_method=comparison,
                draws=draws,
                seed=seed,
            )
            if not _frames_equal(scores, expected_scores):
                problems.append("scores do not match predictions")
            if not _frames_equal(deltas, expected_deltas):
                problems.append("paired deltas do not match predictions")
            expected_gate = _recompute_gate(
                expected_scores,
                expected_deltas,
                methods=methods,
                comparison_method=comparison,
                required_years=required_years,
            )
            if manifest.get("gate") != expected_gate:
                problems.append("gate verdict does not match independently recomputed evidence")
            expected_summary = {
                "analysis": "spatial_surface_baseline",
                "inventory_generation_sha256": manifest.get("inventory_generation_sha256"),
                "output_rows": {name: frame.height for name, frame in tables.items()},
                "gate": expected_gate,
                "feeds_web": False,
                "limitations": list(SPATIAL_BASELINE_LIMITATIONS),
            }
            if summary != expected_summary:
                problems.append("summary semantics do not match independently verified evidence")
        except (KeyError, TypeError, ValueError, IndexError, pl.exceptions.PolarsError):
            problems.append("config or evidence cannot be independently recomputed")
    return list(dict.fromkeys(problems))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify one immutable spatial-surface baseline generation."
    )
    parser.add_argument("generation", type=Path, help="generation directory to verify")
    args = parser.parse_args(argv)
    problems = verify_generation(args.generation)
    if problems:
        for problem in problems:
            print(f"FAIL {problem}")
        return 1
    manifest = json.loads((args.generation / "manifest.json").read_text(encoding="utf-8"))
    print(f"PASS {manifest['generation_sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
