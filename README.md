# air-quality｜台灣空氣品質再分析

[![CI](https://github.com/kuotunyu/air-quality/actions/workflows/ci.yml/badge.svg)](https://github.com/kuotunyu/air-quality/actions/workflows/ci.yml)

> 1982 年至今、全台每一個測站、每一個小時的原始觀測：
> 3.41 億筆資料，標記品質而不悄悄修補，
> 附上完整開源的管線與一個互動網站。

### 台灣的 PM2.5 在 2008–2025 年間降了 60%。其中 43% 是天氣，不是減排。

把氣象條件正規化之後量出來的——61 個測站、同一批資料、兩條線。
用另一種完全不同的聚合方式（逐站斜率比值的中位數）再問一次，答案是 42.2%。
[看那張圖 →](https://kuotunyu.github.io/air-quality/#trend)

[English](README.en.md) ·
[互動網站](https://kuotunyu.github.io/air-quality/) ·
[預測 demo](https://huggingface.co/spaces/steven0226/airlens-taiwan-forecast) ·
資料集 —— *尚未上架* ·
[方法論](docs/methodology.md)

---

## 這是什麼

一份涵蓋 **44 年**、經過完整品管的台灣空氣品質開放再分析：
1982 到 2025 年，82 個測站，**341,442,552 筆逐時觀測**。產出三件事：

1. **一份原本不存在的資料集。** 環境部有公開年度原始檔，但橫跨四種封裝格式、
   兩種日期順序，品質旗標的慣例還逐年在變。本專案把它們全部解析成
   單一份標準化、逐列可追溯來源的資料儲存。
2. **一組方法學對照。** 一份 2018 年的大學畢業專題用月平均（N = 7,286）、
   逐步剔除的 OLS 與 AR(1) 混合模型分析了其中一段資料。
   把那套方法與修正後的方法放在**同一份資料**上並排重跑，就能算出每個選擇值多少。
3. **一個可以自己驗算的網站**，包含在你自己瀏覽器裡跑 SQL 的資料探索器。

### 貫穿全案的原則

**測量並公布資料品質，絕不悄悄修補。** 無效值標記但不刪除；超出物理範圍的值
保留原數字以供檢視；覆蓋率不足的聚合直接回傳 **null**，而不是一個有偏誤的平均。
網站上每一條線的中斷都是真的中斷。

### 關於那份 2018 年的專題

它是起點，不是主題。**本專案不提及原專題的任何作者或指導教授姓名，
也不重製它的任何圖表**——只使用它的「方法選擇」（方法學事實）與
「已公布的數值」（可獨立驗證）。原始 PDF 已列入 `.gitignore`，不隨程式碼發布。

其中兩個選擇的代價很大：

1. **用 PM10 去預測 PM2.5。** PM2.5 在定義上就是 PM10 的子集，這是定義上的重疊，
   不是實證發現。實測代價：**含 PM10 的模型有 32.1% 的 R² 來自這個重疊**（0.524 → 0.772）。
2. **把風向（0–360°）當成線性連續變數。** 0° 與 359° 在物理上相鄰，數值上卻相距 359。
   在 OLS 之下，sin/cos 編碼的 R² 是原始方位角的 **2.55 倍**。

另外有**兩項我原先列為缺陷、卻被全量資料否證**的說法，一樣照實記錄——見
[PROGRESS.md](PROGRESS.md)。

## 四項產出

| | 內容 |
|---|---|
| 📦 **開源資料集** | 1982–今 全測站逐時空品觀測 + 氣象，含原始品質旗標，發布於 HuggingFace |
| 📊 **可重現研究** | 從復刻 2018 年結果開始，逐項修正並量化差異 |
| 🌐 **互動網站** | 趨勢、個人化暴露報告、污染來源方位、事件偵測極限、方法學對照 |
| 🔮 **預測 demo** | [HuggingFace Space](https://huggingface.co/spaces/steven0226/airlens-taiwan-forecast) — PM2.5 未來 1–48 小時，模型 vs 兩條基準線 |
| 🔧 **Python 套件** | `twair` — 資料管線與分析工具 |

## 資料來源

全部為政府公開資料，依《政府資料開放授權條款第 1 版》使用。

| 來源 | 內容 | 期間 | 狀態 |
|---|---|---|---|
| [環境部 空氣品質監測網](https://airtw.moenv.gov.tw/cht/Query/His_Data.aspx) | 全年逐時原始觀測 | 1982–2025 | ✅ **全部 44 年已取得**，本專案所有結果都出自這裡 |
| [環境部 環境資料開放平臺](https://data.moenv.gov.tw/) | 測站座標、即時 AQI | 即時 | ✅ 使用中（測站登錄、資料新鮮度檢查） |
| [中央氣象署 開放資料平臺](https://opendata.cwa.gov.tw/) | 氣象站逐時觀測 | 近期 | ⬜ **尚未取得** |
| [Copernicus ERA5](https://cds.climate.copernicus.eu/) | **邊界層高度**、風場、氣壓 | 1982– | ⬜ **尚未取得** |
| [Sentinel-5P / MODIS](https://earthengine.google.com/) | 衛星柱濃度、氣膠光學厚度 | 2018– | ⬜ **尚未取得**（Phase 6，建議略過） |

氣象變數目前**全部來自空品測站自己的儀器**（溫度、濕度、雨量、風速風向），
不是氣象署，也不是再分析資料。

> **邊界層高度（BLH）是原專題最關鍵的缺失變數——而它現在也是本專案的缺失變數。**
> 污染物濃度大致正比於排放量除以混合層體積，少了 BLH，任何「氣象如何影響 PM2.5」
> 的討論都缺一塊。這一塊量得出來：M4 氣象正規化的 holdout R² 中位數是 **0.445**，
> 也就是逐時變異有一半以上不是本地氣象能解釋的。把 BLH 補進去是這個專案
> 最有價值的下一步之一，但它還沒發生，所以這裡照實寫。

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

`probe sources` 會實地連線各資料源、解析出當前有效的下載連結、抓取小樣本，
並把結果寫進 `conf/sources.yaml` 與 `docs/data-sources.md`。
政府網站的連結會變動，所以這一步每次都重新解析，不寫死任何 URL。

## 專案狀態

目前進度見 **[PROGRESS.md](PROGRESS.md)**。

| 階段 | 內容 | 狀態 |
|---|---|---|
| Phase 0 | 專案骨架、資料源盤點 | ✅ 完成 |
| Phase 1 | 資料取得、QA/QC、Canonical Parquet | ✅ 完成 |
| Phase 2 | 復刻 2018 專題 → 逐時重做 → 方法學對照 | ✅ 完成 |
| Phase 3 | 互動網站首波上線 | ✅ 完成 |
| Phase 4 | 氣象正規化、事件效應偵測 | ✅ 完成 |
| Phase 5 | 境外傳輸（CBPF）✅ ／ 空間自相關 ⬜ | 🔄 部分完成 |
| Phase 6 | 衛星與微型感測器融合 | ⬜ |
| Phase 7 | 預測模型（M9）✅ ／ HuggingFace Space ✅ | ✅ 完成 |
| Phase 8 | 健康衝擊（M10）✅ ／ 資料新鮮度檢查 ✅ ／ 完整報告 ⬜ | 🔄 進行中 |

完整計畫見 [PLAN.md](PLAN.md)。磁碟上的實際狀態用 `uv run twair status` 看——
這份表寫的是意圖，那個指令量的是事實。

## 網站

```bash
uv run twair export web                 # 從 Parquet 產生網站的資料層
cd web && npm install && npm run dev    # http://localhost:4321
```

Astro 靜態網站，深淺兩色主題，沒有繪圖套件——圖表在建置時產生 SVG，
所以沒有 JavaScript 也看得到、可以列印。細節見 [web/README.md](web/README.md)。

已上線：**<https://kuotunyu.github.io/air-quality/>**，由 `.github/workflows/pages.yml` 在推送到 `master`
且動到 `web/` 時自動建置部署。

CI 沒有那 3.41 億列資料庫的複本，所以**更新網站的資料是本機步驟加一次 commit**——
`uv run twair export web` 之後把 `web/public/data/` 一起提交。

## 授權

- 程式碼：[MIT](LICENSE)
- 資料衍生物：[CC BY 4.0](LICENSE-DATA)，原始資料出處為中華民國環境部與交通部中央氣象署

## 引用

這是個人的 side project，沒有 DOI 也沒有正式引用格式。
要引用的話，連結到這個 repo 就夠了；資料來源請一併註明「中華民國環境部」。
