"""Replace in-band sentinel codes with explicit flags.

MOENV encodes two non-measurements inside the wind-direction value itself.
From ``ReadMe_普通測站20170301.odt``:

    風向資料888代表無風，999則代表儀器故障

These are not bearings. Left in place they are silently averaged as if the wind
blew from 888°, which is exactly what happens when wind direction is fed into a
regression as an ordinary continuous variable.

Empirically the practice is era-dependent — see :func:`sentinel_report` and
docs/archive-formats.md for measured rates — so this pass is applied
unconditionally rather than gated on a year range.
"""

from __future__ import annotations

from typing import Any

import polars as pl

from twair.config import load_conf
from twair.qc.flags import Flag

__all__ = ["apply_sentinels", "sentinel_columns", "sentinel_report"]

_FLAG_BY_NAME = {
    "calm": Flag.CALM,
    "instrument_fault": Flag.INSTRUMENT_FAULT,
}


def sentinel_columns(config: dict[str, Any] | None = None) -> dict[str, dict[float, Flag]]:
    """Map pollutant code -> {sentinel value: flag}, from conf/pollutants.yaml."""
    conf = config if config is not None else load_conf("pollutants")
    sets = conf.get("sentinels", {})

    mapping: dict[str, dict[float, Flag]] = {}
    for code, meta in conf.get("pollutants", {}).items():
        set_name = meta.get("sentinel_set")
        if not set_name:
            continue
        codes = sets.get(set_name, {})
        mapping[code] = {
            float(value): _FLAG_BY_NAME[name]
            for value, name in codes.items()
            if name in _FLAG_BY_NAME
        }
    return mapping


def apply_sentinels(
    frame: pl.DataFrame,
    *,
    config: dict[str, Any] | None = None,
) -> pl.DataFrame:
    """Null out sentinel readings and flag them.

    Only cells the agency already considers ``valid`` are rewritten: an
    explicit instrument-check rejection is more authoritative than our
    inference, so those keep their original flag.
    """
    mapping = sentinel_columns(config)
    if not mapping:
        return frame

    is_sentinel = pl.lit(False)
    flag_expr = pl.col("flag")

    for pollutant, codes in mapping.items():
        for value, flag in codes.items():
            hit = (
                (pl.col("pollutant") == pollutant)
                & (pl.col("value") == value)
                & (pl.col("flag") == Flag.VALID.value)
            )
            is_sentinel = is_sentinel | hit
            flag_expr = pl.when(hit).then(pl.lit(flag.value)).otherwise(flag_expr)

    return frame.with_columns(
        pl.when(is_sentinel).then(None).otherwise(pl.col("value")).alias("value"),
        flag_expr.alias("flag"),
    )


def sentinel_report(frame: pl.DataFrame) -> pl.DataFrame:
    """Per-year sentinel rates, for the Phase 2 write-up.

    Answers the question the 2018 project could not: how often were 888/999
    actually present in the years it analysed?
    """
    wind = frame.filter(pl.col("flag").is_in([Flag.CALM.value, Flag.INSTRUMENT_FAULT.value]))
    if wind.is_empty():
        return pl.DataFrame(
            schema={"year": pl.Int32, "pollutant": pl.Utf8, "flag": pl.Utf8, "n": pl.UInt32}
        )

    return (
        wind.with_columns(pl.col("ts_local").dt.year().cast(pl.Int32).alias("year"))
        .group_by("year", "pollutant", "flag")
        .len(name="n")
        .sort("year", "pollutant", "flag")
    )
