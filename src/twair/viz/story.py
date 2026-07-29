"""Per-chapter payloads for the website.

``export.py`` ships the data. This ships the *arguments* — the small derived
tables each chapter is built around. They are separate because they carry
editorial choices that the raw layers must not: which baseline, which
threshold, which comparison.

Every one of those choices is written into the payload next to the numbers it
produced, so a reader can see the assumption without reading this file.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import polars as pl

from twair.panels import balanced_panel_options, choose_balanced_start
from twair.paths import outputs_dir, processed_dir
from twair.scalars import as_int, opt_float
from twair.viz.export import web_data_dir, write_json

log = logging.getLogger(__name__)

__all__ = [
    "GUIDELINES",
    "annual_by_station",
    "export_story",
    "national_trend",
    "station_cards",
]

# Thresholds, with their source, because "days over the limit" means nothing
# without saying whose limit. WHO tightened its guideline in 2021; Taiwan's
# standard has not moved since 2020, and the gap between the two is itself one
# of the more useful things a reader can take away.
# Annotated because the inner dict is genuinely heterogeneous — four float
# limits beside a nested `source` mapping — and left to infer, mypy widens the
# values to `object`, which then refuses to be compared against a Polars
# column. The shape is what the site reads, so it stays as it is.
GUIDELINES: dict[str, dict[str, Any]] = {
    "PM2.5": {
        "who_2021_annual": 5.0,
        "who_2021_daily": 15.0,
        "taiwan_annual": 15.0,
        "taiwan_daily": 35.0,
        "source": {
            "who": "WHO global air quality guidelines (2021)",
            "taiwan": "空氣品質標準（2020 年修正）",
        },
    },
    "PM10": {
        "who_2021_annual": 15.0,
        "who_2021_daily": 45.0,
        "taiwan_annual": 50.0,
        "taiwan_daily": 100.0,
        "source": {
            "who": "WHO global air quality guidelines (2021)",
            "taiwan": "空氣品質標準（2020 年修正）",
        },
    },
}

# Berkeley Earth's rule of thumb, kept because it is the single most effective
# way to make a microgram figure mean something, and labelled because it is a
# popularisation rather than a dose-response model.
CIGARETTE_EQUIVALENT_UGM3 = 22.0
CIGARETTE_CAVEAT = (
    "Berkeley Earth 的粗略換算：24 小時暴露於 22 μg/m³ PM2.5 約等於抽 1 支菸。"
    "這是為了讓濃度有感的類比，不是劑量反應模型——菸含有 PM2.5 以外的致癌物，"
    "而空污的成分隨來源而異。用來理解數量級，不要用來估計個人風險。"
)

# A year needs this share of days with a usable daily mean before its annual
# statistics are reported. Below it the number exists but is not comparable,
# and it is marked rather than dropped.
MIN_ANNUAL_COVERAGE = 0.75


def _daily(pollutant: str, root: Path | None = None) -> pl.LazyFrame:
    path = (root or processed_dir("daily")) / "daily.parquet"
    if not path.exists():
        raise FileNotFoundError(f"{path} not found — run `twair aggregate` first")
    return pl.scan_parquet(path).filter(pl.col("pollutant") == pollutant)


def annual_by_station(pollutant: str = "PM2.5", root: Path | None = None) -> pl.DataFrame:
    """One row per station-year: mean, exceedance counts, and the denominator.

    Exceedance *counts* are not comparable between a station that reported 361
    days and one that reported 200, so the rate travels with the count and the
    coverage travels with both. A chart that shows only the count invites the
    reader to conclude that the station with worse instrument uptime has
    cleaner air.
    """
    limits = GUIDELINES.get(pollutant, {})
    who_daily = limits.get("who_2021_daily")
    tw_daily = limits.get("taiwan_daily")

    frame = (
        _daily(pollutant, root)
        .filter(pl.col("mean").is_not_null())
        .select(
            "station_name",
            pl.col("date").dt.year().alias("year"),
            "mean",
        )
        .group_by("station_name", "year")
        .agg(
            pl.col("mean").mean().alias("annual_mean"),
            pl.col("mean").max().alias("worst_day"),
            pl.len().alias("days_with_data"),
            *(
                [(pl.col("mean") > who_daily).sum().alias("days_over_who")]
                if who_daily is not None
                else []
            ),
            *(
                [(pl.col("mean") > tw_daily).sum().alias("days_over_taiwan")]
                if tw_daily is not None
                else []
            ),
        )
        .collect()
    )

    days_in_year = (
        pl.when(
            (pl.col("year") % 4 == 0) & ((pl.col("year") % 100 != 0) | (pl.col("year") % 400 == 0))
        )
        .then(366)
        .otherwise(365)
    )

    frame = frame.with_columns(
        (pl.col("days_with_data") / days_in_year).alias("coverage"),
    ).with_columns(
        (pl.col("coverage") >= MIN_ANNUAL_COVERAGE).alias("comparable"),
    )

    if who_daily is not None:
        frame = frame.with_columns(
            (pl.col("days_over_who") / pl.col("days_with_data")).alias("share_over_who")
        )
    if tw_daily is not None:
        frame = frame.with_columns(
            (pl.col("days_over_taiwan") / pl.col("days_with_data")).alias("share_over_taiwan")
        )

    # Ranked among stations that are comparable in that year. A station with
    # 40% coverage does not get to be "cleanest in Taiwan".
    ranked = (
        frame.filter(pl.col("comparable"))
        .with_columns(pl.col("annual_mean").rank("min").over("year").cast(pl.Int32).alias("rank"))
        .select("station_name", "year", "rank")
    )
    stations_ranked = (
        frame.filter(pl.col("comparable")).group_by("year").agg(pl.len().alias("stations_ranked"))
    )

    return (
        frame.join(ranked, on=["station_name", "year"], how="left")
        .join(stations_ranked, on="year", how="left")
        .sort("year", "station_name")
    )


def national_trend(
    pollutant: str = "PM2.5",
    root: Path | None = None,
    *,
    balanced_since: int | None = None,
) -> tuple[pl.DataFrame, dict[str, Any]]:
    """Annual national mean, computed two ways because they disagree.

    Taiwan's monitoring network grew from one station in 1982 to 84 by 1999,
    and its PM2.5 network from 5 stations to 77. A mean over "all stations
    reporting that year" therefore compares different places in different
    years, and part of any trend it shows is the network changing shape rather
    than the air changing composition.

    So both are reported:

    ``all_stations``
        every comparable station-year — the number most sources quote.
    ``balanced``
        stations comparable in every year from ``balanced_since`` onwards, so
        the same places are compared throughout. Null before that year.

    Returns the series and the panel's provenance: which start year was used,
    which stations are in it, and what the alternatives would have given.
    """
    annual = annual_by_station(pollutant, root).filter(pl.col("comparable"))
    if annual.is_empty():
        raise RuntimeError(f"no comparable station-years for {pollutant}")

    options = balanced_panel_options(annual)
    start = balanced_since if balanced_since is not None else choose_balanced_start(options)

    all_stations = annual.group_by("year").agg(
        pl.col("annual_mean").mean().alias("all_stations"),
        pl.len().alias("n_stations"),
    )

    members: list[str] = []
    if start is not None:
        window = annual.filter(pl.col("year") >= start)
        n_years = as_int(annual["year"].max()) - start + 1
        members = sorted(
            window.group_by("station_name")
            .agg(pl.col("year").n_unique().alias("years"))
            .filter(pl.col("years") == n_years)["station_name"]
            .to_list()
        )

    if members:
        balanced = (
            annual.filter((pl.col("year") >= start) & pl.col("station_name").is_in(members))
            .group_by("year")
            .agg(
                pl.col("annual_mean").mean().alias("balanced"),
                pl.len().alias("n_balanced"),
            )
        )
        series = all_stations.join(balanced, on="year", how="left")
    else:
        series = all_stations.with_columns(
            pl.lit(None, dtype=pl.Float64).alias("balanced"),
            pl.lit(0, dtype=pl.UInt32).alias("n_balanced"),
        )

    provenance = {
        "balanced_since": start,
        "balanced_stations": members,
        "selection_rule": "maximise station-years; ties to the longer record",
        "options": options.to_dicts(),
    }
    return series.sort("year"), provenance


def trend_by_group(
    pollutant: str = "PM2.5",
    *,
    group: str = "airzone",
    root: Path | None = None,
) -> pl.DataFrame:
    """Annual mean per air-quality zone or county, from comparable years only."""
    annual = annual_by_station(pollutant, root).filter(pl.col("comparable"))
    stations = _stations()
    if group not in stations.columns:
        raise ValueError(f"stations table has no column {group!r}")

    return (
        annual.join(stations.select("station_name", group), on="station_name", how="left")
        .filter(pl.col(group).is_not_null())
        .group_by(group, "year")
        .agg(
            pl.col("annual_mean").mean().alias("mean"),
            pl.len().alias("n_stations"),
        )
        .sort(group, "year")
    )


def station_cards(pollutant: str = "PM2.5", root: Path | None = None) -> list[dict[str, Any]]:
    """Chapter 2: one personalised record per station.

    Built from the most recent year in which the station was comparable, so a
    station that closed in 2003 gets its 2003 card rather than an empty one.
    The card says which year it describes.
    """
    annual = annual_by_station(pollutant, root)
    comparable = annual.filter(pl.col("comparable"))
    if comparable.is_empty():
        return []

    latest = (
        comparable.sort("year").group_by("station_name").agg(pl.all().last()).sort("station_name")
    )

    stations = _stations()
    join_columns = [
        c
        for c in ("county", "township", "airzone", "station_type", "lon", "lat")
        if c in stations.columns
    ]
    latest = latest.join(
        stations.select("station_name", *join_columns), on="station_name", how="left"
    )

    cards: list[dict[str, Any]] = []
    for row in latest.iter_rows(named=True):
        mean = row["annual_mean"]
        cards.append(
            {
                **{k: row.get(k) for k in ("station_name", *join_columns)},
                "year": row["year"],
                "annual_mean": round(mean, 2),
                "worst_day": round(row["worst_day"], 1),
                "days_with_data": row["days_with_data"],
                "days_over_who": row.get("days_over_who"),
                "days_over_taiwan": row.get("days_over_taiwan"),
                "rank": row.get("rank"),
                "stations_ranked": row.get("stations_ranked"),
                "times_who_annual": round(mean / GUIDELINES[pollutant]["who_2021_annual"], 2),
                "cigarettes_per_day": round(mean / CIGARETTE_EQUIVALENT_UGM3, 2),
            }
        )
    return cards


def export_story(destination: Path | None = None, *, pollutant: str = "PM2.5") -> list[Path]:
    """Write every chapter payload under ``web/public/data/story/``."""
    root = (destination or web_data_dir()) / "story"
    written: list[Path] = []

    trend, panel = national_trend(pollutant)
    written.append(
        write_json(
            root / "trend-national.json",
            {
                "pollutant": pollutant,
                "guidelines": GUIDELINES.get(pollutant),
                "method": {
                    "all_stations": "mean over every station comparable that year",
                    "balanced": (
                        f"mean over the {len(panel['balanced_stations'])} station(s) "
                        f"comparable in every year since {panel['balanced_since']}"
                    ),
                    "min_annual_coverage": MIN_ANNUAL_COVERAGE,
                    "why": (
                        "The network grew from 1 station (1982) to 84 (1999), and the "
                        "PM2.5 network from 5 to 77. An all-stations mean partly "
                        "measures that growth rather than the air."
                    ),
                },
                "panel": panel,
                "years": trend["year"].to_list(),
                "all_stations": [_round(v) for v in trend["all_stations"]],
                "n_stations": trend["n_stations"].to_list(),
                "balanced": [_round(v) for v in trend["balanced"]],
                "n_balanced": trend["n_balanced"].to_list(),
            },
        )
    )

    for group in ("airzone", "county"):
        try:
            grouped = trend_by_group(pollutant, group=group)
        except ValueError as exc:
            log.warning("skipping %s trend: %s", group, exc)
            continue
        written.append(
            write_json(
                root / f"trend-{group}.json",
                {
                    "pollutant": pollutant,
                    "group": group,
                    "series": [
                        {
                            "name": name,
                            "years": part["year"].to_list(),
                            "mean": [_round(v) for v in part["mean"]],
                            "n_stations": part["n_stations"].to_list(),
                        }
                        for (name,), part in grouped.group_by(group, maintain_order=True)
                    ],
                },
            )
        )

    written.append(
        write_json(
            root / "station-cards.json",
            {
                "pollutant": pollutant,
                "guidelines": GUIDELINES.get(pollutant),
                "cigarette_equivalent_ugm3": CIGARETTE_EQUIVALENT_UGM3,
                "cigarette_caveat": CIGARETTE_CAVEAT,
                "cards": station_cards(pollutant),
            },
        )
    )

    written.extend(_export_pitfalls(root))
    written.extend(_export_replication(root))
    written.extend(_export_deweather(root))
    written.extend(_export_detection_limit(root))
    written.extend(_export_sources(root))
    written.extend(_export_forecast(root))
    written.extend(_export_health(root))
    written.extend(_export_imputation(root))
    return written


def _export_imputation(root: Path) -> list[Path]:
    """Chapter 7's seventh pitfall: what the gap-filling sentence cost.

    Copied from the M11 tables for the same reason the other six are: the
    chapter and the report have to show the same numbers, and reading one file
    is the only way to guarantee it.

    Two editorial choices are written into the payload beside the numbers.
    ``buckets`` fixes the gap-length order — it is physical, and sorting it
    alphabetically would put ">48h" first and destroy the shape the chart
    exists to show. And ``not_reported`` says what was measured and withheld,
    because a reader who does not find "did it change the conclusion" here
    should be told it was attempted rather than left to assume it was not.
    """
    source = outputs_dir("m11_imputation")
    distribution_path = source / "gap_distribution.parquet"
    reconstruction_path = source / "reconstruction.parquet"
    if not distribution_path.exists() or not reconstruction_path.exists():
        log.warning("no M11 output — run `twair analyze m11` before publishing pitfall 07")
        return []

    buckets = ["1h", "2-3h", "4-12h", "13-48h", ">48h"]
    order = {name: i for i, name in enumerate(buckets)}

    distribution = pl.read_parquet(distribution_path)
    scores = pl.read_parquet(reconstruction_path)
    pooled = scores.filter(pl.col("gap_bucket") == "all")
    by_gap = scores.filter(pl.col("gap_bucket") != "all")

    payload = {
        "period": str(distribution["period"][0]) if "period" in distribution.columns else None,
        # Counts, not measurements: `_round(x, 0)` would ship 78.0, and a
        # station count with a decimal point in it reads as an estimate.
        "stations_measured": as_int(distribution["stations"][0]),
        "stations_compared": as_int(pooled["stations"][0]),
        "hidden": as_int(pooled["hidden"][0]),
        "buckets": buckets,
        "distribution": sorted(
            (
                {
                    "gap_bucket": row["gap_bucket"],
                    "gaps": row["gaps"],
                    "hours": row["hours"],
                    "share_of_gaps": _round(row["share_of_gaps"], 4),
                    "share_of_missing_hours": _round(row["share_of_missing_hours"], 4),
                }
                for row in distribution.iter_rows(named=True)
            ),
            key=lambda r: order.get(str(r["gap_bucket"]), 99),
        ),
        "pooled": [
            {
                "strategy": row["strategy"],
                "recovered": row["recovered"],
                "recovery_rate": _round(row["recovery_rate"], 4),
                "mae": _round(row["mae"], 2),
                "rmse": _round(row["rmse"], 2),
                "bias": _round(row["bias"], 3),
            }
            for row in pooled.iter_rows(named=True)
        ],
        "by_gap": sorted(
            (
                {
                    "strategy": row["strategy"],
                    "gap_bucket": row["gap_bucket"],
                    "n": row["n"],
                    "mae": _round(row["mae"], 2),
                    "rmse": _round(row["rmse"], 2),
                }
                for row in by_gap.iter_rows(named=True)
            ),
            key=lambda r: (str(r["strategy"]), order.get(str(r["gap_bucket"]), 99)),
        ),
        "method": {
            "hidden_runs_drawn_from": "the gap-length distribution measured in the same data",
            "why": (
                "Hiding isolated cells would give interpolation a series of one-hour gaps "
                "it cannot fail, and the table would then say interpolation is excellent."
            ),
            "interpolate_max_gap_hours": 3,
            "neighbour_max_distance_km": 30,
            "neighbour_min_correlation": 0.7,
        },
        "not_reported": {
            "downstream_r2": (
                "Measured and withheld. Filling only changes rows the baseline could not "
                "use, so each strategy ends up scored on a different test set. A "
                "confounded number is worse than no number."
            )
        },
    }
    return [write_json(root / "imputation.json", payload)]


def _export_health(root: Path) -> list[Path]:
    """Chapter 6: the attributable fraction, and how much of it is the assumption.

    The payload leads with the *relative* spread rather than the fraction,
    because that is the finding. The absolute gap between counterfactuals
    barely moves across twenty years; its share of the estimate nearly triples,
    and only the second framing shows that cleaner air makes the number more
    assumption-dependent rather than less.
    """
    source = outputs_dir("m10_health")
    spread_path = source / "spread.parquet"
    if not spread_path.exists():
        log.warning("no M10 output — run `twair analyze m10`")
        return []

    from twair.analysis.health import load_counterfactuals, load_response_functions

    spread = pl.read_parquet(spread_path)
    national = pl.read_parquet(source / "national.parquet").filter(pl.col("bound") == "central")
    coverage = pl.read_parquet(source / "coverage.parquet").to_dicts()[0]

    functions = load_response_functions()
    counterfactuals = load_counterfactuals()
    first, last = spread.head(1).to_dicts()[0], spread.tail(1).to_dicts()[0]

    series = [
        {
            "name": cf.name,
            "label": cf.label,
            "value": cf.value,
            "why": cf.why,
            "years": part["year"].to_list(),
            "paf": [_round(v, 4) for v in part["paf_median"]],
        }
        for cf in sorted(counterfactuals.values(), key=lambda c: c.value)
        for part in [national.filter(pl.col("counterfactual") == cf.name).sort("year")]
        if not part.is_empty()
    ]

    return [
        write_json(
            root / "health.json",
            {
                "panel": {
                    "start_year": coverage["panel_start_year"],
                    "stations": coverage["panel_stations"],
                    "station_years": coverage["station_years_total"],
                    "why": (
                        "PM2.5 監測從少數實驗測站開始，1998 年只有 5 站有足夠涵蓋率、"
                        "2025 年有 77 站。不固定樣本的話，趨勢有一部分是測站網在長大，"
                        "不是空氣在變化。這裡用的固定樣本規則與第 1 章相同。"
                    ),
                },
                "formula": "PAF = (RR − 1) ÷ RR，RR = exp(β × max(0, C − 反事實濃度))",
                "functions": [
                    {
                        "name": f.name,
                        "rr_per_10": f.rr_per_10,
                        "rr_per_10_low": f.rr_per_10_low,
                        "rr_per_10_high": f.rr_per_10_high,
                        "outcome": f.outcome,
                        "source": f.source,
                        "source_url": f.source_url,
                        "caveat": f.caveat,
                    }
                    for f in functions.values()
                ],
                "series": series,
                "years": spread["year"].to_list(),
                "mean_median": [_round(v, 2) for v in spread["mean_median"]],
                "spread_share": [_round(v, 4) for v in spread["spread_as_share_of_estimate"]],
                "headline": {
                    "first_year": first["year"],
                    "last_year": last["year"],
                    "first_share": _round(first["spread_as_share_of_estimate"], 4),
                    "last_share": _round(last["spread_as_share_of_estimate"], 4),
                    "first_range": [
                        _round(first["paf_lowest_assumption"], 4),
                        _round(first["paf_highest_assumption"], 4),
                    ],
                    "last_range": [
                        _round(last["paf_lowest_assumption"], 4),
                        _round(last["paf_highest_assumption"], 4),
                    ],
                },
                "extrapolation": {
                    "ceiling_ugm3": coverage["extrapolation_ceiling_ugm3"],
                    "share_above": _round(coverage["share_above_ceiling"], 4),
                    "why": (
                        "係數來自的世代研究多半觀測在 30 μg/m³ 以下。高於這個值是外推，不是內插。"
                    ),
                },
                "not_reported": {
                    "deaths": (
                        "沒有死亡人數。人數 = 比例 × 人口 × 基礎死亡率，"
                        "而本專案只有測站觀測，沒有人口資料。"
                        "人口那一項會是誤差最大的來源，卻看起來像句子裡最紮實的部分。"
                    ),
                    "exposure": (
                        "測站平均不是人口加權暴露。全國數字是「各測站的中位數」，"
                        "那是關於監測網的陳述，不是關於任何人呼吸到什麼的陳述。"
                    ),
                },
            },
        )
    ]


def _export_forecast(root: Path) -> list[Path]:
    """Chapter 8: three metrics on one axis, disagreeing.

    The payload is built so the chart cannot show one baseline alone. R²,
    skill against persistence and skill against climatology are all on the same
    scale — 1 is perfect, 0 is no better than the thing compared against — so
    they belong on one axis, and on one axis the contradiction is a shape
    rather than a paragraph: R² falling, skill-vs-persistence rising,
    skill-vs-climatology collapsing.

    Every split ships too, not just the mean over them. At six hours the mean
    is +0.190 and one split is -0.111, and a chart drawn from means alone would
    make the same mistake the module's own summary line made.
    """
    source = outputs_dir("m9_forecast")
    scores_path = source / "scores.parquet"
    if not scores_path.exists():
        log.warning("no M9 output — run `twair analyze m9`")
        return []

    from twair.models.forecast import summarise_scores

    # Derived from the splits rather than read from by_horizon.parquet: the
    # aggregate is a view of the scores, and re-deriving it means an older
    # artefact cannot ship a summary that predates the columns it should carry.
    scores = pl.read_parquet(scores_path)
    by_horizon = summarise_scores(scores)

    horizons = []
    for row in by_horizon.iter_rows(named=True):
        horizon = int(row["horizon"])
        splits = scores.filter(pl.col("horizon") == horizon).sort("split")
        horizons.append(
            {
                "horizon": horizon,
                "n": int(row["n"]),
                "splits": int(row["splits"]),
                "model_r2": _round(row["model_r2"], 3),
                "skill_persistence": _round(row["skill_vs_persistence"], 3),
                "skill_persistence_worst": _round(row["skill_worst_split"], 3),
                "skill_climatology": _round(row["skill_vs_climatology"], 3),
                "skill_climatology_worst": _round(row["skill_vs_climatology_worst"], 3),
                "splits_not_beating_persistence": int(row["splits_not_beating_persistence"]),
                "model_rmse": _round(row["model_rmse"], 2),
                "persistence_rmse": _round(row["persistence_rmse"], 2),
                "climatology_rmse": _round(row["climatology_rmse"], 2),
                "per_split": [
                    {
                        "split": s["split"],
                        "skill_persistence": _round(s["skill_vs_persistence"], 3),
                        "skill_climatology": _round(s["skill_vs_climatology"], 3),
                        "model_r2": _round(s["model_r2"], 3),
                    }
                    for s in splits.iter_rows(named=True)
                ],
            }
        )

    return [
        write_json(
            root / "forecast.json",
            {
                "period": [2015, 2025],
                "target": "PM2.5",
                "validation": "rolling-origin：過去訓練、未來測試，切點往前走",
                "skill_formula": "skill = 1 − MSE(模型) ÷ MSE(基準線)",
                "baselines": [
                    {
                        "name": "persistence",
                        "label": "「跟現在一樣」",
                        "what": "把此刻的濃度直接當成 h 小時後的預測",
                        "why": (
                            "Phase 2 量到它的 R² 是 0.900，勝過所有解釋性模型的 0.524，"
                            "因為它用了 PM2.5 自己的前一個值。它是要超越的門檻，不是比較對象。"
                        ),
                    },
                    {
                        "name": "climatology",
                        "label": "「這站這時候的平均」",
                        "what": "訓練期間內，同一測站、同月份、同小時的平均值",
                        "why": (
                            "它完全不看今天發生什麼事。模型如果贏不過它，"
                            "代表模型學到的只是季節與日夜循環。"
                        ),
                    },
                ],
                "reading": [
                    {
                        "claim": "R² 掉四倍的同時，skill 是往上的",
                        "detail": (
                            "R² 衡量的是「這個目標本來多好預測」，而 PM2.5 一小時後"
                            "任何人都很好預測——包括一條說「跟現在一樣」的規則。"
                            "skill 問的是不同的問題：模型有沒有加到東西。"
                            "答案隨距離變好，因為 persistence 衰退得比模型快。"
                        ),
                    },
                    {
                        "claim": "只看一條基準線，一定會高估模型",
                        "detail": (
                            "vs persistence 一路漲到 48 小時，單看這條會讀成「愈遠愈好」。"
                            "但 vs climatology 在同一段從 +0.84 崩到 +0.09——"
                            "兩天後模型已經幾乎退化成「這站、這個月、這個小時的平均」。"
                            "在 persistence 已經輸給長期平均的地方贏過 persistence，不算成就。"
                            "實用範圍大約到 24 小時。"
                        ),
                    },
                    {
                        "claim": "平均值會藏掉一個輸掉的分割",
                        "detail": (
                            "16 個「期距 × 分割」格子裡有一個是 −0.111（6 小時、rolling_1），"
                            "而四個期距的平均全是正的。rolling_1 是訓練資料最少的分割。"
                            "這跟「R² 藏掉一個爛模型」是同一個錯，只是高了一層——"
                            "所以每個平均值旁邊都畫著它最差的分割。"
                        ),
                    },
                    {
                        "claim": "6 小時是最不穩的期距，而且有兩批獨立資料同意",
                        "detail": (
                            "回測裡 6 小時的四個分割從 −0.111 到 +0.303，跨度 0.41，"
                            "是四個期距裡最大的（1 小時只有 0.07）。"
                            "後來為了做互動 demo，另外用 2015–2024 訓練、2025 完全保留，"
                            "在六個測站上重跑一次——6 小時的 skill 是 **−0.043**，"
                            "六站沒有一個明顯為正，而 1、24、48 小時全都穩定為正。"
                            "兩批不同的資料指向同一件事，那就不是某個分割運氣差。"
                            "合理的解釋是 6 小時卡在兩種訊號中間：太遠，此刻的漲跌動量已經用完；"
                            "又太近，隔天同一時刻的日夜循環還沒接上。"
                        ),
                    },
                ],
                "leakage_note": (
                    "lag_k 在第 t 列存 t−k+1 的值，target 往反方向移一個期距。"
                    "兩個位移方向相反，那就是全部的安全性質。"
                    "lag 跑在每站完整的逐時索引上，所以三天停機後那一小時拿到 null，"
                    "不是被標成「一小時前」的三天前資料。"
                ),
                "horizons": horizons,
            },
        )
    ]


def _export_sources(root: Path) -> list[Path]:
    """Chapter 3: the CBPF grid for every station, plus its reading.

    The grid ships whole rather than pre-rendered, because the same geometry
    serves every station and only the fills change. A null probability stays
    null all the way to the browser — a bin the wind rarely reaches must look
    unknown, not clean.
    """
    source = outputs_dir("m7_sources")
    summary_path = source / "summary.parquet"
    if not summary_path.exists():
        log.warning("no M7 output — run `twair analyze m7`")
        return []

    summary = pl.read_parquet(summary_path)
    grid = pl.read_parquet(source / "grid.parquet")
    stations = _stations()

    geo_columns = [c for c in ("county", "airzone", "station_type") if c in stations.columns]
    if geo_columns:
        summary = summary.join(
            stations.select("station_name", *geo_columns), on="station_name", how="left"
        )

    # Speed bins in physical order, not the alphabetical order a group_by
    # leaves behind. "<0.5" sorts after "1.5-2.5" as a string.
    speed_order = [
        s
        for s in ("<0.5", "0.5-1.5", "1.5-2.5", "2.5-4", "4-6", "6-8", "8+")
        if s in set(grid["speed_bin"].cast(pl.Utf8).to_list())
    ]
    sectors = sorted({int(s) for s in grid["sector"]})

    def signature(peak_speed: str | None) -> str:
        if peak_speed in {"6-8", "8+"}:
            return "transport"
        if peak_speed in {"<0.5", "0.5-1.5"}:
            return "local"
        return "mixed"

    by_station: dict[str, Any] = {}
    for row in summary.iter_rows(named=True):
        name = row["station_name"]
        cells = grid.filter(pl.col("station_name") == name)
        lookup = {(int(c["sector"]), str(c["speed_bin"])): c for c in cells.iter_rows(named=True)}
        by_station[name] = {
            "threshold": _round(row["threshold"], 1),
            "calm_fraction": _round(row["calm_fraction"], 4),
            "resultant": _round(row["resultant"], 3),
            "peak_sector": row["peak_sector"],
            "peak_speed": row["peak_speed"],
            "signature": signature(row["peak_speed"]),
            **{k: row.get(k) for k in geo_columns},
            # Row-major over (sector, speed). null means the bin had too few
            # hours to report, which is not the same as a low probability.
            "probability": [
                [_round(lookup.get((s, b), {}).get("probability"), 3) for b in speed_order]
                for s in sectors
            ],
            "n": [
                [int(lookup.get((s, b), {}).get("n") or 0) for b in speed_order] for s in sectors
            ],
        }

    counts = dict.fromkeys(("transport", "local", "mixed"), 0)
    for entry in by_station.values():
        counts[entry["signature"]] += 1

    return [
        write_json(
            root / "sources.json",
            {
                "method": "CBPF (Uria-Tellaetxe & Carslaw, 2014)",
                "explains": (
                    "給定風從某方位、以某風速吹來，該小時濃度落在高值區的機率。"
                    "近處的污染源在**低風速**時顯現（風大就吹散），"
                    "遠處的在**高風速**時顯現（要有風才送得到）。"
                ),
                "percentile": _round(summary["percentile"][0], 0),
                "min_bin_count": 20,
                "null_means": "該格觀測時數不足 20 小時，機率不予報告（不是機率為零）",
                "cannot_say": (
                    "方位不是來源地。這張圖支撐「高值在強風、風從某方位來時出現」，"
                    "不支撐「污染來自某地」——距離、沿途其他污染源、"
                    "以及空氣不走直線，都在中間。"
                ),
                "sectors": sectors,
                "speed_bins": speed_order,
                "signature_counts": counts,
                "median_calm_fraction": _round(summary["calm_fraction"].median(), 4),
                "n_suppressed_bins": int(summary["n_suppressed_bins"].sum()),
                "stations": by_station,
            },
        )
    ]


def _export_deweather(root: Path) -> list[Path]:
    """Chapter 1's second line: how much of the trend was the weather."""
    source = outputs_dir("m4_deweather") / "summary.parquet"
    if not source.exists():
        log.warning("no M4 output — run `twair analyze m4`")
        return []

    summary = pl.read_parquet(source)
    significant = summary.filter(pl.col("normalised_significant"))
    stations = _stations()

    # The zone breakdown is a bonus, not the payload. A stations table without
    # airzone still produces the national figures rather than failing the whole
    # export — same degradation as `trend_by_group`.
    by_zone = pl.DataFrame()
    if "airzone" in stations.columns:
        by_zone = (
            significant.join(
                stations.select("station_name", "airzone"), on="station_name", how="left"
            )
            .filter(pl.col("airzone").is_not_null())
            .group_by("airzone")
            .agg(
                pl.len().alias("n"),
                pl.col("observed_slope").median().alias("observed"),
                pl.col("normalised_slope").median().alias("normalised"),
                pl.col("weather_share").median().alias("weather_share"),
            )
            .sort("normalised")
        )
    else:
        log.warning("stations table has no column 'airzone' — skipping the zone breakdown")

    return [
        write_json(
            root / "deweather.json",
            {
                "method": "Grange et al. (2018), random-forest meteorological normalisation",
                "period": [2006, 2025],
                "n_stations": summary.height,
                "n_significant": significant.height,
                "median_observed_slope": _round(significant["observed_slope"].median(), 3),
                "median_normalised_slope": _round(significant["normalised_slope"].median(), 3),
                "median_weather_share": _round(significant["weather_share"].median(), 3),
                # The spread matters as much as the median: 73 independently
                # fitted models landing on the same share is the finding.
                "weather_share_p10": _round(significant["weather_share"].quantile(0.1), 3),
                "weather_share_p90": _round(significant["weather_share"].quantile(0.9), 3),
                "median_holdout_r2": _round(summary["holdout_r2"].median(), 3),
                "caveat": (
                    "正規化移除的是模型看得見的氣象影響。holdout R² 中位數 0.445，"
                    "代表逐時變異有一半以上不是本地氣象能解釋的——境外傳輸就在其中，"
                    "而它會被算進「排放改變」。這不是因果歸因。"
                ),
                "by_zone": [
                    {
                        "airzone": row["airzone"],
                        "n": row["n"],
                        "observed": _round(row["observed"], 3),
                        "normalised": _round(row["normalised"], 3),
                        "weather_share": _round(row["weather_share"], 3),
                    }
                    for row in by_zone.iter_rows(named=True)
                ]
                if not by_zone.is_empty()
                else [],
                "unresolved": summary.filter(~pl.col("normalised_significant"))
                .select("station_name", "normalised_slope", "normalised_low", "normalised_high")
                .to_dicts(),
            },
        )
    ]


def _export_detection_limit(root: Path) -> list[Path]:
    """Chapter 4: how large an effect this method could have found.

    The payload leads with the placebo spread rather than the estimates,
    because that ordering is the argument. An effect of -0.96 µg/m³ is only
    interesting once you know the same procedure returns -0.69 in years when
    nothing happened.
    """
    source = outputs_dir("m5_causal")
    effects_path = source / "effects.parquet"
    if not effects_path.exists():
        log.warning("no M5 output — run `twair analyze m5`")
        return []

    effects = pl.read_parquet(effects_path)
    placebos = pl.read_parquet(source / "placebos.parquet")
    breaks_path = source / "trend_breaks.parquet"
    breaks = pl.read_parquet(breaks_path) if breaks_path.exists() else pl.DataFrame()

    # Two SD on a normal distribution leaves ~4.55% in the tails, so this many
    # stations clear the bar by chance alone. Printing it beside the count is
    # what stops one survivor out of seventy reading as a discovery.
    chance_rate = 0.0455

    windowed = []
    for (name,), group in effects.group_by("event", maintain_order=True):
        pool = placebos.filter(pl.col("event") == name)["placebo_effect"]
        windowed.append(
            {
                "event": name,
                "kind": "window",
                "n_stations": group.height,
                "median_effect": _round(group["effect"].median(), 3),
                "median_placebo_mean": _round(group["placebo_mean"].median(), 3),
                "median_placebo_sd": _round(group["placebo_sd"].median(), 3),
                "n_credible": int(group["credible"].sum()),
                "n_expected_by_chance": _round(chance_rate * group.height, 1),
                "credible_stations": group.filter(pl.col("credible"))["station"].to_list(),
                # Every placebo estimate, so the chart can show the real result
                # inside the cloud it has to be distinguished from.
                "placebo_effects": [_round(v, 2) for v in pool.to_list()],
                "station_effects": [
                    {"station": r["station"], "effect": _round(r["effect"], 2)}
                    for r in group.select("station", "effect").iter_rows(named=True)
                ],
            }
        )

    if not breaks.is_empty():
        for (name,), group in breaks.group_by("event", maintain_order=True):
            windowed.append(
                {
                    "event": name,
                    "kind": "trend_break",
                    "n_stations": group.height,
                    "median_effect": _round(group["delta"].median(), 3),
                    "median_placebo_mean": _round(group["placebo_mean"].median(), 3),
                    "median_placebo_sd": _round(group["placebo_sd"].median(), 3),
                    "n_credible": int(group["credible"].sum()),
                    "n_expected_by_chance": _round(chance_rate * group.height, 1),
                    "credible_stations": group.filter(pl.col("credible"))["station"].to_list(),
                    "placebo_effects": [],
                    "station_effects": [
                        {"station": r["station"], "effect": _round(r["delta"], 2)}
                        for r in group.select("station", "delta").iter_rows(named=True)
                    ],
                }
            )

    # The spatial falsification: if the plant's units really stopped, the
    # stations around the plant are where it would show.
    near_taichung = ["沙鹿", "線西", "忠明", "西屯", "大里", "豐原", "彰化", "二林"]
    taichung = effects.filter(
        pl.col("event").str.contains("台中電廠") & pl.col("station").is_in(near_taichung)
    ).sort("effect")

    return [
        write_json(
            root / "detection-limit.json",
            {
                "method": {
                    "window": "視窗外訓練，用視窗當時的實際天氣預測視窗，差額即效應",
                    "trend_break": "在氣象正規化後的月序列上做分段迴歸，比較斜率",
                    "placebo": "同一套程序，跑在沒有事件的年份／其他候選斷點上",
                    "threshold": "效應要距離安慰劑均值 2 個標準差才算偵測到",
                },
                "events": windowed,
                "spatial_check": {
                    "label": "台中電廠周邊測站",
                    "why": (
                        "若機組真的停止燃煤，最近的測站是效應該出現的地方。"
                        "8 站全部落在安慰劑散布之內，其中 4 站效應為正。"
                    ),
                    "stations": [
                        {
                            "station": r["station"],
                            "effect": _round(r["effect"], 2),
                            "z": _round(r["z_against_placebo"], 2),
                        }
                        for r in taichung.iter_rows(named=True)
                    ],
                },
            },
        )
    ]


def _export_pitfalls(root: Path) -> list[Path]:
    """Chapter 5's evidence, straight from the M3 tables.

    Copied verbatim rather than recomputed: the chapter must show the same
    numbers as the report, and the only way to guarantee that is for both to
    read the same file.
    """
    source = outputs_dir("m3_pitfalls")
    if not source.exists():
        log.warning("no M3 output — run `twair analyze m3` before publishing chapter 5")
        return []

    tables = {}
    for path in sorted(source.glob("*.parquet")):
        tables[path.stem] = pl.read_parquet(path).to_dicts()

    if not tables:
        return []
    return [write_json(root / "pitfalls.json", {"tables": tables})]


def _export_replication(root: Path) -> list[Path]:
    """The M1 comparison table: published 2018 value against the reproduction."""
    path = outputs_dir("m1_replication") / "comparison.parquet"
    if not path.exists():
        log.warning("no M1 output — run `twair analyze m1` before publishing chapter 5")
        return []
    return [write_json(root / "replication.json", {"rows": pl.read_parquet(path).to_dicts()})]


def _round(value: Any, places: int = 2) -> float | None:
    """Round a number for the wire, passing null straight through.

    Takes ``Any`` for the same reason :mod:`twair.scalars` does: most callers
    hand it a Polars aggregate, whose declared type is a union of every value a
    cell could hold, and narrowing that at 43 call sites would mean 43 casts
    that assert rather than convert. ``opt_float`` does the conversion for
    real, here, once.

    Null passes through because a withheld aggregate must reach the browser as
    null. Rounding it to 0.0 would be the exact failure this project documents.
    """
    number = opt_float(value)
    return None if number is None else round(number, places)


def _stations() -> pl.DataFrame:
    path = outputs_dir("qc") / "stations.parquet"
    if not path.exists():
        raise FileNotFoundError(f"{path} not found — run `twair stations` first")
    return pl.read_parquet(path)
