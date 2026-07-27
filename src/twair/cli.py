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
def doctor() -> None:
    """Report which credentials are configured and which are still missing."""
    settings = get_settings()
    checks: list[tuple[str, str | None, str]] = [
        ("MOENV_API_KEY", settings.moenv_api_key, "Phase 1 — MOENV open data API"),
        ("CWA_API_KEY", settings.cwa_api_key, "Phase 1 — CWA weather observations"),
        ("CDSAPI_KEY", settings.cdsapi_key, "Phase 4 — ERA5 boundary layer height"),
        ("GEE_PROJECT_ID", settings.gee_project_id, "Phase 6 — Sentinel-5P / MODIS"),
        ("HF_TOKEN", settings.hf_token, "Publishing — HuggingFace dataset & Space"),
    ]
    missing = 0
    for name, value, purpose in checks:
        if value:
            console.print(f"[green]OK[/green]      {name:<16} {purpose}")
        else:
            missing += 1
            console.print(f"[yellow]MISSING[/yellow] {name:<16} {purpose}")
    if missing:
        console.print(
            f"\n[yellow]{missing} credential(s) missing.[/yellow] "
            "See [bold]docs/registrations.md[/bold]."
        )


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


@app.command("summary")
def summary() -> None:
    """Row counts per year in the canonical store."""
    from twair.store.writer import partition_summary

    frame = partition_summary()
    console.print(frame)
    console.print(f"total rows: {frame['rows'].sum():,}")


if __name__ == "__main__":
    app()
