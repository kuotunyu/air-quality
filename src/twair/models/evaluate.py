"""Out-of-sample evaluation: splits, baselines and metrics.

A common practice is to report only in-sample fit — AIC, BIC and a stepwise search
over the same rows the model was estimated on. Nothing there could detect
overfitting, and nothing there answered the question a reader actually has:
would this model have been any use on data it had not seen?

Three split strategies, because "unseen" means three different things:

* **rolling origin** — train on the past, test on the future. The only honest
  test for anything that will be used to forecast.
* **leave one station out** — can the model reach a site it was never fitted
  on? This is what "a model for Taiwan" has to mean.
* **leave one year out** — does it survive a year with unusual weather?

Every score is reported beside a baseline. A model that cannot beat persistence
has demonstrated nothing, however good its R² looks.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass

import numpy as np
import polars as pl

from twair.scalars import as_float

log = logging.getLogger(__name__)

__all__ = [
    "Metrics",
    "Split",
    "climatology_baseline",
    "evaluate_predictions",
    "leave_one_station_out",
    "leave_one_year_out",
    "persistence_baseline",
    "rolling_origin",
]


@dataclass(frozen=True, slots=True)
class Split:
    name: str
    train: pl.DataFrame
    test: pl.DataFrame

    def __repr__(self) -> str:
        return f"Split({self.name!r}, train={self.train.height:,}, test={self.test.height:,})"


@dataclass(frozen=True, slots=True)
class Metrics:
    n: int
    rmse: float
    mae: float
    r2: float
    exceedance_f1: float | None = None
    """F1 for predicting a threshold exceedance — often what a reader cares about.

    Getting the absolute value slightly wrong matters far less than failing to
    flag the day the air is dangerous.
    """

    def as_dict(self) -> dict[str, float | int | None]:
        return {
            "n": self.n,
            "rmse": self.rmse,
            "mae": self.mae,
            "r2": self.r2,
            "exceedance_f1": self.exceedance_f1,
        }


def evaluate_predictions(
    truth: np.ndarray | pl.Series,
    prediction: np.ndarray | pl.Series,
    *,
    exceedance_threshold: float | None = 35.0,
) -> Metrics:
    """Score predictions, ignoring rows where either side is missing.

    The default threshold is 35 µg/m³, Taiwan's 24-hour PM2.5 standard.
    """
    y = np.asarray(truth, dtype=float)
    yhat = np.asarray(prediction, dtype=float)
    keep = np.isfinite(y) & np.isfinite(yhat)
    y, yhat = y[keep], yhat[keep]

    if y.size == 0:
        return Metrics(n=0, rmse=float("nan"), mae=float("nan"), r2=float("nan"))

    error = yhat - y
    rmse = float(np.sqrt(np.mean(error**2)))
    mae = float(np.mean(np.abs(error)))

    variance = float(np.sum((y - y.mean()) ** 2))
    r2 = float(1.0 - np.sum(error**2) / variance) if variance > 0 else float("nan")

    f1 = None
    if exceedance_threshold is not None:
        actual = y > exceedance_threshold
        predicted = yhat > exceedance_threshold
        true_positive = float(np.sum(actual & predicted))
        if true_positive or np.any(predicted) or np.any(actual):
            precision = true_positive / max(float(np.sum(predicted)), 1.0)
            recall = true_positive / max(float(np.sum(actual)), 1.0)
            f1 = (
                float(2 * precision * recall / (precision + recall))
                if (precision + recall) > 0
                else 0.0
            )

    return Metrics(n=int(y.size), rmse=rmse, mae=mae, r2=r2, exceedance_f1=f1)


def rolling_origin(
    frame: pl.DataFrame,
    *,
    time_column: str = "ts_local",
    n_splits: int = 5,
    min_train_fraction: float = 0.4,
) -> Iterator[Split]:
    """Expanding-window splits: always train on the past, test on the future.

    Ordinary k-fold would let a model learn from tomorrow to predict today,
    which flatters any time series and is meaningless for forecasting.
    """
    ordered = frame.sort(time_column)
    total = ordered.height
    if total < n_splits + 1:
        return

    start = int(total * min_train_fraction)
    step = max((total - start) // n_splits, 1)

    for i in range(n_splits):
        train_end = start + i * step
        test_end = min(train_end + step, total)
        if train_end <= 0 or test_end <= train_end:
            continue
        yield Split(
            name=f"rolling_{i + 1}",
            train=ordered.slice(0, train_end),
            test=ordered.slice(train_end, test_end - train_end),
        )


def leave_one_station_out(
    frame: pl.DataFrame,
    *,
    station_column: str = "station_name",
    stations: list[str] | None = None,
) -> Iterator[Split]:
    """Hold out whole stations — tests whether the model generalises spatially."""
    targets = stations or sorted(frame[station_column].unique().to_list())
    for station in targets:
        held_out = pl.col(station_column) == station
        test = frame.filter(held_out)
        if test.is_empty():
            continue
        yield Split(name=f"station_{station}", train=frame.filter(~held_out), test=test)


def leave_one_year_out(
    frame: pl.DataFrame,
    *,
    time_column: str = "ts_local",
    years: list[int] | None = None,
) -> Iterator[Split]:
    """Hold out whole years — tests robustness to unusual weather."""
    year_expr = pl.col(time_column).dt.year()
    targets = years or sorted(frame.select(year_expr.alias("y"))["y"].unique().to_list())
    for year in targets:
        held_out = year_expr == year
        test = frame.filter(held_out)
        if test.is_empty():
            continue
        yield Split(name=f"year_{year}", train=frame.filter(~held_out), test=test)


def persistence_baseline(
    test: pl.DataFrame,
    *,
    target: str = "PM2.5",
    station_column: str = "station_name",
    time_column: str = "ts_local",
) -> pl.Series:
    """Predict each hour from the previous one at the same station.

    Brutally simple and hard to beat at short horizons. Any model that cannot
    is not adding information.
    """
    return (
        test.sort([station_column, time_column])
        .with_columns(pl.col(target).shift(1).over(station_column).alias("_pred"))["_pred"]
        .rename("prediction")
    )


def climatology_baseline(
    train: pl.DataFrame,
    test: pl.DataFrame,
    *,
    target: str = "PM2.5",
    station_column: str = "station_name",
    time_column: str = "ts_local",
) -> pl.Series:
    """Predict the training mean for this station, month and hour.

    Encodes the seasonal and daily cycles and nothing else, so it separates
    "the model learned when pollution is usually high" from "the model learned
    something about this particular hour".
    """
    keys = [station_column, "_month", "_hour"]

    def _with_keys(f: pl.DataFrame) -> pl.DataFrame:
        return f.with_columns(
            pl.col(time_column).dt.month().alias("_month"),
            pl.col(time_column).dt.hour().alias("_hour"),
        )

    lookup = _with_keys(train).group_by(keys).agg(pl.col(target).mean().alias("_clim"))
    fallback = as_float(train[target].mean(), what="climatology fallback")

    return (
        _with_keys(test)
        .join(lookup, on=keys, how="left")
        .with_columns(pl.col("_clim").fill_null(fallback))["_clim"]
        .rename("prediction")
    )
