"""Check that every built chapter has the shared publication opening.

The chapter registry owns the route set.  This gate reads that set from the
TypeScript literal, then checks the HTML readers actually receive rather than
the Astro source that produced it.

    python scripts/check_publication_structure.py [web/dist]
"""

from __future__ import annotations

import pathlib
import re
import sys
from collections import Counter
from html.parser import HTMLParser

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT = ROOT / "web" / "dist"
REGISTRY = ROOT / "web" / "src" / "lib" / "chapters.ts"
EXPECTED_CHAPTERS = 10
REQUIRED_CLASSES = ("chapter-intro", "chapter-question", "chapter-finding")
SLUG = re.compile(r'\bslug:\s*"([^"]+)"')


def chapter_slugs() -> list[str]:
    """Return the registry's chapter slugs in publication order."""
    source = REGISTRY.read_text(encoding="utf-8")
    marker = "export const CHAPTERS"
    if marker not in source:
        raise SystemExit(f"{REGISTRY} — CHAPTERS registry not found")

    body = source[source.index(marker) :]
    end = body.find("] as const")
    if end == -1:
        raise SystemExit(f"{REGISTRY} — CHAPTERS registry has no closing literal")

    slugs = SLUG.findall(body[:end])
    if len(slugs) != EXPECTED_CHAPTERS:
        raise SystemExit(
            f"{REGISTRY} — expected {EXPECTED_CHAPTERS} chapter slugs, found {len(slugs)}"
        )
    if len(set(slugs)) != len(slugs):
        raise SystemExit(f"{REGISTRY} — chapter slugs must be unique")
    return slugs


class StructureParser(HTMLParser):
    """Count opening-contract elements and retain heading text in document order."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.classes: Counter[str] = Counter()
        self.headings: list[tuple[str, list[str]]] = []
        self._heading_stack: list[int] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        for class_name in (attributes.get("class") or "").split():
            if class_name in REQUIRED_CLASSES:
                self.classes[class_name] += 1

        lowered = tag.lower()
        if lowered in {"h1", "h2"}:
            self.headings.append((lowered, []))
            self._heading_stack.append(len(self.headings) - 1)

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if lowered in {"h1", "h2"} and self._heading_stack:
            self._heading_stack.pop()

    def handle_data(self, data: str) -> None:
        for index in self._heading_stack:
            self.headings[index][1].append(data)


def failures_for(page: pathlib.Path) -> list[str]:
    parser = StructureParser()
    parser.feed(page.read_text(encoding="utf-8"))
    failures: list[str] = []

    for class_name in REQUIRED_CLASSES:
        count = parser.classes[class_name]
        if count == 0:
            failures.append(f"missing .{class_name}")
        elif count != 1:
            failures.append(f"expected exactly one .{class_name}, found {count}")

    h1_positions = [index for index, (tag, _) in enumerate(parser.headings) if tag == "h1"]
    if not h1_positions:
        failures.append("missing <h1>")
    elif len(h1_positions) != 1:
        failures.append(f"expected exactly one <h1>, found {len(h1_positions)}")
    else:
        h1_position = h1_positions[0]
        meaningful_h2 = any(
            index > h1_position and tag == "h2" and "".join(text).strip()
            for index, (tag, text) in enumerate(parser.headings)
        )
        if not meaningful_h2:
            failures.append("no meaningful <h2> after <h1>")

    return failures


def main(argv: list[str]) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    dist = pathlib.Path(argv[1]) if len(argv) > 1 else DEFAULT
    if not dist.exists():
        print(f"{dist} not found — run `npm run build` in web/ first", file=sys.stderr)
        return 1

    failed_chapters = 0
    slugs = chapter_slugs()
    for slug in slugs:
        page = dist / slug / "index.html"
        if not page.exists():
            failures = [f"missing {page.relative_to(ROOT).as_posix()}"]
        else:
            failures = failures_for(page)

        if failures:
            failed_chapters += 1
            for failure in failures:
                print(f"{slug}: {failure}")

    print(f"chapters checked: {len(slugs)}")
    print(f"chapters with structure failures: {failed_chapters}")
    return 1 if failed_chapters else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
