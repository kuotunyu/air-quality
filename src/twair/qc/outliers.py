"""Separate a short isolated excursion from a network-wide episode.

PLAN.md section 1.2 item 4 asks for one thing:

    區分「儀器尖峰」與「真實污染事件」：單站尖峰但鄰站無反應 + 持續 <2h → 可疑；
    多站同步上升 → 真實事件（沙塵/境外傳輸）

Both halves of that are claims about **extent**, so the module measures extent
and refuses to measure anything else. It marks; it never edits `value` or
`flag`, never writes back to the store, and never fills a gap.

The statistic
-------------
An hour is scored against the median and MAD of its own
``(station, year, month, hour-of-day)`` cell — roughly thirty observations, one
month of one clock hour at one station. Station removes site level, month
removes season, hour-of-day removes the diurnal cycle, and year removes the
44-year trend.

It is deliberately **not** a centred rolling window. A rolling baseline follows
a sustained rise: measured on a synthetic 36-hour, +60 µg/m³ step against an
N(20,4) background at a 25-hour window, detection collapses from 6 hours of
episode to **zero at 7 hours and above** — the baseline has climbed with the
data and the z falls to ~0 in the middle while the inflated spread kills the
edges. The per-cell climatology returns all 36 hours as one run. A rolling
window also starves itself on exactly the hours that matter: a three-day outage
removes most of a 25-hour window but only ~3 of ~30 cell members. Measured
scoreable fractions of the agency's own rejected readings, PM2.5 2015: 99.2% /
96.9% / 74.4% per rejection category under the cell, against 57.1% / 59.6% /
4.9% under the rolling window. Both of those comparisons are reproducible —
``uv run python scripts/compare_outlier_baselines.py`` runs them here rather
than citing a review.

What the threshold is not
-------------------------
``min_zscore: 3.0`` is a **rank threshold on a skewed distribution**, not a
Gaussian tail probability. A Gaussian one-sided z ≥ 3 is 0.135% of hours; the
measured rate on agency-accepted PM2.5 2015 hours is **3.11% on the high tail
and 3.18% counting either** — the second is what ``hour_rates.excursion_rate``
and every ``base_rate`` in ``agency_agreement`` carry. Hourly pollutant
distributions are strongly right-skewed and the scale is estimated from ~30
points, which fattens the tail on its own. Absolute counts of excursions are
therefore large, and only the rate against the accepted-hour base rate — both
published in the same row — carries information.

Both tails are scored, and this is not symmetry for its own sake. Measured on
this statistic, ``instrument_check_invalid`` — the largest invalidation category
in the store — is enriched on the **low** tail, not the high one. A high-only
detector would have had almost nothing to say about it. A low run is published
with ``direction = "low"`` and can never become a ``regional_episode``: below
the local level is a statement about a reading, not about the air.

What it may not conclude
------------------------
* An ``uncorroborated_short_rise`` run is not an instrument fault, and it is named
  the way it is because an earlier name said it was. Measured with the method
  `qc/sentinels.py` uses — the separately valid wind and rain recorded at the
  same station-hour — this class is **not distinguishable** from
  ``regional_episode``: relative to ordinary hours at the same stations both are
  stagnant and dry, and the gap between the two classes is 17-34% of the gap
  each has from background, with the sign reversing on P(wind < 0.5 m/s) so that
  the uncorroborated class is the calmer one. Its month-of-year profile matches
  ``uncorroborated_sustained_rise`` almost exactly (both peak Jul-Aug on five of
  six measurands) rather than looking like a fault, which has no season. What
  was measured is that the rise was short and the neighbours did not share it.
  That is all the name now says.
* A station's share of uncorroborated verdicts is substantially a property of
  where it was built, and the size of that depends on which runs you look at.
  Among stations with at least 200 high runs: **r = +0.61** between a station's
  nearest-neighbour distance and its uncorroborated-short-rise share over runs
  that received a verdict, **+0.75** once restricted to hours with at least five
  scoreable neighbours, and **+0.34** over every high run including the refused
  ones. The last is lowest because ``too_few_scoreable_neighbours`` now removes
  the most isolated stations from the judged population — their median nearest
  neighbour is 32.2 km against 8.3 km for all high runs — so the ≥20 km band
  comes out *lowest* on the shipped output (6.1%) while the 10-20 km band is
  highest (18.0%). Ranking stations or years by that share still ranks network
  geometry and build-out unless ``km_nearest_neighbour`` is held constant, which
  is why it ships on every run.
* A ``regional_episode`` is not 沙塵, not 境外傳輸 and not an inversion. Naming
  an origin needs the trajectory model this repository deliberately does not
  have (see `analysis/sources.py`), and the hypothesis in the conf/qc.yaml
  comment is a hypothesis in a config file, not a result.
* Agreement with ``program_check_invalid`` (程式檢核) may be agreement between
  two automated threshold rules applied to the same numbers. The agency's check
  procedure is not documented in this repository and cannot be assumed
  independent of this one. The annual 品保查核報告, which is the only document
  that could break the circularity, has not been obtained — see PROGRESS.md.
* A lift indistinguishable from 1 is evidence **against** the statistic tracking
  that category, and is reported as prominently as a large one.
* An excursion occupying a large share of one calendar month at one clock hour
  cannot be detected at all: the cell absorbs it and becomes the baseline. The
  detector is a band-pass, and ``elevated_share_of_cell`` is published so a
  reader can see which cells scored their own episode.
* No agreement figure exists from 2018 onwards. The modern archives replace an
  invalidated reading with the flag itself, so those rows carry no number.
  Measured 2018-2025: **1,132,936** invalidation-flagged rows across every
  measurand in the store and **555,559** across the six this module scans —
  **zero** with a value in either case. Those years appear in
  ``agency_agreement.parquet`` with a zero denominator rather than being absent,
  because a missing row reads as "not measured" and a zero row reads as
  "nothing to measure".

The corroboration null
----------------------
"Several stations rose together" means nothing without knowing how often
several stations rise together anyway. Every run therefore carries
``n_neighbors_elevated_null`` — the same count recomputed against each
neighbour's own excursion series shifted by whole weeks in both directions,
which preserves each neighbour's rate, its clustering and its seasonal phase and
destroys only the synchrony — and ``corroboration_excess``, the difference.
Classification uses the observed count, and the excess is what says whether the
observed count meant anything.

The shifts run forwards as well as backwards because a backwards-only null is
undefined at the start of the scanned span, and a null that could not be
computed must not arrive as a zero: that would hand the largest excess to
exactly the runs with no comparison behind them. Where a shift lands outside the
data it is dropped rather than counted, ``n_null_shifts_measurable`` says how
many survived, and a run with none gets a null excess.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import numpy as np
import polars as pl

from twair.config import load_conf
from twair.geometry import pairwise_km
from twair.paths import outputs_dir
from twair.qc.flags import Flag
from twair.qc.rainfall import usable
from twair.store.aggregate import circular_variables
from twair.store.stations import normalise_name_expr
from twair.store.writer import scan_observations

log = logging.getLogger(__name__)

__all__ = [
    "BASELINE_CELL",
    "BOUNDARY_REASONS",
    "OUTPUT_TABLES",
    "VERDICTS",
    "OutlierReport",
    "classify_boundaries",
    "corroborate",
    "delimit_runs",
    "neighbour_edges",
    "outliers_conf",
    "run_outliers",
    "score_hours",
    "verdict_expr",
    "write_outlier_report",
]

STATION = "station_name"
TIMESTAMP = "ts_local"

#: The population a reading is compared against. ``obs_year`` is inside it on
#: purpose: without it the 44-year decline would make the whole 1990s read as
#: one long episode. It also makes a per-year chunk produce bit-identical
#: baselines to a whole-span run.
BASELINE_CELL = (STATION, "obs_year", "obs_month", "obs_hour")

#: Phi^-1(0.75). Scales a MAD so it is comparable to a standard deviation *for a
#: Gaussian* — which these distributions are not; see the module docstring.
MAD_TO_SIGMA = 0.6744897501960817

#: Derived from the enum rather than spelled out. `qc/report.py` records that a
#: hand-written flag list undercounted this population by 75% until it was
#: derived; importing that module's copy would pull in a `rich` Console at
#: import time for a three-element tuple.
INVALID_FLAGS: tuple[str, ...] = (
    Flag.INSTRUMENT_CHECK_INVALID.value,
    Flag.PROGRAM_CHECK_INVALID.value,
    Flag.MANUAL_CHECK_INVALID.value,
)

Verdict = Literal[
    "uncorroborated_short_rise",
    "regional_episode",
    "uncorroborated_sustained_rise",
    "uncheckable",
]
VERDICTS: tuple[Verdict, ...] = (
    "uncorroborated_short_rise",
    "regional_episode",
    "uncorroborated_sustained_rise",
    "uncheckable",
)

#: Why a run stopped where it did — not a censored/not-censored bit, because the
#: reason a run ended is itself a finding. Only ``below_threshold`` means the
#: excursion ended; every other value means the record did, and they are kept
#: apart because they are not the same event. No count is written here: this
#: comment said "five" while the tuple held six, and then "six" while it held
#: eight. `tests/test_outliers.py` checks the tuple against what
#: `classify_boundaries` can actually emit instead.
BOUNDARY_REASONS: tuple[str, ...] = (
    "below_threshold",
    "unscored",
    "agency_rejected",
    "value_null",
    "absent",
    "series_edge",
    "outside_requested_span",
    "direction_reversal",
)


def outliers_conf(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """The ``outliers`` block of conf/qc.yaml."""
    conf = config if config is not None else load_conf("qc")
    block = conf.get("outliers", {})
    if not isinstance(block, dict):  # pragma: no cover - a malformed config is a bug
        raise ValueError("conf/qc.yaml: `outliers` must be a mapping")
    return block


def _section(name: str, config: dict[str, Any] | None = None) -> dict[str, Any]:
    block = outliers_conf(config).get(name, {})
    if not isinstance(block, dict):  # pragma: no cover - a malformed config is a bug
        raise ValueError(f"conf/qc.yaml: `outliers.{name}` must be a mapping")
    return block


@dataclass(frozen=True, slots=True)
class OutlierReport:
    """What one pass measured, and under which parameters.

    Every count is per pollutant so a rate can never be quoted without its
    denominator. Derived quantities are properties rather than stored fields,
    following ``FillReport``.
    """

    pollutants: tuple[str, ...]
    years: tuple[int, int]
    usable_hours: dict[str, int] = field(default_factory=dict)
    scored_hours: dict[str, int] = field(default_factory=dict)
    unscored_thin_cell: dict[str, int] = field(default_factory=dict)
    unscored_zero_scale: dict[str, int] = field(default_factory=dict)
    high_hours: dict[str, int] = field(default_factory=dict)
    low_hours: dict[str, int] = field(default_factory=dict)
    runs: dict[str, int] = field(default_factory=dict)
    runs_censored: dict[str, int] = field(default_factory=dict)
    verdicts: dict[str, int] = field(default_factory=dict)
    params: dict[str, Any] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    @property
    def total_runs(self) -> int:
        return sum(self.runs.values())

    @property
    def total_scored(self) -> int:
        return sum(self.scored_hours.values())

    @property
    def high_rate(self) -> float | None:
        """High-tail hours as a share of scored hours, or None over nothing.

        Publishing 0.0 for an empty denominator would read as "nothing was
        elevated" when the truth is "nothing could be judged".
        """
        scored = self.total_scored
        return None if scored == 0 else sum(self.high_hours.values()) / scored

    @property
    def excursion_rate(self) -> float | None:
        """Either tail, which is what `hour_rates.excursion_rate` publishes.

        Named apart from :attr:`high_rate` on purpose. The two differ — 3.11%
        against 3.18% for PM2.5 2015 — and a summary line that says "elevated"
        while the table beside it computes something else is how two numbers
        that should agree stop agreeing without anyone noticing.
        """
        scored = self.total_scored
        if scored == 0:
            return None
        return (sum(self.high_hours.values()) + sum(self.low_hours.values())) / scored

    @property
    def suspect_rate(self) -> float | None:
        """Suspected-instrument runs as a share of runs that got a verdict."""
        judged = self.total_runs - self.verdicts.get("uncheckable", 0)
        return None if judged == 0 else self.verdicts.get("uncorroborated_short_rise", 0) / judged

    def summary(self) -> str:
        high = self.high_rate
        either = self.excursion_rate
        suspect = self.suspect_rate
        parts = [
            f"{len(self.pollutants)} measurand(s), {self.years[0]}-{self.years[1]}",
            f"{self.total_scored:,} hours scored",
            f"excursions {'—' if either is None else f'{either:.2%}'}"
            f" ({'—' if high is None else f'{high:.2%}'} high)",
            f"{self.total_runs:,} runs",
            f"suspect {'—' if suspect is None else f'{suspect:.1%}'} of judged",
        ]
        counted = ", ".join(f"{k} {v:,}" for k, v in sorted(self.verdicts.items()))
        return " | ".join(parts) + (f"\n  verdicts: {counted}" if counted else "")


def _load_geography() -> pl.DataFrame:
    from twair.ingest.station_meta import load_station_geo

    return load_station_geo()


def neighbour_edges(
    *,
    radius_km: float,
    geography: pl.DataFrame | None = None,
    config: dict[str, Any] | None = None,
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Every ordered station pair within ``radius_km``, plus the placement ledger.

    Refuses an empty register rather than publishing a table of ``uncheckable``.
    ``load_station_geo`` returns an empty typed frame when the cache is absent,
    by design, so without this guard a missing conf/station_geo.yaml would
    produce a complete, well-formed and entirely vacuous result in which nothing
    distinguishes "the register is missing" from "the network is unplaceable" —
    the same refusal `analysis/spatial.py` makes.

    Names are normalised on both sides: 台南 and 臺南 would otherwise be two
    stations, one of them with no edges at all.
    """
    geo = geography if geography is not None else _load_geography()
    placed = geo.with_columns(normalise_name_expr(config=config)).filter(
        pl.col("lat").is_not_null() & pl.col("lon").is_not_null()
    )
    if placed.is_empty():
        raise ValueError(
            "no station in the register has coordinates — conf/station_geo.yaml is "
            "missing or empty, and every run would come back uncheckable"
        )

    names = placed[STATION].to_list()
    distance = pairwise_km(placed["lat"].to_numpy(), placed["lon"].to_numpy())
    left, right = np.nonzero((distance > 0.0) & (distance <= float(radius_km)))
    edges = pl.DataFrame(
        {
            STATION: [names[a] for a in left],
            "neighbor_name": [names[b] for b in right],
            "km": distance[left, right],
        },
        schema={STATION: pl.Utf8, "neighbor_name": pl.Utf8, "km": pl.Float64},
    )

    ledger = (
        pl.DataFrame({STATION: names})
        .join(edges.group_by(STATION).len(name="n_neighbours"), on=STATION, how="left")
        # Distance to the nearest placed station, kept rather than discarded.
        # A station's share of uncorroborated verdicts is substantially a
        # function of how far away its nearest neighbour is; the module
        # docstring carries the measured correlations and, more importantly,
        # the population each one was measured over, because the same statistic
        # ranges from +0.34 to +0.75 across four defensible populations of the
        # very same output. Without this column a reader ranking stations by
        # that share is ranking network geometry with no way to see it.
        .join(
            edges.group_by(STATION).agg(pl.col("km").min().alias("km_nearest_neighbour")),
            on=STATION,
            how="left",
        )
        # A placed station with no edge measured zero neighbours inside the
        # radius. That is a number; an unplaceable station has none.
        .with_columns(
            pl.col("n_neighbours").fill_null(0).cast(pl.UInt32),
            pl.lit(True).alias("has_coordinates"),
        )
    )
    return edges, ledger


def score_hours(frame: pl.LazyFrame, *, min_samples: int, min_zscore: float) -> pl.DataFrame:
    """Attach the cell statistics, the robust z, and three-valued candidacy.

    ``robust_z`` is withheld — null, not infinite and not zero — where the cell
    holds fewer than ``min_samples`` readings or where its spread is exactly
    zero. ``deviation`` stays defined in native units in both cases, because a
    reading 900 µg/m³ above a cell whose MAD is zero is a finding the z cannot
    express.

    ``excursion`` is Boolean-or-null, never Boolean. Null means the hour was
    never tested, which is a different claim from "tested and ordinary" — the
    two-distinguishable-nulls rule at hour granularity, and what makes the
    ``unscored`` boundary reason possible.
    """
    scored = (
        frame.with_columns(
            pl.col("value").median().over(BASELINE_CELL).alias("baseline"),
            pl.len().over(BASELINE_CELL).cast(pl.UInt32).alias("n_cell"),
        )
        .with_columns((pl.col("value") - pl.col("baseline")).alias("deviation"))
        .with_columns(pl.col("deviation").abs().median().over(BASELINE_CELL).alias("scale"))
        .with_columns(
            pl.when((pl.col("n_cell") >= min_samples) & (pl.col("scale") > 0.0))
            .then(MAD_TO_SIGMA * pl.col("deviation") / pl.col("scale"))
            .otherwise(None)
            .alias("robust_z")
        )
        .with_columns(
            pl.when(pl.col("robust_z").is_null())
            .then(None)
            .otherwise(pl.col("robust_z").abs() >= min_zscore)
            .alias("excursion"),
            pl.when(pl.col("robust_z").is_null())
            .then(None)
            .when(pl.col("robust_z") >= 0)
            .then(pl.lit("high"))
            .otherwise(pl.lit("low"))
            .alias("direction"),
            pl.when(pl.col("robust_z").is_not_null())
            .then(None)
            .when(pl.col("scale") <= 0.0)
            .then(pl.lit("zero_scale"))
            .otherwise(pl.lit("thin_cell"))
            .alias("unscored_reason"),
        )
        .with_columns(
            # How much of this cell scored as an excursion. A cell whose own
            # month contained a persistent episode has that episode in its
            # median and its MAD, so the excursion is defined away — the
            # detector is a band-pass and this column is where that shows.
            # Contamination also inflates the MAD: about 10% of a thirty-point
            # cell lifts the scale roughly 14%, which deflates every z in it.
            pl.when(pl.col("robust_z").is_not_null())
            .then(
                pl.col("excursion").fill_null(value=False).sum().over(BASELINE_CELL)
                / pl.col("n_cell")
            )
            .otherwise(None)
            .alias("elevated_share_of_cell")
        )
    )
    return scored.collect()


def delimit_runs(marked: pl.DataFrame) -> pl.DataFrame:
    """Group consecutive excursion hours of the same sign into runs.

    A run continues only if both hours are excursions of the same direction
    **and** the timestamps are exactly one hour apart. Because the frame holds
    only usable rows, an agency-rejected hour and a null-valued hour are already
    absent and both terminate a run by that same one rule — which is why no
    dense hourly index is needed to express "the reading stopped here".

    ``fill_null`` appears only on the boolean helpers. It never touches a
    measurement.
    """
    if marked.is_empty():
        return _empty_runs()

    ordered = marked.sort(STATION, TIMESTAMP)
    is_excursion = pl.col("excursion").fill_null(value=False)
    same_hour_step = (pl.col(TIMESTAMP).diff().over(STATION) == pl.duration(hours=1)).fill_null(
        value=False
    )
    same_direction = (pl.col("direction") == pl.col("direction").shift(1).over(STATION)).fill_null(
        value=False
    )
    continues = (
        is_excursion.shift(1).over(STATION).fill_null(value=False) & same_hour_step & same_direction
    )
    starts = is_excursion & ~continues

    ordered = ordered.with_columns(
        pl.when(is_excursion).then(starts.cum_sum().over(STATION)).otherwise(None).alias("run_id")
    )

    peak = pl.col("robust_z").abs()
    return (
        ordered.filter(pl.col("run_id").is_not_null())
        .group_by(STATION, "run_id")
        .agg(
            pl.col(TIMESTAMP).min().alias("t_start"),
            pl.col(TIMESTAMP).max().alias("t_end"),
            pl.len().cast(pl.UInt32).alias("duration_hours"),
            pl.col("direction").first().alias("direction"),
            # At-peak columns are gathered from the row that produced the peak,
            # not taken as independent maxima of each column, which would
            # describe an hour that never happened.
            peak.max().alias("peak_abs_z"),
            pl.col("robust_z").gather(peak.arg_max()).first().alias("peak_z"),
            pl.col("value").gather(peak.arg_max()).first().alias("peak_value"),
            pl.col("baseline").gather(peak.arg_max()).first().alias("baseline_at_peak"),
            pl.col("scale").gather(peak.arg_max()).first().alias("scale_at_peak"),
            pl.col("n_cell").gather(peak.arg_max()).first().alias("n_cell_at_peak"),
            pl.col("elevated_share_of_cell")
            .gather(peak.arg_max())
            .first()
            .alias("elevated_share_of_cell_at_peak"),
            pl.col("obs_year").first().alias("obs_year"),
        )
        .sort(STATION, "t_start")
    )


def _empty_runs() -> pl.DataFrame:
    return pl.DataFrame(
        schema={
            STATION: pl.Utf8,
            "run_id": pl.UInt32,
            "t_start": pl.Datetime("us"),
            "t_end": pl.Datetime("us"),
            "duration_hours": pl.UInt32,
            "direction": pl.Utf8,
            "peak_abs_z": pl.Float64,
            "peak_z": pl.Float64,
            "peak_value": pl.Float64,
            "baseline_at_peak": pl.Float64,
            "scale_at_peak": pl.Float64,
            "n_cell_at_peak": pl.UInt32,
            "obs_year": pl.Int32,
        }
    )


def classify_boundaries(
    runs: pl.DataFrame,
    marked: pl.DataFrame,
    present: pl.DataFrame,
    *,
    span: tuple[datetime, datetime] | None = None,
) -> pl.DataFrame:
    """Say why each run ended, and refuse to call a censored run short.

    Only ``below_threshold`` means the excursion actually ended. Everything else
    means the record stopped: the hour was present but never tested, present but
    unusable, absent from the store, outside the station's observed span for
    this measurand, or outside the span the caller asked for. A censored run's
    duration is a **lower bound**, so the "<2h therefore suspicious" test must
    not be applied to it — this is what stops a null from silently ending a run
    and being read as brevity.

    The last two reasons are kept apart because only one of them is a fact about
    the data. A run touching 1 January of a one-year request is censored by the
    request; a run touching the first hour a station ever reported is censored by
    the record. Both stop a verdict, and calling the first one `series_edge`
    would put a property of the command line into a column that reads as a
    property of the network.
    """
    if runs.is_empty():
        return runs.with_columns(
            pl.lit(None, dtype=pl.Utf8).alias("left_boundary"),
            pl.lit(None, dtype=pl.Utf8).alias("right_boundary"),
            pl.lit(None, dtype=pl.Boolean).alias("censored"),
        )

    # The span is per station for this measurand only, taken from the same
    # unfiltered frame the boundary lookup uses. An hour present for CO must not
    # count as present for PM2.5.
    spans = present.group_by(STATION).agg(
        pl.col(TIMESTAMP).min().alias("_span_lo"),
        pl.col(TIMESTAMP).max().alias("_span_hi"),
    )
    lookup = present.join(
        marked.select(STATION, TIMESTAMP, "excursion", "direction"),
        on=[STATION, TIMESTAMP],
        how="left",
    )

    def requested_miss(hour_col: str) -> pl.Expr:
        if span is None:
            return pl.lit(False)
        return (pl.col(hour_col) < span[0]) | (pl.col(hour_col) > span[1])

    out = runs.join(spans, on=STATION, how="left").with_columns(
        (pl.col("t_start") - pl.duration(hours=1)).alias("_before"),
        (pl.col("t_end") + pl.duration(hours=1)).alias("_after"),
    )
    for side, hour_col in (("left", "_before"), ("right", "_after")):
        out = out.join(
            lookup.select(
                STATION,
                pl.col(TIMESTAMP).alias(hour_col),
                pl.col("is_usable").alias(f"_{side}_usable"),
                pl.col("is_rejected").alias(f"_{side}_rejected"),
                pl.col("excursion").alias(f"_{side}_tested"),
                pl.col("direction").alias(f"_{side}_direction"),
            ),
            on=[STATION, hour_col],
            how="left",
        ).with_columns(
            # Ordered: outside the record first, then absent, then present but
            # unusable, then present and usable but never tested, and only
            # then "we tested it and it was ordinary".
            pl.when(requested_miss(hour_col))
            .then(pl.lit("outside_requested_span"))
            .when(pl.col(hour_col).is_between(pl.col("_span_lo"), pl.col("_span_hi")).not_())
            .then(pl.lit("series_edge"))
            .when(pl.col(f"_{side}_usable").is_null())
            .then(pl.lit("absent"))
            # Split, because these two are the difference between "the agency
            # looked at this hour and threw it out" and "there was no number".
            # The first is the nearest thing to an independent check the module
            # has, and while both were called `unusable` it could not be run.
            .when(~pl.col(f"_{side}_usable") & pl.col(f"_{side}_rejected"))
            .then(pl.lit("agency_rejected"))
            .when(~pl.col(f"_{side}_usable"))
            .then(pl.lit("value_null"))
            .when(pl.col(f"_{side}_tested").is_null())
            .then(pl.lit("unscored"))
            # Tested, and it WAS an excursion — the other way. Measured on the
            # whole store, 472 boundary hours across 463 runs. Calling those
            # `below_threshold` says the hour was ordinary, and it was not.
            .when(pl.col(f"_{side}_tested") & (pl.col(f"_{side}_direction") != pl.col("direction")))
            .then(pl.lit("direction_reversal"))
            .otherwise(pl.lit("below_threshold"))
            .alias(f"{side}_boundary")
        )

    return out.with_columns(
        (
            (pl.col("left_boundary") != "below_threshold")
            | (pl.col("right_boundary") != "below_threshold")
        ).alias("censored")
    ).drop(
        "_before",
        "_after",
        "_span_lo",
        "_span_hi",
        "_left_usable",
        "_right_usable",
        "_left_tested",
        "_right_tested",
    )


def corroborate(
    runs: pl.DataFrame,
    marked: pl.DataFrame,
    edges: pl.DataFrame,
    ledger: pl.DataFrame,
    *,
    lag_hours: int,
    null_shift_days: tuple[int, ...],
) -> pl.DataFrame:
    """Count the neighbours that were placed, testable, and excursing.

    Three counts, not one, and the difference between them is the finding:

    ``n_neighbors_placed``      in radius, coordinates known
    ``n_neighbors_scoreable``   of those, at least one **tested** hour in window
    ``n_neighbors_elevated``    of those, at least one excursion in window

    A neighbour that reported but was never scoreable is not evidence of
    anything, so the gate is on ``n_neighbors_scoreable``. All three are NULL
    for a station with no coordinates — we did not measure zero neighbours, we
    could not measure at all — and 0 for a placed station with an empty radius.

    ``lag_hours`` exists because a store timestamp is an hour aggregate and a
    plume crossing an hour boundary would otherwise score as no response. It is
    not travel-time matching; that would need the trajectory model this
    repository deliberately does not have.

    A neighbour counts as responding only if it is excursing **in the same
    direction** as the run. Several stations reading below their own level is
    not several stations rising together.

    ``null_shift_days`` gives the same count against each neighbour's series
    shifted by whole weeks. That preserves every neighbour's own rate, its run
    clustering and its diurnal and seasonal phase, and destroys only synchrony —
    so ``corroboration_excess`` is the part of the observed count that being
    the same hour actually bought.
    """
    if runs.is_empty():
        return runs

    windows = runs.select(
        STATION,
        "run_id",
        # The run's own direction travels with it: 多站同步**上升** means the
        # neighbours have to be moving the same way. Without this a high run
        # whose neighbours all dipped below their own level would be counted as
        # a corroborated regional episode, which is the opposite of the claim.
        pl.col("direction").alias("_run_direction"),
        pl.datetime_ranges(
            pl.col("t_start") - pl.duration(hours=lag_hours),
            pl.col("t_end") + pl.duration(hours=lag_hours),
            interval="1h",
        ).alias(TIMESTAMP),
        # Explicit because the default flips in Polars 2.0. A run always spans
        # at least one hour so no range here is ever empty, but a silent change
        # of meaning under a version bump is the kind of thing this repository
        # has already been bitten by once.
    ).explode(TIMESTAMP, empty_as_null=False)

    neighbours = marked.select(
        pl.col(STATION).alias("neighbor_name"),
        TIMESTAMP,
        pl.col("excursion").alias("_excursion"),
        pl.col("direction").alias("_direction"),
    )
    paired = windows.join(edges, on=STATION, how="inner")

    def counted(offset_hours: int, suffix: str) -> pl.DataFrame:
        # Shifting the run window backwards is equivalent to shifting every
        # neighbour series forwards, and keeps one copy of the neighbour frame.
        shifted = paired.with_columns(
            (pl.col(TIMESTAMP) - pl.duration(hours=offset_hours)).alias(TIMESTAMP)
        )
        return (
            shifted.join(neighbours, on=["neighbor_name", TIMESTAMP], how="left")
            .group_by(STATION, "run_id")
            .agg(
                pl.col("neighbor_name").n_unique().cast(pl.UInt32).alias(f"n_placed{suffix}"),
                pl.col("neighbor_name")
                .filter(pl.col("_excursion").is_not_null())
                .n_unique()
                .cast(pl.UInt32)
                .alias(f"n_scoreable{suffix}"),
                pl.col("neighbor_name")
                .filter(
                    pl.col("_excursion").fill_null(value=False)
                    & (pl.col("_direction") == pl.col("_run_direction"))
                )
                .n_unique()
                .cast(pl.UInt32)
                .alias(f"n_elevated{suffix}"),
            )
        )

    observed = counted(0, "").rename(
        {
            "n_placed": "n_neighbors_placed",
            "n_scoreable": "n_neighbors_scoreable",
            "n_elevated": "n_neighbors_elevated",
        }
    )
    out = runs.join(observed, on=[STATION, "run_id"], how="left")

    nulls = [counted(days * 24, f"_s{i}") for i, days in enumerate(null_shift_days)]
    for frame in nulls:
        out = out.join(frame, on=[STATION, "run_id"], how="left")
    if nulls:
        # A shift that lands outside the scanned span has no neighbour data to
        # look at, so its count is not zero — it is unmeasurable, and averaging
        # a zero in there would report "the shifted calendar saw nothing" when
        # the truth is "there was no shifted calendar to see". Measured on a
        # single-year span, runs in the first 42 days had a shifted denominator
        # of 0.036 neighbours against an observed 15.9, so their excess came out
        # as the whole observed count. Each shift is therefore kept only where
        # it had at least one scoreable neighbour, and a run with no usable
        # shift at all gets a null excess rather than a flattering one.
        usable_counts = [
            pl.when(pl.col(f"n_scoreable_s{i}") > 0)
            .then(pl.col(f"n_elevated_s{i}"))
            .otherwise(None)
            for i in range(len(nulls))
        ]
        usable_denominators = [
            pl.when(pl.col(f"n_scoreable_s{i}") > 0)
            .then(pl.col(f"n_scoreable_s{i}"))
            .otherwise(None)
            for i in range(len(nulls))
        ]
        out = out.with_columns(
            pl.mean_horizontal(usable_counts).alias("n_neighbors_elevated_null"),
            pl.mean_horizontal(usable_denominators).alias("n_neighbors_scoreable_null"),
            pl.sum_horizontal(
                [(pl.col(f"n_scoreable_s{i}") > 0).cast(pl.UInt32) for i in range(len(nulls))]
            ).alias("n_null_shifts_measurable"),
        ).drop(
            [
                c
                for i in range(len(nulls))
                for c in (f"n_placed_s{i}", f"n_scoreable_s{i}", f"n_elevated_s{i}")
            ]
        )
    else:  # pragma: no cover - the shipped config always carries shifts
        out = out.with_columns(
            pl.lit(None, dtype=pl.Float64).alias("n_neighbors_elevated_null"),
            pl.lit(None, dtype=pl.Float64).alias("n_neighbors_scoreable_null"),
            pl.lit(0, dtype=pl.UInt32).alias("n_null_shifts_measurable"),
        )

    out = out.join(ledger.select(STATION, "km_nearest_neighbour"), on=STATION, how="left")
    placed = set(ledger.filter(pl.col("has_coordinates"))[STATION].to_list())
    counts = ("n_neighbors_placed", "n_neighbors_scoreable", "n_neighbors_elevated")
    return (
        out.with_columns(pl.col(STATION).is_in(placed).alias("has_coordinates"))
        .with_columns(
            # Three states, and the order matters. A placed station whose radius is
            # empty produced no join rows and arrives here as null; that is a
            # measured zero. An unplaceable station stays null, because "we could
            # not look" must never render as "we looked and found none".
            *[
                pl.when(pl.col("has_coordinates"))
                .then(pl.col(c).fill_null(0))
                .otherwise(None)
                .alias(c)
                for c in counts
            ],
            # Deliberately NOT filled. The three counts above are filled to zero
            # for a placed station because an empty radius is a measured zero,
            # but a shifted calendar that fell outside the scanned span is not a
            # calendar in which nothing happened — it is no calendar. Filling it
            # would make `corroboration_excess` equal the whole observed count
            # for exactly the runs whose null could not be computed.
            pl.when(pl.col("has_coordinates"))
            .then(pl.col("n_neighbors_elevated_null"))
            .otherwise(None)
            .alias("n_neighbors_elevated_null"),
            # Same rule as the counts above it: an unplaceable station did not
            # try four calendars and find them empty, it had no calendar.
            pl.when(pl.col("has_coordinates"))
            .then(pl.col("n_null_shifts_measurable"))
            .otherwise(None)
            .alias("n_null_shifts_measurable"),
        )
        .with_columns(
            (pl.col("n_neighbors_elevated") - pl.col("n_neighbors_elevated_null")).alias(
                "corroboration_excess"
            )
        )
    )


def verdict_expr(
    *, min_stations: int, max_duration_hours: int, min_neighbor_corroboration: int
) -> tuple[pl.Expr, pl.Expr]:
    """The verdict and its reason, as one ordered chain.

    The order is load-bearing:

    1. no coordinates          — nothing about neighbours can be said at all
    2. too few scoreable neighbours — absence of evidence, not evidence of a
       lone spike. The bar is ``min_stations - 1`` rather than one, because a
       high run with a single scoreable neighbour cannot reach
       ``regional_episode`` whatever that neighbour did: the verdict would
       record the station's situation, not a comparison. Measured on the first
       whole-store pass, 19,105 runs sat in that unreachable support and were
       judged anyway. Low runs keep the bar at zero, since they are never
       eligible for the episode verdict in the first place.
    3. synchronous rise        — **before** the duration test, because a
       one-hour rise seen at four stations is still network-wide; the spec's
       discriminator is extent, not brevity. High tail only: several stations
       reading below their own level is not an episode.
    4. censored                — **after** the episode test, because
       corroboration holds however this station's run was cut, and **before**
       the spike test, because "<2h" is exactly the claim censoring destroys
    5. short and uncorroborated — the spec's 可疑
    6. otherwise                — the residual the spec does not describe, left
       named but unexplained rather than given a mechanism nobody measured

    Steps 5 and 6 are one population cut at ``max_duration_hours``. They share a
    seasonal profile and they share their meteorology; only the length differs.
    The two names differ only in the word that describes the length.
    """
    stations_rising = pl.col("n_neighbors_elevated") + 1
    high = pl.col("direction") == "high"

    verdict = (
        pl.when(~pl.col("has_coordinates"))
        .then(pl.lit("uncheckable"))
        .when(high & (pl.col("n_neighbors_scoreable") < min_stations - 1))
        .then(pl.lit("uncheckable"))
        .when(~high & (pl.col("n_neighbors_scoreable") == 0))
        .then(pl.lit("uncheckable"))
        .when(high & (stations_rising >= min_stations))
        .then(pl.lit("regional_episode"))
        .when(pl.col("censored"))
        .then(pl.lit("uncheckable"))
        .when(
            (pl.col("duration_hours") <= max_duration_hours)
            & (pl.col("n_neighbors_elevated") < min_neighbor_corroboration)
        )
        .then(pl.lit("uncorroborated_short_rise"))
        .otherwise(pl.lit("uncorroborated_sustained_rise"))
        .alias("verdict")
    )
    reason = (
        pl.when(~pl.col("has_coordinates"))
        .then(pl.lit("no_coordinates"))
        .when(high & (pl.col("n_neighbors_scoreable") < min_stations - 1))
        .then(pl.lit("too_few_scoreable_neighbours"))
        .when(~high & (pl.col("n_neighbors_scoreable") == 0))
        .then(pl.lit("no_scoreable_neighbour"))
        .when(high & (stations_rising >= min_stations))
        .then(pl.lit("synchronous_rise"))
        .when(pl.col("censored"))
        .then(pl.lit("duration_censored"))
        .when(
            (pl.col("duration_hours") <= max_duration_hours)
            & (pl.col("n_neighbors_elevated") < min_neighbor_corroboration)
        )
        .then(pl.lit("short_and_alone"))
        .otherwise(pl.lit("sustained_and_alone"))
        .alias("verdict_reason")
    )
    return verdict, reason


def agency_agreement(
    root: Path | None,
    *,
    pollutant: str,
    years: tuple[int, int],
    cells: pl.DataFrame,
    min_samples: int,
    min_zscore: float,
    valid_rates: pl.DataFrame,
) -> pl.DataFrame:
    """Score the agency's own rejections against the same statistic.

    Rejected readings are a separate population and never candidates: they
    contribute to no baseline and no run, because an agency rejection outranks
    every inference this package makes. They are also the closest thing to
    ground truth available, so agreement is **measured** rather than asserted.

    Three counts are emitted so a zero denominator is visible rather than
    absent: ``rejected_rows`` (the flag was set), ``rejected_with_value`` (and a
    number survived — only the legacy suffix era retains one), and
    ``rejected_scored`` (and its cell could describe it). From 2018 the middle
    one is zero for every flag, which is why those years must appear as a row
    rather than vanish.

    ``lift`` never travels alone: ``excursion_rate``, ``base_rate`` and the
    counts behind both are in the same row, because the enrichment is a ratio to
    the detector's own false-positive rate on accepted hours — halving that rate
    doubles the lift with no change in detection.

    ``n_negative`` and ``n_exactly_zero`` are the trivial benchmarks. A z-based
    agreement claim that cannot beat "the reading is negative" has not earned
    the arithmetic behind it.
    """
    rejected = (
        scan_observations(root)
        .filter(pl.col("year").is_between(years[0], years[1]))
        .filter(
            (pl.col("pollutant") == pollutant) & pl.col("flag").cast(pl.Utf8).is_in(INVALID_FLAGS)
        )
        .select(
            normalise_name_expr(),
            TIMESTAMP,
            pl.col("value").cast(pl.Float64),
            pl.col("flag").cast(pl.Utf8),
        )
        .with_columns(
            pl.col(TIMESTAMP).dt.year().cast(pl.Int32).alias("obs_year"),
            pl.col(TIMESTAMP).dt.month().cast(pl.Int8).alias("obs_month"),
            pl.col(TIMESTAMP).dt.hour().cast(pl.Int8).alias("obs_hour"),
        )
        .join(cells.lazy(), on=list(BASELINE_CELL), how="left")
        .with_columns(
            pl.when(
                pl.col("value").is_not_null()
                & (pl.col("n_cell") >= min_samples)
                & (pl.col("scale") > 0.0)
            )
            .then(MAD_TO_SIGMA * (pl.col("value") - pl.col("baseline")) / pl.col("scale"))
            .otherwise(None)
            .alias("robust_z")
        )
        .group_by("obs_year", "flag")
        .agg(
            pl.len().cast(pl.UInt32).alias("rejected_rows"),
            pl.col("value").is_not_null().sum().cast(pl.UInt32).alias("rejected_with_value"),
            pl.col("robust_z").is_not_null().sum().cast(pl.UInt32).alias("rejected_scored"),
            (pl.col("robust_z") >= min_zscore).sum().cast(pl.UInt32).alias("n_high"),
            (pl.col("robust_z") <= -min_zscore).sum().cast(pl.UInt32).alias("n_low"),
            pl.col("robust_z").median().alias("z_median"),
            (pl.col("value") < 0).sum().cast(pl.UInt32).alias("n_negative"),
            (pl.col("value") == 0).sum().cast(pl.UInt32).alias("n_exactly_zero"),
        )
        .collect()
    )
    if rejected.is_empty():
        return rejected

    return (
        rejected.join(valid_rates, on="obs_year", how="left")
        .with_columns(
            pl.lit(pollutant).alias("pollutant"),
            # None over an empty denominator, never 0.0: a rate nobody could
            # compute and a rate that came out zero are different findings.
            pl.when(pl.col("rejected_scored") > 0)
            .then((pl.col("n_high") + pl.col("n_low")) / pl.col("rejected_scored"))
            .otherwise(None)
            .alias("excursion_rate"),
        )
        .with_columns(
            pl.when((pl.col("base_rate") > 0) & pl.col("excursion_rate").is_not_null())
            .then(pl.col("excursion_rate") / pl.col("base_rate"))
            .otherwise(None)
            .alias("lift")
        )
        .select(
            "obs_year",
            "pollutant",
            "flag",
            "rejected_rows",
            "rejected_with_value",
            "rejected_scored",
            "n_high",
            "n_low",
            "z_median",
            "n_negative",
            "n_exactly_zero",
            "excursion_rate",
            "base_rate",
            "base_scored",
            "lift",
        )
        .sort("obs_year", "flag")
    )


def _hour_rates(marked: pl.DataFrame, pollutant: str) -> pl.DataFrame:
    """Per-year exceedance, with the denominators that make it readable."""
    excursion = pl.col("excursion").fill_null(value=False)
    return (
        marked.group_by("obs_year")
        .agg(
            pl.len().cast(pl.UInt32).alias("usable_hours"),
            pl.col("robust_z").is_not_null().sum().cast(pl.UInt32).alias("scored_hours"),
            (excursion & (pl.col("direction") == "high")).sum().cast(pl.UInt32).alias("high_hours"),
            (excursion & (pl.col("direction") == "low")).sum().cast(pl.UInt32).alias("low_hours"),
            (pl.col("unscored_reason") == "thin_cell").sum().cast(pl.UInt32).alias("thin_cell"),
            (pl.col("unscored_reason") == "zero_scale").sum().cast(pl.UInt32).alias("zero_scale"),
            # Over cells, not over hours. Taking the median of `n_cell` across
            # rows counts every cell once per reading it holds, so it is
            # weighted by the thing it is measuring and reads high.
            pl.col("n_cell")
            .filter(pl.struct(BASELINE_CELL).is_first_distinct())
            .median()
            .alias("median_cell_size"),
            pl.col("elevated_share_of_cell").max().alias("max_elevated_share_of_cell"),
            pl.when(pl.col("robust_z").is_not_null().any())
            .then((pl.col("elevated_share_of_cell") >= 0.25).sum().cast(pl.UInt32))
            .otherwise(None)
            .alias("hours_in_quarter_contaminated_cells"),
        )
        .with_columns(
            pl.when(pl.col("scored_hours") > 0)
            .then((pl.col("high_hours") + pl.col("low_hours")) / pl.col("scored_hours"))
            .otherwise(None)
            .alias("excursion_rate"),
            pl.lit(pollutant).alias("pollutant"),
        )
        .sort("obs_year")
    )


def run_outliers(
    *,
    years: tuple[int, int] = (1982, 2025),
    pollutants: list[str] | None = None,
    root: Path | None = None,
    config: dict[str, Any] | None = None,
    geography: pl.DataFrame | None = None,
) -> tuple[dict[str, pl.DataFrame], OutlierReport]:
    """Run the pass over the requested span and return the tables and the report."""
    conf = outliers_conf(config)
    spike = _section("spike", config)
    event = _section("event", config)
    baseline = _section("baseline", config)

    min_samples = int(baseline["min_samples"])
    min_zscore = float(event["min_zscore"])
    radius_km = float(spike["neighbor_radius_km"])
    lag_hours = int(spike["neighbor_lag_hours"])
    max_duration = int(spike["max_duration_hours"])
    min_corroboration = int(spike["min_neighbor_corroboration"])
    min_stations = int(event["min_stations"])
    shifts = tuple(int(days) for days in conf["null_shift_days"])

    requested = list(pollutants or conf.get("pollutants", []))
    if not requested:
        raise ValueError("conf/qc.yaml: `outliers.pollutants` is empty and none were requested")

    pollutant_conf = load_conf("pollutants")
    circular = circular_variables(pollutant_conf) & set(requested)
    if circular:
        raise ValueError(
            "circular measurand(s) cannot be described by a median and a MAD: "
            f"{sorted(circular)} — 359 and 1 are two degrees apart, not 358"
        )
    described = {
        code
        for code, meta in pollutant_conf.get("pollutants", {}).items()
        if meta.get("valid_range") is not None
    }
    undescribed = sorted(set(requested) - described)
    if undescribed:
        raise ValueError(
            f"no valid_range in conf/pollutants.yaml for {undescribed} — nothing "
            "downstream could interpret a z on a measurand with no declared domain"
        )

    edges, ledger = neighbour_edges(radius_km=radius_km, geography=geography)

    notes: list[str] = []
    observed_stations: set[str] = set()
    collected: dict[str, list[pl.DataFrame]] = {
        "runs": [],
        "hour_rates": [],
        "agency_agreement": [],
        "unscored_deviation": [],
    }
    usable_hours: dict[str, int] = {}
    scored_hours: dict[str, int] = {}
    thin: dict[str, int] = {}
    zero_scale: dict[str, int] = {}
    high: dict[str, int] = {}
    low: dict[str, int] = {}
    run_counts: dict[str, int] = {}
    censored_counts: dict[str, int] = {}
    verdict_counts: dict[str, int] = {}

    for pollutant in requested:
        # The unfiltered key frame. Step "classify_boundaries" needs to tell an
        # hour that is absent from the store from one that is present and
        # unusable, and that distinction is the whole defence of the "<2h" claim.
        present = (
            scan_observations(root)
            .filter(pl.col("year").is_between(years[0], years[1]))
            .filter(pl.col("pollutant") == pollutant)
            .select(
                normalise_name_expr(),
                TIMESTAMP,
                pl.col("value").cast(pl.Float64),
                usable().alias("is_usable"),
                pl.col("flag").cast(pl.Utf8).is_in(INVALID_FLAGS).alias("is_rejected"),
            )
            .collect()
        )
        if present.is_empty():
            notes.append(f"{pollutant}: no rows in {years[0]}-{years[1]}")
            continue
        observed_stations |= set(present[STATION].unique().to_list())

        frame = (
            present.lazy()
            .filter(pl.col("is_usable"))
            .with_columns(
                pl.col(TIMESTAMP).dt.year().cast(pl.Int32).alias("obs_year"),
                pl.col(TIMESTAMP).dt.month().cast(pl.Int8).alias("obs_month"),
                pl.col(TIMESTAMP).dt.hour().cast(pl.Int8).alias("obs_hour"),
            )
        )
        marked = score_hours(frame, min_samples=min_samples, min_zscore=min_zscore)

        excursion = pl.col("excursion").fill_null(value=False)
        usable_hours[pollutant] = marked.height
        scored_hours[pollutant] = int(marked["robust_z"].is_not_null().sum())
        thin[pollutant] = marked.filter(pl.col("unscored_reason") == "thin_cell").height
        zero_scale[pollutant] = marked.filter(pl.col("unscored_reason") == "zero_scale").height
        high[pollutant] = marked.filter(excursion & (pl.col("direction") == "high")).height
        low[pollutant] = marked.filter(excursion & (pl.col("direction") == "low")).height

        # The withheld class, characterised rather than merely counted: a cell
        # with no spread can still sit far below a reading, and the deviation
        # says so in native units where the z cannot.
        unscored = marked.filter(pl.col("robust_z").is_null())
        if not unscored.is_empty():
            collected["unscored_deviation"].append(
                unscored.group_by("obs_year", "unscored_reason")
                .agg(
                    pl.len().cast(pl.UInt32).alias("hours"),
                    pl.col("deviation").abs().max().alias("max_abs_deviation"),
                    pl.col("deviation").abs().median().alias("median_abs_deviation"),
                )
                .with_columns(pl.lit(pollutant).alias("pollutant"))
                .sort("obs_year", "unscored_reason")
            )

        runs = delimit_runs(marked)
        runs = classify_boundaries(
            runs,
            marked,
            present,
            span=(datetime(years[0], 1, 1), datetime(years[1], 12, 31, 23)),
        )
        runs = corroborate(runs, marked, edges, ledger, lag_hours=lag_hours, null_shift_days=shifts)
        run_counts[pollutant] = runs.height
        if not runs.is_empty():
            verdict, reason = verdict_expr(
                min_stations=min_stations,
                max_duration_hours=max_duration,
                min_neighbor_corroboration=min_corroboration,
            )
            runs = runs.with_columns(verdict, reason, pl.lit(pollutant).alias("pollutant"))
            censored_counts[pollutant] = int(runs["censored"].sum())
            for name, count in runs["verdict"].value_counts().iter_rows():
                verdict_counts[name] = verdict_counts.get(name, 0) + int(count)
            collected["runs"].append(runs)
        else:
            censored_counts[pollutant] = 0

        rates = _hour_rates(marked, pollutant)
        collected["hour_rates"].append(rates)

        cells = marked.unique(subset=list(BASELINE_CELL)).select(
            *BASELINE_CELL, "baseline", "scale", "n_cell"
        )
        agreement = agency_agreement(
            root,
            pollutant=pollutant,
            years=years,
            cells=cells,
            min_samples=min_samples,
            min_zscore=min_zscore,
            valid_rates=rates.select(
                "obs_year",
                pl.col("excursion_rate").alias("base_rate"),
                pl.col("scored_hours").alias("base_scored"),
            ),
        )
        if not agreement.is_empty():
            collected["agency_agreement"].append(agreement)

    # A ledger that lists only the stations the register could place records
    # every success and no failure. The six stations with decades of
    # observations and no register entry are exactly what a reader checking
    # coverage is looking for, so they go in the table, not only in a note.
    unplaceable = sorted(observed_stations - set(ledger[STATION].to_list()))
    if unplaceable:
        # Built from the ledger's own schema rather than from a literal, so a
        # column added to `neighbour_edges` cannot desynchronise the two. It
        # did: adding `km_nearest_neighbour` there left this frame one column
        # short and `run_outliers` raised ShapeError on the shipped config,
        # while every one of the 655 tests still passed — because none of them
        # ran this function end to end.
        measured = [name for name in ledger.columns if name not in (STATION, "has_coordinates")]
        ledger = pl.concat(
            [
                ledger,
                pl.DataFrame({STATION: unplaceable}, schema={STATION: ledger.schema[STATION]})
                .with_columns(
                    # Every measured column is null: not one of these was zero,
                    # none of them was measurable.
                    *[pl.lit(None, dtype=ledger.schema[name]).alias(name) for name in measured],
                    pl.lit(False).alias("has_coordinates"),
                )
                .select(ledger.columns),
            ],
            how="vertical",
        ).sort(STATION)
        notes.append(
            f"{len(unplaceable)} station(s) reported in this span but are absent from "
            f"conf/station_geo.yaml, so no run of theirs can be corroborated: "
            f"{', '.join(unplaceable)}"
        )
    notes.append(
        f"min_zscore={min_zscore} is a rank threshold on a skewed distribution, not a "
        "Gaussian tail probability; the realised rate is hour_rates.excursion_rate"
    )

    tables = {
        name: pl.concat(frames, how="vertical_relaxed")
        for name, frames in collected.items()
        if frames
    }
    # Month-of-year per verdict, published rather than left to be derived. It is
    # the check that separates the two "alone" classes from the corroborated
    # one, and it is the module's strongest positive result: on the first
    # whole-store pass, `regional_episode` peaked in November-December for the
    # particle and combustion tracers — the north-east monsoon season — and in
    # June for O3, the photochemical one. A statistic that had merely relabelled
    # noise would not have put the ozone phase opposite the particle phase. The
    # two uncorroborated classes share one profile, peaking in July-August on
    # five of six measurands, which is why their names differ only in length.
    if "runs" in tables:
        tables["verdict_seasonality"] = (
            tables["runs"]
            .with_columns(pl.col("t_start").dt.month().cast(pl.Int8).alias("month"))
            .group_by("pollutant", "verdict", "direction", "month")
            .agg(pl.len().cast(pl.UInt32).alias("runs"))
            .sort("pollutant", "verdict", "direction", "month")
        )
    tables["station_coverage"] = ledger
    tables["parameters"] = pl.DataFrame(
        {
            "parameter": [
                "pollutants",
                "years",
                "min_samples",
                "min_zscore",
                "neighbor_radius_km",
                "neighbor_lag_hours",
                "max_duration_hours",
                "min_neighbor_corroboration",
                "min_stations",
                "null_shift_days",
            ],
            "value": [
                # Without these two a reader cannot tell "this measurand was
                # scanned and produced nothing" from "this measurand was never
                # scanned", and a directory of parquet says nothing about which
                # span it describes.
                ",".join(requested),
                f"{years[0]}:{years[1]}",
                str(min_samples),
                str(min_zscore),
                str(radius_km),
                str(lag_hours),
                str(max_duration),
                str(min_corroboration),
                str(min_stations),
                ",".join(str(days) for days in shifts),
            ],
        }
    )

    tables["notes"] = pl.DataFrame({"note": list(notes)}, schema={"note": pl.Utf8})
    # An empty run table still has a shape. A reader who loads a zero-column
    # parquet learns that the file was written; one who loads this learns what
    # the run looked for and did not find.
    tables.setdefault("runs", _empty_runs())

    report = OutlierReport(
        pollutants=tuple(requested),
        years=years,
        usable_hours=usable_hours,
        scored_hours=scored_hours,
        unscored_thin_cell=thin,
        unscored_zero_scale=zero_scale,
        high_hours=high,
        low_hours=low,
        runs=run_counts,
        runs_censored=censored_counts,
        verdicts=verdict_counts,
        params={
            "min_samples": min_samples,
            "min_zscore": min_zscore,
            "min_zscore_source": "conf/qc.yaml outliers.event.min_zscore",
            "neighbor_radius_km": radius_km,
            "neighbor_lag_hours": lag_hours,
            "max_duration_hours": max_duration,
            "min_neighbor_corroboration": min_corroboration,
            "min_stations": min_stations,
            "null_shift_days": shifts,
        },
        notes=tuple(notes),
    )
    log.info("%s", report.summary())
    return tables, report


#: Every table a run is expected to produce. Named here so a run that produces
#: nothing for one of them still writes an empty frame over the last run's file:
#: an empty table is a finding, a stale one from a different span is a lie, and
#: the two are indistinguishable once they sit side by side in a directory.
OUTPUT_TABLES: tuple[str, ...] = (
    "runs",
    "hour_rates",
    "agency_agreement",
    "unscored_deviation",
    "verdict_seasonality",
    "station_coverage",
    "parameters",
    "notes",
)


def write_outlier_report(tables: dict[str, pl.DataFrame]) -> dict[str, Path]:
    """Persist one parquet per table under ``data/outputs/qc_outliers/``.

    Every name in :data:`OUTPUT_TABLES` is written on every run, empty or not.
    Skipping the empty ones left the previous run's file in place beside the new
    ones, so the directory could describe two different spans at once with
    nothing in it naming either — which is exactly what happened during this
    module's own review.
    """
    destination = outputs_dir("qc_outliers")
    destination.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    for name in OUTPUT_TABLES:
        frame = tables.get(name, pl.DataFrame())
        path = destination / f"{name}.parquet"
        frame.write_parquet(path, compression="zstd")
        written[name] = path
    for name, frame in tables.items():
        if name in written:
            continue
        path = destination / f"{name}.parquet"
        frame.write_parquet(path, compression="zstd")
        written[name] = path
    return written
