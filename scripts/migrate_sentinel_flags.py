"""One-off migration: correct the wind-direction sentinel labels in the store.

`twair repair` cannot do this one, and the reason is worth recording.

The sentinel pass replaces the in-band code with null and records what it was
in the flag. That is the right thing to do — 888 is not a bearing — but it
means the original code is gone. When the *meaning* of the codes was corrected
(see `twair.qc.sentinels`), repair scanned all 341 million rows and changed
nothing, because there was no longer an 888 or a 999 anywhere to reclassify.

The flag is now the only surviving record of which code fired, and the mapping
from old flag to original value is exact:

    flag "calm"             <- was 888   -> should be "variable_direction"
    flag "instrument_fault" <- was 999   -> should be "calm"

Nothing else in the pipeline emits either flag, so the rename is unambiguous.
It is a **swap**, so both columns are computed from the pre-migration values in
one expression; renaming them in sequence would send everything to "calm".

Idempotent: a store that already contains "variable_direction" has been
migrated and is left alone.

    uv run python scripts/migrate_sentinel_flags.py [--dry-run]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import polars as pl

from twair.paths import processed_dir
from twair.qc.flags import Flag
from twair.store.schema import conform_partition
from twair.store.writer import COMPRESSION, OBSERVATIONS

# old flag -> corrected flag
RENAME = {
    Flag.CALM.value: Flag.VARIABLE_DIRECTION.value,
    Flag.INSTRUMENT_FAULT.value: Flag.CALM.value,
}


def migrate_partition(path: Path, *, dry_run: bool = False) -> int:
    frame = pl.read_parquet(path)
    if frame.is_empty():
        return 0

    flags = frame["flag"].cast(pl.Utf8)

    if (flags == Flag.VARIABLE_DIRECTION.value).any():
        return 0  # already migrated

    affected = int(flags.is_in(list(RENAME)).sum())
    if affected == 0 or dry_run:
        return affected

    # One expression over the original column: a sequential rename would map
    # calm -> variable_direction and then instrument_fault -> calm, which is
    # correct, but the reverse order would collapse both into variable_direction.
    # Computing from `flags` removes the ordering hazard entirely.
    migrated = frame.with_columns(
        pl.Series("flag", [RENAME.get(value, value) for value in flags.to_list()])
    )

    tmp = path.with_suffix(".parquet.tmp")
    conform_partition(migrated).write_parquet(tmp, compression=COMPRESSION, statistics=True)
    tmp.replace(path)
    return affected


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Count without writing.")
    args = parser.parse_args()

    files = sorted(processed_dir(OBSERVATIONS).rglob("*.parquet"))
    if not files:
        print("no partitions found", file=sys.stderr)
        return 1

    total = 0
    touched = 0
    for path in files:
        affected = migrate_partition(path, dry_run=args.dry_run)
        total += affected
        touched += 1 if affected else 0

    verb = "would relabel" if args.dry_run else "relabelled"
    print(f"{verb} {total:,} row(s) across {touched} partition(s) of {len(files)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
