"""Assemble the Phase 2 report from the analysis outputs.

Deliberately plain Markdown rather than Quarto. Every number here comes from a
Parquet file written by the analysis modules, so the report cannot drift from
the results it describes, and regenerating it needs nothing but Python.

Sections whose inputs are absent are skipped with a note rather than faked, so
a partial run still produces a readable document.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

import polars as pl

from twair.paths import REPORTS_DIR, outputs_dir
from twair.scalars import as_float, opt_float

log = logging.getLogger(__name__)

__all__ = ["build_core_report", "build_spatial_report"]

# The parquet carries the design's internal name; the report is read by people.
# Kept next to the report rather than in the analysis module because it is a
# presentation choice, and because a control the module stops emitting should
# vanish from the table rather than appear under a stale label.
_SPATIAL_CONTROLS: tuple[tuple[str, str, str], ...] = (
    ("pooled", "合併式（未分層）", ""),
    ("zone_era_dummies", "七區截距", ""),
    ("zone_official_today", "今日分區截距", "時代錯置對照"),
    ("within_zone_separate_fits", "字面上的「分區各跑一次」", ""),
    ("station_dummies", "測站固定效果", "天花板"),
)
_SPATIAL_COVARIANCES: tuple[tuple[str, str], ...] = (
    ("iid", "iid（基準模型自己的假設）"),
    ("cluster_month", "聚類月（空間）"),
    ("cluster_station", "聚類站（時間）"),
    ("cluster_twoway", "two-way（CGM）"),
)


def _load(module: str, name: str) -> pl.DataFrame | None:
    path = outputs_dir(module) / f"{name}.parquet"
    if not path.exists():
        log.info("missing %s", path)
        return None
    return pl.read_parquet(path)


def _table(frame: pl.DataFrame, columns: list[str] | None = None, *, limit: int = 40) -> str:
    subset = frame.select(columns) if columns else frame
    subset = subset.head(limit)

    def cell(value: object) -> str:
        if value is None:
            return "—"
        if isinstance(value, float):
            return f"{value:.4f}"
        return str(value)

    header = "| " + " | ".join(subset.columns) + " |"
    divider = "|" + "|".join(["---"] * len(subset.columns)) + "|"
    body = "\n".join("| " + " | ".join(cell(v) for v in row) + " |" for row in subset.iter_rows())
    note = f"\n\n_共 {frame.height} 列，顯示前 {limit} 列。_" if frame.height > limit else ""
    return f"{header}\n{divider}\n{body}{note}"


def _m1_section() -> str:
    panel = _load("m1_baseline", "panel")
    correlations = _load("m1_baseline", "correlations")
    ols = _load("m1_baseline", "ols")
    if panel is None or correlations is None or ols is None:
        return "_基準模型尚未執行。跑 `uv run twair analyze m1`。_\n"

    n_stations = panel["station_name"].n_unique()
    r_squared = float(ols["r_squared"][0])
    # A Polars aggregate is typed as a union of every value a cell could hold;
    # `as_float` converts for real and raises on null rather than formatting a
    # plausible zero into the sentence below.
    worst_vif = as_float(ols.filter(pl.col("vif").is_not_null())["vif"].max())
    leaked = ols.filter(pl.col("term") == "PM10")

    pm10_line = ""
    if not leaked.is_empty():
        pm10_line = (
            f"模型裡最顯著的變數是 PM10，係數 {float(leaked['coefficient'][0]):.4f}、"
            f"t = {float(leaked['t'][0]):.2f}。它在定義上就包含被預測的 PM2.5，"
            "所以那個顯著性不是實證發現——M3 量出這個重疊佔了多少解釋力。\n\n"
        )

    return f"""這條基準**每一步都刻意選錯的那一邊**：2010–2017、月**算術**平均（風向也是）、
不檢查覆蓋率、PM10 當解釋變數、NO 與 NO2 與 NOx 同時進模型。它不使用
`twair.store.aggregate`——那個模組三件事都做對了，而做對就無法當對照組。

沒有這條基準，後面任何「修正之後結論不同」的宣稱都無從驗證：要比較兩種做法，
兩邊必須跑在同一批列上。

**樣本**：{panel.height:,} 個站月，{n_stations} 個測站；完整案例，任一變數缺值即排除。

**配適**：R² = {r_squared:.4f}，最大 VIF {worst_vif:,.0f}。VIF 會到這個量級是因為
NO + NO2 ≡ NOx 是恆等式，設計矩陣在捨入誤差之外是奇異的。

{pm10_line}### 與 PM2.5 的相關係數

{_table(correlations, ["variable", "r"])}

### OLS 係數

{_table(ols, ["term", "coefficient", "std_error", "t", "p", "vif"])}
"""


def _tree_wind_encoding_r2() -> tuple[float, float] | None:
    """The gradient-boosted tree's R² with the raw bearing, and with sin/cos.

    M3's prose states this pair and M2's table computes it, 126 lines apart in
    one generated report. The prose was typed: 「R² 0.537 對 0.524」 beside a
    table that prints 0.5371 from the same frame. `ChapterMethods.astro` is held
    against the payload by `check_published_site_prose`, so the website copy was
    safe and the report's own sentence was not.

    Rolling splits only, matching the table the sentence sits under. `None` when
    M2 has not run, so the sentence can drop its figures rather than state a
    comparison nothing measured.
    """
    scores = _load("m2_drivers", "scores")
    if scores is None:
        return None
    rolling = scores.filter((pl.col("split_kind") == "rolling") & (pl.col("model") == "lightgbm"))

    def mean_r2(feature_set: str) -> float | None:
        # `opt_float` and not `float`: an all-null column is a legitimate answer
        # here — the sentence drops its figures — and a Polars aggregate's
        # declared type is a union that `float` will not take.
        subset = rolling.filter(pl.col("feature_set") == feature_set)
        return None if subset.is_empty() else opt_float(subset["r2"].mean())

    raw, encoded = mean_r2("full_raw_wind"), mean_r2("full")
    return None if raw is None or encoded is None else (raw, encoded)


def _encoding_gap() -> str:
    """The parenthetical the sentence above carries, or nothing to carry."""
    pair = _tree_wind_encoding_r2()
    return "" if pair is None else f"（R² {pair[0]:.3f} 對 {pair[1]:.3f}）"


def _m2_section() -> str:
    scores = _load("m2_drivers", "scores")
    if scores is None:
        return "_M2 尚未執行。跑 `uv run twair analyze m2`。_\n"

    summary = (
        scores.group_by("model", "feature_set", "split_kind")
        .agg(
            pl.col("rmse").mean().alias("rmse"),
            pl.col("mae").mean().alias("mae"),
            pl.col("r2").mean().alias("r2"),
            pl.col("exceedance_f1").mean().alias("f1"),
            pl.len().alias("splits"),
        )
        .sort("split_kind", "rmse")
    )

    rolling = summary.filter(pl.col("split_kind") == "rolling")
    leak_note = _leak_comparison(rolling)

    sections = [
        "M2 用同一批資料，把基準的每一個方法選擇反過來做，並且**一律報告樣本外表現**。",
        "",
        "### 滾動原點驗證（訓練用過去、測試用未來）",
        "",
        _table(rolling, ["model", "feature_set", "rmse", "mae", "r2", "f1", "splits"]),
        "",
        _persistence_note(rolling),
        "",
        leak_note,
    ]

    for kind, title in (("station", "留一測站（空間泛化）"), ("year", "留一年份（氣象異常年）")):
        subset = summary.filter(pl.col("split_kind") == kind)
        if not subset.is_empty():
            sections += [
                "",
                f"### {title}",
                "",
                _table(subset, ["model", "feature_set", "rmse", "r2", "f1", "splits"]),
            ]

    importance = _load("m2_drivers", "importance_full")
    if importance is not None:
        sections += [
            "",
            "### 特徵重要性（mean |SHAP|，不含 PM10 的誠實模型）",
            "",
            _table(importance, limit=20),
        ]

    return "\n".join(sections) + "\n"


def _persistence_note(rolling: pl.DataFrame) -> str:
    """Explain why the simplest baseline tops the table.

    Without this the table reads as "the models are useless", which is the
    wrong conclusion from the right numbers.
    """
    persistence = rolling.filter(pl.col("model") == "persistence")
    honest = rolling.filter(pl.col("feature_set") == "full")
    if persistence.is_empty() or honest.is_empty():
        return ""

    return f"""#### 為什麼最笨的基準排第一

persistence（「這小時等於上小時」）以 R² {float(persistence["r2"][0]):.3f} 遠勝所有模型的
{float(honest["r2"][0]):.3f}。這不是模型沒用，而是**兩者回答的問題不同**：

- persistence 用的是 **PM2.5 自己前一小時的值**
- 所有 LightGBM 模型**完全沒有 PM2.5 的歷史**，只有同一小時的其他測項與氣象

逐時 PM2.5 的自相關極高，所以只要允許使用前一小時，就能贏過任何只看當下共變數的模型。

這給出兩個明確結論：

1. **本節的模型是解釋性的，不是預報用的。** 它們回答「這小時的濃度由什麼構成」，
   而非「下小時會是多少」。
2. **要做預報就必須加入落後項。** 這是 Phase 7 的工作，屆時 persistence 是
   必須超越的門檻，而不是拿來比較的對象。

月平均面板同樣沒有落後項，也同樣沒有報告任何基準——所以看不見這一點。"""


def _leak_comparison(rolling: pl.DataFrame) -> str:
    """Price the PM10 leak: the same model with and without it."""
    honest = rolling.filter(pl.col("feature_set") == "full")
    leaking = rolling.filter(pl.col("feature_set") == "full_with_pm10")
    if honest.is_empty() or leaking.is_empty():
        return ""

    r2_honest = float(honest["r2"][0])
    r2_leaking = float(leaking["r2"][0])
    rmse_honest = float(honest["rmse"][0])
    rmse_leaking = float(leaking["rmse"][0])

    share = 100.0 * (r2_leaking - r2_honest) / r2_leaking if r2_leaking else float("nan")

    return f"""#### PM10 洩漏的代價

PM2.5 在定義上是 PM10 的子集，用後者預測前者是恆等式而非發現。
同一個模型加上 PM10 之後：

| | 不含 PM10 | 含 PM10 | 差異 |
|---|---|---|---|
| R² | {r2_honest:.4f} | {r2_leaking:.4f} | **+{r2_leaking - r2_honest:.4f}** |
| RMSE | {rmse_honest:.3f} | {rmse_leaking:.3f} | {rmse_leaking - rmse_honest:+.3f} |

也就是說，含 PM10 的模型有 **{share:.0f}%** 的解釋力來自這個定義上的重疊，
而不是來自任何關於 PM2.5 成因的知識。基準裡 PM10 那個極高的 t 值
正是這一項在撐場面。"""


def _m3_section() -> str:
    normality = _load("m3_pitfalls", "normality_by_sample_size")
    wind = _load("m3_pitfalls", "wind_summary")
    sectors = _load("m3_pitfalls", "wind_by_sector")
    wind_linear = _load("m3_pitfalls", "wind_linear_model_encoding")
    leakage = _load("m3_pitfalls", "leakage_price")
    validation = _load("m3_pitfalls", "validation_in_vs_out_of_sample")
    variance = _load("m3_pitfalls", "diurnal_variance")
    stability = _load("m3_pitfalls", "collinearity_stability")

    if all(x is None for x in (normality, wind, variance, stability)):
        return "_M3 尚未執行。跑 `uv run twair analyze m3`。_\n"

    parts = ["同一批資料，兩種做法，兩個結論。"]

    if variance is not None:
        # `station_month` and not `monthly_mean`: the baseline averages to
        # one value per station per month, and `monthly_mean` additionally pools
        # all 78 stations into one national series. Quoting the national figure
        # here charged the original with losing variance it kept — 20.3% where
        # its own aggregation retains 40.3%. Both rows still ship; this line
        # names the one the comparison is about.
        own = variance.filter(pl.col("scale") == "station_month")["variance_retained"]
        pooled = variance.filter(pl.col("scale") == "monthly_mean")["variance_retained"]
        if len(own) and len(pooled):
            parts += [
                "",
                "### 陷阱 1：月平均抹掉了什麼",
                "",
                f"基準把逐時資料平均成「每站每月一個值」，變異只剩 "
                f"**{100 * float(own[0]):.1f}%**。日夜循環、週末效應、污染事件"
                f"全部消失在平均裡。",
                "",
                f"若再把各站併成單一全台月序列，只剩 **{100 * float(pooled[0]):.1f}%**"
                f"——兩者的差額是**測站之間**的變異，那一半基準並沒有丟掉。",
                "",
                _table(variance),
            ]

    if wind is not None and sectors is not None:
        parts += [
            "",
            "### 陷阱 3：方位角不是數字",
            "",
            _table(wind),
            "",
            "線性相關係數很小，但分方位看濃度差距很大。線性處理會得到很小的值並稱「無相關」——",
            "問題不在那個數字小，而在**那個方法只可能產生小數字**，無論真實的方向依賴多強。",
            "",
            _table(sectors, limit=12),
        ]

    if wind_linear is not None:
        raw = wind_linear.filter(pl.col("encoding") == "raw_bearing")
        encoded = wind_linear.filter(pl.col("encoding") == "sin_cos")
        ratio = float(encoded["r2_relative_to_raw_bearing"][0]) if not encoded.is_empty() else 0.0
        parts += [
            "",
            "#### 但要說清楚：這個問題只對線性模型致命",
            "",
            "M2 的比較產生了一個與預期相反、必須直說的結果：**梯度提升樹用原始方位角",
            f"反而略勝** sin/cos 編碼{_encoding_gap()}。樹可以對同一變數反覆切分，",
            "把 0–360 切成任意多段，跨越 0°/360° 的不連續幾乎不造成損失。",
            "",
            "這並不能為線性處理解套——基準用的正是 Pearson 相關與 OLS。",
            "在**同一批資料上用 OLS** 比較兩種編碼：",
            "",
            _table(
                wind_linear,
                ["encoding", "n_terms", "r_squared", "rmse", "r2_relative_to_raw_bearing"],
            ),
            "",
            f"線性模型下 sin/cos 的解釋力是原始方位角的 **{ratio:.2f} 倍**"
            f"（R² {float(raw['r_squared'][0]):.4f} → {float(encoded['r_squared'][0]):.4f}）。",
            "編碼方式在線性模型這個家族裡是決定性的，在樹模型裡不是。",
        ]

    if leakage is not None:
        share = leakage.filter(pl.col("feature_set") == "leak_share_of_r2")
        parts += [
            "",
            "### 陷阱 2：PM10 洩漏的定價",
            "",
            _table(leakage),
        ]
        if not share.is_empty() and share["r2"][0] is not None:
            parts += [
                "",
                f"含 PM10 的模型有 **{100 * float(share['r2'][0]):.1f}%** 的解釋力"
                "來自這個定義上的重疊。",
            ]

    if validation is not None:
        optimism = validation.filter(pl.col("evaluated_on") == "optimism_r2")
        parts += [
            "",
            "### 陷阱 6：樣本內與樣本外",
            "",
            "以 AIC、BIC 選模時，兩者都算在配適用的同一批資料上。",
            "同一個模型分別在訓練列與未見過的未來列上評分：",
            "",
            _table(validation),
        ]
        if not optimism.is_empty():
            parts += [
                "",
                f"樂觀偏誤 **{float(optimism['r2'][0]):.3f} R²**——這正是樣本內選模無法回報的量。",
            ]

    if normality is not None:
        parts += [
            "",
            "### 陷阱 4：大樣本下的常態檢定",
            "",
            "在**已知真相**的合成資料上示範：真常態資料在任何樣本數都不被拒絕；",
            "輕微偏態的資料在 N=100 通過、N≥1,000 全部被拒絕。同一個分布，只有 N 變了。",
            "",
            _table(normality, ["n", "data", "p_value", "rejected_at_0.05", "skewness"]),
        ]

        parts += [
            "",
            "#### 調低 α 不是補救措施",
            "",
            "遇到常態檢定被拒絕時，把拒絕標準從 0.05 降到 0.01 是常見的處理方式，",
            "但它對這個問題無效：大樣本下 p 值本來就會遠小於 0.001，",
            "在 0.01 之下同樣被拒絕。改變的是門檻，不是資料的分布形狀。",
        ]

    if stability is not None:
        parts += [
            "",
            "### 陷阱 5：共線性讓係數變成任意的",
            "",
            "`NO + NO2 = NOx` 是恆等式，三者同時進模型就沒有唯一解。",
            "重抽樣重配適後，這三個係數劇烈擺盪，而一個可識別的變數幾乎不動。",
            "逐步剔除會看著正負號一輪一輪翻轉，很容易把翻轉當成關於氮化學的發現。",
            "",
            _table(stability),
        ]

    return "\n".join(parts) + "\n"


def _spatial_missing(step: str) -> str:
    return f"_M6 的 `{step}` 尚未產出。跑 `uv run twair analyze m6`。_\n"


def _signed(value: float, places: int = 3) -> str:
    """Render with an explicit sign, because the sign is the finding here."""
    return f"{value:+.{places}f}"


def _quote(text: str) -> str:
    """Prefix every line, so an interpolated block stays inside the blockquote."""
    return "\n".join(f"> {line}" if line else ">" for line in text.strip().splitlines())


def _spatial_partition_section() -> str:
    price = _load("m6_spatial", "partition_price")
    if price is None:
        return _spatial_missing("partition_price")

    rows = []
    by_control = {row["control"]: row for row in price.iter_rows(named=True)}
    for key, label, aside in _SPATIAL_CONTROLS:
        row = by_control.get(key)
        if row is None:
            continue
        name = f"{label}（{aside}）" if aside else label
        interval = ""
        if row["mean_i_lo"] is not None and row["mean_i_hi"] is not None:
            interval = f"（{_signed(float(row['mean_i_lo']))}, {_signed(float(row['mean_i_hi']))}）"
        # The row with the least residual dependence is the finding, so it is
        # emphasised — found by comparison rather than by remembering which one
        # won, because which one wins is exactly what a re-run could change.
        best = float(row["mean_i"]) == min(float(other["mean_i"]) for other in by_control.values())
        mark = "**" if best else ""
        rows.append(
            f"| {name} | {row['design_columns']} | {float(row['r_squared']):.4f} | "
            f"{mark}{_signed(float(row['mean_i']))}{mark}{interval} | "
            f"{mark}{row['months_significant_bh']}/{row['months_scored']}{mark} |"
        )

    pooled = by_control.get("pooled")
    separate = by_control.get("within_zone_separate_fits")
    measured = ""
    if pooled is not None and separate is not None:
        measured = (
            f"每月殘差 Moran's I 從合併式的 {_signed(float(pooled['mean_i']))}\n"
            f"降到分區各自配適的 {_signed(float(separate['mean_i']))}——\n"
        )

    return f"""缺陷清單把 D7 記成「78 個測站的空間維度沒用」。量測之後這句話被修正：
{measured}按空品區分層、每區各跑一次本來就**有**用空間。可檢定的問題是
**那個分層是不是足夠的空間控制**，而答案分成兩半。

## 一、分層本身：大部分有效

基準模型（M1）的殘差在五種空間控制下的每月 Moran's I 平均
（`partition_price.parquet`）：

| 控制 | 參數 | R² | 平均 I（95% CI） | BH 顯著月數 |
|---|---|---|---|---|
{chr(10).join(rows)}

分區分層比測站固定效果更有效（斜率也隨區變）。差值不寫成比例：I 是正規化
相關，沒有可加分解性質。
"""


def _spatial_inference_section() -> str:
    price = _load("m6_spatial", "inference_price")
    if price is None:
        return _spatial_missing("inference_price")

    pm10 = price.filter(pl.col("term") == "PM10")
    by_cov = {row["cov_type"]: row for row in pm10.iter_rows(named=True)}
    rows = []
    for key, label in _SPATIAL_COVARIANCES:
        row = by_cov.get(key)
        if row is None:
            continue
        name = label
        if key == "cluster_twoway":
            fixed = "觸發" if row["psd_fix_applied"] else "未觸發"
            name = f"{label}，{fixed} PSD 修復"
        emphasis = "**" if key == "cluster_twoway" else ""
        rows.append(
            f"| {name} | {emphasis}{float(row['t']):.2f}{emphasis} | "
            f"×{float(row['se_inflation_vs_iid']):.2f} |"
        )

    # Which terms the correction actually costs, rather than a remembered list.
    def significant(cov: str) -> set[str]:
        subset = price.filter((pl.col("cov_type") == cov) & (pl.col("p") < 0.05))
        return set(subset["term"].to_list())

    dropped = significant("iid") - significant("cluster_twoway")
    # Design-matrix order, not alphabetical: a reader comparing this list against
    # the fit should not have to re-sort it in their head.
    lost = [term for term in price.filter(pl.col("cov_type") == "iid")["term"] if term in dropped]
    lost_line = ""
    if lost:
        wind = (
            "\n最後一項即 D3——同一個係數上，編碼錯誤（D3）與推論錯誤（D7）疊加。"
            if lost[-1] == "WD_HR"
            else ""
        )
        lost_line = f"\n\ntwo-way 下失去顯著：{'、'.join(lost)}。{wind}"

    return f"""## 二、但 t 值通常報自合併式模型

同一設計、只換共變異估計量（`inference_price.parquet`），看 PM10 這一項：

| 共變異 | t(PM10) | SE 膨脹 |
|---|---|---|
{chr(10).join(rows)}{lost_line}
"""


def _spatial_distance_section() -> str:
    correlogram = _load("m6_spatial", "correlogram")
    lisa = _load("m6_spatial", "lisa")
    if correlogram is None:
        return _spatial_missing("correlogram")

    near = correlogram.sort("bin_lo_km").row(0, named=True)
    far = correlogram.sort("i").row(0, named=True)
    sign_change = "反號至" if float(far["i"]) < 0 <= float(near["i"]) else "降至"

    lisa_line = ""
    if lisa is not None:
        lisa_line = (
            f"\n\nLISA（`lisa.parquet`）：{lisa.height} 站中 raw 顯著 "
            f"{int(lisa['significant_raw'].sum())} 站、"
            f"**BH 校正後 {int(lisa['significant_bh'].sum())} 站**。\n"
            "相依是整個場的性質，不是幾個可剔除的「異常站」。"
        )

    near_band = f"{float(near['bin_lo_km']):.0f}–{float(near['bin_hi_km']):.0f} km"
    far_band = f"{float(far['bin_lo_km']):.0f}–{float(far['bin_hi_km']):.0f} km"

    # Which bands survive BH, named rather than left for the reader to infer
    # from a z. Restoring one station to the network moved two near bands from
    # significant to not, so reporting z alone would have hidden a real change.
    surviving = correlogram.filter(pl.col("significant_bh"))
    bands = "、".join(
        f"{float(row['bin_lo_km']):.0f}–{float(row['bin_hi_km']):.0f} km"
        for row in surviving.sort("bin_lo_km").iter_rows(named=True)
    )
    survival = (
        f"\n\nBH 校正後仍顯著的距離帶有 {surviving.height}/{correlogram.height} 個："
        f"{bands}。近距離的正相關本身沒有通過校正，所以本節的證據是"
        "**符號隨距離改變**，不是「近處顯著正相關」。"
        if surviving.height
        else ""
    )

    return f"""## 三、距離結構：偶極，不是一團正相關

站均殘差的 correlogram（`correlogram.parquet`）在 {near_band} 為
{_signed(float(near["i"]))}（z={_signed(float(near["z"]), 2)}）、{far_band} {sign_change}
**{_signed(float(far["i"]))}（z={_signed(float(far["z"]), 2)}）**。北部與南部殘差反向
共變——這也是為什麼本報告不發表單一的 Moran's I。{survival}{lisa_line}
"""


def _spatial_agreement_section() -> str:
    agreement = _load("m6_spatial", "partition_agreement")
    if agreement is None:
        return _spatial_missing("partition_agreement")

    official = agreement.filter(pl.col("partition") == "zone_era")
    ward = agreement.filter(pl.col("partition").str.starts_with("ward_k"))
    if official.is_empty() or ward.is_empty():
        return _spatial_missing("partition_agreement")

    row = official.row(0, named=True)
    best = ward.sort("silhouette", descending=True).row(0, named=True)
    # A Polars aggregate is typed as the union of every value a cell could hold,
    # including bytes, so mypy refuses to format it directly.
    k_lo = int(as_float(ward["k"].min()))
    k_hi = int(as_float(ward["k"].max()))
    matched = ward.filter(pl.col("k") == row["k"])
    ari_line = ""
    if not matched.is_empty():
        ari_line = f"，官方 k 下一致性 ARI {float(matched['ari_vs_zone_era'][0]):.2f}"

    return f"""## 四、官方分區 vs 純地理

虛無是 {row["ensemble_draws"]} 個只知道地理的隨機 Voronoi 分割（隨機重貼標籤任何連續分割都贏，
不成其為檢定）。時代七區＋離島桶（`partition_agreement.parquet`）：

- 區內−區間相關差 {_signed(float(row["separation_r"]))}，居 ensemble
  **{100 * float(row["pct_vs_geographic_ensemble_separation"]):.1f} 百分位**——分區載有超出鄰接性的資訊，不是亂劃的；
- 但 silhouette 僅 {_signed(float(row["silhouette"]))}，Ward 掃 k={k_lo}..{k_hi} 偏好 **k={best["k"]}**
  （silhouette {_signed(float(best["silhouette"]))}；北群對南群）{ari_line}。

**一句話：分區不是亂劃、但比資料支持的粒度細。**
{_spatial_climatology_note()}"""


def _spatial_climatology_note() -> str:
    """Why the correlation is taken on anomalies, priced in both directions."""
    metadata = _load("m6_spatial", "metadata")
    if metadata is None:
        return ""
    meta = {row["key"]: row["value"] for row in metadata.iter_rows(named=True)}
    raw = meta.get("station_correlation_raw")
    anomaly = meta.get("station_correlation_anomaly")
    if raw is None or anomaly is None:
        return ""

    return f"""
相關取於距島均值的異常量，不是生料。生料上全島測站兩兩平均相關
**{float(raw):.3f}**——大家共用同一個冬季高峰，任何分割都會看起來一致；
扣掉逐月全島均值之後降到 **{float(anomaly):.3f}**，這一步才讓比較是關於分區
而不是關於季風。兩個值都由 `metadata.parquet` 每次執行重算。
"""


def _spatial_spacing_clause() -> str:
    """The nearest-neighbour range, which is why 1 km is refused."""
    coverage = _load("m6_spatial", "station_coverage")
    if coverage is None or "km_nearest_neighbour" not in coverage.columns:
        return "測站間距遠大於一個 1 km 的格子"
    spacing = coverage["km_nearest_neighbour"].drop_nulls()
    if spacing.len() == 0:
        return "測站間距遠大於一個 1 km 的格子"
    return (
        f"本島 {spacing.len()} 個有座標測站的最近鄰距離跨越兩個數量級——\n"
        f"從 **{as_float(spacing.min()):.1f} km** 到 **{as_float(spacing.max()):.0f} km**，"
        f"中位數 {as_float(spacing.median()):.1f} km"
    )


def _spatial_field_section() -> str:
    field = _load("m6_spatial", "field_skill")
    if field is None:
        return _spatial_missing("field_skill")

    buffers = sorted({float(v) for v in field["buffer_km"].to_list() if v})
    methods = sorted(set(field["method"].to_list()))
    kriging = [name for name in methods if name.startswith("kriging")]
    failures = field["failed"].drop_nulls().len()

    return f"""## 五、測站之間的空白（`field_skill.parquet`）

1 km 濃度場**不出**：{_spatial_spacing_clause()}，
所以 1 km 的格子宣稱了網路給不起的解析度。這個間距由
`station_coverage.parquet` 的 `km_nearest_neighbour` 每次執行重算。

取而代之量補值技巧本身：留一站（樂觀上界）與 {"/".join(f"{value:.0f}" for value in buffers)} km
緩衝交叉驗證，涵蓋 {field["station_name"].n_unique()} 站 × {field["month"].n_unique()} 個月，
比較 {len(methods)} 種方法（{"、".join(methods)}），
其中 {len(kriging)} 個變異圖家族並列、逐 fold 重配。
本次執行記錄到 {failures} 個失敗 fold。數字見網站第三章與 parquet。
"""


def _spatial_header() -> str:
    metadata = _load("m6_spatial", "metadata")
    if metadata is None:
        return "所有數字由 `uv run twair analyze m6` 產出，表格存於 `data/outputs/m6_spatial/*.parquet`。"

    meta = {row["key"]: row["value"] for row in metadata.iter_rows(named=True)}
    return f"""所有數字由 `uv run twair analyze m6` 產出（seed {meta.get("seed", "?")}、
{meta.get("residual_null_draws", "?")} 次模擬虛無、{meta.get("weights", "?")}、BH 校正），
表格存於 `data/outputs/m6_spatial/*.parquet`。面板為
{meta.get("panel_stations", "?")} 站 × {meta.get("panel_months", "?")} 個月，
其中 {meta.get("panel_stations_placed", "?")} 站有座標、{meta.get("panel_stations_complete", "?")} 站每月都回報。"""


def build_spatial_report() -> Path:
    """Write ``reports/03-spatial.md`` from the M6 outputs."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    destination = REPORTS_DIR / "03-spatial.md"

    generated = datetime.now(UTC).isoformat(timespec="seconds")
    content = f"""# 03 — 空間結構：「分區各跑一次」買到了什麼

> 由 `uv run twair report spatial` 產生於 {generated}。
> 本報告採 Markdown，與 `reports/01-core.md` 使用同一套公開報告契約；repo 不使用
> Quarto 工具鏈。這項交付取代了早期 `.qmd` 藍圖，判定記在 [PLAN.md](../PLAN.md) 的 Phase 5。
>
{_quote(_spatial_header())}
> 方法推導見 [docs/methodology.md](../docs/methodology.md) 的 D7 節。

## 問題的修正

{_spatial_partition_section()}
{_spatial_inference_section()}
{_spatial_distance_section()}
{_spatial_agreement_section()}
{_spatial_field_section()}
## 範圍限制

1. 本報告只為 **OLS 階段**定價。修正的是「誤差互相獨立」這個假設，而 t 值
   不等於整個推論；帶 AR(1) 誤差結構的模型有自己的標準誤，本 repo 沒有計算。
2. 殘差 I 是場相依的**下界**（解釋變數自帶空間結構；面板被完整度篩選）。
3. **不做**人口加權暴露：repo 無人口網格。所需輸入與來源要求記錄於
   `conf/spatial.yaml`。
"""
    destination.write_text(content, encoding="utf-8")
    return destination


def build_core_report() -> Path:
    """Write ``reports/01-core.md`` from whatever analysis outputs exist."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    destination = REPORTS_DIR / "01-core.md"

    generated = datetime.now(UTC).isoformat(timespec="seconds")
    content = f"""# 核心分析：基準、重做、對照

> 由 `uv run twair report core` 產生於 {generated}。
> 每一個數字都來自 `data/outputs/` 下的 Parquet，報告不會與結果脫節。

---

## M1 — 刻意有缺陷的月平均基準

{_m1_section()}

---

## M2 — 逐時重做

{_m2_section()}

---

## M3 — 方法學對照

{_m3_section()}
"""
    destination.write_text(content, encoding="utf-8")
    return destination
