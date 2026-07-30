"""Fail if a built page has spaces inside Chinese sentences.

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
「8 年月平均」, 「N = 3.41 億」 are all expected to still be there, and this
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
NUL = "\x00"

# Spacing that is deliberate and has to survive. Chinese/Latin and Chinese/digit
# boundaries read better with a space, and the transform must not touch them.
# Each probe now names the page that carries it: with the chapters split across
# routes, "still somewhere in the site" is a weaker claim than "still on the page
# it belongs to".
MUST_KEEP = (
    ("第 1 版", "index.html"),
    ("8 年月平均", "index.html"),
    ("3.41 億", "index.html"),
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
    for page in pages:
        gaps = GAP.findall(prose_of(page.read_text(encoding="utf-8")))
        total += len(gaps)
        print(f"{len(gaps):>4} gap(s)  {page.relative_to(ROOT).as_posix()}")
        for gap in gaps[:6]:
            print(f"           {gap!r}")

    missing = []
    for probe, where in MUST_KEEP:
        page = (target / where) if target.is_dir() else target
        if not page.exists() or probe not in prose_of(page.read_text(encoding="utf-8")):
            missing.append((probe, where))

    print(f"\npages checked   : {len(pages)}")
    print(f"gaps in Chinese : {total}")
    print(f"spacing kept    : {len(MUST_KEEP) - len(missing)}/{len(MUST_KEEP)}")
    for probe, where in missing:
        print(f"  lost {probe!r} (expected on {where})")

    return 1 if (total or missing) else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
