"""M3 — the same data, two methods, two conclusions.

Six paired demonstrations, each isolating one decision the 2018 project made
and showing what the other choice yields on identical rows. This is the part of
the project that is worth reading: not "here is a better model" but "here is
why the earlier answer was not an answer".

Each function returns the *evidence* — a frame you can plot or tabulate —
rather than a verdict. The reader should be able to disagree with the
interpretation while accepting the numbers.
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import polars as pl

from twair.qc.rainfall import usable
from twair.store.stations import normalise_name_expr
from twair.store.writer import scan_observations

log = logging.getLogger(__name__)

__all__ = [
    "collinearity_instability",
    "diurnal_cycle_lost_to_monthly_means",
    "normality_remedy_does_not_work_on_its_own_numbers",
    "normality_test_fallacy",
    "wind_direction_linearisation",
]


def diurnal_cycle_lost_to_monthly_means(
    root: Path | None = None,
    *,
    period: tuple[int, int] = (2010, 2017),
    pollutant: str = "PM2.5",
) -> dict[str, pl.DataFrame]:
    """Pitfall 1 — what monthly averaging removes.

    Returns the hourly and weekday profiles alongside the monthly series. The
    monthly series is flat where the others are not, and the gap is the
    information the original discarded before it began analysing.
    """
    start, end = period
    hourly = (
        scan_observations(root)
        .filter(
            (pl.col("pollutant") == pollutant)
            & pl.col("ts_local").dt.year().is_between(start, end)
            & usable()
        )
        .select(
            normalise_name_expr(),
            "ts_local",
            pl.col("value").cast(pl.Float64),
        )
        .collect()
    )

    diurnal = (
        hourly.group_by(pl.col("ts_local").dt.hour().alias("hour"))
        .agg(
            pl.col("value").mean().alias("mean"),
            pl.col("value").quantile(0.9).alias("p90"),
            pl.len().alias("n"),
        )
        .sort("hour")
    )

    weekly = (
        hourly.group_by(pl.col("ts_local").dt.weekday().alias("weekday"))
        .agg(pl.col("value").mean().alias("mean"), pl.len().alias("n"))
        .sort("weekday")
    )

    monthly = (
        hourly.group_by(pl.col("ts_local").dt.truncate("1mo").alias("month"))
        .agg(pl.col("value").mean().alias("mean"), pl.len().alias("n"))
        .sort("month")
    )

    # How much of the hourly variance survives monthly averaging.
    hourly_sd = float(hourly["value"].std())
    monthly_sd = float(monthly["mean"].std())

    variance = pl.DataFrame(
        {
            "scale": ["hourly", "monthly_mean"],
            "sd": [hourly_sd, monthly_sd],
            "variance_retained": [1.0, (monthly_sd / hourly_sd) ** 2],
        }
    )

    return {
        "diurnal": diurnal,
        "weekly": weekly,
        "monthly": monthly,
        "variance": variance,
    }


def wind_direction_linearisation(
    root: Path | None = None,
    *,
    period: tuple[int, int] = (2010, 2017),
) -> dict[str, pl.DataFrame]:
    """Pitfall 3 — a bearing is not a number.

    Produces two things:

    * the correlation between PM2.5 and the raw bearing, which is what the
      original reported (0.089, and duly called "no correlation");
    * the mean concentration by 30° sector, which shows a large and orderly
      directional dependence that a linear correlation cannot express.

    The point is not that the original got a small number. It is that a small
    number was the only thing that method could produce, however strong the
    real dependence.
    """
    start, end = period
    paired = (
        scan_observations(root)
        .filter(
            pl.col("pollutant").is_in(["PM2.5", "WD_HR", "WS_HR"])
            & pl.col("ts_local").dt.year().is_between(start, end)
            & usable()
        )
        .select(
            normalise_name_expr(),
            pl.col("pollutant").cast(pl.Utf8),
            "ts_local",
            pl.col("value").cast(pl.Float64),
        )
        .collect()
        .pivot(
            on="pollutant",
            index=["station_name", "ts_local"],
            values="value",
            aggregate_function="first",
        )
        .drop_nulls(["PM2.5", "WD_HR"])
    )

    linear_r = float(paired.select(pl.corr("PM2.5", "WD_HR")).item())

    by_sector = (
        paired.with_columns(((pl.col("WD_HR") // 30) * 30).cast(pl.Int32).alias("sector"))
        .group_by("sector")
        .agg(
            pl.col("PM2.5").mean().alias("mean"),
            pl.col("PM2.5").median().alias("median"),
            pl.len().alias("n"),
        )
        .sort("sector")
    )

    # A directional effect the linear correlation cannot see: the spread across
    # sectors, expressed against the overall mean.
    overall = float(paired["PM2.5"].mean())
    spread = float(by_sector["mean"].max() - by_sector["mean"].min())

    summary = pl.DataFrame(
        {
            "measure": [
                "pearson_r_with_raw_bearing",
                "sector_mean_range",
                "sector_range_as_pct_of_mean",
            ],
            "value": [linear_r, spread, 100.0 * spread / overall],
        }
    )

    return {"by_sector": by_sector, "summary": summary}


def normality_test_fallacy(
    sample_sizes: tuple[int, ...] = (100, 1_000, 7_286, 100_000),
    *,
    seed: int = 0,
) -> pl.DataFrame:
    """Pitfall 4 — what a significant normality test at large N means.

    The original wrote (p.37) that because every variable was rejected, the
    rejection threshold should be lowered to 0.01 so that none would be, and
    concluded the data were therefore normal.

    Two things are wrong, and this demonstrates both on synthetic data where
    the truth is known:

    1. A goodness-of-fit test rejects *any* departure given enough rows. Data
       drawn from a genuinely normal distribution passes at every N; data with
       a slight, harmless skew passes at small N and fails at large N. The test
       is measuring sample size as much as normality.
    2. Moving the threshold changes what the test reports, not what the data
       are. It cannot make a distribution normal.
    """
    from scipy import stats

    rng = np.random.default_rng(seed)
    rows: list[dict[str, object]] = []

    for n in sample_sizes:
        truly_normal = rng.normal(size=n)
        # Lognormal with small sigma: visually near-normal, genuinely not.
        slightly_skewed = rng.lognormal(mean=0.0, sigma=0.35, size=n)

        for label, sample in (
            ("truly_normal", truly_normal),
            ("slightly_skewed", slightly_skewed),
        ):
            statistic, p = stats.kstest((sample - sample.mean()) / sample.std(ddof=1), "norm")
            rows.append(
                {
                    "n": n,
                    "data": label,
                    "ks_statistic": float(statistic),
                    "p_value": float(p),
                    "rejected_at_0.05": bool(p < 0.05),
                    "rejected_at_0.01": bool(p < 0.01),
                    "skewness": float(stats.skew(sample)),
                }
            )

    return pl.DataFrame(rows).sort("data", "n")


def collinearity_instability(
    root: Path | None = None,
    *,
    period: tuple[int, int] = (2010, 2017),
    n_bootstrap: int = 40,
    sample_size: int = 5_000,
    seed: int = 0,
) -> dict[str, pl.DataFrame]:
    """Pitfall 5 — why the stepwise search kept changing its mind.

    ``NO + NO2 = NOx`` holds by definition, so a model containing all three has
    no unique solution: the fit can trade coefficients between them freely
    without changing a single prediction. The original ran eight rounds of
    stepwise elimination watching signs flip, and read the flipping as a
    substantive finding about nitrogen chemistry.

    Refitting on bootstrap resamples shows what is really going on. The three
    collinear coefficients swing wildly across resamples while a
    well-identified predictor barely moves — and the predictions are
    indistinguishable throughout.
    """
    import statsmodels.api as sm

    start, end = period
    wide = (
        scan_observations(root)
        .filter(
            pl.col("pollutant").is_in(["PM2.5", "NO", "NO2", "NOx", "O3"])
            & pl.col("ts_local").dt.year().is_between(start, end)
            & usable()
        )
        .select(
            normalise_name_expr(),
            pl.col("pollutant").cast(pl.Utf8),
            "ts_local",
            pl.col("value").cast(pl.Float64),
        )
        .collect()
        .pivot(
            on="pollutant",
            index=["station_name", "ts_local"],
            values="value",
            aggregate_function="first",
        )
        .drop_nulls(["PM2.5", "NO", "NO2", "NOx", "O3"])
    )

    # Built explicitly rather than via Series.describe(), whose column naming
    # is a presentation detail this result should not depend on.
    residual = wide.select((pl.col("NO") + pl.col("NO2") - pl.col("NOx")).abs().alias("e"))["e"]
    identity_error = pl.DataFrame(
        {
            "statistic": ["mean", "median", "p99", "max"],
            "e": [
                float(residual.mean()),
                float(residual.median()),
                float(residual.quantile(0.99)),
                float(residual.max()),
            ],
        }
    )

    predictors = ["NO", "NO2", "NOx", "O3"]
    rng = np.random.default_rng(seed)
    frame = wide.select([*predictors, "PM2.5"]).to_pandas()

    coefficients: list[dict[str, float]] = []
    fits: list[float] = []
    for _ in range(n_bootstrap):
        idx = rng.choice(len(frame), size=min(sample_size, len(frame)), replace=True)
        sample = frame.iloc[idx]
        x = sm.add_constant(sample[predictors], has_constant="add")
        model = sm.OLS(sample["PM2.5"], x).fit()
        coefficients.append({name: float(model.params[name]) for name in predictors})
        fits.append(float(model.rsquared))

    spread = pl.DataFrame(coefficients).unpivot(variable_name="term", value_name="coefficient")
    stability = (
        spread.group_by("term")
        .agg(
            pl.col("coefficient").mean().alias("mean"),
            pl.col("coefficient").std().alias("sd"),
            pl.col("coefficient").min().alias("min"),
            pl.col("coefficient").max().alias("max"),
            (pl.col("coefficient") > 0).mean().alias("share_positive"),
        )
        .with_columns((pl.col("sd") / pl.col("mean").abs()).alias("coefficient_of_variation"))
        .sort("coefficient_of_variation", descending=True)
    )

    return {
        "identity_error": identity_error,
        "stability": stability,
        "r_squared": pl.DataFrame(
            {"measure": ["r2_mean", "r2_sd"], "value": [float(np.mean(fits)), float(np.std(fits))]}
        ),
    }


def normality_remedy_does_not_work_on_its_own_numbers() -> pl.DataFrame:
    """Pitfall 4b — the stated fix fails against the report's own table.

    The 2018 report says (p.37) that lowering the rejection threshold to 0.01
    left every variable non-rejected, and concludes the data are normal.

    Its own Kolmogorov-Smirnov table on the same page gives 顯著性 = .000 for
    all thirteen variables. A p-value below 0.001 is rejected at 0.01 just as
    surely as at 0.05. The remedy does not do what the text claims it does,
    independently of whether the remedy would be sound.

    Values are quoted from the published table; the verdicts are arithmetic.
    """
    # p.36, Kolmogorov-Smirnov with Lilliefors correction, df = 7286.
    published = {
        "AMB_TEMP": 0.099,
        "CO": 0.137,
        "NO": 0.300,
        "NO2": 0.051,
        "NOx": 0.151,
        "O3": 0.051,
        "PM10": 0.101,
        "PM2.5": 0.088,
        "RAINFALL": 0.196,
        "RH": 0.027,
        "SO2": 0.127,
        "WD_HR": 0.047,
        "WS_HR": 0.148,
    }
    # Every row of the published table reads ".000", i.e. p < 0.0005.
    reported_p = 0.000

    return pl.DataFrame(
        {
            "variable": list(published),
            "ks_statistic_published": list(published.values()),
            "p_value_published": [reported_p] * len(published),
            "rejected_at_0.05": [True] * len(published),
            "rejected_at_0.01": [True] * len(published),
            "report_claims_not_rejected_at_0.01": [True] * len(published),
        }
    )
