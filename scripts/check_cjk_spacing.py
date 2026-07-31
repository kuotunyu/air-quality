"""Fail if a built page spaces Chinese wrongly — in either direction.

Two opposite failures, both caused by the same thing and both invisible to every
other gate. This file began as a check for the first only, and shipped nineteen
instances of the second while reporting a clean run.

A newline in an `.astro` source file is whitespace in the HTML it produces. In
English that is invisible, because words are separated by spaces anyway. Chinese
has no inter-word spaces, so **every place a source line happened to wrap turns
into a visible gap in the middle of a sentence** — and the gaps land wherever
the author hit the column limit, which is why the page read as though the
alignment changed from paragraph to paragraph. There were 248 of them.

`web/src/middleware.ts` closes them at render time. This checks the result,
because the failure is invisible to every other gate: the markup is valid, the
types check, the tests pass, and the page looks fine to anyone not reading the
Chinese.

Only whitespace **between two CJK characters** counts. A space between Chinese
and Latin or a digit is correct typography and must survive — 「第 1 版」,
「8 年月平均」, 「N = 3.40 億」 are all expected to still be there, and this
script asserts that too, so a transform that fixed the gaps by deleting every
space would fail here rather than pass.

The default target is the whole `dist` tree, not one file. It used to be
`dist/index.html`, which was the entire site; the document is now eleven routes,
and a check pinned to the entry page would report the whole thing clean on the
strength of one eleventh of it.

    python scripts/check_cjk_spacing.py [web/dist | web/dist/some/page.html]
"""

from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT = ROOT / "web" / "dist"

# Han, kana, CJK punctuation and fullwidth forms. U+3000 (ideographic space) is
# excluded on purpose: it is whitespace, not a character to close up against.
CJK = (
    r"[⺀-⿿、-〿ぁ-㏿㐀-䶿"
    r"一-鿿豈-﫿︰-﹏！-｠￠-￦]"
)
OPAQUE = re.compile(r"<(script|style|pre|code|textarea)\b[^>]*>.*?</\1>", re.S | re.I)
TAG = re.compile(r"<[^>]+>")
GAP = re.compile(CJK + r"[ \t\r\n]+" + CJK)

# ── the opposite failure ─────────────────────────────────────────────────────
#
# `GAP` catches a space wrongly INSERTED into Chinese. This catches one wrongly
# LOST: a Han character welded straight onto a digit, as in 「降到13.4」.
#
# Nineteen of those shipped while this gate reported zero, because it had only
# ever been asked about the first failure. The cause is uniform, and it is a build
# artefact rather than a typo: a line break immediately before a `{expression}` in
# an Astro template is collapsed away, so prose that reads correctly in the source
# renders welded. Three places in the codebase already worked around it with an
# explicit `{" "}`; thirteen did not.
#
# Only Han-then-digit is flagged, and the direction matters. Digit-then-Han is
# usually correct — 「6時」, 「6.9千筆」 and 「180°」 all want no space — so flagging
# both directions would report the axis labels of chapter 8 as defects.
WELDED = re.compile(r"[⺀-⿕一-鿿]" + r"[0-9]")

NUL = "\x00"

# Spacing that is deliberate and has to survive. Chinese/Latin and Chinese/digit
# boundaries read better with a space, and the transform must not touch them.
# Each probe now names the page that carries it: with the chapters split across
# routes, "still somewhere in the site" is a weaker claim than "still on the page
# it belongs to".
MUST_KEEP = (
    ("第 1 版", "index.html"),
    ("8 年月平均", "index.html"),
    ("3.40 億", "index.html"),
    ("165 公尺", "index.html"),
    ("N = 7,286", "methods/index.html"),
    ("N = 7,286", "index.html"),
)


def prose_of(html: str) -> str:
    """Everything a reader sees, with data, code and markup blanked out.

    Blanking the tags matters as much as blanking the data: whitespace inside an
    attribute would otherwise be read as whitespace between two characters of
    prose.
    """
    return TAG.sub(NUL, OPAQUE.sub(NUL, html))


def main(argv: list[str]) -> int:
    target = pathlib.Path(argv[1]) if len(argv) > 1 else DEFAULT
    if not target.exists():
        print(f"{target} not found — run `npm run build` in web/ first", file=sys.stderr)
        return 1

    pages = sorted(target.rglob("*.html")) if target.is_dir() else [target]
    if not pages:
        print(f"no .html files under {target}", file=sys.stderr)
        return 1

    total = 0
    welded = 0
    for page in pages:
        prose = prose_of(page.read_text(encoding="utf-8"))
        gaps = GAP.findall(prose)
        stuck = WELDED.findall(prose)
        total += len(gaps)
        welded += len(stuck)
        print(f"{len(gaps):>4} gap(s) {len(stuck):>4} welded  {page.relative_to(ROOT).as_posix()}")
        for gap in gaps[:6]:
            print(f"           gap    {gap!r}")
        for one in stuck[:6]:
            print(f"           welded {one!r}")

    missing = []
    for probe, where in MUST_KEEP:
        page = (target / where) if target.is_dir() else target
        if not page.exists() or probe not in prose_of(page.read_text(encoding="utf-8")):
            missing.append((probe, where))

    print(f"\npages checked   : {len(pages)}")
    print(f"gaps in Chinese : {total}")
    print(f"welded to digits: {welded}")
    print(f"spacing kept    : {len(MUST_KEEP) - len(missing)}/{len(MUST_KEEP)}")
    for probe, where in missing:
        print(f"  lost {probe!r} (expected on {where})")

    return 1 if (total or missing or welded) else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
