"""M10 — attributable fraction, and the assumption that sets its size.

Health-impact numbers are quoted more often than they are qualified. "Air
pollution causes N deaths a year" is a sentence with three hidden choices in
it: which exposure-response function, which counterfactual concentration, and
which population was exposed. Change the second one alone — from 2.4 to 5.9
μg/m³, both endpoints of the *same* published TMREL distribution — and the
answer moves by roughly a third at Taiwan's current concentrations.

So this module computes the attributable fraction across a **grid** of those
choices and reports the spread as the result, the way M4 reports the weather
share across 74 independently fitted models and M5 reports the placebo spread
beside every effect.

Measured on the balanced panel — 68 stations, 2006–2025 — the answer splits in
two, and the halves point opposite ways:

===== ============== ================ ==========================
 year  median µg/m³   attributable %   assumption as % of that
===== ============== ================ ==========================
 2006           31.9      18.1 – 21.8                       17%
 2025           12.8       5.2 –  9.4                       45%
===== ============== ================ ==========================

**The fall is robust.** It happens at every counterfactual, by 12.4 to 12.9
percentage points. Whatever you assume, the burden roughly halved.

**The level is not.** In 2025 it is 5.2% or 9.4% — near a factor of two — from
a choice between two endpoints of one published TMREL range, a choice almost
never stated when such a figure is quoted.

And the second gets worse as the first succeeds. The absolute spread barely
moves (3.6 to 4.2 points), but as a share of the estimate it nearly triples,
because the excess over the counterfactual is shrinking toward the
counterfactual itself. Subtracting 2.4 rather than 5.9 hardly matters at 32
µg/m³ and matters enormously at 13. **The cleaner the air gets, the more of a
health-burden number is the assumption** — which is the opposite of the
intuition that better data makes estimates firmer.

What is deliberately not here:

**No death counts.** A count is a fraction multiplied by a population and a
baseline mortality rate, and this repository holds station observations, not
people. The population term would dominate the error while reading as the most
solid part of the sentence.

**No GEMM.** It is cause- and age-specific; applying it needs an age-stratified
population that does not exist here. Using it anyway would add decimal places
without adding information.

**No exposure surface.** A station mean is not a population-weighted exposure.
Every result is per station, and the national figure is a median over stations,
labelled as such — not as what anyone breathed.

The fraction reported is

    PAF = (RR - 1) / RR,     RR = exp(beta * max(0, C - counterfactual))

with ``beta = ln(rr_per_10) / 10``. It answers "what share of the risk at this
concentration is attributable to the excess over the counterfactual", and it
needs no population to be meaningful.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import polars as pl

from twair.config import load_conf
from twair.paths import processed_dir

log = logging.getLogger(__name__)

__all__ = [
    "Counterfactual",
    "ResponseFunction",
    "annual_station_means",
    "attributable_fraction",
    "load_counterfactuals",
    "load_response_functions",
    "paf_expr",
    "relative_risk",
    "run_health",
    "sensitivity_grid",
]

TARGET = "PM2.5"

# A station-year needs this share of days before its annual mean is used. The
# daily table already withholds means below its own coverage threshold; this is
# the second gate, at the year level, and it is the same 0.75 the aggregates
# use so the two do not silently disagree.
MIN_ANNUAL_COVERAGE = 0.75


@dataclass(frozen=True, slots=True)
class ResponseFunction:
    name: str
    rr_per_10: float
    rr_per_10_low: float
    rr_per_10_high: float
    outcome: str
    source: str
    source_url: str
    caveat: str

    @property
    def beta(self) -> float:
        """Log-linear coefficient per 1 μg/m³."""
        return math.log(self.rr_per_10) / 10.0

    def beta_at(self, bound: str) -> float:
        value = {
            "central": self.rr_per_10,
            "low": self.rr_per_10_low,
            "high": self.rr_per_10_high,
        }[bound]
        return math.log(value) / 10.0


@dataclass(frozen=True, slots=True)
class Counterfactual:
    name: str
    value: float
    label: str
    why: str


def load_response_functions(conf: dict[str, Any] | None = None) -> dict[str, ResponseFunction]:
    """Read the exposure-response functions, refusing any without a source.

    The same gate `conf/events.yaml` has, for the same reason: a coefficient
    with no provenance cannot be told apart from one that was made up, and this
    project has already caught fabricated citations once.

    Takes the mapping rather than a path so a test can hand it a bad entry
    without writing a YAML file to disk.
    """
    conf = conf if conf is not None else load_conf("health")
    functions: dict[str, ResponseFunction] = {}

    for name, entry in (conf.get("response_functions") or {}).items():
        for required in ("rr_per_10", "source", "source_url", "outcome"):
            if not entry.get(required):
                raise ValueError(f"response function {name!r} has no {required}")
        if not str(entry["source_url"]).startswith("http"):
            raise ValueError(f"response function {name!r} has a source_url that is not a URL")

        central = float(entry["rr_per_10"])
        low = float(entry.get("rr_per_10_low", central))
        high = float(entry.get("rr_per_10_high", central))
        if not low <= central <= high:
            raise ValueError(
                f"response function {name!r} has a central estimate outside its own "
                f"interval: {low} .. {central} .. {high}"
            )

        functions[name] = ResponseFunction(
            name=name,
            rr_per_10=central,
            rr_per_10_low=low,
            rr_per_10_high=high,
            outcome=str(entry["outcome"]),
            source=str(entry["source"]),
            source_url=str(entry["source_url"]),
            caveat=str(entry.get("caveat", "")),
        )

    if not functions:
        raise ValueError("conf/health.yaml declares no response functions")
    return functions


def load_counterfactuals(conf: dict[str, Any] | None = None) -> dict[str, Counterfactual]:
    """Read the counterfactual concentrations, each with its rationale."""
    conf = conf if conf is not None else load_conf("health")
    out: dict[str, Counterfactual] = {}

    for name, entry in (conf.get("counterfactuals") or {}).items():
        if "value" not in entry:
            raise ValueError(f"counterfactual {name!r} has no value")
        value = float(entry["value"])
        if value < 0:
            raise ValueError(f"counterfactual {name!r} is negative: {value}")
        if not entry.get("why"):
            raise ValueError(f"counterfactual {name!r} has no rationale")
        out[name] = Counterfactual(
            name=name,
            value=value,
            label=str(entry.get("label", f"{value} μg/m³")),
            why=str(entry["why"]),
        )

    if not out:
        raise ValueError("conf/health.yaml declares no counterfactuals")
    return out


def relative_risk(concentration: float, *, beta: float, counterfactual: float) -> float:
    """``exp(beta * excess)`` where excess is clipped at zero.

    Clipped because a concentration below the counterfactual does not confer a
    protective effect — it simply contributes nothing attributable. Without the
    clip, a clean offshore station in a good year returns RR < 1 and the
    attributable fraction goes negative, which would read as air pollution
    saving lives.
    """
    return math.exp(beta * max(0.0, concentration - counterfactual))


def attributable_fraction(concentration: float, *, beta: float, counterfactual: float) -> float:
    """``(RR - 1) / RR`` — the share of risk attributable to the excess.

    A proportion, not a count. It needs no population to mean something, which
    is exactly why this module reports it and not deaths.
    """
    rr = relative_risk(concentration, beta=beta, counterfactual=counterfactual)
    return (rr - 1.0) / rr


def paf_expr(column: str, *, beta: float, counterfactual: float) -> pl.Expr:
    """The Polars form of :func:`attributable_fraction`, over a whole column.

    Kept beside the scalar version rather than replacing it: the scalar reads
    like the formula and the expression runs over 3,000 station-years, and
    `tests/test_health.py` asserts they agree. This repository has been bitten
    by a fast path that quietly diverged from the readable one before.
    """
    rr = ((pl.col(column) - counterfactual).clip(lower_bound=0.0) * beta).exp()
    return (rr - 1.0) / rr


def annual_station_means(root: Path | None = None) -> pl.DataFrame:
    """One row per station-year: annual mean PM2.5 and the days behind it.

    Built from the daily table, which has already withheld any day below its
    coverage threshold, so `n_days` counts days that survived that gate rather
    than days on the calendar.
    """
    path = (root or processed_dir("daily")) / "daily.parquet"
    if not path.exists():
        raise FileNotFoundError(f"{path} not found — run `twair aggregate` first")

    daily = (
        pl.scan_parquet(path)
        .filter((pl.col("pollutant") == TARGET) & pl.col("mean").is_not_null())
        .with_columns(pl.col("date").dt.year().alias("year"))
        .group_by("station_name", "year")
        .agg(
            pl.col("mean").mean().alias("annual_mean"),
            pl.len().alias("n_days"),
        )
        .collect()
    )

    return (
        daily.with_columns((pl.col("n_days") / 365.25).alias("coverage"))
        .filter(pl.col("coverage") >= MIN_ANNUAL_COVERAGE)
        .sort("station_name", "year")
    )


def sensitivity_grid(
    annual: pl.DataFrame,
    *,
    functions: dict[str, ResponseFunction],
    counterfactuals: dict[str, Counterfactual],
    bounds: tuple[str, ...] = ("central", "low", "high"),
) -> pl.DataFrame:
    """Attributable fraction for every (function, counterfactual, bound) cell.

    The whole point of the module is this being a grid rather than a number.
    """
    frames: list[pl.DataFrame] = []

    for function in functions.values():
        for bound in bounds:
            beta = function.beta_at(bound)
            for counterfactual in counterfactuals.values():
                paf = paf_expr("annual_mean", beta=beta, counterfactual=counterfactual.value)
                frames.append(
                    annual.with_columns(
                        pl.lit(function.name).alias("function"),
                        pl.lit(bound).alias("bound"),
                        pl.lit(counterfactual.name).alias("counterfactual"),
                        pl.lit(counterfactual.value).alias("counterfactual_ugm3"),
                        paf.alias("paf"),
                    )
                )

    return pl.concat(frames).sort("function", "bound", "counterfactual", "station_name", "year")


def run_health(root: Path | None = None) -> dict[str, pl.DataFrame]:
    """M10 end to end: the grid, its national summary, and the spread.

    Runs on a **balanced panel**, not on every station-year. PM2.5 monitoring
    began at a handful of experimental sites — 1998 has five stations with
    usable coverage against 77 in 2025 — so an unbalanced series would compare
    five places in 1998 with the whole island in 2025 and call the difference a
    trend. Chapter 1 already had to fix this for concentration; the attributable
    fraction inherits it, because it is a function of concentration.

    The panel rule is `twair.panels`, shared with chapter 1 rather than
    reimplemented. On this data it independently picks the same window: 2006
    onwards, 68 stations, 1,360 station-years.
    """
    from twair.panels import balanced_panel_options, choose_balanced_start

    functions = load_response_functions()
    counterfactuals = load_counterfactuals()
    ceiling = float(load_conf("health").get("extrapolation_ceiling_ugm3", 30.0))

    everything = annual_station_means(root)
    if everything.is_empty():
        raise ValueError("no station-year met the coverage threshold")

    options = balanced_panel_options(everything)
    start = choose_balanced_start(options)
    if start is None:
        raise ValueError("no balanced panel could be formed")

    window = everything.filter(pl.col("year") >= start)
    n_years = window["year"].n_unique()
    members = (
        window.group_by("station_name")
        .agg(pl.col("year").n_unique().alias("years"))
        .filter(pl.col("years") == n_years)["station_name"]
        .to_list()
    )
    annual = window.filter(pl.col("station_name").is_in(members))
    log.info("balanced panel: %d stations from %d (%d rows)", len(members), start, annual.height)

    grid = sensitivity_grid(annual, functions=functions, counterfactuals=counterfactuals)

    # National figure = median across stations, per year and per cell. A median
    # over stations is not a population-weighted exposure and is never called
    # one; it is the middle station, which is a statement about the network.
    national = (
        grid.group_by("function", "bound", "counterfactual", "counterfactual_ugm3", "year")
        .agg(
            pl.col("paf").median().alias("paf_median"),
            pl.col("annual_mean").median().alias("mean_median"),
            pl.len().alias("n_stations"),
        )
        .sort("function", "bound", "counterfactual", "year")
    )

    # The headline: at each year, how far apart are the choices? Restricted to
    # the central bound so this measures the *counterfactual* choice rather
    # than mixing it with statistical uncertainty.
    central = national.filter(pl.col("bound") == "central")
    spread = (
        central.group_by("year")
        .agg(
            pl.col("paf_median").min().alias("paf_lowest_assumption"),
            pl.col("paf_median").max().alias("paf_highest_assumption"),
            pl.col("mean_median").first().alias("mean_median"),
            pl.col("n_stations").first().alias("n_stations"),
        )
        .with_columns(
            (pl.col("paf_highest_assumption") - pl.col("paf_lowest_assumption")).alias(
                "assumption_spread"
            )
        )
        .with_columns(
            # The absolute spread barely moves; this is the number that does.
            # Subtracting 2.4 rather than 5.9 hardly matters at 40 µg/m³ and
            # matters enormously at 13, so the cleaner the air gets the more of
            # the answer is the assumption. Reported as a share for that reason.
            (pl.col("assumption_spread") / pl.col("paf_highest_assumption")).alias(
                "spread_as_share_of_estimate"
            )
        )
        .sort("year")
    )

    extrapolated = annual.filter(pl.col("annual_mean") > ceiling)
    coverage = pl.DataFrame(
        {
            "extrapolation_ceiling_ugm3": [ceiling],
            "station_years_total": [annual.height],
            "station_years_above_ceiling": [extrapolated.height],
            "share_above_ceiling": [extrapolated.height / annual.height if annual.height else None],
            "first_year": [annual["year"].min()],
            "last_year": [annual["year"].max()],
            # Panel provenance, so a reader can tell this is 68 fixed stations
            # rather than "whatever was reporting".
            "panel_start_year": [start],
            "panel_stations": [len(members)],
            "unbalanced_station_years": [everything.height],
        }
    )

    return {
        "annual": annual,
        "grid": grid,
        "national": national,
        "spread": spread,
        "coverage": coverage,
    }


def write_health_report(tables: dict[str, pl.DataFrame]) -> dict[str, Path]:
    from twair.paths import outputs_dir

    destination = outputs_dir("m10_health")
    destination.mkdir(parents=True, exist_ok=True)

    written: dict[str, Path] = {}
    for name, frame in tables.items():
        path = destination / f"{name}.parquet"
        frame.write_parquet(path)
        written[name] = path
    return written
