from __future__ import annotations

import json
import re
import sys
from datetime import date, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

TABLE_MEMBERS = {
    "fold_audit": "fold_audit.parquet",
    "scores": "scores.parquet",
    "deltas": "deltas.parquet",
    "uncertainty": "uncertainty.parquet",
    "control_scores": "control_scores.parquet",
    "control_summary": "control_summary.parquet",
    "fusion_gate": "fusion_gate.parquet",
}
MEMBER_NAMES = (*TABLE_MEMBERS.values(), "summary.json", "manifest.json")
CONDITION_IDS = (
    "colocated_truth",
    "four_seasons",
    "validation_regimes",
    "multi_year_drift",
    "prediction_location_time",
    "primary_scale_improvement",
    "field_spatial_buffer",
)
EXPECTED_GATE_STATES = {
    "colocated_truth": "fail",
    "four_seasons": "fail",
    "validation_regimes": "fail",
    "multi_year_drift": "fail",
    "prediction_location_time": "fail",
    "primary_scale_improvement": "fail",
    "field_spatial_buffer": "unmet",
}
SHA256 = re.compile(r"[0-9a-f]{64}")
TRANSIENT_MANIFEST_FIELDS = {
    "generated_at",
    "complete",
    "generation_sha256",
    "members",
    "tables",
}


class VerificationError(RuntimeError):
    pass


def canonical_hash(value: object) -> str:
    payload = (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode()
    return sha256(payload).hexdigest()


def _json_scalar(value: object) -> object:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, np.generic):
        return value.item()
    return value


def semantic_frame_hash(frame: pl.DataFrame) -> str:
    rows = (
        []
        if frame.is_empty()
        else [
            {name: _json_scalar(value) for name, value in row.items()}
            for row in frame.sort(*frame.columns, nulls_last=True).to_dicts()
        ]
    )
    return canonical_hash(
        {
            "schema": [(name, str(dtype)) for name, dtype in frame.schema.items()],
            "rows": rows,
        }
    )


def _reject_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON constant: {value}")


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"), parse_constant=_reject_constant
        )
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise VerificationError(f"{label} is unreadable") from exc
    if not isinstance(value, dict):
        raise VerificationError(f"{label} must be an object")
    return value


def _sha256_file(path: Path) -> str:
    digest = sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise VerificationError(f"member is unreadable: {path.name}") from exc
    return digest.hexdigest()


def _is_link_like(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", None)
    return path.is_symlink() or (is_junction is not None and is_junction())


def _file_identity(path: Path, *, parent: Path) -> tuple[int, str]:
    try:
        resolved = path.resolve(strict=True)
        stat = path.stat()
    except OSError as exc:
        raise VerificationError(f"member is unreadable: {path.name}") from exc
    if (
        _is_link_like(path)
        or not path.is_file()
        or resolved.parent != parent
        or stat.st_nlink != 1
    ):
        raise VerificationError(f"member is linked or not ordinary: {path.name}")
    return stat.st_size, _sha256_file(path)


def _validate_inventory(directory: Path) -> dict[str, Path]:
    try:
        resolved = directory.resolve(strict=True)
        entries = tuple(directory.iterdir())
    except OSError as exc:
        raise VerificationError("generation directory is unreadable") from exc
    if _is_link_like(directory) or resolved != directory.absolute() or not directory.is_dir():
        raise VerificationError("generation directory is linked or outside")
    if {entry.name for entry in entries} != set(MEMBER_NAMES):
        raise VerificationError("member inventory changed")
    paths = {name: directory / name for name in MEMBER_NAMES}
    for path in paths.values():
        _file_identity(path, parent=resolved)
    return paths


def _declared_member(value: object, *, name: str) -> tuple[int, str]:
    if not isinstance(value, dict) or set(value) != {"path", "bytes", "sha256"}:
        raise VerificationError(f"member declaration changed: {name}")
    size = value.get("bytes")
    digest = value.get("sha256")
    if value.get("path") != name:
        raise VerificationError(f"member path changed: {name}")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise VerificationError(f"member bytes changed: {name}")
    if not isinstance(digest, str) or SHA256.fullmatch(digest) is None:
        raise VerificationError(f"member hash declaration changed: {name}")
    return size, digest


def _result_identity(
    manifest: dict[str, Any],
    frames: dict[str, pl.DataFrame],
    summary: dict[str, Any],
) -> str:
    manifest_identity = {
        key: value
        for key, value in manifest.items()
        if key not in TRANSIENT_MANIFEST_FIELDS
    }
    return canonical_hash(
        {
            "manifest": manifest_identity,
            "tables": {
                name: semantic_frame_hash(frames[name]) for name in TABLE_MEMBERS
            },
            "summary": summary,
        }
    )


def _require_columns(frame: pl.DataFrame, columns: set[str], *, label: str) -> None:
    missing = sorted(columns - set(frame.columns))
    if missing:
        raise VerificationError(f"{label} schema is missing columns: {missing}")


def _validate_hash_columns(frame: pl.DataFrame, *, label: str) -> None:
    for column in (name for name in frame.columns if name.endswith("_sha256")):
        invalid = frame.filter(
            pl.col(column).is_not_null()
            & ~pl.col(column).str.contains(r"^[0-9a-f]{64}$")
        )
        if invalid.height:
            raise VerificationError(f"{label} contains an invalid {column}")


def _validate_fold_audit(frame: pl.DataFrame) -> None:
    _require_columns(frame, {"evaluation", "fold", "state"}, label="fold audit")
    if frame.is_empty() or frame.select("evaluation", "fold").n_unique() != frame.height:
        raise VerificationError("fold audit row inventory changed")
    allowed = {
        "scored",
        "unscored_empty_train",
        "unscored_insufficient_train",
        "unscored_empty_test",
        "unscored_single_target",
    }
    if not set(frame["state"]) <= allowed:
        raise VerificationError("fold audit state changed")
    _validate_hash_columns(frame, label="fold audit")


def _validate_scores(frame: pl.DataFrame) -> None:
    _require_columns(
        frame,
        {"scope", "evaluation", "fold", "model", "unit", "metric", "state", "value"},
        label="scores",
    )
    if frame.is_empty() or frame.schema["value"] != pl.Float64:
        raise VerificationError("score schema or row inventory changed")
    if frame.filter(pl.col("value").is_not_null() & ~pl.col("value").is_finite()).height:
        raise VerificationError("score value is non-finite")
    _validate_hash_columns(frame, label="scores")


def _validate_deltas(frame: pl.DataFrame) -> None:
    _require_columns(
        frame,
        {
            "scope",
            "evaluation",
            "fold",
            "model",
            "baseline_model",
            "unit",
            "metric",
            "state",
            "value",
        },
        label="deltas",
    )
    if frame.is_empty() or frame.schema["value"] != pl.Float64:
        raise VerificationError("delta schema or row inventory changed")
    if frame.filter(pl.col("value").is_not_null() & ~pl.col("value").is_finite()).height:
        raise VerificationError("delta value is non-finite")
    _validate_hash_columns(frame, label="deltas")


def _validate_score_delta_relationships(
    scores: pl.DataFrame, deltas: pl.DataFrame
) -> None:
    key = ("scope", "evaluation", "fold", "unit", "metric")
    scored = scores.filter(pl.col("state") == "scored")
    baseline = scored.select(
        *key,
        pl.col("model").alias("baseline_model"),
        pl.col("value").alias("baseline_value"),
    )
    candidates = scored.select(
        *key,
        "model",
        pl.col("value").alias("candidate_value"),
    )
    comparable = deltas.filter(pl.col("state") == "scored").join(
        baseline, on=(*key, "baseline_model"), how="inner", nulls_equal=True
    ).join(candidates, on=(*key, "model"), how="inner", nulls_equal=True)
    if comparable.height and comparable.filter(
        (pl.col("value") - (pl.col("candidate_value") - pl.col("baseline_value"))).abs()
        > 1e-12
    ).height:
        raise VerificationError("delta does not equal candidate minus comparator score")


def _validate_uncertainty(frame: pl.DataFrame, deltas: pl.DataFrame) -> None:
    _require_columns(
        frame,
        {
            "candidate",
            "comparator",
            "unit",
            "state",
            "observed_delta_rmse",
            "delta_rmse_ci_low",
            "delta_rmse_ci_high",
        },
        label="uncertainty",
    )
    if frame.is_empty():
        raise VerificationError("uncertainty row inventory changed")
    values = ("observed_delta_rmse", "delta_rmse_ci_low", "delta_rmse_ci_high")
    if frame.filter(
        pl.any_horizontal(pl.col(name).is_null() | ~pl.col(name).is_finite() for name in values)
    ).height:
        raise VerificationError("uncertainty value is non-finite")
    observed = deltas.filter(
        (pl.col("scope") == "overall")
        & (pl.col("unit") == "station_day")
        & (pl.col("metric") == "rmse")
        & (pl.col("state") == "scored")
    ).select(
        pl.col("model").alias("candidate"),
        pl.col("baseline_model").alias("comparator"),
        pl.col("value").alias("expected"),
    )
    paired = frame.join(observed, on=("candidate", "comparator"), how="inner")
    if paired.height and paired.filter(
        (pl.col("observed_delta_rmse") - pl.col("expected")).abs() > 1e-12
    ).height:
        raise VerificationError("uncertainty observed delta changed")
    _validate_hash_columns(frame, label="uncertainty")


def _validate_controls(scores: pl.DataFrame, summary: pl.DataFrame) -> None:
    _require_columns(scores, {"control", "state", "value"}, label="control scores")
    _require_columns(summary, {"control", "state", "value"}, label="control summary")
    if scores.is_empty() or summary.is_empty():
        raise VerificationError("control row inventory changed")
    if set(scores["control"]) != set(summary["control"]):
        raise VerificationError("control family inventory changed")
    if not set(summary["state"]) <= {"complete"}:
        raise VerificationError("control summary is incomplete")
    _validate_hash_columns(scores, label="control scores")
    _validate_hash_columns(summary, label="control summary")


def _validate_gate(frame: pl.DataFrame) -> None:
    _require_columns(
        frame,
        {"condition_id", "state", "reason", "evidence_sha256", "overall_verdict"},
        label="fusion gate",
    )
    if (
        frame.height != len(CONDITION_IDS)
        or frame["condition_id"].n_unique() != len(CONDITION_IDS)
        or set(frame["condition_id"]) != set(CONDITION_IDS)
    ):
        raise VerificationError("fusion gate condition inventory changed")
    states = dict(frame.select("condition_id", "state").iter_rows())
    if states != EXPECTED_GATE_STATES:
        raise VerificationError("fusion gate state changed")
    if set(frame["overall_verdict"]) != {"stop"}:
        raise VerificationError("fusion gate verdict changed")
    _validate_hash_columns(frame, label="fusion gate")


def _validate_summary(
    summary: dict[str, Any],
    scores: pl.DataFrame,
    deltas: pl.DataFrame,
    gate: pl.DataFrame,
) -> None:
    if summary.get("overall_verdict") != "stop" or set(gate["overall_verdict"]) != {
        summary.get("overall_verdict")
    }:
        raise VerificationError("summary contradicts fusion gate")
    primary = summary.get("primary_station_day_rmse")
    if isinstance(primary, dict):
        selected = scores.filter(
            (pl.col("scope") == "overall")
            & (pl.col("evaluation") == "held_station")
            & (pl.col("unit") == "station_day")
            & (pl.col("metric") == "rmse")
            & (pl.col("state") == "scored")
        )
        if primary != dict(selected.select("model", "value").iter_rows()):
            raise VerificationError("summary contradicts station-day scores")
    secondary = summary.get("secondary_device_day_delta_rmse")
    if isinstance(secondary, dict):
        selected = deltas.filter(
            (pl.col("scope") == "overall")
            & (pl.col("evaluation") == "held_station")
            & (pl.col("unit") == "device_day")
            & (pl.col("metric") == "rmse")
            & (pl.col("state") == "scored")
        )
        if secondary != dict(selected.select("model", "value").iter_rows()):
            raise VerificationError("summary contradicts device-day deltas")


def _verify(directory: Path) -> None:
    directory = directory.absolute()
    paths = _validate_inventory(directory)
    parent = directory.resolve(strict=True)
    before = {name: _file_identity(path, parent=parent) for name, path in paths.items()}
    manifest = _read_json(paths["manifest.json"], label="manifest")
    summary = _read_json(paths["summary.json"], label="summary")
    generation = manifest.get("generation_sha256")
    if (
        manifest.get("schema_version") != 1
        or manifest.get("analysis") != "micro_sensor_agreement_audit"
        or manifest.get("complete") is not True
        or not isinstance(generation, str)
        or SHA256.fullmatch(generation) is None
        or directory.name != generation
    ):
        raise VerificationError("manifest or generation directory identity changed")
    members = manifest.get("members")
    expected_members = set(MEMBER_NAMES) - {"manifest.json"}
    if not isinstance(members, dict) or set(members) != expected_members:
        raise VerificationError("member declaration inventory changed")
    for name in expected_members:
        if _declared_member(members[name], name=name) != _file_identity(
            paths[name], parent=parent
        ):
            raise VerificationError(f"member bytes or hash changed: {name}")
    declarations = manifest.get("tables")
    if not isinstance(declarations, dict) or set(declarations) != set(TABLE_MEMBERS):
        raise VerificationError("table declaration inventory changed")
    frames: dict[str, pl.DataFrame] = {}
    for table_name, member_name in TABLE_MEMBERS.items():
        declaration = declarations[table_name]
        if not isinstance(declaration, dict) or set(declaration) != {
            "rows",
            "schema",
            "semantic_sha256",
        }:
            raise VerificationError(f"table declaration changed: {table_name}")
        frame = pl.read_parquet(paths[member_name])
        if (
            declaration.get("rows") != frame.height
            or declaration.get("schema")
            != {name: str(dtype) for name, dtype in frame.schema.items()}
            or declaration.get("semantic_sha256") != semantic_frame_hash(frame)
        ):
            raise VerificationError(f"table declaration mismatch: {table_name}")
        frames[table_name] = frame
    if _result_identity(manifest, frames, summary) != generation:
        raise VerificationError("generation identity changed")
    _validate_fold_audit(frames["fold_audit"])
    _validate_scores(frames["scores"])
    _validate_deltas(frames["deltas"])
    _validate_score_delta_relationships(frames["scores"], frames["deltas"])
    _validate_uncertainty(frames["uncertainty"], frames["deltas"])
    _validate_controls(frames["control_scores"], frames["control_summary"])
    _validate_gate(frames["fusion_gate"])
    _validate_summary(
        summary, frames["scores"], frames["deltas"], frames["fusion_gate"]
    )
    after = {name: _file_identity(path, parent=parent) for name, path in paths.items()}
    if before != after:
        raise VerificationError("generation changed during verification")
    _validate_inventory(directory)


def verify_generation(path: Path) -> list[str]:
    try:
        _verify(path)
    except (
        VerificationError,
        OSError,
        UnicodeError,
        ValueError,
        TypeError,
        json.JSONDecodeError,
        pl.exceptions.PolarsError,
    ) as exc:
        return [str(exc) or exc.__class__.__name__]
    return []


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1:
        print("usage: verify_micro_sensor_agreement_audit.py GENERATION", file=sys.stderr)
        return 2
    generation = Path(arguments[0])
    problems = verify_generation(generation)
    if problems:
        for problem in problems:
            print(problem, file=sys.stderr)
        return 1
    print(f"PASS {generation.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
