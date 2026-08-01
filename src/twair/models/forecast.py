"""M9 — forecasting, scored against the baseline that already wins.

Phase 2 left this module a specific instruction. Persistence — "the next hour
looks like this one" — reached R² 0.900 while the best explanatory model
managed 0.524, because persistence used PM2.5's own history and those models
used none of it. The conclusion recorded then was that those were explanatory
models, not forecasting ones, and that any forecasting work would have to treat
persistence as **the bar to clear rather than a peer to compare against**.

So the headline number here is not R². It is **skill**:

    skill = 1 - MSE(model) / MSE(persistence)

Zero means the model matched a one-line rule. Positive means it beat it, and by
how much. Negative means it lost.

I expected negative skill at one hour and was wrong, which is worth recording
rather than quietly deleting. The reason is visible in the feature set:
persistence assumes the concentration is flat, while `delta1` and `delta3` let
the model see that it is currently rising or falling and carry that motion
forward. An hour ahead, extrapolating the trajectory beats assuming there is
none.

Full backtest, 74 stations over 2015-2025, four rolling splits, ~1.78M test
rows per horizon:

===== ======= ============= ============= =================
  h      R²    skill vs      skill vs      worst split vs
                persistence   climatology   persistence
===== ======= ============= ============= =================
   1    0.859       +0.175        +0.839          +0.131
   6    0.550       +0.190        +0.480          **-0.111**
  24    0.351       +0.238        +0.249          +0.189
  48    0.217       +0.243        +0.088          +0.055
===== ======= ============= ============= =================

Three things to read off that, in descending order of how easy they are to get
wrong:

**R² and skill move in opposite directions.** R² falls by a factor of four
across the horizons while skill *rises*. R² is dominated by how predictable the
target happens to be, and PM2.5 an hour out is very predictable by anyone —
including a one-line rule. Skill asks the different question of whether the
model added anything, and the answer improves with distance because persistence
degrades faster than the model does. So horizon is never averaged over.

**The useful range ends around 24 hours, and only the climatology column says
so.** Skill against persistence keeps climbing to 48h, which on its own reads
as "better the further out you go". Skill against climatology collapses over
the same span, +0.839 to +0.088: by two days the model has nearly decayed to
"the average for this station, this month, this hour". Beating persistence at
48h is not an achievement when persistence is beaten by the long-run mean.
**Two baselines are needed because either one alone flatters the model.**

**The mean over splits hid a losing split.** At six hours `rolling_1` scores
-0.111 — the one trained on the least data, since rolling-origin gives split 1
the shortest history. The first summary line printed here said "4/4 horizons
beat persistence", which was true of the means and false of 1 of the 16
horizon-split cells. `summarise_scores` now carries `skill_worst_split`
alongside every mean for that reason.

Validation is rolling-origin: train on everything before a cut, test on what
follows, walk the cut forward. Random splits would let the model train on
Tuesday to predict Monday, which no forecaster can do.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from twair.features.lags import (
    add_lag_features,
    add_target,
    complete_hourly_index,
    lag_feature_names,
)
from twair.scalars import as_float

log = logging.getLogger(__name__)

__all__ = [
    "HORIZONS",
    "ForecastScore",
    "build_feature_frame",
    "build_forecast_frame",
    "run_forecast",
    "skill_score",
    "summarise_scores",
]

TARGET = "PM2.5"

# Hours ahead. 1 is where persistence is nearly unbeatable, 24 is where a
# forecast starts being useful to someone deciding whether to go out tomorrow,
# 48 is where the atmosphere has largely forgotten today.
HORIZONS: tuple[int, ...] = (1, 6, 24, 48)

# Read from the store alongside the target. Chemistry is allowed here, unlike
# in M4 and M5: this module is not trying to attribute anything, it is trying
# to predict, and a forecaster standing at time t genuinely knows the NOx
# reading at time t.
_POLLUTANTS = ("PM2.5", "PM10", "NOx", "O3", "SO2", "CO", "AMB_TEMP", "RH", "WS_HR", "WD_HR")

# One definition, because the Space deploys a model whose skill this module
# measures. Two copies that happen to agree today is a claim with a shelf life;
# `twair.models.deploy` imports this rather than restating it.
MODEL_PARAMS: dict[str, Any] = {
    "n_estimators": 600,
    "learning_rate": 0.05,
    "num_leaves": 63,
    "min_child_samples": 40,
    "subsample": 0.8,
    "subsample_freq": 1,
    "colsample_bytree": 0.8,
    "n_jobs": -1,
    "verbose": -1,
}


@dataclass(frozen=True, slots=True)
class ForecastScore:
    horizon: int
    split: str
    n: int
    stations: int
    """How many stations the test set covers.

    Carried rather than left to whoever reads the number later: chapter 8's
    prose said 「74 站」 as a literal, and the count moves whenever the feature
    frame does — most recently when the station-boundary leak in
    `features/lags.py` was fixed and a station's first 167 hours stopped being
    filled with the previous station's week.
    """
    model_rmse: float
    persistence_rmse: float
    climatology_rmse: float
    model_r2: float
    skill_vs_persistence: float
    """1 - MSE(model)/MSE(persistence). The number that actually matters."""
    skill_vs_climatology: float

    @property
    def beats_persistence(self) -> bool:
        return self.skill_vs_persistence > 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "horizon": self.horizon,
            "split": self.split,
            "n": self.n,
            "stations": self.stations,
            "model_rmse": self.model_rmse,
            "persistence_rmse": self.persistence_rmse,
            "climatology_rmse": self.climatology_rmse,
            "model_r2": self.model_r2,
            "skill_vs_persistence": self.skill_vs_persistence,
            "skill_vs_climatology": self.skill_vs_climatology,
            "beats_persistence": self.beats_persistence,
        }


def skill_score(model_error: np.ndarray, baseline_error: np.ndarray) -> float:
    """Fraction of the baseline's squared error the model removed.

    Positive is better than the baseline, zero is equal to it, negative is
    worse. Undefined when the baseline is perfect, which does not happen on
    real data but does on a constant test fixture.
    """
    baseline_mse = float(np.mean(baseline_error**2))
    if baseline_mse <= 0:
        return float("nan")
    return 1.0 - float(np.mean(model_error**2)) / baseline_mse


def build_feature_frame(
    root: Path | None = None,
    *,
    period: tuple[int, int] = (2015, 2025),
    stations: list[str] | None = None,
) -> tuple[pl.DataFrame, list[str]]:
    """Hourly matrix with every lag feature, and no target at all.

    Split out from :func:`build_forecast_frame` because the features do not
    depend on the horizon — only the target does. Backtesting four horizons
    rebuilt this four times, which is four full scans of the store for an
    identical result, and the deployment bundle needs one feature frame with
    four targets rather than four frames.
    """
    from twair.features.met import add_wind_features
    from twair.features.temporal import TEMPORAL_FEATURES, add_temporal_features
    from twair.qc.rainfall import usable
    from twair.store.stations import normalise_name_expr
    from twair.store.writer import scan_observations

    start, end = period
    lazy = scan_observations(root).filter(
        pl.col("ts_local").dt.year().is_between(start, end)
        & pl.col("pollutant").is_in(list(_POLLUTANTS))
        & usable()
    )
    if stations:
        lazy = lazy.filter(normalise_name_expr().is_in(stations))

    tidy = lazy.select(
        normalise_name_expr(),
        pl.col("pollutant").cast(pl.Utf8),
        "ts_local",
        pl.col("value").cast(pl.Float64),
    ).collect()

    wide = tidy.pivot(
        on="pollutant",
        index=["station_name", "ts_local"],
        values="value",
        aggregate_function="first",
    )
    for column in _POLLUTANTS:
        if column not in wide.columns:
            wide = wide.with_columns(pl.lit(None, dtype=pl.Float64).alias(column))

    # The complete index first: a lag taken across a gap is the bug this whole
    # module is written to avoid.
    full = complete_hourly_index(wide)
    featured = add_temporal_features(add_wind_features(full))
    featured = add_lag_features(featured, column=TARGET)
    featured = add_lag_features(featured, column="PM10", lags=(1, 24), windows=(24,))

    features = (
        lag_feature_names(TARGET)
        + lag_feature_names("PM10", lags=(1, 24), windows=(24,))
        + ["NOx", "O3", "SO2", "CO", "AMB_TEMP", "RH", "WS_HR"]
        + ["wd_sin", "wd_cos"]
        + list(TEMPORAL_FEATURES)
    )
    features = [f for f in features if f in featured.columns]

    return featured.sort("station_name", "ts_local"), features


def build_forecast_frame(
    root: Path | None = None,
    *,
    period: tuple[int, int] = (2015, 2025),
    stations: list[str] | None = None,
    horizon: int = 24,
) -> tuple[pl.DataFrame, list[str]]:
    """Feature matrix plus a target ``horizon`` hours ahead, rows complete.

    Returns the frame and the feature names, so the caller cannot accidentally
    train on a column that leaks. ``target`` and the raw pollutant are
    deliberately not among them.
    """
    featured, features = build_feature_frame(root, period=period, stations=stations)
    with_target = add_target(featured, column=TARGET, horizon=horizon)
    return with_target.drop_nulls(["target", *features]), features


def _fit_predict(
    train: pl.DataFrame, test: pl.DataFrame, features: list[str], *, seed: int
) -> np.ndarray:
    import lightgbm as lgb

    model = lgb.LGBMRegressor(random_state=seed, **MODEL_PARAMS)
    model.fit(train.select(features).to_numpy(), train["target"].to_numpy())
    return np.asarray(model.predict(test.select(features).to_numpy()))


def run_forecast(
    root: Path | None = None,
    *,
    period: tuple[int, int] = (2015, 2025),
    stations: list[str] | None = None,
    horizons: tuple[int, ...] = HORIZONS,
    n_splits: int = 4,
    seed: int = 20260729,
) -> dict[str, pl.DataFrame]:
    """Rolling-origin backtest at every horizon, scored against persistence."""
    from twair.models.evaluate import rolling_origin

    rows: list[dict[str, Any]] = []

    for horizon in horizons:
        frame, features = build_forecast_frame(
            root, period=period, stations=stations, horizon=horizon
        )
        if frame.height < 5000:
            log.warning("horizon %dh: only %d rows — skipping", horizon, frame.height)
            continue

        log.info("horizon %dh: %d rows, %d features", horizon, frame.height, len(features))

        for split in rolling_origin(frame, n_splits=n_splits, time_column="ts_local"):
            predicted = _fit_predict(split.train, split.test, features, seed=seed)
            truth = split.test["target"].to_numpy()

            # Persistence at horizon h is "the value now" — which is exactly
            # lag_1, already in the frame and already known to be leak-free.
            persistence = split.test[f"{TARGET}_lag1"].to_numpy()

            # Climatology: the training mean for this station, month and hour.
            clim_lookup = (
                split.train.with_columns(
                    pl.col("ts_local").dt.month().alias("_m"),
                    pl.col("ts_local").dt.hour().alias("_h"),
                )
                .group_by("station_name", "_m", "_h")
                .agg(pl.col("target").mean().alias("_c"))
            )
            climatology = (
                split.test.with_columns(
                    pl.col("ts_local").dt.month().alias("_m"),
                    pl.col("ts_local").dt.hour().alias("_h"),
                )
                .join(clim_lookup, on=["station_name", "_m", "_h"], how="left")
                .with_columns(pl.col("_c").fill_null(as_float(split.train["target"].mean())))["_c"]
                .to_numpy()
            )

            e_model = truth - predicted
            e_pers = truth - persistence
            e_clim = truth - climatology

            ss_tot = float(np.sum((truth - truth.mean()) ** 2))
            rows.append(
                ForecastScore(
                    horizon=horizon,
                    split=split.name,
                    n=int(truth.size),
                    stations=int(split.test["station_name"].n_unique()),
                    model_rmse=float(np.sqrt(np.mean(e_model**2))),
                    persistence_rmse=float(np.sqrt(np.mean(e_pers**2))),
                    climatology_rmse=float(np.sqrt(np.mean(e_clim**2))),
                    model_r2=1.0 - float(np.sum(e_model**2)) / ss_tot
                    if ss_tot > 0
                    else float("nan"),
                    skill_vs_persistence=skill_score(e_model, e_pers),
                    skill_vs_climatology=skill_score(e_model, e_clim),
                ).as_dict()
            )

    if not rows:
        raise RuntimeError("no horizon had enough data to backtest")

    scores = pl.DataFrame(rows)
    return {"scores": scores.sort("horizon", "split"), "by_horizon": summarise_scores(scores)}


def summarise_scores(scores: pl.DataFrame) -> pl.DataFrame:
    """Collapse splits to one row per horizon — carrying the worst, not just the mean.

    The mean alone reproduces the exact failure this module was written about,
    one level up. The first full backtest averaged to +0.190 at six hours and
    the summary line read "4/4 horizons beat persistence" — while `rolling_1`,
    the split with the least training data, sat at **-0.111**. A mean over four
    splits hid a losing one just as an R² hides a losing model.

    So `skill_worst_split` and `splits_not_beating_persistence` travel with
    every mean, and `tests/test_forecast.py` pins that they do.
    """
    return (
        scores.group_by("horizon")
        .agg(
            pl.len().alias("splits"),
            pl.col("n").sum(),
            # Max, not sum: the same stations recur across splits. The widest
            # split is the honest answer to 「how many stations is this about」.
            pl.col("stations").max(),
            pl.col("model_rmse").mean(),
            pl.col("persistence_rmse").mean(),
            pl.col("climatology_rmse").mean(),
            pl.col("model_r2").mean(),
            pl.col("skill_vs_persistence").mean(),
            pl.col("skill_vs_persistence").min().alias("skill_worst_split"),
            (pl.col("skill_vs_persistence") <= 0).sum().alias("splits_not_beating_persistence"),
            pl.col("skill_vs_climatology").mean(),
            pl.col("skill_vs_climatology").min().alias("skill_vs_climatology_worst"),
        )
        .sort("horizon")
    )


def write_forecast_report(tables: dict[str, pl.DataFrame]) -> dict[str, Path]:
    from twair.paths import outputs_dir

    destination = outputs_dir("m9_forecast")
    destination.mkdir(parents=True, exist_ok=True)

    written: dict[str, Path] = {}
    for name, frame in tables.items():
        path = destination / f"{name}.parquet"
        frame.write_parquet(path)
        written[name] = path
    return written
