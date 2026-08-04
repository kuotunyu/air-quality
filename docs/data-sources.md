# 資料來源盤點

> 由 `uv run twair probe sources` 產生於 2026-08-01T21:48:16+00:00。
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
| 各年度頁（2018–2025） | 資料型態 \| 測站型態 \| 檔案下載 \| 備註 |
| 「歷年」頁 | 年度 \| 資料型態 \| 測站型態 \| 檔案下載 \| 備註 |

欄位數不同，所以解析器以**表頭文字**定位「檔案下載」欄，不用固定索引。

### 實際盤點結果

| 項目 | 值 |
|---|---|
| 檔案總數 | 111 |
| 全年逐時資料檔數 | 108 |
| 逐時資料涵蓋年份 | 1982–2025（44 年） |
| 資料型態 | 全年逐時資料, 品保查核報告, 年報 |
| 測站型態分組 | 中部空品區, 全部, 北部空品區, 宜蘭空品區, 竹苗空品區, 花東空品區, 離島, 雲嘉南空品區, 高屏空品區 |

**這是本專案最重要的發現之一**：可取得的逐時資料回溯到
1982 年，而 2018 年的原始畢業專題只使用了 2010–2017 共 8 年。

> 注意：環保署於 1993 年擴充測站網後才有接近 80 站的規模，
> 1993 年以前的資料測站極少，分析時必須考慮測站數的時變性，
> 不可直接比較跨年度的「全台平均」。

### 逐時資料以外

除「全年逐時資料」外，部分年度另提供**年報**與**品保查核報告**。
品保查核報告對 QA/QC 模組特別有價值——它記錄了官方自己認定的儀器問題，
可用來驗證我們的異常偵測邏輯。

### 樣本

`airtw_2024_離島.zip` (592,023 bytes)

## 2. 環境部 環境資料開放平臺

- 網址：<https://data.moenv.gov.tw/>
- API：`https://data.moenv.gov.tw/api/v2/{dataset_id}?format=json&api_key=…`
- 參數：`offset`、`limit`、`sort`、`format`、`year_month`（如 `2020_01`）
- 需免費註冊會員取得 API key。
- 網站本身是 Nuxt SSR 單頁應用，資料集清單為前端非同步載入，
  **無法從 HTML 直接刮取 dataset id 清單**。
- 本專案已辨識的資料集與目前用途：
  - `AQX_P_07` — 空氣品質監測站基本資料（經緯度、測站類型、空品區）
  - `aqx_p_432` — 唯讀 freshness 檢查與 API 憑證驗證；不寫回 canonical store
  - `aqx_p_488` — AQI 歷史月包候選；目前 canonical ingest 不使用
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
