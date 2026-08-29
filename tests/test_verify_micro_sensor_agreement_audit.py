from __future__ import annotations

import ast
import copy
import json
from dataclasses import replace
from pathlib import Path

import polars as pl
import pytest
from scripts import verify_micro_sensor_agreement_audit as verifier

from tests.test_micro_sensor_agreement_audit import assembled_result_fixture
from twair.analysis import micro_sensor_agreement_audit as audit
from twair.net import sha256_file


@pytest.fixture
def generation(tmp_path: Path) -> Path:
    result = assembled_result_fixture()
    control_families = (
        "station_label",
        "target_shift",
        "satellite_context",
        "acquisition_density",
        "neighbor_exclusion",
    )
    result = replace(
        result,
        control_scores=pl.DataFrame(
            {
                "control": list(control_families),
                "state": ["scored"] * len(control_families),
                "value": [0.1] * len(control_families),
            }
        ),
        control_summary=pl.DataFrame(
            {
                "control": list(control_families),
                "state": ["complete"] * len(control_families),
                "observed_value": [0.1] * len(control_families),
            }
        ),
        manifest={
            "schema_version": 1,
            "analysis": "micro_sensor_agreement_audit",
        },
    )
    result = replace(
        result,
        manifest={**result.manifest, "generation_sha256": audit.result_identity(result)},
    )
    written = audit.write_micro_sensor_agreement_audit_result(
        result, output_root=tmp_path / "audit"
    )
    return written["manifest.json"].parent


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )


def _resign_generation(
    generation: Path,
    *,
    frames: dict[str, pl.DataFrame],
    summary: dict[str, object],
    manifest: dict[str, object],
) -> Path:
    for table_name, member_name in audit._AUDIT_TABLE_MEMBERS.items():
        frames[table_name].write_parquet(generation / member_name)
    _write_json(generation / "summary.json", summary)
    manifest["tables"] = {
        name: audit._manifest_table_identity(frame) for name, frame in frames.items()
    }
    manifest["members"] = {
        name: {
            "path": name,
            "bytes": (generation / name).stat().st_size,
            "sha256": sha256_file(generation / name),
        }
        for name in set(audit.AUDIT_MEMBER_NAMES) - {"manifest.json"}
    }
    result = audit.AgreementAuditResult(
        fold_audit=frames["fold_audit"],
        scores=frames["scores"],
        deltas=frames["deltas"],
        uncertainty=frames["uncertainty"],
        control_scores=frames["control_scores"],
        control_summary=frames["control_summary"],
        fusion_gate=frames["fusion_gate"],
        summary=summary,
        manifest=manifest,
    )
    identity = audit.result_identity(result)
    manifest["generation_sha256"] = identity
    _write_json(generation / "manifest.json", manifest)
    destination = generation.with_name(identity)
    generation.rename(destination)
    return destination


def mutate_and_resign_generation(generation: Path, mutation: str) -> Path:
    if mutation == "member_byte":
        with (generation / "scores.parquet").open("ab") as handle:
            handle.write(b"mutated")
        return generation
    if mutation == "missing_member":
        (generation / "scores.parquet").unlink()
        return generation
    if mutation == "extra_member":
        (generation / "extra.txt").write_text("extra", encoding="utf-8")
        return generation

    manifest = json.loads((generation / "manifest.json").read_text(encoding="utf-8"))
    summary: dict[str, object] = copy.deepcopy(
        json.loads((generation / "summary.json").read_text(encoding="utf-8"))
    )
    frames = {
        name: pl.read_parquet(generation / member)
        for name, member in audit._AUDIT_TABLE_MEMBERS.items()
    }
    if mutation == "row_count":
        frames["fold_audit"] = frames["fold_audit"].head(0)
    elif mutation == "schema":
        frames["scores"] = frames["scores"].with_columns(
            pl.col("value").cast(pl.String)
        )
    elif mutation == "fold_state":
        frames["fold_audit"] = frames["fold_audit"].with_columns(
            pl.lit("corrupted").alias("state")
        )
    elif mutation == "score":
        frames["scores"] = frames["scores"].with_columns(
            (pl.col("value") + 1.0).alias("value")
        )
    elif mutation == "delta":
        frames["deltas"] = frames["deltas"].with_columns(
            pl.when(pl.col("unit") == "device_day")
            .then(pl.col("value") + 1.0)
            .otherwise(pl.col("value"))
            .alias("value")
        )
    elif mutation == "membership_hash":
        frames["scores"] = frames["scores"].with_columns(
            pl.lit("bad").alias("membership_sha256")
        )
    elif mutation == "truth_hash":
        frames["scores"] = frames["scores"].with_columns(
            pl.lit("bad").alias("truth_sha256")
        )
    elif mutation == "missing_control_replicate":
        frames["control_scores"] = frames["control_scores"].head(0)
    elif mutation == "incorrect_gate_pass":
        frames["fusion_gate"] = frames["fusion_gate"].with_columns(
            pl.when(pl.col("condition_id") == "colocated_truth")
            .then(pl.lit("pass"))
            .otherwise(pl.col("state"))
            .alias("state")
        )
    elif mutation == "summary_contradiction":
        summary["overall_verdict"] = "go"
    else:
        raise AssertionError(f"unknown mutation: {mutation}")
    return _resign_generation(
        generation,
        frames=frames,
        summary=summary,
        manifest=manifest,
    )


def test_complete_generation_passes_independent_verification(generation: Path) -> None:
    assert verifier.verify_generation(generation) == []
    tree = ast.parse(Path(verifier.__file__).read_text(encoding="utf-8"))
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    source = Path(verifier.__file__).read_text(encoding="utf-8")
    prohibited = {
        "twair.analysis.micro_sensor_annual_agreement",
        "twair.analysis.micro_sensor_agreement_audit",
    }
    assert imports.isdisjoint(prohibited)
    assert all(name not in source for name in prohibited)


@pytest.mark.parametrize(
    "mutation",
    [
        "member_byte",
        "row_count",
        "schema",
        "missing_member",
        "extra_member",
        "fold_state",
        "score",
        "delta",
        "membership_hash",
        "truth_hash",
        "missing_control_replicate",
        "incorrect_gate_pass",
        "summary_contradiction",
    ],
)
def test_resigned_mutation_is_rejected(generation: Path, mutation: str) -> None:
    changed = mutate_and_resign_generation(generation, mutation)
    assert verifier.verify_generation(changed)


def test_verifier_cli_prints_one_pass_line_for_valid_generation(
    generation: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert verifier.main([str(generation)]) == 0
    captured = capsys.readouterr()
    assert captured.out == f"PASS {generation.name}\n"
    assert captured.err == ""


def test_verifier_cli_prints_no_pass_line_for_invalid_generation(
    generation: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (generation / "scores.parquet").unlink()
    assert verifier.main([str(generation)]) == 1
    captured = capsys.readouterr()
    assert "PASS" not in captured.out
    assert "member inventory" in captured.err
