"""Wide (one row per station-hour) view for modelling.

The long table is the source of truth because it can record *why* a value is
absent. Modelling wants a matrix, so this pivots — but the pivot is a derived
artefact, regenerated rather than edited, and it deliberately keeps the
distinction the 2018 project erased: a cell is null here because the reading
was missing, rejected, or never measured at that station, and the companion
``*_flag`` columns say which.

⚠️ **Nothing in the pipeline calls this yet.** M2, M9 and the Space bundle each
pivot their own frame inline, so `build_wide` and `scan_wide` have no callers
outside the tests — which is how `scan_wide` came to raise `SchemaError` on any
multi-decade store without anyone noticing. That is fixed, but the choice
between wiring this in and deleting it in favour of the three inline pivots is
still open.
"""

from __future__ import annotations

import logging
from pathlib import Path

import polars as pl

from twair.paths import processed_dir
from twair.qc.flags import Flag
from twair.store.stations import normalise_name_expr
from twair.store.writer import scan_observations

log = logging.getLogger(__name__)

__all__ = ["build_wide", "wide_year"]

WIDE = "hourly_wide"

# PM10 is excluded from the modelling default on purpose. PM2.5 is a physical
# subset of PM10, so regressing one on the other is a definitional identity
# dressed up as a finding — it was the single largest contributor to the 2018
# project's apparent explanatory power. It stays available in the long table
# and in `chem` ratio features, but it is not a default predictor.
LEAKING_PREDICTORS = ("PM10",)


def wide_year(year: int, root: Path | None = None) -> pl.DataFrame:
    """Pivot one year into station-hour rows."""
    valid = (
        scan_observations(root)
        .filter(pl.col("ts_local").dt.year() == year)
        .select(
            normalise_name_expr(),
            pl.col("pollutant").cast(pl.Utf8),
            "ts_local",
            "value",
            pl.col("flag").cast(pl.Utf8),
        )
        .collect()
    )
    if valid.is_empty():
        return valid

    values = valid.pivot(
        on="pollutant",
        index=["station_name", "ts_local"],
        values="value",
        aggregate_function="first",
    )

    # Retaining the flag alongside each value is what lets a later analysis ask
    # "was this absent or rejected?" — a question the original could not pose.
    flags = valid.pivot(
        on="pollutant",
        index=["station_name", "ts_local"],
        values="flag",
        aggregate_function="first",
    ).rename(lambda name: name if name in {"station_name", "ts_local"} else f"{name}_flag")

    return values.join(flags, on=["station_name", "ts_local"], how="left").sort(
        "station_name", "ts_local"
    )


def build_wide(years: list[int], root: Path | None = None) -> dict[int, int]:
    """Build and persist the wide table, one Parquet file per year."""
    destination = processed_dir(WIDE)
    destination.mkdir(parents=True, exist_ok=True)

    written: dict[int, int] = {}
    for year in years:
        frame = wide_year(year, root)
        if frame.is_empty():
            log.warning("no data for %s", year)
            continue
        frame.write_parquet(destination / f"{year}.parquet", compression="zstd")
        written[year] = frame.height
        log.info("wide %s: %d station-hours, %d columns", year, frame.height, frame.width)
    return written


def scan_wide(root: Path | None = None) -> pl.LazyFrame:
    """Every year's wide file as one lazy frame.

    This was ``pl.scan_parquet(target / "*.parquet")``, and a glob scan needs
    one schema. These files do not have one: :func:`wide_year` pivots on the
    pollutants a year actually contains, and the network's measurands changed
    over four decades. 1994 has no PM2.5 column at all; 2015 does. Files sort
    numerically, so the oldest and narrowest year is always the anchor and every
    later one is 「extra column in file outside of expected schema」.

    Measured on a two-year fixture: 1994 pivots to 6 columns, 2015 to 8, and the
    scan raised ``SchemaError`` on PM2.5 — meaning it failed on every store
    spanning a measurand's introduction, which is every real one.

    ``diagonal_relaxed`` is what :mod:`twair.build` already uses for this exact
    shape, a column that appears partway through the record. Years before a
    measurand existed read null, which is the honest value: the long table is
    still where 「missing」, 「rejected」 and 「never measured here」 are told
    apart, and that is why it remains the source of truth.
    """
    target = root or processed_dir(WIDE)
    files = sorted(target.glob("*.parquet"))
    if not files:
        return pl.LazyFrame()
    return pl.concat([pl.scan_parquet(path) for path in files], how="diagonal_relaxed")


def modelling_columns(frame: pl.DataFrame | pl.LazyFrame) -> list[str]:
    """Predictor columns, with definitional leaks and flag columns removed."""
    columns = frame.columns if isinstance(frame, pl.DataFrame) else frame.collect_schema().names()
    return [
        c
        for c in columns
        if not c.endswith("_flag")
        and c not in {"station_name", "ts_local", "PM2.5", *LEAKING_PREDICTORS}
    ]


def valid_only(frame: pl.DataFrame, pollutant: str) -> pl.DataFrame:
    """Null out a pollutant wherever its flag is not ``valid``."""
    flag_column = f"{pollutant}_flag"
    if flag_column not in frame.columns:
        return frame
    return frame.with_columns(
        pl.when(pl.col(flag_column) == Flag.VALID.value)
        .then(pl.col(pollutant))
        .otherwise(None)
        .alias(pollutant)
    )
