"""D7 — what 「分 8 區各跑一次」 actually buys.

The defect list records D7 as「78 個測站的空間維度沒用」. That is nearly right and
the "nearly" is the interesting part: the baseline does not ignore space, it
*stratified* by air-quality zone and fitted the model once inside each zone. So
the honest question is not "did they use space" but **was that stratification an
adequate spatial control**, and that question has an answer with a number.

The answer is a residual diagnostic, not a claim about the air. PM2.5 is
obviously spatially autocorrelated; saying so is not a finding. What matters is
whether the *residuals* of the fitted model still are, because the baseline's
inference — 6,771 station-months treated as independent observations, t
statistics in the eighties — is valid only if they are not.

Four things this module refuses to do, each because doing it would produce a
number that looks precise and is not:

**It does not permute residuals.** A permutation null assumes exchangeability,
and OLS residuals are not exchangeable: they satisfy X'e = 0 by construction, so
shuffling them destroys a constraint the real residuals obey and the resulting
p-value is about the wrong null. The null here simulates under the fitted model
instead. That turns out to be cheap for a reason worth recording: since MX = 0,
refitting inside each draw and projecting an iid draw through the residual maker
give the *same* vector, so a valid draw costs one matrix-vector product rather
than one regression.

**It does not use E[I] = -1/(n-1).** That is the null for a raw field. For
regression residuals the moments are Cliff-Ord's, which depend on the design
through M = I - X(X'X)⁻¹X'. With this panel's twelve predictors the two differ by
more than a third of a standard deviation of I, which is enough to move a
borderline month across the threshold.

**It does not report a single headline Moran's I.** The magnitude of I is a joint
property of the field and the weights matrix, and the correlogram in this network
changes sign with distance — so one number is a weighted blend of opposite-signed
bands. The correlogram and the sensitivity grid ship whole, and the CLI prints
the correlogram before any single I.

**It prices the OLS stage only.** What it corrects is the assumption of
independent errors in the baseline fit, and a t-statistic is not the whole of an
inference. A terminal model with an AR(1) error structure would carry its own
standard errors, and none are computed here, so nothing in this module should be
read as overturning such a model. That limit is in the payload, the report and
the CLI output, not only in this docstring.

One measured caveat on the closed form. Cliff-Ord's Var[I] is an approximation
whose accuracy falls away as k/n grows; a Monte-Carlo against the fitted null
puts it 11% too high at this panel's k/n of about 0.18, and monotonically worse
above that. The direction is conservative — it understates significance — but
"conservative" is not "correct", so the published p-value comes from the
simulated null and the closed-form z ships beside it as a reference.
`tests/test_spatial.py` pins both the agreement of E[I] and the size of the
Var[I] gap, so a future change that silently swaps one for the other fails.

The residual I is a **lower bound** on the field's spatial dependence, twice
over, and both belong in any sentence that quotes it: the fitted model contains
predictors that are themselves spatially correlated (PM10 above all), so it has
already absorbed spatial signal; and the panel is complete-case, so the network
it covers is selected by data completeness.
"""

from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import numpy as np
import polars as pl

from twair.config import load_conf
from twair.geometry import EARTH_RADIUS_KM, pairwise_km
from twair.paths import outputs_dir
from twair.scalars import as_float

log = logging.getLogger(__name__)

__all__ = [
    "STEPS",
    "DissimilarityBundle",
    "MoranResult",
    "SpatialConf",
    "SpatialResult",
    "WeightsBundle",
    "benjamini_hochberg",
    "block_bootstrap_mean",
    "build_weights",
    "cliff_ord_moments",
    "field_skill",
    "geographic_ensemble",
    "inference_price",
    "lisa",
    "load_m1_artefacts",
    "load_spatial_conf",
    "moran_correlogram",
    "morans_i",
    "partition_agreement",
    "partition_price",
    "placed_panel",
    "reconstruct_residuals",
    "residual_autocorrelation",
    "residual_null_draws",
    "run_spatial",
    "station_coverage",
    "station_dissimilarity",
    "weights_sensitivity",
    "write_spatial_report",
]

STATION = "station_name"
MONTH = "month"
RESIDUAL = "residual"

STEPS: tuple[str, ...] = (
    "coverage",
    "headline",
    "sensitivity",
    "partition",
    "lisa",
    "inference",
    "field",
)

WeightsFamily = Literal["knn", "distance_band"]


# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class SpatialConf:
    """Every threshold, grid, seed and station list M6 uses.

    Held as one frozen object so that no literal k, bin edge or zone name
    appears anywhere else in the module — a sensitivity run and a headline run
    cannot silently disagree about what "neighbour" means.
    """

    weights: dict[str, Any]
    correlogram: dict[str, Any]
    inference: dict[str, Any]
    panel: dict[str, Any]
    clustering: dict[str, Any]
    field: dict[str, Any]

    @property
    def seed(self) -> int:
        return int(self.inference["seed"])

    @property
    def min_units(self) -> int:
        return int(self.weights["min_units"])

    @property
    def fdr_alpha(self) -> float:
        return float(self.inference["fdr_alpha"])

    @property
    def era_zones(self) -> tuple[str, ...]:
        return tuple(self.panel["era_zones"])

    @property
    def offshore(self) -> tuple[str, ...]:
        return tuple(self.panel["offshore_stations"])


def load_spatial_conf() -> SpatialConf:
    """Read ``conf/spatial.yaml`` through the loader that rejects boolean keys."""
    raw = load_conf("spatial")
    missing = [
        block
        for block in ("weights", "correlogram", "inference", "panel", "clustering", "field")
        if block not in raw
    ]
    if missing:
        raise ValueError(f"conf/spatial.yaml is missing block(s): {missing}")
    return SpatialConf(
        weights=dict(raw["weights"]),
        correlogram=dict(raw["correlogram"]),
        inference=dict(raw["inference"]),
        panel=dict(raw["panel"]),
        clustering=dict(raw["clustering"]),
        field=dict(raw["field"]),
    )


# --------------------------------------------------------------------------- #
# inputs
# --------------------------------------------------------------------------- #
def load_m1_artefacts(root: Path | None = None) -> tuple[pl.DataFrame, pl.DataFrame]:
    """The replicated 2018 panel and its OLS table.

    Read from M1's outputs rather than re-derived, so that the model whose
    residuals are tested here is provably the same fit M1 published. All of
    ``data/`` is gitignored, so a fresh clone has neither file and the error says
    which command produces them.
    """
    base = outputs_dir("m1_baseline") if root is None else root / "outputs" / "m1_baseline"
    panel_path, ols_path = base / "panel.parquet", base / "ols.parquet"
    for path in (panel_path, ols_path):
        if not path.exists():
            raise FileNotFoundError(
                f"{path} not found — run `uv run twair analyze m1` first; M6 tests the "
                "residuals of that fit rather than re-deriving it"
            )
    return pl.read_parquet(panel_path), pl.read_parquet(ols_path)


def reconstruct_residuals(panel: pl.DataFrame, ols: pl.DataFrame) -> pl.DataFrame:
    """Residuals of M1's published fit, checked against its published R².

    Recomputing the fit here would risk testing a *different* model from the one
    the site reports. Reconstructing it from the stored coefficients and then
    asserting that the implied R² reproduces the stored one to twelve decimals
    catches the failure that would otherwise be invisible: a column-order or
    name mismatch produces residuals that look completely plausible.
    """
    from twair.analysis.baseline import BASELINE_PREDICTORS, RESPONSE

    coefficients = {row["term"]: float(row["coefficient"]) for row in ols.iter_rows(named=True)}
    unknown = [term for term in coefficients if term not in (*BASELINE_PREDICTORS, "Intercept")]
    if unknown:
        raise ValueError(f"ols.parquet carries terms M6 does not know how to apply: {unknown}")

    fitted = pl.lit(coefficients["Intercept"], dtype=pl.Float64)
    for name in BASELINE_PREDICTORS:
        fitted = fitted + pl.col(name) * coefficients[name]

    out = panel.with_columns(
        fitted.alias("fitted"),
        (pl.col(RESPONSE) - fitted).alias(RESIDUAL),
    )

    observed = out[RESPONSE].to_numpy()
    resid = out[RESIDUAL].to_numpy()
    implied = 1.0 - float((resid**2).sum()) / float(((observed - observed.mean()) ** 2).sum())
    stored = as_float(ols["r_squared"][0])
    if stored is None or abs(implied - stored) > 1e-12:
        raise ValueError(
            f"reconstructed R² {implied!r} does not reproduce the stored {stored!r}; "
            "the coefficient table and the panel columns disagree"
        )
    return out


def placed_panel(panel: pl.DataFrame, conf: SpatialConf) -> pl.DataFrame:
    """Attach coordinates, county, today's zone and the panel era's zone.

    Raises when the register is empty. ``load_station_geo`` deliberately returns
    an empty frame with the right schema when its cache is missing, so that a
    join degrades to null coordinates rather than raising — correct for a map
    that wants to show an unplaceable station, and fatal here, because a Moran's
    I computed over zero stations would report success.

    ``zone_era`` is the seven-zone partition of the panel's own years, not the
    register's current eight. 離島空品區 did not exist as a zone before 2018;
    ``conf/stations.yaml`` records that override, and using today's partition on
    a 2010-2017 panel would be testing a map drawn after the fact.

    The geography is **resolved**, not the raw current register, for the same
    reason. The current register lists stations that exist *now*, so a station
    retired since the panel window vanishes from it — 萬里 measured through 2025
    and reports in all 96 panel months, and was dropped from every spatial
    statistic here purely because MOENV's register was fetched after it retired.
    Its coordinates are in this repository, reviewed and provenanced in
    ``conf/station_geo_historical.yaml``; ``resolve_station_geo`` fills only names
    the current register lacks and can never overwrite or alias one. Using
    today's register to decide who existed in 2010-2017 is the same error as
    using today's eight zones on the same panel.
    """
    from twair.ingest.station_meta import resolve_station_geo
    from twair.store.stations import build_station_table, normalise_name_expr

    register = resolve_station_geo()
    if register.height == 0:
        raise ValueError(
            "the station register cache is empty (conf/station_geo.yaml absent) — every "
            "coordinate would be null and every spatial statistic would be computed over "
            "an empty network; run `uv run twair stations geo` first"
        )

    era = build_station_table(geography=False).select(STATION, pl.col("airzone").alias("zone_era"))
    geo = register.select(
        STATION,
        "lat",
        "lon",
        "county",
        "township",
        "airzone_official",
        "station_type_official",
        # Carried through so the coverage ledger can say *on what authority* each
        # station is placed. One of them is not the current MOENV register, and a
        # reader comparing this network to the register should be able to see
        # which without leaving the parquet.
        "geo_source",
    )

    out = (
        panel.with_columns(normalise_name_expr().alias(STATION))
        .join(geo, on=STATION, how="left")
        .join(era, on=STATION, how="left")
    )
    # The era partition had no 離島 zone. A station the store places there is
    # outside the era's seven zones, so it gets an explicit label rather than a
    # null that a dummy-coder would silently drop.
    return out.with_columns(
        pl.when(pl.col("zone_era").is_in(list(conf.era_zones)))
        .then(pl.col("zone_era"))
        .otherwise(pl.lit("未分區"))
        .alias("zone_era"),
        pl.col(STATION).is_in(list(conf.offshore)).alias("offshore"),
    )


def station_coverage(
    panel: pl.DataFrame, conf: SpatialConf, *, root: Path | None = None
) -> pl.DataFrame:
    """The exclusion ledger: who is in the network being measured, and who is not.

    Printed before any statistic. Every number below it is conditional on this
    table, and a spatial result quoted without its network is not checkable.
    """
    complete = _complete_stations(panel)
    rows: list[dict[str, Any]] = []
    for name, group in panel.group_by(STATION):
        station = str(name[0] if isinstance(name, tuple) else name)
        lat = group["lat"][0]
        rows.append(
            {
                STATION: station,
                "months_in_panel": group.height,
                "placed": lat is not None,
                "offshore": bool(group["offshore"][0]),
                "lat": None if lat is None else float(lat),
                "lon": None if group["lon"][0] is None else float(group["lon"][0]),
                "geo_source": group["geo_source"][0] if "geo_source" in group.columns else None,
                "zone_era": str(group["zone_era"][0]),
                "airzone_official": group["airzone_official"][0],
                "complete_every_month": station in complete,
                "excluded_reason": (
                    "no coordinates in the MOENV register"
                    if lat is None
                    else None
                    if station in complete
                    else "not present in every panel month"
                ),
            }
        )
    return _attach_nearest_neighbour(pl.DataFrame(rows).sort(STATION))


def _attach_nearest_neighbour(coverage: pl.DataFrame) -> pl.DataFrame:
    """Great-circle km to the closest other main-island station.

    The choice of k-nearest weights over a distance band rests on this spacing —
    `conf/spatial.yaml` says so in prose — and nothing recomputed it, so the
    range it quotes could not go stale in a way anyone would notice. It is a
    property of the network, so it ships beside the network.

    Offshore stations are excluded from both sides: they are tens of kilometres
    of open water from anything, so including them would widen the range while
    describing a different question from the one the weights choice asks.
    """
    mainland = coverage.filter(pl.col("placed") & ~pl.col("offshore"))
    if mainland.height < 2:
        return coverage.with_columns(pl.lit(None, dtype=pl.Float64).alias("km_nearest_neighbour"))
    distance = pairwise_km(mainland["lat"].to_numpy(), mainland["lon"].to_numpy())
    np.fill_diagonal(distance, np.inf)
    nearest = pl.DataFrame(
        {
            STATION: mainland[STATION],
            "km_nearest_neighbour": distance.min(axis=1),
        }
    )
    return coverage.join(nearest, on=STATION, how="left")


def _complete_stations(panel: pl.DataFrame) -> set[str]:
    """Stations reporting in every month the panel covers.

    Needed because a fixed weights matrix must have a fixed station set: if W
    changes month to month, a trend in I is partly a measurement of the network
    rather than of the air. Nothing is filled to achieve this — incomplete
    stations are excluded and counted.
    """
    months = panel[MONTH].n_unique()
    counts = panel.group_by(STATION).agg(pl.col(MONTH).n_unique().alias("n"))
    return set(counts.filter(pl.col("n") == months)[STATION].to_list())


# --------------------------------------------------------------------------- #
# weights
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class WeightsBundle:
    """A row-standardised W plus the diagnostics needed to interpret an I from it."""

    dense: np.ndarray
    stations: tuple[str, ...]
    family: str
    parameter: float
    n_islands: int
    longest_link_km: float
    mean_link_km: float
    metric: str

    @property
    def n(self) -> int:
        return len(self.stations)

    def spec(self) -> str:
        return f"{self.family}({self.parameter:g}), {self.metric}, row-standardised"


def build_weights(
    coords: pl.DataFrame,
    *,
    family: WeightsFamily,
    parameter: float,
    conf: SpatialConf,
) -> WeightsBundle:
    """Row-standardised spatial weights over the stations in ``coords``.

    Distances are great-circle kilometres from :mod:`twair.geometry`, computed
    here rather than delegated. libpysal 4.15's ``KNN`` and ``DistanceBand``
    default to Euclidean distance on raw decimal degrees; at Taiwan's latitude a
    degree of longitude is about 0.90 of a degree of latitude, so that default
    stretches the map east-west and quietly changes both the neighbour sets and
    the isolate count. Doing the metric explicitly makes it a decision.
    """
    stations = tuple(coords[STATION].to_list())
    d = pairwise_km(coords["lat"].to_numpy(), coords["lon"].to_numpy())
    n = len(stations)
    w = np.zeros((n, n), dtype=float)

    if family == "knn":
        k = int(parameter)
        if k >= n:
            raise ValueError(f"k={k} needs more than {n} stations")
        masked = d.copy()
        np.fill_diagonal(masked, np.inf)
        for i in range(n):
            w[i, np.argsort(masked[i], kind="stable")[:k]] = 1.0
    elif family == "distance_band":
        w = ((d > 0.0) & (d <= float(parameter))).astype(float)
    else:  # pragma: no cover - Literal keeps this unreachable
        raise ValueError(f"unknown weights family {family!r}")

    degree = w.sum(axis=1)
    islands = int((degree == 0).sum())
    # An isolate contributes nothing to the numerator but would divide by zero
    # here. Its row stays all-zero and the count is reported, because a band that
    # isolates the east coast is a property of the band worth seeing.
    safe = np.where(degree == 0.0, 1.0, degree)
    w = w / safe[:, None]

    linked = d[w > 0.0]
    return WeightsBundle(
        dense=w,
        stations=stations,
        family=family,
        parameter=float(parameter),
        n_islands=islands,
        longest_link_km=float(linked.max()) if linked.size else float("nan"),
        mean_link_km=float(linked.mean()) if linked.size else float("nan"),
        metric=str(conf.weights["distance_metric"]),
    )


# --------------------------------------------------------------------------- #
# the statistic and its null
# --------------------------------------------------------------------------- #
def cliff_ord_moments(w_dense: np.ndarray, design: np.ndarray) -> tuple[float, float]:
    """E[I] and Var[I] for Moran's I computed on OLS residuals.

    With M = I - X(X'X)⁻¹X' and S0 = ΣΣw:

        E[I]   = (n/S0) · tr(MW) / (n-k)
        Var[I] = (n/S0)² · [tr(MWMW') + tr(MWMW) + tr(MW)²] / ((n-k)(n-k+2)) - E[I]²

    Hand-coded in numpy because ``spreg`` is not installed and M6 adds no
    dependency. The trace terms are short and easy to get subtly wrong, and a
    wrong Var[I] yields plausible z-scores — so a Monte-Carlo against the fitted
    null is the guard, and it lives in the tests.

    Var[I] is an approximation (it comes from treating a ratio of quadratic forms
    as a ratio of expectations) and it degrades with k/n. Measured against
    simulation it runs about 11% high at k/n ≈ 0.18, which is this panel's ratio,
    and worse above. It is therefore reported, not used as the test.
    """
    n, k = design.shape
    if w_dense.shape != (n, n):
        raise ValueError(f"W is {w_dense.shape} but the design has {n} rows")
    s0 = float(w_dense.sum())
    m = np.eye(n) - design @ np.linalg.solve(design.T @ design, design.T)
    mw = m @ w_dense

    tr_mw = float(np.trace(mw))
    tr_mwmw = float(np.trace(mw @ mw))
    tr_mwmwt = float(np.trace(mw @ mw.T))

    df = n - k
    if df <= 2:
        return float("nan"), float("nan")
    scale = n / s0
    expected = scale * tr_mw / df
    variance = scale**2 * (tr_mwmwt + tr_mwmw + tr_mw**2) / (df * (df + 2)) - expected**2
    return expected, variance


def _moran_statistic(values: np.ndarray, w_dense: np.ndarray) -> float:
    z = values - values.mean()
    denominator = float((z**2).sum())
    if denominator == 0.0:
        return float("nan")
    return len(z) * float(z @ (w_dense @ z)) / (float(w_dense.sum()) * denominator)


@dataclass(frozen=True, slots=True)
class MoranResult:
    """One Moran's I with everything needed to judge it."""

    i: float | None
    expected: float | None
    variance: float | None
    z: float | None
    p_simulated: float | None
    n: int
    draws: int
    refused: str | None = None


def morans_i(
    y: np.ndarray,
    bundle: WeightsBundle,
    *,
    rng: np.random.Generator,
    draws: int,
    design: np.ndarray | None = None,
    null_draws: np.ndarray | None = None,
    min_units: int = 8,
) -> MoranResult:
    """Moran's I with an inference procedure valid for the field it is given.

    ``design`` present means ``y`` is a residual vector: the moments come from
    :func:`cliff_ord_moments` and the p-value from a null simulated under the
    fitted model, supplied in ``null_draws`` (one column per draw, already
    projected through the residual maker by the caller so that every scope shares
    one set of draws). Absent, ``y`` is a raw field and the conditional
    permutation null applies.

    Returns ``None`` for the statistic with a reason rather than 0.0 when the
    field is constant or the network is too small. Zero is indistinguishable from
    "no spatial autocorrelation", which is exactly the finding at issue.
    """
    n = len(y)
    if n < min_units:
        return MoranResult(None, None, None, None, None, n, 0, f"fewer than {min_units} stations")
    if float(np.nanstd(y)) == 0.0:
        return MoranResult(None, None, None, None, None, n, 0, "the field is constant")

    observed = _moran_statistic(y, bundle.dense)

    expected: float | None = None
    variance: float | None = None
    if design is not None:
        expected, variance = cliff_ord_moments(bundle.dense, design)
    else:
        expected = -1.0 / (n - 1)

    if null_draws is not None:
        null = np.array(
            [_moran_statistic(null_draws[:, j], bundle.dense) for j in range(null_draws.shape[1])]
        )
    else:
        null = np.empty(draws)
        centred = y - y.mean()
        for j in range(draws):
            null[j] = _moran_statistic(rng.permutation(centred), bundle.dense)
        variance = float(null.var(ddof=1))

    null = null[np.isfinite(null)]
    reference = expected if expected is not None else 0.0
    # Two-sided against the null's own centre: the alternative here is "spatially
    # structured", and a negative I is as much a structure as a positive one.
    extreme = int((np.abs(null - reference) >= abs(observed - reference)).sum())
    p_simulated = (1 + extreme) / (1 + null.size) if null.size else None

    z: float | None = None
    if variance is not None and variance > 0 and expected is not None:
        z = (observed - expected) / float(np.sqrt(variance))

    return MoranResult(
        i=observed,
        expected=expected,
        variance=variance,
        z=z,
        p_simulated=p_simulated,
        n=n,
        draws=int(null.size),
    )


def residual_null_draws(design: np.ndarray, *, draws: int, rng: np.random.Generator) -> np.ndarray:
    """Residual vectors drawn under the fitted null: iid errors, same design.

    The cheap identity that makes this affordable: the residuals of a refit on
    simulated data are M(Xβ + ε) = Mε, because MX = 0. So "refit inside every
    draw" and "project an iid draw through the residual maker" are the same
    vector, and a draw costs one solve of a k×k system rather than a regression.

    Scale is irrelevant — I is invariant to it — so the errors are standard
    normal and no σ̂ is needed.
    """
    n = design.shape[0]
    gram = design.T @ design
    noise = rng.standard_normal((n, draws))
    out: np.ndarray = noise - design @ np.linalg.solve(gram, design.T @ noise)
    return out


# --------------------------------------------------------------------------- #
# multiplicity and intervals
# --------------------------------------------------------------------------- #
def benjamini_hochberg(p: np.ndarray, alpha: float) -> np.ndarray:
    """Step-up FDR. Returns the rejection mask.

    Every count this module publishes over months, stations or bins is a family
    of tests, not one. "71 of 96 months significant" is 96 statements, and the
    raw count is also Monte-Carlo unstable near the threshold — so the adjusted
    count leads everywhere and the raw one ships beside it.
    """
    finite = np.isfinite(p)
    out = np.zeros(len(p), dtype=bool)
    values = p[finite]
    if values.size == 0:
        return out
    order = np.argsort(values, kind="stable")
    ranked = values[order]
    m = len(ranked)
    thresholds = alpha * np.arange(1, m + 1) / m
    passing = np.flatnonzero(ranked <= thresholds)
    if passing.size == 0:
        return out
    cut = passing.max()
    rejected = np.zeros(m, dtype=bool)
    rejected[order[: cut + 1]] = True
    out[finite] = rejected
    return out


def block_bootstrap_mean(
    values: np.ndarray, *, draws: int, rng: np.random.Generator
) -> tuple[float, float, float]:
    """Mean of a per-month series with a percentile interval, resampling months.

    Whole months are the block because that is the unit the statistic is computed
    on and adjacent months share weather. The two most quotable numbers in this
    module are differences of means over months; neither may be printed bare.
    """
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return float("nan"), float("nan"), float("nan")
    means = np.array(
        [finite[rng.integers(0, finite.size, finite.size)].mean() for _ in range(draws)]
    )
    return float(finite.mean()), float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


# --------------------------------------------------------------------------- #
# scopes
# --------------------------------------------------------------------------- #
def _moran_over_draws(draws: np.ndarray, w_dense: np.ndarray) -> np.ndarray:
    """Moran's I for every column of ``draws`` at once.

    Vectorised because the null is evaluated once per (scope, month): a Python
    loop over 999 draws x 96 months x four scopes is minutes spent on nothing.
    """
    z = draws - draws.mean(axis=0, keepdims=True)
    wz = w_dense @ z
    numerator = (z * wz).sum(axis=0)
    denominator = (z**2).sum(axis=0)
    n = draws.shape[0]
    with np.errstate(divide="ignore", invalid="ignore"):
        out: np.ndarray = n * numerator / (float(w_dense.sum()) * denominator)
    return out


def _placed(
    frame: pl.DataFrame, value: str = RESIDUAL
) -> tuple[np.ndarray, pl.DataFrame, np.ndarray]:
    """Row indices, coordinates and values for the stations that can be placed."""
    subset = frame.filter(pl.col("lat").is_not_null())
    return (
        subset["_row"].to_numpy(),
        subset.select(STATION, "lat", "lon"),
        subset[value].to_numpy(),
    )


def _score(
    name: str,
    rows: np.ndarray,
    coords: pl.DataFrame,
    values: np.ndarray,
    *,
    conf: SpatialConf,
    null_draws: np.ndarray,
    family: str | None = None,
    parameter: float | None = None,
    averaged: np.ndarray | None = None,
) -> dict[str, Any]:
    """Moran's I for one cross-section, inferred against the simulated null.

    The null is simulated rather than closed-form, and the reason is a real
    subtlety rather than caution. Cliff-Ord's moments assume M is a projection.
    For a per-month cross-section of a *pooled* fit's residuals the relevant
    matrix is the (t,t) block of M, and **a sub-block of a projection is not a
    projection** — it is not idempotent, so the formula does not apply to it.
    Drawing under the fitted null and restricting to the month gives a vector
    with exactly the right covariance and assumes nothing. The closed form is
    kept for the case where it is genuinely valid, reported in its own column, so
    that the two can never be mistaken for each other.
    """
    n = len(values)
    if n < conf.min_units:
        return {
            "scope": name,
            "n_stations": n,
            "i": None,
            "refused": f"fewer than {conf.min_units} placed stations",
        }

    bundle = build_weights(
        coords,
        family=family or str(conf.weights["family"]),  # type: ignore[arg-type]
        parameter=parameter if parameter is not None else float(conf.weights["k"]),
        conf=conf,
    )
    observed = _moran_statistic(values, bundle.dense)
    null = _moran_over_draws(
        averaged if averaged is not None else null_draws[rows, :], bundle.dense
    )
    null = null[np.isfinite(null)]
    if null.size == 0:
        return {"scope": name, "n_stations": n, "i": observed, "refused": "the null degenerated"}

    expected = float(null.mean())
    sd = float(null.std(ddof=1)) if null.size > 1 else float("nan")
    extreme = int((np.abs(null - expected) >= abs(observed - expected)).sum())
    return {
        "scope": name,
        "n_stations": n,
        "i": observed,
        "expected_simulated": expected,
        "sd_simulated": sd,
        "z": (observed - expected) / sd if np.isfinite(sd) and sd > 0 else None,
        "p_simulated": (1 + extreme) / (1 + null.size),
        "null_draws": int(null.size),
        "weights": bundle.spec(),
        "n_islands": bundle.n_islands,
        "longest_link_km": bundle.longest_link_km,
        "refused": None,
    }


def _design_of(frame: pl.DataFrame) -> np.ndarray:
    """The baseline's design matrix, intercept first, in a fixed column order."""
    from twair.analysis.baseline import BASELINE_PREDICTORS

    return np.column_stack(
        [np.ones(frame.height)] + [frame[name].to_numpy() for name in BASELINE_PREDICTORS]
    )


def residual_autocorrelation(
    panel: pl.DataFrame, conf: SpatialConf, *, rng: np.random.Generator
) -> pl.DataFrame:
    """Residual Moran's I at four scopes, one row per (scope, month).

    ``per_month`` lets the weights matrix follow whichever stations reported that
    month; ``per_month_fixed_w`` holds the station set to those complete in every
    month. Both ship, because they answer different questions and their
    disagreeing is itself informative — a trend visible only in the first is a
    trend in the network, not in the air.

    ``station_mean`` and ``station_mean_main_island`` collapse to one value per
    station, restricted to the complete stations: the mean of three months and
    the mean of ninety-six are not exchangeable, and a null that treats them as
    such is measuring record length.
    """
    work = panel.with_row_index("_row")
    draws = residual_null_draws(
        _design_of(work), draws=int(conf.inference["residual_null_draws"]), rng=rng
    )
    complete = _complete_stations(panel)
    rows: list[dict[str, Any]] = []

    for month, group in work.group_by(MONTH, maintain_order=True):
        stamp = month[0] if isinstance(month, tuple) else month
        for name, subset in (
            ("per_month", group),
            ("per_month_fixed_w", group.filter(pl.col(STATION).is_in(list(complete)))),
        ):
            idx, coords, values = _placed(subset)
            record = _score(name, idx, coords, values, conf=conf, null_draws=draws)
            record[MONTH] = stamp
            rows.append(record)

    for name, keep_offshore in (("station_mean", True), ("station_mean_main_island", False)):
        subset = work.filter(pl.col(STATION).is_in(list(complete)))
        if not keep_offshore:
            subset = subset.filter(~pl.col("offshore"))
        placed = subset.filter(pl.col("lat").is_not_null())
        if placed.height == 0:
            continue
        grouped = placed.group_by(STATION, maintain_order=True).agg(
            pl.col(RESIDUAL).mean().alias(RESIDUAL),
            pl.col("lat").first(),
            pl.col("lon").first(),
            pl.col("_row").alias("member_rows"),
        )
        # The null is averaged the same way the observation is, so the comparison
        # is between two station means rather than between a mean and a single hour.
        averaged = np.vstack(
            [draws[np.asarray(r), :].mean(axis=0) for r in grouped["member_rows"].to_list()]
        )
        record = _score(
            name,
            np.array([], dtype=int),
            grouped.select(STATION, "lat", "lon"),
            grouped[RESIDUAL].to_numpy(),
            conf=conf,
            null_draws=draws,
            averaged=averaged,
        )
        record[MONTH] = None
        rows.append(record)

    return _attach_fdr(pl.DataFrame(rows, infer_schema_length=None), conf)


def _attach_fdr(frame: pl.DataFrame, conf: SpatialConf) -> pl.DataFrame:
    """Add a BH rejection column, computed inside each scope separately.

    Inside scope, because the family of tests is "the months of this scope".
    Pooling the scopes would make a family out of the same data looked at four
    different ways, which is not what FDR control is for.
    """
    if "p_simulated" not in frame.columns:
        return frame
    pieces = [
        group.with_columns(
            pl.Series(
                "significant_bh",
                benjamini_hochberg(
                    np.array(
                        [np.nan if v is None else float(v) for v in group["p_simulated"].to_list()]
                    ),
                    conf.fdr_alpha,
                ),
            )
        )
        for _, group in frame.group_by("scope", maintain_order=True)
    ]
    return pl.concat(pieces)


# --------------------------------------------------------------------------- #
# the correlogram — printed before any single I
# --------------------------------------------------------------------------- #
def moran_correlogram(
    panel: pl.DataFrame, conf: SpatialConf, *, rng: np.random.Generator
) -> pl.DataFrame:
    """Residual Moran's I by distance band, on the station-mean residual field.

    This is the primary spatial-dependence table and the CLI prints it first. A
    single I is a weighted blend over every distance the weights matrix spans; if
    the correlogram changes sign at mid range then that blend is an average of
    opposite-signed things and quoting it alone is misleading. It also decides
    whether a monotone spherical variogram is even the right model for the field
    step, so it has to exist before the field does.
    """
    work = panel.with_row_index("_row")
    draws = residual_null_draws(
        _design_of(work), draws=int(conf.inference["residual_null_draws"]), rng=rng
    )
    complete = _complete_stations(panel)
    placed = work.filter(pl.col(STATION).is_in(list(complete))).filter(pl.col("lat").is_not_null())
    grouped = placed.group_by(STATION, maintain_order=True).agg(
        pl.col(RESIDUAL).mean().alias(RESIDUAL),
        pl.col("lat").first(),
        pl.col("lon").first(),
        pl.col("_row").alias("member_rows"),
    )
    values = grouped[RESIDUAL].to_numpy()
    averaged = np.vstack(
        [draws[np.asarray(r), :].mean(axis=0) for r in grouped["member_rows"].to_list()]
    )
    d = pairwise_km(grouped["lat"].to_numpy(), grouped["lon"].to_numpy())

    edges = [float(e) for e in conf.correlogram["bins_km"]]
    rows: list[dict[str, Any]] = []
    for lo, hi in itertools.pairwise(edges):
        mask = (d > lo) & (d <= hi)
        pairs = int(mask.sum() // 2)
        degree = mask.sum(axis=1)
        if pairs == 0:
            rows.append(
                {
                    "bin_lo_km": lo,
                    "bin_hi_km": hi,
                    "pairs": 0,
                    "i": None,
                    "refused": "no station pairs fall in this band",
                }
            )
            continue
        w = mask.astype(float)
        w = w / np.where(degree == 0, 1.0, degree)[:, None]
        observed = _moran_statistic(values, w)
        null = _moran_over_draws(averaged, w)
        null = null[np.isfinite(null)]
        expected = float(null.mean())
        sd = float(null.std(ddof=1))
        extreme = int((np.abs(null - expected) >= abs(observed - expected)).sum())
        rows.append(
            {
                "bin_lo_km": lo,
                "bin_hi_km": hi,
                "pairs": pairs,
                "stations_with_a_neighbour": int((degree > 0).sum()),
                "isolates": int((degree == 0).sum()),
                "i": observed,
                "expected_simulated": expected,
                "sd_simulated": sd,
                "z": (observed - expected) / sd if sd > 0 else None,
                "p_simulated": (1 + extreme) / (1 + null.size),
                "refused": None,
            }
        )

    out = pl.DataFrame(rows, infer_schema_length=None)
    p = np.array([np.nan if v is None else float(v) for v in out["p_simulated"].to_list()])
    return out.with_columns(pl.Series("significant_bh", benjamini_hochberg(p, conf.fdr_alpha)))


# --------------------------------------------------------------------------- #
# the headline
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class _Fit:
    """An OLS fit reported by what it can and cannot support."""

    residuals: np.ndarray
    design: np.ndarray
    rank: int
    columns: int
    condition: float
    r_squared: float
    adjusted_r_squared: float
    rank_deficient: bool


def _least_squares(design: np.ndarray, y: np.ndarray) -> _Fit:
    """Least squares by SVD, so a rank-deficient design returns a fit and a warning.

    NO, NO2 and NOx enter the baseline's specification together and NO + NO2 ≡
    NOx, so the pooled design is singular in all but rounding error and a
    within-zone design over three stations is worse. `lstsq` gives the minimum-norm
    solution, which leaves the *residuals* — the only thing this module reads —
    well defined even where the coefficients are not. The rank and the condition
    number ship so that nobody quotes a coefficient from such a fit.
    """
    solution, _, rank, singular = np.linalg.lstsq(design, y, rcond=None)
    fitted = design @ solution
    residuals = y - fitted
    ss_res = float((residuals**2).sum())
    ss_tot = float(((y - y.mean()) ** 2).sum())
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan")
    n = len(y)
    df = n - int(rank)
    adjusted = 1.0 - (1.0 - r2) * (n - 1) / df if df > 0 else float("nan")
    positive = singular[singular > 0]
    condition = float(positive.max() / positive.min()) if positive.size else float("inf")
    return _Fit(
        residuals=residuals,
        design=design,
        rank=int(rank),
        columns=design.shape[1],
        condition=condition,
        r_squared=r2,
        adjusted_r_squared=adjusted,
        rank_deficient=int(rank) < design.shape[1],
    )


def _dummies(labels: list[str], *, drop_first: bool) -> tuple[np.ndarray, list[str]]:
    """Indicator columns for a categorical control.

    ``drop_first`` only when an intercept is present. Nothing is ever dropped for
    being *unknown*: a station whose era zone cannot be determined gets its own
    indicator, because dropping it would silently remove it from the comparison.
    """
    levels = sorted(set(labels))
    used = levels[1:] if drop_first else levels
    matrix = np.column_stack([np.array([1.0 if v == lvl else 0.0 for v in labels]) for lvl in used])
    return matrix, used


def partition_price(
    panel: pl.DataFrame, conf: SpatialConf, *, rng: np.random.Generator
) -> pl.DataFrame:
    """THE HEADLINE: how much spatial dependence each control actually removes.

    Five controls on the baseline's own design, in increasing strength:

    ``pooled``
        the fit exactly as M1 publishes it — no spatial control at all.
    ``zone_era_dummies``
        the seven zones of the panel's own years as intercept shifts.
    ``zone_official_today``
        the register's current zones, run so the anachronism is *visible* rather
        than chosen. It is not the headline.
    ``within_zone_separate_fits``
        the literal 「分區各跑一次」: every coefficient free to differ by zone,
        residuals pooled afterwards. Expressed as one design — zone indicators
        interacted with the full predictor set — because separate fits and a
        fully interacted fit produce the same residuals, and one design keeps the
        null machinery identical across controls.
    ``station_dummies``
        a fixed effect per station: far stronger than the zone stratification,
        included as the ceiling. If dependence survives even this, no amount of
        stratifying by area would have fixed it.

    Reported as "mean I fell from X to Y, with an interval". There is deliberately
    no ``share_of_dependence_removed`` column: I is a normalised correlation, it
    has no decomposition property, and a ratio of two I values would look like a
    variance share while being nothing of the kind.
    """
    from twair.analysis.baseline import RESPONSE

    work = panel.with_row_index("_row")
    y = work[RESPONSE].to_numpy()
    base = _design_of(work)
    zone_era, _ = _dummies(work["zone_era"].to_list(), drop_first=True)
    zone_now, _ = _dummies(
        [v if v is not None else "未登錄" for v in work["airzone_official"].to_list()],
        drop_first=True,
    )
    station, _ = _dummies(work[STATION].to_list(), drop_first=True)
    era_labels = work["zone_era"].to_list()
    interacted = np.column_stack(
        [
            base * np.array([1.0 if v == lvl else 0.0 for v in era_labels])[:, None]
            for lvl in sorted(set(era_labels))
        ]
    )

    controls: list[tuple[str, np.ndarray]] = [
        ("pooled", base),
        ("zone_era_dummies", np.column_stack([base, zone_era])),
        ("zone_official_today", np.column_stack([base, zone_now])),
        ("within_zone_separate_fits", interacted),
        ("station_dummies", np.column_stack([base, station])),
    ]

    complete = _complete_stations(panel)
    draws_wanted = int(conf.inference["residual_null_draws"])
    rows: list[dict[str, Any]] = []

    for name, design in controls:
        fit = _least_squares(design, y)
        draws = residual_null_draws(design, draws=draws_wanted, rng=rng)
        scored = work.with_columns(pl.Series(RESIDUAL, fit.residuals))

        per_month: list[dict[str, Any]] = []
        for month, group in scored.group_by(MONTH, maintain_order=True):
            subset = group.filter(pl.col(STATION).is_in(list(complete)))
            idx, coords, values = _placed(subset)
            record = _score(name, idx, coords, values, conf=conf, null_draws=draws)
            record[MONTH] = month[0] if isinstance(month, tuple) else month
            per_month.append(record)

        i_values = np.array([np.nan if r.get("i") is None else float(r["i"]) for r in per_month])
        p_values = np.array(
            [np.nan if r.get("p_simulated") is None else float(r["p_simulated"]) for r in per_month]
        )
        mean_i, lo, hi = block_bootstrap_mean(
            i_values, draws=int(conf.inference["block_bootstrap_draws"]), rng=rng
        )
        rejected = benjamini_hochberg(p_values, conf.fdr_alpha)
        rows.append(
            {
                "control": name,
                "n_obs": len(y),
                "design_columns": fit.columns,
                "rank": fit.rank,
                "rank_deficient": fit.rank_deficient,
                "condition_number": fit.condition,
                "r_squared": fit.r_squared,
                "adjusted_r_squared": fit.adjusted_r_squared,
                "months_scored": int(np.isfinite(i_values).sum()),
                "mean_i": mean_i,
                "mean_i_lo": lo,
                "mean_i_hi": hi,
                "median_i": float(np.nanmedian(i_values)),
                "months_significant_bh": int(rejected.sum()),
                "months_significant_raw": int(np.nansum(p_values < conf.fdr_alpha)),
                "coefficients_reportable": not fit.rank_deficient,
            }
        )

    return pl.DataFrame(rows, infer_schema_length=None)


def weights_sensitivity(
    panel: pl.DataFrame, conf: SpatialConf, *, rng: np.random.Generator
) -> pl.DataFrame:
    """The same residual field under every weights definition in the grid.

    Without this the headline's magnitude reads as a property of the air. It is
    not: it is a property of the air *and* of a matrix somebody chose. The grid is
    what stops a single k being quoted as if the number were k-free.
    """
    work = panel.with_row_index("_row")
    draws = residual_null_draws(
        _design_of(work), draws=int(conf.inference["residual_null_draws"]), rng=rng
    )
    complete = _complete_stations(panel)
    rows: list[dict[str, Any]] = []

    for main_island_only in (False, True):
        subset = work.filter(pl.col(STATION).is_in(list(complete)))
        if main_island_only:
            subset = subset.filter(~pl.col("offshore"))
        placed = subset.filter(pl.col("lat").is_not_null())
        if placed.height == 0:
            continue
        grouped = placed.group_by(STATION, maintain_order=True).agg(
            pl.col(RESIDUAL).mean().alias(RESIDUAL),
            pl.col("lat").first(),
            pl.col("lon").first(),
            pl.col("_row").alias("member_rows"),
        )
        averaged = np.vstack(
            [draws[np.asarray(r), :].mean(axis=0) for r in grouped["member_rows"].to_list()]
        )
        coords = grouped.select(STATION, "lat", "lon")
        values = grouped[RESIDUAL].to_numpy()

        grid: list[tuple[str, float]] = [("knn", float(k)) for k in conf.weights["knn_grid"]] + [
            ("distance_band", float(km)) for km in conf.weights["distance_band_grid_km"]
        ]

        for family, parameter in grid:
            if family == "knn" and int(parameter) >= len(values):
                continue
            record = _score(
                "station_mean",
                np.array([], dtype=int),
                coords,
                values,
                conf=conf,
                null_draws=draws,
                family=family,
                parameter=parameter,
                averaged=averaged,
            )
            record["family"] = family
            record["parameter"] = parameter
            record["main_island_only"] = main_island_only
            rows.append(record)

    # `_score` returns four keys when it refuses and twelve when it succeeds, and
    # `infer_schema_length=None` unions whatever it is given — so this frame's
    # SCHEMA depends on whether any weighting produced a result. If every one
    # refused, `n_islands` and `z` are simply absent, and `cli.py` selects them
    # unconditionally: ColumnNotFoundError after `field_skill`'s kriging and five
    # rounds of 999 residual null draws, with nothing written.
    #
    # Reachable: `run_spatial`'s guard counts stations that have COORDINATES,
    # while every scope here needs stations complete in every month. Reproduced
    # on a synthetic panel with 40 placed and 6 complete — the guard passes, this
    # returns 7 columns, and the CLI raises. At 5 or fewer `lisa` fails earlier
    # inside `run_spatial`, so the window is exactly 6 and 7.
    #
    # `_attach_fdr` already guards the same hazard with
    # `if "p_simulated" not in frame.columns`. This is that guard, stated as the
    # schema rather than discovered by a caller.
    frame = pl.DataFrame(rows, infer_schema_length=None)
    for column in ("i", "z", "n_islands", "p_simulated"):
        if column not in frame.columns:
            frame = frame.with_columns(pl.lit(None, dtype=pl.Float64).alias(column))
    return frame


# --------------------------------------------------------------------------- #
# orchestration
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class SpatialResult:
    """Every table M6 produced, plus the run's own parameters."""

    metadata: pl.DataFrame
    coverage: pl.DataFrame
    correlogram: pl.DataFrame
    residual: pl.DataFrame
    partition: pl.DataFrame
    sensitivity: pl.DataFrame | None = None
    agreement: pl.DataFrame | None = None
    clusters: pl.DataFrame | None = None
    lisa: pl.DataFrame | None = None
    inference: pl.DataFrame | None = None
    field: pl.DataFrame | None = None

    def tables(self) -> dict[str, pl.DataFrame]:
        out = {
            "metadata": self.metadata,
            "station_coverage": self.coverage,
            "correlogram": self.correlogram,
            "residual_autocorrelation": self.residual,
            "partition_price": self.partition,
        }
        if self.sensitivity is not None:
            out["weights_sensitivity"] = self.sensitivity
        if self.agreement is not None:
            out["partition_agreement"] = self.agreement
        if self.clusters is not None:
            out["station_clusters"] = self.clusters
        if self.lisa is not None:
            out["lisa"] = self.lisa
        if self.inference is not None:
            out["inference_price"] = self.inference
        if self.field is not None:
            out["field_skill"] = self.field
        return out


def run_spatial(
    root: Path | None = None,
    *,
    conf: SpatialConf | None = None,
    steps: tuple[str, ...] = STEPS,
) -> SpatialResult:
    """Run M6, threading one seeded generator through every consumer."""
    settings = conf or load_spatial_conf()
    rng = np.random.default_rng(settings.seed)

    panel_raw, ols = load_m1_artefacts(root)
    residuals = reconstruct_residuals(panel_raw, ols)
    panel = placed_panel(residuals, settings)

    placed_count = panel.filter(pl.col("lat").is_not_null())[STATION].n_unique()
    if placed_count < settings.min_units:
        raise ValueError(
            f"only {placed_count} of {panel[STATION].n_unique()} panel stations can be placed; "
            f"conf/spatial.yaml requires at least {settings.min_units}"
        )

    metadata = pl.DataFrame(
        {
            "key": [
                "seed",
                "earth_radius_km",
                "residual_null_draws",
                "block_bootstrap_draws",
                "weights",
                "zone_partition",
                "panel_months",
                "panel_stations",
                "panel_stations_placed",
                "panel_stations_complete",
                "r_squared_pooled",
            ],
            "value": [
                str(settings.seed),
                str(EARTH_RADIUS_KM),
                str(settings.inference["residual_null_draws"]),
                str(settings.inference["block_bootstrap_draws"]),
                f"{settings.weights['family']}({settings.weights['k']})",
                str(settings.panel["zone_partition"]),
                str(panel[MONTH].n_unique()),
                str(panel[STATION].n_unique()),
                str(placed_count),
                str(len(_complete_stations(panel))),
                str(as_float(ols["r_squared"][0])),
            ],
        }
    )

    coverage = station_coverage(panel, settings, root=root)
    log.info(
        "M6 panel: %d station-months, %d stations, %d placed, %d complete in every month",
        panel.height,
        panel[STATION].n_unique(),
        placed_count,
        len(_complete_stations(panel)),
    )

    correlogram = (
        moran_correlogram(panel, settings, rng=rng) if "headline" in steps else pl.DataFrame()
    )
    residual = (
        residual_autocorrelation(panel, settings, rng=rng)
        if "headline" in steps
        else pl.DataFrame()
    )
    partition = partition_price(panel, settings, rng=rng) if "headline" in steps else pl.DataFrame()
    sensitivity = weights_sensitivity(panel, settings, rng=rng) if "sensitivity" in steps else None

    agreement: pl.DataFrame | None = None
    clusters: pl.DataFrame | None = None
    if "partition" in steps:
        bundle = station_dissimilarity(settings, root=root)
        agreement, clusters = partition_agreement(bundle, settings, rng=rng)
        # Appended rather than built above, because these are measured by the
        # partition step and metadata must not claim them when it did not run.
        metadata = pl.concat(
            [
                metadata,
                pl.DataFrame(
                    {
                        "key": [
                            "clustering_stations_kept",
                            "clustering_months_used",
                            "station_correlation_raw",
                            "station_correlation_anomaly",
                        ],
                        "value": [
                            str(len(bundle.stations)),
                            str(bundle.months_used),
                            str(bundle.raw_correlation),
                            str(bundle.anomaly_correlation),
                        ],
                    }
                ),
            ]
        )

    lisa_table = lisa(panel, settings, rng=rng) if "lisa" in steps else None
    inference = inference_price(panel, settings) if "inference" in steps else None
    field = field_skill(settings, root=root, rng=rng) if "field" in steps else None

    return SpatialResult(
        metadata=metadata,
        coverage=coverage,
        correlogram=correlogram,
        residual=residual,
        partition=partition,
        sensitivity=sensitivity,
        agreement=agreement,
        clusters=clusters,
        lisa=lisa_table,
        inference=inference,
        field=field,
    )


def write_spatial_report(result: SpatialResult) -> dict[str, Path]:
    """Persist every table as zstd parquet and return name -> path."""
    base = outputs_dir("m6_spatial")
    base.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    for name, frame in result.tables().items():
        if frame.height == 0:
            continue
        path = base / f"{name}.parquet"
        frame.write_parquet(path, compression="zstd")
        written[name] = path
    return written


# --------------------------------------------------------------------------- #
# the partition test — does the official zoning match how stations co-vary?
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class DissimilarityBundle:
    """Everything the partition test consumes, built once.

    ``anomaly`` is stations × months, each row z-scored. ``distance`` is the
    correlation distance 1 − r between those rows. The two are the same
    information twice on purpose: Ward linkage needs Euclidean geometry, and on
    z-scored rows ‖zᵢ − zⱼ‖² = 2T(1 − rᵢⱼ), so Ward on the rows *is* Ward under
    the correlation distance — while silhouette and the separation statistic
    read the distance matrix directly.

    ``months_used`` is the count of *common* months — those every kept station
    published — and ``months_window`` the window's full count. The difference is
    part of the result, not a footnote: every correlation in ``distance`` is
    computed on exactly the same months, which is what makes it one metric.
    """

    stations: tuple[str, ...]
    coords: pl.DataFrame
    labels_era: tuple[str, ...]
    anomaly: np.ndarray
    distance: np.ndarray
    months_used: int
    months_window: int
    excluded: pl.DataFrame
    raw_correlation: float
    anomaly_correlation: float


def _mean_off_diagonal_correlation(series: np.ndarray) -> float:
    """Mean pairwise correlation between station series, diagonal excluded."""
    correlation = np.asarray(np.corrcoef(series))
    off_diagonal = ~np.eye(correlation.shape[0], dtype=bool)
    return float(correlation[off_diagonal].mean())


def station_dissimilarity(conf: SpatialConf, *, root: Path | None = None) -> DissimilarityBundle:
    """Monthly PM2.5 anomalies per station over the clustering window.

    Reads the monthly aggregate table, whose withheld means are already null —
    those stay excluded here, never filled. Demanding a published mean in every
    window month keeps 6 stations (measured), which is no network; so a station
    enters if it publishes in at least ``min_station_share`` of the window, and
    the correlations are then computed on the months **every** kept station
    published. Both cuts are counted — stations in ``excluded`` with the reason,
    months in ``months_used`` against ``months_window`` — because a distance
    computed on a different month set per pair would not be one metric.

    The island-wide monthly mean is removed first. Without that step every
    station in Taiwan correlates at ~0.7 through the shared winter maximum and
    the test would find all partitions equally good — an artefact of the
    monsoon, not evidence about zoning.
    """
    from twair.ingest.station_meta import resolve_station_geo
    from twair.paths import processed_dir
    from twair.store.stations import build_station_table

    base = processed_dir("monthly") if root is None else root / "processed" / "monthly"
    start = str(conf.clustering["window_start"])
    end = str(conf.clustering["window_end"])
    monthly = (
        pl.scan_parquet(f"{base}/**/*.parquet")
        .filter(
            (pl.col("pollutant") == "PM2.5")
            & (pl.col(MONTH).dt.strftime("%Y-%m") >= start)
            & (pl.col(MONTH).dt.strftime("%Y-%m") <= end)
        )
        .select(STATION, MONTH, "mean")
        .collect()
    )
    if monthly.height == 0:
        raise FileNotFoundError(
            f"no monthly PM2.5 rows under {base} for {start}..{end} — run `uv run twair aggregate`"
        )

    months = sorted(monthly[MONTH].unique().to_list())
    published = monthly.filter(pl.col("mean").is_not_null())
    counts = published.group_by(STATION).agg(pl.len().alias("published_months"))
    required = float(conf.clustering["min_station_share"]) * len(months)

    # Resolved, matching `placed_panel` — the two must describe the same network
    # or the partition test scores a different set of stations from the headline.
    register = resolve_station_geo()
    if register.height == 0:
        raise ValueError(
            "the station register cache is empty — the ensemble null is geographic and "
            "needs coordinates; run `uv run twair stations geo` first"
        )
    geo = register.select(STATION, "lat", "lon")
    era = build_station_table(root, geography=False).select(
        STATION, pl.col("airzone").alias("zone_era")
    )

    ledger = (
        counts.join(geo, on=STATION, how="left")
        .join(era, on=STATION, how="left")
        .with_columns(
            pl.when(pl.col("published_months") < required)
            .then(
                pl.lit(
                    "published in too few window months (mean withheld below the "
                    "coverage threshold, or the station was not measuring)"
                )
            )
            .when(pl.col("lat").is_null())
            .then(pl.lit("no coordinates in the MOENV register"))
            .otherwise(pl.lit(None, dtype=pl.Utf8))
            .alias("excluded_reason")
        )
    )
    kept = ledger.filter(pl.col("excluded_reason").is_null()).sort(STATION)
    excluded = ledger.filter(pl.col("excluded_reason").is_not_null()).sort(STATION)
    if kept.height < conf.min_units:
        raise ValueError(
            f"only {kept.height} stations publish in {required:.0f}+ months of "
            f"{start}..{end}; the partition test needs at least {conf.min_units}"
        )

    wide = (
        published.filter(pl.col(STATION).is_in(kept[STATION].to_list()))
        .pivot(on=STATION, index=MONTH, values="mean")
        .sort(MONTH)
    )
    names = tuple(sorted(c for c in wide.columns if c != MONTH))
    full = wide.select(names).to_numpy().T  # stations × window months
    common = ~np.isnan(full).any(axis=0)
    matrix = full[:, common]
    if matrix.shape[1] < 24:
        raise ValueError(
            f"only {matrix.shape[1]} months are shared by every kept station — "
            "too few for a correlation anyone should trust"
        )

    # Measured on both sides of the switch below, because the switch is the
    # methodological claim: on raw series every station in Taiwan correlates
    # with every other one at roughly the same high value, which is the monsoon
    # and not a partition anyone drew. `conf/spatial.yaml` states that in a
    # comment; these two numbers are the same statement, recomputed each run.
    raw_correlation = _mean_off_diagonal_correlation(matrix)
    if bool(conf.clustering["remove_island_climatology"]):
        matrix = matrix - matrix.mean(axis=0, keepdims=True)
    anomaly_correlation = _mean_off_diagonal_correlation(matrix)

    # Z-score each station's series so that Ward-on-rows equals Ward under the
    # correlation distance (see the dataclass docstring for the identity).
    centred = matrix - matrix.mean(axis=1, keepdims=True)
    scale = centred.std(axis=1, keepdims=True)
    if (scale == 0).any():
        flat = [names[i] for i in np.flatnonzero(scale[:, 0] == 0)]
        raise ValueError(f"station(s) with a constant anomaly series: {flat}")
    z = centred / scale
    distance: np.ndarray = np.asarray(1.0 - np.corrcoef(z))
    np.fill_diagonal(distance, 0.0)

    order = {str(row[STATION]): row for row in kept.iter_rows(named=True)}
    labels = tuple(
        str(order[name]["zone_era"]) if order[name]["zone_era"] in conf.era_zones else "未分區"
        for name in names
    )
    coords = pl.DataFrame(
        {
            STATION: list(names),
            "lat": [float(order[n]["lat"]) for n in names],
            "lon": [float(order[n]["lon"]) for n in names],
        }
    )
    return DissimilarityBundle(
        stations=names,
        coords=coords,
        labels_era=labels,
        anomaly=z,
        distance=distance,
        months_used=int(matrix.shape[1]),
        months_window=len(months),
        excluded=excluded,
        raw_correlation=raw_correlation,
        anomaly_correlation=anomaly_correlation,
    )


def geographic_ensemble(
    coords: pl.DataFrame, *, k: int, draws: int, rng: np.random.Generator
) -> np.ndarray:
    """``draws`` geography-only partitions of the stations into ``k`` groups.

    Each draw seeds ``k`` stations at random and assigns every station to its
    nearest seed — a random Voronoi partition, contiguous by construction. That
    is the null the official zoning has to beat: a random *relabelling* would be
    beaten by any contiguous partition whatsoever (measured on this data the
    official zones beat relabelling at p = 0.001, which demonstrates nothing),
    so the reference class is partitions that also know the geography and know
    nothing else.
    """
    d = pairwise_km(coords["lat"].to_numpy(), coords["lon"].to_numpy())
    n = d.shape[0]
    out = np.empty((draws, n), dtype=np.int64)
    for j in range(draws):
        seeds = rng.choice(n, size=k, replace=False)
        out[j] = np.argmin(d[:, seeds], axis=1)
    return out


def _separation(distance: np.ndarray, labels: np.ndarray) -> float:
    """Mean within-group correlation minus mean between-group correlation.

    On the correlation distance this is (mean between-distance − mean
    within-distance); reported on the correlation scale because 「區內平均相關
    比區間高多少」 is the sentence a reader can check.
    """
    same = labels[:, None] == labels[None, :]
    off = ~np.eye(len(labels), dtype=bool)
    within = distance[same & off]
    between = distance[~same]
    if within.size == 0 or between.size == 0:
        return float("nan")
    return float(between.mean() - within.mean())


def partition_agreement(
    bundle: DissimilarityBundle, conf: SpatialConf, *, rng: np.random.Generator
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Score the era partition against geography-only nulls and Ward's tree.

    PLAN.md predicted the official zoning is 「很可能不合理」 and called that a
    good finding. A prediction that cannot fail is not a finding, so both
    directions are live: the partition's percentile in the Voronoi ensemble says
    whether it beats geography-alone, and the Ward sweep over ``k_grid`` says
    what structure the data prefers, including a k nowhere near the official
    count. Whichever way it comes out is the result.
    """
    from sklearn.cluster import AgglomerativeClustering
    from sklearn.metrics import (
        adjusted_rand_score,
        normalized_mutual_info_score,
        silhouette_score,
    )

    labels_era = np.array(bundle.labels_era)
    era_groups = len(set(bundle.labels_era))
    sil_era = float(silhouette_score(bundle.distance, labels_era, metric="precomputed"))
    sep_era = _separation(bundle.distance, labels_era)

    draws = int(conf.clustering["ensemble_draws"])
    ensemble = geographic_ensemble(bundle.coords, k=era_groups, draws=draws, rng=rng)
    sil_null = np.empty(draws)
    sep_null = np.empty(draws)
    for j in range(draws):
        labels = ensemble[j]
        # A draw can produce a group with one station; silhouette treats a
        # singleton as 0, which only handicaps the null, never the official row.
        sil_null[j] = silhouette_score(bundle.distance, labels, metric="precomputed")
        sep_null[j] = _separation(bundle.distance, labels)

    def percentile(observed: float, null: np.ndarray) -> float:
        return float((null < observed).mean())

    rows: list[dict[str, Any]] = [
        {
            "partition": "zone_era",
            "k": era_groups,
            "silhouette": sil_era,
            "separation_r": sep_era,
            "pct_vs_geographic_ensemble_silhouette": percentile(sil_era, sil_null),
            "pct_vs_geographic_ensemble_separation": percentile(sep_era, sep_null),
            "ensemble_draws": draws,
            "ari_vs_zone_era": 1.0,
            "nmi_vs_zone_era": 1.0,
        }
    ]

    cluster_rows: list[dict[str, Any]] = []
    ward_at_era_k: np.ndarray | None = None
    for k in (int(v) for v in conf.clustering["k_grid"]):
        if k >= len(bundle.stations):
            continue
        found = AgglomerativeClustering(n_clusters=k, linkage="ward").fit_predict(bundle.anomaly)
        rows.append(
            {
                "partition": f"ward_k{k}",
                "k": k,
                "silhouette": float(silhouette_score(bundle.distance, found, metric="precomputed")),
                "separation_r": _separation(bundle.distance, found),
                "pct_vs_geographic_ensemble_silhouette": None,
                "pct_vs_geographic_ensemble_separation": None,
                "ensemble_draws": 0,
                "ari_vs_zone_era": float(adjusted_rand_score(labels_era, found)),
                "nmi_vs_zone_era": float(normalized_mutual_info_score(labels_era, found)),
            }
        )
        if k == era_groups:
            ward_at_era_k = found
        for station, zone, cluster in zip(bundle.stations, labels_era, found, strict=True):
            cluster_rows.append(
                {STATION: station, "k": k, "cluster": int(cluster), "zone_era": zone}
            )

    agreement = pl.DataFrame(rows, infer_schema_length=None)
    if ward_at_era_k is not None:
        log.info(
            "ward at the era k agrees with the official partition at ARI %.3f",
            adjusted_rand_score(labels_era, ward_at_era_k),
        )
    return agreement, pl.DataFrame(cluster_rows, infer_schema_length=None)


# --------------------------------------------------------------------------- #
# LISA — where the residual dependence lives
# --------------------------------------------------------------------------- #
def lisa(panel: pl.DataFrame, conf: SpatialConf, *, rng: np.random.Generator) -> pl.DataFrame:
    """Local Moran's I per station on the station-mean residual field.

    The global I says the pooled model's errors cluster; this says *where*. Same
    machinery as the global statistic on purpose — the same weights bundle and
    the same simulated null, restricted to each station — so the local map can
    never disagree with the global number about what the null is. esda's
    conditional permutation is not used for the same reason the global
    permutation was rejected: residuals are not exchangeable.

    Quadrants read as usual: HH means a station whose residual is high where its
    neighbours' are high (the model under-predicts a whole area), LL the mirror,
    HL/LH a station out of step with its surroundings. The published count is
    BH-adjusted across the stations; the raw count ships beside it.
    """
    work = panel.with_row_index("_row")
    draws = residual_null_draws(
        _design_of(work), draws=int(conf.inference["residual_null_draws"]), rng=rng
    )
    complete = _complete_stations(panel)
    placed = work.filter(pl.col(STATION).is_in(list(complete))).filter(pl.col("lat").is_not_null())
    grouped = placed.group_by(STATION, maintain_order=True).agg(
        pl.col(RESIDUAL).mean().alias(RESIDUAL),
        pl.col("lat").first(),
        pl.col("lon").first(),
        pl.col("zone_era").first(),
        pl.col("airzone_official").first(),
        pl.col("station_type_official").first(),
        pl.col("offshore").first(),
        pl.col("_row").alias("member_rows"),
    )
    values = grouped[RESIDUAL].to_numpy()
    averaged = np.vstack(
        [draws[np.asarray(r), :].mean(axis=0) for r in grouped["member_rows"].to_list()]
    )
    bundle = build_weights(
        grouped.select(STATION, "lat", "lon"),
        family=str(conf.weights["family"]),  # type: ignore[arg-type]
        parameter=float(conf.weights["k"]),
        conf=conf,
    )

    def local_i(field: np.ndarray) -> np.ndarray:
        z = field - field.mean()
        denominator = float((z**2).sum()) / len(z)
        lag: np.ndarray = bundle.dense @ z
        out: np.ndarray = z * lag / denominator
        return out

    observed = local_i(values)
    null = np.column_stack([local_i(averaged[:, j]) for j in range(averaged.shape[1])])
    centre = null.mean(axis=1)
    # Two-sided per station against its own null distribution: each station has
    # a different neighbour count and sits in a different corner of the design,
    # so one pooled null would be wrong for most of them.
    extreme = (np.abs(null - centre[:, None]) >= np.abs(observed - centre)[:, None]).sum(axis=1)
    p = (1 + extreme) / (1 + null.shape[1])
    rejected = benjamini_hochberg(p, conf.fdr_alpha)

    z_field = values - values.mean()
    lag = bundle.dense @ z_field
    quadrant = np.where(
        z_field >= 0, np.where(lag >= 0, "HH", "HL"), np.where(lag >= 0, "LH", "LL")
    )

    return grouped.drop("member_rows").with_columns(
        pl.Series("local_i", observed),
        pl.Series("p_simulated", p),
        pl.Series("significant_bh", rejected),
        pl.Series("significant_raw", p < conf.fdr_alpha),
        pl.Series("quadrant", quadrant),
        pl.lit(bundle.spec()).alias("weights"),
    )


# --------------------------------------------------------------------------- #
# the inference price — the baseline's own t-statistics, re-priced
# --------------------------------------------------------------------------- #
def inference_price(panel: pl.DataFrame, conf: SpatialConf) -> pl.DataFrame:
    """The pooled fit's standard errors under covariances that admit dependence.

    Month clustering is the *spatial* correction here — it allows arbitrary
    correlation among every station within a month, which is exactly the
    dependence the Moran tables measure. Station clustering is the temporal one.
    Two-way allows both, combined as Cameron–Gelbach–Miller (V_month +
    V_station − V_white); a non-PSD combination in small samples is repaired by
    clipping negative eigenvalues to zero and the repair is **recorded in its
    own column**, never applied silently.

    This is deliberately not a spatial-lag or error model: those change the
    estimand, and then the before/after against the baseline's own numbers —
    the entire point — evaporates. Same design, same coefficients, different
    covariance.
    """
    import statsmodels.api as sm
    from scipy import stats

    from twair.analysis.baseline import BASELINE_PREDICTORS, RESPONSE

    frame = panel.to_pandas()
    x = sm.add_constant(frame[list(BASELINE_PREDICTORS)], has_constant="add")
    y = frame[RESPONSE]
    terms = ["Intercept" if c == "const" else c for c in x.columns]

    fits = {
        "iid": sm.OLS(y, x).fit(),
        "cluster_month": sm.OLS(y, x).fit(
            cov_type="cluster", cov_kwds={"groups": frame[MONTH].to_numpy()}
        ),
        "cluster_station": sm.OLS(y, x).fit(
            cov_type="cluster", cov_kwds={"groups": frame[STATION].to_numpy()}
        ),
    }

    covariances = {name: np.asarray(fit.cov_params()) for name, fit in fits.items()}
    white = np.asarray(sm.OLS(y, x).fit(cov_type="HC0").cov_params())
    two_way = covariances["cluster_month"] + covariances["cluster_station"] - white
    eigenvalues = np.linalg.eigvalsh(two_way)
    psd_fixed = bool(eigenvalues.min() < 0)
    if psd_fixed:
        values, vectors = np.linalg.eigh(two_way)
        two_way = vectors @ np.diag(np.clip(values, 0.0, None)) @ vectors.T
    covariances["cluster_twoway"] = two_way

    beta = np.asarray(fits["iid"].params, dtype=float)
    df = int(fits["iid"].df_resid)
    rows: list[dict[str, Any]] = []
    wanted = [str(name) for name in conf.inference["cov_types"]]
    label = {
        "iid": "independent errors, the baseline's own assumption",
        "cluster_month": "spatial: any dependence within a month",
        "cluster_station": "temporal: any dependence within a station",
        "cluster_twoway": "both at once (CGM)",
    }
    for cov_name in wanted:
        se = np.sqrt(np.diag(covariances[cov_name]))
        for i, term in enumerate(terms):
            t = float(beta[i] / se[i]) if se[i] > 0 else float("nan")
            rows.append(
                {
                    "term": term,
                    "cov_type": cov_name,
                    "cov_meaning": label.get(cov_name, cov_name),
                    "coefficient": float(beta[i]),
                    "se": float(se[i]),
                    "t": t,
                    "p": float(2 * stats.t.sf(abs(t), df)) if np.isfinite(t) else None,
                    "se_inflation_vs_iid": float(se[i] / np.sqrt(covariances["iid"][i, i])),
                    "psd_fix_applied": psd_fixed if cov_name == "cluster_twoway" else False,
                }
            )
    return pl.DataFrame(rows, infer_schema_length=None)


# --------------------------------------------------------------------------- #
# the field's skill — measured before any surface exists
# --------------------------------------------------------------------------- #
def field_skill(
    conf: SpatialConf, *, root: Path | None = None, rng: np.random.Generator
) -> pl.DataFrame:
    """Can anything honest be interpolated between 65 stations? Scored first.

    Every method is scored on identical folds: hold one station out, predict it
    from the rest, at three buffer radii. Radius 0 is plain leave-one-out —
    **the optimistic bound**, because with a neighbour 600 m away it scores
    interpolation between near-duplicates. The buffered folds also withhold
    every station within 20 or 40 km of the target, which is the question a map
    of the empty space between stations actually poses.

    Kriging appears twice, once per variogram family, rather than with an
    internal selection step: choosing the family by fit inside each fold would
    be honest but doubles the cost, choosing it once outside the folds would
    leak, and publishing both lets the reader see when the hole-effect family —
    offered because the correlogram changes sign at mid range — actually earns
    its keep. The variogram is refitted inside every fold either way; fitting it
    once on all stations and then "holding one out" would leak the held-out
    station into the model it is scored against.

    A fold can fail: pykrige needs a stable variogram fit and a 40 km buffer in
    the dense west removes most of the network. Failures come back as null
    predictions with the reason, and the CLI prints the count before any skill
    number — a mean over the folds that happened to converge is the same trap as
    a mean over splits without the worst split.
    """
    bundle = station_dissimilarity(conf, root=root)
    lat = bundle.coords["lat"].to_numpy()
    lon = bundle.coords["lon"].to_numpy()
    d = pairwise_km(lat, lon)
    offshore = np.array([name in conf.offshore for name in bundle.stations])

    from twair.paths import processed_dir

    base = processed_dir("monthly") if root is None else root / "processed" / "monthly"
    start = str(conf.clustering["window_start"])
    end = str(conf.clustering["window_end"])
    monthly = (
        pl.scan_parquet(f"{base}/**/*.parquet")
        .filter(
            (pl.col("pollutant") == "PM2.5")
            & (pl.col(MONTH).dt.strftime("%Y-%m") >= start)
            & (pl.col(MONTH).dt.strftime("%Y-%m") <= end)
            & pl.col("mean").is_not_null()
            & pl.col(STATION).is_in(list(bundle.stations))
        )
        .select(STATION, MONTH, "mean")
        .collect()
        .pivot(on=STATION, index=MONTH, values="mean")
        .sort(MONTH)
    )
    months = monthly[MONTH].to_list()
    values = monthly.select(list(bundle.stations)).to_numpy()  # months × stations

    radii = [0.0] + [float(r) for r in conf.field["blocked_cv_radii_km"]]
    candidates = [str(v) for v in conf.field["variogram_candidates"]]
    exclude_offshore = bool(conf.field["exclude_offshore"])

    rows: list[dict[str, Any]] = []
    for t, month in enumerate(months):
        field = values[t]
        usable = np.isfinite(field)
        if exclude_offshore:
            usable &= ~offshore
        index = np.flatnonzero(usable)
        if index.size < conf.min_units:
            continue
        for i in index:
            for radius in radii:
                train = index[(d[index, i] > radius) & (index != i)]
                record_base = {
                    MONTH: month,
                    STATION: bundle.stations[i],
                    "buffer_km": radius,
                    "n_train": int(train.size),
                    "observed": float(field[i]),
                }
                if train.size < conf.min_units:
                    for method in (
                        *[f"kriging_{c}" for c in candidates],
                        "nearest",
                        "idw2",
                        "station_mean",
                    ):
                        rows.append(
                            {
                                **record_base,
                                "method": method,
                                "predicted": None,
                                "failed": "buffer leaves too few stations",
                            }
                        )
                    continue

                dist = d[train, i]
                train_values = field[train]
                rows.append(
                    {
                        **record_base,
                        "method": "nearest",
                        "predicted": float(train_values[np.argmin(dist)]),
                        "failed": None,
                    }
                )
                weight = 1.0 / np.maximum(dist, 0.1) ** 2
                rows.append(
                    {
                        **record_base,
                        "method": "idw2",
                        "predicted": float(weight @ train_values / weight.sum()),
                        "failed": None,
                    }
                )
                rows.append(
                    {
                        **record_base,
                        "method": "station_mean",
                        "predicted": float(train_values.mean()),
                        "failed": None,
                    }
                )
                for family in candidates:
                    rows.append(
                        _krige_one(
                            record_base,
                            family,
                            lat[train],
                            lon[train],
                            train_values,
                            lat[i],
                            lon[i],
                        )
                    )

    out = pl.DataFrame(rows, infer_schema_length=None)
    return out.with_columns(
        (pl.col("predicted") - pl.col("observed")).alias("error"),
    )


def _krige_one(
    record: dict[str, Any],
    family: str,
    train_lat: np.ndarray,
    train_lon: np.ndarray,
    train_values: np.ndarray,
    target_lat: float,
    target_lon: float,
) -> dict[str, Any]:
    """One ordinary-kriging prediction, failure recorded rather than raised.

    ``coordinates_type="geographic"`` so pykrige works on the sphere — its
    distances are then arc *degrees*, which is why no kilometre-named range
    parameter is ever passed (conf/spatial.yaml documents the trap).
    """
    from pykrige.ok import OrdinaryKriging

    method = f"kriging_{family}"
    try:
        krige = OrdinaryKriging(
            train_lon,
            train_lat,
            train_values,
            variogram_model=family,
            nlags=8,
            coordinates_type="geographic",
            enable_plotting=False,
        )
        predicted, variance = krige.execute(
            "points", np.array([target_lon]), np.array([target_lat])
        )
        return {
            **record,
            "method": method,
            "predicted": float(np.asarray(predicted).ravel()[0]),
            "kriging_sd": float(np.sqrt(max(float(np.asarray(variance).ravel()[0]), 0.0))),
            "failed": None,
        }
    except Exception as error:  # the failure IS the datum
        return {**record, "method": method, "predicted": None, "failed": type(error).__name__}
