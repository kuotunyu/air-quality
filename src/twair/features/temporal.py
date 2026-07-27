"""Time-of-day, day-of-week and seasonal features.

Averaging to months erased all of this. The daily cycle of PM2.5 is large —
morning traffic, afternoon mixing, evening inversion — and so is the weekly one
where traffic dominates. A monthly mean cannot see any of it, which is the
single biggest reason the 2018 analysis had so little to work with.

Cyclic quantities get sine/cosine pairs for the same reason wind direction
does: hour 23 and hour 0 are adjacent, and a raw integer encoding says they are
maximally far apart.
"""

from __future__ import annotations

import datetime as dt

import polars as pl

__all__ = [
    "TEMPORAL_FEATURES",
    "add_temporal_features",
    "cyclic_encoding",
]

TEMPORAL_FEATURES = (
    "hour_sin",
    "hour_cos",
    "doy_sin",
    "doy_cos",
    "dow_sin",
    "dow_cos",
    "is_weekend",
    "trend_days",
)

# Anchor for the linear trend term. Fixed rather than derived from the data so
# that models fitted on different subsets remain comparable.
TREND_ORIGIN = dt.date(1982, 1, 1)


def cyclic_encoding(expr: pl.Expr, period: float, name: str) -> list[pl.Expr]:
    """Sine/cosine pair for a quantity that wraps every ``period`` units."""
    angle = expr.cast(pl.Float64) * (2.0 * 3.141592653589793 / period)
    return [angle.sin().alias(f"{name}_sin"), angle.cos().alias(f"{name}_cos")]


def add_temporal_features(
    frame: pl.DataFrame,
    *,
    timestamp: str = "ts_local",
) -> pl.DataFrame:
    """Attach cyclic time features and a linear trend term.

    ``trend_days`` carries the long-run change that seasonal terms cannot: it
    is what a meteorological-normalisation model varies to ask "what would the
    concentration have been under average weather?"
    """
    if timestamp not in frame.columns:
        raise KeyError(f"temporal features need {timestamp!r}")

    ts = pl.col(timestamp)
    return frame.with_columns(
        *cyclic_encoding(ts.dt.hour(), 24.0, "hour"),
        *cyclic_encoding(ts.dt.ordinal_day(), 365.25, "doy"),
        *cyclic_encoding(ts.dt.weekday(), 7.0, "dow"),
        (ts.dt.weekday() >= 6).alias("is_weekend"),
        (ts.dt.date() - pl.lit(TREND_ORIGIN)).dt.total_days().alias("trend_days"),
    )
