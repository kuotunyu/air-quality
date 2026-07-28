"""``twair`` command line interface.

The CLI is the source of truth for how the pipeline runs; the justfile and CI
workflows are thin wrappers around these commands.
"""

from __future__ import annotations

import logging

import typer
from rich.console import Console
from rich.logging import RichHandler

from twair import __version__
from twair.config import get_settings
from twair.paths import ensure_dirs

console = Console()

app = typer.Typer(
    name="twair",
    help="Taiwan air quality reanalysis pipeline.",
    no_args_is_help=True,
    add_completion=False,
)

probe_app = typer.Typer(help="Phase 0: discover and verify upstream data sources.")
app.add_typer(probe_app, name="probe")

ingest_app = typer.Typer(help="Phase 1: download raw data from upstream providers.")
app.add_typer(ingest_app, name="ingest")


def _parse_year_range(spec: str | None) -> range | None:
    """Accept ``2010:2017``, ``2024``, or None for everything."""
    if not spec:
        return None
    if ":" in spec:
        start, _, end = spec.partition(":")
        return range(int(start), int(end) + 1)
    year = int(spec)
    return range(year, year + 1)


def _setup_logging() -> None:
    level = get_settings().twair_log_level.upper()
    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=console, rich_tracebacks=True, show_path=False)],
    )


@app.callback()
def main() -> None:
    """Shared setup for every subcommand."""
    _setup_logging()
    ensure_dirs()


@app.command()
def version() -> None:
    """Print the package version."""
    console.print(f"twair {__version__}")


@app.command()
def doctor(
    live: bool = typer.Option(
        True,
        "--live/--offline",
        help="Contact each provider to prove the credential works, not just that it is set.",
    ),
) -> None:
    """Verify credentials against the providers themselves."""
    from rich.table import Table

    from twair.ingest.verify import verify_all

    if live:
        console.print("Contacting providers …\n")
    results = verify_all(live=live)

    style = {"ok": "green", "failed": "red", "missing": "yellow", "unchecked": "dim"}
    table = Table(header_style="bold")
    table.add_column("provider")
    table.add_column("status")
    table.add_column("variable")
    table.add_column("detail", overflow="fold")

    for r in results:
        table.add_row(
            r.name,
            f"[{style[r.status]}]{r.status.upper()}[/{style[r.status]}]",
            r.env_var,
            r.detail or r.purpose,
        )
    console.print(table)

    failed = [r for r in results if r.status == "failed"]
    missing = [r for r in results if r.status == "missing"]

    if failed:
        console.print(f"\n[red]{len(failed)} credential(s) configured but not working.[/red]")
    if missing:
        console.print(
            f"[yellow]{len(missing)} not yet configured:[/yellow] "
            f"{', '.join(r.env_var for r in missing)}"
        )
        console.print("See [bold]docs/registrations.md[/bold] for where to obtain each.")
    if not failed and not missing:
        console.print("\n[green]All credentials verified.[/green]")


@probe_app.command("sources")
def probe_sources(
    samples: bool = typer.Option(
        True, "--samples/--no-samples", help="Download one small real sample per source."
    ),
) -> None:
    """Discover real dataset ids and download URLs, then write conf/sources.yaml."""
    from twair.ingest.probe import run_probe

    run_probe(download_samples=samples)


@ingest_app.command("airtw")
def ingest_airtw(
    years: str = typer.Option(
        None,
        "--years",
        "-y",
        help="Year or range, e.g. 2024 or 2010:2017. Omit for every available year.",
    ),
    refresh_catalog: bool = typer.Option(
        False,
        "--refresh-catalog",
        help="Re-resolve download links from airtw instead of using conf/sources.yaml.",
    ),
    force: bool = typer.Option(False, "--force", help="Re-download even if already cached."),
    patient: bool = typer.Option(
        False,
        "--patient",
        help="Wait out Google Drive rate limits (adds up to ~25 min per file).",
    ),
) -> None:
    """Download annual hourly archives (全部 station group, one file per year)."""
    from twair.ingest.download import download_archives

    download_archives(
        years=_parse_year_range(years),
        refresh_catalog=refresh_catalog,
        force=force,
        patient=patient,
    )


@app.command("build")
def build(
    years: str = typer.Option(
        None, "--years", "-y", help="Year or range, e.g. 2024 or 2010:2017. Omit for all."
    ),
) -> None:
    """Parse downloaded archives into the canonical Parquet store."""
    from twair.build import build_observations

    build_observations(years=_parse_year_range(years))


qc_app = typer.Typer(help="Quality assurance reporting over the canonical store.")
app.add_typer(qc_app, name="qc")


@qc_app.command("report")
def qc_report() -> None:
    """Measure and publish data-quality properties of the built store."""
    from twair.qc.report import run_report

    run_report()


@app.command("repair")
def repair() -> None:
    """Re-apply quality rules to the built store, without re-parsing archives."""
    from twair.store.repair import repair_store

    repair_store()


@app.command("aggregate")
def aggregate() -> None:
    """Build daily and monthly tables with coverage gating and circular means."""
    from twair.store.aggregate import build_aggregates

    tables = build_aggregates()
    for name, frame in tables.items():
        gated = frame.filter(~frame["meets_threshold"]).height
        console.print(
            f"{name}: [green]{frame.height:,}[/green] rows, "
            f"[yellow]{gated:,}[/yellow] below coverage threshold (mean withheld)"
        )


@app.command("stations")
def stations(
    save: bool = typer.Option(True, "--save/--no-save", help="Write to data/outputs/qc/."),
) -> None:
    """Resolve station identity, air-quality zone and type from the store."""
    from twair.paths import outputs_dir
    from twair.store.stations import build_station_table

    table = build_station_table()
    console.print(table)

    missing = table.filter(table["airzone"].is_null())
    if not missing.is_empty():
        console.print(
            f"[yellow]{missing.height} station(s) without an air-quality zone:[/yellow] "
            f"{missing['station_name'].to_list()}"
        )

    if save:
        destination = outputs_dir("qc")
        destination.mkdir(parents=True, exist_ok=True)
        path = destination / "stations.parquet"
        table.write_parquet(path)
        console.print(f"wrote {path}")


analysis_app = typer.Typer(help="Phase 2+: analysis modules.")
app.add_typer(analysis_app, name="analyze")


@analysis_app.command("m1")
def analyze_m1(
    valid_only: bool = typer.Option(
        True, "--valid-only/--all-values", help="Exclude agency-rejected readings."
    ),
) -> None:
    """M1 — replicate the 2018 project and compare against its published numbers."""
    from twair.analysis.replication import run_replication, write_replication_report

    result = run_replication(valid_only=valid_only)
    console.print(
        f"N = [bold]{result.n:,}[/bold] (published 7,286), {result.n_stations} stations\n"
    )
    console.print(result.comparison.filter(result.comparison["kind"] == "ols_coefficient"))
    paths = write_replication_report(result)
    for name, path in paths.items():
        console.print(f"wrote {name}: {path}")


@analysis_app.command("m3")
def analyze_m3(
    years: str = typer.Option("2010:2017", "--years", "-y", help="Period to analyse."),
) -> None:
    """M3 — the paired demonstrations of what each method choice cost."""
    from twair.analysis.pitfalls import run_all_pitfalls, write_pitfall_report

    span = _parse_year_range(years)
    period = (span.start, span.stop - 1) if span else (2010, 2017)

    tables = run_all_pitfalls(period=period)
    for name, frame in tables.items():
        console.print(f"\n[bold]{name}[/bold]")
        console.print(frame.head(14))

    for name, path in write_pitfall_report(tables).items():
        console.print(f"wrote {name}: {path}")


report_app = typer.Typer(help="Assemble reports from analysis outputs.")
app.add_typer(report_app, name="report")


@report_app.command("core")
def report_core() -> None:
    """Build reports/01-core.md from the M1, M2 and M3 outputs."""
    from twair.reporting import build_core_report

    path = build_core_report()
    console.print(f"wrote {path}")


@app.command("summary")
def summary() -> None:
    """Row counts per year in the canonical store."""
    from twair.store.writer import partition_summary

    frame = partition_summary()
    console.print(frame)
    console.print(f"total rows: {frame['rows'].sum():,}")


if __name__ == "__main__":
    app()
