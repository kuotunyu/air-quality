"""M2 — what actually drives hourly PM2.5.

The counterpart to :mod:`twair.analysis.replication`. Same underlying data,
every one of the original's method choices reversed:

============================  ==============================  ===========================
                              2018                            here
============================  ==============================  ===========================
resolution                    monthly means, N ≈ 7,286        hourly, N ≈ 10^6
wind direction                raw bearing 0-360               sin/cos plus u/v vector
PM10                          a predictor                     excluded; ratio only
time of day, day of week      averaged away                   cyclic features
collinear NO/NO2/NOx          all three, stepwise removal     NOx and NO2/NOx ratio
validation                    in-sample AIC/BIC               rolling origin, LOSO, LOYO
comparison                    none                            persistence and climatology
============================  ==============================  ===========================

The headline number is the honest one: how much explanatory power survives once
PM10 is no longer allowed to predict its own subset.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import polars as pl

from twair.features.chem import add_chem_features
from twair.features.met import WIND_FEATURES, add_wind_features
from twair.features.temporal import TEMPORAL_FEATURES, add_temporal_features
from twair.models.evaluate import (
    Metrics,
    climatology_baseline,
    evaluate_predictions,
    leave_one_station_out,
    leave_one_year_out,
    persistence_baseline,
    rolling_origin,
)
from twair.qc.rainfall import usable
from twair.store.stations import normalise_name_expr
from twair.store.writer import scan_observations

log = logging.getLogger(__name__)

__all__ = [
    "FEATURE_SETS",
    "DriverResult",
    "build_modelling_frame",
    "run_drivers",
]

TARGET = "PM2.5"

# Pollutants read from the store. PM10 is loaded because the ratio needs it and
# because the leakage comparison needs it — never because it is a predictor.
POLLUTANTS = (
    "PM2.5",
    "PM10",
    "AMB_TEMP",
    "CO",
    "NO",
    "NO2",
    "NOx",
    "O3",
    "RAINFALL",
    "RH",
    "SO2",
    "WD_HR",
    "WS_HR",
)

# NO, NO2 and NOx cannot all be used: NO + NO2 = NOx exactly. The original kept
# all three and let a stepwise search thrash between them. Here NOx carries the
# level and NO2/NOx the photochemical age, which is the same information
# without the singularity.
_CHEMISTRY = ("CO", "NOx", "O3", "SO2", "no2_nox_ratio", "ox")
_WEATHER = ("AMB_TEMP", "RH", "RAINFALL", "WS_HR", *WIND_FEATURES)

FEATURE_SETS: dict[str, tuple[str, ...]] = {
    # What the original effectively had, minus the leak.
    "chemistry_only": _CHEMISTRY,
    "weather_only": _WEATHER,
    # The honest specification.
    "full": (*_CHEMISTRY, *_WEATHER, *TEMPORAL_FEATURES),
    # Same as `full` plus the definitional leak, to quantify what it was worth.
    "full_with_pm10": (*_CHEMISTRY, *_WEATHER, *TEMPORAL_FEATURES, "PM10"),
    # The original's own wind treatment, for the M3 contrast.
    "full_raw_wind": (
        *_CHEMISTRY,
        "AMB_TEMP",
        "RH",
        "RAINFALL",
        "WS_HR",
        "WD_HR",
        *TEMPORAL_FEATURES,
    ),
}


def build_modelling_frame(
    root: Path | None = None,
    *,
    period: tuple[int, int] = (2010, 2017),
    stations: list[str] | None = None,
) -> pl.DataFrame:
    """Hourly station-level matrix with every feature attached.

    Defaults to the original's window so the two analyses are compared on the
    same years rather than on different data.
    """
    start, end = period

    long = scan_observations(root).filter(
        pl.col("ts_local").dt.year().is_between(start, end)
        & pl.col("pollutant").is_in(list(POLLUTANTS))
        & usable()
    )
    if stations:
        long = long.filter(normalise_name_expr().is_in(stations))

    tidy = long.select(
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

    for column in POLLUTANTS:
        if column not in wide.columns:
            wide = wide.with_columns(pl.lit(None, dtype=pl.Float64).alias(column))

    featured = add_temporal_features(add_chem_features(add_wind_features(wide)))
    return featured.drop_nulls([TARGET]).sort("station_name", "ts_local")


@dataclass
class DriverResult:
    feature_set: str
    model: str
    split_kind: str
    metrics: dict[str, Metrics] = field(default_factory=dict)
    importance: pl.DataFrame | None = None

    def summary(self) -> pl.DataFrame:
        return pl.DataFrame(
            [
                {
                    "feature_set": self.feature_set,
                    "model": self.model,
                    "split_kind": self.split_kind,
                    "split": name,
                    **m.as_dict(),
                }
                for name, m in self.metrics.items()
            ]
        )


def _prepare(frame: pl.DataFrame, features: list[str]) -> tuple[np.ndarray, np.ndarray]:
    subset = frame.select([*features, TARGET]).drop_nulls()
    x = subset.select(features).to_numpy()
    y = subset[TARGET].to_numpy()
    return x, y


# TreeSHAP costs roughly (trees x leaves) per row, so computing it over a
# multi-hundred-thousand-row test set dominates the whole fit. A sample of this
# size pins mean |SHAP| to well within the spread across splits.
SHAP_SAMPLE = 20_000


def _fit_predict_gbdt(
    train: pl.DataFrame,
    test: pl.DataFrame,
    features: list[str],
    *,
    seed: int = 0,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Fit LightGBM and return (truth, prediction, mean |SHAP| per feature).

    SHAP comes from LightGBM's own ``pred_contrib``, which is exact TreeSHAP —
    no separate explainability dependency — computed on a sample of the test
    set rather than all of it.
    """
    import lightgbm as lgb

    x_train, y_train = _prepare(train, features)
    test_subset = test.select([*features, TARGET]).drop_nulls()
    x_test = test_subset.select(features).to_numpy()
    y_test = test_subset[TARGET].to_numpy()

    if x_train.size == 0 or x_test.size == 0:
        return np.array([]), np.array([]), np.zeros(len(features))

    model = lgb.LGBMRegressor(
        n_estimators=300,
        learning_rate=0.05,
        num_leaves=63,
        min_child_samples=50,
        subsample=0.8,
        subsample_freq=1,
        colsample_bytree=0.8,
        random_state=seed,
        n_jobs=-1,
        verbose=-1,
    )
    model.fit(x_train, y_train)

    prediction = model.predict(x_test)

    rng = np.random.default_rng(seed)
    if x_test.shape[0] > SHAP_SAMPLE:
        sample = x_test[rng.choice(x_test.shape[0], SHAP_SAMPLE, replace=False)]
    else:
        sample = x_test
    # Last column is the base value, so it is dropped.
    contributions = model.predict(sample, pred_contrib=True)[:, :-1]
    return y_test, np.asarray(prediction), np.abs(contributions).mean(axis=0)


def run_drivers(
    frame: pl.DataFrame,
    *,
    feature_set: str = "full",
    split_kind: str = "rolling",
    n_splits: int = 4,
    seed: int = 0,
    stations: list[str] | None = None,
) -> DriverResult:
    """Fit and score one feature set under one split strategy.

    ``stations`` restricts leave-one-station-out to a subset. Holding out all
    77 stations in turn is 77 fits for an answer a spread of eight gives just
    as well.
    """
    features = list(FEATURE_SETS[feature_set])
    missing = [f for f in features if f not in frame.columns]
    if missing:
        raise KeyError(f"frame lacks features {missing}")

    splitters = {
        "rolling": lambda f: rolling_origin(f, n_splits=n_splits),
        "station": lambda f: leave_one_station_out(f, stations=stations),
        "year": leave_one_year_out,
    }
    if split_kind not in splitters:
        raise ValueError(f"unknown split_kind {split_kind!r}")

    result = DriverResult(feature_set=feature_set, model="lightgbm", split_kind=split_kind)
    importances: list[np.ndarray] = []

    for split in splitters[split_kind](frame):
        truth, prediction, contribution = _fit_predict_gbdt(
            split.train, split.test, features, seed=seed
        )
        if truth.size == 0:
            continue
        result.metrics[split.name] = evaluate_predictions(truth, prediction)
        importances.append(contribution)

    if importances:
        result.importance = pl.DataFrame(
            {
                "feature": features,
                "mean_abs_shap": np.mean(importances, axis=0),
            }
        ).sort("mean_abs_shap", descending=True)

    return result


def baseline_scores(frame: pl.DataFrame, *, n_splits: int = 4) -> pl.DataFrame:
    """Score persistence and climatology under the same rolling-origin splits.

    Any model result is meaningless without these beside it.
    """
    rows: list[dict[str, object]] = []
    for split in rolling_origin(frame, n_splits=n_splits):
        for name, prediction in (
            ("persistence", persistence_baseline(split.test, target=TARGET)),
            ("climatology", climatology_baseline(split.train, split.test, target=TARGET)),
        ):
            ordered = (
                split.test.sort(["station_name", "ts_local"])
                if name == "persistence"
                else split.test
            )
            metrics = evaluate_predictions(ordered[TARGET], prediction)
            rows.append(
                {
                    "feature_set": "-",
                    "model": name,
                    "split_kind": "rolling",
                    "split": split.name,
                    **metrics.as_dict(),
                }
            )
    return pl.DataFrame(rows)
