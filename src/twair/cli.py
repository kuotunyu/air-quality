"""``twair`` command line interface.

The CLI is the source of truth for how the pipeline runs; the justfile and CI
workflows are thin wrappers around these commands.
"""

from __future__ import annotations

import logging

import polars as pl
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


stations_app = typer.Typer(
    help="Station identity, air-quality zone, type and geography.",
    invoke_without_command=True,
)
app.add_typer(stations_app, name="stations")


@stations_app.callback(invoke_without_command=True)
def stations(
    ctx: typer.Context,
    save: bool = typer.Option(True, "--save/--no-save", help="Write to data/outputs/qc/."),
) -> None:
    """Resolve station identity, air-quality zone, type and coordinates."""
    if ctx.invoked_subcommand is not None:
        return

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

    unplaced = table.filter(table["lat"].is_null())
    if not unplaced.is_empty():
        console.print(
            f"[yellow]{unplaced.height} station(s) with no coordinates[/yellow] "
            f"(in the archives, not in MOENV's current register): "
            f"{unplaced['station_name'].to_list()}"
        )

    if save:
        destination = outputs_dir("qc")
        destination.mkdir(parents=True, exist_ok=True)
        path = destination / "stations.parquet"
        table.write_parquet(path)
        console.print(f"wrote {path}")


@stations_app.command("geo")
def stations_geo(
    refresh: bool = typer.Option(
        False, "--refresh", help="Re-fetch from MOENV (needs MOENV_API_KEY) and rewrite the cache."
    ),
) -> None:
    """Show — or refresh — the cached MOENV station register."""
    from twair.ingest.station_meta import (
        load_station_geo,
        reconcile_with_store,
        refresh_station_geo,
    )
    from twair.store.stations import build_station_table

    geo = refresh_station_geo() if refresh else load_station_geo()
    console.print(geo.select("station_name", "county", "lon", "lat", "station_type_official"))
    console.print(f"{geo.height} station(s) in the register")

    table = build_station_table(geography=False)
    presence = reconcile_with_store(table, geo)
    counts = presence.group_by("presence").len().sort("presence")
    console.print(counts)

    for kind, note in (
        ("archive_only", "measured, but not in the current register — no coordinates"),
        ("register_only", "in the register, but absent from every annual archive"),
    ):
        names = presence.filter(pl.col("presence") == kind)["station_name"].to_list()
        if names:
            console.print(f"[yellow]{kind}[/yellow] ({note}): {names}")


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


@analysis_app.command("m4")
def analyze_m4(
    years: str = typer.Option("2006:2025", "--years", "-y", help="Period to analyse."),
    stations: str = typer.Option(
        None, "--stations", help="Comma-separated subset. Omit for every eligible station."
    ),
    samples: int = typer.Option(
        None, "--samples", help="Resampling iterations per station. Default is measured, not 300."
    ),
) -> None:
    """M4 — meteorological normalisation, and the trend that survives it."""
    from twair.analysis.deweather import DEFAULT_SAMPLES, run_deweather, write_deweather_report

    span = _parse_year_range(years)
    period = (span.start, span.stop - 1) if span else (2006, 2025)
    subset = [s.strip() for s in stations.split(",")] if stations else None

    tables = run_deweather(
        period=period,
        stations=subset,
        n_samples=samples or DEFAULT_SAMPLES,
    )

    summary = tables["summary"]
    console.print(
        summary.select(
            "station_name", "holdout_r2", "observed_slope", "normalised_slope", "weather_share"
        )
    )

    # The headline: how much of the observed change was the weather rather than
    # the emissions. Reported over stations whose normalised trend is
    # distinguishable from zero, because a share of nothing is not a number.
    real = summary.filter(pl.col("normalised_significant"))
    if not real.is_empty():
        console.print(
            f"\n[bold]{real.height}[/bold] station(s) with a significant normalised trend; "
            f"median weather share [bold]{real['weather_share'].median():.1%}[/bold]"
        )

    for name, path in write_deweather_report(tables).items():
        console.print(f"wrote {name}: {path}")


@analysis_app.command("m5")
def analyze_m5(
    years: str = typer.Option("2006:2025", "--years", "-y", help="Period to analyse."),
    stations: str = typer.Option(None, "--stations", help="Comma-separated subset."),
    max_stations: int = typer.Option(None, "--max-stations", help="Cap, for a quick run."),
) -> None:
    """M5 — did the policy do anything? Counterfactuals, checked against placebos."""
    from twair.analysis.causal import run_causal, write_causal_report

    span = _parse_year_range(years)
    period = (span.start, span.stop - 1) if span else (2006, 2025)
    subset = [s.strip() for s in stations.split(",")] if stations else None

    tables = run_causal(period=period, stations=subset, max_stations=max_stations)
    effects = tables["effects"]

    for name, group in effects.group_by("event", maintain_order=True):
        credible = group.filter(pl.col("credible"))
        console.print(
            f"\n[bold]{name[0]}[/bold] — {group.height} station(s), "
            f"median effect {group['effect'].median():+.2f} µg/m³"
        )
        # The placebo spread is the honest yardstick: it is what this method
        # finds in years when nothing happened.
        console.print(
            f"  placebo spread (median SD) {group['placebo_sd'].median():.2f} µg/m³; "
            f"[bold]{credible.height}[/bold] station(s) clear it by 2 SD"
        )
        if credible.is_empty():
            console.print(
                "  [yellow]no station shows an effect distinguishable from the method's "
                "own noise — reported as not detected, not as zero[/yellow]"
            )

    # Open-ended policies are regime changes, not windows: what they should
    # alter is the slope, not the level. Those are tested against M4's
    # normalised series instead.
    from twair.analysis.causal import run_trend_breaks

    breaks = run_trend_breaks()
    if not breaks.is_empty():
        tables["trend_breaks"] = breaks
        for name, group in breaks.group_by("event", maintain_order=True):
            credible = int(group["credible"].sum())
            # At two SD, roughly 5% of stations clear the bar by chance alone.
            expected = 0.046 * group.height
            console.print(
                f"\n[bold]{name[0]}[/bold] (trend break) — {group.height} station(s), "
                f"median slope change {group['delta'].median():+.3f} µg/m³/yr²"
            )
            console.print(
                f"  [bold]{credible}[/bold] station(s) clear 2 SD; "
                f"~{expected:.1f} expected by chance at this many stations"
            )
            if credible <= expected:
                console.print("  [yellow]at or below the chance rate — no break detected[/yellow]")

    for name, path in write_causal_report(tables).items():
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


export_app = typer.Typer(help="Phase 3: build the data layers the website reads.")
app.add_typer(export_app, name="export")


@export_app.command("web")
def export_web(
    levels: str = typer.Option(
        "L0,L1", "--levels", help="Which tiers to build. L2 is HuggingFace-only, never local."
    ),
    story: bool = typer.Option(True, "--story/--no-story", help="Also build chapter payloads."),
) -> None:
    """Export meta, L0 (station-month JSON) and L1 (station-day Parquet)."""
    from twair.viz.export import export_all, write_manifest
    from twair.viz.story import export_story

    selected = tuple(part.strip().upper() for part in levels.split(",") if part.strip())
    if "L2" in selected:
        raise typer.BadParameter(
            "L2 is the full hourly record. It is published to HuggingFace, not to the site, "
            "and is blocked pending the MOENV licensing answer — see docs/legal.md."
        )

    results = export_all(levels=selected)
    for name, result in results.items():
        console.print(f"{name}: [green]{result.summary()}[/green]")

    if story:
        paths = export_story()
        total = sum(p.stat().st_size for p in paths)
        console.print(f"story: [green]{len(paths)} file(s), {total / 1e6:.1f} MB[/green]")
        write_manifest()

    from twair.viz.export import web_data_dir

    console.print(f"wrote {web_data_dir()}")


@app.command("summary")
def summary() -> None:
    """Row counts per year in the canonical store."""
    from twair.store.writer import partition_summary

    frame = partition_summary()
    console.print(frame)
    console.print(f"total rows: {frame['rows'].sum():,}")


if __name__ == "__main__":
    app()
