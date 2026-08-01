"""The repair pass, which rewrites the canonical store in place.

`store/repair.py` exists so a corrected QC rule does not cost a re-parse of 44
annual archives, and it does its work by overwriting partitions of the store
this whole project is built on. It was 46 statements at 0% coverage.

Its module docstring makes one strong claim — "Every pass here is idempotent by
construction ... running it repeatedly is safe and running it on a partially
repaired store is safe" — and that claim was never executed. These tests
execute it, along with the three properties that make an in-place rewrite
survivable: a clean partition is not touched, a partition whose VALUES are
already correct but whose DTYPES have drifted still is, and nothing is left
behind if the process is interrupted mid-write.

The dtype case is not hypothetical. The comment above it records that an
earlier repair wrote `flag` as String into 298 partitions, the store stopped
scanning, and re-running found nothing to fix because every value was already
right.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import polars as pl
import pytest

from twair.qc.flags import Flag
from twair.store.repair import repair_partition, repair_store
from twair.store.schema import PARTITION_SCHEMA, conform_partition


def partition(rows: list[dict[str, object]]) -> pl.DataFrame:
    """A frame with the store's real dtypes, so a test cannot pass on a shape
    the store would reject."""
    frame = pl.DataFrame(rows)
    for missing in PARTITION_SCHEMA:
        if missing not in frame.columns:
            frame = frame.with_columns(pl.lit(None).alias(missing))
    return conform_partition(frame.select(list(PARTITION_SCHEMA)))


def a_sentinel_row(**over: object) -> dict[str, object]:
    """A wind direction of 999 that the agency still calls valid.

    That is the case `apply_sentinels` exists for: 999 is 靜風, not an angle,
    and it has to become a null carrying the `calm` flag.
    """
    row: dict[str, object] = {
        "station_name": "板橋",
        "pollutant": "WIND_DIREC",
        "ts_local": datetime(2001, 3, 4, 5),
        "value": 999.0,
        "flag": Flag.VALID.value,
        "value_retained": True,
        "imputed": False,
        "impute_method": None,
        "generation": "gen2",
        "source_member": "x.csv",
    }
    row.update(over)
    return row


def a_clean_row(**over: object) -> dict[str, object]:
    return a_sentinel_row(pollutant="PM2.5", value=12.5, **over)


def write(path: Path, frame: pl.DataFrame) -> Path:
    frame.write_parquet(path)
    return path


def test_a_sentinel_is_nulled_and_flagged(tmp_path: Path) -> None:
    path = write(tmp_path / "p.parquet", partition([a_sentinel_row()]))

    rows, changed, rewritten = repair_partition(path)

    assert (rows, changed, rewritten) == (1, 1, True)
    after = pl.read_parquet(path)
    assert after["value"][0] is None
    assert after["flag"][0] == Flag.CALM.value


def test_repair_is_idempotent(tmp_path: Path) -> None:
    """The module's own central claim, executed.

    Everything downstream rests on it: the repair is offered as something you
    run when a rule changes, on a store that may already be half repaired.
    """
    path = write(tmp_path / "p.parquet", partition([a_sentinel_row(), a_clean_row()]))

    first = repair_partition(path)
    after_first = pl.read_parquet(path)

    second = repair_partition(path)
    after_second = pl.read_parquet(path)

    assert first[1] == 1, "the sentinel should have been corrected on the first pass"
    assert second[1] == 0, "a second pass found something to change"
    assert second[2] is False, "a second pass rewrote a file it had nothing to fix"
    assert after_first.equals(after_second)


def test_a_clean_partition_is_not_rewritten(tmp_path: Path) -> None:
    """521 partitions, and rewriting them all to change nothing is the cost."""
    path = write(tmp_path / "p.parquet", partition([a_clean_row()]))
    before = path.read_bytes()

    rows, changed, rewritten = repair_partition(path)

    assert (rows, changed, rewritten) == (1, 0, False)
    assert path.read_bytes() == before, "an untouched partition was rewritten"


def test_dtype_drift_is_repaired_even_though_no_value_changes(tmp_path: Path) -> None:
    """The 298-partition bug, pinned.

    A partition whose values are all correct but whose `flag` is String rather
    than Categorical makes the store unscannable, and reports zero changes. If
    the rewrite were driven by `changed` alone, re-running would keep finding
    nothing and the store would stay broken.
    """
    drifted = partition([a_clean_row()]).with_columns(pl.col("flag").cast(pl.Utf8))
    path = write(tmp_path / "p.parquet", drifted)
    assert pl.read_parquet(path).schema["flag"] == pl.Utf8

    _rows, changed, rewritten = repair_partition(path)

    assert changed == 0, "no value should have changed"
    assert rewritten is True, "schema drift did not trigger a rewrite"
    assert pl.read_parquet(path).schema["flag"] == PARTITION_SCHEMA["flag"]


def test_nothing_is_left_behind_after_a_rewrite(tmp_path: Path) -> None:
    """The swap is a rename, so an interrupted run cannot leave a partial file.

    The test that matters is the one after a SUCCESSFUL run: a stray `.tmp`
    beside a partition would be picked up by the next `rglob("*.parquet")`
    depending on how it was named, and is dead weight regardless.
    """
    path = write(tmp_path / "p.parquet", partition([a_sentinel_row()]))

    repair_partition(path)

    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "p.parquet"]
    assert leftovers == [], f"repair left files behind: {leftovers}"


def test_an_empty_partition_is_left_alone(tmp_path: Path) -> None:
    empty = partition([]).clear()
    path = write(tmp_path / "p.parquet", empty)
    before = path.read_bytes()

    assert repair_partition(path) == (0, 0, False)
    assert path.read_bytes() == before


def test_repair_store_walks_every_partition(tmp_path: Path) -> None:
    (tmp_path / "2001").mkdir()
    (tmp_path / "2002").mkdir()
    write(tmp_path / "2001" / "a.parquet", partition([a_sentinel_row()]))
    write(tmp_path / "2002" / "b.parquet", partition([a_clean_row(), a_sentinel_row()]))

    summary = repair_store(tmp_path)

    assert summary["partitions"] == 2
    assert summary["rows"] == 3
    assert summary["changed"] == 2
    assert summary["rewritten"] == 2


def test_repair_store_on_an_empty_tree_is_not_an_error(tmp_path: Path) -> None:
    """A fresh clone has no store, and reporting that as a crash helps nobody."""
    summary = repair_store(tmp_path)
    assert summary == {"partitions": 0, "rows": 0, "changed": 0}


@pytest.mark.parametrize(
    "flag",
    [Flag.INSTRUMENT_CHECK_INVALID.value, Flag.MANUAL_CHECK_INVALID.value],
)
def test_an_agency_rejection_outranks_our_inference(tmp_path: Path, flag: str) -> None:
    """`apply_sentinels` only rewrites cells the agency still calls valid.

    A 999 that the agency has already rejected keeps its own flag: an explicit
    instrument-check result is more authoritative than reading the number as a
    sentinel, and overwriting it would destroy the more specific fact.
    """
    path = write(tmp_path / "p.parquet", partition([a_sentinel_row(flag=flag)]))

    _, changed, _ = repair_partition(path)

    assert changed == 0
    assert pl.read_parquet(path)["flag"][0] == flag
