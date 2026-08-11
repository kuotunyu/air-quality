"""``twair`` command line interface.

The CLI is the source of truth for how the pipeline runs; the justfile and CI
workflows are thin wrappers around these commands.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import polars as pl
import typer
from rich.console import Console
from rich.logging import RichHandler

from twair import __version__
from twair.config import get_settings
from twair.paths import ensure_dirs, interim_dir, outputs_dir
from twair.scalars import as_float, as_int

if TYPE_CHECKING:
    from twair.ingest.maiac import ExportEntry, ExportLedger

# Rich draws its tables with box-drawing characters, and a redirected stdout on
# Windows in this locale is cp950, which has no code point for ┆. So
# `twair analyze m9 > run.log` died with
# 「UnicodeEncodeError: 'cp950' codec can't encode character '┆'」 — after
# every model was fitted and before anything was written to disk. Seven minutes
# of computation discarded because the summary could not be printed.
#
# `errors="replace"` as well as the encoding, because the point is that no
# character a console cannot render may ever cost a result.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure") and (getattr(_stream, "encoding", "") or "").lower() not in {
        "utf-8",
        "utf8",
    }:
        _stream.reconfigure(encoding="utf-8", errors="replace")

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

maiac_app = typer.Typer(
    help="Plan, submit, monitor, and import resumable MODIS MAIAC exports.",
    no_args_is_help=True,
)
ingest_app.add_typer(maiac_app, name="maiac")


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
    kind: str = typer.Option(
        "全年逐時資料",
        "--kind",
        help=(
            "Which MOENV file type. 全年逐時資料 is the hourly archive the pipeline "
            "builds from; 品保查核報告 is the QA audit report and 年報 the annual "
            "report, neither of which the store reads."
        ),
    ),
) -> None:
    """Download annual archives (全部 station group, one file per year).

    Defaults to the hourly data. The other two types MOENV publishes are
    reachable with `--kind` — their links have always been in
    `conf/sources.yaml`, and only a hardcoded filter stood in front of them.
    They land under their own filenames; before this they would have overwritten
    the hourly archive for the same year.
    """
    from twair.ingest.download import download_archives

    download_archives(
        years=_parse_year_range(years),
        refresh_catalog=refresh_catalog,
        force=force,
        patient=patient,
        data_type=kind,
    )


@ingest_app.command("micro-sensor-catalog")
def ingest_micro_sensor_catalog(
    month: str = typer.Option(..., "--month", help="Calendar month in YYYYMM form."),
    confirm_network: bool = typer.Option(
        False,
        "--confirm-network",
        help="Confirm the small official station-metadata and directory requests.",
    ),
) -> None:
    """Snapshot official low-cost-sensor metadata and one archive directory."""
    if not confirm_network:
        raise typer.BadParameter(
            "micro-sensor catalogue acquisition requires --confirm-network",
            param_hint="--confirm-network",
        )
    from twair.config import ConfigError
    from twair.ingest import micro_sensors

    source = micro_sensors.load_micro_sensor_source()
    try:
        source.month_path(month)
    except ConfigError as exc:
        raise typer.BadParameter(str(exc), param_hint="--month") from exc
    with micro_sensors.FileGatorHistoryBackend(source) as backend:
        written = micro_sensors.acquire_micro_sensor_catalog(
            month,
            backend=backend,
            source=source,
        )
    station = written.manifest["station_metadata"]
    archive = written.manifest["archive_catalog"]
    console.print(f"generation: [cyan]{written.generation_sha256}[/cyan]")
    console.print(
        f"stations: [green]{int(station['rows']):,}[/green] station rows; "
        f"[yellow]{int(station['rows_in_duplicate_coordinates']):,}[/yellow] "
        "rows in duplicate coordinates"
    )
    console.print(
        f"archives: [green]{int(archive['present']):,}[/green] archive files present; "
        f"[yellow]{int(archive['absent']):,}[/yellow] archive files absent"
    )
    console.print(f"wrote raw: {written.raw_directory}")
    console.print(f"wrote normalized: {written.interim_directory}")


@ingest_app.command("micro-sensor-day")
def ingest_micro_sensor_day(
    catalog_generation: str = typer.Option(
        ...,
        "--catalog-generation",
        help="Immutable micro-sensor catalogue generation SHA-256.",
    ),
    day: str = typer.Option(..., "--date", help="Calendar day in YYYY-MM-DD form."),
    confirm_download: bool = typer.Option(
        False,
        "--confirm-download",
        help="Confirm download of the three catalogue-selected daily ZIP archives.",
    ),
) -> None:
    """Acquire one reviewed low-cost-sensor day without parsing observations."""
    if not confirm_download:
        raise typer.BadParameter(
            "micro-sensor observation acquisition requires --confirm-download",
            param_hint="--confirm-download",
        )
    from twair.config import ConfigError
    from twair.ingest import micro_sensors

    source = micro_sensors.load_micro_sensor_source()
    try:
        catalog = micro_sensors.load_catalog_generation(
            catalog_generation,
            source=source,
        )
        micro_sensors.select_observation_archives(catalog, day=day, source=source)
    except (ConfigError, RuntimeError) as exc:
        raise typer.BadParameter(
            str(exc),
            param_hint="--catalog-generation/--date",
        ) from exc
    with micro_sensors.FileGatorHistoryBackend(source) as backend:
        written = micro_sensors.acquire_micro_sensor_day(
            catalog_generation,
            day=day,
            backend=backend,
            source=source,
        )
    members = written.manifest["members"]
    total_bytes = sum(int(identity["bytes"]) for identity in members.values())
    console.print(f"generation: [cyan]{written.generation_sha256}[/cyan]")
    console.print(f"date: [green]{day}[/green]")
    console.print(
        f"archives: [green]{len(members):,}[/green] archive files; "
        f"[green]{total_bytes:,}[/green] bytes"
    )
    console.print(f"wrote raw: {written.directory}")


@ingest_app.command("micro-sensor-parse")
def ingest_micro_sensor_parse(
    generation: str = typer.Option(
        ...,
        "--generation",
        help="Immutable raw micro-sensor observation generation SHA-256.",
    ),
    confirm_parse: bool = typer.Option(
        False,
        "--confirm-parse",
        help="Confirm parsing of the three verified daily observation archives.",
    ),
) -> None:
    """Parse one verified low-cost-sensor day without repairing observations."""
    if not confirm_parse:
        raise typer.BadParameter(
            "micro-sensor observation parsing requires --confirm-parse",
            param_hint="--confirm-parse",
        )
    from twair.config import ConfigError
    from twair.ingest import micro_sensor_observations

    try:
        written = micro_sensor_observations.parse_micro_sensor_observation_generation(generation)
    except (ConfigError, RuntimeError) as exc:
        raise typer.BadParameter(str(exc), param_hint="--generation") from exc
    members = written.manifest["members"]
    total_rows = sum(int(identity["summary"]["rows"]) for identity in members.values())
    console.print(f"generation: [cyan]{written.generation_sha256}[/cyan]")
    console.print(f"date: [green]{written.manifest['date']}[/green]")
    console.print(
        f"parsed: [green]{len(members):,}[/green] Parquet members; "
        f"[green]{total_rows:,}[/green] source rows"
    )
    console.print(f"wrote interim: {written.directory}")


@ingest_app.command("satellite")
def ingest_satellite(
    year: int = typer.Option(2025, "--year", help="Calendar year to acquire."),
    months: str = typer.Option(
        "1:12",
        "--months",
        help="Comma-separated months or inclusive ranges, e.g. 1,3,6:8.",
    ),
    inventory_generation: bool = typer.Option(
        False,
        "--inventory-generation",
        help="Write to a new immutable station-inventory generation path.",
    ),
) -> None:
    """Acquire S5P monthly atmospheric columns at standard-station locations."""
    from twair.ingest.satellite import (
        EarthEngineBackend,
        acquire_s5p,
        parse_months,
        write_satellite_result,
    )
    from twair.ingest.station_inventory import satellite_generation_dir

    try:
        selected_months = parse_months(months)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--months") from exc
    if year < 2018:
        raise typer.BadParameter(
            "Sentinel-5P OFFL sources do not cover a complete pre-2018 year",
            param_hint="--year",
        )
    station_path = outputs_dir("qc") / "stations.parquet"
    if not station_path.exists():
        raise typer.BadParameter(
            f"{station_path} not found — run `twair stations` first",
            param_hint="station snapshot",
        )
    try:
        project = get_settings().require("gee_project_id")
    except RuntimeError as exc:
        raise typer.BadParameter(str(exc), param_hint="GEE_PROJECT_ID") from exc
    generation_mode = inventory_generation is True
    result = acquire_s5p(
        pl.read_parquet(station_path),
        backend=EarthEngineBackend(project),
        project=project,
        year=year,
        months=selected_months,
        inventory_generation=generation_mode,
    )
    destination = None
    if generation_mode:
        generation = result.manifest["inventory_generation_sha256"]
        if not isinstance(generation, str):
            raise RuntimeError("S5P generation acquisition produced no generation identity")
        destination = satellite_generation_dir(year, generation)
        console.print(f"inventory generation: [cyan]{generation}[/cyan]")
    paths = write_satellite_result(result, destination=destination)
    written = pl.read_parquet(paths["values"])
    console.print(
        f"S5P: [green]{written.height:,}[/green] station-month rows, "
        f"[yellow]{written['value'].null_count():,}[/yellow] masked/null values"
    )
    for name, path in paths.items():
        console.print(f"wrote {name}: {path}")


@ingest_app.command("era5")
def ingest_era5(
    year: int = typer.Option(2025, "--year", help="Calendar year to acquire."),
    months: str = typer.Option(
        "1,7",
        "--months",
        help="Comma-separated months or inclusive ranges, e.g. 1,3,6:8.",
    ),
    inventory_generation: bool = typer.Option(
        False,
        "--inventory-generation",
        help="Bind the output to the immutable reviewed station inventory.",
    ),
    confirm_download: bool = typer.Option(
        False,
        "--confirm-download",
        help="Confirm that this command may submit CDS requests and download NetCDF files.",
    ),
) -> None:
    """Acquire generation-scoped ERA5 hourly fields at station grid cells."""
    if not confirm_download:
        raise typer.BadParameter(
            "ERA5 acquisition requires --confirm-download",
            param_hint="--confirm-download",
        )
    if not inventory_generation:
        raise typer.BadParameter(
            "ERA5 acquisition requires --inventory-generation",
            param_hint="--inventory-generation",
        )
    from twair.ingest.era5 import CdsEra5Backend, acquire_era5
    from twair.ingest.satellite import parse_months

    try:
        selected_months = parse_months(months)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--months") from exc
    station_path = outputs_dir("qc") / "stations.parquet"
    if not station_path.exists():
        raise typer.BadParameter(
            f"{station_path} not found — run `twair stations` first",
            param_hint="station snapshot",
        )
    settings = get_settings()
    try:
        key = settings.require("cdsapi_key")
    except RuntimeError as exc:
        raise typer.BadParameter(str(exc), param_hint="CDSAPI_KEY") from exc
    result = acquire_era5(
        pl.read_parquet(station_path),
        backend=CdsEra5Backend(settings.cdsapi_url, key),
        year=year,
        months=selected_months,
        inventory_generation=True,
        confirm_download=True,
    )
    generation = str(result.manifest["inventory_generation_sha256"])
    null_counts = result.manifest.get("null_values")
    if not isinstance(null_counts, dict) or any(
        isinstance(value, bool) or not isinstance(value, int) for value in null_counts.values()
    ):
        raise RuntimeError("ERA5 acquisition produced invalid null counts")
    nulls = sum(null_counts.values())
    console.print(f"inventory generation: [cyan]{generation}[/cyan]")
    console.print(
        f"ERA5: [green]{result.values.height:,}[/green] station-hour rows, "
        f"[yellow]{nulls:,}[/yellow] source null values"
    )


def _maiac_months(spec: str) -> tuple[int, ...]:
    from twair.ingest.satellite import parse_months

    try:
        return parse_months(spec)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--months") from exc


def _maiac_stations() -> pl.DataFrame:
    station_path = outputs_dir("qc") / "stations.parquet"
    if not station_path.exists():
        raise typer.BadParameter(
            f"{station_path} not found — run `twair stations` first",
            param_hint="station snapshot",
        )
    return pl.read_parquet(station_path)


def _maiac_project() -> str:
    try:
        return get_settings().require("gee_project_id")
    except RuntimeError as exc:
        raise typer.BadParameter(str(exc), param_hint="GEE_PROJECT_ID") from exc


def _maiac_ledger_path(year: int, generation: str | None = None) -> Path:
    if isinstance(generation, str):
        from twair.ingest.station_inventory import maiac_generation_ledger_path

        try:
            return maiac_generation_ledger_path(year, generation)
        except ValueError as exc:
            raise typer.BadParameter(str(exc), param_hint="--generation") from exc
    return interim_dir("maiac") / f"year={year}" / "export-ledger.json"


def _print_maiac_states(entries: Sequence[ExportEntry]) -> None:
    counts: dict[str, int] = {}
    for entry in entries:
        state = entry.state
        counts[state] = counts.get(state, 0) + 1
    console.print(
        "MAIAC task states: " + ", ".join(f"{state}: {counts[state]}" for state in sorted(counts))
    )


@maiac_app.command("plan")
def plan_maiac(
    year: int = typer.Option(2025, "--year", help="Calendar year to export."),
    months: str = typer.Option(
        "1:12",
        "--months",
        help="Comma-separated months or inclusive ranges, e.g. 1,3,6:8.",
    ),
    inventory_generation: bool = typer.Option(
        False,
        "--inventory-generation",
        help="Plan beneath a new immutable station-inventory generation path.",
    ),
) -> None:
    """Write deterministic local export intent without contacting Earth Engine."""
    from twair.ingest.maiac import plan_exports, read_export_ledger, write_export_ledger

    selected_months = _maiac_months(months)
    ledger = plan_exports(
        _maiac_stations(),
        project=_maiac_project(),
        year=year,
        months=selected_months,
        inventory_generation=inventory_generation is True,
    )
    try:
        path = write_export_ledger(ledger)
        persisted = read_export_ledger(path)
    except RuntimeError as exc:
        raise typer.BadParameter(str(exc), param_hint="MAIAC plan") from exc
    console.print(
        f"MAIAC: [green]{len(persisted.entries)}[/green] month(s) planned, "
        f"{persisted.stations_with_coordinates} stations with coordinates"
    )
    _print_maiac_states(persisted.entries)
    if persisted.inventory_generation_sha256 is not None:
        console.print(f"inventory generation: [cyan]{persisted.inventory_generation_sha256}[/cyan]")
    console.print(f"wrote ledger: {path}")


@maiac_app.command("submit")
def submit_maiac(
    year: int = typer.Option(2025, "--year", help="Calendar year in the local ledger."),
    generation: str | None = typer.Option(
        None,
        "--generation",
        help="Full station-inventory generation SHA-256; omit for the legacy ledger.",
    ),
    confirm_drive_export: bool = typer.Option(
        False,
        "--confirm-drive-export",
        help="Explicitly allow new Earth Engine batch tasks to write CSV files to Drive.",
    ),
) -> None:
    """Reconcile existing tasks, then submit only the available bounded capacity."""
    if not confirm_drive_export:
        raise typer.BadParameter(
            "submission requires --confirm-drive-export",
            param_hint="--confirm-drive-export",
        )
    from twair.ingest.maiac import (
        EarthEngineMaiacBackend,
        read_export_ledger,
        submit_exports,
        write_export_ledger,
    )

    path = _maiac_ledger_path(year, generation)
    try:
        ledger = read_export_ledger(path)
    except (FileNotFoundError, RuntimeError) as exc:
        raise typer.BadParameter(str(exc), param_hint="MAIAC ledger") from exc
    project = _maiac_project()
    if ledger.gee_project != project:
        raise typer.BadParameter(
            "GEE_PROJECT_ID differs from the project recorded in the MAIAC ledger",
            param_hint="GEE_PROJECT_ID",
        )

    def persist(snapshot: ExportLedger) -> None:
        write_export_ledger(snapshot, destination=path)

    try:
        updated = submit_exports(
            ledger,
            _maiac_stations(),
            backend=EarthEngineMaiacBackend(project),
            confirm=True,
            persist=persist,
        )
        write_export_ledger(updated, destination=path)
    except RuntimeError as exc:
        raise typer.BadParameter(str(exc), param_hint="MAIAC submission") from exc
    _print_maiac_states(updated.entries)
    console.print(f"updated ledger: {path}")


@maiac_app.command("status")
def status_maiac(
    year: int = typer.Option(2025, "--year", help="Calendar year in the local ledger."),
    generation: str | None = typer.Option(
        None,
        "--generation",
        help="Full station-inventory generation SHA-256; omit for the legacy ledger.",
    ),
) -> None:
    """Refresh local state from Earth Engine without creating a task."""
    from twair.ingest.maiac import (
        EarthEngineMaiacBackend,
        read_export_ledger,
        refresh_export_status,
        write_export_ledger,
    )

    path = _maiac_ledger_path(year, generation)
    try:
        ledger = read_export_ledger(path)
    except (FileNotFoundError, RuntimeError) as exc:
        raise typer.BadParameter(str(exc), param_hint="MAIAC ledger") from exc
    project = _maiac_project()
    if ledger.gee_project != project:
        raise typer.BadParameter(
            "GEE_PROJECT_ID differs from the project recorded in the MAIAC ledger",
            param_hint="GEE_PROJECT_ID",
        )
    try:
        updated = refresh_export_status(
            ledger,
            backend=EarthEngineMaiacBackend(project),
        )
        write_export_ledger(updated, destination=path)
    except RuntimeError as exc:
        raise typer.BadParameter(str(exc), param_hint="MAIAC status") from exc
    _print_maiac_states(updated.entries)
    console.print(f"updated ledger: {path}")


@maiac_app.command("import-files")
def import_maiac_files(
    from_dir: Annotated[
        Path,
        typer.Option(
            "--from-dir",
            exists=True,
            file_okay=False,
            dir_okay=True,
            readable=True,
            resolve_path=True,
            help="Directory containing downloaded Earth Engine CSV exports.",
        ),
    ],
    year: int = typer.Option(2025, "--year", help="Calendar year in the local ledger."),
    generation: str | None = typer.Option(
        None,
        "--generation",
        help="Full station-inventory generation SHA-256; omit for the legacy ledger.",
    ),
    months: str = typer.Option(
        "1:12",
        "--months",
        help="Comma-separated completed months or inclusive ranges to import.",
    ),
) -> None:
    """Validate downloaded CSV files and merge complete station months atomically."""
    from twair.ingest.maiac import read_export_ledger
    from twair.ingest.maiac_import import import_exported_files, write_maiac_result

    path = _maiac_ledger_path(year, generation)
    try:
        ledger = read_export_ledger(path)
        result = import_exported_files(
            ledger,
            _maiac_stations(),
            source_dir=from_dir,
            months=_maiac_months(months),
        )
        paths = write_maiac_result(result)
        persisted = pl.read_parquet(paths["values"])
    except (FileNotFoundError, RuntimeError) as exc:
        raise typer.BadParameter(str(exc), param_hint="MAIAC import") from exc
    console.print(
        f"MAIAC: [green]{persisted.height:,}[/green] station-month rows, "
        f"[yellow]{persisted['value'].null_count():,}[/yellow] masked/null values"
    )
    for name, output_path in paths.items():
        console.print(f"wrote {name}: {output_path}")


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


@qc_app.command("stuck")
def qc_stuck(
    years: str = typer.Option("1982:2025", "--years", "-y", help="Year or range, e.g. 2010:2017."),
    pollutants: str = typer.Option(
        None, "--pollutants", help="Comma-separated subset; default from conf/qc.yaml."
    ),
) -> None:
    """Find readings that stopped changing, and how often the agency agreed."""
    from twair.qc.stuck import run_stuck, write_stuck_report

    span = _parse_year_range(years)
    if span is None:  # pragma: no cover - the option carries a default
        raise typer.BadParameter("--years is required")
    subset = [p.strip() for p in pollutants.split(",")] if pollutants else None

    tables, report = run_stuck(years=(span.start, span.stop - 1), pollutants=subset)
    console.print(report.summary())
    for note in report.notes:
        console.print(f"  [yellow]{note}[/yellow]")
    for name, path in write_stuck_report(tables).items():
        console.print(f"wrote {name}: {path}")


@qc_app.command("outliers")
def qc_outliers(
    years: str = typer.Option("1982:2025", "--years", "-y", help="Year or range, e.g. 2010:2017."),
    pollutants: str = typer.Option(
        None, "--pollutants", help="Comma-separated subset; default from conf/qc.yaml."
    ),
) -> None:
    """Separate short isolated excursions from network-wide episodes."""
    from twair.qc.outliers import run_outliers, write_outlier_report

    span = _parse_year_range(years)
    if span is None:  # pragma: no cover - the option carries a default
        raise typer.BadParameter("--years is required")
    subset = [p.strip() for p in pollutants.split(",")] if pollutants else None

    tables, report = run_outliers(years=(span.start, span.stop - 1), pollutants=subset)
    console.print(report.summary())
    for note in report.notes:
        console.print(f"  [yellow]{note}[/yellow]")
    for name, path in write_outlier_report(tables).items():
        console.print(f"wrote {name}: {path}")


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
            f"(in the archives, absent from reviewed current and historical sources): "
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
        resolve_station_geo,
    )
    from twair.store.stations import build_station_table

    current_geo = refresh_station_geo() if refresh else load_station_geo()
    geo = resolve_station_geo(current=current_geo)
    console.print(
        geo.select(
            "station_name",
            "county",
            "lon",
            "lat",
            "station_type_official",
            "geo_source",
            "geo_source_record_namespace",
            "geo_source_record_id",
        )
    )
    console.print(
        f"{current_geo.height} station(s) in the current register; "
        f"{geo.height} station geography record(s) after reviewed historical sources"
    )

    table = build_station_table(geography=False)
    presence = reconcile_with_store(table, geo)
    counts = presence.group_by("presence").len().sort("presence")
    console.print(counts)

    for kind, note in (
        (
            "archive_only",
            "measured, but absent from reviewed current and historical sources — no coordinates",
        ),
        ("register_only", "in reviewed geography, but absent from every annual archive"),
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


@analysis_app.command("m2")
def analyze_m2(
    years: str = typer.Option("2010:2017", "--years", "-y", help="Period to analyse."),
    splits: int = typer.Option(3, "--splits", help="Rolling-origin splits per feature set."),
    stations: int = typer.Option(
        8, "--loso", help="How many stations to hold out one at a time. All 77 is 77 fits."
    ),
) -> None:
    """M2 — what actually drives hourly PM2.5, once PM10 may not predict its own subset."""
    from twair.analysis.drivers import run_all_drivers, write_driver_report

    span = _parse_year_range(years)
    period = (span.start, span.stop - 1) if span else (2010, 2017)

    tables = run_all_drivers(period=period, n_splits=splits, loso_sample=stations)

    # The baselines first and in the same table, because a model score without
    # persistence beside it is not a result — which is the entire complaint
    # this module makes about the 2018 project.
    with pl.Config(tbl_rows=40, tbl_width_chars=120, float_precision=4):
        console.print(tables["summary"])

    for name, path in write_driver_report(tables).items():
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
            f"median weather share [bold]{as_float(real['weather_share'].median()):.1%}[/bold]"
        )

    for name, path in write_deweather_report(tables).items():
        console.print(f"wrote {name}: {path}")


@analysis_app.command("m8")
def analyze_m8(
    year: int = typer.Option(2025, "--year", "-y", help="Calendar year to analyse."),
    generation: str | None = typer.Option(
        None,
        "--generation",
        help="Immutable station-inventory SHA-256. Omit to analyse legacy results.",
    ),
) -> None:
    """M8 — provisional satellite/PM2.5 association, not calibration."""
    from twair.analysis.satellite import run_satellite_association, write_satellite_analysis
    from twair.ingest.station_inventory import validate_generation_sha256

    identity: str | None = None
    if generation is not None:
        try:
            identity = validate_generation_sha256(generation)
        except ValueError as exc:
            raise typer.BadParameter(str(exc), param_hint="--generation") from exc

    result = run_satellite_association(year=year, generation=identity)
    written = write_satellite_analysis(result)

    console.print(
        "[bold]provisional descriptive association[/bold] — not causal, not calibration, "
        "and not satellite-estimated PM2.5"
    )
    console.print(
        f"input identity: year {result.manifest['year']}; mode {result.manifest['mode']}; "
        f"inventory generation {result.manifest['inventory_generation_sha256'] or 'legacy'}"
    )
    console.print("\n[bold]joint coverage[/bold]")
    console.print(result.coverage)
    console.print("\n[bold]association scopes[/bold]")
    console.print(result.association)
    for name, path in written.items():
        console.print(f"wrote {name}: {path}")


@analysis_app.command("era5-value")
def analyze_era5_value(
    generation: str = typer.Option(
        ...,
        "--generation",
        help="Immutable station-inventory SHA-256 shared by both ERA5 UTC years.",
    ),
    pilot: bool = typer.Option(
        False,
        "--pilot/--full",
        help="Run the reviewed six-station CPU pilot instead of all eligible stations.",
    ),
) -> None:
    """Measure ERA5 weather value on future local-calendar quarters."""
    from twair.analysis.era5_value import run_era5_value, write_era5_value_result
    from twair.ingest.station_inventory import validate_generation_sha256
    from twair.paths import data_root

    try:
        identity = validate_generation_sha256(generation)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--generation") from exc

    result = run_era5_value(
        data_root=data_root(),
        generation_sha256=identity,
        pilot=pilot,
    )
    # A console/display failure must not discard hundreds of completed fits.
    written = write_era5_value_result(result)

    mode = str(result.manifest["mode"])
    station_count = len(result.manifest.get("stations", []))
    paired_rows = int(result.manifest.get("paired_rows", 0))
    console.print(
        f"ERA5 value-add [bold]{mode}[/bold]: {station_count} station(s), "
        f"{paired_rows:,} paired station-hour rows"
    )
    console.print(
        "Same rows and forward local-time folds; descriptive predictive value, "
        "not causal attribution or calibration."
    )
    console.print(result.deltas)
    for name, path in written.items():
        console.print(f"wrote {name}: {path}")


@analysis_app.command("satellite-value")
def analyze_satellite_value(
    generation: str = typer.Option(
        ...,
        "--generation",
        help="Immutable station-inventory SHA-256 shared by S5P and MAIAC.",
    ),
    year: int = typer.Option(2025, "--year", "-y", help="Calendar year to evaluate."),
) -> None:
    """Test held-out satellite predictive value without creating a fused product."""
    from twair.analysis.satellite_value import (
        load_satellite_value_config,
        run_satellite_value,
        write_satellite_value_result,
    )
    from twair.ingest.station_inventory import validate_generation_sha256
    from twair.paths import data_root

    try:
        identity = validate_generation_sha256(generation)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--generation") from exc
    selected = load_satellite_value_config()
    if year != selected.year:
        raise typer.BadParameter(
            f"satellite value config is reviewed for {selected.year}",
            param_hint="--year",
        )
    result = run_satellite_value(
        data_root=data_root(),
        generation_sha256=identity,
        config=selected,
    )
    # A console/display failure must not discard completed serial model fits.
    written = write_satellite_value_result(result)

    console.print(
        f"Satellite held-out value: year {result.manifest['year']}; "
        f"{int(result.manifest['common_complete_rows']):,} common station-month rows"
    )
    console.print(
        "Seasonal-block and held-out-station prediction only; not causal attribution, "
        "calibration, fusion, future-year transfer, or an M4 replacement."
    )
    console.print(result.deltas)
    for name, path in written.items():
        console.print(f"wrote {name}: {path}")


@analysis_app.command("satellite-robustness")
def analyze_satellite_robustness(
    generation: str = typer.Option(
        ...,
        "--generation",
        help="Immutable station-inventory SHA-256 shared by both M8 association years.",
    ),
) -> None:
    """Test held-out satellite prediction across the reviewed association years."""
    from twair.analysis.satellite_robustness import (
        run_satellite_robustness,
        write_satellite_robustness_result,
    )
    from twair.ingest.station_inventory import validate_generation_sha256
    from twair.paths import data_root

    try:
        identity = validate_generation_sha256(generation)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--generation") from exc
    result = run_satellite_robustness(
        data_root=data_root(),
        generation_sha256=identity,
    )
    written = write_satellite_robustness_result(result)

    common_stations = len(result.manifest.get("common_stations", []))
    console.print(f"Satellite robustness: 2024 and 2025; {common_stations} common station(s)")
    console.print(
        "Year replication plus cross-year and held-out-station cross-year prediction; "
        "not causal attribution, calibration, fusion, or an M4 replacement."
    )
    console.print(result.deltas)
    for name, path in written.items():
        console.print(f"wrote {name}: {path}")


@analysis_app.command("era5-robustness")
def analyze_era5_robustness(
    generation: str = typer.Option(
        ...,
        "--generation",
        help="Immutable station-inventory SHA-256 shared by every ERA5 UTC year.",
    ),
    pilot: bool = typer.Option(
        False,
        "--pilot/--full",
        help="Run the reviewed six-station CPU pilot instead of all eligible stations.",
    ),
) -> None:
    """Test whether ERA5 prediction value transfers across years and stations."""
    from twair.analysis.era5_robustness import (
        run_era5_robustness,
        write_era5_robustness_result,
    )
    from twair.ingest.station_inventory import validate_generation_sha256
    from twair.paths import data_root

    try:
        identity = validate_generation_sha256(generation)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--generation") from exc

    result = run_era5_robustness(
        data_root=data_root(),
        generation_sha256=identity,
        pilot=pilot,
    )
    # A console/display failure must not discard completed serial model fits.
    written = write_era5_robustness_result(result)

    mode = str(result.manifest["mode"])
    common_stations = len(result.manifest.get("common_stations", []))
    console.print(f"ERA5 robustness [bold]{mode}[/bold]: {common_stations} common station(s)")
    console.print(
        "Year replication plus temporal, held-out-station, and combined transfer; "
        "descriptive prediction, not causal attribution or calibration."
    )
    console.print(result.deltas)
    for name, path in written.items():
        console.print(f"wrote {name}: {path}")


@analysis_app.command("m9")
def analyze_m9(
    years: str = typer.Option("2015:2025", "--years", "-y", help="Period to backtest."),
    stations: str = typer.Option(None, "--stations", help="Comma-separated subset."),
    horizons: str = typer.Option("1,6,24,48", "--horizons", help="Hours ahead."),
    splits: int = typer.Option(4, "--splits", help="Rolling-origin splits."),
) -> None:
    """M9 — forecasting, scored against persistence rather than against zero."""
    from twair.models.forecast import run_forecast, write_forecast_report

    span = _parse_year_range(years)
    period = (span.start, span.stop - 1) if span else (2015, 2025)
    subset = [s.strip() for s in stations.split(",")] if stations else None
    hours = tuple(int(h) for h in horizons.split(",") if h.strip())

    tables = run_forecast(period=period, stations=subset, horizons=hours, n_splits=splits)

    # Persisted before it is displayed, and the order is the point. This ran the
    # other way round and a console encoding error in the summary threw away
    # every fit behind it. The paths are still reported last, where a reader
    # expects them; only the writing moved.
    written = write_forecast_report(tables)

    by_horizon = tables["by_horizon"]

    console.print(
        by_horizon.select(
            "horizon", "n", "model_rmse", "persistence_rmse", "model_r2", "skill_vs_persistence"
        )
    )

    # Three columns because each one alone misleads. R² tracks how predictable
    # the target happens to be, not whether the model helped. Skill against
    # persistence rises with horizon even as the model decays toward the
    # long-run mean, which only the climatology column reveals. And the mean
    # over splits once reported "4/4 beat persistence" while one split sat at
    # -0.111, so the worst split is printed beside every mean.
    total_splits = int(by_horizon["splits"].sum())
    losing = int(by_horizon["splits_not_beating_persistence"].sum())
    console.print(
        f"\n[bold]{total_splits - losing}/{total_splits}[/bold] horizon-split cells beat persistence"
    )
    for row in by_horizon.iter_rows(named=True):
        verdict = "green" if row["skill_worst_split"] > 0 else "yellow"
        console.print(
            f"  {row['horizon']:>3}h  R² {row['model_r2']:.3f}  "
            f"[{verdict}]skill {row['skill_vs_persistence']:+.3f} "
            f"(worst split {row['skill_worst_split']:+.3f})[/{verdict}]  "
            f"vs climatology {row['skill_vs_climatology']:+.3f}"
        )

    for name, path in written.items():
        console.print(f"wrote {name}: {path}")


@analysis_app.command("m10")
def analyze_m10() -> None:
    """M10 — attributable fraction, and how much the assumption sets it."""
    from twair.analysis.health import run_health, write_health_report

    tables = run_health()
    spread = tables["spread"]
    coverage = tables["coverage"]

    console.print(
        spread.select(
            "year",
            "mean_median",
            "paf_lowest_assumption",
            "paf_highest_assumption",
            "assumption_spread",
        ).tail(10)
    )

    first, latest = spread.head(1).to_dicts()[0], spread.tail(1).to_dicts()[0]
    row = coverage.to_dicts()[0]

    console.print(
        f"\npanel: [bold]{row['panel_stations']}[/bold] stations balanced from "
        f"{row['panel_start_year']} ({row['station_years_total']:,} station-years)"
    )
    console.print(
        f"[bold]{latest['year']}[/bold]: median station {latest['mean_median']:.1f} µg/m³, "
        f"attributable fraction [bold]{latest['paf_lowest_assumption']:.1%} to "
        f"{latest['paf_highest_assumption']:.1%}[/bold] — the whole range comes from "
        f"which counterfactual is chosen"
    )

    # Two findings that point opposite ways, so both get printed. The fall is
    # robust: it happens at every counterfactual. The level is not: today it is
    # 5% or 9% depending on a choice that is almost never stated. And the
    # second gets worse as the first succeeds, which is the non-obvious part.
    fall_lo = first["paf_lowest_assumption"] - latest["paf_lowest_assumption"]
    fall_hi = first["paf_highest_assumption"] - latest["paf_highest_assumption"]
    console.print(
        f"  the [green]fall[/green] survives every assumption: "
        f"{fall_lo:.1%} to {fall_hi:.1%} since {first['year']}"
    )
    console.print(
        f"  the [yellow]level[/yellow] does not: assumption is "
        f"[bold]{first['spread_as_share_of_estimate']:.0%}[/bold] of the {first['year']} estimate "
        f"and [bold]{latest['spread_as_share_of_estimate']:.0%}[/bold] of the "
        f"{latest['year']} one"
    )
    console.print(
        "  [dim]cleaner air makes the counterfactual choice matter more, not less — "
        "the excess shrinks toward it[/dim]"
    )
    console.print(
        f"  [yellow]{row['share_above_ceiling']:.1%}[/yellow] of station-years sit above "
        f"{row['extrapolation_ceiling_ugm3']:.0f} µg/m³, outside the cohorts the "
        f"coefficient came from"
    )
    console.print("[dim]  fractions, not death counts — see the module docstring[/dim]")

    for name, path in write_health_report(tables).items():
        console.print(f"wrote {name}: {path}")


@analysis_app.command("m11")
def analyze_m11(
    sample: int = typer.Option(12, "--sample", help="Stations used for the costly comparisons."),
    fraction: float = typer.Option(0.05, "--fraction", help="Share of known readings to hide."),
) -> None:
    """M11 — what the 2018 project's one sentence about missing values cost."""
    import polars as pl

    from twair.analysis.imputation import run_imputation

    written = run_imputation(sample=sample, fraction=fraction)

    distribution = pl.read_parquet(written["gap_distribution"])
    console.print("\n[bold]how much is missing, and in what shape[/bold]")
    console.print(distribution.select("gap_bucket", "gaps", "hours", "share_of_missing_hours"))

    scores = pl.read_parquet(written["reconstruction"])
    pooled = scores.filter(pl.col("gap_bucket") == "all")
    console.print("\n[bold]reconstruction error against readings that were hidden[/bold]")
    console.print(pooled.select("strategy", "recovered", "recovery_rate", "mae", "rmse", "bias"))

    console.print("\n[bold]by gap length[/bold]")
    console.print(
        scores.filter(pl.col("gap_bucket") != "all")
        .select("strategy", "gap_bucket", "n", "mae")
        .sort("strategy", "gap_bucket")
    )

    # The comparison the whole module exists for: the original's method against
    # the alternatives, on its own window, with an error attached at last.
    rows = {row["strategy"]: row for row in pooled.iter_rows(named=True)}
    neighbour, interpolation = rows.get("neighbor"), rows.get("interpolate")
    if neighbour and interpolation and neighbour["mae"] and interpolation["mae"]:
        console.print(
            f"\nthe 2018 method reaches [bold]{neighbour['recovery_rate']:.0%}[/bold] of the "
            f"hidden readings at [bold]{neighbour['mae']:.2f}[/bold] µg/m³ mean absolute error; "
            f"interpolation reaches {interpolation['recovery_rate']:.0%} at "
            f"{interpolation['mae']:.2f}"
        )
        console.print(
            "[dim]  neither is free: the first invents more values less accurately, "
            "the second declines most of the problem[/dim]"
        )

    for name, path in written.items():
        console.print(f"wrote {name}: {path}")


@analysis_app.command("m12")
def analyze_m12(
    sample: int = typer.Option(6, "--sample", help="Stations to score. Each one costs minutes."),
    splits: int = typer.Option(3, "--splits", help="Rolling-origin splits per station."),
    years: str = typer.Option("2015:2025", "--years", "-y", help="Period to analyse."),
    stations: str = typer.Option(None, "--stations", help="Comma-separated subset."),
) -> None:
    """M12 — SARIMA, the model the 2018 project set aside as 「實屬不便」."""
    from twair.models.sarima import run_sarima
    from twair.paths import outputs_dir

    span = _parse_year_range(years)
    period = (span.start, span.stop - 1) if span else (2015, 2025)
    subset = [s.strip() for s in stations.split(",")] if stations else None

    tables = run_sarima(period=period, stations=subset, sample=sample, n_splits=splits)
    cost, fits, scores = tables["selection_cost"], tables["fits"], tables["scores"]

    console.print("\n[bold]what the inconvenience costs[/bold]")
    console.print(cost)

    # Convergence first, because every number below it is a claim about a fitted
    # model, and a non-converged fit makes that claim about an optimiser instead.
    converged = int(fits["converged"].sum())
    console.print(
        f"\n[bold]{converged}/{fits.height}[/bold] fits converged, "
        f"median {as_float(fits['fit_seconds'].median()):.0f}s on "
        f"{as_int(fits['train_observed'].median()):,} observed points"
    )
    if converged < fits.height:
        console.print(
            f"  [yellow]{fits.height - converged} did not[/yellow] — kept in the table, not dropped"
        )

    pooled = (
        scores.group_by("method", "horizon")
        .agg(
            pl.col("rmse").mean().alias("rmse"),
            pl.col("mae").mean().alias("mae"),
            pl.col("n").sum().alias("n"),
        )
        .sort("horizon", "rmse")
    )
    console.print("\n[bold]scored on identical origins[/bold]")
    console.print(pooled)

    # The question D10 exists to answer: at each horizon, did the expensive
    # model beat the two things that cost nothing?
    console.print("\n[bold]did it buy anything?[/bold]")
    for horizon in sorted(pooled["horizon"].unique().to_list()):
        at = {
            row["method"]: row["rmse"]
            for row in pooled.filter(pl.col("horizon") == horizon).to_dicts()
        }
        sarima = at.get("sarima")
        if sarima is None:
            continue
        rivals = {name: rmse for name, rmse in at.items() if name != "sarima"}
        best = min(rivals, key=lambda name: rivals[name])
        margin = (rivals[best] - sarima) / rivals[best]
        verdict = "[green]beats[/green]" if margin > 0 else "[yellow]loses to[/yellow]"
        console.print(
            f"  {horizon:>3}h  SARIMA {sarima:.2f}  {verdict} best baseline "
            f"({best} {rivals[best]:.2f}) by [bold]{abs(margin):.1%}[/bold]"
        )

    for name in ("selection_cost", "fits", "scores"):
        console.print(f"wrote {name}: {outputs_dir('m12_sarima') / (name + '.parquet')}")


@analysis_app.command("m7")
def analyze_m7(
    years: str = typer.Option("2006:2025", "--years", "-y", help="Period to analyse."),
    percentile: float = typer.Option(75.0, "--percentile", help="What counts as a high hour."),
    stations: str = typer.Option(None, "--stations", help="Comma-separated subset."),
) -> None:
    """M7 — wind patterns during high PM2.5 hours (CBPF, no trajectory model)."""
    from twair.analysis.sources import run_sources, write_sources_report

    span = _parse_year_range(years)
    period = (span.start, span.stop - 1) if span else (2006, 2025)
    subset = [s.strip() for s in stations.split(",")] if stations else None

    tables = run_sources(period=period, stations=subset, percentile=percentile)
    summary = tables["summary"]

    console.print(
        summary.select(
            "station_name", "threshold", "calm_fraction", "resultant", "peak_sector", "peak_speed"
        ).head(15)
    )

    high_wind = summary.filter(pl.col("peak_speed").is_in(["6-8", "8+"]))
    low_wind = summary.filter(pl.col("peak_speed").is_in(["<0.5", "0.5-1.5"]))
    console.print(
        f"\n[bold]{summary.height}[/bold] station(s); "
        f"[bold]{high_wind.height}[/bold] peak in high-wind bins, "
        f"[bold]{low_wind.height}[/bold] in low-wind bins"
    )
    console.print(
        f"  median calm fraction {as_float(summary['calm_fraction'].median()):.1%} "
        f"(excluded from the polar plot — no direction — but counted)"
    )
    console.print(f"  {int(summary['n_suppressed_bins'].sum())} bin(s) withheld for too few hours")

    for name, path in write_sources_report(tables).items():
        console.print(f"wrote {name}: {path}")


@analysis_app.command("m5")
def analyze_m5(
    years: str = typer.Option("2006:2025", "--years", "-y", help="Period to analyse."),
    stations: str = typer.Option(None, "--stations", help="Comma-separated subset."),
    max_stations: int = typer.Option(None, "--max-stations", help="Cap, for a quick run."),
) -> None:
    """M5 — marked-window observed-minus-predicted contrasts.

    Compare them with unmarked controls; they are not identified causal policy effects.
    """
    from twair.analysis.causal import run_causal, write_causal_report

    span = _parse_year_range(years)
    period = (span.start, span.stop - 1) if span else (2006, 2025)
    subset = [s.strip() for s in stations.split(",")] if stations else None

    tables = run_causal(period=period, stations=subset, max_stations=max_stations)

    # Persisted here and again below, for the reason m9 persists before it
    # prints. The trend-break step further down reads M4's output and raises
    # FileNotFoundError when M4 has not run — nothing makes M4 a precondition of
    # M5, `run_causal` does not touch it, and there is no pipeline command that
    # orders them. So a complete event study over every station was thrown away
    # by an optional extra that could not start.
    written = write_causal_report(tables)

    effects = tables["effects"]

    for name, group in effects.group_by("event", maintain_order=True):
        credible = group.filter(pl.col("credible"))
        console.print(
            f"\n[bold]{name[0]}[/bold] — {group.height} station(s), "
            "median observed-minus-predicted contrast "
            f"{as_float(group['effect'].median()):+.2f} µg/m³"
        )
        # The unmarked-control-window spread is the honest yardstick because it
        # measures same-calendar background variation without this event label.
        console.print(
            "  unmarked-control-window spread (median SD) "
            f"{as_float(group['placebo_sd'].median()):.2f} µg/m³; "
            f"[bold]{credible.height}[/bold] station(s) clear it by 2 SD"
        )
        if credible.is_empty():
            console.print(
                "  [yellow]no station shows a contrast distinguishable from the method's "
                "own noise — reported as not detected, not as zero[/yellow]"
            )

    # Open-ended event labels are screened as possible slope changes rather
    # than finite windows. The observed slope differences use M4's normalised
    # series and remain non-causal comparisons.
    from twair.analysis.causal import run_trend_breaks

    try:
        breaks = run_trend_breaks()
    except FileNotFoundError as exc:
        # An optional comparison, not a prerequisite. Saying so beats dying with
        # the event study already computed and unsaved.
        console.print(f"\n[yellow]skipping trend breaks:[/yellow] {exc}")
        breaks = pl.DataFrame()
    if not breaks.is_empty():
        tables["trend_breaks"] = breaks
        for name, group in breaks.group_by("event", maintain_order=True):
            n_credible = as_int(group["credible"].sum())
            # At two SD, roughly 5% of stations clear the bar by chance alone.
            expected = 0.046 * group.height
            console.print(
                f"\n[bold]{name[0]}[/bold] (trend break) — {group.height} station(s), "
                f"median slope change {as_float(group['delta'].median()):+.3f} µg/m³/yr²"
            )
            console.print(
                f"  [bold]{n_credible}[/bold] station(s) clear 2 SD; "
                f"~{expected:.1f} expected by chance at this many stations"
            )
            if n_credible <= expected:
                console.print("  [yellow]at or below the chance rate — no break detected[/yellow]")

    # Again, because `trend_breaks` may have been added since the first write.
    for table_name, path in write_causal_report(tables).items():
        written[table_name] = path
    for table_name, path in written.items():
        console.print(f"wrote {table_name}: {path}")


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


@analysis_app.command("m6")
def analyze_m6(
    steps: str = typer.Option(
        None,
        "--steps",
        help="Comma list of steps to run; omit for all of them.",
    ),
    draws: int = typer.Option(None, "--draws", help="Override the null draw count for a fast run."),
) -> None:
    """M6 — whether 「分區各跑一次」 was an adequate spatial control."""
    from twair.analysis.spatial import load_spatial_conf, run_spatial, write_spatial_report

    conf = load_spatial_conf()
    if draws:
        inference = dict(conf.inference)
        inference["residual_null_draws"] = draws
        conf = replace(conf, inference=inference)

    from twair.analysis.spatial import STEPS

    chosen = tuple(s.strip() for s in steps.split(",")) if steps else STEPS
    result = run_spatial(conf=conf, steps=chosen)

    # Support first. Every number below this block is conditional on which
    # stations are in the network, and a spatial result quoted without its
    # network is not checkable — the same reason m12 prints convergence before
    # any fitted number.
    meta = {row["key"]: row["value"] for row in result.metadata.iter_rows(named=True)}
    console.print("\n[bold]the network these statistics are about[/bold]")
    console.print(
        f"  {meta['panel_stations']} stations over {meta['panel_months']} months; "
        f"{meta['panel_stations_placed']} have coordinates, "
        f"{meta['panel_stations_complete']} report in every month"
    )
    unplaced = result.coverage.filter(~pl.col("placed"))["station_name"].to_list()
    if unplaced:
        console.print(
            f"  [yellow]{len(unplaced)} excluded for want of coordinates[/yellow]: "
            + "、".join(unplaced)
        )
    console.print(
        f"  weights {meta['weights']}, zone partition {meta['zone_partition']}, "
        f"seed {meta['seed']}, {meta['residual_null_draws']} null draws"
    )

    # The correlogram before any single I: if it changes sign, one number is a
    # blend of opposite-signed bands and must not be quoted alone.
    if result.correlogram.height:
        console.print("\n[bold]residual Moran's I by distance[/bold]")
        for row in result.correlogram.iter_rows(named=True):
            if row["i"] is None:
                console.print(
                    f"  {row['bin_lo_km']:>5.0f}–{row['bin_hi_km']:<5.0f} {row['refused']}"
                )
                continue
            mark = " [bold]significant[/bold]" if row["significant_bh"] else ""
            console.print(
                f"  {row['bin_lo_km']:>5.0f}–{row['bin_hi_km']:<5.0f} km  "
                f"{row['pairs']:>4} pairs  I = {row['i']:+.4f}  z = {row['z']:+6.2f}{mark}"
            )
        signs = [
            row["i"] > 0 for row in result.correlogram.iter_rows(named=True) if row["i"] is not None
        ]
        if len(set(signs)) > 1:
            console.print(
                "  [dim]the sign changes with distance, so no single I describes this field[/dim]"
            )

    if result.partition.height:
        console.print("\n[bold]what each spatial control removes[/bold]")
        for row in result.partition.iter_rows(named=True):
            console.print(
                f"  {row['control']:<26} {row['rank']:>4} params  R² {row['r_squared']:.4f}  "
                f"mean I {row['mean_i']:+.4f} "
                f"(95% CI {row['mean_i_lo']:+.4f} to {row['mean_i_hi']:+.4f})  "
                f"{row['months_significant_bh']:>2}/{row['months_scored']} months significant"
            )
            if row["rank_deficient"]:
                console.print(
                    f"    [yellow]rank deficient[/yellow] — {row['design_columns']} columns, "
                    f"rank {row['rank']}; residuals stand, coefficients do not"
                )

        rows = {row["control"]: row for row in result.partition.iter_rows(named=True)}
        pooled, literal = rows.get("pooled"), rows.get("within_zone_separate_fits")
        if pooled and literal:
            console.print(
                f"\n  the literal 「分區各跑一次」 takes mean I from "
                f"[bold]{pooled['mean_i']:+.4f}[/bold] to [bold]{literal['mean_i']:+.4f}[/bold], "
                f"and significant months from {pooled['months_significant_bh']} to "
                f"{literal['months_significant_bh']} of {pooled['months_scored']}"
            )
            console.print(
                "  [dim]reported as a difference of means, never as a share: I is a "
                "normalised correlation and has no decomposition property[/dim]"
            )

    if result.sensitivity is not None and result.sensitivity.height:
        console.print("\n[bold]the same field under every weights definition[/bold]")
        console.print(
            result.sensitivity.filter(pl.col("i").is_not_null()).select(
                "family", "parameter", "main_island_only", "n_stations", "n_islands", "i", "z"
            )
        )

    if result.agreement is not None and result.agreement.height:
        console.print("\n[bold]does the official zoning know more than geography?[/bold]")
        era = result.agreement.filter(pl.col("partition") == "zone_era").to_dicts()[0]
        console.print(
            f"  era partition, {era['k']} groups: separation {era['separation_r']:+.3f} on the "
            f"correlation scale, silhouette {era['silhouette']:+.3f}"
        )
        console.print(
            f"  vs {era['ensemble_draws']} geography-only Voronoi partitions: percentile "
            f"{era['pct_vs_geographic_ensemble_separation']:.3f} on separation, "
            f"{era['pct_vs_geographic_ensemble_silhouette']:.3f} on silhouette"
        )
        ward = result.agreement.filter(pl.col("partition").str.starts_with("ward_")).sort(
            "silhouette", descending=True
        )
        best = ward.to_dicts()[0]
        at_era = result.agreement.filter(pl.col("partition") == f"ward_k{era['k']}").to_dicts()
        console.print(
            f"  the data's own preference: k = {best['k']} "
            f"(silhouette {best['silhouette']:+.3f})"
            + (
                f"; ward at the official k agrees at ARI {at_era[0]['ari_vs_zone_era']:.2f}"
                if at_era
                else ""
            )
        )

    if result.lisa is not None and result.lisa.height:
        bh = int(result.lisa["significant_bh"].sum())
        raw = int(result.lisa["significant_raw"].sum())
        quadrants = {
            row["quadrant"]: row["len"]
            for row in result.lisa.group_by("quadrant").len().iter_rows(named=True)
        }
        console.print("\n[bold]where the residual dependence lives (LISA)[/bold]")
        console.print(
            f"  {result.lisa.height} stations: {bh} significant after BH across the family "
            f"({raw} raw) — "
            + ("no station is individually a hot spot; " if bh == 0 else "")
            + "the dependence is a property of the field, not of a few sites"
        )
        console.print(
            "  quadrants: "
            + "  ".join(f"{q} {quadrants.get(q, 0)}" for q in ("HH", "HL", "LH", "LL"))
        )

    if result.inference is not None and result.inference.height:
        console.print("\n[bold]the original's t-statistics under honest covariances[/bold]")
        pm10 = result.inference.filter(pl.col("term") == "PM10")
        for row in pm10.iter_rows(named=True):
            published = (
                f"  (published: {row['published_t']:.2f})"
                if row["published_t"] is not None and row["cov_type"] == "iid"
                else ""
            )
            fix = "  [yellow]PSD repair applied[/yellow]" if row["psd_fix_applied"] else ""
            console.print(
                f"  PM10  {row['cov_meaning']:<42} t = {row['t']:>7.2f}  "
                f"se ×{row['se_inflation_vs_iid']:.2f}{published}{fix}"
            )
        flips = (
            result.inference.filter(pl.col("cov_type") == "iid")
            .join(
                result.inference.filter(pl.col("cov_type") == "cluster_month"),
                on="term",
                suffix="_m",
            )
            .filter((pl.col("p") < 0.05) & (pl.col("p_m") >= 0.05))["term"]
            .to_list()
        )
        if flips:
            console.print(
                f"  significance lost under the spatial covariance: [bold]{'、'.join(flips)}[/bold]"
            )

    console.print(
        "\n[dim]this prices the OLS stage only. The 2018 report's terminal inference was a "
        "mixed model with AR(1), and its standard errors are not recorded in this "
        "repository[/dim]"
    )
    console.print(
        "[dim]no population-weighted exposure: there is no population grid under data/ or "
        "conf/, and a station mean weighted by anything already here is not an exposure[/dim]"
    )

    for name, path in write_spatial_report(result).items():
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
        "L0,L1", "--levels", help="Which tiers to build. L2 is not published — see docs/legal.md."
    ),
    story: bool = typer.Option(True, "--story/--no-story", help="Also build chapter payloads."),
) -> None:
    """Export meta, L0 (station-month JSON) and L1 (station-day Parquet)."""
    from twair.viz.export import export_all, write_manifest
    from twair.viz.story import export_story

    selected = tuple(part.strip().upper() for part in levels.split(",") if part.strip())
    if "L2" in selected:
        raise typer.BadParameter(
            "L2 is the full hourly record and is not published anywhere. The 2026-07-28 "
            "decision ships the derived layers and the pipeline that rebuilds everything "
            "else, which sidesteps the licensing conflict rather than resolving it — "
            "see docs/legal.md."
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


@export_app.command("space")
def export_space(
    demo_year: int = typer.Option(2025, "--demo-year", help="Year held out for the demo."),
    horizons: str = typer.Option("1,6,24,48", "--horizons", help="Hours ahead to fit."),
) -> None:
    """Build the HuggingFace Space bundle: models, demo slice, manifest.

    Writes into spaces/forecast/. Pushing it anywhere is a separate, manual
    step — this only assembles the directory.
    """
    from twair.models.deploy import build_space_bundle, space_dir

    hours = tuple(int(h) for h in horizons.split(",") if h.strip())
    report = build_space_bundle(demo_year=demo_year, horizons=hours)

    console.print(f"[green]{report.summary()}[/green]")
    console.print(f"stations: {', '.join(report.stations)}")
    console.print(f"wrote {space_dir()}")
    console.print(
        "\n[yellow]Not published.[/yellow] The bundle embeds a sample of hourly "
        "observations; see docs/legal.md before pushing it anywhere."
    )


@export_app.command("dataset")
def export_dataset(
    destination: Annotated[
        Path | None,
        typer.Option(
            "--destination",
            help="Local bundle directory; defaults below the ignored data/exports tree.",
        ),
    ] = None,
    overwrite: Annotated[
        bool,
        typer.Option(
            "--overwrite",
            help="Replace an existing bundle at the exact destination.",
        ),
    ] = False,
) -> None:
    """Build the public L0/L1 Hugging Face Dataset bundle locally."""
    from twair.viz.hf_dataset import build_dataset_bundle

    report = build_dataset_bundle(destination=destination, overwrite=overwrite)
    console.print(f"[green]{report.summary()}[/green]")
    console.print(f"wrote {report.destination}")
    console.print(
        "\n[yellow]Not uploaded.[/yellow] L2 remains local and is not part of this bundle."
    )


@app.command("freshness")
def freshness(
    fail_if_stale: bool = typer.Option(
        False,
        "--fail-if-stale",
        help="Exit 1 unless the export is demonstrably current: a completed year is "
        "missing, or meta.json cannot say. Distance from the live feed is context, "
        "never a failure — the archives are annual.",
    ),
) -> None:
    """How far behind upstream the published data is.

    Reads the committed meta.json rather than the store, so it works in a
    checkout with no data — which is what lets it run in CI. Refreshing is
    still a local step; this only says when one is due.
    """
    from twair.freshness import check_freshness

    report = check_freshness()

    # Green has to mean "measured, and current". An export that cannot say how
    # current it is has not been measured, so it does not get the same colour
    # as one that has — see FreshnessReport.is_unknown.
    trustworthy = not (report.is_unknown or report.is_stale)
    console.print(f"[{'green' if trustworthy else 'yellow'}]{report.summary()}[/]")

    if fail_if_stale and not trustworthy:
        raise typer.Exit(code=1)


@app.command("summary")
def summary() -> None:
    """Row counts per year in the canonical store."""
    from twair.store.writer import partition_summary

    frame = partition_summary()
    console.print(frame)
    console.print(f"total rows: {frame['rows'].sum():,}")


@app.command("status")
def status(
    rows: bool = typer.Option(True, "--rows/--no-rows", help="Count store rows (~0.5s)."),
) -> None:
    """Where the project stands on disk: what has run, what is stale, what next.

    The measured half of the portable return path. ``PLAN.md`` carries the
    roadmap and public technical docs carry durable reasoning; this command
    carries timestamps because stale prose still looks current.
    """
    from twair.status import collect_status, render

    for line in render(collect_status(count_rows=rows)):
        console.print(line, highlight=False)


if __name__ == "__main__":
    app()
