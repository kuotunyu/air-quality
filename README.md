# air-quality｜台灣空氣品質再分析

[![CI](https://github.com/kuotunyu/air-quality/actions/workflows/ci.yml/badge.svg)](https://github.com/kuotunyu/air-quality/actions/workflows/ci.yml)

> 1982–2025 年環境部年度檔裡的全台逐時原始觀測：
> 3.40 億筆資料，標記品質而不悄悄修補，
> 附上完整開源的管線與一個互動網站。

### 台灣的 PM2.5 在 2008–2025 年間降了 60%；氣象正規化把其中 43% 歸於模型看得見的氣象差異。

把氣象條件正規化之後量出來的——61 個測站、同一批資料、兩條線。
用另一種完全不同的聚合方式（逐站斜率比值的中位數）再問一次，答案是 42.2%。
這是模型分解，不是政策或排放的因果歸因；本地儀器看不到的 BLH 與長程傳輸仍是限制。
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
[docs/working-rules.md](docs/working-rules.md)。

## 五項產出

| | 內容 |
|---|---|
| 📦 **開源資料集** | 1982–2025 全測站的 L0 站月與 L1 站日衍生統計，可從[網站第十章](https://kuotunyu.github.io/air-quality/data/)直接下載，也可封裝成 Hugging Face Dataset。完整 L2 逐時複本不發布；任何人可用公開管線與上游資料重建 |
| 📊 **可重現研究** | 從復刻 2018 年結果開始，逐項修正並量化差異 |
| 🌐 **互動網站** | 趨勢、個人化暴露報告、污染來源方位、事件偵測極限、方法學對照 |
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
| [Copernicus ERA5](https://cds.climate.copernicus.eu/) | **邊界層高度**、風場、氣壓 | 1982– | ⬜ **尚未取得** |
| [Sentinel-5P TROPOMI](https://developers.google.com/earth-engine/datasets/catalog/COPERNICUS_S5P_OFFL_L3_NO2) | NO₂ 對流層柱濃度、SO₂ 垂直柱濃度 | 2018– | 🟡 2025 站月來源取得已實作；M8 分析與融合尚未完成 |
| [MODIS MAIAC](https://developers.google.com/earth-engine/datasets/catalog/MODIS_061_MCD19A2_GRANULES) | 氣膠光學厚度 | 2000– | ⏸ 同步查詢 pilot 逾時；批次取得與校正研究待辦 |

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

`probe sources` 會實地解析 airtw 年度目錄與當前下載連結、抓取一個真實 archive 樣本，
並把結果寫進 `conf/sources.yaml` 與 `docs/data-sources.md`；需要憑證的加值來源若未設定，
會明確保留為未驗證。
政府網站的連結會變動，所以這一步每次都重新解析，不寫死任何 URL。

## 專案狀態

目前磁碟與產物的可量測狀態，請先執行 `uv run twair status`。後續方向見
[PLAN.md](PLAN.md)；可重用的實測證據與穩定決策見相關的 [docs/](docs/) 技術文件。

| 階段 | 目前交付 | 交付判定 |
|---|---|---|
| Phase 0 | 專案骨架、airtw live probe、真實跨世代樣本與資料源文件 | ✅ 核心完成；需憑證的加值源延後 |
| Phase 1 | 1982–2025 Canonical Parquet、QA/QC、coverage-aware aggregates | ✅ 完成；L0／L1 Dataset bundle 可本機重建，遠端上架另行人工確認 |
| Phase 2 | M1 復刻、M2 逐時重做、M3 方法學對照與核心報告 | ✅ 完成 |
| Phase 3 | 首頁、10 個主題 route、build-time SVG、DuckDB-WASM | ✅ 完成 |
| Phase 4 | M4 氣象正規化、M5 counterfactual + placebo 偵測極限 | ✅ 有界完成，不宣稱政策因果 |
| Phase 5 | M6 空間結構、M7 CBPF 污染來向 | ✅ 有界完成；HYSPLIT／1 km 場延後 |
| Phase 6 | 衛星與微型感測器融合 | 🟡 S5P 來源取得 Stage A 已交付；分析、MAIAC 與融合仍延後，不阻擋目前 release |
| Phase 7 | M9 四期距 forecast、M12 SARIMA、公開 HF Space | ✅ 完成；DL／GNN stretch goals 不納入 |
| Phase 8 | M10 健康假設、CI、weekly freshness、完整網站敘事 | 🔄 收尾：HF Dataset 與人工讀者試讀待辦；PyPI 選配 |

完整計畫見 [PLAN.md](PLAN.md)。磁碟上的實際狀態用 `uv run twair status` 看——
這份表寫的是 release 邊界；那個指令量的是本機事實。原始 blueprint 與每項
「已取代／延後」的理由都保留在 [PLAN.md](PLAN.md)。

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
