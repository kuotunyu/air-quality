"""Read MOENV annual archives across all three format generations.

The archives changed shape several times between 1982 and 2025 (encoding,
column order, hour labelling, container format). Rather than hard-coding year
boundaries — which would silently mis-parse any year we guessed wrong — the
dialect is **detected from each file's own header**.

See docs/archive-formats.md for the observed differences.
"""

from __future__ import annotations

import io
import logging
import os
import re
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree

import polars as pl

from twair.qc.flags import parse_expr

log = logging.getLogger(__name__)

# Canonical names for the three identifying columns, and the header labels
# each generation uses for them.
DATE_HEADERS = ("日期", "監測日期")
STATION_HEADERS = ("測站", "測站名稱")
ITEM_HEADERS = ("測項", "測項簡稱")

_HOUR_LABEL = re.compile(r"^\d{1,2}$")

# Encodings to try, in order. UTF-8 first: a Big5 file almost never decodes as
# valid UTF-8, so a successful UTF-8 decode is strong evidence.
ENCODINGS = ("utf-8-sig", "utf-8", "cp950", "big5", "gb18030")

# ODS namespaces.
_NS = {
    "table": "urn:oasis:names:tc:opendocument:xmlns:table:1.0",
    "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
    "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
}
_REPEAT_ATTR = f"{{{_NS['table']}}}number-columns-repeated"
_CELL_TAG = f"{{{_NS['table']}}}table-cell"
_ROW_TAG = f"{{{_NS['table']}}}table-row"

# ODS pads rows with cells repeated thousands of times; expanding those
# faithfully would blow up memory for no benefit.
_MAX_REPEAT = 64


class ArchiveFormatError(RuntimeError):
    """Raised when a member cannot be mapped onto a known dialect."""


# MOENV switched some years to 7-Zip while keeping the `.zip` extension in the
# download link, so the container is identified by magic bytes, not by name.
_ZIP_MAGIC = b"PK\x03\x04"
_7Z_MAGIC = b"7z\xbc\xaf\x27\x1c"


class ArchiveContainer:
    """Uniform read access to a zip or 7z archive.

    Both are used by MOENV, sometimes for adjacent years, and 7z members can
    only be extracted in bulk — so this hides the difference from the readers.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.kind = detect_container(path)
        self._zip: zipfile.ZipFile | None = None
        self._extracted: Path | None = None
        self._tmp: tempfile.TemporaryDirectory[str] | None = None

    def __enter__(self) -> ArchiveContainer:
        if self.kind == "zip":
            self._zip = zipfile.ZipFile(self.path)
        return self

    def __exit__(self, *exc: object) -> None:
        if self._zip is not None:
            self._zip.close()
            self._zip = None
        if self._tmp is not None:
            self._tmp.cleanup()
            self._tmp = None
            self._extracted = None

    def _extract_seven(self) -> Path:
        """Extract a 7z archive once into scratch space.

        py7zr has no random-access read, and decompression is solid-block, so
        pulling members individually would re-inflate the whole archive each
        time. Extracting once and reading from disk is both simpler and faster.
        """
        if self._extracted is None:
            import py7zr

            self._tmp = tempfile.TemporaryDirectory(prefix="twair-7z-")
            self._extracted = Path(self._tmp.name)
            log.debug("extracting %s to %s", self.path.name, self._extracted)
            with py7zr.SevenZipFile(self.path, mode="r") as archive:
                archive.extractall(path=self._extracted)
        return self._extracted

    def namelist(self) -> list[str]:
        if self.kind == "zip":
            assert self._zip is not None, "use ArchiveContainer as a context manager"
            return self._zip.namelist()

        import py7zr

        with py7zr.SevenZipFile(self.path, mode="r") as archive:
            return list(archive.namelist())

    def read(self, name: str) -> bytes:
        if self.kind == "zip":
            assert self._zip is not None, "use ArchiveContainer as a context manager"
            return self._zip.read(name)
        return (self._extract_seven() / name).read_bytes()


def detect_container(path: Path) -> str:
    """Identify the container from its magic bytes."""
    with path.open("rb") as fh:
        magic = fh.read(8)
    if magic.startswith(_ZIP_MAGIC):
        return "zip"
    if magic.startswith(_7Z_MAGIC):
        return "7z"
    raise ArchiveFormatError(f"{path.name} is neither zip nor 7z (magic={magic[:8]!r})")


@dataclass(frozen=True, slots=True)
class Dialect:
    """How one archive member lays out its data."""

    date_col: str
    station_col: str
    item_col: str
    hour_map: dict[str, int]
    """Header label -> hour of day (0-23)."""
    hour_base: int
    """1 when hours are labelled 1-24, 0 when labelled 00-23."""
    encoding: str | None
    container: str
    """csv | ods | xls"""

    @property
    def generation(self) -> str:
        if self.container == "xls":
            return "legacy_xls"
        if self.container == "ods":
            return "legacy_ods"
        if self.encoding in {"cp950", "big5"}:
            return "legacy_csv_big5"
        return "modern_csv_utf8"


def _find_header(headers: list[str], candidates: tuple[str, ...], what: str) -> str:
    for name in headers:
        if name.strip() in candidates:
            return name
    raise ArchiveFormatError(f"no {what} column in headers: {headers!r}")


def detect_dialect(headers: list[str], *, encoding: str | None, container: str) -> Dialect:
    """Work out the layout from header labels alone.

    The hour labelling is the subtle one. Big5-era files label the columns
    1-24, but the bundled ReadMe states "0時：指 0:00-0:59" — so the column
    labelled ``1`` holds midnight, not 1 a.m. Getting this wrong shifts an
    entire generation of data by one hour.
    """
    cleaned = [h.strip() for h in headers]

    hour_headers = [h for h in cleaned if _HOUR_LABEL.match(h)]
    if not hour_headers:
        raise ArchiveFormatError(f"no hour columns in headers: {headers!r}")

    values = sorted(int(h) for h in hour_headers)
    if values == list(range(24)):
        base = 0
    elif values == list(range(1, 25)):
        base = 1
    else:
        raise ArchiveFormatError(
            f"unexpected hour labels {values[:3]}…{values[-3:]} (expected 0-23 or 1-24)"
        )

    hour_map = {h: int(h) - base for h in hour_headers}

    return Dialect(
        date_col=_find_header(cleaned, DATE_HEADERS, "date"),
        station_col=_find_header(cleaned, STATION_HEADERS, "station"),
        item_col=_find_header(cleaned, ITEM_HEADERS, "item"),
        hour_map=hour_map,
        hour_base=base,
        encoding=encoding,
        container=container,
    )


def _decode(raw: bytes) -> tuple[str, str]:
    """Return (text, encoding_name), trying encodings in priority order."""
    for encoding in ENCODINGS:
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    # Last resort: never lose a file to a stray byte, but say so loudly.
    log.warning("falling back to lossy utf-8 decode")
    return raw.decode("utf-8", errors="replace"), "utf-8-lossy"


def _read_csv_member(raw: bytes) -> tuple[pl.DataFrame, str]:
    text, encoding = _decode(raw)
    frame = pl.read_csv(
        io.BytesIO(text.encode("utf-8")),
        infer_schema_length=0,  # everything stays Utf8; flags live inside values
        truncate_ragged_lines=True,
        has_header=True,
    )
    return frame, encoding


def _ods_rows(raw: bytes) -> list[list[str]]:
    """Extract the first sheet of an ODS document as rows of raw strings.

    Handles ``table:number-columns-repeated``, which ODS uses both for genuine
    repeats and for end-of-row padding; ignoring it shifts every column.
    """
    with zipfile.ZipFile(io.BytesIO(raw)) as inner:
        content = inner.read("content.xml")

    rows: list[list[str]] = []
    for _, element in ElementTree.iterparse(io.BytesIO(content), events=("end",)):
        if element.tag != _ROW_TAG:
            continue

        cells: list[str] = []
        for cell in element.findall(_CELL_TAG):
            text = "".join(cell.itertext()).strip()
            repeat = int(cell.get(_REPEAT_ATTR, "1"))
            cells.extend([text] * min(repeat, _MAX_REPEAT))

        while cells and cells[-1] == "":
            cells.pop()
        if cells:
            rows.append(cells)

        element.clear()

    return rows


def _read_ods_member(raw: bytes) -> tuple[pl.DataFrame, str]:
    rows = _ods_rows(raw)
    if not rows:
        raise ArchiveFormatError("ODS member contained no rows")

    header = rows[0]
    width = len(header)
    body = [row[:width] + [""] * (width - len(row)) for row in rows[1:]]

    frame = pl.DataFrame(
        {name: [row[i] for row in body] for i, name in enumerate(header)},
        schema=dict.fromkeys(header, pl.Utf8),
    )
    return frame, "xml"


def _xls_cell_to_text(book: object, sheet: object, row: int, col: int) -> str:
    """Render one XLS cell as the string the CSV generations would have held.

    Three cases matter: dates arrive as Excel serial numbers, numbers as
    floats, and quality-flagged readings (``-99#``) as text that must survive
    untouched for the flag parser.
    """
    import xlrd

    kind = sheet.cell_type(row, col)  # type: ignore[attr-defined]
    value = sheet.cell_value(row, col)  # type: ignore[attr-defined]

    if kind == xlrd.XL_CELL_DATE:
        stamp = xlrd.xldate.xldate_as_datetime(value, book.datemode)  # type: ignore[attr-defined]
        return stamp.strftime("%Y/%m/%d")
    if kind == xlrd.XL_CELL_EMPTY:
        return ""
    if kind == xlrd.XL_CELL_NUMBER:
        # Keep integers integral so `15.0` does not become the string "15.0"
        # in a column the rest of the pipeline treats as raw text.
        return str(int(value)) if float(value).is_integer() else str(value)
    return str(value).strip()


def _read_xls_member(raw: bytes) -> tuple[pl.DataFrame, str]:
    """Read a legacy BIFF spreadsheet.

    Only needed for 1987, the one year MOENV published without an ODS twin.
    """
    import xlrd

    try:
        book = xlrd.open_workbook(file_contents=raw)
        sheet = book.sheet_by_index(0)
    except Exception as exc:
        # Normalise to our own error so a single unreadable member is skipped
        # by read_archive rather than aborting the whole year.
        raise ArchiveFormatError(f"unreadable XLS member: {exc}") from exc

    if sheet.nrows < 2:
        raise ArchiveFormatError("XLS member has no data rows")

    header = [str(sheet.cell_value(0, c)).strip() for c in range(sheet.ncols)]
    columns: dict[str, list[str]] = {name: [] for name in header}
    for row in range(1, sheet.nrows):
        for col, name in enumerate(header):
            columns[name].append(_xls_cell_to_text(book, sheet, row, col))

    frame = pl.DataFrame(columns, schema=dict.fromkeys(header, pl.Utf8))
    return frame, "biff"


def select_members(names: list[str]) -> list[str]:
    """Choose which archive members to parse.

    1994-era archives ship each station as **both** ``.ods`` and ``.xls`` with
    identical content. We take the ODS — it is plain XML inside a zip, so it
    needs no binary spreadsheet reader — and skip the XLS twin. ``.doc`` and
    ``.odt`` members are the bundled ReadMe, not data.
    """
    data_members = [n for n in names if not n.endswith("/")]
    by_stem: dict[str, dict[str, str]] = {}

    for name in data_members:
        path = PurePosixPath(name)
        suffix = path.suffix.lower()
        if suffix not in {".csv", ".ods", ".xls"}:
            continue
        by_stem.setdefault(str(path.with_suffix("")), {})[suffix] = name

    chosen: list[str] = []
    for variants in by_stem.values():
        for suffix in (".csv", ".ods", ".xls"):
            if suffix in variants:
                chosen.append(variants[suffix])
                break

    return sorted(chosen)


def _to_long(frame: pl.DataFrame, dialect: Dialect, *, source_member: str) -> pl.DataFrame:
    """Unpivot the 24 hour columns and decode every value cell."""
    hour_cols = list(dialect.hour_map)

    long = frame.select(
        pl.col(dialect.date_col).str.slice(0, 10).alias("date_raw"),
        pl.col(dialect.station_col).str.strip_chars().alias("station_name"),
        pl.col(dialect.item_col).str.strip_chars().alias("pollutant"),
        *[pl.col(c) for c in hour_cols],
    ).unpivot(
        index=["date_raw", "station_name", "pollutant"],
        on=hour_cols,
        variable_name="hour_label",
        value_name="raw",
    )

    hour_expr = pl.col("hour_label").replace_strict(
        dialect.hour_map, return_dtype=pl.Int8, default=None
    )

    return (
        long.with_columns(
            pl.col("date_raw").str.to_date("%Y/%m/%d", strict=False).alias("date"),
            hour_expr.alias("hour"),
            parse_expr("raw").alias("parsed"),
        )
        .unnest("parsed")
        .with_columns(
            (pl.col("date").cast(pl.Datetime("us")) + pl.duration(hours=pl.col("hour"))).alias(
                "ts_local"
            ),
            pl.lit(dialect.generation).alias("generation"),
            pl.lit(source_member).alias("source_member"),
        )
        .drop("date_raw", "hour_label", "date")
        .filter(pl.col("ts_local").is_not_null())
    )


def _read_by_suffix(raw: bytes, member: str) -> tuple[pl.DataFrame, str, str]:
    """Dispatch on member type, returning (frame, encoding, container)."""
    suffix = PurePosixPath(member).suffix.lower()
    if suffix == ".csv":
        frame, encoding = _read_csv_member(raw)
        return frame, encoding, "csv"
    if suffix == ".ods":
        frame, encoding = _read_ods_member(raw)
        return frame, encoding, "ods"
    if suffix == ".xls":
        frame, encoding = _read_xls_member(raw)
        return frame, encoding, "xls"
    raise ArchiveFormatError(f"unsupported member type: {member!r}")


def _parse_member_bytes(raw: bytes, member: str) -> pl.DataFrame:
    frame, encoding, container = _read_by_suffix(raw, member)
    dialect = detect_dialect(frame.columns, encoding=encoding, container=container)
    return _to_long(frame, dialect, source_member=member)


def read_member(archive: Path, member: str) -> pl.DataFrame:
    """Parse one member of an archive into the long observation format."""
    with ArchiveContainer(archive) as container:
        raw = container.read(member)
    return _parse_member_bytes(raw, member)


def _parse_member_task(args: tuple[str, str]) -> pl.DataFrame | None:
    """Worker entry point: open the archive independently and parse one member.

    Top-level (not a closure) so it can be pickled for a process pool. Zip
    members are randomly accessible, so each worker paying its own open is
    cheaper than shipping decompressed bytes across the process boundary.
    """
    archive_path, member = args
    try:
        with ArchiveContainer(Path(archive_path)) as container:
            return _parse_member_bytes(container.read(member), member)
    except (ArchiveFormatError, zipfile.BadZipFile, OSError) as exc:
        log.warning("skipping %s in %s: %s", member, Path(archive_path).name, exc)
        return None


def read_archive(
    archive: Path,
    *,
    limit: int | None = None,
    workers: int | None = None,
) -> pl.DataFrame:
    """Parse an entire annual archive into one long frame.

    ``limit`` caps the number of members read, which keeps exploratory runs and
    tests fast on multi-hundred-megabyte archives.

    ``workers`` parallelises member parsing. ODS years are dominated by XML
    decoding — hundreds of megabytes of it per year — which is CPU-bound and
    scales almost linearly. 7z archives stay single-threaded because each
    worker would have to re-extract the whole solid block.
    """
    with ArchiveContainer(archive) as container:
        kind = container.kind
        members = select_members(container.namelist())
        if limit is not None:
            members = members[:limit]

        if kind == "7z" or (workers is not None and workers <= 1) or len(members) < 4:
            frames = [
                frame
                for frame in (_safe_parse(container, member, archive.name) for member in members)
                if frame is not None
            ]
        else:
            frames = _parse_in_parallel(archive, members, workers)

    if not frames:
        raise ArchiveFormatError(f"no parseable members in {archive}")

    return pl.concat(frames, how="vertical_relaxed")


def _safe_parse(container: ArchiveContainer, member: str, archive_name: str) -> pl.DataFrame | None:
    try:
        return _parse_member_bytes(container.read(member), member)
    except (ArchiveFormatError, zipfile.BadZipFile) as exc:
        log.warning("skipping %s in %s: %s", member, archive_name, exc)
        return None


def _parse_in_parallel(
    archive: Path, members: list[str], workers: int | None
) -> list[pl.DataFrame]:
    from concurrent.futures import ProcessPoolExecutor

    max_workers = workers or max(1, (os.cpu_count() or 2) - 2)
    tasks = [(str(archive), member) for member in members]

    with ProcessPoolExecutor(max_workers=max_workers) as pool:
        return [frame for frame in pool.map(_parse_member_task, tasks) if frame is not None]


def describe_archive(archive: Path) -> dict[str, object]:
    """Cheap structural summary — used to map format generations without parsing."""
    with ArchiveContainer(archive) as zf:
        names = zf.namelist()
        chosen = select_members(names)
        suffixes: dict[str, int] = {}
        for name in names:
            suffix = PurePosixPath(name).suffix.lower() or "<none>"
            suffixes[suffix] = suffixes.get(suffix, 0) + 1

        dialect = None
        error = None
        if chosen:
            try:
                frame, encoding, container = _read_by_suffix(zf.read(chosen[0]), chosen[0])
                dialect = detect_dialect(frame.columns, encoding=encoding, container=container)
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"

    return {
        "archive": archive.name,
        "container": detect_container(archive),
        "members": len(names),
        "data_members": len(chosen),
        "suffixes": suffixes,
        "generation": dialect.generation if dialect else None,
        "encoding": dialect.encoding if dialect else None,
        "hour_base": dialect.hour_base if dialect else None,
        "columns": ([dialect.date_col, dialect.station_col, dialect.item_col] if dialect else None),
        "error": error,
    }
