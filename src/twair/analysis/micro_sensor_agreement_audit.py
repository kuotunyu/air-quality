"""Independently audit the frozen Q4 micro-sensor agreement."""

from __future__ import annotations

import importlib
import json
import math
import re
from dataclasses import dataclass
from datetime import timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
from sklearn.linear_model import Ridge
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from twair.analysis.micro_sensor_annual_readiness import (
    ANNUAL_CALENDAR_SCHEMA,
    ANNUAL_COHORT_THRESHOLD_SCHEMA,
    ANNUAL_DEVICE_COHORT_SCHEMA,
    ANNUAL_DEVICE_DAY_SCHEMA,
    ANNUAL_EXCLUSION_SCHEMA,
)
from twair.config import ConfigError, load_conf
from twair.ingest.station_meta import resolve_station_geo
from twair.net import sha256_file

threadpool_limits: Any = importlib.import_module("threadpoolctl").threadpool_limits

_SHA256 = re.compile(r"[0-9a-f]{64}")
_ANNUAL_IDENTITY_FIELDS = (
    "schema_version",
    "analysis",
    "config",
    "inputs",
    "checkpoint_inventory",
    "claim_boundary",
    "output_rows",
    "members",
    "summary_file",
)
_ANNUAL_MANIFEST_FIELDS = {
    *_ANNUAL_IDENTITY_FIELDS,
    "complete",
    "generated_at",
    "generation_sha256",
    "git_sha",
    "git_dirty",
    "checkpoint_run",
}
_AGREEMENT_IDENTITY_FIELDS = (
    "schema_version",
    "analysis",
    "annual_generation_sha256",
    "panel_generation_sha256",
    "evaluation_generation_sha256",
    "panel_manifest",
    "evaluation_manifest",
    "checkpoint_inventory",
    "config",
    "claim_boundary",
    "output_rows",
    "schemas",
    "members",
    "summary_file",
    "summary_sha256",
    "git_sha",
    "git_dirty",
)
_AGREEMENT_MANIFEST_FIELDS = {
    *_AGREEMENT_IDENTITY_FIELDS,
    "complete",
    "generated_at",
    "generation_sha256",
}
_ANNUAL_SCHEMAS = {
    "calendar_coverage": ANNUAL_CALENDAR_SCHEMA,
    "device_days": ANNUAL_DEVICE_DAY_SCHEMA,
    "device_cohorts": ANNUAL_DEVICE_COHORT_SCHEMA,
    "cohort_thresholds": ANNUAL_COHORT_THRESHOLD_SCHEMA,
    "exclusions": ANNUAL_EXCLUSION_SCHEMA,
}
_AGREEMENT_MEMBERS = (
    "calendar",
    "paired_days",
    "exclusions",
    "fold_membership",
    "folds",
    "predictions",
    "scores",
    "deltas",
)
_SATELLITE_SCHEMA = {
    "source": "String",
    "station_name": "String",
    "month": "Date",
    "satellite_value": "Float64",
    "satellite_unit": "String",
    "ground_value": "Float64",
    "ground_unit": "String",
    "satellite_observed": "Boolean",
    "ground_row_present": "Boolean",
    "ground_meets_threshold": "Boolean",
    "ground_observed": "Boolean",
    "ground_withheld": "Boolean",
    "pair_observed": "Boolean",
    "collection_id": "String",
    "band": "String",
    "sample_scale_m": "Int32",
}
_COORDINATE_FIELDS = (
    "station_name",
    "lon",
    "lat",
    "geo_source",
    "geo_source_record_namespace",
    "geo_source_record_id",
)
_CLAIM_BOUNDARY = {
    "q4_nearby_reference_agreement_only": True,
    "station_day_primary": True,
    "device_day_secondary": True,
    "validated_calibration": False,
    "sensor_fusion": False,
    "colocated_ground_truth": False,
    "annual_generalization": False,
    "seasonal_generalization": False,
    "causal_analysis": False,
    "source_attribution": False,
    "high_resolution_field": False,
    "population_exposure": False,
}
_EXPECTED_ANALYSIS: dict[str, object] = {
    "protocol_revision": 1,
    "annual_generation_sha256": (
        "c74ec40428a907e98821efbaf36c36386d2c1b99de69791b49f157eb7947e5bb"
    ),
    "annual_manifest_sha256": (
        "eb37676fd8d357af4080048828a3f33b8de212a6dfb46752ea059cbab4c6e89d"
    ),
    "annual_git_sha": "e4839bc",
    "agreement_generation_sha256": (
        "df61b34157461f8eca13a119bab88136902aa4e70d8d9794a56a20e422e4c624"
    ),
    "agreement_manifest_sha256": (
        "ffcc92cc03af86834a0a2e37d6e8a82491467648a3db6f423605901ced179957"
    ),
    "agreement_summary_sha256": (
        "18a83123d79f1fa911ee29870c869e35c4ed0dd2113c469be26268ce87f7b782"
    ),
    "agreement_git_sha": "b7bff3e",
    "satellite_generation_sha256": (
        "58e00bb5ab951c9afd1a95e9e98aacdab4e90762e32904ca6d79d198efe6d788"
    ),
    "satellite_year": 2025,
    "satellite_panel_bytes": 33866,
    "satellite_panel_sha256": (
        "aa34e69720098e8868dc1e004f77b3f8e425288089f867bd310e08366d0198e2"
    ),
    "reviewed_geography_sha256": (
        "72146c443374303ad95f69e820e4f067b8378a3b0167a03e67c29c68b63c1f32"
    ),
    "reviewed_airzone_sha256": (
        "911a4967b9e9ab3c3af9821f53d8ba99eb5c1a8bc5c8384ce9ca19867dc8ee54"
    ),
    "primary_radius_km": 0.5,
    "ridge_alpha": 1.0,
    "permutation_draws": 999,
    "permutation_seed": 20260829,
    "bootstrap_draws": 1999,
    "bootstrap_seed": 20260830,
    "target_time_shifts_days": [7, 14, 28],
    "neighbor_exclusion_buffers_km": [0.5, 1.0, 2.0],
    "threads": 1,
    "memory_limit_gb": 6,
    "claim_boundary": _CLAIM_BOUNDARY,
}
_INTEGER_FIELDS = {
    "protocol_revision",
    "satellite_year",
    "satellite_panel_bytes",
    "permutation_draws",
    "permutation_seed",
    "bootstrap_draws",
    "bootstrap_seed",
    "threads",
    "memory_limit_gb",
}
_FLOAT_FIELDS = {"primary_radius_km", "ridge_alpha"}
_SHA_FIELDS = {name for name in _EXPECTED_ANALYSIS if name.endswith("sha256")}
_ANNUAL_EXPECTED_ROWS = {
    "calendar_coverage": 365,
    "device_days": 2_775_609,
    "device_cohorts": 11_556,
    "cohort_thresholds": 320,
    "exclusions": 8,
}
_AGREEMENT_EXPECTED_ROWS = {
    "calendar": 365,
    "paired_days": 45_260,
    "exclusions": 35_698,
    "fold_membership": 277_298,
    "folds": 29,
    "predictions": 28_686,
    "scores": 192,
    "deltas": 128,
}
CORE_FEATURES: dict[str, tuple[str, ...]] = {
    "pooled_micro_ridge": ("micro_pm25_mean",),
    "pooled_weather_ridge": (
        "micro_pm25_mean",
        "micro_humidity_mean",
        "micro_temperature_mean",
    ),
}
_CORE_MODELS = ("raw_micro", *CORE_FEATURES)
_METRICS = ("rmse", "mae", "r2", "bias", "absolute_bias")
_FOLD_COUNT = 5
_QUARTERS = (1, 2, 3, 4)
CONTROL_CODES = {"station_label": 1, "target_shift": 2, "satellite_context": 3}
AUDIT_UNCERTAINTY_SCHEMA: tuple[
    tuple[str, pl.DataType | type[pl.DataType]], ...
] = (
    ("candidate", pl.String),
    ("comparator", pl.String),
    ("unit", pl.String),
    ("state", pl.String),
    ("draws", pl.Int64),
    ("seed", pl.Int64),
    ("clusters", pl.Int64),
    ("station_days", pl.Int64),
    ("membership_sha256", pl.String),
    ("truth_sha256", pl.String),
    ("observed_delta_rmse", pl.Float64),
    ("delta_rmse_ci_low", pl.Float64),
    ("delta_rmse_ci_high", pl.Float64),
    ("observed_delta_mae", pl.Float64),
    ("delta_mae_ci_low", pl.Float64),
    ("delta_mae_ci_high", pl.Float64),
)


@dataclass(frozen=True, slots=True)
class AgreementAuditConfig:
    protocol_revision: int
    annual_generation_sha256: str
    annual_manifest_sha256: str
    annual_git_sha: str
    agreement_generation_sha256: str
    agreement_manifest_sha256: str
    agreement_summary_sha256: str
    agreement_git_sha: str
    satellite_generation_sha256: str
    satellite_year: int
    satellite_panel_bytes: int
    satellite_panel_sha256: str
    reviewed_geography_sha256: str
    reviewed_airzone_sha256: str
    primary_radius_km: float
    ridge_alpha: float
    permutation_draws: int
    permutation_seed: int
    bootstrap_draws: int
    bootstrap_seed: int
    target_time_shifts_days: tuple[int, ...]
    neighbor_exclusion_buffers_km: tuple[float, ...]
    threads: int
    memory_limit_gb: int
    claim_boundary: tuple[tuple[str, bool], ...]


@dataclass(frozen=True, slots=True)
class InputFile:
    role: str
    path: Path
    relative_path: str
    bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class FrozenAuditInputs:
    annual_manifest: dict[str, Any]
    agreement_manifest: dict[str, Any]
    agreement_summary: dict[str, Any]
    agreement_calendar: pl.DataFrame
    agreement_paired_days: pl.DataFrame
    agreement_exclusions: pl.DataFrame
    agreement_fold_membership: pl.DataFrame
    agreement_folds: pl.DataFrame
    agreement_predictions: pl.DataFrame
    agreement_scores: pl.DataFrame
    agreement_deltas: pl.DataFrame
    satellite_panel: pl.DataFrame
    geography: pl.DataFrame
    input_files: tuple[InputFile, ...]


def _mapping(value: object, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ConfigError(f"{label} must be a mapping with string keys")
    return value


def _exact_keys(value: dict[str, Any], expected: set[str], *, label: str) -> None:
    unknown = sorted(set(value) - expected)
    missing = sorted(expected - set(value))
    if unknown:
        raise ConfigError(f"{label} has unknown field(s): {unknown}")
    if missing:
        raise ConfigError(f"{label} is missing field(s): {missing}")


def _require_frozen_value(name: str, value: object, expected: object) -> None:
    label = f"analysis.{name}"
    if name in _INTEGER_FIELDS:
        if isinstance(value, bool) or not isinstance(value, int) or value != expected:
            raise ConfigError(f"{label} changed from the reviewed protocol")
        return
    if name in _FLOAT_FIELDS:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{label} must be a finite number")
        converted = float(value)
        if not math.isfinite(converted) or converted != expected:
            raise ConfigError(f"{label} changed from the reviewed protocol")
        return
    if name in _SHA_FIELDS:
        if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
            raise ConfigError(f"{label} must be a lowercase SHA-256")
        if value != expected:
            raise ConfigError(f"{label} changed from the reviewed protocol")
        return
    if name == "claim_boundary":
        boundary = _mapping(value, label=label)
        _exact_keys(boundary, set(_CLAIM_BOUNDARY), label=label)
        if any(type(item) is not bool for item in boundary.values()) or boundary != expected:
            raise ConfigError(f"{label} changed from the reviewed protocol")
        return
    if name == "target_time_shifts_days":
        if (
            not isinstance(value, list)
            or any(isinstance(item, bool) or not isinstance(item, int) for item in value)
            or value != expected
        ):
            raise ConfigError(f"{label} changed from the reviewed protocol")
        return
    if name == "neighbor_exclusion_buffers_km":
        if not isinstance(value, list) or any(
            isinstance(item, bool)
            or not isinstance(item, (int, float))
            or not math.isfinite(float(item))
            for item in value
        ):
            raise ConfigError(f"{label} must contain finite numbers")
        if [float(item) for item in value] != expected:
            raise ConfigError(f"{label} changed from the reviewed protocol")
        return
    if not isinstance(value, str) or value != expected:
        raise ConfigError(f"{label} changed from the reviewed protocol")


def load_micro_sensor_agreement_audit_config(
    payload: dict[str, Any] | None = None,
) -> AgreementAuditConfig:
    raw = payload if payload is not None else load_conf("micro_sensor_agreement_audit")
    top = _mapping(raw, label="micro_sensor_agreement_audit")
    _exact_keys(top, {"schema_version", "analysis"}, label="micro_sensor_agreement_audit")
    schema_version = top["schema_version"]
    if isinstance(schema_version, bool) or not isinstance(schema_version, int) or schema_version != 1:
        raise ConfigError("micro_sensor_agreement_audit.schema_version must be one")
    analysis = _mapping(top["analysis"], label="micro_sensor_agreement_audit.analysis")
    _exact_keys(analysis, set(_EXPECTED_ANALYSIS), label="micro_sensor_agreement_audit.analysis")
    for name, expected in _EXPECTED_ANALYSIS.items():
        _require_frozen_value(name, analysis[name], expected)
    return AgreementAuditConfig(
        protocol_revision=analysis["protocol_revision"],
        annual_generation_sha256=analysis["annual_generation_sha256"],
        annual_manifest_sha256=analysis["annual_manifest_sha256"],
        annual_git_sha=analysis["annual_git_sha"],
        agreement_generation_sha256=analysis["agreement_generation_sha256"],
        agreement_manifest_sha256=analysis["agreement_manifest_sha256"],
        agreement_summary_sha256=analysis["agreement_summary_sha256"],
        agreement_git_sha=analysis["agreement_git_sha"],
        satellite_generation_sha256=analysis["satellite_generation_sha256"],
        satellite_year=analysis["satellite_year"],
        satellite_panel_bytes=analysis["satellite_panel_bytes"],
        satellite_panel_sha256=analysis["satellite_panel_sha256"],
        reviewed_geography_sha256=analysis["reviewed_geography_sha256"],
        reviewed_airzone_sha256=analysis["reviewed_airzone_sha256"],
        primary_radius_km=float(analysis["primary_radius_km"]),
        ridge_alpha=float(analysis["ridge_alpha"]),
        permutation_draws=analysis["permutation_draws"],
        permutation_seed=analysis["permutation_seed"],
        bootstrap_draws=analysis["bootstrap_draws"],
        bootstrap_seed=analysis["bootstrap_seed"],
        target_time_shifts_days=tuple(analysis["target_time_shifts_days"]),
        neighbor_exclusion_buffers_km=tuple(
            float(value) for value in analysis["neighbor_exclusion_buffers_km"]
        ),
        threads=analysis["threads"],
        memory_limit_gb=analysis["memory_limit_gb"],
        claim_boundary=tuple((key, _CLAIM_BOUNDARY[key]) for key in _CLAIM_BOUNDARY),
    )


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


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON constant: {value}")


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=_reject_json_constant,
        )
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"frozen input {label} is unreadable") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"frozen input {label} must be an object")
    return value


def _is_link_like(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", None)
    return path.is_symlink() or (is_junction is not None and is_junction())


def _ordinary_file_identity(path: Path, *, parent: Path, label: str) -> tuple[int, str]:
    try:
        resolved = path.resolve(strict=True)
        stat = path.stat()
    except OSError as exc:
        raise RuntimeError(f"frozen input {label} is unreadable") from exc
    if (
        _is_link_like(path)
        or not path.is_file()
        or resolved.parent != parent
        or stat.st_nlink != 1
    ):
        raise RuntimeError(f"frozen input {label} must be one ordinary file")
    return stat.st_size, sha256_file(path)


def _generation_directory(path: Path, *, label: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError(f"frozen input {label} generation is unreadable") from exc
    if _is_link_like(path) or not resolved.is_dir() or resolved != path:
        raise RuntimeError(f"frozen input {label} generation must be an ordinary directory")
    return resolved


def _validate_inventory(directory: Path, expected: set[str], *, label: str) -> None:
    try:
        entries = tuple(directory.iterdir())
    except OSError as exc:
        raise RuntimeError(f"frozen input {label} inventory is unreadable") from exc
    if {entry.name for entry in entries} != expected:
        raise RuntimeError(f"frozen input {label} inventory changed")
    for entry in entries:
        _ordinary_file_identity(entry, parent=directory, label=f"{label} {entry.name}")


def _manifest_identity(
    manifest: dict[str, Any],
    fields: tuple[str, ...],
    *,
    label: str,
) -> str:
    try:
        identity = {field: manifest[field] for field in fields}
    except KeyError as exc:
        raise RuntimeError(f"frozen input {label} identity is incomplete") from exc
    try:
        return canonical_hash(identity)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"frozen input {label} identity is not canonical") from exc


def _declared_identity(value: object, *, expected_path: str, label: str) -> tuple[int, str]:
    if not isinstance(value, dict) or set(value) != {"path", "bytes", "sha256"}:
        raise RuntimeError(f"frozen input {label} declaration changed")
    size = value.get("bytes")
    digest = value.get("sha256")
    if value.get("path") != expected_path:
        raise RuntimeError(f"frozen input {label} path changed")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise RuntimeError(f"frozen input {label} byte count changed")
    if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
        raise RuntimeError(f"frozen input {label} SHA-256 changed")
    return size, digest


def _input_file(
    root: Path,
    path: Path,
    *,
    role: str,
    expected: tuple[int, str] | None = None,
) -> InputFile:
    observed = _ordinary_file_identity(path, parent=path.parent, label=role)
    if expected is not None and observed != expected:
        raise RuntimeError(f"frozen input {role} identity changed")
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError as exc:
        raise RuntimeError(f"frozen input {role} is outside the data root") from exc
    return InputFile(
        role=role,
        path=path,
        relative_path=relative,
        bytes=observed[0],
        sha256=observed[1],
    )


def _row_count(path: Path) -> int:
    try:
        return int(pl.scan_parquet(path).select(pl.len()).collect().item())
    except (OSError, pl.exceptions.PolarsError) as exc:
        raise RuntimeError(f"frozen input {path.name} Parquet is unreadable") from exc


def _schema_strings(path: Path) -> dict[str, str]:
    try:
        return {name: str(dtype) for name, dtype in pl.read_parquet_schema(path).items()}
    except (OSError, pl.exceptions.PolarsError) as exc:
        raise RuntimeError(f"frozen input {path.name} schema is unreadable") from exc


def _load_frame(path: Path, *, label: str) -> pl.DataFrame:
    try:
        return pl.read_parquet(path)
    except (OSError, pl.exceptions.PolarsError) as exc:
        raise RuntimeError(f"frozen input {label} Parquet is unreadable") from exc


def _load_annual(
    root: Path,
    config: AgreementAuditConfig,
) -> tuple[dict[str, Any], tuple[InputFile, ...]]:
    directory = _generation_directory(
        root
        / "outputs"
        / "micro_sensor_annual_readiness"
        / "generations"
        / config.annual_generation_sha256,
        label="annual readiness",
    )
    if directory.name != config.annual_generation_sha256:
        raise RuntimeError("frozen input annual readiness generation changed")
    expected_files = {
        "manifest.json",
        "summary.json",
        *(f"{name}.parquet" for name in _ANNUAL_SCHEMAS),
    }
    _validate_inventory(directory, expected_files, label="annual readiness")
    manifest_path = directory / "manifest.json"
    manifest_file = _input_file(root, manifest_path, role="annual_manifest")
    if manifest_file.sha256 != config.annual_manifest_sha256:
        raise RuntimeError("frozen input annual manifest hash changed")
    manifest = _read_json(manifest_path, label="annual manifest")
    if set(manifest) != _ANNUAL_MANIFEST_FIELDS:
        raise RuntimeError("frozen input annual manifest fields changed")
    if (
        manifest.get("complete") is not True
        or manifest.get("schema_version") != 1
        or manifest.get("analysis") != "annual_micro_sensor_readiness"
        or manifest.get("generation_sha256") != config.annual_generation_sha256
        or manifest.get("git_sha") != config.annual_git_sha
        or _manifest_identity(manifest, _ANNUAL_IDENTITY_FIELDS, label="annual manifest")
        != config.annual_generation_sha256
    ):
        raise RuntimeError("frozen input annual manifest identity changed")
    manifest_inputs = manifest.get("inputs")
    if (
        not isinstance(manifest_inputs, dict)
        or manifest_inputs.get("reviewed_geography_sha256")
        != config.reviewed_geography_sha256
    ):
        raise RuntimeError("frozen input annual geography binding changed")
    members = manifest.get("members")
    output_rows = manifest.get("output_rows")
    if (
        not isinstance(members, dict)
        or set(members) != set(_ANNUAL_SCHEMAS)
        or output_rows != _ANNUAL_EXPECTED_ROWS
    ):
        raise RuntimeError("frozen input annual member contract changed")
    files: list[InputFile] = [manifest_file]
    for name, expected_schema in _ANNUAL_SCHEMAS.items():
        path = directory / f"{name}.parquet"
        expected = _declared_identity(
            members[name], expected_path=path.name, label=f"annual {name}"
        )
        files.append(_input_file(root, path, role=f"annual_{name}", expected=expected))
        observed_schema = _schema_strings(path)
        required_schema = {field: str(dtype) for field, dtype in expected_schema}
        if observed_schema != required_schema:
            raise RuntimeError(f"frozen input annual {name} schema changed")
        if _row_count(path) != _ANNUAL_EXPECTED_ROWS[name]:
            raise RuntimeError(f"frozen input annual {name} row count changed")
    summary_path = directory / "summary.json"
    summary_expected = _declared_identity(
        manifest.get("summary_file"), expected_path="summary.json", label="annual summary"
    )
    files.append(_input_file(root, summary_path, role="annual_summary", expected=summary_expected))
    summary = _read_json(summary_path, label="annual summary")
    if "output_rows" in summary and summary["output_rows"] != _ANNUAL_EXPECTED_ROWS:
        raise RuntimeError("frozen input annual summary row counts changed")
    if _ordinary_file_identity(
        manifest_path, parent=directory, label="annual manifest"
    ) != (manifest_file.bytes, manifest_file.sha256) or _read_json(
        manifest_path, label="annual manifest"
    ) != manifest:
        raise RuntimeError("frozen input annual manifest changed during read")
    _validate_inventory(directory, expected_files, label="annual readiness")
    return manifest, tuple(files)


def _load_agreement(
    root: Path,
    config: AgreementAuditConfig,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, pl.DataFrame], tuple[InputFile, ...]]:
    directory = _generation_directory(
        root
        / "outputs"
        / "micro_sensor_annual_agreement"
        / "generations"
        / config.agreement_generation_sha256,
        label="agreement",
    )
    if directory.name != config.agreement_generation_sha256:
        raise RuntimeError("frozen input agreement generation changed")
    expected_files = {
        "manifest.json",
        "summary.json",
        *(f"{name}.parquet" for name in _AGREEMENT_MEMBERS),
    }
    _validate_inventory(directory, expected_files, label="agreement")
    manifest_path = directory / "manifest.json"
    manifest_file = _input_file(root, manifest_path, role="agreement_manifest")
    if manifest_file.sha256 != config.agreement_manifest_sha256:
        raise RuntimeError("frozen input agreement manifest hash changed")
    manifest = _read_json(manifest_path, label="agreement manifest")
    if set(manifest) != _AGREEMENT_MANIFEST_FIELDS:
        raise RuntimeError("frozen input agreement manifest fields changed")
    if (
        manifest.get("complete") is not True
        or manifest.get("schema_version") != 1
        or manifest.get("analysis") != "q4_supported_cross_station_agreement"
        or manifest.get("annual_generation_sha256") != config.annual_generation_sha256
        or manifest.get("generation_sha256") != config.agreement_generation_sha256
        or manifest.get("git_sha") != config.agreement_git_sha
        or _manifest_identity(manifest, _AGREEMENT_IDENTITY_FIELDS, label="agreement manifest")
        != config.agreement_generation_sha256
    ):
        raise RuntimeError("frozen input agreement manifest identity changed")
    members = manifest.get("members")
    schemas = manifest.get("schemas")
    output_rows = manifest.get("output_rows")
    if (
        not isinstance(members, dict)
        or set(members) != set(_AGREEMENT_MEMBERS)
        or not isinstance(schemas, dict)
        or set(schemas) != set(_AGREEMENT_MEMBERS)
        or output_rows != _AGREEMENT_EXPECTED_ROWS
    ):
        raise RuntimeError("frozen input agreement member contract changed")
    files: list[InputFile] = [manifest_file]
    frames: dict[str, pl.DataFrame] = {}
    for name in _AGREEMENT_MEMBERS:
        path = directory / f"{name}.parquet"
        expected = _declared_identity(
            members[name], expected_path=path.name, label=f"agreement {name}"
        )
        files.append(_input_file(root, path, role=f"agreement_{name}", expected=expected))
        declared_schema = schemas[name]
        if not isinstance(declared_schema, dict) or not all(
            isinstance(field, str) and isinstance(dtype, str)
            for field, dtype in declared_schema.items()
        ):
            raise RuntimeError(f"frozen input agreement {name} schema declaration changed")
        if _schema_strings(path) != declared_schema:
            raise RuntimeError(f"frozen input agreement {name} schema changed")
        frames[name] = _load_frame(path, label=f"agreement {name}")
        if frames[name].height != _AGREEMENT_EXPECTED_ROWS[name]:
            raise RuntimeError(f"frozen input agreement {name} row count changed")
    summary_path = directory / "summary.json"
    summary_expected = _declared_identity(
        manifest.get("summary_file"), expected_path="summary.json", label="agreement summary"
    )
    files.append(
        _input_file(root, summary_path, role="agreement_summary", expected=summary_expected)
    )
    summary = _read_json(summary_path, label="agreement summary")
    summary_hash = canonical_hash(summary)
    if (
        summary_hash != config.agreement_summary_sha256
        or manifest.get("summary_sha256") != summary_hash
    ):
        raise RuntimeError("frozen input agreement summary identity changed")
    if "output_rows" in summary and summary["output_rows"] != _AGREEMENT_EXPECTED_ROWS:
        raise RuntimeError("frozen input agreement summary row counts changed")
    if _ordinary_file_identity(
        manifest_path, parent=directory, label="agreement manifest"
    ) != (manifest_file.bytes, manifest_file.sha256) or _read_json(
        manifest_path, label="agreement manifest"
    ) != manifest:
        raise RuntimeError("frozen input agreement manifest changed during read")
    _validate_inventory(directory, expected_files, label="agreement")
    return manifest, summary, frames, tuple(files)


def _load_satellite(
    root: Path,
    config: AgreementAuditConfig,
) -> tuple[pl.DataFrame, InputFile]:
    path = (
        root
        / "outputs"
        / "m8_satellite"
        / "generations"
        / config.satellite_generation_sha256
        / f"year={config.satellite_year}"
        / "panel.parquet"
    )
    try:
        parent = path.parent.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError("frozen input satellite panel directory is unreadable") from exc
    observed = _ordinary_file_identity(path, parent=parent, label="satellite panel")
    expected = (config.satellite_panel_bytes, config.satellite_panel_sha256)
    if observed != expected:
        raise RuntimeError("frozen input satellite panel identity changed")
    if _schema_strings(path) != _SATELLITE_SCHEMA:
        raise RuntimeError("frozen input satellite panel schema changed")
    panel = _load_frame(path, label="satellite panel")
    if _ordinary_file_identity(path, parent=parent, label="satellite panel") != observed:
        raise RuntimeError("frozen input satellite panel changed during read")
    return panel, _input_file(root, path, role="satellite_panel", expected=expected)


def _load_geography(config: AgreementAuditConfig) -> pl.DataFrame:
    geography = resolve_station_geo()
    airzone_fields = (*_COORDINATE_FIELDS, "airzone_official")
    missing = sorted(set(airzone_fields) - set(geography.columns))
    if missing:
        raise RuntimeError(f"frozen input reviewed geography is missing fields: {missing}")
    selected = geography.select(*airzone_fields)
    coordinate_identity = selected.select(*_COORDINATE_FIELDS)
    if (
        selected["station_name"].n_unique() != selected.height
        or coordinate_identity.select(pl.any_horizontal(pl.all().is_null()).any()).item()
        or selected.filter(~pl.col("lon").is_finite() | ~pl.col("lat").is_finite()).height
    ):
        raise RuntimeError("frozen input reviewed geography rows changed")
    coordinate_hash = canonical_hash(
        coordinate_identity.sort("station_name").to_dicts()
    )
    airzone_hash = canonical_hash(selected.sort("station_name").to_dicts())
    if coordinate_hash != config.reviewed_geography_sha256:
        raise RuntimeError("frozen input reviewed geography identity changed")
    if airzone_hash != config.reviewed_airzone_sha256:
        raise RuntimeError("frozen input reviewed air-zone identity changed")
    return geography


def load_frozen_agreement_audit_inputs(
    data_root: Path,
    config: AgreementAuditConfig,
) -> FrozenAuditInputs:
    try:
        root = data_root.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError("frozen input data root is unreadable") from exc
    if _is_link_like(data_root) or not root.is_dir():
        raise RuntimeError("frozen input data root must be an ordinary directory")
    annual_manifest, annual_files = _load_annual(root, config)
    agreement_manifest, agreement_summary, frames, agreement_files = _load_agreement(
        root, config
    )
    satellite_panel, satellite_file = _load_satellite(root, config)
    geography = _load_geography(config)
    return FrozenAuditInputs(
        annual_manifest=annual_manifest,
        agreement_manifest=agreement_manifest,
        agreement_summary=agreement_summary,
        agreement_calendar=frames["calendar"],
        agreement_paired_days=frames["paired_days"],
        agreement_exclusions=frames["exclusions"],
        agreement_fold_membership=frames["fold_membership"],
        agreement_folds=frames["folds"],
        agreement_predictions=frames["predictions"],
        agreement_scores=frames["scores"],
        agreement_deltas=frames["deltas"],
        satellite_panel=satellite_panel,
        geography=geography,
        input_files=(*annual_files, *agreement_files, satellite_file),
    )


def _frame_digest(frame: pl.DataFrame, *, truth: bool = False) -> str:
    identity = (
        "radius_km",
        "date",
        "device_id",
        "station_name",
        "station_fold",
        "quarter",
    )
    columns = (*identity, "ground_pm25_mean") if truth else identity
    records = (
        frame.select(*columns)
        .sort(*identity)
        .with_columns(pl.col("date").cast(pl.String))
        .to_dicts()
    )
    return canonical_hash(records)


def _station_fold_membership(
    paired_days: pl.DataFrame,
    geography: pl.DataFrame,
) -> pl.DataFrame:
    stations = paired_days.select("station_name").unique()
    inventory = stations.join(
        geography.select("station_name", "airzone_official"),
        on="station_name",
        how="left",
    )
    if inventory["airzone_official"].null_count():
        raise RuntimeError("agreement reproduction mismatch: cohort air-zone is missing")
    rows: list[dict[str, object]] = []
    position = 0
    strata = sorted(
        inventory["airzone_official"].unique().to_list(),
        key=lambda value: (value is None, "" if value is None else str(value)),
    )
    for stratum in strata:
        selected = inventory.filter(
            pl.col("airzone_official").is_null()
            if stratum is None
            else pl.col("airzone_official") == stratum
        ).sort("station_name")
        for station in selected["station_name"]:
            rows.append(
                {
                    "station_name": station,
                    "airzone_official": stratum,
                    "station_fold": position % _FOLD_COUNT,
                }
            )
            position += 1
    membership = pl.DataFrame(rows).sort("station_name")
    if sorted(membership["station_fold"].unique().to_list()) != list(range(_FOLD_COUNT)):
        raise RuntimeError("agreement reproduction mismatch: station fold is empty")
    return membership


def _eligible_agreement_rows(
    inputs: FrozenAuditInputs,
    config: AgreementAuditConfig,
) -> pl.DataFrame:
    required = {
        "radius_km",
        "date",
        "device_id",
        "station_name",
        "quarter",
        "micro_pm25_mean",
        "micro_humidity_mean",
        "micro_temperature_mean",
        "ground_pm25_mean",
        "reason",
    }
    missing = sorted(required - set(inputs.agreement_paired_days.columns))
    if missing:
        raise RuntimeError(f"agreement reproduction mismatch: missing columns {missing}")
    eligible = inputs.agreement_paired_days.filter(pl.col("reason") == "eligible")
    if eligible.is_empty() or eligible.filter(
        pl.col("radius_km") != config.primary_radius_km
    ).height:
        raise RuntimeError("agreement reproduction mismatch: primary cohort changed")
    if eligible.select("radius_km", "date", "device_id").n_unique() != eligible.height:
        raise RuntimeError("agreement reproduction mismatch: eligible identity is duplicated")
    numeric = (
        "micro_pm25_mean",
        "micro_humidity_mean",
        "micro_temperature_mean",
        "ground_pm25_mean",
    )
    if eligible.filter(
        pl.any_horizontal(
            pl.col(column).is_null() | ~pl.col(column).is_finite() for column in numeric
        )
    ).height:
        raise RuntimeError("agreement reproduction mismatch: model value is non-finite")
    return eligible


def _fold_state(train: pl.DataFrame, test: pl.DataFrame) -> str:
    if train.is_empty():
        return "unscored_empty_train"
    if train.height < 2 or train["ground_pm25_mean"].n_unique() < 2:
        return "unscored_insufficient_train"
    if test.is_empty():
        return "unscored_empty_test"
    if test["ground_pm25_mean"].n_unique() < 2:
        return "unscored_single_target"
    return "scored"


def _bind_source_predictions(
    memberships: pl.DataFrame,
    source: pl.DataFrame,
) -> pl.DataFrame:
    expected_models = set(_CORE_MODELS)
    if set(source["model"].unique()) != expected_models:
        raise RuntimeError("agreement reproduction mismatch: source model inventory changed")
    keys = (
        "evaluation",
        "fold",
        "radius_km",
        "date",
        "device_id",
        "station_name",
        "station_fold",
        "quarter",
    )
    if source.select(*keys, "model").n_unique() != source.height:
        raise RuntimeError("agreement reproduction mismatch: source prediction is duplicated")
    truth = source.group_by(*keys).agg(
        pl.col("y_true").n_unique().alias("_truth_values"),
        pl.col("y_true").first().alias("source_y_true"),
    )
    if truth.filter(pl.col("_truth_values") != 1).height:
        raise RuntimeError("agreement reproduction mismatch: source truth changed by model")
    truth = truth.drop("_truth_values")
    pivoted = source.select(*keys, "model", "y_pred").pivot(
        on="model",
        index=keys,
        values="y_pred",
    )
    pivoted = pivoted.rename(
        {model: f"source_{model}_y_pred" for model in _CORE_MODELS}
    )
    enriched = memberships.join(pivoted, on=keys, how="left").join(
        truth, on=keys, how="left"
    )
    scored_test = enriched.filter(
        (pl.col("fold_state") == "scored") & (pl.col("role") == "test")
    )
    reference_columns = ("source_y_true", *(f"source_{model}_y_pred" for model in _CORE_MODELS))
    if scored_test.select(
        pl.any_horizontal(pl.col(column).is_null() for column in reference_columns).any()
    ).item():
        raise RuntimeError("agreement reproduction mismatch: source prediction is missing")
    return enriched


def reconstruct_agreement_folds(
    inputs: FrozenAuditInputs,
    config: AgreementAuditConfig,
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Return normalized fold-audit rows and independently rebuilt memberships."""
    airzone_fields = (*_COORDINATE_FIELDS, "airzone_official")
    geography = inputs.geography.select(*airzone_fields)
    observed_airzone_hash = canonical_hash(geography.sort("station_name").to_dicts())
    if observed_airzone_hash != config.reviewed_airzone_sha256:
        raise RuntimeError("reviewed air-zone identity changed")
    eligible = _eligible_agreement_rows(inputs, config)
    station_folds = _station_fold_membership(inputs.agreement_paired_days, geography)
    source = eligible.join(
        station_folds.select("station_name", "station_fold"),
        on="station_name",
        how="left",
    )
    split_frames: list[pl.DataFrame] = []
    for station_fold in range(_FOLD_COUNT):
        split_frames.append(
            source.with_columns(
                pl.lit("held_station").alias("evaluation"),
                pl.lit(f"held_station_{station_fold:02d}").alias("fold"),
                pl.when(pl.col("station_fold") == station_fold)
                .then(pl.lit("test"))
                .otherwise(pl.lit("train"))
                .alias("role"),
            )
        )
    for quarter in _QUARTERS:
        split_frames.append(
            source.with_columns(
                pl.lit("held_quarter").alias("evaluation"),
                pl.lit(f"held_quarter_{quarter:02d}").alias("fold"),
                pl.when(pl.col("quarter") == quarter)
                .then(pl.lit("test"))
                .otherwise(pl.lit("train"))
                .alias("role"),
            )
        )
    for station_fold in range(_FOLD_COUNT):
        for quarter in _QUARTERS:
            split_frames.append(
                source.with_columns(
                    pl.lit("joint").alias("evaluation"),
                    pl.lit(f"joint_{station_fold:02d}_{quarter:02d}").alias("fold"),
                    pl.when(
                        (pl.col("station_fold") == station_fold)
                        & (pl.col("quarter") == quarter)
                    )
                    .then(pl.lit("test"))
                    .when(
                        (pl.col("station_fold") != station_fold)
                        & (pl.col("quarter") != quarter)
                    )
                    .then(pl.lit("train"))
                    .otherwise(pl.lit("excluded"))
                    .alias("role"),
                )
            )
    memberships = pl.concat(split_frames).select(
        "evaluation", "fold", "role", "station_fold", *eligible.columns
    )
    bound: list[pl.DataFrame] = []
    audit_rows: list[dict[str, object]] = []
    for split in memberships.partition_by("evaluation", "fold"):
        train = split.filter(pl.col("role") == "train")
        test = split.filter(pl.col("role") == "test")
        state = _fold_state(train, test)
        train_hash = _frame_digest(train)
        test_hash = _frame_digest(test)
        truth_hash = _frame_digest(test, truth=True)
        train_targets = train["ground_pm25_mean"].n_unique()
        test_targets = test["ground_pm25_mean"].n_unique()
        enriched = split.with_columns(
            pl.lit(state).alias("fold_state"),
            pl.lit(state).alias("fold_reason"),
            pl.lit(train.height, dtype=pl.Int64).alias("train_rows"),
            pl.lit(test.height, dtype=pl.Int64).alias("test_rows"),
            pl.lit(train_targets, dtype=pl.Int64).alias("train_unique_targets"),
            pl.lit(test_targets, dtype=pl.Int64).alias("test_unique_targets"),
            pl.lit(train_hash).alias("train_membership_sha256"),
            pl.lit(test_hash).alias("test_membership_sha256"),
            pl.lit(truth_hash).alias("test_truth_sha256"),
        )
        bound.append(enriched)
        train_devices = set(train["device_id"])
        test_devices = set(test["device_id"])
        audit_rows.append(
            {
                "evaluation": split["evaluation"][0],
                "fold": split["fold"][0],
                "state": state,
                "reason": state,
                "train_rows": train.height,
                "test_rows": test.height,
                "train_unique_targets": train_targets,
                "test_unique_targets": test_targets,
                "train_membership_sha256": train_hash,
                "test_membership_sha256": test_hash,
                "test_truth_sha256": truth_hash,
                "train_devices": len(train_devices),
                "test_devices": len(test_devices),
                "train_stations": train["station_name"].n_unique(),
                "test_stations": test["station_name"].n_unique(),
                "train_dates": train["date"].n_unique(),
                "test_dates": test["date"].n_unique(),
                "device_overlap": len(train_devices & test_devices),
                "excluded_rows": split.filter(pl.col("role") == "excluded").height,
            }
        )
    memberships = pl.concat(bound).sort(
        "evaluation", "fold", "role", "radius_km", "date", "device_id"
    )
    fold_audit = pl.DataFrame(audit_rows).sort("evaluation", "fold")
    source_memberships = inputs.agreement_fold_membership.sort(
        "evaluation", "fold", "role", "radius_km", "date", "device_id"
    )
    if set(source_memberships.columns) - set(memberships.columns) or not memberships.select(
        *source_memberships.columns
    ).equals(source_memberships):
        raise RuntimeError("agreement reproduction mismatch: fold membership changed")
    source_folds = inputs.agreement_folds.sort("evaluation", "fold")
    comparable_folds = fold_audit.rename(
        {"state": "fold_state", "reason": "fold_reason"}
    ).select(*source_folds.columns)
    if not comparable_folds.equals(source_folds):
        raise RuntimeError("agreement reproduction mismatch: fold ledger changed")
    memberships = _bind_source_predictions(memberships, inputs.agreement_predictions)
    return fold_audit, memberships


def _training_weights(train: pl.DataFrame) -> np.ndarray[Any, np.dtype[np.float64]]:
    return (
        train.select(
            (1.0 / pl.len().over("station_name", "date")).alias("_sample_weight")
        )["_sample_weight"]
        .to_numpy()
    )


def refit_core_candidates(
    memberships: pl.DataFrame,
    folds: pl.DataFrame,
    config: AgreementAuditConfig,
) -> pl.DataFrame:
    rows: list[pl.DataFrame] = []
    for fold in folds.filter(pl.col("state") == "scored")["fold"]:
        split = memberships.filter(pl.col("fold") == fold)
        train = split.filter(pl.col("role") == "train")
        test = split.filter(pl.col("role") == "test")
        if _fold_state(train, test) != "scored":
            raise RuntimeError("agreement reproduction mismatch: scored fold changed")
        if not np.array_equal(
            test["ground_pm25_mean"].to_numpy(), test["source_y_true"].to_numpy()
        ):
            raise RuntimeError("agreement reproduction mismatch: prediction truth changed")
        base = test.select(
            "evaluation",
            "fold",
            "fold_state",
            "radius_km",
            "date",
            "device_id",
            "station_name",
            "station_fold",
            "quarter",
            "train_membership_sha256",
            "test_membership_sha256",
            "test_truth_sha256",
        )
        raw_prediction = test["micro_pm25_mean"].to_numpy()
        if not np.array_equal(raw_prediction, test["source_raw_micro_y_pred"].to_numpy()):
            raise RuntimeError("agreement reproduction mismatch: raw prediction changed")
        rows.append(
            base.with_columns(
                pl.lit("raw_micro").alias("model"),
                pl.lit("micro_pm25_mean").alias("model_features"),
                test["ground_pm25_mean"].alias("y_true"),
                pl.Series("y_pred", raw_prediction),
            )
        )
        weights = _training_weights(train)
        for model, features in CORE_FEATURES.items():
            pipeline = Pipeline(
                [("scale", StandardScaler()), ("ridge", Ridge(alpha=config.ridge_alpha))]
            )
            with threadpool_limits(limits=config.threads):
                pipeline.fit(
                    train.select(*features).to_numpy(),
                    train["ground_pm25_mean"].to_numpy(),
                    scale__sample_weight=weights,
                    ridge__sample_weight=weights,
                )
                prediction = pipeline.predict(test.select(*features).to_numpy())
            source_prediction = test[f"source_{model}_y_pred"].to_numpy()
            if not np.allclose(prediction, source_prediction, rtol=0.0, atol=1e-12):
                raise RuntimeError("agreement reproduction mismatch: Ridge prediction changed")
            rows.append(
                base.with_columns(
                    pl.lit(model).alias("model"),
                    pl.lit(",".join(features)).alias("model_features"),
                    test["ground_pm25_mean"].alias("y_true"),
                    pl.Series("y_pred", prediction),
                )
            )
    if not rows:
        raise RuntimeError("agreement reproduction mismatch: no scored fold")
    return pl.concat(rows).sort("evaluation", "fold", "model", "date", "device_id")


def _unit_predictions(predictions: pl.DataFrame, *, unit: str) -> pl.DataFrame:
    if unit == "device_day":
        return predictions
    truth_counts = predictions.group_by(
        "evaluation", "fold", "model", "station_name", "date"
    ).agg(pl.col("y_true").n_unique().alias("_truth_values"))
    if truth_counts.filter(pl.col("_truth_values") != 1).height:
        raise RuntimeError("agreement reproduction mismatch: station-day truth is not unique")
    return predictions.group_by(
        "evaluation", "fold", "model", "station_name", "date"
    ).agg(
        pl.col("radius_km").first(),
        pl.col("y_true").first(),
        pl.col("y_pred").mean(),
    )


def _score_values(frame: pl.DataFrame) -> dict[str, float | None]:
    truth = frame["y_true"].to_numpy()
    prediction = frame["y_pred"].to_numpy()
    residual = prediction - truth
    bias = float(np.mean(residual))
    denominator = float(np.sum((truth - np.mean(truth)) ** 2))
    r2 = None if denominator == 0.0 else 1.0 - float(np.sum(residual**2)) / denominator
    return {
        "rmse": float(np.sqrt(np.mean(residual**2))),
        "mae": float(np.mean(np.abs(residual))),
        "r2": r2,
        "bias": bias,
        "absolute_bias": abs(bias),
    }


def _scored_identity(frame: pl.DataFrame, *, unit: str, truth: bool) -> str:
    identity = ["evaluation", "fold", "station_name", "date"]
    if unit == "device_day":
        identity.append("device_id")
    if truth:
        identity.append("y_true")
    records = (
        frame.select(*identity)
        .sort(*[column for column in identity if column != "y_true"])
        .with_columns(pl.col("date").cast(pl.String))
        .to_dicts()
    )
    return canonical_hash(records)


def _score_rows(
    predictions: pl.DataFrame,
    folds: pl.DataFrame,
) -> list[dict[str, object]]:
    radii = predictions["radius_km"].unique().to_list()
    if len(radii) != 1:
        raise RuntimeError("agreement reproduction mismatch: score radius changed")
    radius = float(radii[0])
    rows: list[dict[str, object]] = []
    for fold_row in folds.sort("evaluation", "fold").iter_rows(named=True):
        evaluation = str(fold_row["evaluation"])
        fold = str(fold_row["fold"])
        state = str(fold_row["state"])
        for unit in ("station_day", "device_day"):
            for model in _CORE_MODELS:
                selected = predictions.filter(
                    (pl.col("evaluation") == evaluation)
                    & (pl.col("fold") == fold)
                    & (pl.col("model") == model)
                )
                scored = _unit_predictions(selected, unit=unit) if not selected.is_empty() else selected
                values = _score_values(scored) if state == "scored" else dict.fromkeys(_METRICS)
                membership_hash = (
                    _scored_identity(scored, unit=unit, truth=False)
                    if state == "scored"
                    else str(fold_row["test_membership_sha256"])
                )
                truth_hash = (
                    _scored_identity(scored, unit=unit, truth=True)
                    if state == "scored"
                    else str(fold_row["test_truth_sha256"])
                )
                for metric in _METRICS:
                    rows.append(
                        {
                            "scope": "fold",
                            "evaluation": evaluation,
                            "fold": fold,
                            "radius_km": radius,
                            "model": model,
                            "unit": unit,
                            "metric": metric,
                            "state": state,
                            "primary": unit == "station_day",
                            "n": scored.height if state == "scored" else 0,
                            "intended_n": int(fold_row["test_rows"]),
                            "membership_sha256": membership_hash,
                            "truth_sha256": truth_hash,
                            "total_folds": 1,
                            "scored_folds": int(state == "scored"),
                            "unscored_folds_sha256": canonical_hash(
                                [] if state == "scored" else [{"fold": fold, "state": state}]
                            ),
                            "value": values[metric],
                        }
                    )
    for evaluation in ("held_station", "held_quarter", "joint"):
        evaluation_folds = folds.filter(pl.col("evaluation") == evaluation)
        unscored = evaluation_folds.filter(pl.col("state") != "scored").select(
            "fold", "state"
        )
        selected_evaluation = predictions.filter(pl.col("evaluation") == evaluation)
        overall_state = "scored" if not selected_evaluation.is_empty() else "unscored_no_scored_folds"
        for unit in ("station_day", "device_day"):
            for model in _CORE_MODELS:
                selected = selected_evaluation.filter(pl.col("model") == model)
                scored = _unit_predictions(selected, unit=unit) if not selected.is_empty() else selected
                values = (
                    _score_values(scored)
                    if overall_state == "scored"
                    else dict.fromkeys(_METRICS)
                )
                membership_hash = _scored_identity(scored, unit=unit, truth=False)
                truth_hash = _scored_identity(scored, unit=unit, truth=True)
                for metric in _METRICS:
                    rows.append(
                        {
                            "scope": "overall",
                            "evaluation": evaluation,
                            "fold": None,
                            "radius_km": radius,
                            "model": model,
                            "unit": unit,
                            "metric": metric,
                            "state": overall_state,
                            "primary": unit == "station_day",
                            "n": scored.height,
                            "intended_n": int(evaluation_folds["test_rows"].sum()),
                            "membership_sha256": membership_hash,
                            "truth_sha256": truth_hash,
                            "total_folds": evaluation_folds.height,
                            "scored_folds": evaluation_folds.filter(
                                pl.col("state") == "scored"
                            ).height,
                            "unscored_folds_sha256": canonical_hash(unscored.to_dicts()),
                            "value": values[metric],
                        }
                    )
    return rows


def score_audit_predictions(
    predictions: pl.DataFrame,
    folds: pl.DataFrame,
) -> tuple[pl.DataFrame, pl.DataFrame]:
    scores = pl.DataFrame(
        _score_rows(predictions, folds),
        schema_overrides={"fold": pl.String, "value": pl.Float64},
    ).sort(
        "scope", "evaluation", "fold", "unit", "model", "metric", nulls_last=True
    )
    key_columns = ("scope", "evaluation", "fold", "unit", "metric")
    baseline = scores.filter(
        (pl.col("model") == "raw_micro") & (pl.col("state") == "scored")
    )
    delta_rows: list[dict[str, object]] = []
    for candidate in CORE_FEATURES:
        candidate_rows = scores.filter(
            (pl.col("model") == candidate) & (pl.col("state") == "scored")
        )
        paired = candidate_rows.join(
            baseline.select(
                *key_columns,
                pl.col("membership_sha256").alias("_baseline_membership"),
                pl.col("truth_sha256").alias("_baseline_truth"),
                pl.col("value").alias("_baseline_value"),
            ),
            on=key_columns,
            how="inner",
            nulls_equal=True,
        )
        if paired.height != candidate_rows.height or paired.filter(
            (pl.col("membership_sha256") != pl.col("_baseline_membership"))
            | (pl.col("truth_sha256") != pl.col("_baseline_truth"))
        ).height:
            raise RuntimeError("agreement reproduction mismatch: score pairing changed")
        for row in paired.iter_rows(named=True):
            value = float(row["value"]) - float(row["_baseline_value"])
            metric = str(row["metric"])
            improved = value > 0.0 if metric == "r2" else value < 0.0
            delta_rows.append(
                {
                    **{
                        name: row[name]
                        for name in (
                            "scope",
                            "evaluation",
                            "fold",
                            "radius_km",
                            "unit",
                            "state",
                            "primary",
                            "n",
                            "intended_n",
                            "membership_sha256",
                            "truth_sha256",
                            "total_folds",
                            "scored_folds",
                            "unscored_folds_sha256",
                        )
                    },
                    "model": candidate,
                    "baseline_model": "raw_micro",
                    "metric": metric,
                    "value": value,
                    "improved": improved,
                }
            )
    deltas = pl.DataFrame(
        delta_rows,
        schema_overrides={"fold": pl.String, "value": pl.Float64},
    ).sort(
        "scope", "evaluation", "fold", "unit", "model", "metric", nulls_last=True
    )
    return scores, deltas


def _station_day_prediction_table(predictions: pl.DataFrame) -> pl.DataFrame:
    required = {"station_name", "date", "device_id", "model", "y_true", "y_pred"}
    missing = sorted(required - set(predictions.columns))
    if missing:
        raise RuntimeError(f"clustered uncertainty is missing columns: {missing}")
    if predictions.is_empty() or predictions.filter(
        pl.any_horizontal(
            pl.col(column).is_null() | ~pl.col(column).is_finite()
            for column in ("y_true", "y_pred")
        )
    ).height:
        raise RuntimeError("clustered uncertainty contains invalid predictions")
    grouped = predictions.group_by("station_name", "date", "model").agg(
        pl.col("y_true").n_unique().alias("_truth_values"),
        pl.col("y_true").first(),
        pl.col("y_pred").mean(),
    )
    if grouped.filter(pl.col("_truth_values") != 1).height:
        raise RuntimeError("clustered uncertainty has non-unique station-day truth")
    grouped = grouped.drop("_truth_values")
    truth = grouped.group_by("station_name", "date").agg(
        pl.col("y_true").n_unique().alias("_truth_values"),
        pl.col("y_true").first(),
    )
    if truth.filter(pl.col("_truth_values") != 1).height:
        raise RuntimeError("clustered uncertainty truth changes by model")
    return (
        grouped.drop("y_true")
        .pivot(
            on="model",
            index=("station_name", "date"),
            values="y_pred",
        )
        .join(truth.drop("_truth_values"), on=("station_name", "date"), how="left")
        .sort("station_name", "date")
    )


def _bootstrap_identity(frame: pl.DataFrame, *, truth: bool) -> str:
    columns = ["station_name", "date"]
    if truth:
        columns.append("y_true")
    return canonical_hash(
        frame.select(*columns)
        .sort("station_name", "date")
        .with_columns(pl.col("date").cast(pl.String))
        .to_dicts()
    )


def station_cluster_bootstrap(
    predictions: pl.DataFrame,
    pairs: tuple[tuple[str, str], ...],
    config: AgreementAuditConfig,
) -> pl.DataFrame:
    station_days = _station_day_prediction_table(predictions)
    stations = sorted(str(value) for value in station_days["station_name"].unique())
    if len(stations) < 2:
        raise RuntimeError("station-cluster bootstrap requires at least two station clusters")
    normalized_pairs = tuple(sorted(pairs))
    if not normalized_pairs or len(set(normalized_pairs)) != len(normalized_pairs):
        raise RuntimeError("station-cluster bootstrap pairs must be unique and non-empty")
    available_models = set(station_days.columns) - {"station_name", "date", "y_true"}
    if any(
        candidate == comparator
        or candidate not in available_models
        or comparator not in available_models
        for candidate, comparator in normalized_pairs
    ):
        raise RuntimeError("station-cluster bootstrap pair is unavailable")
    rng = np.random.default_rng(config.bootstrap_seed)
    rows: list[dict[str, object]] = []
    for candidate, comparator in normalized_pairs:
        paired = station_days.select(
            "station_name", "date", "y_true", candidate, comparator
        )
        if paired.select(
            pl.any_horizontal(pl.col(candidate).is_null(), pl.col(comparator).is_null()).any()
        ).item():
            raise RuntimeError("station-cluster bootstrap pair has missing station-days")
        truth = paired["y_true"].to_numpy()
        candidate_error = paired[candidate].to_numpy() - truth
        comparator_error = paired[comparator].to_numpy() - truth
        observed_delta_rmse = float(
            np.sqrt(np.mean(candidate_error**2))
            - np.sqrt(np.mean(comparator_error**2))
        )
        observed_delta_mae = float(
            np.mean(np.abs(candidate_error)) - np.mean(np.abs(comparator_error))
        )
        blocks = {
            station: paired.filter(pl.col("station_name") == station)
            for station in stations
        }
        rmse_draws = np.empty(config.bootstrap_draws, dtype=np.float64)
        mae_draws = np.empty(config.bootstrap_draws, dtype=np.float64)
        for draw in range(config.bootstrap_draws):
            sampled = rng.integers(0, len(stations), size=len(stations))
            selected = pl.concat([blocks[stations[index]] for index in sampled])
            selected_truth = selected["y_true"].to_numpy()
            selected_candidate_error = selected[candidate].to_numpy() - selected_truth
            selected_comparator_error = selected[comparator].to_numpy() - selected_truth
            rmse_draws[draw] = np.sqrt(np.mean(selected_candidate_error**2)) - np.sqrt(
                np.mean(selected_comparator_error**2)
            )
            mae_draws[draw] = np.mean(np.abs(selected_candidate_error)) - np.mean(
                np.abs(selected_comparator_error)
            )
        rmse_interval = np.percentile(rmse_draws, (2.5, 97.5))
        mae_interval = np.percentile(mae_draws, (2.5, 97.5))
        rows.append(
            {
                "candidate": candidate,
                "comparator": comparator,
                "unit": "station_day",
                "state": "descriptive_station_cluster_bootstrap",
                "draws": config.bootstrap_draws,
                "seed": config.bootstrap_seed,
                "clusters": len(stations),
                "station_days": paired.height,
                "membership_sha256": _bootstrap_identity(paired, truth=False),
                "truth_sha256": _bootstrap_identity(paired, truth=True),
                "observed_delta_rmse": observed_delta_rmse,
                "delta_rmse_ci_low": float(rmse_interval[0]),
                "delta_rmse_ci_high": float(rmse_interval[1]),
                "observed_delta_mae": observed_delta_mae,
                "delta_mae_ci_low": float(mae_interval[0]),
                "delta_mae_ci_high": float(mae_interval[1]),
            }
        )
    return pl.DataFrame(rows, schema=dict(AUDIT_UNCERTAINTY_SCHEMA)).sort(
        "candidate", "comparator"
    )


def permute_station_day_targets(
    station_days: pl.DataFrame,
    geography: pl.DataFrame,
    *,
    replicate: int,
    seed: int,
) -> tuple[pl.DataFrame, pl.DataFrame]:
    if isinstance(replicate, bool) or not isinstance(replicate, int) or replicate < 0:
        raise ValueError("station-label replicate must be a non-negative integer")
    required = {"station_name", "date", "ground_pm25_mean"}
    if required - set(station_days.columns):
        raise RuntimeError("station-label control target schema changed")
    if station_days.select("station_name", "date").n_unique() != station_days.height:
        raise RuntimeError("station-label control target rows are duplicated")
    if station_days.filter(
        pl.col("ground_pm25_mean").is_null() | ~pl.col("ground_pm25_mean").is_finite()
    ).height:
        raise RuntimeError("station-label control target is non-finite")
    station_geography = geography.select("station_name", "airzone_official")
    if station_geography["station_name"].n_unique() != station_geography.height:
        raise RuntimeError("station-label control geography is duplicated")
    joined = station_days.join(station_geography, on="station_name", how="left")
    if joined["airzone_official"].null_count():
        raise RuntimeError("station-label control air zone is missing")
    rng = np.random.default_rng(
        np.random.SeedSequence([seed, CONTROL_CODES["station_label"], replicate])
    )
    rows: list[pl.DataFrame] = []
    issues: list[dict[str, object]] = []
    groups = joined.sort("date", "airzone_official", "station_name").partition_by(
        "date", "airzone_official", maintain_order=True
    )
    for group in groups:
        stations = group["station_name"].n_unique()
        if stations < 2:
            issues.append(
                {
                    "replicate": replicate,
                    "date": group["date"][0],
                    "airzone_official": group["airzone_official"][0],
                    "state": "unpermutable_group",
                    "n_stations": stations,
                }
            )
            rows.append(
                group.with_columns(
                    pl.lit(None, dtype=pl.Float64).alias("ground_pm25_mean")
                )
            )
            continue
        permuted = rng.permutation(group["ground_pm25_mean"].to_numpy())
        rows.append(
            group.with_columns(pl.Series("ground_pm25_mean", permuted, dtype=pl.Float64))
        )
    issue_schema = {
        "replicate": pl.Int64,
        "date": pl.Date,
        "airzone_official": pl.String,
        "state": pl.String,
        "n_stations": pl.Int64,
    }
    issue_frame = pl.DataFrame(issues, schema=issue_schema)
    return (
        pl.concat(rows)
        .with_columns(pl.lit(replicate, dtype=pl.Int64).alias("replicate"))
        .sort("date", "airzone_official", "station_name"),
        issue_frame.sort("date", "airzone_official"),
    )


def _fit_control_predictions(
    memberships: pl.DataFrame,
    folds: pl.DataFrame,
    config: AgreementAuditConfig,
) -> pl.DataFrame:
    rows: list[pl.DataFrame] = []
    scored_folds = folds.filter(
        (pl.col("state") == "scored") & (pl.col("evaluation") == "held_station")
    )
    for fold in scored_folds["fold"]:
        split = memberships.filter(pl.col("fold") == fold).sort(
            "role", "station_name", "date", "device_id"
        )
        train = split.filter(pl.col("role") == "train")
        test = split.filter(pl.col("role") == "test")
        if _fold_state(train, test) != "scored":
            continue
        base = test.select("evaluation", "fold", "station_name", "date", "device_id")
        rows.append(
            base.with_columns(
                pl.lit("raw_micro").alias("model"),
                test["ground_pm25_mean"].alias("y_true"),
                test["micro_pm25_mean"].alias("y_pred"),
            )
        )
        weights = _training_weights(train)
        for model, features in CORE_FEATURES.items():
            pipeline = Pipeline(
                [("scale", StandardScaler()), ("ridge", Ridge(alpha=config.ridge_alpha))]
            )
            with threadpool_limits(limits=config.threads):
                pipeline.fit(
                    train.select(*features).to_numpy(),
                    train["ground_pm25_mean"].to_numpy(),
                    scale__sample_weight=weights,
                    ridge__sample_weight=weights,
                )
                prediction = pipeline.predict(test.select(*features).to_numpy())
            rows.append(
                base.with_columns(
                    pl.lit(model).alias("model"),
                    test["ground_pm25_mean"].alias("y_true"),
                    pl.Series("y_pred", prediction, dtype=pl.Float64),
                )
            )
    if not rows:
        return pl.DataFrame(
            schema={
                "evaluation": pl.String,
                "fold": pl.String,
                "station_name": pl.String,
                "date": pl.Date,
                "device_id": pl.String,
                "model": pl.String,
                "y_true": pl.Float64,
                "y_pred": pl.Float64,
            }
        )
    return pl.concat(rows)


def _control_metric_rows(
    predictions: pl.DataFrame,
    *,
    replicate: int,
) -> list[dict[str, object]]:
    station_day = _station_day_prediction_table(predictions)
    membership_hash = _bootstrap_identity(station_day, truth=False)
    truth_hash = _bootstrap_identity(station_day, truth=True)
    rows: list[dict[str, object]] = []
    raw_error = station_day["raw_micro"].to_numpy() - station_day["y_true"].to_numpy()
    raw_metrics = {
        "rmse": float(np.sqrt(np.mean(raw_error**2))),
        "mae": float(np.mean(np.abs(raw_error))),
    }
    for model in CORE_FEATURES:
        error = station_day[model].to_numpy() - station_day["y_true"].to_numpy()
        candidate_metrics = {
            "rmse": float(np.sqrt(np.mean(error**2))),
            "mae": float(np.mean(np.abs(error))),
        }
        for metric in ("rmse", "mae"):
            rows.append(
                {
                    "control": "station_label",
                    "variant": "within_date_airzone_bijection",
                    "replicate": replicate,
                    "state": "scored",
                    "model": model,
                    "comparator": "raw_micro",
                    "unit": "station_day",
                    "metric": f"delta_{metric}",
                    "value": candidate_metrics[metric] - raw_metrics[metric],
                    "intended_rows": station_day.height,
                    "scored_rows": station_day.height,
                    "membership_sha256": membership_hash,
                    "truth_sha256": truth_hash,
                    "issue": None,
                }
            )
    return rows


def run_station_label_control(
    control: tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame],
    config: AgreementAuditConfig,
) -> pl.DataFrame:
    memberships, folds, geography = control
    truth_counts = memberships.group_by("station_name", "date").agg(
        pl.col("ground_pm25_mean").n_unique().alias("_truth_values"),
        pl.col("ground_pm25_mean").first(),
    )
    if truth_counts.filter(pl.col("_truth_values") != 1).height:
        raise RuntimeError("station-label control truth is not unique")
    station_days = truth_counts.drop("_truth_values")
    rows: list[dict[str, object]] = []
    for replicate in range(config.permutation_draws):
        permuted, issues = permute_station_day_targets(
            station_days,
            geography,
            replicate=replicate,
            seed=config.permutation_seed,
        )
        if not issues.is_empty():
            rows.append(
                {
                    "control": "station_label",
                    "variant": "within_date_airzone_bijection",
                    "replicate": replicate,
                    "state": "unpermutable_group",
                    "model": None,
                    "comparator": None,
                    "unit": "station_day",
                    "metric": "delta_rmse",
                    "value": None,
                    "intended_rows": station_days.height,
                    "scored_rows": 0,
                    "membership_sha256": canonical_hash([]),
                    "truth_sha256": canonical_hash([]),
                    "issue": canonical_hash(
                        issues.with_columns(pl.col("date").cast(pl.String)).to_dicts()
                    ),
                }
            )
            continue
        rebound = memberships.drop("ground_pm25_mean").join(
            permuted.select("station_name", "date", "ground_pm25_mean"),
            on=("station_name", "date"),
            how="left",
        )
        predictions = _fit_control_predictions(rebound, folds, config)
        rows.extend(_control_metric_rows(predictions, replicate=replicate))
    schema = {
        "control": pl.String,
        "variant": pl.String,
        "replicate": pl.Int64,
        "state": pl.String,
        "model": pl.String,
        "comparator": pl.String,
        "unit": pl.String,
        "metric": pl.String,
        "value": pl.Float64,
        "intended_rows": pl.Int64,
        "scored_rows": pl.Int64,
        "membership_sha256": pl.String,
        "truth_sha256": pl.String,
        "issue": pl.String,
    }
    return pl.DataFrame(rows, schema=schema).sort("replicate", "model", "metric")


def shift_station_day_targets(
    station_days: pl.DataFrame,
    *,
    shift_days: int,
) -> pl.DataFrame:
    if isinstance(shift_days, bool) or not isinstance(shift_days, int) or shift_days <= 0:
        raise ValueError("target shift must be a positive integer")
    required = {"station_name", "date", "ground_pm25_mean"}
    if required - set(station_days.columns):
        raise RuntimeError("target-shift control schema changed")
    if station_days.select("station_name", "date").n_unique() != station_days.height:
        raise RuntimeError("target-shift control station-days are duplicated")
    source = station_days.select(
        "station_name",
        pl.col("date").alias("_wanted_source"),
        pl.col("date").alias("source_date"),
        pl.col("ground_pm25_mean").alias("shifted_ground_pm25_mean"),
    )
    shifted = (
        station_days.with_columns(
            (pl.col("date") + timedelta(days=shift_days)).alias("_wanted_source")
        )
        .join(source, on=("station_name", "_wanted_source"), how="inner")
        .drop("_wanted_source")
        .sort("station_name", "date")
    )
    membership_records = shifted.select("station_name", "date").with_columns(
        pl.col("date").cast(pl.String)
    )
    truth_records = shifted.select(
        "station_name", "date", "shifted_ground_pm25_mean"
    ).with_columns(pl.col("date").cast(pl.String))
    return shifted.with_columns(
        pl.lit(canonical_hash(membership_records.to_dicts())).alias("membership_sha256"),
        pl.lit(canonical_hash(truth_records.to_dicts())).alias("truth_sha256"),
    )


def run_target_shift_controls(
    control: tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame],
    config: AgreementAuditConfig,
) -> pl.DataFrame:
    memberships, folds, _geography = control
    truth_counts = memberships.group_by("station_name", "date").agg(
        pl.col("ground_pm25_mean").n_unique().alias("_truth_values"),
        pl.col("ground_pm25_mean").first(),
    )
    if truth_counts.filter(pl.col("_truth_values") != 1).height:
        raise RuntimeError("target-shift control truth is not unique")
    station_days = truth_counts.drop("_truth_values")
    rows: list[dict[str, object]] = []
    for shift_days in config.target_time_shifts_days:
        shifted = shift_station_day_targets(station_days, shift_days=shift_days)
        rebound = memberships.drop("ground_pm25_mean").join(
            shifted.select(
                "station_name",
                "date",
                pl.col("shifted_ground_pm25_mean").alias("ground_pm25_mean"),
            ),
            on=("station_name", "date"),
            how="inner",
        )
        predictions = _fit_control_predictions(rebound, folds, config)
        if predictions.is_empty():
            rows.append(
                {
                    "control": "target_shift",
                    "variant": f"shift_{shift_days:02d}d",
                    "shift_days": shift_days,
                    "state": "unscored_empty_shift",
                    "model": None,
                    "comparator": None,
                    "unit": "station_day",
                    "metric": "delta_rmse",
                    "value": None,
                    "intended_rows": station_days.height,
                    "scored_rows": 0,
                    "intended_dates": station_days["date"].n_unique(),
                    "scored_dates": 0,
                    "stations": 0,
                    "folds": 0,
                    "membership_sha256": canonical_hash([]),
                    "truth_sha256": canonical_hash([]),
                }
            )
            continue
        station_predictions = _station_day_prediction_table(predictions)
        membership_hash = _bootstrap_identity(station_predictions, truth=False)
        truth_hash = _bootstrap_identity(station_predictions, truth=True)
        truth = station_predictions["y_true"].to_numpy()
        raw_error = station_predictions["raw_micro"].to_numpy() - truth
        raw_metrics = {
            "rmse": float(np.sqrt(np.mean(raw_error**2))),
            "mae": float(np.mean(np.abs(raw_error))),
        }
        for model in CORE_FEATURES:
            error = station_predictions[model].to_numpy() - truth
            candidate_metrics = {
                "rmse": float(np.sqrt(np.mean(error**2))),
                "mae": float(np.mean(np.abs(error))),
            }
            for metric in ("rmse", "mae"):
                rows.append(
                    {
                        "control": "target_shift",
                        "variant": f"shift_{shift_days:02d}d",
                        "shift_days": shift_days,
                        "state": "scored",
                        "model": model,
                        "comparator": "raw_micro",
                        "unit": "station_day",
                        "metric": f"delta_{metric}",
                        "value": candidate_metrics[metric] - raw_metrics[metric],
                        "intended_rows": station_days.height,
                        "scored_rows": station_predictions.height,
                        "intended_dates": station_days["date"].n_unique(),
                        "scored_dates": station_predictions["date"].n_unique(),
                        "stations": station_predictions["station_name"].n_unique(),
                        "folds": predictions["fold"].n_unique(),
                        "membership_sha256": membership_hash,
                        "truth_sha256": truth_hash,
                    }
                )
    schema = {
        "control": pl.String,
        "variant": pl.String,
        "shift_days": pl.Int64,
        "state": pl.String,
        "model": pl.String,
        "comparator": pl.String,
        "unit": pl.String,
        "metric": pl.String,
        "value": pl.Float64,
        "intended_rows": pl.Int64,
        "scored_rows": pl.Int64,
        "intended_dates": pl.Int64,
        "scored_dates": pl.Int64,
        "stations": pl.Int64,
        "folds": pl.Int64,
        "membership_sha256": pl.String,
        "truth_sha256": pl.String,
    }
    return pl.DataFrame(rows, schema=schema).sort("shift_days", "model", "metric")


def empirical_lower_tail_p(observed: float, null_values: np.ndarray[Any, Any]) -> float:
    values = np.asarray(null_values, dtype=np.float64)
    if not math.isfinite(observed) or values.ndim != 1 or not np.isfinite(values).all():
        raise ValueError("empirical lower-tail inputs must be finite")
    return float((1 + np.count_nonzero(values <= observed)) / (values.size + 1))
