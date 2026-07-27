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

from twair.config import load_conf
from twair.store.writer import scan_observations

log = logging.getLogger(__name__)

__all__ = [
    "alias_map",
    "build_station_table",
    "derive_airzones",
    "normalise_name_expr",
    "station_type_map",
]

# `99年 中部空品區/…` — the leading 民國 year prefix varies, so match the tail.
_AIRZONE = re.compile(r"([一-鿿]+空品區)")


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
            pl.col("source_member").str.extract(_AIRZONE.pattern, 1).alias("airzone")
        )
        .filter(pl.col("airzone").is_not_null())
        .select("station_name", "airzone")
        .unique()
    )

    # A station should map to exactly one zone; report it if not.
    conflicts = (
        zoned.group_by("station_name")
        .agg(pl.col("airzone").n_unique().alias("n"))
        .filter(pl.col("n") > 1)
    )
    if not conflicts.is_empty():
        log.warning("stations with conflicting airzones: %s", conflicts["station_name"].to_list())

    resolved = zoned.unique(subset=["station_name"], keep="first")

    all_stations = pairs.select("station_name").unique()
    overrides = conf.get("airzone_overrides", {}) or {}

    return (
        all_stations.join(resolved, on="station_name", how="left")
        .with_columns(
            pl.when(pl.col("airzone").is_null())
            .then(pl.col("station_name").replace_strict(overrides, default=None))
            .otherwise(pl.col("airzone"))
            .alias("airzone")
        )
        .sort("station_name")
    )


def build_station_table(root: Path | None = None) -> pl.DataFrame:
    """Assemble everything known about each station from the store itself.

    Coordinates are not here: they come from MOENV dataset ``AQX_P_07``, which
    needs an API key. Everything in this table is derivable from the archives
    alone, so Phase 1 does not block on credentials.
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

    return (
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
            "station_type",
            "dual_role",
            "first_year",
            "last_year",
            "years_present",
            "span",
            "has_gap",
        )
        .sort("airzone", "station_name")
    )
