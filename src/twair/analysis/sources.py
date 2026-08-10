"""M7 — wind patterns during high-concentration hours.

The **conditional bivariate probability function** (Uria-Tellaetxe & Carslaw,
2014) describes the high-concentration pattern already present in the store.

For every combination of wind direction and wind speed, CBPF asks: *given that
the wind was blowing from here at this speed, how often was the concentration
high?*

    CBPF(θ, u) = (samples in that bin above the threshold) / (samples in that bin)

The bivariate part separates whether high values are observed during weaker or
stronger winds at each bearing. A plain wind-direction rose cannot show that
wind-speed pattern.

Three things this module does that a textbook implementation would not:

**A bin with too few observations returns null, not zero.** A direction the
wind almost never comes from would otherwise render as reliably clean — the
brightest possible example of the failure this project exists to avoid. The
minimum count is a parameter and the number of suppressed bins is reported.

**Calm hours are counted and excluded, not silently dropped.** Stagnation is
when pollution accumulates, so those hours matter — but they have no direction
and cannot go on a polar plot. Reporting the calm fraction beside the plot
keeps the reader from reading absence as absence of a problem.

**The output carries a concentration statistic.** The resultant length of the
probability-weighted direction vector measures whether high values concentrate
at one bearing or are spread across bearings, using the same circular machinery
as the aggregates. Along with the peak speed, it describes a wind pattern that
can screen source hypotheses; it does not identify a source's distance, place,
or contribution.

.. warning::

   **What this cannot tell you is where the pollution came from.**

   A bearing is not an origin. CBPF supports statements of the form "high
   hours at this station arrive on strong winds from the north-north-east";
   it does not support "this pollution came from *place X*" or measure its
   distance or contribution. Intervening sources and the fact that air does
   not travel in straight lines all sit between a pattern and an origin, and
   closing that gap is what a trajectory model is for — which is deliberately
   not built here.

   Naming an origin from a bearing alone would be an unsupported inference,
   and in this region a politically loaded one. State the bearing and the
   wind speed; stop there.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

log = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_SPEED_BINS",
    "CBPFResult",
    "cbpf",
    "directional_concentration",
    "run_sources",
]

TARGET = "PM2.5"

# Metres per second. The top bin is open-ended: Taiwan's hourly means rarely
# exceed 8 m/s, and splitting the tail further would put every bin below the
# minimum count.
DEFAULT_SPEED_BINS: tuple[float, ...] = (0.5, 1.5, 2.5, 4.0, 6.0, 8.0)

# A bin needs this many hours before its probability is reported. Twenty gives
# a standard error of about 0.11 on a probability near 0.5 — coarse, but not
# meaningless. Below it the estimate is withheld.
MIN_BIN_COUNT = 20

# Hours at or below this wind speed are treated as calm: the vane cannot
# resolve a direction that is not there.
CALM_THRESHOLD_MS = 0.5

# A station needs this many usable hours before CBPF is attempted at all.
# Separate from `min_count`, which decides whether an individual bin is
# reportable — tying them together meant that raising the per-bin threshold to
# inspect suppression behaviour silently disqualified the whole station.
MIN_STATION_HOURS = 400


@dataclass(frozen=True, slots=True)
class CBPFResult:
    station: str
    pollutant: str
    threshold: float
    """The concentration above which an hour counts as "high"."""
    percentile: float
    grid: pl.DataFrame
    n_hours: int
    n_calm: int
    n_suppressed: int
    """Bins withheld for too few observations."""
    resultant: float
    """0 = high values come from everywhere; 1 = from one bearing only."""
    peak_sector: int | None
    peak_speed: str | None

    @property
    def calm_fraction(self) -> float:
        return self.n_calm / self.n_hours if self.n_hours else float("nan")

    def summary_row(self) -> dict[str, Any]:
        return {
            "station_name": self.station,
            "pollutant": self.pollutant,
            "threshold": self.threshold,
            "percentile": self.percentile,
            "n_hours": self.n_hours,
            "n_calm": self.n_calm,
            "calm_fraction": self.calm_fraction,
            "n_suppressed_bins": self.n_suppressed,
            "resultant": self.resultant,
            "peak_sector": self.peak_sector,
            "peak_speed": self.peak_speed,
        }


def _speed_label(bins: tuple[float, ...], index: int) -> str:
    if index == 0:
        return f"<{bins[0]:g}"
    if index >= len(bins):
        return f"{bins[-1]:g}+"
    return f"{bins[index - 1]:g}-{bins[index]:g}"


def cbpf(
    frame: pl.DataFrame,
    *,
    station: str,
    pollutant: str = TARGET,
    percentile: float = 75.0,
    n_sectors: int = 12,
    speed_bins: tuple[float, ...] = DEFAULT_SPEED_BINS,
    min_count: int = MIN_BIN_COUNT,
) -> CBPFResult | None:
    """Conditional probability of a high hour, by wind direction and speed.

    ``frame`` needs ``station_name``, the pollutant column, ``WS_HR`` and
    ``WD_HR``. Rows missing any of those are dropped before anything is
    counted, so the denominators are honest.
    """
    subset = frame.filter(pl.col("station_name") == station).drop_nulls(
        [pollutant, "WS_HR", "WD_HR"]
    )
    if subset.height < MIN_STATION_HOURS:
        log.info("%s: %d usable hours — too few for CBPF", station, subset.height)
        return None

    n_hours = subset.height
    calm = subset.filter(pl.col("WS_HR") <= CALM_THRESHOLD_MS)
    moving = subset.filter(pl.col("WS_HR") > CALM_THRESHOLD_MS)

    if moving.height < MIN_STATION_HOURS:
        log.info("%s: almost every hour is calm — no directional signal", station)
        return None

    threshold = float(np.percentile(subset[pollutant].to_numpy(), percentile))
    sector_width = 360 / n_sectors

    # `% 360` before binning: 360 and 0 are the same bearing. This module is a
    # short walk from the one where that exact mistake was found.
    binned = moving.with_columns(
        (((pl.col("WD_HR") % 360) / sector_width).floor() * sector_width)
        .cast(pl.Int32)
        .alias("sector"),
        pl.col("WS_HR")
        .cut(
            list(speed_bins),
            labels=[_speed_label(speed_bins, i) for i in range(len(speed_bins) + 1)],
        )
        .alias("speed_bin"),
        (pl.col(pollutant) > threshold).alias("is_high"),
    )

    grid = (
        binned.group_by("sector", "speed_bin")
        .agg(pl.len().alias("n"), pl.col("is_high").sum().alias("n_high"))
        .with_columns(
            # Withheld rather than zero. A bearing the wind rarely comes from
            # would otherwise plot as reliably clean.
            pl.when(pl.col("n") >= min_count)
            .then(pl.col("n_high") / pl.col("n"))
            .otherwise(None)
            .alias("probability"),
            (pl.col("n") >= min_count).alias("reliable"),
        )
        .sort("sector", "speed_bin")
    )

    reported = grid.filter(pl.col("probability").is_not_null())
    peak = reported.sort("probability", descending=True).head(1)

    return CBPFResult(
        station=station,
        pollutant=pollutant,
        threshold=threshold,
        percentile=percentile,
        grid=grid,
        n_hours=n_hours,
        n_calm=calm.height,
        n_suppressed=int((~grid["reliable"]).sum()),
        resultant=directional_concentration(reported),
        peak_sector=int(peak["sector"][0]) if peak.height else None,
        peak_speed=str(peak["speed_bin"][0]) if peak.height else None,
    )


def directional_concentration(grid: pl.DataFrame) -> float:
    """How tightly the high-concentration probability points one way.

    The resultant length of the probability-weighted unit vectors, in [0, 1].
    Near 1 means high values concentrate at a single bearing; near 0 means
    they are spread across bearings. It describes that pattern without
    identifying its origin or contribution.

    This is the same statistic ``circular_resultant_expr`` computes for wind
    direction itself; here the weights are probabilities rather than counts.
    """
    if grid.is_empty():
        return float("nan")

    weights = grid["probability"].to_numpy()
    total = float(weights.sum())
    if total <= 0:
        return float("nan")

    radians = grid["sector"].to_numpy() * (np.pi / 180.0)
    x = float((weights * np.cos(radians)).sum()) / total
    y = float((weights * np.sin(radians)).sum()) / total
    return float(np.hypot(x, y))


def build_wind_frame(
    root: Path | None = None,
    *,
    period: tuple[int, int] = (2006, 2025),
    pollutant: str = TARGET,
    stations: list[str] | None = None,
) -> pl.DataFrame:
    """Hourly pollutant with the wind that was blowing at the time.

    Reads the canonical hourly store rather than the daily aggregates: CBPF is
    a question about hours, and a daily mean wind direction over a day that
    changed direction is exactly the circular-averaging mistake this project
    documents elsewhere.
    """
    from twair.qc.rainfall import usable
    from twair.store.stations import normalise_name_expr
    from twair.store.writer import scan_observations

    start, end = period
    lazy = scan_observations(root).filter(
        pl.col("ts_local").dt.year().is_between(start, end)
        & pl.col("pollutant").is_in([pollutant, "WS_HR", "WD_HR"])
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
    for column in (pollutant, "WS_HR", "WD_HR"):
        if column not in wide.columns:
            wide = wide.with_columns(pl.lit(None, dtype=pl.Float64).alias(column))
    return wide


def run_sources(
    root: Path | None = None,
    *,
    period: tuple[int, int] = (2006, 2025),
    pollutant: str = TARGET,
    stations: list[str] | None = None,
    percentile: float = 75.0,
) -> dict[str, pl.DataFrame]:
    """CBPF for every station with enough hours."""
    frame = build_wind_frame(root, period=period, pollutant=pollutant, stations=stations)
    available = stations or sorted(frame["station_name"].unique().to_list())

    summaries: list[dict[str, Any]] = []
    grids: list[pl.DataFrame] = []

    for station in available:
        result = cbpf(frame, station=station, pollutant=pollutant, percentile=percentile)
        if result is None:
            continue
        summaries.append(result.summary_row())
        grids.append(result.grid.with_columns(pl.lit(station).alias("station_name")))

    if not summaries:
        raise RuntimeError("no station had enough paired wind and concentration hours")

    return {
        "summary": pl.DataFrame(summaries).sort("resultant", descending=True),
        "grid": pl.concat(grids).select(
            "station_name", "sector", "speed_bin", "n", "n_high", "probability", "reliable"
        ),
    }


def write_sources_report(tables: dict[str, pl.DataFrame]) -> dict[str, Path]:
    from twair.paths import outputs_dir

    destination = outputs_dir("m7_sources")
    destination.mkdir(parents=True, exist_ok=True)

    written: dict[str, Path] = {}
    for name, frame in tables.items():
        path = destination / f"{name}.parquet"
        frame.write_parquet(path)
        written[name] = path
    return written
