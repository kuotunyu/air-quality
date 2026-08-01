"""The M9 backtest table is written out in four places. Do they still agree?

`data/outputs/m9_forecast/` is the measurement; `web/public/data/story/forecast.json`
is the committed export of it, and the only copy CI can see. Three other files
retype the same numbers for readers who are not looking at the chart:

* ``src/twair/models/forecast.py`` — the module docstring, an rst table
* ``spaces/forecast/README.md`` — the Space's landing page, a markdown table
* ``spaces/forecast/app.py`` — the Space's own ``SKILL`` dict, which it prints
  beside the live demo to show the two disagreeing

Nothing connected them. Re-running the backtest updated the payload and the
chart drawn from it, and left the three prose copies saying whatever they said
before — which is how the same three-item sentinel list ended up wrong in two
of its three copies elsewhere in this repository.

The station count is checked too. It was 「74 站」 as a literal in four files
while every number beside it came from the payload.

    uv run python scripts/check_published_forecast.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
PAYLOAD = REPO_ROOT / "web" / "public" / "data" / "story" / "forecast.json"

FORECAST_PY = REPO_ROOT / "src" / "twair" / "models" / "forecast.py"
SPACE_README = REPO_ROOT / "spaces" / "forecast" / "README.md"
SPACE_APP = REPO_ROOT / "spaces" / "forecast" / "app.py"

# U+2212 MINUS SIGN reads better than a hyphen in a table and does not parse as
# one. Typography is not a discrepancy.
MINUS = {"−": "-", "–": "-"}


def num(text: str) -> float:
    for odd, plain in MINUS.items():
        text = text.replace(odd, plain)
    return float(text.strip().strip("*").replace("+", ""))


# Each row is (horizon, R², skill vs persistence, skill vs climatology, worst split).
_RST_ROW = re.compile(
    r"^\s{2,}(\d+)\s+([\d.]+)\s+\*{0,2}([+\-−][\d.]+)\*{0,2}"
    r"\s+\*{0,2}([+\-−][\d.]+)\*{0,2}\s+\*{0,2}([+\-−][\d.]+)\*{0,2}\s*$",
    re.M,
)

_MD_ROW = re.compile(
    r"^\|\s*(\d+)h\s*\|\s*\*{0,2}([\d.]+)\*{0,2}\s*\|\s*\*{0,2}([+\-−][\d.]+)\*{0,2}"
    r"\s*\|\s*\*{0,2}([+\-−][\d.]+)\*{0,2}\s*\|\s*\*{0,2}([+\-−][\d.]+)\*{0,2}\s*\|",
    re.M,
)

# methodology.md prints the same table without the worst-split column, and
# deliberately omits the 6h row — so it is checked for agreement, not for
# completeness.
_MD_ROW_3 = re.compile(
    r"^\|\s*(\d+)h\s*\|\s*\*{0,2}([\d.]+)\*{0,2}\s*\|\s*\*{0,2}([+\-−][\d.]+)\*{0,2}"
    r"\s*\|\s*\*{0,2}([+\-−][\d.]+)\*{0,2}\s*\|\s*$",
    re.M,
)

_APP_ROW = re.compile(
    r'^\s*(\d+):\s*\{"r2":\s*([\d.]+),\s*"persistence":\s*([\d.\-]+),'
    r'\s*"climatology":\s*([\d.\-]+),\s*"rmse":\s*([\d.]+)\}',
    re.M,
)

FIELDS = ("model_r2", "skill_persistence", "skill_climatology", "skill_persistence_worst")


def truth() -> dict[int, dict[str, Any]]:
    if not PAYLOAD.exists():
        raise SystemExit(f"no payload at {PAYLOAD} — run `twair export web` first")
    data = json.loads(PAYLOAD.read_text(encoding="utf-8"))
    return {int(h["horizon"]): h for h in data["horizons"]}


def compare(
    where: str, horizon: int, field: str, quoted: float, actual: float, places: int
) -> str | None:
    """Compare at the precision the file prints, not at full float precision."""
    if round(actual, places) == round(quoted, places):
        return None
    return f"{where}  h={horizon:>2}  {field:<26} says {quoted:+.3f}, payload has {actual:+.3f}"


def check_table(
    where: str,
    pattern: re.Pattern[str],
    text: str,
    expected: dict[int, dict[str, Any]],
    *,
    fields: tuple[str, ...] = FIELDS,
    require_all: bool = True,
) -> list[str]:
    problems: list[str] = []
    seen: set[int] = set()
    for match in pattern.finditer(text):
        horizon = int(match.group(1))
        seen.add(horizon)
        if horizon not in expected:
            problems.append(f"{where}  h={horizon} is not a horizon the payload reports")
            continue
        for i, field in enumerate(fields, start=2):
            problem = compare(
                where, horizon, field, num(match.group(i)), expected[horizon][field], 3
            )
            if problem:
                problems.append(problem)
    missing = sorted(set(expected) - seen)
    if require_all and missing:
        problems.append(f"{where}  no row for horizon(s) {missing}")
    if not seen:
        problems.append(f"{where}  no rows matched at all — has the table changed shape?")
    return problems


def check_app(text: str, expected: dict[int, dict[str, Any]]) -> list[str]:
    """The Space's dict, which also carries RMSE and prints it in the app."""
    problems: list[str] = []
    seen: set[int] = set()
    for match in _APP_ROW.finditer(text):
        horizon = int(match.group(1))
        seen.add(horizon)
        if horizon not in expected:
            problems.append(f"app.py  h={horizon} is not a horizon the payload reports")
            continue
        row = expected[horizon]
        for field, group, places in (
            ("model_r2", 2, 3),
            ("skill_persistence", 3, 3),
            ("skill_climatology", 4, 3),
            ("model_rmse", 5, 2),
        ):
            problem = compare(
                "app.py ", horizon, field, num(match.group(group)), row[field], places
            )
            if problem:
                problems.append(problem)
    missing = sorted(set(expected) - seen)
    if missing:
        problems.append(f"app.py  no SKILL entry for horizon(s) {missing}")
    return problems


_STATION_CLAIMS = re.compile(r"(\d+)\s*(?:個測站|站|stations)")

# Scoped to lines that also name the backtest period, because the same files
# quote a *different* station count for a different population: the Space's
# demo bundle is six stations over one year, and its whole point is to disagree
# with the backtest. A bare 「6 站」 is not a stale 74.
_BACKTEST_LINE = "2015"


def check_stations(expected: dict[int, dict[str, Any]]) -> list[str]:
    """`74 站` was a literal in four files beside numbers that were all derived."""
    actual = max(int(h.get("stations", 0)) for h in expected.values())
    if actual == 0:
        return ["the payload carries no station count — re-export after `twair analyze m9`"]

    # Only the files that are *entirely* about M9. `docs/methodology.md` covers
    # eleven modules and says 「6 個測站 × 3 個 rolling-origin 分割、2015–2025」
    # about M12's SARIMA experiment on the same period — a different population,
    # correctly stated. A regex cannot tell which module a line belongs to, and a
    # gate that cries wolf on a true sentence gets switched off.
    problems: list[str] = []
    for path in (FORECAST_PY, SPACE_README, SPACE_APP):
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if _BACKTEST_LINE not in line:
                continue
            for match in _STATION_CLAIMS.finditer(line):
                if int(match.group(1)) != actual:
                    problems.append(
                        f"{path.relative_to(REPO_ROOT).as_posix()}  says "
                        f"「{match.group(0)}」, the backtest covers {actual}"
                    )
    return problems


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    expected = truth()

    problems = [
        *check_table(
            "forecast.py docstring", _RST_ROW, FORECAST_PY.read_text(encoding="utf-8"), expected
        ),
        *check_table("Space README", _MD_ROW, SPACE_README.read_text(encoding="utf-8"), expected),
        *check_table(
            "methodology.md",
            _MD_ROW_3,
            (REPO_ROOT / "docs" / "methodology.md").read_text(encoding="utf-8"),
            expected,
            fields=("model_r2", "skill_persistence", "skill_climatology"),
            require_all=False,
        ),
        *check_app(SPACE_APP.read_text(encoding="utf-8"), expected),
        *check_stations(expected),
    ]

    print(f"horizons in payload : {sorted(expected)}")
    print(f"stations            : {max(int(h.get('stations', 0)) for h in expected.values())}")
    print(f"disagreements       : {len(problems)}")
    for problem in problems:
        print(f"  {problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
