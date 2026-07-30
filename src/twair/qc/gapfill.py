"""Gap filling — switchable, recorded, and never the default.

The 2018 project disposed of every missing value in one sentence:
「將有遺漏值之資料以鄰近測站之資料代替」. No count, no error estimate, no
statement of which stations donated to which. That single clause is the largest
undocumented decision in the work this project re-examines, and criticising it
is cheap. Pricing it is not.

So the original's method is implemented here alongside the alternatives, and
the comparison runs on the same data. `analysis/imputation.py` is what turns
this module into an answer.

**Nothing here runs by default and nothing here touches the store.** The
shipped strategy is ``none``; every other strategy is something a caller asks
for explicitly, on a derived frame, and every value it invents comes back
marked. That is the whole design:

- a filled column gains a companion ``<column>_imputed`` boolean, so a
  downstream model can drop invented rows without knowing which strategy ran;
- :class:`FillReport` carries the counts and the parameters that produced them,
  because a fill rate without its configuration cannot be checked;
- an existing value is never overwritten. ``test_filling_never_moves_a_value
  _that_was_already_there`` pins that for all four strategies.

The one thing this module must not become is a way to make missing data go
away. A null in this codebase is a finding. Filling one is an experiment about
what the finding costs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal

import polars as pl

from twair.config import load_conf
from twair.geometry import haversine_km
from twair.scalars import as_float, opt_float

log = logging.getLogger(__name__)

__all__ = [
    "STRATEGIES",
    "FillReport",
    "Strategy",
    "fill",
    "gapfill_conf",
    "imputed_column",
]

Strategy = Literal["none", "interpolate", "neighbor", "iterative"]

STRATEGIES: tuple[Strategy, ...] = ("none", "interpolate", "neighbor", "iterative")

STATION = "station_name"
TIMESTAMP = "ts_local"


def imputed_column(column: str) -> str:
    """Name of the companion flag that marks which cells were invented."""
    return f"{column}_imputed"


def gapfill_conf(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """The ``gapfill`` block of ``conf/qc.yaml``.

    Parameters live in config rather than in signatures so that a sensitivity
    analysis and a production call cannot silently disagree about what
    "neighbour" means.
    """
    conf = config if config is not None else load_conf("qc")
    block = conf.get("gapfill", {})
    if not isinstance(block, dict):  # pragma: no cover - a malformed config is a bug
        raise ValueError("conf/qc.yaml: `gapfill` must be a mapping")
    return block


def _strategy_params(name: Strategy, config: dict[str, Any] | None = None) -> dict[str, Any]:
    params = gapfill_conf(config).get("strategies", {}).get(name, {})
    return dict(params) if isinstance(params, dict) else {}


@dataclass(frozen=True, slots=True)
class FillReport:
    """What a fill actually did, in enough detail to be checked.

    ``missing_before`` and ``filled`` are per column. The difference is the part
    the strategy could not reach — which is as much of the result as the part it
    could, and is why both are kept rather than a single percentage.
    """

    strategy: Strategy
    rows: int
    missing_before: dict[str, int]
    filled: dict[str, int]
    params: dict[str, Any] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    @property
    def total_missing(self) -> int:
        return sum(self.missing_before.values())

    @property
    def total_filled(self) -> int:
        return sum(self.filled.values())

    @property
    def fill_rate(self) -> float | None:
        """Share of missing cells the strategy invented a value for.

        ``None`` rather than 0.0 when nothing was missing: a rate over an empty
        denominator is undefined, and reporting it as zero would read as "this
        strategy did nothing" when the truth is "there was nothing to do".
        """
        if self.total_missing == 0:
            return None
        return self.total_filled / self.total_missing

    def summary(self) -> str:
        if self.total_missing == 0:
            return f"{self.strategy}: nothing missing across {self.rows:,} rows"
        rate = self.fill_rate
        assert rate is not None
        return (
            f"{self.strategy}: filled {self.total_filled:,} of {self.total_missing:,} "
            f"missing cells ({rate:.1%}) over {self.rows:,} rows"
        )


def fill(
    frame: pl.DataFrame,
    *,
    columns: list[str],
    strategy: Strategy = "none",
    config: dict[str, Any] | None = None,
    geography: pl.DataFrame | None = None,
) -> tuple[pl.DataFrame, FillReport]:
    """Fill gaps in ``columns`` and mark every value invented doing so.

    The frame is one row per (station, hour); ``columns`` are the measurement
    columns to operate on. Returns the frame with a ``<column>_imputed``
    boolean beside each, and a report.

    Row count is preserved. ``interpolate`` needs a complete hourly index to
    know how long a gap is, so it builds one internally and drops back to the
    caller's rows afterwards — otherwise a three-day outage that is *absent*
    rather than *null* looks like two adjacent hours, which is the same trap
    the lag features document.
    """
    if strategy not in STRATEGIES:
        raise ValueError(f"unknown strategy {strategy!r}; expected one of {list(STRATEGIES)}")

    present = [c for c in columns if c in frame.columns]
    unknown = sorted(set(columns) - set(present))
    if unknown:
        raise ValueError(f"columns not in frame: {unknown}")

    missing_before = {c: int(frame[c].null_count()) for c in present}
    params = _strategy_params(strategy, config)

    if strategy == "none":
        out = frame.with_columns([pl.lit(value=False).alias(imputed_column(c)) for c in present])
        return out, FillReport(
            strategy=strategy,
            rows=frame.height,
            missing_before=missing_before,
            filled=dict.fromkeys(present, 0),
            params=params,
            notes=("no values were invented; this is the shipped behaviour",),
        )

    if strategy == "interpolate":
        out, notes = _interpolate(frame, present, params)
    elif strategy == "neighbor":
        out, notes = _neighbor(frame, present, params, geography)
    else:
        out, notes = _iterative(frame, present, params)

    filled = {c: int(out.select(pl.col(imputed_column(c)).sum()).item() or 0) for c in present}
    report = FillReport(
        strategy=strategy,
        rows=frame.height,
        missing_before=missing_before,
        filled=filled,
        params=params,
        notes=notes,
    )
    log.info("%s", report.summary())
    return out, report


def _mark(frame: pl.DataFrame, original: pl.DataFrame, columns: list[str]) -> pl.DataFrame:
    """Attach ``<column>_imputed`` by comparing against the pre-fill frame.

    Derived rather than tracked through each strategy: a flag computed from the
    two frames cannot drift from what actually happened, and it also catches a
    strategy that overwrites a value it should have left alone (that shows up as
    an imputed cell whose original was not null, which ``fill`` asserts against
    in the tests).
    """
    return frame.with_columns(
        [(original[c].is_null() & frame[c].is_not_null()).alias(imputed_column(c)) for c in columns]
    )


def _interpolate(
    frame: pl.DataFrame, columns: list[str], params: dict[str, Any]
) -> tuple[pl.DataFrame, tuple[str, ...]]:
    """Linear interpolation along the hour axis, bounded by gap length.

    Polars' ``interpolate`` will happily bridge a month. The bound is the whole
    point of the strategy: three hours of missing PM2.5 between two real
    readings is a defensible straight line, three weeks is an invention.
    """
    max_gap = int(params.get("max_gap_hours", 3))
    if max_gap < 1:
        raise ValueError(f"max_gap_hours must be >= 1, got {max_gap}")

    from twair.features.lags import complete_hourly_index

    keys = frame.select(STATION, TIMESTAMP)
    dense = complete_hourly_index(frame).sort(STATION, TIMESTAMP)

    filled = dense
    for column in columns:
        is_null = pl.col(column).is_null()
        # A run id that changes whenever null-ness flips, so `len().over(...)`
        # measures the length of *this* gap rather than of all gaps.
        run_id = (is_null != is_null.shift(1).fill_null(value=False)).cum_sum().over(STATION)
        run_length = pl.len().over([pl.col(STATION), run_id])

        filled = filled.with_columns(
            pl.when(is_null & (run_length <= max_gap))
            .then(pl.col(column).interpolate().over(STATION))
            .otherwise(pl.col(column))
            .alias(column)
        )

    back = keys.join(filled, on=[STATION, TIMESTAMP], how="left").sort(STATION, TIMESTAMP)
    original = frame.sort(STATION, TIMESTAMP)
    inserted = dense.height - frame.height
    notes = (
        f"linear interpolation over gaps of at most {max_gap}h",
        f"{inserted:,} absent hours were materialised to measure gap length, then dropped",
    )
    return _mark(back, original, columns), notes


def _neighbor(
    frame: pl.DataFrame,
    columns: list[str],
    params: dict[str, Any],
    geography: pl.DataFrame | None,
) -> tuple[pl.DataFrame, tuple[str, ...]]:
    """The 2018 method: borrow from a nearby station.

    Implemented as the regression `conf/qc.yaml` specifies rather than as a
    raw substitution, because a bare copy ignores that two stations measure
    different mean levels — and the original's own wording (「代替」) is
    ambiguous between the two. Regression is the more favourable reading, so
    pricing *it* is the honest comparison.

    A donor must be within ``max_distance_km`` and correlate at least
    ``min_correlation`` on the hours where both stations reported. Stations
    with no coordinates cannot be served at all, and that is reported rather
    than passed over: six stations in this archive have decades of observations
    and no entry in the MOENV register.
    """
    max_km = float(params.get("max_distance_km", 30))
    min_r = float(params.get("min_correlation", 0.7))

    geo = geography if geography is not None else _load_geography()
    coords = {
        row[STATION]: (row["lat"], row["lon"])
        for row in geo.iter_rows(named=True)
        if row.get("lat") is not None and row.get("lon") is not None
    }

    stations = frame[STATION].unique().sort().to_list()
    unplaced = [s for s in stations if s not in coords]

    out = frame.sort(STATION, TIMESTAMP)
    original = out
    donors_used: list[str] = []

    for column in columns:
        # One wide matrix of station columns for this measurand: correlation and
        # regression both need the two series aligned on the hour, and a pivot
        # states that alignment once instead of per candidate pair.
        matrix = out.select(STATION, TIMESTAMP, column).pivot(
            on=STATION, index=TIMESTAMP, values=column, aggregate_function="first"
        )
        replacements: list[pl.DataFrame] = []

        for target in stations:
            if target not in coords or target not in matrix.columns:
                continue
            if matrix[target].null_count() == 0:
                continue

            best = _best_donor(matrix, target, stations, coords, max_km, min_r)
            if best is None:
                continue
            donor, slope, intercept = best
            donors_used.append(f"{column}:{target}<-{donor}")

            predicted = matrix.select(
                pl.col(TIMESTAMP),
                pl.when(pl.col(target).is_null() & pl.col(donor).is_not_null())
                .then(pl.col(donor) * slope + intercept)
                .otherwise(pl.col(target))
                .alias("_filled"),
            ).with_columns(pl.lit(target).alias(STATION))
            replacements.append(predicted)

        if not replacements:
            continue

        patch = pl.concat(replacements, how="vertical").select(STATION, TIMESTAMP, "_filled")
        out = (
            out.join(patch, on=[STATION, TIMESTAMP], how="left")
            .with_columns(pl.coalesce(pl.col(column), pl.col("_filled")).alias(column))
            .drop("_filled")
        )

    notes: tuple[str, ...] = (
        f"donor within {max_km:.0f} km and r >= {min_r:.2f}, fitted by OLS on overlapping hours",
        f"{len(donors_used)} (column, station) pairs found a donor",
    )
    if unplaced:
        notes += (
            f"{len(unplaced)} station(s) have no coordinates and cannot be served: "
            f"{', '.join(unplaced)}",
        )
    return _mark(out.sort(STATION, TIMESTAMP), original, columns), notes


def _best_donor(
    matrix: pl.DataFrame,
    target: str,
    stations: list[str],
    coords: dict[str, tuple[float, float]],
    max_km: float,
    min_r: float,
) -> tuple[str, float, float] | None:
    """Nearest qualifying station, with the regression that maps it onto the target."""
    tlat, tlon = coords[target]
    candidates: list[tuple[float, str]] = []
    for other in stations:
        if other == target or other not in coords or other not in matrix.columns:
            continue
        olat, olon = coords[other]
        km = haversine_km(tlat, tlon, olat, olon)
        if km <= max_km:
            candidates.append((km, other))

    for _km, donor in sorted(candidates):
        pair = matrix.select(target, donor).drop_nulls()
        # Two points define a line and tell you nothing about whether it holds.
        if pair.height < 24:
            continue
        r = pair.select(pl.corr(target, donor)).item()
        if r is None or r < min_r:
            continue
        x, y = pair[donor], pair[target]
        # Every one of these is a Polars aggregate, typed as the union of
        # everything a cell could hold; `as_float` converts for real and refuses
        # a null rather than letting an empty selection become a plausible zero.
        # See twair.scalars for why this is the sanctioned form.
        variance = opt_float(x.var())
        if variance is None or variance == 0:
            continue
        x_mean, y_mean = as_float(x.mean()), as_float(y.mean())
        covariance = as_float(((x - x_mean) * (y - y_mean)).sum()) / (pair.height - 1)
        slope = covariance / variance
        return donor, slope, y_mean - slope * x_mean
    return None


def _load_geography() -> pl.DataFrame:
    from twair.ingest.station_meta import load_station_geo

    return load_station_geo()


def _iterative(
    frame: pl.DataFrame, columns: list[str], params: dict[str, Any]
) -> tuple[pl.DataFrame, tuple[str, ...]]:
    """Multivariate iterative imputation (the MICE family).

    Every column is regressed on the others in turn, round-robin, until the
    estimates settle. It is the most capable of the four and the least
    inspectable: there is no donor to name and no straight line to draw, so a
    reader cannot check any individual value. That trade is the reason it is
    reported next to the others rather than recommended.

    Fitted per station. Pooling would let a quiet rural site borrow the
    covariance structure of a roadside one, which is the same borrowing the
    neighbour strategy at least makes explicit.
    """
    max_iter = int(params.get("max_iter", 10))

    from sklearn.experimental import enable_iterative_imputer  # noqa: F401
    from sklearn.impute import IterativeImputer

    out = frame.sort(STATION, TIMESTAMP)
    original = out
    pieces: list[pl.DataFrame] = []
    skipped = 0

    for _station, group in out.group_by([STATION], maintain_order=True):
        block = group.select(columns)
        usable = [c for c in columns if block[c].null_count() < block.height]
        if len(usable) < 2 or block.height < 24:
            # One column has nothing to regress on, so the imputer would return
            # the column mean under a name that implies more than that.
            skipped += 1
            pieces.append(group)
            continue

        imputer = IterativeImputer(max_iter=max_iter, random_state=0, keep_empty_features=True)
        filled = imputer.fit_transform(block.select(usable).to_numpy())
        pieces.append(
            group.with_columns(
                [
                    pl.Series(name, filled[:, i]).cast(group.schema[name])
                    for i, name in enumerate(usable)
                ]
            )
        )

    out = pl.concat(pieces, how="vertical").sort(STATION, TIMESTAMP)
    notes: tuple[str, ...] = (
        f"IterativeImputer, max_iter={max_iter}, fitted per station",
        "no donor is nameable and no individual value is checkable",
    )
    if skipped:
        notes += (f"{skipped} station(s) had too little to regress on and were left alone",)
    return _mark(out, original, columns), notes
