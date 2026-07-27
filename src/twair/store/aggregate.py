"""Daily and monthly aggregates, done properly.

Two things the 2018 project got wrong at exactly this step, both fixed here:

**Coverage is checked before averaging.** That project collapsed hourly data to
monthly means with no record of how many valid hours went into each value, so a
station reporting three hours in a month carried the same weight as one
reporting seven hundred. Here every aggregate ships with ``n_valid`` and
``coverage_ratio``, and falls to null when coverage is below the configured
threshold rather than emitting a confidently wrong number.

**Wind direction is averaged as a vector.** Direction is circular: the
arithmetic mean of 350° and 10° is 180°, pointing due south when the true
answer is due north. That project fed monthly *arithmetic* means of ``WD_HR``
into correlation, regression and a mixed model. Those numbers cannot be
interpreted. Circular quantities here are averaged as unit vectors, and the
resultant length is retained as ``*_consistency`` — a value near 0 means the
wind was so variable that no mean direction is meaningful, which is itself
information the original had no way to express.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import polars as pl

from twair.config import load_conf
from twair.paths import processed_dir
from twair.qc.rainfall import usable
from twair.store.stations import normalise_name_expr
from twair.store.writer import scan_observations

log = logging.getLogger(__name__)

__all__ = [
    "aggregate_daily",
    "aggregate_monthly",
    "circular_mean_expr",
    "circular_resultant_expr",
]

HOURS_PER_DAY = 24


def circular_variables(config: dict[str, Any] | None = None) -> set[str]:
    """Pollutants flagged ``circular: true`` in conf/pollutants.yaml."""
    conf = config if config is not None else load_conf("pollutants")
    return {
        code for code, meta in conf.get("pollutants", {}).items() if meta.get("circular") is True
    }


def circular_mean_expr(column: str = "value") -> pl.Expr:
    """Mean direction in degrees, via unit-vector averaging.

    ``atan2(mean(sin θ), mean(cos θ))``, wrapped into [0, 360).
    """
    radians = pl.col(column).radians()
    sin_mean = radians.sin().mean()
    cos_mean = radians.cos().mean()
    return (pl.arctan2(sin_mean, cos_mean).degrees() + 360.0) % 360.0


def circular_resultant_expr(column: str = "value") -> pl.Expr:
    """Resultant vector length in [0, 1] — how consistent the direction was.

    1.0 means every reading pointed the same way; near 0 means the mean
    direction carries essentially no information.
    """
    radians = pl.col(column).radians()
    sin_mean = radians.sin().mean()
    cos_mean = radians.cos().mean()
    return (sin_mean.pow(2) + cos_mean.pow(2)).sqrt()


def _thresholds(config: dict[str, Any] | None = None) -> tuple[int, float]:
    conf = config if config is not None else load_conf("qc")
    agg = conf.get("aggregation", {})
    min_hours = int(agg.get("hourly_to_daily", {}).get("min_valid_hours", 16))
    min_days_ratio = float(agg.get("daily_to_monthly", {}).get("min_valid_days_ratio", 0.75))
    return min_hours, min_days_ratio


def aggregate_daily(
    root: Path | None = None,
    *,
    config: dict[str, Any] | None = None,
    qc_config: dict[str, Any] | None = None,
) -> pl.DataFrame:
    """Hourly -> daily, separating circular from linear pollutants."""
    circular = circular_variables(config)
    min_hours, _ = _thresholds(qc_config)

    valid = (
        scan_observations(root)
        .filter(usable())
        .select(
            normalise_name_expr(),
            pl.col("pollutant").cast(pl.Utf8),
            pl.col("ts_local").dt.date().alias("date"),
            pl.col("value").cast(pl.Float64),
        )
    )

    linear = (
        valid.filter(~pl.col("pollutant").is_in(list(circular)))
        .group_by("station_name", "pollutant", "date")
        .agg(
            pl.col("value").mean().alias("mean"),
            pl.col("value").min().alias("min"),
            pl.col("value").max().alias("max"),
            pl.len().alias("n_valid"),
        )
        .with_columns(pl.lit(None, dtype=pl.Float64).alias("consistency"))
    )

    circular_agg = (
        valid.filter(pl.col("pollutant").is_in(list(circular)))
        .group_by("station_name", "pollutant", "date")
        .agg(
            circular_mean_expr().alias("mean"),
            pl.col("value").min().alias("min"),
            pl.col("value").max().alias("max"),
            pl.len().alias("n_valid"),
            circular_resultant_expr().alias("consistency"),
        )
    )

    combined = pl.concat([linear, circular_agg], how="vertical_relaxed")

    return (
        combined.with_columns(
            (pl.col("n_valid") / HOURS_PER_DAY).alias("coverage_ratio"),
            pl.col("pollutant").is_in(list(circular)).alias("is_circular"),
        )
        .with_columns(
            # Below-threshold days keep their counts but surrender the mean:
            # the evidence of sparseness is more useful than a biased number.
            pl.when(pl.col("n_valid") >= min_hours)
            .then(pl.col("mean"))
            .otherwise(None)
            .alias("mean"),
            (pl.col("n_valid") >= min_hours).alias("meets_threshold"),
        )
        .sort("date", "station_name", "pollutant")
        .collect()
    )


def aggregate_monthly(
    daily: pl.DataFrame,
    *,
    config: dict[str, Any] | None = None,
    qc_config: dict[str, Any] | None = None,
) -> pl.DataFrame:
    """Daily -> monthly, from days that met the daily threshold.

    Monthly means are built only from qualifying days, and a month that does
    not reach ``min_valid_days_ratio`` yields a null mean rather than one drawn
    from a handful of days.
    """
    circular = circular_variables(config)
    _, min_days_ratio = _thresholds(qc_config)

    qualifying = daily.lazy().filter(pl.col("meets_threshold") & pl.col("mean").is_not_null())

    linear = (
        qualifying.filter(~pl.col("pollutant").is_in(list(circular)))
        .group_by(
            "station_name",
            "pollutant",
            pl.col("date").dt.truncate("1mo").alias("month"),
        )
        .agg(
            pl.col("mean").mean().alias("mean"),
            pl.col("min").min().alias("min"),
            pl.col("max").max().alias("max"),
            pl.len().alias("n_days"),
            pl.col("n_valid").sum().alias("n_valid_hours"),
        )
        .with_columns(pl.lit(None, dtype=pl.Float64).alias("consistency"))
    )

    circular_agg = (
        qualifying.filter(pl.col("pollutant").is_in(list(circular)))
        .group_by(
            "station_name",
            "pollutant",
            pl.col("date").dt.truncate("1mo").alias("month"),
        )
        .agg(
            circular_mean_expr("mean").alias("mean"),
            pl.col("min").min().alias("min"),
            pl.col("max").max().alias("max"),
            pl.len().alias("n_days"),
            pl.col("n_valid").sum().alias("n_valid_hours"),
            circular_resultant_expr("mean").alias("consistency"),
        )
    )

    combined = pl.concat([linear, circular_agg], how="vertical_relaxed")

    return (
        combined.with_columns(
            pl.col("month").dt.month_end().dt.day().cast(pl.Int32).alias("days_in_month")
        )
        .with_columns((pl.col("n_days") / pl.col("days_in_month")).alias("coverage_ratio"))
        .with_columns(
            pl.when(pl.col("coverage_ratio") >= min_days_ratio)
            .then(pl.col("mean"))
            .otherwise(None)
            .alias("mean"),
            (pl.col("coverage_ratio") >= min_days_ratio).alias("meets_threshold"),
            pl.col("pollutant").is_in(list(circular)).alias("is_circular"),
        )
        .sort("month", "station_name", "pollutant")
        .collect()
    )


def _write(frame: pl.DataFrame, table: str, name: str) -> Path:
    """Write one aggregate, creating its directory first.

    ``processed_dir`` resolves a path without creating it, so a table that is
    not part of the standard tree needs the mkdir here — otherwise a full
    aggregation runs to completion and then dies on the write.
    """
    destination = processed_dir(table)
    destination.mkdir(parents=True, exist_ok=True)
    path = destination / name
    frame.write_parquet(path, compression="zstd")
    return path


def build_aggregates(root: Path | None = None) -> dict[str, pl.DataFrame]:
    """Produce and persist both aggregate tables."""
    daily = aggregate_daily(root)
    _write(daily, "daily", "daily.parquet")

    monthly = aggregate_monthly(daily)
    _write(monthly, "monthly", "monthly.parquet")

    return {"daily": daily, "monthly": monthly}
