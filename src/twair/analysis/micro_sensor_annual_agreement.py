"""Benchmark annual micro-sensor agreement with nearby reference stations."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from itertools import pairwise
from pathlib import Path
from typing import Any

import polars as pl

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
