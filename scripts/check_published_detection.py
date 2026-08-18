"""D8's detection-limit table is retyped in prose. Do the cells still agree?

`docs/methodology.md` prints one row per marked event: the station count, the
median estimate, the reference distribution's mean and standard deviation, how
many stations passed the threshold, and how many would pass by chance. Every one
is a named field in `web/public/data/story/detection-limit.json` — the committed
export, and the only copy CI can see.

This table carries the most careful sentence in the document — 「所以誠實的說法是
『測不到』，不是『等於零』」 — and that reading rests on the passed count being
**below** the chance expectation. A row drifting the wrong way would leave the
sentence standing on numbers that no longer support it, so the gate also checks
that relation directly rather than only cell by cell.

The four properties from `docs/working-rules.md` are all present: integers
compare exactly, a pattern that stops matching is itself a problem, patterns
anchor to the row rather than to the shape of a number, and the separators are
non-greedy over `re.S` because the prose is hard-wrapped.

    uv run python scripts/check_published_detection.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
PAYLOAD = REPO_ROOT / "web" / "public" / "data" / "story" / "detection-limit.json"
METHODOLOGY = REPO_ROOT / "docs" / "methodology.md"

MINUS = {"−": "-", "–": "-"}

# The table labels events in its own words; the payload uses the config's names.
# Matched on a distinctive substring so neither side has to copy the other.
EVENT_KEYS = {
    "COVID": "COVID",
    "台中電廠": "台中電廠",
    "空污法": "空氣污染防制法",
}


def num(text: str) -> float:
    for odd, plain in MINUS.items():
        text = text.replace(odd, plain)
    return float(text.strip().strip("*").replace("+", "").replace(",", ""))


def truth() -> list[dict[str, Any]]:
    if not PAYLOAD.exists():
        raise SystemExit(f"no payload at {PAYLOAD} — run `twair export web` first")
    payload: dict[str, Any] = json.loads(PAYLOAD.read_text(encoding="utf-8"))
    events: list[dict[str, Any]] = payload.get("events", [])
    if not events:
        raise SystemExit(f"{PAYLOAD.name} carries no events — re-export after `twair analyze m5`")
    return events


def agrees(quoted: float, actual: float, places: int | None) -> bool:
    """``places=None`` means an exact integer, which never gets a tolerance."""
    if places is None:
        return quoted == actual
    return abs(quoted - actual) <= 10.0**-places


def compare(what: str, quoted: float, actual: float, places: int | None) -> str | None:
    if agrees(quoted, actual, places):
        return None
    return f"methodology.md  {what:<44} says {quoted:g}, payload has {actual:g}"


# | COVID-19 全國三級警戒（窗口差額） | 73 | −0.494 μg/m³ | −0.690 μg/m³ | 2.503 μg/m³ | 1 | 3.3 |
_ROW = re.compile(
    r"^\|\s*([^|]+?)\s*\|\s*(\d+)\s*\|\s*([+\-−][\d.]+)[^|]*\|\s*([+\-−][\d.]+)[^|]*\|"
    r"\s*([\d.]+)[^|]*\|\s*(\d+)\s*\|\s*([\d.]+)\s*\|",
    re.M,
)


def event_for(label: str, events: list[dict[str, Any]]) -> dict[str, Any] | None:
    for prose_key, payload_key in EVENT_KEYS.items():
        if prose_key in label:
            for event in events:
                if payload_key in str(event["event"]):
                    return event
    return None


def check_table(text: str, events: list[dict[str, Any]]) -> list[str]:
    problems: list[str] = []
    seen: list[str] = []
    for match in _ROW.finditer(text):
        label = match.group(1)
        event = event_for(label, events)
        if event is None:
            continue  # a table row from another section
        seen.append(str(event["event"]))
        short = str(event["event"])[:10]
        for what, quoted, actual, places in (
            ("stations", num(match.group(2)), float(event["n_stations"]), None),
            ("median estimate", num(match.group(3)), float(event["median_effect"]), 3),
            ("reference mean", num(match.group(4)), float(event["median_placebo_mean"]), 3),
            ("reference sd", num(match.group(5)), float(event["median_placebo_sd"]), 3),
            ("stations passing", num(match.group(6)), float(event["n_credible"]), None),
            ("expected by chance", num(match.group(7)), float(event["n_expected_by_chance"]), 1),
        ):
            problem = compare(f"{short} {what}", quoted, actual, places)
            if problem:
                problems.append(problem)

    missing = [str(e["event"]) for e in events if str(e["event"]) not in seen]
    if missing:
        problems.append(f"methodology.md  no detection row for {missing}")
    return problems


def check_the_reading_still_holds(events: list[dict[str, Any]]) -> list[str]:
    """「測不到」 rests on every event passing fewer stations than chance predicts.

    Checked against the payload rather than the prose: if a re-run ever made an
    event exceed its chance expectation, the table could be updated cell by cell
    and the sentence beneath it would quietly become wrong.
    """
    problems = []
    for event in events:
        passed = float(event["n_credible"])
        expected = float(event["n_expected_by_chance"])
        if passed >= expected:
            problems.append(
                f"detection-limit.json  {str(event['event'])[:16]} now passes {passed:g} "
                f"stations against {expected:g} expected by chance — "
                "「低於機率預期」 in methodology.md no longer describes this"
            )
    return problems


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    events = truth()
    text = METHODOLOGY.read_text(encoding="utf-8")

    problems = [*check_table(text, events), *check_the_reading_still_holds(events)]

    print(f"events              : {len(events)}")
    for event in events:
        print(
            f"  {str(event['event'])[:24]:<26} "
            f"{event['n_credible']} passed of {event['n_expected_by_chance']} expected"
        )
    print(f"disagreements       : {len(problems)}")
    for problem in problems:
        print(f"  {problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
