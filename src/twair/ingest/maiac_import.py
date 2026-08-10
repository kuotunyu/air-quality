"""Validate downloaded MAIAC tables before they become local covariates.

An absent CSV row and a CSV row with a blank AOD cell are not interchangeable.
The former means the provider contract was not fulfilled and stops the import;
the latter is a masked observation and remains a null in the output Parquet.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import UTC, date, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

import polars as pl

from twair.ingest.maiac import (
    ExportEntry,
    ExportLedger,
    MaiacConfig,
    load_maiac_config,
    plan_exports,
    validate_export_ledger,
)
from twair.ingest.station_inventory import (
    maiac_generation_result_dir,
    validate_generation_sha256,
)
from twair.paths import interim_dir
from twair.provenance import git_state
from twair.scalars import as_int

__all__ = [
    "MaiacResult",
    "import_exported_files",
    "read_maiac_result",
    "write_maiac_result",
]


VALUE_SCHEMA = {
    "station_name": pl.String,
    "month": pl.Date,
    "source": pl.String,
    "value": pl.Float64,
    "unit": pl.String,
    "collection_id": pl.String,
    "band": pl.String,
    "sample_scale_m": pl.Int32,
    "qa_rule": pl.String,
    "scale_factor": pl.Float64,
}

COVERAGE_SCHEMA = {
    "source": pl.String,
    "month": pl.Date,
    "source_images": pl.Int64,
    "n_stations": pl.Int64,
    "n_valid": pl.Int64,
    "n_null": pl.Int64,
}

CSV_COLUMNS = ["station_name", "year", "month", "value", "source_images"]

OUTPUT_FILENAMES = {
    "values": "maiac_station_month.parquet",
    "coverage": "maiac_coverage.parquet",
    "manifest": "manifest.json",
}


@dataclass(frozen=True, slots=True)
class MaiacResult:
    values: pl.DataFrame
    coverage: pl.DataFrame
    manifest: dict[str, Any]


def _canonical_sha256(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _selected_entries(
    ledger: ExportLedger,
    months: tuple[int, ...] | None,
) -> list[ExportEntry]:
    selected = tuple(ledger.months) if months is None else months
    if (
        not selected
        or any(not isinstance(month, int) or isinstance(month, bool) for month in selected)
        or any(month < 1 or month > 12 for month in selected)
        or len(set(selected)) != len(selected)
    ):
        raise ValueError("months must be unique integers from 1 through 12")
    by_month = {entry.month: entry for entry in ledger.entries}
    unexpected = set(selected) - set(by_month)
    if unexpected:
        raise RuntimeError(f"MAIAC ledger does not plan month(s) {sorted(unexpected)}")
    entries = [by_month[month] for month in sorted(selected)]
    for entry in entries:
        if entry.state != "COMPLETED":
            raise RuntimeError(f"MAIAC month {entry.month} is {entry.state}, not COMPLETED")
        if entry.task_id is None:
            raise RuntimeError(f"completed MAIAC month {entry.month} has no task id")
    return entries


def _validate_current_contract(
    ledger: ExportLedger,
    stations: pl.DataFrame,
    config: MaiacConfig,
) -> None:
    current = plan_exports(
        stations,
        project=ledger.gee_project,
        year=ledger.year,
        months=tuple(ledger.months),
        config=config,
        planned_at=ledger.planned_at,
        inventory_generation=ledger.inventory_generation_sha256 is not None,
    )
    if current.station_inventory_sha256 != ledger.station_inventory_sha256:
        raise RuntimeError("current station inventory differs from the MAIAC export ledger")
    if current.source_contract_sha256 != ledger.source_contract_sha256:
        raise RuntimeError("current source contract differs from the MAIAC export ledger")
    if (
        current.stations_total != ledger.stations_total
        or current.stations_with_coordinates != ledger.stations_with_coordinates
        or current.stations_without_coordinates != ledger.stations_without_coordinates
    ):
        raise RuntimeError("current station counts differ from the MAIAC export ledger")


def _discover_csv(source_dir: Path, entry: ExportEntry) -> Path:
    matches = sorted(source_dir.glob(f"{entry.file_name_prefix}*.csv"))
    expected = source_dir / f"{entry.file_name_prefix}.csv"
    if matches != [expected]:
        raise RuntimeError(
            f"MAIAC month {entry.month} requires exactly one CSV named {expected.name}; "
            f"found {[path.name for path in matches]}"
        )
    return expected


def _read_month_csv(
    path: Path,
    entry: ExportEntry,
    *,
    expected_stations: set[str],
) -> tuple[pl.DataFrame, int]:
    try:
        frame = pl.read_csv(
            path,
            null_values=[""],
            schema_overrides={
                "station_name": pl.String,
                "year": pl.Int64,
                "month": pl.Int64,
                "value": pl.Float64,
                "source_images": pl.Int64,
            },
        )
    except (OSError, pl.exceptions.PolarsError) as exc:
        raise RuntimeError(f"MAIAC CSV cannot be parsed: {path.name}") from exc
    if frame.columns != CSV_COLUMNS:
        raise RuntimeError(f"MAIAC CSV must contain exactly the selectors {CSV_COLUMNS} in order")
    if frame["station_name"].null_count():
        raise RuntimeError(f"MAIAC month {entry.month} contains a null station name")

    duplicates = frame.group_by("station_name").len().filter(pl.col("len") > 1)
    if not duplicates.is_empty():
        raise RuntimeError(
            f"MAIAC month {entry.month} has duplicate station row(s): "
            f"{duplicates['station_name'].to_list()}"
        )
    actual_stations = set(frame["station_name"].to_list())
    missing = expected_stations - actual_stations
    unexpected = actual_stations - expected_stations
    if missing:
        raise RuntimeError(
            f"MAIAC month {entry.month} is missing {len(missing)} expected station row(s)"
        )
    if unexpected:
        raise RuntimeError(
            f"MAIAC month {entry.month} has {len(unexpected)} unexpected station row(s)"
        )
    if frame.filter(pl.col("year") != entry.year).height:
        raise RuntimeError(f"MAIAC CSV month {entry.month} must contain year {entry.year}")
    if frame.filter(pl.col("month") != entry.month).height:
        raise RuntimeError(f"MAIAC CSV must contain month {entry.month}")
    if frame["source_images"].null_count() or frame.filter(pl.col("source_images") < 0).height:
        raise RuntimeError("MAIAC source_images must be one non-negative integer")
    if frame["source_images"].n_unique() != 1:
        raise RuntimeError("MAIAC source_images must be identical within a month")
    nonfinite = frame.filter(pl.col("value").is_not_null() & ~pl.col("value").is_finite())
    if not nonfinite.is_empty():
        raise RuntimeError("MAIAC values must be finite or blank")
    source_images = as_int(frame["source_images"][0], what="MAIAC source image count")
    return frame.sort("station_name"), source_images


def _qa_rule(config: MaiacConfig) -> str:
    return f"({config.qa_band} >> {config.qa_shift}) & {config.qa_mask} == {config.qa_best}"


def import_exported_files(
    ledger: ExportLedger,
    stations: pl.DataFrame,
    *,
    source_dir: Path,
    months: tuple[int, ...] | None = None,
    config: MaiacConfig | None = None,
    imported_at: str | None = None,
) -> MaiacResult:
    validate_export_ledger(ledger)
    selected_config = config or load_maiac_config()
    _validate_current_contract(ledger, stations, selected_config)
    entries = _selected_entries(ledger, months)
    if not source_dir.is_dir():
        raise RuntimeError(f"MAIAC CSV source directory not found: {source_dir}")

    current_plan = plan_exports(
        stations,
        project=ledger.gee_project,
        year=ledger.year,
        months=tuple(ledger.months),
        config=selected_config,
        planned_at=ledger.planned_at,
        inventory_generation=ledger.inventory_generation_sha256 is not None,
    )
    expected_stations = {
        row["station_name"]
        for row in stations.filter(pl.col("lon").is_not_null() & pl.col("lat").is_not_null())
        .select(pl.col("station_name").cast(pl.String))
        .iter_rows(named=True)
    }
    timestamp = imported_at or datetime.now(UTC).isoformat(timespec="seconds")
    sha, dirty = git_state()
    value_frames: list[pl.DataFrame] = []
    coverage_records: list[dict[str, object]] = []
    tasks: dict[str, str] = {}
    input_files: dict[str, dict[str, str]] = {}
    for entry in entries:
        path = _discover_csv(source_dir, entry)
        frame, source_images = _read_month_csv(
            path,
            entry,
            expected_stations=expected_stations,
        )
        values = (
            frame.select("station_name", "value")
            .with_columns(
                pl.lit(date(entry.year, entry.month, 1)).cast(pl.Date).alias("month"),
                pl.lit("maiac_aod").alias("source"),
                pl.lit(selected_config.unit).alias("unit"),
                pl.lit(selected_config.collection_id).alias("collection_id"),
                pl.lit(selected_config.aod_band).alias("band"),
                pl.lit(selected_config.sample_scale_m).cast(pl.Int32).alias("sample_scale_m"),
                pl.lit(_qa_rule(selected_config)).alias("qa_rule"),
                pl.lit(selected_config.scale_factor).cast(pl.Float64).alias("scale_factor"),
            )
            .select(*VALUE_SCHEMA)
        )
        value_frames.append(values.cast(pl.Schema(VALUE_SCHEMA)))
        n_valid = values["value"].len() - values["value"].null_count()
        coverage_records.append(
            {
                "source": "maiac_aod",
                "month": date(entry.year, entry.month, 1),
                "source_images": source_images,
                "n_stations": values.height,
                "n_valid": n_valid,
                "n_null": values["value"].null_count(),
            }
        )
        month_key = str(entry.month)
        tasks[month_key] = entry.task_id or ""
        input_files[month_key] = {
            "name": path.name,
            "sha256": sha256(path.read_bytes()).hexdigest(),
        }

    values = pl.concat(value_frames).sort("month", "station_name")
    coverage = pl.from_dicts(coverage_records, schema=COVERAGE_SCHEMA).sort("month")
    imported_months = [entry.month for entry in entries]
    import_run = {
        "imported_at": timestamp,
        "git_sha": sha,
        "git_dirty": dirty,
        "months": imported_months,
        "tasks": dict(tasks),
        "input_files": json.loads(json.dumps(input_files)),
    }
    manifest: dict[str, Any] = {
        "schema_version": 2 if ledger.inventory_generation_sha256 is not None else 1,
        "generated_at": timestamp,
        "gee_project": ledger.gee_project,
        "year": ledger.year,
        "months": imported_months,
        "stations_total": current_plan.stations_total,
        "stations_with_coordinates": current_plan.stations_with_coordinates,
        "stations_without_coordinates": current_plan.stations_without_coordinates,
        "station_inventory_sha256": ledger.station_inventory_sha256,
        "source_contract_sha256": ledger.source_contract_sha256,
        "source_contract": dict(ledger.source_contract),
        "rows": values.height,
        "null_values": values["value"].null_count(),
        "tasks": tasks,
        "input_files": input_files,
        "git_sha": sha,
        "git_dirty": dirty,
        "import_runs": [import_run],
    }
    if ledger.inventory_generation_sha256 is not None:
        manifest["inventory_generation_sha256"] = ledger.inventory_generation_sha256
    result = MaiacResult(values=values, coverage=coverage, manifest=manifest)
    _validate_maiac_result(result)
    return result


def _validate_maiac_result(result: MaiacResult) -> None:
    if result.values.schema != pl.Schema(VALUE_SCHEMA):
        raise RuntimeError("MAIAC values schema does not match the import contract")
    if result.coverage.schema != pl.Schema(COVERAGE_SCHEMA):
        raise RuntimeError("MAIAC coverage schema does not match the import contract")
    duplicates = (
        result.values.group_by("source", "month", "station_name").len().filter(pl.col("len") > 1)
    )
    if not duplicates.is_empty():
        raise RuntimeError("MAIAC values contain duplicate source-month-station keys")
    nonfinite = result.values.filter(pl.col("value").is_not_null() & ~pl.col("value").is_finite())
    if not nonfinite.is_empty():
        raise RuntimeError("MAIAC values contain a nonfinite number")
    stats = result.values.group_by("source", "month").agg(
        pl.len().alias("rows"),
        pl.col("station_name").n_unique().alias("unique_stations"),
        pl.col("value").count().alias("valid_values"),
        pl.col("value").null_count().alias("null_values"),
    )
    compared = result.coverage.join(stats, on=["source", "month"], how="inner")
    if compared.height != result.coverage.height or stats.height != result.coverage.height:
        raise RuntimeError("MAIAC values and coverage do not describe the same months")
    inconsistent = compared.filter(
        (pl.col("rows") != pl.col("unique_stations"))
        | (pl.col("rows") != pl.col("n_stations"))
        | (pl.col("valid_values") != pl.col("n_valid"))
        | (pl.col("null_values") != pl.col("n_null"))
    )
    if not inconsistent.is_empty():
        raise RuntimeError("MAIAC values and coverage counts are inconsistent")

    manifest = result.manifest
    schema_version = manifest.get("schema_version")
    if schema_version not in {1, 2}:
        raise RuntimeError("unsupported MAIAC result manifest schema")
    year = as_int(manifest.get("year"), what="MAIAC result manifest year")
    months = manifest.get("months")
    actual_months = sorted(result.values["month"].dt.month().unique().to_list())
    if months != actual_months:
        raise RuntimeError("MAIAC result manifest months are inconsistent")
    if result.values.filter(pl.col("month").dt.year() != year).height:
        raise RuntimeError("MAIAC values do not match the manifest year")
    if manifest.get("rows") != result.values.height:
        raise RuntimeError("MAIAC result manifest row count is inconsistent")
    if manifest.get("null_values") != result.values["value"].null_count():
        raise RuntimeError("MAIAC result manifest null count is inconsistent")
    counts = (
        manifest.get("stations_total"),
        manifest.get("stations_with_coordinates"),
        manifest.get("stations_without_coordinates"),
    )
    validated_counts: list[int] = []
    for value in counts:
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise RuntimeError("MAIAC result station counts must be non-negative integers")
        validated_counts.append(value)
    total, placed, unplaced = validated_counts
    if placed <= 0 or placed + unplaced != total:
        raise RuntimeError("MAIAC result station counts are inconsistent")
    source_contract = manifest.get("source_contract")
    if not isinstance(source_contract, dict):
        raise RuntimeError("MAIAC result source contract must be a mapping")
    if _canonical_sha256(source_contract) != manifest.get("source_contract_sha256"):
        raise RuntimeError("MAIAC result source contract hash is inconsistent")
    station_hash = manifest.get("station_inventory_sha256")
    if not isinstance(station_hash, str) or len(station_hash) != 64:
        raise RuntimeError("MAIAC result station inventory hash is invalid")
    if schema_version == 1:
        if manifest.get("inventory_generation_sha256") is not None:
            raise RuntimeError("legacy MAIAC results cannot claim an inventory generation")
    else:
        generation = manifest.get("inventory_generation_sha256")
        if not isinstance(generation, str):
            raise RuntimeError("MAIAC result inventory generation is missing")
        try:
            validate_generation_sha256(generation)
        except ValueError as exc:
            raise RuntimeError("MAIAC result inventory generation is invalid") from exc
        if generation != station_hash:
            raise RuntimeError("MAIAC result inventory generation is inconsistent")
    tasks = manifest.get("tasks")
    input_files = manifest.get("input_files")
    month_keys = {str(month) for month in actual_months}
    if not isinstance(tasks, dict) or set(tasks) != month_keys:
        raise RuntimeError("MAIAC result task inventory is inconsistent")
    if not isinstance(input_files, dict) or set(input_files) != month_keys:
        raise RuntimeError("MAIAC result file inventory is inconsistent")
    for month, payload in input_files.items():
        if not isinstance(payload, dict):
            raise RuntimeError(f"MAIAC input file record for month {month} must be a mapping")
        name = payload.get("name")
        digest = payload.get("sha256")
        if not isinstance(name, str) or not name.endswith(".csv"):
            raise RuntimeError(f"MAIAC input filename for month {month} is invalid")
        if not isinstance(digest, str) or len(digest) != 64:
            raise RuntimeError(f"MAIAC input checksum for month {month} is invalid")
    import_runs = manifest.get("import_runs")
    if not isinstance(import_runs, list) or not import_runs:
        raise RuntimeError("MAIAC import run provenance is missing")


def _recover_result_swap(destination: Path) -> None:
    parent = destination.parent
    if not parent.exists():
        return
    backups = sorted(parent.glob(f".{destination.name}.backup-*"))
    stages = sorted(parent.glob(f".{destination.name}.staging-*"))
    if len(backups) > 1:
        raise RuntimeError(f"multiple interrupted MAIAC result backups found beside {destination}")
    if len(stages) > 1:
        raise RuntimeError(f"multiple interrupted MAIAC result stages found beside {destination}")
    if destination.exists() and backups and stages:
        raise RuntimeError(f"ambiguous interrupted MAIAC result swap found beside {destination}")
    if not destination.exists() and backups:
        backups[0].replace(destination)
        backups = []
    for staged in stages:
        shutil.rmtree(staged)
    if destination.exists() and backups:
        shutil.rmtree(backups[0])


def _read_maiac_result(destination: Path) -> MaiacResult | None:
    if not destination.exists():
        return None
    if not destination.is_dir():
        raise RuntimeError(f"MAIAC result destination is not a directory: {destination}")
    expected = set(OUTPUT_FILENAMES.values())
    present = {path.name for path in destination.iterdir()}
    if present != expected:
        raise RuntimeError(
            f"MAIAC result destination must contain exactly {sorted(expected)}, "
            f"found {sorted(present)}"
        )
    paths = {name: destination / filename for name, filename in OUTPUT_FILENAMES.items()}
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise RuntimeError("MAIAC result manifest must be a JSON object")
    result = MaiacResult(
        values=pl.read_parquet(paths["values"]),
        coverage=pl.read_parquet(paths["coverage"]),
        manifest=manifest,
    )
    _validate_maiac_result(result)
    return result


def read_maiac_result(destination: Path) -> MaiacResult:
    """Read one complete result after applying the import contract."""
    result = _read_maiac_result(destination)
    if result is None:
        raise FileNotFoundError(f"MAIAC result not found: {destination}")
    return result


def _merge_maiac_results(existing: MaiacResult, incoming: MaiacResult) -> MaiacResult:
    if existing.manifest.get("inventory_generation_sha256") is not None and (
        existing.manifest.get("stations_total"),
        existing.manifest.get("stations_with_coordinates"),
        existing.manifest.get("stations_without_coordinates"),
    ) != (
        incoming.manifest.get("stations_total"),
        incoming.manifest.get("stations_with_coordinates"),
        incoming.manifest.get("stations_without_coordinates"),
    ):
        raise RuntimeError("MAIAC result merge changed the station counts")
    for field_name, label in (
        ("schema_version", "schema version"),
        ("gee_project", "Earth Engine project"),
        ("year", "year"),
        ("station_inventory_sha256", "station inventory"),
        ("source_contract_sha256", "source contract"),
        ("inventory_generation_sha256", "inventory generation"),
    ):
        if existing.manifest.get(field_name) != incoming.manifest.get(field_name):
            raise RuntimeError(f"MAIAC result merge changed the {label}")
    incoming_months = set(incoming.manifest["months"])
    preserved_values = existing.values.filter(~pl.col("month").dt.month().is_in(incoming_months))
    preserved_coverage = existing.coverage.filter(
        ~pl.col("month").dt.month().is_in(incoming_months)
    )
    values = pl.concat([preserved_values, incoming.values]).sort("month", "station_name")
    coverage = pl.concat([preserved_coverage, incoming.coverage]).sort("month")

    manifest = json.loads(json.dumps(existing.manifest))
    manifest["generated_at"] = incoming.manifest["generated_at"]
    manifest["months"] = sorted(set(existing.manifest["months"]) | incoming_months)
    manifest["rows"] = values.height
    manifest["null_values"] = values["value"].null_count()
    manifest["git_sha"] = incoming.manifest["git_sha"]
    manifest["git_dirty"] = incoming.manifest["git_dirty"]
    manifest["tasks"].update(incoming.manifest["tasks"])
    manifest["input_files"].update(incoming.manifest["input_files"])
    manifest["import_runs"].extend(incoming.manifest["import_runs"])
    result = MaiacResult(values=values, coverage=coverage, manifest=manifest)
    _validate_maiac_result(result)
    return result


def write_maiac_result(
    result: MaiacResult,
    *,
    destination: Path | None = None,
) -> dict[str, Path]:
    _validate_maiac_result(result)
    year = as_int(result.manifest.get("year"), what="MAIAC result manifest year")
    generation = result.manifest.get("inventory_generation_sha256")
    out = destination or (
        maiac_generation_result_dir(year, generation)
        if isinstance(generation, str)
        else interim_dir("maiac") / f"year={year}" / "result"
    )
    if not out.name or out == out.parent:
        raise RuntimeError("MAIAC result destination must be a named directory")
    _validate_result_generation_destination(result.manifest, out)
    _recover_result_swap(out)
    existing = _read_maiac_result(out)
    if existing is not None:
        _validate_result_generation_destination(existing.manifest, out)
    combined = _merge_maiac_results(existing, result) if existing is not None else result
    _validate_maiac_result(combined)

    out.parent.mkdir(parents=True, exist_ok=True)
    token = uuid4().hex
    staged = out.with_name(f".{out.name}.staging-{token}")
    backup = out.with_name(f".{out.name}.backup-{token}")
    staged.mkdir()
    paths = {name: staged / filename for name, filename in OUTPUT_FILENAMES.items()}
    try:
        combined.values.write_parquet(paths["values"])
        combined.coverage.write_parquet(paths["coverage"])
        paths["manifest"].write_text(
            json.dumps(combined.manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        had_existing = out.exists()
        if had_existing:
            out.replace(backup)
        try:
            staged.replace(out)
        except Exception:
            if had_existing and backup.exists():
                backup.replace(out)
            raise
        if backup.exists():
            shutil.rmtree(backup)
    finally:
        if staged.exists():
            shutil.rmtree(staged)
    return {name: out / filename for name, filename in OUTPUT_FILENAMES.items()}


def _validate_result_generation_destination(manifest: dict[str, Any], destination: Path) -> None:
    generation = manifest.get("inventory_generation_sha256")
    path_generation = (
        destination.parent.parent.name
        if destination.name == "result"
        and destination.parent.name == f"year={manifest.get('year')}"
        and destination.parent.parent.parent.name == "generations"
        else None
    )
    if generation is None:
        if path_generation is not None:
            raise RuntimeError("legacy MAIAC results cannot write to a generation path")
        return
    if path_generation != generation:
        raise RuntimeError("MAIAC result destination generation does not match the manifest")
