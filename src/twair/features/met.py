"""Meteorological features.

The centrepiece is wind. A plain specification feeds ``WD_HR`` — a compass bearing —
into correlation, regression and a mixed model as an ordinary continuous
variable. On that scale 1° and 359° sit at opposite ends while being one degree
apart in reality, so any coefficient estimated for it is uninterpretable.

Two encodings replace it, and they answer different questions:

* ``wd_sin`` / ``wd_cos`` — the bearing on the unit circle. Use when direction
  matters regardless of how hard the wind blew.
* ``u`` / ``v`` — the wind vector itself, scaled by speed. Use when transport
  matters, since a 1 m/s northerly and a 15 m/s northerly move very different
  amounts of air.

Both are continuous across north, which is the whole point.
"""

from __future__ import annotations

import polars as pl

__all__ = [
    "WIND_FEATURES",
    "add_wind_features",
    "uv_components",
    "wind_direction_encoding",
]

# Meteorological convention: the reported bearing is the direction the wind
# blows *from*, measured clockwise from north.
WIND_FEATURES = ("wd_sin", "wd_cos", "u", "v")


def wind_direction_encoding(direction: str = "WD_HR") -> list[pl.Expr]:
    """Bearing as a point on the unit circle.

    Continuous across 0°/360°, which a raw bearing is not.
    """
    radians = pl.col(direction).radians()
    return [radians.sin().alias("wd_sin"), radians.cos().alias("wd_cos")]


def uv_components(direction: str = "WD_HR", speed: str = "WS_HR") -> list[pl.Expr]:
    """Wind vector components, in the direction the air is travelling.

    ``u`` is eastward, ``v`` northward. The negation converts "comes from" into
    "blows toward": a northerly (0°) has v < 0, since it moves air southward.
    """
    radians = pl.col(direction).radians()
    return [
        (-pl.col(speed) * radians.sin()).alias("u"),
        (-pl.col(speed) * radians.cos()).alias("v"),
    ]


def add_wind_features(
    frame: pl.DataFrame,
    *,
    direction: str = "WD_HR",
    speed: str = "WS_HR",
) -> pl.DataFrame:
    """Attach both wind encodings, leaving the raw bearing in place.

    The raw column stays so that M3 can show, on one dataset, what the baseline
    obtained by using it directly.
    """
    missing = [c for c in (direction, speed) if c not in frame.columns]
    if missing:
        raise KeyError(f"wind features need {missing}")
    return frame.with_columns(
        *wind_direction_encoding(direction),
        *uv_components(direction, speed),
    )
