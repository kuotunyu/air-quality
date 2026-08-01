"""Solve a categorical palette — for the series that are KINDS, not amounts.

Every coloured series on this site is currently drawn from `--c0`..`--c6`, and
that ramp encodes concentration. Chapter 1 teaches the reader what those colours
mean and chapters 5 to 8 then spend them on gap-filling methods, forecast
baselines and counterfactual scenarios, none of which is a concentration. The
hue is saying something false.

It is also the worst available choice for telling series apart, and for a
structural reason: a sequential ramp is BUILT so that neighbours resemble each
other. Picking members of it for categories selects, on purpose, the pairs with
the least distance between them. `ChapterMethods` takes c3, c2 and c1 — three
adjacent steps.

So this solves a separate palette against a different objective. A sequential
ramp maximises the ORDER you can read off it; a categorical set maximises the
minimum distance between ANY two members, because there is no order to read.

The other half of the difference is deliberate and is what makes the two
readable as two: the concentration ramp has monotone lightness, and the
stylesheet says why — "Seven isoluminant hues say 'seven kinds of thing', not
'low to high'". For categories, "seven kinds of thing" is the correct sentence.
So lightness here is NOT monotone, and a reader can tell at a glance whether a
chart's colours mean a quantity or a kind.

    uv run python scripts/solve_categorical.py

Read-only. Prints candidate tokens; edits nothing.
"""

from __future__ import annotations

import sys
from itertools import combinations, pairwise

import numpy as np

sys.path.insert(0, "scripts")

from check_palette import (
    MARK_MIN,
    SHIPPED_DARK,
    SHIPPED_LIGHT,
    ciede2000,
    hexed,
    in_gamut,
    oklch_to_linear,
    simulate,
    to_lab,
    wcag_ratio,
)

# Deuteranopia and protanopia are ~8% of men between them. Tritanopia is scored
# and printed but not optimised against, on the same grounds the ramp records:
# the blue-yellow axis is where the ordering lives, and tritan is ~0.0001%.
SCORED = ("normal", "deuteranopia", "protanopia")

# The ramp's own ceiling: `--c6` is the most saturated token on the site at
# C 0.133. Left uncapped this search returns neon — the first run produced
# #1f6bff and #b7ef05 at chroma 0.232, which separate beautifully and belong to
# a different website. Maximum distinguishability is not the brief; maximum
# distinguishability inside this palette's voice is.
CHROMA_MAX = 0.135

# A categorical set has to be visibly NOT a ramp, and monotone lightness is the
# ramp's signature. The n=3 solve came back monotone on the first run, which
# reads as an ordering that does not exist.
MIN_LIGHTNESS_ZIGZAG = 1

# A series has to look like a series, and on this site "no hue" is already
# taken. The reference guides `--who` and `--taiwan` were deliberately stripped
# of hue so that hue means concentration and nothing else, so a near-neutral
# member would read as a guideline rather than as data. The n=4 solve returned
# #928777 at chroma 0.028 before this floor existed.
CHROMA_MIN = 0.055

# And it has to stay clear of those guides as colours, not just as a rule of
# thumb: both cross the plot area, both can run alongside a series.
GUIDES = {
    "light": ((0.628, 0.012, 250), (0.430, 0.012, 250)),
    "dark": ((0.536, 0.012, 250), (0.760, 0.012, 250)),
}
GUIDE_MIN = 12.0


def marks_of(specs: list[tuple[float, float, float]]) -> list[np.ndarray]:
    return [oklch_to_linear(*s) for s in specs]


def turns(lightnesses: list[float]) -> int:
    """How many times the lightness sequence changes direction."""
    signs = [np.sign(b - a) for a, b in pairwise(lightnesses) if b != a]
    return sum(1 for a, b in pairwise(signs) if a != b)


def legal(
    specs: list[tuple[float, float, float]],
    surfaces: list[np.ndarray],
    guides: tuple[tuple[float, float, float], ...] = (),
) -> bool:
    for _lightness, chroma, _hue in specs:
        if not CHROMA_MIN <= chroma <= CHROMA_MAX:
            return False
    for linear in marks_of(specs):
        if not in_gamut(linear):
            return False
        if min(wcag_ratio(linear, s) for s in surfaces) < MARK_MIN:
            return False
    if guides:
        guide_labs = [to_lab(oklch_to_linear(*g)) for g in guides]
        for linear in marks_of(specs):
            lab = to_lab(linear)
            if min(ciede2000(lab, g) for g in guide_labs) < GUIDE_MIN:
                return False
    return turns([s[0] for s in specs]) >= MIN_LIGHTNESS_ZIGZAG


def min_pairwise(specs: list[tuple[float, float, float]], kinds: tuple[str, ...] = SCORED) -> float:
    """The smallest distance between ANY two members, over every vision scored.

    Adjacent-only is the right measure for a ramp, where a reader compares a
    mark with the step above it. On a line chart with four series any pair can
    end up crossing, so the weakest pair anywhere is what decides.
    """
    worst = 1e9
    for kind in kinds:
        marks = marks_of(specs)
        if kind != "normal":
            marks = [simulate(m, kind) for m in marks]
        labs = [to_lab(m) for m in marks]
        for i, j in combinations(range(len(labs)), 2):
            worst = min(worst, ciede2000(labs[i], labs[j]))
    return worst


def solve(
    n: int,
    surfaces: list[np.ndarray],
    *,
    lightness_band: tuple[float, float],
    guides: tuple[tuple[float, float, float], ...] = (),
    seed: int = 20260731,
    restarts: int = 24,
    steps: int = 4000,
) -> tuple[list[tuple[float, float, float]], float]:
    """Random restart plus local hill-climbing on (L, C, H) for each member.

    Seeded, so the tokens this prints can be re-derived. There is no gradient to
    follow here — the objective runs through a gamut test, a contrast test and
    three colour-blindness simulations — so a direct search is the honest tool
    rather than a solver that would need the problem lied about to fit it.
    """
    rng = np.random.default_rng(seed)
    lo, hi = lightness_band
    best: list[tuple[float, float, float]] = []
    best_score = -1.0

    for _ in range(restarts):
        current = [
            (
                float(rng.uniform(lo, hi)),
                float(rng.uniform(CHROMA_MIN, CHROMA_MAX)),
                float(rng.uniform(0, 360)),
            )
            for _ in range(n)
        ]
        if not legal(current, surfaces, guides):
            continue
        score = min_pairwise(current)

        temperature = 1.0
        for step in range(steps):
            temperature = 1.0 - step / steps
            i = int(rng.integers(n))
            lightness, chroma, hue = current[i]
            trial = list(current)
            trial[i] = (
                float(np.clip(lightness + rng.normal(0, 0.05 * temperature), lo, hi)),
                float(np.clip(chroma + rng.normal(0, 0.03 * temperature), CHROMA_MIN, CHROMA_MAX)),
                float((hue + rng.normal(0, 40 * temperature)) % 360),
            )
            if not legal(trial, surfaces, guides):
                continue
            trial_score = min_pairwise(trial)
            if trial_score > score:
                current, score = trial, trial_score

        if score > best_score:
            best, best_score = current, score

    return best, best_score


def show(title: str, specs: list[tuple[float, float, float]], surfaces: list[np.ndarray]) -> None:
    print(f"\n  {title}")
    lightnesses = [s[0] for s in specs]
    monotone = lightnesses == sorted(lightnesses) or lightnesses == sorted(
        lightnesses, reverse=True
    )
    for i, spec in enumerate(specs):
        linear = oklch_to_linear(*spec)
        low = min(wcag_ratio(linear, s) for s in surfaces)
        print(
            f"    k{i}  oklch({spec[0]:.3f} {spec[1]:.3f} {spec[2]:.0f})  "
            f"{hexed(linear)}  contrast {low:.2f}"
        )
    print(f"    min pairwise dE00: {min_pairwise(specs):.1f}  (normal, deutan, protan)")
    print(f"    tritanopia:        {min_pairwise(specs, ('tritanopia',)):.1f}")
    print(f"    lightness monotone: {monotone}  <- must be False to read as categorical")


def before_and_after() -> list[tuple[str, list[int], str]]:
    """What each chart took from the concentration ramp, and what it takes now.

    Kept as a table rather than deleted once the change landed: the numbers in
    the left column are the reason the right column exists, and a reader asking
    "why are there two palettes" should be able to run this and see it.
    """
    return [
        ("ChapterMethods, three gap-filling methods", [3, 2, 1], "k0 k1 k2"),
        ("ChapterForecast, model / persistence / climatology", [0, 5, 3], "k0 k1 k2"),
        ("ChapterTrend fig 2, observed / normalised", [1, 5], "k0 k1"),
        ("ChapterTrend fig 1, all stations / fixed sample", [1, 3], "k0 k1"),
        (
            "ChapterHealth, four counterfactuals (KEPT the ramp — see below)",
            [6, 4, 2, 0],
            "unchanged, + dashes",
        ),
    ]


def main() -> None:
    for theme, palette in (("light", SHIPPED_LIGHT), ("dark", SHIPPED_DARK)):
        surfaces = [oklch_to_linear(*v) for v in palette.surfaces.values()]
        print("=" * 78)
        print(f"{theme} theme")
        print("=" * 78)

        print("\n  worst pair per chart, as it was on the ramp")
        for label, picks, now in before_and_after():
            specs = [palette.ramp[i] for i in picks]
            ramp = "c" + "".join(str(p) for p in picks)
            print(f"    {min_pairwise(specs):5.1f}   {ramp:<6} -> {now:<20} {label}")
        print(
            "\n  ChapterHealth keeps the ramp because its four ARE ordered and the"
            "\n  ordering is the argument; it gained dash patterns instead, which"
            "\n  separate with no hue at all."
        )

        band = (0.30, 0.66) if theme == "light" else (0.55, 0.88)
        for n in (3, 4, 5):
            specs, score = solve(n, surfaces, lightness_band=band, guides=GUIDES[theme])
            if not specs:
                print(f"\n  n={n}: no legal start found")
                continue
            show(f"solved categorical, n={n}   (worst pair {score:.1f})", specs, surfaces)


if __name__ == "__main__":
    main()
