"""Website data export — the three-tier strategy.

341 million hourly rows cannot go into a browser, and a site that only ships
pre-rendered pictures cannot be interrogated. The compromise is three layers,
each with an honest job:

===== ======================== ============ ==================================
Level Grain                    Size         Delivery
===== ======================== ============ ==================================
L0    station × month          ~KB/pollutant JSON, fetched up front, instant
L1    station × day            ~MB/pollutant Parquet, DuckDB-WASM range reads
L2    station × hour           ~GB          not published; rebuild it with the pipeline
===== ======================== ============ ==================================

Four rules hold across all of them.

**Dense columnar arrays, never arrays of objects.** ``{"station": "三重",
"month": "2015-03", "mean": 21.4}`` repeats its own schema on every row and is
roughly ten times the size of the number it carries. The axes are stated once
and the values are a rectangle.

**Absence keeps its meaning.** The single most important property to survive
the trip to the browser is the difference between *no measurement* and *a
measurement withheld for insufficient coverage*. ``n`` records how many
qualifying periods went into each cell: ``n == 0`` with a null mean means the
station was not reporting; ``n > 0`` with a null mean means the aggregate
existed but did not clear the coverage threshold and was deliberately
suppressed upstream. A chart must be able to draw a gap. Interpolating across
one is exactly the sin this whole project exists to document.

**Rounding is one-way and declared.** Values are rounded to a per-pollutant
precision that reflects how they were measured. The website is a viewer, not
the archive of record — the store this exports from is, and every export
names it.

**Only documented measurands ship.** The store carries 48 distinct channel
codes; 20 of them appear in MOENV's own ReadMe and the rest are artefacts of
instrument commissioning (``O3-TEST2``, ``SO2-test``, ``SHELTER``, nine rows
in total for some). Those belong in the data-quality report, not on a chart.
``conf/pollutants.yaml`` is the allow-list.
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import polars as pl

from twair import __version__
from twair.config import load_conf
from twair.paths import WEB_DIR, outputs_dir, processed_dir

log = logging.getLogger(__name__)

__all__ = [
    "ExportResult",
    "export_all",
    "export_l0",
    "export_l1",
    "web_data_dir",
    "write_manifest",
]

# Where the site reads its data from. Astro serves `public/` at the site root.
WEB_DATA = Path("public") / "data"

# Decimal places per unit. Reporting PM2.5 to four places would imply a
# precision the beta-attenuation instrument does not have; reporting CO to
# zero places would throw away most of its range.
PRECISION_BY_UNIT = {
    "ppm": 3,
    "ppb": 2,
    "ug/m3": 2,
    "degC": 2,
    "percent": 1,
    "m/s": 2,
    "degree": 1,
    "mm": 2,
    "pH": 2,
    "uS/cm": 1,
    "UVI": 2,
}
DEFAULT_PRECISION = 2

# One Parquet row group per station-decade or so. Small enough that DuckDB-WASM
# can fetch a single station's slice without pulling the file, large enough
# that the footer does not dominate.
L1_ROW_GROUP = 50_000


def web_data_dir() -> Path:
    return WEB_DIR / WEB_DATA


@dataclass(frozen=True, slots=True)
class ExportResult:
    """What an export produced, for the CLI and the manifest."""

    files: list[Path]
    bytes_total: int

    def summary(self) -> str:
        return f"{len(self.files)} file(s), {self.bytes_total / 1e6:.1f} MB"


def documented_pollutants(config: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    """The allow-list: measurands MOENV documents, in config order."""
    conf = config if config is not None else load_conf("pollutants")
    return dict(conf.get("pollutants", {}))


def _precision(meta: dict[str, Any]) -> int:
    return PRECISION_BY_UNIT.get(str(meta.get("unit", "")), DEFAULT_PRECISION)


def _git(*args: str) -> str | None:
    """Run a git command, or return None if git is not there to answer."""
    try:
        out = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip()


def _git_state() -> tuple[str | None, bool]:
    """The commit this export was made at, and whether that is the whole story.

    This used to be `_git_sha()` alone, documented as "the commit that produced
    this export" — a sentence that is false every time the export is run over
    uncommitted work, which is most times. `rev-parse HEAD` names the last
    commit, not the tree; a payload built from a modified working directory is
    stamped with the commit BEFORE the change that produced it, and every
    consumer of that field then points at code which cannot have made it.

    That is not hypothetical here. The site's footer prints this sha and now
    links it, `status.py` reports 「generated … from <sha>」, and
    `scripts/check_web_export.py` — whose whole reason for existing is that a
    payload and its manifest drifted apart for two days — printed the field and
    never checked it.

    So the dirty flag travels with the sha. A reader is told 「這份資料匯出時工作
    區還有未提交的變更」 instead of being given a commit link that lies, and the
    gate can say so out loud rather than implying a provenance nobody verified.
    """
    sha = _git("rev-parse", "--short", "HEAD") or None
    if sha is None:
        return None, False
    # `--porcelain` is empty exactly when the tree matches HEAD. Untracked files
    # are excluded: a scratch file beside the repo does not change what the code
    # did, and counting it would mark almost every honest export dirty.
    status = _git("status", "--porcelain", "--untracked-files=no")
    return sha, bool(status)


def _json_default(value: Any) -> Any:
    """Serialise the types Polars hands back that JSON does not know.

    Dates become ISO strings rather than epoch numbers: a chart axis reads
    ``2015-03-01`` correctly in every timezone, and ``1425168000000`` does not.
    """
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, time):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(f"cannot serialise {type(value).__name__} to JSON")


def write_json(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    # separators without spaces: on a 40,000-element array the spaces alone are
    # tens of kilobytes over the wire.
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=_json_default)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def _month_axis(lo: Any, hi: Any) -> list[str]:
    months: list[str] = []
    year, month = lo.year, lo.month
    while (year, month) <= (hi.year, hi.month):
        months.append(f"{year:04d}-{month:02d}")
        month += 1
        if month == 13:
            year, month = year + 1, 1
    return months


def export_l0(
    destination: Path | None = None,
    *,
    monthly: pl.DataFrame | None = None,
    stations: list[str] | None = None,
) -> ExportResult:
    """Station × month means, one dense JSON per pollutant.

    Each file carries its own month axis, trimmed to the span in which that
    measurand was actually recorded. PM2.5 monitoring began two decades after
    SO2; padding it back to 1982 with nulls would triple the file to say
    nothing.
    """
    root = destination or web_data_dir()
    frame = monthly if monthly is not None else _read_monthly()

    allowed = documented_pollutants()
    frame = frame.filter(pl.col("pollutant").is_in(list(allowed)))
    if frame.is_empty():
        raise RuntimeError("no documented pollutants in the monthly table — nothing to export")

    station_axis = stations or sorted(frame["station_name"].unique().to_list())
    station_index = {name: i for i, name in enumerate(station_axis)}

    written: list[Path] = []
    index_entries: list[dict[str, Any]] = []

    for code, meta in allowed.items():
        subset = frame.filter(pl.col("pollutant") == code)
        if subset.is_empty():
            continue

        months = _month_axis(subset["month"].min(), subset["month"].max())
        month_index = {m: i for i, m in enumerate(months)}
        places = _precision(meta)

        # Rectangles pre-filled with the "never reported" state, then punched
        # out by the rows that exist. Anything the store has no row for stays
        # (null, 0) — which the site reads as "this station was not measuring".
        mean: list[list[float | None]] = [[None] * len(months) for _ in station_axis]
        days: list[list[int]] = [[0] * len(months) for _ in station_axis]

        rows = subset.select(
            "station_name",
            pl.col("month").dt.strftime("%Y-%m").alias("ym"),
            pl.col("mean").round(places),
            pl.col("n_days").cast(pl.Int32),
        )
        for station, ym, value, n_days in rows.iter_rows():
            si = station_index.get(station)
            mi = month_index.get(ym)
            if si is None or mi is None:
                continue
            mean[si][mi] = None if value is None else round(value, places)
            days[si][mi] = int(n_days)

        payload = {
            "pollutant": code,
            "name_zh": meta.get("name_zh"),
            "unit": meta.get("unit"),
            "precision": places,
            "months": months,
            "stations": station_axis,
            "mean": mean,
            "n_days": days,
            "null_means": {
                "n_days == 0": "station not reporting this measurand that month",
                "n_days > 0": "aggregate withheld: coverage below threshold",
            },
        }
        path = write_json(root / "l0" / f"{_slug(code)}.json", payload)
        written.append(path)
        index_entries.append(
            {
                "pollutant": code,
                "name_zh": meta.get("name_zh"),
                "unit": meta.get("unit"),
                "file": f"l0/{_slug(code)}.json",
                "months": [months[0], months[-1]],
                "bytes": path.stat().st_size,
            }
        )

    written.append(
        write_json(
            root / "l0" / "index.json",
            {"stations": station_axis, "pollutants": index_entries},
        )
    )
    return ExportResult(written, sum(p.stat().st_size for p in written))


def export_l1(
    destination: Path | None = None,
    *,
    daily: pl.LazyFrame | None = None,
) -> ExportResult:
    """Station × day, one Parquet per pollutant, for DuckDB-WASM.

    Only the four columns a chart needs survive: which station, which day, the
    mean, and how many valid hours produced it. ``min``/``max``/``consistency``
    stay in ``data/processed`` — a reader who wants them wants L2 anyway.
    """
    root = destination or web_data_dir()
    lazy = daily if daily is not None else pl.scan_parquet(_daily_path())

    allowed = documented_pollutants()
    written: list[Path] = []

    for code, meta in allowed.items():
        places = _precision(meta)
        subset = (
            lazy.filter(pl.col("pollutant") == code)
            .select(
                pl.col("station_name").cast(pl.Categorical),
                pl.col("date"),
                pl.col("mean").round(places).cast(pl.Float32),
                pl.col("n_valid").cast(pl.UInt8),
            )
            .sort("station_name", "date")
            .collect()
        )
        if subset.is_empty():
            continue

        path = root / "l1" / f"{_slug(code)}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        subset.write_parquet(path, compression="zstd", row_group_size=L1_ROW_GROUP)
        written.append(path)
        log.info("L1 %s: %d rows, %.1f MB", code, subset.height, path.stat().st_size / 1e6)

    return ExportResult(written, sum(p.stat().st_size for p in written))


def export_meta(destination: Path | None = None) -> Path:
    """Stations, measurands, coverage and provenance — the site's index.

    Stations without coordinates are included with null ``lon``/``lat``. The
    map cannot draw them; a list of "stations we have data for but cannot
    place" can, and should.
    """
    root = destination or web_data_dir()
    stations = _read_stations()
    allowed = documented_pollutants()

    geo_columns = [
        c for c in ("county", "township", "lon", "lat", "station_name_en") if c in stations.columns
    ]
    station_records = stations.select(
        "station_name",
        *geo_columns,
        "airzone",
        "station_type",
        "first_year",
        "last_year",
        "years_present",
    ).to_dicts()

    unplaced = [r["station_name"] for r in station_records if r.get("lat") is None]

    _sha, _dirty = _git_state()
    payload = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "twair_version": __version__,
        "git_sha": _sha,
        "git_dirty": _dirty,
        "stations": station_records,
        "stations_without_coordinates": unplaced,
        "pollutants": [
            {
                "code": code,
                "name_zh": meta.get("name_zh"),
                "unit": meta.get("unit"),
                "valid_range": meta.get("valid_range"),
                "circular": bool(meta.get("circular", False)),
                "caveat": meta.get("caveat"),
            }
            for code, meta in allowed.items()
        ],
        "layers": {
            "L0": "station-month means, inline JSON",
            "L1": "station-day means, Parquet via DuckDB-WASM",
            "L2": "station-hour observations with QA flags — HuggingFace, not this site",
        },
        "coverage_note": (
            "Aggregates below the coverage threshold carry a null mean and a non-zero "
            "count. That is a deliberate refusal to average, not a missing file."
        ),
        # The newest observation the export was built from, which is not the
        # same as when the export ran and is the question a reader actually
        # has. It is also the only way anything without a copy of the store can
        # tell how far behind upstream this site is — see `twair freshness`,
        # which runs in CI where the 341M-row store does not exist.
        "data_through": _data_through(),
        # How many hourly observations the record holds.
        #
        # Added because the site was stating this figure as literal text in its
        # opening line — 341,442,552, a number that appeared nowhere else in the
        # repository, so nothing could contradict it and no run could refresh it.
        # It is the one headline figure the published data cannot check on its
        # own: it counts L2 station-hours, and L2 is deliberately not published.
        # Recording it here is what makes it checkable.
        "hourly_observations": _hourly_observations(),
    }
    return write_json(root / "meta.json", payload)


def _data_through() -> str | None:
    """Timestamp of the newest observation in the store, as ISO 8601."""
    from twair.store.writer import scan_observations

    try:
        newest = scan_observations().select(pl.col("ts_local").max()).collect().item()
    except Exception as exc:
        log.warning("could not determine data_through: %s", exc)
        return None
    return None if newest is None else str(newest)


def _hourly_observations() -> int | None:
    """How many rows the observation store holds, or ``None`` if it cannot say.

    Same shape as :func:`_data_through`, and for the same reason: this runs in CI
    where the 341M-row store does not exist, so a missing store has to degrade to
    "unknown" rather than fail the export. The site renders the clause only when
    the number is present, so ``None`` costs a clause and never a wrong figure.

    Cheap on a Parquet-backed store — a length over a lazy scan is answered from
    row-group metadata rather than by reading the columns.
    """
    from twair.store.writer import scan_observations

    try:
        total = scan_observations().select(pl.len()).collect().item()
    except Exception as exc:
        log.warning("could not count hourly observations: %s", exc)
        return None
    return None if total is None else int(total)


def write_manifest(destination: Path | None = None) -> Path:
    """Checksum every exported file.

    A figure on the site should be traceable to the bytes that produced it. The
    manifest is what makes "which version of the data is this chart drawn from"
    answerable after the fact.
    """
    root = destination or web_data_dir()
    entries = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name == "manifest.json":
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        entries.append(
            {
                "file": path.relative_to(root).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": digest,
            }
        )

    _sha, _dirty = _git_state()
    return write_json(
        root / "manifest.json",
        {
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "git_sha": _sha,
            "git_dirty": _dirty,
            "files": entries,
            "bytes_total": sum(e["bytes"] for e in entries),
        },
    )


def export_all(
    destination: Path | None = None, *, levels: tuple[str, ...] = ("L0", "L1")
) -> dict[str, ExportResult]:
    """Build every layer the site needs, then the manifest."""
    root = destination or web_data_dir()
    root.mkdir(parents=True, exist_ok=True)

    results: dict[str, ExportResult] = {}
    meta = export_meta(root)
    results["meta"] = ExportResult([meta], meta.stat().st_size)

    if "L0" in levels:
        results["L0"] = export_l0(root)
    if "L1" in levels:
        results["L1"] = export_l1(root)

    manifest = write_manifest(root)
    results["manifest"] = ExportResult([manifest], manifest.stat().st_size)
    return results


def _slug(code: str) -> str:
    """File-safe pollutant code. ``PM2.5`` -> ``pm25``."""
    return code.lower().replace(".", "").replace("/", "-")


def _daily_path() -> Path:
    path = processed_dir("daily") / "daily.parquet"
    if not path.exists():
        raise FileNotFoundError(f"{path} not found — run `twair aggregate` first")
    return path


def _read_monthly() -> pl.DataFrame:
    path = processed_dir("monthly") / "monthly.parquet"
    if not path.exists():
        raise FileNotFoundError(f"{path} not found — run `twair aggregate` first")
    return pl.read_parquet(path)


def _read_stations() -> pl.DataFrame:
    path = outputs_dir("qc") / "stations.parquet"
    if not path.exists():
        raise FileNotFoundError(f"{path} not found — run `twair stations` first")
    return pl.read_parquet(path)
