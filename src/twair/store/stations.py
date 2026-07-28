"""Station identity, air-quality zone and station type.

Three problems the raw archives leave unsolved:

1. **Renames.** MOENV switched the orthography of some station names
   (台南 → 臺南). Left alone, one station becomes two disjoint series.
2. **Missing air-quality zone.** Pre-2018 archives encode the zone in the
   member path (``99年 中部空品區/99年二林站_….csv``); 2018+ archives are flat
   and drop it entirely. The zone is therefore recovered from the older files
   and carried forward.
3. **Station type.** Traffic, industrial, background and national-park stations
   measure fundamentally different things. The 2018 project treated this as a
   nuisance and discarded the pollutants that were not common to all types;
   here it becomes a model variable.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import polars as pl

from twair.config import ConfigError, load_conf
from twair.store.writer import scan_observations

log = logging.getLogger(__name__)

__all__ = [
    "airzone_pattern",
    "alias_map",
    "attach_geography",
    "build_station_table",
    "derive_airzones",
    "normalise_name_expr",
    "station_type_map",
]


def airzone_pattern(config: dict[str, Any] | None = None) -> str:
    """Regex alternation over the known air-quality zones.

    Deliberately an explicit list rather than a wildcard. Folder names carry a
    民國 year prefix with inconsistent spacing (``99年 北部空品區`` and
    ``97年中部空品區`` both occur), and a greedy CJK match swallows the 年,
    splitting each zone into two spellings.
    """
    conf = config if config is not None else load_conf("stations")
    zones = conf.get("airzones") or []
    if not zones:
        raise ConfigError("conf/stations.yaml defines no `airzones`")
    return "(" + "|".join(re.escape(z) for z in zones) + ")"


def alias_map(config: dict[str, Any] | None = None) -> dict[str, str]:
    conf = config if config is not None else load_conf("stations")
    return dict(conf.get("aliases", {}))


def station_type_map(config: dict[str, Any] | None = None) -> dict[str, str]:
    """Station name -> type, with the configured default applied elsewhere."""
    conf = config if config is not None else load_conf("stations")
    mapping: dict[str, str] = {}
    for kind, names in conf.get("station_types", {}).items():
        for name in names or []:
            mapping[name] = kind
    return mapping


def normalise_name_expr(
    column: str = "station_name",
    config: dict[str, Any] | None = None,
) -> pl.Expr:
    """Map historical spellings onto the current official name."""
    aliases = alias_map(config)
    expr = pl.col(column).cast(pl.Utf8).str.strip_chars()
    if aliases:
        expr = expr.replace(aliases)
    return expr.alias(column)


def derive_airzones(
    frame: pl.DataFrame | pl.LazyFrame,
    config: dict[str, Any] | None = None,
) -> pl.DataFrame:
    """Recover each station's air-quality zone from legacy member paths.

    Returns one row per station. Stations that only ever appear in the flat
    modern archives fall back to ``airzone_overrides`` in conf/stations.yaml,
    and are left null if not listed there — a null zone is a visible gap, not
    a silent guess.
    """
    conf = config if config is not None else load_conf("stations")
    lazy = frame.lazy() if isinstance(frame, pl.DataFrame) else frame

    pairs = (
        lazy.select(
            normalise_name_expr(config=conf),
            pl.col("source_member").cast(pl.Utf8),
        )
        .unique()
        .collect()
    )

    zoned = (
        pairs.with_columns(
            pl.col("source_member").str.extract(airzone_pattern(conf), 1).alias("airzone")
        )
        .filter(pl.col("airzone").is_not_null())
        .group_by("station_name", "airzone")
        .agg(pl.len().alias("members"))
    )

    # Some stations appear under more than one zone. Two causes, both real:
    #
    #   * upstream filing errors — 臺南, 臺西, 嘉義 and others each show up once
    #     in a 北部空品區 folder inside the 1999 package alone, against 23
    #     members under 雲嘉南空品區;
    #   * genuine reclassification — 阿里山 is filed under 中部空品區 (6) and
    #     雲嘉南空品區 (3) across overlapping years.
    #
    # Majority by member count settles both deterministically, and the fact
    # that a station was ambiguous is kept as a column rather than discarded.
    ranked = zoned.sort(["station_name", "members", "airzone"], descending=[False, True, False])
    resolved = ranked.unique(subset=["station_name"], keep="first").select(
        "station_name", "airzone"
    )

    ambiguity = zoned.group_by("station_name").agg(
        (pl.col("airzone").n_unique() > 1).alias("airzone_ambiguous")
    )

    contested = ambiguity.filter(pl.col("airzone_ambiguous"))["station_name"].to_list()
    if contested:
        log.info("airzone resolved by majority for %d station(s): %s", len(contested), contested)

    all_stations = pairs.select("station_name").unique()
    overrides = conf.get("airzone_overrides", {}) or {}

    return (
        all_stations.join(resolved, on="station_name", how="left")
        .join(ambiguity, on="station_name", how="left")
        .with_columns(
            pl.when(pl.col("airzone").is_null())
            .then(pl.col("station_name").replace_strict(overrides, default=None))
            .otherwise(pl.col("airzone"))
            .alias("airzone"),
            pl.col("airzone_ambiguous").fill_null(False),
        )
        .sort("station_name")
    )


def build_station_table(root: Path | None = None, *, geography: bool = True) -> pl.DataFrame:
    """Assemble everything known about each station.

    The activity, zone and type columns are derived from the archives alone, so
    this runs without credentials. Geography is joined from the cached MOENV
    station register (``conf/station_geo.yaml``) when it is present, and left
    null when it is not — a station whose coordinates are unknown says so
    rather than disappearing.

    The register's own ``airzone_official`` and ``station_type_official`` are
    carried alongside this project's values rather than replacing them. The
    register describes stations as they are classified today; the archive paths
    and the 1994 ReadMe describe how they were classified while most of the
    data was being recorded. Where the two disagree, the disagreement is the
    interesting part.
    """
    conf = load_conf("stations")
    lazy = scan_observations(root)

    activity = (
        lazy.select(
            normalise_name_expr(config=conf),
            pl.col("ts_local").dt.year().cast(pl.Int16).alias("year"),
        )
        .group_by("station_name")
        .agg(
            pl.col("year").min().alias("first_year"),
            pl.col("year").max().alias("last_year"),
            pl.col("year").n_unique().alias("years_present"),
        )
        .collect()
    )

    zones = derive_airzones(lazy, conf)
    types = station_type_map(conf)
    default_type = conf.get("default_station_type", "general")
    dual = set(conf.get("dual_role", []) or [])

    table = (
        activity.join(zones, on="station_name", how="left")
        .with_columns(
            pl.col("station_name")
            .replace_strict(types, default=default_type)
            .alias("station_type"),
            pl.col("station_name").is_in(list(dual)).alias("dual_role"),
            (pl.col("last_year") - pl.col("first_year") + 1).alias("span"),
        )
        .with_columns((pl.col("years_present") < pl.col("span")).alias("has_gap"))
        .select(
            "station_name",
            "airzone",
            "airzone_ambiguous",
            "station_type",
            "dual_role",
            "first_year",
            "last_year",
            "years_present",
            "span",
            "has_gap",
        )
    )

    if geography:
        table = attach_geography(table)

    return table.sort("airzone", "station_name")


def attach_geography(table: pl.DataFrame) -> pl.DataFrame:
    """Left-join the cached station register onto a station table.

    Imported lazily because ``ingest.station_meta`` normalises names with this
    module, and a module-level import in both directions is a cycle.
    """
    from twair.ingest.station_meta import load_station_geo

    geo = load_station_geo().drop("address")
    joined = table.join(geo, on="station_name", how="left")

    unplaced = joined.filter(pl.col("lat").is_null())["station_name"].to_list()
    if unplaced:
        log.info(
            "%d station(s) present in the archives but absent from the MOENV register, "
            "so without coordinates: %s",
            len(unplaced),
            unplaced,
        )
    return joined
