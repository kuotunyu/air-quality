# air-quality｜台灣空氣品質再分析

[![CI](https://github.com/kuotunyu/air-quality/actions/workflows/ci.yml/badge.svg)](https://github.com/kuotunyu/air-quality/actions/workflows/ci.yml)

> 1982–2025 年環境部年度檔裡的全台逐時原始觀測：
> 3.40 億筆資料，標記品質而不悄悄修補，
> 附上完整開源的管線與一個互動網站。

### 台灣的 PM2.5 在 2008–2025 年間降了 60%；氣象正規化把其中 43% 歸於模型看得見的氣象差異。

把氣象條件正規化之後量出來的——61 個測站、同一批資料、兩條線。
用另一種完全不同的聚合方式（逐站斜率比值的中位數）再問一次，答案是 42.2%。
這是模型分解，不是政策或排放的因果歸因；已發布的 M4 尚未使用 ERA5 BLH，長程傳輸也仍是限制。
[看那張圖 →](https://kuotunyu.github.io/air-quality/trend/)

[English](README.en.md) ·
[互動網站](https://kuotunyu.github.io/air-quality/) ·
[預測 demo](https://huggingface.co/spaces/steven0226/airlens-taiwan-forecast) ·
資料集 —— *尚未上架* ·
[方法論](docs/methodology.md)

---

## 這是什麼

一份涵蓋 **44 年**、經過完整品管的台灣空氣品質開放再分析：
1982 到 2025 年，82 個測站，**340,371,384 筆逐時觀測**。產出三件事：

1. **一份原本不存在的資料集。** 環境部有公開年度原始檔，但橫跨四種封裝格式、
   兩種日期順序，品質旗標的慣例還逐年在變。本專案把它們全部解析成
   單一份標準化、逐列可追溯來源的資料儲存。
2. **一組方法學對照。** 月平均、逐步剔除的 OLS、用 PM10 預測 PM2.5——
   這些是台灣空品分析用了一整個世代的做法。把它們與修正後的方法放在
   **同一份資料**上並排重跑，就能算出每個選擇值多少。
3. **一個可以自己驗算的網站**，包含在你自己瀏覽器裡跑 SQL 的資料探索器。

### 貫穿全案的原則

**測量並公布資料品質，絕不悄悄修補。** 無效值標記但不刪除；超出物理範圍的值
保留原數字以供檢視；覆蓋率不足的聚合直接回傳 **null**，而不是一個有偏誤的平均。
網站上每一條線的中斷都是真的中斷。

### 對照組是方法，不是任何人

有缺陷的那一邊不是描述，是 `analysis/baseline.py` **實際配適出來的模型**——
所以兩邊跑在同一批列上，每個代價都是量出來的。其中兩個選擇的代價很大：

1. **用 PM10 去預測 PM2.5。** PM2.5 在定義上就是 PM10 的子集，這是定義上的重疊，
   不是實證發現。實測代價：**含 PM10 的模型有 32.1% 的 R² 來自這個重疊**（0.524 → 0.772）。
2. **把風向（0–360°）當成線性連續變數。** 0° 與 359° 在物理上相鄰，數值上卻相距 359。
   在 OLS 之下，sin/cos 編碼的 R² 是原始方位角的 **2.55 倍**。

另外有**兩項我原先列為缺陷、卻被全量資料否證**的說法，一樣照實記錄——見
[docs/working-rules.md](docs/working-rules.md)。

## 五項產出

| | 內容 |
|---|---|
| 📦 **開源資料集** | 1982–2025 全測站的 L0 站月與 L1 站日衍生統計，可從[網站第十章](https://kuotunyu.github.io/air-quality/data/)直接下載，也可封裝成 Hugging Face Dataset。完整 L2 逐時複本不發布；任何人可用公開管線與上游資料重建 |
| 📊 **可重現研究** | 有缺陷的基準在這裡實際配適，逐項修正並量化差異 |
| 🌐 **互動網站** | 趨勢、個人化暴露報告、高值時段的風速／風向型態（不識別來源身分、位置、傳輸距離或貢獻）、事件偵測極限、方法學對照 |
| 🔮 **預測 demo** | [Hugging Face Space](https://huggingface.co/spaces/steven0226/airlens-taiwan-forecast) — PM2.5 未來 1–48 小時，模型 vs 兩條基準線 |
| 🔧 **Python 套件** | `twair` — 資料管線與分析工具 |

## 資料來源

環境部與中央氣象署資料依《政府資料開放授權條款第 1 版》使用；
Copernicus 衛星資料另依其 [Sentinel Data Legal Notice](https://sentinels.copernicus.eu/documents/247904/690755/Sentinel_Data_Legal_Notice)。
各來源的授權與再散布邊界見 [docs/legal.md](docs/legal.md)。

| 來源 | 內容 | 期間 | 狀態 |
|---|---|---|---|
| [環境部 空氣品質監測網](https://airtw.moenv.gov.tw/cht/Query/His_Data.aspx) | 全年逐時原始觀測 | 1982–2025 | ✅ **全部 44 年已取得**，本專案所有結果都出自這裡 |
| [環境部 環境資料開放平臺](https://data.moenv.gov.tw/) | 測站座標、即時 AQI | 即時 | ✅ 使用中（測站登錄、資料新鮮度檢查） |
| [中央氣象署 開放資料平臺](https://opendata.cwa.gov.tw/) | 氣象站逐時觀測 | 近期 | ⬜ **尚未取得** |
| [Copernicus ERA5](https://cds.climate.copernicus.eu/) | **邊界層高度**、10m 風、2m 溫度／露點、地面氣壓 | 1940– | ✅ 2024–2025 年來源取得與多年度／留出測站 robustness 已完成；校正尚未交付 |
| [Sentinel-5P TROPOMI](https://developers.google.com/earth-engine/datasets/catalog/COPERNICUS_S5P_OFFL_L3_NO2) | NO₂ 對流層柱濃度、SO₂ 垂直柱濃度 | 2018– | 🟡 2024–2025 站月來源、M8 關聯與 multi-year predictive robustness 已交付；校正／融合未做 |
| [MODIS MAIAC](https://developers.google.com/earth-engine/datasets/catalog/MODIS_061_MCD19A2_GRANULES) | 氣膠光學厚度 | 2000– | 🟡 2024–2025 站月來源、M8 關聯與 multi-year predictive robustness 已交付；AOD 校正／融合未做 |

<details>
<summary><b>衛星與 ERA5 的 held-out predictive value —— 逐 fold 數字與範圍限制</b>（點開）<br>
兩者都在 2025 年內的留出評估上顯示增量預測資訊；<b>都不是</b>因果歸因、衛星 PM2.5 校正、
融合場，也不是 M4 的替代。校正與融合仍然延後。</summary>

<br>

2025 M8 關聯與 held-out predictive-value 診斷已交付。predictive-value 實驗使用
851 筆共同完整站月、76 站、12 個月份；all-satellite 相對只含月份週期與測站地理的
baseline，在 held-quarter、held-station、joint transfer 分別有 3／4、9／10、37／40 個
fold 同時改善 RMSE 與 R²。三種設計合併後，all-satellite、AOD、NO₂、SO₂ 分別有
49／54、44／54、48／54、25／54 個 fold 同時改善；SO₂ 另有 29／54 個 fold 變差。
all-satellite 的整體 median ΔRMSE 為 −0.588 µg/m³、median ΔR² 為 +0.147。
這 54 個 fold-evaluations 是同一批 851 筆資料在三種設計下的重複評估，不是 54 個獨立年份或測站，
也不是未來年度 transfer。結果只支持 2025 年內的 held-out predictive value；不是因果、校正、
融合或 M4 replacement，校正與融合仍延後。

後續 satellite multi-year robustness 使用 76 個共同測站，2024／2025 分別量得
848 / 851 common complete station-months，所有 baseline／candidate 比較都配對同一批 test rows。
按 all/AOD/NO₂/SO₂ 順序，同年度 quarter replication 在 2024 同時改善 RMSE 與 R² 的 fold 數為
3/4, 4/4, 3/4, 3/4，2025 為 3/4, 3/4, 2/4, 1/4；其餘 fold 對兩項指標同時變差。
真正的 future-year `2024_to_2025` 對四組特徵是
forward: improve / improve / improve / improve，all-satellite 的配對結果為
−0.378 µg/m³ / +0.057 R²。`2025_to_2024` 是反向複驗，不是預測過去；四組結果是
reverse: improve / improve / worsen / worsen，all-satellite 為 −0.179 µg/m³ / +0.024 R²。
再同時留出測站與年份時，`2024_to_2025` 的改善 fold 數為 10/10, 10/10, 9/10, 6/10，
`2025_to_2024` 為 10/10, 10/10, 9/10, 7/10。這是 predictive robustness only；
not causal, calibration, fusion, satellite-estimated PM2.5，且 not a spatial-resolution claim or an M4 replacement。

ERA5 2025 來源取得已交付：77 站、8,760 小時、674,520 筆 station-hour，
六個來源變數皆為 0 個 null。獨立 value-add 實驗再以 74 站的 632,760 筆共同完整
station-hour、三個 forward local-time folds，比較 temporal-only、站內氣象、ERA5 氣象與 combined
四種資訊集。combined 相對站內氣象在 205／222 個 station-fold 同時改善 RMSE 與 R²；
median RMSE delta 為 −0.758 µg/m³，median R² delta 為 +0.249。

後續 robustness 以相同 74 站量得 2024 年 636,244 筆、2025 年 632,760 筆
paired rows。combined 相對站內氣象在跨年同站、2025 同年留站、跨年留站三種 transfer
設計中，分別有 63／74、66／74、70／74 個測站同時改善 RMSE 與 R²；同年 replication
在 2024、2025 年則為 177／222、205／222 個 station-time-fold。ERA5-only 相對站內氣象
也分別改善 59／74、64／74、72／74 個 transfer 測站，顯示主要增量不是單純來自 feature 變多。

三重、淡水、陽明有 PM2.5 target，但兩年都沒有四組資訊集可共同使用的完整 rows，
因此沒有被塞值或強行納入。spatial transfer 使用同年度資料，只量測留出測站的
predictive generalisation；整組結果不是因果歸因、校正或融合。ERA5 尚未納入已發布的 M4；
網站上的氣象正規化仍只使用站內觀測。

</details>

## 快速開始

```bash
uv sync
```

```bash
cp .env.example .env
```

```bash
uv run twair doctor
```

```bash
uv run twair probe sources
```

從 source checkout 執行時，repository root 就是 workspace。若從安裝好的
wheel 執行，可在啟動前以 process environment 設定 `TWAIR_WORKSPACE_DIR`；未設定時
使用目前工作目錄。相對的 `data/`、`.env`、`conf/`、報告與 probe 輸出都留在這個
外部 workspace。`conf/*.yaml` 若不存在，程式會讀取 wheel 內隨版本審查過的
read-only defaults；refresh 或 probe 只會建立 workspace override，不會改寫安裝套件。

`probe sources` 會實地解析 airtw 年度目錄與當前下載連結、抓取一個真實 archive 樣本，
並把結果寫進 `conf/sources.yaml` 與 `docs/data-sources.md`；需要憑證的加值來源若未設定，
會明確保留為未驗證。
政府網站的連結會變動，所以這一步每次都重新解析，不寫死任何 URL。

## 專案狀態

目前磁碟與產物的可量測狀態，請先執行 `uv run twair status`。公開 release boundary
與後續方向見下表；可重用的實測證據與穩定決策見相關的 [docs/](docs/) 技術文件。

| 階段 | 目前交付 | 交付判定 |
|---|---|---|
| Phase 0 | 專案骨架、airtw live probe、真實跨世代樣本與資料源文件 | ✅ 核心完成；GEE 衛星 Stage A 已交付；ERA5 2024–2025 來源取得與多年度／留出測站 robustness 已交付；CWA 延後 |
| Phase 1 | 1982–2025 Canonical Parquet、QA/QC、coverage-aware aggregates | ✅ 完成；L0／L1 Dataset bundle 可本機重建，遠端上架另行人工確認 |
| Phase 2 | M1 基準、M2 逐時重做、M3 方法學對照與核心報告 | ✅ 完成 |
| Phase 3 | 首頁、10 個主題 route、build-time SVG、DuckDB-WASM | ✅ 完成 |
| Phase 4 | M4 氣象正規化、M5 counterfactual + placebo 偵測極限 | ✅ 有界完成，不宣稱政策因果 |
| Phase 5 | M6 空間結構、M7 CBPF 高值時段的風速／風向型態（不識別來源身分、位置、傳輸距離或貢獻） | ✅ 有界完成；HYSPLIT／1 km 場／人口加權暴露延後（repo 無人口網格） |
| Phase 6 | 衛星、ERA5 與微型感測器加值 | 🟡 S5P 與 MAIAC 來源取得 Stage A 已交付；2025 M8 關聯與 held-out predictive-value 診斷已交付；ERA5 2024–2025 robustness 已交付；微型感測器 2025-01 觀測、readiness 與 grouped predictive benchmark 已交付，一月 reference-station satellite-context predictive-value limit 已交付，微型感測器 2025 全年 readiness audit 已交付，Q4-supported cross-station agreement 亦已交付（29 個 fold 中只有 5 個可評分，其餘 18 個測試集為空、6 個訓練集為空，均標為 unscored 而非以零計；held-quarter 與 joint station-quarter 不可估計）；validated calibration 與融合仍延後；不是因果、不是校正，也不是衛星推估 PM2.5 |
| Phase 7 | M9 四期距 forecast、M12 SARIMA、公開 HF Space | ✅ 完成；DL／GNN stretch goals 不納入 |
| Phase 8 | M10 健康假設、CI、weekly freshness、完整網站敘事 | 🔄 發布收尾：正常工程與編輯式科學圖集 UI 已完成，並已整合至 `master`、部署於 GitHub Pages。L0／L1 HF Dataset 仍留到最後由 owner 決定上架或明確不發布；非本科讀者試讀延後且不阻擋發布；PyPI 選配。 |

磁碟上的實際狀態用 `uv run twair status` 看——這份表寫的是 release 邊界；
那個指令量的是本機事實。已實作的工程取捨見
[docs/working-rules.md](docs/working-rules.md)，資料與方法的現行證據分別見
[docs/data-sources.md](docs/data-sources.md) 與 [docs/methodology.md](docs/methodology.md)。

## 網站

```bash
uv run twair export web                 # 從 Parquet 產生網站的資料層
cd web && npm install && npm run dev    # http://localhost:4321
```

Astro 靜態網站，深淺兩色主題，沒有繪圖套件——圖表在建置時產生 SVG，
所以沒有 JavaScript 也看得到、可以列印。細節見 [web/README.md](web/README.md)。

已上線：**<https://kuotunyu.github.io/air-quality/>**，由 `.github/workflows/pages.yml` 在推送到 `master`
且動到 `web/` 時自動建置部署。

CI 沒有那 3.40 億列資料庫的複本，所以**更新網站的資料是本機步驟加一次 commit**——
`uv run twair export web` 之後把 `web/public/data/` 一起提交。

## 授權

- 程式碼：[MIT](LICENSE)
- 資料衍生物：[CC BY 4.0](LICENSE-DATA)，原始資料出處為中華民國環境部
  （地圖的縣市界線來自內政部國土測繪中心，依政府資料開放授權條款第 1 版）

## 引用

這是個人的 side project，沒有 DOI 也沒有正式引用格式。
要引用的話，連結到這個 repo 就夠了；資料來源請一併註明「中華民國環境部」。
