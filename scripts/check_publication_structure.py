"""Check that every built chapter has the shared publication opening.

The chapter registry owns the route set.  This gate reads that set from the
TypeScript literal, then checks the HTML readers actually receive rather than
the Astro source that produced it.

    python scripts/check_publication_structure.py [web/dist]
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import unquote, urlsplit

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT = ROOT / "web" / "dist"
REGISTRY = ROOT / "web" / "src" / "lib" / "chapters.ts"
TYPESCRIPT_COMPILER = ROOT / "web" / "node_modules" / "typescript" / "lib" / "typescript.js"
EXPECTED_CHAPTERS = 10
REQUIRED_START_HERE_DESTINATIONS = {"/trend/", "/stations/", "/methods/"}
START_HERE_DATA_DESTINATIONS = {"/explore/", "/data/"}
IGNORED_SUBTREES = {"template", "script", "style"}
VOID_ELEMENTS = {
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
    "param",
    "source",
    "track",
    "wbr",
}


NODE_REGISTRY_PARSER = r"""
const ts = require(process.argv[1]);
const fs = require("fs");
const sourceText = fs.readFileSync(0, "utf8");
const source = ts.createSourceFile(
  "chapters.ts",
  sourceText,
  ts.ScriptTarget.Latest,
  true,
  ts.ScriptKind.TS,
);

function fail(message) {
  process.stderr.write(message);
  process.exit(2);
}

if (source.parseDiagnostics.length) {
  const message = ts.flattenDiagnosticMessageText(
    source.parseDiagnostics[0].messageText,
    " ",
  );
  fail(`TypeScript parse error: ${message}`);
}

const declarations = [];
for (const statement of source.statements) {
  if (!ts.isVariableStatement(statement)) continue;
  const exported = statement.modifiers?.some(
    (modifier) => modifier.kind === ts.SyntaxKind.ExportKeyword,
  );
  const constant = (statement.declarationList.flags & ts.NodeFlags.Const) !== 0;
  if (!exported || !constant) continue;
  for (const declaration of statement.declarationList.declarations) {
    if (ts.isIdentifier(declaration.name) && declaration.name.text === "CHAPTERS") {
      declarations.push(declaration);
    }
  }
}

if (declarations.length !== 1) {
  fail(`expected one exported const CHAPTERS declaration, found ${declarations.length}`);
}

let initializer = declarations[0].initializer;
if (!initializer) fail("CHAPTERS declaration has no initializer");
while (ts.isParenthesizedExpression(initializer)) initializer = initializer.expression;
if (ts.isAsExpression(initializer)) {
  const type = initializer.type;
  const isConst =
    ts.isTypeReferenceNode(type) &&
    ts.isIdentifier(type.typeName) &&
    type.typeName.text === "const" &&
    !type.typeArguments;
  if (!isConst) fail("CHAPTERS initializer assertion must be 'as const'");
  initializer = initializer.expression;
  while (ts.isParenthesizedExpression(initializer)) initializer = initializer.expression;
}

if (!ts.isArrayLiteralExpression(initializer)) {
  fail("CHAPTERS initializer is not an array literal");
}
if (initializer.elements.length !== 10) {
  fail(`expected 10 CHAPTERS entries, found ${initializer.elements.length}`);
}

const slugs = [];
for (const [entryIndex, entry] of initializer.elements.entries()) {
  if (!ts.isObjectLiteralExpression(entry)) {
    fail(`CHAPTERS entry ${entryIndex + 1} is not an object literal`);
  }
  for (const member of entry.properties) {
    if (ts.isSpreadAssignment(member)) {
      fail(`entry ${entryIndex + 1} must not contain a spread assignment`);
    }
    if (ts.isComputedPropertyName(member.name)) {
      fail(`entry ${entryIndex + 1} must not contain a computed property name`);
    }
  }
  const slugMembers = entry.properties.filter((member) => {
    const name = member.name;
    return (
      (ts.isIdentifier(name) && name.text === "slug") ||
      (ts.isStringLiteral(name) && name.text === "slug")
    );
  });
  if (slugMembers.length !== 1) {
    fail(
      `entry ${entryIndex + 1} must have exactly one direct slug member, found ${slugMembers.length}`,
    );
  }
  const slugProperty = slugMembers[0];
  if (!ts.isPropertyAssignment(slugProperty)) {
    fail(`entry ${entryIndex + 1} slug must be a property assignment`);
  }
  const value = slugProperty.initializer;
  if (!ts.isStringLiteral(value) && !ts.isNoSubstitutionTemplateLiteral(value)) {
    fail(`entry ${entryIndex + 1} slug must be a string literal`);
  }
  if (!value.text) fail(`entry ${entryIndex + 1} slug must not be empty`);
  slugs.push(value.text);
}

if (new Set(slugs).size !== slugs.length) fail("chapter slugs must be unique");
process.stdout.write(JSON.stringify(slugs));
"""


def _chapter_slugs_from_source(source: str) -> list[str]:
    if not TYPESCRIPT_COMPILER.exists():
        raise ValueError(
            f"TypeScript compiler not found at {TYPESCRIPT_COMPILER}; "
            "run `npm ci` and `npm run build` in web/ first"
        )
    try:
        result = subprocess.run(
            ["node", "-e", NODE_REGISTRY_PARSER, str(TYPESCRIPT_COMPILER)],
            input=source,
            text=True,
            capture_output=True,
            check=False,
            encoding="utf-8",
        )
    except FileNotFoundError as exc:
        raise ValueError(
            "Node.js was not found; run `npm ci` and `npm run build` in web/ first"
        ) from exc
    if result.returncode != 0:
        message = result.stderr.strip() or f"Node.js exited with status {result.returncode}"
        raise ValueError(message)
    try:
        slugs = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("TypeScript registry parser returned invalid JSON") from exc
    if not isinstance(slugs, list) or any(not isinstance(slug, str) for slug in slugs):
        raise ValueError("TypeScript registry parser returned invalid slugs")
    return slugs


def chapter_slugs() -> list[str]:
    """Return the registry's chapter slugs in publication order."""
    source = REGISTRY.read_text(encoding="utf-8")
    try:
        return _chapter_slugs_from_source(source)
    except ValueError as exc:
        raise SystemExit(f"{REGISTRY} — {exc}") from exc


@dataclass
class Element:
    tag: str
    classes: frozenset[str]
    attributes: dict[str, str | None]
    parent: Element | None
    visible: bool
    start_order: int
    end_order: int | None = None
    text: list[str] = field(default_factory=list)
    children: list[Element] = field(default_factory=list)

    def is_inside(self, ancestor: Element) -> bool:
        parent = self.parent
        while parent is not None:
            if parent is ancestor:
                return True
            parent = parent.parent
        return False

    def rendered_text(self, *, without_labels: bool = False) -> str:
        if without_labels and "chapter-intro-label" in self.classes:
            return ""
        parts = list(self.text)
        for child in self.children:
            if child.visible and not (without_labels and "chapter-intro-label" in child.classes):
                parts.append(child.rendered_text(without_labels=without_labels))
        return " ".join("".join(parts).split())


def _without_css_comments(style: str) -> str | None:
    result: list[str] = []
    index = 0
    quote: str | None = None
    while index < len(style):
        character = style[index]
        if quote is not None:
            result.append(character)
            if character == "\\" and index + 1 < len(style):
                index += 1
                result.append(style[index])
            elif character == quote:
                quote = None
            index += 1
            continue
        if character in {'"', "'"}:
            quote = character
            result.append(character)
            index += 1
            continue
        if style.startswith("/*", index):
            end = style.find("*/", index + 2)
            if end < 0:
                return None
            index = end + 2
            continue
        result.append(character)
        index += 1
    return "".join(result)


def _hidden_by_inline_style(style: str | None) -> bool:
    uncommented = _without_css_comments(style or "")
    if uncommented is None:
        return True
    declarations: dict[str, tuple[str, bool]] = {}
    for declaration in uncommented.split(";"):
        property_name, separator, value = declaration.partition(":")
        if separator:
            name = property_name.strip().lower()
            raw_value = value.strip().lower()
            unprioritized, bang, priority = raw_value.rpartition("!")
            important = bool(bang and priority.strip() == "important")
            normalized = unprioritized.strip() if important else raw_value
            current = declarations.get(name)
            if current is None or important or not current[1]:
                declarations[name] = (normalized, important)
    display = declarations.get("display", ("", False))[0]
    visibility = declarations.get("visibility", ("", False))[0]
    return display == "none" or visibility in {
        "hidden",
        "collapse",
    }


class StructureParser(HTMLParser):
    """Count opening-contract elements and retain heading text in document order."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.elements: list[Element] = []
        self.errors: list[str] = []
        self._stack: list[Element] = []
        self._order = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._open(tag, attrs, self_closing=tag.lower() in VOID_ELEMENTS)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._open(tag, attrs, self_closing=True)

    def _open(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
        *,
        self_closing: bool,
    ) -> None:
        lowered = tag.lower()
        attributes = {name.lower(): value for name, value in attrs}
        attribute_names = {name.lower() for name, _ in attrs}
        parent = self._stack[-1] if self._stack else None
        visible = (
            (parent is None or parent.visible)
            and lowered not in IGNORED_SUBTREES
            and "hidden" not in attribute_names
            and (attributes.get("aria-hidden") or "").strip().lower() != "true"
            and not _hidden_by_inline_style(attributes.get("style"))
        )

        if visible and lowered in {"h1", "h2"}:
            for ancestor in self._stack:
                if ancestor.visible and ancestor.tag in {"h1", "h2"}:
                    self.errors.append(f"nested heading <{lowered}> inside <{ancestor.tag}>")
                    break

        self._order += 1
        element = Element(
            tag=lowered,
            classes=frozenset((attributes.get("class") or "").split()),
            attributes=attributes,
            parent=parent,
            visible=visible,
            start_order=self._order,
        )
        self.elements.append(element)
        if parent is not None:
            parent.children.append(element)
        if self_closing:
            element.end_order = self._order
        else:
            self._stack.append(element)

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if lowered in VOID_ELEMENTS:
            return
        self._order += 1
        if not self._stack:
            self.errors.append(f"unexpected closing </{lowered}>")
            return
        if self._stack[-1].tag == lowered:
            self._stack.pop().end_order = self._order
            return

        expected = self._stack[-1].tag
        self.errors.append(f"mismatched closing </{lowered}> while <{expected}> is open")
        matching = next(
            (
                position
                for position in range(len(self._stack) - 1, -1, -1)
                if self._stack[position].tag == lowered
            ),
            None,
        )
        if matching is None:
            return
        while len(self._stack) > matching:
            self._stack.pop().end_order = self._order

    def handle_data(self, data: str) -> None:
        if not self._stack or not self._stack[-1].visible:
            return
        self._stack[-1].text.append(data)

    def finish(self) -> None:
        if self._stack:
            unclosed = ", ".join(f"<{element.tag}>" for element in self._stack)
            self.errors.append(f"unclosed elements: {unclosed}")


def failures_for_text(html: str) -> list[str]:
    parser = StructureParser()
    parser.feed(html)
    parser.close()
    parser.finish()
    failures = list(parser.errors)
    visible = [element for element in parser.elements if element.visible]

    intro_candidates = [element for element in visible if "chapter-intro" in element.classes]
    intro: Element | None = None
    if not intro_candidates:
        failures.append("missing visible header.chapter-intro")
    elif len(intro_candidates) != 1:
        failures.append(
            f"expected exactly one visible .chapter-intro, found {len(intro_candidates)}"
        )
    elif intro_candidates[0].tag != "header":
        failures.append("visible .chapter-intro is not a <header>")
    else:
        intro = intro_candidates[0]

    h1_candidates = [element for element in visible if element.tag == "h1"]
    h1: Element | None = None
    if not h1_candidates:
        failures.append("missing visible <h1>")
    elif len(h1_candidates) != 1:
        failures.append(f"expected exactly one visible <h1>, found {len(h1_candidates)}")
    else:
        h1 = h1_candidates[0]
        if not h1.rendered_text():
            failures.append("visible <h1> has no rendered text")
        if intro is not None and not h1.is_inside(intro):
            failures.append("visible <h1> is not inside header.chapter-intro")
        elif intro is not None and h1.parent is not intro:
            failures.append("visible <h1> is not a direct child of header.chapter-intro")

    contract: dict[str, Element] = {}
    for class_name in ("chapter-question", "chapter-finding"):
        candidates = [element for element in visible if class_name in element.classes]
        if not candidates:
            failures.append(f"missing visible .{class_name}")
            continue
        if len(candidates) != 1:
            failures.append(f"expected exactly one visible .{class_name}, found {len(candidates)}")
            continue
        candidate = candidates[0]
        contract[class_name] = candidate
        if intro is not None and not candidate.is_inside(intro):
            failures.append(f"visible .{class_name} is not inside header.chapter-intro")
        elif intro is not None and candidate.parent is not intro:
            failures.append(f"visible .{class_name} is not a direct child of header.chapter-intro")
        if not candidate.rendered_text(without_labels=True):
            failures.append(f"visible .{class_name} has no rendered text")

    question = contract.get("chapter-question")
    finding = contract.get("chapter-finding")
    if question is not None and finding is not None:
        if question is finding or question.is_inside(finding) or finding.is_inside(question):
            failures.append("chapter question and finding must be independent descendants")
        if question.start_order >= finding.start_order:
            failures.append("chapter question must precede chapter finding")
        if h1 is not None and (h1.end_order is None or h1.end_order >= question.start_order):
            failures.append("chapter <h1> must close before question and finding")

    visible_h2 = [element for element in visible if element.tag == "h2"]
    if not visible_h2:
        failures.append("no visible meaningful <h2> after chapter intro")
    else:
        meaningful_after = 0
        for h2 in visible_h2:
            if not h2.rendered_text():
                failures.append("visible <h2> has no rendered text")
                continue
            if intro is None or h1 is None:
                continue
            intro_end = intro.end_order
            h1_end = h1.end_order
            if intro_end is None or h1_end is None:
                failures.append("chapter intro and <h1> must close before <h2>")
            elif h2.start_order <= max(intro_end, h1_end):
                failures.append("visible <h2> appears before chapter intro and <h1> close")
            else:
                meaningful_after += 1
        if intro is not None and h1 is not None and meaningful_after == 0:
            failures.append("no visible meaningful <h2> after chapter intro")

    return failures


def failures_for(page: pathlib.Path) -> list[str]:
    return failures_for_text(page.read_text(encoding="utf-8"))


def home_failures_for_text(html: str) -> list[str]:
    parser = StructureParser()
    parser.feed(html)
    parser.close()
    parser.finish()
    failures = list(parser.errors)
    visible = [element for element in parser.elements if element.visible]

    candidates = [element for element in visible if "start-here" in element.classes]
    if not candidates:
        failures.append("missing nav.start-here")
        return failures
    if len(candidates) != 1:
        failures.append(f"expected exactly one visible nav.start-here, found {len(candidates)}")
        return failures

    navigation = candidates[0]
    if navigation.tag != "nav":
        failures.append("visible .start-here is not a <nav>")
        return failures

    aria_label = (navigation.attributes.get("aria-label") or "").strip()
    labelled_by = (navigation.attributes.get("aria-labelledby") or "").strip().split()
    labelled_ids = {
        element.attributes.get("id")
        for element in visible
        if element.rendered_text() and element.attributes.get("id")
    }
    if not aria_label and (
        not labelled_by or any(label not in labelled_ids for label in labelled_by)
    ):
        failures.append("nav.start-here has no visible accessible label")

    links = [element for element in visible if element.tag == "a" and element.is_inside(navigation)]
    if len(links) != 4:
        failures.append(
            f"nav.start-here must contain exactly four visible links, found {len(links)}"
        )
        return failures

    for link in links:
        if not link.rendered_text():
            failures.append("nav.start-here contains a visible link with no rendered text")

    parsed_destinations = [
        _site_destination_from_href(link.attributes.get("href")) for link in links
    ]
    destinations = [destination[1] if destination else None for destination in parsed_destinations]
    if len(set(destinations)) != len(destinations):
        failures.append("nav.start-here link destinations must be unique")
    valid_destinations = [destination for destination in parsed_destinations if destination]
    if len(valid_destinations) == len(links) and len({item[0] for item in valid_destinations}) != 1:
        failures.append("nav.start-here links must share one site base path")
    destination_set = set(destinations)
    has_required = destination_set >= REQUIRED_START_HERE_DESTINATIONS
    chosen_data_destinations = destination_set & START_HERE_DATA_DESTINATIONS
    has_only_expected = destination_set <= (
        REQUIRED_START_HERE_DESTINATIONS | START_HERE_DATA_DESTINATIONS
    )
    if not has_required or len(chosen_data_destinations) != 1 or not has_only_expected:
        found = ", ".join(str(destination) for destination in destinations)
        failures.append(
            "nav.start-here links must target /trend/, /stations/, /methods/, "
            f"and one of /explore/ or /data/; found {found}"
        )

    return failures


def home_failures_for(page: pathlib.Path) -> list[str]:
    return home_failures_for_text(page.read_text(encoding="utf-8"))


def _fully_unquote(value: str) -> str:
    decoded = value
    while True:
        previous = decoded
        decoded = unquote(previous)
        if decoded == previous:
            return decoded


def _site_destination_from_href(href: str | None) -> tuple[str, str] | None:
    if not href:
        return None
    normalized_href = _fully_unquote(href).replace("\\", "/")
    if normalized_href.startswith("//"):
        return None
    parsed = urlsplit(href)
    if (
        parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/")
        or not parsed.path.endswith("/")
    ):
        return None
    segments: list[str] = []
    for raw_segment in parsed.path.split("/"):
        if not raw_segment:
            continue
        segment = _fully_unquote(raw_segment)
        if segment in {".", ".."} or "/" in segment or "\\" in segment:
            return None
        segments.append(segment)
    if not segments:
        return None
    base = f"/{'/'.join(segments[:-1])}" if len(segments) > 1 else ""
    return base, f"/{segments[-1]}/"


def _run_preflight() -> None:
    valid_slugs = [f"chapter-{index}" for index in range(EXPECTED_CHAPTERS)]
    valid_entries = ",\n".join(
        f'{{ ratio: total / divisor, slug: "{slug}", pattern: /[a/]+\\/not-slug/giu, '
        'note: \'slug: "string-decoy"\', nested: { slug: "nested-decoy" } }'
        for slug in valid_slugs
    )
    valid_registry = (
        '// export const CHAPTERS = [{ slug: "comment-decoy" }];\n'
        "const text = 'export const CHAPTERS = [{ slug: \"string-decoy\" }];';\n"
        'const template = `outer ${`export const CHAPTERS = [{ slug: "template-decoy" }]`} tail`;\n'
        "export const CHAPTERS: readonly Chapter[] = [\n"
        f"{valid_entries}\n"
        "] as const;"
    )
    if _chapter_slugs_from_source(valid_registry) != valid_slugs:
        raise RuntimeError("registry preflight rejected the valid control")

    def registry_with_entries(entries: list[str]) -> str:
        return "export const CHAPTERS = [\n" + ",\n".join(entries) + "\n] as const;"

    comment_decoys = "\n".join(f'// slug: "comment-{index}"' for index in range(EXPECTED_CHAPTERS))
    for name, source in {
        "empty array with comment slugs": f"export const CHAPTERS = [\n{comment_decoys}\n]",
        "unclosed array": ("export const CHAPTERS = [\n" + valid_entries),
        "duplicate slug": valid_registry.replace(
            f'slug: "{valid_slugs[-1]}"', f'slug: "{valid_slugs[0]}"', 1
        ),
        "slug expression": valid_registry.replace(
            f'slug: "{valid_slugs[0]}"', f'slug: "{valid_slugs[0]}" + suffix', 1
        ),
        "regex literal slugs": registry_with_entries(
            [f'{{pattern:/slug:"{slug}",/}}' for slug in valid_slugs]
        ),
        "shorthand slug override": registry_with_entries(
            [f'{{slug:"{slug}", slug}}' for slug in valid_slugs]
        ),
        "spread override": registry_with_entries(
            [f'{{slug:"{slug}", ...override}}' for slug in valid_slugs]
        ),
        "computed property override": registry_with_entries(
            [
                f'{{slug:"{slug}", ["sl"+"ug"]:"override-{index}"}}'
                for index, slug in enumerate(valid_slugs)
            ]
        ),
    }.items():
        try:
            _chapter_slugs_from_source(source)
        except ValueError:
            continue
        raise RuntimeError(f"registry preflight accepted {name}")

    valid_html = (
        '<header class="chapter-intro"><h1><span>Title</span></h1>'
        '<div class="chapter-question"><i class="chapter-intro-label">Label</i>Question</div>'
        '<div class="chapter-finding"><i class="chapter-intro-label">Label</i>'
        "<strong>Finding</strong></div></header><h2>Evidence</h2>"
    )
    valid_failures = failures_for_text(valid_html)
    if valid_failures:
        raise RuntimeError(f"HTML preflight rejected the valid control: {valid_failures}")

    mutations = {
        "three classes on one element": (
            "chapter question and finding must be independent descendants",
            '<header class="chapter-intro chapter-question chapter-finding">'
            "<h1>Title</h1></header><h2>Evidence</h2>",
        ),
        "nested headings": (
            "nested heading <h2> inside <h1>",
            '<header class="chapter-intro"><h1>Title<h2>Nested</h2></h1>'
            '<div class="chapter-question">Question</div>'
            '<div class="chapter-finding">Finding</div></header><h2>Evidence</h2>',
        ),
        "mismatched headings": (
            "mismatched closing </h2> while <h1> is open",
            valid_html.replace("</h1>", "</h2>", 1),
        ),
        "empty finding": (
            "visible .chapter-finding has no rendered text",
            '<header class="chapter-intro"><h1>Title</h1>'
            '<div class="chapter-question">Question</div><div class="chapter-finding">'
            '<i class="chapter-intro-label">Label</i></div></header><h2>Evidence</h2>',
        ),
        "contracts nested inside h1": (
            "chapter <h1> must close before question and finding",
            '<header class="chapter-intro"><h1>Title'
            '<span class="chapter-question">Question</span>'
            '<span class="chapter-finding">Finding</span></h1></header><h2>Evidence</h2>',
        ),
        "self-labeled question": (
            "visible .chapter-question has no rendered text",
            '<header class="chapter-intro"><h1>Title</h1>'
            '<div class="chapter-question chapter-intro-label">Label</div>'
            '<div class="chapter-finding">Finding</div></header><h2>Evidence</h2>',
        ),
        "self-labeled finding": (
            "visible .chapter-finding has no rendered text",
            '<header class="chapter-intro"><h1>Title</h1>'
            '<div class="chapter-question">Question</div>'
            '<div class="chapter-finding chapter-intro-label">Label</div>'
            "</header><h2>Evidence</h2>",
        ),
    }
    wrapped_contracts = {
        "h1": '<div><h1>Title</h1></div><div class="chapter-question">Question</div>'
        '<div class="chapter-finding">Finding</div>',
        "chapter-question": '<h1>Title</h1><div><div class="chapter-question">Question</div></div>'
        '<div class="chapter-finding">Finding</div>',
        "chapter-finding": '<h1>Title</h1><div class="chapter-question">Question</div>'
        '<div><div class="chapter-finding">Finding</div></div>',
    }
    for class_name, markup in wrapped_contracts.items():
        selector = f"<{class_name}>" if class_name == "h1" else f".{class_name}"
        mutations[f"wrapped {class_name}"] = (
            f"visible {selector} is not a direct child of header.chapter-intro",
            f'<header class="chapter-intro">{markup}</header><h2>Evidence</h2>',
        )
    for tag in IGNORED_SUBTREES:
        mutations[f"contract inside {tag}"] = (
            "missing visible header.chapter-intro",
            f"<{tag}>{valid_html}</{tag}>",
        )
    for attribute in ("hidden", 'aria-hidden="true"'):
        mutations[f"contract inside {attribute}"] = (
            "missing visible header.chapter-intro",
            f"<div {attribute}>{valid_html}</div>",
        )
    for name, (expected_failure, html) in mutations.items():
        mutation_failures = failures_for_text(html)
        if expected_failure not in mutation_failures:
            raise RuntimeError(
                f"HTML preflight did not reject {name} for the expected reason: {mutation_failures}"
            )

    start_here_destinations = ("/trend/", "/stations/", "/methods/", "/explore/")
    start_here_links = "".join(
        f'<a href="{destination}">Path {index}</a>'
        for index, destination in enumerate(start_here_destinations, start=1)
    )
    valid_home = (
        '<h2 id="start-here-heading">Start here</h2>'
        '<nav class="start-here" aria-labelledby="start-here-heading">'
        f"{start_here_links}</nav>"
    )
    valid_home_failures = home_failures_for_text(valid_home)
    if valid_home_failures:
        raise RuntimeError(f"home HTML preflight rejected the valid control: {valid_home_failures}")
    valid_data_home = valid_home.replace('/explore/">Path 4', '/data/">Path 4')
    valid_data_home_failures = home_failures_for_text(valid_data_home)
    if valid_data_home_failures:
        raise RuntimeError(
            "home HTML preflight rejected the valid /data/ alternative control: "
            f"{valid_data_home_failures}"
        )
    valid_prefixed_home = valid_home.replace('href="/', 'href="/project/')
    valid_prefixed_home_failures = home_failures_for_text(valid_prefixed_home)
    if valid_prefixed_home_failures:
        raise RuntimeError(
            "home HTML preflight rejected the valid base-prefixed control: "
            f"{valid_prefixed_home_failures}"
        )
    valid_cascade_home = valid_home.replace(
        '<a href="/explore/">Path 4</a>',
        '<a href="/explore/" style="display: none; display: block">Path 4</a>',
    )
    valid_cascade_home_failures = home_failures_for_text(valid_cascade_home)
    if valid_cascade_home_failures:
        raise RuntimeError(
            "home HTML preflight rejected the valid inline cascade control: "
            f"{valid_cascade_home_failures}"
        )
    valid_important_cascade_home = valid_home.replace(
        '<a href="/explore/">Path 4</a>',
        '<a href="/explore/" style="display: none; display: block !important">Path 4</a>',
    )
    valid_important_cascade_home_failures = home_failures_for_text(valid_important_cascade_home)
    if valid_important_cascade_home_failures:
        raise RuntimeError(
            "home HTML preflight rejected the valid important cascade control: "
            f"{valid_important_cascade_home_failures}"
        )
    valid_commented_cascade_home = valid_home.replace(
        '<a href="/explore/">Path 4</a>',
        '<a href="/explore/" style="display: none; dis/**/play: block">Path 4</a>',
    )
    valid_commented_cascade_home_failures = home_failures_for_text(valid_commented_cascade_home)
    if valid_commented_cascade_home_failures:
        raise RuntimeError(
            "home HTML preflight rejected the valid commented cascade control: "
            f"{valid_commented_cascade_home_failures}"
        )
    valid_commented_important_cascade_home = valid_home.replace(
        '<a href="/explore/">Path 4</a>',
        '<a href="/explore/" '
        'style="display: none !important; display: b/**/lock !/**/important">Path 4</a>',
    )
    valid_commented_important_cascade_home_failures = home_failures_for_text(
        valid_commented_important_cascade_home
    )
    if valid_commented_important_cascade_home_failures:
        raise RuntimeError(
            "home HTML preflight rejected the valid commented important cascade control: "
            f"{valid_commented_important_cascade_home_failures}"
        )

    home_mutations = {
        "empty start-here": (
            "nav.start-here must contain exactly four visible links, found 0",
            valid_home.replace(start_here_links, ""),
        ),
        "duplicate start-here": (
            "expected exactly one visible nav.start-here, found 2",
            valid_home + '<nav class="start-here" aria-label="Duplicate"></nav>',
        ),
        "duplicate destination": (
            "nav.start-here link destinations must be unique",
            valid_home.replace('/explore/">Path 4', '/trend/">Path 4'),
        ),
        "both data destinations": (
            "nav.start-here must contain exactly four visible links, found 5",
            valid_home.replace("</nav>", '<a href="/data/">Path 5</a></nav>', 1),
        ),
        "hidden start-here": (
            "missing nav.start-here",
            f"<div hidden>{valid_home}</div>",
        ),
        "hidden links": (
            "nav.start-here must contain exactly four visible links, found 0",
            valid_home.replace(start_here_links, f"<div hidden>{start_here_links}</div>"),
        ),
        "inline-hidden link": (
            "nav.start-here must contain exactly four visible links, found 3",
            valid_home.replace(
                '<a href="/explore/">Path 4</a>',
                '<a href="/explore/" style="display: none">Path 4</a>',
            ),
        ),
        "important inline-hidden link": (
            "nav.start-here must contain exactly four visible links, found 3",
            valid_home.replace(
                '<a href="/explore/">Path 4</a>',
                '<a href="/explore/" style="display: none !important; display: block">Path 4</a>',
            ),
        ),
        "commented important inline-hidden link": (
            "nav.start-here must contain exactly four visible links, found 3",
            valid_home.replace(
                '<a href="/explore/">Path 4</a>',
                '<a href="/explore/" '
                'style="display: none/**/!important; display: block">Path 4</a>',
            ),
        ),
        "commented property inline-hidden link": (
            "nav.start-here must contain exactly four visible links, found 3",
            valid_home.replace(
                '<a href="/explore/">Path 4</a>',
                '<a href="/explore/" '
                'style="dis/**/play: none !important; display: block">Path 4</a>',
            ),
        ),
        "commented value inline-hidden link": (
            "nav.start-here must contain exactly four visible links, found 3",
            valid_home.replace(
                '<a href="/explore/">Path 4</a>',
                '<a href="/explore/" '
                'style="display/**/: n/**/one !important; display: block">Path 4</a>',
            ),
        ),
        "unclosed style comment": (
            "nav.start-here must contain exactly four visible links, found 3",
            valid_home.replace(
                '<a href="/explore/">Path 4</a>',
                '<a href="/explore/" style="display: block; /*">Path 4</a>',
            ),
        ),
        "later important inline-hidden link": (
            "nav.start-here must contain exactly four visible links, found 3",
            valid_home.replace(
                '<a href="/explore/">Path 4</a>',
                '<a href="/explore/" style="display: block; display: none !important">Path 4</a>',
            ),
        ),
        "collapsed inline link": (
            "nav.start-here must contain exactly four visible links, found 3",
            valid_home.replace(
                '<a href="/explore/">Path 4</a>',
                '<a href="/explore/" style="visibility: visible; visibility: collapse">Path 4</a>',
            ),
        ),
        "template link": (
            "nav.start-here must contain exactly four visible links, found 3",
            valid_home.replace(
                '<a href="/explore/">Path 4</a>',
                '<template><a href="/explore/">Path 4</a></template>',
            ),
        ),
        "wrong destination": (
            "nav.start-here links must target ",
            valid_home.replace('/explore/">Path 4', '/space/">Path 4'),
        ),
        "missing fixed destination": (
            "nav.start-here links must target ",
            valid_home.replace('/stations/">Path 2', '/space/">Path 2'),
        ),
        "mixed base prefixes": (
            "nav.start-here links must share one site base path",
            valid_home.replace('href="/trend/', 'href="/alpha/trend/').replace(
                'href="/stations/', 'href="/beta/stations/'
            ),
        ),
        "dot segment base prefix": (
            "nav.start-here links must target ",
            valid_home.replace('href="/', 'href="/project/../'),
        ),
        "encoded dot segment base prefix": (
            "nav.start-here links must target ",
            valid_home.replace('href="/', 'href="/project/%2e%2e/'),
        ),
        "partially encoded dot segment base prefix": (
            "nav.start-here links must target ",
            valid_home.replace('href="/', 'href="/project/.%2e/'),
        ),
        "encoded backslash base prefix": (
            "nav.start-here links must target ",
            valid_home.replace('href="/', 'href="/project%5c../'),
        ),
        "triple-slash network path": (
            "nav.start-here links must target ",
            valid_home.replace('href="/', 'href="///'),
        ),
        "authority network path": (
            "nav.start-here links must target ",
            valid_home.replace('href="/', 'href="//host/'),
        ),
        "encoded authority network path": (
            "nav.start-here links must target ",
            valid_home.replace('href="/', 'href="%2f%2fhost/'),
        ),
        "encoded backslash authority path": (
            "nav.start-here links must target ",
            valid_home.replace('href="/', 'href="%5c%5chost/'),
        ),
    }
    for name, (expected_failure, html) in home_mutations.items():
        mutation_failures = home_failures_for_text(html)
        if not any(failure.startswith(expected_failure) for failure in mutation_failures):
            raise RuntimeError(
                f"home HTML preflight did not reject {name} for the expected reason: "
                f"{mutation_failures}"
            )


def main(argv: list[str]) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    try:
        _run_preflight()
    except (RuntimeError, ValueError) as exc:
        print(f"publication structure preflight failed: {exc}", file=sys.stderr)
        return 1

    dist = pathlib.Path(argv[1]) if len(argv) > 1 else DEFAULT
    if not dist.exists():
        print(f"{dist} not found — run `npm run build` in web/ first", file=sys.stderr)
        return 1

    home_page = dist / "index.html"
    if not home_page.exists():
        home_failures = [f"missing {home_page.relative_to(ROOT).as_posix()}"]
    else:
        home_failures = home_failures_for(home_page)
    for failure in home_failures:
        print(f"home: {failure}")

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
    print(f"home with structure failures: {int(bool(home_failures))}")
    return 1 if failed_chapters or home_failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
