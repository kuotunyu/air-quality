"""Lagged and rolling features, built so they cannot see the future.

Phase 2 established the fact this module exists to act on: **persistence beats
every explanatory model here** — R² 0.900 against 0.524 — because persistence
uses PM2.5's own last value and those models used none of its history. That is
not a defeat for the models; they were answering a different question. But it
means any forecasting attempt starts from a very high bar, and clearing it
requires giving the model the same history persistence has.

Giving it that history is where forecasting code usually breaks, and it breaks
silently. A forecast for time ``t + h`` made at time ``t`` may use observations
up to and including ``t`` and nothing after. Off by one row and the model reads
its own answer, reports a spectacular score, and is worthless in production.

So every function here is written around one rule:

    A feature named ``lag_k`` at row ``t`` holds the value at ``t - k + 1``,
    where ``lag_1`` is the most recent observation available at forecast time.

and the target is shifted the other way by the horizon. The two shifts are in
opposite directions on purpose — that opposition is the whole safety property,
and ``tests/test_lags.py`` checks it on a series whose values encode their own
timestamps, so a leak is visible as an exact number rather than an improbably
good metric.

**Gaps are not bridged.** The store is full of them, and a lag taken across a
three-day outage would claim the value from before the outage was "the previous
hour". Lags are computed on a complete hourly index per station, so a missing
hour produces a null feature, and a row whose features are null is dropped from
training rather than filled.
"""

from __future__ import annotations

import polars as pl

__all__ = [
    "DEFAULT_LAGS",
    "DEFAULT_WINDOWS",
    "add_lag_features",
    "add_target",
    "lag_feature_names",
]

# Hours back. 1-3 carry the immediate trajectory, 24 the same hour yesterday,
# 168 the same hour last week — both of which matter because emissions follow
# daily and weekly cycles.
DEFAULT_LAGS: tuple[int, ...] = (1, 2, 3, 6, 12, 24, 48, 168)

# Rolling windows, in hours, ending at the most recent available observation.
DEFAULT_WINDOWS: tuple[int, ...] = (3, 12, 24, 72)


# Rates of change to derive, in hours. A delta over k hours is built from
# lag_1 minus lag_(k+1), so it can only exist when that lag does.
DEFAULT_DELTAS: tuple[int, ...] = (1, 3, 24)


def _producible_deltas(lags: tuple[int, ...], deltas: tuple[int, ...]) -> tuple[int, ...]:
    """Which deltas the given lag set can actually support.

    Single source of truth for both the name list and the builder. They drifted
    apart once — `delta3` needs `lag4`, which is not in the default lag set —
    and the contract test caught it, which is what that test is for.
    """
    available = set(lags)
    return tuple(k for k in deltas if (k + 1) in available and 1 in available)


def lag_feature_names(
    column: str,
    *,
    lags: tuple[int, ...] = DEFAULT_LAGS,
    windows: tuple[int, ...] = DEFAULT_WINDOWS,
    deltas: tuple[int, ...] = DEFAULT_DELTAS,
) -> list[str]:
    """Every feature name :func:`add_lag_features` will produce for a column."""
    names = [f"{column}_lag{k}" for k in lags]
    names += [f"{column}_mean{w}" for w in windows]
    names += [f"{column}_max{w}" for w in windows]
    names += [f"{column}_delta{k}" for k in _producible_deltas(lags, deltas)]
    return names


def add_lag_features(
    frame: pl.DataFrame,
    *,
    column: str,
    lags: tuple[int, ...] = DEFAULT_LAGS,
    windows: tuple[int, ...] = DEFAULT_WINDOWS,
    deltas: tuple[int, ...] = DEFAULT_DELTAS,
    station: str = "station_name",
    timestamp: str = "ts_local",
) -> pl.DataFrame:
    """Attach lags, rolling statistics and differences, per station.

    ``lag_1`` is the value at the row itself — the most recent observation a
    forecaster standing at that row would have. Nothing here shifts forward.

    The frame must already be on a complete hourly index per station (see
    :func:`complete_hourly_index`), or a lag will silently step over a gap.
    """
    if column not in frame.columns:
        raise KeyError(f"{column!r} is not in the frame")

    over = pl.col(column).over(station)
    exprs: list[pl.Expr] = []

    for k in lags:
        # shift(k - 1): lag_1 is the current row, lag_2 the one before it.
        exprs.append(over.shift(k - 1).alias(f"{column}_lag{k}"))

    for w in windows:
        exprs.append(over.rolling_mean(window_size=w, min_samples=w).alias(f"{column}_mean{w}"))
        exprs.append(over.rolling_max(window_size=w, min_samples=w).alias(f"{column}_max{w}"))

    out = frame.sort(station, timestamp).with_columns(exprs)

    # Rates of change, computed from the lags rather than from the raw column,
    # so they inherit the same no-look-ahead guarantee.
    changes = [
        (pl.col(f"{column}_lag1") - pl.col(f"{column}_lag{k + 1}")).alias(f"{column}_delta{k}")
        for k in _producible_deltas(lags, deltas)
    ]
    return out.with_columns(changes) if changes else out


def add_target(
    frame: pl.DataFrame,
    *,
    column: str,
    horizon: int,
    station: str = "station_name",
    timestamp: str = "ts_local",
    name: str = "target",
) -> pl.DataFrame:
    """Attach the value ``horizon`` hours *after* each row.

    The opposite direction to :func:`add_lag_features`, which is the point. A
    row then holds features from its own past and a target from its own future,
    and the gap between them is exactly the forecast horizon.
    """
    if horizon < 1:
        raise ValueError(f"horizon must be at least 1 hour, got {horizon}")

    return frame.sort(station, timestamp).with_columns(
        pl.col(column).over(station).shift(-horizon).alias(name)
    )


def complete_hourly_index(
    frame: pl.DataFrame,
    *,
    station: str = "station_name",
    timestamp: str = "ts_local",
) -> pl.DataFrame:
    """Insert a null row for every missing hour, per station.

    Without this a lag walks over gaps: after a three-day outage, ``lag_1``
    would hand the model the value from three days earlier and label it "an
    hour ago". Inserting the gap makes the lag null instead, and a null feature
    removes the row from training rather than quietly poisoning it.
    """
    spans = frame.group_by(station).agg(
        pl.col(timestamp).min().alias("_from"), pl.col(timestamp).max().alias("_to")
    )

    grids = [
        pl.DataFrame(
            {
                station: [row[station]],
            }
        ).join(
            pl.datetime_range(row["_from"], row["_to"], interval="1h", eager=True, time_unit="us")
            .alias(timestamp)
            .to_frame(),
            how="cross",
        )
        for row in spans.iter_rows(named=True)
    ]
    if not grids:
        return frame

    full = pl.concat(grids).with_columns(pl.col(timestamp).cast(frame.schema[timestamp]))
    return full.join(frame, on=[station, timestamp], how="left").sort(station, timestamp)
