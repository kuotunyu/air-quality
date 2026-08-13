"""Benchmark annual micro-sensor agreement with nearby reference stations."""

from __future__ import annotations

import importlib
import json
import math
import re
import shutil
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from itertools import pairwise
from pathlib import Path
from typing import Any
from uuid import uuid4

import duckdb
import numpy as np
import polars as pl
from sklearn.linear_model import Ridge
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from twair.analysis.era5_robustness import assign_station_folds
from twair.analysis.micro_sensor_annual_readiness import (
    ANNUAL_CALENDAR_SCHEMA,
    ANNUAL_COHORT_THRESHOLD_SCHEMA,
    ANNUAL_DEVICE_COHORT_SCHEMA,
    ANNUAL_DEVICE_DAY_SCHEMA,
    ANNUAL_EXCLUSION_SCHEMA,
    annual_micro_sensor_readiness_dir,
    load_annual_micro_sensor_panel_config,
)
from twair.config import ConfigError, load_conf
from twair.ingest.micro_sensor_observations import (
    OBSERVATION_OUTPUT_SCHEMA,
    load_micro_sensor_observation_generation,
)
from twair.ingest.station_meta import resolve_station_geo
from twair.net import sha256_file
from twair.paths import data_root as configured_data_root
from twair.provenance import git_state
from twair.store.schema import PARTITION_SCHEMA

threadpool_limits: Any = importlib.import_module("threadpoolctl").threadpool_limits

_SHA256 = re.compile(r"[0-9a-f]{64}")
_ANNUAL_GENERATION_SHA256 = "c74ec40428a907e98821efbaf36c36386d2c1b99de69791b49f157eb7947e5bb"
_ANALYSIS_FIELDS = {
    "annual_generation_sha256",
    "distance_bands_km",
    "primary_distance_km",
    "primary_devices",
    "primary_stations",
    "minimum_active_months",
    "minimum_trio_dates",
    "minimum_trio_hours",
    "minimum_source_rows",
    "minimum_observed_hours",
    "station_folds",
    "quarters",
    "ridge_alpha",
    "threads",
    "memory_limit_gb",
    "claim_boundary",
}
_CLAIM_BOUNDARY = {
    "reference_station_agreement_only": True,
    "validated_calibration": False,
    "sensor_bias_estimate": False,
    "sensor_fusion": False,
    "colocated_ground_truth": False,
    "high_resolution_field": False,
    "satellite_feature_used": False,
    "values_imputed": False,
    "causal_analysis": False,
}
_ANNUAL_MEMBER_SCHEMAS = {
    "calendar_coverage": ANNUAL_CALENDAR_SCHEMA,
    "device_days": ANNUAL_DEVICE_DAY_SCHEMA,
    "device_cohorts": ANNUAL_DEVICE_COHORT_SCHEMA,
    "cohort_thresholds": ANNUAL_COHORT_THRESHOLD_SCHEMA,
    "exclusions": ANNUAL_EXCLUSION_SCHEMA,
}
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
_ANNUAL_SUMMARY_FIELDS = {"calendar", "devices", "threshold_grid_rows", "output_rows"}
_ANNUAL_SUMMARY_CALENDAR_FIELDS = {"complete_dates", "catalogue_absent_dates"}
_AGREEMENT_VARIABLES = ("pm25", "humidity", "temperature")
_AGREEMENT_COUNT_COLUMNS = tuple(
    name for name, dtype in ANNUAL_DEVICE_DAY_SCHEMA if dtype == pl.Int64
)
AGREEMENT_DAY_SCHEMA: tuple[tuple[str, pl.DataType | type[pl.DataType]], ...] = (
    *ANNUAL_DEVICE_DAY_SCHEMA,
    ("micro_pm25_mean", pl.Float64),
    ("micro_humidity_mean", pl.Float64),
    ("micro_temperature_mean", pl.Float64),
    ("ground_pm25_mean", pl.Float64),
    ("reason", pl.String),
)
AGREEMENT_CALENDAR_SCHEMA: tuple[tuple[str, pl.DataType | type[pl.DataType]], ...] = (
    ("date", pl.Date),
    ("calendar_state", pl.String),
    ("catalog_generation_sha256", pl.String),
    ("parsed_generation_sha256", pl.String),
)
AGREEMENT_PAIRED_DAY_SCHEMA: tuple[tuple[str, pl.DataType | type[pl.DataType]], ...] = (
    ("radius_km", pl.Float64),
    ("calendar_state", pl.String),
    ("quarter", pl.Int64),
    *AGREEMENT_DAY_SCHEMA,
)
AGREEMENT_EXCLUSION_SCHEMA: tuple[tuple[str, pl.DataType | type[pl.DataType]], ...] = (
    ("radius_km", pl.Float64),
    ("date", pl.Date),
    ("device_id", pl.String),
    ("station_name", pl.String),
    ("quarter", pl.Int64),
    ("reason", pl.String),
)
AGREEMENT_COHORT_COVERAGE_SCHEMA: tuple[tuple[str, pl.DataType | type[pl.DataType]], ...] = (
    ("radius_km", pl.Float64),
    ("station_name", pl.String),
    ("quarter", pl.Int64),
    ("reason", pl.String),
    ("device_days", pl.Int64),
    ("devices", pl.Int64),
    ("dates", pl.Int64),
)
_AGREEMENT_PANEL_SCHEMAS = {
    "calendar": AGREEMENT_CALENDAR_SCHEMA,
    "paired_days": AGREEMENT_PAIRED_DAY_SCHEMA,
    "exclusions": AGREEMENT_EXCLUSION_SCHEMA,
    "cohort_coverage": AGREEMENT_COHORT_COVERAGE_SCHEMA,
}
_AGREEMENT_PANEL_IDENTITY_FIELDS = (
    "schema_version",
    "analysis",
    "inputs",
    "checkpoint_inventory",
    "config",
    "claim_boundary",
    "output_rows",
    "members",
    "summary_file",
    "summary_sha256",
)
_FINAL_MEMBER_NAMES = (
    "calendar",
    "paired_days",
    "exclusions",
    "fold_membership",
    "folds",
    "predictions",
    "scores",
    "deltas",
)
_FINAL_IDENTITY_FIELDS = (
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


@dataclass(frozen=True, slots=True)
class AnnualAgreementConfig:
    annual_generation_sha256: str
    distance_bands_km: tuple[float, ...]
    primary_distance_km: float
    primary_devices: int
    primary_stations: int
    minimum_active_months: int
    minimum_trio_dates: int
    minimum_trio_hours: int
    minimum_source_rows: int
    minimum_observed_hours: int
    station_folds: int
    quarters: tuple[int, ...]
    ridge_alpha: float
    threads: int
    memory_limit_gb: int
    claim_boundary: tuple[tuple[str, bool], ...]


@dataclass(frozen=True, slots=True)
class PinnedAnnualMember:
    path: Path
    generation_dir: Path
    bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class AnnualDistanceCohort:
    radius_km: float
    candidates: pl.DataFrame


@dataclass(frozen=True, slots=True)
class AnnualReadinessInput:
    generation_dir: Path
    manifest: dict[str, Any]
    summary: dict[str, Any]
    calendar_coverage: pl.DataFrame
    device_days: PinnedAnnualMember
    device_cohorts: pl.DataFrame
    cohort_thresholds: pl.DataFrame
    exclusions: pl.DataFrame
    candidate_cohorts: tuple[AnnualDistanceCohort, ...]


@dataclass(frozen=True, slots=True)
class AgreementDayAggregation:
    rows: pl.DataFrame
    summary: dict[str, int]
    input_identities: dict[str, object]
    config: AnnualAgreementConfig
    input_paths: AgreementDayInputPaths | None


@dataclass(frozen=True, slots=True)
class AgreementDayInputPaths:
    micro_paths: tuple[tuple[str, Path], ...]
    annual_device_days: PinnedAnnualMember
    ground_path: Path | None


@dataclass(frozen=True, slots=True)
class ReviewedAgreementDaySources:
    micro_paths: tuple[tuple[str, Path], ...]
    ground_path: Path
    raw_observation_generation_sha256: str
    parsed_directory: Path
    parsed_manifest: dict[str, Any]
    containment: tuple[tuple[Path, Path, bool], ...]
    file_identities: tuple[tuple[Path, dict[str, object]], ...]

    def assert_unchanged(self) -> None:
        _assert_reviewed_parsed_generation(
            self.parsed_directory,
            expected_manifest=self.parsed_manifest,
        )
        for path, parent, is_directory in self.containment:
            _assert_reviewed_direct_child(
                path,
                parent=parent,
                is_directory=is_directory,
            )
        if any(_file_identity(path) != identity for path, identity in self.file_identities):
            raise RuntimeError("annual agreement reviewed source changed during use")


@dataclass(frozen=True, slots=True)
class AgreementDayCheckpoint:
    directory: Path
    member_path: Path
    manifest_path: Path
    rows: pl.DataFrame
    manifest: dict[str, Any]


@dataclass(frozen=True, slots=True)
class AnnualAgreementPanel:
    calendar: pl.DataFrame
    paired_days: pl.DataFrame
    exclusions: pl.DataFrame
    cohort_coverage: pl.DataFrame
    summary: dict[str, object]
    manifest: dict[str, object]
    annual_input: AnnualReadinessInput | None
    checkpoints: tuple[AgreementDayCheckpoint, ...] | None


@dataclass(frozen=True, slots=True)
class AnnualAgreementEvaluation:
    memberships: pl.DataFrame
    folds: pl.DataFrame
    predictions: pl.DataFrame
    scores: pl.DataFrame
    deltas: pl.DataFrame
    manifest: dict[str, object]


@dataclass(frozen=True, slots=True)
class AnnualAgreementRunPlan:
    annual_generation_sha256: str
    annual_generation_dir: Path
    checkpoint_root: Path
    panel_destination: Path
    output_root: Path
    lock_path: Path
    threads: int
    memory_limit_gb: int


@dataclass(frozen=True, slots=True)
class AnnualAgreementResult:
    directory: Path
    calendar: pl.DataFrame
    paired_days: pl.DataFrame
    exclusions: pl.DataFrame
    fold_membership: pl.DataFrame
    folds: pl.DataFrame
    predictions: pl.DataFrame
    scores: pl.DataFrame
    deltas: pl.DataFrame
    summary: dict[str, object]
    manifest: dict[str, object]
    written: dict[str, Path]


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


def _positive_integer(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(f"{label} must be a positive integer")
    return value


def _finite_number(value: object, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{label} must be a finite number")
    converted = float(value)
    if not math.isfinite(converted):
        raise ConfigError(f"{label} must be a finite number")
    return converted


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{label} is unreadable") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} must be an object")
    return value


def _file_identity(path: Path) -> dict[str, object]:
    if not path.is_file():
        raise RuntimeError(f"annual readiness member is missing: {path.name}")
    return {"bytes": path.stat().st_size, "sha256": sha256_file(path)}


def _is_link_like(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", None)
    return path.is_symlink() or (is_junction is not None and is_junction())


def _validate_generation_inventory(
    directory: Path,
    *,
    expected_files: set[str],
    during_read: bool,
) -> None:
    try:
        entries = tuple(directory.iterdir())
    except OSError as exc:
        raise RuntimeError("annual readiness generation directory is unreadable") from exc
    if {entry.name for entry in entries} != expected_files:
        suffix = " during read" if during_read else ""
        raise RuntimeError(f"annual readiness generation file set changed{suffix}")
    for entry in entries:
        try:
            outside = entry.resolve(strict=True).parent != directory
        except OSError as exc:
            raise RuntimeError(f"annual readiness member is unreadable: {entry.name}") from exc
        if _is_link_like(entry) or outside:
            raise RuntimeError(
                f"annual readiness member is linked or outside generation: {entry.name}"
            )


def _validate_agreement_panel_inventory(
    directory: Path,
    *,
    expected_files: set[str],
    during_read: bool,
) -> None:
    try:
        resolved_directory = directory.resolve(strict=True)
        entries = tuple(directory.iterdir())
    except OSError as exc:
        raise RuntimeError("annual agreement panel generation is unreadable") from exc
    if (
        _is_link_like(directory)
        or resolved_directory != directory
        or not resolved_directory.is_dir()
    ):
        raise RuntimeError("annual agreement panel is linked or outside generation")
    if {entry.name for entry in entries} != expected_files:
        suffix = " during read" if during_read else ""
        raise RuntimeError(f"annual agreement panel file set changed{suffix}")
    for entry in entries:
        try:
            outside = entry.resolve(strict=True).parent != directory
        except OSError as exc:
            raise RuntimeError(
                f"annual agreement panel member is unreadable: {entry.name}"
            ) from exc
        if _is_link_like(entry) or outside:
            raise RuntimeError(
                f"annual agreement panel member is linked or outside generation: {entry.name}"
            )


def _canonical_hash(value: object) -> str:
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


def _geography_identity(geography: pl.DataFrame) -> str:
    fields = (
        "station_name",
        "lon",
        "lat",
        "geo_source",
        "geo_source_record_namespace",
        "geo_source_record_id",
    )
    missing = sorted(set(fields) - set(geography.columns))
    if missing:
        raise RuntimeError(f"reviewed geography is missing provenance columns: {missing}")
    selected = geography.select(*fields)
    if selected["station_name"].n_unique() != selected.height:
        raise RuntimeError("reviewed geography station names are duplicated")
    if selected.select(pl.any_horizontal(pl.all().is_null()).any()).to_series().item():
        raise RuntimeError("reviewed geography contains null identity or coordinates")
    if selected.filter(~pl.col("lon").is_finite() | ~pl.col("lat").is_finite()).height:
        raise RuntimeError("reviewed geography contains non-finite coordinates")
    return _canonical_hash(selected.sort("station_name").to_dicts())


def _declared_file_identity(value: object, *, expected_path: str) -> dict[str, object]:
    raw = _mapping(value, label=f"annual readiness member {expected_path}")
    _exact_keys(raw, {"path", "bytes", "sha256"}, label=f"annual readiness member {expected_path}")
    if raw["path"] != expected_path:
        raise RuntimeError(f"annual readiness member path changed: {expected_path}")
    size = raw["bytes"]
    digest = raw["sha256"]
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise RuntimeError(f"annual readiness member bytes changed: {expected_path}")
    if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
        raise RuntimeError(f"annual readiness member SHA-256 changed: {expected_path}")
    return {"bytes": size, "sha256": digest}


def _pinned_member(path: Path, identity: dict[str, object]) -> PinnedAnnualMember:
    size = identity["bytes"]
    digest = identity["sha256"]
    if isinstance(size, bool) or not isinstance(size, int) or not isinstance(digest, str):
        raise RuntimeError(f"annual readiness member identity changed: {path.name}")
    return PinnedAnnualMember(
        path=path,
        generation_dir=path.parent,
        bytes=size,
        sha256=digest,
    )


def _assert_pinned_member(member: PinnedAnnualMember, *, during_read: bool) -> None:
    try:
        resolved_directory = member.generation_dir.resolve(strict=True)
        resolved_member = member.path.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError(f"annual readiness member is unreadable: {member.path.name}") from exc
    if (
        _is_link_like(member.generation_dir)
        or _is_link_like(member.path)
        or member.path.parent != member.generation_dir
        or resolved_directory != member.generation_dir
        or resolved_member.parent != member.generation_dir
    ):
        raise RuntimeError(
            f"annual readiness member is linked or outside generation: {member.path.name}"
        )
    observed = _file_identity(member.path)
    if observed != {"bytes": member.bytes, "sha256": member.sha256}:
        suffix = " during read" if during_read else ""
        raise RuntimeError(f"annual readiness member {member.path.name} changed{suffix}")


@contextmanager
def stable_annual_member_path(member: PinnedAnnualMember) -> Iterator[Path]:
    _assert_pinned_member(member, during_read=False)
    try:
        yield member.path
    finally:
        _assert_pinned_member(member, during_read=True)


def _load_annual_member(
    directory: Path,
    *,
    name: str,
    declaration: object,
) -> tuple[pl.DataFrame | PinnedAnnualMember, PinnedAnnualMember]:
    path = directory / f"{name}.parquet"
    expected = _declared_file_identity(declaration, expected_path=path.name)
    before = _file_identity(path)
    if before != expected:
        raise RuntimeError(f"annual readiness member changed: {path.name}")
    try:
        observed_schema = pl.read_parquet_schema(path)
        if observed_schema != dict(_ANNUAL_MEMBER_SCHEMAS[name]):
            raise RuntimeError(f"annual readiness member schema changed: {path.name}")
        pinned = _pinned_member(path, before)
        loaded: pl.DataFrame | PinnedAnnualMember = (
            pinned if name == "device_days" else pl.read_parquet(path)
        )
    except (OSError, pl.exceptions.PolarsError) as exc:
        raise RuntimeError(f"annual readiness member is unreadable: {path.name}") from exc
    after = _file_identity(path)
    if after != before:
        raise RuntimeError(f"annual readiness member changed during read: {path.name}")
    return loaded, pinned


def _nonnegative_integer(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeError(f"{label} must be a non-negative integer")
    return value


def _validate_annual_summary(summary: dict[str, Any]) -> dict[str, int]:
    if set(summary) != _ANNUAL_SUMMARY_FIELDS:
        raise RuntimeError("annual readiness summary fields changed")
    calendar = _mapping(summary["calendar"], label="annual readiness summary calendar")
    if set(calendar) != _ANNUAL_SUMMARY_CALENDAR_FIELDS:
        raise RuntimeError("annual readiness summary calendar fields changed")
    for name in _ANNUAL_SUMMARY_CALENDAR_FIELDS:
        _nonnegative_integer(calendar[name], label=f"annual readiness summary calendar {name}")
    _nonnegative_integer(summary["devices"], label="annual readiness summary devices")
    _nonnegative_integer(
        summary["threshold_grid_rows"],
        label="annual readiness summary threshold rows",
    )
    return _validate_output_rows(
        summary["output_rows"],
        label="annual readiness summary output rows",
    )


def _validate_output_rows(value: object, *, label: str) -> dict[str, int]:
    rows = _mapping(value, label=label)
    if set(rows) != set(_ANNUAL_MEMBER_SCHEMAS):
        raise RuntimeError(f"{label} fields changed")
    return {
        name: _nonnegative_integer(rows[name], label=f"{label} {name}")
        for name in _ANNUAL_MEMBER_SCHEMAS
    }


def load_annual_agreement_config(
    config: dict[str, Any] | None = None,
) -> AnnualAgreementConfig:
    raw = config if config is not None else load_conf("micro_sensor_annual_agreement")
    top = _mapping(raw, label="micro_sensor_annual_agreement")
    _exact_keys(top, {"analysis"}, label="micro_sensor_annual_agreement")
    analysis = _mapping(top["analysis"], label="micro_sensor_annual_agreement.analysis")
    _exact_keys(analysis, _ANALYSIS_FIELDS, label="micro_sensor_annual_agreement.analysis")

    generation = analysis["annual_generation_sha256"]
    if not isinstance(generation, str) or _SHA256.fullmatch(generation) is None:
        raise ConfigError("analysis.annual_generation_sha256 must be a lowercase SHA-256")
    if generation != _ANNUAL_GENERATION_SHA256:
        raise ConfigError("analysis annual generation does not match the reviewed output")

    raw_distances = analysis["distance_bands_km"]
    if not isinstance(raw_distances, list):
        raise ConfigError("analysis.distance_bands_km must be a list")
    distances = tuple(
        _finite_number(value, label="analysis.distance_bands_km") for value in raw_distances
    )
    primary_distance = _finite_number(
        analysis["primary_distance_km"], label="analysis.primary_distance_km"
    )

    raw_quarters = analysis["quarters"]
    if not isinstance(raw_quarters, list):
        raise ConfigError("analysis.quarters must be a list")
    quarters = tuple(_positive_integer(value, label="analysis.quarters") for value in raw_quarters)

    raw_boundary = _mapping(analysis["claim_boundary"], label="analysis.claim_boundary")
    _exact_keys(raw_boundary, set(_CLAIM_BOUNDARY), label="analysis.claim_boundary")
    if any(type(value) is not bool for value in raw_boundary.values()):
        raise ConfigError("analysis.claim_boundary values must be booleans")
    if raw_boundary != _CLAIM_BOUNDARY:
        raise ConfigError("analysis.claim_boundary must match the reviewed boundary")
    boundary = tuple((key, raw_boundary[key]) for key in _CLAIM_BOUNDARY)

    loaded = AnnualAgreementConfig(
        annual_generation_sha256=generation,
        distance_bands_km=distances,
        primary_distance_km=primary_distance,
        primary_devices=_positive_integer(
            analysis["primary_devices"], label="analysis.primary_devices"
        ),
        primary_stations=_positive_integer(
            analysis["primary_stations"], label="analysis.primary_stations"
        ),
        minimum_active_months=_positive_integer(
            analysis["minimum_active_months"], label="analysis.minimum_active_months"
        ),
        minimum_trio_dates=_positive_integer(
            analysis["minimum_trio_dates"], label="analysis.minimum_trio_dates"
        ),
        minimum_trio_hours=_positive_integer(
            analysis["minimum_trio_hours"], label="analysis.minimum_trio_hours"
        ),
        minimum_source_rows=_positive_integer(
            analysis["minimum_source_rows"], label="analysis.minimum_source_rows"
        ),
        minimum_observed_hours=_positive_integer(
            analysis["minimum_observed_hours"], label="analysis.minimum_observed_hours"
        ),
        station_folds=_positive_integer(analysis["station_folds"], label="analysis.station_folds"),
        quarters=quarters,
        ridge_alpha=_finite_number(analysis["ridge_alpha"], label="analysis.ridge_alpha"),
        threads=_positive_integer(analysis["threads"], label="analysis.threads"),
        memory_limit_gb=_positive_integer(
            analysis["memory_limit_gb"], label="analysis.memory_limit_gb"
        ),
        claim_boundary=boundary,
    )
    if loaded != AnnualAgreementConfig(
        annual_generation_sha256=generation,
        distance_bands_km=(0.5, 1.0, 2.0),
        primary_distance_km=0.5,
        primary_devices=124,
        primary_stations=13,
        minimum_active_months=3,
        minimum_trio_dates=30,
        minimum_trio_hours=360,
        minimum_source_rows=1080,
        minimum_observed_hours=18,
        station_folds=5,
        quarters=(1, 2, 3, 4),
        ridge_alpha=1.0,
        threads=1,
        memory_limit_gb=6,
        claim_boundary=tuple(_CLAIM_BOUNDARY.items()),
    ):
        raise ConfigError(
            "micro_sensor_annual_agreement.analysis changed from the reviewed protocol"
        )
    return loaded


def _load_annual_readiness_input(
    generation_dir: Path,
    *,
    expected_generation_sha256: str,
    config: AnnualAgreementConfig,
    reviewed_geography: pl.DataFrame | None = None,
) -> AnnualReadinessInput:
    if config.annual_generation_sha256 != expected_generation_sha256:
        raise RuntimeError("annual readiness generation and agreement config differ")
    if _is_link_like(generation_dir):
        raise RuntimeError("annual readiness generation directory is linked or outside")
    try:
        directory = generation_dir.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError("annual readiness generation directory is unreadable") from exc
    if not directory.is_dir():
        raise RuntimeError("annual readiness generation directory is unreadable")
    if directory.name != expected_generation_sha256:
        raise RuntimeError("annual readiness generation directory identity changed")
    expected_files = {
        "manifest.json",
        "summary.json",
        *(f"{name}.parquet" for name in _ANNUAL_MEMBER_SCHEMAS),
    }
    _validate_generation_inventory(
        directory,
        expected_files=expected_files,
        during_read=False,
    )
    manifest_path = directory / "manifest.json"
    manifest_before = _file_identity(manifest_path)
    manifest = _read_json(manifest_path, label="annual readiness manifest")
    if set(manifest) != _ANNUAL_MANIFEST_FIELDS:
        raise RuntimeError("annual readiness manifest fields changed")
    if manifest.get("complete") is not True:
        raise RuntimeError("annual readiness generation is not complete")
    if (
        type(manifest.get("schema_version")) is not int
        or manifest.get("schema_version") != 1
        or manifest.get("analysis") != "annual_micro_sensor_readiness"
    ):
        raise RuntimeError("annual readiness manifest contract changed")
    if manifest.get("generation_sha256") != expected_generation_sha256:
        raise RuntimeError("annual readiness generation changed")
    try:
        identity = {field: manifest[field] for field in _ANNUAL_IDENTITY_FIELDS}
    except KeyError as exc:
        raise RuntimeError("annual readiness generation identity is incomplete") from exc
    if _canonical_hash(identity) != expected_generation_sha256:
        raise RuntimeError("annual readiness generation identity changed")
    inputs = _mapping(manifest.get("inputs"), label="annual readiness inputs")
    expected_geography = inputs.get("reviewed_geography_sha256")
    if not isinstance(expected_geography, str) or _SHA256.fullmatch(expected_geography) is None:
        raise RuntimeError("annual readiness geography identity is missing")
    geography = reviewed_geography if reviewed_geography is not None else resolve_station_geo()
    if _geography_identity(geography) != expected_geography:
        raise RuntimeError("annual readiness geography identity changed")
    members = _mapping(manifest.get("members"), label="annual readiness members")
    _exact_keys(members, set(_ANNUAL_MEMBER_SCHEMAS), label="annual readiness members")
    loaded: dict[str, pl.DataFrame | PinnedAnnualMember] = {}
    pinned_members: dict[str, PinnedAnnualMember] = {}
    for name in _ANNUAL_MEMBER_SCHEMAS:
        loaded[name], pinned_members[name] = _load_annual_member(
            directory,
            name=name,
            declaration=members[name],
        )
    summary_declaration = _declared_file_identity(
        manifest.get("summary_file"), expected_path="summary.json"
    )
    summary_path = directory / "summary.json"
    summary_before = _file_identity(summary_path)
    if summary_before != summary_declaration:
        raise RuntimeError("annual readiness member changed: summary.json")
    summary = _read_json(summary_path, label="annual readiness summary")
    if _file_identity(summary_path) != summary_before:
        raise RuntimeError("annual readiness member changed during read: summary.json")
    summary_rows = _validate_annual_summary(summary)
    calendar = loaded["calendar_coverage"]
    device_days = loaded["device_days"]
    cohorts = loaded["device_cohorts"]
    thresholds = loaded["cohort_thresholds"]
    exclusions = loaded["exclusions"]
    if not isinstance(calendar, pl.DataFrame):
        raise RuntimeError("annual readiness calendar member was not loaded")
    if not isinstance(device_days, PinnedAnnualMember):
        raise RuntimeError("annual readiness device-day member identity was not retained")
    if not isinstance(cohorts, pl.DataFrame):
        raise RuntimeError("annual readiness cohort member was not loaded")
    if not isinstance(thresholds, pl.DataFrame):
        raise RuntimeError("annual readiness threshold member was not loaded")
    if not isinstance(exclusions, pl.DataFrame):
        raise RuntimeError("annual readiness exclusion member was not loaded")
    declared_rows = _validate_output_rows(
        manifest.get("output_rows"),
        label="annual readiness manifest output rows",
    )
    with stable_annual_member_path(device_days) as device_days_path:
        device_day_rows = pl.scan_parquet(device_days_path).select(pl.len()).collect().item()
    observed_rows = {
        "calendar_coverage": calendar.height,
        "device_days": device_day_rows,
        "device_cohorts": cohorts.height,
        "cohort_thresholds": thresholds.height,
        "exclusions": exclusions.height,
    }
    if declared_rows != observed_rows or summary_rows != observed_rows:
        raise RuntimeError("annual readiness output row counts changed")
    candidates = derive_agreement_candidates(cohorts, config=config)
    candidate_cohorts = tuple(
        AnnualDistanceCohort(
            radius_km=distance,
            candidates=candidates.filter(pl.col("distance_km") <= distance),
        )
        for distance in config.distance_bands_km
    )
    _validate_generation_inventory(
        directory,
        expected_files=expected_files,
        during_read=True,
    )
    for member in pinned_members.values():
        _assert_pinned_member(member, during_read=True)
    if _file_identity(summary_path) != summary_before:
        raise RuntimeError("annual readiness member changed during read: summary.json")
    if _file_identity(manifest_path) != manifest_before:
        raise RuntimeError("annual readiness manifest changed during read")
    _validate_generation_inventory(
        directory,
        expected_files=expected_files,
        during_read=True,
    )
    return AnnualReadinessInput(
        generation_dir=directory,
        manifest=manifest,
        summary=summary,
        calendar_coverage=calendar,
        device_days=device_days,
        device_cohorts=cohorts,
        cohort_thresholds=thresholds,
        exclusions=exclusions,
        candidate_cohorts=candidate_cohorts,
    )


def derive_agreement_candidates(
    device_cohorts: pl.DataFrame,
    *,
    config: AnnualAgreementConfig,
) -> pl.DataFrame:
    required = {
        "device_id",
        "station_name",
        "distance_km",
        "spatial_state",
        "active_months",
        "trio_dates",
        "trio_observed_hours",
    }
    missing = sorted(required - set(device_cohorts.columns))
    if missing:
        raise RuntimeError(f"annual readiness cohorts are missing required columns: {missing}")
    duplicates = device_cohorts.group_by("device_id").len().filter(pl.col("len") != 1)
    if duplicates.height:
        raise RuntimeError("annual readiness cohort device IDs are duplicated")
    candidates = (
        device_cohorts.filter(
            (pl.col("spatial_state") == "eligible")
            & (pl.col("active_months") >= config.minimum_active_months)
            & (pl.col("trio_dates") >= config.minimum_trio_dates)
            & (pl.col("trio_observed_hours") >= config.minimum_trio_hours)
            & pl.col("device_id").is_not_null()
            & pl.col("station_name").is_not_null()
            & pl.col("distance_km").is_not_null()
            & pl.col("distance_km").is_finite()
            & (pl.col("distance_km") <= max(config.distance_bands_km))
        )
        .select("device_id", "station_name", "distance_km")
        .sort("device_id")
    )
    primary = candidates.filter(pl.col("distance_km") <= config.primary_distance_km)
    if primary.height != config.primary_devices:
        raise RuntimeError("annual agreement primary device count changed")
    if primary["station_name"].n_unique() != config.primary_stations:
        raise RuntimeError("annual agreement primary station count changed")
    counts = tuple(
        candidates.filter(pl.col("distance_km") <= distance).height
        for distance in config.distance_bands_km
    )
    if any(left > right for left, right in pairwise(counts)):
        raise RuntimeError("annual agreement distance cohorts are not nested")
    return candidates


def load_annual_readiness_input(
    generation_dir: Path,
    *,
    reviewed_geography: pl.DataFrame | None = None,
) -> AnnualReadinessInput:
    config = load_annual_agreement_config()
    return _load_annual_readiness_input(
        generation_dir,
        expected_generation_sha256=config.annual_generation_sha256,
        config=config,
        reviewed_geography=reviewed_geography,
    )


def _sql_path(path: Path) -> str:
    return path.as_posix().replace("'", "''")


def _validate_agreement_candidates(candidates: pl.DataFrame) -> pl.DataFrame:
    expected = {"device_id": pl.String, "station_name": pl.String, "distance_km": pl.Float64}
    if not set(expected).issubset(candidates.columns) or any(
        candidates.schema[name] != dtype for name, dtype in expected.items()
    ):
        raise RuntimeError("annual agreement candidate schema changed")
    selected = candidates.select(*expected)
    invalid = selected.filter(
        pl.col("device_id").is_null()
        | (pl.col("device_id").str.strip_chars() == "")
        | pl.col("station_name").is_null()
        | (pl.col("station_name").str.strip_chars() == "")
        | pl.col("distance_km").is_null()
        | ~pl.col("distance_km").is_finite()
        | (pl.col("distance_km") < 0)
    )
    if invalid.height or selected["device_id"].n_unique() != selected.height:
        raise RuntimeError("annual agreement candidates are invalid")
    return selected.sort("device_id")


def _validate_agreement_source_member(path: Path, *, variable: str) -> None:
    try:
        schema = pl.read_parquet_schema(path)
    except (OSError, pl.exceptions.PolarsError) as exc:
        raise RuntimeError(f"annual agreement {variable} member is unreadable") from exc
    if schema != dict(OBSERVATION_OUTPUT_SCHEMA):
        raise RuntimeError(f"annual agreement {variable} member schema changed")


def _validate_agreement_ground_member(path: Path) -> None:
    try:
        schema = pl.read_parquet_schema(path)
    except (OSError, pl.exceptions.PolarsError) as exc:
        raise RuntimeError("annual agreement ground member is unreadable") from exc
    if set(schema) != set(PARTITION_SCHEMA) or any(
        schema[name] != dtype for name, dtype in PARTITION_SCHEMA.items()
    ):
        raise RuntimeError("annual agreement ground member schema changed")


def _create_agreement_variable_table(
    connection: duckdb.DuckDBPyConnection,
    *,
    variable: str,
    minimum: float,
    maximum: float,
) -> None:
    connection.execute(
        f"""
        CREATE TEMP TABLE {variable}_daily AS
        WITH duplicate_groups AS (
            SELECT device_id, count(*)::BIGINT AS duplicate_timestamp_groups
            FROM (
                SELECT device_id, ts_local
                FROM {variable}_input
                WHERE ts_local IS NOT NULL
                GROUP BY device_id, ts_local
                HAVING count(*) > 1
            ) groups
            GROUP BY device_id
        ), aggregated AS (
            SELECT device_id,
                   count(*)::BIGINT AS source_rows,
                   count(*) FILTER (WHERE value IS NULL)::BIGINT AS null_value_rows,
                   count(*) FILTER (WHERE ts_local IS NULL)::BIGINT AS null_timestamp_rows,
                   count(DISTINCT ts_local)::BIGINT AS distinct_timestamps,
                   count(DISTINCT date_trunc('hour', ts_local)) FILTER (
                       WHERE ts_local IS NOT NULL AND value IS NOT NULL
                   )::BIGINT AS observed_hours,
                   count(*) FILTER (
                       WHERE value IS NOT NULL AND (value < {minimum} OR value > {maximum})
                   )::BIGINT AS extreme_rows,
                   avg(value)::DOUBLE AS mean_value
            FROM {variable}_input
            GROUP BY device_id
        )
        SELECT aggregated.*,
               coalesce(duplicate_groups.duplicate_timestamp_groups, 0)::BIGINT
                   AS duplicate_timestamp_groups
        FROM aggregated LEFT JOIN duplicate_groups USING (device_id)
        """
    )
    connection.execute(
        f"""
        CREATE TEMP TABLE {variable}_hours AS
        SELECT DISTINCT device_id, date_trunc('hour', ts_local) AS hour
        FROM {variable}_input
        WHERE ts_local IS NOT NULL AND value IS NOT NULL
        """
    )


def _agreement_daily_query(day: date) -> str:
    metrics = ",\n".join(
        f"coalesce({variable}.{metric}, 0)::BIGINT AS {variable}_{metric}"
        for variable in _AGREEMENT_VARIABLES
        for metric in (
            "source_rows",
            "null_value_rows",
            "null_timestamp_rows",
            "distinct_timestamps",
            "observed_hours",
            "duplicate_timestamp_groups",
            "extreme_rows",
        )
    )
    return f"""
        SELECT DATE '{day.isoformat()}' AS date,
               candidates.device_id,
               {metrics},
               coalesce(trio.observed_hours, 0)::BIGINT AS trio_observed_hours,
               coalesce(coordinates.source_rows, 0)::BIGINT AS coordinate_source_rows,
               coalesce(coordinates.null_rows, 0)::BIGINT AS coordinate_null_rows,
               coalesce(coordinates.invalid_rows, 0)::BIGINT AS coordinate_invalid_rows,
               coalesce(coordinates.positions, 0)::BIGINT AS coordinate_positions,
               coordinates.lon_min, coordinates.lon_max,
               coordinates.lat_min, coordinates.lat_max,
               CASE WHEN pm25.source_rows IS NULL THEN 'missing_pm25_coordinate'
                    ELSE 'eligible' END AS spatial_state,
               candidates.station_name, candidates.distance_km,
               coalesce(ground.present_hours, 0)::BIGINT AS ground_present_trio_hours,
               coalesce(ground.eligible_hours, 0)::BIGINT AS ground_eligible_trio_hours,
               coalesce(ground.present_ineligible_hours, 0)::BIGINT
                   AS ground_present_ineligible_trio_hours,
               coalesce(ground.absent_hours, 0)::BIGINT AS ground_absent_trio_hours,
               pm25.mean_value AS raw_pm25_mean,
               humidity.mean_value AS raw_humidity_mean,
               temperature.mean_value AS raw_temperature_mean,
               ground.mean_value AS raw_ground_mean
        FROM candidates
        LEFT JOIN pm25_daily pm25 USING (device_id)
        LEFT JOIN humidity_daily humidity USING (device_id)
        LEFT JOIN temperature_daily temperature USING (device_id)
        LEFT JOIN trio USING (device_id)
        LEFT JOIN coordinates USING (device_id)
        LEFT JOIN ground_by_device ground USING (device_id)
        ORDER BY candidates.device_id
    """


def _assert_annual_counts_match(
    recomputed: pl.DataFrame,
    annual: pl.DataFrame,
) -> None:
    if annual["device_id"].n_unique() != annual.height:
        raise RuntimeError("annual agreement device-day identities changed")
    annual_rows = {str(row["device_id"]): row for row in annual.to_dicts()}
    compared = (
        *_AGREEMENT_COUNT_COLUMNS,
        "lon_min",
        "lon_max",
        "lat_min",
        "lat_max",
        "spatial_state",
        "station_name",
        "distance_km",
    )
    for row in recomputed.to_dicts():
        device_id = str(row["device_id"])
        source_present = any(
            row[f"{variable}_source_rows"] > 0 for variable in _AGREEMENT_VARIABLES
        )
        expected = annual_rows.pop(device_id, None)
        if source_present != (expected is not None):
            raise RuntimeError("annual agreement device-day presence changed")
        if expected is not None and any(row[name] != expected[name] for name in compared):
            raise RuntimeError("annual agreement derived device-day counts changed")
    if annual_rows:
        raise RuntimeError("annual agreement device-day identities changed")


def _agreement_reason_expression(config: AnnualAgreementConfig) -> pl.Expr:
    conditions: list[tuple[pl.Expr, str]] = [
        (
            pl.sum_horizontal(
                *(pl.col(f"{variable}_source_rows") for variable in _AGREEMENT_VARIABLES)
            )
            == 0,
            "device_day_absent",
        )
    ]
    checks = (
        ("null_timestamp_rows", "null_timestamp"),
        ("null_value_rows", "null_value"),
        ("duplicate_timestamp_groups", "duplicate_timestamp"),
        ("extreme_rows", "extreme_value"),
    )
    for variable in _AGREEMENT_VARIABLES:
        for metric, suffix in checks:
            conditions.append((pl.col(f"{variable}_{metric}") > 0, f"{variable}_{suffix}"))
        conditions.extend(
            (
                (
                    pl.col(f"{variable}_source_rows") < config.minimum_source_rows,
                    f"{variable}_insufficient_source_rows",
                ),
                (
                    pl.col(f"{variable}_observed_hours") < config.minimum_observed_hours,
                    f"{variable}_insufficient_observed_hours",
                ),
            )
        )
    conditions.extend(
        (
            (pl.col("ground_present_trio_hours") == 0, "ground_absent"),
            (
                pl.col("ground_eligible_trio_hours") < config.minimum_observed_hours,
                "ground_present_but_ineligible",
            ),
        )
    )
    reason = pl.lit("eligible")
    for condition, label in reversed(conditions):
        reason = pl.when(condition).then(pl.lit(label)).otherwise(reason)
    return reason.alias("reason")


def _annual_selected_date_rows(path: Path, *, day: date) -> int:
    try:
        schema = pl.read_parquet_schema(path)
        if schema != dict(ANNUAL_DEVICE_DAY_SCHEMA):
            raise RuntimeError("annual agreement device-day schema changed")
        rows = pl.scan_parquet(path).filter(pl.col("date") == day).select(pl.len()).collect().item()
    except (OSError, pl.exceptions.PolarsError) as exc:
        raise RuntimeError("annual agreement device-day member is unreadable") from exc
    if isinstance(rows, bool) or not isinstance(rows, int):
        raise RuntimeError("annual agreement selected device-day row count changed")
    return rows


def _aggregate_agreement_day_paths(
    *,
    day: date,
    micro_paths: dict[str, Path] | None,
    annual_device_days: Path,
    ground_path: Path | None,
    candidates: pl.DataFrame,
    config: AnnualAgreementConfig,
) -> AgreementDayAggregation:
    if config.threads != 1 or config.memory_limit_gb != 6:
        raise RuntimeError("annual agreement aggregation resource limits changed")
    selected_candidates = _validate_agreement_candidates(candidates)
    if micro_paths is None:
        if ground_path is not None:
            raise RuntimeError("catalogue-absent agreement day cannot have a ground input")
        if _annual_selected_date_rows(annual_device_days, day=day) != 0:
            raise RuntimeError("catalogue absence contradicts annual device-days")
        rows = selected_candidates.with_columns(
            pl.lit(day, dtype=pl.Date).alias("date"),
            *(pl.lit(0, dtype=pl.Int64).alias(name) for name in _AGREEMENT_COUNT_COLUMNS),
            pl.lit(None, dtype=pl.Float64).alias("lon_min"),
            pl.lit(None, dtype=pl.Float64).alias("lon_max"),
            pl.lit(None, dtype=pl.Float64).alias("lat_min"),
            pl.lit(None, dtype=pl.Float64).alias("lat_max"),
            pl.lit("missing_pm25_coordinate").alias("spatial_state"),
            pl.lit(None, dtype=pl.Float64).alias("micro_pm25_mean"),
            pl.lit(None, dtype=pl.Float64).alias("micro_humidity_mean"),
            pl.lit(None, dtype=pl.Float64).alias("micro_temperature_mean"),
            pl.lit(None, dtype=pl.Float64).alias("ground_pm25_mean"),
            pl.lit("catalogue_absent").alias("reason"),
        ).select(*(name for name, _ in AGREEMENT_DAY_SCHEMA))
        return AgreementDayAggregation(
            rows=rows,
            summary={"catalogue_absent": rows.height},
            input_identities={},
            config=config,
            input_paths=None,
        )
    if set(micro_paths) != set(_AGREEMENT_VARIABLES) or ground_path is None:
        raise RuntimeError("annual agreement aggregation requires three source members and ground")
    try:
        annual_schema = pl.read_parquet_schema(annual_device_days)
    except (OSError, pl.exceptions.PolarsError) as exc:
        raise RuntimeError("annual agreement device-day member is unreadable") from exc
    if annual_schema != dict(ANNUAL_DEVICE_DAY_SCHEMA):
        raise RuntimeError("annual agreement device-day schema changed")
    for variable in _AGREEMENT_VARIABLES:
        _validate_agreement_source_member(micro_paths[variable], variable=variable)
    _validate_agreement_ground_member(ground_path)
    identity_paths = (
        *tuple(micro_paths[variable] for variable in _AGREEMENT_VARIABLES),
        annual_device_days,
        ground_path,
    )
    before = tuple(_file_identity(path) for path in identity_paths)
    ranges = dict(load_annual_micro_sensor_panel_config().analysis.extreme_ranges)
    next_day = day + timedelta(days=1)
    with tempfile.TemporaryDirectory(prefix="twair-agreement-day-") as temporary:
        temp_dir = Path(temporary) / "duckdb-spill"
        temp_dir.mkdir()
        connection = duckdb.connect()
        try:
            connection.execute(f"SET threads={config.threads}")
            connection.execute(f"SET memory_limit='{config.memory_limit_gb}GB'")
            connection.execute(f"SET temp_directory='{_sql_path(temp_dir)}'")
            connection.execute("SET preserve_insertion_order=false")
            connection.register("candidate_input", selected_candidates.to_arrow())
            connection.execute("CREATE TEMP TABLE candidates AS SELECT * FROM candidate_input")
            for variable in _AGREEMENT_VARIABLES:
                path = micro_paths[variable]
                connection.execute(
                    f"CREATE TEMP VIEW {variable}_input AS "
                    f"SELECT source.* FROM read_parquet('{_sql_path(path)}') source "
                    "INNER JOIN candidates USING (device_id)"
                )
                invalid = connection.execute(
                    f"""
                    SELECT count(*) FILTER (WHERE variable IS NULL OR variable != ?),
                           count(*) FILTER (WHERE ts_local IS NOT NULL AND (
                               ts_local < ?::TIMESTAMP OR ts_local >= ?::TIMESTAMP
                           )),
                           count(*) FILTER (WHERE value IS NOT NULL AND NOT isfinite(value)),
                           count(*) FILTER (WHERE (coordinate_wgs84_valid IS NULL)
                               != (lon IS NULL OR lat IS NULL))
                    FROM {variable}_input
                    """,
                    (variable, day.isoformat(), next_day.isoformat()),
                ).fetchone()
                if invalid is None or any(int(value) for value in invalid):
                    raise RuntimeError(f"annual agreement {variable} source rows are invalid")
                _create_agreement_variable_table(
                    connection,
                    variable=variable,
                    minimum=ranges[variable][0],
                    maximum=ranges[variable][1],
                )
            connection.execute(
                """
                CREATE TEMP TABLE trio_hours AS
                SELECT pm25.device_id, pm25.hour
                FROM pm25_hours pm25
                INNER JOIN humidity_hours humidity USING (device_id, hour)
                INNER JOIN temperature_hours temperature USING (device_id, hour)
                """
            )
            connection.execute(
                """
                CREATE TEMP TABLE trio AS
                SELECT device_id, count(*)::BIGINT AS observed_hours
                FROM trio_hours
                GROUP BY device_id
                """
            )
            connection.execute(
                """
                CREATE TEMP TABLE coordinates AS
                SELECT device_id, count(*)::BIGINT AS source_rows,
                       count(*) FILTER (WHERE lon IS NULL OR lat IS NULL)::BIGINT AS null_rows,
                       count(*) FILTER (WHERE coordinate_wgs84_valid IS FALSE)::BIGINT AS invalid_rows,
                       count(DISTINCT struct_pack(lon := lon, lat := lat)) FILTER (
                           WHERE coordinate_wgs84_valid IS TRUE
                       )::BIGINT AS positions,
                       min(lon) FILTER (WHERE coordinate_wgs84_valid IS TRUE)::DOUBLE AS lon_min,
                       max(lon) FILTER (WHERE coordinate_wgs84_valid IS TRUE)::DOUBLE AS lon_max,
                       min(lat) FILTER (WHERE coordinate_wgs84_valid IS TRUE)::DOUBLE AS lat_min,
                       max(lat) FILTER (WHERE coordinate_wgs84_valid IS TRUE)::DOUBLE AS lat_max
                FROM pm25_input GROUP BY device_id
                """
            )
            connection.execute(
                f"""
                CREATE TEMP TABLE ground_pm25 AS
                SELECT CAST(station_name AS VARCHAR) AS station_name, ts_local AS hour,
                       value::DOUBLE AS ground_pm25, CAST(flag AS VARCHAR) AS ground_flag
                FROM read_parquet('{_sql_path(ground_path)}')
                WHERE CAST(pollutant AS VARCHAR) = 'PM2.5'
                  AND ts_local >= TIMESTAMP '{day.isoformat()} 00:00:00'
                  AND ts_local < TIMESTAMP '{next_day.isoformat()} 00:00:00'
                  AND station_name IN (SELECT station_name FROM candidates)
                """
            )
            ground_invalid = connection.execute(
                """
                    SELECT (SELECT count(*) FROM (
                               SELECT station_name, hour FROM ground_pm25
                               GROUP BY ALL HAVING count(*) > 1
                           ))
                    """
            ).fetchone()
            if ground_invalid is None or any(int(value) for value in ground_invalid):
                raise RuntimeError("annual agreement ground PM2.5 member is invalid")
            connection.execute(
                """
                CREATE TEMP TABLE ground_by_device AS
                SELECT trio.device_id,
                       count(*) FILTER (WHERE ground.hour IS NOT NULL)::BIGINT AS present_hours,
                       count(*) FILTER (WHERE ground.hour IS NOT NULL
                           AND ground.ground_flag = 'valid'
                           AND ground.ground_pm25 IS NOT NULL
                           AND isfinite(ground.ground_pm25))::BIGINT AS eligible_hours,
                       count(*) FILTER (WHERE ground.hour IS NOT NULL AND NOT coalesce(
                           ground.ground_flag = 'valid' AND ground.ground_pm25 IS NOT NULL
                           AND isfinite(ground.ground_pm25),
                           false
                       ))::BIGINT AS present_ineligible_hours,
                       count(*) FILTER (WHERE ground.hour IS NULL)::BIGINT AS absent_hours,
                       avg(ground.ground_pm25) FILTER (WHERE ground.ground_flag = 'valid'
                           AND ground.ground_pm25 IS NOT NULL
                           AND isfinite(ground.ground_pm25))::DOUBLE AS mean_value
                FROM trio_hours trio
                INNER JOIN candidates USING (device_id)
                LEFT JOIN ground_pm25 ground
                  ON ground.station_name = candidates.station_name AND ground.hour = trio.hour
                GROUP BY trio.device_id
                """
            )
            recomputed = pl.DataFrame(
                connection.execute(_agreement_daily_query(day)).to_arrow_table()
            )
            annual = pl.DataFrame(
                connection.execute(
                    f"""
                    SELECT annual.* FROM read_parquet('{_sql_path(annual_device_days)}') annual
                    INNER JOIN candidates USING (device_id)
                    WHERE annual.date = DATE '{day.isoformat()}'
                    """
                ).to_arrow_table()
            ).cast(dict(ANNUAL_DEVICE_DAY_SCHEMA), strict=True)
        finally:
            connection.close()
            shutil.rmtree(temp_dir)
    after = tuple(_file_identity(path) for path in identity_paths)
    if before != after:
        raise RuntimeError("an annual agreement input changed while it was read")
    recomputed = recomputed.cast(
        {
            **dict(ANNUAL_DEVICE_DAY_SCHEMA),
            "raw_pm25_mean": pl.Float64,
            "raw_humidity_mean": pl.Float64,
            "raw_temperature_mean": pl.Float64,
            "raw_ground_mean": pl.Float64,
        },
        strict=True,
    )
    _assert_annual_counts_match(recomputed, annual)
    reason = _agreement_reason_expression(config)
    rows = (
        recomputed.with_columns(reason)
        .with_columns(
            pl.when(pl.col("reason") == "eligible")
            .then(pl.col("raw_pm25_mean"))
            .alias("micro_pm25_mean"),
            pl.when(pl.col("reason") == "eligible")
            .then(pl.col("raw_humidity_mean"))
            .alias("micro_humidity_mean"),
            pl.when(pl.col("reason") == "eligible")
            .then(pl.col("raw_temperature_mean"))
            .alias("micro_temperature_mean"),
            pl.when(pl.col("reason") == "eligible")
            .then(pl.col("raw_ground_mean"))
            .alias("ground_pm25_mean"),
        )
        .select(*(name for name, _ in AGREEMENT_DAY_SCHEMA))
    )
    rows = rows.cast(dict(AGREEMENT_DAY_SCHEMA), strict=True)
    summary = {
        str(reason_value): count
        for reason_value, count in rows.group_by("reason").len().iter_rows()
    }
    return AgreementDayAggregation(
        rows=rows,
        summary=summary,
        input_identities={},
        config=config,
        input_paths=None,
    )


def _observed_checkpoint_file_identity(path: Path) -> dict[str, object]:
    observed = _file_identity(path)
    return {"path": path.name, "bytes": observed["bytes"], "sha256": observed["sha256"]}


def _aggregate_agreement_day(
    *,
    day: date,
    micro_paths: dict[str, Path] | None,
    annual_device_days: PinnedAnnualMember,
    ground_path: Path | None,
    candidates: pl.DataFrame,
    input_identities: dict[str, object],
    config: AnnualAgreementConfig,
) -> AgreementDayAggregation:
    normalized = _checkpoint_inputs(input_identities, config=config)
    if normalized["annual_device_days"] != {
        "path": annual_device_days.path.name,
        "bytes": annual_device_days.bytes,
        "sha256": annual_device_days.sha256,
    }:
        raise RuntimeError("annual agreement device-day member changed")
    selected = _validate_agreement_candidates(candidates)
    if normalized["candidate_identity_sha256"] != _canonical_hash(selected.to_dicts()):
        raise RuntimeError("annual agreement candidate identity changed")
    if micro_paths is None:
        if normalized["catalogue_state"] != "absent":
            raise RuntimeError("annual agreement catalogue state changed")
        with stable_annual_member_path(annual_device_days) as annual_path:
            selected_date_rows = _annual_selected_date_rows(annual_path, day=day)
            if selected_date_rows != 0:
                raise RuntimeError("catalogue absence contradicts annual device-days")
            if normalized["annual_selected_date_rows"] != selected_date_rows:
                raise RuntimeError("annual agreement selected device-day row count changed")
            result = _aggregate_agreement_day_paths(
                day=day,
                micro_paths=None,
                annual_device_days=annual_path,
                ground_path=ground_path,
                candidates=selected,
                config=config,
            )
        return replace(
            result,
            input_identities=normalized,
            input_paths=AgreementDayInputPaths((), annual_device_days, None),
        )
    if set(micro_paths) != set(_AGREEMENT_VARIABLES) or ground_path is None:
        raise RuntimeError("annual agreement aggregation requires three source members and ground")
    if normalized["catalogue_state"] != "present":
        raise RuntimeError("annual agreement catalogue state changed")
    normalized_sources = _mapping(
        normalized["source_members"], label="annual agreement source members"
    )
    for variable in _AGREEMENT_VARIABLES:
        if normalized_sources[variable] != _observed_checkpoint_file_identity(
            micro_paths[variable]
        ):
            raise RuntimeError(f"annual agreement {variable} source member changed")
    if normalized["ground_member"] != _observed_checkpoint_file_identity(ground_path):
        raise RuntimeError("annual agreement ground member changed")
    with stable_annual_member_path(annual_device_days) as annual_path:
        if normalized["annual_selected_date_rows"] != _annual_selected_date_rows(
            annual_path, day=day
        ):
            raise RuntimeError("annual agreement selected device-day row count changed")
        result = _aggregate_agreement_day_paths(
            day=day,
            micro_paths=micro_paths,
            annual_device_days=annual_path,
            ground_path=ground_path,
            candidates=selected,
            config=config,
        )
    return replace(
        result,
        input_identities=normalized,
        input_paths=AgreementDayInputPaths(
            tuple((variable, micro_paths[variable]) for variable in _AGREEMENT_VARIABLES),
            annual_device_days,
            ground_path,
        ),
    )


def aggregate_agreement_day(
    *,
    day: date,
    micro_paths: dict[str, Path] | None,
    annual_device_days: PinnedAnnualMember,
    ground_path: Path | None,
    candidates: pl.DataFrame,
    input_identities: dict[str, object],
    config: AnnualAgreementConfig,
) -> AgreementDayAggregation:
    if not isinstance(annual_device_days, PinnedAnnualMember):
        raise TypeError("annual_device_days must be a PinnedAnnualMember")
    reviewed = load_annual_agreement_config()
    if config != reviewed:
        raise RuntimeError("annual agreement reviewed protocol changed")
    selected = _validate_agreement_candidates(candidates)
    if (
        selected.height != reviewed.primary_devices
        or selected["station_name"].n_unique() != reviewed.primary_stations
        or selected.filter(pl.col("distance_km") > reviewed.primary_distance_km).height
    ):
        raise RuntimeError("annual agreement reviewed candidate cohort changed")
    readiness = load_annual_readiness_input(annual_device_days.generation_dir)
    if readiness.device_days != annual_device_days:
        raise RuntimeError("annual agreement reviewed annual member changed")
    primary_cohorts = tuple(
        cohort
        for cohort in readiness.candidate_cohorts
        if cohort.radius_km == reviewed.primary_distance_km
    )
    if len(primary_cohorts) != 1:
        raise RuntimeError("annual agreement reviewed primary cohort changed")
    reviewed_candidates = _validate_agreement_candidates(primary_cohorts[0].candidates)
    if _canonical_hash(selected.to_dicts()) != _canonical_hash(reviewed_candidates.to_dicts()):
        raise RuntimeError("annual agreement exact reviewed primary cohort changed")
    return _aggregate_agreement_day(
        day=day,
        micro_paths=micro_paths,
        annual_device_days=annual_device_days,
        ground_path=ground_path,
        candidates=reviewed_candidates,
        input_identities=input_identities,
        config=config,
    )


def _validate_agreement_day_rows(frame: pl.DataFrame, *, day: date) -> None:
    if frame.schema != dict(AGREEMENT_DAY_SCHEMA):
        raise RuntimeError("annual agreement checkpoint schema changed")
    if (
        frame["date"].null_count()
        or set(frame["date"].unique().to_list()) != {day}
        or frame["device_id"].null_count()
        or frame["device_id"].n_unique() != frame.height
    ):
        raise RuntimeError("annual agreement checkpoint row identities changed")
    if any(
        frame[column].null_count() or (frame[column] < 0).any()
        for column in _AGREEMENT_COUNT_COLUMNS
    ):
        raise RuntimeError("annual agreement checkpoint counts changed")
    model_columns = (
        "micro_pm25_mean",
        "micro_humidity_mean",
        "micro_temperature_mean",
        "ground_pm25_mean",
    )
    if frame.filter(
        pl.any_horizontal(
            pl.col(column).is_not_null() & ~pl.col(column).is_finite() for column in model_columns
        )
    ).height:
        raise RuntimeError("annual agreement checkpoint model value is non-finite")
    if frame.filter(
        (pl.col("reason") != "eligible")
        & pl.any_horizontal(pl.col(column).is_not_null() for column in model_columns)
    ).height:
        raise RuntimeError("annual agreement checkpoint ineligible value is not null")
    if (
        frame["reason"].null_count()
        or frame.filter(
            (pl.col("reason") == "eligible")
            & pl.any_horizontal(pl.col(column).is_null() for column in model_columns)
        ).height
    ):
        raise RuntimeError("annual agreement checkpoint eligible value is null")


def _checkpoint_config(config: AnnualAgreementConfig) -> dict[str, object]:
    value = json.loads(json.dumps(asdict(config), allow_nan=False))
    if not isinstance(value, dict):
        raise RuntimeError("annual agreement checkpoint config is invalid")
    return value


def _checkpoint_file_identity(value: object, *, label: str) -> dict[str, object]:
    raw = _mapping(value, label=label)
    if set(raw) != {"path", "bytes", "sha256"}:
        raise RuntimeError(f"{label} fields changed")
    path = raw["path"]
    size = raw["bytes"]
    digest = raw["sha256"]
    if (
        not isinstance(path, str)
        or not path
        or Path(path).is_absolute()
        or any(part in {"", ".", ".."} for part in Path(path).parts)
    ):
        raise RuntimeError(f"{label} path changed")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise RuntimeError(f"{label} bytes changed")
    if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
        raise RuntimeError(f"{label} SHA-256 changed")
    return {"path": path, "bytes": size, "sha256": digest}


def _checkpoint_inputs(
    value: dict[str, object],
    *,
    config: AnnualAgreementConfig,
) -> dict[str, object]:
    expected = {
        "catalog_generation_sha256",
        "raw_observation_generation_sha256",
        "parsed_generation_sha256",
        "source_members",
        "ground_member",
        "annual_generation_sha256",
        "annual_device_days",
        "candidate_identity_sha256",
        "catalogue_state",
        "annual_selected_date_rows",
    }
    if set(value) != expected:
        raise RuntimeError("annual agreement checkpoint input identity fields changed")
    identities: dict[str, str] = {}
    for name in (
        "catalog_generation_sha256",
        "raw_observation_generation_sha256",
        "parsed_generation_sha256",
        "annual_generation_sha256",
        "candidate_identity_sha256",
    ):
        item = value[name]
        if not isinstance(item, str) or _SHA256.fullmatch(item) is None:
            raise RuntimeError(f"annual agreement checkpoint {name} changed")
        identities[name] = item
    if identities["annual_generation_sha256"] != config.annual_generation_sha256:
        raise RuntimeError("annual agreement checkpoint annual generation changed")
    catalogue_state = value["catalogue_state"]
    if catalogue_state not in {"present", "absent"}:
        raise RuntimeError("annual agreement checkpoint catalogue state changed")
    selected_date_rows = value["annual_selected_date_rows"]
    if (
        isinstance(selected_date_rows, bool)
        or not isinstance(selected_date_rows, int)
        or selected_date_rows < 0
    ):
        raise RuntimeError("annual agreement checkpoint selected-date rows changed")
    if catalogue_state == "absent":
        if (
            value["source_members"] is not None
            or value["ground_member"] is not None
            or selected_date_rows != 0
        ):
            raise RuntimeError("annual agreement checkpoint catalogue absence changed")
        sources: dict[str, object] | None = None
        ground: dict[str, object] | None = None
    else:
        source_raw = _mapping(value["source_members"], label="annual agreement source members")
        if set(source_raw) != set(_AGREEMENT_VARIABLES):
            raise RuntimeError("annual agreement checkpoint source member fields changed")
        sources = {
            variable: _checkpoint_file_identity(
                source_raw[variable],
                label=f"annual agreement {variable} source member",
            )
            for variable in _AGREEMENT_VARIABLES
        }
        ground = _checkpoint_file_identity(
            value["ground_member"],
            label="annual agreement ground member",
        )
    return {
        **identities,
        "source_members": sources,
        "ground_member": ground,
        "annual_device_days": _checkpoint_file_identity(
            value["annual_device_days"],
            label="annual agreement device-day member",
        ),
        "catalogue_state": catalogue_state,
        "annual_selected_date_rows": selected_date_rows,
    }


def _checkpoint_contract(
    *,
    day: date,
    input_identities: dict[str, object],
    config: AnnualAgreementConfig,
) -> dict[str, object]:
    normalized_inputs = _checkpoint_inputs(input_identities, config=config)
    return {
        "schema_version": 1,
        "kind": "annual_reference_station_agreement_day",
        "date": day.isoformat(),
        "inputs": normalized_inputs,
        "config": _checkpoint_config(config),
    }


def _agreement_checkpoint_directory(checkpoint_root: Path, *, day: date) -> Path:
    return checkpoint_root / day.isoformat()


def _write_checkpoint_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


@contextmanager
def _agreement_checkpoint_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = path.open("a+b")
    acquired = False
    try:
        if path.stat().st_size == 0:
            lock_file.write(b"\0")
            lock_file.flush()
        lock_file.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError("another annual agreement checkpoint writer is active") from None
        acquired = True
        yield
    finally:
        try:
            if acquired:
                lock_file.seek(0)
                if sys.platform == "win32":
                    import msvcrt

                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            lock_file.close()


def _recover_agreement_checkpoint_swap(destination: Path) -> None:
    parent = destination.parent
    if not parent.exists():
        return
    stages = sorted(parent.glob(f".{destination.name}.staging-*"))
    backups = sorted(parent.glob(f".{destination.name}.backup-*"))
    if len(stages) > 1 or len(backups) > 1:
        raise RuntimeError("multiple interrupted annual agreement checkpoint swaps")
    if destination.exists() and stages and backups:
        raise RuntimeError("ambiguous interrupted annual agreement checkpoint swap")
    if not destination.exists() and backups:
        backups[0].replace(destination)
        backups = []
    for staged in stages:
        shutil.rmtree(staged)
    if destination.exists() and backups:
        shutil.rmtree(backups[0])


def _replaceable_incomplete_checkpoint(destination: Path) -> bool:
    manifest_path = destination / "manifest.json"
    if not manifest_path.exists():
        return True
    manifest = _read_json(
        manifest_path,
        label="existing annual agreement checkpoint manifest",
    )
    return manifest.get("complete") is not True


def _same_json_type_and_value(observed: object, expected: object) -> bool:
    if type(observed) is not type(expected):
        return False
    if isinstance(expected, dict) and isinstance(observed, dict):
        return set(observed) == set(expected) and all(
            _same_json_type_and_value(observed[key], value) for key, value in expected.items()
        )
    if isinstance(expected, list) and isinstance(observed, list):
        return len(observed) == len(expected) and all(
            _same_json_type_and_value(left, right)
            for left, right in zip(observed, expected, strict=True)
        )
    return observed == expected


def _load_agreement_day_checkpoint_unlocked(
    *,
    day: date,
    input_identities: dict[str, object],
    config: AnnualAgreementConfig,
    checkpoint_root: Path,
) -> AgreementDayCheckpoint:
    directory = _agreement_checkpoint_directory(checkpoint_root, day=day)
    member_path = directory / "paired_day.parquet"
    manifest_path = directory / "manifest.json"
    if not directory.is_dir():
        raise FileNotFoundError(f"annual agreement checkpoint is missing: {day}")
    if {path.name for path in directory.iterdir()} != {"paired_day.parquet", "manifest.json"}:
        raise RuntimeError("annual agreement checkpoint file set changed")
    manifest = _read_json(manifest_path, label="annual agreement checkpoint manifest")
    contract = _checkpoint_contract(
        day=day,
        input_identities=input_identities,
        config=config,
    )
    if type(manifest.get("schema_version")) is not int:
        raise RuntimeError("annual agreement checkpoint manifest contract changed")
    if any(
        not _same_json_type_and_value(manifest.get(key), value) for key, value in contract.items()
    ):
        raise RuntimeError("annual agreement checkpoint input identity changed")
    if (
        set(manifest)
        != {
            *contract,
            "complete",
            "rows",
            "schema",
            "member",
            "summary",
            "summary_sha256",
        }
        or type(manifest.get("schema_version")) is not int
        or manifest.get("complete") is not True
    ):
        raise RuntimeError("annual agreement checkpoint manifest contract changed")
    expected_schema = {name: str(dtype) for name, dtype in AGREEMENT_DAY_SCHEMA}
    if manifest.get("schema") != expected_schema:
        raise RuntimeError("annual agreement checkpoint schema changed")
    observed_member = _file_identity(member_path)
    expected_member = {
        "path": "paired_day.parquet",
        "bytes": observed_member["bytes"],
        "sha256": observed_member["sha256"],
    }
    if manifest.get("member") != expected_member:
        raise RuntimeError("annual agreement checkpoint member changed")
    before = (observed_member, _file_identity(manifest_path))
    try:
        rows = pl.read_parquet(member_path)
    except (OSError, pl.exceptions.PolarsError) as exc:
        raise RuntimeError("annual agreement checkpoint member is unreadable") from exc
    _validate_agreement_day_rows(rows, day=day)
    declared_rows = manifest.get("rows")
    if type(declared_rows) is not int or declared_rows < 0 or declared_rows != rows.height:
        raise RuntimeError("annual agreement checkpoint row count changed")
    summary = manifest.get("summary")
    if (
        not isinstance(summary, dict)
        or not all(
            isinstance(key, str) and type(value) is int and value >= 0
            for key, value in summary.items()
        )
        or manifest.get("summary_sha256") != _canonical_hash(summary)
    ):
        raise RuntimeError("annual agreement checkpoint summary changed")
    observed_summary = {
        str(reason): count for reason, count in rows.group_by("reason").len().iter_rows()
    }
    if summary != observed_summary:
        raise RuntimeError("annual agreement checkpoint summary changed")
    after = (_file_identity(member_path), _file_identity(manifest_path))
    if before != after:
        raise RuntimeError("annual agreement checkpoint changed during read")
    return AgreementDayCheckpoint(
        directory=directory,
        member_path=member_path,
        manifest_path=manifest_path,
        rows=rows,
        manifest=manifest,
    )


def _load_agreement_day_checkpoint(
    *,
    day: date,
    input_identities: dict[str, object],
    config: AnnualAgreementConfig,
    checkpoint_root: Path,
) -> AgreementDayCheckpoint:
    destination = _agreement_checkpoint_directory(checkpoint_root, day=day)
    with _agreement_checkpoint_lock(checkpoint_root / f".{day.isoformat()}.lock"):
        _recover_agreement_checkpoint_swap(destination)
        return _load_agreement_day_checkpoint_unlocked(
            day=day,
            input_identities=input_identities,
            config=config,
            checkpoint_root=checkpoint_root,
        )


def load_agreement_day_checkpoint(
    *,
    day: date,
    input_identities: dict[str, object],
    config: AnnualAgreementConfig,
    checkpoint_root: Path,
) -> AgreementDayCheckpoint:
    if config != load_annual_agreement_config():
        raise RuntimeError("annual agreement reviewed protocol changed")
    return _load_agreement_day_checkpoint(
        day=day,
        input_identities=input_identities,
        config=config,
        checkpoint_root=checkpoint_root,
    )


def _revalidate_agreement_result_inputs(result: AgreementDayAggregation) -> None:
    paths = result.input_paths
    if paths is None:
        raise RuntimeError("annual agreement aggregation input paths are missing")
    normalized = _checkpoint_inputs(result.input_identities, config=result.config)
    micro_paths = dict(paths.micro_paths)
    catalogue_absent_rows = (
        result.rows.filter(pl.col("reason") == "catalogue_absent").height
        if "reason" in result.rows.columns
        else -1
    )
    if normalized["catalogue_state"] == "absent":
        if (
            paths.micro_paths
            or paths.ground_path is not None
            or normalized["source_members"] is not None
            or normalized["ground_member"] is not None
            or not result.rows.height
            or catalogue_absent_rows != result.rows.height
        ):
            raise RuntimeError("annual agreement catalogue state and input paths disagree")
    elif (
        len(paths.micro_paths) != len(_AGREEMENT_VARIABLES)
        or set(micro_paths) != set(_AGREEMENT_VARIABLES)
        or paths.ground_path is None
        or not isinstance(normalized["source_members"], dict)
        or not isinstance(normalized["ground_member"], dict)
        or catalogue_absent_rows != 0
    ):
        raise RuntimeError("annual agreement catalogue state and input paths disagree")
    if normalized["catalogue_state"] == "present":
        normalized_sources = _mapping(
            normalized["source_members"], label="annual agreement source members"
        )
        for variable in _AGREEMENT_VARIABLES:
            if normalized_sources[variable] != _observed_checkpoint_file_identity(
                micro_paths[variable]
            ):
                raise RuntimeError(f"annual agreement {variable} source member changed")
        if paths.ground_path is None or normalized[
            "ground_member"
        ] != _observed_checkpoint_file_identity(paths.ground_path):
            raise RuntimeError("annual agreement ground member changed")
    with stable_annual_member_path(paths.annual_device_days) as annual_path:
        if normalized["annual_device_days"] != {
            "path": paths.annual_device_days.path.name,
            "bytes": paths.annual_device_days.bytes,
            "sha256": paths.annual_device_days.sha256,
        }:
            raise RuntimeError("annual agreement device-day member changed")
        days = result.rows["date"].unique().to_list()
        if len(days) != 1 or not isinstance(days[0], date):
            raise RuntimeError("annual agreement aggregation date changed")
        if normalized["annual_selected_date_rows"] != _annual_selected_date_rows(
            annual_path, day=days[0]
        ):
            raise RuntimeError("annual agreement selected device-day row count changed")
    candidates = result.rows.select("device_id", "station_name", "distance_km").sort("device_id")
    if normalized["candidate_identity_sha256"] != _canonical_hash(candidates.to_dicts()):
        raise RuntimeError("annual agreement candidate identity changed")


def _write_agreement_day_checkpoint(
    result: AgreementDayAggregation,
    *,
    day: date,
    checkpoint_root: Path,
) -> AgreementDayCheckpoint:
    _revalidate_agreement_result_inputs(result)
    _validate_agreement_day_rows(result.rows, day=day)
    observed_summary = {
        str(reason): count for reason, count in result.rows.group_by("reason").len().iter_rows()
    }
    if result.summary != observed_summary:
        raise RuntimeError("annual agreement checkpoint summary changed")
    contract = _checkpoint_contract(
        day=day,
        input_identities=result.input_identities,
        config=result.config,
    )
    destination = _agreement_checkpoint_directory(checkpoint_root, day=day)
    lock_path = checkpoint_root / f".{day.isoformat()}.lock"
    with _agreement_checkpoint_lock(lock_path):
        _recover_agreement_checkpoint_swap(destination)
        if destination.exists() and not _replaceable_incomplete_checkpoint(destination):
            return _load_agreement_day_checkpoint_unlocked(
                day=day,
                input_identities=result.input_identities,
                config=result.config,
                checkpoint_root=checkpoint_root,
            )
        checkpoint_root.mkdir(parents=True, exist_ok=True)
        token = uuid4().hex
        staged = destination.with_name(f".{destination.name}.staging-{token}")
        backup = destination.with_name(f".{destination.name}.backup-{token}")
        staged.mkdir()
        had_existing = destination.exists()
        try:
            member_path = staged / "paired_day.parquet"
            result.rows.write_parquet(member_path)
            member = _file_identity(member_path)
            manifest = {
                **contract,
                "rows": result.rows.height,
                "schema": {name: str(dtype) for name, dtype in AGREEMENT_DAY_SCHEMA},
                "member": {
                    "path": "paired_day.parquet",
                    "bytes": member["bytes"],
                    "sha256": member["sha256"],
                },
                "summary": result.summary,
                "summary_sha256": _canonical_hash(result.summary),
                "complete": True,
            }
            _write_checkpoint_json(staged / "manifest.json", manifest)
            try:
                if had_existing:
                    destination.replace(backup)
                staged.replace(destination)
            except BaseException:
                if had_existing and backup.exists():
                    if destination.exists():
                        shutil.rmtree(destination)
                    backup.replace(destination)
                raise
            if backup.exists():
                shutil.rmtree(backup)
        finally:
            if staged.exists():
                shutil.rmtree(staged)
        return _load_agreement_day_checkpoint_unlocked(
            day=day,
            input_identities=result.input_identities,
            config=result.config,
            checkpoint_root=checkpoint_root,
        )


def write_agreement_day_checkpoint(
    result: AgreementDayAggregation,
    *,
    day: date,
    checkpoint_root: Path,
) -> AgreementDayCheckpoint:
    reviewed = load_annual_agreement_config()
    if result.config != reviewed:
        raise RuntimeError("annual agreement reviewed protocol changed")
    rows = _validate_agreement_candidates(
        result.rows.select("device_id", "station_name", "distance_km")
    )
    if (
        rows.height != reviewed.primary_devices
        or rows["station_name"].n_unique() != reviewed.primary_stations
        or rows.filter(pl.col("distance_km") > reviewed.primary_distance_km).height
    ):
        raise RuntimeError("annual agreement reviewed candidate cohort changed")
    return _write_agreement_day_checkpoint(
        result,
        day=day,
        checkpoint_root=checkpoint_root,
    )


def _agreement_calendar(annual_input: AnnualReadinessInput) -> pl.DataFrame:
    source = annual_input.calendar_coverage
    if source.schema != dict(ANNUAL_CALENDAR_SCHEMA) or source.height != 365:
        raise RuntimeError("annual agreement calendar contract changed")
    ordered = source.sort("date")
    expected_dates = [date(2025, 1, 1) + timedelta(days=index) for index in range(365)]
    if ordered["date"].to_list() != expected_dates:
        raise RuntimeError("annual agreement calendar arithmetic changed")
    if (
        ordered.filter(pl.col("state") == "complete").height != 322
        or ordered.filter(pl.col("state") == "catalogue_absent").height != 43
        or ordered.filter(~pl.col("state").is_in(["complete", "catalogue_absent"])).height
        or ordered.filter(
            (pl.col("state") == "complete") != pl.col("parsed_generation_sha256").is_not_null()
        ).height
    ):
        raise RuntimeError("annual agreement calendar partition changed")
    return ordered.rename({"state": "calendar_state"}).cast(
        dict(AGREEMENT_CALENDAR_SCHEMA), strict=True
    )


def _checkpoint_inventory_evidence(
    checkpoints: tuple[AgreementDayCheckpoint, ...],
    *,
    annual_input: AnnualReadinessInput,
    calendar: pl.DataFrame,
    candidates: pl.DataFrame,
    config: AnnualAgreementConfig,
) -> tuple[tuple[Path, ...], list[dict[str, object]], tuple[dict[str, object], ...]]:
    parsed_rows = calendar.filter(pl.col("calendar_state") == "complete")
    parsed_dates = parsed_rows["date"].to_list()
    observed_dates: list[date] = []
    paths: list[Path] = []
    evidence: list[dict[str, object]] = []
    before: list[dict[str, object]] = []
    candidate_identity = _canonical_hash(candidates.sort("device_id").to_dicts())
    expected_schema = {name: str(dtype) for name, dtype in AGREEMENT_DAY_SCHEMA}
    reviewed_inputs = _mapping(
        annual_input.manifest.get("inputs"), label="reviewed annual agreement inputs"
    )
    catalog_generations = _mapping(
        reviewed_inputs.get("catalog_generations"),
        label="reviewed annual agreement catalog generations",
    )
    parsed_generations = reviewed_inputs.get("parsed_generations")
    ground_files = reviewed_inputs.get("ground_files")
    if (
        not isinstance(parsed_generations, list)
        or len(parsed_generations) != len(parsed_dates)
        or not isinstance(ground_files, list)
        or len(ground_files) != 12
    ):
        raise RuntimeError("annual agreement reviewed input inventory changed")
    reviewed_parsed = {
        str(_mapping(item, label="reviewed annual parsed generation").get("date")): _mapping(
            item, label="reviewed annual parsed generation"
        )
        for item in parsed_generations
    }
    checkpoint_roots = {
        checkpoint.directory.parent.resolve(strict=True) for checkpoint in checkpoints
    }
    if len(checkpoint_roots) != 1:
        raise RuntimeError("annual agreement checkpoints span multiple roots")
    checkpoint_root = checkpoint_roots.pop()
    for checkpoint in checkpoints:
        manifest_path = checkpoint.manifest_path
        member_path = checkpoint.member_path
        try:
            resolved_directory = checkpoint.directory.resolve(strict=True)
            resolved_manifest = manifest_path.resolve(strict=True)
            resolved_member = member_path.resolve(strict=True)
        except OSError as exc:
            raise RuntimeError("annual agreement checkpoint path is unreadable") from exc
        if (
            _is_link_like(checkpoint.directory)
            or _is_link_like(manifest_path)
            or _is_link_like(member_path)
            or checkpoint.directory != manifest_path.parent
            or checkpoint.directory != member_path.parent
            or resolved_directory.parent != checkpoint_root
            or resolved_manifest.parent != resolved_directory
            or resolved_member.parent != resolved_directory
            or not checkpoint.directory.is_dir()
            or {path.name for path in checkpoint.directory.iterdir()}
            != {"manifest.json", "paired_day.parquet"}
        ):
            raise RuntimeError("annual agreement linked or outside checkpoint root")
        manifest_identity = _file_identity(manifest_path)
        member_identity = _file_identity(member_path)
        manifest = _read_json(manifest_path, label="annual agreement checkpoint manifest")
        if manifest != checkpoint.manifest:
            raise RuntimeError("annual agreement checkpoint evidence changed")
        try:
            observed_day = date.fromisoformat(str(manifest["date"]))
        except (KeyError, ValueError) as exc:
            raise RuntimeError("annual agreement checkpoint date changed") from exc
        if checkpoint.directory.name != observed_day.isoformat():
            raise RuntimeError("annual agreement checkpoint date changed")
        observed_dates.append(observed_day)
        if (
            type(manifest.get("schema_version")) is not int
            or manifest.get("schema_version") != 1
            or manifest.get("kind") != "annual_reference_station_agreement_day"
            or manifest.get("complete") is not True
            or manifest.get("config") != _checkpoint_config(config)
            or manifest.get("schema") != expected_schema
            or type(manifest.get("rows")) is not int
            or manifest.get("rows") != candidates.height
        ):
            raise RuntimeError("annual agreement checkpoint contract changed")
        normalized_inputs = _checkpoint_inputs(
            _mapping(manifest.get("inputs"), label="annual agreement checkpoint inputs"),
            config=config,
        )
        reviewed_day = reviewed_parsed.get(observed_day.isoformat())
        if reviewed_day is None:
            raise RuntimeError("annual agreement checkpoint does not match reviewed annual input")
        reviewed_files = reviewed_day.get("input_files")
        if not isinstance(reviewed_files, list) or len(reviewed_files) != len(_AGREEMENT_VARIABLES):
            raise RuntimeError("annual agreement reviewed source inventory changed")
        reviewed_sources: dict[str, dict[str, object]] = {}
        for value in reviewed_files:
            declared = _checkpoint_file_identity(value, label="reviewed annual source member")
            variable = Path(str(declared["path"])).stem
            if variable not in _AGREEMENT_VARIABLES or variable in reviewed_sources:
                raise RuntimeError("annual agreement reviewed source inventory changed")
            reviewed_sources[variable] = {
                "path": f"{variable}.parquet",
                "bytes": declared["bytes"],
                "sha256": declared["sha256"],
            }
        reviewed_ground = _checkpoint_file_identity(
            ground_files[observed_day.month - 1],
            label="reviewed annual ground member",
        )
        expected_ground = {
            "path": Path(str(reviewed_ground["path"])).name,
            "bytes": reviewed_ground["bytes"],
            "sha256": reviewed_ground["sha256"],
        }
        expected_annual = {
            "path": annual_input.device_days.path.name,
            "bytes": annual_input.device_days.bytes,
            "sha256": annual_input.device_days.sha256,
        }
        if (
            normalized_inputs["catalogue_state"] != "present"
            or normalized_inputs["candidate_identity_sha256"] != candidate_identity
            or normalized_inputs["catalog_generation_sha256"]
            != catalog_generations.get(observed_day.strftime("%Y%m"))
            or normalized_inputs["parsed_generation_sha256"]
            != parsed_rows.filter(pl.col("date") == observed_day)["parsed_generation_sha256"].item()
            or normalized_inputs["parsed_generation_sha256"]
            != reviewed_day.get("generation_sha256")
            or normalized_inputs["source_members"] != reviewed_sources
            or normalized_inputs["ground_member"] != expected_ground
            or normalized_inputs["annual_device_days"] != expected_annual
        ):
            raise RuntimeError("annual agreement checkpoint does not match reviewed annual input")
        expected_member = {
            "path": "paired_day.parquet",
            "bytes": member_identity["bytes"],
            "sha256": member_identity["sha256"],
        }
        if manifest.get("member") != expected_member:
            raise RuntimeError("annual agreement checkpoint member changed")
        summary = manifest.get("summary")
        if (
            not isinstance(summary, dict)
            or not all(
                isinstance(key, str) and type(value) is int and value >= 0
                for key, value in summary.items()
            )
            or sum(summary.values()) != candidates.height
            or manifest.get("summary_sha256") != _canonical_hash(summary)
        ):
            raise RuntimeError("annual agreement checkpoint summary changed")
        try:
            if pl.read_parquet_schema(member_path) != dict(AGREEMENT_DAY_SCHEMA):
                raise RuntimeError("annual agreement checkpoint schema changed")
        except (OSError, pl.exceptions.PolarsError) as exc:
            raise RuntimeError("annual agreement checkpoint member is unreadable") from exc
        paths.append(member_path)
        before.append({"manifest": manifest_identity, "member": member_identity})
        evidence.append(
            {
                "date": observed_day.isoformat(),
                "catalog_generation_sha256": normalized_inputs["catalog_generation_sha256"],
                "raw_observation_generation_sha256": normalized_inputs[
                    "raw_observation_generation_sha256"
                ],
                "parsed_generation_sha256": normalized_inputs["parsed_generation_sha256"],
                "inputs_sha256": _canonical_hash(normalized_inputs),
                "rows": candidates.height,
                "manifest_bytes": manifest_identity["bytes"],
                "manifest_sha256": manifest_identity["sha256"],
                "member_bytes": member_identity["bytes"],
                "member_sha256": member_identity["sha256"],
                "summary_sha256": manifest["summary_sha256"],
            }
        )
    if observed_dates != parsed_dates:
        raise RuntimeError("annual agreement ordered checkpoint inventory changed")
    return tuple(paths), evidence, tuple(before)


def _panel_select_sql() -> str:
    expressions: list[str] = [
        "0.5::DOUBLE AS radius_km",
        "calendar.calendar_state",
        "quarter(calendar.date)::BIGINT AS quarter",
        "calendar.date",
        "candidates.device_id",
    ]
    candidate_fields = {"station_name", "distance_km"}
    nullable_float_fields = {
        "lon_min",
        "lon_max",
        "lat_min",
        "lat_max",
        "micro_pm25_mean",
        "micro_humidity_mean",
        "micro_temperature_mean",
        "ground_pm25_mean",
    }
    for name, dtype in AGREEMENT_DAY_SCHEMA[2:]:
        if name in candidate_fields:
            expressions.append(f"candidates.{name}")
        elif dtype == pl.Int64:
            expressions.append(
                f"CASE WHEN calendar.calendar_state = 'catalogue_absent' "
                f"THEN 0::BIGINT ELSE checkpoint.{name} END AS {name}"
            )
        elif name in nullable_float_fields:
            expressions.append(
                f"CASE WHEN calendar.calendar_state = 'catalogue_absent' "
                f"THEN NULL::DOUBLE ELSE checkpoint.{name} END AS {name}"
            )
        elif name == "spatial_state":
            expressions.append(
                "CASE WHEN calendar.calendar_state = 'catalogue_absent' "
                "THEN 'missing_pm25_coordinate' ELSE checkpoint.spatial_state END "
                "AS spatial_state"
            )
        elif name == "reason":
            expressions.append(
                "CASE WHEN calendar.calendar_state = 'catalogue_absent' "
                "THEN 'catalogue_absent' ELSE checkpoint.reason END AS reason"
            )
        else:
            raise RuntimeError(f"annual agreement panel field is unhandled: {name}")
    return ",\n".join(expressions)


def _prepare_annual_agreement_panel(
    annual_input: AnnualReadinessInput,
    checkpoints: tuple[AgreementDayCheckpoint, ...],
    config: AnnualAgreementConfig,
) -> AnnualAgreementPanel:
    if config.threads != 1 or config.memory_limit_gb != 6:
        raise RuntimeError("annual agreement panel resource limits changed")
    calendar = _agreement_calendar(annual_input)
    primary = tuple(
        cohort
        for cohort in annual_input.candidate_cohorts
        if cohort.radius_km == config.primary_distance_km
    )
    if len(primary) != 1:
        raise RuntimeError("annual agreement reviewed primary cohort changed")
    candidates = _validate_agreement_candidates(primary[0].candidates)
    if (
        candidates.height != config.primary_devices
        or candidates["station_name"].n_unique() != config.primary_stations
        or candidates.filter(pl.col("distance_km") > config.primary_distance_km).height
    ):
        raise RuntimeError("annual agreement reviewed primary cohort changed")
    checkpoint_paths, checkpoint_evidence, checkpoint_before = _checkpoint_inventory_evidence(
        checkpoints,
        annual_input=annual_input,
        calendar=calendar,
        candidates=candidates,
        config=config,
    )
    path_list = ", ".join(f"'{_sql_path(path)}'" for path in checkpoint_paths)
    with tempfile.TemporaryDirectory(prefix="twair-agreement-panel-") as temporary:
        spill = Path(temporary) / "duckdb-spill"
        spill.mkdir()
        connection = duckdb.connect()
        try:
            connection.execute(f"SET threads={config.threads}")
            connection.execute(f"SET memory_limit='{config.memory_limit_gb}GB'")
            connection.execute(f"SET temp_directory='{_sql_path(spill)}'")
            connection.execute("SET preserve_insertion_order=false")
            connection.register("calendar_input", calendar.to_arrow())
            connection.register("candidate_input", candidates.to_arrow())
            connection.execute("CREATE TEMP TABLE calendar AS SELECT * FROM calendar_input")
            connection.execute("CREATE TEMP TABLE candidates AS SELECT * FROM candidate_input")
            connection.execute(
                f"CREATE TEMP VIEW checkpoint AS "
                f"SELECT * FROM read_parquet([{path_list}], filename=true)"
            )
            physical_order = connection.execute(
                """
                SELECT count(*) FROM (
                    SELECT filename, date, device_id,
                           lag(date) OVER (PARTITION BY filename) AS previous_date,
                           lag(device_id) OVER (PARTITION BY filename) AS previous_device_id
                    FROM checkpoint
                ) ordered
                WHERE previous_date > date
                   OR (previous_date = date AND previous_device_id >= device_id)
                """
            ).fetchone()
            if physical_order is None or int(physical_order[0]):
                raise RuntimeError("annual agreement physical checkpoint key order changed")
            eligible_null = connection.execute(
                """
                SELECT count(*) FROM checkpoint
                WHERE reason = 'eligible' AND (
                    micro_pm25_mean IS NULL OR micro_humidity_mean IS NULL
                    OR micro_temperature_mean IS NULL OR ground_pm25_mean IS NULL
                )
                """
            ).fetchone()
            if eligible_null is None or int(eligible_null[0]):
                raise RuntimeError("annual agreement checkpoint eligible value is null")
            ineligible_value = connection.execute(
                """
                SELECT count(*) FROM checkpoint
                WHERE reason != 'eligible' AND (
                    micro_pm25_mean IS NOT NULL OR micro_humidity_mean IS NOT NULL
                    OR micro_temperature_mean IS NOT NULL OR ground_pm25_mean IS NOT NULL
                )
                """
            ).fetchone()
            if ineligible_value is None or int(ineligible_value[0]):
                raise RuntimeError("annual agreement checkpoint ineligible value is not null")
            invalid_model_value = connection.execute(
                """
                SELECT count(*) FROM checkpoint
                WHERE (micro_pm25_mean IS NOT NULL AND NOT isfinite(micro_pm25_mean))
                   OR (micro_humidity_mean IS NOT NULL AND NOT isfinite(micro_humidity_mean))
                   OR (micro_temperature_mean IS NOT NULL
                       AND NOT isfinite(micro_temperature_mean))
                   OR (ground_pm25_mean IS NOT NULL AND NOT isfinite(ground_pm25_mean))
                """
            ).fetchone()
            if invalid_model_value is None or int(invalid_model_value[0]):
                raise RuntimeError("annual agreement checkpoint model value is non-finite")
            invalid = connection.execute(
                """
                SELECT count(*) FILTER (WHERE calendar.calendar_state = 'complete'
                           AND checkpoint.device_id IS NULL),
                       count(*) FILTER (WHERE calendar.calendar_state = 'catalogue_absent'
                           AND checkpoint.device_id IS NOT NULL),
                       count(*) FILTER (WHERE checkpoint.device_id IS NOT NULL
                           AND (checkpoint.station_name != candidates.station_name
                             OR checkpoint.distance_km != candidates.distance_km))
                FROM calendar CROSS JOIN candidates
                LEFT JOIN checkpoint
                  ON checkpoint.date = calendar.date
                 AND checkpoint.device_id = candidates.device_id
                """
            ).fetchone()
            if invalid is None or any(int(value) for value in invalid):
                raise RuntimeError("annual agreement checkpoint panel keys changed")
            checkpoint_count = connection.execute("SELECT count(*) FROM checkpoint").fetchone()
            if checkpoint_count is None or int(checkpoint_count[0]) != 322 * candidates.height:
                raise RuntimeError("annual agreement checkpoint panel row count changed")
            paired_days = pl.DataFrame(
                connection.execute(
                    f"""
                    SELECT {_panel_select_sql()}
                    FROM calendar CROSS JOIN candidates
                    LEFT JOIN checkpoint
                      ON checkpoint.date = calendar.date
                     AND checkpoint.device_id = candidates.device_id
                    ORDER BY radius_km, calendar.date, candidates.device_id
                    """
                ).to_arrow_table()
            ).cast(dict(AGREEMENT_PAIRED_DAY_SCHEMA), strict=True)
            connection.register("paired_days", paired_days.to_arrow())
            exclusions = pl.DataFrame(
                connection.execute(
                    """
                    SELECT radius_km, date, device_id, station_name, quarter, reason
                    FROM paired_days WHERE reason != 'eligible'
                    ORDER BY radius_km, date, device_id
                    """
                ).to_arrow_table()
            ).cast(dict(AGREEMENT_EXCLUSION_SCHEMA), strict=True)
            cohort_coverage = pl.DataFrame(
                connection.execute(
                    """
                    SELECT radius_km, station_name, quarter, reason,
                           count(*)::BIGINT AS device_days,
                           count(DISTINCT device_id)::BIGINT AS devices,
                           count(DISTINCT date)::BIGINT AS dates
                    FROM paired_days GROUP BY ALL
                    ORDER BY radius_km, station_name, quarter, reason
                    """
                ).to_arrow_table()
            ).cast(dict(AGREEMENT_COHORT_COVERAGE_SCHEMA), strict=True)
        finally:
            connection.close()
            shutil.rmtree(spill)
    checkpoint_after = tuple(
        {
            "manifest": _file_identity(checkpoint.manifest_path),
            "member": _file_identity(checkpoint.member_path),
        }
        for checkpoint in checkpoints
    )
    if checkpoint_before != checkpoint_after:
        raise RuntimeError("annual agreement checkpoint evidence changed during reduction")
    checkpoint_root = checkpoints[0].directory.parent.resolve(strict=True)
    for checkpoint in checkpoints:
        try:
            resolved_directory = checkpoint.directory.resolve(strict=True)
            resolved_manifest = checkpoint.manifest_path.resolve(strict=True)
            resolved_member = checkpoint.member_path.resolve(strict=True)
        except OSError as exc:
            raise RuntimeError("annual agreement checkpoint path changed during reduction") from exc
        if (
            _is_link_like(checkpoint.directory)
            or _is_link_like(checkpoint.manifest_path)
            or _is_link_like(checkpoint.member_path)
            or resolved_directory.parent != checkpoint_root
            or resolved_manifest.parent != resolved_directory
            or resolved_member.parent != resolved_directory
        ):
            raise RuntimeError("annual agreement linked or outside checkpoint root")
    output_rows = {
        "calendar": calendar.height,
        "paired_days": paired_days.height,
        "exclusions": exclusions.height,
        "cohort_coverage": cohort_coverage.height,
    }
    reason_counts = {
        str(reason): int(count)
        for reason, count in paired_days.group_by("reason").len().sort("reason").iter_rows()
    }
    summary: dict[str, object] = {
        "calendar": {"complete_dates": 322, "catalogue_absent_dates": 43},
        "primary": {
            "radius_km": config.primary_distance_km,
            "devices": candidates.height,
            "stations": candidates["station_name"].n_unique(),
        },
        "reason_counts": reason_counts,
        "output_rows": output_rows,
    }
    manifest: dict[str, object] = {
        "schema_version": 1,
        "analysis": "annual_reference_station_agreement_panel",
        "inputs": {
            "annual_generation_sha256": config.annual_generation_sha256,
            "candidate_identity_sha256": _canonical_hash(candidates.to_dicts()),
            "catalog_generations": _mapping(
                _mapping(
                    annual_input.manifest.get("inputs"),
                    label="reviewed annual agreement inputs",
                ).get("catalog_generations"),
                label="reviewed annual agreement catalog generations",
            ),
        },
        "checkpoint_inventory": checkpoint_evidence,
        "config": _checkpoint_config(config),
        "claim_boundary": dict(config.claim_boundary),
        "output_rows": output_rows,
        "summary_sha256": _canonical_hash(summary),
    }
    return AnnualAgreementPanel(
        calendar=calendar,
        paired_days=paired_days,
        exclusions=exclusions,
        cohort_coverage=cohort_coverage,
        summary=summary,
        manifest=manifest,
        annual_input=annual_input,
        checkpoints=checkpoints,
    )


def prepare_annual_agreement_panel(
    annual_input: AnnualReadinessInput,
    checkpoints: tuple[AgreementDayCheckpoint, ...],
    config: AnnualAgreementConfig,
) -> AnnualAgreementPanel:
    reviewed = load_annual_agreement_config()
    if config != reviewed:
        raise RuntimeError("annual agreement reviewed protocol changed")
    reloaded = load_annual_readiness_input(annual_input.generation_dir)
    if reloaded.generation_dir != annual_input.generation_dir:
        raise RuntimeError("annual agreement reviewed annual input changed")
    return _prepare_annual_agreement_panel(reloaded, checkpoints, reviewed)


def _validate_agreement_panel_frames(panel: AnnualAgreementPanel) -> None:
    frames = {name: getattr(panel, name) for name in _AGREEMENT_PANEL_SCHEMAS}
    for name, schema in _AGREEMENT_PANEL_SCHEMAS.items():
        if frames[name].schema != dict(schema):
            raise RuntimeError(f"annual agreement panel {name} schema changed")
    paired = panel.paired_days
    if (
        paired.height != 365 * 124
        or paired.select("radius_km", "date", "device_id").n_unique() != paired.height
        or paired["date"].n_unique() != 365
        or paired["device_id"].n_unique() != 124
        or paired["station_name"].n_unique() != 13
    ):
        raise RuntimeError("annual agreement panel row identities changed")
    order_contracts = {
        "calendar": (panel.calendar, ["date"]),
        "paired_days": (paired, ["radius_km", "date", "device_id"]),
        "exclusions": (
            panel.exclusions,
            ["radius_km", "date", "device_id"],
        ),
        "cohort_coverage": (
            panel.cohort_coverage,
            ["radius_km", "station_name", "quarter", "reason"],
        ),
    }
    for frame, keys in order_contracts.values():
        if frame.rows() != frame.sort(keys).rows():
            raise RuntimeError("annual agreement physical output key order changed")
    absent = paired.filter(pl.col("calendar_state") == "catalogue_absent")
    model_columns = (
        "micro_pm25_mean",
        "micro_humidity_mean",
        "micro_temperature_mean",
        "ground_pm25_mean",
    )
    if (
        absent.height != 43 * 124
        or absent.filter(pl.col("reason") != "catalogue_absent").height
        or absent.select(
            pl.any_horizontal(pl.col(column).is_not_null() for column in model_columns).any()
        ).item()
    ):
        raise RuntimeError("annual agreement catalogue absence changed")
    if paired.filter(
        (pl.col("reason") != "eligible")
        & pl.any_horizontal(pl.col(column).is_not_null() for column in model_columns)
    ).height:
        raise RuntimeError("annual agreement panel ineligible value is not null")
    if paired.filter(
        (pl.col("reason") == "eligible")
        & pl.any_horizontal(pl.col(column).is_null() for column in model_columns)
    ).height:
        raise RuntimeError("annual agreement panel eligible value is null")
    if paired.filter(
        pl.any_horizontal(
            pl.col(column).is_not_null() & ~pl.col(column).is_finite() for column in model_columns
        )
    ).height:
        raise RuntimeError("annual agreement panel model value is non-finite")
    expected_exclusions = paired.filter(pl.col("reason") != "eligible").select(
        *(name for name, _ in AGREEMENT_EXCLUSION_SCHEMA)
    )
    if panel.exclusions.rows() != expected_exclusions.rows():
        raise RuntimeError("annual agreement exclusions changed")
    rows = {name: frame.height for name, frame in frames.items()}
    if panel.manifest.get("output_rows") != rows or panel.summary.get("output_rows") != rows:
        raise RuntimeError("annual agreement panel output row counts changed")
    if panel.manifest.get("summary_sha256") != _canonical_hash(panel.summary):
        raise RuntimeError("annual agreement panel summary changed")
    if panel.manifest.get("claim_boundary") != dict(load_annual_agreement_config().claim_boundary):
        raise RuntimeError("annual agreement panel claim boundary changed")
    _validate_persisted_agreement_panel_semantics(panel)


def _validate_persisted_agreement_panel_semantics(panel: AnnualAgreementPanel) -> None:
    reviewed = load_annual_agreement_config()
    if panel.manifest.get("config") != _checkpoint_config(reviewed):
        raise RuntimeError("annual agreement persisted semantics changed")
    inputs = _mapping(panel.manifest.get("inputs"), label="annual agreement panel inputs")
    candidates = (
        panel.paired_days.select("device_id", "station_name", "distance_km")
        .unique()
        .sort("device_id")
    )
    catalog_generations = _mapping(
        inputs.get("catalog_generations"),
        label="annual agreement panel catalog generations",
    )
    reviewed_panel = load_annual_micro_sensor_panel_config()
    reviewed_catalogs = dict(reviewed_panel.catalog_generations)
    reviewed_parsed = {
        record.date.isoformat(): record.generation_sha256
        for record in reviewed_panel.parsed_generations
    }
    readiness = load_annual_readiness_input(
        annual_micro_sensor_readiness_dir(generation_sha256=reviewed.annual_generation_sha256)
    )
    primary_cohorts = tuple(
        cohort
        for cohort in readiness.candidate_cohorts
        if cohort.radius_km == reviewed.primary_distance_km
    )
    if len(primary_cohorts) != 1:
        raise RuntimeError("annual agreement persisted semantics changed")
    reviewed_candidates = _validate_agreement_candidates(primary_cohorts[0].candidates)
    persisted_cohort = (
        panel.paired_days.select("radius_km", "device_id", "station_name", "distance_km")
        .unique()
        .sort("device_id")
    )
    reviewed_cohort = reviewed_candidates.with_columns(
        pl.lit(reviewed.primary_distance_km).alias("radius_km")
    ).select("radius_km", "device_id", "station_name", "distance_km")
    expected_catalog_months = {f"2025{month:02d}" for month in range(1, 13)}
    if (
        set(catalog_generations) != expected_catalog_months
        or any(
            not isinstance(value, str) or _SHA256.fullmatch(value) is None
            for value in catalog_generations.values()
        )
        or catalog_generations != reviewed_catalogs
        or inputs.get("annual_generation_sha256") != reviewed.annual_generation_sha256
        or persisted_cohort.rows() != reviewed_cohort.rows()
        or candidates.rows() != reviewed_candidates.rows()
        or inputs.get("candidate_identity_sha256")
        != _canonical_hash(reviewed_candidates.to_dicts())
        or set(inputs)
        != {
            "annual_generation_sha256",
            "candidate_identity_sha256",
            "catalog_generations",
        }
    ):
        raise RuntimeError("annual agreement persisted semantics changed")
    expected_dates = [date(2025, 1, 1) + timedelta(days=index) for index in range(365)]
    if panel.calendar["date"].to_list() != expected_dates:
        raise RuntimeError("annual agreement persisted semantics changed")
    complete = panel.calendar.filter(pl.col("calendar_state") == "complete")
    absent = panel.calendar.filter(pl.col("calendar_state") == "catalogue_absent")
    if (
        complete.height != 322
        or absent.height != 43
        or complete["parsed_generation_sha256"].null_count()
        or absent["parsed_generation_sha256"].null_count() != absent.height
    ):
        raise RuntimeError("annual agreement persisted semantics changed")
    inventory = panel.manifest.get("checkpoint_inventory")
    evidence_fields = {
        "date",
        "catalog_generation_sha256",
        "raw_observation_generation_sha256",
        "parsed_generation_sha256",
        "inputs_sha256",
        "rows",
        "manifest_bytes",
        "manifest_sha256",
        "member_bytes",
        "member_sha256",
        "summary_sha256",
    }
    if not isinstance(inventory, list) or len(inventory) != complete.height:
        raise RuntimeError("annual agreement persisted semantics changed")
    for calendar_row, value in zip(complete.iter_rows(named=True), inventory, strict=True):
        evidence = _mapping(value, label="annual agreement panel checkpoint evidence")
        if set(evidence) != evidence_fields:
            raise RuntimeError("annual agreement persisted semantics changed")
        day_text = calendar_row["date"].isoformat()
        catalog_generation = evidence["catalog_generation_sha256"]
        raw_observation_generation = evidence["raw_observation_generation_sha256"]
        parsed_generation = evidence["parsed_generation_sha256"]
        if (
            evidence["date"] != day_text
            or catalog_generation != calendar_row["catalog_generation_sha256"]
            or catalog_generation != catalog_generations.get(day_text[:4] + day_text[5:7])
            or parsed_generation != calendar_row["parsed_generation_sha256"]
            or parsed_generation != reviewed_parsed.get(day_text)
            or not isinstance(catalog_generation, str)
            or _SHA256.fullmatch(catalog_generation) is None
            or not isinstance(raw_observation_generation, str)
            or _SHA256.fullmatch(raw_observation_generation) is None
            or not isinstance(parsed_generation, str)
            or _SHA256.fullmatch(parsed_generation) is None
            or any(
                not isinstance(evidence[name], str) or _SHA256.fullmatch(evidence[name]) is None
                for name in (
                    "inputs_sha256",
                    "manifest_sha256",
                    "member_sha256",
                    "summary_sha256",
                )
            )
            or any(
                type(evidence[name]) is not int or evidence[name] < 0
                for name in ("rows", "manifest_bytes", "member_bytes")
            )
        ):
            raise RuntimeError("annual agreement persisted semantics changed")
    for row in panel.calendar.iter_rows(named=True):
        month = row["date"].strftime("%Y%m")
        if row["catalog_generation_sha256"] != catalog_generations.get(month):
            raise RuntimeError("annual agreement persisted semantics changed")
    expected_coverage = (
        panel.paired_days.group_by("radius_km", "station_name", "quarter", "reason")
        .agg(
            pl.len().cast(pl.Int64).alias("device_days"),
            pl.col("device_id").n_unique().cast(pl.Int64).alias("devices"),
            pl.col("date").n_unique().cast(pl.Int64).alias("dates"),
        )
        .sort("radius_km", "station_name", "quarter", "reason")
        .cast(dict(AGREEMENT_COHORT_COVERAGE_SCHEMA), strict=True)
    )
    if panel.cohort_coverage.rows() != expected_coverage.rows():
        raise RuntimeError("annual agreement persisted semantics changed")
    output_rows = {name: getattr(panel, name).height for name in _AGREEMENT_PANEL_SCHEMAS}
    expected_summary: dict[str, object] = {
        "calendar": {"complete_dates": complete.height, "catalogue_absent_dates": absent.height},
        "primary": {
            "radius_km": reviewed.primary_distance_km,
            "devices": candidates.height,
            "stations": candidates["station_name"].n_unique(),
        },
        "reason_counts": {
            str(reason): int(count)
            for reason, count in panel.paired_days.group_by("reason")
            .len()
            .sort("reason")
            .iter_rows()
        },
        "output_rows": output_rows,
    }
    if panel.summary != expected_summary:
        raise RuntimeError("annual agreement persisted semantics changed")


@contextmanager
def _agreement_panel_lock(path: Path) -> Iterator[None]:
    with _agreement_checkpoint_lock(path):
        yield


def _recover_agreement_panel_swap(destination: Path) -> None:
    parent = destination.parent
    if not parent.exists():
        return
    stages = sorted(parent.glob(f".{destination.name}.staging-*"))
    backups = sorted(parent.glob(f".{destination.name}.backup-*"))
    if len(stages) > 1 or len(backups) > 1:
        raise RuntimeError("multiple interrupted annual agreement panel swaps")
    if destination.exists() and stages and backups:
        raise RuntimeError("ambiguous interrupted annual agreement panel swap")
    if not destination.exists() and backups:
        backups[0].replace(destination)
        backups = []
    for staged in stages:
        shutil.rmtree(staged)
    if destination.exists() and backups:
        shutil.rmtree(backups[0])


def _load_annual_agreement_panel_unlocked(directory: Path) -> AnnualAgreementPanel:
    directory = directory.absolute()
    expected_files = {
        *(f"{name}.parquet" for name in _AGREEMENT_PANEL_SCHEMAS),
        "summary.json",
        "manifest.json",
    }
    _validate_agreement_panel_inventory(
        directory,
        expected_files=expected_files,
        during_read=False,
    )
    manifest_path = directory / "manifest.json"
    summary_path = directory / "summary.json"
    manifest = _read_json(manifest_path, label="annual agreement panel manifest")
    if (
        set(manifest)
        != {
            *_AGREEMENT_PANEL_IDENTITY_FIELDS,
            "complete",
            "generation_sha256",
        }
        or type(manifest.get("schema_version")) is not int
        or manifest.get("schema_version") != 1
        or manifest.get("analysis") != "annual_reference_station_agreement_panel"
        or manifest.get("complete") is not True
    ):
        raise RuntimeError("annual agreement panel manifest contract changed")
    identity = {field: manifest[field] for field in _AGREEMENT_PANEL_IDENTITY_FIELDS}
    if manifest.get("generation_sha256") != _canonical_hash(identity):
        raise RuntimeError("annual agreement panel generation identity changed")
    members = _mapping(manifest.get("members"), label="annual agreement panel members")
    if set(members) != set(_AGREEMENT_PANEL_SCHEMAS):
        raise RuntimeError("annual agreement panel member fields changed")
    summary_declaration = _checkpoint_file_identity(
        manifest.get("summary_file"), label="annual agreement panel summary member"
    )
    observed_summary = _observed_checkpoint_file_identity(summary_path)
    if summary_declaration != observed_summary:
        raise RuntimeError("annual agreement panel summary member changed")
    before = {
        "manifest": _file_identity(manifest_path),
        "summary": _file_identity(summary_path),
    }
    summary = _read_json(summary_path, label="annual agreement panel summary")
    frames: dict[str, pl.DataFrame] = {}
    for name, schema in _AGREEMENT_PANEL_SCHEMAS.items():
        path = directory / f"{name}.parquet"
        declaration = _checkpoint_file_identity(
            members[name], label=f"annual agreement panel {name} member"
        )
        observed = _observed_checkpoint_file_identity(path)
        if declaration != observed:
            raise RuntimeError(f"annual agreement panel {name} member changed")
        try:
            if pl.read_parquet_schema(path) != dict(schema):
                raise RuntimeError(f"annual agreement panel {name} schema changed")
            frames[name] = pl.read_parquet(path)
        except (OSError, pl.exceptions.PolarsError) as exc:
            raise RuntimeError(f"annual agreement panel {name} member is unreadable") from exc
        before[name] = _file_identity(path)
    loaded = AnnualAgreementPanel(
        calendar=frames["calendar"],
        paired_days=frames["paired_days"],
        exclusions=frames["exclusions"],
        cohort_coverage=frames["cohort_coverage"],
        summary=summary,
        manifest=manifest,
        annual_input=None,
        checkpoints=None,
    )
    _validate_agreement_panel_frames(loaded)
    after = {
        "manifest": _file_identity(manifest_path),
        "summary": _file_identity(summary_path),
        **{
            name: _file_identity(directory / f"{name}.parquet") for name in _AGREEMENT_PANEL_SCHEMAS
        },
    }
    if before != after:
        raise RuntimeError("annual agreement panel changed during read")
    _validate_agreement_panel_inventory(
        directory,
        expected_files=expected_files,
        during_read=True,
    )
    return loaded


def load_annual_agreement_panel(directory: Path) -> AnnualAgreementPanel:
    with _agreement_panel_lock(directory.parent / f".{directory.name}.lock"):
        _recover_agreement_panel_swap(directory)
        return _load_annual_agreement_panel_unlocked(directory)


def _reprepare_agreement_panel_for_publication(
    panel: AnnualAgreementPanel,
) -> AnnualAgreementPanel:
    if panel.annual_input is None or panel.checkpoints is None:
        raise RuntimeError("annual agreement reviewed preparation changed")
    reviewed = load_annual_agreement_config()
    reloaded = load_annual_readiness_input(panel.annual_input.generation_dir)
    prepared = _prepare_annual_agreement_panel(reloaded, panel.checkpoints, reviewed)
    if (
        panel.calendar.rows() != prepared.calendar.rows()
        or panel.paired_days.rows() != prepared.paired_days.rows()
        or panel.exclusions.rows() != prepared.exclusions.rows()
        or panel.cohort_coverage.rows() != prepared.cohort_coverage.rows()
        or panel.summary != prepared.summary
        or panel.manifest != prepared.manifest
    ):
        raise RuntimeError("annual agreement reviewed preparation changed")
    return prepared


def write_annual_agreement_panel(
    panel: AnnualAgreementPanel,
    *,
    destination: Path,
) -> AnnualAgreementPanel:
    panel = _reprepare_agreement_panel_for_publication(panel)
    _validate_agreement_panel_frames(panel)
    destination.parent.mkdir(parents=True, exist_ok=True)
    lock_path = destination.parent / f".{destination.name}.lock"
    with _agreement_panel_lock(lock_path):
        _recover_agreement_panel_swap(destination)
        if destination.exists():
            raise RuntimeError(f"annual agreement panel output already exists: {destination}")
        token = uuid4().hex
        staged = destination.with_name(f".{destination.name}.staging-{token}")
        backup = destination.with_name(f".{destination.name}.backup-{token}")
        staged.mkdir()
        try:
            members: dict[str, dict[str, object]] = {}
            for name in _AGREEMENT_PANEL_SCHEMAS:
                path = staged / f"{name}.parquet"
                getattr(panel, name).write_parquet(path)
                members[name] = _observed_checkpoint_file_identity(path)
            paired_path = staged / "paired_days.parquet"
            with tempfile.TemporaryDirectory(
                prefix="twair-agreement-panel-copy-", dir=staged
            ) as temporary:
                rewritten = staged / ".paired_days.ordered.parquet"
                spill = Path(temporary) / "duckdb-spill"
                spill.mkdir()
                connection = duckdb.connect()
                try:
                    config = load_annual_agreement_config()
                    connection.execute(f"SET threads={config.threads}")
                    connection.execute(f"SET memory_limit='{config.memory_limit_gb}GB'")
                    connection.execute(f"SET temp_directory='{_sql_path(spill)}'")
                    connection.execute("SET preserve_insertion_order=false")
                    copied = connection.execute(
                        f"""
                        COPY (
                            SELECT * FROM read_parquet('{_sql_path(paired_path)}')
                            ORDER BY radius_km, date, device_id
                        ) TO '{_sql_path(rewritten)}' (FORMAT PARQUET)
                        """
                    ).fetchone()
                    if copied is None or int(copied[0]) != panel.paired_days.height:
                        raise RuntimeError("annual agreement paired-day COPY row count changed")
                finally:
                    connection.close()
                    shutil.rmtree(spill)
                rewritten.replace(paired_path)
            members["paired_days"] = _observed_checkpoint_file_identity(paired_path)
            _write_checkpoint_json(staged / "summary.json", panel.summary)
            summary_file = _observed_checkpoint_file_identity(staged / "summary.json")
            identity = {
                **panel.manifest,
                "members": members,
                "summary_file": summary_file,
            }
            final_identity = {field: identity[field] for field in _AGREEMENT_PANEL_IDENTITY_FIELDS}
            manifest = {
                **identity,
                "generation_sha256": _canonical_hash(final_identity),
                "complete": True,
            }
            _write_checkpoint_json(staged / "manifest.json", manifest)
            try:
                staged.replace(destination)
                return _load_annual_agreement_panel_unlocked(destination)
            except BaseException:
                if destination.exists():
                    shutil.rmtree(destination)
                if backup.exists():
                    backup.replace(destination)
                raise
        finally:
            if staged.exists():
                shutil.rmtree(staged)


def assign_agreement_folds(panel: AnnualAgreementPanel) -> pl.DataFrame:
    _validate_agreement_panel_frames(panel)
    config = load_annual_agreement_config()
    eligible = panel.paired_days.filter(pl.col("reason") == "eligible")
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
    }
    if required - set(eligible.columns) or eligible.is_empty():
        raise RuntimeError("annual agreement has no eligible split rows")
    if eligible.filter(~pl.col("quarter").is_in(config.quarters)).height:
        raise RuntimeError("annual agreement eligible quarter changed")
    if eligible.select("radius_km", "date", "device_id").n_unique() != eligible.height:
        raise RuntimeError("annual agreement eligible row identity changed")
    model_columns = (
        "micro_pm25_mean",
        "micro_humidity_mean",
        "micro_temperature_mean",
        "ground_pm25_mean",
    )
    if eligible.filter(
        pl.any_horizontal(
            pl.col(column).is_null() | ~pl.col(column).is_finite() for column in model_columns
        )
    ).height:
        raise RuntimeError("annual agreement eligible model value is non-finite")
    stations = panel.paired_days.select("station_name").unique().sort("station_name")
    geography = resolve_station_geo().select("station_name", "airzone_official")
    inventory = stations.join(geography, on="station_name", how="left")
    if inventory["airzone_official"].null_count():
        raise RuntimeError("annual agreement station geography changed")
    station_membership = assign_station_folds(inventory, fold_count=config.station_folds)
    source = eligible.join(
        station_membership.select("station_name", "station_fold"),
        on="station_name",
        how="left",
    )
    rows: list[pl.DataFrame] = []
    for station_fold in range(config.station_folds):
        rows.append(
            source.with_columns(
                pl.lit("held_station").alias("evaluation"),
                pl.lit(f"held_station_{station_fold:02d}").alias("fold"),
                pl.when(pl.col("station_fold") == station_fold)
                .then(pl.lit("test"))
                .otherwise(pl.lit("train"))
                .alias("role"),
            )
        )
    for quarter in config.quarters:
        rows.append(
            source.with_columns(
                pl.lit("held_quarter").alias("evaluation"),
                pl.lit(f"held_quarter_{quarter:02d}").alias("fold"),
                pl.when(pl.col("quarter") == quarter)
                .then(pl.lit("test"))
                .otherwise(pl.lit("train"))
                .alias("role"),
            )
        )
    for station_fold in range(config.station_folds):
        for quarter in config.quarters:
            rows.append(
                source.with_columns(
                    pl.lit("joint").alias("evaluation"),
                    pl.lit(f"joint_{station_fold:02d}_{quarter:02d}").alias("fold"),
                    pl.when(
                        (pl.col("station_fold") == station_fold) & (pl.col("quarter") == quarter)
                    )
                    .then(pl.lit("test"))
                    .when((pl.col("station_fold") != station_fold) & (pl.col("quarter") != quarter))
                    .then(pl.lit("train"))
                    .otherwise(pl.lit("excluded"))
                    .alias("role"),
                )
            )
    memberships = (
        pl.concat(rows)
        .select(
            "evaluation",
            "fold",
            "role",
            "station_fold",
            *eligible.columns,
        )
        .sort("evaluation", "fold", "role", "radius_km", "date", "device_id")
    )
    identity_columns = (
        "radius_km",
        "date",
        "device_id",
        "station_name",
        "station_fold",
        "quarter",
    )
    bound: list[pl.DataFrame] = []
    for split in memberships.partition_by("evaluation", "fold", maintain_order=True):
        train = split.filter(pl.col("role") == "train")
        test = split.filter(pl.col("role") == "test")
        if train.height < 2 or train["ground_pm25_mean"].n_unique() < 2:
            raise RuntimeError("annual agreement fold has insufficient training targets")
        if test.is_empty():
            state = "unscored_empty_test"
        elif test["ground_pm25_mean"].n_unique() < 2:
            state = "unscored_single_target"
        else:
            state = "scored"

        def digest(frame: pl.DataFrame, *, truth: bool = False) -> str:
            columns = (*identity_columns, "ground_pm25_mean") if truth else identity_columns
            records = (
                frame.select(*columns)
                .sort(*identity_columns)
                .with_columns(pl.col("date").cast(pl.String))
                .to_dicts()
            )
            return _canonical_hash(records)

        bound.append(
            split.with_columns(
                pl.lit(state).alias("fold_state"),
                pl.lit(train.height, dtype=pl.Int64).alias("train_rows"),
                pl.lit(test.height, dtype=pl.Int64).alias("test_rows"),
                pl.lit(digest(train)).alias("train_membership_sha256"),
                pl.lit(digest(test)).alias("test_membership_sha256"),
                pl.lit(digest(test, truth=True)).alias("test_truth_sha256"),
            )
        )
    return pl.concat(bound).sort("evaluation", "fold", "role", "radius_km", "date", "device_id")


def _agreement_fold_table(memberships: pl.DataFrame) -> pl.DataFrame:
    identities = (
        memberships.select(
            "evaluation",
            "fold",
            "fold_state",
            "train_rows",
            "test_rows",
            "train_membership_sha256",
            "test_membership_sha256",
            "test_truth_sha256",
        )
        .unique()
        .sort("evaluation", "fold")
    )
    if identities.height != 29:
        raise RuntimeError("annual agreement fold inventory changed")
    rows: list[dict[str, object]] = []
    for identity in identities.iter_rows(named=True):
        split = memberships.filter(pl.col("fold") == identity["fold"])
        train = split.filter(pl.col("role") == "train")
        test = split.filter(pl.col("role") == "test")
        train_devices = set(train["device_id"])
        test_devices = set(test["device_id"])
        rows.append(
            {
                **identity,
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
    return pl.DataFrame(rows).sort("evaluation", "fold")


def _agreement_prediction_rows(
    memberships: pl.DataFrame,
    *,
    config: AnnualAgreementConfig,
) -> pl.DataFrame:
    models = {
        "pooled_micro_ridge": ("micro_pm25_mean",),
        "pooled_weather_ridge": (
            "micro_pm25_mean",
            "micro_humidity_mean",
            "micro_temperature_mean",
        ),
    }
    rows: list[pl.DataFrame] = []
    common = (
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
    for split in memberships.partition_by("evaluation", "fold", maintain_order=True):
        train = split.filter(pl.col("role") == "train")
        test = split.filter(pl.col("role") == "test")
        if test.is_empty():
            continue
        rows.append(
            test.select(*common, pl.col("ground_pm25_mean").alias("y_true")).with_columns(
                pl.lit("raw_micro").alias("model"),
                pl.lit("micro_pm25_mean").alias("model_features"),
                test["micro_pm25_mean"].alias("y_pred"),
            )
        )
        weighted_train = train.with_columns(
            (1.0 / pl.len().over("station_name", "date")).alias("_sample_weight")
        )
        y_train = weighted_train["ground_pm25_mean"].to_numpy()
        weights = weighted_train["_sample_weight"].to_numpy()
        for model, features in models.items():
            x_train = weighted_train.select(*features).to_numpy()
            x_test = test.select(*features).to_numpy()
            if not (
                np.isfinite(x_train).all()
                and np.isfinite(x_test).all()
                and np.isfinite(y_train).all()
                and np.isfinite(weights).all()
                and (weights > 0).all()
            ):
                raise RuntimeError("annual agreement model input is non-finite")
            pipeline = Pipeline(
                [
                    ("standardscaler", StandardScaler()),
                    ("ridge", Ridge(alpha=config.ridge_alpha)),
                ]
            )
            with threadpool_limits(limits=config.threads):
                pipeline.fit(
                    x_train,
                    y_train,
                    standardscaler__sample_weight=weights,
                    ridge__sample_weight=weights,
                )
                predicted = pipeline.predict(x_test)
            if predicted.shape != (test.height,) or not np.isfinite(predicted).all():
                raise RuntimeError("annual agreement prediction is non-finite")
            rows.append(
                test.select(*common, pl.col("ground_pm25_mean").alias("y_true")).with_columns(
                    pl.lit(model).alias("model"),
                    pl.lit(",".join(features)).alias("model_features"),
                    pl.Series("y_pred", predicted, dtype=pl.Float64),
                )
            )
    if not rows:
        raise RuntimeError("annual agreement produced no test predictions")
    return pl.concat(rows).sort("evaluation", "fold", "radius_km", "date", "device_id", "model")


def _validate_agreement_predictions(
    predictions: pl.DataFrame,
    *,
    memberships: pl.DataFrame,
    folds: pl.DataFrame,
) -> None:
    required = {
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
        "model",
        "model_features",
        "y_true",
        "y_pred",
    }
    if set(predictions.columns) != required or predictions.is_empty():
        raise RuntimeError("annual agreement prediction schema changed")
    if _agreement_fold_table(memberships).rows() != folds.rows():
        raise RuntimeError("annual agreement trusted fold evidence changed")
    identity = (
        "evaluation",
        "fold",
        "radius_km",
        "date",
        "device_id",
        "station_name",
        "station_fold",
        "quarter",
    )
    if predictions.select(*identity, "model").n_unique() != predictions.height:
        raise RuntimeError("annual agreement predictions contain duplicate rows")
    expected_models = {"raw_micro", "pooled_micro_ridge", "pooled_weather_ridge"}
    expected_features = {
        "raw_micro": "micro_pm25_mean",
        "pooled_micro_ridge": "micro_pm25_mean",
        "pooled_weather_ridge": ("micro_pm25_mean,micro_humidity_mean,micro_temperature_mean"),
    }
    if set(predictions["model"].unique()) - expected_models:
        raise RuntimeError("annual agreement predictions differ from trusted prediction universe")
    if predictions.filter(
        pl.col("model_features")
        != pl.col("model").replace_strict(expected_features, return_dtype=pl.String)
    ).height:
        raise RuntimeError("annual agreement prediction features changed")
    membership_identity = (
        "radius_km",
        "date",
        "device_id",
        "station_name",
        "station_fold",
        "quarter",
    )
    trusted_prediction_rows: list[pl.DataFrame] = []
    for fold_row in folds.iter_rows(named=True):
        fold_name = str(fold_row["fold"])
        fold_memberships = memberships.filter(pl.col("fold") == fold_name)
        train = fold_memberships.filter(pl.col("role") == "train")
        test = fold_memberships.filter(pl.col("role") == "test")

        def digest(frame: pl.DataFrame, *, truth: bool = False) -> str:
            columns = (*membership_identity, "ground_pm25_mean") if truth else membership_identity
            return _canonical_hash(
                frame.select(*columns)
                .sort(*membership_identity)
                .with_columns(pl.col("date").cast(pl.String))
                .to_dicts()
            )

        if (
            digest(train) != fold_row["train_membership_sha256"]
            or digest(test) != fold_row["test_membership_sha256"]
            or digest(test, truth=True) != fold_row["test_truth_sha256"]
            or train.height != fold_row["train_rows"]
            or test.height != fold_row["test_rows"]
        ):
            raise RuntimeError("annual agreement trusted fold evidence changed")
        expected = test.select(
            "evaluation",
            "fold",
            *membership_identity,
            pl.col("ground_pm25_mean").alias("y_true"),
        ).sort(*identity)
        observed = predictions.filter(pl.col("fold") == fold_name)
        if test.is_empty():
            if not observed.is_empty():
                raise RuntimeError("annual agreement predictions differ from trusted fold")
            continue
        trusted_prediction_rows.append(
            expected.with_columns(
                pl.lit(fold_row["fold_state"]).alias("fold_state"),
                pl.lit(fold_row["train_membership_sha256"]).alias("train_membership_sha256"),
                pl.lit(fold_row["test_membership_sha256"]).alias("test_membership_sha256"),
                pl.lit(fold_row["test_truth_sha256"]).alias("test_truth_sha256"),
            )
            .join(
                pl.DataFrame(
                    {
                        "model": list(expected_features),
                        "model_features": list(expected_features.values()),
                    }
                ),
                how="cross",
            )
            .select(
                *identity,
                "fold_state",
                "train_membership_sha256",
                "test_membership_sha256",
                "test_truth_sha256",
                "model",
                "model_features",
                "y_true",
            )
        )
        if set(observed["model"].unique()) != expected_models:
            raise RuntimeError("annual agreement prediction models changed")
        for model in expected_models:
            model_rows = observed.filter(pl.col("model") == model).sort(*identity)
            if (
                model_rows.select(*identity, "y_true").rows() != expected.rows()
                or model_rows["fold_state"].unique().to_list() != [fold_row["fold_state"]]
                or model_rows["train_membership_sha256"].unique().to_list()
                != [fold_row["train_membership_sha256"]]
                or model_rows["test_membership_sha256"].unique().to_list()
                != [fold_row["test_membership_sha256"]]
                or model_rows["test_truth_sha256"].unique().to_list()
                != [fold_row["test_truth_sha256"]]
            ):
                raise RuntimeError("annual agreement predictions differ from trusted fold")
    trusted_universe = pl.concat(trusted_prediction_rows).sort(*identity, "model")
    observed_universe = predictions.select(trusted_universe.columns).sort(*identity, "model")
    if observed_universe.rows() != trusted_universe.rows():
        raise RuntimeError("annual agreement predictions differ from trusted prediction universe")
    if predictions.filter(
        pl.col("y_true").is_null()
        | ~pl.col("y_true").is_finite()
        | pl.col("y_pred").is_null()
        | ~pl.col("y_pred").is_finite()
    ).height:
        raise RuntimeError("annual agreement prediction is non-finite")


def _agreement_scoring_frame(frame: pl.DataFrame, *, unit: str) -> pl.DataFrame:
    if unit == "device_day":
        return frame
    if unit != "station_day":
        raise RuntimeError("annual agreement evaluation unit changed")
    truth_counts = frame.group_by("station_name", "date").agg(
        pl.col("y_true").n_unique().alias("truths")
    )
    if truth_counts.filter(pl.col("truths") != 1).height:
        raise RuntimeError("annual agreement station-day truth changed within a target")
    return (
        frame.group_by("station_name", "date")
        .agg(pl.col("y_true").first(), pl.col("y_pred").mean())
        .sort("station_name", "date")
    )


def _agreement_metric_values(frame: pl.DataFrame) -> dict[str, float]:
    truth = frame["y_true"].to_numpy()
    predicted = frame["y_pred"].to_numpy()
    if truth.size < 2 or np.unique(truth).size < 2:
        raise RuntimeError("annual agreement score requires multi-level targets")
    error = predicted - truth
    denominator = float(np.sum((truth - np.mean(truth)) ** 2))
    if denominator <= 0.0:
        raise RuntimeError("annual agreement score has a single-level target")
    bias = float(np.mean(error))
    values = {
        "rmse": float(np.sqrt(np.mean(error**2))),
        "mae": float(np.mean(np.abs(error))),
        "r2": float(1.0 - np.sum(error**2) / denominator),
        "bias": bias,
        "absolute_bias": abs(bias),
    }
    if not all(math.isfinite(value) for value in values.values()):
        raise RuntimeError("annual agreement metric is non-finite")
    return values


def _agreement_score_row(
    frame: pl.DataFrame,
    *,
    scope: str,
    evaluation: str,
    fold: str | None,
    radius_km: float,
    model: str,
    unit: str,
    state: str,
    membership_sha256: str,
    truth_sha256: str,
) -> dict[str, object]:
    scored = _agreement_scoring_frame(frame, unit=unit)
    row: dict[str, object] = {
        "scope": scope,
        "evaluation": evaluation,
        "fold": fold,
        "radius_km": radius_km,
        "model": model,
        "unit": unit,
        "state": state,
        "n": scored.height,
        "membership_sha256": membership_sha256,
        "truth_sha256": truth_sha256,
    }
    if state == "scored":
        row.update(_agreement_metric_values(scored))
    else:
        row.update(
            {
                "rmse": None,
                "mae": None,
                "r2": None,
                "bias": None,
                "absolute_bias": None,
            }
        )
    return row


def _score_annual_agreement_predictions(
    predictions: pl.DataFrame,
    *,
    memberships: pl.DataFrame,
    folds: pl.DataFrame,
) -> tuple[pl.DataFrame, pl.DataFrame]:
    _validate_agreement_predictions(predictions, memberships=memberships, folds=folds)
    score_rows: list[dict[str, object]] = []
    for fold_row in folds.iter_rows(named=True):
        for model in ("raw_micro", "pooled_micro_ridge", "pooled_weather_ridge"):
            split = predictions.filter(
                (pl.col("fold") == fold_row["fold"]) & (pl.col("model") == model)
            )
            for unit in ("device_day", "station_day"):
                score_rows.append(
                    _agreement_score_row(
                        split,
                        scope="fold",
                        evaluation=str(fold_row["evaluation"]),
                        fold=str(fold_row["fold"]),
                        radius_km=float(
                            memberships.filter(pl.col("fold") == fold_row["fold"])["radius_km"]
                            .unique()
                            .item()
                        ),
                        model=model,
                        unit=unit,
                        state=str(fold_row["fold_state"]),
                        membership_sha256=str(fold_row["test_membership_sha256"]),
                        truth_sha256=str(fold_row["test_truth_sha256"]),
                    )
                )
    identity = (
        "radius_km",
        "date",
        "device_id",
        "station_name",
        "station_fold",
        "quarter",
    )
    for split in predictions.partition_by("evaluation", "radius_km", "model", maintain_order=True):
        first = split.row(0, named=True)
        membership_records = (
            split.select(*identity)
            .unique()
            .sort(*identity)
            .with_columns(pl.col("date").cast(pl.String))
            .to_dicts()
        )
        truth_records = (
            split.select(*identity, "y_true")
            .unique()
            .sort(*identity)
            .with_columns(pl.col("date").cast(pl.String))
            .to_dicts()
        )
        for unit in ("device_day", "station_day"):
            score_rows.append(
                _agreement_score_row(
                    split,
                    scope="overall",
                    evaluation=str(first["evaluation"]),
                    fold=None,
                    radius_km=float(first["radius_km"]),
                    model=str(first["model"]),
                    unit=unit,
                    state="scored",
                    membership_sha256=_canonical_hash(membership_records),
                    truth_sha256=_canonical_hash(truth_records),
                )
            )
    scores = pl.DataFrame(score_rows).sort(
        "scope", "evaluation", "fold", "radius_km", "model", "unit", nulls_last=True
    )
    delta_rows: list[dict[str, object]] = []
    keys = ("scope", "evaluation", "fold", "radius_km", "unit")
    for group in scores.partition_by(*keys, maintain_order=True):
        baseline = group.filter(pl.col("model") == "raw_micro")
        if baseline.height != 1:
            raise RuntimeError("annual agreement baseline score is missing or duplicated")
        raw = baseline.row(0, named=True)
        for model in ("pooled_micro_ridge", "pooled_weather_ridge"):
            adjusted = group.filter(pl.col("model") == model)
            if adjusted.height != 1:
                raise RuntimeError("annual agreement adjusted score is missing or duplicated")
            candidate = adjusted.row(0, named=True)
            if any(
                candidate[field] != raw[field]
                for field in ("state", "n", "membership_sha256", "truth_sha256")
            ):
                raise RuntimeError("annual agreement scores are not paired to the same test truth")
            scored = raw["state"] == "scored"
            row: dict[str, object] = {
                **{key: raw[key] for key in keys},
                "model": model,
                "baseline_model": "raw_micro",
                "state": raw["state"],
                "n": raw["n"],
                "membership_sha256": raw["membership_sha256"],
                "truth_sha256": raw["truth_sha256"],
            }
            for metric in ("rmse", "mae", "r2", "bias", "absolute_bias"):
                row[f"delta_{metric}"] = (
                    float(candidate[metric]) - float(raw[metric]) if scored else None
                )
            row.update(
                {
                    "improved_rmse": candidate["rmse"] < raw["rmse"] if scored else None,
                    "improved_mae": candidate["mae"] < raw["mae"] if scored else None,
                    "improved_r2": candidate["r2"] > raw["r2"] if scored else None,
                    "improved_absolute_bias": candidate["absolute_bias"] < raw["absolute_bias"]
                    if scored
                    else None,
                }
            )
            delta_rows.append(row)
    deltas = pl.DataFrame(delta_rows).sort(
        "scope", "evaluation", "fold", "radius_km", "model", "unit", nulls_last=True
    )
    return scores, deltas


def score_annual_agreement_predictions(
    panel: AnnualAgreementPanel,
    predictions: pl.DataFrame,
) -> tuple[pl.DataFrame, pl.DataFrame]:
    memberships = assign_agreement_folds(panel)
    folds = _agreement_fold_table(memberships)
    return _score_annual_agreement_predictions(
        predictions,
        memberships=memberships,
        folds=folds,
    )


def _agreement_frame_hash(frame: pl.DataFrame) -> str:
    temporal = [
        name
        for name, dtype in frame.schema.items()
        if dtype == pl.Date or isinstance(dtype, pl.Datetime)
    ]
    normalized = (
        frame.with_columns(pl.col(name).cast(pl.String) for name in temporal) if temporal else frame
    )
    return _canonical_hash(normalized.to_dicts())


def evaluate_annual_agreement(panel: AnnualAgreementPanel) -> AnnualAgreementEvaluation:
    config = load_annual_agreement_config()
    memberships = assign_agreement_folds(panel)
    folds = _agreement_fold_table(memberships)
    predictions = _agreement_prediction_rows(memberships, config=config)
    scores, deltas = _score_annual_agreement_predictions(
        predictions,
        memberships=memberships,
        folds=folds,
    )
    panel_generation = panel.manifest.get("generation_sha256")
    if not isinstance(panel_generation, str) or _SHA256.fullmatch(panel_generation) is None:
        raise RuntimeError("annual agreement panel generation identity changed")
    outputs = {
        "folds": folds,
        "predictions": predictions,
        "scores": scores,
        "deltas": deltas,
    }
    identity: dict[str, object] = {
        "schema_version": 1,
        "analysis": "annual_reference_station_agreement_benchmark",
        "panel_generation_sha256": panel_generation,
        "config": _checkpoint_config(config),
        "claim_boundary": dict(config.claim_boundary),
        "output_rows": {name: frame.height for name, frame in outputs.items()},
        "output_hashes": {name: _agreement_frame_hash(frame) for name, frame in outputs.items()},
    }
    manifest = {**identity, "generation_sha256": _canonical_hash(identity)}
    return AnnualAgreementEvaluation(
        memberships=memberships,
        folds=folds,
        predictions=predictions,
        scores=scores,
        deltas=deltas,
        manifest=manifest,
    )


def annual_agreement_run_plan() -> AnnualAgreementRunPlan:
    config = load_annual_agreement_config()
    root = configured_data_root()
    run_identity = _canonical_hash(_checkpoint_config(config))
    run_root = root / "interim" / "micro_sensor_annual_agreement" / run_identity
    return AnnualAgreementRunPlan(
        annual_generation_sha256=config.annual_generation_sha256,
        annual_generation_dir=annual_micro_sensor_readiness_dir(
            generation_sha256=config.annual_generation_sha256
        ),
        checkpoint_root=run_root / "days",
        panel_destination=run_root / "panel",
        output_root=root / "outputs" / "micro_sensor_annual_agreement" / "generations",
        lock_path=run_root / ".run.lock",
        threads=config.threads,
        memory_limit_gb=config.memory_limit_gb,
    )


@contextmanager
def _annual_agreement_run_lock(path: Path) -> Iterator[None]:
    try:
        with _agreement_checkpoint_lock(path):
            yield
    except RuntimeError as exc:
        if str(exc) == "another annual agreement checkpoint writer is active":
            raise RuntimeError("another annual agreement run is active") from None
        raise


def _assert_reviewed_direct_child(
    path: Path,
    *,
    parent: Path,
    is_directory: bool,
) -> None:
    path = path.absolute()
    parent = parent.absolute()
    try:
        resolved_parent = parent.resolve(strict=True)
        resolved_path = path.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError("annual agreement reviewed source is unreadable") from exc
    if (
        _is_link_like(parent)
        or _is_link_like(path)
        or resolved_parent != parent
        or resolved_path != path
        or resolved_path.parent != resolved_parent
        or (is_directory and not path.is_dir())
        or (not is_directory and not path.is_file())
    ):
        raise RuntimeError("annual agreement reviewed source is linked or outside")


def _reviewed_source_containment(
    data_root: Path,
    *,
    parsed_generation_sha256: str,
    reviewed_year: int,
    month: int,
) -> tuple[tuple[Path, Path, bool], ...]:
    root = data_root.absolute()
    try:
        if _is_link_like(root) or root.resolve(strict=True) != root or not root.is_dir():
            raise RuntimeError("annual agreement reviewed source is linked or outside")
    except OSError as exc:
        raise RuntimeError("annual agreement reviewed source is unreadable") from exc
    paths: list[tuple[Path, Path, bool]] = []

    def add_chain(parts: tuple[str, ...]) -> Path:
        parent = root
        for part in parts:
            child = parent / part
            paths.append((child, parent, True))
            parent = child
        return parent

    parsed_directory = add_chain(
        (
            "interim",
            "micro_sensors",
            "observations",
            "generations",
            parsed_generation_sha256,
        )
    )
    paths.extend(
        (parsed_directory / name, parsed_directory, False)
        for name in ("manifest.json", *(f"{variable}.parquet" for variable in _AGREEMENT_VARIABLES))
    )
    ground_directory = add_chain(
        (
            "processed",
            "observations",
            f"year={reviewed_year}",
            f"month={month:02d}",
        )
    )
    paths.append((ground_directory / "part-0.parquet", ground_directory, False))
    return tuple(paths)


def _portable_reviewed_identity(path: Path, *, data_root: Path) -> dict[str, object]:
    try:
        relative = path.relative_to(data_root).as_posix()
    except ValueError as exc:
        raise RuntimeError("annual agreement reviewed source is linked or outside") from exc
    return {"path": relative, **_file_identity(path)}


def _assert_reviewed_parsed_generation(
    directory: Path,
    *,
    expected_manifest: dict[str, Any],
) -> dict[str, object]:
    expected_files = {
        "manifest.json",
        *(f"{variable}.parquet" for variable in _AGREEMENT_VARIABLES),
    }
    try:
        observed_files = {entry.name for entry in directory.iterdir()}
    except OSError as exc:
        raise RuntimeError("annual agreement reviewed parsed generation changed") from exc
    manifest_path = directory / "manifest.json"
    try:
        before = _file_identity(manifest_path)
        observed_manifest = _read_json(
            manifest_path,
            label="annual agreement reviewed parsed manifest",
        )
        after = _file_identity(manifest_path)
    except RuntimeError as exc:
        raise RuntimeError("annual agreement reviewed parsed generation changed") from exc
    if (
        observed_files != expected_files
        or observed_manifest != expected_manifest
        or before != after
    ):
        raise RuntimeError("annual agreement reviewed parsed generation changed")
    return before


def _load_reviewed_agreement_day_sources(
    annual_input: AnnualReadinessInput,
    *,
    day: date,
    parsed_generation_sha256: str,
    data_root: Path,
    reviewed_year: int,
) -> ReviewedAgreementDaySources:
    data_root = data_root.absolute()
    parsed_root = data_root / "interim" / "micro_sensors" / "observations" / "generations"
    loaded = load_micro_sensor_observation_generation(
        parsed_generation_sha256,
        interim_observation_root=parsed_root,
    )
    expected_directory = parsed_root / parsed_generation_sha256
    if (
        loaded.generation_sha256 != parsed_generation_sha256
        or loaded.directory.absolute() != expected_directory
        or loaded.manifest.get("date") != day.isoformat()
    ):
        raise RuntimeError("annual agreement reviewed source is linked or outside")
    raw_observation_generation_sha256 = loaded.manifest.get("raw_observation_generation_sha256")
    if (
        not isinstance(raw_observation_generation_sha256, str)
        or _SHA256.fullmatch(raw_observation_generation_sha256) is None
    ):
        raise RuntimeError("annual agreement reviewed raw-observation generation changed")
    manifest_identity = _assert_reviewed_parsed_generation(
        expected_directory,
        expected_manifest=loaded.manifest,
    )
    containment = _reviewed_source_containment(
        data_root,
        parsed_generation_sha256=parsed_generation_sha256,
        reviewed_year=reviewed_year,
        month=day.month,
    )
    for path, parent, is_directory in containment:
        _assert_reviewed_direct_child(path, parent=parent, is_directory=is_directory)
    micro_paths = tuple(
        (variable, expected_directory / f"{variable}.parquet") for variable in _AGREEMENT_VARIABLES
    )
    ground_path = (
        data_root
        / "processed"
        / "observations"
        / f"year={reviewed_year}"
        / f"month={day.month:02d}"
        / "part-0.parquet"
    )
    inputs = _mapping(annual_input.manifest.get("inputs"), label="annual readiness inputs")
    parsed_evidence = inputs.get("parsed_generations")
    if not isinstance(parsed_evidence, list):
        raise RuntimeError("annual agreement reviewed parsed inventory changed")
    matches = [
        value
        for value in parsed_evidence
        if isinstance(value, dict)
        and value.get("date") == day.isoformat()
        and value.get("generation_sha256") == parsed_generation_sha256
    ]
    if len(matches) != 1:
        raise RuntimeError("annual agreement reviewed parsed inventory changed")
    input_files = matches[0].get("input_files")
    observed_micro = [
        _portable_reviewed_identity(path, data_root=data_root) for _, path in micro_paths
    ]
    members = _mapping(
        loaded.manifest.get("members"),
        label="annual agreement reviewed parsed members",
    )
    if (
        not isinstance(input_files, list)
        or len(input_files) != len(micro_paths)
        or not all(isinstance(value, dict) for value in input_files)
        or input_files != observed_micro
        or any(
            _mapping(
                members.get(path.name),
                label=f"annual agreement reviewed {variable} parsed member",
            ).get("bytes")
            != observed["bytes"]
            or _mapping(
                members.get(path.name),
                label=f"annual agreement reviewed {variable} parsed member",
            ).get("sha256")
            != observed["sha256"]
            for (variable, path), observed in zip(micro_paths, observed_micro, strict=True)
        )
    ):
        raise RuntimeError("annual agreement reviewed parsed evidence changed")
    ground_evidence = inputs.get("ground_files")
    observed_ground = _portable_reviewed_identity(ground_path, data_root=data_root)
    if not isinstance(ground_evidence, list) or ground_evidence.count(observed_ground) != 1:
        raise RuntimeError("annual agreement reviewed ground evidence changed")
    trusted_file_identities = (
        (expected_directory / "manifest.json", manifest_identity),
        *(
            (
                path,
                {"bytes": evidence["bytes"], "sha256": evidence["sha256"]},
            )
            for (_, path), evidence in zip(micro_paths, observed_micro, strict=True)
        ),
        (
            ground_path,
            {"bytes": observed_ground["bytes"], "sha256": observed_ground["sha256"]},
        ),
    )
    return ReviewedAgreementDaySources(
        micro_paths=micro_paths,
        ground_path=ground_path,
        raw_observation_generation_sha256=raw_observation_generation_sha256,
        parsed_directory=expected_directory,
        parsed_manifest=json.loads(json.dumps(loaded.manifest, allow_nan=False)),
        containment=containment,
        file_identities=trusted_file_identities,
    )


def _annual_agreement_input_identities(
    *,
    day: date,
    catalog_generation_sha256: str,
    sources: ReviewedAgreementDaySources,
    annual_device_days: PinnedAnnualMember,
    candidates: pl.DataFrame,
) -> dict[str, object]:
    micro_paths = dict(sources.micro_paths)
    return {
        "catalog_generation_sha256": catalog_generation_sha256,
        "raw_observation_generation_sha256": sources.raw_observation_generation_sha256,
        "parsed_generation_sha256": sources.parsed_directory.name,
        "source_members": {
            variable: _observed_checkpoint_file_identity(micro_paths[variable])
            for variable in _AGREEMENT_VARIABLES
        },
        "ground_member": _observed_checkpoint_file_identity(sources.ground_path),
        "annual_generation_sha256": _ANNUAL_GENERATION_SHA256,
        "annual_device_days": {
            "path": annual_device_days.path.name,
            "bytes": annual_device_days.bytes,
            "sha256": annual_device_days.sha256,
        },
        "candidate_identity_sha256": _canonical_hash(candidates.to_dicts()),
        "catalogue_state": "present",
        "annual_selected_date_rows": _annual_selected_date_rows(annual_device_days.path, day=day),
    }


def _validate_reviewed_agreement_source_inventory(
    annual_input: AnnualReadinessInput,
    *,
    reviewed_panel: Any,
) -> None:
    inputs = _mapping(annual_input.manifest.get("inputs"), label="annual readiness inputs")
    parsed = inputs.get("parsed_generations")
    expected_parsed = [
        (record.date.isoformat(), record.generation_sha256)
        for record in reviewed_panel.parsed_generations
    ]
    observed_parsed = (
        [
            (value.get("date"), value.get("generation_sha256"))
            for value in parsed
            if isinstance(value, dict)
        ]
        if isinstance(parsed, list)
        else []
    )
    ground = inputs.get("ground_files")
    expected_ground_paths = [
        f"processed/observations/year={reviewed_panel.year}/month={month:02d}/part-0.parquet"
        for month in range(1, 13)
    ]
    observed_ground_paths = (
        [value.get("path") for value in ground if isinstance(value, dict)]
        if isinstance(ground, list)
        else []
    )
    if observed_parsed != expected_parsed or observed_ground_paths != expected_ground_paths:
        raise RuntimeError("annual agreement reviewed source inventory changed")


def _prepare_annual_agreement_checkpoints(
    annual_input: AnnualReadinessInput,
    *,
    plan: AnnualAgreementRunPlan,
    config: AnnualAgreementConfig,
) -> tuple[AgreementDayCheckpoint, ...]:
    reviewed_panel = load_annual_micro_sensor_panel_config()
    primary = tuple(
        cohort
        for cohort in annual_input.candidate_cohorts
        if cohort.radius_km == config.primary_distance_km
    )
    if len(primary) != 1:
        raise RuntimeError("annual agreement reviewed primary cohort changed")
    candidates = _validate_agreement_candidates(primary[0].candidates)
    _validate_reviewed_agreement_source_inventory(
        annual_input,
        reviewed_panel=reviewed_panel,
    )
    catalogs = dict(reviewed_panel.catalog_generations)
    root = configured_data_root()
    checkpoints: list[AgreementDayCheckpoint] = []
    for record in reviewed_panel.parsed_generations:
        day = record.date
        catalog_generation = catalogs[day.strftime("%Y%m")]
        parsed_generation = record.generation_sha256
        sources = _load_reviewed_agreement_day_sources(
            annual_input,
            day=day,
            parsed_generation_sha256=parsed_generation,
            data_root=root,
            reviewed_year=reviewed_panel.year,
        )
        micro_paths = dict(sources.micro_paths)
        ground_path = sources.ground_path
        identities = _annual_agreement_input_identities(
            day=day,
            catalog_generation_sha256=catalog_generation,
            sources=sources,
            annual_device_days=annual_input.device_days,
            candidates=candidates,
        )
        sources.assert_unchanged()
        try:
            checkpoint = _load_agreement_day_checkpoint(
                day=day,
                input_identities=identities,
                config=config,
                checkpoint_root=plan.checkpoint_root,
            )
        except FileNotFoundError:
            aggregated = aggregate_agreement_day(
                day=day,
                micro_paths=micro_paths,
                annual_device_days=annual_input.device_days,
                ground_path=ground_path,
                candidates=candidates,
                input_identities=identities,
                config=config,
            )
            sources.assert_unchanged()
            checkpoint = write_agreement_day_checkpoint(
                aggregated,
                day=day,
                checkpoint_root=plan.checkpoint_root,
            )
        sources.assert_unchanged()
        checkpoints.append(checkpoint)
    return tuple(checkpoints)


def _final_frames(
    panel: AnnualAgreementPanel,
    evaluation: AnnualAgreementEvaluation,
) -> dict[str, pl.DataFrame]:
    return {
        "calendar": panel.calendar,
        "paired_days": panel.paired_days,
        "exclusions": panel.exclusions,
        "fold_membership": evaluation.memberships,
        "folds": evaluation.folds,
        "predictions": evaluation.predictions,
        "scores": evaluation.scores,
        "deltas": evaluation.deltas,
    }


def _load_or_write_annual_agreement_panel(
    panel: AnnualAgreementPanel,
    *,
    destination: Path,
) -> AnnualAgreementPanel:
    if not destination.exists():
        return write_annual_agreement_panel(panel, destination=destination)
    loaded = load_annual_agreement_panel(destination)
    if (
        loaded.calendar.rows() != panel.calendar.rows()
        or loaded.paired_days.rows() != panel.paired_days.rows()
        or loaded.exclusions.rows() != panel.exclusions.rows()
        or loaded.cohort_coverage.rows() != panel.cohort_coverage.rows()
        or loaded.summary != panel.summary
        or any(loaded.manifest.get(key) != value for key, value in panel.manifest.items())
    ):
        raise RuntimeError("annual agreement staged panel changed")
    return loaded


def _final_written(directory: Path) -> dict[str, Path]:
    return {
        **{name: directory / f"{name}.parquet" for name in _FINAL_MEMBER_NAMES},
        "summary": directory / "summary.json",
        "manifest": directory / "manifest.json",
    }


def _validate_final_inventory(directory: Path, *, during_read: bool) -> None:
    _validate_agreement_panel_inventory(
        directory,
        expected_files={
            *(f"{name}.parquet" for name in _FINAL_MEMBER_NAMES),
            "summary.json",
            "manifest.json",
        },
        during_read=during_read,
    )


def _validate_loaded_annual_agreement_result(result: AnnualAgreementResult) -> None:
    panel_manifest = _mapping(
        result.manifest.get("panel_manifest"), label="annual agreement final panel manifest"
    )
    panel = AnnualAgreementPanel(
        calendar=result.calendar,
        paired_days=result.paired_days,
        exclusions=result.exclusions,
        cohort_coverage=(
            result.paired_days.group_by("radius_km", "station_name", "quarter", "reason")
            .agg(
                pl.len().cast(pl.Int64).alias("device_days"),
                pl.col("device_id").n_unique().cast(pl.Int64).alias("devices"),
                pl.col("date").n_unique().cast(pl.Int64).alias("dates"),
            )
            .sort("radius_km", "station_name", "quarter", "reason")
            .cast(dict(AGREEMENT_COHORT_COVERAGE_SCHEMA), strict=True)
        ),
        summary=_mapping(result.summary.get("panel"), label="annual agreement final panel summary"),
        manifest=panel_manifest,
        annual_input=None,
        checkpoints=None,
    )
    _validate_agreement_panel_frames(panel)
    recomputed = evaluate_annual_agreement(panel)
    persisted = {
        "fold_membership": result.fold_membership,
        "folds": result.folds,
        "predictions": result.predictions,
        "scores": result.scores,
        "deltas": result.deltas,
    }
    expected = {
        "fold_membership": recomputed.memberships,
        "folds": recomputed.folds,
        "predictions": recomputed.predictions,
        "scores": recomputed.scores,
        "deltas": recomputed.deltas,
    }
    if result.manifest.get("evaluation_manifest") != recomputed.manifest or any(
        persisted[name].rows() != expected[name].rows() for name in persisted
    ):
        raise RuntimeError("annual agreement persisted model evidence changed")


def _validate_embedded_panel_manifest(manifest: dict[str, Any]) -> None:
    if (
        set(manifest)
        != {
            *_AGREEMENT_PANEL_IDENTITY_FIELDS,
            "complete",
            "generation_sha256",
        }
        or type(manifest.get("schema_version")) is not int
        or manifest.get("schema_version") != 1
        or manifest.get("analysis") != "annual_reference_station_agreement_panel"
        or manifest.get("complete") is not True
    ):
        raise RuntimeError("annual agreement embedded panel manifest changed")
    identity = {field: manifest[field] for field in _AGREEMENT_PANEL_IDENTITY_FIELDS}
    if manifest.get("generation_sha256") != _canonical_hash(identity):
        raise RuntimeError("annual agreement embedded panel manifest changed")


def _validate_final_manifest_relationships(manifest: dict[str, Any]) -> None:
    reviewed = load_annual_agreement_config()
    panel = _mapping(manifest.get("panel_manifest"), label="annual agreement final panel manifest")
    evaluation = _mapping(
        manifest.get("evaluation_manifest"),
        label="annual agreement final evaluation manifest",
    )
    _validate_embedded_panel_manifest(panel)
    panel_inputs = _mapping(panel.get("inputs"), label="annual agreement final panel inputs")
    expected_config = _checkpoint_config(reviewed)
    expected_claim = dict(reviewed.claim_boundary)
    if (
        manifest.get("annual_generation_sha256") != reviewed.annual_generation_sha256
        or panel_inputs.get("annual_generation_sha256") != reviewed.annual_generation_sha256
        or manifest.get("panel_generation_sha256") != panel.get("generation_sha256")
        or manifest.get("evaluation_generation_sha256") != evaluation.get("generation_sha256")
        or evaluation.get("panel_generation_sha256") != panel.get("generation_sha256")
        or manifest.get("checkpoint_inventory") != panel.get("checkpoint_inventory")
        or manifest.get("config") != expected_config
        or panel.get("config") != expected_config
        or evaluation.get("config") != expected_config
        or manifest.get("claim_boundary") != expected_claim
        or panel.get("claim_boundary") != expected_claim
        or evaluation.get("claim_boundary") != expected_claim
    ):
        raise RuntimeError("annual agreement final evidence relationships changed")


def _load_annual_agreement_result_unlocked(
    directory: Path,
    *,
    trusted_panel: AnnualAgreementPanel,
) -> AnnualAgreementResult:
    directory = directory.absolute()
    _validate_final_inventory(directory, during_read=False)
    written = _final_written(directory)
    manifest = _read_json(written["manifest"], label="annual agreement final manifest")
    summary = _read_json(written["summary"], label="annual agreement final summary")
    if (
        set(manifest) != {*_FINAL_IDENTITY_FIELDS, "complete", "generated_at", "generation_sha256"}
        or type(manifest.get("schema_version")) is not int
        or manifest.get("schema_version") != 1
        or manifest.get("analysis") != "annual_reference_station_agreement_benchmark"
        or manifest.get("complete") is not True
        or not isinstance(manifest.get("git_sha"), str)
        or re.fullmatch(r"[0-9a-f]{7,40}", str(manifest.get("git_sha"))) is None
        or type(manifest.get("git_dirty")) is not bool
    ):
        raise RuntimeError("annual agreement final manifest contract changed")
    _validate_final_manifest_relationships(manifest)
    identity = {field: manifest[field] for field in _FINAL_IDENTITY_FIELDS}
    if manifest.get("generation_sha256") != _canonical_hash(identity):
        raise RuntimeError("annual agreement final generation identity changed")
    if directory.name != manifest["generation_sha256"]:
        raise RuntimeError("annual agreement final generation directory changed")
    members = _mapping(manifest.get("members"), label="annual agreement final members")
    schemas = _mapping(manifest.get("schemas"), label="annual agreement final schemas")
    if set(members) != set(_FINAL_MEMBER_NAMES) or set(schemas) != set(_FINAL_MEMBER_NAMES):
        raise RuntimeError("annual agreement final member fields changed")
    summary_file = _checkpoint_file_identity(
        manifest.get("summary_file"), label="annual agreement final summary member"
    )
    if summary_file != _observed_checkpoint_file_identity(written["summary"]) or manifest.get(
        "summary_sha256"
    ) != _canonical_hash(summary):
        raise RuntimeError("annual agreement final summary changed")
    before = {name: _file_identity(path) for name, path in written.items()}
    frames: dict[str, pl.DataFrame] = {}
    for name in _FINAL_MEMBER_NAMES:
        path = written[name]
        if _checkpoint_file_identity(
            members[name], label=f"annual agreement final {name} member"
        ) != _observed_checkpoint_file_identity(path):
            raise RuntimeError(f"annual agreement final {name} member changed")
        frame = pl.read_parquet(path)
        if {column: str(dtype) for column, dtype in frame.schema.items()} != schemas[name]:
            raise RuntimeError(f"annual agreement final {name} schema changed")
        frames[name] = frame
    output_rows = {name: frame.height for name, frame in frames.items()}
    if manifest.get("output_rows") != output_rows:
        raise RuntimeError("annual agreement final output row counts changed")
    if manifest.get("panel_manifest") != trusted_panel.manifest or any(
        frames[name].rows() != getattr(trusted_panel, name).rows()
        for name in ("calendar", "paired_days", "exclusions")
    ):
        raise RuntimeError("annual agreement embedded panel manifest changed")
    if (
        set(summary) != {"panel", "output_rows"}
        or summary.get("panel") != trusted_panel.summary
        or summary.get("output_rows") != output_rows
    ):
        raise RuntimeError("annual agreement final summary changed")
    result = AnnualAgreementResult(
        directory=directory,
        calendar=frames["calendar"],
        paired_days=frames["paired_days"],
        exclusions=frames["exclusions"],
        fold_membership=frames["fold_membership"],
        folds=frames["folds"],
        predictions=frames["predictions"],
        scores=frames["scores"],
        deltas=frames["deltas"],
        summary=summary,
        manifest=manifest,
        written=written,
    )
    _validate_loaded_annual_agreement_result(result)
    if before != {name: _file_identity(path) for name, path in written.items()}:
        raise RuntimeError("annual agreement final generation changed during read")
    _validate_final_inventory(directory, during_read=True)
    return result


def load_annual_agreement_result(directory: Path) -> AnnualAgreementResult:
    plan = annual_agreement_run_plan()
    requested = directory.absolute()
    if requested.parent != plan.output_root.absolute():
        raise RuntimeError("annual agreement final generation is outside reviewed output root")
    with _annual_agreement_run_lock(plan.lock_path):
        _validate_final_inventory(requested, during_read=False)
        trusted_panel = load_annual_agreement_panel(plan.panel_destination)
        return _load_annual_agreement_result_unlocked(requested, trusted_panel=trusted_panel)


def _recover_annual_agreement_final_residue(output_root: Path) -> None:
    residue = sorted(
        entry
        for entry in output_root.iterdir()
        if entry.name.startswith(".annual-agreement.staging-")
        or entry.name.startswith(".annual-agreement.backup-")
    )
    if len(residue) > 1:
        raise RuntimeError("ambiguous final publication residue")
    if not residue:
        return
    candidate = residue[0]
    if re.fullmatch(r"\.annual-agreement\.(?:staging|backup)-[0-9a-f]{32}", candidate.name) is None:
        raise RuntimeError("annual agreement final publication residue name changed")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError("annual agreement final publication residue is unreadable") from exc
    if (
        _is_link_like(candidate)
        or resolved != candidate.absolute()
        or resolved.parent != output_root
        or not candidate.is_dir()
    ):
        raise RuntimeError("annual agreement final publication residue is linked or outside")
    allowed = {
        *(f"{name}.parquet" for name in _FINAL_MEMBER_NAMES),
        "summary.json",
        "manifest.json",
    }
    for entry in candidate.iterdir():
        try:
            resolved_entry = entry.resolve(strict=True)
        except OSError as exc:
            raise RuntimeError("annual agreement final publication residue is unreadable") from exc
        if (
            entry.name not in allowed
            or _is_link_like(entry)
            or not entry.is_file()
            or resolved_entry != entry.absolute()
            or resolved_entry.parent != resolved
        ):
            raise RuntimeError("annual agreement final publication residue is linked or outside")
    shutil.rmtree(candidate)


def _assert_prepared_final_result(
    result: AnnualAgreementResult,
    *,
    panel: AnnualAgreementPanel,
    evaluation: AnnualAgreementEvaluation,
) -> None:
    expected_frames = _final_frames(panel, evaluation)
    if (
        result.manifest.get("panel_manifest") != panel.manifest
        or result.manifest.get("evaluation_manifest") != evaluation.manifest
        or result.summary
        != {
            "panel": panel.summary,
            "output_rows": {name: frame.height for name, frame in expected_frames.items()},
        }
        or any(
            getattr(result, name).rows() != frame.rows() for name, frame in expected_frames.items()
        )
    ):
        raise RuntimeError("annual agreement existing final output differs from prepared result")


def _publish_annual_agreement_result(
    panel: AnnualAgreementPanel,
    evaluation: AnnualAgreementEvaluation,
    *,
    output_root: Path,
) -> AnnualAgreementResult:
    frames = _final_frames(panel, evaluation)
    output_root = output_root.absolute()
    output_root.mkdir(parents=True, exist_ok=True)
    try:
        resolved_output_root = output_root.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError("annual agreement final output root is unreadable") from exc
    if _is_link_like(output_root) or resolved_output_root != output_root:
        raise RuntimeError("annual agreement final output root is linked or outside")
    _recover_annual_agreement_final_residue(output_root)
    staged = output_root / f".annual-agreement.staging-{uuid4().hex}"
    staged.mkdir()
    try:
        resolved_staged = staged.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError("annual agreement final staging directory is unreadable") from exc
    if _is_link_like(staged) or resolved_staged.parent != output_root:
        raise RuntimeError("annual agreement final staging directory is linked or outside")
    destination: Path | None = None
    try:
        members: dict[str, dict[str, object]] = {}
        schemas: dict[str, dict[str, str]] = {}
        for name, frame in frames.items():
            path = staged / f"{name}.parquet"
            frame.write_parquet(path)
            members[name] = _observed_checkpoint_file_identity(path)
            schemas[name] = {column: str(dtype) for column, dtype in frame.schema.items()}
        summary: dict[str, object] = {
            "panel": panel.summary,
            "output_rows": {name: frame.height for name, frame in frames.items()},
        }
        _write_checkpoint_json(staged / "summary.json", summary)
        summary_file = _observed_checkpoint_file_identity(staged / "summary.json")
        config = load_annual_agreement_config()
        git_sha, git_dirty = git_state()
        identity: dict[str, object] = {
            "schema_version": 1,
            "analysis": "annual_reference_station_agreement_benchmark",
            "annual_generation_sha256": config.annual_generation_sha256,
            "panel_generation_sha256": panel.manifest["generation_sha256"],
            "evaluation_generation_sha256": evaluation.manifest["generation_sha256"],
            "panel_manifest": panel.manifest,
            "evaluation_manifest": evaluation.manifest,
            "checkpoint_inventory": panel.manifest["checkpoint_inventory"],
            "config": _checkpoint_config(config),
            "claim_boundary": dict(config.claim_boundary),
            "output_rows": summary["output_rows"],
            "schemas": schemas,
            "members": members,
            "summary_file": summary_file,
            "summary_sha256": _canonical_hash(summary),
            "git_sha": git_sha,
            "git_dirty": git_dirty,
        }
        generation = _canonical_hash(identity)
        destination = output_root / generation
        if destination.exists():
            existing = _load_annual_agreement_result_unlocked(destination, trusted_panel=panel)
            _assert_prepared_final_result(existing, panel=panel, evaluation=evaluation)
            return existing
        manifest = {
            **identity,
            "complete": True,
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "generation_sha256": generation,
        }
        _write_checkpoint_json(staged / "manifest.json", manifest)
        try:
            staged.replace(destination)
            return _load_annual_agreement_result_unlocked(destination, trusted_panel=panel)
        except BaseException:
            if destination.exists():
                shutil.rmtree(destination)
            raise
    finally:
        if staged.exists():
            shutil.rmtree(staged)


def _run_annual_agreement_locked(
    *,
    plan: AnnualAgreementRunPlan,
    config: AnnualAgreementConfig,
) -> Any:
    annual_input = load_annual_readiness_input(plan.annual_generation_dir)
    checkpoints = _prepare_annual_agreement_checkpoints(
        annual_input,
        plan=plan,
        config=config,
    )
    panel = _prepare_annual_agreement_panel(annual_input, checkpoints, config)
    panel = _load_or_write_annual_agreement_panel(
        panel,
        destination=plan.panel_destination,
    )
    evaluation = evaluate_annual_agreement(panel)
    return _publish_annual_agreement_result(
        panel,
        evaluation,
        output_root=plan.output_root,
    )


def run_and_write_annual_agreement() -> Any:
    config = load_annual_agreement_config()
    plan = annual_agreement_run_plan()
    with _annual_agreement_run_lock(plan.lock_path):
        return _run_annual_agreement_locked(plan=plan, config=config)
