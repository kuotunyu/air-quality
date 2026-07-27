"""Tests for the multi-generation archive reader.

Fixtures are built in memory to mirror each real generation observed in
docs/archive-formats.md, so a regression in dialect detection fails here rather
than silently shifting a decade of data.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import polars as pl
import pytest

from twair.ingest.archive import (
    ArchiveFormatError,
    detect_dialect,
    read_archive,
    read_member,
    select_members,
)

# --- fixture builders -------------------------------------------------------

MODERN_CSV = (
    "﻿測站,日期,測項,00,01,02,03,04,05,06,07,08,09,10,11,"
    "12,13,14,15,16,17,18,19,20,21,22,23,\n"
    "金門,2024/01/01 00:00:00,PM2.5,37,43,51,58,54,54,63,73,72,64,63,60,"
    "59,60,53,52,47,44,42,42,48,48,53,52,\n"
    "金門,2024/01/01 00:00:00,WD_HR,#,34,38,44,48,58,57,46,32,42,31,31,"
    "35,44,44,37,60,68,82,83,72,62,30,31,\n"
)

# Big5 era: 日期 first, hours labelled 1-24, flags suffixed, leading-dot decimals.
LEGACY_CSV = (
    "日期,測站,測項,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,23,24\n"
    "2010/01/01,二林,CO,.4,.35,.33,.33,.33,.4,.5,.6,.7,.8,.9,1.0,"
    "1.1,1.2,1.3,1.4,1.5,1.6,1.7,1.8,1.9,2.0,2.1,2.2\n"
    "2010/01/01,二林,PM2.5,15#,20,25,30,35,40,45,50,55,60,65,70,"
    "75,80,85,90,95,100,105,110,115,120,125,130\n"
)


def _ods_bytes(rows: list[list[str]]) -> bytes:
    """Minimal ODS document, including a repeated-column cell."""
    import io

    ns = (
        'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
        'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"'
    )
    body = []
    for row in rows:
        cells = "".join(
            f"<table:table-cell><text:p>{value}</text:p></table:table-cell>" for value in row
        )
        body.append(f"<table:table-row>{cells}</table:table-row>")
    content = (
        f'<?xml version="1.0"?><office:document-content {ns}>'
        f"<office:body><office:spreadsheet><table:table>"
        f"{''.join(body)}"
        f"</table:table></office:spreadsheet></office:body></office:document-content>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("content.xml", content)
    return buffer.getvalue()


def _hours(base: int) -> list[str]:
    return [f"{h:02d}" for h in range(24)] if base == 0 else [str(h) for h in range(1, 25)]


@pytest.fixture
def modern_archive(tmp_path: Path) -> Path:
    path = tmp_path / "2024_全部.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("金門_2024.csv", MODERN_CSV.encode("utf-8"))
    return path


@pytest.fixture
def legacy_archive(tmp_path: Path) -> Path:
    path = tmp_path / "2010_全部.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("99年 中部空品區/99年二林站_20110329.csv", LEGACY_CSV.encode("cp950"))
    return path


@pytest.fixture
def ods_archive(tmp_path: Path) -> Path:
    header = ["日期", "測站", "測項", *_hours(0)]
    row = ["1994/01/01", "二林", "NO2", *[f"{10 + i}" for i in range(23)], "15#"]
    path = tmp_path / "1994_全部.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("083年 中部空品區/83年二林站.ods", _ods_bytes([header, row]))
        zf.writestr("083年 中部空品區/83年二林站.xls", b"\xd0\xcf\x11\xe0not-really-xls")
        zf.writestr("083年 中部空品區/ReadMe_普通測站.odt", b"docs")
    return path


# --- dialect detection ------------------------------------------------------


class TestDialectDetection:
    def test_zero_based_hour_labels(self) -> None:
        dialect = detect_dialect(
            ["測站", "日期", "測項", *_hours(0)], encoding="utf-8-sig", container="csv"
        )

        assert dialect.hour_base == 0
        assert dialect.hour_map["00"] == 0
        assert dialect.hour_map["23"] == 23

    def test_one_based_hour_labels_shift_back_by_one(self) -> None:
        """Official ReadMe: 「0時：指 0:00-0:59」, so column `1` is midnight."""
        dialect = detect_dialect(
            ["日期", "測站", "測項", *_hours(1)], encoding="cp950", container="csv"
        )

        assert dialect.hour_base == 1
        assert dialect.hour_map["1"] == 0, "column '1' must map to hour 0, not hour 1"
        assert dialect.hour_map["24"] == 23

    def test_column_order_is_resolved_by_label_not_position(self) -> None:
        modern = detect_dialect(
            ["測站", "日期", "測項", *_hours(0)], encoding="utf-8", container="csv"
        )
        legacy = detect_dialect(
            ["日期", "測站", "測項", *_hours(1)], encoding="cp950", container="csv"
        )

        assert modern.date_col == legacy.date_col == "日期"
        assert modern.station_col == legacy.station_col == "測站"

    def test_generation_naming(self) -> None:
        assert (
            detect_dialect(
                ["日期", "測站", "測項", *_hours(0)], encoding="xml", container="ods"
            ).generation
            == "legacy_ods"
        )
        assert (
            detect_dialect(
                ["日期", "測站", "測項", *_hours(1)], encoding="cp950", container="csv"
            ).generation
            == "legacy_csv_big5"
        )
        assert (
            detect_dialect(
                ["測站", "日期", "測項", *_hours(0)], encoding="utf-8-sig", container="csv"
            ).generation
            == "modern_csv_utf8"
        )

    def test_unknown_hour_labelling_is_rejected_loudly(self) -> None:
        with pytest.raises(ArchiveFormatError, match="hour labels"):
            detect_dialect(
                ["日期", "測站", "測項", *[str(h) for h in range(2, 26)]],
                encoding="utf-8",
                container="csv",
            )

    def test_missing_identifier_column_is_rejected(self) -> None:
        with pytest.raises(ArchiveFormatError, match="station"):
            detect_dialect(["日期", "測項", *_hours(0)], encoding="utf-8", container="csv")


# --- member selection -------------------------------------------------------


class TestMemberSelection:
    def test_ods_preferred_over_its_xls_twin(self, ods_archive: Path) -> None:
        with zipfile.ZipFile(ods_archive) as zf:
            chosen = select_members(zf.namelist())

        assert len(chosen) == 1
        assert chosen[0].endswith(".ods")

    def test_documentation_members_are_not_data(self, ods_archive: Path) -> None:
        with zipfile.ZipFile(ods_archive) as zf:
            chosen = select_members(zf.namelist())

        assert not any(name.endswith((".odt", ".doc")) for name in chosen)


# --- end-to-end parsing -----------------------------------------------------


class TestModernGeneration:
    def test_parses_and_timestamps_correctly(self, modern_archive: Path) -> None:
        frame = read_archive(modern_archive)

        assert frame["generation"].unique().to_list() == ["modern_csv_utf8"]
        assert frame["ts_local"].min().hour == 0
        assert frame["ts_local"].max().hour == 23

    def test_trailing_empty_column_is_ignored(self, modern_archive: Path) -> None:
        """The 2024 CSVs end every line with a comma, creating a 28th column."""
        frame = read_archive(modern_archive)

        assert frame.filter(pl.col("pollutant") == "PM2.5").height == 24

    def test_replacement_flag_discards_the_value(self, modern_archive: Path) -> None:
        frame = read_archive(modern_archive)
        midnight = frame.filter((pl.col("pollutant") == "WD_HR") & (pl.col("hour") == 0))

        assert midnight["value"].to_list() == [None]
        assert midnight["flag"].to_list() == ["instrument_check_invalid"]
        assert midnight["value_retained"].to_list() == [False]


class TestLegacyBig5Generation:
    def test_big5_is_decoded(self, legacy_archive: Path) -> None:
        frame = read_archive(legacy_archive)

        assert frame["station_name"].unique().to_list() == ["二林"]

    def test_hour_one_column_lands_at_midnight(self, legacy_archive: Path) -> None:
        """Regression guard for the 1-24 labelling: an off-by-one shifts a decade."""
        frame = read_archive(legacy_archive)
        first = frame.filter((pl.col("pollutant") == "CO") & (pl.col("hour") == 0))

        assert first["ts_local"].to_list()[0].hour == 0
        assert first["value"].to_list() == [pytest.approx(0.4)], "`.4` in column `1`"

    def test_suffix_flag_retains_the_value(self, legacy_archive: Path) -> None:
        frame = read_archive(legacy_archive)
        first = frame.filter((pl.col("pollutant") == "PM2.5") & (pl.col("hour") == 0))

        assert first["value"].to_list() == [pytest.approx(15.0)]
        assert first["flag"].to_list() == ["instrument_check_invalid"]
        assert first["value_retained"].to_list() == [True]


class TestOdsGeneration:
    def test_parses_spreadsheet_members(self, ods_archive: Path) -> None:
        frame = read_archive(ods_archive)

        assert frame["generation"].unique().to_list() == ["legacy_ods"]
        assert frame.filter(pl.col("pollutant") == "NO2").height == 24

    def test_broken_member_is_skipped_not_fatal(self, tmp_path: Path) -> None:
        path = tmp_path / "mixed.zip"
        header = ["日期", "測站", "測項", *_hours(0)]
        row = ["1994/01/01", "二林", "NO2", *[str(i) for i in range(24)]]
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("good.ods", _ods_bytes([header, row]))
            zf.writestr("broken.csv", b"not,a,valid,header\n1,2,3,4\n")

        frame = read_archive(path)

        assert frame.height == 24


def test_archive_with_nothing_parseable_raises(tmp_path: Path) -> None:
    path = tmp_path / "empty.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("readme.txt", b"nothing here")

    with pytest.raises(ArchiveFormatError, match="no parseable members"):
        read_archive(path)


def test_read_member_rejects_unsupported_types(ods_archive: Path) -> None:
    with pytest.raises(ArchiveFormatError, match="unsupported member type"):
        read_member(ods_archive, "083年 中部空品區/83年二林站.xls")
