"""M5 — did the policy do anything?

The question every air-quality story wants answered, and the one most likely to
be answered badly. Concentrations fell during the COVID lockdown; they also fall
every May as the north-east monsoon gives way to southerly flow. Separating the
two needs a counterfactual: what would the concentration have been, on those
particular days, with that particular weather, if the event had not happened?

**This deliberately does not reuse M4's normalised series.** That series is
built by resampling every predictor except the long-run trend, so its output is
a smooth function of time — measured on 忠明 it moves about 0.05 µg/m³ per
month. It resolves a two-decade trend beautifully and a ten-week lockdown not at
all. Different question, different tool.

The tool here is a plain counterfactual:

1. Fit a model of daily PM2.5 on weather and calendar, using **only days
   outside the event window** (plus a buffer, so the run-up does not leak in).
2. Predict the event window using the weather that actually occurred.
3. The gap between observed and predicted is what the weather and the season
   cannot explain.

Daily rather than hourly: a ten-week event is not a diurnal question, and the
aggregation makes the whole thing fast enough to run placebos, which matters
more than the lost resolution.

**Placebos are not optional here.** The same procedure is run on the same
calendar window in every other year of the record, where by construction there
was no event. If those placebo "effects" are as large as the real one, the
method has measured seasonal misfit rather than a policy, and the honest
reading is that nothing was detected. The placebo distribution is reported
beside every estimate for exactly that reason.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from twair.paths import processed_dir

log = logging.getLogger(__name__)

__all__ = [
    "DAILY_FEATURES",
    "EventEffect",
    "build_daily_frame",
    "counterfactual",
    "placebo_distribution",
    "run_causal",
]

TARGET = "PM2.5"

# Weather and calendar only. Chemistry is excluded for the same reason as in
# M4: NOx and CO fall during a lockdown too, so letting the model see them
# would explain the effect away using its own consequence.
DAILY_FEATURES: tuple[str, ...] = (
    "AMB_TEMP",
    "RH",
    "RAINFALL",
    "WS_HR",
    "wd_sin",
    "wd_cos",
    "doy_sin",
    "doy_cos",
    "dow_sin",
    "dow_cos",
    "trend_days",
)

_POLLUTANTS = ("PM2.5", "AMB_TEMP", "RH", "RAINFALL", "WS_HR", "WD_HR")

# Days either side of the event excluded from training. Anticipation and
# recovery are real — people changed behaviour before the formal announcement
# and did not resume instantly — and letting either into the training set would
# teach the model that those days are normal.
DEFAULT_BUFFER_DAYS = 30


@dataclass(frozen=True, slots=True)
class EventEffect:
    """What happened during the window, against what the weather predicted."""

    event: str
    station: str
    n_days: int
    observed_mean: float
    predicted_mean: float
    effect: float
    """Observed minus predicted. Negative means cleaner than the weather implied."""
    effect_pct: float
    ci_low: float
    ci_high: float
    holdout_rmse: float
    """How well the model predicts ordinary days it did not train on."""
    placebo_mean: float
    placebo_sd: float
    placebo_n: int
    z_against_placebo: float
    """Effect in units of the placebo spread. This, not the CI, is the real test."""

    @property
    def credible(self) -> bool:
        """Distinguishable from what the method finds when nothing happened.

        A bootstrap interval says the estimate is precise. It cannot say the
        estimate is *right* — a model that misfits every May will misfit this
        May too, precisely and wrongly. Only the placebos speak to that.
        """
        return abs(self.z_against_placebo) >= 2.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "event": self.event,
            "station": self.station,
            "n_days": self.n_days,
            "observed_mean": self.observed_mean,
            "predicted_mean": self.predicted_mean,
            "effect": self.effect,
            "effect_pct": self.effect_pct,
            "ci_low": self.ci_low,
            "ci_high": self.ci_high,
            "holdout_rmse": self.holdout_rmse,
            "placebo_mean": self.placebo_mean,
            "placebo_sd": self.placebo_sd,
            "placebo_n": self.placebo_n,
            "z_against_placebo": self.z_against_placebo,
            "credible": self.credible,
        }


def build_daily_frame(
    root: Path | None = None,
    *,
    period: tuple[int, int] = (2006, 2025),
    stations: list[str] | None = None,
) -> pl.DataFrame:
    """Station-day matrix: PM2.5 plus the weather and calendar features.

    Built from the gated daily aggregates, so a day whose mean was withheld for
    insufficient coverage is absent rather than present and wrong.
    """
    path = (root or processed_dir("daily")) / "daily.parquet"
    if not path.exists():
        raise FileNotFoundError(f"{path} not found — run `twair aggregate` first")

    start, end = period
    lazy = pl.scan_parquet(path).filter(
        pl.col("date").dt.year().is_between(start, end)
        & pl.col("pollutant").is_in(list(_POLLUTANTS))
        & pl.col("mean").is_not_null()
    )
    if stations:
        lazy = lazy.filter(pl.col("station_name").is_in(stations))

    wide = (
        lazy.select("station_name", "pollutant", "date", "mean")
        .collect()
        .pivot(on="pollutant", index=["station_name", "date"], values="mean")
    )

    for column in _POLLUTANTS:
        if column not in wide.columns:
            wide = wide.with_columns(pl.lit(None, dtype=pl.Float64).alias(column))

    radians = pl.col("WD_HR") * (np.pi / 180.0)
    doy = pl.col("date").dt.ordinal_day()
    dow = pl.col("date").dt.weekday()

    return (
        wide.with_columns(
            radians.sin().alias("wd_sin"),
            radians.cos().alias("wd_cos"),
            (doy * (2 * np.pi / 365.25)).sin().alias("doy_sin"),
            (doy * (2 * np.pi / 365.25)).cos().alias("doy_cos"),
            (dow * (2 * np.pi / 7.0)).sin().alias("dow_sin"),
            (dow * (2 * np.pi / 7.0)).cos().alias("dow_cos"),
            (pl.col("date") - pl.lit(date(1982, 1, 1))).dt.total_days().alias("trend_days"),
        )
        .drop_nulls([TARGET])
        .sort("station_name", "date")
    )


def _fit_and_predict(
    train: pl.DataFrame,
    predict_on: pl.DataFrame,
    *,
    features: list[str],
    seed: int,
) -> tuple[np.ndarray, float]:
    """Fit on ``train``, predict ``predict_on``, and score on held-out train rows."""
    import lightgbm as lgb

    x = train.select(features).to_numpy()
    y = train[TARGET].to_numpy()

    rng = np.random.default_rng(seed)
    mask = rng.random(len(y)) >= 0.2

    model = lgb.LGBMRegressor(
        n_estimators=400,
        learning_rate=0.05,
        num_leaves=31,
        min_child_samples=20,
        subsample=0.8,
        subsample_freq=1,
        colsample_bytree=0.8,
        n_jobs=-1,
        random_state=seed,
        verbose=-1,
    )
    model.fit(x[mask], y[mask])
    residual = y[~mask] - model.predict(x[~mask])
    holdout_rmse = float(np.sqrt(np.mean(residual**2)))

    model.fit(x, y)
    return np.asarray(model.predict(predict_on.select(features).to_numpy())), holdout_rmse


def _block_bootstrap_mean(
    values: np.ndarray, *, block: int = 7, n_boot: int = 1000, seed: int = 0
) -> tuple[float, float]:
    """Percentile interval for a mean, resampling contiguous weeks.

    Daily residuals are autocorrelated — a stagnant spell lasts days — so an
    ordinary bootstrap over individual days would understate the spread.
    """
    rng = np.random.default_rng(seed)
    n = values.size
    block = min(max(block, 1), n)
    n_blocks = int(np.ceil(n / block))

    means = np.empty(n_boot)
    for i in range(n_boot):
        starts = rng.integers(0, max(1, n - block), size=n_blocks)
        index = np.concatenate([np.arange(s, min(s + block, n)) for s in starts])[:n]
        means[i] = values[index].mean()
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def counterfactual(
    frame: pl.DataFrame,
    *,
    event_name: str,
    station: str,
    start: date,
    end: date | None,
    buffer_days: int = DEFAULT_BUFFER_DAYS,
    placebo_years: int = 0,
    seed: int = 20260729,
) -> EventEffect | None:
    """Estimate one event's effect at one station.

    ``placebo_years`` is filled in by :func:`placebo_distribution`; callers
    normally use :func:`run_causal`, which wires the two together.
    """
    subset = frame.filter(pl.col("station_name") == station).sort("date")
    features = [f for f in DAILY_FEATURES if f in subset.columns]
    complete = subset.drop_nulls(features)

    window_end = end or start
    in_window = pl.col("date").is_between(start, window_end)
    excluded = pl.col("date").is_between(
        start - timedelta(days=buffer_days), window_end + timedelta(days=buffer_days)
    )

    window = complete.filter(in_window)
    train = complete.filter(~excluded)

    if window.height < 14 or train.height < 500:
        log.info(
            "%s @ %s: %d window days, %d training days — too few",
            event_name,
            station,
            window.height,
            train.height,
        )
        return None

    predicted, holdout_rmse = _fit_and_predict(train, window, features=features, seed=seed)
    observed = window[TARGET].to_numpy()
    residual = observed - predicted

    effect = float(residual.mean())
    ci_low, ci_high = _block_bootstrap_mean(residual, seed=seed)

    return EventEffect(
        event=event_name,
        station=station,
        n_days=window.height,
        observed_mean=float(observed.mean()),
        predicted_mean=float(predicted.mean()),
        effect=effect,
        effect_pct=100.0 * effect / float(predicted.mean()) if predicted.mean() else float("nan"),
        ci_low=ci_low,
        ci_high=ci_high,
        holdout_rmse=holdout_rmse,
        placebo_mean=float("nan"),
        placebo_sd=float("nan"),
        placebo_n=0,
        z_against_placebo=float("nan"),
    )


def placebo_distribution(
    frame: pl.DataFrame,
    *,
    station: str,
    start: date,
    end: date | None,
    exclude_years: set[int],
    buffer_days: int = DEFAULT_BUFFER_DAYS,
    seed: int = 20260729,
) -> list[float]:
    """The same estimate, on the same calendar window, in years with no event.

    This is the control the 2018 project's methods had no equivalent of. If a
    model systematically misses late May, it will miss late May 2021 too, and
    the miss will look exactly like a lockdown effect. The only way to know is
    to ask it about late May in years when nothing happened.
    """
    window_end = end or start
    span_days = (window_end - start).days

    years = sorted({int(y) for y in frame["date"].dt.year().unique()} - exclude_years)
    effects: list[float] = []

    for year in years:
        try:
            fake_start = start.replace(year=year)
        except ValueError:  # 29 February in a non-leap year
            continue
        fake_end = fake_start + timedelta(days=span_days)

        result = counterfactual(
            frame,
            event_name=f"placebo_{year}",
            station=station,
            start=fake_start,
            end=fake_end,
            buffer_days=buffer_days,
            seed=seed,
        )
        if result is not None:
            effects.append(result.effect)

    return effects


@dataclass(frozen=True, slots=True)
class TrendBreak:
    """A change in slope at a date, and whether it beats the same test elsewhere."""

    event: str
    station: str
    slope_before: float
    slope_after: float
    delta: float
    """After minus before, in µg/m³ per year. Negative means the decline steepened."""
    n_before: int
    n_after: int
    placebo_mean: float
    """Mean delta over other candidate break dates in the same series.

    Rarely zero, and that is the point. A decline that decelerates everywhere —
    which is what diminishing returns look like — produces a positive delta at
    every date you test. Comparing this event's delta against zero would then
    report an effect at every station.
    """
    placebo_sd: float
    placebo_n: int
    z_against_placebo: float

    @property
    def credible(self) -> bool:
        return abs(self.z_against_placebo) >= 2.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "event": self.event,
            "station": self.station,
            "slope_before": self.slope_before,
            "slope_after": self.slope_after,
            "delta": self.delta,
            "n_before": self.n_before,
            "n_after": self.n_after,
            "placebo_mean": self.placebo_mean,
            "placebo_sd": self.placebo_sd,
            "placebo_n": self.placebo_n,
            "z_against_placebo": self.z_against_placebo,
            "credible": self.credible,
        }


def _segmented_slopes(
    years: np.ndarray, values: np.ndarray, break_at: float
) -> tuple[float, float, int, int]:
    """Least-squares slope either side of a breakpoint."""
    before = years < break_at
    after = ~before
    if before.sum() < 12 or after.sum() < 12:
        raise ValueError("need at least a year of months on each side of the break")

    slope_before = float(np.polyfit(years[before], values[before], 1)[0])
    slope_after = float(np.polyfit(years[after], values[after], 1)[0])
    return slope_before, slope_after, int(before.sum()), int(after.sum())


def trend_break(
    monthly: pl.DataFrame,
    *,
    event_name: str,
    station: str,
    when: date,
    min_placebo_gap_years: float = 3.0,
) -> TrendBreak | None:
    """Did the *rate* of change shift at this date?

    The right question for an open-ended policy. A law that takes effect and
    stays in effect is not a window with a before and an after — it is a regime
    change, and what it should alter is the slope, not the level.

    This runs on **M4's meteorologically normalised monthly series**, which is
    where that series is genuinely the right tool: it is smooth by construction,
    which makes it useless for a ten-week lockdown and ideal for asking whether
    a multi-year decline got steeper.

    Placebos again do the real work. The same segmented fit is run at every
    other candidate month at least ``min_placebo_gap_years`` from the true date;
    a series that is even slightly curved will show a "break" wherever you look
    for one, and the only way to know whether this break is special is to try
    the others.

    **The gap matters more than it looks.** A real break contaminates its own
    null: a segmented fit placed a year to its left still straddles it, reports
    a large delta of its own, and inflates the placebo mean it is supposed to be
    measured against. On a synthetic series with a genuine kink of -2.74 per
    year:

        gap = 1 year   placebo mean -1.88   z = -1.54   missed
        gap = 2 years  placebo mean -1.77   z = -1.87   missed
        gap = 3 years  placebo mean -1.66   z = -2.28   found
        gap = 4 years  placebo mean -1.54   z = -2.82   found

    Three years is the default. Even so this test is **conservative by
    construction** — the null is drawn from the same series as the signal — and
    it will miss real breaks before it invents one. For asking whether a policy
    visibly bent a trend, erring that way is correct.
    """
    subset = monthly.filter(pl.col("station_name") == station).sort("month")
    if subset.height < 36:
        return None

    months = subset["month"].cast(pl.Date).to_numpy().astype("datetime64[D]").astype("int64")
    years = (months - months[0]) / 365.25
    values = subset["normalised"].to_numpy()

    break_at = float((np.datetime64(when, "D").astype("int64") - months[0]) / 365.25)

    try:
        before, after, n_before, n_after = _segmented_slopes(years, values, break_at)
    except ValueError:
        return None

    placebos: list[float] = []
    for candidate in np.arange(years.min() + 1.0, years.max() - 1.0, 0.25):
        if abs(candidate - break_at) < min_placebo_gap_years:
            continue
        try:
            b, a, _, _ = _segmented_slopes(years, values, candidate)
        except ValueError:
            continue
        placebos.append(a - b)

    delta = after - before
    # Centred on the placebo mean, not on zero. A uniformly decelerating series
    # yields a positive delta wherever the break is placed, so "delta != 0" is
    # a property of the curve rather than of the date being tested.
    enough = len(placebos) >= 3
    mean = float(np.mean(placebos)) if enough else float("nan")
    sd = float(np.std(placebos, ddof=1)) if enough else float("nan")
    z = (delta - mean) / sd if enough and np.isfinite(sd) and sd > 0 else float("nan")

    return TrendBreak(
        event=event_name,
        station=station,
        slope_before=before,
        slope_after=after,
        delta=delta,
        n_before=n_before,
        n_after=n_after,
        placebo_mean=mean,
        placebo_sd=sd,
        placebo_n=len(placebos),
        z_against_placebo=z,
    )


def run_trend_breaks(
    monthly: pl.DataFrame | None = None,
    *,
    stations: list[str] | None = None,
) -> pl.DataFrame:
    """Every verified open-ended event against M4's normalised series."""
    from twair.analysis.events import load_events
    from twair.paths import outputs_dir

    if monthly is None:
        path = outputs_dir("m4_deweather") / "monthly.parquet"
        if not path.exists():
            raise FileNotFoundError(f"{path} not found — run `twair analyze m4` first")
        monthly = pl.read_parquet(path)

    open_ended = load_events().filter(pl.col("end").is_null())
    if open_ended.is_empty():
        return pl.DataFrame()

    available = stations or sorted(monthly["station_name"].unique().to_list())
    rows = []
    for event in open_ended.iter_rows(named=True):
        for station in available:
            result = trend_break(
                monthly, event_name=event["name"], station=station, when=event["start"]
            )
            if result is not None:
                rows.append(result.as_dict())

    return pl.DataFrame(rows).sort("event", "delta") if rows else pl.DataFrame()


def _attach_placebos(result: EventEffect, placebos: list[float]) -> EventEffect:
    """Score the estimate against the placebo spread.

    Fewer than three placebos leaves the fields NaN rather than computing a
    z-score from two numbers — an estimate that cannot be checked should look
    unchecked, not confident.
    """
    if len(placebos) < 3:
        return replace(result, placebo_n=len(placebos))

    mean = float(np.mean(placebos))
    sd = float(np.std(placebos, ddof=1))
    return replace(
        result,
        placebo_mean=mean,
        placebo_sd=sd,
        placebo_n=len(placebos),
        z_against_placebo=(result.effect - mean) / sd if sd > 0 else float("nan"),
    )


def run_causal(
    root: Path | None = None,
    *,
    period: tuple[int, int] = (2006, 2025),
    stations: list[str] | None = None,
    max_stations: int | None = None,
    seed: int = 20260729,
) -> dict[str, pl.DataFrame]:
    """Every verified event, at every station with enough data."""
    from twair.analysis.events import load_events

    events = load_events()
    if events.is_empty():
        raise RuntimeError(
            "no verified events in conf/events.yaml — see its header for what verification means"
        )

    frame = build_daily_frame(root, period=period, stations=stations)
    available = sorted(frame["station_name"].unique().to_list())
    if max_stations:
        available = available[:max_stations]

    log.info("%d verified event(s) x %d station(s)", events.height, len(available))

    rows: list[dict[str, Any]] = []
    placebo_rows: list[dict[str, Any]] = []

    for event in events.iter_rows(named=True):
        window_end = event["end"] or event["start"]
        event_years = set(range(event["start"].year, window_end.year + 1))

        for station in available:
            result = counterfactual(
                frame,
                event_name=event["name"],
                station=station,
                start=event["start"],
                end=event["end"],
                seed=seed,
            )
            if result is None:
                continue

            placebos = placebo_distribution(
                frame,
                station=station,
                start=event["start"],
                end=event["end"],
                exclude_years=event_years,
                seed=seed,
            )
            for value in placebos:
                placebo_rows.append(
                    {"event": event["name"], "station": station, "placebo_effect": value}
                )

            result = _attach_placebos(result, placebos)
            rows.append(result.as_dict())

    if not rows:
        raise RuntimeError("no station-event pair had enough data")

    return {
        "effects": pl.DataFrame(rows).sort("event", "effect"),
        "placebos": pl.DataFrame(placebo_rows),
    }


def write_causal_report(tables: dict[str, pl.DataFrame]) -> dict[str, Path]:
    from twair.paths import outputs_dir

    destination = outputs_dir("m5_causal")
    destination.mkdir(parents=True, exist_ok=True)

    written: dict[str, Path] = {}
    for name, frame in tables.items():
        path = destination / f"{name}.parquet"
        frame.write_parquet(path)
        written[name] = path
    return written
