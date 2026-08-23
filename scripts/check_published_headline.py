"""The five numbers on the first screen are retyped in six files. Do they agree?

「降了 60%」, 「43%」, 「42.2%」, 「2.55 倍」 and 「32.1%」 are the most-read figures
this project publishes — the opening sentence of both READMEs — and they also
appear in `PLAN.md`, `docs/methodology.md`, `docs/working-rules.md` and the
generated `reports/01-core.md`. Only the last of those regenerates from the data.

`tests/test_public_readmes.py` pins them as literal strings, which catches a
deletion or a reword. It cannot catch drift: when the analysis moves, the test
fails, and the fix is to update the prose *and the assertion* to whatever the new
number is — with nothing checking that the new number is the one the data
actually holds.

This compares them against the committed story payloads, which is the same source
the site draws and the only copy CI can see, `data/` being gitignored:

* `story/trend-national.json` — the 2006-anchored fall, by the definition
  `make_social_card.facts()` uses, imported rather than restated
* `story/deweather.json` — the weather share of that fall, and the independent
  per-station median that is quoted beside it
* `story/pitfalls.json` — the PM10 leak's share of R², and the ratio sin/cos
  encoding buys over a raw bearing under OLS

Design notes, both learned the hard way while writing `check_published_spatial.py`
the same night:

**Integer and exact-decimal claims never get a tolerance.** Only figures the
prose deliberately rounds do, at one unit of the last place printed.

**A pattern that stops matching is a reported problem, not silence.** Rewording
a sentence must not switch its own check off.

    uv run python scripts/check_published_headline.py
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
STORY = REPO_ROOT / "web" / "public" / "data" / "story"
CORE_REPORT = REPO_ROOT / "reports" / "01-core.md"
ROLLING_HEADING = "### 滾動原點驗證"

SOURCES = {
    "README.md": REPO_ROOT / "README.md",
    "README.en.md": REPO_ROOT / "README.en.md",
    "PLAN.md": REPO_ROOT / "PLAN.md",
    "methodology.md": REPO_ROOT / "docs" / "methodology.md",
    "working-rules.md": REPO_ROOT / "docs" / "working-rules.md",
    # Quotes the opening claim to tell a reader-test participant what the
    # site is supposed to have conveyed. That makes it another hand-typed
    # copy, so it is checked here rather than left to drift.
    "reader-test.md": REPO_ROOT / "docs" / "reader-test.md",
}


def load(name: str) -> dict[str, Any]:
    path = STORY / f"{name}.json"
    if not path.exists():
        raise SystemExit(f"no payload at {path} — run `twair export web` first")
    payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return payload


def m2_rolling_r2() -> dict[str, float]:
    """Mean R² per model over the rolling splits, from the report's own table.

    Not from `data/outputs/`, which CI does not have, and not from a payload
    either: `pitfalls.json` carries the `full` feature set but neither the tree's
    raw-bearing score nor the persistence baseline's, and both are quoted in
    `docs/working-rules.md`. `reports/01-core.md` regenerates from the same frame
    on every run, which `check_published_site_prose.py` already relies on for the
    same reason — a regenerated report is as good a source as a payload.

    Scoped to the rolling section before matching rows. The leave-one-station and
    leave-one-year tables repeat every model name with different numbers, and an
    unscoped row pattern would read whichever came first — the mistake
    `retention_truth()` made in the site-prose gate, which read 1997 out of the
    wrong table.
    """
    if not CORE_REPORT.exists():
        raise SystemExit(f"no report at {CORE_REPORT} — run `uv run twair report` first")
    text = CORE_REPORT.read_text(encoding="utf-8")

    start = text.find(ROLLING_HEADING)
    if start < 0:
        raise SystemExit(f"{CORE_REPORT.name} has no {ROLLING_HEADING!r} section")
    end = text.find("\n### ", start + len(ROLLING_HEADING))
    section = text[start : end if end > 0 else len(text)]

    # Cell by cell rather than one regex across the row. A `\S+` for the first
    # column matched the whole `|---|---|---|` divider, because that line has no
    # spaces in it, and produced a key made of dashes with the persistence row's
    # `mae` as its value — a table this gate would then have believed.
    rows: dict[str, float] = {}
    for line in section.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 7 or cells[0] in {"model", ""} or set(cells[0]) <= {"-"}:
            continue
        try:
            rows[f"{cells[0]}/{cells[1]}"] = float(cells[4])
        except ValueError:
            continue
    if not rows:
        raise SystemExit(f"{CORE_REPORT.name}: the rolling table has no readable rows")
    return rows


def card_facts() -> dict[str, Any]:
    """`make_social_card.facts()`, imported so the fall has one definition.

    Restating "(first - last) / first over the balanced window" here would create
    a second place for that choice to be made, which is the defect this file is
    about.
    """
    spec = importlib.util.spec_from_file_location(
        "_social_card", REPO_ROOT / "scripts" / "make_social_card.py"
    )
    if spec is None or spec.loader is None:  # pragma: no cover - packaging accident
        raise SystemExit("cannot import scripts/make_social_card.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    facts: dict[str, Any] = module.facts()
    return facts


def agrees(quoted: float, actual: float, places: int | None) -> bool:
    if places is None:
        return quoted == actual
    return abs(quoted - actual) <= 10.0**-places


def compare(where: str, what: str, quoted: float, actual: float, places: int | None) -> str | None:
    if agrees(quoted, actual, places):
        return None
    return f"{where:<17} {what:<32} says {quoted:g}, data has {actual:g}"


class Claim:
    """One sentence, the number it quotes, and where the truth lives."""

    def __init__(
        self,
        what: str,
        pattern: str,
        actual: float,
        places: int | None,
        *,
        scale: float = 1.0,
        files: tuple[str, ...] = (),
    ) -> None:
        self.what = what
        self.pattern = re.compile(pattern)
        self.actual = actual * scale
        self.places = places
        self.files = files

    def check(self, where: str, text: str) -> list[str]:
        matches = list(self.pattern.finditer(text))
        if not matches:
            return [f"{where:<17} {self.what:<32} no longer matches — reworded or removed?"]
        problems = []
        for match in matches:
            # The patterns carry one alternative per language, so every branch
            # but the matching one yields None.
            captured = next((g for g in match.groups() if g is not None), None)
            if captured is None:
                problems.append(f"{where:<17} {self.what:<32} matched but captured nothing")
                continue
            # U+2212 MINUS SIGN reads better in a table than a hyphen and does
            # not parse as one. Typography is not a disagreement.
            quoted = float(captured.replace("−", "-").replace("–", "-"))
            problem = compare(where, self.what, quoted, self.actual, self.places)
            if problem and problem not in problems:
                problems.append(problem)
        return problems


def build_claims() -> list[Claim]:
    trend = card_facts()
    deweather = load("deweather")
    pitfalls = load("pitfalls")

    leak = {row["feature_set"]: row for row in pitfalls["tables"]["leakage_price"]}
    honest = float(leak["full"]["r2"])
    leaking = float(leak["full_with_pm10"]["r2"])
    leak_share = (leaking - honest) * 100.0 / leaking

    wind = {row["encoding"]: row for row in pitfalls["tables"]["wind_linear_model_encoding"]}
    raw_r2 = float(wind["raw_bearing"]["r_squared"])
    encoded_r2 = float(wind["sin_cos"]["r_squared"])

    rolling = m2_rolling_r2()
    for key in ("lightgbm/full_raw_wind", "lightgbm/full", "persistence/-"):
        if key not in rolling:
            raise SystemExit(f"{CORE_REPORT.name}: the rolling table has no {key} row")

    return [
        Claim(
            "national fall since 2006",
            r"降了\s*(\d+)%|fell\s*(?:by\s*)?(\d+)%",
            float(trend["drop_pct"]),
            0,
            files=("README.md", "README.en.md", "reader-test.md"),
        ),
        Claim(
            "weather share of the fall",
            r"其中\s*(\d+)%\s*歸於|assigns\s*(\d+)%\s*of that fall",
            float(deweather["panel"]["weather_share_of_fall"]),
            0,
            scale=100.0,
            files=("README.md", "README.en.md", "reader-test.md"),
        ),
        Claim(
            "median per-station share",
            r"答案是\s*([\d.]+)%|the answer is\s*([\d.]+)%|中位數\s*\|\s*\*{0,2}([\d.]+)%",
            float(deweather["median_weather_share"]),
            1,
            scale=100.0,
            files=("README.md", "README.en.md"),
        ),
        Claim(
            "PM10 leak share of R2",
            r"([\d.]+)%\s*的\s*R²|([\d.]+)% of the leaking model|貢獻率高達\s*\*{0,2}([\d.]+)%",
            leak_share,
            1,
            files=("README.md", "README.en.md", "PLAN.md", "methodology.md"),
        ),
        # methodology.md's D8 table, the four numbers the normalisation rests on.
        # Every one is a field in the payload, so there is no arithmetic to get
        # wrong here — only the copying, which is exactly what goes stale.
        # `docs/working-rules.md` states three of M2's figures in the present
        # tense and nothing was comparing them. Verified by mutation on
        # 2026-08-24: setting the pair to 0.999 / 0.111 left this gate,
        # `check_published_site_prose` and `check_published_spatial` all green.
        #
        # The tree's scores are the report's, not the payload's — `pitfalls.json`
        # carries `full` but not `full_raw_wind`, and no payload carries the
        # persistence baseline at all. Three decimals in the prose against four
        # in the table, so the tolerance is one unit of the printed place.
        Claim(
            "tree R2 with raw bearing",
            r"raw bearing \(R²\s*([\d.]+)\)",
            rolling["lightgbm/full_raw_wind"],
            3,
            files=("working-rules.md",),
        ),
        Claim(
            "tree R2 with sin/cos",
            r"with sin/cos \(([\d.]+)\)",
            rolling["lightgbm/full"],
            3,
            files=("working-rules.md",),
        ),
        Claim(
            "persistence baseline R2",
            r"reaches R²\s*([\d.]+)",
            rolling["persistence/-"],
            3,
            files=("working-rules.md",),
        ),
        Claim(
            "best feature set against it",
            r"reaches R²\s*[\d.]+\s*against\s*([\d.]+)",
            rolling["lightgbm/full"],
            3,
            files=("working-rules.md",),
        ),
        Claim(
            "median observed slope",
            r"觀測斜率中位數\s*\|\s*\*{0,2}(−?-?[\d.]+)",
            float(deweather["median_observed_slope"]),
            2,
            files=("methodology.md",),
        ),
        Claim(
            "median normalised slope",
            r"正規化後斜率中位數\s*\|\s*\*{0,2}(−?-?[\d.]+)",
            float(deweather["median_normalised_slope"]),
            2,
            files=("methodology.md",),
        ),
        Claim(
            "weather share p10",
            r"p10\s*([\d.]+)%",
            float(deweather["weather_share_p10"]),
            1,
            scale=100.0,
            files=("methodology.md",),
        ),
        Claim(
            "weather share p90",
            r"p90\s*([\d.]+)%",
            float(deweather["weather_share_p90"]),
            1,
            scale=100.0,
            files=("methodology.md",),
        ),
        Claim(
            "median holdout R2",
            r"holdout R²\s*中位數\s*\|\s*\*{0,2}([\d.]+)|holdout R² 中位數\s*([\d.]+)",
            float(deweather["median_holdout_r2"]),
            3,
            files=("methodology.md",),
        ),
        Claim(
            "stations in the normalisation",
            r"(\d+)\s*個測站，\d+\s*個的正規化斜率顯著",
            float(deweather["n_stations"]),
            None,
            files=("methodology.md",),
        ),
        Claim(
            "stations with a significant slope",
            r"\d+\s*個測站，(\d+)\s*個的正規化斜率顯著",
            float(deweather["n_significant"]),
            None,
            files=("methodology.md",),
        ),
        # Anchored to the two sentences that make this claim, not to 「N 倍」.
        # A bare ratio pattern also matched 「鄰站法比內插差 2.8 倍」 (M11) and
        # 「缺口長度變化 20 倍」 — both true, both about something else. A gate
        # that reports true sentences as errors is a gate that gets switched off.
        Claim(
            "sin/cos advantage under OLS",
            r"原始方位角的\s*\*{0,2}([\d.]+)\s*倍|效能提升達\s*\*{0,2}([\d.]+)\s*倍",
            encoded_r2 / raw_r2,
            2,
            files=("README.md", "PLAN.md", "methodology.md"),
        ),
    ]


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    claims = build_claims()

    problems: list[str] = []
    for claim in claims:
        for name in claim.files:
            path = SOURCES[name]
            if not path.exists():
                problems.append(f"{name} is missing")
                continue
            problems.extend(claim.check(name, path.read_text(encoding="utf-8")))

    for claim in claims:
        print(f"{claim.what:<32} = {claim.actual:g}")
    print(f"disagreements                    : {len(problems)}")
    for problem in problems:
        print(f"  {problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
