"""Parser tests for the airtw 歷年監測資料 catalogue.

The two table layouts differ in column count, so anything that indexes columns

by position silently mis-parses one of them. These fixtures are trimmed copies

of the real markup observed on 2026-07-27.

"""

from __future__ import annotations

import pathlib
import sys
import types

import pytest
from selectolax.parser import HTMLParser

from twair.ingest.airtw import (
    AirtwFile,
    _parse_table,
    _year_options,
    hourly_archives,
)


def _drive(file_id: str) -> str:

    return (
        f"<a title='[另開新頁]歷年監測資料' href='https://drive.google.com/file/d/{file_id}/view' "
        "target='_blank' rel='noopener noreferrer'>"
        "<label class='sr-only'>連結</label><i class='fas fa-download'></i></a>"
    )


# Per-year pages (2018-2025): 資料型態 | 測站型態 | 檔案下載 | 備註

YEAR_PAGE = f"""

<div class="QueryTable">

  <table class='table'>

    <thead><tr>

      <th>資料型態</th><th>測站型態</th><th>檔案下載</th><th>備註</th>

    </tr></thead>

    <tr><td>全年逐時資料</td><td>離島</td><td>{_drive("AAA111")}</td><td></td></tr>

    <tr><td>全年逐時資料</td><td>全部</td><td>{_drive("BBB222")}</td><td></td></tr>

    <tr><td>年報</td><td>全部</td><td>{_drive("CCC333")}</td><td>PDF</td></tr>

  </table>

</div>

"""

# The 「歷年」 archive page carries an extra leading 年度 column.

ARCHIVE_PAGE = f"""

<div class="QueryTable">

  <table class='table'>

    <thead><tr>

      <th>年度</th><th>資料型態</th><th>測站型態</th><th>檔案下載</th><th>備註</th>

    </tr></thead>

    <tr><td>2017</td><td>全年逐時資料</td><td>全部</td><td>{_drive("DDD444")}</td><td></td></tr>

    <tr><td>1982</td><td>全年逐時資料</td><td>全部</td><td>{_drive("EEE555")}</td><td></td></tr>

  </table>

</div>

"""


def test_year_page_uses_fallback_year() -> None:

    files = _parse_table(HTMLParser(YEAR_PAGE), fallback_year=2024)

    assert len(files) == 3

    assert {f.year for f in files} == {2024}

    assert files[0].station_group == "離島"

    assert files[0].drive_file_id == "AAA111"


def test_archive_page_reads_year_from_its_own_column() -> None:
    """The 年度 column must win over the fallback — this is the whole point."""

    files = _parse_table(HTMLParser(ARCHIVE_PAGE), fallback_year=2017)

    assert [f.year for f in files] == [2017, 1982]

    assert files[1].drive_file_id == "EEE555"


def test_extra_leading_column_does_not_shift_the_download_link() -> None:
    """Regression: indexing cells by position picks 測站型態 instead of the link."""

    year_files = _parse_table(HTMLParser(YEAR_PAGE), fallback_year=2024)

    archive_files = _parse_table(HTMLParser(ARCHIVE_PAGE), fallback_year=2017)

    assert all(f.drive_file_id for f in year_files + archive_files)

    assert all(f.station_group in {"離島", "全部"} for f in year_files + archive_files)


def test_hourly_archives_excludes_reports() -> None:

    files = _parse_table(HTMLParser(YEAR_PAGE), fallback_year=2024)

    hourly = hourly_archives(files)

    assert len(hourly) == 2

    assert all(f.data_type == "全年逐時資料" for f in hourly)


def test_rows_without_a_download_link_are_skipped() -> None:

    html = """

    <div class="QueryTable"><table>

      <thead><tr><th>資料型態</th><th>測站型態</th><th>檔案下載</th><th>備註</th></tr></thead>

      <tr><td>全年逐時資料</td><td>全部</td><td></td><td>尚未提供</td></tr>

    </table></div>

    """

    assert _parse_table(HTMLParser(html), fallback_year=2025) == []


def test_missing_query_table_returns_empty() -> None:

    assert _parse_table(HTMLParser("<div>no table here</div>"), fallback_year=2025) == []


def test_year_options_includes_the_mislabelled_archive_entry() -> None:
    """value=2017 is labelled 歷年 and expands to every year back to 1982."""

    html = """

    <select name="ctl00$CPH_Content$ddlQYear">

      <option value="2025">2025</option>

      <option value="2018">2018</option>

      <option value="2017">歷年</option>

    </select>

    """

    options = _year_options(HTMLParser(html))

    assert options["2025"] == "2025"

    assert options["2017"] == "歷年"


def test_year_options_fails_loudly_when_the_page_changes() -> None:

    with pytest.raises(RuntimeError, match="Year selector"):
        _year_options(HTMLParser("<html><body>redesigned</body></html>"))


def test_key_is_stable_and_filesystem_safe() -> None:

    item = AirtwFile(
        year=2024,
        data_type="全年逐時資料",
        station_group="雲嘉南空品區",
        drive_file_id="ZZZ999",
        url="https://drive.google.com/file/d/ZZZ999/view",
    )

    assert item.key == "airtw/2024/hourly/雲嘉南"


def test_two_data_types_from_the_same_year_do_not_share_a_file() -> None:
    """MOENV publishes three kinds of file per year behind one page.

    The destination used to be `{year}_{group}.zip` for all of them, so the

    2018 hourly archive, the 2018 QA report and the 2018 annual report all

    resolved to `2018_全部.zip`. Nothing broke only because the downloader

    filters to 全年逐時資料 — and the open backlog item asks for the QA report,

    which is precisely the change that would have made them overwrite each

    other. Worse than a one-time clobber: the ledger keys ARE distinct, so the

    next cache check for the hourly archive sees a size mismatch and downloads

    it back over the report, forever.

    """

    from twair.ingest.download import _destination

    def make(data_type: str) -> AirtwFile:

        return AirtwFile(
            year=2018,
            data_type=data_type,
            station_group="全部",
            drive_file_id="ZZZ999",
            url="https://drive.google.com/file/d/ZZZ999/view",
        )

    kinds = ["全年逐時資料", "品保查核報告", "年報"]

    paths = [_destination(make(k)) for k in kinds]

    assert len(set(paths)) == len(kinds), f"two data types share a path: {paths}"

    # The hourly spelling is load-bearing: 44 archives are cached under it and

    # renaming them would re-download 1.5 GB to change nothing.

    assert paths[0].name == "2018_全部.zip"


def test_download_one_can_actually_call_gdown(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The import line, which was wrong and silently so.

    `import gdown.download as gdown` binds the FUNCTION `gdown.download`, not
    the submodule of the same name — the package attribute wins — so the call
    became `<function>.download(...)` and every fetch raised 「'function' object
    has no attribute 'download'」. Nobody saw it: all 44 archives are cached, so
    `download_one` returns before reaching the call, and CI never downloads.
    The claim this repository rests on is that the data can be rebuilt from
    scratch, and the first step of that could not run.

    No network. The stub is installed at `gdown.download`, the SUBMODULE, which
    is what the fixed code imports from — and that distinction is the bug.
    """
    import twair.ingest.download as dl

    item = AirtwFile(
        year=2024,
        data_type="全年逐時資料",
        station_group="全部",
        drive_file_id="ZZZ999",
        url="https://drive.google.com/file/d/ZZZ999/view",
    )

    calls: list[dict[str, object]] = []

    def fake_download(*, id: str, output: str, quiet: bool = False) -> str:
        calls.append({"id": id, "output": output})
        pathlib.Path(output).write_bytes(b"PK\x03\x04" + b"0" * 64)
        return output

    monkeypatch.setitem(
        sys.modules, "gdown.download", types.SimpleNamespace(download=fake_download)
    )
    monkeypatch.setattr(dl, "raw_dir", lambda _name: tmp_path)
    monkeypatch.setattr(dl, "is_cached", lambda _key: None)
    monkeypatch.setattr(dl, "record_download", lambda **_kw: None)

    result = dl.download_one(item)

    assert result.ok, result.error
    assert len(calls) == 1
    assert calls[0]["id"] == "ZZZ999"


def test_a_report_is_a_pdf_and_is_not_thrown_away(tmp_path: pathlib.Path) -> None:
    """The archive guard rejected the very file the backlog asks for.

    It exists because Drive answers an unavailable file with an HTML
    interstitial and a 200, so "wrong format" is how a failed fetch shows up.
    But 品保查核報告 and 年報 are PDFs, and the guard only knew zip and 7z —
    measured on the real 2018 report, the download succeeded and was then
    deleted with 「not an archive (got b'%PDF-1.4…')」.
    """
    from twair.ingest.download import is_supported_archive

    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%\xc7\xec\x8f\xa2\n")

    assert is_supported_archive(pdf, "品保查核報告")
    # ...and an hourly archive is still required to be an archive, because for
    # that type a PDF means the interstitial.
    assert not is_supported_archive(pdf, "全年逐時資料")
