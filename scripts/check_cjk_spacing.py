"""Fail if the built page has spaces inside Chinese sentences.

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

    python scripts/check_cjk_spacing.py [web/dist/index.html]
"""

from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT = ROOT / "web" / "dist" / "index.html"

# Han, kana, CJK punctuation and fullwidth forms. U+3000 (ideographic space) is
# excluded on purpose: it is whitespace, not a character to close up against.
CJK = (
    r"[⺀-⿿、-〿ぁ-㏿㐀-䶿"
    r"一-鿿豈-﫿︰-﹏！-｠￠-￦]"
)
OPAQUE = re.compile(r"<(script|style|pre|code|textarea)\b[^>]*>.*?</\1>", re.S | re.I)
TAG = re.compile(r"<[^>]+>")
GAP = re.compile(CJK + r"[ \t\r\n]+" + CJK)

# Spacing that is deliberate and has to survive. Chinese/Latin and Chinese/digit
# boundaries read better with a space, and the transform must not touch them.
MUST_KEEP = ("第 1 版", "8 年月平均", "3.41 億", "165 公尺", "N = 7,286")


def main(argv: list[str]) -> int:
    path = pathlib.Path(argv[1]) if len(argv) > 1 else DEFAULT
    if not path.exists():
        print(f"{path} not found — run `npm run build` in web/ first", file=sys.stderr)
        return 1

    html = path.read_text(encoding="utf-8")

    # Blank out data and code, then every tag, so whitespace inside an attribute
    # can never be mistaken for whitespace between two characters of prose.
    prose = OPAQUE.sub("\x00", html)
    prose = TAG.sub("\x00", prose)

    gaps = GAP.findall(prose)
    missing = [probe for probe in MUST_KEEP if probe not in prose]

    print(f"page            : {path.relative_to(ROOT)}")
    print(f"gaps in Chinese : {len(gaps)}")
    for gap in gaps[:10]:
        print(f"  {gap!r}")
    print(f"spacing kept    : {len(MUST_KEEP) - len(missing)}/{len(MUST_KEEP)}")
    for probe in missing:
        print(f"  lost {probe!r}")

    return 1 if (gaps or missing) else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
