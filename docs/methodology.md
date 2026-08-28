# air-quality ── 台灣空氣品質再分析方法論

> 本文件詳細說明 **air-quality** 專案所採用的統計與統計學、機器學習在資料工程上的修正方法。本文件逐項解析 11 項結構性的方法缺陷（標記為 D1–D11），並記錄實作中的嚴謹資料工程與多變量分析校正。
> 本專案實作程式碼已全數在 [src/twair/analysis/baseline.py](../src/twair/analysis/baseline.py)、[src/twair/analysis/pitfalls.py](../src/twair/analysis/pitfalls.py) 與 [src/twair/analysis/drivers.py](../src/twair/analysis/drivers.py) 中建立完成。

---

## D1: 時間尺度特徵聚合偏誤（逐時資料聚合成月平均）

### 1. 原始缺陷
常見做法是「將逐時資料合併為各測站之月資料」，把原始逐時觀測值平均成月值。此操作會造成嚴重的資訊損失，並引發**聚合偏誤（Aggregation Bias）**。

### 2. 統計學原理
將高頻時序資料（小時）聚合至低頻（月）會消抹掉資料中絕大部分的變異性。根據**方差分解公式（Law of Total Variance）**：

$$\text{Var}(X) = \mathbb{E}[\text{Var}(X|T)] + \text{Var}(\mathbb{E}[X|T])$$

其中：
* $\text{Var}(X)$ 為原始逐時資料的總方差（Total Variance）。
* $\mathbb{E}[\text{Var}(X|T)]$ 為「月度內的小時變異」的期望值。
* $\text{Var}(\mathbb{E}[X|T])$ 為「月平均值」之間的變異。

當我們將資料尺度限制在月平均值時，等同於直接假設月度內的小時變異 $\mathbb{E}[\text{Var}(X|T)] \equiv 0$。

### 3. 實測證據與校正
經由 [src/twair/analysis/pitfalls.py](../src/twair/analysis/pitfalls.py) 的 `diurnal_cycle_lost_to_monthly_means` 函式實測 2010–2017 年的數據，
「每站每月一個值」聚合（本站同期得 7,269 個站－月）僅保留了原始逐時 $\text{PM}_{2.5}$ 變異的 **40.3%**（標準差自 $19.4\ \mu g/m^3$ 降至 $12.3\ \mu g/m^3$）。這意味著：
* 喪失了 **59.7%** 的物理訊號（包括日夜循環、上下班交通尖峰、短期寒流沙塵暴事件）。
* 無法反映短時間內的健康暴露風險（例如，數小時內的極端污染事件在月平均下會被完全平滑化）。

⚠️ **本節原本寫的是 20.3%，那是另一種聚合的數字，我們自己踩過同一個坑。**
若再把 78 站併成單一的全台月序列（96 個值），保留率才是 20.3%（標準差
$8.7\ \mu g/m^3$）——但基準並沒有做這一步，它保留了測站維度。
以 n 加權的變異數分解可以把兩者分開：$\mathrm{Var}(\mathbb{E}[X \mid \text{月}])$
佔總變異 **20.1%**，$\mathrm{Var}(\mathbb{E}[X \mid \text{測站}, \text{月}])$ 佔
**40.2%**，差額 **20.1%** 純粹是測站之間的變異。把它也算進「月平均的損失」，
等於把空間的損失記在時間的帳上，而那正是本章在指責別人的那種錯。


**校正對策**：本專案建立的 `twair` 架構全程保留完整的 3.40 億筆逐時觀測資訊，將月平均指標僅作為對照。

---

## D2: 預測特徵資訊洩漏（用 $PM_{10}$ 預測 $PM_{2.5}$ ）

### 1. 原始缺陷
把 $PM_{10}$ 作為解釋變數來預測 $PM_{2.5}$，會得到極顯著的迴歸係數。M1 基準
自己配適的結果是 $\beta_{PM_{10}} = 0.4020$、$t = 86.28$（`m1_baseline/ols.parquet`），
而 t 值之所以這麼大，一部分正是下一節要拆開的定義性重疊。

### 2. 統計學原理
在物理定義上，$\text{PM}_{2.5}$（粒徑 $\le 2.5\ \mu m$ 的懸浮微粒）是 $\text{PM}_{10}$（粒徑 $\le 10\ \mu m$ 的懸浮微粒）的子集。因此兩者存在必然的物理恆等式：

$$\text{PM}_{10} \equiv \text{PM}_{2.5} + \text{PM}_{2.5-10}\ （粗懸浮微粒）$$

這在機器學習與統計建模中構成了嚴重的**特徵洩漏（Information Leakage / Target Leakage）**。當使用包含自身子集的變數作為預測特徵時，模型得到的超高 $R^2$ 只是恆等式的自我映射，掩蓋了真正具有科學意義的物理驅動因子（如風速、溫度、邊界層高度等）。

### 3. 實測證據與校正
經由 [src/twair/analysis/pitfalls.py](../src/twair/analysis/pitfalls.py) 的 `pm10_leakage_price` 實測，
在 Gradient Boosting 模型（LightGBM）中：
* 誠實特徵集模型（不含 $PM_{10}$）的 $R^2$ 為 **0.524**。
* 洩漏特徵集模型（包含 $PM_{10}$）的 $R^2$ 虛增至 **0.772**。
* 洩漏變數對 $R^2$ 的貢獻率高達 **32.1%**。

**校正對策**：全面將 $PM_{10}$ 移出特徵預測集；相反地，我們將 $\text{PM}_{2.5}/\text{PM}_{10}$ 的比值建立為**粒徑組成指標**特徵。它可用於篩選來源假說；單一比值不能唯一辨識或量化來源。來源歸因還需要化學物種分析（chemical speciation）、受體模式，並以軌跡、擴散或排放清冊的獨立證據交叉檢驗。

---

## D3: 風向（ $0^\circ-360^\circ$ ）線性連續化謬誤

### 1. 原始缺陷
在描述統計、相關係數計算及多元迴歸中，把風向角度（`WD_HR`，$0^\circ-360^\circ$）直接視為一個一般的線性連續變數進行物理加總平均或計算 Pearson 相關係數。

### 2. 統計學原理
風向屬於**循環角度數據（Circular / Directional Data）**。角度在數值上是斷開的，但在物理上是相鄰的。
例如：
* $359^\circ$（北北西）與 $1^\circ$（北北東）在物理上僅相距 $2^\circ$。
* 若當成線性數值進行算術平均：

$$\bar{\theta} = \frac{359^\circ + 1^\circ}{2} = 180^\circ\ （正南方）$$

會得出與真實風向（北方）完全物理相反的極端荒謬結論。

### 3. 實測證據與校正
本專案在 [conf/pollutants.yaml](../conf/pollutants.yaml) 將風向特徵明確標記為 `circular: true`，並在 [src/twair/features/met.py](../src/twair/features/met.py) 中透過非線性三角變換，將風速（$WS$）與風向（$\theta$）解構為正交的 $u$ 分量與 $v$ 分量（東風與北風向量）：

$$u = WS \cdot \sin\left(\frac{\theta \cdot \pi}{180}\right)$$

$$v = WS \cdot \cos\left(\frac{\theta \cdot \pi}{180}\right)$$

在線性 OLS 模型中，經由 [src/twair/analysis/pitfalls.py](../src/twair/analysis/pitfalls.py) 中的 `wind_encoding_in_a_linear_model` 實測對照：
* **原始角度未變換**：與 $\text{PM}_{2.5}$ 的聯立 OLS 決定係數 $R^2$ 僅為 **0.0254**。
* **三角向量分解（u/v）**：決定係數 $R^2$ 提升至 **0.0647**（效能提升達 **2.55 倍**）。

此外，本專案在進行角度離散化區間分類時，嚴格遵守「modulo 第一原則」，先計算 `WD_HR % 360` 確保 $360^\circ$ 正確與 $0^\circ$ 物理折疊，避免在區間分割時產生額外子群。

---

## D4: 樣本大尺度下的 Kolmogorov-Smirnov 常態檢定謬誤

### 1. 原始缺陷
一種常見的處理是這樣寫的：由於一開始常態檢定皆顯著拒絕虛無假設（非常態），因此將統計拒絕域標準降低至 0.01，使變數不拒絕常態虛無假設，從而宣稱符合常態性。

### 2. 統計學原理
此處存在雙重學術推理謬誤：

1. **顯著性水準（Alpha）的誤用**：將 $\alpha$ 從 $0.05$ 降低至 $0.01$，是使得拒絕虛無假設（真實分配不等於常態分配）的門檻變得更嚴格，而非使得資料「自動變成常態性」。
2. **大觀測樣本下的檢定效能飽和**：
   Kolmogorov-Smirnov 檢定與 Shapiro-Wilk 檢定，其統計檢定力（Power）會隨著樣本數 $N$ 的增長而趨向於 $1.0$。在巨量原始觀測下（月聚合 $N \approx 7{,}000$，本專案逐時 $N \approx 3.4\text{ 億}$），任何物理上微小且不影響中央極限定理（CLT）漸近性質的極微小偏誤，都會被檢定判定為「極其顯著地偏離常態」（$p\text{-value} = 0.000$）。

### 3. 實測證據與校正
在 [src/twair/analysis/pitfalls.py](../src/twair/analysis/pitfalls.py) 的 `normality_test_fallacy` 中，使用完全常態的分佈與僅有極微小偏態的分佈在不同樣本數 $N$ 下仿真：
* 偏態分佈在 $N=100$ 時能「通過常態檢定」，但在 $N=100,000$ 時會被 100% 絕對拒絕。這證明檢定判定的是**樣本大小**而非**常態本質**。
* 大樣本下 KS 的雙尾顯著性普遍落在 `.000`（即 $p < 0.0005$）。在數學上，低於 $0.0005$ 的數值無論在 $\alpha = 0.05$ 還是 $\alpha = 0.01$ 下都必然被**拒絕**——所以「調低 $\alpha$ 之後就不拒絕常態」這個說法，與它自己的檢定結果自我牴觸。

**校正對策**：捨棄機械式的常態假設檢定，改用穩健統計指標（Robust Statistics）、Bootstrap 區間估計（自助法置信區間）與適用於厚尾地理資訊的角度分位數迴歸（Quantile Regression）。

---

## D5: 氮氧化物（ $NO_x$ ）等價共線性病理

### 1. 原始缺陷
把 $NO$、$NO_2$ 與 $NO_x$ 同時放入線性多元迴歸，再以手動逐步剔除反覆置入，是常見的處理方式。

### 2. 統計學原理
在大氣化學與環境觀測的基礎物理定義中，氮氧化物（$\text{NO}_x$）是活性氮化學物一氧化氮（$\text{NO}$）和二氧化氮（$\text{NO}_2$）的總和。此等價關係在質量守恆上構成了絕對恆等共線性：

$$\text{NO}_x \equiv \text{NO} + \text{NO}_2$$

當我們在獨立估計矩陣中同時引入這三個變數時，其設計矩陣（Design Matrix） $X^T X$ 將會無限趨近於奇異矩陣（Singular Matrix），造成**多重共線性（Multicollinearity）**病理：
* 參數估計的方差虛增到無限大。
* 模型估計值極度不穩定，微小的樣本變動就會造成學術係數正負號轉向。

### 3. 實測證據與校正
經由 [src/twair/analysis/pitfalls.py](../src/twair/analysis/pitfalls.py) 的 `collinearity_instability` 實測：
* **恆等殘差驗證**：實測全台 44 年原始觀測，$\text{NO} + \text{NO}_2 - \text{NO}_x$ 的物理差值均值僅為 $0.0019\ \mu g/m^3$，最大誤差在極限容差內，恆等式絕對成立。
* **共線性指標**：三個變數同時存在時，其共線性變異膨脹因子（VIF）值分別高達：
  $$\text{VIF}_{NO} = 7,764,\quad \text{VIF}_{NO_2} = 25,377,\quad \text{VIF}_{NO_x} = 53,452$$
  （遠遠超出一般學術界認定的嚴重多重共線性臨界值 10）。
* **Bootstrap 穩定性實驗**：對資料進行 $B=40$ 次自助法抽樣重新擬合，$\text{NO}$、$\text{NO}_2$、$\text{NO}_x$ 的迴歸係數產生了毀滅性的劇烈變動與符號反轉；而在設計矩陣中不含恆等式的誠實變數（如 $\text{O}_3$ 臭氧）其估計係數的變異係數（CV）極小，穩定性高。

**校正對策**：限制恆等式組合。改用正則化收縮模型（Elastic Net）、機器學習決策樹（LightGBM）與 SHAP 賽值（Shapley Additive exPlanations）共同評估特徵邊際化貢獻度。

---

## D6: 樣本外驗證缺失（零樣本外交叉驗證）

### 1. 原始缺陷
只報告基於全部適配資料計算得到的樣本內（In-sample）擬合度指標（AIC, BIC與決定係數 $R^2$），並以此進行特徵篩選、解釋與推論。

### 2. 統計學原理
僅評估樣本內指標會導致嚴重的**樂觀偏誤（Optimism Bias）**，無法區分模型究竟是學習到了真實的空氣品質物理成因（訊號），還是僅僅記住了局部樣本特有的雜訊。

### 3. 實測證據與校正
在 [src/twair/analysis/pitfalls.py](../src/twair/analysis/pitfalls.py) 的 `in_sample_versus_out_of_sample` 中，對時序資料進行了劃分測試：
* 樣本內指標 R² 達 **0.690**。
* 未來觀測樣本外指標 R² 驟降至 **0.562**。
* **樂觀度差距（Optimism Gap）高達 +0.128**。

**校正對策**：本專案在 [src/twair/models/evaluate.py](../src/twair/models/evaluate.py) 中建立並實作了三種基於物理空間與時間的嚴格交叉驗證框架：
1. **Rolling-origin （滑動時序起點交叉驗證）**：依時間推移，用過去訓練、未來測試，用作預測評估的基本基準。
2. **Leave-one-station-out （留一測站交叉驗證）**：測試模型對全新未觀測地理測站的泛化估計能力。
3. **Leave-one-year-out （留一年度交叉驗證）**：排除某整年的天氣異常物理干擾，檢驗模型成因解釋之穩健性。

### 4. 延伸：樣本外之後，還有「跟誰比」的問題

把樣本內換成樣本外，只解決了一半。剩下一半是 $R^2$ **本身沒有基準線**——
它衡量的是「這個目標本來多好預測」，不是「模型有沒有加到東西」。

[src/twair/models/forecast.py](../src/twair/models/forecast.py)（M9）在
74 站、2015–2025、4 個 rolling-origin 分割上量到的三組數字，方向互相矛盾：

| 期距 | $R^2$ | skill vs persistence | skill vs climatology |
|---|---|---|---|
| 1h | 0.859 | +0.172 | +0.837 |
| 24h | 0.317 | +0.196 | +0.207 |
| 48h | 0.289 | +0.315 | +0.174 |

其中 $\text{skill} = 1 - \text{MSE}_{\text{模型}} / \text{MSE}_{\text{基準}}$。

* **$R^2$ 掉三倍（0.859 → 0.289），skill 卻沒有跟著掉。**
  因為 PM2.5 一小時後任何人都很好預測，包括一條說「跟現在一樣」的規則；
  persistence 隨期距衰退得比模型快。
* **只用一條基準線一定高估模型。** vs persistence 在 48 小時最高，
  單看這欄會讀成「愈遠愈好」。但 vs climatology 在同一段從 +0.837 崩到 +0.174
  ——兩天後模型已大致退化成「這站、這個月、這個小時的平均」。
  在 persistence 已經輸給長期平均的地方贏過 persistence，不構成成就。
  實用範圍約到 24 小時。
* **平均值會藏掉輸掉的分割——而且曾經藏過。** 第一次回測時，16 個
  「期距 × 分割」格子裡有一個是 −0.111（6 小時、訓練資料最少的 `rolling_1`），
  四個期距的平均卻全是正的。現在這張表沒有負的格子，最差是 +0.080，
  仍在同一個位置。讓它消失的是修掉 `features/lags.py` 裡一個無關的洩漏
  （每個測站的前 167 小時帶著前一個測站的歷史）。
  這與「$R^2$ 藏掉一個爛模型」是同一個錯，只是高了一層。

**這個「變號」本身也被檢查過，因為重新擬合本來就會抖。**
模型跑 `subsample=0.8, subsample_freq=1`，列數一變、每棵樹的隨機路徑就全變，
所以少掉 0.22% 的列不是小擾動。在修正後的資料上另外用 seed 11 與 22
各重跑一次完整回測：

| 量 | 三個 seed 之間的全距 |
|---|---|
| skill vs persistence | 0.001（1h）到 0.023（24h） |
| 模型 RMSE | 最多 0.12 μg/m³（24h） |
| 輸掉的「期距 × 分割」格子 | 12 組（4 期距 × 3 seed）全部是 0 |

6 小時的變動是 +0.05～+0.06，而雜訊全距是 0.006——差一個數量級；
最差分割在三個 seed 下分別是 +0.080、+0.132、+0.152，沒有一次是負的。
48 小時的變動 +0.07 對雜訊 0.013 也站得住。
**24 小時的 −0.04 對雜訊 0.023 不到兩倍，是這裡唯一不該拿來論證的一格。**

換句話說，本節公布的數字本身帶著約 ±0.02（skill）與 ±0.1 μg/m³（RMSE）的
重擬合誤差，這還沒算資料本身的問題。

**校正對策**：預測結果一律同時報告兩條基準線的 skill 與**最差分割**
（`summarise_scores` 的 `skill_worst_split`、`splits_not_beating_persistence`），
且期距永不合併平均。

---

## D7: 空間維度——「分區各跑一次」是不是足夠的空間控制

### 1. 原始缺陷（與對它的修正）

缺陷清單原記載為「78 個測站的空間維度沒用」。實測之後這句話需要修正：基準
**有**使用空間——按空品區分層、每區各跑一次。所以可檢定的問題是：
**那個分層是不是足夠的空間控制？** 這比原指控公平，也才有數字可答。

### 2. 統計學原理

若模型殘差仍具空間自相關，把 6,771 筆站月當獨立樣本的標準誤就被低估，
t 統計量被膨脹。三個方法論決定（各修正一個常見錯誤）：

1. **殘差不可用排列檢定**：OLS 殘差滿足 $X'e=0$，不可交換。虛無分布改在配適
   模型下模擬；由 $MX=0$，「逐次重配」等價於把 iid 誤差投影過殘差製造矩陣
   $M$，一次抽樣只需一次矩陣乘法。
2. **殘差的 $E[I]$ 不是 $-1/(n-1)$**：要用 Cliff–Ord 動差（依賴整個設計矩陣）。
   其 $\mathrm{Var}[I]$ 為近似，Monte-Carlo 實測在本面板 $k/n \approx 0.18$ 下
   **高估 11%**（方向保守），故發表之 p 值一律取自模擬虛無。
3. **不發表單一 Moran's I**：I 是「場 × 權重矩陣」的共同性質，且實測
   correlogram 會變號，單一數字是異號帶的加權混合。

### 3. 實測證據（[src/twair/analysis/spatial.py](../src/twair/analysis/spatial.py)，seed 12345、999 次模擬虛無、knn(5)、BH 校正）

> 本節的數字是 M6 的產物，**現值一律以 [reports/03-spatial.md](../reports/03-spatial.md)
> 為準**——那份報告由 `uv run twair report spatial` 直接從 parquet 產生。這裡抄一份是
> 為了讓方法論可以獨立讀完，代價是它會落後；M6 重跑後請比對兩邊。

**Correlogram 變號**：0–10 km 的站均殘差 I = +0.277（z=+1.98），100–150 km
反號至 **−0.230（z=−4.55）**——北南殘差反向共變的偶極結構。近距離的正相關
本身**沒有**通過 BH 校正（8 個距離帶中 3 個通過），所以這一段的證據是符號隨距離
改變，不是「近處顯著正相關」。

**五種控制在同一設計上**（每月殘差 I 平均、塊狀 bootstrap CI）：

| 控制 | 參數 | 平均 I | BH 顯著月數 |
|---|---|---|---|
| 合併式（未分層） | 13 | +0.156 | 54/96 |
| 七區截距 | 20 | +0.141 | 45/96 |
| 字面上的「分區各跑一次」 | 104 | **+0.064** | **5/96** |
| 測站固定效果 | 84 | +0.097 | 20/96 |

分層其實移除了大部分空間相依——比測站固定效果更有效（斜率也隨區變）。
但 t 值通常報自**合併式**模型。

**推論價格**（同設計、只換共變異）：t(PM10) 從 86.28 降至
聚類月 22.77、聚類站 17.27、two-way（CGM）**14.07**（SE ×6.13）。two-way 下
Intercept、CO、RAINFALL、**WD_HR** 失去顯著——WD_HR 即 D3，同一係數上
編碼錯（D3）與推論錯（D7）疊加。

**LISA**：61 站中 raw 顯著 7 站、**BH 後 0 站**——相依是場性質，非孤立熱點。

**官方分區 vs 純地理 ensemble**（999 個隨機 Voronoi 分割）：時代七區＋離島桶
的區內−區間相關差 +0.802，居 **99.5 百分位**（分區確實載有超出鄰接性的資訊）；
但 silhouette 僅 +0.026，資料偏好 **k=2**（北群/南群，silhouette +0.620）。
結論：**分區不是亂劃、但比資料支持的粒度細**。相關算在距島均值的異常量上——
生料的全島兩兩平均相關幾乎全是季風，扣掉逐月全島均值之後趨近於零。兩個值
每次執行重算並寫入 `metadata.parquet`，現值印在 `reports/03-spatial.md`；
這裡不轉述，因為先前寫死在這裡的那個數字已經漂掉了。

### 4. 範圍限制（隨數字同行）

* 只為 **OLS 階段**定價：修正的是「誤差互相獨立」這個假設，而 t 值不等於整個
  推論。帶 AR(1) 誤差結構的模型有自己的標準誤，本 repo 沒有計算。
* 殘差 I 是場相依的**下界**：解釋變數（尤其 PM10）自帶空間結構已吸走訊號；
  面板為完整案例，網路被資料完整度篩選。
* **不做**人口加權暴露（repo 無人口網格）；**不出** 1 km 濃度場——最近鄰距離
  跨越兩個數量級，1 km 宣稱了網路給不起的解析度。間距由
  `station_coverage.parquet` 的 `km_nearest_neighbour` 每次執行重算，現值印在
  `reports/03-spatial.md`；兩者與所需輸入記錄於 `conf/spatial.yaml`。

## D8: 相關 ≠ 因果——氣象正規化，以及它換來的是什麼

### 1. 原始缺陷

把下降的濃度趨勢直接讀成減量成效是常見的誤讀。一條下降的線有兩個互相競爭的解釋，
而**光看那條線分不出來**：排放真的減少了，或者那幾年的天氣剛好比較好——
風大一點、雨多一點、逆溫少一點。任何「政策有效」的主張都得先把兩者分開。

### 2. 統計學原理

方法是 Grange et al. (2018), *Science of the Total Environment* 653，
在本專案以 Python 重寫（參考實作 `rmweather` 是 R 套件）：

1. 用隨機森林以**氣象 ＋ 時間項**（小時、年積日、星期、線性趨勢）預測濃度。
2. 每次迭代把**除了趨勢以外**的每一個預測變數，用整段記錄裡別處重抽的值取代，再預測。
3. 跨迭代平均。結果是「若當下的天氣是從整段期間隨機抽出來的，濃度會是多少」。
4. 對正規化後的序列用 Theil–Sen 估趨勢。

三件它**不是**的事，而三件都很容易被當成它是：

- **不是因果歸因。** 它移除的是**模型看得見的**氣象影響。本地風速與濕度捕捉不到的
  境外傳輸會留在正規化序列裡，無法與排放、化學反應或其他未建模因素分開。這就是每條趨勢旁邊都要報 R² 的理由：
  **解釋力低的模型不可能移除得多。**
- **不是去趨勢。** 趨勢項是唯一被刻意固定的變數；把它一起重抽就會把要量的訊號本身移除。
- **信賴區間不是課本那個。** 逐時空品序列有強自相關，標準 Theil–Sen 區間（假設獨立觀測）
  太窄。改用移動塊狀 bootstrap 保留局部相關結構，兩者並列報告，因為**差距本身值得看**。

### 3. 實測證據（[src/twair/analysis/deweather.py](../src/twair/analysis/deweather.py)）

74 個測站，73 個的正規化斜率顯著。

| 量 | 值 |
|---|---|
| 觀測斜率中位數 | **−1.24** μg/m³/年 |
| 正規化後斜率中位數 | **−0.73** μg/m³/年 |
| 氣象貢獻佔比中位數 | **42.2%**（p10 37.7%、p90 46.0%） |
| holdout R² 中位數 | **0.445** |

也就是說：模型估計的站內氣象差異，對觀測斜率與正規化斜率差距的中位數貢獻是
**42.2%**。這是模型分解出的差距，不是排放變化的占比。按空品區看仍然成立
（高屏 15 站：觀測 −1.741、正規化 −0.985、氣象佔 43.4%）。

一個測站沒有得到結論並且照實列出：**大同**的正規化斜率 +0.146，
區間 [−0.694, +1.155] 跨過零。

**誠實的上限，寫在 payload 自己的 `caveat` 欄裡**：holdout R² 中位數 0.445，
代表逐時變異有一半以上不是本地氣象能解釋的——境外傳輸就在其中，
並會留在剩餘趨勢裡；排放、化學反應與其他未建模因素也都在同一個剩餘項中。

### 4. 第二個答案：能不能把標記窗口訊號從背景變異中分出來

正規化回答了「趨勢裡有多少是天氣」，但單憑這些資料與模型不能識別政策的因果效應。
[src/twair/analysis/causal.py](../src/twair/analysis/causal.py) 計算標記窗口的觀測－預測差額，
再問它是否超過同站、同日曆時段、未標記的同日曆控制窗口變異。
「未標記」只表示沒有該事件標籤，不代表窗口內沒有其他變化；
這個差額與控制窗口比較不是已識別的政策因果效應。
表中的「參考分布」依估計型態而異：前兩列來自未標記的同日曆控制窗口；
斜率差列來自同一正規化序列中的其他候選斷點月份。

| 事件 | 站數 | 中位估計值 | 參考分布均值 | 參考分布 SD | 通過門檻 | 機率預期 |
|---|---|---|---|---|---|---|
| COVID-19 全國三級警戒（窗口差額） | 73 | −0.494 μg/m³ | −0.690 μg/m³ | 2.503 μg/m³ | 1 | 3.3 |
| 台中電廠生煤許可爭議（窗口差額） | 72 | −1.612 μg/m³ | +0.743 μg/m³ | 3.512 μg/m³ | 1 | 3.3 |
| 2018 空污法修正（斜率差） | 72 | +0.454 μg/m³/年 | +0.024 μg/m³/年 | 0.660 μg/m³/年 | 1 | 3.3 |

三個標記的實際通過數都**低於**機率預期。COVID 那一列是關鍵：
未標記控制窗口均值本身就是 −0.690 μg/m³，表示同日曆控制窗口平均也出現接近的下降。
所以誠實的說法是「**測不到**」，不是「等於零」——這是方法的偵測極限，
不是政策效應大小。1,295 與 1,208 個控制窗口估計值逐一列在網站第五章的表格裡。

---

## D9: GUI 工具不可重現

### 1. 原始缺陷

GUI 統計軟體（SAS Enterprise Guide / SPSS）的點選介面不會留下紀錄：
別人拿到同一份資料，沒有辦法重跑出同一個數字，連原作者六個月後也不行。

### 2. 「可重現」要成立，得同時鎖住四件事

不是「用 Python 就好」——那只換掉了工具，沒有換掉問題。

1. **相依套件的版本**：`uv.lock`（5,105 行）鎖到雜湊。
2. **Python 自己的版本**：`.python-version` 為 `3.12`，`pyproject.toml`
   宣告 `requires-python = ">=3.12"`，mypy 也設 `python_version = "3.12"`。
3. **入口**：每一個分析都是一個 CLI 子指令，不是一段要照順序執行的 notebook。
   `twair status` 會說出每個模組是否跑過、是否過期、下一步是什麼。
4. **有人在看**：CI 每次推送跑 ruff、mypy（`src tests` ＋ `scripts`）、
   色票閘門、agent 指令鏡像、地圖幾何、匯出 manifest、pytest，
   以及一個獨立的 web job（astro check、build、中文空白、章名一致）。

### 3. 一個實際發生過的漏洞，記錄在案

第 2 點是後來補的，而它被發現的方式值得寫下來：**沒有 `.python-version` 時，
CI 明確安裝 3.12，本機 `uv run` 卻挑到 3.14.5**。D9 的答案本來寫的是
「uv 鎖定版本」——鎖的是套件，**Python 版本本身沒鎖**，所以本機與 CI 跑的
不是同一個直譯器。一份寫著「可重現」的文件，在那段期間是不成立的。

這正是這份文件反覆在說的那件事：**一個停止守衛的保證，讀起來跟還在守衛的一樣。**

---

## D10: 「實屬不便」——被跳過的 SARIMA，以及跳過它是否正確

### 1. 原始缺陷

SARIMA 常被以「太麻煩、不實用」為由略過。

問題不在結論，而在**這個結論沒有附任何數字**。一份以方法選擇為主題的工作，
把一個標準時間序列模型排除掉，理由是「不便」——那句話可以是工程判斷，
也可以是個人偏好，而讀者無法分辨是哪一種。一個附了秒數的表格只能是前者。

所以這裡量兩件互相獨立的事：**那個不便值多少**，以及**它買到了什麼**。

### 2. 統計學原理

「不便」的來源不是 SARIMA，是**階數搜尋**。給定 $(p,d,q)(P,D,Q)_s$，
擬合一次是一個中等規模的最大似然問題；但自動選階要在候選空間裡反覆擬合，
而候選數與序列長度無關、每次擬合的成本卻隨長度增長。因此
$\text{搜尋} / \text{固定階數}$ 這個倍數**本身會隨資料量放大**。

第二件事需要基準線。SARIMA 的對照不是「它的 $R^2$ 好不好」，而是
**它有沒有打贏不用擬合就能得到的東西**——persistence（「跟現在一樣」）
與 climatology（「這站、這個月、這個小時的平均」）。而且三者必須
**評分在完全相同的原點上**，否則不構成比較。

### 3. 實測證據

[src/twair/models/sarima.py](../src/twair/models/sarima.py)（M12），
6 個測站 × 3 個 rolling-origin 分割、2015–2025、固定階數
$(1,0,1)(1,0,1)_{24}$、每 72 小時一個預測原點。**18/18 次擬合全部收斂**，
中位數 11 秒、8,612 個有效觀測點。

**（a）那個不便，定價：**

| 序列長度 | 自動選階 | 固定階數 | 倍數 |
|---|---|---|---|
| 1,000 點 | 11.14 s | 0.58 s | **19.1×** |
| 2,000 點 | 22.86 s | 0.91 s | **25.1×** |
| 5,000 點 | 72.29 s | 1.78 s | **40.7×** |

倍數隨長度增長，正如上節所預期。逐時資料的單站序列是**九萬多點**，
不是五千點——所以「它不方便」這件事是對的，而且比一個定值更嚴重。

**（b）那個不便，買到了什麼**（RMSE，μg/m³，同一批原點）：

| 期距 | SARIMA | persistence | climatology | 結果 |
|---|---|---|---|---|
| 1h | 5.13 | **4.06** | 9.31 | **輸給 persistence 26.2%** |
| 6h | **7.46** | 7.86 | — | 贏 5.1% |
| 24h | **8.57** | 9.10 | 9.39 | 贏 5.9% |
| 48h | **9.26** | 10.13 | 9.35 | 贏 climatology 1.0% |

（$n \approx 4{,}700$ 個原點／期距；完整逐站逐分割表在
`data/outputs/m12_sarima/scores.parquet`。）

### 4. 結論：那個判斷是對的，但理由不完整

三件事值得分開講：

* **一小時後，SARIMA 輸給一行規則 26%。** 這不是實作問題，是這個模型的性質：
  $(1,0,1)(1,0,1)_{24}$ 沒有差分項，一步預測會往均值回歸，而不是
  承諾「跟剛才一樣」。在 PM2.5 一小時尺度上，承諾比回歸準。
* **6 到 24 小時它會贏，但只贏 5–6%。** 代價是每個站-分割 11 秒擬合
  加上每個原點約 1.5 秒的濾波。
* **48 小時它的優勢崩到 1%。** 那已經在量測誤差的層級，
  等於「兩天後 SARIMA 和長期平均沒有差別」——這與 D6 對 M9 的觀察一致：
  兩天後任何方法都退化成 climatology。

所以**跳過 SARIMA 這個決定是對的**——但不是因為它不便（雖然它確實不便），
而是因為**在它最該有用的期距上它被一行規則打敗，而它會贏的地方贏得不夠多**。
常見說法給了正確的答案和不完整的理由；現在兩者都有數字。

**這裡刻意沒有放 LightGBM。** 把 M9 的模型放進這張表，需要為了落在
同一批原點而逐站-分割重建整條特徵管線；而 D10 要問的問題不需要它——
一個連「一小時前的值」都打不贏的方法，被放棄是對的，
不管梯度提升樹做得如何。M9 的數字在它自己的列上，兩者不可直接比較。

---

## D11 & 品管規則（QC）: 降雨 `NR` 語意與哨兵值陷阱

### 1. 原始缺陷
常見的預品管描述是：「將有遺漏值之資料以鄰近測站之資料代替。」且未對任何缺失填補（Imputation）進行記錄或敏感度分析，同時直接把所有非數值符號排除。

### 2. 資料工程與學術謬誤
這導致了兩個極其隱蔽但足以摧毀整份研究可信度的資料科學漏洞：

1. **降雨 `NR`（無降雨）排除謬誤**：
   環境部原始資料中，約有 90% 的降雨量格位記錄為中文物理標記 `NR`（No Rain）。若在解析時直接將其過濾為 `null` 丟棄，在後續計算降雨均值時，將把「平均雨量」計算為「有下雨時的降雨強度」。
   這使得 2010–2017 年的平均雨量估值從真正的 **0.23 mm/h** 暴增為 **2.32 mm/h**（虛增達 $1,000\%$）。
2. **風向哨兵碼 (888/999) 線性混入**：
   風向欄位裡的 `888` 與 `999` 不是角度，是哨兵碼。若不解析而直接代入平均值計算，等於宣稱風從 $888^\circ$ 吹來。

   > **⚠️ 兩版官方 ReadMe 對這兩個碼的定義互相矛盾。** 2017 版寫「888 代表無風，999 代表儀器故障」；2001 年資料包所附的版本（`ReadMe_普通測站_20090901.txt`）則寫「888 表示風向不定」「999 表示靜風」。兩份都不是資料採集當下寫的，所以無法用權威性裁決。
   >
   > 本專案改用資料本身裁決：取 1993–2004 年哨兵碼觸發的時刻，讀取**同一測站同一小時、獨立有效的風速值**——
   >
   > | 哨兵碼 | 筆數 | 風速中位數 | 低於 0.5 m/s 的比例 |
   > |---|---|---|---|
   > | `999` | 77,448 | **0.00 m/s** | 81.3% |
   > | `888` | 230,138 | 0.43 m/s | 53.3% |
   > | （正常方位角） | 5,383,753 | 1.84 m/s | 11.0% |
   >
   > 儀器故障與風速靜止之間沒有理由相關。**`999` 是靜風，`888` 是風太弱或飄忽以致風向儀無法判定方向。2001 版才是對的**——而哨兵碼只出現在 1993–2004 年，正是該版說明涵蓋的年代。本專案已據此更正（見 `conf/pollutants.yaml` 與 `twair.qc.sentinels`）。

### 3. 缺漏填補：把那一句話標上價格

缺漏值常被一句「將有遺漏值之資料以鄰近測站之資料代替」處理掉——
沒有數量、沒有誤差估計、沒有敏感度分析，也沒有說哪一站借給了哪一站。
**批評這句話是免費的，標價不是。** 所以本專案把那個做法實作出來
（`src/twair/qc/gapfill.py` 的 `neighbor` 策略：30 公里內、相關係數 ≥ 0.7 的最近測站，
以重疊時段的 OLS 迴歸映射），與另外三種策略並排跑在**同一批資料**上。

#### 先問「缺漏長什麼形狀」

78 個測站、2010–2017（基準自己的窗口）：

| 缺口長度 | 缺口數 | 佔缺口數 | 缺失時數 | 佔缺失時數 |
|---|---|---|---|---|
| 1 小時 | 40,406 | **72.5%** | 40,406 | 20.7% |
| 2–3 小時 | 10,214 | 18.3% | 23,053 | 11.8% |
| 4–12 小時 | 3,546 | 6.4% | 21,427 | 11.0% |
| 13–48 小時 | 1,108 | 2.0% | 25,724 | 13.2% |
| **> 48 小時** | **453** | **0.8%** | **84,972** | **43.4%** |

「缺了多少」有兩個答案且方向相反：按**次數**看幾乎都是零星單小時，
按**時數**看將近一半來自 453 次長停機。所以**一個不附缺口長度分布的填補率等於沒講**。

#### 評估設計：遮罩必須模仿真實的缺漏形狀

隨機遮蔽單一格會把每個洞都變成 1 小時，而線性內插對 1 小時缺口幾乎不可能失敗——
那樣得到的表格會宣稱線性內插非常優秀。因此本專案遮蔽的是**成串的洞**，
長度**抽自這批資料自己量到的缺口分布**，並且分桶報告
（`src/twair/analysis/imputation.py::mask_like_reality`）。

12 個平均分散的測站，隱藏 40,679 筆已知 PM2.5（5%），分成 21,175 個成串的洞：

| 策略 | 猜回幾成 | MAE (μg/m³) | RMSE |
|---|---|---|---|
| `none`（本專案預設） | 0% | — | — |
| `interpolate`（≤3h 線性） | 60.6% | **2.88** | 4.03 |
| **`neighbor`（鄰近測站做法）** | 69.7% | **7.41** | 10.60 |
| `iterative`（MICE 類） | 100% | 6.77 | 9.86 |

同期同批測站的 PM2.5 標準差是 19.8 μg/m³，所以鄰近測站那個做法**每填一格
平均偏離 0.37 個標準差**。

#### 兩個只有分桶才看得到的結論

**(1) 在完全相同的格子上，鄰站法比內插差 2.8 倍。**
單小時缺口：內插 MAE 2.59、鄰站 7.31。從 30 公里外借值，比把前後兩小時
連成一條直線還差——而且差很多。空間相關性在小時尺度上遠弱於時間自相關。

**(2) 鄰站法的誤差幾乎不隨缺口長度成長。**

| 缺口 | 2–3h | 4–12h | 13–48h | >48h |
|---|---|---|---|---|
| `neighbor` MAE | 7.07 | 7.25 | 7.71 | 8.96 |

缺口長度變化 20 倍，誤差只上升 27%。**這看似穩健，實則相反**——它代表這個方法
不知道問題有多難：它會用同樣的自信填一次三週停機和一個一小時缺口，
而輸出裡沒有任何欄位區分兩者。內插的行為正好相反，誤差隨缺口成長（2.59 → 3.42），
超過設定上限後**拒絕作答**。

> **⚠️ 本專案刻意不回答「這會不會改變最終結論」。**
> 在四種填補下重跑同一個模型會得到四個不同的 R²，看起來可以直接比較——不能。
> 填補唯一改變的是**基準線本來用不了的那些列**，所以每個策略是在不同的測試集上被評分。
> 「多了幾列」這個解釋也撐不住：實測基準線本來就能用 95.2% 的站-小時、
> 鄰站法 95.9%，偏偏是鄰站法拿到最大的 R² 增益。
> 要問得成立需要在策略間固定評分集合、只讓訓練集合變動。
> **一個有混淆的數字比沒有數字更糟，所以此處沒有數字。**

### 4. 實測校正
經由全量大數據測試：
* 實測發現風向 `888` 與 `999` 哨兵碼**僅存在於 1993–2004 年的檔案中**（比例達 2.6–6.4%），在 2005 之後的檔案中完全消失。這解釋了為什麼 2010–2017 這個窗口在錯誤使用連續線性風向時沒有被極端異常值（如 999 角度）直接摧毀模型 ──── 因為這個哨兵碼在該特定年份是不存在的。
* 儘管如此，本專案在 [src/twair/qc/rainfall.py](../src/twair/qc/rainfall.py) 與 [src/twair/qc/sentinels.py](../src/twair/qc/sentinels.py) 中，將品管規格當作一等公民。對於 `RAINFALL` 與 `RAIN_INT`，將 `NR` 強制轉化為物理數值 $0.0$，而對於 `PH_RAIN` 則保留為 `null`（因為沒下雨時，雨水的酸鹼值是未定義的）。

---

## 微型感測器 2025 全年 readiness audit

全年 audit 不先擬合模型，而是先確認是否有可以設計 held-station 與 held-time 實驗的觀測 cohort。
`twair analyze micro-sensor-annual-readiness` 每次只以單日 PM2.5、相對濕度與溫度來源做
DuckDB aggregation，保留每個變數的 source rows、nulls、distinct timestamps、observed hours 與
extreme-range counts。只有三個變數在同一小時都有非 null 值才計為 trio-observed hour；這是
coverage flag，不是補值。每日結果原子寫入 checkpoint，年度彙總再以 bounded DuckDB scan／COPY 建立。

immutable generation `c74ec40428a907e98821efbaf36c36386d2c1b99de69791b49f157eb7947e5bb`
由獨立 verifier 重算 322 個已解析日期；365 日日曆的另 43 個來源目錄缺席日期依然缺席。
輸出共 2,775,609 筆 device-day 與 11,556 個裝置。座標條件要求每筆 PM2.5 座標有效，且全年
longitude／latitude 的最小與最大值相同；座標沒有被平均、修復或移到最近標準站。

| 座標／空間狀態 | 裝置數 |
|---|---:|
| 通過空間篩選 | 1,708 |
| invalid or null coordinate | 6,049 |
| moving coordinate | 3,794 |
| outside Taiwan | 4 |
| missing PM2.5 coordinate | 1 |

因此有 1,708 個裝置通過空間篩選。在明確標為寬鬆的「3 個 active months、30 個 trio dates、
360 個 trio-observed hours、距最近標準站不超過 10 km」條件下，
1,343 個裝置符合寬鬆 eligibility 門檻。ground overlap 只計最近標準站在該小時明確有效且
非 null 的 PM2.5；缺列與有列但 flag 不是 valid 或 PM2.5 為 null 繼續分開計數。

這不是 calibration、不是 bias estimation、不是 sensor fusion；沒有取得衛星資料，也沒有補值。
最近標準站不是微型感測器位置的 colocated ground truth，也沒有建立高解析度 PM2.5 場。
全年 audit 只將下一個 calibration 實驗從「有沒有足夠 cohort」變成可以用 held-station／held-time 設計回答的問題。

## 微型感測器 Q4-supported cross-station agreement protocol

`twair analyze micro-sensor-annual-agreement` 的修訂 protocol 先為每個標準站與本地日期建立唯一的
canonical PM2.5 target。`ground_station_present_hours` 計算該站當日實際存在的逐時列；
`ground_station_eligible_hours` 只計入非 null、finite 且 flag 為 valid 的 PM2.5。eligible hours
至少 18 才計算當日算術平均，否則公開 target 保持 null。這兩個 station-day 欄位不取代既有的
device-specific trio-hour provenance，也不因個別裝置的 PM2.5／溫度／濕度重疊時段不同而改變。

Protocol 固定保留 5 個 held-station、4 個 held-quarter 與 20 個 joint station-quarter fold 定義，
並把每個 fold 明確標為 `scored`、`unscored_empty_train`、`unscored_insufficient_train`、
`unscored_empty_test` 或 `unscored_single_target`。只有 `scored` fold 產生 prediction；score 與 delta
仍為所有 fold 保留列，未評分者的 metric 為 null，並保存 intended 與實際 scored population 的
row count 與 hash。現有支持只允許 Q4 內 held-station 的跨站 agreement；held-quarter 與 joint
station-quarter 不可估，不能宣稱全年 temporal／seasonal generalization、validated calibration、
sensor fusion 或高解析度濃度場。新的 production generation 尚未執行與獨立驗證，因此本節只描述
方法契約，不報告新結果。

## 微型感測器 grouped predictive benchmark

2025-01-01 至 2025-01-25 的 readiness panel 先把每個微型感測器小時配到 primary-radius 內最近的
標準站小時。282,581 筆 primary-radius device-hour（1 km）中，只有 PM2.5、溫度、相對濕度、標準站 PM2.5、
距離與 coverage 都通過既定契約的 271,138 筆進入模型；它們涵蓋 470 個裝置、60 個標準站。
排除列沒有刪除或補值，而是與原因一起保留在 readiness generation。

評估使用兩種互補的 grouped split：

1. **25 個 held-date fold**：每次完整留出一天，避免同一天的裝置小時同時出現在 train 與 test。
2. **10 個 air-zone-aware held-station fold**：同一標準站配到的所有裝置小時一起留出，測量未見地點的
   spatial transfer；分層只平衡官方空品區，不把 test station 的 target 洩漏給模型。

每個 fold 比較三個 predictor：直接使用微型感測器 PM2.5 的 `raw_micro`、只以微型感測器 PM2.5 fit 的
`micro_only` LightGBM，以及加入溫度／相對濕度的 `micro_weather` LightGBM。模型固定 seed、
`n_jobs=1`，且每個 fold 只在 train partition fit；不使用衛星特徵。device-hour 保留裝置為觀測單位，
reference-station-hour 則先在同一標準站小時平均各裝置 prediction，再計算 RMSE／MAE／R²／bias。

| Holdout／尺度 | micro-only − raw：median ΔRMSE | micro+weather − raw：median ΔRMSE | micro+weather − micro-only：median ΔRMSE |
|---|---:|---:|---:|
| held-date／device-hour | −0.497 µg/m³ | **−0.618 µg/m³** | −0.120 µg/m³ |
| held-date／reference-station-hour | −0.577 µg/m³ | −0.480 µg/m³ | −0.039 µg/m³ |
| held-station／device-hour | −0.582 µg/m³ | **−0.649 µg/m³** | +0.022 µg/m³ |
| held-station／reference-station-hour | −0.295 µg/m³ | −0.091 µg/m³ | +0.205 µg/m³ |

表中負值表示 candidate 的 RMSE 較低。結果支持 `micro_only` 相對 raw micro 在這 25 日內的 grouped
predictive improvement；weather 在 held-date 有增益，但沒有改善 held-station 相對 `micro_only` 的
median RMSE。獨立 verifier 從 readiness panel 串行重做 70 次 fit，35 個 fold 與 542,276 筆 prediction
bit-exact；獨立 NumPy 公式的 scores／deltas 最大差為 2.66e-15。

這不是 validated calibration、不是 sensor fusion、不使用衛星特徵，也不是因果、全年／跨季節穩定性、
長期 drift 或高解析度濃度場證據。要進入 calibration，仍需要更長期間與獨立 target；要進入三源融合，
還需要另行定義衛星／地面／微感測器共同特徵、空間產物與完整的留出測站驗證。

### 每月標準站 satellite context 是否增加未見測站的預測資訊

`twair analyze micro-sensor-satellite-value` 延續相同 split，但只讓三個衛星來源都完整的列進入五個
共同 cohort feature sets。generation
`a308372bbbb02ea49362b732579649d498c98831f3ec9a4f7cc07bba1f8ff974` 量得
269,952 筆共同 cohort device-hour、468 個裝置、58 個標準站與 25 日；1,186 筆排除列因標準站 MAIAC
為 null 而留在獨立 ledger，沒有填補。25 個 held-date fold 與 10 個 air-zone-aware held-station fold
共完成 140 次 fit、539,904 筆 prediction。獨立 verifier 串行重做全部 fit，prediction bit-exact；
scores 與 deltas 的最大絕對差分別為 1.78e-15、2.66e-15。

方法比較 `micro_satellite` − `micro_only`，以及 `micro_weather_satellite` − `micro_weather`，因此每一列
只回答加入 satellite context 的增量。兩者在 held-date／device-hour 的 median ΔRMSE 分別為
−0.951、−0.953 µg/m³，各有 25／25 fold 改善；但每月標準站 satellite context 在一月內固定，
且同一標準站同時出現在 train 與 test，這只是次要的 station-descriptor 證據。

held-station 是主要證據：兩個比較的 device-hour median ΔRMSE 分別是
+0.320 µg/m³（3／10 fold 改善）與 +0.127 µg/m³（3／10 fold 改善）；reference-station-hour 則是
+0.488 µg/m³（3／10）與 +0.221 µg/m³（4／10）。正值表示加入衛星 context 後 RMSE 更高，所以這批
一月資料沒有顯示穩定的未見測站增量預測價值。這些值不是微型感測器位置的衛星觀測值、
不是 sensor fusion，也不是 validated calibration、因果／來源歸因、跨季節、drift、future transfer
或高解析度濃度場證據。

---

## Spatial baseline readiness gate

這是一個製作濃度場之前的 baseline readiness gate，不是場模型本身。
2026-08-28 的確認執行指令為：

```powershell
$env:TWAIR_DATA_DIR='D:/twair-data'
uv run twair analyze spatial-surface-baseline --confirm-production
```

獨立 verifier 通過的 immutable generation 是
`620b7ba088906611c191d0f371b5405f8096059cefc488306b6849b64588ef0f`。它固定在
station inventory generation
`58e00bb5ab951c9afd1a95e9e98aacdab4e90762e32904ca6d79d198efe6d788`，並記錄兩個
production input：`outputs/qc/stations.parquet` 為 11,878 bytes、SHA-256
`d7bf35df9ee8de52e8bf4f440af9b7ac2bb829918a8d49b65640f9c2cf3c76dd`；
`processed/monthly/monthly.parquet` 為 6,372,327 bytes、SHA-256
`001f62a25adbbb375ff8e7cdfdbd63f0a480b8eaebc9bff0f9ecc13f93ee793f`。

門檻 cohort 有 59 個測站、1,416 個站月 key、24 個月份（2024–2025）。target ledger
保留 1,415 個 `observed` 與恰好一個 `withheld`：新營 2025-05-01 的 mean 是
null、`meets_threshold=false`。這一列沒有被刪除、填值或當零；59 個測站仍全部
留在 support table。

評估將同一批 target 排入 `buffer_20km`、`buffer_40km` 與 `spatial_cluster`
三個 fold family。每個 fold 比較 `station_mean`、`nearest`、`idw2`、
`kriging_spherical` 與 `kriging_hole_effect` 五種方法。總數是 4,248 列 folds、
21,240 列 predictions、30 列 scores 與 24 列 paired deltas。每個 evaluation 的
2024 fold state 是 708 個 `eligible`；2025 是 707 個 `eligible` 加新營的
`unscored_target_withheld`，reason 是 `target_state=withheld`。五種方法在那列都保留
null prediction/error 與明確狀態，沒有 estimator failure。

相同 intended population 下的分母如下；這些數字對五種方法都相同：

| evaluation | year | intended / scored / failed rows | intended / scored stations | score state |
|---|---:|---:|---:|---|
| `buffer_20km` | 2024 | 708 / 708 / 0 | 59 / 59 | `complete` |
| `buffer_20km` | 2025 | 707 / 707 / 0 | 59 / 59 | `complete` |
| `buffer_40km` | 2024 | 708 / 708 / 0 | 59 / 59 | `complete` |
| `buffer_40km` | 2025 | 707 / 707 / 0 | 59 / 59 | `complete` |
| `spatial_cluster` | 2024 | 708 / 708 / 0 | 59 / 59 | `complete` |
| `spatial_cluster` | 2025 | 707 / 707 / 0 | 59 / 59 | `complete` |

凍結規則先要求 2024／2025 在 20／40 km 的四個 required cells 都有完整
prediction，再要求至少一種 candidate 在四格的 median station MAE delta 都小於 0。
此外，each qualifying method must also have a finite, complete spatial_cluster score
in both 2024 and 2025。實測的 candidate-minus-`station_mean` 配對 delta 為：

| candidate | 20 km 2024 | 20 km 2025 | 40 km 2024 | 40 km 2025 |
|---|---:|---:|---:|---:|
| `idw2` | −1.848481 | −1.729178 | −1.808556 | −1.489642 |
| `kriging_hole_effect` | −1.550237 | −1.586651 | −1.194335 | −1.073457 |
| `kriging_spherical` | −1.575993 | −1.795171 | −1.279487 | −1.098592 |
| `nearest` | −1.418523 | −1.127238 | −1.516523 | −1.537436 |

四種 candidate 都合格，所以 verdict 為 `go`。這句結論只支持「covariate-model
design 可以開始」。這個 generation 沒有產生濃度場或人口暴露結果，也沒有 raster、
來源歸因、校正結果、地圖或網站 payload；`feeds_web=false`。通過 readiness gate 不能
改寫成濃度場、暴露或發布已完成。

## Spatial covariate-model readiness gate

這是 full-domain covariate acquisition 之前的固定模型門檻。2026-08-29 以
`TWAIR_DATA_DIR=D:/twair-data` 執行
`uv run twair analyze spatial-covariate-readiness --confirm-production`，再由獨立 verifier
驗證通過。immutable generation 為
`852db84e74980b8664fdc42da0b3fe30c73af189df4eedbe9b894d0318dbbe38`，綁定的
spatial baseline generation 是
`620b7ba088906611c191d0f371b5405f8096059cefc488306b6849b64588ef0f`，station inventory
generation 是 `58e00bb5ab951c9afd1a95e9e98aacdab4e90762e32904ca6d79d198efe6d788`。

目標 ledger 有 59 站、1,416 個 2024–2025 station-month key、1,415 個
`observed` 與一個新營 2025-05 `withheld`。MAIAC AOD、S5P NO₂、S5P SO₂
的非 null 數分別是 1,309 / 1,416、1,416 / 1,416、1,411 / 1,416；1,306 / 1,416
個 key 三者皆有。模型比較固定為 `covariate_gbm`、`covariate_gbm_idw2`
與 comparator `idw2`，不用門檻結果重調參數。同年 2024 每個 method/cell
都是 708 / 708 / 0 intended / scored / failed；同年 2025 與 `2024_to_2025`
都是 707 / 707 / 0；所有 cell 的測站分母都是 59 / 59。forward ledger 只以
2024 target 訓練，2025 PM2.5 沒有進入訓練；留出測站也不在 train-station list。

| cell | station-clustered MAE：GBM / GBM+IDW² / IDW² | paired median station MAE delta [2.5%, 97.5%]：GBM / GBM+IDW² |
|---|---:|---:|
| `buffer_20km`, same-year 2024 | 2.176 / 2.171 / 2.169 | +0.253 [+0.058, +0.355] / +0.231 [+0.006, +0.360] |
| `buffer_20km`, same-year 2025 | 1.837 / 1.822 / 1.796 | +0.268 [+0.078, +0.475] / +0.259 [+0.056, +0.462] |
| `buffer_40km`, same-year 2024 | 2.311 / 2.300 / 2.572 | +0.016 [−0.471, +0.229] / −0.010 [−0.473, +0.222] |
| `buffer_40km`, same-year 2025 | 2.129 / 2.122 / 2.381 | +0.018 [−0.159, +0.205] / +0.021 [−0.163, +0.197] |
| `spatial_cluster`, same-year 2024 | 2.324 / 2.315 / 3.024 | −0.291 [−0.552, −0.067] / −0.308 [−0.573, −0.077] |
| `spatial_cluster`, same-year 2025 | 2.256 / 2.244 / 2.737 | −0.109 [−0.799, +0.126] / −0.171 [−0.803, +0.124] |
| `buffer_20km`, `2024_to_2025` | 2.347 / 2.350 / 2.630 | −0.058 [−0.243, +0.106] / −0.082 [−0.259, +0.125] |
| `buffer_40km`, `2024_to_2025` | 2.484 / 2.491 / 2.939 | −0.235 [−0.501, +0.022] / −0.240 [−0.484, +0.026] |
| `spatial_cluster`, `2024_to_2025` | 2.601 / 2.601 / 3.247 | −0.353 [−0.637, −0.133] / −0.389 [−0.643, −0.141] |

負的 candidate-minus-`idw2` delta 才偏向 candidate。預註冊規則要求同一 candidate
在四個 20／40 km same-year cell 與三個 `2024_to_2025` cell 的 median 全部小於零。
兩種 candidate 都在 `buffer_20km` same-year cells 為正，故 qualifying methods: none；
verdict: `stop`。沒有放鬆 fold、改模型或改 gate。這是 covariate-model readiness only;
no concentration surface was generated。通過時也只能允許另行設計 full-domain acquisition
與 nested surface，not publication of a map。no prediction interval, support mask,
population-weighted ambient concentration or personal-exposure result。沒有 raster、source
attribution、calibration、fusion 或 website payload；`feeds_web=false`。

---

## 結論

這份重製的成果展示，我們建立的不僅僅是「寫出更漂亮的程式碼」，而是藉由資料工程與嚴謹物理原理：
* 證明了資料工程階段隱蔽決定的巨大科學後果（如 `NR` 偏誤與 circular wind angle 錯誤）。
* 在同一批資料庫、相同的觀測行下，展示了兩條不同的分岔道路：一條在數學與大氣物理學上是破碎的，另一條則是強健、可重複檢驗且具備高度科學價值的。這就是 **air-quality** 再分析平台方法論的核心承諾。
