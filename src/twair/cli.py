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


if __name__ == "__main__":
    app()
