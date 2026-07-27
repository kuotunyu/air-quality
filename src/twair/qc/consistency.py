"""Range and physical-consistency checks.

Two kinds of problem, deliberately handled differently:

* **Range violations** are a property of a single reading, so they update that
  reading's flag.
* **Consistency violations** are a property of a *set* of readings taken
  together (``PM2.5 <= PM10``, ``NO + NO2 = NOx``). Blaming one member of the
  set would be arbitrary, so these are reported rather than flagged.

Nothing here deletes data. The violation *rate* — which station, which year,
which pollutant — is itself a result worth publishing, and the 2018 project's
practice of quietly substituting neighbouring stations made exactly this
invisible.
"""

from __future__ import annotations

import logging
from typing import Any

import polars as pl

from twair.config import load_conf
from twair.qc.flags import Flag

log = logging.getLogger(__name__)

__all__ = [
    "check_consistency",
    "check_ranges",
    "range_report",
]


def _ranges(config: dict[str, Any] | None = None) -> dict[str, tuple[float, float]]:
    conf = config if config is not None else load_conf("pollutants")
    out: dict[str, tuple[float, float]] = {}
    for code, meta in conf.get("pollutants", {}).items():
        bounds = meta.get("valid_range")
        if bounds and len(bounds) == 2:
            out[code] = (float(bounds[0]), float(bounds[1]))
    return out


def check_ranges(
    frame: pl.DataFrame,
    *,
    config: dict[str, Any] | None = None,
) -> pl.DataFrame:
    """Flag readings outside their pollutant's physically plausible domain.

    Only currently-``valid`` readings are reconsidered; an agency rejection is
    left as it stands. The value is kept so the outlier remains inspectable —
    a PM2.5 of 1500 might be a broken instrument or it might be a dust storm,
    and that is a question for analysis, not for the parser.
    """
    ranges = _ranges(config)
    if not ranges:
        return frame

    lower = pl.col("pollutant").replace_strict(
        {k: v[0] for k, v in ranges.items()}, return_dtype=pl.Float64, default=None
    )
    upper = pl.col("pollutant").replace_strict(
        {k: v[1] for k, v in ranges.items()}, return_dtype=pl.Float64, default=None
    )

    out_of_range = (
        (pl.col("flag") == Flag.VALID.value)
        & pl.col("value").is_not_null()
        & lower.is_not_null()
        & ((pl.col("value") < lower) | (pl.col("value") > upper))
    )

    return frame.with_columns(
        pl.when(out_of_range)
        .then(pl.lit(Flag.OUT_OF_RANGE.value))
        .otherwise(pl.col("flag"))
        .alias("flag")
    )


def range_report(frame: pl.DataFrame) -> pl.DataFrame:
    """Out-of-range counts per pollutant and year."""
    flagged = frame.filter(pl.col("flag") == Flag.OUT_OF_RANGE.value)
    if flagged.is_empty():
        return pl.DataFrame(schema={"year": pl.Int32, "pollutant": pl.Utf8, "n": pl.UInt32})
    return (
        flagged.with_columns(pl.col("ts_local").dt.year().cast(pl.Int32).alias("year"))
        .group_by("year", "pollutant")
        .len(name="n")
        .sort("year", "pollutant")
    )


# Rules are expressed as (name, required pollutants, predicate factory).
# The predicate receives the wide frame and returns a boolean column that is
# True when the rule HOLDS.
def _pm_subset(_: pl.DataFrame) -> pl.Expr:
    return pl.col("PM2.5") <= pl.col("PM10")


def _nox_identity(_: pl.DataFrame) -> pl.Expr:
    # NO + NO2 = NOx by definition; tolerance absorbs rounding at low levels.
    return (pl.col("NO") + pl.col("NO2") - pl.col("NOx")).abs() <= (0.1 * pl.col("NOx").abs() + 1.0)


def _nmhc_identity(_: pl.DataFrame) -> pl.Expr:
    return (pl.col("THC") - pl.col("CH4") - pl.col("NMHC")).abs() <= (
        0.05 * pl.col("THC").abs() + 0.05
    )


CONSISTENCY_RULES: dict[str, tuple[tuple[str, ...], Any]] = {
    "pm_subset": (("PM2.5", "PM10"), _pm_subset),
    "nox_identity": (("NO", "NO2", "NOx"), _nox_identity),
    "nmhc_identity": (("THC", "CH4", "NMHC"), _nmhc_identity),
}


def check_consistency(frame: pl.DataFrame) -> pl.DataFrame:
    """Evaluate cross-pollutant identities, returning a violation report.

    Returns one row per (year, station, rule) with the number of hours checked
    and the number that failed. An empty result means every rule held wherever
    it could be evaluated.
    """
    valid = frame.filter((pl.col("flag") == Flag.VALID.value) & pl.col("value").is_not_null())
    if valid.is_empty():
        return _empty_consistency()

    wide = valid.pivot(
        on="pollutant",
        index=["station_name", "ts_local"],
        values="value",
        aggregate_function="first",
    )

    rows: list[pl.DataFrame] = []
    for rule, (required, predicate) in CONSISTENCY_RULES.items():
        missing = [p for p in required if p not in wide.columns]
        if missing:
            log.debug("skipping rule %s: missing %s", rule, missing)
            continue

        evaluable = wide.filter(pl.all_horizontal([pl.col(p).is_not_null() for p in required]))
        if evaluable.is_empty():
            continue

        rows.append(
            evaluable.with_columns(
                pl.col("ts_local").dt.year().cast(pl.Int32).alias("year"),
                predicate(evaluable).alias("holds"),
            )
            .group_by("year", "station_name")
            .agg(
                pl.len().alias("checked"),
                (~pl.col("holds")).sum().cast(pl.UInt32).alias("violations"),
            )
            .with_columns(pl.lit(rule).alias("rule"))
        )

    if not rows:
        return _empty_consistency()

    return (
        pl.concat(rows, how="vertical_relaxed")
        .with_columns((pl.col("violations") / pl.col("checked")).alias("violation_rate"))
        .select("year", "station_name", "rule", "checked", "violations", "violation_rate")
        .sort("year", "station_name", "rule")
    )


def _empty_consistency() -> pl.DataFrame:
    return pl.DataFrame(
        schema={
            "year": pl.Int32,
            "station_name": pl.Utf8,
            "rule": pl.Utf8,
            "checked": pl.UInt32,
            "violations": pl.UInt32,
            "violation_rate": pl.Float64,
        }
    )
