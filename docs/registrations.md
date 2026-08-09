# 需要你親自申請的帳號與金鑰

這些都要本人註冊，我無法代辦。全部免費。

申請完把金鑰填進 `.env`（從 `.env.example` 複製），然後執行：

```bash
uv run twair doctor
```

## 優先順序

**Phase 1 完全不需要任何金鑰**，airtw 年度資料包是公開直接下載。

驗證已設定的金鑰（會實際呼叫各家 API，不只檢查有沒有填）：

```bash
uv run twair doctor
```

> **注意**：CWA 的 API 把授權碼放在網址參數。`twair doctor` 會在檢查期間
> 壓低 httpx 的 log 層級以免金鑰被寫進終端機或記錄檔，
> 但若你自行用 `curl` 或其他工具測試，金鑰仍會留在 shell 歷史裡。

| 階段 | 需要 | 用途 |
|---|---|---|
| Phase 1 | 無 | airtw 年度包無須驗證 |
| Metadata／維運 | `MOENV_API_KEY` | 重抓測站基本資料、唯讀 freshness 檢查 |
| 延後的氣象加值 | `CWA_API_KEY` | 氣象站觀測 |
| 延後的氣象加值 | `CDSAPI_KEY` | ERA5 邊界層高度 |
| Phase 6 Stage A | `GEE_PROJECT_ID` | S5P 站月來源取得；進階 MAIAC／融合仍可延後 |
| 發布收尾 | `HF_TOKEN` | Hugging Face Dataset 與 Space 更新 |

---

## 1. 環境部 環境資料開放平臺 — `MOENV_API_KEY`

<https://data.moenv.gov.tw/>

1. 右上角「登入／註冊」→ 註冊會員
2. 完成 email 驗證
3. **API 金鑰會寄到註冊信箱**，不在網站上顯示 —— 請保留該封信件
4. 填入 `.env` 的 `MOENV_API_KEY`

驗證方式：

```bash
uv run twair doctor
```

> 別用 `curl` 手動測。MOENV 跟 CWA 一樣把金鑰放在網址參數裡，
> 打在命令列上就會留在 shell 歷史，而那正是上面那段警告講的事——
> 這份文件以前自己在這裡給了一個 `curl` 範例。`doctor` 會實際連線驗證，
> 並且把請求包在 `net.quiet_http` 裡，金鑰不會進終端機或記錄檔。

## 2. 中央氣象署 開放資料平臺 — `CWA_API_KEY`

<https://opendata.cwa.gov.tw/>

1. 註冊並登入
2. 進入「會員資訊」→「API 授權碼」→「取得授權碼」
3. 授權碼即時產生，形如 `CWA-XXXXXXXX-...`
4. 填入 `.env` 的 `CWA_API_KEY`

API 文件：<https://opendata.cwa.gov.tw/dist/opendata-swagger.html>

## 3. Copernicus Climate Data Store — `CDSAPI_KEY`

<https://cds.climate.copernicus.eu/>

1. 註冊 ECMWF 帳號並登入
2. 進入個人頁面取得 API Token
3. **必須逐一接受每個資料集的授權條款**，否則下載會回 403：
   前往 ERA5 資料集頁面 → 「Terms of use」→ Accept
4. 填入 `.env` 的 `CDSAPI_KEY`

> ERA5 提供**邊界層高度（boundary layer height）**——原始畢業專題最關鍵的
> 缺失變數。污染物濃度大致正比於排放量除以混合層體積。

下載採排隊制，大範圍請求可能等數小時，建議背景批次執行。

## 4. Google Earth Engine — `GEE_PROJECT_ID`

> **S5P Stage A 已使用 GEE 驗證；它仍不是目前 release 的必要依賴。**
>
> GEE 只影響 Phase 6 的衛星部分（Sentinel-5P 柱濃度與 MODIS AOD）。
> Phase 2–5、7、8 完全用不到，
> Phase 6 的另一半（微型感測器校正）也不需要。
>
> 若不使用 GEE，仍可比較這兩個替代方案：
>
> | 替代方案 | 提供 | 註冊難度 |
> |---|---|---|
> | [Copernicus Data Space](https://dataspace.copernicus.eu/) | Sentinel-5P | 低，一般帳號即可 |
> | [NASA Earthdata](https://urs.earthdata.nasa.gov/) | MODIS AOD（LAADS DAAC） | 低 |
>
> GEE 的優勢是「不必自行下載數 TB 原始檔」，代價就是那套 Cloud 專案流程。

<https://code.earthengine.google.com/>

1. 用 Google 帳號登入
2. 註冊使用目的（選 **noncommercial / research**）
3. 建立或指定一個 Google Cloud 專案並啟用 Earth Engine API
4. 在 Earth Engine 設定頁確認 project 已註冊為 noncommercial／research
5. 填入 `.env` 的 `GEE_PROJECT_ID`

本機認證：

```bash
uv run earthengine authenticate
```

驗證與取得：

```bash
uv run twair doctor
uv run twair stations
uv run --extra earth twair ingest satellite --year 2025 --months 1:12
```

`twair ingest satellite` 讀取 `data/outputs/qc/stations.parquet`，因此先跑一次
`twair stations`。輸出只代表官方 S5P atmospheric columns 在測站位置的站月取樣；
它不是地面濃度、排放估計或 M8 融合結果。

MAIAC 使用可續跑的 monthly batch export。`plan` 只寫本機 ledger，不接觸遠端；
`submit` 只有明確提供 `--confirm-drive-export` 才會建立寫入 Google Drive 的 task：

```bash
uv run --extra earth twair ingest maiac plan --year 2025 --months 1:12
uv run --extra earth twair ingest maiac submit --year 2025 --confirm-drive-export
uv run --extra earth twair ingest maiac status --year 2025
```

每次 `submit` 先對照帳號既有 task，並把 `READY`／`RUNNING` 總數限制在 2；等前一批
完成後重跑同一個 `submit`，才會補上後續月份。Earth Engine 會把 CSV 寫到 Drive 的
`twair-earth-engine` 資料夾。下載完成的 CSV 到同一個本機目錄後，再匯入已完成月份：

```bash
uv run twair ingest maiac import-files \
  --year 2025 --months 1:12 --from-dir PATH_TO_DOWNLOADED_CSVS
```

ledger 與匯入結果都在 gitignored 的 `data/interim/maiac/year=2025/`。不要手改 task ID、
CSV 檔名或內容；匯入器會用 ledger、完整測站集合與 checksum 驗證後才接受。

## 5. Hugging Face — `HF_TOKEN`

<https://huggingface.co/settings/tokens>

1. 註冊帳號
2. 建立 Access Token，**scope 選 `write`**
3. 填入 `.env` 的 `HF_TOKEN`，並把使用者名稱填入 `HF_NAMESPACE`

> 發布前請先讀 [legal.md](legal.md)：原始資料的再散布授權目前有未解決的衝突。

## 6. GitHub Pages

不需要金鑰，但要在 repo 設定中開啟：

Settings → Pages → Source 選 **GitHub Actions**

---

## 完成後

```bash
uv run twair doctor
```

未設定的項目會顯示為 `MISSING`，但不會讓管線失敗——
每個模組只在真正需要時才要求對應金鑰。
