# air-quality — 台灣空氣品質再分析平台

> 2018 年畢業專題《台灣地區之 PM2.5 之影響分析》的終極重製版
> 資料工程 × 現代統計/ML × 互動視覺化 × 開源發布

---

**這份文件保留早期 blueprint，也標示現在真正交付的 release boundary。**
磁碟上的 store、分析產物與網站匯出仍以 `uv run twair status` 為準；本文件負責說明
哪些原始構想已交付、哪些被實測結果取代、哪些明確延後。程式、測試與產物的證據
優先於勾選框；規劃若與 [docs/](docs/) 的實測文件牴觸，以後者為準。

## 2026-08-10 實測現況

| 範圍 | 現況 | 可重跑的證據 |
|---|---|---|
| Canonical store | 1982–2025、44 年、521 個分區、340,371,384 筆逐時觀測 | `uv run twair status` |
| QA/QC 與分析 | QC、M1–M7、M9–M12 均有實際 Parquet 產物；M8 已取得 2025 S5P 與 MAIAC Stage A 站月來源表，不冒充分析或融合已完成 | `data/outputs/`、`data/interim/satellite/`、`data/interim/maiac/` 與各 `twair analyze …` 指令 |
| 公開報告 | 核心分析與空間分析以可重生的 Markdown 報告交付 | `reports/01-core.md`、`reports/03-spatial.md` |
| 網站 | 首頁加 10 個主題 route；圖表建置為 SVG，資料查詢在瀏覽器內以 DuckDB-WASM 執行 | `web/src/lib/chapters.ts`、`npm --prefix web run build` |
| 預測 | M9 在 1／6／24／48 小時各跑 4 個 rolling-origin split，並保留 persistence 與 climatology 基準；Space bundle 可重建 | `data/outputs/m9_forecast/`、`uv run twair export space` |
| 發布邊界 | GitHub Pages 與 HF Space 已公開；L0／L1 HF Dataset 先完成本機 bundle 與載入驗證，再由 owner 決定遠端上架；完整 L2 不發布，PyPI 仍是選配 | README 與本文件 Phase 7–8 |

下方驗收標記採三種交付判定：

- `[x]`：已由程式、測試或產物交付。
- `[x] **已取代**`：早期做法沒有照字面實作，但已有更合適且可驗證的替代方案。
- `[ ] **延後**`：不在目前 release boundary；不應被誤讀為現有功能故障。

更正讀者會看到的說法時，直接更新對應的公開技術文件；可重用且容易重蹈的教訓則收進
[docs/working-rules.md](docs/working-rules.md)。

---

## Context

### 為什麼做這件事

起點是一份 2018 年的大學畢業專題（143 頁）。它用環保署 2010–2017 年
全年逐時資料，涵蓋 8 個空品區約 78 個測站，做了：盒型圖 → 散佈圖 → 敘述統計 →
K-S 常態檢定 → Pearson/偏相關 → OLS(VIF) → 殘差分析 → **Mixed Model with AR(1)** →
分區配適 → **ARIMA 轉換函數模型**。工具是 SAS Enterprise Guide + SPSS + R（全 GUI）。

> **不具名原則。** 這份專案不提及原專題的任何作者或指導教授姓名，
> 也不重製原文的任何圖表。要用的只有「方法選擇」與「已公布的數值」——
> 前者是方法學事實，後者是可驗證的數字，兩者都不需要指名任何人。
> 原始 PDF 已列入 `.gitignore`，不隨程式碼發布。

方法在當時的大學部水準是合格的，但以今天的標準有結構性缺陷，
其中兩個足以推翻主要結論。本專案的價值不在「用比較新的工具重做」，
而在**用同一份資料證明修正方法之後結論不同**——
這個「方法學對照」是最有記憶點的內容，但它現在只是整個專案的一章：
44 年的開源資料集、品管管線與互動網站本身就能獨立成立。

### 原專題缺陷清單（終極版必須逐條回應）

| # | 缺陷 | 嚴重度 | 原專題出處 | 終極版對策 |
|---|---|---|---|---|
| D1 | **逐時資料聚合成月平均** | 🔴 致命 | 第三章「將逐時資料合併為各測站之月資料」，N=7,286 | Canonical store 保留 340,371,384 筆逐時觀測；月均只作有覆蓋率門檻的衍生表與對照組 |
| D2 | **用 PM10 預測 PM2.5** | 🔴 致命 | 第五章解釋變數含 PM10，β=0.4133, t=92.75 | PM2.5 是 PM10 的物理子集，定義上的資訊洩漏。移出特徵集；改把 PM2.5/PM10 ratio 當作粒徑組成指標，可篩選來源假說，但單一比值不能唯一辨識或量化來源 |
| D3 | **風向 WD_HR 當線性連續變數** | 🔴 嚴重 | 全文，含 p.29 散佈圖、各章迴歸係數 | 0° 與 359° 物理相鄰卻數值相距 359。改用 sin/cos 分解 + 風速風向合成 u/v 分量 |
| D4 | **常態檢定結論倒果為因** | 🟠 | p.37「將拒絕域的標準降低至 0.01，使每一個變數都不拒絕虛無假設，因此符合常態性」 | M3 直接量測 sample size 如何驅動拒絕率；推論改報 block/bootstrap interval 與樣本外表現，不再靠調 α 宣稱常態 |
| D5 | **共線性用逐步刪除硬處理** | 🟠 | 第五章第三節，NO/NO2/NOx 反覆進出模型 8 次 | NO + NO2 ≡ NOx 是恆等式，本來就不該同時進模型；M2 改用 NOx level + NO2/NOx ratio 與 LightGBM TreeSHAP，M3 另量化係數不穩定 |
| D6 | **零樣本外驗證** | 🟠 | 全文只有 in-sample AIC/BIC | rolling-origin CV + leave-one-station-out + leave-one-year-out，一律報告 baseline 對照 |
| D7 | **78 個測站的空間維度沒用** | 🟠 | 僅「分 8 區各跑一次」 | ✅ M6 實測後修正指控：分層其實移除大部分空間相依（每月殘差 I 0.157→0.063），但原文 t 值出自合併式模型——two-way 校正後 t(PM10) 92.75→14.07，WD_HR 失去顯著；分區在地理 ensemble 99.5 百分位、惟資料偏好 k=2。LISA BH 後 0 熱點。人口加權暴露與 1km 場**不做**，理由記錄於 conf/spatial.yaml |
| D8 | **相關 ≠ 因果，無氣象正規化** | 🟡 | 全文 | Grange et al. (2018) random-forest 氣象正規化 + weather-only counterfactual + placebo distribution；結果寫成偵測極限，不冒充政策因果 |
| D9 | **GUI 工具不可重現** | 🟡 | SAS EG / SPSS | 全 Python，uv 鎖定版本，CI 可重跑 |
| D10 | **放棄 SARIMA** | 🟡 | p.137「SARIMA 不在本專題繼續討論…在此實屬不便」 | M12 補回 AutoARIMA／SARIMA 並與 persistence、climatology 比較；M9 另以 LightGBM 實作可部署預報，N-HiTS／GNN 延後 |
| D11 | **缺漏值以鄰近測站代替，無記錄** | 🟡 | 第三章 | ✅ Canonical store 與公開圖表不補值；`qc/gapfill.py` 的四種策略只在隔離、帶旗標且不回寫 store 的 M11 敏感度實驗中比較（鄰站法 MAE 7.41 μg/m³，同樣的單小時缺口比內插差 2.8 倍） |

### 目標產出（四位一體）

1. **開源資料集** — 台灣 1982–2025 全測站的 L0 站月與 L1 站日衍生統計；網站直接交付，也能封裝成 Hugging Face Dataset。完整 L2 逐時複本不發布，由公開管線重建
2. **看得懂的技術報告** — 方法完整可重現（Markdown，不引入 Quarto/DOI）
3. **科普互動網站** — GitHub Pages，scrollytelling + 瀏覽器端資料探索器
4. **可用工具** — `twair` Python 套件 + Hugging Face Space 預測 demo；PyPI 是選配而非 release gate

---

## 專案識別

- Repo: `kuotunyu/air-quality`
- Python 套件: `twair`（**不隨專案改名**——那是 import 路徑與 CLI 名稱）
- 網站名: **air-quality｜台灣空氣品質再分析**
- HF Dataset: `steven0226/air-quality`（尚未上架）
- HF Space: `steven0226/airlens-taiwan-forecast`（**已上線**，沿用建立時的名稱：
  改名會讓一個已公開、且被 README 與網站第五章連結的網址失效）
- 授權: 程式碼 MIT；資料衍生物 CC BY 4.0（依政府資料開放授權條款第 1 版，須註明出處）

---

## 技術棧（已定案）

| 層 | 選擇 | 理由 |
|---|---|---|
| 套件管理 | **uv** + `pyproject.toml` | 快、鎖定版本、CI 友善 |
| 資料處理 | **Polars** 主力 + **Pandas** 相容層 | 逐時 3.40 億筆；只在 statsmodels 等相容邊界轉換 |
| 查詢/儲存 | **DuckDB** + **Parquet(zstd)**，Hive 分區 | 免資料庫伺服器；同一份 Parquet 給 Python 與瀏覽器共用 |
| Schema 驗證 | `twair.store.schema` 的 typed contract | 驗證 key、dtype 與重複列；衝突資料拒絕而不猜測 |
| 抓取 | **httpx** + **tenacity** + **gdown** | async + 重試 + Google Drive |
| 統計 | statsmodels + scipy | M1 復刻、OLS、趨勢與 bootstrap 推論 |
| ML | scikit-learn + **LightGBM** | 主力模型；TreeSHAP 直接由 LightGBM 計算，不另外安裝 `shap` |
| 時序 | **statsforecast**（AutoARIMA）+ rolling-origin backtest | SARIMA 已作為 M12 實測；N-HiTS/PatchTST 不在目前 release boundary |
| 空間 | geopandas, **libpysal/esda**, **pykrige**, shapely | Moran's I、Kriging |
| 地科 SDK（選配） | **xarray** + **cdsapi** + **earthengine-api** | credential verification 已有；S5P 與 MAIAC 站月來源取得已交付，ERA5 與 M8 分析尚未交付 |
| 報告 | **Markdown** | 表格由 Parquet 產物重生；不維護第二套 Quarto/PDF 工具鏈 |
| 前端 | **Astro** + TypeScript + handwritten CSS | 靜態產出、部署到 Pages 零成本 |
| 圖表 | build-time **SVG** + 少量 progressive enhancement | 無繪圖 runtime；沒有 JavaScript 時仍可閱讀與列印 |
| 瀏覽器端查詢 | **DuckDB-WASM** | 直接查 Parquet，無後端也能做 ad-hoc 分析 |
| Space | **Gradio** | 預測模型 demo |
| 品質 | pytest, ruff, mypy, pre-commit | |
| CI | GitHub Actions | 程式品質、每週離線 freshness gate、Pages 部署 |

---

## Repo 結構

```
air-quality/
├─ README.md / README.en.md / PLAN.md
├─ conf/                         # 資料源、測站、QA/QC、分析與網站參數
├─ src/twair/
│  ├─ ingest/                    # 年度頁探勘、下載、跨格式 archive parser、來源驗證
│  ├─ qc/                        # flag、sentinel、一致性、異常、stuck、報告與 M11 實驗工具
│  ├─ store/                     # schema、去重、Hive Parquet writer、聚合與修復稽核
│  ├─ features/                  # 氣象、化學、時間與不洩漏的 lag features
│  ├─ analysis/                  # M1–M7、M10–M11
│  ├─ models/                    # 共用評估、M9 forecast、M12 SARIMA、Space bundle
│  ├─ viz/                       # 網站 JSON／Parquet／SVG-friendly payload export
│  ├─ freshness.py / status.py  # 離線可判定的資料新鮮度與磁碟現況
│  └─ cli.py                     # `twair` CLI
├─ reports/                      # 由分析產物重生的 Markdown 報告
├─ web/                          # Astro 靜態網站與 DuckDB-WASM 資料探索器
├─ spaces/forecast/              # 可追蹤的 Gradio 程式；model/data bundle 由 CLI 重建
├─ scripts/                      # publication consistency 與 repository gates
├─ tests/
├─ data/                         # gitignored：raw、processed、outputs
└─ .github/workflows/
   ├─ ci.yml                     # lint、type、test、publication consistency
   ├─ freshness.yml              # 每週離線判斷是否漏掉完整年度
   └─ pages.yml                  # 建置 Astro 並部署 GitHub Pages
```

---

## 資料源（核心已查證；外部加值源分階段交付）

> ⚠️ 環保署已於 2023/08 改制為**環境部**，原專題引用的 `taqm.epa.gov.tw` 已失效。

### 主要（必要）

| 來源 | 位址 | 內容 | 取得方式 | 備註 |
|---|---|---|---|---|
| **環境部 空品監測網 年度逐時包** | `airtw.moenv.gov.tw/cht/Query/His_Data.aspx` | 1982–2025 全年逐時原始值，可依空品區或全部測站下載 | 頁面探勘 Google Drive file id → `gdown` | ✅ **主力來源已完整建置**；連結會變動，所以 `twair probe sources` 每次重新解析 |
| **環境部 資料開放平臺 API** | `data.moenv.gov.tw/api/v2/{id}` | 測站基本資料與即時發布時間 | REST，部分功能需 API key | ✅ 測站登錄與 optional live context；freshness 的 pass/fail 不依賴網路 |
| **中央氣象署 開放資料** | `opendata.cwa.gov.tw` | 自動氣象站逐時觀測 | REST，需授權碼 | ⬜ credential probe 已實作，資料尚未取得 |
| **CWA CODiS** | `codis.cwa.gov.tw` | 歷史逐時氣象觀測 | 頁面查詢 | ⬜ 未納入目前 release boundary |

### 加值（進階模組用）

| 來源 | 內容 | 取得 |
|---|---|---|
| **ERA5**（Copernicus CDS） | **邊界層高度 BLH**、10m 風、2m 溫、地面氣壓、降水 | ⬜ credential probe 已實作；尚未取得 |
| **Sentinel-5P TROPOMI** | NO₂ 對流層柱濃度與 SO₂ 垂直柱濃度 | 🟡 2025 站月 Stage A 已由 GEE 取得；相關性、偏差與融合分析尚未交付 |
| **MODIS MAIAC AOD** | 氣膠光學厚度 | 🟡 2025 station-month batch export／checkpoint 已完成；AOD／PM2.5 分析與融合尚未交付 |
| **智慧城鄉空品微型感測器** | 低成本 PM2.5 感測器 | ⬜ Phase 6 延後；尚未取得 |
| **NOAA HYSPLIT + GDAS** | 後推軌跡 | ⬜ 未納入目前 release；Phase 5 以已量測的 CBPF 交付 |
| **內政部 人口統計網格** | 人口加權暴露 | ⬜ 未取得，因此不發布人口暴露數字 |

> **BLH（邊界層高度）是原專題最關鍵的缺失變數。** 污染物濃度 ≈ 排放量 / 混合層體積。
> 沒有 BLH，任何「氣象因子對 PM2.5 影響」的討論都缺一塊。M4 因此把 holdout
> 表現與限制一起公開；補入 BLH 是後續研究，不是目前成果的隱藏前提。

---

## 執行階段

> 每個 Phase 結束都必須有**可單獨發布的產出**。順序不可跳。

---

### Phase 0 — 專案骨架與資料源盤點

**目標**：建立可運作的開發環境，實地確認核心 airtw 年度來源，並讓需要憑證的
加值來源在沒有憑證時明確顯示為未驗證，而不是假裝成功。

**步驟**
1. `uv init` 建立專案，寫 `pyproject.toml`（Python 3.12），安裝核心依賴
2. **已取代**：依賴、Ruff 與 MyPy 設定集中在 `pyproject.toml`，操作入口是 `twair` CLI；
   repository gates 由 `scripts/check_*.py` 與 GitHub Actions 執行，不建立 justfile 或分散設定檔
3. 保留 repo 內 `PLAN.md`，讓原始 blueprint 與實作判定可追蹤
4. **資料源探勘命令** `uv run twair probe sources`（實作於 `twair.ingest.probe`）：
   - 解析 `His_Data.aspx`，抓出實際年度、類型、空品區與 Google Drive file id，輸出到 `conf/sources.yaml`
   - 下載跨格式世代的真實樣本，存到 gitignored 的 `data/raw/_samples/`
   - 產出 `docs/data-sources.md`：每個源的實際欄位、編碼、分隔符、flag 符號、時區、單位
5. 把環境部、CWA、CDS、GEE 的 optional credential contract 寫入 `.env.example`；
   沒有憑證的來源保持 `probed: false`，不阻擋核心年度資料建置
6. `docs/legal.md`：記錄每個來源的授權條款、robots.txt 檢查結果、rate limit 策略

**驗收**
- [x] ~~`just setup` 一鍵可跑~~ → `uv sync --all-extras --group dev`（沒有 justfile）
- [x] `data/raw/_samples/` 有 1994、2010、2024 三個真實 archive 樣本，跨越實際格式世代
- [x] `docs/data-sources.md` 由 live probe 產生，記錄實際 archive inventory 與觀察到的格式
- [x] `conf/sources.yaml` 記錄 1982–2025 的 44 個年度、108 個逐時檔；未提供憑證的外部來源明列 `probed: false`
- [x] `docs/legal.md` 記錄來源、授權、再散布與 attribution 邊界

**風險**：airtw 的 Google Drive 連結會變動 → 探勘腳本必須每次重新解析頁面，並把 file id + 檔案 checksum 存進 `conf/sources.yaml` 供比對。

---

### Phase 1 — 資料取得與 QA/QC → Canonical Dataset ⭐

**目標**：把散落的政府原始檔統一成一份可追溯、有版本、有品質標記的資料集。
**這是整個專案的地基，也是最容易被低估的部分——原專題的「以鄰近測站資料代替」一句話帶過，正是它最大的黑盒。**

#### 1.1 抓取

- `twair ingest airtw` — 依 `conf/sources.yaml` 下載年度包，落地 `data/raw/airtw/`
- `twair stations geo` — 取得測站 metadata；歷史名稱與生命週期另由 canonical register 管理
- CWA／ERA5 尚未取得；GEE 只取得後述 S5P Stage A 來源表，尚未納入 canonical store 或分析宣稱
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
4. **異常偵測**（`outliers.py`）✅ **已完成** — 區分「儀器尖峰」與「真實污染事件」：
   單站尖峰但鄰站無反應 + 持續 <2h → 可疑；多站同步上升 → 真實事件（沙塵/境外傳輸）
5. **缺漏語意與 M11 敏感度實驗**（`gapfill.py`、`analysis/imputation.py`）：
   - canonical store 與網站永遠保留 null；稀疏聚合回傳 null 並保留 `n_valid`／`coverage_ratio`
   - `none` 是 shipped behavior，不發明任何值
   - `interpolate`、`neighbor`、`iterative` 只在 derived frame 上明確呼叫，供 M11 遮罩重建實驗
   - 每個實驗填入值都有 companion flag；任何策略都不回寫 canonical store，也不橋接公開圖表的缺口
6. **測站生命週期** — 處理測站改名、搬遷、新設、停用（`conf/stations.yaml` 維護別名表與有效期間）

#### 1.3 儲存格式

**Long 表**（真實來源，`data/processed/observations/`）：
```
year=YYYY/month=MM/part-*.parquet   # zstd, row group 128MB
```
| 欄位 | 型別 | 說明 |
|---|---|---|
| `station_name` | categorical | 正規化後測站名稱；地理與生命週期由獨立 register 對照 |
| `pollutant` | categorical | 測項代碼 |
| `ts_local` | datetime | 原始資料所用的台灣本地時鐘 |
| `value` | float32 / null | 原始數值；無效或缺測時保持 null |
| `flag` | categorical | 解析後的原始品質語意 |
| `value_retained` | bool | 被標記的原始數字是否仍保留供稽核 |
| `imputed` | bool | canonical store 固定為 false；M11 derived frame 才可能為 true |
| `impute_method` | categorical / null | canonical store 為 null；僅記錄隔離實驗策略 |
| `generation` | categorical | archive 格式世代 |
| `source_member` | string | 原始 archive member，提供逐列 provenance |
| `year` / `month` | int | 由 `ts_local` 推導的 Hive partition key |

**日/月聚合表**：含 `n_valid`、`coverage_ratio` 欄位（**聚合前必須檢查覆蓋率，原專題沒做**）

#### 1.4 資料品質報告

`twair qc report` → `docs/data-quality.md` + 圖：
- 各測站 × 各測項 × 各年 的資料完整度熱圖
- flag 分布時序（儀器異常率有沒有隨年份改善？）
- 物理一致性違反率
- 測站生命週期甘特圖

#### 1.5 L0／L1 Hugging Face Dataset（本機 bundle 完成後再人工上架）

- `twair export dataset` → 本機 `data/exports/huggingface/air-quality/`；不自動上傳
- 完整 **Dataset Card**：來源、授權、欄位字典、已知問題、引用格式、使用範例（含 DuckDB one-liner）
- 提供兩種可獨立載入的 config：`daily` / `monthly`；不建立 `hourly` config
- 打 tag `v1.0.0`，Dataset Card 標註對應的 repo commit

**驗收**
- [x] ~~`just ingest && just qc` 端到端可跑完，產出 Parquet~~ → `uv run twair ingest airtw` + `uv run twair build`
- [x] **已取代**：「測站數 × 時數」不能描述測項與測站會變動的 long table；改以 340,371,384 列、521 個分區、來源 member、duplicate-key gate 與逐年 coverage 共同驗收
- [x] `docs/data-quality.md` 由 QC 產物支持，缺值、旗標、sentinel、物理一致性與測站生命週期均保留
- [x] **本機驗收**：L0／L1 bundle 的兩個 config 已用 `datasets.load_dataset()` 載入；遠端上架需要 owner 最後確認
- [x] pytest 覆蓋 flag 解析、canonical schema、archive 日期順序、午夜、跨年圖軸與缺口語意；1982–2025 的台灣資料期間不含夏令時間切換

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
| 時間解析度 | 月平均 N=7,286 | 逐時；M2 實際可建模樣本 5,136,594 列 |
| 風向 | 線性 0–360 | `sin(θ)`, `cos(θ)`，另加 u/v 分量與風速交互 |
| PM10 | 當解釋變數 | **移出**；PM2.5/PM10 ratio 當作粒徑組成指標衍生變數，可篩選來源假說，但單一比值不能唯一辨識或量化來源 |
| 氣象 | 溫度/濕度/雨量/風 | 同一批站內量測 + 風向 sin/cos 與 u/v；**BLH 尚未取得，明列限制** |
| 時間效應 | 無 | hour/dow/doy 循環編碼與長期趨勢 |
| 化學 | 單一污染物 | Ox = O3 + NO2、SO2/NOx ratio、NOx/CO ratio |
| 共線性 | 逐步剔除 | 不讓 NO、NO2、NOx 同時進模；以 NOx level + NO2/NOx ratio 建模 |
| 模型 | OLS → Mixed AR(1) | **LightGBM** 主力，另以 persistence／climatology 作同 split 基準 |
| 可解釋性 | 迴歸係數 | LightGBM TreeSHAP 的 global mean absolute contribution |
| 驗證 | in-sample AIC/BIC | rolling-origin CV + LOSO(站) + LOYO(年)，對照 persistence/climatology baseline |
| 不確定性 | 無 | bootstrap CI、分位數迴歸（極端高濃度日的驅動因子與平常日不同） |

**必做的敏感度分析**
- 缺漏策略由 M11 隔離實驗評估重建誤差；不把填補 frame 當成 canonical 資料或公開趨勢
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
- [x] M1 有 52 列公布值／復刻值對照，報告逐項說明樣本與係數差異
- [x] **已取代**：沒有為湊數實作原始 model zoo；M2 交付 LightGBM feature-set 對照、rolling-origin、LOSO、LOYO、RMSE／MAE／R²／高值 F1 與兩條 baseline
- [x] 五組 feature-set 的 TreeSHAP importance 已輸出
- [x] M3 六組方法學問題由 14 個 Parquet 產物與網站方法章交付
- [x] **已取代**：`reports/01-core.md` 直接由 Parquet 重生；repo 不使用 Quarto

---

### Phase 3 — 網站骨架 + 首波上線 ⭐

**目標**：早期先讓網站活起來；現行版本已從首波單頁演進成首頁加 10 個獨立 route。

#### 資料分層策略（關鍵設計）

3.40 億筆不可能整份丟進瀏覽器。現行 export 分三層：

| Level | 內容 | 大小 | 交付方式 |
|---|---|---|---|
| L0 | 站-月 aggregates | 每測項一個 JSON | 建置時載入圖表與測站摘要 |
| L1 | 站-日 aggregates | 每測項一個 Parquet | DuckDB-WASM 按需 range-request 載入 |
| L2 | 站-時全量 + QA provenance | 本機 canonical store | 不發布完整複本；公開管線與上游 archive 可重建 |

`viz/export.py` 負責從 Parquet 產出 L0/L1，寫入 `web/public/data/`。

#### 網站章節

| # | route | 章節 | 回答的問題 |
|---|---|---|---|
| 1 | `/trend/` | 長期趨勢與氣象校正 | 監測網擴張與氣象扣除後，下降是否仍成立 |
| 2 | `/stations/` | 測站個別統計 | 每個測站最近哪一年完整到可比較 |
| 3 | `/space/` | 空間結構與官方分區 | 官方分區移除了多少殘差空間相依 |
| 4 | `/sources/` | 污染來向與風速條件 | 高濃度對應哪些方位與風速 |
| 5 | `/detection/` | 事件效應的偵測極限 | 方法能分辨多小的事件效應 |
| 6 | `/forecast/` | 預測技巧與有效期距 | 預測何時不再比簡單基準有用 |
| 7 | `/health/` | 健康負擔與它的假設 | 暴露反應與比較基準如何改變結果 |
| 8 | `/methods/` | 方法選擇的量化代價 | 2018 方法與現行方法逐項相差多少 |
| 9 | `/explore/` | 資料查詢 | 如何在瀏覽器內直接查 Parquet |
| 10 | `/data/` | 資料與方法 | 可下載層級、重建方式、授權與 null 語意 |

首頁負責第一眼看見完整台灣地圖、核心發現與章節入口；各章改成獨立 route，避免早期
單頁版本長達數十個 viewport。視覺採淺色 evidence-first 版面，深色主題另有獨立色階。

> **第 4 章的寫法（重要）**
>
> 章名取「偵測極限」而非提問句，是為了與專案既有的概念對齊：
> 儀器對濃度有偵測極限（資料裡的 `ND` 旗標就是這個），
> **方法對效應也有偵測極限**。這一章量的就是後者。
>
> 這一章碰到的三個事件——空污法修法、台中電廠、COVID——在台灣都帶政治色彩。
> 但本專案沒有立場，也沒有能力評價政策好壞，**而且分析結果本來就不支持那種評價**。
>
> 所以這一章問的是**方法學問題**，不是政治問題：
> 「這種資料、這種方法，能偵測到多大的訊號？」
>
> 實測答案是：安慰劑散布 2.5–3.5 μg/m³，而待測效應 0.5–1.6 μg/m³。
> **噪音底線高於訊號。** 這一章要講的就是這件事，以及沒有安慰劑對照的話，
> 同一批數字看起來會多有說服力（前金 −23.4%！）。
>
> 寫作守則：
> - 主語是**方法**，不是政府。「這個方法測不到」，不是「政策沒用」。
> - **「測不到」≠「等於零」**，每次出現都要講清楚。
> - 台中電廠那筆要說明**處分被撤銷、機組可能從未降載**——
>   否則讀者會把「政策沒發生」讀成「政策失敗」。
> - 不出現政黨、首長姓名、或任何暗示責任歸屬的措辭。
> - 不做「所以政府應該…」的建議。這個專案不提供政策建議。

**驗收**
- [x] 本地可跑 —— `cd web && npm run dev`
- [x] GitHub Actions 部署到 GitHub Pages 成功 —— <https://kuotunyu.github.io/air-quality/>
- [x] 首頁與 10 個主題 route 全部上線，章節順序由 `web/src/lib/chapters.ts` 單一管理
- [x] 第 9 章 DuckDB-WASM 資料探索器 —— 瀏覽器內跑 SQL，無查詢伺服器
- [x] 行動裝置可用 —— 375px 無橫向溢出，全部文字達 WCAG AA
- [x] 深/淺色主題都正常 —— 兩套色階分別定義，非濾鏡

---

### Phase 4 — 氣象正規化 + 事件偵測極限

#### M4 氣象正規化（`analysis/deweather.py`）

實作 Grange et al. (2018) `rmweather` 演算法（R 套件，需**自行以 Python 實作**）：

1. 訓練 RF：`PM2.5 ~ 氣象變數 + unix_time(趨勢) + doy + hour + dow`（300 棵樹）
2. 對每個時間點，**重抽樣**除趨勢外的條件，預測 100 次取平均；這個預設值由收斂量測決定
3. 得到「模型可見的氣象條件標準化後」的濃度序列；剩餘趨勢仍混合排放、傳輸、化學反應與其他未建模因素
4. 用 Theil-Sen 估趨勢，bootstrap 信賴區間

**產出**：全台與各縣市的 raw trend vs deweathered trend 對照。
回答「觀察到的趨勢有多少與模型可見的氣象條件有關」。這不是政策因果歸因；
本地儀器看不到的 BLH 與長程傳輸仍可能留在 normalised series 裡。

#### M5 事件效應與 placebo 偵測（`analysis/causal.py`）

`conf/events.yaml` 建立事件時間軸，至少涵蓋：
- 2018 空污法修法
- 台中電廠燃煤機組降載 / 生煤許可爭議
- 深澳電廠停建（2018-10）
- COVID-19 三級警戒（2021-05-19 ~ 07-26）
- 老舊二行程機車汰換補助
- 六輕歲修期間
- 各縣市自治條例

早期 blueprint 列過 RDiT、合成控制、BSTS 與 DiD。現行交付收斂成一個更窄、
但能被資料支持的問題：在 deweathered daily series 上以 weather-only counterfactual
估計事件窗口，再把結果放進同站、同季節的 placebo-year 分布。這套設計量的是
「目前資料與方法能否把訊號從自然波動中分出來」，不把單一模型包裝成政策因果定論。

**必須誠實報告**：多數政策的效果量會很小或不顯著。這是正確的科學結論，不要為了故事性誇大。

**驗收**
- [x] deweather 模組以合成資料驗證已知斜率、block bootstrap、held-fixed feature 與 resampling 語意
- [x] **已取代**：每個已查證事件輸出 counterfactual effect、信賴區間與 placebo distribution；不把四個未完成的方法名字當成 robustness
- [x] 網站第 1 章趨勢與第 5 章偵測極限上線
- [x] **已取代**：結果由 `m4_deweather`／`m5_causal` Parquet、網站章節與 `docs/methodology.md` 交付，不建立 Quarto 報告

---

### Phase 5 — 空間分析 + 境外傳輸

#### M6 空間（`analysis/spatial.py`）
- **Moran's I / LISA** — 全域與局部空間自相關，找 hot spot / cold spot
- **測站分群** — 以月序列與純地理 ensemble 檢驗官方空品區，讓資料決定支持的粒度
- **空間內插的能力測試** — 以 buffered leave-one-station-out 量測場重建能力；
  實測後拒絕發布超出監測網解析度的 1 km 濃度場
- **人口加權暴露** — 未取得人口網格，因此不發布人口暴露數字，也不預設測站平均偏誤方向

#### M7 境外傳輸（`analysis/trajectory.py`）
- **CPF / CBPF** — conditional (bivariate) probability function 極座標圖，找高濃度對應的風速風向組合 → 污染源方位
- **HYSPLIT／軌跡分群／PSCF** — 需要尚未取得的外部風場與軌跡資料，明確延後；
  現行 M7 只回答監測站風速風向條件支持的「來向」，不宣稱跨境源區
- **交叉驗證** — 用 PM2.5/PM10 ratio、SO2/NOx ratio 作為組成觀測對照以篩選來源假說；PM2.5/PM10 單一比值不能唯一辨識或量化來源

**驗收**
- [x] **已取代**：全台 1 km 濃度場不出；最近鄰 0.6–67 km，1 km 宣稱網絡給不起的解析度，改以緩衝 LOO 實測 `field_skill`
- [x] Moran's I 顯著性、LISA 圖 ✅（M6：Cliff–Ord 殘差虛無、correlogram 變號、LISA BH 後 0/60）
- [ ] **延後**：至少 5 年的後推軌跡資料庫與分群；不阻擋現行 M6／M7 發布
- [x] 網站第 3 章空間結構與第 4 章 CBPF 污染來向上線；沒有資料就不放軌跡動畫
- [x] **已取代**：`reports/03-spatial.md` 由 M6 Parquet 產物支持；repo 不使用 Quarto

**後續風險**：HYSPLIT 不只是部署問題，還需要可重現的風場、起點高度與 archive contract。
若重啟，先寫獨立設計與 validation target；不再用簡化積分器冒充等價替代。

---

### Phase 6 — 衛星 + 微型感測器融合

**交付判定：來源取得已開始，M8 研究交付仍延後。** GEE 註冊與 2025 年 S5P
station-month Stage A 已完成；它只建立可重跑、保留 null 與 provenance 的來源表，
不包含相關性、校正、空間驗證或融合。其餘 blueprint 保留為後續研究，不為了填滿
roadmap 把「能下載」寫成「M8 已完成」。

#### M8（`analysis/satellite.py`）
- **Stage A source acquisition（已交付）** — `twair ingest satellite --year 2025 --months 1:12`
  從 GEE 取得 S5P NO₂／SO₂ 柱濃度到 `data/interim/satellite/year=2025/`；
  嚴格驗證 station-month key，masked sample 保留為 null，負值不裁成零，並輸出 coverage 與 manifest
- **MAIAC Stage A source acquisition（已交付）** — `twair ingest maiac plan|submit|status|import-files`
  2025 年 12 個 batch task 已完成，取得 912 個 station-month row，其中 69 個 masked sample 保留為 null；
  逐月 task、Drive folder bootstrap、durable ledger 與 deterministic description 避免並行競態及中斷後重複送出，
  CSV 只有在完整 station-month key、source contract、checksum 與 task provenance 都通過時才原子合併
- **Sentinel-5P** NO2/SO2 柱濃度 vs 地面測值的相關性與偏差分析
- **MODIS MAIAC AOD → PM2.5** — AOD 與地面 PM2.5 的關係受 BLH 與 RH 調制，建立校正模型
- **微型感測器校正** — 數千個低成本感測器對鄰近標準站做 calibration transfer（含濕度校正），量化校正前後誤差
- **資料融合** — 地面站（準但稀疏）+ 衛星（廣但粗）+ 微感測器（密但雜）→ 高解析度濃度場
  - 方法：geostatistical fusion / RF with satellite covariates / Bayesian hierarchical model

**驗收**
- [x] 2025 S5P NO₂／SO₂ 站月來源表、coverage 與 provenance manifest；這是 M8 的輸入，不是分析結論
- [x] 2025 MAIAC AOD 站月來源表、coverage 與 provenance manifest；null 不補值，AOD 不冒充 PM2.5
- [ ] **延後**：至少 1 年的日 PM2.5 融合產品；解析度必須由獨立驗證決定，不能預設為 1 km
- [ ] **延後**：融合場的留出測站評估
- [ ] **延後**：微感測器校正前後 RMSE 對照
- [ ] **延後**：若重啟本階段，以 Markdown／Parquet／網站章節交付，不新增 Quarto 工具鏈

**下一個條件**：S5P 與 MAIAC Stage A 已排除 credential 與基本取得風險，真實 AOD
站月表也已通過契約驗證。接下來先量測 AOD 與地面 PM2.5 的共同 coverage、相關性與月份／
測站偏差，再決定是否值得投入 BLH、RH、微型感測器與留出測站模型。沒有獨立驗證前，
不發布融合濃度場。

---

### Phase 7 — 預測模型 + Hugging Face Space

#### M9（`src/twair/models/`）

任務：以公開歷史觀測預測各測站 PM2.5 未來 1、6、24、48 小時，並讓每個期距
都先超越 persistence 與 climatology，不能只靠漂亮的 R²。

| 層級 | 模型 |
|---|---|
| Baseline | persistence、station-month-hour climatology |
| 傳統 | M12 **AutoARIMA / SARIMA**，在相同 horizon 對照兩條 baseline |
| ML | M9 LightGBM direct multi-horizon（現行主力） |
| DL／圖模型 | N-HiTS、PatchTST、GNN 是早期 stretch goals，未納入目前 release |

- 統一 backtest 框架（`evaluate.py`）：rolling origin、固定測試期、每個 horizon 分別報告
- 指標：每個 horizon 的 RMSE、R²、skill vs persistence、skill vs climatology，並保留最差 split 與 losing-split count
- **與環境部官方空品預報比較**需要可對齊的歷史 forecast archive，現行資料沒有，因此不做不公平比較

#### HF Space（`spaces/forecast/`）
Gradio 介面：選測站、demo 時刻與 1／6／24／48 小時期距，並排顯示模型、
persistence、climatology 與實際觀測。Tracked app 不含完整資料；model/data bundle 由
`uv run twair export space` 從本機 store 重建。

**驗收**
- [x] **已取代**：不追求未實作的 model zoo；M9 有 4 個 horizon × 4 個 rolling-origin split，M12 另有 SARIMA／baseline 對照
- [x] M9 的 16 個 horizon-split cells 全部勝過 persistence；同時公開 skill 對 climatology 的衰退，不能把「48 小時最有 skill」誤讀成最實用
- [x] HF Space 公開運行，tracked source 與本機可重建 bundle 使用同一組 M9 model parameters
- [x] **已取代**：`data/outputs/m9_forecast/`、網站第 6 章與 `spaces/forecast/README.md` 是 forecast report；不建立 Quarto 檔

---

### Phase 8 — 健康衝擊、收尾、自動化

#### M10 健康衝擊（`analysis/health.py`，加分）
- WHO 2021 annual guideline 與 GBD TMREL endpoints 並列，讓 counterfactual 的選擇可見
- 以有正式來源的 all-cause mortality response function 計算 attributable fraction 與 sensitivity grid
- **不報死亡人數、GEMM、壽命或人口暴露**：repo 沒有人口、年齡結構、基礎死亡率與暴露面，不能用小數位掩飾缺資料

#### 收尾
1. **CI 與 freshness 分工**：`ci.yml` 驗證 commit；`freshness.yml` 每週離線判斷是否漏掉已完整發布的年度；
   資料更新仍是本機、可檢查的 ingest → build → aggregate → export，不讓排程自動改寫 repo 或 HF Dataset
2. **文件**
   - `README.md`（中）/ `README.en.md`：一句話定位、快速開始、主要發現摘要 ✅
   - `docs/methodology.md`：完整方法論 ✅
   - 所有模組的 docstring + `mkdocs` API 文件（選配）
3. **`twair` 發布到 PyPI**（選配，不是目前 release gate）
4. **完整敘事**由首頁與 10 個主題 route 交付；分析細節留在 Markdown 報告與方法文件，
   不再維護內容重複的 `reports/00-full.md`

> **不做學術形式的那一套。** 這是 side project，不是投稿用的研究：
> 沒有 `CITATION.cff`、沒有 Zenodo DOI、不用 Quarto 出 PDF。
> 嚴謹度留著（那是這個專案好玩的地方），行政開銷丟掉。

**驗收**
- [x] **已取代**：CI、weekly freshness 與 Pages workflow 分工，排程只檢查而不 mutation；不以「每日自動 push 7 天」當品質證據
- [x] 首頁與全 10 章上線，沒有 JavaScript 時仍保有主要敘事與圖表
- [ ] **人工發布**：L0／L1 HF Dataset 本機 bundle 與載入 gate 通過後，由 owner 決定上架；完整 L2 不列入發布；HF Space 已公開且 tracked bundle 可重建
- [ ] **人工驗收**：請非本科讀者完成一次 README／首頁試讀；automated quality gate 不能冒充使用者研究
- [ ] **選配**：PyPI 發布；目前以 `uv sync` 與 repo CLI 重現，不阻擋網站／研究 release

---

## 跨階段的硬性規範

1. **可重現**：分析輸出寫入 `data/outputs/<module>/` 的穩定 Parquet contract；報告與網站只讀這些產物，網站 manifest 另記 `git_sha` 與 checksum
2. **不硬編**：所有路徑、URL、參數進 `conf/*.yaml`
3. **Schema 強制**：canonical frame 進出都過 `twair.store.schema` 的 dtype、key、flag 與 duplicate gate，失敗即中止
4. **測試**：`ingest` 用 recorded fixtures（不打真實網路）；`qc` 與 `features` 要有邏輯測試；分析模組用合成資料驗證能還原已知答案
5. **誠實報告**：效果不顯著、模型輸給 baseline、資料有問題——**全部如實寫進報告**。這比漂亮的結果更能證明能力
6. **語言邊界**：README 維持正體中文／英文雙入口；網站以正體中文服務目前受眾；未來 Dataset Card 再提供雙語
7. **合規**：遵守 robots.txt、rate limit（預設 ≥1s/req）、標註來源與授權、不重新散布有授權限制的原始檔

---

## 驗證方式（端到端）

這一段原本寫成 `just …`。**沒有 justfile**——它在計畫裡，實作時由 `twair` CLI
取代了，所以下面是真的可以貼進終端機的版本。

```bash
# 環境
uv sync --all-extras --group dev
uv run python scripts/check_history_identity.py
uv run python scripts/check_repository_anonymity.py
uv run python scripts/check_test_count.py
uv run pytest -q
uv run ruff check .
uv run ruff format .
npm --prefix web run check

# 資料管線（首次會跑很久，用 --years 2024:2025 先試小範圍）
uv run twair ingest airtw && uv run twair build && uv run twair aggregate
duckdb -c "SELECT count(*) FROM 'data/processed/observations/**/*.parquet'"

# 分析
uv run twair analyze m1   # 復刻，比對 reports/_expected/replication_2018.yaml
uv run twair analyze m2  # 逐時重做
uv run twair analyze --help  # M3–M12 依需要逐模組執行；沒有虛構的 `all` 指令

# 網站
uv run twair export web            # 產出 web/public/data/
npm --prefix web run dev           # http://localhost:4321
npm --prefix web run build         # 產出 web/dist/，由 .github/workflows/pages.yml 部署

# 發布
# `uv run twair export space` 重建可部署 bundle；推送 Space 仍是手動外部發布。
# 完整 dataset 尚未上架，待專案收尾與 owner 明確確認。
# 見 docs/legal.md。
```

**關鍵驗收檢查點**
1. Phase 1 結束：`twair status`、逐年 coverage、duplicate-key gate 與 QC report 對同一 store 給出一致狀態
2. Phase 2 結束：M1 復刻的 OLS 係數與原專題 p.49 表格方向一致、量級相近
3. Phase 3 結束：GitHub Pages 網址可公開存取，行動裝置正常
4. Phase 7 結束：主力預測模型在 24h horizon 的 RMSE 優於 persistence baseline

---

## 已識別風險與對策

| 風險 | 對策 |
|---|---|
| airtw Google Drive 連結變動 | 每次執行重新解析 `His_Data.aspx`；file id + checksum 存檔比對 |
| CODiS 爬蟲被擋 | 嚴格 rate limit + 快取；備案改用 opendata API（時間範圍較短）或 ERA5 |
| GEE 大型同步查詢可能逾時 | S5P Stage A 已驗證；MAIAC 改採 batch export／checkpoint，未有可重啟取得流程前不承諾融合產出 |
| ERA5 下載排隊慢 | 背景批次下載；先用單一年份跑通流程 |
| HYSPLIT 需要額外資料與驗證 | 明確延後；重啟時以獨立設計定義風場、起點高度、軌跡真值與 validation target |
| 逐時 3.40 億筆記憶體撐不住 | Polars lazy + DuckDB out-of-core；分區處理 |
| 範圍過大爛尾 | 每階段都要能單獨發布；Phase 6 已明確延後，不為了順序阻擋有資料支持的 M9–M12 |
| 測站改名/搬遷造成序列斷裂 | `conf/stations.yaml` 維護別名與有效期；斷裂處在報告中明確標註 |

---

## 最小可行版本（若時間不足）

**Phase 0 + 1 + 2 + 3 就已經是一個完整、可以拿出去的作品**：
可追溯的 canonical store與公開 L0/L1 + 嚴謹的分析重做 + 方法學對照的互動網站。
目前已超過這個邊界；尚未完成的 HF Dataset 與外部加值資料應獨立發布，
**不要讓尚未取得的資料把已量測成果寫成「未完成」**。
