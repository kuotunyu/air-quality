# 需要你親自申請的帳號與金鑰

這些都要本人註冊，我無法代辦。全部免費。

申請完把金鑰填進 `.env`（從 `.env.example` 複製），然後執行：

```bash
uv run twair doctor
```

## 優先順序

**Phase 1 完全不需要任何金鑰。** airtw 年度資料包是公開直接下載，
`uv run twair probe sources` 現在就能跑。以下金鑰是後續階段才會用到。

| 階段 | 需要 | 用途 |
|---|---|---|
| Phase 1 | 無 | airtw 年度包無須驗證 |
| Phase 1（增量） | `MOENV_API_KEY` | 每日更新、測站基本資料 |
| Phase 2 | `CWA_API_KEY` | 氣象站觀測 |
| Phase 4 | `CDSAPI_KEY` | ERA5 邊界層高度 |
| Phase 6 | `GEE_PROJECT_ID` | 衛星資料 |
| 發布 | `HF_TOKEN` | HuggingFace 資料集與 Space |

---

## 1. 環境部 環境資料開放平臺 — `MOENV_API_KEY`

<https://data.moenv.gov.tw/>

1. 右上角「登入／註冊」→ 註冊會員
2. 完成 email 驗證
3. **API 金鑰會寄到註冊信箱**，不在網站上顯示 —— 請保留該封信件
4. 填入 `.env` 的 `MOENV_API_KEY`

驗證方式（把 `YOUR_KEY` 換掉）：

```bash
curl "https://data.moenv.gov.tw/api/v2/aqx_p_432?limit=1&api_key=YOUR_KEY"
```

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

<https://code.earthengine.google.com/>

1. 用 Google 帳號登入
2. 註冊使用目的（選 **noncommercial / research**）
3. 建立或指定一個 Google Cloud 專案並啟用 Earth Engine API
4. **審核可能需要數天** —— 請及早申請
5. 填入 `.env` 的 `GEE_PROJECT_ID`

本機認證：

```bash
uv run earthengine authenticate
```

## 5. HuggingFace — `HF_TOKEN`

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
