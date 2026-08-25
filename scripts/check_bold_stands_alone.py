"""Bold in running prose is a skim path, and it has to lead somewhere.

Three non-expert readers read the site on 2026-08-24 — the reading `PLAN.md`
Phase 8 had deferred for want of participants — and all three named chapter 3 as
where they disengaged. The third described what they did next rather than
leaving: 「後面的 Moran's I、LISA、Voronoi 分割、silhouette、ARI…我主要看粗體結論」.

Falling back to the bold is a reasonable thing for a reader to do, and the site
made it useless. Chapter 3 had sixteen bold passages and ten were bare terms —
殘差, 虛無分布, 合併式, silhouette, k = 2, 99.5 百分位. Read on its own that is a
list of nouns, which is exactly what the same reader reported: 「有些名詞我知道
大概在做什麼，但當下並沒有真正跟完推理」. The conclusion the chapter was built to
deliver sits at 53% of its length and was on that path all along, buried.

So the rule is not about how much bold there is. **A bold passage must say
something true and useful read on its own.** 「殘差」 does not. 「沒有任何單站足以
擔負「熱點」之名」 does. Applying it took the site from thirty-eight short
in-paragraph bolds to ten, and every one of those ten is a claim.

Scope is three exclusions, each of which is a different rendering rather than a
pardon — a chart's guide labels are data, a card title is a heading, and the
no-JavaScript fallback is a second version of the page. Counting the first of
those as prose is not hypothetical: it put chapter 1 at 74% bare terms in an
earlier report when the true figure is 54%, and sent a plan at the wrong two
chapters. `conf/emphasis.yaml` carries the detail.

**What this cannot do**, stated plainly because the gate would otherwise be
mistaken for more than it is: length is a proxy for substance and a long bare
noun phrase defeats it. 「每月殘差的平均莫蘭指數」 passes on twelve characters and
says nothing. `docs/working-rules.md` already draws this line around the
site-prose gate's two delegated claims — a regex can require that emphasis be
substantial, and only a reader can say whether it carries a claim.

    python scripts/check_bold_stands_alone.py [web/dist]
"""

from __future__ import annotations

import pathlib
import sys
from dataclasses import dataclass
from html.parser import HTMLParser

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
CONFIG = ROOT / "conf" / "emphasis.yaml"
DEFAULT = ROOT / "web" / "dist"

ROUTES = (
    "/",
    "/trend/",
    "/stations/",
    "/space/",
    "/sources/",
    "/detection/",
    "/forecast/",
    "/health/",
    "/methods/",
    "/explore/",
    "/data/",
)

# A second rendering of the same page rather than part of this one. The station
# name inside it is bold and is not an exception: a reader running JavaScript
# never reaches it.
SKIP_CLASSES = ("cbpf-nojs",)


def shown(path: pathlib.Path) -> str:
    """A path as a reader of the output can use it, repo-relative where it can be."""
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


class ProseEmphasis(HTMLParser):
    """Every `<strong>` in running prose, with the figure furniture left out.

    "Running prose" is inside `<main>`, inside a `<p>`, outside any `<figure>`,
    and outside a fallback block. A `<strong>` that is a list item's title —
    `<li><a><strong>基準優勢</strong><p>…</p></a></li>` — is outside a `<p>` by
    construction, which is how a card heading tells itself apart from emphasis
    without anyone having to label it.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._main = 0
        self._figure = 0
        self._paragraph = 0
        self._skipped = 0
        self._strong = 0
        self._in_paragraph = False
        self._buffer: list[str] = []
        self.passages: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = (dict(attrs).get("class") or "").split()
        if self._skipped or any(name in classes for name in SKIP_CLASSES):
            self._skipped += 1
        if tag == "main":
            self._main += 1
        if tag == "figure":
            self._figure += 1
        if tag == "p":
            self._paragraph += 1
        if tag == "strong" and self._main and not self._figure and not self._skipped:
            self._strong += 1
            self._buffer = []
            self._in_paragraph = self._paragraph > 0

    def handle_endtag(self, tag: str) -> None:
        if tag == "strong" and self._strong:
            self._strong -= 1
            text = " ".join("".join(self._buffer).split())
            if text and self._in_paragraph:
                self.passages.append(text)
        if tag == "p" and self._paragraph:
            self._paragraph -= 1
        if tag == "figure" and self._figure:
            self._figure -= 1
        if tag == "main" and self._main:
            self._main -= 1
        if self._skipped:
            self._skipped -= 1

    def handle_data(self, data: str) -> None:
        if self._strong:
            self._buffer.append(data)


def emphasis_in(html: str) -> list[str]:
    parser = ProseEmphasis()
    parser.feed(html)
    return parser.passages


def page_for(dist: pathlib.Path, route: str) -> pathlib.Path:
    slug = route.strip("/")
    return dist / "index.html" if not slug else dist / slug / "index.html"


@dataclass(frozen=True)
class Allowed:
    route: str
    text: str
    why: str


def load_config(path: pathlib.Path | None = None) -> tuple[int, list[Allowed]]:
    """The threshold and the reviewed short passages, or `ValueError`.

    `path` defaults to `None` so the module attribute is read at call time; a
    default bound at import cannot be redirected, which cost three tests a false
    pass earlier today.
    """
    path = CONFIG if path is None else path
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError(f"{path.name} is not a mapping")

    minimum = document.get("min_chars")
    if not isinstance(minimum, int) or minimum <= 0:
        raise ValueError(f"{path.name}: min_chars must be a positive integer, got {minimum!r}")

    entries = document.get("allow")
    if not isinstance(entries, list):
        raise ValueError(f"{path.name}: allow must be a list")

    known = {"route", "text", "why"}
    allowed: list[Allowed] = []
    for index, entry in enumerate(entries):
        where = f"{path.name}: allow[{index}]"
        if not isinstance(entry, dict):
            raise ValueError(f"{where} is not a mapping")
        unknown = sorted(set(entry) - known)
        if unknown:
            raise ValueError(f"{where} has unknown key(s): {', '.join(unknown)}")
        route, text, why = entry.get("route"), entry.get("text"), entry.get("why")
        if not isinstance(route, str) or not route.startswith("/"):
            raise ValueError(f"{where} has no route beginning with /")
        if not isinstance(text, str) or not text:
            raise ValueError(f"{where} has no text")
        # A short passage is admitted one at a time and on a stated ground. An
        # entry without one is the beginning of a list that grows by habit.
        if not isinstance(why, str) or not why:
            raise ValueError(f"{where} ({text}) must say why it stands alone")
        if len(text) >= minimum:
            raise ValueError(
                f"{where} ({text}) is {len(text)} characters and needs no exception "
                f"at min_chars {minimum} — delete the entry rather than carrying it"
            )
        allowed.append(Allowed(route=route, text=text, why=why))
    return minimum, allowed


def problems_for(route: str, passages: list[str], minimum: int, allowed: set[str]) -> list[str]:
    """Short emphasis on one page that nobody has vouched for."""
    problems = []
    for text in passages:
        if len(text) >= minimum or text in allowed:
            continue
        problems.append(
            f"{route:<12}「{text}」 is {len(text)} characters — emphasis a reader "
            "skimming the bold cannot use. Say the claim, or drop the bold, or "
            "add it to conf/emphasis.yaml with a reason."
        )
    return problems


def main(argv: list[str]) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    dist = pathlib.Path(argv[1]) if len(argv) > 1 else DEFAULT
    if not dist.exists():
        print(f"{dist} not found — run `npm run build` in web/ first", file=sys.stderr)
        return 1

    try:
        minimum, allowed = load_config()
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"emphasis config is unusable: {exc}", file=sys.stderr)
        return 1

    problems: list[str] = []
    total = 0
    short = 0
    seen: set[tuple[str, str]] = set()
    for route in ROUTES:
        page = page_for(dist, route)
        if not page.exists():
            problems.append(f"{route}: no built page at {shown(page)}")
            continue
        passages = emphasis_in(page.read_text(encoding="utf-8"))
        total += len(passages)
        allowed_here = {a.text for a in allowed if a.route == route}
        short += sum(1 for p in passages if len(p) < minimum)
        seen.update((route, p) for p in passages)
        problems.extend(problems_for(route, passages, minimum, allowed_here))

    # An exception for a passage that is no longer on the page describes a page
    # that no longer exists as written, and would pass forever while checking
    # nothing.
    for entry in allowed:
        if (entry.route, entry.text) not in seen:
            problems.append(
                f"{entry.route:<12}「{entry.text}」 is allowed but no longer appears — stale entry"
            )

    # 「all reviewed」 next to a non-zero problem count would be a line that is
    # false exactly when it matters, which is the failure mode this repository
    # spends most of its gates on.
    reviewed = short - len(problems)
    print(f"prose emphasis   : {total} passage(s) across {len(ROUTES)} routes")
    print(f"under {minimum} characters: {short} ({reviewed} reviewed in conf/emphasis.yaml)")
    print(f"problems         : {len(problems)}")
    for problem in problems:
        print(f"  {problem}")
    return 1 if problems else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv))
