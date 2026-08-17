"""M1 — the naive monthly-mean OLS baseline, built wrong on purpose.

This module takes the discarded option at every decision point rather than the
corrected one. That is the point: a claim of the form "fixing X changes the
conclusion" needs a baseline that actually contains X, fitted on the same rows.

It therefore **does not** use :mod:`twair.store.aggregate`, which averages
circular quantities as vectors and withholds means below a coverage threshold.
This baseline does neither, because doing both correctly is what it exists to be
contrasted against.

The specification it fits:

* period 2010–2017, monthly means per station
* hourly values averaged straight to monthly, with no daily step and no check
  on how many hours contributed
* wind direction averaged arithmetically along with everything else
* PM10 used as a predictor of PM2.5, a definitional overlap rather than an
  empirical finding
* NO, NO2 and NOx entered together, though NO + NO2 = NOx by definition

M3 prices each of those choices against the corrected alternative. M6 goes
further and reconstructs *this* fit to test its residuals and re-price its
t-statistics under a two-way correction, so the frames written here are an input
to another analysis module and not only to a report.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import polars as pl

from twair.paths import outputs_dir
from twair.qc.rainfall import usable
from twair.store.stations import normalise_name_expr
from twair.store.writer import scan_observations

log = logging.getLogger(__name__)

__all__ = [
    "BASELINE_PREDICTORS",
    "BaselineResult",
    "naive_monthly_panel",
    "run_baseline",
    "write_baseline_report",
]

# The response and the twelve predictors the baseline specification uses.
RESPONSE = "PM2.5"
BASELINE_PREDICTORS = (
    "AMB_TEMP",
    "CO",
    "NO",
    "NO2",
    "NOx",
    "O3",
    "PM10",
    "RAINFALL",
    "RH",
    "SO2",
    "WD_HR",
    "WS_HR",
)
BASELINE_PERIOD = (2010, 2017)


def naive_monthly_panel(
    root: Path | None = None,
    *,
    period: tuple[int, int] = BASELINE_PERIOD,
    valid_only: bool = True,
) -> pl.DataFrame:
    """Monthly arithmetic means per station — the naive aggregation.

    Three deliberate departures from :func:`twair.store.aggregate.aggregate_daily`:

    1. **Arithmetic mean for every variable**, wind direction included. This is
       wrong for a circular quantity, and being wrong here is the point: M3
       measures what it costs.
    2. **No coverage threshold.** A station-month built from three valid hours
       carries the same weight as one built from seven hundred.
    3. **No daily intermediate.** Hours are averaged straight to months.

    ``valid_only`` controls whether agency-rejected readings are excluded. The
    specification settles it either way, so the choice is exposed as a parameter
    and its effect measured rather than assumed.
    """
    start, end = period
    needed = [RESPONSE, *BASELINE_PREDICTORS]

    in_period = scan_observations(root).filter(pl.col("ts_local").dt.year().is_between(start, end))

    # A store that never recorded a required pollutant is a broken input and
    # must say so. A store where quality filtering happens to remove every row
    # of one is a legitimate empty result. Checking the inventory *before*
    # filtering keeps those two apart.
    inventory = set(
        in_period.select(pl.col("pollutant").cast(pl.Utf8)).unique().collect()["pollutant"]
    )
    absent = [c for c in needed if c not in inventory]
    if absent:
        raise RuntimeError(f"store lacks pollutants required for replication: {absent}")

    observations = in_period
    if valid_only:
        observations = observations.filter(usable())

    monthly = (
        observations.filter(pl.col("value").is_not_null())
        .select(
            normalise_name_expr(),
            pl.col("pollutant").cast(pl.Utf8),
            pl.col("ts_local").dt.truncate("1mo").alias("month"),
            pl.col("value").cast(pl.Float64),
        )
        .group_by("station_name", "pollutant", "month")
        .agg(
            pl.col("value").mean().alias("value"),
            pl.len().alias("n_hours"),
        )
        .collect()
    )

    wide = monthly.pivot(
        on="pollutant",
        index=["station_name", "month"],
        values="value",
        aggregate_function="first",
    )

    # Filtering may have emptied a column entirely; treat that as no data
    # rather than as a missing input, which was ruled out above.
    for column in needed:
        if column not in wide.columns:
            wide = wide.with_columns(pl.lit(None, dtype=pl.Float64).alias(column))

    # Complete cases only. A row missing any of the thirteen variables cannot
    # enter an OLS that uses all of them, and dropping it here keeps the panel
    # and the fit describing the same rows.
    return (
        wide.select("station_name", "month", *needed)
        .drop_nulls(needed)
        .sort("station_name", "month")
    )


@dataclass
class BaselineResult:
    panel: pl.DataFrame
    descriptive: pl.DataFrame
    correlations: pl.DataFrame
    ols: pl.DataFrame
    n: int
    n_stations: int


def _descriptive(panel: pl.DataFrame) -> pl.DataFrame:
    rows = []
    for column in [RESPONSE, *BASELINE_PREDICTORS]:
        series = panel[column]
        rows.append(
            {
                "variable": column,
                "min": series.min(),
                "median": series.median(),
                "mean": series.mean(),
                "max": series.max(),
                "sd": series.std(),
            }
        )
    return pl.DataFrame(rows)


def _correlations(panel: pl.DataFrame) -> pl.DataFrame:
    rows = [
        {
            "variable": column,
            "r": panel.select(pl.corr(RESPONSE, column)).item(),
        }
        for column in BASELINE_PREDICTORS
    ]
    return pl.DataFrame(rows)


def _ols(panel: pl.DataFrame) -> pl.DataFrame:
    """OLS with VIF, fitting the baseline specification exactly.

    NO, NO2 and NOx all enter together even though NO + NO2 = NOx by
    definition. The design is singular in all but rounding error, which is what
    drives the VIFs into the tens of thousands — the measurement M3 reports.
    """
    import numpy as np
    import statsmodels.api as sm
    from statsmodels.stats.outliers_influence import variance_inflation_factor

    frame = panel.to_pandas()
    x = sm.add_constant(frame[list(BASELINE_PREDICTORS)], has_constant="add")
    y = frame[RESPONSE]

    model = sm.OLS(y, x).fit()

    vifs: list[float | None] = []
    design = np.asarray(x, dtype=float)
    for i, name in enumerate(x.columns):
        if name == "const":
            vifs.append(None)
            continue
        try:
            vifs.append(float(variance_inflation_factor(design, i)))
        except Exception:
            vifs.append(float("inf"))

    return pl.DataFrame(
        {
            "term": [("Intercept" if c == "const" else c) for c in x.columns],
            "coefficient": [float(v) for v in model.params],
            "std_error": [float(v) for v in model.bse],
            "t": [float(v) for v in model.tvalues],
            "p": [float(v) for v in model.pvalues],
            "vif": vifs,
        }
    ).with_columns(pl.lit(float(model.rsquared)).alias("r_squared"))


def run_baseline(
    root: Path | None = None,
    *,
    valid_only: bool = True,
) -> BaselineResult:
    """Fit the specification and return every table it produces."""
    panel = naive_monthly_panel(root, valid_only=valid_only)

    return BaselineResult(
        panel=panel,
        descriptive=_descriptive(panel),
        correlations=_correlations(panel),
        ols=_ols(panel),
        n=panel.height,
        n_stations=panel["station_name"].n_unique(),
    )


def write_baseline_report(result: BaselineResult) -> dict[str, Path]:
    """Persist the tables for the report and for M6.

    ``panel`` and ``ols`` are not report material. M6 reconstructs this fit from
    them and asserts the implied R² reproduces the stored one, so that the
    residuals it tests provably belong to this model rather than to a re-derived
    lookalike. Dropping either would break that check.
    """
    destination = outputs_dir("m1_baseline")
    destination.mkdir(parents=True, exist_ok=True)

    written: dict[str, Path] = {}
    for name, frame in (
        ("panel", result.panel),
        ("descriptive", result.descriptive),
        ("correlations", result.correlations),
        ("ols", result.ols),
    ):
        path = destination / f"{name}.parquet"
        frame.write_parquet(path, compression="zstd")
        written[name] = path
    return written
