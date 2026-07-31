"""Reproduce the two measurements that chose `qc/outliers.py`'s baseline.

The module compares each hour against the median and MAD of its own
(station, year, month, hour-of-day) cell rather than against a centred rolling
window. That is the only statistical choice in it, and its docstring states
three numbers as the reason. Two of those came from a design review rather than
from a run in this repository, and AGENTS.md red line 3 says a number in a
docstring must come from actually running something. This is that something.

    uv run python scripts/compare_outlier_baselines.py

It measures:

1. **A rolling baseline follows a sustained rise.** On an N(20, 4) background
   with one +60 ug/m3 block and a centred 25-hour window, detection is complete
   up to a six-hour episode and then falls off a cliff: 7 hours and longer are
   detected in *no* hour at all, deterministically across three seeds. In the
   middle the rolling median has climbed to the episode level; at the edges the
   window straddles the step and the spread inflates. A method built to find
   episodes cannot see a 36-hour one. The cell baseline returns all 36 as one
   run, which is what `tests/test_outliers.py` pins with a fixture.

2. **A rolling baseline starves on the hours that matter.** A run of rejected
   readings is a run of holes in a 25-hour window but removes only about three
   of a thirty-member cell. Measured on PM2.5 2015 against the agency's own
   three rejection categories, the window can score 57.1% / 4.9% / 59.6% of
   them where the cell scores 99.2% / 74.4% / 96.9% — and the worst-served
   category is the largest one. A validation set the statistic cannot see most
   of is a selection effect on which finding is even visible.

Read-only. Needs the store at data/processed/observations.
"""

import io
import sys

import numpy as np
import polars as pl

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, "src")
from twair.qc.outliers import INVALID_FLAGS, MAD_TO_SIGMA, STATION, TIMESTAMP  # noqa: E402
from twair.qc.rainfall import usable  # noqa: E402
from twair.store.stations import normalise_name_expr  # noqa: E402
from twair.store.writer import scan_observations  # noqa: E402

WINDOW = 25
MIN_WINDOW_VALID = 13
MIN_Z = 3.0

print("=" * 76)
print("CLAIM 1: a centred rolling baseline follows a sustained rise")
print("=" * 76)
print()
print("  synthetic: N(20, 4) background, one +60 ug/m3 block, centred window 25,")
print("  min_samples 13, |z| >= 3 on a rolling median and a rolling IQR.")
print()
print("  episode hours   detected   (seeds 7/8/9)")
for length in (2, 4, 6, 7, 8, 10, 24, 36):
    detected = []
    for seed in (7, 8, 9):
        rng = np.random.default_rng(seed)
        n = 2000
        values = rng.normal(20.0, 4.0, n)
        start = n // 2
        values[start : start + length] += 60.0
        frame = pl.DataFrame({"v": values})
        rolled = frame.with_columns(
            pl.col("v")
            .rolling_median(window_size=WINDOW, center=True, min_samples=MIN_WINDOW_VALID)
            .alias("med"),
            pl.col("v")
            .rolling_quantile(0.75, window_size=WINDOW, center=True, min_samples=MIN_WINDOW_VALID)
            .alias("q3"),
            pl.col("v")
            .rolling_quantile(0.25, window_size=WINDOW, center=True, min_samples=MIN_WINDOW_VALID)
            .alias("q1"),
        ).with_columns(((pl.col("q3") - pl.col("q1")) / 1.349).alias("sigma"))
        rolled = rolled.with_columns(
            pl.when(pl.col("sigma") > 0)
            .then((pl.col("v") - pl.col("med")) / pl.col("sigma"))
            .otherwise(None)
            .alias("z")
        )
        block = rolled[start : start + length]
        detected.append(int((block["z"].abs() >= MIN_Z).sum()))
    shown = ", ".join(str(d) for d in detected)
    print(f"  {length:13d}   [{shown}]")

print()
print("=" * 76)
print("CLAIM 2: how much of the agency's rejections each baseline can score")
print("=" * 76)

POLLUTANT = "PM2.5"
YEAR = 2015

base = (
    scan_observations()
    .filter(pl.col("year") == YEAR)
    .filter(pl.col("pollutant") == POLLUTANT)
    .select(
        normalise_name_expr(),
        TIMESTAMP,
        pl.col("value").cast(pl.Float64),
        pl.col("flag").cast(pl.Utf8),
        usable().alias("is_usable"),
    )
    .collect()
    .sort(STATION, TIMESTAMP)
)

# The rolling baseline is built from usable readings only, exactly as the cell
# baseline is, and evaluated at the position of every row including rejected
# ones — which is what starves it: a run of rejections is a run of nulls in the
# window.
rolling = base.with_columns(
    pl.when(pl.col("is_usable")).then(pl.col("value")).otherwise(None).alias("_ok")
).with_columns(
    pl.col("_ok")
    .rolling_median(window_size=WINDOW, center=True, min_samples=MIN_WINDOW_VALID)
    .over(STATION)
    .alias("med"),
    pl.col("_ok")
    .rolling_quantile(0.75, window_size=WINDOW, center=True, min_samples=MIN_WINDOW_VALID)
    .over(STATION)
    .alias("q3"),
    pl.col("_ok")
    .rolling_quantile(0.25, window_size=WINDOW, center=True, min_samples=MIN_WINDOW_VALID)
    .over(STATION)
    .alias("q1"),
)
rolling = rolling.with_columns(((pl.col("q3") - pl.col("q1")) / 1.349).alias("sigma")).with_columns(
    pl.when((pl.col("sigma") > 0) & pl.col("value").is_not_null())
    .then((pl.col("value") - pl.col("med")) / pl.col("sigma"))
    .otherwise(None)
    .alias("z_rolling")
)

# The cell baseline, as the module builds it.
cells = (
    base.filter(pl.col("is_usable"))
    .with_columns(
        pl.col(TIMESTAMP).dt.year().cast(pl.Int32).alias("obs_year"),
        pl.col(TIMESTAMP).dt.month().cast(pl.Int8).alias("obs_month"),
        pl.col(TIMESTAMP).dt.hour().cast(pl.Int8).alias("obs_hour"),
    )
    .with_columns(
        pl.col("value").median().over([STATION, "obs_year", "obs_month", "obs_hour"]).alias("med"),
        pl.len().over([STATION, "obs_year", "obs_month", "obs_hour"]).alias("n_cell"),
    )
    .with_columns((pl.col("value") - pl.col("med")).abs().alias("_ad"))
    .with_columns(
        pl.col("_ad").median().over([STATION, "obs_year", "obs_month", "obs_hour"]).alias("scale")
    )
    .unique(subset=[STATION, "obs_year", "obs_month", "obs_hour"])
    .select(STATION, "obs_year", "obs_month", "obs_hour", "med", "scale", "n_cell")
)

rejected = (
    rolling.filter(pl.col("flag").is_in(INVALID_FLAGS))
    .with_columns(
        pl.col(TIMESTAMP).dt.year().cast(pl.Int32).alias("obs_year"),
        pl.col(TIMESTAMP).dt.month().cast(pl.Int8).alias("obs_month"),
        pl.col(TIMESTAMP).dt.hour().cast(pl.Int8).alias("obs_hour"),
    )
    .join(cells, on=[STATION, "obs_year", "obs_month", "obs_hour"], how="left")
    .with_columns(
        pl.when(pl.col("value").is_not_null() & (pl.col("n_cell") >= 20) & (pl.col("scale") > 0))
        .then(MAD_TO_SIGMA * (pl.col("value") - pl.col("med_right")) / pl.col("scale"))
        .otherwise(None)
        .alias("z_cell")
    )
)

print()
print(f"  {POLLUTANT} {YEAR}, per agency rejection category:")
print()
print("  flag                          rows   rolling scoreable      cell scoreable")
summary = (
    rejected.group_by("flag")
    .agg(
        pl.len().alias("rows"),
        pl.col("z_rolling").is_not_null().sum().alias("rolling"),
        pl.col("z_cell").is_not_null().sum().alias("cell"),
    )
    .sort("flag")
)
for row in summary.iter_rows(named=True):
    flag = f"{row['flag']}"
    print(
        f"  {flag:24s} {row['rows']:7,}   "
        f"{row['rolling']:7,} ({row['rolling'] / row['rows']:6.1%})   "
        f"{row['cell']:7,} ({row['cell'] / row['rows']:6.1%})"
    )

accepted = rolling.filter(pl.col("is_usable"))
scoreable = int(accepted["z_rolling"].is_not_null().sum())
print()
print(
    f"  agency-accepted hours: {accepted.height:,}; "
    f"rolling scoreable {scoreable:,} ({scoreable / accepted.height:.1%})"
)
