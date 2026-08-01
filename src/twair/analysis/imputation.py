"""D11: what the one sentence about missing values was worth.

The 2018 project wrote 「將有遺漏值之資料以鄰近測站之資料代替」 and moved on.
No count of how many values that was, no estimate of how wrong they were, no
statement of which station donated to which. Criticising the sentence is free.
This module prices it.

Three questions, in the order they have to be asked:

1. **How much is missing, and in what shape?** A fill rate is meaningless
   without the gap-length distribution behind it, because the strategies differ
   almost entirely in how they behave as a gap gets longer.
2. **How wrong are the invented values?** Hide readings that exist, ask each
   strategy to reconstruct them, compare against what was really there. This is
   the measurement the original could have made with its own data and did not.
A third question — *does any of it change the conclusion?* — is deliberately
**not** answered here, and the reason is worth recording because the attempt
looked like it worked.

Fitting the same model on each filled frame gives four R² values that differ,
and it is tempting to read them off. They are not comparable. Filling only
changes rows the baseline could not use, so each strategy is scored on a
different test set — and the rows filling adds are exactly the rows `none`
has to drop. Measured on this panel the baseline can already use 95.2% of
station-hours, the neighbour strategy 95.9%, and yet it is the neighbour
strategy that gains the most R², so the obvious "more rows" explanation does
not survive checking either.

A well-posed version needs the evaluation set fixed across strategies while the
training set varies, which the shared backtest harness does not currently
support. A confounded number is worse than no number, so there is no number.

**The masking is the part that decides whether the comparison is honest.**
Real missingness is clustered — an instrument fails for hours or days, not for
one hour at a time. Hiding randomly chosen single cells would hand
interpolation a series of one-hour gaps, which it reconstructs almost
perfectly, and the resulting table would say linear interpolation is excellent.
So the runs hidden here are drawn from the gap-length distribution measured in
the same data, and the scores are reported per gap length as well as pooled.
A pooled number alone would hide exactly the effect being measured.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import polars as pl

from twair.paths import outputs_dir
from twair.qc.gapfill import STRATEGIES, Strategy, fill, imputed_column
from twair.qc.rainfall import usable
from twair.scalars import as_float
from twair.store.stations import normalise_name_expr
from twair.store.writer import scan_observations

log = logging.getLogger(__name__)

__all__ = [
    "MaskedPanel",
    "gap_runs",
    "load_panel",
    "mask_like_reality",
    "run_imputation",
    "score_strategies",
]

STATION = "station_name"
TIMESTAMP = "ts_local"
TARGET = "PM2.5"

# The original's own window, so the comparison is on its data rather than on a
# period chosen to flatter one of the answers.
PERIOD = (2010, 2017)

# Measurands carried through the panel. The same set M2 loads, so a frame built
# here can be handed straight to the driver comparison.
PANEL_POLLUTANTS = (
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


def load_panel(
    *,
    period: tuple[int, int] = PERIOD,
    stations: list[str] | None = None,
    root: Path | None = None,
) -> pl.DataFrame:
    """Wide station-hour frame with the nulls **kept**.

    Deliberately not ``drivers.build_modelling_frame``: that drops rows where
    the target is null, which is precisely the population this module studies.
    The hourly index is completed so an absent row and a null row are the same
    thing to everything downstream — a gap the archive did not record is still
    a gap.
    """
    from twair.features.lags import complete_hourly_index

    start, end = period
    long = scan_observations(root).filter(
        pl.col(TIMESTAMP).dt.year().is_between(start, end)
        & pl.col("pollutant").is_in(list(PANEL_POLLUTANTS))
        & usable()
    )
    if stations:
        long = long.filter(normalise_name_expr().is_in(stations))

    tidy = long.select(
        normalise_name_expr(),
        pl.col("pollutant").cast(pl.Utf8),
        TIMESTAMP,
        pl.col("value").cast(pl.Float64),
    ).collect()
    if tidy.is_empty():
        return tidy

    wide = tidy.pivot(
        on="pollutant", index=[STATION, TIMESTAMP], values="value", aggregate_function="first"
    )
    for column in PANEL_POLLUTANTS:
        if column not in wide.columns:
            wide = wide.with_columns(pl.lit(None, dtype=pl.Float64).alias(column))

    return complete_hourly_index(wide).sort(STATION, TIMESTAMP)


def gap_runs(frame: pl.DataFrame, column: str = TARGET) -> pl.DataFrame:
    """Length of every maximal run of missing values, per station.

    This is the shape a fill rate needs beside it. A strategy that fills 40% of
    missing cells has done something very different depending on whether those
    cells are 40% of many one-hour gaps or the first hours of a few outages.
    """
    is_null = pl.col(column).is_null()
    run_id = (is_null != is_null.shift(1).fill_null(value=False)).cum_sum().over(STATION)

    return (
        frame.sort(STATION, TIMESTAMP)
        .with_columns(is_null.alias("_null"), run_id.alias("_run"))
        .filter(pl.col("_null"))
        .group_by(STATION, "_run")
        .agg(pl.len().alias("length"), pl.col(TIMESTAMP).min().alias("started"))
        .drop("_run")
        .sort(STATION, "started")
    )


def bucket_expr(column: str = "length") -> pl.Expr:
    """Label a gap length with the bucket it falls in.

    The 3-hour boundary is not arbitrary: it is ``max_gap_hours`` from
    `conf/qc.yaml`, the point past which interpolation declines to answer. A
    bucket straddling it would average the gaps one strategy handles with the
    gaps it refuses, and the whole comparison turns on that line.
    """
    length = pl.col(column)
    return (
        pl.when(length <= 1)
        .then(pl.lit("1h"))
        .when(length <= 3)
        .then(pl.lit("2-3h"))
        .when(length <= 12)
        .then(pl.lit("4-12h"))
        .when(length <= 48)
        .then(pl.lit("13-48h"))
        .otherwise(pl.lit(">48h"))
        .alias("gap_bucket")
    )


@dataclass(frozen=True, slots=True)
class MaskedPanel:
    """A panel with known values hidden, plus the truth to score against."""

    frame: pl.DataFrame
    truth: pl.DataFrame
    """One row per hidden cell: station, timestamp, true value, gap length."""

    @property
    def n_hidden(self) -> int:
        return self.truth.height


def mask_like_reality(
    frame: pl.DataFrame,
    *,
    column: str = TARGET,
    fraction: float = 0.05,
    seed: int = 0,
    lengths: list[int] | None = None,
) -> MaskedPanel:
    """Hide runs of real readings, with the gap lengths the data actually has.

    ``lengths`` defaults to the observed distribution for this column. Drawing
    from it rather than hiding isolated cells is what stops the evaluation from
    handing interpolation a problem it cannot fail: a one-hour gap between two
    real readings is nearly always reconstructible, and real gaps are mostly
    not one hour.

    A hidden run must be fully known beforehand, or there is nothing to score
    against; runs that cannot be placed are simply not placed, and the achieved
    count is reported rather than the requested one.

    ``gap_length`` records the length of the run that was *hidden*. Where a
    hidden run happens to abut missingness that was already there, the gap a
    strategy actually faces is longer than the label — so the per-bucket scores
    are, if anything, pessimistic about the shorter buckets rather than
    flattering.
    """
    if not 0 < fraction < 1:
        raise ValueError(f"fraction must be in (0, 1), got {fraction}")

    frame = frame.sort(STATION, TIMESTAMP)
    if lengths is None:
        observed = gap_runs(frame, column)["length"].to_list()
        lengths = observed or [1]

    rng = np.random.default_rng(seed)
    values = frame[column].to_numpy()
    known = ~np.isnan(values)
    target_cells = int(known.sum() * fraction)
    if target_cells < 1:
        raise ValueError("fraction is too small to hide a single reading")

    stations = frame[STATION].to_numpy()
    # Run boundaries: a hidden window must not straddle two stations.
    starts = np.flatnonzero(np.r_[True, stations[1:] != stations[:-1]])
    ends = np.r_[starts[1:], len(stations)]
    bounds = dict(zip(starts.tolist(), ends.tolist(), strict=True))

    hidden = np.zeros(len(values), dtype=bool)
    # Length recorded **positionally**, not in acceptance order. Collecting the
    # lengths into a list and zipping them against `flatnonzero(hidden)` looks
    # equivalent and is not: the list is in the order runs were accepted, the
    # index is in row order, so every label lands on the wrong cell as soon as
    # the second run is placed before the first in the frame. That shuffle is
    # invisible in the pooled score and only shows up per gap length — where it
    # had interpolation reconstructing 48-hour gaps it never touched.
    run_length = np.zeros(len(values), dtype=np.int64)
    attempts = 0
    max_attempts = target_cells * 40

    while hidden.sum() < target_cells and attempts < max_attempts:
        attempts += 1
        length = int(rng.choice(lengths))
        block_start = int(rng.choice(starts))
        block_end = bounds[block_start]
        if block_end - block_start <= length:
            continue
        offset = int(rng.integers(block_start, block_end - length))
        window = slice(offset, offset + length)
        if not known[window].all() or hidden[window].any():
            continue
        hidden[window] = True
        run_length[window] = length

    if not hidden.any():  # pragma: no cover - only reachable on a degenerate panel
        raise RuntimeError("could not hide any run; the panel has no complete windows")

    index = np.flatnonzero(hidden)
    truth = pl.DataFrame(
        {
            STATION: frame[STATION].gather(index),
            TIMESTAMP: frame[TIMESTAMP].gather(index),
            "true_value": values[index],
            "gap_length": run_length[index],
        }
    )

    masked = frame.with_columns(
        pl.when(pl.Series("_hide", hidden)).then(None).otherwise(pl.col(column)).alias(column)
    )
    n_runs = int(np.count_nonzero(hidden & ~np.r_[False, hidden[:-1]]))
    log.info(
        "hid %d of %d known %s readings (%.1f%%) in %d runs",
        truth.height,
        int(known.sum()),
        column,
        100 * truth.height / max(int(known.sum()), 1),
        n_runs,
    )
    return MaskedPanel(frame=masked, truth=truth)


def score_strategies(
    panel: MaskedPanel,
    *,
    column: str = TARGET,
    columns: list[str] | None = None,
    strategies: tuple[Strategy, ...] = STRATEGIES,
) -> pl.DataFrame:
    """Reconstruction error per strategy, pooled and by gap length."""
    fill_columns = columns or [c for c in PANEL_POLLUTANTS if c in panel.frame.columns]
    rows: list[dict[str, object]] = []

    for strategy in strategies:
        filled, report = fill(panel.frame, columns=fill_columns, strategy=strategy)
        scored = panel.truth.join(
            filled.select(STATION, TIMESTAMP, column, imputed_column(column)),
            on=[STATION, TIMESTAMP],
            how="left",
        ).with_columns(bucket_expr("gap_length"))

        recovered = scored.filter(pl.col(imputed_column(column)))
        base = {
            "strategy": strategy,
            "hidden": panel.n_hidden,
            "recovered": recovered.height,
            "recovery_rate": recovered.height / panel.n_hidden if panel.n_hidden else None,
            "fill_rate_overall": report.fill_rate,
        }

        rows.append({**base, "gap_bucket": "all", **_errors(recovered, column)})
        for bucket in recovered["gap_bucket"].unique().sort().to_list():
            part = recovered.filter(pl.col("gap_bucket") == bucket)
            rows.append(
                {
                    **base,
                    "gap_bucket": bucket,
                    "recovered": part.height,
                    "recovery_rate": None,
                    **_errors(part, column),
                }
            )

    return pl.DataFrame(rows)


def _errors(scored: pl.DataFrame, column: str) -> dict[str, float | None]:
    """MAE, RMSE and bias. Bias is kept separately because a strategy that is
    wrong in one direction is a different problem from one that is merely
    imprecise, and a mean absolute error cannot tell them apart."""
    if scored.is_empty():
        return {"mae": None, "rmse": None, "bias": None, "n": 0}
    error = scored[column] - scored["true_value"]
    # `or 0.0` would turn a genuinely absent aggregate into a reported zero,
    # which is the failure this project documents; `as_float` refuses instead,
    # and the empty case is already handled above.
    return {
        "mae": as_float(error.abs().mean(), what="mae"),
        "rmse": as_float((error**2).mean(), what="mean squared error") ** 0.5,
        "bias": as_float(error.mean(), what="bias"),
        "n": scored.height,
    }


def spread_sample(stations: list[str], n: int) -> list[str]:
    """An evenly spaced slice of the sorted station list.

    The same device M2 uses for leave-one-station-out (`run_all_drivers`): the
    expensive comparisons do not need all 78 stations to answer the question,
    and a spread beats the first N, which would be alphabetical and therefore
    regional.
    """
    if n >= len(stations):
        return stations
    step = max(len(stations) // n, 1)
    return stations[::step][:n]


def run_imputation(
    *,
    period: tuple[int, int] = PERIOD,
    stations: list[str] | None = None,
    sample: int = 12,
    fraction: float = 0.05,
    seed: int = 0,
    root: Path | None = None,
) -> dict[str, Path]:
    """Run the whole D11 comparison and persist its evidence.

    The gap distribution is measured over **every** station in the window,
    because it costs nothing and describes the problem. The reconstruction and
    downstream comparisons run on a spread of ``sample`` stations, because
    neighbour search and iterative imputation are quadratic and per-station
    respectively. Which stations were used is written into the output rather
    than left to be inferred.
    """
    out = outputs_dir("m11_imputation")
    out.mkdir(parents=True, exist_ok=True)

    panel = load_panel(period=period, stations=stations, root=root)
    if panel.is_empty():
        raise RuntimeError("no observations in the requested period; is the store built?")

    runs = gap_runs(panel).with_columns(bucket_expr())
    distribution = (
        runs.group_by("gap_bucket")
        .agg(pl.len().alias("gaps"), pl.col("length").sum().alias("hours"))
        .with_columns(
            (pl.col("hours") / pl.col("hours").sum()).alias("share_of_missing_hours"),
            (pl.col("gaps") / pl.col("gaps").sum()).alias("share_of_gaps"),
            pl.lit(panel[STATION].n_unique()).alias("stations"),
            pl.lit(f"{period[0]}-{period[1]}").alias("period"),
        )
        .sort("hours", descending=True)
    )
    distribution.write_parquet(out / "gap_distribution.parquet")
    log.info("gap distribution over %d stations", panel[STATION].n_unique())

    chosen = spread_sample(sorted(panel[STATION].unique().to_list()), sample)
    subset = panel.filter(pl.col(STATION).is_in(chosen))
    log.info("comparisons on %d station(s): %s", len(chosen), ", ".join(chosen))

    masked = mask_like_reality(subset, fraction=fraction, seed=seed)
    scores = score_strategies(masked).with_columns(
        pl.lit(len(chosen)).alias("stations"),
        pl.lit(", ".join(chosen)).alias("station_sample"),
        pl.lit(f"{period[0]}-{period[1]}").alias("period"),
    )
    scores.write_parquet(out / "reconstruction.parquet")

    return {
        "gap_distribution": out / "gap_distribution.parquet",
        "reconstruction": out / "reconstruction.parquet",
    }
