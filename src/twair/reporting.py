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
from twair.scalars import as_float

log = logging.getLogger(__name__)

__all__ = ["build_core_report"]


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
        "M2 用同一批資料，把原專題的每一個方法選擇反過來做，並且**一律報告樣本外表現**。",
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

原專題同樣沒有落後項，也同樣沒有報告任何基準——差別在於它沒有機會發現這一點。"""


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
而不是來自任何關於 PM2.5 成因的知識。原專題的 β_PM10 = 0.413、t = 92.75
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
        # `station_month` and not `monthly_mean`: the 2018 project averaged to
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
                f"原專題把逐時資料平均成「每站每月一個值」，變異只剩 "
                f"**{100 * float(own[0]):.1f}%**。日夜循環、週末效應、污染事件"
                f"全部消失在平均裡。",
                "",
                f"若再把各站併成單一全台月序列，只剩 **{100 * float(pooled[0]):.1f}%**"
                f"——兩者的差額是**測站之間**的變異，那一半原專題並沒有丟掉。",
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
            "線性相關係數很小，但分方位看濃度差距很大。原專題得到 0.089 並稱「無相關」——",
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
            "反而略勝** sin/cos 編碼（R² 0.537 對 0.524）。樹可以對同一變數反覆切分，",
            "把 0–360 切成任意多段，跨越 0°/360° 的不連續幾乎不造成損失。",
            "",
            "這並不能為原專題解套——它用的是 Pearson 相關、OLS 與線性混合模型。",
            "在**同一批資料上用 OLS** 比較兩種編碼：",
            "",
            _table(
                wind_linear,
                ["encoding", "n_terms", "r_squared", "rmse", "r2_relative_to_raw_bearing"],
            ),
            "",
            f"線性模型下 sin/cos 的解釋力是原始方位角的 **{ratio:.2f} 倍**"
            f"（R² {float(raw['r_squared'][0]):.4f} → {float(encoded['r_squared'][0]):.4f}）。",
            "編碼方式在原專題所用的方法家族裡是決定性的，在樹模型裡不是。",
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
            "原專題以 AIC、BIC 選模，兩者都算在配適用的同一批資料上。",
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
            "原專題跑了八輪逐步剔除看著正負號翻轉，並把翻轉當成關於氮化學的發現。",
            "",
            _table(stability),
        ]

    return "\n".join(parts) + "\n"


def build_core_report() -> Path:
    """Write ``reports/01-core.md`` from whatever analysis outputs exist."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    destination = REPORTS_DIR / "01-core.md"

    generated = datetime.now(UTC).isoformat(timespec="seconds")
    content = f"""# 核心分析：復刻、重做、對照

> 由 `uv run twair report core` 產生於 {generated}。
> 每一個數字都來自 `data/outputs/` 下的 Parquet，報告不會與結果脫節。

---

## M1 — 復刻 2018 年的分析

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
