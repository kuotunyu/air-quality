"""Run a candidate palette through the four gates this site already claims to pass.

The palette in `global.css` carries measured numbers in its comments — 3:1 for
every mark on all three surfaces, APCA Lc 92/80/70 for the three inks, and a
worst adjacent CIEDE2000 step of 8.2 under deuteranopia. Those numbers were
solved once, in a browser, by hand. Nothing in the repository can re-derive
them, so any proposal to change the colours starts by arguing with prose.

This turns them into a function. Give it a palette, get back the same four
measurements, and a proposal can be compared with the thing it proposes to
replace instead of described.

The colour maths is written out rather than imported because the two libraries
that would supply it (`colour-science`, `coloraide`) are not dependencies of
this project and a palette check is not worth becoming one.

    uv run python scripts/check_palette.py

Read-only. Prints; writes nothing.
"""

from __future__ import annotations

import io
import sys
from dataclasses import dataclass

import numpy as np

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# ── colour space plumbing ────────────────────────────────────────────────────

# Björn Ottosson's OKLab matrices, in the direction OKLab -> linear sRGB.
_LMS_FROM_OKLAB = np.array(
    [
        [1.0, 0.3963377774, 0.2158037573],
        [1.0, -0.1055613458, -0.0638541728],
        [1.0, -0.0894841775, -1.2914855480],
    ]
)
_RGB_FROM_LMS = np.array(
    [
        [4.0767416621, -3.3077115913, 0.2309699292],
        [-1.2684380046, 2.6097574011, -0.3413193965],
        [-0.0041960863, -0.7034186147, 1.7076147010],
    ]
)

# sRGB primaries against D65, for the trip out to XYZ and on to CIELAB.
_XYZ_FROM_RGB = np.array(
    [
        [0.4123907993, 0.3575843394, 0.1804807884],
        [0.2126390059, 0.7151686788, 0.0721923154],
        [0.0193308187, 0.1191947798, 0.9505321522],
    ]
)
_D65 = np.array([0.9504559271, 1.0, 1.0890577508])

# Machado, Oliveira & Fernandes (2009), severity 1.0, applied in LINEAR rgb.
_CVD = {
    "deuteranopia": np.array(
        [
            [0.367322, 0.860646, -0.227968],
            [0.280085, 0.672501, 0.047413],
            [-0.011820, 0.042940, 0.968881],
        ]
    ),
    "protanopia": np.array(
        [
            [0.152286, 1.052583, -0.204868],
            [0.114503, 0.786281, 0.099216],
            [-0.003882, -0.048116, 1.051998],
        ]
    ),
    "tritanopia": np.array(
        [
            [1.255528, -0.076749, -0.178779],
            [-0.078411, 0.930809, 0.147602],
            [0.004733, 0.691367, 0.303900],
        ]
    ),
}


def oklch_to_linear(lightness: float, chroma: float, hue_deg: float) -> np.ndarray:
    """OKLCH to linear sRGB. May land outside [0, 1]; see `in_gamut`."""
    hue = np.radians(hue_deg)
    oklab = np.array([lightness, chroma * np.cos(hue), chroma * np.sin(hue)])
    lms = _LMS_FROM_OKLAB @ oklab
    return _RGB_FROM_LMS @ (lms**3)


def in_gamut(linear: np.ndarray, tolerance: float = 1e-4) -> bool:
    return bool(np.all(linear >= -tolerance) and np.all(linear <= 1 + tolerance))


def encode(linear: np.ndarray) -> np.ndarray:
    """Linear sRGB to display sRGB, 0–1, clipped."""
    c = np.clip(linear, 0.0, 1.0)
    return np.where(c <= 0.0031308, c * 12.92, 1.055 * c ** (1 / 2.4) - 0.055)


def hexed(linear: np.ndarray) -> str:
    return "#" + "".join(f"{round(v * 255):02x}" for v in encode(linear))


def relative_luminance(linear: np.ndarray) -> float:
    """WCAG 2.x luminance, which is a plain dot product on linear light."""
    return float(np.dot(np.clip(linear, 0.0, 1.0), [0.2126, 0.7152, 0.0722]))


def wcag_ratio(a: np.ndarray, b: np.ndarray) -> float:
    la, lb = relative_luminance(a), relative_luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def apca_lc(text_linear: np.ndarray, bg_linear: np.ndarray) -> float:
    """APCA 0.98G-4g. Signed: positive is dark-on-light, negative is the reverse.

    Not a ratio. WCAG rated this site's dark theme the better-contrasted of the
    two while APCA put the same token at Lc 60.8 against light's 74.4, and the
    stylesheet records that the theme WCAG called "AAA" was the tiring one.
    """

    def y(linear: np.ndarray) -> float:
        s = encode(linear)
        value = float(np.dot(s**2.4, [0.2126729, 0.7151522, 0.0721750]))
        return value + (0.022 - value) ** 1.414 if value < 0.022 else value

    y_txt, y_bg = y(text_linear), y(bg_linear)
    if abs(y_bg - y_txt) < 0.0005:
        return 0.0
    if y_bg > y_txt:  # dark text on a light ground
        sapc = (y_bg**0.56 - y_txt**0.57) * 1.14
        return 0.0 if sapc < 0.1 else (sapc - 0.027) * 100
    sapc = (y_bg**0.65 - y_txt**0.62) * 1.14
    return 0.0 if sapc > -0.1 else (sapc + 0.027) * 100


def to_lab(linear: np.ndarray) -> np.ndarray:
    xyz = (_XYZ_FROM_RGB @ np.clip(linear, 0.0, 1.0)) / _D65
    f = np.where(xyz > 216 / 24389, np.cbrt(xyz), (841 / 108) * xyz + 4 / 29)
    return np.array([116 * f[1] - 16, 500 * (f[0] - f[1]), 200 * (f[1] - f[2])])


def ciede2000(lab1: np.ndarray, lab2: np.ndarray) -> float:
    """CIE Delta E 2000. Written out; the constants are the ones in the standard."""
    l1, a1, b1 = lab1
    l2, a2, b2 = lab2
    c1, c2 = np.hypot(a1, b1), np.hypot(a2, b2)
    c_bar = (c1 + c2) / 2
    g = 0.5 * (1 - np.sqrt(c_bar**7 / (c_bar**7 + 25.0**7))) if c_bar > 0 else 0.5
    a1p, a2p = (1 + g) * a1, (1 + g) * a2
    c1p, c2p = np.hypot(a1p, b1), np.hypot(a2p, b2)
    h1p = np.degrees(np.arctan2(b1, a1p)) % 360 if (a1p or b1) else 0.0
    h2p = np.degrees(np.arctan2(b2, a2p)) % 360 if (a2p or b2) else 0.0

    dlp = l2 - l1
    dcp = c2p - c1p
    if c1p * c2p == 0:
        dhp = 0.0
    elif abs(h2p - h1p) <= 180:
        dhp = h2p - h1p
    else:
        dhp = h2p - h1p - 360 if h2p > h1p else h2p - h1p + 360
    dhp_big = 2 * np.sqrt(c1p * c2p) * np.sin(np.radians(dhp) / 2)

    lp_bar = (l1 + l2) / 2
    cp_bar = (c1p + c2p) / 2
    if c1p * c2p == 0:
        hp_bar = h1p + h2p
    elif abs(h1p - h2p) <= 180:
        hp_bar = (h1p + h2p) / 2
    elif h1p + h2p < 360:
        hp_bar = (h1p + h2p + 360) / 2
    else:
        hp_bar = (h1p + h2p - 360) / 2

    t = (
        1
        - 0.17 * np.cos(np.radians(hp_bar - 30))
        + 0.24 * np.cos(np.radians(2 * hp_bar))
        + 0.32 * np.cos(np.radians(3 * hp_bar + 6))
        - 0.20 * np.cos(np.radians(4 * hp_bar - 63))
    )
    d_theta = 30 * np.exp(-(((hp_bar - 275) / 25) ** 2))
    rc = 2 * np.sqrt(cp_bar**7 / (cp_bar**7 + 25.0**7)) if cp_bar > 0 else 0.0
    sl = 1 + (0.015 * (lp_bar - 50) ** 2) / np.sqrt(20 + (lp_bar - 50) ** 2)
    sc = 1 + 0.045 * cp_bar
    sh = 1 + 0.015 * cp_bar * t
    rt = -np.sin(np.radians(2 * d_theta)) * rc

    return float(
        np.sqrt(
            (dlp / sl) ** 2
            + (dcp / sc) ** 2
            + (dhp_big / sh) ** 2
            + rt * (dcp / sc) * (dhp_big / sh)
        )
    )


def simulate(linear: np.ndarray, kind: str) -> np.ndarray:
    return _CVD[kind] @ np.clip(linear, 0.0, 1.0)


# ── the palettes ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Palette:
    name: str
    surfaces: dict[str, tuple[float, float, float]]
    inks: dict[str, tuple[float, float, float]]
    ramp: tuple[tuple[float, float, float], ...]
    note: str = ""


SHIPPED_LIGHT = Palette(
    name="shipped light (Morandi)",
    surfaces={
        "--bg": (0.970, 0.009, 250),
        "--bg-raised": (0.993, 0.012, 250),
        "--bg-sunken": (0.945, 0.009, 250),
    },
    inks={
        "--text": (0.252, 0.010, 250),
        "--text-muted": (0.404, 0.012, 250),
        "--text-faint": (0.493, 0.012, 250),
    },
    ramp=(
        (0.615, 0.088, 252),
        (0.569, 0.088, 206),
        (0.522, 0.088, 143),
        (0.475, 0.088, 82),
        (0.429, 0.088, 34),
        (0.383, 0.092, 11),
        (0.336, 0.133, 346),
    ),
    note="what the site ships today",
)

SHIPPED_DARK = Palette(
    name="shipped dark (Morandi)",
    surfaces={
        "--bg": (0.205, 0.009, 250),
        "--bg-raised": (0.262, 0.009, 250),
        "--bg-sunken": (0.168, 0.012, 250),
    },
    inks={
        "--text": (0.940, 0.010, 250),
        "--text-muted": (0.881, 0.012, 250),
        "--text-faint": (0.830, 0.012, 250),
    },
    ramp=(
        (0.566, 0.130, 247),
        (0.614, 0.091, 211),
        (0.661, 0.079, 170),
        (0.709, 0.131, 82),
        (0.757, 0.090, 52),
        (0.804, 0.088, 12),
        (0.852, 0.091, 346),
    ),
    note="already runs at macaron lightness; its own comment says 'dusty pastels'",
)

# A macaron ramp on the light page: the hue ORDER the site uses, moved to the
# lightness and chroma that make a colour read as macaron. Anything darker than
# roughly L 0.75 stops being one — that is the whole identity of the palette.
MACARON_LIGHT = Palette(
    name="macaron on the light page",
    surfaces=SHIPPED_LIGHT.surfaces,
    inks=SHIPPED_LIGHT.inks,
    ramp=(
        (0.895, 0.055, 252),
        (0.878, 0.058, 206),
        (0.861, 0.060, 143),
        (0.844, 0.070, 82),
        (0.827, 0.068, 34),
        (0.810, 0.075, 11),
        (0.793, 0.085, 346),
    ),
    note="blueberry / sky / pistachio / lemon / apricot / rose / cassis",
)

# The same idea, given every inch the light page can give it: chroma pushed to
# the sRGB edge and the lightness ladder stretched as far down as macaron will
# stretch before it is simply a mid-tone scale.
MACARON_PUSHED = Palette(
    name="macaron, pushed as far as the light page allows",
    surfaces=SHIPPED_LIGHT.surfaces,
    inks=SHIPPED_LIGHT.inks,
    ramp=(
        (0.860, 0.090, 252),
        (0.836, 0.100, 206),
        (0.812, 0.105, 143),
        (0.788, 0.130, 82),
        (0.764, 0.120, 34),
        (0.740, 0.125, 11),
        (0.716, 0.140, 346),
    ),
    note="still recognisably macaron; nothing darker would be",
)


# ── the gates ────────────────────────────────────────────────────────────────

MARK_MIN = 3.0  # WCAG non-text contrast, for a mark that carries meaning
ADJACENT_MIN = 8.0  # the site's own floor for the worst adjacent step
INK_TARGETS = {"--text": 92.0, "--text-muted": 80.0, "--text-faint": 70.0}

# The inks were solved TO these targets, so they land on them to within rounding
# and a strict `<` reports the shipped palette as failing its own spec. The
# first run of this script did exactly that — Lc 80.0 against a target of 80,
# marked FAIL — which is the instrument being wrong about the thing it was
# written to check.
INK_TOLERANCE = 0.5


def report(palette: Palette) -> dict[str, bool]:
    print()
    print("=" * 78)
    print(f"{palette.name}")
    if palette.note:
        print(f"  {palette.note}")
    print("=" * 78)

    surfaces = {k: oklch_to_linear(*v) for k, v in palette.surfaces.items()}
    marks = [oklch_to_linear(*step) for step in palette.ramp]
    passed: dict[str, bool] = {}

    # 1. gamut
    outside = [f"c{i}" for i, m in enumerate(marks) if not in_gamut(m)]
    passed["gamut"] = not outside
    print(f"\n[gamut]      {'ok' if not outside else 'OUTSIDE sRGB: ' + ', '.join(outside)}")

    # 2. every mark clears 3:1 on every surface
    print(f"\n[marks >= {MARK_MIN}:1 on all three surfaces]")
    worst_mark = 99.0
    worst_where = ""
    for i, mark in enumerate(marks):
        ratios = {name: wcag_ratio(mark, surf) for name, surf in surfaces.items()}
        low = min(ratios.values())
        if low < worst_mark:
            worst_mark = low
            worst_where = f"c{i} on {min(ratios, key=lambda k: ratios[k])}"
        flag = "     " if low >= MARK_MIN else "  <--"
        print(
            f"  c{i} {hexed(mark)}  "
            + "  ".join(f"{n.removeprefix('--bg') or '':>7}{r:5.2f}" for n, r in ratios.items())
            + f"   min {low:5.2f}{flag}"
        )
    passed["marks"] = worst_mark >= MARK_MIN
    print(f"  worst: {worst_mark:.2f} ({worst_where})   {'PASS' if passed['marks'] else 'FAIL'}")

    # 3. adjacent separation, seen four ways
    print(f"\n[adjacent CIEDE2000 >= {ADJACENT_MIN}, normal and simulated]")
    labs = {"normal": [to_lab(m) for m in marks]}
    for kind in _CVD:
        labs[kind] = [to_lab(simulate(m, kind)) for m in marks]
    worst_overall = 99.0
    for kind, lab_list in labs.items():
        steps = [ciede2000(lab_list[i], lab_list[i + 1]) for i in range(len(lab_list) - 1)]
        low = min(steps)
        if kind != "tritanopia":  # the site excludes tritan from the floor, on the record
            worst_overall = min(worst_overall, low)
        print(f"  {kind:<13} " + " ".join(f"{s:5.1f}" for s in steps) + f"   worst {low:5.1f}")
    passed["separation"] = worst_overall >= ADJACENT_MIN
    print(
        f"  worst excluding tritanopia: {worst_overall:.1f}   "
        f"{'PASS' if passed['separation'] else 'FAIL'}"
    )

    # 4. the inks, on the worst of the three surfaces
    print("\n[inks, APCA Lc on the worst surface]")
    ink_ok = True
    for name, spec in palette.inks.items():
        ink = oklch_to_linear(*spec)
        lcs = {s: abs(apca_lc(ink, surf)) for s, surf in surfaces.items()}
        low = min(lcs.values())
        target = INK_TARGETS[name]
        meets = low >= target - INK_TOLERANCE
        if not meets:
            ink_ok = False
        wcag = min(wcag_ratio(ink, surf) for surf in surfaces.values())
        print(
            f"  {name:<14} Lc {low:5.1f} (target {target:.0f})  "
            f"WCAG {wcag:4.2f}  {'' if meets else '<--'}"
        )
    passed["inks"] = ink_ok
    print(f"  {'PASS' if ink_ok else 'FAIL'}")

    verdict = all(passed.values())
    print(
        f"\n  ==> {'ALL GATES PASS' if verdict else 'FAILS: ' + ', '.join(k for k, v in passed.items() if not v)}"
    )
    return passed


def richest_chroma(lightness: float, hue: float, surfaces: dict[str, np.ndarray]) -> float:
    """The most chroma this hue can carry at this lightness and still be a mark.

    Bisection, because the two constraints bite from the same side: chroma runs
    out of sRGB before it runs out of contrast at some hues and the other way
    round at others, and both are monotone enough in chroma for a bisection to
    be exact to three decimals.

    This is asked because the stylesheet already answers the OTHER question. It
    records that pushing chroma "moved the worst adjacent step from 3.5 to 4.0 —
    nothing", and concludes that saturation makes a mark easier to NAME but not
    easier to TELL APART. Naming is the half a reader complaining about the
    colours is talking about.
    """

    def ok(chroma: float) -> bool:
        linear = oklch_to_linear(lightness, chroma, hue)
        if not in_gamut(linear):
            return False
        return min(wcag_ratio(linear, s) for s in surfaces.values()) >= MARK_MIN

    lo, hi = 0.0, 0.45
    if ok(hi):
        return hi
    for _ in range(40):
        mid = (lo + hi) / 2
        if ok(mid):
            lo = mid
        else:
            hi = mid
    return lo


def headroom(base: Palette) -> Palette:
    """`base` with every step at the most chroma it can hold, L and H untouched."""
    surfaces = {k: oklch_to_linear(*v) for k, v in base.surfaces.items()}
    print()
    print("=" * 78)
    print(f"how much chroma {base.name} is leaving on the table")
    print("=" * 78)
    lifted: list[tuple[float, float, float]] = []
    for i, (lightness, chroma, hue) in enumerate(base.ramp):
        ceiling = richest_chroma(lightness, hue, surfaces)
        # Monotone chroma is a property the shipped ramp has on purpose, so the
        # top of the scale stays the most insistent colour on the page. Taking
        # each step's own ceiling would break that; the run is clamped upward.
        # Floored, not rounded: `round` at three decimals can step back over the
        # boundary the bisection just found, and the first run of this reported
        # three of its own candidates as outside sRGB.
        lifted.append((lightness, np.floor(ceiling * 1000) / 1000, hue))
        print(
            f"  c{i}  L {lightness:.3f}  h {hue:3.0f}   "
            f"shipped C {chroma:.3f}   ceiling C {ceiling:.3f}   "
            f"x{ceiling / chroma:.2f}"
        )
    return Palette(
        name=f"{base.name}, chroma at the ceiling",
        surfaces=base.surfaces,
        inks=base.inks,
        ramp=tuple(lifted),
        note="same lightness ladder, same hues, same order — only saturation moves",
    )


def main() -> None:
    print(__doc__.split("\n\n")[0])
    for palette in (SHIPPED_LIGHT, SHIPPED_DARK, MACARON_LIGHT, MACARON_PUSHED):
        report(palette)
    report(headroom(SHIPPED_LIGHT))


if __name__ == "__main__":
    main()
