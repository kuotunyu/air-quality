"""A term the site promises to explain must still explain it, where it is met.

Six prose gates come before this one and all six ask the same question: does a
number retyped into prose still equal the number that produced it. None of them
asks whether the sentence around the number can be read. That was deliberate for
a while — `PLAN.md` Phase 8 keeps a non-expert reading deferred for want of a
participant, and an automated gate cannot stand in for user research. It still
cannot. But between "a proofread by a stranger" and "nothing" there is one
question a machine can answer exactly: **is the explanation still there, next to
the word it explains.**

That question had teeth because the site already answered it well in most places
and the failures clustered in one shape. Calibration at `ab66b34` put the
explained terms between 5 and 70 characters of first use and the nearest gap at
366, with nothing in between. The run prints each distance, so the current values
are always in the output rather than here.

Every failure was a **term introduced by an index, a legend or a chapter's own
question list, ahead of the prose that defines it** — a structural consequence of
a chapter growing a navigational layer above its argument, and exactly the class
of defect a proofread misses because each screen looks fine on its own.
`climatology` and `persistence` were named by the same legend of the same figure
and the caption glossed one of them, so one passed and one did not.

All eight are explained now, so the gate holds rather than accuses. That is the
ordinary state of a gate here and it is not the same as having nothing to say:
the run reports where every explanation sits, and the first edit that moves one
out of reach of its term will fail before a reader meets it.

The distances used to be listed here, per term. Seven of thirteen were wrong the
day they were written, because they had been measured with a draft of the
extractor rather than this one, and a comment cannot be checked by anything. That
is why `distance_for` exists and why this paragraph names two numbers instead of
thirteen.

Nothing here judges whether an explanation is good. It checks that the string is
present and close. `docs/working-rules.md` already draws that line around the two
delegated claims in the site-prose gate: a regex can guarantee a caveat is
present and can never guarantee it says the right thing.

The gate reads `web/dist`, so it runs in the `web` job after `npm run build`,
beside `check_publication_structure.py` and `check_cjk_spacing.py`, which read
the built output for the same reason: what a reader meets is the rendered page,
not the `.astro` source that produced it.

    python scripts/check_term_first_use.py [web/dist]
"""

from __future__ import annotations

import pathlib
import sys
from dataclasses import dataclass
from html.parser import HTMLParser

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
GLOSSARY = ROOT / "conf" / "glossary.yaml"
DEFAULT = ROOT / "web" / "dist"

# `pre`, `code` and `textarea` are opaque for the same reason
# `check_cjk_spacing.py` treats them so: chapter 9's SQL examples are code a
# reader is shown, not prose a reader is expected to follow, and `mean`,
# `station_name` and `NULL` are column names rather than vocabulary.
OPAQUE = frozenset({"script", "style", "pre", "code", "textarea", "svg", "template"})

# Everything not listed here separates the text around it. Without that, adjacent
# table cells concatenate — chapter 10's file table produced `JSON0.36` and
# `MBParquet2.37`, and a term glued to a number is a term this gate cannot find.
INLINE = frozenset(
    {
        "a",
        "abbr",
        "b",
        "bdi",
        "bdo",
        "br",
        "cite",
        "data",
        "del",
        "em",
        "i",
        "img",
        "ins",
        "kbd",
        "mark",
        "output",
        "picture",
        "q",
        "rp",
        "rt",
        "ruby",
        "s",
        "samp",
        "small",
        "source",
        "span",
        "strong",
        "sub",
        "sup",
        "time",
        "u",
        "var",
        "wbr",
    }
)


class MainText(HTMLParser):
    """The rendered text of `<main>`, in the order a reader meets it.

    `convert_charrefs` matters more than it looks: the page carries `&lt;` and
    `&gt;` in prose, and reading them literally turns 「&lt; 20 小時」 into a token
    that matches nothing a person ever sees.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._depth = 0
        self._opaque = 0
        self._chunks: list[str] = []

    def _boundary(self, tag: str) -> None:
        if self._depth and not self._opaque and tag not in INLINE:
            self._chunks.append(" ")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "main":
            self._depth += 1
        if tag in OPAQUE:
            self._opaque += 1
        self._boundary(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._boundary(tag)

    def handle_endtag(self, tag: str) -> None:
        self._boundary(tag)
        if tag in OPAQUE and self._opaque:
            self._opaque -= 1
        if tag == "main" and self._depth:
            self._depth -= 1

    def handle_data(self, data: str) -> None:
        if self._depth and not self._opaque:
            self._chunks.append(data)

    @property
    def text(self) -> str:
        return " ".join("".join(self._chunks).split())


def rendered(html: str) -> str:
    """The `<main>` text of one built page."""
    parser = MainText()
    parser.feed(html)
    return parser.text


def page_for(dist: pathlib.Path, route: str) -> pathlib.Path:
    """The built file behind a route, `/trend/` being `trend/index.html`."""
    slug = route.strip("/")
    return dist / "index.html" if not slug else dist / slug / "index.html"


@dataclass(frozen=True)
class Term:
    """One entry of `conf/glossary.yaml`, already validated in shape."""

    term: str
    route: str
    explains: str | None
    why: str | None

    @property
    def label(self) -> str:
        return f"{self.route} {self.term}"


def shown(path: pathlib.Path) -> str:
    """A path as a reader of the output can use it.

    Repo-relative when it is inside the repo, which is how CI runs it, and
    absolute when it is not — a build copied elsewhere, or a test's `tmp_path`.
    `relative_to` raises rather than falling back, and a gate that crashes while
    reporting a problem has lost the problem.
    """
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def load_glossary(path: pathlib.Path | None = None) -> tuple[int, list[Term]]:
    """The window and the terms, or `ValueError` naming what is malformed.

    Shape is checked here rather than trusted, because a mistyped key is the one
    way this gate could report success while checking nothing — `explains` read
    as absent instead of as a string would silently demote a real check to a
    listed gap.

    `path` defaults to `None` rather than to `GLOSSARY` so the module attribute
    is read when the function runs. A default bound at import cannot be
    redirected, and the end-to-end tests below were reading the real glossary
    while believing they had replaced it.
    """
    path = GLOSSARY if path is None else path
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError(f"{path.name} is not a mapping")

    window = document.get("window")
    if not isinstance(window, int) or window <= 0:
        raise ValueError(f"{path.name}: window must be a positive integer, got {window!r}")

    entries = document.get("terms")
    if not isinstance(entries, list) or not entries:
        raise ValueError(f"{path.name}: terms must be a non-empty list")

    known = {"term", "route", "explains", "why"}
    terms: list[Term] = []
    for index, entry in enumerate(entries):
        where = f"{path.name}: terms[{index}]"
        if not isinstance(entry, dict):
            raise ValueError(f"{where} is not a mapping")
        unknown = sorted(set(entry) - known)
        if unknown:
            raise ValueError(f"{where} has unknown key(s): {', '.join(unknown)}")
        term, route, explains, why = (
            entry.get("term"),
            entry.get("route"),
            entry.get("explains"),
            entry.get("why"),
        )
        if not isinstance(term, str) or not term:
            raise ValueError(f"{where} has no term")
        if not isinstance(route, str) or not route.startswith("/"):
            raise ValueError(f"{where} ({term}) has no route beginning with /")
        if explains is not None and (not isinstance(explains, str) or not explains):
            raise ValueError(f"{where} ({term}): explains must be a non-empty string or null")
        if explains is None and not (isinstance(why, str) and why):
            raise ValueError(f"{where} ({term}): explains is null, so why must say what is known")
        if explains is not None and why is not None:
            raise ValueError(f"{where} ({term}): why belongs to an unexplained term only")
        terms.append(Term(term=term, route=route, explains=explains, why=why))

    duplicates = sorted(
        {t.label for t in terms if sum(1 for other in terms if other.label == t.label) > 1}
    )
    if duplicates:
        raise ValueError(f"{path.name}: one entry per term per route; repeated {duplicates}")
    return window, terms


def distance_for(term: Term, text: str) -> int | None:
    """Signed characters from the term's first use to its explanation.

    Negative when the explanation comes first, which several do. `None` when
    there is nothing to measure — no explanation recorded, or either string
    absent, both of which `problems_for` reports on its own.

    This exists so the numbers live in the output. The first version of this
    gate printed only pass or fail and wrote the distances into a comment, where
    seven of thirteen were wrong on the day they were committed: they had been
    measured with a draft of the extractor rather than the one that ships. A
    figure that is recomputed every run cannot drift from what it describes.
    """
    if term.explains is None:
        return None
    use, where = text.find(term.term), text.find(term.explains)
    return None if use < 0 or where < 0 else where - use


def problems_for(term: Term, text: str, window: int) -> list[str]:
    """What is wrong with one term on the page it belongs to, if anything."""
    use = text.find(term.term)
    if use < 0:
        # A term that has left its route is not a pass. The entry is describing a
        # page that no longer exists as written, and every later run would agree
        # with it while checking nothing.
        return [f"{term.label}: not used on this route — the entry is stale"]

    if term.explains is None:
        return []

    where = text.find(term.explains)
    if where < 0:
        return [
            f"{term.label}: the explanation {term.explains!r} is gone. "
            "If it was reworded rather than removed, update conf/glossary.yaml "
            "in the same commit."
        ]

    distance = where - use
    if abs(distance) > window:
        side = "after" if distance > 0 else "before"
        return [
            f"{term.label}: first used at {use}, explained {abs(distance)} characters "
            f"{side} that — further than the {window} the site's own habit allows."
        ]
    return []


def main(argv: list[str]) -> int:
    # The terms and the reasons beside them are Chinese, and this repository is
    # developed on Windows, where the console defaults to cp950 and turns the
    # report into mojibake — or, once the reasons grew past the Big5 repertoire,
    # into a UnicodeEncodeError that took the exit code with it.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    dist = pathlib.Path(argv[1]) if len(argv) > 1 else DEFAULT
    if not dist.exists():
        print(f"{dist} not found — run `npm run build` in web/ first", file=sys.stderr)
        return 1

    try:
        window, terms = load_glossary()
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"glossary is unusable: {exc}", file=sys.stderr)
        return 1

    pages: dict[str, str] = {}
    problems: list[str] = []
    for route in sorted({term.route for term in terms}):
        page = page_for(dist, route)
        if not page.exists():
            problems.append(f"{route}: no built page at {shown(page)}")
            continue
        pages[route] = rendered(page.read_text(encoding="utf-8"))

    explained = [t for t in terms if t.explains is not None]
    unexplained = [t for t in terms if t.explains is None]
    for term in terms:
        if term.route in pages:
            problems.extend(problems_for(term, pages[term.route], window))

    print(f"routes read      : {len(pages)}")
    print(f"terms explained  : {len(explained)} (within {window} characters of first use)")
    for term in explained:
        gap = distance_for(term, pages[term.route]) if term.route in pages else None
        if gap is not None:
            side = "後" if gap >= 0 else "前"
            print(f"  {term.label} — 解釋在首次出現{side} {abs(gap)} 字")
    print(f"terms unexplained: {len(unexplained)}")
    for term in unexplained:
        print(f"  待補 {term.label} — {term.why}")
    print(f"problems         : {len(problems)}")
    for problem in problems:
        print(f"  {problem}")
    return 1 if problems else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv))
