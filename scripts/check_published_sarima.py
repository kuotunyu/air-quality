"""D10's SARIMA numbers are retyped in prose. Do they still agree?

`docs/methodology.md`'s D10 section states the whole M12 result in three places:
the panel it was fitted on, the timing table that prices the inconvenience, and
the RMSE table that says what the inconvenience bought. Every figure is already a
named field in `web/public/data/story/sarima.json`, the committed export and the
only copy CI can see. Only the copying stood between them.

Built after the same shape had already been found twice — M9's backtest table had
rotted in two of four copies, and M6's had gone stale in `methodology.md` and
`PLAN.md` within a day of a re-run.

**The timing figures are machine-dependent** and must never be read as a property
of the method. Checking them here is still right: this compares prose against the
payload of the same run, not against an absolute truth. If M12 is re-run on
different hardware both sides move together.

The three properties every gate here needs, present from the first version
because the earlier ones shipped without them:

* integer counts compare **exactly** — a tolerance of one unit makes every
  off-by-one agree, and 18/18 fits against 17/18 is exactly what this catches;
* a pattern that stops matching is a **reported problem**, never silence;
* patterns anchor to the sentence, not to the shape of the number.

    uv run python scripts/check_published_sarima.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
PAYLOAD = REPO_ROOT / "web" / "public" / "data" / "story" / "sarima.json"
METHODOLOGY = REPO_ROOT / "docs" / "methodology.md"

MINUS = {"−": "-", "–": "-"}


def num(text: str) -> float:
    for odd, plain in MINUS.items():
        text = text.replace(odd, plain)
    return float(text.strip().strip("*").replace("+", "").replace(",", ""))


def truth() -> dict[str, Any]:
    if not PAYLOAD.exists():
        raise SystemExit(f"no payload at {PAYLOAD} — run `twair export web` first")
    payload: dict[str, Any] = json.loads(PAYLOAD.read_text(encoding="utf-8"))
    missing = sorted({"fits", "selection_cost", "horizons"} - set(payload))
    if missing:
        raise SystemExit(
            f"{PAYLOAD.name} is missing {missing} — re-export after `twair analyze m12`"
        )
    return payload


def agrees(quoted: float, actual: float, places: int | None) -> bool:
    """``places=None`` means an exact integer, which never gets a tolerance."""
    if places is None:
        return quoted == actual
    return abs(quoted - actual) <= 10.0**-places


def compare(what: str, quoted: float, actual: float, places: int | None) -> str | None:
    if agrees(quoted, actual, places):
        return None
    return f"methodology.md  {what:<32} says {quoted:g}, payload has {actual:g}"


# 6 個測站 × 3 個 rolling-origin 分割、2015–2025、固定階數
_PANEL = re.compile(r"(\d+)\s*個測站\s*×\s*(\d+)\s*個\s*rolling-origin\s*分割")
# **18/18 次擬合全部收斂**，中位數 11 秒、8,612 個有效觀測點
#
# `re.S` because the sentence wraps: prose in this repository is hard-wrapped, so
# any pattern spanning a clause has to survive a newline landing anywhere in it.
# The first version used `[^\n]` and reported the sentence as missing.
# The separators are non-greedy. Greedy `.{0,3}` swallowed 「、8,」 and captured
# 612 out of 8,612 — a pattern that matches and reads the wrong number is worse
# than one that does not match, because the disagreement it reports looks real.
_FITS = re.compile(
    r"\*{0,2}(\d+)/(\d+)\s*次擬合全部收斂\*{0,2}.{0,4}?中位數\s*([\d.]+)\s*秒.{0,3}?"
    r"([\d,]+)\s*個有效觀測點",
    re.S,
)
# 每 72 小時一個預測原點
_STRIDE = re.compile(r"每\s*(\d+)\s*小時一個預測原點")
# | 1,000 點 | 11.14 s | 0.58 s | **19.1×** |
_COST_ROW = re.compile(
    r"^\|\s*([\d,]+)\s*點\s*\|\s*([\d.]+)\s*s\s*\|\s*([\d.]+)\s*s\s*\|"
    r"\s*\*{0,2}([\d.]+)×\*{0,2}\s*\|",
    re.M,
)
# | 24h | **8.57** | 9.10 | 9.39 | 贏 5.9% |
_RMSE_ROW = re.compile(
    r"^\|\s*(\d+)h\s*\|\s*\*{0,2}([\d.]+)\*{0,2}\s*\|\s*\*{0,2}([\d.]+)\*{0,2}\s*\|"
    r"\s*\*{0,2}([\d.]+|—)\*{0,2}\s*\|",
    re.M,
)


def check_panel(text: str, payload: dict[str, Any]) -> list[str]:
    match = _PANEL.search(text)
    if match is None:
        return ["methodology.md  no 「n 個測站 × m 個 rolling-origin 分割」 — reworded or removed?"]
    problems = []
    for what, quoted, actual in (
        ("stations", num(match.group(1)), float(payload["stations"])),
        ("splits", num(match.group(2)), float(payload["splits"])),
    ):
        problem = compare(what, quoted, actual, None)
        if problem:
            problems.append(problem)

    stride = _STRIDE.search(text)
    if stride is None:
        problems.append("methodology.md  no 「每 n 小時一個預測原點」 — reworded or removed?")
    else:
        problem = compare(
            "origin stride hours",
            num(stride.group(1)),
            float(payload["origin_stride_hours"]),
            None,
        )
        if problem:
            problems.append(problem)
    return problems


def check_fits(text: str, payload: dict[str, Any]) -> list[str]:
    match = _FITS.search(text)
    if match is None:
        return ["methodology.md  no 「n/m 次擬合全部收斂」 sentence — reworded or removed?"]
    fits = payload["fits"]
    problems = []
    for what, quoted, actual, places in (
        ("fits converged", num(match.group(1)), float(fits["converged"]), None),
        ("fits total", num(match.group(2)), float(fits["total"]), None),
        ("median seconds", num(match.group(3)), float(fits["median_seconds"]), 1),
        ("median observed points", num(match.group(4)), float(fits["median_observed"]), None),
    ):
        problem = compare(what, quoted, actual, places)
        if problem:
            problems.append(problem)
    return problems


def check_selection_cost(text: str, payload: dict[str, Any]) -> list[str]:
    by_points = {int(row["points"]): row for row in payload["selection_cost"]}
    problems: list[str] = []
    seen: set[int] = set()
    for match in _COST_ROW.finditer(text):
        points = int(num(match.group(1)))
        seen.add(points)
        row = by_points.get(points)
        if row is None:
            problems.append(f"methodology.md  {points} 點 is not a row the payload reports")
            continue
        for what, quoted, actual, places in (
            (f"{points}pt auto seconds", num(match.group(2)), float(row["auto_seconds"]), 2),
            (f"{points}pt fixed seconds", num(match.group(3)), float(row["fixed_seconds"]), 2),
            (f"{points}pt multiple", num(match.group(4)), float(row["multiple"]), 1),
        ):
            problem = compare(what, quoted, actual, places)
            if problem:
                problems.append(problem)
    missing = sorted(set(by_points) - seen)
    if missing:
        problems.append(f"methodology.md  no selection-cost row for {missing}")
    return problems


def check_rmse(text: str, payload: dict[str, Any]) -> list[str]:
    by_horizon = {int(row["horizon"]): row for row in payload["horizons"]}
    problems: list[str] = []
    seen: set[int] = set()
    for match in _RMSE_ROW.finditer(text):
        horizon = int(num(match.group(1)))
        if horizon not in by_horizon:
            continue  # a table row from another section
        seen.add(horizon)
        row = by_horizon[horizon]
        checks = [
            (f"{horizon}h SARIMA rmse", num(match.group(2)), float(row["sarima_rmse"])),
            (f"{horizon}h persistence rmse", num(match.group(3)), float(row["persistence_rmse"])),
        ]
        # climatology is 「—」 at 6h, where the payload has no value to compare.
        if match.group(4) != "—":
            checks.append(
                (
                    f"{horizon}h climatology rmse",
                    num(match.group(4)),
                    float(row["climatology_rmse"]),
                )
            )
        for what, quoted, actual in checks:
            problem = compare(what, quoted, actual, 2)
            if problem:
                problems.append(problem)
    missing = sorted(set(by_horizon) - seen)
    if missing:
        problems.append(f"methodology.md  no RMSE row for horizon(s) {missing}")
    return problems


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    payload = truth()
    text = METHODOLOGY.read_text(encoding="utf-8")

    problems = [
        *check_panel(text, payload),
        *check_fits(text, payload),
        *check_selection_cost(text, payload),
        *check_rmse(text, payload),
    ]

    fits = payload["fits"]
    print(f"panel               : {payload['stations']} stations × {payload['splits']} splits")
    print(f"fits                : {fits['converged']}/{fits['total']} converged")
    print(f"horizons            : {[row['horizon'] for row in payload['horizons']]}")
    print(f"disagreements       : {len(problems)}")
    for problem in problems:
        print(f"  {problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
