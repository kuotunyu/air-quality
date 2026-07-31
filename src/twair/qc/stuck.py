"""Find readings that stopped moving.

`qc/outliers.py` scores an hour against the spread of its own
(station, year, month, hour-of-day) cell. That statistic is blind to a sensor
that has failed by freezing: a repeated reading sits at its own cell median, so
its robust z is approximately zero. Measured on the agency's own rejections,
that blind spot is not small — **42.4% of ``instrument_check_invalid`` readings
are identical to each of the previous two hours** (76.7% for PM10 alone) against
1.9% of accepted ones. The largest shape in the agency's rejections is the one
shape the outlier module cannot see, which is why this one exists beside it.

Where the threshold comes from
------------------------------
Not from a guess. A repeated reading is not by itself a fault — rainfall is zero
for days, a low concentration quantised to one decimal repeats by arithmetic,
wind speed is zero when the air is still — so the question is at what length a
repeat stops being ordinary, and the agency's own flag is a labelled answer.
Measured over 1994-2017, six measurands, 77,047,859 maximal identical-value
runs, the share of a run's hours that the agency rejected:

    run length   away from zero      at exactly zero
    1 hour            2.2%                67.2%
    2                 1.8%                48.8%
    3                 2.1%                34.4%
    4-5               2.9%                27.5%
    6-8               6.4%                21.7%
    9-12             13.5%                28.1%
    13-24            31.2%                50.6%
    25-72            51.9%                63.8%
    73+              60.5%                62.5%

Away from zero that is flat to five hours and then climbs monotonically to 60%.
The default ``min_length_hours`` is the length at which it starts climbing, and
``calibration.parquet`` ships the whole curve so the choice can be checked
rather than believed.

Zero is a different phenomenon and is reported apart from the rest: an isolated
exact zero is rejected 67% of the time and the rate *falls* as the run
lengthens, which is the opposite direction. A long run of zeros is more often a
real quiet period than a fault, and a lone zero is more often a fault than a
measurement.

What it found
-------------
Over 1982-2025 and six measurands, 123,114,626 readings gave 148,397 flatlines
of six hours or more, and **the agency accepted 68,970 of them outright** —
46.5%. Setting aside the ones at exactly zero and the eight negative ones,
**55,308 mid-range flatlines passed every official check**. Two, verified
against the raw record rather than inferred from the run table:

* 陽明 PM10, from 2000-11-01 21:00, **327 hours** — thirteen and a half days —
  at exactly 12 µg/m³. The six hours before it read 12, 12, 12, 9, 3, 11: the
  instrument was reporting normally and then stopped. Every hour of the 327 is
  flagged ``valid``.
* 基隆 PM10, from 1986-08-19 12:00, **145 hours** at exactly 131 µg/m³, after a
  morning that read 224, 193, 146, 180, 205, 130. A high value frozen for six
  days, flagged ``valid`` throughout, and it enters every daily and monthly mean
  computed over that window.

The second is the one that matters for the rest of the project: a frozen
reading is not a gap, so nothing about coverage or completeness can see it, and
it is not extreme, so `qc/outliers.py` cannot see it either.

What it may not conclude
------------------------
* A long run is not a diagnosed frozen sensor. It is a reading that did not
  change for N hours, which is what was measured. Whether the instrument or the
  air was still is not in this table.
* The rate depends strongly on the measurand's own resolution. Measured at six
  hours and away from zero, the agency rejected 40.2% of NO2 runs and 2.6% of
  CO runs — CO is recorded to 0.1 ppm on a small range, so it repeats by
  arithmetic. A count compared across measurands is comparing quantisation.
* A negative flatline is not a stuck sensor either. ``-99`` is a documented
  legacy no-data marker (see docs/archive-formats.md) and appears here as a
  228-hour run at 永和 in 1994; it is reported with its value visible so it can
  be told apart rather than silently counted.
* From 2018 there is nothing to check against. The modern format replaces an
  invalidated reading with the flag, so a rejected hour carries no number and
  cannot be tested for repetition. ``agency_rejected`` is null for those years,
  not zero.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import polars as pl

from twair.config import load_conf
from twair.paths import outputs_dir
from twair.qc.outliers import INVALID_FLAGS
from twair.scalars import as_int
from twair.store.stations import normalise_name_expr
from twair.store.writer import scan_observations

log = logging.getLogger(__name__)

__all__ = [
    "StuckReport",
    "calibration_curve",
    "run_stuck",
    "stuck_conf",
    "stuck_runs",
    "write_stuck_report",
]

STATION = "station_name"
TIMESTAMP = "ts_local"
KEY = (STATION, "pollutant")

#: The buckets the calibration curve is reported over. Chosen to be readable
#: rather than equal-width: the interesting structure is all below 24 hours.
LENGTH_BUCKETS: tuple[tuple[int, int, str], ...] = (
    (1, 1, "01"),
    (2, 2, "02"),
    (3, 3, "03"),
    (4, 5, "04-05"),
    (6, 8, "06-08"),
    (9, 12, "09-12"),
    (13, 24, "13-24"),
    (25, 72, "25-72"),
    (73, 10**9, "73+"),
)


def stuck_conf(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """The ``stuck`` block of conf/qc.yaml."""
    conf = config if config is not None else load_conf("qc")
    block = conf.get("stuck", {})
    if not isinstance(block, dict):  # pragma: no cover - a malformed config is a bug
        raise ValueError("conf/qc.yaml: `stuck` must be a mapping")
    return block


@dataclass(frozen=True, slots=True)
class StuckReport:
    """What one pass measured."""

    pollutants: tuple[str, ...]
    years: tuple[int, int]
    rows: int = 0
    runs: int = 0
    flagged_runs: dict[str, int] = field(default_factory=dict)
    flagged_hours: dict[str, int] = field(default_factory=dict)
    accepted_runs: dict[str, int] = field(default_factory=dict)
    longest: dict[str, int] = field(default_factory=dict)
    params: dict[str, Any] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    @property
    def total_flagged(self) -> int:
        return sum(self.flagged_runs.values())

    @property
    def total_accepted(self) -> int:
        """Runs long enough to flag that the agency did not reject at all.

        This is the module's reason to exist. A long flatline the agency already
        caught is a confirmation; one it did not is a finding.
        """
        return sum(self.accepted_runs.values())

    def summary(self) -> str:
        share = f"{self.total_accepted / self.total_flagged:.1%}" if self.total_flagged else "—"
        return (
            f"{len(self.pollutants)} measurand(s), {self.years[0]}-{self.years[1]} | "
            f"{self.rows:,} readings | {self.total_flagged:,} flatlines | "
            f"{self.total_accepted:,} of them wholly accepted by the agency ({share})"
        )


def stuck_runs(frame: pl.DataFrame, *, min_length_hours: int) -> pl.DataFrame:
    """Maximal runs of an unchanging reading, at or above ``min_length_hours``.

    A run continues only while the value is unchanged **and** the step to the
    next reading is exactly one hour. A gap ends it: two identical readings
    either side of a three-day outage are two readings, not a sensor that failed
    to move, and joining them would manufacture the very thing being looked for.

    ``agency_rejected`` is the share of the run's hours the agency had already
    invalidated, and it is **null for the modern generation**. From 2018 the
    format replaces an invalidated reading with the flag itself, so a rejected
    hour carries no number, never enters a run, and every modern run would come
    out at exactly 0.0 — "the agency accepted all of this" and "no rejected hour
    could have been seen" rendering as the same number. The generation is a
    column of the store precisely because the two eras are not comparable.
    """
    if frame.is_empty():
        return _empty_runs()

    ordered = frame.sort(*KEY, TIMESTAMP)
    unchanged = (pl.col("value") == pl.col("value").shift(1).over(KEY)).fill_null(value=False)
    one_hour = (pl.col(TIMESTAMP).diff().over(KEY) == pl.duration(hours=1)).fill_null(value=False)
    ordered = ordered.with_columns((~(unchanged & one_hour)).cum_sum().over(KEY).alias("_run"))

    rejected = pl.col("flag").is_in(INVALID_FLAGS)
    return (
        ordered.group_by([*KEY, "_run"])
        .agg(
            pl.col("value").first().alias("value"),
            pl.len().cast(pl.UInt32).alias("length_hours"),
            pl.col(TIMESTAMP).min().alias("t_start"),
            pl.col(TIMESTAMP).max().alias("t_end"),
            rejected.mean().alias("agency_rejected"),
            (pl.col("generation") == "modern_csv_utf8").all().alias("_modern"),
            pl.col(TIMESTAMP).min().dt.year().cast(pl.Int32).alias("obs_year"),
        )
        .filter(pl.col("length_hours") >= min_length_hours)
        .with_columns(
            (pl.col("value") == 0).alias("at_zero"),
            (pl.col("value") < 0).alias("negative"),
            # Not measurable in the modern generation: a rejected reading
            # there has no number, so it is absent from this frame entirely and
            # a run of accepted hours is all that could ever remain.
            pl.when(pl.col("_modern"))
            .then(None)
            .otherwise(pl.col("agency_rejected"))
            .alias("agency_rejected"),
        )
        .drop("_run", "_modern")
        .sort("length_hours", descending=True)
    )


def _empty_runs() -> pl.DataFrame:
    return pl.DataFrame(
        schema={
            STATION: pl.Utf8,
            "pollutant": pl.Utf8,
            "value": pl.Float64,
            "length_hours": pl.UInt32,
            "t_start": pl.Datetime("us"),
            "t_end": pl.Datetime("us"),
            "agency_rejected": pl.Float64,
            "obs_year": pl.Int32,
            "at_zero": pl.Boolean,
            "negative": pl.Boolean,
        }
    )


def calibration_curve(frame: pl.DataFrame) -> pl.DataFrame:
    """Agency rejection rate against run length — the evidence for the threshold.

    Built from **every** run including length 1, not only the flagged ones, so
    the flat part of the curve below the threshold is visible. A threshold whose
    justification is only computed above itself is not a justification.
    """
    if frame.is_empty():
        return pl.DataFrame(
            schema={
                "bucket": pl.Utf8,
                "at_zero": pl.Boolean,
                "runs": pl.UInt32,
                "hours": pl.UInt32,
                "agency_rejected": pl.Float64,
            }
        )

    bucket = pl.lit(LENGTH_BUCKETS[-1][2])
    for low, high, label in reversed(LENGTH_BUCKETS[:-1]):
        bucket = (
            pl.when(pl.col("length_hours").is_between(low, high))
            .then(pl.lit(label))
            .otherwise(bucket)
        )
    return (
        frame.with_columns(bucket.alias("bucket"))
        .group_by("bucket", "at_zero")
        .agg(
            pl.len().cast(pl.UInt32).alias("runs"),
            pl.col("length_hours").sum().cast(pl.UInt32).alias("hours"),
            pl.col("agency_rejected").mean().alias("agency_rejected"),
        )
        .sort("bucket", "at_zero")
    )


def run_stuck(
    *,
    years: tuple[int, int] = (1982, 2025),
    pollutants: list[str] | None = None,
    root: Path | None = None,
    config: dict[str, Any] | None = None,
) -> tuple[dict[str, pl.DataFrame], StuckReport]:
    """Scan for flatlines and return the tables plus what was measured."""
    block = stuck_conf(config)
    min_length = int(block["min_length_hours"])
    requested = list(pollutants or block.get("pollutants", []))
    if not requested:
        raise ValueError("conf/qc.yaml: `stuck.pollutants` is empty and none were requested")

    frames: list[pl.DataFrame] = []
    curves: list[pl.DataFrame] = []
    rows = 0
    runs_total = 0
    flagged_runs: dict[str, int] = {}
    flagged_hours: dict[str, int] = {}
    accepted_runs: dict[str, int] = {}
    longest: dict[str, int] = {}
    notes: list[str] = []

    for pollutant in requested:
        frame = (
            scan_observations(root)
            .filter(pl.col("year").is_between(years[0], years[1]))
            .filter((pl.col("pollutant") == pollutant) & pl.col("value").is_not_null())
            .select(
                normalise_name_expr(),
                TIMESTAMP,
                pl.col("pollutant").cast(pl.Utf8),
                pl.col("value").cast(pl.Float64),
                pl.col("flag").cast(pl.Utf8),
                pl.col("generation").cast(pl.Utf8),
            )
            .collect()
        )
        if frame.is_empty():
            notes.append(f"{pollutant}: no readings with a value in {years[0]}-{years[1]}")
            continue

        rows += frame.height
        every = stuck_runs(frame, min_length_hours=1)
        runs_total += every.height
        curves.append(calibration_curve(every).with_columns(pl.lit(pollutant).alias("pollutant")))

        flagged = every.filter(pl.col("length_hours") >= min_length)
        flagged_runs[pollutant] = flagged.height
        flagged_hours[pollutant] = as_int(
            flagged["length_hours"].sum(), what=f"{pollutant} flagged hours"
        )
        longest[pollutant] = as_int(every["length_hours"].max(), what=f"{pollutant} longest run")
        # `== 0.0` and not `!= null`: a modern run's null is "we could not
        # look", which is not the finding this counter is about.
        accepted_runs[pollutant] = flagged.filter(pl.col("agency_rejected") == 0.0).height
        if not flagged.is_empty():
            frames.append(flagged)

    tables = {
        "flatlines": pl.concat(frames, how="vertical_relaxed") if frames else _empty_runs(),
        "calibration": (
            pl.concat(curves, how="vertical_relaxed")
            if curves
            else calibration_curve(_empty_runs())
        ),
        "parameters": pl.DataFrame(
            {
                "parameter": ["pollutants", "years", "min_length_hours"],
                "value": [",".join(requested), f"{years[0]}:{years[1]}", str(min_length)],
            }
        ),
    }
    notes.append(
        "a long flatline the agency already rejected is a confirmation; one it accepted "
        "is the finding this module exists for"
    )
    tables["notes"] = pl.DataFrame({"note": notes}, schema={"note": pl.Utf8})

    report = StuckReport(
        pollutants=tuple(requested),
        years=years,
        rows=rows,
        runs=runs_total,
        flagged_runs=flagged_runs,
        flagged_hours=flagged_hours,
        accepted_runs=accepted_runs,
        longest=longest,
        params={"min_length_hours": min_length},
        notes=tuple(notes),
    )
    log.info("%s", report.summary())
    return tables, report


#: Written every run, empty or not — the lesson `qc/outliers.py` learned when a
#: skipped table left a previous run's file beside the new ones.
OUTPUT_TABLES: tuple[str, ...] = ("flatlines", "calibration", "parameters", "notes")


def write_stuck_report(tables: dict[str, pl.DataFrame]) -> dict[str, Path]:
    """Persist one parquet per table under ``data/outputs/qc_stuck/``."""
    destination = outputs_dir("qc_stuck")
    destination.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    for name in OUTPUT_TABLES:
        frame = tables.get(name, pl.DataFrame())
        path = destination / f"{name}.parquet"
        frame.write_parquet(path, compression="zstd")
        written[name] = path
    return written
