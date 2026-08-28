"""Do M6's numbers in public prose still agree with the payload?

`data/outputs/m6_spatial/` is the measurement, `reports/03-spatial.md` regenerates
from it, and `web/public/data/story/spatial-structure.json` is the committed
export — the only copy CI can see, because `data/` is gitignored. The public
methodology retypes the same figures for readers who are not looking at the chart:

* ``docs/methodology.md`` — the D7 section's control table, correlogram endpoints,
  LISA counts and partition statistics

Nothing connected the hand-typed methodology to the payload. On 2026-08-18 M6's
network went from 60 stations to 61; the report and website regenerated with the
run while prose copies kept saying 0.157, 60 站, 8 站, +0.035 and +0.625 for a
day. This gate is that failure's lesson made mechanical.

Comparison is at the precision each file prints, with one unit of the last place
as tolerance, because the payload is itself rounded on export: a file printing
three decimals of 0.1555 may legitimately write 0.155 or 0.156, and a gate that
fails on that would be switched off within a week. A genuine drift — 0.157
against 0.1555 — is 1.5 units away and still caught.

    uv run python scripts/check_published_spatial.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
PAYLOAD = REPO_ROOT / "web" / "public" / "data" / "story" / "spatial-structure.json"
METHODOLOGY = REPO_ROOT / "docs" / "methodology.md"

# U+2212 MINUS SIGN reads better in prose and does not parse as a hyphen.
MINUS = {"−": "-", "–": "-"}

CONTROL_LABELS = {
    "合併式（未分層）": "pooled",
    "七區截距": "zone_era_dummies",
    "字面上的「分區各跑一次」": "within_zone_separate_fits",
    "測站固定效果": "station_dummies",
}


def num(text: str) -> float:
    for odd, plain in MINUS.items():
        text = text.replace(odd, plain)
    return float(text.strip().strip("*").replace("+", ""))


def truth() -> dict[str, Any]:
    if not PAYLOAD.exists():
        raise SystemExit(f"no payload at {PAYLOAD} — run `twair export web` first")
    payload: dict[str, Any] = json.loads(PAYLOAD.read_text(encoding="utf-8"))
    missing = sorted({"controls", "correlogram", "lisa", "partition_test"} - set(payload))
    if missing:
        raise SystemExit(
            f"{PAYLOAD.name} is missing {missing} — re-export after `twair analyze m6`"
        )
    return payload


def agrees(quoted: float, actual: float, places: int | None) -> bool:
    """Equal once both are read at the precision the prose prints.

    ``places=None`` means an exact integer — a station count, a month count, a
    choice of k. **These must never be compared with a tolerance.** The first
    version of this file passed ``places=0`` for them, which set the tolerance to
    1.0 and made every off-by-one agree: 60 stations against 61, 54 significant
    months against 55. Off-by-one is precisely the drift this gate exists to
    catch, and it was catching none of it until the gate was tested against the
    stale prose it was written for.
    """
    if places is None:
        return quoted == actual
    return abs(quoted - actual) <= 10.0**-places


def compare(where: str, what: str, quoted: float, actual: float, places: int | None) -> str | None:
    if agrees(quoted, actual, places):
        return None
    return f"{where}  {what:<34} says {quoted:g}, payload has {actual:g}"


# | 合併式（未分層） | 13 | +0.156 | 54/96 |
_CONTROL_ROW = re.compile(
    r"^\|\s*([^|]+?)\s*\|\s*(\d+)\s*\|\s*\*{0,2}([+\-−][\d.]+)\*{0,2}\s*\|"
    r"\s*\*{0,2}(\d+)/(\d+)\*{0,2}\s*\|",
    re.M,
)


def check_control_table(text: str, payload: dict[str, Any]) -> list[str]:
    by_name = {row["control"]: row for row in payload["controls"]}
    problems: list[str] = []
    seen: set[str] = set()
    for match in _CONTROL_ROW.finditer(text):
        label = match.group(1).strip()
        key = CONTROL_LABELS.get(label)
        if key is None:
            continue  # a table row from another section
        seen.add(key)
        row = by_name.get(key)
        if row is None:
            problems.append(f"methodology.md  control 「{label}」 is not in the payload")
            continue
        for what, quoted, actual, places in (
            ("params", float(match.group(2)), float(row["params"]), None),
            ("mean I", num(match.group(3)), float(row["mean_i"]), 3),
            (
                "BH significant months",
                float(match.group(4)),
                float(row["months_significant_bh"]),
                None,
            ),
            ("months scored", float(match.group(5)), float(row["months_scored"]), None),
        ):
            problem = compare("methodology.md", f"{label} {what}", quoted, actual, places)
            if problem:
                problems.append(problem)
    if not seen:
        problems.append("methodology.md  no control rows matched — has the table changed shape?")
    return problems


# **LISA**：61 站中 raw 顯著 7 站、**BH 後 0 站**
_LISA = re.compile(
    r"LISA\*{0,2}[：:]\s*(\d+)\s*站中\s*raw\s*顯著\s*(\d+)\s*站.{0,8}BH\s*後\s*(\d+)\s*站"
)


def check_lisa(text: str, where: str, payload: dict[str, Any]) -> list[str]:
    lisa = payload["lisa"]
    match = _LISA.search(text)
    if match is None:
        # Not silence. A sentence that stops matching is either a rewrite that
        # needs this pattern updated, or a claim that was deleted — and both
        # should be seen. A check that opts itself out when the prose moves is
        # the same failure as a probe nobody has watched fail.
        return [f"{where}  no LISA sentence matched — was it rewritten or removed?"]
    problems = []
    for what, quoted, actual in (
        ("LISA stations", float(match.group(1)), float(lisa["stations"])),
        ("LISA raw significant", float(match.group(2)), float(lisa["significant_raw"])),
        ("LISA BH significant", float(match.group(3)), float(lisa["significant_bh"])),
    ):
        problem = compare(where, what, quoted, actual, None)
        if problem:
            problems.append(problem)
    return problems


# 但 silhouette 僅 +0.026，資料偏好 **k=2**（北群/南群，silhouette +0.620）
_SILHOUETTE = re.compile(
    r"silhouette\s*僅\s*([+\-−][\d.]+).{0,40}?k=(\d+).{0,40}?silhouette\s*([+\-−][\d.]+)",
    re.S,
)


def check_partition(text: str, payload: dict[str, Any]) -> list[str]:
    partition = payload["partition_test"]
    match = _SILHOUETTE.search(text)
    if match is None:
        return ["methodology.md  no silhouette/best-k sentence matched — rewritten or removed?"]
    problems = []
    for what, quoted, actual, places in (
        ("official silhouette", num(match.group(1)), float(partition["silhouette"]), 3),
        ("preferred k", float(match.group(2)), float(partition["best_k"]), None),
        (
            "best-k silhouette",
            num(match.group(3)),
            float(partition["best_k_silhouette"]),
            3,
        ),
    ):
        problem = compare("methodology.md", what, quoted, actual, places)
        if problem:
            problems.append(problem)
    return problems


def check_correlogram(text: str, payload: dict[str, Any]) -> list[str]:
    """The two endpoints methodology.md names, matched by their own band labels."""
    bands = {(float(b["lo_km"]), float(b["hi_km"])): b for b in payload["correlogram"]}
    problems: list[str] = []
    pattern = re.compile(
        r"(\d+)[–\-](\d+)\s*km[^。]{0,30}?I\s*=\s*([+\-−][\d.]+)（z=([+\-−][\d.]+)）"
        r"|(\d+)[–\-](\d+)\s*km\s*\n?反號至\s*\*{0,2}([+\-−][\d.]+)（z=([+\-−][\d.]+)\）",
    )
    for match in pattern.finditer(text):
        groups = [g for g in match.groups() if g is not None]
        if len(groups) != 4:
            continue
        lo, hi, i_text, z_text = groups
        band = bands.get((float(lo), float(hi)))
        if band is None:
            problems.append(f"methodology.md  no payload band for {lo}–{hi} km")
            continue
        for what, quoted, actual, places in (
            (f"{lo}–{hi} km I", num(i_text), float(band["i"]), 3),
            (f"{lo}–{hi} km z", num(z_text), float(band["z"]), 2),
        ):
            problem = compare("methodology.md", what, quoted, actual, places)
            if problem:
                problems.append(problem)
    return problems


_SURVIVING = re.compile(r"(\d+)\s*個距離帶中\s*(\d+)\s*個通過")


def check_surviving_bands(text: str, payload: dict[str, Any]) -> list[str]:
    match = _SURVIVING.search(text)
    if match is None:
        return [
            "methodology.md  no 「n 個距離帶中 m 個通過」 claim matched — rewritten or removed?"
        ]
    total = len(payload["correlogram"])
    surviving = sum(1 for band in payload["correlogram"] if band["significant"])
    problems = []
    for what, quoted, actual in (
        ("correlogram bands", float(match.group(1)), float(total)),
        ("bands surviving BH", float(match.group(2)), float(surviving)),
    ):
        problem = compare("methodology.md", what, quoted, actual, None)
        if problem:
            problems.append(problem)
    return problems


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    payload = truth()
    methodology = METHODOLOGY.read_text(encoding="utf-8")

    problems = [
        *check_control_table(methodology, payload),
        *check_correlogram(methodology, payload),
        *check_surviving_bands(methodology, payload),
        *check_lisa(methodology, "methodology.md", payload),
        *check_partition(methodology, payload),
    ]

    print(
        f"network             : {payload['network']['placed']} placed of "
        f"{payload['network']['stations']}, {payload['lisa']['stations']} scored"
    )
    print(f"controls in payload : {[row['control'] for row in payload['controls']]}")
    print(f"disagreements       : {len(problems)}")
    for problem in problems:
        print(f"  {problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
