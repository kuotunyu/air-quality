"""Independent verification tests for spatial covariate readiness generations."""

from __future__ import annotations

import copy
import json
import math
import subprocess
import sys
from datetime import date
from hashlib import sha256
from pathlib import Path
from typing import Any

import polars as pl
import pytest
from scripts.verify_spatial_covariate_readiness import verify_generation

import twair.analysis.spatial_covariate_readiness as readiness
from twair.analysis.spatial_covariate_readiness import (
    COVARIATE_READINESS_TABLE_SCHEMAS,
    FrozenInputs,
    InputFile,
    load_spatial_covariate_readiness_config,
)

BASELINE_GENERATION = "620b7ba088906611c191d0f371b5405f8096059cefc488306b6849b64588ef0f"
INVENTORY_GENERATION = "58e00bb5ab951c9afd1a95e9e98aacdab4e90762e32904ca6d79d198efe6d788"
TARGET_KEY = (
    "evaluation",
    "training_period",
    "train_year",
    "target_year",
    "month",
    "target_station",
)


def _config() -> readiness.CovariateReadinessConfig:
    return load_spatial_covariate_readiness_config(
        {
            "schema_version": 1,
            "analysis": {
                "years": [2024, 2025],
                "baseline_generation_sha256": BASELINE_GENERATION,
                "station_inventory_generation_sha256": INVENTORY_GENERATION,
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
                "evaluations": ["buffer_20km", "buffer_40km", "spatial_cluster"],
                "bootstrap_draws": 9999,
                "bootstrap_seed": 20260828,
            },
        }
    )


def _synthetic_frames() -> dict[str, pl.DataFrame]:
    station_names = (
        "alpha",
        "beta",
        "gamma",
        "delta",
        "epsilon",
        "zeta",
        "eta",
        "theta",
        "iota",
        "kappa",
    )
    station_values = {
        station: (120.0 + index * 0.1, 23.0) for index, station in enumerate(station_names)
    }
    targets: dict[tuple[str, date], float | None] = {
        (station, date(year, 1, 1)): float(10 * (year - 2023) + index)
        for index, station in enumerate(station_names)
        for year in (2024, 2025)
    }
    targets[("gamma", date(2025, 1, 1))] = None
    stations = pl.DataFrame(
        [
            {
                "station_name": station,
                "station_type_official": "general",
                "lon": coordinates[0],
                "lat": coordinates[1],
            }
            for station, coordinates in station_values.items()
        ],
        schema=COVARIATE_READINESS_TABLE_SCHEMAS["stations"],
    )
    panel_rows: list[dict[str, object]] = []
    covariate_rows: list[dict[str, object]] = []
    for (station, month), observed in targets.items():
        lon, lat = station_values[station]
        target_state = "observed" if observed is not None else "withheld"
        panel_rows.append(
            {
                "station_name": station,
                "station_type_official": "general",
                "lon": lon,
                "lat": lat,
                "month": month,
                "pollutant": "PM2.5",
                "mean": observed,
                "meets_threshold": observed is not None,
                "target_state": target_state,
            }
        )
        covariate_rows.append(
            {
                "station_name": station,
                "month": month,
                "target_state": target_state,
                "PM2.5": observed,
                "lon": lon,
                "lat": lat,
                "x_m": (lon - 120.0) * 100_000.0,
                "y_m": 0.0,
                "month_sin": 0.5,
                "month_cos": math.sqrt(3.0) / 2.0,
                "era5_blh_mean_m": 100.0,
                "era5_u10_mean_m_s": 3.0,
                "era5_v10_mean_m_s": 4.0,
                "era5_wind_speed_mean_m_s": 5.0,
                "era5_t2m_mean_k": 295.0,
                "era5_dewpoint_depression_mean_k": 2.0,
                "era5_sp_mean_pa": 100_000.0,
                "maiac_aod": 0.1,
                "s5p_no2": 0.2,
                "s5p_so2": 0.3,
            }
        )
    panel = pl.DataFrame(panel_rows, schema=COVARIATE_READINESS_TABLE_SCHEMAS["panel"])
    covariates = pl.DataFrame(
        covariate_rows, schema=COVARIATE_READINESS_TABLE_SCHEMAS["covariates"]
    )
    fold_rows: list[dict[str, object]] = []
    for evaluation in ("buffer_20km", "buffer_40km", "spatial_cluster"):
        for (station, month), observed in targets.items():
            periods = [("same_year", month.year, month.year)]
            if month.year == 2025:
                periods.append(("2024_to_2025", 2024, 2025))
            for training_period, train_year, target_year in periods:
                train_stations = sorted(
                    candidate
                    for candidate in station_values
                    if candidate != station
                    and targets[(candidate, date(train_year, 1, 1))] is not None
                )
                target_state = "observed" if observed is not None else "withheld"
                fold_rows.append(
                    {
                        "evaluation": evaluation,
                        "training_period": training_period,
                        "train_year": train_year,
                        "target_year": target_year,
                        "month": month,
                        "target_station": station,
                        "target_cluster": tuple(station_values).index(station),
                        "target_state": target_state,
                        "observed": observed,
                        "train_stations": train_stations,
                        "n_train_stations": len(train_stations),
                        "n_model_train_rows": len(train_stations),
                        "n_same_month_train_rows": len(train_stations),
                        "fold_state": (
                            "eligible" if observed is not None else "unscored_target_withheld"
                        ),
                        "fold_reason": None if observed is not None else "target_state=withheld",
                    }
                )
    folds = pl.DataFrame(fold_rows, schema=COVARIATE_READINESS_TABLE_SCHEMAS["folds"])
    offsets = {"idw2": 2.0, "covariate_gbm": 1.0, "covariate_gbm_idw2": 0.5}
    prediction_rows: list[dict[str, object]] = []
    for fold in folds.iter_rows(named=True):
        for method, offset in offsets.items():
            scored = fold["fold_state"] == "eligible"
            observed = fold["observed"]
            prediction_rows.append(
                {
                    **fold,
                    "method": method,
                    "predicted": (
                        float(observed) + offset if scored and observed is not None else None
                    ),
                    "prediction_state": "scored" if scored else str(fold["fold_state"]),
                    "failure_type": None,
                    "error": offset if scored else None,
                }
            )
    predictions = pl.DataFrame(
        prediction_rows, schema=COVARIATE_READINESS_TABLE_SCHEMAS["predictions"]
    )
    config = _config()
    return {
        "stations": stations,
        "panel": panel,
        "covariates": covariates,
        "folds": folds,
        "predictions": predictions,
        "scores": readiness.score_readiness_predictions(predictions, config),
        "paired_deltas": readiness.paired_readiness_deltas(predictions, config),
    }


@pytest.fixture
def generation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    frames = _synthetic_frames()
    source = tmp_path / "inputs" / "source.bin"
    source.parent.mkdir()
    source.write_bytes(b"frozen-input")
    inputs = FrozenInputs(
        stations=frames["stations"],
        panel=frames["panel"],
        support=pl.DataFrame(),
        baseline_folds=pl.DataFrame(),
        input_files=(
            InputFile(
                path=source,
                bytes=source.stat().st_size,
                sha256=sha256(source.read_bytes()).hexdigest(),
            ),
        ),
        baseline_generation_sha256=BASELINE_GENERATION,
        station_inventory_generation_sha256=INVENTORY_GENERATION,
    )
    monkeypatch.setattr(readiness, "load_frozen_inputs", lambda *_args, **_kwargs: inputs)
    monkeypatch.setattr(
        readiness, "assemble_covariates", lambda *_args, **_kwargs: frames["covariates"]
    )
    monkeypatch.setattr(
        readiness, "build_covariate_fold_ledger", lambda *_args, **_kwargs: frames["folds"]
    )
    monkeypatch.setattr(
        readiness, "predict_readiness_methods", lambda *_args, **_kwargs: frames["predictions"]
    )
    monkeypatch.setattr(readiness, "_exact_git_state", lambda: ("f" * 40, False))
    result = readiness.run_spatial_covariate_readiness(
        data_root=tmp_path,
        config=_config(),
        generated_at="2026-08-28T00:00:00+00:00",
    )
    written = readiness.write_spatial_covariate_readiness_result(
        result,
        output_root=tmp_path / "o" / "a",
    )
    return written["manifest"].parent


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _resign_generation(generation: Path, *tables: str, summary_changed: bool = False) -> Path:
    manifest_path = generation / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for table in tables:
        frame = pl.read_parquet(generation / f"{table}.parquet")
        manifest["tables"][table] = readiness._table_identity(table, frame)
        payload = (generation / f"{table}.parquet").read_bytes()
        manifest["members"][f"{table}.parquet"] = {
            "bytes": len(payload),
            "sha256": sha256(payload).hexdigest(),
        }
    if summary_changed:
        payload = (generation / "summary.json").read_bytes()
        manifest["members"]["summary.json"] = {
            "bytes": len(payload),
            "sha256": sha256(payload).hexdigest(),
        }
    identity = readiness._manifest_identity(manifest)
    new_name = readiness._canonical_hash(identity)
    manifest["generation_sha256"] = new_name
    manifest_path.write_bytes(_canonical_json(manifest))
    renamed = generation.with_name(new_name)
    generation.rename(renamed)
    return renamed


def _rewrite_table(generation: Path, table: str, rows: list[dict[str, Any]]) -> Path:
    pl.DataFrame(rows, schema=COVARIATE_READINESS_TABLE_SCHEMAS[table]).write_parquet(
        generation / f"{table}.parquet"
    )
    return _resign_generation(generation, table)


def _assert_problem(generation: Path, relationship: str) -> None:
    problems = verify_generation(generation)
    assert problems
    assert any(relationship in problem for problem in problems), problems


def test_a_complete_generation_passes_independent_verification(generation: Path) -> None:
    assert verify_generation(generation) == []


@pytest.mark.parametrize("member", ["scores.parquet", "unexpected.txt"])
def test_exact_member_inventory_is_required(generation: Path, member: str) -> None:
    if member == "unexpected.txt":
        (generation / member).write_text("unexpected", encoding="utf-8")
    else:
        (generation / member).unlink()

    _assert_problem(generation, "member inventory")


def test_parquet_byte_mutation_is_rejected_even_when_row_count_is_unchanged(
    generation: Path,
) -> None:
    path = generation / "covariates.parquet"
    frame = pl.read_parquet(path).with_columns(
        pl.when(pl.int_range(pl.len()) == 0)
        .then(pl.col("maiac_aod") + 1.0)
        .otherwise(pl.col("maiac_aod"))
        .alias("maiac_aod")
    )
    frame.write_parquet(path)

    _assert_problem(generation, "member hash")


def test_missing_authoritative_fold_key_is_rejected(generation: Path) -> None:
    rows = pl.read_parquet(generation / "folds.parquet").to_dicts()[1:]
    mutated = _rewrite_table(generation, "folds", rows)
    _assert_problem(mutated, "authoritative fold grid")


def test_duplicate_method_target_key_is_rejected(generation: Path) -> None:
    rows = pl.read_parquet(generation / "predictions.parquet").to_dicts()
    rows.append(copy.deepcopy(rows[0]))
    mutated = _rewrite_table(generation, "predictions", rows)
    _assert_problem(mutated, "prediction key")


def test_prediction_change_without_corresponding_error_change_is_rejected(
    generation: Path,
) -> None:
    rows = pl.read_parquet(generation / "predictions.parquet").to_dicts()
    scored = next(row for row in rows if row["prediction_state"] == "scored")
    scored["predicted"] = float(scored["predicted"]) + 10.0
    mutated = _rewrite_table(generation, "predictions", rows)
    _assert_problem(mutated, "error arithmetic")


def test_score_change_while_predictions_remain_fixed_is_rejected(generation: Path) -> None:
    rows = pl.read_parquet(generation / "scores.parquet").to_dicts()
    rows[0]["station_clustered_mae"] = 999.0
    mutated = _rewrite_table(generation, "scores", rows)
    _assert_problem(mutated, "recomputed scores")


def test_paired_delta_change_is_rejected(generation: Path) -> None:
    rows = pl.read_parquet(generation / "paired_deltas.parquet").to_dicts()
    rows[0]["median_station_mae_delta"] = 999.0
    mutated = _rewrite_table(generation, "paired_deltas", rows)
    _assert_problem(mutated, "recomputed paired deltas")


def test_gate_verdict_change_is_rejected(generation: Path) -> None:
    manifest_path = generation / "manifest.json"
    summary_path = generation / "summary.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    manifest["gate"]["state"] = "stop"
    summary["gate"]["state"] = "stop"
    manifest_path.write_bytes(_canonical_json(manifest))
    summary_path.write_bytes(_canonical_json(summary))
    mutated = _resign_generation(generation, summary_changed=True)
    _assert_problem(mutated, "gate verdict")


def test_target_station_in_training_stations_is_rejected(generation: Path) -> None:
    fold_rows = pl.read_parquet(generation / "folds.parquet").to_dicts()
    target = fold_rows[0]
    changed_key = tuple(target[column] for column in TARGET_KEY)
    changed_stations = sorted([*target["train_stations"], target["target_station"]])
    target["train_stations"] = changed_stations
    target["n_train_stations"] = len(changed_stations)
    prediction_rows = pl.read_parquet(generation / "predictions.parquet").to_dicts()
    for row in prediction_rows:
        if tuple(row[column] for column in TARGET_KEY) == changed_key:
            row["train_stations"] = changed_stations
            row["n_train_stations"] = len(changed_stations)
    generation = _rewrite_table(generation, "folds", fold_rows)
    mutated = _rewrite_table(generation, "predictions", prediction_rows)
    _assert_problem(mutated, "held-station isolation")


def test_forward_row_declaring_2025_training_truth_is_rejected(generation: Path) -> None:
    fold_rows = pl.read_parquet(generation / "folds.parquet").to_dicts()
    target = next(row for row in fold_rows if row["training_period"] == "2024_to_2025")
    changed_key = tuple(target[column] for column in TARGET_KEY)
    target["train_year"] = 2025
    prediction_rows = pl.read_parquet(generation / "predictions.parquet").to_dicts()
    for row in prediction_rows:
        if tuple(row[column] for column in TARGET_KEY) == changed_key:
            row["train_year"] = 2025
    generation = _rewrite_table(generation, "folds", fold_rows)
    mutated = _rewrite_table(generation, "predictions", prediction_rows)
    _assert_problem(mutated, "forward training year")


def test_withheld_target_changed_to_scored_is_rejected(generation: Path) -> None:
    rows = pl.read_parquet(generation / "predictions.parquet").to_dicts()
    withheld = next(row for row in rows if row["target_state"] == "withheld")
    withheld["predicted"] = 1.0
    withheld["prediction_state"] = "scored"
    withheld["error"] = 1.0
    mutated = _rewrite_table(generation, "predictions", rows)
    _assert_problem(mutated, "withheld target")


def test_bound_input_hash_is_recomputed(generation: Path) -> None:
    manifest_path = generation / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["inputs"][0]["sha256"] = "0" * 64
    manifest_path.write_bytes(_canonical_json(manifest))
    mutated = _resign_generation(generation)
    _assert_problem(mutated, "bound input identity")


def test_baseline_generation_must_match_normalized_config(generation: Path) -> None:
    manifest_path = generation / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["baseline_generation_sha256"] = "0" * 64
    manifest_path.write_bytes(_canonical_json(manifest))
    mutated = _resign_generation(generation)
    _assert_problem(mutated, "baseline generation")


def test_generation_directory_name_must_match_identity(generation: Path) -> None:
    renamed = generation.with_name("0" * 64)
    generation.rename(renamed)
    _assert_problem(renamed, "directory identity")


@pytest.mark.parametrize("mutation", ["limitation", "feeds_web"])
def test_claim_boundary_is_fail_closed(generation: Path, mutation: str) -> None:
    manifest_path = generation / "manifest.json"
    summary_path = generation / "summary.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if mutation == "limitation":
        manifest["claim_boundary"]["limitations"] = ["not the required limitations"]
        summary["limitations"] = ["not the required limitations"]
    else:
        manifest["claim_boundary"]["feeds_web"] = True
        summary["feeds_web"] = True
    manifest_path.write_bytes(_canonical_json(manifest))
    summary_path.write_bytes(_canonical_json(summary))
    mutated = _resign_generation(generation, summary_changed=True)
    _assert_problem(mutated, "claim boundary")


def test_cli_prints_exactly_one_pass_line_for_a_valid_generation(generation: Path) -> None:
    completed = subprocess.run(
        [sys.executable, "scripts/verify_spatial_covariate_readiness.py", str(generation)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0
    assert completed.stdout == f"PASS {generation.name}\n"
    assert completed.stderr == ""


def test_cli_exits_one_without_a_pass_line_for_an_invalid_generation(generation: Path) -> None:
    (generation / "scores.parquet").unlink()
    completed = subprocess.run(
        [sys.executable, "scripts/verify_spatial_covariate_readiness.py", str(generation)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 1
    assert "PASS" not in completed.stdout
