"""The prediction interval as text, separated from everything that loads a model.

`app.py` reads its manifest, demo slice and boosters at import time, so nothing
in it can be exercised without a built bundle — and a built bundle is untracked,
so CI has never had one. Nothing in this repository checks `spaces/forecast/` at
all, and on 2026-08-23 that cost two defects that reached a rebuilt bundle before
anyone saw them:

* the 48-hour band came out **narrower** than the 24-hour one. Real, not a slip —
  at 48 hours the calibration residuals have a tighter core and a heavier tail,
  so the 80th percentile crosses over while p90 and p95 do not. Two numbers in a
  table read as 「more certain further out」, which is the opposite of what the
  tail says.
* the 48-hour lower bound rendered as **−1.2 μg/m³**. A conformal band is
  symmetric by construction, so near the floor it goes through zero. Chapter 9's
  own example queries include 「PM2.5 大於 PM10 的比率（物理上不可能）」, and this
  would have shown a demo visitor a negative concentration.

Neither is visible in the manifest or in the source: `model_value - half` is an
ordinary line, and the second only appeared when `forecast()` was called on a
real row. So the part that decides what the reader is told lives here, takes
numbers, returns strings, and is tested without a bundle.
"""

from __future__ import annotations

__all__ = ["band_notes", "interval_cell"]


def interval_cell(model_value: float, half_width: float) -> tuple[str, float]:
    """The bracketed interval as shown, and the untruncated lower end.

    Returns both because the caller has to report the second. Clamping is done
    openly rather than quietly: truncating an interval narrows it, and a narrower
    interval covers less than the level printed on it, so a page that clamps in
    silence is advertising a coverage it no longer has.
    """
    raw_low = model_value - half_width
    shown_low = max(0.0, raw_low)
    return f"{shown_low:.1f}–{model_value + half_width:.1f}", raw_low


def band_notes(
    *,
    horizon: int,
    half_width: float,
    nominal: float,
    calibration_rows: int,
    raw_low: float,
    percentiles: dict[str, float] | None = None,
    longer_horizons: dict[int, tuple[float, dict[str, float]]] | None = None,
) -> list[str]:
    """Everything that must travel with the interval, in reading order.

    ``longer_horizons`` maps a horizon further out than this one to its own
    half-width and residual percentiles. The crossover note is emitted only when
    one of them is genuinely narrower, so it disappears by itself if the ordering
    ever becomes monotonic — which is the difference between reporting a property
    of the data and hard-coding a caveat about it.
    """
    notes: list[str] = [
        f"括號是名目 {nominal:.0%} 的 split conformal 區間，半寬 "
        f"±{half_width:.1f} μg/m³，由訓練期尾段 {calibration_rows:,} 列校準。"
    ]

    if raw_low < 0:
        notes.append(
            f"下界原本是 {raw_low:.1f}，已截在 0。共形區間是對稱的，靠近下限時"
            "必然穿過零；截斷會讓區間變窄，所以它實際涵蓋的比標示的 "
            f"{nominal:.0%} 少一些。"
        )

    narrower = sorted(
        (far, width, pct)
        for far, (width, pct) in (longer_horizons or {}).items()
        if width < half_width
    )
    if narrower:
        far, far_width, far_pct = narrower[0]
        here_p90 = (percentiles or {}).get("90")
        there_p90 = far_pct.get("90")
        detail = (
            f"（{horizon}h 的 p90 是 {here_p90:.1f}，{far}h 是 {there_p90:.1f}）"
            if here_p90 is not None and there_p90 is not None
            else ""
        )
        notes.append(
            f"**注意**：{far} 小時的區間是 ±{far_width:.1f}，比這裡的 ±{half_width:.1f} "
            "**窄**。這不是模型在更遠處更確定——是那個期距的誤差分布核心更緊、尾巴更重，"
            f"交叉發生在第 80 與第 90 百分位之間{detail}。"
            "要比較不確定度的大小，看尾巴而不是看這條區間。"
        )

    notes.append(
        "**這個水準是要求的，不是保證的。** 共形的保證是邊際的，並且假設校準與"
        "測試可交換；逐時 PM2.5 不是。回測在每一個期距都量到四個分割中有一個"
        "低於名目，而且每次都是同一個分割。這裡沒有留出期可以再量一次，所以"
        "這條區間該讀成一個尺度，不是一個承諾。"
    )
    return notes
