"""Plan resumable MAIAC exports without confusing AOD with PM2.5.

MAIAC is too expensive for the interactive station-month path used by the
Sentinel-5P acquisition. This module therefore keeps local intent, remote task
state, and downloaded values as separate contracts. Creating a plan has no
Earth Engine side effect; task submission remains an explicit later boundary.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

import polars as pl

from twair.config import ConfigError, load_conf
from twair.paths import interim_dir

__all__ = [
    "EarthEngineMaiacBackend",
    "ExportEntry",
    "ExportLedger",
    "MaiacConfig",
    "MaiacTaskBackend",
    "PreparedMaiacTask",
    "RemoteTask",
    "load_maiac_config",
    "plan_exports",
    "read_export_ledger",
    "refresh_export_status",
    "submit_exports",
    "validate_export_ledger",
    "write_export_ledger",
]


_DESCRIPTION_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TASK_STATES = {
    "PLANNED",
    "READY",
    "RUNNING",
    "COMPLETED",
    "FAILED",
    "CANCEL_REQUESTED",
    "CANCELLED",
    "UNKNOWN",
}
_REMOTE_TASK_STATES = _TASK_STATES - {"PLANNED", "UNKNOWN"}


@dataclass(frozen=True, slots=True)
class MaiacConfig:
    collection_id: str
    aod_band: str
    qa_band: str
    unit: str
    sample_scale_m: int
    scale_factor: float
    qa_shift: int
    qa_mask: int
    qa_best: int
    tile_scale: int
    tiles: tuple[str, str]
    drive_folder: str
    description_prefix: str
    max_active_tasks: int


@dataclass(slots=True)
class ExportEntry:
    year: int
    month: int
    description: str
    file_name_prefix: str
    state: str
    task_id: str | None
    error_message: str | None
    planned_at: str
    submitted_at: str | None
    updated_at: str | None


@dataclass(slots=True)
class ExportLedger:
    schema_version: int
    planned_at: str
    gee_project: str
    year: int
    months: list[int]
    stations_total: int
    stations_with_coordinates: int
    stations_without_coordinates: int
    station_inventory_sha256: str
    source_contract_sha256: str
    source_contract: dict[str, object]
    entries: list[ExportEntry]

    @property
    def default_path(self) -> Path:
        return interim_dir("maiac") / f"year={self.year}" / "export-ledger.json"


@dataclass(frozen=True, slots=True)
class RemoteTask:
    task_id: str
    description: str
    state: str
    error_message: str | None


class PreparedMaiacTask(Protocol):
    def start(self) -> RemoteTask: ...


class MaiacTaskBackend(Protocol):
    def list_tasks(self) -> list[RemoteTask]: ...

    def prepare_task(
        self,
        entry: ExportEntry,
        config: MaiacConfig,
        station_frame: pl.DataFrame,
    ) -> PreparedMaiacTask: ...


def _required_text(group: dict[str, Any], field: str) -> str:
    value = group.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"satellite.maiac.{field} must be a non-empty string")
    return value.strip()


def _exact_int(
    group: dict[str, Any],
    field: str,
    *,
    positive: bool,
) -> int:
    value = group.get(field)
    requirement = "positive integer" if positive else "non-negative integer"
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or (positive and value <= 0)
        or (not positive and value < 0)
    ):
        raise ConfigError(f"satellite.maiac.{field} must be a {requirement}")
    return value


def load_maiac_config(config: dict[str, Any] | None = None) -> MaiacConfig:
    raw = config if config is not None else load_conf("satellite")
    group = raw.get("maiac")
    if not isinstance(group, dict) or not group:
        raise ConfigError("conf/satellite.yaml must define a non-empty `maiac` mapping")

    collection_id = _required_text(group, "collection_id")
    aod_band = _required_text(group, "aod_band")
    qa_band = _required_text(group, "qa_band")
    unit = _required_text(group, "unit")
    drive_folder = _required_text(group, "drive_folder")
    description_prefix = _required_text(group, "description_prefix")
    if not _DESCRIPTION_RE.fullmatch(description_prefix):
        raise ConfigError(
            "satellite.maiac.description_prefix may contain only letters, numbers, "
            "hyphens, or underscores"
        )

    sample_scale_m = _exact_int(group, "sample_scale_m", positive=True)
    qa_shift = _exact_int(group, "qa_shift", positive=False)
    qa_mask = _exact_int(group, "qa_mask", positive=True)
    qa_best = _exact_int(group, "qa_best", positive=False)
    if qa_best > qa_mask:
        raise ConfigError("satellite.maiac.qa_best must fit within qa_mask")
    tile_scale = _exact_int(group, "tile_scale", positive=True)
    max_active_tasks = group.get("max_active_tasks")
    if (
        not isinstance(max_active_tasks, int)
        or isinstance(max_active_tasks, bool)
        or max_active_tasks not in {1, 2}
    ):
        raise ConfigError("satellite.maiac.max_active_tasks must be an integer from 1 through 2")

    scale_factor = group.get("scale_factor")
    if (
        isinstance(scale_factor, bool)
        or not isinstance(scale_factor, (int, float))
        or not math.isfinite(float(scale_factor))
        or float(scale_factor) <= 0
    ):
        raise ConfigError("satellite.maiac.scale_factor must be a positive finite number")

    raw_tiles = group.get("tiles")
    if (
        not isinstance(raw_tiles, list)
        or len(raw_tiles) != 2
        or any(not isinstance(tile, str) or not tile.strip() for tile in raw_tiles)
        or len({tile.strip() for tile in raw_tiles}) != 2
    ):
        raise ConfigError("satellite.maiac.tiles must contain two unique non-empty strings")
    tiles = (raw_tiles[0].strip(), raw_tiles[1].strip())

    return MaiacConfig(
        collection_id=collection_id,
        aod_band=aod_band,
        qa_band=qa_band,
        unit=unit,
        sample_scale_m=sample_scale_m,
        scale_factor=float(scale_factor),
        qa_shift=qa_shift,
        qa_mask=qa_mask,
        qa_best=qa_best,
        tile_scale=tile_scale,
        tiles=tiles,
        drive_folder=drive_folder,
        description_prefix=description_prefix,
        max_active_tasks=max_active_tasks,
    )


def _placed_stations(stations: pl.DataFrame) -> tuple[pl.DataFrame, int]:
    required = {"station_name", "lon", "lat"}
    missing = required - set(stations.columns)
    if missing:
        raise RuntimeError(f"station table is missing {sorted(missing)}")
    invalid_names = stations.filter(
        pl.col("station_name").is_null()
        | (pl.col("station_name").cast(pl.String).str.strip_chars() == "")
    )
    if not invalid_names.is_empty():
        raise RuntimeError("station names must be non-empty strings")
    duplicates = stations.group_by("station_name").len().filter(pl.col("len") > 1)
    if not duplicates.is_empty():
        raise RuntimeError(f"station names are not unique: {duplicates['station_name'].to_list()}")

    bad = stations.filter(
        (pl.col("lon").is_not_null() & ~pl.col("lon").cast(pl.Float64).is_finite())
        | (pl.col("lat").is_not_null() & ~pl.col("lat").cast(pl.Float64).is_finite())
    )
    if not bad.is_empty():
        raise RuntimeError(f"station coordinates are not finite: {bad['station_name'].to_list()}")
    placed = stations.filter(pl.col("lon").is_not_null() & pl.col("lat").is_not_null()).select(
        pl.col("station_name").cast(pl.String),
        pl.col("lon").cast(pl.Float64),
        pl.col("lat").cast(pl.Float64),
    )
    if placed.is_empty():
        raise RuntimeError("no stations have coordinates")
    return placed.sort("station_name"), stations.height - placed.height


def _canonical_sha256(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _source_contract(config: MaiacConfig) -> dict[str, object]:
    contract = asdict(config)
    contract["tiles"] = list(config.tiles)
    return contract


def plan_exports(
    stations: pl.DataFrame,
    *,
    project: str,
    year: int,
    months: tuple[int, ...],
    config: MaiacConfig | None = None,
    planned_at: str | None = None,
) -> ExportLedger:
    if not isinstance(project, str) or not project.strip():
        raise ValueError("project must be a non-empty string")
    if not isinstance(year, int) or isinstance(year, bool) or year <= 0:
        raise ValueError("year must be a positive integer")
    if (
        not months
        or any(not isinstance(month, int) or isinstance(month, bool) for month in months)
        or any(month < 1 or month > 12 for month in months)
        or len(set(months)) != len(months)
    ):
        raise ValueError("months must be unique integers from 1 through 12")

    selected_config = config or load_maiac_config()
    placed, unplaced_count = _placed_stations(stations)
    station_hash = _canonical_sha256(placed.select("station_name", "lon", "lat").to_dicts())
    source_contract = _source_contract(selected_config)
    source_hash = _canonical_sha256(source_contract)
    timestamp = planned_at or datetime.now(UTC).isoformat(timespec="seconds")
    entries = []
    for month in sorted(months):
        description = (
            f"{selected_config.description_prefix}_{year}_{month:02d}_"
            f"{source_hash[:12]}_{station_hash[:12]}"
        )
        entries.append(
            ExportEntry(
                year=year,
                month=month,
                description=description,
                file_name_prefix=description,
                state="PLANNED",
                task_id=None,
                error_message=None,
                planned_at=timestamp,
                submitted_at=None,
                updated_at=None,
            )
        )
    return ExportLedger(
        schema_version=1,
        planned_at=timestamp,
        gee_project=project.strip(),
        year=year,
        months=[entry.month for entry in entries],
        stations_total=stations.height,
        stations_with_coordinates=placed.height,
        stations_without_coordinates=unplaced_count,
        station_inventory_sha256=station_hash,
        source_contract_sha256=source_hash,
        source_contract=source_contract,
        entries=entries,
    )


def _copy_entry(entry: ExportEntry) -> ExportEntry:
    return ExportEntry(**asdict(entry))


def _copy_ledger(ledger: ExportLedger) -> ExportLedger:
    return ExportLedger(
        schema_version=ledger.schema_version,
        planned_at=ledger.planned_at,
        gee_project=ledger.gee_project,
        year=ledger.year,
        months=list(ledger.months),
        stations_total=ledger.stations_total,
        stations_with_coordinates=ledger.stations_with_coordinates,
        stations_without_coordinates=ledger.stations_without_coordinates,
        station_inventory_sha256=ledger.station_inventory_sha256,
        source_contract_sha256=ledger.source_contract_sha256,
        source_contract=dict(ledger.source_contract),
        entries=[_copy_entry(entry) for entry in ledger.entries],
    )


def validate_export_ledger(ledger: ExportLedger) -> None:
    if ledger.schema_version != 1:
        raise RuntimeError("unsupported MAIAC export ledger schema version")
    if not isinstance(ledger.gee_project, str) or not ledger.gee_project.strip():
        raise RuntimeError("MAIAC export ledger project must be non-empty")
    if not isinstance(ledger.year, int) or isinstance(ledger.year, bool) or ledger.year <= 0:
        raise RuntimeError("MAIAC export ledger year must be a positive integer")
    if (
        not isinstance(ledger.months, list)
        or not ledger.months
        or any(not isinstance(month, int) or isinstance(month, bool) for month in ledger.months)
        or any(month < 1 or month > 12 for month in ledger.months)
        or ledger.months != sorted(set(ledger.months))
    ):
        raise RuntimeError("MAIAC export ledger months must be sorted unique integers")
    counts = (
        ledger.stations_total,
        ledger.stations_with_coordinates,
        ledger.stations_without_coordinates,
    )
    if any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in counts):
        raise RuntimeError("MAIAC export ledger station counts must be non-negative integers")
    if ledger.stations_with_coordinates <= 0:
        raise RuntimeError("MAIAC export ledger must contain coordinate-bearing stations")
    if (
        ledger.stations_with_coordinates + ledger.stations_without_coordinates
        != ledger.stations_total
    ):
        raise RuntimeError("MAIAC export ledger station counts are inconsistent")
    if not _SHA256_RE.fullmatch(ledger.station_inventory_sha256):
        raise RuntimeError("MAIAC export ledger station inventory hash is invalid")
    if not _SHA256_RE.fullmatch(ledger.source_contract_sha256):
        raise RuntimeError("MAIAC export ledger source contract hash is invalid")
    if _canonical_sha256(ledger.source_contract) != ledger.source_contract_sha256:
        raise RuntimeError("MAIAC export ledger source contract hash is inconsistent")
    if not isinstance(ledger.planned_at, str) or not ledger.planned_at:
        raise RuntimeError("MAIAC export ledger planned timestamp is missing")

    entry_months: list[int] = []
    for entry in ledger.entries:
        if entry.year != ledger.year:
            raise RuntimeError("MAIAC export entry year is inconsistent")
        if (
            not isinstance(entry.month, int)
            or isinstance(entry.month, bool)
            or not 1 <= entry.month <= 12
        ):
            raise RuntimeError("MAIAC export entry month must be an integer from 1 through 12")
        if entry.state not in _TASK_STATES:
            raise RuntimeError(f"unsupported MAIAC task state: {entry.state!r}")
        if not _DESCRIPTION_RE.fullmatch(entry.description):
            raise RuntimeError("MAIAC export description is not Earth Engine safe")
        if entry.file_name_prefix != entry.description:
            raise RuntimeError("MAIAC filename prefix must match its deterministic description")
        if not isinstance(entry.planned_at, str) or not entry.planned_at:
            raise RuntimeError("MAIAC export entry planned timestamp is missing")
        for field_name, value in (
            ("task id", entry.task_id),
            ("error message", entry.error_message),
            ("submitted timestamp", entry.submitted_at),
            ("updated timestamp", entry.updated_at),
        ):
            if value is not None and (not isinstance(value, str) or not value):
                raise RuntimeError(
                    f"MAIAC export entry {field_name} must be non-empty when present"
                )
        if entry.state != "PLANNED" and entry.task_id is None:
            raise RuntimeError("a remote MAIAC task state requires a task id")
        entry_months.append(entry.month)
    if entry_months != ledger.months:
        raise RuntimeError("MAIAC export entries must match the ledger months in order")


def _ledger_from_payload(payload: object) -> ExportLedger:
    if not isinstance(payload, dict):
        raise RuntimeError("MAIAC export ledger must be a JSON object")
    raw: dict[str, Any] = payload
    entries_payload = raw.get("entries")
    if not isinstance(entries_payload, list):
        raise RuntimeError("MAIAC export ledger entries must be a list")
    entries: list[ExportEntry] = []
    try:
        for item in entries_payload:
            if not isinstance(item, dict):
                raise RuntimeError("MAIAC export ledger entry must be a JSON object")
            entries.append(ExportEntry(**item))
        source_contract = raw["source_contract"]
        months = raw["months"]
        if not isinstance(source_contract, dict):
            raise RuntimeError("MAIAC export ledger source contract must be a mapping")
        if not isinstance(months, list):
            raise RuntimeError("MAIAC export ledger months must be a list")
        ledger = ExportLedger(
            schema_version=raw["schema_version"],
            planned_at=raw["planned_at"],
            gee_project=raw["gee_project"],
            year=raw["year"],
            months=months,
            stations_total=raw["stations_total"],
            stations_with_coordinates=raw["stations_with_coordinates"],
            stations_without_coordinates=raw["stations_without_coordinates"],
            station_inventory_sha256=raw["station_inventory_sha256"],
            source_contract_sha256=raw["source_contract_sha256"],
            source_contract=source_contract,
            entries=entries,
        )
    except (KeyError, TypeError) as exc:
        raise RuntimeError("MAIAC export ledger is missing a required field") from exc
    validate_export_ledger(ledger)
    return ledger


def _recover_ledger_swap(destination: Path) -> None:
    parent = destination.parent
    if not parent.exists():
        return
    backups = sorted(parent.glob(f".{destination.name}.backup-*"))
    stages = sorted(parent.glob(f".{destination.name}.staging-*"))
    if len(backups) > 1:
        raise RuntimeError(f"multiple interrupted MAIAC ledger backups found beside {destination}")
    if len(stages) > 1:
        raise RuntimeError(f"multiple interrupted MAIAC ledger stages found beside {destination}")
    if destination.exists() and backups and stages:
        raise RuntimeError(f"ambiguous interrupted MAIAC ledger swap found beside {destination}")
    if not destination.exists() and backups:
        backups[0].replace(destination)
        backups = []
    for staged in stages:
        staged.unlink()
    if destination.exists() and backups:
        backups[0].unlink()


def read_export_ledger(path: Path) -> ExportLedger:
    _recover_ledger_swap(path)
    if not path.exists():
        raise FileNotFoundError(f"MAIAC export ledger not found: {path}")
    if not path.is_file():
        raise RuntimeError(f"MAIAC export ledger is not a file: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"MAIAC export ledger is not readable JSON: {path}") from exc
    return _ledger_from_payload(payload)


def _merge_export_ledgers(existing: ExportLedger, incoming: ExportLedger) -> ExportLedger:
    for field_name, label in (
        ("gee_project", "Earth Engine project"),
        ("year", "year"),
        ("station_inventory_sha256", "station inventory"),
        ("source_contract_sha256", "source contract"),
    ):
        if getattr(existing, field_name) != getattr(incoming, field_name):
            raise RuntimeError(f"MAIAC ledger merge changed the {label}")

    by_month = {entry.month: _copy_entry(entry) for entry in existing.entries}
    for new_entry in incoming.entries:
        old_entry = by_month.get(new_entry.month)
        if old_entry is None:
            by_month[new_entry.month] = _copy_entry(new_entry)
            continue
        if old_entry.description != new_entry.description:
            raise RuntimeError(f"MAIAC ledger month {new_entry.month} changed its description")
        if old_entry.task_id and new_entry.task_id and old_entry.task_id != new_entry.task_id:
            raise RuntimeError(f"MAIAC ledger month {new_entry.month} changed its task id")
        if new_entry.state == "PLANNED" and new_entry.task_id is None:
            continue
        by_month[new_entry.month] = _copy_entry(new_entry)

    entries = [by_month[month] for month in sorted(by_month)]
    merged = ExportLedger(
        schema_version=existing.schema_version,
        planned_at=existing.planned_at,
        gee_project=existing.gee_project,
        year=existing.year,
        months=[entry.month for entry in entries],
        stations_total=incoming.stations_total,
        stations_with_coordinates=incoming.stations_with_coordinates,
        stations_without_coordinates=incoming.stations_without_coordinates,
        station_inventory_sha256=existing.station_inventory_sha256,
        source_contract_sha256=existing.source_contract_sha256,
        source_contract=dict(existing.source_contract),
        entries=entries,
    )
    validate_export_ledger(merged)
    return merged


def write_export_ledger(
    ledger: ExportLedger,
    *,
    destination: Path | None = None,
) -> Path:
    target = destination or ledger.default_path
    if not target.name or target == target.parent:
        raise RuntimeError("MAIAC ledger destination must be a named file")
    _recover_ledger_swap(target)
    existing = read_export_ledger(target) if target.exists() else None
    combined = _merge_export_ledgers(existing, ledger) if existing is not None else ledger
    validate_export_ledger(combined)

    target.parent.mkdir(parents=True, exist_ok=True)
    token = uuid4().hex
    staged = target.with_name(f".{target.name}.staging-{token}")
    backup = target.with_name(f".{target.name}.backup-{token}")
    staged.write_text(
        json.dumps(asdict(combined), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    try:
        had_existing = target.exists()
        if had_existing:
            target.replace(backup)
        try:
            staged.replace(target)
        except Exception:
            if had_existing and backup.exists():
                backup.replace(target)
            raise
        if backup.exists():
            backup.unlink()
    finally:
        if staged.exists():
            staged.unlink()
    return target


def _validate_remote_task(task: RemoteTask) -> None:
    if not isinstance(task.task_id, str) or not task.task_id:
        raise RuntimeError("remote MAIAC task id must be non-empty")
    if not isinstance(task.description, str) or not task.description:
        raise RuntimeError("remote MAIAC task description must be non-empty")
    if task.state not in _REMOTE_TASK_STATES:
        raise RuntimeError(f"unsupported remote MAIAC task state: {task.state!r}")
    if task.error_message is not None and (
        not isinstance(task.error_message, str) or not task.error_message
    ):
        raise RuntimeError("remote MAIAC task error must be non-empty when present")


def _remote_indexes(
    remote_tasks: list[RemoteTask],
    *,
    descriptions: set[str],
) -> tuple[dict[str, RemoteTask], dict[str, RemoteTask]]:
    by_id: dict[str, RemoteTask] = {}
    by_description: dict[str, RemoteTask] = {}
    duplicate_descriptions: set[str] = set()
    for task in remote_tasks:
        _validate_remote_task(task)
        if task.task_id in by_id:
            raise RuntimeError(f"duplicate remote MAIAC task id: {task.task_id}")
        by_id[task.task_id] = task
        if task.description not in descriptions:
            continue
        if task.description in by_description:
            duplicate_descriptions.add(task.description)
        by_description[task.description] = task
    if duplicate_descriptions:
        raise RuntimeError(
            f"duplicate remote MAIAC task description(s): {sorted(duplicate_descriptions)}"
        )
    return by_id, by_description


def _apply_remote_task(
    entry: ExportEntry,
    task: RemoteTask,
    *,
    updated_at: str,
    submitted_now: bool,
) -> None:
    _validate_remote_task(task)
    if entry.description != task.description:
        raise RuntimeError(f"remote task description does not match MAIAC month {entry.month}")
    if entry.task_id is not None and entry.task_id != task.task_id:
        raise RuntimeError(f"remote task id changed for MAIAC month {entry.month}")
    entry.task_id = task.task_id
    entry.state = task.state
    entry.error_message = task.error_message
    entry.updated_at = updated_at
    if submitted_now:
        entry.submitted_at = updated_at


def _validated_submission_context(
    ledger: ExportLedger,
    stations: pl.DataFrame,
    config: MaiacConfig,
) -> pl.DataFrame:
    validate_export_ledger(ledger)
    if _canonical_sha256(_source_contract(config)) != ledger.source_contract_sha256:
        raise RuntimeError("current MAIAC source contract differs from the export ledger")
    placed, unplaced_count = _placed_stations(stations)
    station_hash = _canonical_sha256(placed.select("station_name", "lon", "lat").to_dicts())
    if station_hash != ledger.station_inventory_sha256:
        raise RuntimeError("current station inventory differs from the MAIAC export ledger")
    if (
        stations.height != ledger.stations_total
        or placed.height != ledger.stations_with_coordinates
        or unplaced_count != ledger.stations_without_coordinates
    ):
        raise RuntimeError("current station counts are inconsistent")
    return placed


def refresh_export_status(
    ledger: ExportLedger,
    *,
    backend: MaiacTaskBackend,
    updated_at: str | None = None,
) -> ExportLedger:
    updated = _copy_ledger(ledger)
    timestamp = updated_at or datetime.now(UTC).isoformat(timespec="seconds")
    descriptions = {entry.description for entry in updated.entries}
    by_id, by_description = _remote_indexes(
        backend.list_tasks(),
        descriptions=descriptions,
    )
    for entry in updated.entries:
        remote = by_id.get(entry.task_id) if entry.task_id is not None else None
        if remote is None:
            remote = by_description.get(entry.description)
        if remote is not None:
            _apply_remote_task(entry, remote, updated_at=timestamp, submitted_now=False)
        elif entry.task_id is not None:
            entry.state = "UNKNOWN"
            entry.error_message = None
            entry.updated_at = timestamp
    validate_export_ledger(updated)
    return updated


def submit_exports(
    ledger: ExportLedger,
    stations: pl.DataFrame,
    *,
    backend: MaiacTaskBackend,
    confirm: bool,
    config: MaiacConfig | None = None,
    updated_at: str | None = None,
    persist: Callable[[ExportLedger], None] | None = None,
) -> ExportLedger:
    if not confirm:
        raise RuntimeError("MAIAC submission requires --confirm-drive-export")
    selected_config = config or load_maiac_config()
    placed = _validated_submission_context(ledger, stations, selected_config)
    updated = _copy_ledger(ledger)
    timestamp = updated_at or datetime.now(UTC).isoformat(timespec="seconds")
    remote_tasks = backend.list_tasks()
    descriptions = {entry.description for entry in updated.entries}
    _, by_description = _remote_indexes(remote_tasks, descriptions=descriptions)

    for entry in updated.entries:
        remote = by_description.get(entry.description)
        if remote is not None:
            _apply_remote_task(entry, remote, updated_at=timestamp, submitted_now=False)

    active = sum(task.state in {"READY", "RUNNING"} for task in remote_tasks)
    available = max(0, selected_config.max_active_tasks - active)
    if not by_description and all(entry.task_id is None for entry in updated.entries):
        # Drive creates a named export folder lazily.  Two first tasks completed
        # together in the 2025 run and raced into two same-name folders, so the
        # first task must establish the folder before parallel submission begins.
        available = min(available, 1)
    for entry in updated.entries:
        if available <= 0:
            break
        if entry.state != "PLANNED" or entry.task_id is not None:
            continue
        prepared = backend.prepare_task(entry, selected_config, placed)
        remote = prepared.start()
        _apply_remote_task(entry, remote, updated_at=timestamp, submitted_now=True)
        available -= 1
        if persist is not None:
            persist(_copy_ledger(updated))

    validate_export_ledger(updated)
    return updated


@dataclass(slots=True)
class _EarthEnginePreparedTask:
    task: Any
    description: str

    def start(self) -> RemoteTask:
        self.task.start()
        status: dict[str, Any] = self.task.status()
        task_id = status.get("id") or getattr(self.task, "id", None)
        description = status.get("description") or self.description
        remote = RemoteTask(
            task_id=str(task_id) if task_id is not None else "",
            description=str(description),
            state=str(status.get("state", "")),
            error_message=(
                str(status["error_message"]) if status.get("error_message") is not None else None
            ),
        )
        _validate_remote_task(remote)
        return remote


class EarthEngineMaiacBackend:
    def __init__(self, project: str, *, ee_module: Any | None = None) -> None:
        if ee_module is None:
            try:
                import ee
            except ImportError as exc:
                raise RuntimeError(
                    "earthengine-api is not installed; run `uv sync --extra earth`"
                ) from exc
            ee_module = ee
        ee_module.Initialize(project=project)
        self._ee: Any = ee_module

    def _remote_task(self, task: Any) -> RemoteTask:
        status: dict[str, Any] = task.status()
        task_id = status.get("id") or getattr(task, "id", None)
        config = getattr(task, "config", None)
        description = status.get("description")
        if description is None and isinstance(config, dict):
            description = config.get("description")
        remote = RemoteTask(
            task_id=str(task_id) if task_id is not None else "",
            description=str(description) if description is not None else "",
            state=str(status.get("state", "")),
            error_message=(
                str(status["error_message"]) if status.get("error_message") is not None else None
            ),
        )
        _validate_remote_task(remote)
        return remote

    def list_tasks(self) -> list[RemoteTask]:
        return [self._remote_task(task) for task in self._ee.batch.Task.list()]

    def prepare_task(
        self,
        entry: ExportEntry,
        config: MaiacConfig,
        station_frame: pl.DataFrame,
    ) -> PreparedMaiacTask:
        ee = self._ee
        features = [
            ee.Feature(
                ee.Geometry.Point([row["lon"], row["lat"]]),
                {"station_name": row["station_name"]},
            )
            for row in station_frame.iter_rows(named=True)
        ]
        points = ee.FeatureCollection(features)
        tile_filter = ee.Filter.Or(
            *(ee.Filter.stringContains("system:index", tile) for tile in config.tiles)
        )
        collection = ee.ImageCollection(config.collection_id).filter(tile_filter)
        start = ee.Date.fromYMD(entry.year, entry.month, 1)
        end = start.advance(1, "month")
        monthly = collection.filterDate(start, end)
        source_images = monthly.size()

        def best_quality(image: Any) -> Any:
            selected = ee.Image(image)
            qa = selected.select(config.qa_band)
            mask = qa.rightShift(config.qa_shift).bitwiseAnd(config.qa_mask).eq(config.qa_best)
            return selected.select(config.aod_band).updateMask(mask).multiply(config.scale_factor)

        composite = monthly.map(best_quality).mean().rename("value")
        reduced = composite.reduceRegions(
            collection=points,
            reducer=ee.Reducer.first().setOutputs(["value"]),
            scale=config.sample_scale_m,
            tileScale=config.tile_scale,
        ).map(
            lambda feature: feature.set(
                {
                    "year": entry.year,
                    "month": entry.month,
                    "source_images": source_images,
                }
            )
        )
        task = ee.batch.Export.table.toDrive(
            collection=reduced,
            description=entry.description,
            folder=config.drive_folder,
            fileNamePrefix=entry.file_name_prefix,
            fileFormat="CSV",
            selectors=["station_name", "year", "month", "value", "source_images"],
        )
        return _EarthEnginePreparedTask(task=task, description=entry.description)
