"""Fail if a chapter calls itself something the navigation does not call it.

A reader meets a chapter's name FOUR times before they ever see the page: in the
rail, in the entry page's index, in the browser tab, and in the previous/next
step at the foot of the neighbouring chapter. All four read `title` from
`web/src/lib/chapters.ts`. The fifth is the `<h1>`, and that one is typed into
the chapter's own component — so it is the one that drifts.

This gate exists because the drift has already happened three times:

  ch.5  rail 政策效應的偵測極限 / h1 事件效應的偵測極限 — found by eye, fixed by
        hand, and the reason is still in the comment on that entry. 事件 was the
        correct one: the chapter tests a law amendment, a permit dispute and a
        lockdown, not a government.
  ch.4  registry 污染物的來向 / h1 污染來向與風速條件 — and the registry was
        holding a THIRD name. The rename to 污染來向與風速條件 is recorded in
        `docs/working-rules.md`; the component took it, this file did not.
  ch.7  registry 健康負擔與它的假設 / h1 健康負擔估計的假設敏感度.

Nothing was watching, so a fix by hand on one chapter said nothing about the
other nine. Every other gate is blind to this: the markup is valid, the types
check, the tests pass, the spacing check passes, and both names are correct
Chinese — they are just not the same name.

It also holds the rail's one-line rhythm. The rail label box measures 206px and
the type in it is ~22.9px per Han character, so nine characters is exactly one
line and a tenth wraps. Ten entries that are each one line read as a list; one
two-line entry among them reads as an error. A title that no longer fits should
be a deliberate decision about the rail, not a surprise on the next build.

    python scripts/check_chapter_titles.py [web/dist]
"""

from __future__ import annotations

import pathlib
import re
import sys
from html import unescape

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT = ROOT / "web" / "dist"
REGISTRY = ROOT / "web" / "src" / "lib" / "chapters.ts"

# Nine, and it is measured rather than divided.
#
# The first version of this line computed `206 // 22.9 = 8` from an eyeballed
# advance and reported seven of the ten chapters as overflowing while every one
# of them renders on a single line. So the number now comes from growing the
# real `.rail-t` label one character at a time until it takes a second line:
# 6,7,8,9 → one line, 10 → two. The label box is 205.9px and one Han character
# advances 22.56px, which is exactly the label's font-size, as it should be.
#
# Measured at 1280 / 1366 / 1440 / 1920. The box does not change across them —
# the rail width is a clamp that has already resolved by the 80rem breakpoint.
RAIL_LABEL_PX = 205.9
HAN_ADVANCE_PX = 22.56
RAIL_ONE_LINE = 9

# 2026-08-03 — supersedes the title-width model above for the redesigned rail:
# the visible rail copy now comes from `nav`, whose compact editorial contract
# is expressed directly in Han characters rather than inferred from the former
# full-title box.
RAIL_NAV_HAN_LIMIT = 5

# One object per chapter in the `CHAPTERS` array. Parsed rather than imported
# because this runs under Python and the registry is TypeScript; the array is a
# plain literal and has to stay one, which is itself worth pinning.
ENTRY = re.compile(
    r"\{\s*n:\s*(?P<n>\d+),.*?slug:\s*\"(?P<slug>[^\"]+)\".*?nav:\s*\"(?P<nav>[^\"]+)\".*?title:\s*\"(?P<title>[^\"]+)\"",
    re.S,
)
H1 = re.compile(r"<h1[^>]*>(.*?)</h1>", re.S | re.I)
RAIL_LINK = re.compile(
    r'<a(?P<attrs>[^>]*)>\s*<span class="rail-n"[^>]*>.*?</span>\s*'
    r'<span class="rail-t"[^>]*>(?P<visible>.*?)</span>\s*</a>',
    re.S | re.I,
)
TAG = re.compile(r"<[^>]+>")
HAN = re.compile(r"[⺀-⿕一-鿿]")


def chapters() -> list[tuple[int, str, str, str]]:
    """(n, slug, title) for every chapter, in document order."""
    # 2026-08-03 — the tuple description above predates the compact rail; `nav`
    # now sits between `slug` and `title`, while `title` remains the h1 contract.
    src = REGISTRY.read_text(encoding="utf-8")
    body = src[src.index("export const CHAPTERS") :]
    found = [(int(m["n"]), m["slug"], m["nav"], m["title"]) for m in ENTRY.finditer(body)]
    if not found:
        raise SystemExit(f"{REGISTRY} — could not parse any chapter out of CHAPTERS")
    return found


def h1_of(page: pathlib.Path) -> str | None:
    """The page's own heading, with markup and comments stripped.

    A heading is allowed to carry markup — this compares the text a reader sees,
    not the source.
    """
    found = H1.search(page.read_text(encoding="utf-8"))
    if not found:
        return None
    return " ".join(TAG.sub("", found.group(1)).split())


def rail_link_of(page: pathlib.Path, slug: str) -> tuple[str, str, str | None] | None:
    """(visible text, accessible name, current state) for one built rail link."""
    for found in RAIL_LINK.finditer(page.read_text(encoding="utf-8")):
        attrs = found["attrs"]
        href = re.search(r'\bhref="([^"]*)"', attrs, re.I)
        if href is None or href.group(1).rstrip("/").split("/")[-1] != slug:
            continue
        label = re.search(r'\baria-label="([^"]*)"', attrs, re.I)
        current = re.search(r'\baria-current="([^"]*)"', attrs, re.I)
        return (
            unescape(" ".join(TAG.sub("", found["visible"]).split())),
            unescape(label.group(1)) if label is not None else "",
            current.group(1) if current is not None else None,
        )
    return None


def main(argv: list[str]) -> int:
    # Every line this prints is Chinese, and the repo is developed on Windows,
    # where the console defaults to cp950 and turns the whole report into
    # mojibake — including the two names the reader is being asked to compare.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    dist = pathlib.Path(argv[1]) if len(argv) > 1 else DEFAULT
    if not dist.exists():
        print(f"{dist} not found — run `npm run build` in web/ first", file=sys.stderr)
        return 1

    mismatched: list[str] = []
    missing: list[str] = []
    wrapped: list[str] = []
    rail_mismatched: list[str] = []
    current_mismatched: list[str] = []

    for n, slug, nav, title in chapters():
        page = dist / slug / "index.html"
        if not page.exists():
            missing.append(f"  ch.{n} {slug} — no {page.relative_to(ROOT).as_posix()}")
            continue

        heading = h1_of(page)
        if heading is None:
            missing.append(f"  ch.{n} {slug} — page has no <h1>")
            continue

        mark = "ok"
        if heading != title:
            mark = "MISMATCH"
            mismatched.append(
                f"  ch.{n} {slug}\n"
                f"      registry title : {title}\n"
                f"      rendered <h1>  : {heading}\n"
                f"      A reader meets the registry name in the rail, the index, the\n"
                f"      browser tab and both footer steps, then arrives at the other one."
            )

        rail_link = rail_link_of(page, slug)
        if rail_link is None:
            rail_mismatched.append(f"  ch.{n} {slug} — built rail link not found")
        else:
            visible, accessible, current = rail_link
            if visible != nav or nav not in accessible or title not in accessible:
                rail_mismatched.append(
                    f"  ch.{n} {slug}\n"
                    f"      registry nav  : {nav}\n"
                    f"      registry title: {title}\n"
                    f"      visible rail  : {visible}\n"
                    f"      accessible    : {accessible}\n"
                    f"      The compact label and formal title must both remain in the link name."
                )
            if current != "page":
                current_mismatched.append(
                    f"  ch.{n} {slug} — self link has aria-current={current!r}, expected 'page'"
                )

        han = len(HAN.findall(nav))
        if han > RAIL_NAV_HAN_LIMIT:
            mark = "WRAPS" if mark == "ok" else mark
            wrapped.append(
                f"  ch.{n} {slug} — nav is {han} Han characters; the compact rail "
                f"allows {RAIL_NAV_HAN_LIMIT}"
            )

        print(f"  ch.{n:>2} {slug:<10} {han:>2} 字  {mark:<8} {nav} / {title}")

    print(f"\nchapters checked : {len(chapters())}")
    print(f"title mismatches : {len(mismatched)}")
    print(f"rail mismatches  : {len(rail_mismatched)}")
    print(f"current failures : {len(current_mismatched)}")
    print(f"rail overflows   : {len(wrapped)}")
    print(f"pages missing    : {len(missing)}")

    for block in mismatched:
        print(f"\n{block}")
    for block in rail_mismatched:
        print(f"\n{block}")
    for line in current_mismatched:
        print(f"\n{line}")
    for line in wrapped:
        print(f"\n{line}")
    for line in missing:
        print(f"\n{line}")

    return 1 if (mismatched or rail_mismatched or current_mismatched or wrapped or missing) else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
