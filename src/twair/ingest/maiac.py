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
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

import polars as pl

from twair.config import ConfigError, load_conf
from twair.paths import interim_dir

__all__ = [
    "ExportEntry",
    "ExportLedger",
    "MaiacConfig",
    "load_maiac_config",
    "plan_exports",
    "read_export_ledger",
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
    station_hash = _canonical_sha256(
        placed.select("station_name", "lon", "lat").to_dicts()
    )
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


def _validate_export_ledger(ledger: ExportLedger) -> None:
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
    if ledger.stations_with_coordinates + ledger.stations_without_coordinates != ledger.stations_total:
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
        if not isinstance(entry.month, int) or isinstance(entry.month, bool) or not 1 <= entry.month <= 12:
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
                raise RuntimeError(f"MAIAC export entry {field_name} must be non-empty when present")
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
    _validate_export_ledger(ledger)
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
    _validate_export_ledger(merged)
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
    _validate_export_ledger(combined)

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
