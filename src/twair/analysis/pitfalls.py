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
    "run_all_pitfalls",
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

    # The modulo is not decoration. `WD_HR // 30 * 30` sends a reading of
    # exactly 360° to a thirteenth sector holding 3,341 rows, separate from the
    # 285,527 rows at 0° — the same bearing, split in two. It is precisely the
    # wraparound mistake this module exists to document, and it was in this
    # module. Fold 360 onto 0 before binning.
    by_sector = (
        paired.with_columns((((pl.col("WD_HR") % 360) // 30) * 30).cast(pl.Int32).alias("sector"))
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


def run_all_pitfalls(
    root: Path | None = None,
    *,
    period: tuple[int, int] = (2010, 2017),
) -> dict[str, pl.DataFrame]:
    """Run every demonstration and return the evidence tables, flattened.

    Keys are ``<pitfall>.<table>`` so the caller can persist them without
    knowing the shape of each result.
    """
    tables: dict[str, pl.DataFrame] = {}

    for name, result in (
        ("diurnal", diurnal_cycle_lost_to_monthly_means(root, period=period)),
        ("wind", wind_direction_linearisation(root, period=period)),
        ("collinearity", collinearity_instability(root, period=period)),
    ):
        for key, frame in result.items():
            tables[f"{name}.{key}"] = frame

    tables["wind.linear_model_encoding"] = wind_encoding_in_a_linear_model(root, period=period)
    tables["normality.by_sample_size"] = normality_test_fallacy()
    tables["normality.published_table"] = normality_remedy_does_not_work_on_its_own_numbers()

    # These two depend on the M2 run having produced scores; skip rather than
    # fail if it has not, so the other four still publish.
    try:
        tables["leakage.price"] = pm10_leakage_price(root)
    except FileNotFoundError as exc:
        log.warning("skipping PM10 leakage: %s", exc)

    try:
        tables["validation.in_vs_out_of_sample"] = in_sample_versus_out_of_sample(
            root, period=period
        )
    except Exception as exc:
        log.warning("skipping in/out-of-sample: %s", exc)

    return tables


def write_pitfall_report(tables: dict[str, pl.DataFrame]) -> dict[str, Path]:
    """Persist every evidence table for the report and the website."""
    from twair.paths import outputs_dir

    destination = outputs_dir("m3_pitfalls")
    destination.mkdir(parents=True, exist_ok=True)

    written: dict[str, Path] = {}
    for name, frame in tables.items():
        path = destination / f"{name.replace('.', '_')}.parquet"
        frame.write_parquet(path)
        written[name] = path
    return written


def pm10_leakage_price(root: Path | None = None) -> pl.DataFrame:
    """Pitfall 2 — what PM10 was worth as a predictor of its own subset.

    Reads the M2 scores rather than refitting, so the number quoted here is the
    same number the model comparison produced.

    PM2.5 is a physical subset of PM10. A model given both is partly being told
    the answer, and the share of its explanatory power that comes from that
    overlap is not knowledge about how PM2.5 forms.
    """
    from twair.paths import outputs_dir

    path = outputs_dir("m2_drivers") / "scores.parquet"
    if not path.exists():
        raise FileNotFoundError(f"{path} — run scripts/run_m2.py first")

    rolling = (
        pl.read_parquet(path)
        .filter(
            (pl.col("split_kind") == "rolling")
            & pl.col("feature_set").is_in(["full", "full_with_pm10"])
        )
        .group_by("feature_set")
        .agg(
            pl.col("r2").mean().alias("r2"),
            pl.col("rmse").mean().alias("rmse"),
            pl.col("mae").mean().alias("mae"),
            pl.col("exceedance_f1").mean().alias("exceedance_f1"),
        )
    )

    honest = rolling.filter(pl.col("feature_set") == "full")
    leaking = rolling.filter(pl.col("feature_set") == "full_with_pm10")
    if honest.is_empty() or leaking.is_empty():
        return rolling

    r2_honest = float(honest["r2"][0])
    r2_leaking = float(leaking["r2"][0])

    return rolling.vstack(
        pl.DataFrame(
            {
                "feature_set": ["leak_share_of_r2"],
                "r2": [(r2_leaking - r2_honest) / r2_leaking if r2_leaking else None],
                "rmse": [float(leaking["rmse"][0]) - float(honest["rmse"][0])],
                "mae": [float(leaking["mae"][0]) - float(honest["mae"][0])],
                "exceedance_f1": [
                    float(leaking["exceedance_f1"][0]) - float(honest["exceedance_f1"][0])
                ],
            }
        )
    )


def in_sample_versus_out_of_sample(
    root: Path | None = None,
    *,
    period: tuple[int, int] = (2010, 2017),
    feature_set: str = "full",
    seed: int = 0,
) -> pl.DataFrame:
    """Pitfall 6 — the gap the original had no way to see.

    The 2018 project selected its model by AIC and BIC computed on the same
    rows it was fitted to. Nothing in that procedure can detect a model that
    has learned the sample rather than the phenomenon.

    Fits one model and scores it twice: on the rows it was trained on, and on
    future rows it has never seen. The difference is what in-sample selection
    cannot report.
    """
    import lightgbm as lgb

    from twair.analysis.drivers import FEATURE_SETS, TARGET, build_modelling_frame
    from twair.models.evaluate import evaluate_predictions, rolling_origin

    frame = build_modelling_frame(root, period=period)
    features = list(FEATURE_SETS[feature_set])

    splits = list(rolling_origin(frame, n_splits=3))
    if not splits:
        raise RuntimeError("not enough rows for a rolling-origin split")
    split = splits[-1]

    train = split.train.select([*features, TARGET]).drop_nulls()
    test = split.test.select([*features, TARGET]).drop_nulls()

    model = lgb.LGBMRegressor(
        n_estimators=300,
        learning_rate=0.05,
        num_leaves=63,
        min_child_samples=50,
        random_state=seed,
        n_jobs=-1,
        verbose=-1,
    )
    model.fit(train.select(features).to_numpy(), train[TARGET].to_numpy())

    rows = []
    for label, subset in (("in_sample", train), ("out_of_sample", test)):
        prediction = model.predict(subset.select(features).to_numpy())
        metrics = evaluate_predictions(subset[TARGET].to_numpy(), prediction)
        rows.append({"evaluated_on": label, **metrics.as_dict()})

    result = pl.DataFrame(rows)
    optimism = float(result["r2"][0]) - float(result["r2"][1])
    return result.vstack(
        pl.DataFrame(
            {
                "evaluated_on": ["optimism_r2"],
                "n": [None],
                "rmse": [float(result["rmse"][1]) - float(result["rmse"][0])],
                "mae": [float(result["mae"][1]) - float(result["mae"][0])],
                "r2": [optimism],
                "exceedance_f1": [None],
            }
        ),
        in_place=False,
    )


def wind_encoding_in_a_linear_model(
    root: Path | None = None,
    *,
    period: tuple[int, int] = (2010, 2017),
    sample_size: int = 200_000,
    seed: int = 0,
) -> pl.DataFrame:
    """Pitfall 3b — where the circular-variable problem actually bites.

    The M2 comparison produced a result worth stating plainly: a gradient
    boosted tree does *slightly better* with the raw bearing than with sin/cos
    (R² 0.537 against 0.524). Trees split on a variable repeatedly and can
    carve 0-360 into as many pieces as they need, so linearity across the wrap
    point costs them almost nothing.

    That does not rescue the original. Its methods were Pearson correlation,
    OLS and a linear mixed model, and for those the encoding is decisive. This
    fits the same rows both ways under OLS, which is the comparison that
    matches what the 2018 project actually did.
    """
    import statsmodels.api as sm

    from twair.features.met import add_wind_features

    start, end = period
    wide = (
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
        .drop_nulls(["PM2.5", "WD_HR", "WS_HR"])
    )

    if wide.height > sample_size:
        wide = wide.sample(sample_size, seed=seed)

    featured = add_wind_features(wide)
    frame = featured.to_pandas()
    y = frame["PM2.5"]

    rows = []
    for label, columns in (
        ("raw_bearing", ["WD_HR", "WS_HR"]),
        ("sin_cos", ["wd_sin", "wd_cos", "WS_HR"]),
        ("sin_cos_plus_uv", ["wd_sin", "wd_cos", "u", "v", "WS_HR"]),
    ):
        model = sm.OLS(y, sm.add_constant(frame[columns], has_constant="add")).fit()
        rows.append(
            {
                "encoding": label,
                "n_terms": len(columns),
                "r_squared": float(model.rsquared),
                "adj_r_squared": float(model.rsquared_adj),
                "rmse": float(np.sqrt(model.mse_resid)),
            }
        )

    result = pl.DataFrame(rows)
    baseline = float(result.filter(pl.col("encoding") == "raw_bearing")["r_squared"][0])
    return result.with_columns((pl.col("r_squared") / baseline).alias("r2_relative_to_raw_bearing"))
