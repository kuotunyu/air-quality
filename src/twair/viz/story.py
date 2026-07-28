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

from twair.paths import outputs_dir, processed_dir
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
GUIDELINES = {
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


def balanced_panel_options(annual: pl.DataFrame) -> pl.DataFrame:
    """The trade-off between how far back a balanced panel reaches and how wide it is.

    For every candidate start year, how many stations reported comparably in
    *every* year from then to the end of the record, and how many station-years
    that panel therefore contains.

    This exists because the obvious choice — "balance across the whole record"
    — is a trap. PM2.5 measurement began at a handful of experimental sites in
    1998 and only became a national network in 2005, so a panel balanced back
    to 1998 contains **two stations**, and its "national trend" is really the
    trend at two places. Starting later buys breadth at the cost of length, and
    there is no answer that is right in the abstract. The curve is published so
    the reader can see the choice that was made and what the alternatives were.
    """
    if annual.is_empty():
        return pl.DataFrame(
            schema={
                "start_year": pl.Int32,
                "n_stations": pl.UInt32,
                "n_years": pl.Int32,
                "station_years": pl.Int32,
            }
        )

    last = int(annual["year"].max())
    rows = []
    for start in sorted({int(y) for y in annual["year"].unique()}):
        window = annual.filter(pl.col("year") >= start)
        n_years = last - start + 1
        complete = (
            window.group_by("station_name")
            .agg(pl.col("year").n_unique().alias("years"))
            .filter(pl.col("years") == n_years)
        )
        rows.append(
            {
                "start_year": start,
                "n_stations": complete.height,
                "n_years": n_years,
                "station_years": complete.height * n_years,
            }
        )
    return pl.DataFrame(rows).with_columns(
        pl.col("start_year").cast(pl.Int32),
        pl.col("n_stations").cast(pl.UInt32),
        pl.col("n_years").cast(pl.Int32),
        pl.col("station_years").cast(pl.Int32),
    )


def choose_balanced_start(options: pl.DataFrame) -> int | None:
    """Pick the window with the most station-years, ties going to the longer record.

    A stated, computable rule rather than a judgement call, so that the same
    data always yields the same window and the choice can be argued with.
    """
    if options.is_empty():
        return None
    best = options.sort(["station_years", "n_years"], descending=[True, True]).row(0, named=True)
    return int(best["start_year"]) if best["n_stations"] > 0 else None


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
        n_years = int(annual["year"].max()) - start + 1
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
    return written


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


def _round(value: float | None, places: int = 2) -> float | None:
    return None if value is None else round(value, places)


def _stations() -> pl.DataFrame:
    path = outputs_dir("qc") / "stations.parquet"
    if not path.exists():
        raise FileNotFoundError(f"{path} not found — run `twair stations` first")
    return pl.read_parquet(path)
