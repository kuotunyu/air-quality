"""Balanced panels — comparing like with like across a growing network.

Split out of `viz/story.py` because two chapters need the same rule and a
second copy would drift. Chapter 1 uses it for the national concentration
trend; M10 uses it for the attributable fraction, where the problem is sharper:
1998 has five stations with usable PM2.5 coverage and 2025 has 77, so an
unbalanced comparison partly measures the network rather than the air.

Nothing here knows about the web export or about health. It is a rule over a
frame with `station_name` and `year`.
"""

from __future__ import annotations

import polars as pl

from twair.scalars import as_int

__all__ = ["balanced_panel_options", "choose_balanced_start"]


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

    last = as_int(annual["year"].max())
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
