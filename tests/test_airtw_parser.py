"""Parser tests for the airtw 歷年監測資料 catalogue.

The two table layouts differ in column count, so anything that indexes columns
by position silently mis-parses one of them. These fixtures are trimmed copies
of the real markup observed on 2026-07-27.
"""

from __future__ import annotations

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
