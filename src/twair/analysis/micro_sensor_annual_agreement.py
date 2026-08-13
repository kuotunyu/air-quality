"""Benchmark annual micro-sensor agreement with nearby reference stations."""

from __future__ import annotations

import json
import math
import re
import shutil
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import date, timedelta
from hashlib import sha256
from itertools import pairwise
from pathlib import Path
from typing import Any
from uuid import uuid4

import duckdb
import polars as pl

from twair.analysis.micro_sensor_annual_readiness import (
    ANNUAL_CALENDAR_SCHEMA,
    ANNUAL_COHORT_THRESHOLD_SCHEMA,
    ANNUAL_DEVICE_COHORT_SCHEMA,
    ANNUAL_DEVICE_DAY_SCHEMA,
    ANNUAL_EXCLUSION_SCHEMA,
    load_annual_micro_sensor_panel_config,
)
from twair.config import ConfigError, load_conf
from twair.ingest.micro_sensor_observations import OBSERVATION_OUTPUT_SCHEMA
from twair.ingest.station_meta import resolve_station_geo
from twair.net import sha256_file
from twair.store.schema import PARTITION_SCHEMA

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
class AgreementDayCheckpoint:
    directory: Path
    member_path: Path
    manifest_path: Path
    rows: pl.DataFrame
    manifest: dict[str, Any]


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
        "raw_generation_sha256",
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
        "raw_generation_sha256",
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
