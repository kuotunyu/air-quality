"""M1 — replicate the 2018 graduation project, errors included.

This module deliberately reproduces the original method rather than a corrected
version of it. That is the point: every later claim of the form "fixing X
changes the conclusion" needs a baseline that demonstrably reproduces X.

It therefore **does not** use :mod:`twair.store.aggregate`, which averages
circular quantities as vectors and withholds means below a coverage threshold.
The original did neither, so neither does this.

What the original did (第三章, 第五章):

* period 2010–2017, monthly means per station
* hourly values averaged straight to monthly, with no daily step and no check
  on how many hours contributed
* wind direction averaged arithmetically along with everything else
* PM10 used as a predictor of PM2.5
* N = 7,286 station-months

Reference values are in ``reports/_expected/replication_2018.yaml``, quoted
from the PDF. They are the original's *claims*, not ground truth; the purpose
of this module is to find out which reproduce and which do not.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import polars as pl
import yaml

from twair.config import _reject_boolean_keys
from twair.paths import REPO_ROOT, outputs_dir
from twair.qc.rainfall import usable
from twair.store.stations import normalise_name_expr
from twair.store.writer import scan_observations

log = logging.getLogger(__name__)

__all__ = [
    "ORIGINAL_PREDICTORS",
    "ReplicationResult",
    "load_expected",
    "naive_monthly_panel",
    "run_replication",
    "write_replication_report",
]

# The response and the twelve predictors, in the original's own order (p.57).
RESPONSE = "PM2.5"
ORIGINAL_PREDICTORS = (
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
ORIGINAL_PERIOD = (2010, 2017)

EXPECTED_PATH = REPO_ROOT / "reports" / "_expected" / "replication_2018.yaml"


def load_expected(path: Path | None = None) -> dict[str, Any]:
    """Load the numbers the 2018 report published.

    Runs the same boolean-key guard as :func:`twair.config.load_conf`. That
    guard lives in the config loader, and this file bypassed it — so the
    unquoted key ``NO`` became ``False`` here too, and nitric oxide silently
    lost its reference value. The Norway problem does not respect module
    boundaries.
    """
    target = path or EXPECTED_PATH
    with target.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    _reject_boolean_keys(data, target.name)
    return data


def naive_monthly_panel(
    root: Path | None = None,
    *,
    period: tuple[int, int] = ORIGINAL_PERIOD,
    valid_only: bool = True,
) -> pl.DataFrame:
    """Monthly arithmetic means per station — the 2018 aggregation, reproduced.

    Three deliberate departures from :func:`twair.store.aggregate.aggregate_daily`:

    1. **Arithmetic mean for every variable**, wind direction included. This is
       wrong for a circular quantity and is exactly what the original did.
    2. **No coverage threshold.** A station-month built from three valid hours
       carries the same weight as one built from seven hundred.
    3. **No daily intermediate.** Hours are averaged straight to months, per
       「將逐時資料合併為各測站之月資料」.

    ``valid_only`` controls whether agency-rejected readings are excluded. The
    original never says which it did, so the choice is exposed and its effect
    measured rather than assumed.
    """
    start, end = period
    needed = [RESPONSE, *ORIGINAL_PREDICTORS]

    in_period = scan_observations(root).filter(
        pl.col("ts_local").dt.year().is_between(start, end)
    )

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

    # The original's N implies complete cases: 7,286 rows over 96 months is
    # about 76 stations reporting every variable in every month.
    return (
        wide.select("station_name", "month", *needed)
        .drop_nulls(needed)
        .sort("station_name", "month")
    )


@dataclass
class ReplicationResult:
    panel: pl.DataFrame
    descriptive: pl.DataFrame
    correlations: pl.DataFrame
    ols: pl.DataFrame
    comparison: pl.DataFrame
    n: int
    n_stations: int


def _descriptive(panel: pl.DataFrame) -> pl.DataFrame:
    rows = []
    for column in [RESPONSE, *ORIGINAL_PREDICTORS]:
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
        for column in ORIGINAL_PREDICTORS
    ]
    return pl.DataFrame(rows)


def _ols(panel: pl.DataFrame) -> pl.DataFrame:
    """OLS with VIF, matching the original's specification exactly.

    NO, NO2 and NOx all enter together even though NO + NO2 = NOx by
    definition. The design is singular in all but rounding error, which is what
    produced the original's VIFs in the tens of thousands.
    """
    import numpy as np
    import statsmodels.api as sm
    from statsmodels.stats.outliers_influence import variance_inflation_factor

    frame = panel.to_pandas()
    x = sm.add_constant(frame[list(ORIGINAL_PREDICTORS)], has_constant="add")
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


def _compare(
    descriptive: pl.DataFrame,
    correlations: pl.DataFrame,
    ols: pl.DataFrame,
    expected: dict[str, Any],
    n: int,
) -> pl.DataFrame:
    """Line up every reproduced number against the one the report published."""
    rows: list[dict[str, Any]] = []

    def add(kind: str, item: str, published: float | None, reproduced: float | None) -> None:
        if published is None or reproduced is None:
            diff = None
            pct = None
        else:
            diff = reproduced - published
            pct = (diff / published * 100) if published != 0 else None
        rows.append(
            {
                "kind": kind,
                "item": item,
                "published_2018": published,
                "reproduced": reproduced,
                "difference": diff,
                "pct_difference": pct,
            }
        )

    add("sample", "N", float(expected["study"]["n_observations"]), float(n))

    published_desc = expected["descriptive"]
    for row in descriptive.iter_rows(named=True):
        stats = published_desc.get(row["variable"], {})
        for stat in ("mean", "sd"):
            add(f"descriptive.{stat}", row["variable"], stats.get(stat), row[stat])

    published_corr = expected["correlation_with_pm25"]
    for row in correlations.iter_rows(named=True):
        add("correlation", row["variable"], published_corr.get(row["variable"]), row["r"])

    published_ols = expected["ols_coefficients"]
    for row in ols.iter_rows(named=True):
        add("ols_coefficient", row["term"], published_ols.get(row["term"]), row["coefficient"])

    return pl.DataFrame(rows)


def run_replication(
    root: Path | None = None,
    *,
    valid_only: bool = True,
) -> ReplicationResult:
    """Reproduce the 2018 analysis and compare it against the published numbers."""
    panel = naive_monthly_panel(root, valid_only=valid_only)

    descriptive = _descriptive(panel)
    correlations = _correlations(panel)
    ols = _ols(panel)
    comparison = _compare(descriptive, correlations, ols, load_expected(), panel.height)

    return ReplicationResult(
        panel=panel,
        descriptive=descriptive,
        correlations=correlations,
        ols=ols,
        comparison=comparison,
        n=panel.height,
        n_stations=panel["station_name"].n_unique(),
    )


def write_replication_report(result: ReplicationResult) -> dict[str, Path]:
    """Persist the replication tables for the report and the website."""
    destination = outputs_dir("m1_replication")
    destination.mkdir(parents=True, exist_ok=True)

    written: dict[str, Path] = {}
    for name, frame in (
        ("panel", result.panel),
        ("descriptive", result.descriptive),
        ("correlations", result.correlations),
        ("ols", result.ols),
        ("comparison", result.comparison),
    ):
        path = destination / f"{name}.parquet"
        frame.write_parquet(path, compression="zstd")
        written[name] = path

    csv_path = destination / "comparison.csv"
    result.comparison.write_csv(csv_path)
    written["comparison_csv"] = csv_path
    return written
