"""The website retypes measurements too, and nothing was reading it.

Five prose gates already compare `docs/*.md` against the committed payloads.
Every one of them reads Markdown. **None reads a `.astro` file**, and the website
is the surface readers actually use.

That gap stayed cheap while the chapters were data-driven — a 2026-08-18 sweep
found one numeric literal in rendered prose, `4.55%`, which is the two-tailed
area beyond 2 sigma under a normal distribution and cannot drift. Then 17,448
lines of editorial change landed and the sweep was never repeated. It should
have been:

* `ChapterSpatial.astro` said the raw island-wide correlation was **0.73**, while
  `reports/03-spatial.md`, which regenerates from `metadata.parquet` on every
  run, said **0.782**. 0.73 is the figure `docs/working-rules.md` cites as this
  project's canonical drift case — corrected in `conf/spatial.yaml`, in
  `docs/methodology.md` and in the report, while **the website copy was never
  brought into that fix**, and live for as long as it took to build this.
* Its caption said the correlogram runs to **±0.4**. That was the axis end and
  the outermost tick when the caption was written, at `4232e8e`; after the
  network went from 60 stations to 61 the axis ends at ±0.32 and the outermost
  tick reads 0.2. The `maxAbs` it should have quoted is four lines above it.
* `ChapterMethods.astro` said month explains **20.1%** of hourly variance;
  `pitfalls.json` says **20.298%**. The line directly above interpolated that
  paragraph's other figure from the same payload — one paragraph, one number read
  from the data and two typed by hand, and a typed one drifted.

All three came from the same re-run, and none of them changed a conclusion: they
are the reasons for a method, not results. That is precisely why they went
unnoticed, and why a gate rather than a proofread is the right instrument.

Truth comes from three committed sources, never from `data/`, which is gitignored
and invisible to CI:

* `reports/03-spatial.md` — regenerated from the M6 outputs on every run
* `web/public/data/story/pitfalls.json` — the D-chapter export
* `web/public/data/manifest.json` — file sizes, as exported

Comparison is at the precision each sentence prints, with one unit of the last
place as tolerance, because the sources are themselves rounded: a chapter
printing two decimals of 0.006 may legitimately write 0.01, and a gate that
failed on that would be switched off within a week. 0.73 against 0.782 is five
units away and still caught.

    uv run python scripts/check_published_site_prose.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPONENTS = REPO_ROOT / "web" / "src" / "components"
SPATIAL_REPORT = REPO_ROOT / "reports" / "03-spatial.md"
PITFALLS = REPO_ROOT / "web" / "public" / "data" / "story" / "pitfalls.json"
MANIFEST = REPO_ROOT / "web" / "public" / "data" / "manifest.json"

# U+2212 MINUS SIGN reads better in prose and does not parse as a hyphen.
MINUS = {"−": "-", "–": "-"}


def num(text: str) -> float:
    for odd, plain in MINUS.items():
        text = text.replace(odd, plain)
    return float(text.strip().strip("*").replace("+", ""))


def flat(text: str) -> str:
    """One line, so a pattern need not know where the source happened to wrap."""
    return re.sub(r"\s+", " ", text)


def expression(source: str) -> str:
    """A regex matching exactly this interpolation, tolerant only of whitespace.

    The first version of this gate accepted `\\{[^{}]*\\}` — any interpolation at
    all. That satisfies the contract it was written for, 「this number can no
    longer drift」, and still passes a caption that reads the wrong variable: the
    correlogram's axis interpolated into the controls caption cannot go stale and
    is wrong every time it renders. Naming the expression closes that.

    Whitespace is the one thing allowed to move, because a formatter may reflow
    `{n(maxI, 2)}` to `{n(maxI,2)}` and that is not a change of variable.
    """
    return r"\s*".join(re.escape(part) for part in source.split())


def agrees(quoted: float, actual: float, places: int | None) -> bool:
    """Equal once both are read at the precision the prose prints.

    ``places=None`` means an exact count — never compare those with a tolerance.
    `check_published_spatial.py` shipped with ``places=0`` for its counts, which
    set the tolerance to 1.0 and made every off-by-one agree; off-by-one was
    exactly what it existed to catch.
    """
    if places is None:
        return quoted == actual
    return abs(quoted - actual) <= 10.0**-places


class Claim:
    """One sentence in one chapter, the number it types, and the truth for it."""

    def __init__(
        self,
        component: str,
        what: str,
        pattern: str,
        places: int | None,
        *,
        without_a_literal: str | None = None,
    ) -> None:
        self.component = component
        self.what = what
        self.pattern = re.compile(pattern)
        self.places = places
        # The same sentence with the number no longer typed. Two shapes count as
        # fixed: the page interpolates it from the payload at build time, or the
        # prose stops quoting a figure and names the file that recomputes it —
        # what `docs/methodology.md` did with this project's canonical drift
        # case. Neither can drift, so neither needs comparing.
        #
        # This is not a way out of the gate. The pattern names the shape the
        # sentence must take, so **deleting the sentence still fails**: without
        # it, removing a methodological caveat and fixing one would look
        # identical from here.
        self.without_a_literal = re.compile(without_a_literal) if without_a_literal else None

    def check(self, text: str, actual: float) -> list[str]:
        matches = list(self.pattern.finditer(text))
        if not matches:
            if self.without_a_literal is not None and self.without_a_literal.search(text):
                return []
            return [
                f"{self.component:<22} {self.what:<28} no longer matches — reworded or removed?"
            ]
        problems = []
        for match in matches:
            quoted = num(match.group(1))
            if not agrees(quoted, actual, self.places):
                problems.append(
                    f"{self.component:<22} {self.what:<28} says {quoted:g}, truth is {actual:g}"
                )
        return problems


def spatial_truth() -> dict[str, float]:
    if not SPATIAL_REPORT.exists():
        raise SystemExit(f"no report at {SPATIAL_REPORT} — regenerate it first")
    text = flat(SPATIAL_REPORT.read_text(encoding="utf-8"))

    found: dict[str, float] = {}
    for key, pattern in (
        ("raw_correlation", r"兩兩平均相關\s*\*\*([\d.]+)\*\*"),
        ("anomaly_correlation", r"降到\s*\*\*([\d.]+)\*\*"),
        ("spacing_min", r"從\s*\*\*([\d.]+)\s*km\*\*"),
        ("spacing_max", r"到\s*\*\*([\d.]+)\s*km\*\*"),
    ):
        match = re.search(pattern, text)
        if match is None:
            raise SystemExit(
                f"{SPATIAL_REPORT.name} no longer states {key} — has the report been reworded?"
            )
        found[key] = num(match.group(1))
    return found


def pitfalls_truth() -> dict[str, float]:
    if not PITFALLS.exists():
        raise SystemExit(f"no payload at {PITFALLS} — run `twair export web` first")
    payload: dict[str, Any] = json.loads(PITFALLS.read_text(encoding="utf-8"))
    rows = payload.get("tables", {}).get("diurnal_variance")
    if not rows:
        raise SystemExit(f"{PITFALLS.name} has no diurnal_variance — re-export after the analysis")
    by_scale = {row["scale"]: row["variance_retained"] for row in rows}
    missing = sorted({"monthly_mean", "station_month"} - set(by_scale))
    if missing:
        raise SystemExit(f"{PITFALLS.name} diurnal_variance is missing {missing}")
    return {
        "month_pct": by_scale["monthly_mean"] * 100,
        "station_month_pct": by_scale["station_month"] * 100,
    }


def structure_truth() -> dict[str, float]:
    """Figures whose captions quote how far their own bars reach."""
    path = REPO_ROOT / "web" / "public" / "data" / "story" / "spatial-structure.json"
    if not path.exists():
        raise SystemExit(f"no payload at {path} — run `twair export web` first")
    payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    missing = sorted({"controls", "correlogram"} - set(payload))
    if missing:
        raise SystemExit(f"{path.name} is missing {missing}")
    max_abs_i = max(abs(row["i"]) for row in payload["correlogram"])
    return {
        # Both captions quote an **axis end**, not a data extreme — 「上圖是 ±X」
        # and 「這裡是 0 到 Y」 describe the scales the reader is being told not to
        # compare across. Anchoring the controls one to `max(mean_i_hi)` instead
        # let 「0 到 0.18」 pass: 0.1752 is 0.0048 away, inside a two-place
        # tolerance, while the axis actually ends at 0.196224 and the outermost
        # labelled tick is 0.15. 0.18 was none of the three.
        #
        # Both headroom factors are duplicated here rather than parsed out of the
        # .astro, because a gate that read the constant from the file it checks
        # would agree with whatever that file chose.
        "controls_axis": max(row["mean_i_hi"] for row in payload["controls"]) * 1.12,
        "correlogram_axis": max_abs_i * 1.15,
    }


def deweather_truth() -> dict[str, float]:
    path = REPO_ROOT / "web" / "public" / "data" / "story" / "deweather.json"
    if not path.exists():
        raise SystemExit(f"no payload at {path} — run `twair export web` first")
    payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    try:
        panel = payload["panel"]["weather_share_of_fall"] * 100
        median = payload["median_weather_share"] * 100
    except (KeyError, TypeError) as exc:
        raise SystemExit(f"{path.name} no longer carries both weather shares: {exc}") from exc
    return {"panel_pct": panel, "median_pct": median, "gap": abs(panel - median)}


def check_the_two_aggregations_still_agree(text: str, gap: float) -> list[str]:
    """`ChapterTrend.astro` reads both weather shares from the payload and then
    types a bound on their difference: 「相差不到 1.5 個百分點」.

    Both without_a_literal numbers can stay correct while a re-run pushes them apart,
    and the sentence beneath them becomes false with nothing to notice — the
    same shape as D8's 「低於機率預期」, which `check_published_detection.py`
    checks as a relation rather than as cells. So the bound is read out of the
    prose and tested against the payload, not compared to a stored copy of
    itself.
    """
    match = re.search(r"相差不到\s*([\d.]+)\s*個百分點", text)
    if match is None:
        return ["ChapterTrend.astro      agreement bound         no longer stated — reworded?"]
    claimed = num(match.group(1))
    if gap <= claimed:
        return []
    return [
        f"ChapterTrend.astro      agreement bound         "
        f"claims the two aggregations differ by under {claimed:g} points, "
        f"payload has {gap:.2f}"
    ]


def manifest_truth() -> dict[str, float]:
    if not MANIFEST.exists():
        raise SystemExit(f"no manifest at {MANIFEST} — run `twair export web` first")
    payload: dict[str, Any] = json.loads(MANIFEST.read_text(encoding="utf-8"))
    l1 = [item for item in payload["files"] if str(item["file"]).startswith("l1/")]
    if not l1:
        raise SystemExit(f"{MANIFEST.name} lists no l1 files — has the export layout changed?")
    return {"l1_files": float(len(l1)), "l1_mb": sum(item["bytes"] for item in l1) / 1e6}


CLAIMS: tuple[tuple[Claim, str, str], ...] = (
    (
        Claim(
            "ChapterSpatial.astro",
            "raw island correlation",
            r"生料月均值全島相關\s*([\d.]+)",
            2,
            # The chapter now states the property and hands the figure to the
            # report, as `docs/methodology.md` already did. The caveat itself is
            # load-bearing — it is why the anomaly step exists — so the pattern
            # requires the sentence, not merely the absence of a number.
            without_a_literal=r"生料月均值在全島高度相關.*?reports/03-spatial\.md",
        ),
        "spatial",
        "raw_correlation",
    ),
    (
        Claim(
            "ChapterSpatial.astro",
            "anomaly correlation",
            r"平均相關掉到\s*([\d.]+)",
            2,
            without_a_literal=r"平均相關趨近於零",
        ),
        "spatial",
        "anomaly_correlation",
    ),
    (
        Claim(
            "ChapterSpatial.astro",
            "nearest-neighbour min km",
            r"最近鄰距離從\s*([\d.]+)\s*到",
            1,
        ),
        "spatial",
        "spacing_min",
    ),
    (
        Claim(
            "ChapterSpatial.astro",
            "nearest-neighbour max km",
            r"最近鄰距離從\s*[\d.]+\s*到\s*([\d.]+)\s*公里",
            None,
        ),
        "spatial",
        "spacing_max",
    ),
    (
        Claim(
            "ChapterMethods.astro",
            "month share of variance",
            r"月份解釋\s*([\d.]+)%",
            1,
            without_a_literal=r"月份解釋\s*" + expression("{n(pooledRetained * 100, 1)}") + "%",
        ),
        "pitfalls",
        "month_pct",
    ),
    (
        Claim(
            "ChapterMethods.astro",
            "station x month share",
            r"「測站 × 月份」解釋\s*([\d.]+)%",
            1,
            without_a_literal=r"「測站 × 月份」解釋\s*"
            + expression("{n(retained * 100, 1)}")
            + "%",
        ),
        "pitfalls",
        "station_month_pct",
    ),
    (
        Claim(
            "ChapterSpatial.astro",
            "correlogram scale",
            r"上圖是\s*±([\d.]+)\s*的距離分帶",
            2,
            # `maxAbs` is `max|I| * 1.15`, four lines from the caption that
            # quoted it. When the caption was written the axis ended at 0.4004
            # and the outermost tick label read 0.4; after the network went from
            # 60 stations to 61 the axis ends at 0.319 and the outermost tick is
            # 0.2, and the caption still said 0.4.
            without_a_literal=r"上圖是\s*±" + expression("{n(maxAbs, 2)}") + r"\s*的距離分帶",
        ),
        "structure",
        "correlogram_axis",
    ),
    (
        Claim(
            "ChapterSpatial.astro",
            "controls bar extent",
            r"這裡是\s*0\s*到\s*([\d.]+)",
            2,
            without_a_literal=r"這裡是\s*0\s*到\s*" + expression("{n(maxI, 2)}"),
        ),
        "structure",
        "controls_axis",
    ),
    (
        Claim(
            "Explorer.astro",
            "published measurand count",
            r"完整的\s*(\d+)\s*個測項",
            None,
        ),
        "manifest",
        "l1_files",
    ),
    (
        Claim(
            "Explorer.astro",
            "published bundle size",
            r"個測項共\s*([\d.]+)\s*MB",
            1,
        ),
        "manifest",
        "l1_mb",
    ),
)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

    truths = {
        "spatial": spatial_truth(),
        "structure": structure_truth(),
        "pitfalls": pitfalls_truth(),
        "manifest": manifest_truth(),
    }
    deweather = deweather_truth()

    sources: dict[str, str] = {}
    problems: list[str] = []
    for claim, source, key in CLAIMS:
        if claim.component not in sources:
            path = COMPONENTS / claim.component
            if not path.exists():
                problems.append(f"{claim.component:<22} missing — has the chapter been renamed?")
                sources[claim.component] = ""
            else:
                sources[claim.component] = flat(path.read_text(encoding="utf-8"))
        text = sources[claim.component]
        if text:
            problems.extend(claim.check(text, truths[source][key]))

    trend = COMPONENTS / "ChapterTrend.astro"
    if not trend.exists():
        problems.append("ChapterTrend.astro     missing — has the chapter been renamed?")
    else:
        sources[trend.name] = flat(trend.read_text(encoding="utf-8"))
        problems.extend(
            check_the_two_aggregations_still_agree(sources[trend.name], deweather["gap"])
        )

    read = [name for name, text in sources.items() if text]
    if not read:
        raise SystemExit("no chapter was read — refusing to report success for checking none")

    print(f"claims checked   : {len(CLAIMS)} + 1 relation")
    print(f"chapters read    : {len(read)}")
    print(f"disagreements    : {len(problems)}")
    for problem in problems:
        print(f"  {problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
