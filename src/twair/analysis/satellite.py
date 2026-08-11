"""M8 Stage B: joint coverage and descriptive satellite/PM2.5 association.

Satellite columns, AOD, and surface PM2.5 are different physical quantities.
This module keeps their units separate and asks only whether their observed
station-month patterns move together. It does not calibrate one into another,
estimate emissions, identify a source, or create a fused concentration field.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

import numpy as np
import polars as pl

from twair.ingest.maiac_import import MaiacResult, read_maiac_result
from twair.ingest.satellite import SatelliteResult, read_satellite_result
from twair.ingest.station_inventory import (
    maiac_generation_result_dir,
    satellite_generation_dir,
    validate_generation_sha256,
)
from twair.paths import data_root, interim_dir, outputs_dir, processed_dir
from twair.provenance import git_state

__all__ = [
    "SatelliteAssociationResult",
    "analyse_satellite_frames",
    "run_satellite_association",
    "satellite_analysis_dir",
    "write_satellite_analysis",
]

TARGET = "PM2.5"
GROUND_UNIT = "ug/m3"
OUTPUT_FILENAMES = {
    "panel": "panel.parquet",
    "coverage": "coverage.parquet",
    "association": "association.parquet",
    "station_context": "station_context.parquet",
    "month_context": "month_context.parquet",
    "manifest": "manifest.json",
}


@dataclass(frozen=True, slots=True)
class SatelliteAssociationResult:
    panel: pl.DataFrame
    coverage: pl.DataFrame
    association: pl.DataFrame
    station_context: pl.DataFrame
    month_context: pl.DataFrame
    manifest: dict[str, Any]


def _validate_year(year: int) -> int:
    if isinstance(year, bool) or not isinstance(year, int) or year <= 0:
        raise ValueError("analysis year must be a positive integer")
    return year


def _canonical_sha256(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _file_sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _directory_file_sha256s(path: Path) -> dict[str, str]:
    if not path.is_dir():
        return {}
    hashes: dict[str, str] = {}
    for child in sorted(path.iterdir(), key=lambda item: item.name):
        if child.is_file():
            digest = _file_sha256(child)
            if digest is None:
                raise RuntimeError(f"source file disappeared while hashing: {child}")
            hashes[child.name] = digest
    return hashes


def _read_stable_source[ResultT](
    path: Path,
    reader: Callable[[Path], ResultT],
    *,
    label: str,
) -> tuple[ResultT, dict[str, str]]:
    before = _directory_file_sha256s(path)
    result = reader(path)
    after = _directory_file_sha256s(path)
    if not before or before != after:
        raise RuntimeError(f"{label} changed while it was being read")
    return result, after


def _read_stable_ground(path: Path) -> tuple[pl.DataFrame, str]:
    before = _file_sha256(path)
    frame = pl.read_parquet(path)
    after = _file_sha256(path)
    if before is None or before != after:
        raise RuntimeError("ground monthly table changed while it was being read")
    return frame, after


def _data_relative(path: Path) -> str:
    root = data_root()
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _source_provenance(
    result: SatelliteResult | MaiacResult,
    path: Path,
    files: dict[str, str],
) -> dict[str, Any]:
    manifest = result.manifest
    return {
        "path": _data_relative(path),
        "files": files,
        "manifest_sha256": _canonical_sha256(manifest),
        "schema_version": manifest.get("schema_version"),
        "rows": manifest.get("rows"),
        "null_values": manifest.get("null_values"),
        "station_inventory_sha256": manifest.get("station_inventory_sha256"),
        "inventory_generation_sha256": manifest.get("inventory_generation_sha256"),
    }


def satellite_analysis_dir(year: int, generation: str | None = None) -> Path:
    """Immutable output directory for one source inventory and calendar year."""
    selected_year = _validate_year(year)
    base = outputs_dir("m8_satellite")
    if generation is None:
        return base / "legacy" / f"year={selected_year}"
    identity = validate_generation_sha256(generation)
    return base / "generations" / identity / f"year={selected_year}"


def _require_columns(frame: pl.DataFrame, required: set[str], *, label: str) -> None:
    missing = sorted(required - set(frame.columns))
    if missing:
        raise RuntimeError(f"{label} is missing required column(s): {missing}")


def _validated_ground(frame: pl.DataFrame, *, year: int) -> pl.DataFrame:
    _require_columns(
        frame,
        {"station_name", "pollutant", "month", "mean", "meets_threshold"},
        label="ground monthly table",
    )
    if frame.schema["month"] != pl.Date:
        raise RuntimeError("ground monthly month must use Date values")
    if frame.schema["meets_threshold"] != pl.Boolean:
        raise RuntimeError("ground monthly meets_threshold must be Boolean")
    if not frame.schema["mean"].is_float():
        raise RuntimeError("ground monthly mean must be floating point")

    selected = frame.filter(
        (pl.col("pollutant") == TARGET) & (pl.col("month").dt.year() == year)
    ).select(
        pl.col("station_name").cast(pl.String),
        "month",
        pl.col("mean").cast(pl.Float64).alias("ground_value"),
        pl.col("meets_threshold").alias("ground_meets_threshold"),
    )
    if selected.is_empty():
        raise RuntimeError(f"ground monthly table has no {TARGET} rows for {year}")
    if selected["ground_meets_threshold"].null_count():
        raise RuntimeError("ground monthly meets_threshold must not be null")
    if selected.filter(
        pl.col("station_name").is_null() | (pl.col("station_name").str.strip_chars() == "")
    ).height:
        raise RuntimeError("ground monthly station names must be non-empty")
    duplicates = selected.group_by("station_name", "month").len().filter(pl.col("len") > 1)
    if not duplicates.is_empty():
        raise RuntimeError("ground monthly table has duplicate PM2.5 station-month rows")
    if selected.filter(
        pl.col("ground_value").is_not_null() & ~pl.col("ground_value").is_finite()
    ).height:
        raise RuntimeError("ground PM2.5 means must be finite or null")
    inconsistent = selected.filter(
        (pl.col("ground_value").is_null() & pl.col("ground_meets_threshold"))
        | (pl.col("ground_value").is_not_null() & ~pl.col("ground_meets_threshold"))
    )
    if not inconsistent.is_empty():
        raise RuntimeError(
            "withheld PM2.5 means must be null and reported means must meet threshold"
        )
    return selected.with_columns(pl.lit(True).alias("ground_row_present"))


def _validated_satellite(frame: pl.DataFrame, *, year: int) -> pl.DataFrame:
    required = {
        "station_name",
        "month",
        "source",
        "value",
        "unit",
        "collection_id",
        "band",
        "sample_scale_m",
    }
    _require_columns(frame, required, label="satellite station-month table")
    if frame.is_empty():
        raise RuntimeError("satellite station-month table is empty")
    if frame.schema["month"] != pl.Date:
        raise RuntimeError("satellite month must use Date values")
    if not frame.schema["value"].is_float():
        raise RuntimeError("satellite value must be floating point")
    if not frame.schema["sample_scale_m"].is_integer():
        raise RuntimeError("satellite sample scale must be an integer")

    selected = frame.select(
        pl.col("source").cast(pl.String),
        pl.col("station_name").cast(pl.String),
        "month",
        pl.col("value").cast(pl.Float64).alias("satellite_value"),
        pl.col("unit").cast(pl.String).alias("satellite_unit"),
        pl.col("collection_id").cast(pl.String),
        pl.col("band").cast(pl.String),
        pl.col("sample_scale_m").cast(pl.Int32),
    )
    text_columns = ("source", "station_name", "satellite_unit", "collection_id", "band")
    for column in text_columns:
        if selected.filter(
            pl.col(column).is_null() | (pl.col(column).str.strip_chars() == "")
        ).height:
            raise RuntimeError(f"satellite {column} must be non-empty")
    if selected.filter(pl.col("month").dt.year() != year).height:
        raise RuntimeError("satellite rows must match the selected analysis year")
    if selected.filter(
        pl.col("satellite_value").is_not_null() & ~pl.col("satellite_value").is_finite()
    ).height:
        raise RuntimeError("satellite values must be finite or null")
    if selected.filter(pl.col("sample_scale_m") <= 0).height:
        raise RuntimeError("satellite sample scales must be positive")
    duplicates = (
        selected.group_by("source", "station_name", "month").len().filter(pl.col("len") > 1)
    )
    if not duplicates.is_empty():
        raise RuntimeError("satellite table has duplicate source-station-month rows")
    unstable = (
        selected.group_by("source")
        .agg(
            pl.col("satellite_unit").n_unique().alias("units"),
            pl.col("collection_id").n_unique().alias("collections"),
            pl.col("band").n_unique().alias("bands"),
            pl.col("sample_scale_m").n_unique().alias("scales"),
        )
        .filter(
            (pl.col("units") != 1)
            | (pl.col("collections") != 1)
            | (pl.col("bands") != 1)
            | (pl.col("scales") != 1)
        )
    )
    if not unstable.is_empty():
        raise RuntimeError("one satellite source cannot change unit or sampling contract")
    return selected


def _build_panel(ground: pl.DataFrame, satellite: pl.DataFrame, *, year: int) -> pl.DataFrame:
    ground_rows = _validated_ground(ground, year=year)
    source_rows = _validated_satellite(satellite, year=year)
    return (
        source_rows.join(ground_rows, on=["station_name", "month"], how="left")
        .with_columns(
            pl.col("ground_row_present").fill_null(False),
            pl.lit(GROUND_UNIT).alias("ground_unit"),
        )
        .with_columns(
            pl.col("satellite_value").is_not_null().alias("satellite_observed"),
            (pl.col("ground_row_present") & pl.col("ground_value").is_not_null()).alias(
                "ground_observed"
            ),
            (pl.col("ground_row_present") & pl.col("ground_value").is_null()).alias(
                "ground_withheld"
            ),
        )
        .with_columns(
            (pl.col("satellite_observed") & pl.col("ground_observed")).alias("pair_observed")
        )
        .select(
            "source",
            "station_name",
            "month",
            "satellite_value",
            "satellite_unit",
            "ground_value",
            "ground_unit",
            "satellite_observed",
            "ground_row_present",
            "ground_meets_threshold",
            "ground_observed",
            "ground_withheld",
            "pair_observed",
            "collection_id",
            "band",
            "sample_scale_m",
        )
        .sort("source", "month", "station_name")
    )


def _coverage(panel: pl.DataFrame) -> pl.DataFrame:
    return (
        panel.group_by("source")
        .agg(
            pl.len().cast(pl.Int64).alias("source_rows"),
            pl.col("satellite_observed").sum().cast(pl.Int64).alias("satellite_observed_rows"),
            (~pl.col("satellite_observed")).sum().cast(pl.Int64).alias("satellite_null_rows"),
            pl.col("ground_row_present").sum().cast(pl.Int64).alias("ground_present_rows"),
            (~pl.col("ground_row_present")).sum().cast(pl.Int64).alias("ground_absent_rows"),
            pl.col("ground_observed").sum().cast(pl.Int64).alias("ground_observed_rows"),
            pl.col("ground_withheld").sum().cast(pl.Int64).alias("ground_withheld_rows"),
            pl.col("pair_observed").sum().cast(pl.Int64).alias("paired_rows"),
            pl.col("station_name").n_unique().cast(pl.Int64).alias("source_stations"),
            pl.col("station_name")
            .filter(pl.col("pair_observed"))
            .n_unique()
            .cast(pl.Int64)
            .alias("paired_stations"),
            pl.col("month").n_unique().cast(pl.Int64).alias("source_months"),
            pl.col("month")
            .filter(pl.col("pair_observed"))
            .n_unique()
            .cast(pl.Int64)
            .alias("paired_months"),
        )
        .with_columns((pl.col("paired_rows") / pl.col("source_rows")).alias("paired_fraction"))
        .sort("source")
    )


def _correlations(frame: pl.DataFrame, x: str, y: str) -> dict[str, float | str | None]:
    n = frame.height
    if n < 3:
        return {
            "pearson_r": None,
            "spearman_r": None,
            "refusal": "fewer_than_three_pairs",
        }
    x_values = frame[x].to_numpy()
    y_values = frame[y].to_numpy()
    if np.ptp(x_values) == 0:
        return {
            "pearson_r": None,
            "spearman_r": None,
            "refusal": "constant_satellite_value",
        }
    if np.ptp(y_values) == 0:
        return {
            "pearson_r": None,
            "spearman_r": None,
            "refusal": "constant_ground_value",
        }
    x_rank = pl.Series(x_values).rank(method="average").to_numpy()
    y_rank = pl.Series(y_values).rank(method="average").to_numpy()
    return {
        "pearson_r": float(np.corrcoef(x_values, y_values)[0, 1]),
        "spearman_r": float(np.corrcoef(x_rank, y_rank)[0, 1]),
        "refusal": None,
    }


def _association(panel: pl.DataFrame) -> pl.DataFrame:
    rows: list[dict[str, Any]] = []
    for source, source_panel in panel.partition_by("source", as_dict=True).items():
        source_name = str(source[0] if isinstance(source, tuple) else source)
        paired = source_panel.filter(pl.col("pair_observed"))
        scopes = {
            "pooled": paired.with_columns(
                pl.col("satellite_value").alias("_x"),
                pl.col("ground_value").alias("_y"),
            ),
            "within_station": paired.with_columns(
                (
                    pl.col("satellite_value")
                    - pl.col("satellite_value").mean().over("station_name")
                ).alias("_x"),
                (pl.col("ground_value") - pl.col("ground_value").mean().over("station_name")).alias(
                    "_y"
                ),
            ),
            "within_month": paired.with_columns(
                (pl.col("satellite_value") - pl.col("satellite_value").mean().over("month")).alias(
                    "_x"
                ),
                (pl.col("ground_value") - pl.col("ground_value").mean().over("month")).alias("_y"),
            ),
        }
        for scope, scoped in scopes.items():
            rows.append(
                {
                    "source": source_name,
                    "scope": scope,
                    "n_pairs": scoped.height,
                    "n_stations": scoped["station_name"].n_unique(),
                    "n_months": scoped["month"].n_unique(),
                    **_correlations(scoped, "_x", "_y"),
                }
            )
    return pl.DataFrame(rows).sort("source", "scope")


def _context(panel: pl.DataFrame, *, dimension: str) -> pl.DataFrame:
    paired = panel.filter(pl.col("pair_observed"))
    rows: list[dict[str, Any]] = []
    if not paired.is_empty():
        for keys, group in paired.group_by("source", dimension, maintain_order=True):
            source, value = keys
            rows.append(
                {
                    "source": source,
                    dimension: value,
                    "n_pairs": group.height,
                    "n_stations": group["station_name"].n_unique(),
                    "n_months": group["month"].n_unique(),
                    "satellite_mean": group["satellite_value"].mean(),
                    "satellite_sd": group["satellite_value"].std(),
                    "pm25_mean": group["ground_value"].mean(),
                    "pm25_sd": group["ground_value"].std(),
                    **_correlations(group, "satellite_value", "ground_value"),
                }
            )
    if rows:
        return pl.DataFrame(rows).sort("source", dimension)
    dimension_dtype = pl.String if dimension == "station_name" else pl.Date
    return pl.DataFrame(
        schema={
            "source": pl.String,
            dimension: dimension_dtype,
            "n_pairs": pl.Int64,
            "n_stations": pl.Int64,
            "n_months": pl.Int64,
            "satellite_mean": pl.Float64,
            "satellite_sd": pl.Float64,
            "pm25_mean": pl.Float64,
            "pm25_sd": pl.Float64,
            "pearson_r": pl.Float64,
            "spearman_r": pl.Float64,
            "refusal": pl.String,
        }
    )


def run_satellite_association(
    *,
    year: int = 2025,
    generation: str | None = None,
) -> SatelliteAssociationResult:
    """Read complete local inputs and build one provisional M8 association result."""
    selected_year = _validate_year(year)
    identity = validate_generation_sha256(generation) if generation is not None else None
    if identity is None:
        s5p_path = interim_dir("satellite") / f"year={selected_year}"
        maiac_path = interim_dir("maiac") / f"year={selected_year}" / "result"
    else:
        s5p_path = satellite_generation_dir(selected_year, identity)
        maiac_path = maiac_generation_result_dir(selected_year, identity)

    s5p, s5p_files = _read_stable_source(
        s5p_path,
        read_satellite_result,
        label="S5P result",
    )
    maiac, maiac_files = _read_stable_source(
        maiac_path,
        read_maiac_result,
        label="MAIAC result",
    )
    source_generations = (
        s5p.manifest.get("inventory_generation_sha256"),
        maiac.manifest.get("inventory_generation_sha256"),
    )
    if identity is None:
        if source_generations != (None, None):
            raise RuntimeError("legacy inputs must not claim an inventory generation")
    elif source_generations != (identity, identity):
        raise RuntimeError("both sources must match the requested inventory generation")

    columns = [
        "station_name",
        "month",
        "source",
        "value",
        "unit",
        "collection_id",
        "band",
        "sample_scale_m",
    ]
    satellite_values = pl.concat(
        [s5p.values.select(columns), maiac.values.select(columns)],
        how="vertical_relaxed",
    )
    ground_path = processed_dir("monthly") / "monthly.parquet"
    ground_monthly, ground_sha256 = _read_stable_ground(ground_path)
    upstream = {
        "ground": {
            "path": _data_relative(ground_path),
            "sha256": ground_sha256,
        },
        "s5p": _source_provenance(s5p, s5p_path, s5p_files),
        "maiac": _source_provenance(maiac, maiac_path, maiac_files),
    }
    return analyse_satellite_frames(
        ground_monthly,
        satellite_values,
        year=selected_year,
        generation=identity,
        upstream=upstream,
    )


def analyse_satellite_frames(
    ground_monthly: pl.DataFrame,
    satellite_values: pl.DataFrame,
    *,
    year: int,
    generation: str | None = None,
    upstream: dict[str, Any] | None = None,
    generated_at: str | None = None,
    git_sha: str | None = None,
    git_dirty: bool | None = None,
) -> SatelliteAssociationResult:
    """Build the complete descriptive result from already validated source rows."""
    selected_year = _validate_year(year)
    identity = validate_generation_sha256(generation) if generation is not None else None
    panel = _build_panel(ground_monthly, satellite_values, year=selected_year)
    coverage = _coverage(panel)
    association = _association(panel)
    station_context = _context(panel, dimension="station_name")
    month_context = _context(panel, dimension="month")
    if git_dirty is None:
        measured_sha, measured_dirty = git_state()
        selected_sha = git_sha if git_sha is not None else measured_sha
        selected_dirty = measured_dirty
    else:
        selected_sha = git_sha
        selected_dirty = git_dirty
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "analysis": "m8_satellite_association",
        "claim_boundary": (
            "provisional descriptive association; not causal; not calibration; "
            "not satellite-estimated PM2.5 or fusion"
        ),
        "year": selected_year,
        "mode": "generation" if identity is not None else "legacy",
        "inventory_generation_sha256": identity,
        "ground_pollutant": TARGET,
        "ground_unit": GROUND_UNIT,
        "sources": sorted(panel["source"].unique().to_list()),
        "upstream": upstream or {},
        "table_rows": {
            "panel": panel.height,
            "coverage": coverage.height,
            "association": association.height,
            "station_context": station_context.height,
            "month_context": month_context.height,
        },
        "generated_at": generated_at or datetime.now(UTC).isoformat(),
        "git_sha": selected_sha,
        "git_dirty": selected_dirty,
    }
    return SatelliteAssociationResult(
        panel=panel,
        coverage=coverage,
        association=association,
        station_context=station_context,
        month_context=month_context,
        manifest=manifest,
    )


def _validate_result(result: SatelliteAssociationResult) -> None:
    manifest = result.manifest
    if manifest.get("schema_version") != 1:
        raise RuntimeError("unsupported M8 satellite analysis manifest schema")
    year = manifest.get("year")
    if isinstance(year, bool) or not isinstance(year, int) or year <= 0:
        raise RuntimeError("M8 satellite analysis year must be a positive integer")
    generation = manifest.get("inventory_generation_sha256")
    mode = manifest.get("mode")
    if mode == "legacy":
        if generation is not None:
            raise RuntimeError("legacy M8 analysis cannot claim an inventory generation")
    elif mode == "generation":
        if not isinstance(generation, str):
            raise RuntimeError("generation M8 analysis is missing its inventory identity")
        validate_generation_sha256(generation)
    else:
        raise RuntimeError("M8 satellite analysis mode is invalid")
    tables = {
        "panel": result.panel,
        "coverage": result.coverage,
        "association": result.association,
        "station_context": result.station_context,
        "month_context": result.month_context,
    }
    expected_rows = {name: frame.height for name, frame in tables.items()}
    if manifest.get("table_rows") != expected_rows:
        raise RuntimeError("M8 satellite analysis table counts are inconsistent")
    if not isinstance(manifest.get("claim_boundary"), str):
        raise RuntimeError("M8 satellite analysis claim boundary is missing")
    json.dumps(manifest, ensure_ascii=False, allow_nan=False)


def _recover_swap(destination: Path) -> None:
    parent = destination.parent
    if not parent.exists():
        return
    backups = sorted(parent.glob(f".{destination.name}.backup-*"))
    stages = sorted(parent.glob(f".{destination.name}.staging-*"))
    if len(backups) > 1 or len(stages) > 1:
        raise RuntimeError(f"multiple interrupted M8 analysis swaps found beside {destination}")
    if destination.exists() and backups and stages:
        raise RuntimeError(f"ambiguous interrupted M8 analysis swap found beside {destination}")
    if not destination.exists() and backups:
        backups[0].replace(destination)
        backups = []
    for staged in stages:
        shutil.rmtree(staged)
    if destination.exists() and backups:
        shutil.rmtree(backups[0])


def write_satellite_analysis(result: SatelliteAssociationResult) -> dict[str, Path]:
    """Atomically persist every table and its binding manifest."""
    _validate_result(result)
    year = int(result.manifest["year"])
    raw_generation = result.manifest.get("inventory_generation_sha256")
    generation = raw_generation if isinstance(raw_generation, str) else None
    destination = satellite_analysis_dir(year, generation)
    _recover_swap(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    token = uuid4().hex
    staged = destination.with_name(f".{destination.name}.staging-{token}")
    backup = destination.with_name(f".{destination.name}.backup-{token}")
    staged.mkdir()
    tables = {
        "panel": result.panel,
        "coverage": result.coverage,
        "association": result.association,
        "station_context": result.station_context,
        "month_context": result.month_context,
    }
    try:
        for name, frame in tables.items():
            frame.write_parquet(staged / OUTPUT_FILENAMES[name], compression="zstd")
        (staged / OUTPUT_FILENAMES["manifest"]).write_text(
            json.dumps(result.manifest, ensure_ascii=False, allow_nan=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        if destination.exists():
            destination.replace(backup)
        try:
            staged.replace(destination)
        except BaseException:
            if backup.exists() and not destination.exists():
                backup.replace(destination)
            raise
        if backup.exists():
            shutil.rmtree(backup)
    finally:
        if staged.exists():
            shutil.rmtree(staged)
    return {name: destination / filename for name, filename in OUTPUT_FILENAMES.items()}
