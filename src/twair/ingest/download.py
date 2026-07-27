"""Download airtw annual archives, with caching and provenance.

Only the 全部 (all-stations) archive is fetched per year: the per-airzone files
are subsets of it, so downloading them as well would duplicate every record.

Every completed download is recorded in ``data/raw/_manifest.jsonl`` with its
SHA-256, so re-running is cheap and the provenance of the processed dataset is
always traceable back to a specific upstream file.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console
from rich.progress import BarColumn, Progress, TaskProgressColumn, TextColumn, TimeElapsedColumn

from twair.config import load_conf
from twair.ingest.airtw import HOURLY_DATA_TYPE, AirtwFile, fetch_catalog
from twair.net import PoliteClient, is_cached, record_download
from twair.paths import raw_dir

log = logging.getLogger(__name__)
console = Console()

ALL_STATIONS = "全部"


@dataclass(frozen=True, slots=True)
class DownloadResult:
    file: AirtwFile
    path: Path | None
    cached: bool
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.path is not None


def load_catalog_from_conf() -> list[AirtwFile]:
    """Read the catalogue recorded by ``twair probe sources``."""
    conf = load_conf("sources")
    entries = conf.get("airtw", {}).get("files", [])
    if not entries:
        raise RuntimeError(
            "conf/sources.yaml has no airtw files — run `twair probe sources` first."
        )
    return [AirtwFile(**entry) for entry in entries]


def select_archives(
    catalog: list[AirtwFile],
    *,
    years: range | None = None,
    station_group: str = ALL_STATIONS,
) -> list[AirtwFile]:
    """Pick one hourly archive per year, newest first."""
    chosen = [
        f
        for f in catalog
        if f.data_type == HOURLY_DATA_TYPE
        and f.station_group == station_group
        and (years is None or f.year in years)
    ]
    # Guard against a year appearing twice if MOENV lists it in both views.
    by_year: dict[int, AirtwFile] = {}
    for item in chosen:
        by_year.setdefault(item.year, item)
    return [by_year[y] for y in sorted(by_year, reverse=True)]


def _destination(item: AirtwFile) -> Path:
    return raw_dir("airtw") / f"{item.year}_{item.station_group}.zip"


# Drive throttles after a few dozen fetches from the same folder with
# "Cannot retrieve the public link ... or have had many accesses". It is a
# rate limit, not a permanent failure, so it is worth waiting out.
_QUOTA_MARKERS = ("many accesses", "Cannot retrieve the public link", "quota")
_QUOTA_BACKOFF_SECONDS = (60, 180, 420, 900)


def _looks_like_quota(message: str) -> bool:
    return any(marker.lower() in message.lower() for marker in _QUOTA_MARKERS)


def _is_supported_archive(path: Path) -> bool:
    """MOENV ships both zip and 7z; anything else means we got an error page."""
    with path.open("rb") as fh:
        magic = fh.read(8)
    return magic.startswith(b"PK\x03\x04") or magic.startswith(b"7z\xbc\xaf\x27\x1c")


def download_one(item: AirtwFile, *, force: bool = False, patient: bool = False) -> DownloadResult:
    """Fetch a single archive, reusing an intact cached copy when possible.

    With ``patient=True`` the download waits out Google Drive rate limits
    instead of giving up, which is what the oldest years need after a long run
    has already burned through the per-folder allowance.
    """
    if not force:
        cached = is_cached(item.key)
        if cached is not None:
            return DownloadResult(item, cached, cached=True)

    dest = _destination(item)
    dest.parent.mkdir(parents=True, exist_ok=True)

    import gdown

    attempts = len(_QUOTA_BACKOFF_SECONDS) + 1 if patient else 1
    error = ""
    for attempt in range(attempts):
        try:
            gdown.download(id=item.drive_file_id, output=str(dest), quiet=True)
            error = ""
            break
        except Exception as exc:
            error = str(exc)
            if not (patient and _looks_like_quota(error) and attempt < attempts - 1):
                log.warning("download failed for %s: %s", item.key, error)
                return DownloadResult(item, None, cached=False, error=error)
            wait = _QUOTA_BACKOFF_SECONDS[attempt]
            log.info("%s rate-limited; waiting %ss before retry", item.key, wait)
            time.sleep(wait)

    if error:
        return DownloadResult(item, None, cached=False, error=error)

    if not dest.exists() or dest.stat().st_size == 0:
        # Google Drive returns an HTML interstitial instead of an error status
        # when a file is unavailable, so an empty result is a real failure mode.
        return DownloadResult(item, None, cached=False, error="empty or missing after download")

    if not _is_supported_archive(dest):
        head = dest.read_bytes()[:64]
        dest.unlink(missing_ok=True)
        return DownloadResult(item, None, cached=False, error=f"not an archive (got {head[:32]!r})")

    record_download(
        key=item.key,
        url=item.url,
        path=dest,
        source="airtw",
        extra={
            "year": item.year,
            "station_group": item.station_group,
            "data_type": item.data_type,
            "drive_file_id": item.drive_file_id,
        },
    )
    return DownloadResult(item, dest, cached=False)


def download_archives(
    *,
    years: range | None = None,
    refresh_catalog: bool = False,
    force: bool = False,
    patient: bool = False,
) -> list[DownloadResult]:
    """Download every selected annual archive."""
    if refresh_catalog:
        with PoliteClient() as client:
            catalog = fetch_catalog(client)
    else:
        catalog = load_catalog_from_conf()

    targets = select_archives(catalog, years=years)
    if not targets:
        console.print("[yellow]No archives matched the requested years.[/yellow]")
        return []

    console.print(
        f"[bold]{len(targets)}[/bold] annual archive(s) selected "
        f"({targets[-1].year}–{targets[0].year})"
    )

    results: list[DownloadResult] = []
    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("downloading", total=len(targets))
        for item in targets:
            progress.update(task, description=f"downloading {item.year}")
            result = download_one(item, force=force, patient=patient)
            results.append(result)
            progress.advance(task)

    downloaded = sum(1 for r in results if r.ok and not r.cached)
    cached = sum(1 for r in results if r.cached)
    failed = [r for r in results if not r.ok]

    total_bytes = sum(r.path.stat().st_size for r in results if r.path)
    console.print(
        f"  [green]{downloaded}[/green] downloaded, "
        f"[blue]{cached}[/blue] already cached, "
        f"[red]{len(failed)}[/red] failed — {total_bytes / 1e9:.2f} GB on disk"
    )
    for r in failed:
        console.print(f"  [red]FAILED[/red] {r.file.year}: {r.error}")

    return results
