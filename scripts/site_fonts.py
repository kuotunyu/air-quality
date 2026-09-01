"""Subset the site's three faces to the characters the built pages use, and check them.

    uv run python scripts/site_fonts.py build          # after `npm run build`; needs the network
    uv run python scripts/site_fonts.py check          # the CI gate: web job, after Build
    uv run python scripts/site_fonts.py --self-test    # exercises check's failure paths

One script with two verbs rather than a generator and a checker, because the
checker must walk the pages with exactly the walker the generator used, and
`scripts/` deliberately has no shared module — the mypy step in
`.github/workflows/ci.yml` records why two scripts may not import each other.

Why the built pages and not the sources: a chapter's text is assembled from
Astro components, payload JSON and YAML, and a walker over three source formats
is a second parser to keep in step with the first. `web/dist` is the one place
every rendered character exists exactly once.

Three roles, three faces. Text inside `h1`, `h2`, `h3` and `.hero-finding` is
set in Noto Serif TC, falling back to Plex and then the sans for a symbol the
serif lacks (the subscript in NO₂ is in none of the Noto sources); everything
else is offered IBM Plex Sans first (Latin, digits, symbols) and Noto Sans TC
second. So the sans role must be covered by the union of those two, and the
display role by the three together. A character a role lacks still renders —
in whatever the system stack falls back to — which is why the check exists:
the fallback is visibly a different face, and silently so. The first real run
found the rail's close glyph, ✕, in none of the three sources; it is × now.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import shutil
import sys
import tempfile
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DIST = REPO_ROOT / "web" / "dist"
FONTS = REPO_ROOT / "web" / "src" / "fonts"
CACHE = REPO_ROOT / ".cache" / "fonts"
MANIFEST = "fonts.json"
GOOGLE_FONTS = "https://raw.githubusercontent.com/google/fonts/main/ofl/"


@dataclass(frozen=True)
class Face:
    name: str
    role: str
    source: str
    licence: str
    pin: dict[str, float] | None


FACES: tuple[Face, ...] = (
    Face(
        "noto-sans-tc",
        "sans",
        "notosanstc/NotoSansTC%5Bwght%5D.ttf",
        "notosanstc/OFL.txt",
        None,
    ),
    Face(
        "noto-serif-tc",
        "display",
        "notoseriftc/NotoSerifTC%5Bwght%5D.ttf",
        "notoseriftc/OFL.txt",
        None,
    ),
    Face(
        "ibm-plex-sans",
        "latin",
        "ibmplexsans/IBMPlexSans%5Bwdth,wght%5D.ttf",
        "ibmplexsans/OFL.txt",
        {"wdth": 100.0},
    ),
)

# Which served faces a role may draw from, first choice first. Mirrors the
# `--font-*` stacks in web/src/styles/global.css.
STACKS: dict[str, tuple[str, ...]] = {
    "sans": ("ibm-plex-sans", "noto-sans-tc"),
    "display": ("noto-serif-tc", "ibm-plex-sans", "noto-sans-tc"),
}

DISPLAY_TAGS = frozenset({"h1", "h2", "h3"})
DISPLAY_CLASSES = frozenset({"hero-finding"})
SKIPPED_TAGS = frozenset({"script", "style", "template", "svg"})
VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "source",
        "track",
        "wbr",
    }
)
LATIN_LIMIT = 0x2E80

_ASCII = {chr(c) for c in range(0x20, 0x7F)}
_LATIN_1 = {chr(c) for c in range(0xA0, 0x100)}
_SYMBOLS = set("μ²³°±−×→←↑↓≤≥≈·…–—′″‰")
_CJK_PUNCTUATION = {chr(c) for c in range(0x3000, 0x3040)}
# The assigned fullwidth forms only — U+FF01–FF60 and U+FFE0–FFE6. The whole
# block also holds halfwidth kana and Hangul fillers the site never sets, and
# 16 unassigned code points the first coverage run reported as missing.
_FULLWIDTH = {chr(c) for c in range(0xFF01, 0xFF61)} | {chr(c) for c in range(0xFFE0, 0xFFE7)}

# Always in the sans subset, whatever the pages held on the day: printable
# ASCII, Latin-1, the symbols the formatters emit, CJK punctuation, fullwidth
# forms. Body copy changes often, and a base this wide keeps most edits from
# needing a rebuild.
BASE_CHARACTERS: frozenset[str] = frozenset(
    _ASCII | _LATIN_1 | _SYMBOLS | _CJK_PUNCTUATION | _FULLWIDTH
)

# The serif carries a smaller base. Measured on 2026-09-02: with the full base
# the display subset held 958 characters in 474,976 bytes, of which the
# Latin-1 and fullwidth blocks were 336 glyphs the headings used five of; the
# three static instances the spec's size rule would have shipped instead came
# to 410,416 bytes between them. Without those two blocks the variable file
# held 628 characters in 366,960 bytes. `character_sets` adds back whatever
# Latin-1 or fullwidth characters the pages render anywhere, so a heading may
# borrow one without a rebuild.
DISPLAY_BASE_CHARACTERS: frozenset[str] = frozenset(_ASCII | _SYMBOLS | _CJK_PUNCTUATION)


class _Roles(HTMLParser):
    """Which characters each role has to draw, read off the rendered document."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.sans: set[str] = set()
        self.display: set[str] = set()
        self._stack: list[tuple[str, bool]] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes: set[str] = set()
        for name, value in attrs:
            if name == "class" and value:
                classes.update(value.split())
        if tag in SKIPPED_TAGS:
            self._skip += 1
        if tag not in VOID_TAGS:
            display = tag in DISPLAY_TAGS or bool(classes & DISPLAY_CLASSES)
            self._stack.append((tag, display))

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIPPED_TAGS:
            self._skip = max(0, self._skip - 1)
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i][0] == tag:
                del self._stack[i:]
                break

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        chars = {c for c in data if not c.isspace()}
        self.sans |= chars
        if any(display for _, display in self._stack):
            self.display |= chars


def character_sets(pages: list[Path], *, with_base: bool = True) -> dict[str, set[str]]:
    """The characters each role must draw.

    `with_base` is what the build asks the subsetter for — the pages plus the
    base sets, so most copy edits need no rebuild. The check passes `False`:
    it holds the faces to what the pages render, because a base character the
    source font never had (the unassigned fullwidth code points, on the first
    run) is not a defect a rebuild could fix.
    """
    roles = _Roles()
    for page in pages:
        roles.feed(page.read_text(encoding="utf-8"))
    if not with_base:
        sans = set(roles.sans)
        display = set(roles.display)
        return {
            "sans": sans,
            "display": display,
            "latin": {c for c in sans if ord(c) < LATIN_LIMIT},
        }
    sans = set(roles.sans) | BASE_CHARACTERS
    borrowed = {c for c in roles.sans if c in _LATIN_1 or c in _FULLWIDTH}
    display = set(roles.display) | DISPLAY_BASE_CHARACTERS | borrowed
    latin = {c for c in sans if ord(c) < LATIN_LIMIT}
    return {"sans": sans, "display": display, "latin": latin}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def download(url: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=120) as response:
        target.write_bytes(response.read())


def build(dist: Path, out: Path, cache: Path) -> dict[str, Any]:
    from fontTools import subset
    from fontTools.ttLib import TTFont
    from fontTools.varLib import instancer

    pages = sorted(dist.rglob("*.html"))
    if not pages:
        raise SystemExit(f"no built pages under {dist}; run `npm run build` first")
    sets = character_sets(pages)
    out.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {
        "pages": len(pages),
        "characters": {role: len(chars) for role, chars in sets.items()},
        "faces": {},
    }
    for face in FACES:
        source_name = face.source.rsplit("/", 1)[-1].replace("%5B", "[").replace("%5D", "]")
        source = cache / source_name
        if not source.exists():
            download(GOOGLE_FONTS + face.source, source)
        licence = cache / f"{face.name}-OFL.txt"
        if not licence.exists():
            download(GOOGLE_FONTS + face.licence, licence)
        font = TTFont(source)
        options = subset.Options()
        options.flavor = "woff2"
        options.desubroutinize = True
        options.hinting = False
        options.notdef_outline = True
        options.name_IDs = ["*"]
        subsetter = subset.Subsetter(options)
        subsetter.populate(text="".join(sorted(sets[face.role])))
        subsetter.subset(font)
        # Subset first, pin the axis second. Pinning `wdth` makes the instancer
        # drop every glyph whose deltas vanish — the space and the soft hyphen
        # among them — and the subsetter then reads each retained glyph out of
        # `gvar` and raises KeyError on the first one missing. On the untouched
        # variable font `gvar` lists every glyph, so the subset comes first and
        # the instancer works on what is left.
        if face.pin:
            font = instancer.instantiateVariableFont(font, face.pin)
            font.flavor = "woff2"
        target = out / f"{face.name}.woff2"
        font.save(target)
        shutil.copyfile(licence, out / f"{face.name}-OFL.txt")
        axes = (
            {
                axis.axisTag: [axis.minValue, axis.defaultValue, axis.maxValue]
                for axis in font["fvar"].axes
            }
            if "fvar" in font
            else {}
        )
        manifest["faces"][face.name] = {
            "role": face.role,
            "source": GOOGLE_FONTS + face.source,
            "source_bytes": source.stat().st_size,
            "source_sha256": sha256(source),
            "axes": axes,
            "glyphs": len(font.getGlyphOrder()),
            "characters": len(sets[face.role]),
            "output": target.name,
            "bytes": target.stat().st_size,
            "sha256": sha256(target),
        }
    (out / MANIFEST).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


_FONT_URL = re.compile(r"url\(\s*['\"]?([^'\")]+\.woff2)[^)]*\)")


def _cmap(path: Path) -> set[str]:
    from fontTools.ttLib import TTFont

    with TTFont(path) as font:
        return {chr(code) for code in font.getBestCmap()}


def check(dist: Path, fonts: Path) -> list[str]:
    """Every character a role draws is in the faces that serve it, and the
    served files are the ones the manifest and the built stylesheet name."""
    problems: list[str] = []
    pages = sorted(dist.rglob("*.html"))
    if not pages:
        return [f"no built pages under {dist}"]
    manifest_path = fonts / MANIFEST
    if not manifest_path.exists():
        return [f"{manifest_path} is missing; run `site_fonts.py build`"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    faces: dict[str, dict[str, Any]] = manifest.get("faces", {})
    cmaps: dict[str, set[str]] = {}
    for name, face in faces.items():
        path = fonts / str(face["output"])
        if not path.exists():
            problems.append(f"{name}: {path.name} is missing")
            continue
        if sha256(path) != face["sha256"]:
            problems.append(
                f"{name}: {path.name} differs from fonts.json; run `site_fonts.py build`"
            )
        cmaps[name] = _cmap(path)
    sets = character_sets(pages, with_base=False)
    for role, stack in STACKS.items():
        served: set[str] = set()
        for name in stack:
            served |= cmaps.get(name, set())
        missing = sorted(sets[role] - served)
        if missing:
            shown = "".join(missing[:40]) + (" …" if len(missing) > 40 else "")
            problems.append(
                f"{role}: {len(missing)} character(s) on the pages are not in "
                f"{' or '.join(stack)}: {shown}"
            )
    assets = dist / "_astro"
    stylesheets = sorted(assets.glob("*.css")) if assets.exists() else []
    named: set[str] = set()
    for sheet in stylesheets:
        for url in _FONT_URL.findall(sheet.read_text(encoding="utf-8")):
            basename = url.rsplit("/", 1)[-1]
            named.add(basename)
            if not (assets / basename).exists() and not (dist / basename).exists():
                problems.append(f"{sheet.name} names {basename}, which is not in the built site")
    for name in faces:
        if not any(base.startswith((name + ".", name + "-")) for base in named):
            problems.append(f"{name}: no built stylesheet declares it in an @font-face")
    return problems


def _tiny_font(chars: str) -> bytes:
    """A real woff2 whose cmap holds exactly `chars`, for the self-test."""
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.ttGlyphPen import TTGlyphPen

    names = [".notdef"] + [f"u{ord(c):04X}" for c in chars]
    builder = FontBuilder(1000, isTTF=True)
    builder.setupGlyphOrder(names)
    builder.setupCharacterMap({ord(c): f"u{ord(c):04X}" for c in chars})
    pen = TTGlyphPen(None)
    pen.moveTo((50, 0))
    pen.lineTo((550, 0))
    pen.lineTo((550, 700))
    pen.lineTo((50, 700))
    pen.closePath()
    box = pen.glyph()
    builder.setupGlyf(dict.fromkeys(names, box))
    builder.setupHorizontalMetrics(dict.fromkeys(names, (600, 50)))
    builder.setupHorizontalHeader(ascent=800, descent=-200)
    builder.setupOS2()
    builder.setupPost()
    builder.setupNameTable({"familyName": "SiteFontsFixture", "styleName": "Regular"})
    builder.font.flavor = "woff2"
    buffer = io.BytesIO()
    builder.save(buffer)
    return buffer.getvalue()


def write_fixture_site(dist: Path, fonts: Path, *, page_text: str, face_text: str) -> None:
    """A one-page site and three served faces that hold exactly `face_text`.

    Exactly, and no base characters: the check holds the faces to what the
    page renders, so a fixture whose faces carry only the page's characters
    is the complete case.
    """
    fonts.mkdir(parents=True, exist_ok=True)
    (dist / "_astro").mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {"pages": 1, "characters": {}, "faces": {}}
    css: list[str] = []
    for face in FACES:
        data = _tiny_font(face_text)
        (fonts / f"{face.name}.woff2").write_bytes(data)
        hashed = f"{face.name}.fixture.woff2"
        (dist / "_astro" / hashed).write_bytes(data)
        css.append(
            f'@font-face{{font-family:"{face.name}";src:url(/_astro/{hashed}) format("woff2")}}'
        )
        manifest["faces"][face.name] = {
            "role": face.role,
            "output": f"{face.name}.woff2",
            "sha256": hashlib.sha256(data).hexdigest(),
        }
    (fonts / MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")
    (dist / "_astro" / "site.fixture.css").write_text("".join(css), encoding="utf-8")
    (dist / "index.html").write_text(f"<h1>{page_text}</h1><p>{page_text}</p>", encoding="utf-8")


def self_test() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        dist, fonts = root / "dist", root / "fonts"
        write_fixture_site(dist, fonts, page_text="A中", face_text="A中")
        complete = check(dist, fonts)
        if complete:
            raise SystemExit(f"self-test: a complete site was rejected: {complete}")
        (dist / "index.html").write_text("<h1>A中文</h1>", encoding="utf-8")
        problems = check(dist, fonts)
        if not any("文" in p and p.startswith("display") for p in problems):
            raise SystemExit("self-test: a heading character the serif lacks was not named")
        if not any("文" in p and p.startswith("sans") for p in problems):
            raise SystemExit("self-test: a body character the sans faces lack was not named")
        write_fixture_site(dist, fonts, page_text="A", face_text="A")
        (fonts / "noto-sans-tc.woff2").write_bytes(_tiny_font("A中"))
        if not any("differs from fonts.json" in p for p in check(dist, fonts)):
            raise SystemExit("self-test: a regenerated font without its manifest was accepted")
        write_fixture_site(dist, fonts, page_text="A", face_text="A")
        (dist / "_astro" / "ibm-plex-sans.fixture.woff2").unlink()
        if not any("not in the built site" in p for p in check(dist, fonts)):
            raise SystemExit("self-test: a stylesheet naming a missing file was accepted")
        write_fixture_site(dist, fonts, page_text="A", face_text="A")
        sheet = dist / "_astro" / "site.fixture.css"
        sheet.write_text(
            re.sub(r"@font-face\{[^}]*noto-serif-tc[^}]*\}", "", sheet.read_text(encoding="utf-8")),
            encoding="utf-8",
        )
        if not any("no built stylesheet declares it" in p for p in check(dist, fonts)):
            raise SystemExit("self-test: a face with no @font-face was accepted")
    print("site fonts self-test passed")
    return 0


def main(argv: list[str] | None = None) -> int:
    # The problems name the characters themselves, and a Windows console
    # defaults to cp950, which cannot encode most of them — the first real run
    # found a missing character and then crashed printing it.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("verb", nargs="?", choices=("build", "check"))
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--dist", type=Path, default=DIST)
    parser.add_argument("--fonts", type=Path, default=FONTS)
    parser.add_argument("--cache", type=Path, default=CACHE)
    args = parser.parse_args(argv)
    if args.self_test:
        return self_test()
    if args.verb == "build":
        manifest = build(args.dist, args.fonts, args.cache)
        for name, face in manifest["faces"].items():
            print(
                f"{name:<16} {face['characters']:>5} chars  {face['glyphs']:>5} glyphs"
                f"  {face['bytes']:>9,} bytes"
            )
        return 0
    if args.verb == "check":
        problems = check(args.dist, args.fonts)
        for problem in problems:
            print(problem)
        pages = len(list(args.dist.rglob("*.html")))
        print(f"site fonts: {pages} page(s), {len(problems)} problem(s)")
        return 1 if problems else 0
    parser.error("choose `build` or `check`, or pass --self-test")
    return 2


if __name__ == "__main__":
    sys.exit(main())
