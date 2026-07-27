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
import re
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


def read_member(archive: Path, member: str) -> pl.DataFrame:
    """Parse one member of an archive into the long observation format."""
    with zipfile.ZipFile(archive) as zf:
        raw = zf.read(member)

    suffix = PurePosixPath(member).suffix.lower()
    if suffix == ".csv":
        frame, encoding = _read_csv_member(raw)
        container = "csv"
    elif suffix == ".ods":
        frame, encoding = _read_ods_member(raw)
        container = "ods"
    else:
        raise ArchiveFormatError(f"unsupported member type: {member!r}")

    dialect = detect_dialect(frame.columns, encoding=encoding, container=container)
    return _to_long(frame, dialect, source_member=member)


def read_archive(archive: Path, *, limit: int | None = None) -> pl.DataFrame:
    """Parse an entire annual archive into one long frame.

    ``limit`` caps the number of members read, which keeps exploratory runs and
    tests fast on multi-hundred-megabyte archives.
    """
    with zipfile.ZipFile(archive) as zf:
        members = select_members(zf.namelist())

    if limit is not None:
        members = members[:limit]

    frames: list[pl.DataFrame] = []
    for member in members:
        try:
            frames.append(read_member(archive, member))
        except (ArchiveFormatError, zipfile.BadZipFile) as exc:
            log.warning("skipping %s in %s: %s", member, archive.name, exc)

    if not frames:
        raise ArchiveFormatError(f"no parseable members in {archive}")

    return pl.concat(frames, how="vertical_relaxed")


def describe_archive(archive: Path) -> dict[str, object]:
    """Cheap structural summary — used to map format generations without parsing."""
    with zipfile.ZipFile(archive) as zf:
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
                raw = zf.read(chosen[0])
                if chosen[0].lower().endswith(".csv"):
                    frame, encoding = _read_csv_member(raw)
                    container = "csv"
                else:
                    frame, encoding = _read_ods_member(raw)
                    container = "ods"
                dialect = detect_dialect(frame.columns, encoding=encoding, container=container)
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"

    return {
        "archive": archive.name,
        "members": len(names),
        "data_members": len(chosen),
        "suffixes": suffixes,
        "generation": dialect.generation if dialect else None,
        "encoding": dialect.encoding if dialect else None,
        "hour_base": dialect.hour_base if dialect else None,
        "columns": ([dialect.date_col, dialect.station_col, dialect.item_col] if dialect else None),
        "error": error,
    }
