"""Phase 0 source probe.

Contacts every upstream provider, records what is *actually* served (not what
the documentation claims), and writes the results to ``conf/sources.yaml`` and
``docs/data-sources.md``.

Run it again whenever a download starts failing: government portals rotate
their file links, and this is the single place that resolves them.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table

from twair.config import get_settings, write_conf
from twair.ingest.airtw import AirtwFile, fetch_catalog, hourly_archives
from twair.ingest.download import is_supported_archive
from twair.net import PoliteClient
from twair.paths import DOCS_DIR, samples_dir

log = logging.getLogger(__name__)
console = Console()

CONF_HEADER = """\
Discovered data sources — GENERATED FILE, do not edit by hand.

Regenerate with:  uv run twair probe sources

Google Drive file ids rotate whenever MOENV re-uploads an archive, so this
file is a cache of the last successful resolution, not a permanent contract.
"""

# Chosen as the sample because it is the smallest station group, so the probe
# stays quick and light on MOENV's bandwidth.
SAMPLE_STATION_GROUP = "離島"
SAMPLE_YEAR = 2024


def _catalog_summary(catalog: list[AirtwFile]) -> dict[str, Any]:
    hourly = hourly_archives(catalog)
    years = sorted({f.year for f in hourly})
    return {
        "total_files": len(catalog),
        "hourly_files": len(hourly),
        "hourly_year_min": years[0] if years else None,
        "hourly_year_max": years[-1] if years else None,
        "hourly_year_count": len(years),
        "data_types": sorted({f.data_type for f in catalog}),
        "station_groups": sorted({f.station_group for f in catalog}),
    }


def _render_catalog_table(catalog: list[AirtwFile]) -> None:
    hourly = hourly_archives(catalog)
    by_year: dict[int, list[AirtwFile]] = {}
    for item in hourly:
        by_year.setdefault(item.year, []).append(item)

    table = Table(title="airtw 全年逐時資料", header_style="bold")
    table.add_column("year", justify="right")
    table.add_column("files", justify="right")
    table.add_column("station groups")
    for year in sorted(by_year, reverse=True):
        items = by_year[year]
        groups = ", ".join(sorted(i.station_group for i in items))
        table.add_row(str(year), str(len(items)), groups[:70])
    console.print(table)


def probe_airtw(client: PoliteClient) -> tuple[list[AirtwFile], dict[str, Any]]:
    """Resolve the full annual-archive catalogue."""
    console.print("[bold]Probing[/bold] airtw 歷年監測資料 …")
    catalog = fetch_catalog(client)
    summary = _catalog_summary(catalog)
    console.print(
        f"  resolved [green]{summary['total_files']}[/green] file(s); "
        f"hourly archives span [green]{summary['hourly_year_min']}–"
        f"{summary['hourly_year_max']}[/green] "
        f"({summary['hourly_year_count']} years)"
    )
    return catalog, summary


def download_sample(catalog: list[AirtwFile]) -> Path | None:
    """Fetch one small real archive so downstream parsing is written against truth."""
    candidates = [
        f
        for f in hourly_archives(catalog)
        if f.year == SAMPLE_YEAR and f.station_group == SAMPLE_STATION_GROUP
    ]
    if not candidates:
        candidates = sorted(hourly_archives(catalog), key=lambda f: -f.year)[:1]
    if not candidates:
        console.print("[yellow]No hourly archive available to sample.[/yellow]")
        return None

    target = candidates[0]
    dest = samples_dir() / f"airtw_{target.year}_{target.station_group}.zip"
    # `st_size > 0` was the whole cache check, and an interrupted download leaves
    # a file that passes it. Then the sample is poisoned permanently: the probe
    # reports its byte count in the published doc and never fetches it again.
    if dest.exists() and is_supported_archive(dest, target.data_type):
        console.print(f"  sample already present: {dest.name} ({dest.stat().st_size:,} bytes)")
        return dest

    # The same spelling `download_one` uses, and for the same reason. This read
    # `import gdown.download as gdown`: gdown ships a submodule `gdown/download.py`
    # and re-exports the function as `gdown.download`, and the package attribute
    # wins — so the name was bound to the FUNCTION and the call below raised
    # 「'function' object has no attribute 'download'」. Every time, caught by the
    # except clause, printed as a download failure.
    #
    # Which is why `docs/data-sources.md` publishes 「_not captured_」 under
    # 「### 樣本」 while data/raw/_samples/ holds the 592 KB 離島 2024 archive it
    # says was not captured. The bug was found and fixed in download.py and
    # missed here — the argument for the project having one spelling of it.
    from gdown.download import download as gdown_download

    console.print(f"  downloading sample: {target.year} {target.station_group} …")
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        # `fuzzy` is only meaningful alongside `url`; passing an explicit `id`
        # already bypasses link parsing.
        gdown_download(id=target.drive_file_id, output=str(dest), quiet=False)
    except Exception as exc:
        console.print(f"[yellow]  sample download failed: {exc}[/yellow]")
        return None

    # Google Drive answers an unavailable file with an HTML interstitial and a
    # 200, so a file that exists is not yet a file that arrived.
    if not dest.exists() or not is_supported_archive(dest, target.data_type):
        head = dest.read_bytes()[:32] if dest.exists() else b""
        dest.unlink(missing_ok=True)
        console.print(f"[yellow]  sample is not an archive (got {head!r})[/yellow]")
        return None
    return dest


def probe_credentialed_sources() -> dict[str, Any]:
    """Report which keyed sources can be reached, without failing when keys are absent."""
    settings = get_settings()
    status: dict[str, Any] = {}
    for name, key, note in (
        ("moenv_api", settings.moenv_api_key, "https://data.moenv.gov.tw"),
        ("cwa_opendata", settings.cwa_api_key, "https://opendata.cwa.gov.tw"),
        ("era5_cds", settings.cdsapi_key, "https://cds.climate.copernicus.eu"),
        ("earthengine", settings.gee_project_id, "https://code.earthengine.google.com"),
    ):
        status[name] = {
            "credential_present": bool(key),
            "register_at": note,
            "probed": False,
        }
    return status


def write_sources_conf(
    catalog: list[AirtwFile],
    summary: dict[str, Any],
    credentials: dict[str, Any],
) -> None:
    payload: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "airtw": {
            "his_data_url": "https://airtw.moenv.gov.tw/cht/Query/His_Data.aspx",
            "access": "ASP.NET WebForms postback on ctl00$CPH_Content$ddlQYear",
            "license": "政府資料開放授權條款第1版",
            "summary": summary,
            "files": [f.to_dict() for f in catalog],
        },
        "credentialed": credentials,
    }
    write_conf("sources", payload, header=CONF_HEADER)
    console.print("  wrote [bold]conf/sources.yaml[/bold]")


def write_sources_doc(summary: dict[str, Any], sample: Path | None) -> None:
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    doc = DOCS_DIR / "data-sources.md"
    sample_line = (
        f"`{sample.name}` ({sample.stat().st_size:,} bytes)" if sample else "_not captured_"
    )
    doc.write_text(
        f"""# 資料來源盤點

> 由 `uv run twair probe sources` 產生於 {datetime.now(UTC).isoformat(timespec="seconds")}。
> 記錄的是**實際觀察到**的行為，不是官方文件的宣稱。

## 1. 環境部 空氣品質監測網 — 歷年監測資料

- 網址：<https://airtw.moenv.gov.tw/cht/Query/His_Data.aspx>
- 技術形式：ASP.NET WebForms。年份下拉 `ctl00$CPH_Content$ddlQYear`
  透過 `__VIEWSTATE` postback 切換，**沒有 query string 介面**，
  必須帶 cookie 逐年 POST。
- 送出按鈕：`ctl00$CPH_Content$btnQuery`
- 檔案託管於 Google Drive；**file id 會隨環境部重新上傳而變動**，
  因此本專案每次執行都重新解析，不寫死連結。

### 年份下拉的陷阱

下拉選項 2025…2018 各自對應該年度，但 **value=2017 的選項標籤是「歷年」**，
選取後回傳的不是 2017 單一年，而是 **1982–2017 的完整目錄**。

### 兩種表格版面

| 頁面 | 欄位 |
|---|---|
| 各年度頁（2018–2025） | 資料型態 \\| 測站型態 \\| 檔案下載 \\| 備註 |
| 「歷年」頁 | 年度 \\| 資料型態 \\| 測站型態 \\| 檔案下載 \\| 備註 |

欄位數不同，所以解析器以**表頭文字**定位「檔案下載」欄，不用固定索引。

### 實際盤點結果

| 項目 | 值 |
|---|---|
| 檔案總數 | {summary["total_files"]} |
| 全年逐時資料檔數 | {summary["hourly_files"]} |
| 逐時資料涵蓋年份 | {summary["hourly_year_min"]}–{summary["hourly_year_max"]}（{summary["hourly_year_count"]} 年） |
| 資料型態 | {", ".join(summary["data_types"])} |
| 測站型態分組 | {", ".join(summary["station_groups"])} |

**這是本專案最重要的發現之一**：可取得的逐時資料回溯到
{summary["hourly_year_min"]} 年，而 2018 年的原始畢業專題只使用了 2010–2017 共 8 年。

> 注意：環保署於 1993 年擴充測站網後才有接近 80 站的規模，
> 1993 年以前的資料測站極少，分析時必須考慮測站數的時變性，
> 不可直接比較跨年度的「全台平均」。

### 逐時資料以外

除「全年逐時資料」外，部分年度另提供**年報**與**品保查核報告**。
品保查核報告對 QA/QC 模組特別有價值——它記錄了官方自己認定的儀器問題，
可用來驗證我們的異常偵測邏輯。

### 樣本

{sample_line}

## 2. 環境部 環境資料開放平臺

- 網址：<https://data.moenv.gov.tw/>
- API：`https://data.moenv.gov.tw/api/v2/{{dataset_id}}?format=json&api_key=…`
- 參數：`offset`、`limit`、`sort`、`format`、`year_month`（如 `2020_01`）
- 需免費註冊會員取得 API key。
- 網站本身是 Nuxt SSR 單頁應用，資料集清單為前端非同步載入，
  **無法從 HTML 直接刮取 dataset id 清單**。
- 本專案實際需要的資料集：
  - `AQX_P_07` — 空氣品質監測站基本資料（經緯度、測站類型、空品區）
  - `aqx_p_432` — 即時 AQI（每日增量更新用）
  - `aqx_p_488` — AQI 歷史月包
- 歷史逐時原始值一律走 airtw 年度包，不用 API 分頁（快得多且完整）。

## 3. 中央氣象署 開放資料平臺

- 網址：<https://opendata.cwa.gov.tw/>
- 需登入取得「API 授權碼」。
- Swagger：<https://opendata.cwa.gov.tw/dist/opendata-swagger.html>
- 歷史觀測回溯有限，較長歷史需搭配 CODiS（<https://codis.cwa.gov.tw/>）
  或改用 ERA5 再分析資料。

## 4. 加值資料源

| 來源 | 用途 | 取得 |
|---|---|---|
| Copernicus ERA5 | 邊界層高度、風場、氣壓 | `cdsapi`，需免費帳號 |
| Sentinel-5P TROPOMI | NO2/SO2/CO 柱濃度 | Google Earth Engine |
| MODIS MAIAC AOD | 1 km 氣膠光學厚度 | Google Earth Engine |
| 智慧城鄉微型感測器 | 高密度 PM2.5 | 環境部開放平臺 |

見 [registrations.md](registrations.md) 取得各項憑證。
""",
        encoding="utf-8",
    )
    console.print("  wrote [bold]docs/data-sources.md[/bold]")


def run_probe(*, download_samples: bool = True) -> None:
    """Entry point for ``twair probe sources``."""
    with PoliteClient() as client:
        catalog, summary = probe_airtw(client)

    _render_catalog_table(catalog)

    sample = download_sample(catalog) if download_samples else None
    credentials = probe_credentialed_sources()

    write_sources_conf(catalog, summary, credentials)
    write_sources_doc(summary, sample)

    missing = [name for name, info in credentials.items() if not info["credential_present"]]
    if missing:
        console.print(
            f"\n[yellow]Credentialed sources not yet configured:[/yellow] {', '.join(missing)}"
            "\nSee docs/registrations.md — airtw archives need no key and are already usable."
        )
