"""What is each of the agency's three checks actually rejecting?

`qc/outliers.py` measures how often this project's robust-z agrees with the
agency's own invalidation flags, and warns in its docstring that agreement with
`program_check_invalid` (程式檢核) may be agreement between two automated
threshold rules applied to the same numbers. It then says the only document that
could settle it — the annual 品保查核報告 — has not been obtained, and stops.

That document is not the only way to ask. If a check is a threshold rule, the
readings it rejects carry its fingerprint: they pile up outside a boundary. If
it is a hardware test, they carry a different one — impossible values, exact
zeros, a sensor that has stopped moving. The readings are in the store. Only the
legacy era retains a number behind a rejection, so this covers 1982-2017 and the
six measurands `twair qc outliers` scans.

    uv run python scripts/check_agency_flags.py

Three things it found, all of which are in `qc/outliers.py`'s docstring:

1.  **The agency's `valid` population arrived with readings outside the physical
    domain.** 11,096 rows over the six measurands, **98.2% of them negative**,
    every one of them flagged `valid` upstream — `check_ranges` only reconsiders
    rows already marked valid, so that is where they came from. SO2 contributes
    7,448 and O3 1,964. The three official checks are not exhaustive, and this
    project's range check is what caught them.

2.  **`program_check_invalid` is not a range rule.** If it were, its rejections
    would sit above a boundary. Measured, the share of its rejections above this
    project's configured upper bound is exactly 0.0 for CO, O3 and SO2, 0.0005
    for NO2, and reaches 2.3% only for PM10.

3.  **The dominant signature is a stuck sensor, and nothing in this project can
    see it.** Taking "identical to each of the previous two hours, with no gap"
    as stuck: 1.9% of agency-valid readings, against **42.4%** of
    `instrument_check_invalid` (76.7% for PM10), 24.4% of `program_check_invalid`
    and 12.2% of `manual_check_invalid`. A stuck reading sits at its own cell
    median, so its robust z is approximately zero and the outlier module is blind
    to it by construction.

Together those bear on the circularity worry. The z tests extremeness; the
agency's rejections are dominated by negativity, exact zeros and stuckness, and
only the first of those is visible to a z — as a low excursion, which is exactly
why `instrument_check_invalid` came out enriched on the low tail. The two rules
are not reading the same feature, so the measured agreement is not simple
circularity. It is also substantially carried by negatives, which the trivial
`value < 0` benchmark published beside every lift already catches.

Read-only. Needs the store.
"""

import io
import sys

import polars as pl

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, "src")
from twair.config import load_conf  # noqa: E402
from twair.qc.outliers import INVALID_FLAGS  # noqa: E402
from twair.store.stations import normalise_name_expr  # noqa: E402
from twair.store.writer import scan_observations  # noqa: E402

POLLUTANTS = ["PM2.5", "PM10", "SO2", "NO2", "O3", "CO"]
LAST_LEGACY = 2017

ranges = {
    code: meta["valid_range"]
    for code, meta in load_conf("pollutants").get("pollutants", {}).items()
    if meta.get("valid_range") is not None
}

frame = (
    scan_observations()
    .filter(pl.col("year") <= LAST_LEGACY)
    .filter(pl.col("pollutant").is_in(POLLUTANTS) & pl.col("value").is_not_null())
    .select(
        normalise_name_expr(),
        "ts_local",
        "pollutant",
        pl.col("value").cast(pl.Float64),
        pl.col("flag").cast(pl.Utf8),
    )
    .collect()
    .sort("station_name", "pollutant", "ts_local")
)
print(f"legacy-era rows with a number: {frame.height:,}")

# "Stuck" is only meaningful against the immediately preceding hours at the same
# station and measurand, so the lag is taken over that key and gated on the step
# actually being one hour — a gap is not a repeat.
key = ["station_name", "pollutant"]
frame = frame.with_columns(
    (pl.col("ts_local").diff().over(key) == pl.duration(hours=1)).alias("_step1"),
    (pl.col("value") == pl.col("value").shift(1).over(key)).alias("_same1"),
    (pl.col("value") == pl.col("value").shift(2).over(key)).alias("_same2"),
    (pl.col("ts_local").diff(2).over(key) == pl.duration(hours=2)).alias("_step2"),
).with_columns(
    (
        pl.col("_step1").fill_null(value=False)
        & pl.col("_same1").fill_null(value=False)
        & pl.col("_step2").fill_null(value=False)
        & pl.col("_same2").fill_null(value=False)
    ).alias("stuck3")
)

# One expression rather than a when/then chain: `replace_strict` maps the code
# straight to its bound and returns null for anything unmapped, which is the
# same refusal `check_ranges` makes.
frame = frame.with_columns(
    pl.col("pollutant")
    .cast(pl.Utf8)
    .replace_strict(
        {code: float(ranges[code][1]) for code in POLLUTANTS},
        return_dtype=pl.Float64,
        default=None,
    )
    .alias("_upper")
)

# The baseline is the agency's own `valid`, not "anything not rejected".
# Rows this project marked `out_of_range` are OUR inference and must not be
# folded into the ground-truth denominator — they were what put a 9,999,999
# into the accepted bucket on the first pass.
frame = frame.with_columns(
    pl.when(pl.col("flag").is_in(INVALID_FLAGS))
    .then(pl.col("flag"))
    .when(pl.col("flag") == "valid")
    .then(pl.lit("_agency_valid"))
    .otherwise(pl.lit("_our_own_marking"))
    .alias("population")
)

summary = (
    frame.group_by("population")
    .agg(
        pl.len().alias("rows"),
        (pl.col("value") < 0).mean().alias("negative"),
        (pl.col("value") == 0).mean().alias("exactly_zero"),
        (pl.col("value") > pl.col("_upper")).mean().alias("above_range"),
        pl.col("stuck3").mean().alias("stuck_3h"),
        pl.col("value").median().alias("median"),
        pl.col("value").max().alias("max"),
    )
    .sort("rows", descending=True)
)
with pl.Config(tbl_cols=-1, tbl_width_chars=200, set_ascii_tables=True):
    print()
    print("SIGNATURE OF EACH POPULATION (1982-2017, six measurands)")
    print(summary)

acc = summary.filter(pl.col("population") == "_agency_valid").row(0, named=True)
print()
print("Lift against the agency's own valid readings:")
for row in summary.filter(pl.col("population").is_in(list(INVALID_FLAGS))).iter_rows(named=True):
    parts = []
    for name in ("negative", "exactly_zero", "above_range", "stuck_3h"):
        base = acc[name]
        parts.append(
            f"{name} {row[name] / base:8.1f}x" if base > 0 else f"{name} {row[name]:.4f}/0"
        )
    print(f"  {row['population']:26s} " + "  ".join(parts))

print()
print("=" * 78)
print("DOES `program_check_invalid` LOOK LIKE A RANGE RULE?")
print("=" * 78)
print("If it were, its rejections would sit outside a boundary. Per measurand,")
print("share of each population above this project's configured upper bound:")
print()
per = (
    frame.group_by("pollutant", "population")
    .agg(
        pl.len().alias("rows"),
        (pl.col("value") > pl.col("_upper")).mean().alias("above"),
        (pl.col("value") < 0).mean().alias("neg"),
        pl.col("stuck3").mean().alias("stuck"),
    )
    .sort("pollutant", "population")
)
with pl.Config(tbl_rows=40, set_ascii_tables=True):
    print(per)
