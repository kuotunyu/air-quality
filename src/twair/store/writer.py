"""Write the canonical observation table as Hive-partitioned Parquet.

Partitioning by ``year=…/month=…`` lets both DuckDB and the browser-side
DuckDB-WASM prune whole files for time-ranged queries, which is what most of
the analysis and the whole website do.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

import polars as pl

from twair.paths import processed_dir
from twair.store.schema import conform, validate

log = logging.getLogger(__name__)

OBSERVATIONS = "observations"
COMPRESSION = "zstd"


def partition_path(root: Path, year: int, month: int) -> Path:
    return root / f"year={year}" / f"month={month:02d}"


def write_observations(
    frame: pl.DataFrame,
    *,
    root: Path | None = None,
    overwrite_partitions: bool = True,
) -> list[Path]:
    """Validate and write one frame, splitting it across year/month partitions.

    Partitions are replaced wholesale rather than appended to, so re-ingesting
    a year is idempotent instead of silently doubling its rows.
    """
    target = root or processed_dir(OBSERVATIONS)
    target.mkdir(parents=True, exist_ok=True)

    validated = validate(conform(frame), context="write_observations")

    written: list[Path] = []
    for (year, month), part in validated.group_by(["year", "month"], maintain_order=True):
        directory = partition_path(target, int(year), int(month))
        if overwrite_partitions and directory.exists():
            shutil.rmtree(directory)
        directory.mkdir(parents=True, exist_ok=True)

        path = directory / "part-0.parquet"
        part.drop("year", "month").write_parquet(path, compression=COMPRESSION, statistics=True)
        written.append(path)

    log.info("wrote %d partition(s), %d rows", len(written), validated.height)
    return written


def scan_observations(root: Path | None = None) -> pl.LazyFrame:
    """Lazily scan every partition, restoring the partition keys as columns."""
    target = root or processed_dir(OBSERVATIONS)
    pattern = str(target / "**" / "*.parquet")
    return pl.scan_parquet(pattern, hive_partitioning=True)


def partition_summary(root: Path | None = None) -> pl.DataFrame:
    """Row counts per year — the cheapest sanity check after an ingest.

    The year is derived from ``ts_local`` rather than the Hive partition key:
    grouping directly on a partition column makes Polars push the aggregation
    into each file and report per-file totals as if they were global. Deriving
    it from the data also means the answer reflects the rows themselves, not
    the directory they happen to sit in.
    """
    return (
        scan_observations(root)
        .select(
            pl.col("ts_local").dt.year().alias("year"),
            "station_name",
            "pollutant",
            "flag",
        )
        .group_by("year")
        .agg(
            pl.len().alias("rows"),
            pl.col("station_name").n_unique().alias("stations"),
            pl.col("pollutant").n_unique().alias("pollutants"),
            (pl.col("flag") == "valid").sum().alias("valid"),
        )
        .with_columns((pl.col("valid") / pl.col("rows")).round(4).alias("valid_ratio"))
        .sort("year")
        .collect()
    )
