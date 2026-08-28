from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import Any

import polars as pl
import pytest

from tests.test_spatial_surface_baseline import _config, write_surface_fixture
from twair.analysis.spatial_surface_baseline import (
    decide_baseline_gate,
    paired_method_deltas,
    run_spatial_surface_baseline,
    score_predictions,
    write_spatial_surface_baseline_result,
)

SCRIPT = Path(__file__).parents[1] / "scripts" / "verify_spatial_surface_baseline.py"


def _load_verifier() -> ModuleType:
    spec = importlib.util.spec_from_file_location("verify_spatial_surface_baseline", SCRIPT)
    if spec is None or spec.loader is None:
        raise ImportError("spatial surface baseline verifier cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


VERIFIER = _load_verifier()
verify_generation = VERIFIER.verify_generation


def _canonical_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ),
        encoding="utf-8",
        newline="",
    )


@pytest.fixture(scope="module")
def valid_generation_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("spatial-verifier-source")
    data_root = write_surface_fixture(root / "data")
    result = run_spatial_surface_baseline(
        data_root=data_root,
        config=replace(_config(spatial_folds=2), min_train_stations=1),
        generated_at="2026-08-28T00:00:00+00:00",
    )
    written = write_spatial_surface_baseline_result(result, output_root=root / "output")
    return written["manifest"].parent


@pytest.fixture
def generation(valid_generation_template: Path, tmp_path: Path) -> Path:
    copied = tmp_path / valid_generation_template.name
    shutil.copytree(valid_generation_template, copied)
    return copied


def _cli(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _assert_rejected(path: Path, phrase: str) -> None:
    problems = verify_generation(path)
    completed = _cli(path)

    assert problems
    assert any(phrase in problem for problem in problems), problems
    assert completed.returncode == 1
    assert "FAIL" in completed.stdout


def _mutate_row(path: Path, expression: pl.Expr) -> None:
    frame = pl.read_parquet(path).with_row_index("_row").with_columns(expression).drop("_row")
    frame.write_parquet(path)


def _rewrite_generation_identity(generation: Path) -> Path:
    manifest_path = generation / "manifest.json"
    manifest: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["tables"] = {
        name: VERIFIER._table_identity(name, pl.read_parquet(generation / f"{name}.parquet"))
        for name in VERIFIER.SPATIAL_BASELINE_TABLE_SCHEMAS
    }
    manifest["members"] = {
        name: VERIFIER._file_identity(generation / name)
        for name in VERIFIER.SPATIAL_BASELINE_MEMBER_NAMES[:-1]
    }
    identity = {
        key: value
        for key, value in manifest.items()
        if key not in VERIFIER._VOLATILE_MANIFEST_FIELDS
    }
    generation_sha256 = VERIFIER._canonical_hash(identity)
    manifest["generation_sha256"] = generation_sha256
    _canonical_json(manifest_path, manifest)
    renamed = generation.with_name(generation_sha256)
    generation.replace(renamed)
    return renamed


def test_a_complete_generation_passes_independent_verification(generation: Path) -> None:
    manifest = json.loads((generation / "manifest.json").read_text(encoding="utf-8"))

    assert verify_generation(generation) == []
    completed = _cli(generation)

    assert completed.returncode == 0
    assert completed.stdout.strip() == f"PASS {manifest['generation_sha256']}"


def test_a_valid_generation_preserves_nan_as_invalid_non_finite_and_passes(tmp_path: Path) -> None:
    data_root = write_surface_fixture(tmp_path / "data")
    monthly_path = data_root / "processed" / "monthly" / "monthly.parquet"
    monthly = (
        pl.read_parquet(monthly_path)
        .with_row_index("_row")
        .with_columns(
            pl.when(pl.col("_row") == 0)
            .then(pl.lit(float("nan")))
            .otherwise(pl.col("mean"))
            .alias("mean")
        )
        .drop("_row")
    )
    monthly.write_parquet(monthly_path)
    result = run_spatial_surface_baseline(
        data_root=data_root,
        config=replace(_config(spatial_folds=2), min_train_stations=1),
        generated_at="2026-08-28T00:00:00+00:00",
    )

    assert result.panel.filter(pl.col("mean").is_nan())["target_state"].to_list() == [
        "invalid_non_finite"
    ]
    written = write_spatial_surface_baseline_result(result, output_root=tmp_path / "output")

    assert verify_generation(written["manifest"].parent) == []


def test_a_parquet_value_mutation_is_rejected_with_the_same_row_count(generation: Path) -> None:
    path = generation / "stations.parquet"
    before = pl.read_parquet(path).height
    _mutate_row(
        path,
        pl.when(pl.col("_row") == 0)
        .then(pl.col("lon") + 0.001)
        .otherwise(pl.col("lon"))
        .alias("lon"),
    )

    assert pl.read_parquet(path).height == before
    _assert_rejected(generation, "stations.parquet checksum")


def test_a_table_row_count_mutation_is_rejected_with_the_same_schema(generation: Path) -> None:
    path = generation / "panel.parquet"
    before = pl.read_parquet(path)
    before.tail(-1).write_parquet(path)

    assert pl.read_parquet(path).schema == before.schema
    _assert_rejected(generation, "panel row count")


def test_a_prediction_changed_without_its_error_is_rejected(generation: Path) -> None:
    path = generation / "predictions.parquet"
    predictions = pl.read_parquet(path)
    scored_row = (
        predictions.with_row_index("_row")
        .filter(pl.col("prediction_state") == "scored")["_row"]
        .item(0)
    )
    _mutate_row(
        path,
        pl.when(pl.col("_row") == scored_row)
        .then(pl.col("predicted") + 1.0)
        .otherwise(pl.col("predicted"))
        .alias("predicted"),
    )

    _assert_rejected(generation, "prediction error")


def test_a_score_changed_while_predictions_stay_fixed_is_rejected(generation: Path) -> None:
    path = generation / "scores.parquet"
    scores = pl.read_parquet(path).with_row_index("_row")
    scored_row = scores.filter(pl.col("station_clustered_mae").is_not_null())["_row"].item(0)
    _mutate_row(
        path,
        pl.when(pl.col("_row") == scored_row)
        .then(pl.col("station_clustered_mae") + 1.0)
        .otherwise(pl.col("station_clustered_mae"))
        .alias("station_clustered_mae"),
    )

    _assert_rejected(generation, "scores do not match predictions")


def test_a_changed_gate_verdict_is_rejected_without_trusting_summary(generation: Path) -> None:
    path = generation / "manifest.json"
    manifest: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    manifest["gate"]["state"] = "go" if manifest["gate"]["state"] == "stop" else "stop"
    _canonical_json(path, manifest)

    _assert_rejected(generation, "gate verdict")


def test_a_canonical_summary_mutation_is_rejected_semantically(generation: Path) -> None:
    path = generation / "summary.json"
    summary: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    summary["feeds_web"] = True
    summary["gate"] = {"state": "go", "winning_method": "unreviewed"}
    _canonical_json(path, summary)

    _assert_rejected(generation, "summary semantics")


def test_synchronized_prediction_removal_cannot_shrink_authoritative_denominators(
    generation: Path,
) -> None:
    config = replace(_config(spatial_folds=2), min_train_stations=1)
    folds = pl.read_parquet(generation / "folds.parquet")
    selected = folds.filter(pl.col("fold_state") == "eligible").row(0, named=True)
    target_filter = ~(
        (pl.col("evaluation") == selected["evaluation"])
        & (pl.col("fold_id") == selected["fold_id"])
        & (pl.col("year") == selected["year"])
        & (pl.col("month") == selected["month"])
        & (pl.col("target_station") == selected["target_station"])
    )
    predictions = pl.read_parquet(generation / "predictions.parquet").filter(target_filter)
    scores = score_predictions(predictions, config)
    deltas = paired_method_deltas(predictions, config)
    gate = decide_baseline_gate(scores, deltas, config)
    predictions.write_parquet(generation / "predictions.parquet")
    scores.write_parquet(generation / "scores.parquet")
    deltas.write_parquet(generation / "paired_deltas.parquet")
    summary_path = generation / "summary.json"
    summary: dict[str, Any] = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["output_rows"].update(
        predictions=predictions.height,
        scores=scores.height,
        paired_deltas=deltas.height,
    )
    summary["gate"] = gate
    _canonical_json(summary_path, summary)
    manifest_path = generation / "manifest.json"
    manifest: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["gate"] = gate
    _canonical_json(manifest_path, manifest)
    attacked = _rewrite_generation_identity(generation)

    _assert_rejected(attacked, "prediction grid")


@pytest.mark.parametrize("mutation", ["extra", "method_domain", "duplicate"])
def test_prediction_grid_rejects_extra_method_domain_and_duplicate_rows(
    generation: Path,
    mutation: str,
) -> None:
    path = generation / "predictions.parquet"
    predictions = pl.read_parquet(path)
    first = predictions.head(1)
    if mutation == "extra":
        first = first.with_columns(pl.lit("rogue_method").alias("method"))
        mutated = pl.concat([predictions, first])
    elif mutation == "method_domain":
        mutated = (
            predictions.with_row_index("_row")
            .with_columns(
                pl.when(pl.col("_row") == 0)
                .then(pl.lit("rogue_method"))
                .otherwise(pl.col("method"))
                .alias("method")
            )
            .drop("_row")
        )
    else:
        mutated = pl.concat([predictions, first])
    mutated.write_parquet(path)

    _assert_rejected(generation, "prediction grid")


def test_prediction_rows_must_copy_every_authoritative_fold_field(generation: Path) -> None:
    path = generation / "predictions.parquet"
    _mutate_row(
        path,
        pl.when(pl.col("_row") == 0)
        .then(pl.col("target_cluster") + 1)
        .otherwise(pl.col("target_cluster"))
        .alias("target_cluster"),
    )

    _assert_rejected(generation, "authoritative fold")


def test_manifest_cannot_redefine_the_exact_five_method_domain(generation: Path) -> None:
    path = generation / "manifest.json"
    manifest: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    manifest["config"]["validation"]["methods"][-1] = "rogue_method"
    _canonical_json(path, manifest)

    _assert_rejected(generation, "exact five configured methods")


def test_a_changed_manifest_input_hash_is_rejected(generation: Path) -> None:
    path = generation / "manifest.json"
    manifest: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    manifest["inputs"][0]["sha256"] = "0" * 64
    _canonical_json(path, manifest)

    _assert_rejected(generation, "generation identity")


def test_a_changed_generation_directory_name_is_rejected(generation: Path) -> None:
    renamed = generation.with_name("0" * 64)
    generation.replace(renamed)

    _assert_rejected(renamed, "directory name")


def test_an_unexpected_generation_member_is_rejected(generation: Path) -> None:
    (generation / "unexpected.txt").write_text("not reviewed", encoding="utf-8")

    _assert_rejected(generation, "member inventory")
