"""Chemical ratios and combinations.

Individual pollutant concentrations answer "how much"; ratios between them
describe relative composition. A plain specification uses each pollutant only in
isolation, and used PM10 as a predictor rather than as a reference.

``PM10`` appears here, and only here. It is excluded from the predictor set
because PM2.5 is a physical subset of it, but the *ratio* is not a leak: it
carries no information about the PM2.5 level on its own, only about particle-
size composition. It can screen source hypotheses but cannot uniquely identify
or quantify a source.
"""

from __future__ import annotations

import polars as pl

__all__ = ["CHEM_FEATURES", "add_chem_features"]

CHEM_FEATURES = ("ox", "pm_ratio", "so2_nox_ratio", "no2_nox_ratio")

# Ratios blow up as the denominator approaches zero. Below this the ratio is
# not informative, so it is nulled rather than allowed to dominate a model.
_MIN_DENOMINATOR = 1e-3


def _safe_ratio(numerator: str, denominator: str, name: str) -> pl.Expr:
    return (
        pl.when(pl.col(denominator).abs() > _MIN_DENOMINATOR)
        .then(pl.col(numerator) / pl.col(denominator))
        .otherwise(None)
        .alias(name)
    )


def add_chem_features(frame: pl.DataFrame) -> pl.DataFrame:
    """Attach chemical combinations, where their inputs are present.

    Each feature is added only if the columns it needs exist, so a station or
    era measuring a reduced set still yields whatever is computable rather than
    failing outright.
    """
    columns = set(frame.columns)
    expressions: list[pl.Expr] = []

    if {"O3", "NO2"} <= columns:
        # Total oxidant. O3 and NO2 interconvert rapidly near sources, so their
        # sum is conserved where either alone is not, making it the more stable
        # measure of photochemical activity.
        expressions.append((pl.col("O3") + pl.col("NO2")).alias("ox"))

    if {"PM2.5", "PM10"} <= columns:
        # Fine fraction. It records particle-size composition and can screen
        # source hypotheses, but cannot uniquely identify or quantify a source.
        expressions.append(_safe_ratio("PM2.5", "PM10", "pm_ratio"))

    if {"SO2", "NOx"} <= columns:
        # Sulphur against nitrogen: stationary combustion versus traffic.
        expressions.append(_safe_ratio("SO2", "NOx", "so2_nox_ratio"))

    if {"NO2", "NOx"} <= columns:
        # Photochemical age. Fresh emissions are mostly NO, so a ratio near 1
        # means the air mass has had time to oxidise.
        expressions.append(_safe_ratio("NO2", "NOx", "no2_nox_ratio"))

    return frame.with_columns(expressions) if expressions else frame
