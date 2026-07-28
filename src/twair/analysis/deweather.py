"""M4 — meteorological normalisation.

A falling PM2.5 trend has two possible causes and they are not distinguishable
by looking at the series: emissions went down, or the weather happened to be
kinder — more wind, more rain, fewer inversions. Any claim that a policy worked
has to separate them first.

The method is Grange et al. (2018), *Science of the Total Environment* 653,
implemented here in Python because the reference implementation (`rmweather`) is
an R package:

1. Fit a random forest predicting concentration from meteorology plus temporal
   terms — hour, day of year, day of week, and a linear trend.
2. For each of ``n_samples`` iterations, replace every predictor **except the
   trend** with values resampled from elsewhere in the record, and predict.
3. Average across iterations. The result is what the concentration would have
   been at each point in the trend under weather drawn at random from the whole
   period, rather than under the weather that actually occurred.
4. Estimate the trend of that normalised series with Theil–Sen.

Three things this method is not, all of which are easy to assume it is:

**It is not causal attribution.** It removes the weather influence *the model
can see*. Long-range transport that local wind speed and humidity do not
capture stays in the normalised series, and will be read as an emissions
change. The R² of the fit is reported next to every trend for exactly this
reason: a model that explains little cannot have removed much.

**It is not detrending.** The trend term is deliberately the one variable held
fixed. Resampling it too would remove the very signal being measured.

**The confidence interval is not the textbook one.** Hourly air-quality series
are strongly autocorrelated, so the standard Theil–Sen interval — which assumes
independent observations — is far too narrow. A moving-block bootstrap is used
instead, which preserves the local correlation structure. The two are reported
side by side, because the gap between them is itself worth seeing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from twair.analysis.drivers import build_modelling_frame
from twair.features.met import WIND_FEATURES

log = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_HELD_FIXED",
    "NORMALISE_FEATURES",
    "DeweatherResult",
    "TrendEstimate",
    "block_bootstrap_slope",
    "deweather_station",
    "fit_deweather_model",
    "normalise",
    "run_deweather",
    "theil_sen",
]

TARGET = "PM2.5"

# Meteorology and time, and nothing else. Chemistry is deliberately absent: NOx
# and CO co-vary with PM2.5 because they share sources, so including them would
# let the model explain away the emissions change this analysis exists to
# measure.
NORMALISE_FEATURES: tuple[str, ...] = (
    "AMB_TEMP",
    "RH",
    "RAINFALL",
    "WS_HR",
    *WIND_FEATURES,
    "hour_sin",
    "hour_cos",
    "doy_sin",
    "doy_cos",
    "dow_sin",
    "dow_cos",
    "trend_days",
)

# The one predictor not resampled. Everything else — weather, hour of day, day
# of year, weekday — is randomised, which is what makes the output "this point
# in the trend, under average conditions".
DEFAULT_HELD_FIXED: tuple[str, ...] = ("trend_days",)

# Grange's default is 300. Measured on 忠明, 2006-2025, the number this module
# actually reports — the trend of the normalised series — does not move:
#
#     n_samples=30    slope -0.8595/yr   block CI [-0.973, -0.832]   22 s
#     n_samples=100   slope -0.8603/yr   block CI [-0.972, -0.832]   85 s
#     n_samples=300   slope -0.8604/yr   block CI [-0.972, -0.832]  283 s
#
# The resampling noise is averaged away twice over, first by the monthly means
# and then by the slope through them, so 300 buys a tenth of a percent for ten
# times the runtime. 100 is the default here: comfortably past the point where
# the answer settles, without pretending 30 is a safe place to stop.
#
# The per-station `convergence` figure still travels with every result, so a
# station where this does not hold would be visible rather than assumed.
DEFAULT_SAMPLES = 100

HOURS_PER_YEAR = 8_766.0


@dataclass(frozen=True, slots=True)
class TrendEstimate:
    """A Theil–Sen slope with two intervals that disagree on purpose."""

    slope_per_year: float
    intercept: float
    naive_low: float
    naive_high: float
    block_low: float
    block_high: float
    n: int

    @property
    def naive_width(self) -> float:
        return self.naive_high - self.naive_low

    @property
    def block_width(self) -> float:
        return self.block_high - self.block_low

    @property
    def significant(self) -> bool:
        """Significant only if the *honest* interval excludes zero."""
        return (self.block_low > 0.0) or (self.block_high < 0.0)

    def as_dict(self) -> dict[str, float | int | bool]:
        return {
            "slope_per_year": self.slope_per_year,
            "naive_low": self.naive_low,
            "naive_high": self.naive_high,
            "block_low": self.block_low,
            "block_high": self.block_high,
            "interval_widening": (
                self.block_width / self.naive_width if self.naive_width > 0 else float("nan")
            ),
            "significant": self.significant,
            "n": self.n,
        }


@dataclass
class DeweatherResult:
    station: str
    series: pl.DataFrame
    """One row per timestamp: observed, normalised."""
    monthly: pl.DataFrame
    """The same pair aggregated to months — what the trends were fitted on."""
    observed_trend: TrendEstimate
    normalised_trend: TrendEstimate
    holdout_r2: float
    n_samples: int
    convergence: float
    """How much the normalised mean moves between half the samples and all of them."""

    def summary_row(self) -> dict[str, Any]:
        return {
            "station_name": self.station,
            "n": self.series.height,
            "holdout_r2": self.holdout_r2,
            "n_samples": self.n_samples,
            "convergence": self.convergence,
            "observed_slope": self.observed_trend.slope_per_year,
            "observed_significant": self.observed_trend.significant,
            "normalised_slope": self.normalised_trend.slope_per_year,
            "normalised_low": self.normalised_trend.block_low,
            "normalised_high": self.normalised_trend.block_high,
            "normalised_significant": self.normalised_trend.significant,
            "weather_share": _weather_share(
                self.observed_trend.slope_per_year, self.normalised_trend.slope_per_year
            ),
        }


def _weather_share(observed: float, normalised: float) -> float:
    """Fraction of the observed trend that normalisation removed.

    Positive means the weather flattered the improvement; negative means the
    weather worked against it and the underlying change was larger than it
    looked. Undefined when nothing was happening in the first place.
    """
    if observed == 0.0:
        return float("nan")
    return (observed - normalised) / observed


def fit_deweather_model(
    frame: pl.DataFrame,
    *,
    features: tuple[str, ...] = NORMALISE_FEATURES,
    n_estimators: int = 300,
    holdout: float = 0.2,
    seed: int = 20260728,
) -> tuple[Any, float]:
    """Fit the random forest and score it on held-out rows.

    LightGBM in random-forest mode rather than scikit-learn's estimator: the
    normalisation step below runs hundreds of full-dataset predictions, and the
    difference is minutes against hours. The estimator family is the same one
    Grange used.

    The holdout is a **random** split, not a temporal one, and that is correct
    here for once: the model is not being asked to forecast, it is being asked
    to interpolate the weather-to-concentration relationship across the period
    it was fitted on. A temporal split would measure something this use never
    does.
    """
    import lightgbm as lgb

    available = [f for f in features if f in frame.columns]
    missing = set(features) - set(available)
    if missing:
        log.warning(
            "deweather: %d feature(s) absent from the frame: %s", len(missing), sorted(missing)
        )
    if not available:
        raise ValueError("no usable features for deweathering")

    usable = frame.drop_nulls([TARGET, *available])
    if usable.height < 1000:
        raise ValueError(f"only {usable.height} complete rows — too few to deweather")

    rng = np.random.default_rng(seed)
    mask = rng.random(usable.height) >= holdout

    x = usable.select(available).to_numpy()
    y = usable[TARGET].to_numpy()

    model = lgb.LGBMRegressor(
        boosting_type="rf",
        n_estimators=n_estimators,
        # LightGBM refuses random-forest mode without sub-sampling; these are
        # the conventional bagging settings that make it a random forest rather
        # than a very slow single tree.
        bagging_fraction=0.632,
        bagging_freq=1,
        feature_fraction=0.6,
        num_leaves=127,
        min_child_samples=20,
        n_jobs=-1,
        random_state=seed,
        verbose=-1,
    )
    model.fit(x[mask], y[mask])

    predicted = model.predict(x[~mask])
    truth = y[~mask]
    ss_res = float(np.sum((truth - predicted) ** 2))
    ss_tot = float(np.sum((truth - truth.mean()) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan")

    # Refit on everything: the holdout existed to produce a score, and the
    # normalisation should use every row available.
    model.fit(x, y)
    return model, r2


def normalise(
    model: Any,
    frame: pl.DataFrame,
    *,
    features: tuple[str, ...] = NORMALISE_FEATURES,
    held_fixed: tuple[str, ...] = DEFAULT_HELD_FIXED,
    n_samples: int = DEFAULT_SAMPLES,
    seed: int = 20260728,
) -> tuple[np.ndarray, float]:
    """Average predictions over resampled conditions.

    Returns the normalised series and a convergence diagnostic: the mean
    absolute difference between the answer from the first half of the samples
    and the answer from all of them, as a fraction of the series mean. A small
    number means more samples would not change the result.
    """
    available = [f for f in features if f in frame.columns]
    matrix = frame.select(available).to_numpy()
    rows = matrix.shape[0]

    resample_columns = [i for i, name in enumerate(available) if name not in held_fixed]
    if not resample_columns:
        raise ValueError("every feature is held fixed — nothing would be normalised")

    rng = np.random.default_rng(seed)
    running = np.zeros(rows, dtype=float)
    half = np.zeros(rows, dtype=float)
    half_count = max(1, n_samples // 2)

    scratch = matrix.copy()
    for i in range(n_samples):
        # One permutation for all resampled columns together, so realistic
        # combinations of weather survive. Drawing each column independently
        # would manufacture conditions that never occur — 35 °C with the
        # humidity of a January morning.
        order = rng.integers(0, rows, size=rows)
        scratch[:, resample_columns] = matrix[np.ix_(order, resample_columns)]

        prediction = model.predict(scratch)
        running += prediction
        if i < half_count:
            half += prediction

    full_mean = running / n_samples
    half_mean = half / half_count

    scale = float(np.mean(np.abs(full_mean))) or 1.0
    convergence = float(np.mean(np.abs(full_mean - half_mean))) / scale
    return full_mean, convergence


def theil_sen(
    x: np.ndarray,
    y: np.ndarray,
    *,
    block: int = 12,
    n_boot: int = 500,
    seed: int = 20260728,
) -> TrendEstimate:
    """Theil–Sen slope, with both the textbook interval and an honest one.

    ``x`` is expected in **years** and ``y`` in monthly means, one point per
    month. Two reasons the input is monthly rather than hourly, and both matter:

    *Feasibility.* Theil–Sen compares every pair of points, so it is O(n²) in
    both time and memory. On one station's 155,816 hourly values scipy tries to
    allocate a 155,816 × 155,816 matrix — 181 GiB — and dies. Monthly means
    over twenty years are 240 points.

    *Meaning.* Even if it fit in memory, a slope fitted through hourly values
    is dominated by the diurnal and synoptic variation this analysis has just
    finished normalising away. Monthly aggregation before trend estimation is
    the standard practice (openair's ``TheilSen`` does the same).

    ``scipy.stats.theilslopes`` then assumes independent observations. Monthly
    air-quality series are still autocorrelated — a bad winter spans several
    months — so that interval remains too narrow. The block bootstrap resamples
    contiguous runs of ``block`` months, preserving correlation within a block
    and breaking it between. Both intervals are reported; the gap is the price
    of pretending the months were independent.
    """
    from scipy import stats

    keep = np.isfinite(x) & np.isfinite(y)
    x, y = x[keep], y[keep]
    if x.size < 3:
        raise ValueError(f"need at least 3 points for a trend, got {x.size}")

    slope, intercept, low, high = stats.theilslopes(y, x, alpha=0.95)
    block_low, block_high = block_bootstrap_slope(x, y, block=block, n_boot=n_boot, seed=seed)

    return TrendEstimate(
        slope_per_year=float(slope),
        intercept=float(intercept),
        naive_low=float(low),
        naive_high=float(high),
        block_low=block_low,
        block_high=block_high,
        n=int(x.size),
    )


def block_bootstrap_slope(
    x: np.ndarray,
    y: np.ndarray,
    *,
    block: int = 12,
    n_boot: int = 500,
    seed: int = 20260728,
) -> tuple[float, float]:
    """Percentile interval for an ordinary-least-squares slope over moving blocks.

    OLS rather than Theil–Sen inside the loop purely for speed: Theil–Sen is
    O(n²) in the number of points and this runs hundreds of times. The interval
    describes the sampling variability of a linear trend under the series'
    own correlation structure, which is what the naive interval gets wrong.

    ``block`` is a number of consecutive points — twelve months by default, so
    a whole seasonal cycle stays intact inside each resampled run.
    """
    rng = np.random.default_rng(seed)
    n = x.size
    block = min(max(int(block), 2), n)
    n_blocks = int(np.ceil(n / block))

    slopes = np.empty(n_boot, dtype=float)
    starts_max = max(1, n - block)

    for i in range(n_boot):
        starts = rng.integers(0, starts_max, size=n_blocks)
        index = np.concatenate([np.arange(s, min(s + block, n)) for s in starts])[:n]
        xb, yb = x[index], y[index]
        # A resample can land entirely inside one flat stretch; skip those
        # rather than dividing by a zero variance.
        var = float(np.var(xb))
        slopes[i] = np.polyfit(xb, yb, 1)[0] if var > 0 else np.nan

    finite = slopes[np.isfinite(slopes)]
    if finite.size == 0:
        return float("nan"), float("nan")
    return float(np.percentile(finite, 2.5)), float(np.percentile(finite, 97.5))


def deweather_station(
    frame: pl.DataFrame,
    station: str,
    *,
    n_samples: int = DEFAULT_SAMPLES,
    n_estimators: int = 300,
    held_fixed: tuple[str, ...] = DEFAULT_HELD_FIXED,
    n_boot: int = 500,
    seed: int = 20260728,
) -> DeweatherResult:
    """Fit, normalise and trend one station."""
    subset = frame.filter(pl.col("station_name") == station).sort("ts_local")
    features = tuple(f for f in NORMALISE_FEATURES if f in subset.columns)
    complete = subset.drop_nulls([TARGET, *features])

    log.info("%s: %d complete hours of %d", station, complete.height, subset.height)

    model, r2 = fit_deweather_model(
        complete, features=features, n_estimators=n_estimators, seed=seed
    )
    normalised, convergence = normalise(
        model,
        complete,
        features=features,
        held_fixed=held_fixed,
        n_samples=n_samples,
        seed=seed,
    )

    series = complete.select("ts_local", pl.col(TARGET).alias("observed")).with_columns(
        pl.Series("normalised", normalised)
    )

    # Trends are estimated on monthly means, never on the hourly series — see
    # `theil_sen` for why (181 GiB, and the wrong question besides). The hourly
    # series is still returned in full; only the trend step aggregates.
    monthly = monthly_means(series)
    years = _years_since_start(monthly["month"])

    return DeweatherResult(
        station=station,
        series=series,
        monthly=monthly,
        observed_trend=theil_sen(years, monthly["observed"].to_numpy(), n_boot=n_boot, seed=seed),
        normalised_trend=theil_sen(
            years, monthly["normalised"].to_numpy(), n_boot=n_boot, seed=seed
        ),
        holdout_r2=r2,
        n_samples=n_samples,
        convergence=convergence,
    )


def monthly_means(series: pl.DataFrame) -> pl.DataFrame:
    """Collapse the hourly observed/normalised pair to one row per month."""
    return (
        series.group_by(pl.col("ts_local").dt.truncate("1mo").alias("month"))
        .agg(
            pl.col("observed").mean(),
            pl.col("normalised").mean(),
            pl.len().alias("hours"),
        )
        .sort("month")
    )


def _years_since_start(months: pl.Series) -> np.ndarray:
    """Month starts as a float number of years from the first one."""
    days = months.cast(pl.Date).to_numpy().astype("datetime64[D]").astype("int64").astype(float)
    return (days - days[0]) / 365.25


def run_deweather(
    root: Path | None = None,
    *,
    period: tuple[int, int] = (2006, 2025),
    stations: list[str] | None = None,
    n_samples: int = DEFAULT_SAMPLES,
    n_estimators: int = 300,
    min_hours: int = 20_000,
    seed: int = 20260728,
) -> dict[str, pl.DataFrame]:
    """Deweather every station with enough data, and summarise.

    Defaults to 2006 onwards — the window in which the PM2.5 network is a
    national network rather than five experimental sites (see
    ``viz.story.balanced_panel_options``).
    """
    frame = build_modelling_frame(root, period=period, stations=stations)

    counts = (
        frame.drop_nulls([TARGET])
        .group_by("station_name")
        .len()
        .filter(pl.col("len") >= min_hours)
        .sort("station_name")
    )
    eligible = counts["station_name"].to_list()
    log.info(
        "deweathering %d station(s) with >= %d hours (of %d present)",
        len(eligible),
        min_hours,
        frame["station_name"].n_unique(),
    )

    summaries: list[dict[str, Any]] = []
    monthly: list[pl.DataFrame] = []

    for station in eligible:
        try:
            result = deweather_station(
                frame,
                station,
                n_samples=n_samples,
                n_estimators=n_estimators,
                seed=seed,
            )
        except (ValueError, KeyError) as exc:
            log.warning("skipping %s: %s", station, exc)
            continue

        summaries.append(result.summary_row())
        # Reuse the frame the trend was fitted on rather than re-aggregating:
        # a chart drawn from a second, separately computed monthly series could
        # drift from the slope printed beside it.
        monthly.append(result.monthly.with_columns(pl.lit(station).alias("station_name")))

    if not summaries:
        raise RuntimeError("no station had enough data to deweather")

    return {
        "summary": pl.DataFrame(summaries).sort("normalised_slope"),
        "monthly": pl.concat(monthly).select(
            "station_name", "month", "observed", "normalised", "hours"
        ),
    }


def write_deweather_report(tables: dict[str, pl.DataFrame]) -> dict[str, Path]:
    from twair.paths import outputs_dir

    destination = outputs_dir("m4_deweather")
    destination.mkdir(parents=True, exist_ok=True)

    written: dict[str, Path] = {}
    for name, frame in tables.items():
        path = destination / f"{name}.parquet"
        frame.write_parquet(path)
        written[name] = path
    return written
