"""Check that every built chapter has the shared publication opening.

The chapter registry owns the route set.  This gate reads that set from the
TypeScript literal, then checks the HTML readers actually receive rather than
the Astro source that produced it.

    python scripts/check_publication_structure.py [web/dist]
"""

from __future__ import annotations

import ast
import pathlib
import sys
from dataclasses import dataclass, field
from html.parser import HTMLParser

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT = ROOT / "web" / "dist"
REGISTRY = ROOT / "web" / "src" / "lib" / "chapters.ts"
EXPECTED_CHAPTERS = 10
REQUIRED_CLASSES = ("chapter-intro", "chapter-question", "chapter-finding")
OPEN_TO_CLOSE = {"{": "}", "[": "]", "(": ")"}
CLOSE_TO_OPEN = {close: open_ for open_, close in OPEN_TO_CLOSE.items()}
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


@dataclass(frozen=True)
class Token:
    kind: str
    value: str
    offset: int


def _quoted_token(source: str, start: int) -> tuple[Token, int]:
    quote = source[start]
    index = start + 1
    while index < len(source):
        char = source[index]
        if char == quote:
            literal = source[start : index + 1]
            if quote == "`":
                return Token("template", literal, start), index + 1
            try:
                value = ast.literal_eval(literal)
            except (SyntaxError, ValueError) as exc:
                raise ValueError(f"invalid string at offset {start}") from exc
            if not isinstance(value, str):
                raise ValueError(f"non-string literal at offset {start}")
            return Token("string", value, start), index + 1
        if char in "\r\n" and quote != "`":
            raise ValueError(f"unterminated string at offset {start}")
        index += 2 if char == "\\" else 1

    raise ValueError(f"unterminated string at offset {start}")


def _typescript_tokens(source: str) -> list[Token]:
    tokens: list[Token] = []
    index = 0
    punctuation = set(OPEN_TO_CLOSE) | set(CLOSE_TO_OPEN) | {":", ",", "=", ";"}

    while index < len(source):
        char = source[index]
        if char.isspace():
            index += 1
            continue
        if source.startswith("//", index):
            newline = source.find("\n", index + 2)
            index = len(source) if newline == -1 else newline + 1
            continue
        if source.startswith("/*", index):
            closing = source.find("*/", index + 2)
            if closing == -1:
                raise ValueError(f"unterminated block comment at offset {index}")
            index = closing + 2
            continue
        if char in {'"', "'", "`"}:
            token, index = _quoted_token(source, index)
            tokens.append(token)
            continue
        if char.isalpha() or char in "_$":
            end = index + 1
            while end < len(source) and (source[end].isalnum() or source[end] in "_$"):
                end += 1
            tokens.append(Token("identifier", source[index:end], index))
            index = end
            continue
        if char in punctuation:
            tokens.append(Token("punctuation", char, index))
        else:
            tokens.append(Token("other", char, index))
        index += 1

    return tokens


def _chapters_array_index(tokens: list[Token]) -> int:
    declaration = ("export", "const", "CHAPTERS")
    declarations = [
        index
        for index in range(len(tokens) - 2)
        if tuple(token.value for token in tokens[index : index + 3]) == declaration
        and all(token.kind == "identifier" for token in tokens[index : index + 3])
    ]
    if len(declarations) != 1:
        raise ValueError(f"expected one CHAPTERS declaration, found {len(declarations)}")

    stack: list[str] = []
    index = declarations[0] + 3
    while index < len(tokens):
        token = tokens[index]
        if token.value in OPEN_TO_CLOSE:
            stack.append(token.value)
        elif token.value in CLOSE_TO_OPEN:
            if not stack or stack[-1] != CLOSE_TO_OPEN[token.value]:
                raise ValueError(f"unmatched {token.value!r} before CHAPTERS assignment")
            stack.pop()
        elif token.value == "=" and not stack:
            break
        elif token.value == ";" and not stack:
            raise ValueError("CHAPTERS declaration has no array assignment")
        index += 1

    if index + 1 >= len(tokens) or tokens[index + 1].value != "[":
        raise ValueError("CHAPTERS assignment is not an array literal")
    return index + 1


def _matching_end(tokens: list[Token], start: int) -> int:
    if tokens[start].value not in OPEN_TO_CLOSE:
        raise ValueError(f"token at offset {tokens[start].offset} does not open a structure")
    stack = [tokens[start].value]
    for index in range(start + 1, len(tokens)):
        value = tokens[index].value
        if value in OPEN_TO_CLOSE:
            stack.append(value)
        elif value in CLOSE_TO_OPEN:
            if not stack or stack[-1] != CLOSE_TO_OPEN[value]:
                raise ValueError(f"unmatched {value!r} at offset {tokens[index].offset}")
            stack.pop()
            if not stack:
                return index
    raise ValueError(f"unclosed {tokens[start].value!r} at offset {tokens[start].offset}")


def _chapter_entries(tokens: list[Token], array_start: int) -> list[list[Token]]:
    entries: list[list[Token]] = []
    index = array_start + 1
    while index < len(tokens):
        token = tokens[index]
        if token.value == "]":
            return entries
        if token.value != "{":
            raise ValueError(
                f"CHAPTERS entry {len(entries) + 1} is not an object literal "
                f"at offset {token.offset}"
            )
        end = _matching_end(tokens, index)
        entries.append(tokens[index : end + 1])
        index = end + 1
        if index >= len(tokens):
            break
        if tokens[index].value == ",":
            index += 1
            continue
        if tokens[index].value == "]":
            return entries
        raise ValueError(f"expected ',' or ']' after CHAPTERS entry {len(entries)}")
    raise ValueError("CHAPTERS array is not closed")


def _entry_slug(entry: list[Token], entry_number: int) -> str:
    nested: list[str] = []
    found: list[str] = []
    for index in range(1, len(entry) - 1):
        token = entry[index]
        if token.value in OPEN_TO_CLOSE:
            nested.append(token.value)
            continue
        if token.value in CLOSE_TO_OPEN:
            if not nested or nested[-1] != CLOSE_TO_OPEN[token.value]:
                raise ValueError(f"entry {entry_number} has unmatched {token.value!r}")
            nested.pop()
            continue
        if nested or token.value != "slug" or token.kind not in {"identifier", "string"}:
            continue
        if index + 2 >= len(entry) or entry[index + 1].value != ":":
            continue
        value = entry[index + 2]
        if value.kind != "string":
            raise ValueError(f"entry {entry_number} slug must be a string literal")
        if index + 3 >= len(entry) or entry[index + 3].value not in {",", "}"}:
            raise ValueError(f"entry {entry_number} slug must contain only a string literal")
        found.append(value.value)

    if len(found) != 1:
        raise ValueError(
            f"entry {entry_number} must have exactly one top-level slug, found {len(found)}"
        )
    if not found[0]:
        raise ValueError(f"entry {entry_number} slug must not be empty")
    return found[0]


def _chapter_slugs_from_source(source: str) -> list[str]:
    tokens = _typescript_tokens(source)
    entries = _chapter_entries(tokens, _chapters_array_index(tokens))
    if len(entries) != EXPECTED_CHAPTERS:
        raise ValueError(f"expected {EXPECTED_CHAPTERS} chapter entries, found {len(entries)}")
    slugs = [_entry_slug(entry, index) for index, entry in enumerate(entries, start=1)]
    if len(set(slugs)) != len(slugs):
        raise ValueError("chapter slugs must be unique")
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
        parts = list(self.text)
        for child in self.children:
            if child.visible and not (without_labels and "chapter-intro-label" in child.classes):
                parts.append(child.rendered_text(without_labels=without_labels))
        return " ".join("".join(parts).split())


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
        if not candidate.rendered_text(without_labels=True):
            failures.append(f"visible .{class_name} has no rendered text")

    question = contract.get("chapter-question")
    finding = contract.get("chapter-finding")
    if question is not None and finding is not None:
        if question is finding or question.is_inside(finding) or finding.is_inside(question):
            failures.append("chapter question and finding must be independent descendants")
        if question.start_order >= finding.start_order:
            failures.append("chapter question must precede chapter finding")
        if h1 is not None and h1.start_order >= question.start_order:
            failures.append("chapter <h1> must precede question and finding")

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


def _run_preflight() -> None:
    valid_slugs = [f"chapter-{index}" for index in range(EXPECTED_CHAPTERS)]
    valid_entries = ",\n".join(
        f'{{ slug: "{slug}", note: \'slug: "string-decoy"\', nested: {{ slug: "nested-decoy" }} }}'
        for slug in valid_slugs
    )
    valid_registry = (
        '// export const CHAPTERS = [{ slug: "comment-decoy" }];\n'
        "const text = 'export const CHAPTERS = [{ slug: \"string-decoy\" }];';\n"
        "export const CHAPTERS: readonly Chapter[] = [\n"
        f"{valid_entries}\n"
        "] as const;"
    )
    if _chapter_slugs_from_source(valid_registry) != valid_slugs:
        raise RuntimeError("registry preflight rejected the valid control")

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
    }
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
