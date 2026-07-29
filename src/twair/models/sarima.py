"""D10: the model the 2018 project abandoned for being inconvenient.

Its own words, p.137: 「SARIMA 不在本專題繼續討論…在此實屬不便」. A project whose
premise is answering that work's choices one at a time cannot leave this one
answered by also not doing it.

**The measurement changed the shape of the criticism.** The original was right
that it is inconvenient — automatic order selection on hourly data is
genuinely, quantifiably expensive. What it did not do was say *how* expensive,
or check whether the expense bought anything. One clause reads as a personal
preference; a table with seconds in it reads as an engineering judgement, and
only one of those can be disagreed with.

So this module measures two separate things:

1. **What the inconvenience costs.** `measure_selection_cost` times automatic
   order search against a fixed order at several series lengths. The gap is not
   subtle and it is not SARIMA's fault: the expensive part is the *search*.
2. **What a fixed-order SARIMA is worth.** Scored against persistence and
   climatology **on the same forecast origins**, because a comparison between
   methods evaluated on different rows is not a comparison.

   LightGBM is deliberately absent. Scoring it here would mean rebuilding the
   whole feature pipeline per station-split to land on these same origins, and
   the question D10 actually asks does not need it: if a method cannot beat
   "the value an hour ago" or "the average for this hour", abandoning it was
   right regardless of what a gradient-boosted model does. `models.forecast`
   has the LightGBM numbers, on its own rows, and they are not comparable to
   these.

Three things this does deliberately, each of which would otherwise mislead:

- **The series is a complete hourly index with gaps as NaN.** SARIMA assumes a
  regular grid. The modelling frame is filtered by feature availability, so
  fitting on its rows would silently tell the model that two readings either
  side of a three-day outage are an hour apart. The Kalman filter handles NaN
  natively; a reindex does not.
- **Parameters are fitted once per split and then the state is filtered
  forward**, never refitted per origin. Refitting at every hour is the textbook
  answer and is computationally absurd here; not filtering forward at all would
  have the model forecasting a thousand hours from a stale state and losing for
  a reason that has nothing to do with SARIMA.
- **Convergence is recorded per fit.** At realistic training lengths the
  maximum-likelihood optimiser does not always converge, and a non-converged
  fit reported as "SARIMA's performance" would be a claim about an optimiser.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from twair.paths import outputs_dir

log = logging.getLogger(__name__)

__all__ = [
    "ORIGIN_STRIDE",
    "SARIMA_ORDER",
    "SARIMA_SEASONAL_ORDER",
    "TRAIN_WINDOW_HOURS",
    "SarimaFit",
    "measure_selection_cost",
    "rolling_sarima",
    "run_sarima",
    "station_series",
]

TARGET = "PM2.5"
STATION = "station_name"
TIMESTAMP = "ts_local"

# Fixed, and stated rather than searched. One autoregressive and one
# moving-average term, with the same pair at the 24-hour season: the standard
# starting point for hourly pollutant series, where today's hour resembles both
# the hour before it and the same hour yesterday. Searching for something
# better is what `measure_selection_cost` prices.
SARIMA_ORDER: tuple[int, int, int] = (1, 0, 1)
SARIMA_SEASONAL_ORDER: tuple[int, int, int, int] = (1, 0, 1, 24)

# Forecast origins are sampled every this many hours rather than every hour.
# Producing one honest out-of-sample forecast costs ~1.2s (see the context note
# below), so every hour would be weeks of compute for an answer a few hundred
# origins already give. Three days' spacing also keeps successive origins from
# being near-duplicates.
ORIGIN_STRIDE = 72

# SARIMA is fitted on the trailing year before each test span rather than on
# everything available. This is not a compromise for speed — it is the better
# fit. Measured on 古亭: 8,760 points takes 23.8s and the optimiser converges;
# the expanding window's 38,466 points takes longer *and* fails to converge.
# The model has four parameters, so the extra history mostly buys
# non-convergence.
TRAIN_WINDOW_HOURS = 8760

# Horizons match `models.forecast` so the two are directly comparable.
HORIZONS: tuple[int, ...] = (1, 6, 24, 48)


@dataclass(frozen=True, slots=True)
class SarimaFit:
    """One fitted model, with the facts needed to distrust it."""

    train_points: int
    train_observed: int
    seconds: float
    converged: bool
    aic: float | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "train_points": self.train_points,
            "train_observed": self.train_observed,
            "fit_seconds": round(self.seconds, 2),
            "converged": self.converged,
            "aic": self.aic,
        }


def measure_selection_cost(
    series: np.ndarray,
    *,
    sizes: tuple[int, ...] = (1000, 2000, 5000),
    season_length: int = 24,
) -> pl.DataFrame:
    """Time automatic order selection against a fixed order, same data.

    This is the whole of 「實屬不便」, priced. Both columns fit the same series
    at the same lengths; the only difference is whether the orders are searched
    for or supplied.
    """
    from statsforecast.models import AutoARIMA
    from statsmodels.tsa.statespace.sarimax import SARIMAX

    clean = series[~np.isnan(series)]
    rows: list[dict[str, Any]] = []

    for size in sizes:
        if clean.size < size:
            log.warning("only %d observed points — skipping size %d", clean.size, size)
            continue
        window = clean[:size]

        start = time.perf_counter()
        AutoARIMA(season_length=season_length).fit(window)
        auto_seconds = time.perf_counter() - start

        start = time.perf_counter()
        SARIMAX(
            window,
            order=SARIMA_ORDER,
            seasonal_order=SARIMA_SEASONAL_ORDER,
            enforce_stationarity=False,
            enforce_invertibility=False,
        ).fit(disp=0)
        fixed_seconds = time.perf_counter() - start

        rows.append(
            {
                "points": size,
                "auto_seconds": round(auto_seconds, 2),
                "fixed_seconds": round(fixed_seconds, 2),
                "search_multiple": round(auto_seconds / fixed_seconds, 1)
                if fixed_seconds
                else None,
            }
        )
        log.info(
            "%d points: search %.1fs, fixed order %.1fs (%.0fx)",
            size,
            auto_seconds,
            fixed_seconds,
            auto_seconds / fixed_seconds if fixed_seconds else float("nan"),
        )

    return pl.DataFrame(rows)


def station_series(
    station: str, *, period: tuple[int, int], root: Path | None = None
) -> pl.DataFrame:
    """One station's PM2.5 on a complete hourly grid, gaps kept as null.

    Read straight from the store rather than from ``build_forecast_frame``.
    That frame is filtered by *feature* availability, so an hour with a real
    PM2.5 reading but a missing anemometer is absent from it — and the
    univariate model needs none of those covariates. Measured at 古亭 over
    2015-2025: the forecast frame carries 40,362 usable hours, the store
    carries **92,059**. Fitting on the filtered frame would have handed SARIMA
    a series 58% missing for reasons that have nothing to do with PM2.5, and
    then blamed the model for the result.

    SARIMA also reads position as time, so the grid is completed and the gaps
    stay null; the Kalman filter treats them as missing rather than pretending
    the hours either side of an outage are adjacent.
    """
    from twair.features.lags import complete_hourly_index
    from twair.qc.rainfall import usable
    from twair.store.stations import normalise_name_expr
    from twair.store.writer import scan_observations

    first, last = period
    one = (
        scan_observations(root)
        .filter(
            pl.col(TIMESTAMP).dt.year().is_between(first, last)
            & (pl.col("pollutant") == TARGET)
            & usable()
        )
        .select(
            normalise_name_expr(),
            TIMESTAMP,
            pl.col("value").cast(pl.Float64).alias(TARGET),
        )
        .filter(pl.col(STATION) == station)
        .collect()
        .sort(TIMESTAMP)
    )
    if one.is_empty():
        return one
    # `complete_hourly_index` already does exactly this for the lag features,
    # and a second implementation of "insert the hours nobody reported" is a
    # second thing that can drift.
    return complete_hourly_index(one).sort(TIMESTAMP).select(TIMESTAMP, TARGET)


def rolling_sarima(
    values: np.ndarray,
    *,
    train_end: int,
    horizons: tuple[int, ...] = HORIZONS,
    stride: int = ORIGIN_STRIDE,
) -> tuple[dict[int, list[tuple[int, float]]], SarimaFit | None]:
    """Fit once on the training span, then filter forward through the test span.

    Returns, per horizon, a list of ``(origin index, forecast)`` and the fit's
    own provenance. The origin index is into ``values``, so the caller can line
    the forecast up against the truth and against any other method scored on
    the same rows.
    """
    from statsmodels.tsa.statespace.sarimax import SARIMAX

    # Trailing window, not everything before the split — see TRAIN_WINDOW_HOURS.
    window_start = max(0, train_end - TRAIN_WINDOW_HOURS)
    train = values[window_start:train_end]
    observed = int(np.count_nonzero(~np.isnan(train)))
    if observed < 24 * 30:
        log.warning("only %d observed training points — skipping", observed)
        return {}, None

    start = time.perf_counter()
    model = SARIMAX(
        train,
        order=SARIMA_ORDER,
        seasonal_order=SARIMA_SEASONAL_ORDER,
        enforce_stationarity=False,
        enforce_invertibility=False,
    )
    result = model.fit(disp=0)
    seconds = time.perf_counter() - start
    converged = bool(result.mle_retvals.get("converged", False))
    if not converged:
        log.warning("MLE did not converge on %d points — recorded, not hidden", train.size)

    forecasts: dict[int, list[tuple[int, float]]] = {h: [] for h in horizons}
    longest = max(horizons)
    params = result.params

    # Each origin is filtered from scratch over the preceding window with the
    # parameters held fixed — never refitted, and never `append`ed.
    #
    # `append` looked right and is 2-3x slower here, because it carries the
    # whole history forward and the per-origin cost grows without bound. A
    # short context looked right too, and is wrong for a measured reason: the
    # fitted seasonal AR root is **0.9963**, so 0.9963^n needs on the order of
    # a thousand seasonal cycles to decay and the filter simply does not forget
    # its initialisation inside a few hundred hours. Checked against a
    # full-context reference, a 720-hour context is off by up to 4.8 µg/m³ at
    # one hour and 7.7 at 48; even 2,000 hours is off by 2.3 and 3.2. So the
    # context is the whole training window, and the cost of that is the honest
    # cost of evaluating this model out of sample.
    for origin in range(train_end, values.size - longest, stride):
        context = values[max(0, origin - TRAIN_WINDOW_HOURS) : origin]
        if np.count_nonzero(~np.isnan(context)) < 24 * 30:
            continue
        try:
            filtered = SARIMAX(
                context,
                order=SARIMA_ORDER,
                seasonal_order=SARIMA_SEASONAL_ORDER,
                enforce_stationarity=False,
                enforce_invertibility=False,
            ).filter(params)
            path = np.asarray(filtered.forecast(longest), dtype=float)
        except Exception as exc:  # pragma: no cover - a filter failure is rare
            log.warning("forecast failed at origin %d: %s", origin, type(exc).__name__)
            continue
        for horizon in horizons:
            value = path[horizon - 1]
            if np.isfinite(value):
                forecasts[horizon].append((origin, float(value)))

    return forecasts, SarimaFit(
        train_points=int(train.size),
        train_observed=observed,
        seconds=seconds,
        converged=converged,
        aic=float(result.aic) if np.isfinite(result.aic) else None,
    )


def _scores(truth: np.ndarray, prediction: np.ndarray) -> dict[str, float]:
    error = truth - prediction
    ss_tot = float(np.sum((truth - truth.mean()) ** 2))
    return {
        "rmse": float(np.sqrt(np.mean(error**2))),
        "mae": float(np.mean(np.abs(error))),
        "r2": 1.0 - float(np.sum(error**2)) / ss_tot if ss_tot > 0 else float("nan"),
    }


def run_sarima(
    root: Path | None = None,
    *,
    period: tuple[int, int] = (2015, 2025),
    stations: list[str] | None = None,
    sample: int = 6,
    horizons: tuple[int, ...] = HORIZONS,
    n_splits: int = 3,
    cost_sizes: tuple[int, ...] = (1000, 2000, 5000),
) -> dict[str, pl.DataFrame]:
    """The D10 comparison: SARIMA against the methods that replaced it."""
    from twair.models.evaluate import rolling_origin
    from twair.store.stations import normalise_name_expr
    from twair.store.writer import scan_observations

    first, last = period
    available = sorted(
        scan_observations(root)
        .filter(
            pl.col(TIMESTAMP).dt.year().is_between(first, last) & (pl.col("pollutant") == TARGET)
        )
        .select(normalise_name_expr())
        .unique()
        .collect()[STATION]
        .to_list()
    )
    if not available:
        raise RuntimeError("no PM2.5 in the requested period; is the store built?")

    chosen = stations or _spread(available, sample)
    log.info("SARIMA on %d station(s): %s", len(chosen), ", ".join(chosen))

    cost = measure_selection_cost(
        station_series(chosen[0], period=period, root=root)[TARGET].to_numpy().astype(float),
        sizes=cost_sizes,
    )

    fits: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []

    for station in chosen:
        series = station_series(station, period=period, root=root)
        if series.is_empty():
            continue
        values = series[TARGET].to_numpy().astype(float)

        for split in rolling_origin(series, n_splits=n_splits, time_column=TIMESTAMP):
            train_end = split.train.height
            climatology = _climatology(series, train_end)
            forecasts, fit = rolling_sarima(values, train_end=train_end, horizons=horizons)
            if fit is None:
                continue
            fits.append({**fit.as_dict(), "station_name": station, "split": split.name})

            for horizon in horizons:
                pairs = forecasts.get(horizon, [])
                # Score only where the truth exists. A forecast for an hour the
                # instrument did not report is not wrong, it is unscorable.
                usable = [
                    (origin, value)
                    for origin, value in pairs
                    if np.isfinite(values[origin + horizon]) and np.isfinite(values[origin])
                ]
                if len(usable) < 30:
                    continue
                origins = np.array([o for o, _ in usable])
                predicted = np.array([v for _, v in usable])
                truth = values[origins + horizon]

                scored = {
                    "sarima": predicted,
                    # Persistence at horizon h is the value at the origin — the
                    # same definition models.forecast uses.
                    "persistence": values[origins],
                    # Station-month-hour mean over the training span. The bar
                    # SARIMA has to clear before its inconvenience buys
                    # anything at all.
                    "climatology": climatology[origins + horizon],
                }
                for name, prediction in scored.items():
                    rows.append(
                        {
                            "station_name": station,
                            "split": split.name,
                            "horizon": horizon,
                            "method": name,
                            "n": len(usable),
                            **_scores(truth, prediction),
                        }
                    )

    if not rows:
        raise RuntimeError("no station-split produced a scorable SARIMA forecast")

    scores = pl.DataFrame(rows)
    fit_table = pl.DataFrame(fits)

    out = outputs_dir("m12_sarima")
    out.mkdir(parents=True, exist_ok=True)
    cost.write_parquet(out / "selection_cost.parquet")
    fit_table.write_parquet(out / "fits.parquet")
    scores.write_parquet(out / "scores.parquet")

    return {"selection_cost": cost, "fits": fit_table, "scores": scores}


def _climatology(series: pl.DataFrame, train_end: int) -> np.ndarray:
    """Station-month-hour mean over the training span, broadcast to every row.

    Fitted on the training span only. Computing it over the whole series would
    let the baseline see the test period, which would make the bar SARIMA has
    to clear artificially high and the comparison meaningless.
    """
    stamped = series.with_columns(
        pl.col(TIMESTAMP).dt.month().alias("_m"), pl.col(TIMESTAMP).dt.hour().alias("_h")
    )
    lookup = (
        stamped.head(train_end)
        .filter(pl.col(TARGET).is_not_null())
        .group_by("_m", "_h")
        .agg(pl.col(TARGET).mean().alias("_c"))
    )
    overall = stamped.head(train_end)[TARGET].mean()
    joined = stamped.join(lookup, on=["_m", "_h"], how="left").with_columns(
        pl.col("_c").fill_null(overall)
    )
    return joined["_c"].to_numpy().astype(float)


def _spread(items: list[str], n: int) -> list[str]:
    if n >= len(items):
        return items
    step = max(len(items) // n, 1)
    return items[::step][:n]
