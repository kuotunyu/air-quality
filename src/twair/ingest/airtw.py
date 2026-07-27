"""Catalogue and download Taiwan MOENV annual hourly air-quality archives.

Source: https://airtw.moenv.gov.tw/cht/Query/His_Data.aspx (歷年監測資料)

The page is ASP.NET WebForms: the year selector ``ctl00$CPH_Content$ddlQYear``
is driven by a ``__VIEWSTATE`` postback, not a query string, so the catalogue
has to be walked year by year. Files themselves are hosted on Google Drive and
their ids change whenever MOENV re-uploads, which is why we re-resolve the
catalogue on every run instead of hard-coding links.

Two table layouts exist and must both be handled:

* per-year pages (2018-2025): 資料型態 | 測站型態 | 檔案下載 | 備註
* the "歷年" archive page:     年度 | 資料型態 | 測站型態 | 檔案下載 | 備註

Columns are located by header text, never by position.
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass
from typing import Any

from selectolax.parser import HTMLParser, Node

from twair.net import PoliteClient

log = logging.getLogger(__name__)

HIS_DATA_URL = "https://airtw.moenv.gov.tw/cht/Query/His_Data.aspx"

YEAR_SELECT = "ctl00$CPH_Content$ddlQYear"
QUERY_BUTTON = "ctl00$CPH_Content$btnQuery"
HIDDEN_FIELDS = (
    "__VIEWSTATE",
    "__VIEWSTATEGENERATOR",
    "__VIEWSTATEENCRYPTED",
    "__EVENTVALIDATION",
)

# Header labels as they appear on the page.
COL_YEAR = "年度"
COL_DATA_TYPE = "資料型態"
COL_STATION_GROUP = "測站型態"
COL_DOWNLOAD = "檔案下載"
COL_NOTE = "備註"

HOURLY_DATA_TYPE = "全年逐時資料"

_DRIVE_ID = re.compile(r"drive\.google\.com/file/d/([A-Za-z0-9_-]+)")


@dataclass(frozen=True, slots=True)
class AirtwFile:
    """One downloadable archive listed in the 歷年監測資料 table."""

    year: int
    data_type: str
    station_group: str
    drive_file_id: str
    url: str
    note: str = ""

    @property
    def key(self) -> str:
        """Stable logical name, used for the download ledger and local filename."""
        group = self.station_group.replace("空品區", "").strip() or "all"
        kind = "hourly" if self.data_type == HOURLY_DATA_TYPE else self.data_type
        return f"airtw/{self.year}/{kind}/{group}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _hidden_fields(tree: HTMLParser) -> dict[str, str]:
    fields: dict[str, str] = {}
    for node in tree.css("input[type=hidden]"):
        name = node.attributes.get("name")
        if name in HIDDEN_FIELDS:
            fields[name] = node.attributes.get("value") or ""
    return fields


def _year_options(tree: HTMLParser) -> dict[str, str]:
    """Map option value -> visible label for the year dropdown.

    Note the archive entry is labelled 「歷年」 but carries value ``2017``;
    selecting it returns a directory of every year back to 1982.
    """
    select = tree.css_first(f'select[name="{YEAR_SELECT}"]')
    if select is None:
        raise RuntimeError(f"Year selector {YEAR_SELECT!r} not found — page layout changed.")
    options: dict[str, str] = {}
    for opt in select.css("option"):
        value = opt.attributes.get("value")
        if value:
            options[value] = opt.text(strip=True)
    return options


def _cell_text(node: Node) -> str:
    return node.text(strip=True)


def _cell_link(node: Node) -> str:
    anchor = node.css_first("a[href]")
    return anchor.attributes.get("href", "") if anchor else ""


def _parse_table(tree: HTMLParser, *, fallback_year: int) -> list[AirtwFile]:
    """Parse the QueryTable, resolving columns by header label."""
    table = tree.css_first("div.QueryTable table")
    if table is None:
        log.warning("no QueryTable on page (year=%s)", fallback_year)
        return []

    rows = table.css("tr")
    if not rows:
        return []

    headers = [_cell_text(c) for c in rows[0].css("th, td")]
    index = {name: i for i, name in enumerate(headers)}
    if COL_DOWNLOAD not in index:
        log.warning("header %s missing %r — skipping", headers, COL_DOWNLOAD)
        return []

    files: list[AirtwFile] = []
    for row in rows[1:]:
        cells = row.css("td")
        if len(cells) <= index[COL_DOWNLOAD]:
            continue

        href = _cell_link(cells[index[COL_DOWNLOAD]])
        match = _DRIVE_ID.search(href)
        if not match:
            continue

        if COL_YEAR in index:
            year_text = _cell_text(cells[index[COL_YEAR]])
            year = int(year_text) if year_text.isdigit() else fallback_year
        else:
            year = fallback_year

        files.append(
            AirtwFile(
                year=year,
                data_type=_cell_text(cells[index[COL_DATA_TYPE]]) if COL_DATA_TYPE in index else "",
                station_group=(
                    _cell_text(cells[index[COL_STATION_GROUP]])
                    if COL_STATION_GROUP in index
                    else "全部"
                ),
                drive_file_id=match.group(1),
                url=href,
                note=_cell_text(cells[index[COL_NOTE]]) if COL_NOTE in index else "",
            )
        )
    return files


def fetch_catalog(client: PoliteClient, *, years: list[str] | None = None) -> list[AirtwFile]:
    """Walk every year in the dropdown and return every downloadable archive.

    ``years`` restricts the walk to specific dropdown *values* (useful in tests
    and for incremental runs); by default every option is visited.
    """
    landing = client.get_text(HIS_DATA_URL)
    tree = HTMLParser(landing)
    options = _year_options(tree)
    hidden = _hidden_fields(tree)

    targets = years if years is not None else list(options)
    log.info("airtw catalogue: %d year option(s) to walk", len(targets))

    catalog: list[AirtwFile] = []
    seen: set[str] = set()

    for value in targets:
        label = options.get(value, value)
        payload = {**hidden, YEAR_SELECT: value, QUERY_BUTTON: "查詢"}
        html = client.post(
            HIS_DATA_URL,
            data=payload,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": HIS_DATA_URL,
            },
        ).text
        page = HTMLParser(html)
        # VIEWSTATE rolls forward; reuse the freshest one for the next postback.
        hidden = _hidden_fields(page) or hidden

        found = _parse_table(page, fallback_year=int(value))
        new = [f for f in found if f.drive_file_id not in seen]
        seen.update(f.drive_file_id for f in new)
        catalog.extend(new)
        log.info("  option %-6s (%s): %d file(s), %d new", value, label, len(found), len(new))

    catalog.sort(key=lambda f: (f.year, f.data_type, f.station_group))
    return catalog


def hourly_archives(catalog: list[AirtwFile]) -> list[AirtwFile]:
    """Only the 全年逐時資料 entries — the raw hourly measurements."""
    return [f for f in catalog if f.data_type == HOURLY_DATA_TYPE]
