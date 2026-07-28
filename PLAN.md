# AirLens Taiwan — 台灣空氣品質再分析平台

> 2018 年畢業專題《台灣地區之 PM2.5 之影響分析》的終極重製版
> 資料工程 × 現代統計/ML × 互動視覺化 × 開源發布

---

**這份文件是規劃，不是進度。** 目前做到哪裡請看 **[PROGRESS.md](PROGRESS.md)**。

執行過程中發現的事實若與本規劃牴觸，以 `docs/` 下的實測文件為準
（特別是 [docs/archive-formats.md](docs/archive-formats.md)），
並在 PROGRESS.md 的「已更正的說法」一節記錄差異。

---

## Context

### 為什麼做這件事

2018 年的畢業專題（143 頁，指導教授[2018 project supervisor]，[2018 project author]、[2018 project author]合撰）用環保署 2010–2017 年
全年逐時資料，涵蓋 8 個空品區約 78 個測站，做了：盒型圖 → 散佈圖 → 敘述統計 →
K-S 常態檢定 → Pearson/偏相關 → OLS(VIF) → 殘差分析 → **Mixed Model with AR(1)** →
分區配適 → **ARIMA 轉換函數模型**。工具是 SAS Enterprise Guide + SPSS + R（全 GUI）。

方法在當時的大學部水準是合格的，但以今天的標準有**七個結構性缺陷**，
其中兩個足以推翻主要結論。終極版的價值不只是「用比較新的工具重做」，
而是**逐條指出當年錯在哪、用同一份資料證明修正後結論不同**——
這個「方法學對照」本身就是最有記憶點的內容。

### 原專題缺陷清單（終極版必須逐條回應）

| # | 缺陷 | 嚴重度 | 原專題出處 | 終極版對策 |
|---|---|---|---|---|
| D1 | **逐時資料聚合成月平均** | 🔴 致命 | 第三章「將逐時資料合併為各測站之月資料」，N=7,286 | 全程使用逐時原始值（~1 億筆），月均只當作對照組 |
| D2 | **用 PM10 預測 PM2.5** | 🔴 致命 | 第五章解釋變數含 PM10，β=0.4133, t=92.75 | PM2.5 是 PM10 的物理子集，定義上的資訊洩漏。移出特徵集；改把 PM2.5/PM10 ratio 當作**來源指紋**分析 |
| D3 | **風向 WD_HR 當線性連續變數** | 🔴 嚴重 | 全文，含 p.29 散佈圖、各章迴歸係數 | 0° 與 359° 物理相鄰卻數值相距 359。改用 sin/cos 分解 + 風速風向合成 u/v 分量 |
| D4 | **常態檢定結論倒果為因** | 🟠 | p.37「將拒絕域的標準降低至 0.01，使每一個變數都不拒絕虛無假設，因此符合常態性」 | 不做這種檢定。改用穩健統計、bootstrap CI、分位數迴歸 |
| D5 | **共線性用逐步刪除硬處理** | 🟠 | 第五章第三節，NO/NO2/NOx 反覆進出模型 8 次 | NO + NO2 ≡ NOx 是恆等式，本來就不該同時進模型。改用 Elastic Net / 樹模型 + SHAP |
| D6 | **零樣本外驗證** | 🟠 | 全文只有 in-sample AIC/BIC | rolling-origin CV + leave-one-station-out + leave-one-year-out，一律報告 baseline 對照 |
| D7 | **78 個測站的空間維度沒用** | 🟠 | 僅「分 8 區各跑一次」 | Moran's I / LISA、測站時序分群、Kriging 濃度場、人口加權暴露 |
| D8 | **相關 ≠ 因果，無氣象正規化** | 🟡 | 全文 | Grange et al. (2018) random-forest 氣象正規化 + 事件研究/合成控制 |
| D9 | **GUI 工具不可重現** | 🟡 | SAS EG / SPSS | 全 Python，uv 鎖定版本，CI 可重跑 |
| D10 | **放棄 SARIMA** | 🟡 | p.137「SARIMA 不在本專題繼續討論…在此實屬不便」 | 補回 SARIMA，並加上現代基準（LightGBM / N-HiTS / GNN） |
| D11 | **缺漏值以鄰近測站代替，無記錄** | 🟡 | 第三章 | 完整 QA/QC 模組，保留原始 flag，缺漏處理策略可切換且有敏感度分析 |

### 目標產出（四位一體）

1. **開源資料集** — HuggingFace Datasets，台灣 2006→今 全測站逐時空品 + 氣象，含 QA flag
2. **可引用研究** — Quarto 產出的技術報告，方法完整可重現
3. **科普互動網站** — GitHub Pages，scrollytelling + 瀏覽器端資料探索器
4. **可用工具** — `twair` Python 套件（PyPI）+ HuggingFace Space 預測 demo

---

## 專案識別

- Repo: `taiwan-air-quality`
- Python 套件: `twair`
- 網站名: **AirLens Taiwan｜台灣空氣品質再分析**
- HF Dataset: `<user>/taiwan-air-quality`
- HF Space: `<user>/airlens-forecast`
- 授權: 程式碼 MIT；資料衍生物 CC BY 4.0（依政府資料開放授權條款第 1 版，須註明出處）

---

## 技術棧（已定案）

| 層 | 選擇 | 理由 |
|---|---|---|
| 套件管理 | **uv** + `pyproject.toml` | 快、鎖定版本、CI 友善 |
| 資料處理 | **Polars** 主力 + **Pandas** 相容層 | 逐時 1 億筆，Pandas 單獨扛不住 |
| 查詢/儲存 | **DuckDB** + **Parquet(zstd)**，Hive 分區 | 免資料庫伺服器；同一份 Parquet 給 Python 與瀏覽器共用 |
| Schema 驗證 | **Pandera**（Polars backend） | 每個 pipeline 階段強制 schema |
| 抓取 | **httpx** + **tenacity** + **gdown** | async + 重試 + Google Drive |
| 統計 | statsmodels, scipy, **pingouin** | 混合模型、SARIMAX、穩健統計 |
| ML | scikit-learn, **LightGBM**, **SHAP** | 主力 + 可解釋性 |
| 時序 | **statsforecast**（SARIMA/ETS）、**neuralforecast**（N-HiTS/PatchTST） | 快速 backtest |
| 空間 | geopandas, **libpysal/esda**, **pykrige**, shapely | Moran's I、Kriging |
| 地科 | **xarray** + **cdsapi**(ERA5) + **earthengine-api**(S5P/MODIS) | 再分析與衛星 |
| 圖 | **PyTorch Geometric**（僅 M9 GNN） | 測站關係圖 |
| 報告 | **Quarto** | 一份原始檔輸出 HTML/PDF |
| 前端 | **Astro** + TypeScript + **Tailwind** | 靜態產出、島嶼式互動、部署到 Pages 零成本 |
| 圖表 | **ECharts**（統計圖）+ **MapLibre GL JS**（地圖）+ **deck.gl**（軌跡/大量點） | |
| 瀏覽器端查詢 | **DuckDB-WASM** | 直接查 Parquet，無後端也能做 ad-hoc 分析 |
| Space | **Gradio** | 預測模型 demo |
| 品質 | pytest, ruff, mypy, pre-commit | |
| CI | GitHub Actions | 每日增量更新 + 部署 |

---

## Repo 結構

```
taiwan-air-quality/
├─ README.md                    # 中文主 README（含 hero 圖 + 一句話定位）
├─ README.en.md
├─ PLAN.md                      # 本計畫（Phase 0 從此檔複製進來）
├─ LICENSE (MIT) / LICENSE-DATA (CC BY 4.0)
├─ pyproject.toml / uv.lock
├─ justfile                     # just ingest / just qc / just analyze / just web
├─ conf/
│  ├─ sources.yaml              # 所有資料源的 URL、dataset id、參數
│  ├─ stations.yaml             # 測站白名單、別名對照（改名/搬遷歷史）
│  ├─ pollutants.yaml           # 測項單位、合理值域、偵測極限
│  ├─ events.yaml               # 政策/事件時間軸（因果分析用）
│  └─ qc.yaml                   # QA/QC 規則參數
├─ src/twair/
│  ├─ ingest/
│  │  ├─ moenv_api.py           # data.moenv.gov.tw REST API
│  │  ├─ airtw_yearly.py        # airtw 年度逐時包（Google Drive）
│  │  ├─ cwa.py                 # 中央氣象署 API + CODiS 歷史觀測
│  │  ├─ era5.py                # Copernicus CDS
│  │  ├─ gee.py                 # Sentinel-5P / MODIS AOD
│  │  ├─ microsensors.py        # 智慧城鄉空品微型感測器
│  │  └─ registry.py            # 來源註冊 + 快取 + checksum
│  ├─ qc/
│  │  ├─ flags.py               # 解析 #, *, x, A, NR, ND, - 等原始標記
│  │  ├─ range.py               # 值域/物理一致性檢查
│  │  ├─ outliers.py            # 尖峰 vs 真實事件的區辨
│  │  ├─ gapfill.py             # 多策略缺漏填補（可切換 + 記錄）
│  │  └─ report.py              # 產出資料品質報告
│  ├─ store/
│  │  ├─ schema.py              # Pandera schema 定義
│  │  ├─ writer.py              # Parquet Hive 分區寫入
│  │  ├─ catalog.py             # DuckDB view 註冊
│  │  └─ hf.py                  # 推送 HuggingFace Datasets
│  ├─ features/
│  │  ├─ met.py                 # 風向 sin/cos、u/v、露點、通風係數
│  │  ├─ temporal.py            # 循環時間編碼、假日、農曆節慶（燒金/鞭炮）
│  │  └─ chem.py                # PM2.5/PM10 ratio、SO2/NOx、Ox=O3+NO2
│  ├─ analysis/
│  │  ├─ replication.py         # M1 復刻 2018 專題
│  │  ├─ drivers.py             # M2 逐時驅動因子
│  │  ├─ pitfalls.py            # M3 方法學對照（錯 vs 對）
│  │  ├─ deweather.py           # M4 氣象正規化
│  │  ├─ causal.py              # M5 政策因果
│  │  ├─ spatial.py             # M6 空間
│  │  ├─ trajectory.py          # M7 境外傳輸
│  │  ├─ satellite.py           # M8 衛星融合
│  │  └─ health.py              # M10 健康衝擊
│  ├─ models/
│  │  ├─ baselines.py           # persistence / climatology / SARIMA
│  │  ├─ gbdt.py / dl.py / gnn.py
│  │  └─ evaluate.py            # 統一 backtest 框架
│  ├─ viz/
│  │  └─ export.py              # 產出網站用的 json / parquet（分層）
│  └─ cli.py                    # typer CLI: twair ingest / qc / run ...
├─ notebooks/                   # 探索用，不進 CI
├─ reports/                     # Quarto (.qmd) → docs/report/
├─ web/                         # Astro 專案 → docs/
├─ spaces/forecast/             # HF Gradio Space（獨立 requirements）
├─ tests/
├─ data/                        # gitignored
│  ├─ raw/  interim/  processed/  outputs/
└─ .github/workflows/
   ├─ ci.yml                    # lint + type + test
   ├─ daily-update.yml          # 每日增量抓取 → 更新 HF dataset
   └─ deploy-web.yml            # 建置 Astro → GitHub Pages
```

---

## 資料源（已查證，2026-07 現況）

> ⚠️ 環保署已於 2023/08 改制為**環境部**，原專題引用的 `taqm.epa.gov.tw` 已失效。

### 主要（必要）

| 來源 | 位址 | 內容 | 取得方式 | 備註 |
|---|---|---|---|---|
| **環境部 空品監測網 年度逐時包** | `airtw.moenv.gov.tw/cht/Query/His_Data.aspx` | 全年逐時原始值，2018–2025 + 「歷年」，可依空品區或全部測站下載 | 頁面刮取 Google Drive file id → `gdown` | **主力歷史資料源**。連結會變動，需每次解析頁面 |
| **環境部 資料開放平臺 API** | `data.moenv.gov.tw/api/v2/{id}` | `AQX_P_07` 測站基本資料（經緯度/測站類型/空品區）、`aqx_p_432` 即時 AQI、`aqx_p_488` AQI 歷史月包（已到 2026/06，117 檔） | REST，需免費 API key，支援 `year_month`, `offset`, `limit`, `format`, `sort` | 用於**每日增量更新**與 metadata |
| **中央氣象署 開放資料** | `opendata.cwa.gov.tw` | 自動氣象站逐時觀測（氣壓、日射、能見度等原專題缺的變數） | REST，需免費授權碼 | Swagger: `/dist/opendata-swagger.html` |
| **CWA CODiS** | `codis.cwa.gov.tw` | 歷史逐時氣象觀測（可回溯數十年） | 頁面查詢刮取 | 補 opendata 只有近期的缺口；**須遵守 robots.txt 與 rate limit** |

### 加值（進階模組用）

| 來源 | 內容 | 取得 |
|---|---|---|
| **ERA5**（Copernicus CDS） | **邊界層高度 BLH**、10m 風、2m 溫、地面氣壓、降水 | `cdsapi`，免費帳號 |
| **Sentinel-5P TROPOMI** | NO2 / SO2 / CO / UV Aerosol Index 柱濃度 | Google Earth Engine `COPERNICUS/S5P/OFFL/L3_*` |
| **MODIS MAIAC AOD** | 1 km 氣膠光學厚度 | GEE `MODIS/061/MCD19A2_GRANULES` |
| **智慧城鄉空品微型感測器** | 數千個低成本 PM2.5 感測器 | 環境部開放平臺 / 民生公共物聯網 |
| **NOAA HYSPLIT + GDAS** | 後推軌跡 | ARL 氣象檔 + HYSPLIT（容器化）或 ERA5 風場自建 Lagrangian 積分器 |
| **內政部 人口統計網格** | 人口加權暴露 | 政府資料開放平臺 |

> **BLH（邊界層高度）是原專題最關鍵的缺失變數。** 污染物濃度 ≈ 排放量 / 混合層體積。
> 沒有 BLH，任何「氣象因子對 PM2.5 影響」的討論都缺一塊。這是終極版最有力的加分項之一。

---

## 執行階段

> 每個 Phase 結束都必須有**可單獨發布的產出**。順序不可跳。

---

### Phase 0 — 專案骨架與資料源盤點

**目標**：建立可運作的開發環境，並**實地確認**每個資料源真的拿得到。

**步驟**
1. `uv init` 建立專案，寫 `pyproject.toml`（Python 3.12），安裝核心依賴
2. 建立上述完整目錄結構，`justfile`、`.pre-commit-config.yaml`、`ruff.toml`、`mypy.ini`
3. 把本計畫複製為 repo 內的 `PLAN.md`
4. **資料源探勘腳本** `scripts/probe_sources.py`：
   - 呼叫 `data.moenv.gov.tw` 列出所有 `空品測站小時值` 標籤的 dataset（搜尋結果顯示有 79 個），**輸出實際 dataset id 清單到 `conf/sources.yaml`**（不要憑猜測寫死 id）
   - 解析 `His_Data.aspx`，抓出每個年份/空品區對應的 Google Drive file id
   - 各下載 1 個最小樣本（1 個月 / 1 個測站），存到 `data/raw/_samples/`
   - 產出 `docs/data-sources.md`：每個源的實際欄位、編碼、分隔符、flag 符號、時區、單位
5. 申請所有 API key（環境部、CWA、CDS、GEE），寫 `.env.example`，`.env` 進 `.gitignore`
6. `docs/legal.md`：記錄每個來源的授權條款、robots.txt 檢查結果、rate limit 策略

**驗收**
- [ ] `just setup` 一鍵可跑
- [ ] `data/raw/_samples/` 下每個資料源都有真實樣本檔
- [ ] `docs/data-sources.md` 記錄了**實際觀察到**的欄位而非文件宣稱的
- [ ] `conf/sources.yaml` 的每個 id / URL 都經過驗證

**風險**：airtw 的 Google Drive 連結會變動 → 探勘腳本必須每次重新解析頁面，並把 file id + 檔案 checksum 存進 `conf/sources.yaml` 供比對。

---

### Phase 1 — 資料取得與 QA/QC → Canonical Dataset ⭐

**目標**：把散落的政府原始檔統一成一份乾淨、有版本、有品質標記的資料集。
**這是整個專案的地基，也是最容易被低估的部分——原專題的「以鄰近測站資料代替」一句話帶過，正是它最大的黑盒。**

#### 1.1 抓取

- `twair ingest airtw --years 2006:2026` — 年度包，落地 `data/raw/airtw/`
- `twair ingest moenv --dataset AQX_P_07` — 測站 metadata
- `twair ingest cwa --years ...` — 氣象站觀測
- 所有下載存 checksum 到 `data/raw/_manifest.jsonl`，重跑時跳過已存在且 checksum 相符者
- 全部走 `registry.py` 統一的重試 / rate limit / 快取

#### 1.2 QA/QC（`src/twair/qc/`）

**這是與原專題差距最大的模組，要當成一等公民做。**

1. **原始 flag 解析**（`flags.py`）— 環境部原始檔的值欄位會帶符號：
   `#` 硬體異常、`*` 儀器檢核、`x` 程序檢核、`A` 有降雨、`NR` 無降雨、`ND` 無資料、`-`/空白 缺值
   → 拆成 `value: float` + `flag: enum`，**絕不把 flag 當成數值或直接丟棄不記錄**
2. **值域檢查**（`range.py`）— 依 `conf/pollutants.yaml` 的物理合理域 + 偵測極限（低於 DL 標記，不直接設 0）
3. **物理一致性** — `PM2.5 ≤ PM10`、`NO + NO2 ≈ NOx`（容差內）、`RH ∈ [0,100]`、風速 ≥ 0
   → **違反者標記而非刪除**，並在報告中統計違反率（這本身是有趣的發現）
4. **異常偵測**（`outliers.py`）— 區分「儀器尖峰」與「真實污染事件」：
   單站尖峰但鄰站無反應 + 持續 <2h → 可疑；多站同步上升 → 真實事件（沙塵/境外傳輸）
5. **缺漏填補**（`gapfill.py`）— **多策略可切換**，預設**不填補**：
   - `none`（預設，分析時 dropna）
   - `interpolate`（≤3h 線性內插）
   - `neighbor`（鄰近測站迴歸，**復刻原專題做法**，供對照）
   - `mice` / `iterative`
   → 每筆填補值都帶 `imputed: bool` + `impute_method` 欄位；**M2 分析必須做填補策略的敏感度分析**
6. **測站生命週期** — 處理測站改名、搬遷、新設、停用（`conf/stations.yaml` 維護別名表與有效期間）

#### 1.3 儲存格式

**Long 表**（真實來源，`data/processed/observations/`）：
```
year=YYYY/month=MM/part-*.parquet   # zstd, row group 128MB
```
| 欄位 | 型別 | 說明 |
|---|---|---|
| `station_id` | int32 | 環境部站代碼 |
| `station_name` | categorical | |
| `county` / `airzone` | categorical | 縣市 / 空品區（8 區，沿用原專題分類） |
| `station_type` | categorical | 一般/工業/交通/國家公園/背景 — **原專題「只能用所有測站都有的測項」的限制，改為明確建模** |
| `lat` / `lon` / `elevation` | float32 | |
| `ts_local` | datetime(Asia/Taipei) | |
| `ts_utc` | datetime(UTC) | |
| `pollutant` | categorical | |
| `value` | float32 | |
| `unit` | categorical | |
| `flag` | categorical | valid / hw_error / calibration / procedure / below_dl / missing |
| `imputed` | bool | |
| `impute_method` | categorical | |
| `source` | categorical | airtw_yearly / moenv_api / … |

**Wide 表**（衍生，`data/processed/hourly_wide/`）：DuckDB PIVOT 產生，供建模直接使用
**日/月聚合表**：含 `n_valid`、`coverage_ratio` 欄位（**聚合前必須檢查覆蓋率，原專題沒做**）

#### 1.4 資料品質報告

`twair qc report` → `docs/data-quality.md` + 圖：
- 各測站 × 各測項 × 各年 的資料完整度熱圖
- flag 分布時序（儀器異常率有沒有隨年份改善？）
- 物理一致性違反率
- 測站生命週期甘特圖

#### 1.5 發布到 HuggingFace

- `twair publish hf` → `<user>/taiwan-air-quality`
- 完整 **Dataset Card**：來源、授權、欄位字典、已知問題、引用格式、使用範例（含 DuckDB one-liner）
- 提供三種粒度的 config：`hourly` / `daily` / `monthly`
- 打 tag `v1.0.0`，Dataset Card 標註對應的 repo commit

**驗收**
- [ ] `just ingest && just qc` 端到端可跑完，產出 Parquet
- [ ] 逐時筆數與環境部官方公告的測站數 × 時數大致相符（誤差 <1%，差異須有解釋）
- [ ] `docs/data-quality.md` 完整
- [ ] HF dataset 頁面可用 `datasets.load_dataset()` 載入
- [ ] pytest 覆蓋 flags 解析、schema 驗證、時區轉換（**夏令時間/跨年邊界要有測試**）

> **這個 Phase 結束就已經是一個可以獨立發布、被別人引用的貢獻了。** 台灣目前沒有一份
> 乾淨、有 QA flag、涵蓋全歷史的開源空品資料集。

---

### Phase 2 — 核心分析：復刻 → 修正 → 對照 ⭐

**目標**：先證明你能重現當年的結果，再證明當年的結果是錯的。

#### M1 復刻（`analysis/replication.py`）

**完全照 2018 年的方法重跑**：2010–2017、月平均、同樣 12 個解釋變數、同樣的
Mixed Model with AR(1)、同樣的逐步剔除順序。

- 目標：重現 N≈7,286、Pearson 相關矩陣、OLS 係數（β_PM10≈0.413）、最終 8 變數模型
- 產出對照表：`原專題數值 | 復刻數值 | 差異 | 差異原因`
- **差異必然存在**（缺漏填補細節不同、測站清單不同），要如實記錄並解釋

> 這一步是整個專案的**可信度基礎**。沒有它，後面所有「我改進了」的宣稱都沒有基準。

#### M2 逐時重做（`analysis/drivers.py`）

| 面向 | 原專題 | 終極版 |
|---|---|---|
| 時間解析度 | 月平均 N=7,286 | 逐時 N≈1e8 |
| 風向 | 線性 0–360 | `sin(θ)`, `cos(θ)`，另加 u/v 分量與風速交互 |
| PM10 | 當解釋變數 | **移出**；PM2.5/PM10 ratio 當作來源指紋衍生變數 |
| 氣象 | 溫度/濕度/雨量/風 | + **BLH**、氣壓、日射、通風係數(BLH×WS)、露點差 |
| 時間效應 | 無 | hour/dow/doy 循環編碼、國定假日、**農曆年與中元（燒金/鞭炮）** |
| 化學 | 單一污染物 | Ox = O3 + NO2、SO2/NOx ratio、NOx/CO ratio |
| 共線性 | 逐步剔除 | Elastic Net、VIF 診斷、樹模型天然免疫 |
| 模型 | OLS → Mixed AR(1) | OLS / Elastic Net / GAM / Mixed / **LightGBM**（主力）多模型對照 |
| 可解釋性 | 迴歸係數 | **SHAP**（global + local + interaction） |
| 驗證 | in-sample AIC/BIC | rolling-origin CV + LOSO(站) + LOYO(年)，對照 persistence/climatology baseline |
| 不確定性 | 無 | bootstrap CI、分位數迴歸（極端高濃度日的驅動因子與平常日不同） |

**必做的敏感度分析**
- 缺漏填補策略（none / interpolate / neighbor / mice）對結論的影響
- 測站類型（工業/交通/一般/背景）分層是否改變驅動因子排序
- 加不加 PM10 的 R² 差距 → **量化 D2 這個洩漏到底虛胖了多少**

#### M3 方法學對照（`analysis/pitfalls.py`）

專章展示「同一份資料、兩種做法、兩個結論」。每個缺陷做成一組成對圖表：

1. **月平均 vs 逐時** — 日夜雙峰、週末效應、污染事件全被月平均抹平
2. **PM10 洩漏** — 有/無 PM10 的 R² 與 SHAP 排序對比
3. **風向線性化** — 用線性 WD_HR 得到的「風向係數」vs 極座標圖顯示的真實方位依賴
4. **常態檢定謬誤** — 大樣本下 p 值必然顯著；調低 α 不會讓資料變常態
5. **NO/NO2/NOx 恆等式** — 為什麼逐步刪除會來回震盪
6. **in-sample vs out-of-sample** — 原模型在 2018–2025 資料上的表現

**驗收**
- [ ] 復刻結果與原專題數值對照表完成，主要係數方向一致
- [ ] M2 完整 backtest 結果表（多模型 × 多驗證策略 × 多指標）
- [ ] SHAP 圖組完成
- [ ] M3 六組對照圖完成
- [ ] Quarto 報告 `reports/01-core.qmd` 可產出 HTML

---

### Phase 3 — 網站骨架 + 首波上線 ⭐

**目標**：在分析全部做完之前先讓網站活起來。前三章先上，後續章節逐步補。

#### 資料分層策略（關鍵設計）

1 億筆不可能丟進瀏覽器。分三層：

| Level | 內容 | 大小 | 交付方式 |
|---|---|---|---|
| L0 | 站-月 aggregates（78 站 × ~240 月 × 15 測項） | ~2 MB JSON | 內嵌，即時互動 |
| L1 | 站-日（~850 萬列） | ~40 MB Parquet 分檔 | DuckDB-WASM 按需 range-request 載入 |
| L2 | 站-時 全量 | ~2 GB | 只放 HF，網站提供下載連結 + 查詢範例 |

`viz/export.py` 負責從 Parquet 產出 L0/L1，寫入 `web/public/data/`。

#### 網站章節

| # | 章節 | 內容 | 互動 |
|---|---|---|---|
| 0 | Hero | 全台最新 PM2.5 地圖 | MapLibre 測站點 + 時間軸 scrubber |
| 1 | 這 20 年到底變好了嗎？ | 全台趨勢，raw vs 氣象正規化雙線 | 切換縣市/測站類型 |
| 2 | 你住的地方 | 選測站 → 個人化報告：年均、超過 WHO 指引幾天、換算等效香菸數、與全台排名 | 下拉/地圖點選 |
| 3 | 空氣從哪裡來？ | CBPF 極座標圖 + HYSPLIT 軌跡動畫 | deck.gl 軌跡播放 |
| 4 | 政策有用嗎？ | 事件研究互動圖（台中電廠降載 / COVID 三級警戒 / 空污法修法） | 選事件切換 |
| 5 | **我當年錯在哪** | M3 六組成對圖表 + 原 PDF 截圖對照 | before/after 滑桿 |
| 6 | 資料探索器 | DuckDB-WASM，使用者寫 SQL 或用 UI 拉圖 | |
| 7 | 方法與資料 | 完整技術文件、資料集下載、引用格式 | |

**視覺方向**：深色為主、資料密度高、克制的動效。第 5 章是差異化重點，值得花最多設計力氣。

**驗收**
- [x] 本地可跑 —— `cd web && npm run dev`
- [ ] GitHub Actions 部署到 GitHub Pages 成功 —— 工作流已備妥，**等 repo 建立**
- [x] 章節 0/1/2/5 上線（外加第 7 章資料與方法）
- [ ] 第 6 章 DuckDB-WASM 資料探索器 —— L1 已備妥，前端未做
- [x] 行動裝置可用 —— 375px 無橫向溢出，全部文字達 WCAG AA
- [x] 深/淺色主題都正常 —— 兩套色階分別定義，非濾鏡

---

### Phase 4 — 氣象正規化 + 政策因果

#### M4 氣象正規化（`analysis/deweather.py`）

實作 Grange et al. (2018) `rmweather` 演算法（R 套件，需**自行以 Python 實作**）：

1. 訓練 RF：`PM2.5 ~ 氣象變數 + unix_time(趨勢) + doy + hour + dow`（300 棵樹）
2. 對每個時間點，**重抽樣**氣象變數（從整個資料期間隨機抽），預測 1000 次取平均
3. 得到「氣象條件標準化後」的濃度序列 → 反映真實排放變化
4. 用 Theil-Sen 估趨勢，bootstrap 信賴區間

**產出**：全台與各縣市的 raw trend vs deweathered trend 對照。
回答「空氣變好，有多少是政策的功勞、多少只是那年風比較大」。

#### M5 政策因果（`analysis/causal.py`）

`conf/events.yaml` 建立事件時間軸，至少涵蓋：
- 2018 空污法修法
- 台中電廠燃煤機組降載 / 生煤許可爭議
- 深澳電廠停建（2018-10）
- COVID-19 三級警戒（2021-05-19 ~ 07-26）
- 老舊二行程機車汰換補助
- 六輕歲修期間
- 各縣市自治條例

方法（多方法交叉驗證，結論一致才可信）：
1. **事件研究 / 時間斷點迴歸（RDiT）** — 在 deweathered 序列上做
2. **合成控制法** — 用未受影響的測站當 donor pool
3. **Bayesian structural time series（CausalImpact）**
4. **DiD** — 處理組 vs 對照組測站

**必須誠實報告**：多數政策的效果量會很小或不顯著。這是正確的科學結論，不要為了故事性誇大。

**驗收**
- [ ] deweather 模組有單元測試（合成資料驗證能還原已知趨勢）
- [ ] 每個事件至少 2 種方法的結果，含信賴區間
- [ ] 網站第 1、4 章上線
- [ ] `reports/02-trends-causal.qmd`

---

### Phase 5 — 空間分析 + 境外傳輸

#### M6 空間（`analysis/spatial.py`）
- **Moran's I / LISA** — 全域與局部空間自相關，找 hot spot / cold spot
- **測站分群** — 時序型態分群（DTW + k-medoids，或 UMAP + HDBSCAN）→ 檢驗官方 8 空品區劃分是否合理（**很可能不合理，這是好發現**）
- **Kriging / RF 空間內插** — 產出全台 1km 網格 PM2.5 濃度場（逐月）
- **人口加權暴露** — 結合人口網格，算真正的人口暴露量而非測站平均（**測站平均系統性低估都會區暴露**）

#### M7 境外傳輸（`analysis/trajectory.py`）
- **CPF / CBPF** — conditional (bivariate) probability function 極座標圖，找高濃度對應的風速風向組合 → 污染源方位
- **HYSPLIT 72h 後推軌跡** — 容器化 HYSPLIT + GDAS，或用 ERA5 風場自建 Lagrangian 積分器（fallback）
- **軌跡分群** — k-means on trajectory，分類傳輸路徑（東北季風 / 西南氣流 / 局地滯留）
- **PSCF / CWT** — 潛在源貢獻函數，畫出境外源區
- **交叉驗證** — 用 PM2.5/PM10 ratio、SO2/NOx ratio 佐證來源類型（沙塵 ratio 低、燃燒 ratio 高）

**驗收**
- [ ] 全台 1km 濃度場（GeoTIFF + Cloud-Optimized）
- [ ] Moran's I 顯著性、LISA 圖
- [ ] 至少 5 年的後推軌跡資料庫 + 分群結果
- [ ] 網站第 3 章上線（軌跡動畫）
- [ ] `reports/03-spatial.qmd`

**風險**：HYSPLIT 部署較重。若受阻，先用 ERA5 風場的簡易積分器出結果，把 HYSPLIT 列為後續增強，**不要卡住整條流程**。

---

### Phase 6 — 衛星 + 微型感測器融合

#### M8（`analysis/satellite.py`）
- **Sentinel-5P** NO2/SO2 柱濃度 vs 地面測值的相關性與偏差分析
- **MODIS MAIAC AOD → PM2.5** — AOD 與地面 PM2.5 的關係受 BLH 與 RH 調制，建立校正模型
- **微型感測器校正** — 數千個低成本感測器對鄰近標準站做 calibration transfer（含濕度校正），量化校正前後誤差
- **資料融合** — 地面站（準但稀疏）+ 衛星（廣但粗）+ 微感測器（密但雜）→ 高解析度濃度場
  - 方法：geostatistical fusion / RF with satellite covariates / Bayesian hierarchical model

**驗收**
- [ ] 至少 1 年的 1km 日 PM2.5 融合產品
- [ ] 融合場的獨立驗證（留出測站評估）
- [ ] 微感測器校正前後 RMSE 對照
- [ ] `reports/04-fusion.qmd`

**風險**：GEE 需註冊專案（免費但需審核，可能數天）。Phase 0 就要先申請。

---

### Phase 7 — 預測模型 + HuggingFace Space

#### M9（`src/twair/models/`）

任務：全台各測站 PM2.5 未來 1–72 小時預測。

| 層級 | 模型 |
|---|---|
| Baseline | persistence、climatology、seasonal naive |
| 傳統 | **SARIMA / SARIMAX**（補上原專題明說放棄的） |
| ML | LightGBM direct multi-horizon（主力） |
| DL | N-HiTS / PatchTST（neuralforecast） |
| 圖 | **GNN**（測站為節點，距離+風向為邊，捕捉污染傳輸） |

- 統一 backtest 框架（`evaluate.py`）：rolling origin、固定測試期、每個 horizon 分別報告
- 指標：RMSE / MAE / MAPE / 高濃度事件的 F1（**預測「會不會爆表」比預測絕對值更有用**）
- **與環境部官方空品預報比較**（若可取得歷史預報）

#### HF Space（`spaces/forecast/`）
Gradio 介面：選測站 → 顯示最近觀測 + 未來 72h 預測（含預測區間）+ SHAP 解釋當前預測的主因。

**驗收**
- [ ] Backtest 結果表：所有模型 × 所有 horizon
- [ ] 主力模型明顯優於 baseline（否則誠實報告）
- [ ] HF Space 可運行
- [ ] `reports/05-forecast.qmd`

---

### Phase 8 — 健康衝擊、收尾、自動化

#### M10 健康衝擊（`analysis/health.py`，加分）
- WHO 2021 全球空氣品質指引比對（年均 5 µg/m³、日均 15 µg/m³）
- GEMM / IER 曝露反應函數 → 歸因死亡數估計（**須明確標註方法不確定性**）
- 換算成直覺單位（等效香菸數、預期壽命損失）供科普章節使用

#### 收尾
1. **CI 自動更新**：`daily-update.yml` — 每日抓增量 → QC → 更新 Parquet → push HF dataset → 重建網站 L0/L1 → 部署
2. **文件**
   - `README.md`（中）/ `README.en.md`：一句話定位、hero 圖、快速開始、主要發現摘要、引用格式
   - `docs/methodology.md`：完整方法論
   - `CITATION.cff`
   - 所有模組的 docstring + `mkdocs` API 文件（選配）
3. **`twair` 發布到 PyPI**
4. **Zenodo DOI**（讓資料集與程式碼可被正式引用）
5. **完整報告** `reports/00-full.qmd` → PDF + HTML，作為「新版畢業專題」

**驗收**
- [ ] CI 綠燈，每日更新連續成功 7 天
- [ ] 網站全 8 章上線
- [ ] HF dataset + Space 都活著
- [ ] README 有人看得懂在幹嘛（找非本科朋友試讀）
- [ ] PyPI 可 `pip install twair`

---

## 跨階段的硬性規範

1. **可重現**：每個分析輸出寫入 `data/outputs/<module>/<run_id>/`，含 `config.yaml`（含 hash）、`git_sha`、`env.txt`、`metrics.json`
2. **不硬編**：所有路徑、URL、參數進 `conf/*.yaml`
3. **Schema 強制**：pipeline 每個階段進出都過 Pandera 驗證，失敗即中止
4. **測試**：`ingest` 用 recorded fixtures（不打真實網路）；`qc` 與 `features` 要有邏輯測試；分析模組用合成資料驗證能還原已知答案
5. **誠實報告**：效果不顯著、模型輸給 baseline、資料有問題——**全部如實寫進報告**。這比漂亮的結果更能證明能力
6. **雙語**：README、網站、Dataset Card 中英雙語；程式碼註解與 commit 英文
7. **合規**：遵守 robots.txt、rate limit（預設 ≥1s/req）、標註來源與授權、不重新散布有授權限制的原始檔

---

## 驗證方式（端到端）

```bash
# 環境
just setup && just test && just lint

# 資料管線（首次會跑很久，用 --years 2024:2025 先試小範圍）
just ingest && just qc && just build-tables
duckdb -c "SELECT count(*) FROM 'data/processed/observations/**/*.parquet'"

# 分析
just analyze M1   # 復刻，比對 reports/_expected/replication_2018.csv
just analyze M2   # 逐時重做
just analyze all

# 網站
just export-web && just web        # http://localhost:4321
just build-web                     # 產出 docs/

# 發布（需 token）
just publish-hf && just publish-space
```

**關鍵驗收檢查點**
1. Phase 1 結束：逐時筆數 vs 官方測站數 × 時數，誤差 <1%
2. Phase 2 結束：M1 復刻的 OLS 係數與原專題 p.49 表格方向一致、量級相近
3. Phase 3 結束：GitHub Pages 網址可公開存取，行動裝置正常
4. Phase 7 結束：主力預測模型在 24h horizon 的 RMSE 優於 persistence baseline

---

## 已識別風險與對策

| 風險 | 對策 |
|---|---|
| airtw Google Drive 連結變動 | 每次執行重新解析 `His_Data.aspx`；file id + checksum 存檔比對 |
| CODiS 爬蟲被擋 | 嚴格 rate limit + 快取；備案改用 opendata API（時間範圍較短）或 ERA5 |
| GEE 專案審核需時 | Phase 0 就申請 |
| ERA5 下載排隊慢 | 背景批次下載；先用單一年份跑通流程 |
| HYSPLIT 部署複雜 | Fallback：ERA5 風場自建簡易 Lagrangian 積分器 |
| 逐時 1 億筆記憶體撐不住 | Polars lazy + DuckDB out-of-core；分區處理 |
| 範圍過大爛尾 | **嚴格照 Phase 順序**，每階段都要能單獨發布；Phase 1–3 完成即已是完整作品 |
| 測站改名/搬遷造成序列斷裂 | `conf/stations.yaml` 維護別名與有效期；斷裂處在報告中明確標註 |

---

## 最小可行版本（若時間不足）

**Phase 0 + 1 + 2 + 3 就已經是一個完整、可以拿出去的作品**：
乾淨的開源資料集 + 嚴謹的分析重做 + 「我當年錯在哪」的互動網站。
Phase 4–8 是加分，可以在上線後逐步補齊——**先上線再迭代，不要等全部做完才發布**。
