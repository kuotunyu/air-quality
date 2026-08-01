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
    data_type: str = HOURLY_DATA_TYPE,
) -> list[AirtwFile]:
    """Pick one archive per year, newest first.

    `data_type` was hardcoded to 全年逐時資料. The catalogue this walks already
    holds the other two kinds MOENV publishes — 品保查核報告 and 年報 — and the
    open backlog item 「取得各年度品保查核報告，交叉驗證異常偵測結果」 has been
    written as though there were no way to reach them. There was: the link is
    committed in `conf/sources.yaml`, and only this line stood between it and a
    download.

    What was genuinely missing was somewhere safe to put the result — see
    `_destination`, where all three 2018 files resolved to one filename.
    """
    chosen = [
        f
        for f in catalog
        if f.data_type == data_type
        and f.station_group == station_group
        and (years is None or f.year in years)
    ]
    # Guard against a year appearing twice if MOENV lists it in both views.
    by_year: dict[int, AirtwFile] = {}
    for item in chosen:
        by_year.setdefault(item.year, item)
    return [by_year[y] for y in sorted(by_year, reverse=True)]


def _destination(item: AirtwFile) -> Path:
    """Where an archive lands. The data type is part of it, and has to be.

    MOENV publishes three kinds of file per year behind the same page — 全年逐時
    資料, 品保查核報告 and 年報 — and this returned ``{year}_{group}.zip`` for
    all of them. Enumerated over the cached catalogue, three different 2018
    files resolve to ``2018_全部.zip`` and two 2019 files to ``2019_全部.zip``.

    Nothing has gone wrong yet only because `hourly_archives()` filters the
    other two away before anything is fetched. The moment anyone relaxes that
    filter — which is exactly what the open backlog item 「取得各年度品保查核報告」
    asks for — the QA report overwrites the hourly archive at that path. And
    then it oscillates: the ledger keys ARE distinct
    (``airtw/2018/hourly/all`` against ``airtw/2018/品保查核報告/all``), so the
    next `is_cached` for the hourly archive finds the right ledger entry, sees
    the size no longer matches, logs 「cached file size mismatch」 and downloads
    the hourly archive back over the report. The two would fight over one
    filename forever, and each would look correct on its own.

    The key already encodes what makes a file unique, so the path follows it.
    The hourly case keeps its historical spelling: 44 archives are cached under
    it, and renaming them would re-download 1.5 GB to change nothing.
    """
    if item.data_type == HOURLY_DATA_TYPE:
        return raw_dir("airtw") / f"{item.year}_{item.station_group}.zip"
    # The reports are PDFs; naming one `.zip` would be a third thing that is
    # true in the filename and false in the file.
    return raw_dir("airtw") / f"{item.year}_{item.station_group}_{item.data_type}.pdf"


# Drive throttles after a few dozen fetches from the same folder with
# "Cannot retrieve the public link ... or have had many accesses". It is a
# rate limit, not a permanent failure, so it is worth waiting out.
_QUOTA_MARKERS = ("many accesses", "Cannot retrieve the public link", "quota")
_QUOTA_BACKOFF_SECONDS = (60, 180, 420, 900)


def _looks_like_quota(message: str) -> bool:
    return any(marker.lower() in message.lower() for marker in _QUOTA_MARKERS)


def _is_supported_archive(path: Path, data_type: str = HOURLY_DATA_TYPE) -> bool:
    """Does the downloaded file look like the thing it claims to be?

    The guard exists because Google Drive answers an unavailable file with an
    HTML interstitial and a 200, so "not the expected format" is how a failed
    download announces itself here.

    What counts as expected depends on the type, and it used to not. The hourly
    archives are zip or 7z; 品保查核報告 and 年報 are **PDFs**, and this
    returned False for them and deleted the file — measured, on the real 2018
    report: 「not an archive (got b'%PDF-1.4…')」. The download had worked.
    """
    with path.open("rb") as fh:
        magic = fh.read(8)
    if data_type == HOURLY_DATA_TYPE:
        return magic.startswith(b"PK\x03\x04") or magic.startswith(b"7z\xbc\xaf\x27\x1c")
    # A report may be published as a PDF or wrapped in an archive; both are real.
    return (
        magic.startswith(b"%PDF")
        or magic.startswith(b"PK\x03\x04")
        or magic.startswith(b"7z\xbc\xaf\x27\x1c")
    )


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

    # `from gdown.download import download` — and the spelling is the whole bug.
    #
    # This said `import gdown.download as gdown`. gdown ships a submodule
    # `gdown/download.py` AND re-exports the function as `gdown.download`, and
    # for that form the package attribute wins: the name `gdown` was bound to
    # the FUNCTION, so the call below became `<function>.download(...)` and
    # raised 「'function' object has no attribute 'download'」 on every archive.
    #
    # Which means this project's first pipeline step did not run at all under
    # gdown 6.1.0. It was invisible because all 44 archives are already cached
    # so `download_one` returns before reaching here, and because CI never
    # downloads anything. The claim this whole repository rests on is that
    # someone else can rebuild the data from scratch; that path was broken.
    #
    # The submodule form rather than plain `import gdown`, because gdown ships
    # `py.typed` without an `__all__`, so `gdown.download` is an implicit
    # re-export and mypy's strict mode rejects it. Importing from the module
    # that actually defines the function is both the honest spelling and the one
    # that type-checks.
    from gdown.download import download as gdown_download

    attempts = len(_QUOTA_BACKOFF_SECONDS) + 1 if patient else 1
    error = ""
    for attempt in range(attempts):
        try:
            gdown_download(id=item.drive_file_id, output=str(dest), quiet=True)
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

    if not _is_supported_archive(dest, item.data_type):
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
    data_type: str = HOURLY_DATA_TYPE,
) -> list[DownloadResult]:
    """Download every selected annual archive."""
    if refresh_catalog:
        with PoliteClient() as client:
            catalog = fetch_catalog(client)
    else:
        catalog = load_catalog_from_conf()

    targets = select_archives(catalog, years=years, data_type=data_type)
    if not targets:
        console.print("[yellow]No archives matched the requested years.[/yellow]")
        return []

    console.print(
        f"[bold]{len(targets)}[/bold] {data_type} archive(s) selected "
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
