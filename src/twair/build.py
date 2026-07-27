"""Turn downloaded archives into the canonical Parquet store.

One year at a time: parse every station member, apply sentinel handling, then
write year/month partitions. Working per year keeps peak memory bounded (a
full year across ~80 stations is roughly 10^7 rows) and makes a failed year
re-runnable on its own.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import polars as pl
from rich.console import Console
from rich.progress import BarColumn, Progress, TaskProgressColumn, TextColumn, TimeElapsedColumn

from twair.ingest.archive import ArchiveFormatError, read_archive
from twair.paths import outputs_dir, raw_dir
from twair.qc.consistency import check_consistency, check_ranges
from twair.qc.flags import Flag
from twair.qc.rainfall import apply_no_rain_zero
from twair.qc.sentinels import apply_sentinels
from twair.store.writer import write_observations

log = logging.getLogger(__name__)
console = Console()

_ARCHIVE_YEAR = re.compile(r"^(\d{4})_")


@dataclass
class YearResult:
    year: int
    rows: int = 0
    stations: int = 0
    pollutants: int = 0
    generation: str = ""
    partitions: int = 0
    valid: int = 0
    sentinels: int = 0
    out_of_range: int = 0
    retained_invalid: int = 0
    """Invalid readings whose value survived — only the legacy suffix form allows this."""
    consistency_checked: int = 0
    consistency_violations: int = 0
    unparseable: dict[str, int] = field(default_factory=dict)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def discover_archives(years: range | None = None) -> list[tuple[int, Path]]:
    """Find downloaded annual archives, newest first."""
    found: list[tuple[int, Path]] = []
    for path in sorted(raw_dir("airtw").glob("*.zip")):
        match = _ARCHIVE_YEAR.match(path.name)
        if not match:
            continue
        year = int(match.group(1))
        if years is None or year in years:
            found.append((year, path))
    return sorted(found, key=lambda item: -item[0])


def _unparseable_tokens(frame: pl.DataFrame, *, limit: int = 20) -> dict[str, int]:
    """Collect the raw text of cells we could not interpret.

    These are not stored per row — that would bloat the table for a handful of
    oddities — but they must never vanish unnoticed either.
    """
    if "raw" not in frame.columns:
        return {}
    odd = frame.filter(pl.col("flag") == Flag.UNPARSEABLE.value)
    if odd.is_empty():
        return {}
    counts = odd["raw"].value_counts(sort=True).head(limit)
    return {row["raw"]: row["count"] for row in counts.to_dicts()}


def build_year(year: int, archive: Path, *, root: Path | None = None) -> YearResult:
    """Parse and store one annual archive."""
    result = YearResult(year=year)
    try:
        return _build_year(result, archive, root=root)
    except Exception as exc:
        # A 44-year unattended run must not die on one malformed archive.
        # The failure is recorded in the summary and the year can be retried
        # on its own with `twair build --years <year>`.
        log.exception("build failed for %s", year)
        result.error = f"{type(exc).__name__}: {exc}"
        return result


def _build_year(result: YearResult, archive: Path, *, root: Path | None) -> YearResult:
    year = result.year
    try:
        parsed = read_archive(archive)
    except (ArchiveFormatError, OSError) as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        return result

    if parsed.is_empty():
        # An archive that parses without error but yields nothing is a silent
        # failure, and it happened: 1992 and 2008 date their rows M/D/YYYY,
        # every timestamp came back null, and the whole year was dropped while
        # the run reported success. Treat it as the error it is.
        result.error = (
            "archive parsed to zero rows — likely an unhandled date or hour "
            "format; inspect with describe_archive()"
        )
        return result

    # Order matters. Sentinels first (888/999 are not measurements, so they
    # must not be judged against a 0-360 range), then no-rain zeros (which
    # create values that the range check should then see), then ranges.
    parsed = apply_sentinels(parsed)
    parsed = apply_no_rain_zero(parsed)
    parsed = check_ranges(parsed)

    result.rows = parsed.height
    result.stations = parsed["station_name"].n_unique()
    result.pollutants = parsed["pollutant"].n_unique()
    generations = parsed["generation"].unique().to_list()
    result.generation = ",".join(sorted(generations))
    result.sentinels = parsed.filter(
        pl.col("flag").is_in([Flag.CALM.value, Flag.INSTRUMENT_FAULT.value])
    ).height
    result.out_of_range = parsed.filter(pl.col("flag") == Flag.OUT_OF_RANGE.value).height
    result.valid = parsed.filter(pl.col("flag") == Flag.VALID.value).height
    result.retained_invalid = parsed.filter(pl.col("value_retained")).height
    result.unparseable = _unparseable_tokens(parsed)

    consistency = check_consistency(parsed)
    if not consistency.is_empty():
        destination = outputs_dir("qc") / "consistency"
        destination.mkdir(parents=True, exist_ok=True)
        consistency.write_parquet(destination / f"{year}.parquet")
        result.consistency_violations = int(consistency["violations"].sum())
        result.consistency_checked = int(consistency["checked"].sum())

    written = write_observations(parsed.drop("raw"), root=root)
    result.partitions = len(written)
    return result


def build_observations(
    *,
    years: range | None = None,
    root: Path | None = None,
) -> list[YearResult]:
    """Build the canonical store from every downloaded archive."""
    archives = discover_archives(years)
    if not archives:
        console.print(
            "[yellow]No archives found under data/raw/airtw.[/yellow] "
            "Run [bold]twair ingest airtw[/bold] first."
        )
        return []

    console.print(f"[bold]{len(archives)}[/bold] archive(s) to build")

    results: list[YearResult] = []
    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("building", total=len(archives))
        for year, path in archives:
            progress.update(task, description=f"building {year}")
            result = build_year(year, path, root=root)
            results.append(result)
            if result.ok:
                log.info(
                    "%s: %s rows, %d stations, %s",
                    year,
                    f"{result.rows:,}",
                    result.stations,
                    result.generation,
                )
            else:
                log.error("%s failed: %s", year, result.error)
            progress.advance(task)

    _report(results)
    return results


def _report(results: list[YearResult]) -> None:
    ok = [r for r in results if r.ok]
    failed = [r for r in results if not r.ok]

    total_rows = sum(r.rows for r in ok)
    total_sentinels = sum(r.sentinels for r in ok)
    console.print(
        f"  [green]{len(ok)}[/green] built, [red]{len(failed)}[/red] failed — "
        f"{total_rows:,} rows, {total_sentinels:,} wind sentinels"
    )
    for r in failed:
        console.print(f"  [red]FAILED[/red] {r.year}: {r.error}")

    odd = {token: n for r in ok for token, n in r.unparseable.items()}
    if odd:
        console.print(f"  [yellow]unparseable tokens:[/yellow] {odd}")

    summary = pl.DataFrame(
        [
            {
                "year": r.year,
                "rows": r.rows,
                "stations": r.stations,
                "pollutants": r.pollutants,
                "generation": r.generation,
                "valid": r.valid,
                "valid_ratio": round(r.valid / r.rows, 4) if r.rows else 0.0,
                "sentinels": r.sentinels,
                "out_of_range": r.out_of_range,
                "retained_invalid": r.retained_invalid,
                "consistency_checked": r.consistency_checked,
                "consistency_violations": r.consistency_violations,
                "error": r.error or "",
            }
            for r in results
        ]
    ).sort("year")

    destination = outputs_dir("build")
    destination.mkdir(parents=True, exist_ok=True)
    path = destination / "year_summary.csv"

    # Merge rather than replace: rebuilding a single year must not erase the
    # record of the other forty-three. Rows for years in this run win.
    if path.exists():
        previous = pl.read_csv(path).filter(~pl.col("year").is_in(summary["year"].to_list()))
        summary = pl.concat([previous, summary], how="diagonal_relaxed").sort("year")

    summary.write_csv(path)
    console.print(f"  wrote {path} ({summary.height} year(s) recorded)")
