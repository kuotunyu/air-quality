# 資料來源盤點

> 環境部核心盤點由 `uv run twair probe sources` 產生於 2026-08-01T21:48:16+00:00；
> GEE 段落另標 2026-08-10 實測。這裡記錄的是**實際觀察到**的行為，
> 不是只抄官方文件的宣稱。

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

### 測站地理資料的兩來源解析

測站地理資料不從年度逐時檔猜測。`conf/station_geo.yaml` 是由 `AQX_P_07`
產生的現行測站 register；`conf/station_geo_historical.yaml` 則只收錄經人工審查、
且不在現行 register 內的歷史測站紀錄。解析時以正規化後的測站名稱合併，現行
register 永遠優先，歷史資料不覆蓋同名現行紀錄。

歷史補充中的 `historical_site_id` 與 AIRTW 來源頁面的 `source_record_id` 是兩種
不同識別碼，不得互相代用。解析後的測站表以 `geo_source`、
`geo_source_record_namespace`、`geo_source_record_id` 保留實際採用的來源；無法解析
的測站仍保留列與 null 座標。

2026-08-11 讀取的現行匯出共有 82 個測站：76 個來自現行 register、1 個（萬里）來自
審查過的歷史紀錄，另有台中、崇倫、阿里山、泰山、三民 5 個名稱未解析。缺少座標
只表示目前兩個審查來源都沒有可用紀錄，不表示測站不存在、已停用或應被併到其他站。
`uv run twair stations geo` 會分開報告現行與解析後的數量及來源。

### 官方公告事件 ledger

`conf/station_publication_events.yaml` 記錄經審查的公告 URL、發布日、生效時間與來源
原文摘要；`twair qc report` 只量測生效後年度檔仍有多少列、非空值與 null，不刪除、
補值或改寫任何觀測。零列是實際量得的零；在沒有本機 QC 產物的 fresh clone 中，
網站匯出會標示 `unavailable` 與原因，絕不把缺少產物寫成零。現行實測有 23 個
測項紀錄；逐項數字與時間範圍由 [data-quality.md](data-quality.md) 的產生器表格提供。
這些紀錄描述來源之間的差異，不是資料有效性的判定。

## 3. 中央氣象署 開放資料平臺

- 網址：<https://opendata.cwa.gov.tw/>
- 需登入取得「API 授權碼」。
- Swagger：<https://opendata.cwa.gov.tw/dist/opendata-swagger.html>
- 歷史觀測回溯有限，較長歷史需搭配 CODiS（<https://codis.cwa.gov.tw/>）
  或改用 ERA5 再分析資料。

## 4. 加值資料源

| 來源 | 用途 | 取得 |
|---|---|---|
| Copernicus ERA5 | 邊界層高度、10m 風、2m 溫度／露點、地面氣壓 | ✅ 2024–2025 年逐時來源與多年度／留出測站 robustness 已完成 |
| Sentinel-5P TROPOMI | NO2/SO2/CO 柱濃度 | Google Earth Engine |
| MODIS MAIAC AOD | 1 km 氣膠光學厚度 | Google Earth Engine |
| 智慧城鄉微型感測器 | 高密度 PM2.5 | 環境部開放平臺 |

### 2026-08-11 ERA5 2025 來源取得邊界

`twair ingest era5 --year 2025 --months 1:12 --inventory-generation <sha256>
--confirm-download` 已取得並獨立重讀 12 個 Copernicus CDS NetCDF。輸出綁定 77 站
immutable inventory generation，共 8,760 小時、674,520 筆 station-hour；
六個來源變數皆為 0 個 null。原始檔合計 37,520,309 bytes，manifest 逐月保存 request、byte count 與
SHA-256。最近 ERA5 grid cell 距測站的中位數為 9.244 km，範圍 0.432–17.467 km。

為完整組成臺北時間的 2025 年，分析另取得 2024 年 12 月 744 個 UTC 小時、
57,288 筆 station-hour。其中 8 個 UTC 小時，共 616 筆轉換後落在臺北時間
2025-01-01 00:00–07:00；不把 UTC year 直接當成 local-calendar year。

為後續 robustness 另取得 2024 年 12 個 UTC 月，共 8,784 小時、676,368 筆
station-hour，並取得 2023 年 12 月 744 個 UTC 小時、57,288 筆 station-hour，
以同一個 immutable 77 站 generation 組成完整的臺北時間 2024 年。六個來源變數在
這兩個新增來源檔也都是 0 個 null；原有 2024 年 12 月 raw NetCDF 的 checksum 在合併後保持不變。

這一節是來源取得與 sampling contract。ERA5 尚未納入已發布的 M4，亦未用於
衛星校正、融合或 PM2.5 推估。

### 2026-08-11 ERA5 2025 held-out value-add 邊界

`twair analyze era5-value --generation <sha256> --full` 以測站為單位、固定 `n_jobs=1` 的
LightGBM，在三個 expanding forward local-time folds 上比較四種資訊集：
temporal-only、站內氣象、ERA5 氣象，以及兩者 combined。每個模型只使用同一批
PM2.5 target、站內氣象與 ERA5 feature 都完整的 rows；不填值、不插值、不讓模型各自挑 rows。

77 站均有 PM2.5 target 與完整 ERA5 join；ERA5 join 缺失與 ERA5 feature 不完整均為 0 筆。
但三重、淡水、陽明在 2025 年每筆 target row 都缺至少一個站內氣象 feature，
因此明確排除，沒有以 ERA5 回填。其餘 74 站共有 632,760 筆 paired rows；
662,267 筆 PM2.5 target rows 中有 29,507 筆因站內氣象 feature 不完整而不列入。

主要比較 `combined - local_weather` 在 205／222 個 station-fold 同時改善 RMSE 與 R²，
17／222 個同時變差；median RMSE delta 為 −0.758 µg/m³，median R² delta 為 +0.249。
三個 folds 的 median RMSE delta 均小於 0，其中最弱的 q3 為 −0.568 µg/m³。
`era5_weather - local_weather` 則有 171／222 個同時改善，顯示 ERA5 不是在每站每季都可取代站內觀測。

這些是單一年度、非因果的 held-out predictive-value 結果：不是趨勢因果歸因、
不是 satellite calibration、不是融合濃度場，也沒有改寫已發布的 M4。

### 2026-08-11 ERA5 2024–2025 robustness 邊界

`twair analyze era5-robustness --generation <sha256> --full` 把上述單年結果延伸成
四種 evaluation：逐年重跑相同的三個 expanding folds、同站由 2024 訓練並測試 2025、
2025 同年訓練其他測站並留出一站，以及同時留出年份與測站。四組資訊集在每個比較中
仍使用完全相同的 rows；2024 年 636,244 筆、2025 年 632,760 筆。三重、淡水、陽明
在兩年都因四組資訊集沒有共同完整 rows 而排除，沒有以 ERA5 或其他測站回填。

74 個共同測站分成 10 個 deterministic folds。73 站依官方空品區排序；萬里的
`airzone_official` 仍是 null，保留在 1 個未分類 air-zone stratum，而不是杜撰分類或
排除測站。完整 run 以 `n_jobs=1` serial 執行，產生 2,664 筆 score 與 1,998 筆
paired delta。

下表的 transfer 列以測站為單位；replication 列以測站 × 三個時間 fold 為單位。
delta 均為 `combined - local_weather`，所以 RMSE 小於 0、R² 大於 0 代表改善。

| evaluation | train → test | 同時改善 RMSE 與 R² | 同時變差 | median RMSE delta (µg/m³) | median R² delta |
|---|---|---:|---:|---:|---:|
| 同站跨年 | 2024 → 2025 | 63／74 | 11／74 | −0.308 | +0.066 |
| 同年留站 | 2025 → 2025 | 66／74 | 8／74 | −0.576 | +0.078 |
| 跨年留站 | 2024 → 2025 | 70／74 | 4／74 | −0.876 | +0.197 |
| 同年 replication | 2024 → 2024 | 177／222 | 45／222 | −0.584 | +0.195 |
| 同年 replication | 2025 → 2025 | 205／222 | 17／222 | −0.758 | +0.249 |

三種 transfer 的 combined 同時改善數依序為 63／74、66／74、70／74；兩年的
replication 為 177／222、205／222。`era5_weather - local_weather` 在三種 transfer
也有 59／74、64／74、72／74 個測站同時改善，顯示主要增量不是單純來自 combined
增加 feature 數量。反過來比較 `combined - era5_weather`，同站跨年、同年留站與跨年
留站分別只有 49／74、40／74、37／74 個測站同時改善；跨年留站正好 37／74 改善、
37／74 變差，因此不能宣稱加入站內氣象總會提升可轉移性。

這些結果支持 ERA5 在目前資料與四種 evaluation 下具有 predictive value，但不是因果
歸因、PM2.5 calibration、sensor fusion 或 M4 replacement。同年留站的 spatial transfer
使用 contemporaneous 2025 rows，不等於未來年份的預測；跨年留站才同時改變年份與測站。
ERA5 尚未納入已發布的 M4，也沒有用來改寫網站現有的氣象正規化結果。

### 2026-08-10 GEE 實測與 Stage A 邊界

Earth Engine credential 與真實查詢均已驗證。現行 `twair ingest satellite` 只取得兩個
官方 L3 column band，不把它們改名為地面濃度或排放量：

| key | collection | band | unit |
|---|---|---|---|
| `s5p_no2` | `COPERNICUS/S5P/OFFL/L3_NO2` | `tropospheric_NO2_column_number_density` | mol/m² |
| `s5p_so2` | `COPERNICUS/S5P/OFFL/L3_SO2` | `SO2_column_number_density` | mol/m² |

GEE 的 L3 ingest grid 約為 0.01°（此處取樣 scale 設為 1,113 m），但 Sentinel-5P
來源產品在 nadir 的實際解析度約為 3.5 × 7 km。grid spacing 不是彼此獨立的 1 km
衛星資訊，後續分析不得用前者宣稱 1 km 的物理解像力。

2025 全年重跑指令：

```bash
uv run --extra earth twair ingest satellite --year 2025 --months 1:12
```

實測產物位於 `data/interim/satellite/year=2025/`：1,824 個 station-month-source row、
24 個 source-month coverage row。該 generation 的 manifest 固定記錄 76 個有座標、
6 個沒有座標的測站；S5P NO₂ 有 1 個 masked/null sample，S5P SO₂ 有 362 個負值，
均照原值保留。這些是 M8 的來源輸入，不是相關性、校正或融合結論。

同年度的部分月份重跑會保留其他月份，但只有在 source contract 與測站名稱／座標
inventory hash 完全相同時才允許合併。每次取得的月份、source、時間與 Git 狀態都留在
manifest 的 `acquisition_runs`；年度輸出先寫入 staging generation，再交換正式目錄，
若上次程序在交換途中中斷，下次執行會先恢復孤立 backup，避免把部分月份當成完整年度。

MODIS MAIAC 的 metadata 與 tile 位置可查，但 2025 station-month 的同步 `getInfo`
pilot 即使限縮到台灣兩個 tile 並只取 best-quality QA 仍逾時。因此 MAIAC 改走每月
Earth Engine batch export：先寫本機 ledger，再以 deterministic description 對照遠端
task。全新 Drive folder 先以 1 個 task 建立，後續才讓帳號同時最多維持 2 個
`READY`／`RUNNING` task；程序每啟動一個 task 就保存 ID，中斷後會接續而不重複送出。
這個 bootstrap 是實測必要條件：2025 首次執行曾讓同時完成的前兩個 task 競態建立兩個
同名 folder，1 月與其餘月份因此分開；12 份 CSV 全數找回後已加入回歸測試。

匯出的 `Optical_Depth_055` 只保留 `AOD_QA` bits 8–11 等於 0 的 sample，套用 0.001
scale factor，並在 1,000 m sampling scale 對台灣 `h28v06`、`h29v06` tile 的測站位置
計算月平均。CSV 必須逐月含完整測站列；空白 AOD 保留為 null，缺列、重複列、月份錯置、
欄位漂移或非有限數值則拒絕匯入。輸入 checksum、Earth Engine task ID、source contract
與測站 inventory hash 都寫入 manifest，年度結果以完整 generation 原子交換。

2025 全年 12 個 task 已實際完成並通過匯入：76 個有座標測站形成 912 個 station-month
row，69 個 best-quality QA 後沒有值的 sample 保留為 null；沒有重複 key、非有限值或
負值。非 null AOD 的實測範圍為 0.015–0.669，中位數 0.2555。7 月有 24 個 null，明顯
高於其他月份，因此後續模型必須顯式處理 coverage，而不是先補值。原始 CSV、ledger、
Parquet 與 manifest 位於 gitignored 的 `data/interim/maiac/year=2025/`。

S5P manifest（2026-08-09T22:43:12Z）與 MAIAC result manifest
（2026-08-09T23:55:09Z）都產生於歷史地理 fallback 納入之前，因此各自正確凍結為
82 個測站中的 76 個有座標、6 個無座標。現行 canonical 地理解析已量得 77 個有座標、
5 個未解析；這不會回頭改寫或重新標示既有衛星表。若要讓第 77 站進入 S5P／MAIAC，
必須以新的 station inventory hash 建立另一個受審查的 generation，保留舊產物的
provenance。

2026-08-11 已建立共用的 immutable station inventory generation。它只以正規化測站名稱
與座標（`station_name`、`lon`、`lat`）計算完整 SHA-256；地理 provenance 仍保留在
canonical 測站表，但不是這個空間測站 inventory identity 的雜湊欄位。目前實測
generation 為
`58e00bb5ab951c9afd1a95e9e98aacdab4e90762e32904ca6d79d198efe6d788`，對應 82 個
canonical 測站、77 個有座標與 5 個未解析。新 S5P 與 MAIAC 產物只有在明確指定這個
generation 時才會寫入 `generations/<sha256>/year=2025/`，路徑、manifest 與測站計數
不一致時會在寫入前失敗；舊路徑、舊 hash 與既有檔案不變。

同日已完成 77 站 S5P 查詢與 12 個 MAIAC Earth Engine monthly batch。MAIAC ledger 的
12 筆 task 均為 `COMPLETED`，逐月 CSV checksum、task ID、source contract 與 generation
identity 通過 importer 後才形成新的 924 列 AOD 表；S5P 亦形成 77 × 12 × 2 的完整 key
grid。這些產物與下述 M8 結果都寫入 generation 專屬路徑，網站使用的 76 站 legacy
表與 manifest 沒有被覆寫或重新標示。

AOD 不是 PM2.5；這份站月來源表不是衛星推估 PM2.5 或融合結論。操作順序見
[registrations.md](registrations.md)。

### 2026-08-11 M8 provisional 關聯診斷

`twair analyze m8 --year 2025` 已以上述凍結的 76 站 legacy S5P／MAIAC
來源表與 canonical 地面 PM2.5 月表完成本機 Stage B 量測。這個指令只讀取
已通過 manifest 與 schema 驗證的本機檔案，不連線 Earth Engine。輸出位於
`data/outputs/m8_satellite/legacy/year=2025/`；manifest 記錄地面表與每個
S5P／MAIAC 來源檔的 SHA-256，避免將不同輸入的結果混在一起。

| 來源 | 來源列 | 衛星 null | 地面 coverage 不足而保留的 null | 完整 pair |
|---|---:|---:|---:|---:|
| MAIAC AOD | 912 | 69 | 1 | 842（92.3%） |
| S5P NO₂ | 912 | 1 | 1 | 910（99.8%） |
| S5P SO₂ | 912 | 0 | 1 | 911（99.9%） |

三個來源的地面列缺席數都是 0，並都覆蓋 12 個月份。MAIAC 完整 pair
覆蓋 75 站，S5P NO₂／SO₂ 各覆蓋 76 站。null 沒有被補值、插值、刪除或當成零。

| 來源 | pooled Pearson／Spearman | within-station Pearson／Spearman | within-month Pearson／Spearman |
|---|---:|---:|---:|
| MAIAC AOD | 0.456／0.475 | 0.257／0.281 | 0.555／0.613 |
| S5P NO₂ | 0.586／0.609 | 0.699／0.743 | 0.420／0.325 |
| S5P SO₂ | 0.101／0.101 | 0.038／0.044 | 0.131／0.131 |

#### 77 站 immutable generation 已完成

`twair analyze m8 --year 2025 --generation
58e00bb5ab951c9afd1a95e9e98aacdab4e90762e32904ca6d79d198efe6d788` 已以同一份
77 站 inventory 重跑 S5P、MAIAC 與 M8。萬里是相對 legacy 唯一新增的測站；共同
76 站的 2,736 個 panel row 在 satellite value、ground value 與全部觀測／withheld／
pair flags 上完全相同。

| 來源 | 來源列 | 衛星 null | 地面列缺席 | 地面 coverage 不足而保留的 null | 完整 pair |
|---|---:|---:|---:|---:|---:|
| MAIAC AOD | 924 | 69 | 2 | 1 | 851（92.1%） |
| S5P NO₂ | 924 | 1 | 2 | 1 | 919（99.5%） |
| S5P SO₂ | 924 | 0 | 2 | 1 | 920（99.6%） |

三個來源仍各覆蓋 12 個月份。萬里的 12 個月份中，每個來源有 9 個完整 pair、1 個
coverage 不足而 withheld 的地面月值，以及 2 個沒有地面列的月份；這三種狀態保持分開。
MAIAC 完整 pair 覆蓋 76 站，S5P NO₂／SO₂ 各覆蓋 77 站。

| 來源 | pooled Pearson／Spearman | within-station Pearson／Spearman | within-month Pearson／Spearman |
|---|---:|---:|---:|
| MAIAC AOD | 0.460／0.480 | 0.261／0.286 | 0.556／0.614 |
| S5P NO₂ | 0.588／0.612 | 0.700／0.744 | 0.423／0.333 |
| S5P SO₂ | 0.099／0.099 | 0.038／0.045 | 0.130／0.129 |

`pooled` 同時包含測站間的穩定差異與月份變化；`within-station` 先移除各測站
在完整 pair 中的平均值，`within-month` 則先移除各月的平均值。三者回答不同問題，
不應從單一係數挑選故事。這些數字是 2025 年的 descriptive association：不是因果、
不是校正，也不是衛星推估 PM2.5。單靠這些關聯係數沒有 BLH、RH 或 held-out evidence，
因此不報告 calibration performance、濃度 bias、排放、來源歸因或融合濃度場；下一節另行
回答同年度 held-out predictive value，沒有回頭替關聯係數增加因果或校正意義。

### 2026-08-11 M8 held-out predictive-value 診斷

`twair analyze satellite-value --generation
58e00bb5ab951c9afd1a95e9e98aacdab4e90762e32904ca6d79d198efe6d788 --year 2025`
只讀取通過 manifest、input hash、station inventory generation 與 schema 驗證的本機產物，
不連線 Earth Engine。完整分母是 924 個 station-month；其中 MAIAC null 69 筆、S5P NO₂
null 1 筆、S5P SO₂ null 0 筆、地面列缺席 2 筆、地面 coverage 不足而 withheld 2 筆、
座標缺失 0 筆。排除條件有重疊，因此共同完整資料是 851 筆共同完整站月、76 站、12 個月份，
不是把各類 null 數目直接相加後得到的差。null 沒有被補值、插值或當成零。

模型比較使用相同的共同完整 rows。baseline 只含月份週期與測站經緯度；candidate 在其上
分別加入 AOD、NO₂、SO₂ 或三者全部。held-quarter 有 4 個 fold，air-zone-aware
held-station 有 10 個 fold，兩者組合的 joint transfer 有 40 個 fold。每一種設計都讓
851 筆資料恰好進入 test set 一次；三種設計合計 2,553 個 test-row appearances，但不是
2,553 筆獨立觀測。

| candidate vs baseline | fold-evaluations | RMSE 與 R² 同時改善 | 同時變差 | median ΔRMSE（µg/m³） | median ΔMAE（µg/m³） | median ΔR² |
|---|---:|---:|---:|---:|---:|---:|
| AOD | 54 | 44／54 | 10／54 | −0.244 | −0.253 | +0.055 |
| NO₂ | 54 | 48／54 | 6／54 | −0.379 | −0.266 | +0.103 |
| SO₂ | 54 | 25／54 | 29／54 | +0.020 | +0.015 | −0.003 |
| all-satellite | 54 | 49／54 | 5／54 | −0.588 | −0.407 | +0.147 |

all-satellite 在 held-quarter、held-station、joint transfer 分別有 3／4、9／10、37／40
個 fold 同時改善 RMSE 與 R²；median ΔRMSE 分別是 −0.317、−0.375、−0.600
µg/m³，median ΔR² 分別是 +0.188、+0.050、+0.204。SO₂ 單獨使用時改善 25／54、
變差 29／54，因此不能宣稱每一種衛星變數都穩定增加預測資訊。

這 54 個 fold-evaluations 不是 54 個獨立年份或測站，也不是未來年度 transfer。
held-quarter 只在 2025 年內按季度留出；held-station 與 joint transfer 也仍使用同年度資料。
結果只支持 2025 年內的 held-out predictive value，不支持因果、衛星 PM2.5 校正、融合場、
未來年度泛化、空間解析度或 M4 replacement。校正與融合仍延後。

### 2026-08-12 M8 multi-year predictive robustness

`twair analyze satellite-robustness --generation
58e00bb5ab951c9afd1a95e9e98aacdab4e90762e32904ca6d79d198efe6d788`
只讀取同一 immutable generation 的 2024／2025 M8 panel 與測站 inventory，並以 `n_jobs=1`
完成一次 serial run。兩年完整分母各為 924 個 station-month；2024 的 MAIAC／NO₂／SO₂ null
為 72／1／7，地面 absent／withheld 為 0／0；2025 對應為 69／1／0 與 2／2，座標缺失皆為 0。
共同完整 cohort 有 76 站，兩年各是 848 / 851 common complete station-months；兩年各自的
完整測站都在共同 cohort，因此 cross-year common-station cohort 的額外排除列與排除站均為 0。
前述 76／73 筆 incomplete/null station-months 仍保留並計數在 source panels，只是不進入
complete-case model cohort；null 未被補值、插值、刪除或改成零。

每一個 candidate 都與 baseline 配對完全相同的 train/test rows。按 all/AOD/NO₂/SO₂ 順序，
同年度 quarter replication 在 2024 同時改善 RMSE 與 R² 的 fold 數為
3/4, 4/4, 3/4, 3/4，2025 為 3/4, 3/4, 2/4, 1/4；其餘 fold 同時變差。
真正的 future-year `2024_to_2025` 為 forward: improve / improve / improve / improve；
all-satellite 的配對 ΔRMSE／ΔR² 是 −0.378 µg/m³ / +0.057 R²。
`2025_to_2024` 是反向複驗，不是預測過去，結果為
reverse: improve / improve / worsen / worsen；all-satellite 為 −0.179 µg/m³ / +0.024 R²。

同時留出測站與年份的 10 個 air-zone-aware fold 中，`2024_to_2025` 四組特徵的改善數為
10/10, 10/10, 9/10, 6/10，`2025_to_2024` 為 10/10, 10/10, 9/10, 7/10；
其餘 fold 同時變差。正向與反向的每一列各恰好進入 test set 一次，反向結果不能重述成
對過去的預測。這是 predictive robustness only；not causal, calibration, fusion, satellite-estimated PM2.5，
且 not a spatial-resolution claim or an M4 replacement。

見 [registrations.md](registrations.md) 取得各項憑證。
