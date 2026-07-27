"""Canonical schema for the observation table.

The long form is the source of truth: one row per
(station, timestamp, pollutant). Wide tables for modelling are derived from it
by pivoting, never the other way round — pivoting first would force a decision
about missing combinations before we have recorded why they are missing.
"""

from __future__ import annotations

import polars as pl

from twair.qc.flags import Flag

__all__ = [
    "OBSERVATION_SCHEMA",
    "SchemaError",
    "conform",
    "validate",
]


class SchemaError(RuntimeError):
    """Raised when a frame does not satisfy the canonical schema."""


OBSERVATION_SCHEMA: dict[str, pl.DataType] = {
    # --- identity ---
    "station_name": pl.Categorical,
    "pollutant": pl.Categorical,
    "ts_local": pl.Datetime("us"),
    # --- measurement ---
    "value": pl.Float32,
    "flag": pl.Categorical,
    # --- provenance ---
    "value_retained": pl.Boolean,
    "imputed": pl.Boolean,
    "impute_method": pl.Categorical,
    "generation": pl.Categorical,
    "source_member": pl.Utf8,
    # --- partition keys ---
    "year": pl.Int16,
    "month": pl.Int8,
}

REQUIRED_COLUMNS = tuple(OBSERVATION_SCHEMA)

VALID_FLAGS = frozenset(f.value for f in Flag)


def conform(frame: pl.DataFrame) -> pl.DataFrame:
    """Coerce a parsed frame into the canonical schema.

    Adds the columns the ingest stage does not produce (imputation provenance
    defaults to "untouched") and derives the partition keys.
    """
    out = frame

    if "imputed" not in out.columns:
        out = out.with_columns(pl.lit(False).alias("imputed"))
    if "impute_method" not in out.columns:
        out = out.with_columns(pl.lit(None, dtype=pl.Utf8).alias("impute_method"))

    out = out.with_columns(
        pl.col("ts_local").dt.year().cast(pl.Int16).alias("year"),
        pl.col("ts_local").dt.month().cast(pl.Int8).alias("month"),
    )

    missing = [c for c in REQUIRED_COLUMNS if c not in out.columns]
    if missing:
        raise SchemaError(f"cannot conform: missing columns {missing}")

    return out.select([pl.col(name).cast(dtype) for name, dtype in OBSERVATION_SCHEMA.items()])


def validate(frame: pl.DataFrame, *, context: str = "") -> pl.DataFrame:
    """Assert the canonical schema and the invariants that must always hold.

    Deliberately strict: a pipeline that quietly writes malformed Parquet is
    worse than one that stops.
    """
    where = f" ({context})" if context else ""

    missing = [c for c in REQUIRED_COLUMNS if c not in frame.columns]
    if missing:
        raise SchemaError(f"missing columns{where}: {missing}")

    extra = [c for c in frame.columns if c not in OBSERVATION_SCHEMA]
    if extra:
        raise SchemaError(f"unexpected columns{where}: {extra}")

    for name, expected in OBSERVATION_SCHEMA.items():
        actual = frame.schema[name]
        if actual != expected:
            raise SchemaError(f"column {name!r} is {actual}, expected {expected}{where}")

    if frame["ts_local"].null_count():
        raise SchemaError(f"ts_local contains nulls{where}")

    if frame["flag"].null_count():
        raise SchemaError(f"flag contains nulls{where}")

    unknown = set(frame["flag"].cast(pl.Utf8).unique().to_list()) - VALID_FLAGS
    if unknown:
        raise SchemaError(f"unknown flag values{where}: {sorted(unknown)}")

    # A valid reading must actually have a number behind it.
    orphans = frame.filter(
        (pl.col("flag").cast(pl.Utf8) == Flag.VALID.value) & pl.col("value").is_null()
    ).height
    if orphans:
        raise SchemaError(f"{orphans} rows flagged valid but carry no value{where}")

    return frame
