"""Give the "no rain" flag the value it actually represents.

``NR`` (無降雨) is not a missing reading — it records that the gauge measured
no rain. About 90% of hourly rainfall cells carry it. Treating it as absent
turns "mean rainfall" into "mean rainfall intensity while it was raining", an
order of magnitude larger. The 2018 replication surfaced this: reproducing the
original's monthly mean rainfall gave 2.32 mm against its published 0.23, and
counting ``NR`` as zero gives 0.2296.

The conversion is **pollutant-specific and must stay that way**. For rainfall
amount and intensity, no rain is genuinely zero. For the acid-rain
measurements it is not: the pH of rain that never fell is undefined, and
coercing it to 0 would manufacture extremely acidic rain out of dry weather.
``conf/pollutants.yaml`` marks which is which via ``no_rain_is_zero``.
"""

from __future__ import annotations

import logging
from typing import Any

import polars as pl

from twair.config import load_conf
from twair.qc.flags import Flag

log = logging.getLogger(__name__)

__all__ = ["USABLE_FLAGS", "apply_no_rain_zero", "no_rain_zero_pollutants", "usable"]

# Flags whose rows carry a real measurement. NO_RAIN belongs here for the
# pollutants where zero is the true value; elsewhere its value stays null and
# the null check excludes it anyway.
USABLE_FLAGS = (Flag.VALID.value, Flag.NO_RAIN.value)


def usable(value_column: str = "value", flag_column: str = "flag") -> pl.Expr:
    """Predicate for rows that carry a usable measurement."""
    return pl.col(flag_column).is_in(USABLE_FLAGS) & pl.col(value_column).is_not_null()


def no_rain_zero_pollutants(config: dict[str, Any] | None = None) -> set[str]:
    """Pollutants for which ``NR`` means a measured zero."""
    conf = config if config is not None else load_conf("pollutants")
    return {
        code
        for code, meta in conf.get("pollutants", {}).items()
        if meta.get("no_rain_is_zero") is True
    }


def apply_no_rain_zero(
    frame: pl.DataFrame,
    *,
    config: dict[str, Any] | None = None,
) -> pl.DataFrame:
    """Set ``value`` to 0 where ``NR`` denotes a measured zero.

    The flag is left as ``no_rain`` rather than rewritten to ``valid``: how the
    agency recorded it is worth keeping, and downstream code selects on
    :data:`USABLE_FLAGS` rather than on ``valid`` alone.
    """
    targets = no_rain_zero_pollutants(config)
    if not targets:
        return frame

    is_measured_zero = (
        (pl.col("flag") == Flag.NO_RAIN.value)
        & pl.col("pollutant").is_in(list(targets))
        & pl.col("value").is_null()
    )

    return frame.with_columns(
        pl.when(is_measured_zero).then(0.0).otherwise(pl.col("value")).alias("value")
    )
