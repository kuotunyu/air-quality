# AirLens Taiwan｜台灣空氣品質再分析

> 用 1982 年至今、全台每一個測站、每一個小時的原始觀測資料，
> 重做一份 2018 年的大學畢業專題——並誠實說明當年錯在哪裡。

[English](README.en.md) ·
[互動網站](https://OWNER.github.io/taiwan-air-quality/) ·
[資料集](https://huggingface.co/datasets/OWNER/taiwan-air-quality) ·
[方法論](docs/methodology.md)

---

## 這是什麼

2018 年，我的大學畢業專題《台灣地區之 PM2.5 之影響分析》用環保署 2010–2017 年的資料，
把逐時觀測**聚合成月平均**（N = 7,286），跑了迴歸、混合模型與 ARIMA。

以今天的標準，那份報告有幾個足以推翻結論的問題。最嚴重的兩個：

1. **用 PM10 去預測 PM2.5。** PM2.5 在定義上就是 PM10 的子集，這是資訊洩漏，
   不是發現。模型的解釋力幾乎全由這一項撐起。
2. **把風向（0–360°）當成線性連續變數。** 0° 與 359° 在物理上相鄰，數值上卻相距 359。

這個專案不迴避那些錯誤，而是把它們當成主題：**用同一份資料，證明修正方法後結論會不同。**

同時，它順手產出一件台灣目前沒有的東西——一份乾淨、帶品質標記、涵蓋完整歷史的
開源空氣品質資料集。

## 四項產出

| | 內容 |
|---|---|
| 📦 **開源資料集** | 1982–今 全測站逐時空品觀測 + 氣象，含原始品質旗標，發布於 HuggingFace |
| 📊 **可重現研究** | 從復刻 2018 年結果開始，逐項修正並量化差異 |
| 🌐 **互動網站** | 趨勢、個人化暴露報告、污染來源方位、政策效果、方法學對照 |
| 🔧 **Python 套件** | `twair` — 資料管線與分析工具 |

## 資料來源

全部為政府公開資料，依《政府資料開放授權條款第 1 版》使用。

| 來源 | 內容 | 期間 |
|---|---|---|
| [環境部 空氣品質監測網](https://airtw.moenv.gov.tw/cht/Query/His_Data.aspx) | 全年逐時原始觀測（**已取得全部 44 年**） | 1982–2025 |
| [環境部 環境資料開放平臺](https://data.moenv.gov.tw/) | 測站基本資料、即時 AQI（每日增量） | 即時 |
| [中央氣象署 開放資料平臺](https://opendata.cwa.gov.tw/) | 氣象站逐時觀測 | 近期 |
| [Copernicus ERA5](https://cds.climate.copernicus.eu/) | **邊界層高度**、風場、氣壓 | 1982– |
| [Sentinel-5P / MODIS](https://earthengine.google.com/) | 衛星柱濃度、氣膠光學厚度 | 2018– |

> 邊界層高度（BLH）是原專題最關鍵的缺失變數。污染物濃度大致正比於
> 排放量除以混合層體積——少了 BLH，任何「氣象如何影響 PM2.5」的討論都缺一塊。

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
| Phase 4 | 氣象正規化、政策因果推論 | ⬜ 下一步 |
| Phase 5 | 空間分析、境外傳輸軌跡 | ⬜ |
| Phase 6 | 衛星與微型感測器融合 | ⬜ |
| Phase 7 | 預測模型、HuggingFace Space | ⬜ |
| Phase 8 | 健康衝擊、自動更新、發布 | ⬜ |

完整計畫見 [PLAN.md](PLAN.md)。

## 網站

```bash
uv run twair export web                 # 從 Parquet 產生網站的資料層
cd web && npm install && npm run dev    # http://localhost:4321
```

Astro 靜態網站，深淺兩色主題，沒有繪圖套件——圖表在建置時產生 SVG，
所以沒有 JavaScript 也看得到、可以列印。細節見 [web/README.md](web/README.md)。

尚未部署：需要先建立 GitHub repository，並在 Settings → Pages
把來源設為 GitHub Actions。工作流程已備妥於 `.github/workflows/pages.yml`。

## 授權

- 程式碼：[MIT](LICENSE)
- 資料衍生物：[CC BY 4.0](LICENSE-DATA)，原始資料出處為中華民國環境部與交通部中央氣象署

## 引用

引用格式與 DOI 將於 v1.0 發布時提供，見 `CITATION.cff`。
