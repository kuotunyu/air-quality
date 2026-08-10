"""Per-chapter payloads for the website.

``export.py`` ships the data. This ships the *arguments* — the small derived
tables each chapter is built around. They are separate because they carry
editorial choices that the raw layers must not: which baseline, which
threshold, which comparison.

Every one of those choices is written into the payload next to the numbers it
produced, so a reader can see the assumption without reading this file.

**The prose in these payloads is printed verbatim by the site.** It is not
Markdown and nothing renders it: an asterisk pair written for emphasis reaches
the reader as two asterisks. Emphasis belongs in the component, which has real
markup; the payload carries the words. `tests/test_story.py` pins this.
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
# without saying whose limit. WHO tightened its guideline in 2021, and the gap
# between the two is itself one of the more useful things a reader can take
# away.
#
# This block used to say "Taiwan's standard has not moved since 2020" and cite
# 「空氣品質標準（2020 年修正）」. Both were wrong, and a reader asked the
# question that found it. PM2.5 was not in 空氣品質標準 at all until it was
# added on 2012-05-14 (民國101年5月14日) at 15 annual / 35 daily, and on
# 2024-09-30 (民國113年9月30日) those were tightened to 12 and 30.
#
# Which limit to count against, on a record that spans the change.
#
# Two kinds of figure ask two different questions, and they get two different
# answers.
#
# A TIME SERIES against the standard — chapter 1's annual line — uses the limit
# in force at the end of each year, drawn as a step and null before PM2.5 was
# regulated at all. There the x axis IS time, so the change in the law is part
# of what the reader is looking at.
#
# A PER-STATION SNAPSHOT — the station card's `days_over_taiwan` — uses ONE
# limit, the current one, for every station and every year. Three reasons, and
# the first is the one that decided it:
#
#   1. The card shows each station's most recent COMPLETE year, and those years
#      differ: 2025 for most, earlier for stations that stopped reporting. Under
#      a contemporaneous rule the same column would silently count against 35
#      for one station and 30 for the next — a number whose denominator moves
#      without saying so, which is the failure this project exists to avoid.
#   2. The card ranks stations against each other (「全台排名 43／77 站」). A
#      ranking needs one yardstick or it is not a ranking.
#   3. The statistic sitting beside it already works this way: 「超過 WHO 日均
#      指引的天數」 applies the 2021 guideline to years before 2021. Counting
#      Taiwan's limit contemporaneously next to a WHO limit applied
#      retroactively would be two rules in one row of one card.
#
# What the card is asking is an exposure question — how many days was the air
# over the line we now consider acceptable — not a compliance question. Whether
# a station broke the law in 2019 is a question about 2019's law, and this site
# is not a compliance report. The site says which limit it used, and the value
# it prints is read from this dict rather than written into the markup, so the
# label cannot drift away from the count again.
#
# PM10 was tightened by the same 2024 revision, and its new values are published
# only as an image on the ministry's page. They are not recorded here: nothing
# on the site consumes PM10 limits, and a constant that is wrong is worse than
# one that is absent. The WHO values stay because those are verifiable.
# Annotated because the inner dict is genuinely heterogeneous — float limits
# beside a nested `source` mapping — and left to infer, mypy widens the values
# to `object`, which then refuses to be compared against a Polars column. The
# shape is what the site reads, so it stays as it is.
GUIDELINES: dict[str, dict[str, Any]] = {
    "PM2.5": {
        "who_2021_annual": 5.0,
        "who_2021_daily": 15.0,
        "taiwan_annual": 12.0,
        "taiwan_daily": 30.0,
        "source": {
            "who": "WHO global air quality guidelines (2021)",
            "taiwan": "空氣品質標準（民國113年9月30日修正；PM2.5 於民國101年5月14日納入）",
        },
    },
    "PM10": {
        "who_2021_annual": 15.0,
        "who_2021_daily": 45.0,
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
    written.extend(_export_sarima(root))
    written.extend(_export_health(root))
    written.extend(_export_imputation(root))
    written.extend(_export_spatial(root))
    return written


def _export_spatial(root: Path) -> list[Path]:
    """The spatial chapter: what 「分區各跑一次」 bought, and what it did not.

    Named ``spatial-structure`` rather than ``spatial`` because this file
    already exports a ``spatial_check`` for M5's falsification tests, rendered
    under 「空間檢定」 in the detection chapter — two payloads whose names
    differ by one word would eventually be confused for each other.

    Every number here is read from the M6 parquets; nothing is recomputed. The
    payload deliberately repeats the two scope limits (OLS stage only; residual
    I is a lower bound) because a payload is quoted without its module
    docstring.
    """
    source = outputs_dir("m6_spatial")
    partition_path = source / "partition_price.parquet"
    if not partition_path.exists():
        log.warning("no M6 output — run `twair analyze m6`")
        return []

    partition = pl.read_parquet(partition_path)
    correlogram = pl.read_parquet(source / "correlogram.parquet")
    residual = pl.read_parquet(source / "residual_autocorrelation.parquet")
    coverage = pl.read_parquet(source / "station_coverage.parquet")
    metadata = {
        row["key"]: row["value"]
        for row in pl.read_parquet(source / "metadata.parquet").iter_rows(named=True)
    }
    agreement = pl.read_parquet(source / "partition_agreement.parquet")
    lisa_table = pl.read_parquet(source / "lisa.parquet")
    inference = pl.read_parquet(source / "inference_price.parquet")

    station_mean = residual.filter(pl.col("scope") == "station_mean").to_dicts()[0]
    era = agreement.filter(pl.col("partition") == "zone_era").to_dicts()[0]
    ward = agreement.filter(pl.col("partition").str.starts_with("ward_")).sort(
        "silhouette", descending=True
    )
    best_ward = ward.to_dicts()[0]
    ward_at_era = agreement.filter(pl.col("partition") == f"ward_k{era['k']}").to_dicts()

    pm10 = inference.filter(pl.col("term") == "PM10")
    iid = inference.filter(pl.col("cov_type") == "iid")
    two_way = inference.filter(pl.col("cov_type") == "cluster_twoway")
    flips = (
        iid.join(two_way, on="term", suffix="_tw")
        .filter((pl.col("p") < 0.05) & (pl.col("p_tw") >= 0.05))["term"]
        .to_list()
    )

    field_path = source / "field_skill.parquet"
    field_summary: list[dict[str, Any]] = []
    field_failed = 0
    if field_path.exists():
        skill = pl.read_parquet(field_path)
        field_failed = skill.filter(pl.col("failed").is_not_null()).height
        scored = skill.filter(pl.col("predicted").is_not_null())
        field_summary = [
            {
                "method": row["method"],
                "buffer_km": row["buffer_km"],
                "mae": _round(row["mae"], 2),
                "rmse": _round(row["rmse"], 2),
                "n": int(row["n"]),
            }
            for row in (
                scored.with_columns(pl.col("error").abs().alias("abs_error"))
                .group_by("method", "buffer_km")
                .agg(
                    pl.col("abs_error").mean().alias("mae"),
                    (pl.col("error") ** 2).mean().sqrt().alias("rmse"),
                    pl.len().alias("n"),
                )
                .sort("buffer_km", "mae")
                .iter_rows(named=True)
            )
        ]

    unplaced = coverage.filter(~pl.col("placed"))["station_name"].to_list()
    quadrants = {
        row["quadrant"]: row["len"]
        for row in lisa_table.group_by("quadrant").len().iter_rows(named=True)
    }

    return [
        write_json(
            root / "spatial-structure.json",
            {
                "network": {
                    "stations": int(metadata["panel_stations"]),
                    "months": int(metadata["panel_months"]),
                    "placed": int(metadata["panel_stations_placed"]),
                    "complete": int(metadata["panel_stations_complete"]),
                    "unplaced": unplaced,
                    "weights": metadata["weights"],
                    "zone_partition": metadata["zone_partition"],
                    "null_draws": int(metadata["residual_null_draws"]),
                    "seed": int(metadata["seed"]),
                },
                "correlogram": [
                    {
                        "lo_km": row["bin_lo_km"],
                        "hi_km": row["bin_hi_km"],
                        "pairs": row["pairs"],
                        "i": _round(row["i"], 4),
                        "z": _round(row["z"], 2),
                        "significant": bool(row["significant_bh"]),
                    }
                    for row in correlogram.iter_rows(named=True)
                    if row["i"] is not None
                ],
                "station_mean_i": {
                    "i": _round(station_mean["i"], 4),
                    "z": _round(station_mean["z"], 2),
                    "p": _round(station_mean["p_simulated"], 4),
                    "n": int(station_mean["n_stations"]),
                },
                "controls": [
                    {
                        "control": row["control"],
                        "params": int(row["rank"]),
                        "r_squared": _round(row["r_squared"], 4),
                        "mean_i": _round(row["mean_i"], 4),
                        "mean_i_lo": _round(row["mean_i_lo"], 4),
                        "mean_i_hi": _round(row["mean_i_hi"], 4),
                        "months_significant_bh": int(row["months_significant_bh"]),
                        "months_scored": int(row["months_scored"]),
                    }
                    for row in partition.iter_rows(named=True)
                ],
                "partition_test": {
                    "groups": int(era["k"]),
                    "separation_r": _round(era["separation_r"], 3),
                    "silhouette": _round(era["silhouette"], 3),
                    "pct_separation": _round(era["pct_vs_geographic_ensemble_separation"], 3),
                    "pct_silhouette": _round(era["pct_vs_geographic_ensemble_silhouette"], 3),
                    "ensemble_draws": int(era["ensemble_draws"]),
                    "best_k": int(best_ward["k"]),
                    "best_k_silhouette": _round(best_ward["silhouette"], 3),
                    "ari_at_official_k": _round(ward_at_era[0]["ari_vs_zone_era"], 2)
                    if ward_at_era
                    else None,
                },
                "lisa": {
                    "stations": int(lisa_table.height),
                    "significant_bh": int(lisa_table["significant_bh"].sum()),
                    "significant_raw": int(lisa_table["significant_raw"].sum()),
                    "quadrants": {q: int(quadrants.get(q, 0)) for q in ("HH", "HL", "LH", "LL")},
                },
                "inference": {
                    "published_t_pm10": _round(
                        pm10.filter(pl.col("cov_type") == "iid")["published_t"][0], 2
                    ),
                    "rows": [
                        {
                            "cov_type": row["cov_type"],
                            "meaning": row["cov_meaning"],
                            "t": _round(row["t"], 2),
                            "se_inflation": _round(row["se_inflation_vs_iid"], 2),
                        }
                        for row in pm10.iter_rows(named=True)
                    ],
                    "flips_two_way": flips,
                },
                "field": {
                    "summary": field_summary,
                    "failed_folds": field_failed,
                },
                # The two sentences that must travel with any quotation of these
                # numbers. The component prints both; keeping them here as well
                # means a future chapter cannot forget them.
                "scope_limits": [
                    "這裡定價的是 OLS 階段。原文最終的推論是 AR(1) 混合模型，"
                    "其標準誤未被記錄於本專案，所以這些數字修正的是它的中間步驟，"
                    "不構成對其整體推論的反駁。",
                    "殘差的空間自相關是場相依性的下界，原因有二：模型裡的解釋變數"
                    "（尤其 PM10）本身就帶空間結構，已吸走一部分訊號；"
                    "而面板是完整案例，網絡本身就被資料完整度篩選過。",
                ],
                "refusals": [
                    "不做人口加權暴露：repo 內沒有任何人口網格，"
                    "拿測站平均乘上任何現有欄位都不是暴露量。",
                    "不出 1 公里濃度場：測站最近鄰距離從 0.6 到 67 公里，"
                    "1 公里的格子宣稱了網絡給不起的解析度。",
                ],
            },
        )
    ]


def _export_sarima(root: Path) -> list[Path]:
    """Chapter 5's coda: the model the 2018 project set aside, priced.

    Two things go in, and they answer different questions. The selection cost is
    the whole of 「實屬不便」 with seconds attached; the horizon table is whether
    the inconvenience bought anything.

    The payload carries `not_comparable` and the chapter prints it, because the
    one thing a reader will want to do with this table is set it beside the
    LightGBM numbers above it — and those are scored on different stations, at
    different origins, over different rows. Putting the two in one chart would be
    the same error this chapter exists to document, committed by the chapter.
    """
    source = outputs_dir("m12_sarima")
    scores_path = source / "scores.parquet"
    if not scores_path.exists():
        log.warning("no M12 output — run `twair analyze m12`")
        return []

    scores = pl.read_parquet(scores_path)
    fits = pl.read_parquet(source / "fits.parquet")
    cost = pl.read_parquet(source / "selection_cost.parquet")

    pooled = (
        scores.group_by("method", "horizon")
        .agg(pl.col("rmse").mean().alias("rmse"), pl.col("n").sum().alias("n"))
        .sort("horizon")
    )

    horizons = []
    for horizon in sorted({int(h) for h in pooled["horizon"].to_list()}):
        at = {
            row["method"]: row["rmse"]
            for row in pooled.filter(pl.col("horizon") == horizon).iter_rows(named=True)
        }
        counted = pooled.filter((pl.col("horizon") == horizon) & (pl.col("method") == "sarima"))[
            "n"
        ]
        rivals = {name: rmse for name, rmse in at.items() if name != "sarima"}
        best = min(rivals, key=lambda name: rivals[name])
        horizons.append(
            {
                "horizon": horizon,
                # Origins, not rows: one origin per forecast, and all three
                # methods are scored on the same ones.
                "origins": int(counted[0]) if counted.len() else 0,
                "sarima_rmse": _round(at["sarima"], 2),
                "persistence_rmse": _round(at.get("persistence"), 2),
                "climatology_rmse": _round(at.get("climatology"), 2),
                "best_baseline": best,
                # Signed so positive always means SARIMA won.
                "margin": _round((rivals[best] - at["sarima"]) / rivals[best], 3),
            }
        )

    return [
        write_json(
            root / "sarima.json",
            {
                "quote": "SARIMA 不在本專題繼續討論⋯⋯在此實屬不便",
                "quote_page": 137,
                "period": [2015, 2025],
                "order": "(1,0,1)(1,0,1)₂₄",
                "stations": int(fits["station_name"].n_unique()),
                "splits": int(fits["split"].n_unique()),
                "origin_stride_hours": 72,
                "fits": {
                    "total": int(fits.height),
                    "converged": int(fits["converged"].sum()),
                    "median_seconds": _round(fits["fit_seconds"].median(), 0),
                    "median_observed": as_int(fits["train_observed"].median()),
                },
                "selection_cost": [
                    {
                        "points": int(row["points"]),
                        "auto_seconds": _round(row["auto_seconds"], 2),
                        "fixed_seconds": _round(row["fixed_seconds"], 2),
                        "multiple": _round(row["search_multiple"], 1),
                    }
                    for row in cost.iter_rows(named=True)
                ],
                "horizons": horizons,
                "why_multiple_grows": (
                    "候選階數的數量與序列長度無關，但每一次擬合的成本隨長度增長，"
                    "所以搜尋對固定階數的倍數本身會放大。逐時單站序列是九萬多點，"
                    "不是五千點。"
                ),
                "why_it_loses_at_one_hour": (
                    "(1,0,1)(1,0,1)₂₄ 沒有差分項，一步預測會往均值回歸，"
                    "而不是承諾「跟剛才一樣」。在 PM2.5 的一小時尺度上，"
                    "承諾比回歸準——所以這不是實作問題，是這個模型的性質。"
                ),
                # No headline sentence here. The component supplies one in
                # markup, and a payload that also states it makes the site print
                # the same claim twice in a row — which it did on the first run.
                "verdict": (
                    "它在最該有用的期距上被一行規則打敗，會贏的地方又贏得不夠多。"
                    "原文給了正確的答案，和一個不完整的理由；現在兩者都有數字。"
                ),
                "not_comparable": (
                    "那些是 74 站、另一組原點、另一批列上算出來的；"
                    "要讓 LightGBM 落在這裡的同一批原點上，得逐站-分割重建整條特徵管線。"
                    "把兩者畫在同一張圖上，正是本章在記錄的那種錯誤。"
                ),
                "no_lightgbm": (
                    "而 D10 要問的問題不需要它：一個連「一小時前的值」都打不贏的方法，"
                    "被放棄就是對的，不管梯度提升樹做得如何。"
                ),
            },
        )
    ]


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

    Every split ships too, not just the mean over them. The first backtest had
    a mean of +0.190 at six hours and one split at -0.111, and a chart drawn
    from means alone would have made the same mistake the module's own summary
    line made. The corrected frame has no losing cell, which is a reason to keep
    shipping the splits rather than to stop: nothing announced the first one
    either.
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
                "stations": int(row["stations"]),
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
                        "claim": "R² 掉三倍的同時，skill 沒有跟著掉",
                        "detail": (
                            "R² 從 0.859 掉到 0.289，而 skill 收在比起點更高的地方。"
                            "R² 衡量的是「這個目標本來多好預測」，而 PM2.5 一小時後"
                            "任何人都很好預測——包括一條說「跟現在一樣」的規則。"
                            "skill 問的是不同的問題：模型有沒有加到東西。"
                            "它不是單調上升的（1、6、24、48 小時分別是 +0.172、+0.237、"
                            "+0.196、+0.315），但確實沒有隨 R² 一起崩，"
                            "因為 persistence 衰退得比模型快。"
                        ),
                    },
                    {
                        "claim": "只看一條基準線，一定會高估模型",
                        "detail": (
                            "vs persistence 在 48 小時最高，單看這條會讀成「愈遠愈好」。"
                            "但 vs climatology 在同一段從 +0.84 崩到 +0.17——"
                            "兩天後模型已經大致退化成「這站、這個月、這個小時的平均」。"
                            "在 persistence 已經輸給長期平均的地方贏過 persistence，不算成就。"
                            "實用範圍大約到 24 小時。"
                        ),
                    },
                    {
                        "claim": "平均值會藏掉一個輸掉的分割——而且真的藏過一次",
                        "detail": (
                            "第一次回測時，16 個「期距 × 分割」格子裡有一個是 −0.111"
                            "（6 小時、訓練資料最少的 rolling_1），四個期距的平均卻全是正的。"
                            "這跟「R² 藏掉一個爛模型」是同一個錯，只是高了一層。"
                            "現在這張表沒有負的格子，最差是 +0.080，仍在同一格——"
                            "讓它變號的是修掉 features/lags.py 裡一個跟預測無關的洩漏"
                            "（每個測站的前 167 小時帶著前一個測站的歷史）。"
                            "所以最差分割照畫：第一次也沒有任何東西提醒過我們。"
                        ),
                    },
                    {
                        "claim": "6 小時仍是最不穩的期距，但當初那個負值是我們自己的 bug",
                        "detail": (
                            "6 小時的四個分割是 +0.080 到 +0.305，跨度 0.225，"
                            "仍是四個期距裡最大的（1 小時 0.074、24 小時 0.139、48 小時 0.055）。"
                            "但這一章原本寫的是「兩批獨立資料都說 6 小時會輸」——"
                            "回測有一格 −0.111，互動 demo 的 6 小時 skill 是 −0.043、"
                            "六個測站沒有一個明顯為正。兩批都翻面了："
                            "同一個 demo 在修好洩漏後重跑，6 小時 skill 是 +0.256，"
                            "六站全為正（+0.098 到 +0.384）。"
                            "兩批資料確實指向同一件事，只是那件事是 features/lags.py 的 bug，"
                            "不是大氣。留著這段，是因為「兩個獨立來源同意」"
                            "在它們共用同一個特徵建構器時，並不構成獨立證據。"
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


def wind_peak_class(peak_speed: str | None) -> str:
    if peak_speed in {"6-8", "8+"}:
        return "high_wind_peak"
    if peak_speed in {"<0.5", "0.5-1.5"}:
        return "low_wind_peak"
    return "mid_wind_peak"


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
            "wind_peak_class": wind_peak_class(row["peak_speed"]),
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

    counts = dict.fromkeys(("low_wind_peak", "mid_wind_peak", "high_wind_peak"), 0)
    for entry in by_station.values():
        counts[entry["wind_peak_class"]] += 1

    return [
        write_json(
            root / "sources.json",
            {
                "method": "CBPF (Uria-Tellaetxe & Carslaw, 2014)",
                "explains": "給定風從某方位、以某風速吹來，該小時濃度落在高值區的機率。",
                "percentile": _round(summary["percentile"][0], 0),
                "min_bin_count": 20,
                "null_means": "該格觀測時數不足 20 小時，機率不予報告（不是機率為零）",
                "cannot_say": (
                    "這張圖不能判定來源地、距離、來源身分或各來源的貢獻。"
                    "歸因需要化學成分、受體模式、軌跡／擴散模式或排放清冊等獨立證據。"
                ),
                "sectors": sectors,
                "speed_bins": speed_order,
                "wind_peak_counts": counts,
                "median_calm_fraction": _round(summary["calm_fraction"].median(), 4),
                "n_suppressed_bins": int(summary["n_suppressed_bins"].sum()),
                "stations": by_station,
            },
        )
    ]


MIN_MONTHS_FOR_A_YEAR = 11


def _deweather_series() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """The two annual lines, both drawn from M4's own monthly output.

    **Both series come from the same rows.** That is the point of building them
    here rather than plotting the normalised line against chapter 1's existing
    trend: chapter 1's series comes from the daily aggregates over a 68-station
    panel, M4 fitted 74 stations, and a difference between two lines drawn from
    two different station sets is partly the station sets. Comparing M4's
    ``observed`` against M4's ``normalised`` removes that entirely.

    Annual, never monthly. The normalised monthly series is nearly flat within
    a year by construction — ``doy`` and ``hour`` are resampled along with the
    meteorology, so the seasonal cycle goes with them. Plotted monthly it looks
    like a broken chart; annually it is the trend comparison it was built for.

    The panel is balanced on the same rule chapter 1 uses: the start year that
    maximises station-years, with a station-year counting only if the station
    reported at least ``MIN_MONTHS_FOR_A_YEAR`` months.
    """
    monthly_path = outputs_dir("m4_deweather") / "monthly.parquet"
    if not monthly_path.exists():
        return [], {}

    monthly = pl.read_parquet(monthly_path).with_columns(pl.col("month").dt.year().alias("year"))
    complete = (
        monthly.group_by("station_name", "year")
        .agg(pl.len().alias("months"))
        .filter(pl.col("months") >= MIN_MONTHS_FOR_A_YEAR)
    )
    years = sorted(complete["year"].unique().to_list())

    best_start, best_members, best_score = None, [], -1
    for start in years:
        window = [y for y in years if y >= start]
        members = (
            complete.filter(pl.col("year").is_in(window))
            .group_by("station_name")
            .agg(pl.len().alias("n"))
            .filter(pl.col("n") == len(window))["station_name"]
            .to_list()
        )
        score = len(members) * len(window)
        if score > best_score:
            best_start, best_members, best_score = start, sorted(members), score

    if best_start is None or not best_members:
        log.warning("no balanced panel in the M4 monthly output — skipping the annual series")
        return [], {}

    window = [y for y in years if y >= best_start]
    panel = monthly.filter(
        pl.col("station_name").is_in(best_members) & pl.col("year").is_in(window)
    )
    annual = (
        panel.group_by("year")
        .agg(
            pl.col("observed").mean().alias("observed"),
            pl.col("normalised").mean().alias("normalised"),
        )
        .sort("year")
    )

    first, last = annual.row(0, named=True), annual.row(-1, named=True)
    observed_fall = first["observed"] - last["observed"]
    normalised_fall = first["normalised"] - last["normalised"]

    provenance = {
        "balanced_since": best_start,
        "n_stations": len(best_members),
        "stations": best_members,
        "station_years": best_score,
        "selection_rule": (
            f"最大化站年數的起始年，且一站一年至少要有 {MIN_MONTHS_FOR_A_YEAR} 個月才算數"
        ),
        "why_same_source": (
            "兩條線都出自 M4 的同一批逐月輸出。拿正規化序列去跟第一章既有的"
            "趨勢線比會多一個差異來源——那條線來自日聚合、68 站的面板，"
            "而 M4 配適的是 74 站。"
        ),
        "observed_fall": _round(observed_fall, 2),
        "normalised_fall": _round(normalised_fall, 2),
        # A second, independent way of asking the same question as
        # `median_weather_share`: that one is the median across per-station
        # slope ratios, this one is the level difference across the national
        # panel. They are different aggregations of the same fits, so they are
        # both reported — agreement between them is worth more than either.
        "weather_share_of_fall": _round(
            (observed_fall - normalised_fall) / observed_fall if observed_fall else None, 3
        ),
    }
    series = [
        {
            "year": row["year"],
            "observed": _round(row["observed"], 2),
            "normalised": _round(row["normalised"], 2),
        }
        for row in annual.iter_rows(named=True)
    ]
    return series, provenance


def _export_deweather(root: Path) -> list[Path]:
    """Chapter 1's second line: how much of the trend was the weather."""
    source = outputs_dir("m4_deweather") / "summary.parquet"
    if not source.exists():
        log.warning("no M4 output — run `twair analyze m4`")
        return []

    summary = pl.read_parquet(source)
    significant = summary.filter(pl.col("normalised_significant"))
    stations = _stations()
    series, panel = _deweather_series()

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
                # The two annual lines and the panel they were drawn on. Empty
                # if M4's monthly output is absent, so the chapter degrades to
                # the slope figures rather than failing the export.
                "series": series,
                "panel": panel,
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
                    "並會留在剩餘趨勢裡。它無法與排放、化學反應或其他未建模因素分開。"
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
    interpretable once you know the same procedure returns -0.69 in unmarked
    control windows.
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

    # The selected eight-station comparison stays observational because no
    # dispersion model establishes where a plant effect would have to appear.
    near_taichung = ["沙鹿", "線西", "忠明", "西屯", "大里", "豐原", "彰化", "二林"]
    taichung = effects.filter(
        pl.col("event").str.contains("台中電廠") & pl.col("station").is_in(near_taichung)
    ).sort("effect")

    return [
        write_json(
            root / "detection-limit.json",
            {
                "method": {
                    "window": (
                        "事件日曆窗口內「觀測值減去模型預測值」的差額；"
                        "這是待檢驗的事件訊號，不等同於已識別的因果效應。"
                    ),
                    "trend_break": "在氣象正規化後的月序列上做分段迴歸，比較斜率",
                    "placebo": "同一套程序，跑在未標記為該事件年份的對照窗口／其他候選斷點上",
                    "threshold": "效應要距離安慰劑均值 2 個標準差才算偵測到",
                },
                "events": windowed,
                "spatial_check": {
                    "label": "台中電廠周邊測站",
                    "why": (
                        "這項空間檢查比較預先選定的台中電廠周邊 8 站。"
                        "實測 8 站全部落在安慰劑散布之內，其中 4 站的觀測－預測差額為正。"
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
