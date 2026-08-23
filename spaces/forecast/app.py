"""air-quality — PM2.5 forecast demo.

This Space exists to show a comparison, not a number.

The backtest behind it (74 stations, 2015–2025, rolling-origin) found that the
model beats persistence at every horizon *and* decays toward climatology by two
days out. Both halves matter, and a demo that printed a single predicted value
would hide the second one. So every result here shows four things at once: what
the model said, what "the same as now" said, what "the average for this station,
this month, this hour" said, and what the concentration actually turned out to
be.

The demo year is genuinely held out — the models were fitted on 2015–2024 and
have never seen these rows.
"""

from __future__ import annotations

import json
from pathlib import Path

import gradio as gr
import lightgbm as lgb
import numpy as np
import polars as pl

HERE = Path(__file__).parent
MANIFEST = json.loads((HERE / "data" / "manifest.json").read_text(encoding="utf-8"))

FEATURES: list[str] = MANIFEST["features"]
HORIZONS: list[int] = MANIFEST["horizons"]
TARGET: str = MANIFEST["target"]

DEMO = pl.read_parquet(HERE / "data" / "demo.parquet")
CLIMATOLOGY = pl.read_parquet(HERE / "data" / "climatology.parquet")

# Loaded once. The text format carries no code, so this is just parsing.
# Read in Python and passed as a string rather than by path: LightGBM's C
# library resolves paths through the platform's ANSI code page, which fails on
# any non-ASCII directory name.
BOOSTERS = {
    h: lgb.Booster(
        model_str=(HERE / MANIFEST["trained"][str(h)]["file"]).read_text(encoding="utf-8")
    )
    for h in HORIZONS
}

STATIONS = sorted(DEMO["station_name"].unique().to_list())

# The full backtest: 74 stations, 2015-2025, four rolling-origin splits.
SKILL = {
    1: {"r2": 0.859, "persistence": 0.172, "climatology": 0.837, "rmse": 4.04},
    6: {"r2": 0.576, "persistence": 0.237, "climatology": 0.508, "rmse": 7.05},
    24: {"r2": 0.317, "persistence": 0.196, "climatology": 0.207, "rmse": 8.90},
    48: {"r2": 0.289, "persistence": 0.315, "climatology": 0.174, "rmse": 9.15},
}

# And the same measurement on the six stations and one year bundled here, which
# does not agree. At six hours the sample has the model *losing* to persistence
# where the backtest has it winning. Showing only the headline would leave a
# reader clicking through six-hour examples watching the model lose while the
# page insisted it wins, so both ship and the app says which is which.
DEMO_SKILL = MANIFEST.get("demo_skill", {"overall": {}, "by_station": {}})


def _climatology_for(station: str, month: int, hour: int) -> float | None:
    match = CLIMATOLOGY.filter(
        (pl.col("station_name") == station) & (pl.col("month") == month) & (pl.col("hour") == hour)
    )
    if match.is_empty():
        return None
    value = match["climatology"][0]
    return None if value is None else float(value)


# Both the dropdown labels and the lookup are built with this one format.
# They were not, and nothing worked: Python's `str(datetime)` gives
# "2025-07-12 07:00:00" while Polars' `cast(Utf8)` on a microsecond Datetime
# gives "2025-07-12 07:00:00.000000", so every lookup missed and every query
# answered "no complete record". Neither language's default repr is a contract.
STAMP = "%Y-%m-%d %H:%M"


def _rows_for(station: str) -> pl.DataFrame:
    return DEMO.filter(pl.col("station_name") == station).sort("ts_local")


def _labelled(station: str) -> pl.DataFrame:
    return _rows_for(station).with_columns(pl.col("ts_local").dt.strftime(STAMP).alias("_label"))


def timestamp_choices(station: str) -> list[str]:
    """Every 24th *available* record, so the dropdown is navigable.

    Not every 24 hours, which is what the label used to claim. Rows without a
    complete feature set are already gone, so after an outage the spacing
    stretches — 01-06 13:00 is followed by 01-10 15:00. Sampling by position is
    fine; describing it as a daily sample was not.
    """
    labels = _labelled(station)["_label"].to_list()
    return labels[::24] or labels


def timestamps_for(station: str) -> gr.Dropdown:
    sampled = timestamp_choices(station)
    return gr.Dropdown(choices=sampled, value=sampled[0] if sampled else None)


def forecast(station: str, timestamp: str, horizon: int) -> tuple[str, str]:
    """Run every method on one held-out hour and report them side by side."""
    if not station or not timestamp:
        return "Pick a station and a time.", ""

    rows = _labelled(station).filter(pl.col("_label") == timestamp)
    if rows.is_empty():
        return f"這個樣本裡沒有 {timestamp} 的完整紀錄。", ""

    row = rows.head(1)
    now = float(row[TARGET][0])
    moment = row["ts_local"][0]

    model_value = float(np.asarray(BOOSTERS[horizon].predict(row.select(FEATURES).to_numpy()))[0])
    persistence = float(row[f"{TARGET}_lag1"][0])
    clim = _climatology_for(station, moment.month, moment.hour)
    truth_column = f"truth_h{horizon}"
    truth = row[truth_column][0]
    truth_value = None if truth is None else float(truth)

    lines = [
        f"### {station} · {moment:%Y-%m-%d %H:%M} · {horizon} 小時後",
        "",
        f"此刻觀測 **{now:.1f}** μg/m³",
        "",
        "| 方法 | 預測 μg/m³ | 誤差 |",
        "|---|---:|---:|",
    ]

    def err(value: float | None) -> str:
        if value is None or truth_value is None:
            return "—"
        return f"{value - truth_value:+.1f}"

    band = MANIFEST["trained"][str(horizon)].get("band")
    if band:
        half = float(band["half_width"])
        lines.append(
            f"| **模型** | **{model_value:.1f}** "
            f"（{model_value - half:.1f}–{model_value + half:.1f}） | **{err(model_value)}** |"
        )
    else:
        lines.append(f"| **模型** | **{model_value:.1f}** | **{err(model_value)}** |")
    lines.append(f"| persistence（跟現在一樣） | {persistence:.1f} | {err(persistence)} |")
    if clim is not None:
        lines.append(f"| climatology（這站這時候的平均） | {clim:.1f} | {err(clim)} |")

    if truth_value is None:
        lines.append("")
        lines.append("實際值不在這份樣本裡（樣本在該時刻之後就結束了）。")
    else:
        lines.append(f"| **實際發生** | **{truth_value:.1f}** | — |")

    if band:
        lines.append("")
        lines.append(
            f"括號是名目 {float(band['nominal']):.0%} 的 split conformal 區間，半寬 "
            f"±{float(band['half_width']):.1f} μg/m³，由訓練期尾段 "
            f"{int(band['calibration_rows']):,} 列校準。"
        )
        lines.append("")
        lines.append(
            "**這個水準是要求的，不是保證的。** 共形的保證是邊際的，並且假設校準與"
            "測試可交換；逐時 PM2.5 不是。回測在每一個期距都量到四個分割中有一個"
            "低於名目，而且每次都是同一個分割。這裡沒有留出期可以再量一次，所以"
            "這條區間該讀成一個尺度，不是一個承諾。"
        )

    s = SKILL.get(horizon)
    if s is None:
        return "\n".join(lines), ""

    sample = DEMO_SKILL["overall"].get(str(horizon))
    here = DEMO_SKILL["by_station"].get(station, {}).get(str(horizon))

    note = [
        "#### 一列資料證明不了什麼",
        "",
        "模型在某一小時贏過 persistence，可能只是運氣。所以下面給兩組 skill——"
        "**它們並不一致，而那個不一致本身就是重點。**",
        "",
        "| 量在哪批資料上 | 對 persistence 的 skill |",
        "|---|---:|",
        f"| 完整回測（74 站、2015–2025、約 178 萬列） | **{s['persistence']:+.3f}** |",
    ]
    if sample is not None:
        note.append(
            f"| 這個 demo 的樣本（6 站、2025 一年、{sample['n']:,} 列） "
            f"| **{sample['skill_persistence']:+.3f}** |"
        )
    if here is not None:
        note.append(f"| 只算 {station} 這一站 | **{here:+.3f}** |")

    note += [
        "",
        "skill = 1 − MSE(模型) ÷ MSE(基準線)。0 代表跟基準線一樣，負的代表更差。",
        "",
        f"完整回測在這個期距的其他數字：對 climatology 的 skill {s['climatology']:+.3f}、"
        f"R² {s['r2']:.3f}、平均 RMSE {s['rmse']:.2f} μg/m³。",
    ]

    if horizon == 6:
        note += [
            "",
            "⚠️ **6 小時是這個模型最不穩的期距。** 完整回測的四個分割從 −0.111 到 +0.303，"
            "跨度 0.41，是四個期距裡最大的（1 小時只有 0.07）。"
            "而在這份 demo 的樣本上，六個測站沒有一個明顯為正。"
            "\n\n這大概是因為 6 小時卡在兩種訊號中間：太遠，此刻的漲跌動量已經用完；"
            "又太近，明天同一時刻的日夜循環還沒接上。",
        ]
    if horizon >= 48:
        note += [
            "",
            "⚠️ **48 小時這一欄要小心。** 對 persistence 的 skill 是四個期距裡最高的，"
            "但對 climatology 只剩 +0.088——模型已經幾乎退化成長期平均值。"
            "在 persistence 自己都輸給長期平均的地方贏過 persistence，不算成就。",
        ]
    return "\n".join(lines), "\n".join(note)


with gr.Blocks(title="air-quality — PM2.5 預測 demo") as demo:
    gr.Markdown(
        """
# PM2.5 預測：模型 vs 兩條基準線

挑一個測站、一個時刻、一個預測期距，看**四個數字**：模型的預測、
「跟現在一樣」的預測、「這站這個月這個小時的平均」的預測，以及**實際發生了什麼**。

這樣呈現是刻意的。單獨一個預測值沒辦法讓人判斷模型好不好——
要判斷，得知道它贏過了什麼。

模型訓練資料是 2015–2024，這裡的每一列都來自 2025，**模型沒有看過**。
"""
    )

    # Populated at construction, not by `demo.load`. Relying on the load event
    # left the dropdown empty on arrival, so the first click always hit the
    # "pick a time" guard and the app looked broken until you changed station.
    _initial = timestamp_choices(STATIONS[0])

    with gr.Row():
        station = gr.Dropdown(STATIONS, value=STATIONS[0], label="測站")
        timestamp = gr.Dropdown(
            _initial, value=_initial[0], label="時刻（每 24 筆有完整紀錄的取 1 筆）"
        )
        horizon = gr.Radio(HORIZONS, value=24, label="預測幾小時後")

    run = gr.Button("預測", variant="primary")
    result = gr.Markdown()
    context = gr.Markdown()

    station.change(timestamps_for, inputs=station, outputs=timestamp)
    run.click(forecast, inputs=[station, timestamp, horizon], outputs=[result, context])

    gr.Markdown(
        """
---

### 這不是空品預報

它只用測站自己的歷史觀測與同時刻的其他測項——沒有數值天氣預報、沒有衛星、
沒有境外傳輸的前導資訊，而那些正是官方預報用的東西。
這裡量的是「只用公開的歷史觀測，靠自己能走多遠」，答案大約是一天。

官方預報請看 [環境部空氣品質監測網](https://airtw.moenv.gov.tw/)。

### 資料

環境部空氣品質監測網公開的年度逐時資料。這個 Space 內含的是
六個測站一年份的**樣本**，不是完整逐時紀錄的複本。

六個測站涵蓋不同監測情境，也呈現兩種已觀測到的 CBPF 高值機率峰值風速組別：
富貴角、馬公的峰值出現在高風速組別；忠明、前金、潮州、埔里的峰值出現在
低風速組別。這只描述 CBPF 高值機率峰值出現的觀測風速組別，不是污染來源
身分或傳輸距離的分類。忠明與前金是都會測站、潮州是鄉村測站、埔里在內陸
盆地（空氣容易滯留）。
"""
    )

if __name__ == "__main__":
    demo.launch()
