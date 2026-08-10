"""Acquire bounded Sentinel-5P covariates without pretending they are M8.

Earth Engine returns atmospheric columns, not surface concentrations. This
module therefore stops at a station-month acquisition table with provenance;
correlation, calibration and fusion belong to a later analysis stage. The
strict key check is deliberate: a provider response that silently omits one
station is not interchangeable with a masked pixel that explicitly arrives as
null.
"""

from __future__ import annotations

import json
import shutil
import time
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

import polars as pl

from twair.config import ConfigError, load_conf
from twair.ingest.station_inventory import (
    station_inventory_generation,
    validate_generation_sha256,
)
from twair.paths import interim_dir
from twair.provenance import git_state
from twair.scalars import as_int

__all__ = [
    "EarthEngineBackend",
    "SatelliteResponse",
    "SatelliteResult",
    "SatelliteSource",
    "acquire_s5p",
    "load_s5p_sources",
    "parse_months",
    "read_satellite_result",
    "write_satellite_result",
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
}

COVERAGE_SCHEMA = {
    "source": pl.String,
    "month": pl.Date,
    "source_images": pl.Int64,
    "n_stations": pl.Int64,
    "n_valid": pl.Int64,
    "n_null": pl.Int64,
    "query_wall_seconds": pl.Float64,
}

OUTPUT_FILENAMES = {
    "values": "s5p_station_month.parquet",
    "coverage": "s5p_coverage.parquet",
    "manifest": "manifest.json",
}


@dataclass(frozen=True, slots=True)
class SatelliteSource:
    key: str
    collection_id: str
    band: str
    unit: str
    sample_scale_m: int
    column_kind: str


@dataclass(frozen=True, slots=True)
class SatelliteResponse:
    rows: list[dict[str, object]]
    image_counts: dict[int, int]
    wall_seconds: float


@dataclass(frozen=True, slots=True)
class SatelliteResult:
    values: pl.DataFrame
    coverage: pl.DataFrame
    manifest: dict[str, object]


class SatelliteBackend(Protocol):
    def fetch(
        self,
        source: SatelliteSource,
        station_frame: pl.DataFrame,
        *,
        year: int,
        months: tuple[int, ...],
    ) -> SatelliteResponse: ...


def load_s5p_sources(config: dict[str, Any] | None = None) -> list[SatelliteSource]:
    raw = config if config is not None else load_conf("satellite")
    group = raw.get("s5p")
    if not isinstance(group, dict) or not group:
        raise ConfigError("conf/satellite.yaml must define a non-empty `s5p` mapping")

    required = {"collection_id", "band", "unit", "sample_scale_m", "column_kind"}
    sources: list[SatelliteSource] = []
    for key, payload in group.items():
        if not isinstance(key, str) or not isinstance(payload, dict):
            raise ConfigError("every `s5p` source must be a named mapping")
        if not key.strip() or key != key.strip():
            raise ConfigError("every `s5p` source key must be a non-empty string without padding")
        missing = required - set(payload)
        if missing:
            raise ConfigError(f"satellite.s5p.{key} is missing {sorted(missing)}")
        scale = payload["sample_scale_m"]
        if not isinstance(scale, int) or isinstance(scale, bool) or scale <= 0:
            raise ConfigError(f"satellite.s5p.{key}.sample_scale_m must be a positive integer")
        text_fields: dict[str, str] = {}
        for field_name in required - {"sample_scale_m"}:
            value = payload[field_name]
            if not isinstance(value, str) or not value.strip():
                raise ConfigError(f"satellite.s5p.{key}.{field_name} must be a non-empty string")
            text_fields[field_name] = value.strip()
        sources.append(
            SatelliteSource(
                key=key,
                collection_id=text_fields["collection_id"],
                band=text_fields["band"],
                unit=text_fields["unit"],
                sample_scale_m=scale,
                column_kind=text_fields["column_kind"],
            )
        )
    return sources


def parse_months(spec: str) -> tuple[int, ...]:
    values: list[int] = []
    for part in spec.split(","):
        token = part.strip()
        if not token:
            continue
        if ":" in token:
            start_text, _, end_text = token.partition(":")
            start, end = int(start_text), int(end_text)
            if start > end:
                raise ValueError(f"month range starts after it ends: {token}")
            values.extend(range(start, end + 1))
        else:
            values.append(int(token))
    months = tuple(dict.fromkeys(values))
    if not months or any(month < 1 or month > 12 for month in months):
        raise ValueError("months must be integers from 1 through 12")
    return months


def _placed_stations(stations: pl.DataFrame) -> tuple[pl.DataFrame, int]:
    required = {"station_name", "lon", "lat"}
    missing = required - set(stations.columns)
    if missing:
        raise RuntimeError(f"station table is missing {sorted(missing)}")
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


def _validated_rows(
    response: SatelliteResponse,
    source: SatelliteSource,
    stations: pl.DataFrame,
    *,
    year: int,
    months: tuple[int, ...],
) -> list[dict[str, object]]:
    station_names = set(stations["station_name"].to_list())
    expected = {(station, month) for station in station_names for month in months}
    keys: list[tuple[str, int]] = []
    records: list[dict[str, object]] = []

    for row in response.rows:
        try:
            station = str(row["station_name"])
            raw_month = row["month"]
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"{source.key}: provider row has no usable station/month key"
            ) from exc
        if isinstance(raw_month, bool) or not isinstance(raw_month, int):
            raise RuntimeError(f"{source.key}: provider month must be an exact integer")
        month = raw_month
        value = row.get("value")
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise RuntimeError(
                f"{source.key}: value for {station} month {month} is not numeric or null"
            )
        keys.append((station, month))
        records.append(
            {
                "station_name": station,
                "month": date(year, month, 1),
                "source": source.key,
                "value": value,
                "unit": source.unit,
                "collection_id": source.collection_id,
                "band": source.band,
                "sample_scale_m": source.sample_scale_m,
            }
        )

    duplicates = sorted(key for key, count in Counter(keys).items() if count > 1)
    if duplicates:
        raise RuntimeError(f"{source.key}: duplicate station-month row(s): {duplicates[:3]}")
    actual = set(keys)
    missing = expected - actual
    unexpected = actual - expected
    if missing:
        raise RuntimeError(f"{source.key}: missing {len(missing)} expected row(s)")
    if unexpected:
        raise RuntimeError(f"{source.key}: returned {len(unexpected)} unexpected row(s)")

    count_months = set(response.image_counts)
    requested_months = set(months)
    if count_months != requested_months:
        raise RuntimeError(
            f"{source.key}: image counts cover months {sorted(count_months)}, "
            f"expected {sorted(requested_months)}"
        )
    return records


def acquire_s5p(
    stations: pl.DataFrame,
    *,
    backend: SatelliteBackend,
    project: str,
    year: int,
    months: tuple[int, ...] = tuple(range(1, 13)),
    sources: list[SatelliteSource] | None = None,
    generated_at: str | None = None,
    inventory_generation: bool = False,
) -> SatelliteResult:
    if year < 2018:
        raise ValueError("Sentinel-5P OFFL sources do not cover a complete pre-2018 year")
    if not months or any(month < 1 or month > 12 for month in months):
        raise ValueError("months must be integers from 1 through 12")
    if len(set(months)) != len(months):
        raise ValueError("months must not contain duplicates")

    generation = station_inventory_generation(stations) if inventory_generation else None
    if generation is None:
        placed, unplaced_count = _placed_stations(stations)
    else:
        placed = generation.stations
        unplaced_count = generation.stations_without_coordinates
    selected_sources = sources if sources is not None else load_s5p_sources()
    if not selected_sources:
        raise RuntimeError("no S5P sources are configured")
    run_generated_at = generated_at or datetime.now(UTC).isoformat(timespec="seconds")
    sha, dirty = git_state()

    all_records: list[dict[str, object]] = []
    coverage_records: list[dict[str, object]] = []
    source_manifest: dict[str, object] = {}
    for source in selected_sources:
        response = backend.fetch(source, placed, year=year, months=months)
        records = _validated_rows(response, source, placed, year=year, months=months)
        all_records.extend(records)
        for month in months:
            month_date = date(year, month, 1)
            month_records = [record for record in records if record["month"] == month_date]
            n_valid = sum(record["value"] is not None for record in month_records)
            coverage_records.append(
                {
                    "source": source.key,
                    "month": month_date,
                    "source_images": response.image_counts[month],
                    "n_stations": placed.height,
                    "n_valid": n_valid,
                    "n_null": placed.height - n_valid,
                    "query_wall_seconds": response.wall_seconds,
                }
            )
        source_manifest[source.key] = {
            "collection_id": source.collection_id,
            "band": source.band,
            "unit": source.unit,
            "sample_scale_m": source.sample_scale_m,
            "column_kind": source.column_kind,
            "query_wall_seconds": response.wall_seconds,
        }

    values = pl.from_dicts(all_records, schema=VALUE_SCHEMA).sort("source", "month", "station_name")
    coverage = pl.from_dicts(coverage_records, schema=COVERAGE_SCHEMA).sort("source", "month")
    manifest: dict[str, object] = {
        "schema_version": 3 if generation is not None else 2,
        "generated_at": run_generated_at,
        "gee_project": project,
        "year": year,
        "months": list(months),
        "stations_total": stations.height,
        "stations_with_coordinates": placed.height,
        "stations_without_coordinates": unplaced_count,
        "station_inventory_sha256": _station_inventory_sha256(placed),
        "rows": values.height,
        "null_values": values["value"].null_count(),
        "sources": source_manifest,
        "git_sha": sha,
        "git_dirty": dirty,
        "acquisition_runs": [
            {
                "generated_at": run_generated_at,
                "git_sha": sha,
                "git_dirty": dirty,
                "sources": [source.key for source in selected_sources],
                "months": list(months),
            }
        ],
    }
    if generation is not None:
        manifest["inventory_generation_sha256"] = generation.sha256
    return SatelliteResult(values=values, coverage=coverage, manifest=manifest)


class EarthEngineBackend:
    def __init__(self, project: str) -> None:
        try:
            import ee
        except ImportError as exc:
            raise RuntimeError(
                "earthengine-api is not installed; run `uv sync --extra earth`"
            ) from exc
        ee.Initialize(project=project)
        self._ee: Any = ee

    def fetch(
        self,
        source: SatelliteSource,
        station_frame: pl.DataFrame,
        *,
        year: int,
        months: tuple[int, ...],
    ) -> SatelliteResponse:
        ee = self._ee
        features = [
            ee.Feature(
                ee.Geometry.Point([row["lon"], row["lat"]]),
                {"station_name": row["station_name"]},
            )
            for row in station_frame.iter_rows(named=True)
        ]
        points = ee.FeatureCollection(features)
        collection = (
            ee.ImageCollection(source.collection_id)
            .filterBounds(points.geometry().bounds())
            .select(source.band)
        )
        reductions = []
        image_counts = []
        for month in months:
            start = ee.Date.fromYMD(year, month, 1)
            end = start.advance(1, "month")
            monthly = collection.filterDate(start, end)
            composite = monthly.mean().rename("value")
            reduced = composite.reduceRegions(
                collection=points,
                reducer=ee.Reducer.first().setOutputs(["value"]),
                scale=source.sample_scale_m,
            ).map(lambda feature, month=month: feature.set("month", month))
            reductions.append(reduced)
            image_counts.append(monthly.size())

        started = time.perf_counter()
        payload = ee.FeatureCollection(reductions).flatten().getInfo()
        wall_seconds = round(time.perf_counter() - started, 3)
        counts = ee.Dictionary.fromLists([str(month) for month in months], image_counts).getInfo()
        rows = [dict(feature["properties"]) for feature in payload["features"]]
        return SatelliteResponse(
            rows=rows,
            image_counts={int(month): int(count) for month, count in counts.items()},
            wall_seconds=wall_seconds,
        )


def write_satellite_result(
    result: SatelliteResult, *, destination: Path | None = None
) -> dict[str, Path]:
    year = as_int(result.manifest["year"], what="satellite manifest year")
    out = destination or interim_dir("satellite") / f"year={year}"
    if not out.name or out == out.parent:
        raise RuntimeError("satellite destination must be a named year directory")
    _validate_satellite_result(result)
    _validate_generation_destination(result.manifest, out)
    stale_backup = _recover_interrupted_swap(out)
    existing = _read_satellite_result(out)
    if existing is not None:
        _validate_generation_destination(existing.manifest, out)
    if stale_backup is not None:
        shutil.rmtree(stale_backup)
    combined = _merge_satellite_results(existing, result) if existing is not None else result
    _validate_satellite_result(combined)

    out.parent.mkdir(parents=True, exist_ok=True)
    token = uuid4().hex
    staged = out.with_name(f".{out.name}.staging-{token}")
    backup = out.with_name(f".{out.name}.backup-{token}")
    staged.mkdir()
    staged_paths = {name: staged / filename for name, filename in OUTPUT_FILENAMES.items()}
    try:
        combined.values.write_parquet(staged_paths["values"])
        combined.coverage.write_parquet(staged_paths["coverage"])
        staged_paths["manifest"].write_text(
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


def _station_inventory_sha256(stations: pl.DataFrame) -> str:
    payload = json.dumps(
        stations.sort("station_name").select("station_name", "lon", "lat").to_dicts(),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(payload).hexdigest()


def _recover_interrupted_swap(destination: Path) -> Path | None:
    parent = destination.parent
    if not parent.exists():
        return None
    backups = sorted(parent.glob(f".{destination.name}.backup-*"))
    stages = sorted(parent.glob(f".{destination.name}.staging-*"))
    if len(backups) > 1:
        raise RuntimeError(f"multiple interrupted satellite backups found beside {destination}")
    if destination.exists() and backups and stages:
        raise RuntimeError(f"ambiguous interrupted satellite swap found beside {destination}")
    if not destination.exists() and backups:
        backups[0].replace(destination)
        backups = []
    for staged in stages:
        shutil.rmtree(staged)
    return backups[0] if backups else None


def _read_satellite_result(destination: Path) -> SatelliteResult | None:
    if not destination.exists():
        return None
    if not destination.is_dir():
        raise RuntimeError(f"satellite destination is not a directory: {destination}")
    expected = set(OUTPUT_FILENAMES.values())
    present = {path.name for path in destination.iterdir()}
    if present != expected:
        raise RuntimeError(
            f"satellite destination must contain exactly {sorted(expected)}, found {sorted(present)}"
        )
    paths = {name: destination / filename for name, filename in OUTPUT_FILENAMES.items()}
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise RuntimeError("satellite manifest must be a JSON object")
    result = SatelliteResult(
        values=pl.read_parquet(paths["values"]),
        coverage=pl.read_parquet(paths["coverage"]),
        manifest=manifest,
    )
    _validate_satellite_result(result)
    return result


def read_satellite_result(destination: Path) -> SatelliteResult:
    """Read one complete result after applying the acquisition contract."""
    result = _read_satellite_result(destination)
    if result is None:
        raise FileNotFoundError(f"satellite result not found: {destination}")
    return result


def _validate_satellite_result(result: SatelliteResult) -> None:
    if result.values.schema != pl.Schema(VALUE_SCHEMA):
        raise RuntimeError("satellite values schema does not match the acquisition contract")
    if result.coverage.schema != pl.Schema(COVERAGE_SCHEMA):
        raise RuntimeError("satellite coverage schema does not match the acquisition contract")

    stats = result.values.group_by("source", "month").agg(
        pl.len().alias("rows"),
        pl.col("station_name").n_unique().alias("unique_stations"),
        pl.col("value").count().alias("valid_values"),
        pl.col("value").null_count().alias("null_values"),
    )
    if stats.height != result.coverage.height:
        raise RuntimeError("satellite values and coverage do not describe the same source-months")
    compared = result.coverage.join(stats, on=["source", "month"], how="inner")
    inconsistent = compared.filter(
        (pl.col("rows") != pl.col("unique_stations"))
        | (pl.col("rows") != pl.col("n_stations"))
        | (pl.col("valid_values") != pl.col("n_valid"))
        | (pl.col("null_values") != pl.col("n_null"))
    )
    if compared.height != result.coverage.height or not inconsistent.is_empty():
        raise RuntimeError("satellite values and coverage counts are inconsistent")

    manifest = result.manifest
    manifest_year = manifest.get("year")
    if isinstance(manifest_year, bool) or not isinstance(manifest_year, int):
        raise RuntimeError("satellite manifest year must be an exact integer")
    observed_years = result.values["month"].dt.year().unique().to_list()
    if observed_years != [manifest_year]:
        raise RuntimeError("satellite values do not match the manifest year")
    observed_months = sorted(result.coverage["month"].dt.month().unique().to_list())
    if manifest.get("months") != observed_months:
        raise RuntimeError("satellite coverage does not match the manifest months")
    if manifest.get("rows") != result.values.height:
        raise RuntimeError("satellite manifest row count is inconsistent")
    if manifest.get("null_values") != result.values["value"].null_count():
        raise RuntimeError("satellite manifest null count is inconsistent")
    sources = manifest.get("sources")
    if not isinstance(sources, dict) or set(sources) != set(result.values["source"].unique()):
        raise RuntimeError("satellite manifest source inventory is inconsistent")
    if not all(isinstance(source, dict) for source in sources.values()):
        raise RuntimeError("satellite manifest source contracts must be mappings")
    schema_version = manifest.get("schema_version")
    if schema_version not in {2, 3}:
        raise RuntimeError("satellite manifest schema version is not supported")
    if schema_version in {2, 3}:
        inventory_hash = manifest.get("station_inventory_sha256")
        if not isinstance(inventory_hash, str) or len(inventory_hash) != 64:
            raise RuntimeError("satellite station coordinate inventory hash is missing")
        runs = manifest.get("acquisition_runs")
        if not isinstance(runs, list) or not runs or not all(isinstance(run, dict) for run in runs):
            raise RuntimeError("satellite acquisition run provenance is missing")
    if schema_version == 3:
        generation = manifest.get("inventory_generation_sha256")
        if not isinstance(generation, str):
            raise RuntimeError("satellite inventory generation identity is missing")
        try:
            validate_generation_sha256(generation)
        except ValueError as exc:
            raise RuntimeError("satellite inventory generation identity is missing") from exc


def _validate_generation_destination(manifest: dict[str, object], destination: Path) -> None:
    raw_generation = manifest.get("inventory_generation_sha256")
    path_generation = (
        destination.parent.name
        if destination.parent.parent.name == "generations"
        and destination.name == f"year={manifest.get('year')}"
        else None
    )
    if raw_generation is None:
        if path_generation is not None:
            raise RuntimeError("legacy satellite results cannot write to a generation path")
        return
    if not isinstance(raw_generation, str):
        raise RuntimeError("satellite inventory generation identity is invalid")
    try:
        generation = validate_generation_sha256(raw_generation)
    except ValueError as exc:
        raise RuntimeError("satellite inventory generation identity is invalid") from exc
    if path_generation != generation:
        raise RuntimeError("satellite destination generation does not match the manifest")


def _merge_satellite_results(
    existing: SatelliteResult, incoming: SatelliteResult
) -> SatelliteResult:
    replacement_keys = incoming.coverage.select("source", "month")
    retained_values = existing.values.join(replacement_keys, on=["source", "month"], how="anti")
    retained_coverage = existing.coverage.join(replacement_keys, on=["source", "month"], how="anti")
    if retained_coverage.is_empty():
        return incoming

    for field in (
        "schema_version",
        "gee_project",
        "year",
        "stations_total",
        "stations_with_coordinates",
        "stations_without_coordinates",
        "station_inventory_sha256",
    ):
        if existing.manifest.get(field) != incoming.manifest.get(field):
            detail = (
                "station coordinate inventory"
                if field == "station_inventory_sha256"
                else f"manifest field {field!r}"
            )
            raise RuntimeError(f"satellite rerun changed {detail}")
    if set(existing.values["station_name"].unique()) != set(
        incoming.values["station_name"].unique()
    ):
        raise RuntimeError("satellite rerun changed the placed-station inventory")

    values = pl.concat(
        [
            retained_values,
            incoming.values,
        ],
        how="vertical",
    ).sort("source", "month", "station_name")
    coverage = pl.concat(
        [
            retained_coverage,
            incoming.coverage,
        ],
        how="vertical",
    ).sort("source", "month")

    old_sources = existing.manifest.get("sources")
    new_sources = incoming.manifest.get("sources")
    if not isinstance(old_sources, dict) or not isinstance(new_sources, dict):
        raise RuntimeError("satellite source manifests must be mappings")
    for source_name in set(old_sources) & set(new_sources):
        old_contract = {
            key: value
            for key, value in old_sources[source_name].items()
            if key != "query_wall_seconds"
        }
        new_contract = {
            key: value
            for key, value in new_sources[source_name].items()
            if key != "query_wall_seconds"
        }
        if old_contract != new_contract:
            raise RuntimeError(f"satellite rerun changed source contract {source_name!r}")

    manifest = dict(incoming.manifest)
    manifest["months"] = sorted(coverage["month"].dt.month().unique().to_list())
    manifest["rows"] = values.height
    manifest["null_values"] = values["value"].null_count()
    manifest["sources"] = {**old_sources, **new_sources}
    old_runs = existing.manifest.get("acquisition_runs")
    new_runs = incoming.manifest.get("acquisition_runs")
    if not isinstance(old_runs, list) or not isinstance(new_runs, list):
        raise RuntimeError("satellite acquisition run provenance must be a list")
    manifest["acquisition_runs"] = [*old_runs, *new_runs]
    return SatelliteResult(values=values, coverage=coverage, manifest=manifest)
