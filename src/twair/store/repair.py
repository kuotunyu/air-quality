"""Re-apply the quality passes to an already-built store.

When a QC rule is corrected, the alternative to this is re-parsing 44 annual
archives — hours of work to fix something that is a pure function of what is
already on disk. Every pass here is idempotent by construction (each acts only
on rows it has not already handled), so running it repeatedly is safe and
running it on a partially repaired store is safe.

This does **not** replace a rebuild when the *parser* changes. It only reapplies
rules that operate on parsed values.
"""

from __future__ import annotations

import logging
from pathlib import Path

import polars as pl
from rich.console import Console
from rich.progress import BarColumn, Progress, TaskProgressColumn, TextColumn, TimeElapsedColumn

from twair.paths import processed_dir
from twair.qc.consistency import check_ranges
from twair.qc.rainfall import apply_no_rain_zero
from twair.qc.sentinels import apply_sentinels
from twair.store.schema import PARTITION_SCHEMA, conform_partition
from twair.store.writer import COMPRESSION, OBSERVATIONS

log = logging.getLogger(__name__)
console = Console()

__all__ = ["repair_partition", "repair_store"]


def repair_partition(path: Path) -> tuple[int, int, bool]:
    """Re-apply QC to one Parquet file.

    Returns ``(rows, rows_changed, rewritten)``. The last is separate because a
    schema-drift repair rewrites the file while changing no values, and
    reporting only ``rows_changed`` made such a run look like a no-op.
    """
    before = pl.read_parquet(path)
    if before.is_empty():
        return 0, 0, False

    after = check_ranges(apply_no_rain_zero(apply_sentinels(before)))

    changed = int(
        (
            before["value"].is_null().ne(after["value"].is_null())
            | before["flag"].cast(pl.Utf8).ne(after["flag"].cast(pl.Utf8))
        ).sum()
    )

    # Dtype drift is a defect in its own right, and it will not show up as a
    # changed value. An earlier repair wrote flag as String into 298 partitions
    # and the store stopped scanning; re-running found nothing to fix because
    # the values were already correct. Rewrite on schema mismatch too.
    drifted = any(before.schema.get(name) != dtype for name, dtype in PARTITION_SCHEMA.items())

    if changed or drifted:
        # Conform first: the QC passes emit String where the store holds
        # Categorical, and a partition written with the wrong dtype makes the
        # whole store unscannable.
        conformed = conform_partition(after)
        # Written to a sibling then swapped, so an interrupted run cannot leave
        # a half-written partition behind.
        tmp = path.with_suffix(".parquet.tmp")
        conformed.write_parquet(tmp, compression=COMPRESSION, statistics=True)
        tmp.replace(path)

    return before.height, changed, bool(changed or drifted)


def repair_store(root: Path | None = None) -> dict[str, int]:
    """Re-apply QC across every partition."""
    target = root or processed_dir(OBSERVATIONS)
    files = sorted(target.rglob("*.parquet"))
    if not files:
        console.print(f"[yellow]No partitions under {target}.[/yellow]")
        return {"partitions": 0, "rows": 0, "changed": 0}

    console.print(f"[bold]{len(files)}[/bold] partition(s) to repair")

    rows = changed = touched = 0
    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("repairing", total=len(files))
        for path in files:
            n, delta, rewritten = repair_partition(path)
            rows += n
            changed += delta
            touched += 1 if rewritten else 0
            progress.advance(task)

    console.print(
        f"  {rows:,} rows scanned, [green]{changed:,}[/green] value(s) corrected, "
        f"[green]{touched}[/green] partition(s) rewritten"
    )
    return {"partitions": len(files), "rows": rows, "changed": changed, "rewritten": touched}
