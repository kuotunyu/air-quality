"""Official Civil IoT low-cost-sensor catalogue acquisition and validation."""

from __future__ import annotations

import base64
import calendar
import csv
import hashlib
import json
import re
import shutil
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from io import StringIO
from pathlib import Path, PurePosixPath
from typing import Any, Protocol
from uuid import uuid4

import polars as pl

from twair.config import ConfigError, load_conf
from twair.net import PoliteClient, sha256_file
from twair.paths import interim_dir, raw_dir
from twair.provenance import git_state
from twair.scalars import as_int

STATION_SOURCE_COLUMNS = (
    "deviceId",
    "locationId",
    "desc",
    "lat",
    "lon",
    "area",
    "areatype",
    "town",
    "county",
    "project_name",
)
STATION_OUTPUT_COLUMNS = (
    "device_id",
    "location_id",
    "description",
    "lat",
    "lon",
    "area",
    "area_type",
    "township",
    "county",
    "project_name",
)
CATALOG_SCHEMA = {
    "date": pl.Date,
    "variable": pl.String,
    "archive_present": pl.Boolean,
    "filename": pl.String,
    "filename_prefix": pl.String,
    "path": pl.String,
    "bytes": pl.Int64,
    "modified_unix": pl.Int64,
}


@dataclass(frozen=True, slots=True)
class MicroSensorSource:
    provider: str
    history_base_url: str
    history_root_path: str
    station_metadata_filename: str
    routes: dict[str, str]
    required_variables: tuple[str, ...]
    archive_filename_pattern: str
    max_directory_entries: int
    max_archive_bytes: int
    max_total_bytes: int
    allowed_archive_content_types: tuple[str, ...]
    max_zip_members: int
    max_uncompressed_bytes_per_archive: int

    @property
    def station_metadata_path(self) -> str:
        return f"{self.history_root_path}/{self.station_metadata_filename}"

    def month_path(self, month: str) -> str:
        _parse_month(month)
        return f"{self.history_root_path}/{month}"


@dataclass(frozen=True, slots=True)
class MicroSensorCatalogSnapshot:
    generation_sha256: str
    month: str
    station_metadata_bytes: bytes
    directory_payload: dict[str, Any]
    stations: pl.DataFrame
    catalog: pl.DataFrame
    manifest: dict[str, Any]


@dataclass(frozen=True, slots=True)
class MicroSensorCatalogWrite:
    generation_sha256: str
    raw_directory: Path
    interim_directory: Path
    manifest: dict[str, Any]


@dataclass(frozen=True, slots=True)
class MicroSensorCatalogGeneration:
    generation_sha256: str
    directory: Path
    catalog: pl.DataFrame
    manifest: dict[str, Any]


@dataclass(frozen=True, slots=True)
class MicroSensorArchive:
    variable: str
    filename: str
    path: str
    bytes: int
    modified_unix: int


@dataclass(frozen=True, slots=True)
class MicroSensorDayWrite:
    generation_sha256: str
    directory: Path
    manifest: dict[str, Any]


class MicroSensorHistoryBackend(Protocol):
    def fetch_station_metadata(self) -> bytes: ...

    def list_month(self, month: str) -> dict[str, Any]: ...


class MicroSensorObservationBackend(Protocol):
    def download_archive(
        self,
        provider_path: str,
        destination: Path,
        *,
        expected_bytes: int,
        max_bytes: int,
        allowed_content_types: frozenset[str],
    ) -> Path: ...


class FileGatorHistoryBackend:
    """Credential-free adapter for the official history service's guest API."""

    def __init__(
        self,
        source: MicroSensorSource | None = None,
        *,
        min_interval: float | None = None,
    ) -> None:
        self.source = source or load_micro_sensor_source()
        self._client = PoliteClient(min_interval=min_interval)
        self._csrf_token: str | None = None

    def __enter__(self) -> FileGatorHistoryBackend:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _route_params(self, name: str, **extra: str) -> dict[str, str]:
        return {"r": self.source.routes[name], **extra}

    def _guest_headers(self) -> dict[str, str]:
        if self._csrf_token is not None:
            return {"X-CSRF-Token": self._csrf_token}
        try:
            config_response = self._client.get(
                self.source.history_base_url,
                params=self._route_params("get_config"),
            )
            _mapping(config_response.json(), label="micro-sensor guest configuration")
            user_response = self._client.get(
                self.source.history_base_url,
                params=self._route_params("get_user"),
            )
            user_envelope = _mapping(
                user_response.json(),
                label="micro-sensor guest user response",
            )
        except (ValueError, TypeError) as exc:
            raise ConfigError("micro-sensor history guest session returned invalid JSON") from exc
        user = _mapping(user_envelope.get("data"), label="micro-sensor guest user")
        permissions = user.get("permissions")
        if not isinstance(permissions, list) or not {"read", "download"}.issubset(
            {value for value in permissions if isinstance(value, str)}
        ):
            raise ConfigError("micro-sensor guest session has no read and download permission")
        token = user_response.headers.get("X-CSRF-Token")
        if not isinstance(token, str) or not token:
            raise ConfigError("micro-sensor guest session returned no CSRF token")
        self._csrf_token = token
        return {"X-CSRF-Token": token}

    def list_month(self, month: str) -> dict[str, Any]:
        """Return the provider envelope for one monthly archive directory."""
        response = self._client.post(
            self.source.history_base_url,
            params=self._route_params("get_directory"),
            headers=self._guest_headers(),
            json={"dir": self.source.month_path(month)},
        )
        try:
            return _mapping(response.json(), label="micro-sensor directory response")
        except (ValueError, TypeError) as exc:
            raise ConfigError("micro-sensor directory response is not valid JSON") from exc

    def fetch_station_metadata(self) -> bytes:
        """Download the reviewed station CSV without accepting an HTML login page."""
        encoded_path = base64.b64encode(self.source.station_metadata_path.encode()).decode()
        response = self._client.get(
            self.source.history_base_url,
            params=self._route_params("download", path=encoded_path),
            headers=self._guest_headers(),
        )
        content_type = response.headers.get("Content-Type", "").lower()
        if not content_type.startswith("text/csv"):
            raise ConfigError("micro-sensor station download did not have a CSV content type")
        if not response.content:
            raise ConfigError("micro-sensor station download is empty")
        return response.content

    def download_archive(
        self,
        provider_path: str,
        destination: Path,
        *,
        expected_bytes: int,
        max_bytes: int,
        allowed_content_types: frozenset[str],
    ) -> Path:
        """Stream one catalogue-selected ZIP through the shared guarded client."""
        encoded_path = base64.b64encode(provider_path.encode()).decode()
        headers = {**self._guest_headers(), "Accept-Encoding": "identity"}
        return self._client.stream_to_file(
            self.source.history_base_url,
            destination,
            params=self._route_params("download", path=encoded_path),
            headers=headers,
            expected_bytes=expected_bytes,
            max_bytes=max_bytes,
            allowed_content_types=allowed_content_types,
        )


def _mapping(value: object, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ConfigError(f"{label} must be a string-keyed mapping")
    return value


def _required_string(mapping: dict[str, Any], key: str, *, label: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{label}.{key} must be a non-empty string")
    return value


def _required_positive_int(mapping: dict[str, Any], key: str, *, label: str) -> int:
    value = mapping.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(f"{label}.{key} must be a positive integer")
    return value


def load_micro_sensor_source() -> MicroSensorSource:
    """Load the reviewed, credential-free history source contract."""
    config = _mapping(load_conf("micro_sensors"), label="micro_sensors")
    if config.get("schema_version") != 1:
        raise ConfigError("micro_sensors.schema_version must be 1")
    source = _mapping(config.get("source"), label="micro_sensors.source")
    pilot = _mapping(
        source.get("observation_pilot"),
        label="micro_sensors.source.observation_pilot",
    )
    routes = _mapping(source.get("routes"), label="micro_sensors.source.routes")
    required_route_names = ("get_config", "get_user", "get_directory", "download")
    selected_routes = {
        name: _required_string(routes, name, label="micro_sensors.source.routes")
        for name in required_route_names
    }
    variables = source.get("required_variables")
    if (
        not isinstance(variables, list)
        or not variables
        or not all(isinstance(value, str) and value for value in variables)
        or len(set(variables)) != len(variables)
    ):
        raise ConfigError("micro_sensors.source.required_variables must be unique strings")
    pattern = _required_string(
        source,
        "archive_filename_pattern",
        label="micro_sensors.source",
    )
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        raise ConfigError("micro-sensor archive filename pattern is invalid") from exc
    if not {"prefix", "variable", "date"}.issubset(compiled.groupindex):
        raise ConfigError("archive filename pattern must capture prefix, variable, and date")
    limit = source.get("max_directory_entries")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ConfigError("micro_sensors.source.max_directory_entries must be positive")
    base_url = _required_string(source, "history_base_url", label="micro_sensors.source")
    root_path = _required_string(source, "history_root_path", label="micro_sensors.source")
    if not base_url.startswith("https://") or not base_url.endswith("/"):
        raise ConfigError("micro-sensor history_base_url must be an HTTPS directory URL")
    if not root_path.startswith("/") or root_path.endswith("/"):
        raise ConfigError("micro-sensor history_root_path must be an absolute provider path")
    content_types = pilot.get("allowed_content_types")
    if (
        not isinstance(content_types, list)
        or not content_types
        or not all(
            isinstance(value, str)
            and value == value.strip().lower()
            and value.startswith("application/")
            and ";" not in value
            for value in content_types
        )
        or len(set(content_types)) != len(content_types)
    ):
        raise ConfigError(
            "micro_sensors.source.observation_pilot.allowed_content_types "
            "must be unique reviewed application media types"
        )
    return MicroSensorSource(
        provider=_required_string(source, "provider", label="micro_sensors.source"),
        history_base_url=base_url,
        history_root_path=root_path,
        station_metadata_filename=_required_string(
            source,
            "station_metadata_filename",
            label="micro_sensors.source",
        ),
        routes=selected_routes,
        required_variables=tuple(variables),
        archive_filename_pattern=pattern,
        max_directory_entries=limit,
        max_archive_bytes=_required_positive_int(
            pilot,
            "max_archive_bytes",
            label="micro_sensors.source.observation_pilot",
        ),
        max_total_bytes=_required_positive_int(
            pilot,
            "max_total_bytes",
            label="micro_sensors.source.observation_pilot",
        ),
        allowed_archive_content_types=tuple(content_types),
        max_zip_members=_required_positive_int(
            pilot,
            "max_zip_members",
            label="micro_sensors.source.observation_pilot",
        ),
        max_uncompressed_bytes_per_archive=_required_positive_int(
            pilot,
            "max_uncompressed_bytes_per_archive",
            label="micro_sensors.source.observation_pilot",
        ),
    )


def parse_station_metadata(raw: bytes) -> pl.DataFrame:
    """Validate every source row before converting the two coordinate columns."""
    try:
        source_rows = list(csv.reader(StringIO(raw.decode("utf-8-sig"), newline="")))
    except (csv.Error, UnicodeError) as exc:
        raise ConfigError("micro-sensor station metadata is not a valid UTF-8 CSV") from exc
    if not source_rows or tuple(source_rows[0]) != STATION_SOURCE_COLUMNS:
        raise ConfigError(
            "micro-sensor station metadata columns do not match the reviewed source schema"
        )
    if len(source_rows) == 1:
        raise ConfigError("micro-sensor station metadata has no data rows")
    if any(len(row) != len(STATION_SOURCE_COLUMNS) for row in source_rows[1:]):
        raise ConfigError(
            "micro-sensor station metadata columns do not match the reviewed source schema"
        )
    frame = pl.DataFrame(
        source_rows[1:],
        schema=dict.fromkeys(STATION_SOURCE_COLUMNS, pl.String),
        orient="row",
    )
    if frame["deviceId"].str.strip_chars().eq("").any():
        raise ConfigError("micro-sensor station metadata has an empty deviceId")
    duplicates = frame.group_by("deviceId").len().filter(pl.col("len") > 1)
    if not duplicates.is_empty():
        raise ConfigError("micro-sensor station metadata has a duplicate deviceId")

    parsed = frame.with_columns(
        pl.col("lat").cast(pl.Float64, strict=False).alias("_lat"),
        pl.col("lon").cast(pl.Float64, strict=False).alias("_lon"),
    )
    if parsed["_lat"].null_count() or parsed["_lon"].null_count():
        raise ConfigError("micro-sensor station metadata has an invalid latitude or longitude")
    outside = parsed.filter(
        ~pl.col("_lat").is_between(-90.0, 90.0, closed="both")
        | ~pl.col("_lon").is_between(-180.0, 180.0, closed="both")
    )
    if not outside.is_empty():
        raise ConfigError("micro-sensor station metadata coordinates are outside WGS84 bounds")

    return parsed.select(
        pl.col("deviceId").alias("device_id"),
        pl.col("locationId").alias("location_id"),
        pl.col("desc").alias("description"),
        pl.col("_lat").alias("lat"),
        pl.col("_lon").alias("lon"),
        pl.col("area"),
        pl.col("areatype").alias("area_type"),
        pl.col("town").alias("township"),
        pl.col("county"),
        pl.col("project_name"),
    )


def _parse_month(month: str) -> tuple[int, int]:
    if not re.fullmatch(r"[0-9]{6}", month):
        raise ConfigError("micro-sensor month must use YYYYMM")
    year = int(month[:4])
    month_number = int(month[4:])
    if year < 1 or not 1 <= month_number <= 12:
        raise ConfigError("micro-sensor month must use YYYYMM")
    return year, month_number


def _provider_files(payload: object, *, expected_location: str, limit: int) -> list[object]:
    envelope = _mapping(payload, label="micro-sensor directory response")
    data = _mapping(envelope.get("data"), label="micro-sensor directory response.data")
    if data.get("location") != expected_location:
        raise ConfigError("micro-sensor directory location does not match the requested month")
    files = data.get("files")
    if not isinstance(files, list):
        raise ConfigError("micro-sensor directory files must be a list")
    if len(files) > limit:
        raise ConfigError("micro-sensor directory exceeds the reviewed entry limit")
    return files


def _entry_int(entry: dict[str, Any], field: str, *, filename: str) -> int:
    value = entry.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConfigError(f"micro-sensor archive {filename} has invalid {field}")
    return value


def normalize_month_catalog(
    payload: object,
    *,
    month: str,
    source: MicroSensorSource | None = None,
) -> pl.DataFrame:
    """Turn a sparse provider listing into an explicit calendar-by-variable matrix."""
    selected = source or load_micro_sensor_source()
    year, month_number = _parse_month(month)
    location = selected.month_path(month)
    pattern = re.compile(selected.archive_filename_pattern)
    observed: dict[tuple[date, str], dict[str, object]] = {}

    for raw_entry in _provider_files(
        payload,
        expected_location=location,
        limit=selected.max_directory_entries,
    ):
        entry = _mapping(raw_entry, label="micro-sensor directory entry")
        if entry.get("type") == "back" and entry.get("name") == "..":
            continue
        if entry.get("type") != "file":
            raise ConfigError("micro-sensor directory contains a non-file archive entry")
        filename = entry.get("name")
        path = entry.get("path")
        if not isinstance(filename, str) or not filename:
            raise ConfigError("micro-sensor directory entry has no filename")
        if "/" in filename or "\\" in filename:
            raise ConfigError(f"micro-sensor archive {filename} has an invalid provider filename")
        if not isinstance(path, str) or path != f"{location}/{filename}":
            raise ConfigError(f"micro-sensor archive {filename} has an invalid provider path")
        match = pattern.fullmatch(filename)
        if match is None:
            raise ConfigError(f"unrecognised archive entry: {filename}")
        variable = match.group("variable")
        if variable not in selected.required_variables:
            raise ConfigError(f"unrecognised micro-sensor variable: {variable}")
        try:
            observed_date = datetime.strptime(match.group("date"), "%Y%m%d").date()
        except ValueError as exc:
            raise ConfigError(f"micro-sensor archive {filename} has an invalid date") from exc
        if (observed_date.year, observed_date.month) != (year, month_number):
            raise ConfigError(f"micro-sensor archive {filename} is outside requested month")
        key = (observed_date, variable)
        if key in observed:
            raise ConfigError(f"duplicate archive for {observed_date.isoformat()} {variable}")
        observed[key] = {
            "filename": filename,
            "filename_prefix": match.group("prefix"),
            "path": path,
            "bytes": _entry_int(entry, "size", filename=filename),
            "modified_unix": _entry_int(entry, "time", filename=filename),
        }

    rows: list[dict[str, object]] = []
    days = calendar.monthrange(year, month_number)[1]
    for day in range(1, days + 1):
        current = date(year, month_number, day)
        for variable in selected.required_variables:
            file_metadata = observed.get((current, variable))
            rows.append(
                {
                    "date": current,
                    "variable": variable,
                    "archive_present": file_metadata is not None,
                    "filename": None if file_metadata is None else file_metadata["filename"],
                    "filename_prefix": (
                        None if file_metadata is None else file_metadata["filename_prefix"]
                    ),
                    "path": None if file_metadata is None else file_metadata["path"],
                    "bytes": None if file_metadata is None else file_metadata["bytes"],
                    "modified_unix": (
                        None if file_metadata is None else file_metadata["modified_unix"]
                    ),
                }
            )
    return pl.DataFrame(rows, schema=CATALOG_SCHEMA)


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode()


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _source_contract(source: MicroSensorSource) -> dict[str, object]:
    return {
        "provider": source.provider,
        "history_base_url": source.history_base_url,
        "history_root_path": source.history_root_path,
        "station_metadata_filename": source.station_metadata_filename,
        "routes": dict(source.routes),
        "required_variables": list(source.required_variables),
        "archive_filename_pattern": source.archive_filename_pattern,
        "max_directory_entries": source.max_directory_entries,
    }


def _observation_contract(source: MicroSensorSource) -> dict[str, object]:
    return {
        "max_archive_bytes": source.max_archive_bytes,
        "max_total_bytes": source.max_total_bytes,
        "allowed_content_types": list(source.allowed_archive_content_types),
        "max_zip_members": source.max_zip_members,
        "max_uncompressed_bytes_per_archive": source.max_uncompressed_bytes_per_archive,
    }


def _empty_string_counts(stations: pl.DataFrame) -> dict[str, int]:
    return {
        name: int(stations.select(pl.col(name).str.strip_chars().eq("").sum()).item())
        for name, dtype in stations.schema.items()
        if dtype == pl.String
    }


def _duplicate_coordinate_counts(stations: pl.DataFrame) -> tuple[int, int, int]:
    duplicates = stations.group_by("lat", "lon").len().filter(pl.col("len") > 1)
    if duplicates.is_empty():
        return 0, 0, 0
    return (
        duplicates.height,
        as_int(duplicates["len"].sum(), what="rows in duplicate coordinates"),
        as_int(duplicates["len"].max(), what="largest duplicate coordinate group"),
    )


def build_catalog_snapshot(
    station_metadata_bytes: bytes,
    directory_payload: dict[str, Any],
    *,
    month: str,
    source: MicroSensorSource | None = None,
    generated_at: str | None = None,
    git_sha: str | None = None,
    git_dirty: bool | None = None,
) -> MicroSensorCatalogSnapshot:
    """Bind exact provider inputs to normalized tables and a stable generation."""
    selected = source or load_micro_sensor_source()
    stations = parse_station_metadata(station_metadata_bytes)
    catalog = normalize_month_catalog(directory_payload, month=month, source=selected)
    directory_bytes = _canonical_json(directory_payload)
    contract = _source_contract(selected)
    contract_sha = _sha256(_canonical_json(contract))
    station_sha = _sha256(station_metadata_bytes)
    directory_sha = _sha256(directory_bytes)
    identity = {
        "schema_version": 1,
        "month": month,
        "source_contract_sha256": contract_sha,
        "station_metadata_sha256": station_sha,
        "directory_response_sha256": directory_sha,
        "station_schema": list(STATION_OUTPUT_COLUMNS),
        "catalog_schema": list(CATALOG_SCHEMA),
    }
    generation = _sha256(_canonical_json(identity))
    duplicate_groups, duplicate_rows, largest_duplicate_group = _duplicate_coordinate_counts(
        stations
    )
    present = catalog.filter(pl.col("archive_present"))
    measured_sha, measured_dirty = git_state()
    run_sha = measured_sha if git_sha is None else git_sha
    run_dirty = measured_dirty if git_dirty is None else git_dirty
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "generation_sha256": generation,
        "generated_at": generated_at or datetime.now(UTC).isoformat(timespec="seconds"),
        "git_sha": run_sha,
        "git_dirty": run_dirty,
        "source_contract": contract,
        "source_contract_sha256": contract_sha,
        "station_metadata_path": selected.station_metadata_path,
        "directory_path": selected.month_path(month),
        "directory_response": {
            "bytes": len(directory_bytes),
            "sha256": directory_sha,
        },
        "station_metadata": {
            "bytes": len(station_metadata_bytes),
            "sha256": station_sha,
            "rows": stations.height,
            "empty_strings": _empty_string_counts(stations),
            "duplicate_coordinate_groups": duplicate_groups,
            "rows_in_duplicate_coordinates": duplicate_rows,
            "largest_duplicate_coordinate_group": largest_duplicate_group,
        },
        "archive_catalog": {
            "month": month,
            "rows": catalog.height,
            "present": present.height,
            "absent": catalog.height - present.height,
            "present_bytes": as_int(present["bytes"].sum(), what="present archive bytes"),
        },
    }
    return MicroSensorCatalogSnapshot(
        generation_sha256=generation,
        month=month,
        station_metadata_bytes=station_metadata_bytes,
        directory_payload=directory_payload,
        stations=stations,
        catalog=catalog,
        manifest=manifest,
    )


def _member_identity(path: Path) -> dict[str, object]:
    return {"bytes": path.stat().st_size, "sha256": sha256_file(path)}


def _write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"catalog manifest is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"catalog manifest must be an object: {path}")
    return value


def _validate_member_identities(directory: Path, manifest: dict[str, Any]) -> None:
    members = manifest.get("members")
    if not isinstance(members, dict):
        raise RuntimeError(f"catalog manifest has no member identities: {directory}")
    expected_names = {"manifest.json", *members}
    actual_names = {path.name for path in directory.iterdir()}
    if actual_names != expected_names:
        raise RuntimeError(f"catalog generation members changed: {directory}")
    for name, expected in members.items():
        if not isinstance(name, str) or not isinstance(expected, dict):
            raise RuntimeError(f"catalog manifest member identity is invalid: {directory}")
        if _member_identity(directory / name) != expected:
            raise RuntimeError(f"catalog generation checksum changed: {directory / name}")


def _validate_snapshot_metadata(
    directory: Path,
    manifest: dict[str, Any],
    snapshot: MicroSensorCatalogSnapshot,
) -> None:
    stable_keys = (
        "schema_version",
        "generation_sha256",
        "source_contract",
        "source_contract_sha256",
        "station_metadata_path",
        "directory_path",
        "directory_response",
        "station_metadata",
        "archive_catalog",
    )
    if any(manifest.get(key) != snapshot.manifest.get(key) for key in stable_keys):
        raise RuntimeError(f"catalog generation manifest metadata changed: {directory}")


def _validate_raw_generation(
    directory: Path,
    snapshot: MicroSensorCatalogSnapshot,
) -> dict[str, Any]:
    manifest = _manifest(directory / "manifest.json")
    if manifest.get("kind") != "raw_micro_sensor_catalog":
        raise RuntimeError(f"catalog raw generation kind is invalid: {directory}")
    if manifest.get("generation_sha256") != snapshot.generation_sha256:
        raise RuntimeError(f"catalog raw generation identity changed: {directory}")
    _validate_snapshot_metadata(directory, manifest, snapshot)
    _validate_member_identities(directory, manifest)
    if (directory / "station_metadata.csv").read_bytes() != snapshot.station_metadata_bytes:
        raise RuntimeError(f"catalog station metadata checksum changed: {directory}")
    if (directory / "directory.json").read_bytes() != _canonical_json(snapshot.directory_payload):
        raise RuntimeError(f"catalog directory response checksum changed: {directory}")
    return manifest


def _validate_interim_generation(
    directory: Path,
    snapshot: MicroSensorCatalogSnapshot,
    *,
    raw_manifest_sha256: str,
) -> dict[str, Any]:
    manifest = _manifest(directory / "manifest.json")
    if manifest.get("kind") != "normalized_micro_sensor_catalog":
        raise RuntimeError(f"catalog normalized generation kind is invalid: {directory}")
    if manifest.get("generation_sha256") != snapshot.generation_sha256:
        raise RuntimeError(f"catalog normalized generation identity changed: {directory}")
    _validate_snapshot_metadata(directory, manifest, snapshot)
    if manifest.get("raw_manifest_sha256") != raw_manifest_sha256:
        raise RuntimeError(f"catalog normalized generation raw identity changed: {directory}")
    _validate_member_identities(directory, manifest)
    try:
        stations = pl.read_parquet(directory / "stations.parquet")
        catalog = pl.read_parquet(directory / "archive_catalog.parquet")
    except (OSError, pl.exceptions.PolarsError) as exc:
        raise RuntimeError(f"catalog normalized generation is unreadable: {directory}") from exc
    if not stations.equals(snapshot.stations) or not catalog.equals(snapshot.catalog):
        raise RuntimeError(f"catalog normalized values changed: {directory}")
    return manifest


def _atomic_generation(
    directory: Path,
    writer: Callable[[Path], None],
    validator: Callable[[Path], dict[str, Any]],
) -> dict[str, Any]:
    directory.parent.mkdir(parents=True, exist_ok=True)
    if directory.exists():
        if not directory.is_dir():
            raise RuntimeError(f"catalog generation path is not a directory: {directory}")
        return validator(directory)
    staged = directory.parent / f".staging-{uuid4().hex}"
    staged.mkdir()
    try:
        writer(staged)
        validator(staged)
        staged.replace(directory)
    except BaseException:
        if staged.exists():
            shutil.rmtree(staged)
        raise
    return validator(directory)


def write_catalog_snapshot(
    snapshot: MicroSensorCatalogSnapshot,
    *,
    raw_root: Path | None = None,
    interim_root: Path | None = None,
) -> MicroSensorCatalogWrite:
    """Persist immutable raw and normalized generations with interruption recovery."""
    selected_raw_root = raw_root or raw_dir("micro_sensors") / "catalog" / "generations"
    selected_interim_root = interim_root or interim_dir("micro_sensors") / "catalog" / "generations"
    raw_destination = selected_raw_root / snapshot.generation_sha256
    interim_destination = selected_interim_root / snapshot.generation_sha256

    def write_raw(staged: Path) -> None:
        (staged / "station_metadata.csv").write_bytes(snapshot.station_metadata_bytes)
        (staged / "directory.json").write_bytes(_canonical_json(snapshot.directory_payload))
        manifest = {
            **snapshot.manifest,
            "kind": "raw_micro_sensor_catalog",
            "members": {
                name: _member_identity(staged / name)
                for name in ("directory.json", "station_metadata.csv")
            },
        }
        _write_json(staged / "manifest.json", manifest)

    raw_manifest = _atomic_generation(
        raw_destination,
        write_raw,
        lambda directory: _validate_raw_generation(directory, snapshot),
    )
    raw_manifest_sha = _sha256((raw_destination / "manifest.json").read_bytes())

    def write_interim(staged: Path) -> None:
        snapshot.stations.write_parquet(staged / "stations.parquet")
        snapshot.catalog.write_parquet(staged / "archive_catalog.parquet")
        manifest = {
            **snapshot.manifest,
            "kind": "normalized_micro_sensor_catalog",
            "raw_manifest_sha256": raw_manifest_sha,
            "members": {
                name: _member_identity(staged / name)
                for name in ("archive_catalog.parquet", "stations.parquet")
            },
        }
        _write_json(staged / "manifest.json", manifest)

    interim_manifest = _atomic_generation(
        interim_destination,
        write_interim,
        lambda directory: _validate_interim_generation(
            directory,
            snapshot,
            raw_manifest_sha256=raw_manifest_sha,
        ),
    )
    if raw_manifest.get("generation_sha256") != interim_manifest.get("generation_sha256"):
        raise RuntimeError("raw and normalized micro-sensor catalog generations disagree")
    return MicroSensorCatalogWrite(
        generation_sha256=snapshot.generation_sha256,
        raw_directory=raw_destination,
        interim_directory=interim_destination,
        manifest=interim_manifest,
    )


def acquire_micro_sensor_catalog(
    month: str,
    *,
    backend: MicroSensorHistoryBackend,
    source: MicroSensorSource | None = None,
    generated_at: str | None = None,
    raw_root: Path | None = None,
    interim_root: Path | None = None,
) -> MicroSensorCatalogWrite:
    """Fetch only station metadata and a monthly listing, then persist their snapshot."""
    selected = source or load_micro_sensor_source()
    selected.month_path(month)
    station_metadata = backend.fetch_station_metadata()
    directory_payload = backend.list_month(month)
    snapshot = build_catalog_snapshot(
        station_metadata,
        directory_payload,
        month=month,
        source=selected,
        generated_at=generated_at,
    )
    return write_catalog_snapshot(
        snapshot,
        raw_root=raw_root,
        interim_root=interim_root,
    )


def _validated_generation_sha256(value: str, *, label: str) -> str:
    if re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ConfigError(f"{label} must be a lowercase SHA-256")
    return value


def _catalog_identity_from_manifest(manifest: dict[str, Any]) -> dict[str, object]:
    station = _mapping(manifest.get("station_metadata"), label="catalog station_metadata")
    directory = _mapping(manifest.get("directory_response"), label="catalog directory_response")
    catalog = _mapping(manifest.get("archive_catalog"), label="catalog archive_catalog")
    return {
        "schema_version": manifest.get("schema_version"),
        "month": catalog.get("month"),
        "source_contract_sha256": manifest.get("source_contract_sha256"),
        "station_metadata_sha256": station.get("sha256"),
        "directory_response_sha256": directory.get("sha256"),
        "station_schema": list(STATION_OUTPUT_COLUMNS),
        "catalog_schema": list(CATALOG_SCHEMA),
    }


def load_catalog_generation(
    generation_sha256: str,
    *,
    source: MicroSensorSource | None = None,
    raw_root: Path | None = None,
    interim_root: Path | None = None,
) -> MicroSensorCatalogGeneration:
    """Reload and fully bind an immutable catalogue before selecting observations."""
    generation = _validated_generation_sha256(
        generation_sha256,
        label="micro-sensor catalogue generation",
    )
    selected = source or load_micro_sensor_source()
    selected_raw_root = raw_root or raw_dir("micro_sensors") / "catalog" / "generations"
    selected_interim_root = interim_root or interim_dir("micro_sensors") / "catalog" / "generations"
    raw_directory = selected_raw_root / generation
    directory = selected_interim_root / generation
    if not raw_directory.is_dir() or not directory.is_dir():
        raise ConfigError("micro-sensor catalogue generation is missing")

    raw_manifest = _manifest(raw_directory / "manifest.json")
    manifest = _manifest(directory / "manifest.json")
    if raw_manifest.get("kind") != "raw_micro_sensor_catalog":
        raise RuntimeError(f"catalog raw generation kind is invalid: {raw_directory}")
    if manifest.get("kind") != "normalized_micro_sensor_catalog":
        raise RuntimeError(f"catalog normalized generation kind is invalid: {directory}")
    if (
        raw_manifest.get("generation_sha256") != generation
        or manifest.get("generation_sha256") != generation
    ):
        raise RuntimeError("catalog generation identity changed")
    _validate_member_identities(raw_directory, raw_manifest)
    _validate_member_identities(directory, manifest)
    raw_manifest_sha = _sha256((raw_directory / "manifest.json").read_bytes())
    if manifest.get("raw_manifest_sha256") != raw_manifest_sha:
        raise RuntimeError("catalog normalized generation raw identity changed")
    stable_keys = (
        "schema_version",
        "generation_sha256",
        "source_contract",
        "source_contract_sha256",
        "station_metadata_path",
        "directory_path",
        "directory_response",
        "station_metadata",
        "archive_catalog",
    )
    if any(raw_manifest.get(key) != manifest.get(key) for key in stable_keys):
        raise RuntimeError("raw and normalized catalog manifests disagree")
    current_contract_sha = _sha256(_canonical_json(_source_contract(selected)))
    if manifest.get("source_contract_sha256") != current_contract_sha:
        raise RuntimeError("catalog source contract changed")
    if _sha256(_canonical_json(_catalog_identity_from_manifest(manifest))) != generation:
        raise RuntimeError("catalog generation SHA-256 is not reproducible")
    try:
        catalog = pl.read_parquet(directory / "archive_catalog.parquet")
    except (OSError, pl.exceptions.PolarsError) as exc:
        raise RuntimeError(f"catalog normalized generation is unreadable: {directory}") from exc
    if catalog.schema != pl.Schema(CATALOG_SCHEMA):
        raise RuntimeError("catalog normalized schema changed")
    catalog_summary = _mapping(manifest.get("archive_catalog"), label="catalog archive_catalog")
    present = catalog.filter(pl.col("archive_present"))
    if (
        catalog.height != catalog_summary.get("rows")
        or present.height != catalog_summary.get("present")
        or catalog.height - present.height != catalog_summary.get("absent")
        or as_int(present["bytes"].sum(), what="present archive bytes")
        != catalog_summary.get("present_bytes")
    ):
        raise RuntimeError("catalog normalized counts changed")
    return MicroSensorCatalogGeneration(
        generation_sha256=generation,
        directory=directory,
        catalog=catalog,
        manifest=manifest,
    )


def _parse_day(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ConfigError("micro-sensor day must use YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise ConfigError("micro-sensor day must use YYYY-MM-DD")
    return parsed


def select_observation_archives(
    generation: MicroSensorCatalogGeneration,
    *,
    day: str,
    source: MicroSensorSource | None = None,
) -> tuple[MicroSensorArchive, ...]:
    """Select a complete configured variable set without treating absence as zero."""
    selected_source = source or load_micro_sensor_source()
    parsed_day = _parse_day(day)
    month = _mapping(
        generation.manifest.get("archive_catalog"),
        label="catalog archive_catalog",
    ).get("month")
    if month != parsed_day.strftime("%Y%m"):
        raise ConfigError("micro-sensor day is outside the catalogue month")
    rows = generation.catalog.filter(pl.col("date") == parsed_day)
    archives: list[MicroSensorArchive] = []
    for variable in selected_source.required_variables:
        match = rows.filter(pl.col("variable") == variable)
        if match.height != 1:
            raise RuntimeError(f"catalogue has no unique row for {day} {variable}")
        record = match.row(0, named=True)
        if record["archive_present"] is not True:
            raise ConfigError(f"micro-sensor {variable} archive is absent for {day}")
        filename = record["filename"]
        provider_path = record["path"]
        declared_bytes = record["bytes"]
        modified_unix = record["modified_unix"]
        if (
            not isinstance(filename, str)
            or not filename
            or not isinstance(provider_path, str)
            or not provider_path
            or isinstance(declared_bytes, bool)
            or not isinstance(declared_bytes, int)
            or declared_bytes <= 0
            or isinstance(modified_unix, bool)
            or not isinstance(modified_unix, int)
            or modified_unix < 0
        ):
            raise RuntimeError(f"catalogue metadata is invalid for {day} {variable}")
        if declared_bytes > selected_source.max_archive_bytes:
            raise ConfigError(
                f"micro-sensor {variable} archive exceeds the per-archive byte ceiling"
            )
        archives.append(
            MicroSensorArchive(
                variable=variable,
                filename=filename,
                path=provider_path,
                bytes=declared_bytes,
                modified_unix=modified_unix,
            )
        )
    if sum(archive.bytes for archive in archives) > selected_source.max_total_bytes:
        raise ConfigError("micro-sensor day exceeds the total byte ceiling")
    return tuple(archives)


def inspect_observation_zip(
    path: Path,
    *,
    max_members: int,
    max_uncompressed_bytes: int,
) -> list[dict[str, object]]:
    """Validate only the ZIP directory; observation rows remain untouched."""
    if max_members <= 0 or max_uncompressed_bytes <= 0:
        raise ValueError("ZIP safety ceilings must be positive")
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise ConfigError(f"micro-sensor observation archive is not a valid ZIP: {path}") from exc
    if not infos:
        raise ConfigError("micro-sensor observation ZIP has no members")
    if len(infos) > max_members:
        raise ConfigError("micro-sensor observation ZIP exceeds the member count ceiling")
    seen: set[str] = set()
    members: list[dict[str, object]] = []
    uncompressed_total = 0
    for info in infos:
        name = info.filename
        member_path = PurePosixPath(name)
        if not name or "\\" in name or member_path.is_absolute() or ".." in member_path.parts:
            raise ConfigError("micro-sensor observation ZIP has an unsafe member path")
        if info.is_dir():
            raise ConfigError("micro-sensor observation ZIP has a directory member")
        if name in seen:
            raise ConfigError("micro-sensor observation ZIP has a duplicate member path")
        seen.add(name)
        if info.flag_bits & 0x1:
            raise ConfigError("micro-sensor observation ZIP has an encrypted member")
        uncompressed_total += info.file_size
        if uncompressed_total > max_uncompressed_bytes:
            raise ConfigError("micro-sensor observation ZIP exceeds the uncompressed byte ceiling")
        members.append(
            {
                "name": name,
                "compressed_bytes": info.compress_size,
                "uncompressed_bytes": info.file_size,
                "crc32": info.CRC,
                "compression_method": info.compress_type,
            }
        )
    return sorted(members, key=lambda member: str(member["name"]))


def _archive_dict(archive: MicroSensorArchive) -> dict[str, object]:
    return {
        "variable": archive.variable,
        "filename": archive.filename,
        "path": archive.path,
        "bytes": archive.bytes,
        "modified_unix": archive.modified_unix,
    }


def _observation_request_identity(
    catalog_generation: str,
    day: str,
    archives: tuple[MicroSensorArchive, ...],
    source: MicroSensorSource,
) -> dict[str, object]:
    observation_contract = _observation_contract(source)
    return {
        "schema_version": 1,
        "catalog_generation_sha256": catalog_generation,
        "date": day,
        "source_contract_sha256": _sha256(_canonical_json(_source_contract(source))),
        "observation_contract": observation_contract,
        "observation_contract_sha256": _sha256(_canonical_json(observation_contract)),
        "selected_archives": [_archive_dict(archive) for archive in archives],
    }


def _observation_generation_identity(manifest: dict[str, Any]) -> dict[str, object]:
    return {
        "schema_version": manifest.get("schema_version"),
        "request_sha256": manifest.get("request_sha256"),
        "catalog_generation_sha256": manifest.get("catalog_generation_sha256"),
        "date": manifest.get("date"),
        "source_contract_sha256": manifest.get("source_contract_sha256"),
        "observation_contract_sha256": manifest.get("observation_contract_sha256"),
        "selected_archives": manifest.get("selected_archives"),
        "members": manifest.get("members"),
    }


def _validate_observation_generation(
    directory: Path,
    *,
    request_identity: dict[str, object],
    request_sha256: str,
    source: MicroSensorSource,
    require_directory_name: bool = True,
) -> dict[str, Any]:
    manifest = _manifest(directory / "manifest.json")
    if manifest.get("kind") != "raw_micro_sensor_observation_day":
        raise RuntimeError(f"micro-sensor observation generation kind is invalid: {directory}")
    if manifest.get("request_sha256") != request_sha256:
        raise RuntimeError("micro-sensor observation request identity changed")
    for key, expected in request_identity.items():
        if manifest.get(key) != expected:
            raise RuntimeError(f"micro-sensor observation manifest changed: {key}")
    members = _mapping(manifest.get("members"), label="micro-sensor observation members")
    selected_archives = request_identity.get("selected_archives")
    if not isinstance(selected_archives, list) or not all(
        isinstance(record, dict) and isinstance(record.get("filename"), str)
        for record in selected_archives
    ):
        raise RuntimeError("micro-sensor observation request archives are invalid")
    selected_names = {str(record["filename"]) for record in selected_archives}
    selected_by_name = {str(record["filename"]): record for record in selected_archives}
    if set(members) != selected_names:
        raise RuntimeError("micro-sensor observation member names changed")
    if {path.name for path in directory.iterdir()} != {"manifest.json", *selected_names}:
        raise RuntimeError("micro-sensor observation generation members changed")
    for name, recorded in members.items():
        identity = _mapping(recorded, label=f"micro-sensor observation member {name}")
        path = directory / name
        current = _member_identity(path)
        if any(identity.get(key) != current[key] for key in ("bytes", "sha256")):
            raise RuntimeError(f"micro-sensor observation checksum changed: {path}")
        if identity.get("bytes") != selected_by_name[name].get("bytes"):
            raise RuntimeError(f"micro-sensor observation byte count changed: {path}")
        zip_members = inspect_observation_zip(
            path,
            max_members=source.max_zip_members,
            max_uncompressed_bytes=source.max_uncompressed_bytes_per_archive,
        )
        if identity.get("zip_members") != zip_members:
            raise RuntimeError(f"micro-sensor observation ZIP metadata changed: {path}")
    generation = _sha256(_canonical_json(_observation_generation_identity(manifest)))
    if manifest.get("generation_sha256") != generation or (
        require_directory_name and directory.name != generation
    ):
        raise RuntimeError("micro-sensor observation generation identity changed")
    return manifest


def _existing_observation_generation(
    root: Path,
    *,
    request_identity: dict[str, object],
    request_sha256: str,
    source: MicroSensorSource,
) -> MicroSensorDayWrite | None:
    if not root.exists():
        return None
    matches: list[Path] = []
    for candidate in root.iterdir():
        if not candidate.is_dir() or re.fullmatch(r"[0-9a-f]{64}", candidate.name) is None:
            continue
        try:
            manifest = _manifest(candidate / "manifest.json")
        except RuntimeError:
            continue
        if manifest.get("request_sha256") == request_sha256:
            matches.append(candidate)
    if len(matches) > 1:
        raise RuntimeError("multiple micro-sensor observation generations claim one request")
    if not matches:
        return None
    manifest = _validate_observation_generation(
        matches[0],
        request_identity=request_identity,
        request_sha256=request_sha256,
        source=source,
    )
    return MicroSensorDayWrite(
        generation_sha256=str(manifest["generation_sha256"]),
        directory=matches[0],
        manifest=manifest,
    )


def acquire_micro_sensor_day(
    catalog_generation_sha256: str,
    *,
    day: str,
    backend: MicroSensorObservationBackend,
    source: MicroSensorSource | None = None,
    raw_catalog_root: Path | None = None,
    interim_catalog_root: Path | None = None,
    observation_root: Path | None = None,
    generated_at: str | None = None,
    git_sha: str | None = None,
    git_dirty: bool | None = None,
) -> MicroSensorDayWrite:
    """Download one complete day into an immutable raw-only generation."""
    selected_source = source or load_micro_sensor_source()
    catalog_generation = load_catalog_generation(
        catalog_generation_sha256,
        source=selected_source,
        raw_root=raw_catalog_root,
        interim_root=interim_catalog_root,
    )
    archives = select_observation_archives(
        catalog_generation,
        day=day,
        source=selected_source,
    )
    request_identity = _observation_request_identity(
        catalog_generation.generation_sha256,
        day,
        archives,
        selected_source,
    )
    request_sha = _sha256(_canonical_json(request_identity))
    root = observation_root or raw_dir("micro_sensors") / "observations" / "generations"
    existing = _existing_observation_generation(
        root,
        request_identity=request_identity,
        request_sha256=request_sha,
        source=selected_source,
    )
    if existing is not None:
        return existing

    root.mkdir(parents=True, exist_ok=True)
    staged = root / f".staging-{request_sha}"
    if staged.exists():
        if not staged.is_dir():
            raise RuntimeError(
                f"micro-sensor observation staging path is not a directory: {staged}"
            )
        shutil.rmtree(staged)
    staged.mkdir()
    try:
        member_records: dict[str, dict[str, object]] = {}
        for archive in archives:
            destination = staged / archive.filename
            backend.download_archive(
                archive.path,
                destination,
                expected_bytes=archive.bytes,
                max_bytes=selected_source.max_archive_bytes,
                allowed_content_types=frozenset(selected_source.allowed_archive_content_types),
            )
            member_records[archive.filename] = {
                **_member_identity(destination),
                "zip_members": inspect_observation_zip(
                    destination,
                    max_members=selected_source.max_zip_members,
                    max_uncompressed_bytes=selected_source.max_uncompressed_bytes_per_archive,
                ),
            }
        measured_sha, measured_dirty = git_state()
        manifest: dict[str, Any] = {
            **request_identity,
            "kind": "raw_micro_sensor_observation_day",
            "request_sha256": request_sha,
            "members": member_records,
            "generated_at": generated_at or datetime.now(UTC).isoformat(timespec="seconds"),
            "git_sha": measured_sha if git_sha is None else git_sha,
            "git_dirty": measured_dirty if git_dirty is None else git_dirty,
        }
        generation = _sha256(_canonical_json(_observation_generation_identity(manifest)))
        manifest["generation_sha256"] = generation
        _write_json(staged / "manifest.json", manifest)
        destination = root / generation
        _validate_observation_generation(
            staged,
            request_identity=request_identity,
            request_sha256=request_sha,
            source=selected_source,
            require_directory_name=False,
        )
        if destination.exists():
            existing_manifest = _validate_observation_generation(
                destination,
                request_identity=request_identity,
                request_sha256=request_sha,
                source=selected_source,
            )
            shutil.rmtree(staged)
            return MicroSensorDayWrite(
                generation_sha256=generation,
                directory=destination,
                manifest=existing_manifest,
            )
        staged.replace(destination)
    except BaseException:
        if staged.exists():
            shutil.rmtree(staged)
        raise
    final_manifest = _validate_observation_generation(
        destination,
        request_identity=request_identity,
        request_sha256=request_sha,
        source=selected_source,
    )
    return MicroSensorDayWrite(
        generation_sha256=generation,
        directory=destination,
        manifest=final_manifest,
    )
