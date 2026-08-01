"""
Draw the social card the site links to as `og:image`.

A link to this document posted in a chat app or a group used to unfurl as a bare
line of text, because there was no image to unfurl. This draws one.

Every number on the card is READ FROM THE DATA the site itself reads, never typed
in. A card is the most-seen and least-checked surface a project has: it is what
people look at when they decide whether to open the thing, and it is the last
place anyone thinks to re-check after a pipeline run. A hardcoded figure there
would be correct on the day it was drawn and quietly wrong afterwards.

Run after a data refresh:

    uv run python scripts/make_social_card.py

Output: web/public/og.png, 1200x630, committed alongside the data it describes.
"""

from __future__ import annotations

import json
import pathlib
import sys

from PIL import Image, ImageDraw, ImageFont, PngImagePlugin

ROOT = pathlib.Path(__file__).resolve().parents[1]
DATA = ROOT / "web" / "public" / "data"
OUT = ROOT / "web" / "public" / "og.png"

WIDTH, HEIGHT = 1200, 630

# The site's own light-theme tokens, resolved. Written as hex here because Pillow
# has no OKLCH; the source of truth is web/src/styles/global.css and these are
# what those tokens measured as in the browser.
INK = (31, 34, 38)  # --text        oklch(0.252 0.008 250)
MUTED = (69, 73, 78)  # --text-muted  oklch(0.404 0.010 250)
FAINT = (93, 98, 103)  # --text-faint  oklch(0.493 0.010 250)
FIELD = (243, 245, 248)  # --bg          oklch(0.970 0.004 250)
RULE = (132, 137, 142)  # --rule        oklch(0.628 0.010 250)
# The concentration ramp, low to high. The one mark that is unmistakably this
# document, and the only saturated colour it allows itself.
RAMP = ["#5d88b8", "#238690", "#4a7647", "#755716", "#783c2e", "#6b2b37", "#5e0d4e"]

CJK_BOLD = "C:/Windows/Fonts/msjhbd.ttc"
CJK = "C:/Windows/Fonts/msjh.ttc"
LATIN = "C:/Windows/Fonts/segoeui.ttf"


def font(path: str, size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(path, size)
    except OSError as exc:  # pragma: no cover - environment dependent
        raise SystemExit(f"missing font {path}: {exc}") from exc


def facts() -> dict[str, object]:
    """Everything the card says, taken from the published data."""
    meta = json.loads((DATA / "meta.json").read_text(encoding="utf-8"))
    trend = json.loads((DATA / "story" / "trend-national.json").read_text(encoding="utf-8"))

    years = trend["years"]
    series = trend["all_stations"]
    # The decline is quoted from the balanced window, exactly as the hero does:
    # before that the network was a handful of experimental sites and a percentage
    # anchored there would be a statement about those few places.
    since = trend["panel"]["balanced_since"]
    start = years.index(since)
    first, last = series[start], series[-1]

    # The RECORD span — every measurand, from the station register — which is a
    # different quantity from `years[0]`. The trend series begins in 1998
    # because that is when PM2.5 does; the archive begins in 1982. The card
    # wants the archive, and reaching for `years[0]` to remove a hardcoded
    # string would have replaced a right number with a wrong one.
    first_years = [s["first_year"] for s in meta["stations"] if s.get("first_year")]
    last_years = [s["last_year"] for s in meta["stations"] if s.get("last_year")]

    return {
        "record_first": min(first_years),
        "record_last": max(last_years),
        "first_year": years[0],
        "last_year": years[-1],
        "stations": len(meta["stations"]),
        "pollutants": len(meta["pollutants"]),
        "since": since,
        "from_value": first,
        "to_value": last,
        "drop_pct": (first - last) / first * 100.0,
    }


def draw() -> None:
    f = facts()
    img = Image.new("RGB", (WIDTH, HEIGHT), FIELD)
    d = ImageDraw.Draw(img)

    pad = 84

    # The ramp as a rule down the left edge: the document's signature, and the
    # only thing on the card carrying colour.
    band_w, band_top, band_h = 12, pad, HEIGHT - 2 * pad
    step = band_h / len(RAMP)
    for i, colour in enumerate(RAMP):
        d.rectangle(
            [pad, band_top + i * step, pad + band_w, band_top + (i + 1) * step],
            fill=colour,
        )

    x = pad + band_w + 46
    y = pad - 6

    # Not a literal. This file's docstring promises that every number on the
    # card is read from the data, and this line was the exception — 「1982 – 2025」
    # typed in, on the one surface nobody re-checks after a pipeline run. It
    # happened to be right, which is the only reason it survived.
    d.text((x, y), f"{f['record_first']} – {f['record_last']}", font=font(LATIN, 30), fill=FAINT)
    y += 54

    title = font(CJK_BOLD, 76)
    d.text((x, y), "台灣空氣品質的", font=title, fill=INK)
    y += 92
    d.text((x, y), "長期重新分析", font=title, fill=INK)
    y += 116

    d.line([x, y, WIDTH - pad, y], fill=RULE, width=1)
    y += 40

    lede = font(CJK, 32)
    num = font(LATIN, 34)
    line = (
        f"{f['since']} 年以來，全台 PM2.5 年均值從 {f['from_value']:.1f} "
        f"降到 {f['to_value']:.1f} μg/m³"
    )
    d.text((x, y), line, font=lede, fill=MUTED)
    y += 52
    d.text(
        (x, y),
        f"{f['stations']} 個測站 · {f['pollutants']} 個測項 · 逐時觀測重建",
        font=lede,
        fill=MUTED,
    )

    # Bottom line, in the figure face, because these are figures.
    d.text(
        (x, HEIGHT - pad - 34),
        "kuotunyu.github.io/air-quality",
        font=num,
        fill=FAINT,
    )

    OUT.parent.mkdir(parents=True, exist_ok=True)
    # The facts travel inside the PNG, so `--check` can ask whether the card
    # still describes the data the site ships without re-rendering it. og.png is
    # committed, and it was the only committed artefact with nothing watching
    # whether it had gone stale.
    info = PngImagePlugin.PngInfo()
    info.add_text("twair-facts", json.dumps(f, ensure_ascii=False, sort_keys=True))
    img.save(OUT, "PNG", optimize=True, pnginfo=info)
    size_kb = OUT.stat().st_size / 1024
    print(f"wrote {OUT.relative_to(ROOT)}  {WIDTH}x{HEIGHT}  {size_kb:.0f} KB")
    print(
        f"  facts: {f['since']}–{f['last_year']}, "
        f"{f['from_value']:.1f} -> {f['to_value']:.1f} ug/m3 "
        f"({f['drop_pct']:.0f}% down), {f['stations']} stations"
    )


def check() -> int:
    """Is the committed card still describing the data the site ships?

    Compares the facts embedded when it was drawn against the facts recomputed
    from `web/public/data` now. Re-rendering and comparing bytes would not work:
    PNG output is not reproducible across Pillow versions or font builds, and a
    check that fails for that reason is a check that gets deleted.
    """
    if not OUT.exists():
        print(f"{OUT.relative_to(ROOT)} missing — run this script without --check")
        return 1
    with Image.open(OUT) as img:
        recorded = img.text.get("twair-facts")
    if not recorded:
        print(f"{OUT.relative_to(ROOT)} carries no facts — redraw it")
        return 1
    was = json.loads(recorded)
    now = facts()
    drifted = {k: (was.get(k), now[k]) for k in now if was.get(k) != now[k]}
    if drifted:
        print(f"{OUT.relative_to(ROOT)} describes an older export:")
        for k, (a, b) in drifted.items():
            print(f"  {k}: card says {a!r}, data says {b!r}")
        print("re-run `uv run python scripts/make_social_card.py`")
        return 1
    print(f"{OUT.relative_to(ROOT)} matches the published data ({len(now)} facts)")
    return 0


if __name__ == "__main__":
    # `sys.exit(draw())` read as "exit with draw's status", and `draw()` returns
    # None — so the exit code was 0 whatever happened inside. It always was 0;
    # what was wrong was the sentence, not the behaviour.
    if "--check" in sys.argv:
        raise SystemExit(check())
    draw()
