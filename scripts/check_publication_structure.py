"""Check that every built chapter has the shared publication opening.

The chapter registry owns the route set.  This gate reads that set from the
TypeScript literal, then checks the HTML readers actually receive rather than
the Astro source that produced it.

    python scripts/check_publication_structure.py [web/dist]
"""

from __future__ import annotations

import itertools
import json
import math
import pathlib
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any
from urllib.parse import unquote, urlsplit

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT = ROOT / "web" / "dist"
REGISTRY = ROOT / "web" / "src" / "lib" / "chapters.ts"
DETECTION_LIMIT = ROOT / "web" / "public" / "data" / "story" / "detection-limit.json"
TYPESCRIPT_COMPILER = ROOT / "web" / "node_modules" / "typescript" / "lib" / "typescript.js"
EXPECTED_CHAPTERS = 10
REQUIRED_START_HERE_DESTINATIONS = {"/trend/", "/stations/", "/methods/"}
START_HERE_DATA_DESTINATIONS = {"/explore/", "/data/"}
REQUIRED_CHAPTER_INDEX_DESTINATIONS = (
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
EXPECTED_THESIS_FRAGMENTS = {
    "space": ("那個分層是不是足夠的空間控制",),
}
EXPECTED_ANALYTICAL_FIGURES = {
    "trend": (
        ("1.1", "固定測站後，全台 PM2.5 的下降仍然成立嗎？"),
        ("1.2", "在相同天氣條件下，PM2.5 還下降多少？"),
        ("1.3", "各空品區的長期趨勢是否一致？"),
    ),
    "space": (
        ("3.1", "官方分區移除了多少空間相依？"),
        ("3.2", "純地理分群會得到不同結論嗎？"),
    ),
    "sources": (("4.1", "高濃度空氣在什麼風向與風速條件下出現？"),),
    "detection": (("5.1", "事件估計值能否離開安慰劑散布？"),),
    "forecast": (
        ("6.1", "各預測期距的誤差如何變化？"),
        ("6.2", "模型相對兩條基準線何時失去優勢？"),
        ("6.3", "自動搜尋買到的準確度足以抵銷成本嗎？"),
    ),
    "health": (
        ("7.1", "比較基準如何改變可歸因比例？"),
        ("7.2", "不同暴露反應函數會把結果推動多少？"),
    ),
    "methods": (
        ("8.1", "月平均隱藏了多少逐時變異？"),
        ("8.2", "不同補值方法對不同缺口長度付出什麼代價？"),
    ),
}
URL_C0_AND_SPACE = "".join(chr(value) for value in range(0x21))
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

NODE_URL_CONTROL = r"""
const fs = require("fs");
const hrefs = JSON.parse(fs.readFileSync(0, "utf8"));
const base = "https://local.invalid/project/";
process.stdout.write(JSON.stringify(hrefs.map((href) => {
  const url = new URL(href, base);
  return [url.origin, url.pathname];
})));
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


def _whatwg_url_results(hrefs: list[str]) -> list[list[str]]:
    try:
        result = subprocess.run(
            ["node", "-e", NODE_URL_CONTROL],
            input=json.dumps(hrefs),
            text=True,
            capture_output=True,
            check=False,
            encoding="utf-8",
        )
    except FileNotFoundError as exc:
        raise ValueError("Node.js was not found; URL preflight cannot run") from exc
    if result.returncode != 0:
        message = result.stderr.strip() or f"Node.js exited with status {result.returncode}"
        raise ValueError(message)
    try:
        resolved = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("WHATWG URL preflight returned invalid JSON") from exc
    if not isinstance(resolved, list) or any(
        not isinstance(item, list)
        or len(item) != 2
        or any(not isinstance(value, str) for value in item)
        for item in resolved
    ):
        raise ValueError("WHATWG URL preflight returned invalid results")
    return resolved


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
    content: list[str | Element] = field(default_factory=list)

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
        parts: list[str] = []
        for item in self.content:
            if isinstance(item, str):
                parts.append(item)
            elif item.visible and not (without_labels and "chapter-intro-label" in item.classes):
                parts.append(item.rendered_text(without_labels=without_labels))
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
        normalized_attribute_names = [name.lower() for name, _ in attrs]
        attribute_names = set(normalized_attribute_names)
        duplicate_attribute_names = sorted(
            {name for name in attribute_names if normalized_attribute_names.count(name) > 1}
        )
        if duplicate_attribute_names:
            self.errors.append(
                f"duplicate HTML attribute names on <{lowered}>: {duplicate_attribute_names}"
            )
        attributes = {name.lower(): value for name, value in attrs}
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
            parent.content.append(element)
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
        self._stack[-1].content.append(data)

    def finish(self) -> None:
        if self._stack:
            unclosed = ", ".join(f"<{element.tag}>" for element in self._stack)
            self.errors.append(f"unclosed elements: {unclosed}")


TREND_READING_MAP = (
    ("#evidence-1-1-title", "固定測站後，下降是否仍成立？"),
    ("#trend-weather-adjustment", "排除天氣後，下降幅度剩多少？"),
    ("#trend-airzones", "各空品區是否同步改善？"),
)

DETECTION_EVENT_KEYS = frozenset(
    {
        "credible_stations",
        "event",
        "kind",
        "median_effect",
        "median_placebo_mean",
        "median_placebo_sd",
        "n_credible",
        "n_expected_by_chance",
        "n_stations",
        "placebo_effects",
        "station_effects",
    }
)
DETECTION_READING_STEPS = (
    ("placebo", "先看灰線", "沒有事件標記時，同一程序仍會算出的差額。"),
    ("event", "再看橘點", "事件窗口各測站的觀測－預測差額。"),
    ("threshold", "最後看門檻", "通過數是否高於純靠機率的預期。"),
)
DETECTION_EVENT_CONTRACT = (
    ("COVID-19 全國三級警戒", "window"),
    ("台中電廠 2、3 號機生煤許可爭議", "window"),
    ("2018 空氣污染防制法修正", "trend_break"),
)
DETECTION_KIND_LABELS = {
    "window": "窗口事件：觀測－預測差額",
    "trend_break": "趨勢斷點：斜率差",
}
DETECTION_BOUNDARY_CLAIMS = (
    "「測不到」不等於「等於零」。",
    "每個事件的實際通過數都低於各自純靠機率的預期。",
    "這個方法在這些日曆窗口的噪音底線是 2.5–3.5 μg/m³，",
    "而待測的效應量是 0.5–1.6 μg/m³。",
    "噪音底線高於訊號。",
    "這批資料與這個方法，無法分辨這種大小的效應",
    "不是「這些事件沒有影響」。",
    "非偵測不是「事件沒有發生」或「介入無效」的證明。",
    "本分析沒有驗證機組的逐時操作或燃料狀態",
    "介入沒有依事件標籤發生",
    "介入發生但環境訊號太小",
    "模型與測站配置無法辨識",
    "這三種情況都與目前的非偵測相容。",
)


@dataclass(frozen=True)
class DetectionExpectedEvent:
    event: str
    kind: str
    n_credible: int
    n_expected_by_chance: int | float


def _is_inside_disclosure(element: Element) -> bool:
    current: Element | None = element
    while current is not None:
        if current.tag == "details":
            return True
        current = current.parent
    return False


def _detection_expected_events_from_payload(payload: object) -> tuple[DetectionExpectedEvent, ...]:
    if not isinstance(payload, dict) or set(payload) != {"events", "method", "spatial_check"}:
        raise ValueError("detection payload top-level shape changed")
    events = payload["events"]
    if not isinstance(events, list) or len(events) != len(DETECTION_EVENT_CONTRACT):
        count = len(events) if isinstance(events, list) else "not a list"
        raise ValueError(
            "detection payload event inventory changed: "
            f"found {count}, expected {len(DETECTION_EVENT_CONTRACT)}"
        )

    expected_events: list[DetectionExpectedEvent] = []
    identities: set[str] = set()
    for index, (raw_event, (expected_identity, expected_kind)) in enumerate(
        zip(events, DETECTION_EVENT_CONTRACT, strict=True), start=1
    ):
        if not isinstance(raw_event, dict) or set(raw_event) != DETECTION_EVENT_KEYS:
            raise ValueError(f"detection payload event {index} shape changed")
        event = raw_event["event"]
        kind = raw_event["kind"]
        observed = raw_event["n_credible"]
        expected = raw_event["n_expected_by_chance"]
        if not isinstance(event, str) or not event:
            raise ValueError(f"detection payload event {index} identity is invalid")
        if event != expected_identity:
            raise ValueError(f"detection payload event identity/order changed at position {index}")
        if kind != expected_kind:
            raise ValueError(f"detection payload event kind changed at position {index}")
        if isinstance(observed, bool) or not isinstance(observed, int) or observed < 0:
            raise ValueError(f"detection payload event {index} observed count is invalid")
        if (
            isinstance(expected, bool)
            or not isinstance(expected, (int, float))
            or not math.isfinite(expected)
            or expected < 0
        ):
            raise ValueError(f"detection payload event {index} expected count is invalid")
        if event in identities:
            raise ValueError(f"detection payload repeats event identity: {event}")
        if observed >= expected:
            raise ValueError(
                f"detection payload event {index} no longer supports the below-chance claim"
            )
        identities.add(event)
        expected_events.append(DetectionExpectedEvent(event, kind, observed, expected))
    return tuple(expected_events)


def _reject_nonfinite_json_constant(value: str) -> object:
    raise ValueError(f"detection payload contains invalid JSON number: {value}")


def load_detection_expected_events() -> tuple[DetectionExpectedEvent, ...]:
    """Load only the event identities and counts that the published page must expose."""
    try:
        with DETECTION_LIMIT.open(encoding="utf-8") as handle:
            payload = json.load(handle, parse_constant=_reject_nonfinite_json_constant)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read detection payload: {exc}") from exc
    return _detection_expected_events_from_payload(payload)


def detection_limitation_brief_failures_for_text(
    html: str,
    expected_events: tuple[DetectionExpectedEvent, ...],
) -> list[str]:
    parser = StructureParser()
    parser.feed(html)
    parser.close()
    parser.finish()
    failures = list(parser.errors)
    elements = parser.elements

    figure_titles = [element for element in elements if "evidence-title" in element.classes]
    figure_title: Element | None = None
    if (
        len(figure_titles) != 1
        or not figure_titles[0].visible
        or figure_titles[0].rendered_text() != "事件估計值能否離開安慰劑散布？"
    ):
        failures.append("detection Figure 5.1 title changed")
    else:
        figure_title = figure_titles[0]

    reading_keys = [
        element for element in elements if "data-detection-reading-key" in element.attributes
    ]
    reading_key: Element | None = None
    if len(reading_keys) != 1 or not reading_keys[0].visible:
        failures.append(f"detection reading key inventory changed: {len(reading_keys)}")
    else:
        reading_key = reading_keys[0]
        if reading_key.tag != "ol":
            failures.append("detection reading key is not an ordered list")
        if _is_inside_disclosure(reading_key):
            failures.append("detection reading key is user-collapsible")
        steps = [
            element
            for element in elements
            if element.tag == "li" and element.is_inside(reading_key)
        ]
        marked_steps = [
            element
            for element in elements
            if "data-detection-reading-step" in element.attributes
            and element.is_inside(reading_key)
        ]
        expected_step_names = [step[0] for step in DETECTION_READING_STEPS]
        observed_step_names = [step.attributes.get("data-detection-reading-step") for step in steps]
        if (
            steps != marked_steps
            or observed_step_names != expected_step_names
            or any(not step.visible for step in steps)
        ):
            failures.append(f"detection reading-step inventory changed: {observed_step_names!r}")
        else:
            for step, (_name, heading, explanation) in zip(
                steps, DETECTION_READING_STEPS, strict=True
            ):
                text = step.rendered_text()
                if heading not in text or explanation not in text:
                    failures.append("detection reading step text changed")

    primary_plots = [element for element in elements if "data-primary-plot" in element.attributes]
    primary_plot: Element | None = None
    if len(primary_plots) != 1 or not primary_plots[0].visible:
        failures.append(f"detection primary plot inventory changed: {len(primary_plots)}")
    else:
        primary_plot = primary_plots[0]

    caption: Element | None = None
    if primary_plot is not None:
        figures = [
            ancestor
            for ancestor in elements
            if ancestor.tag == "figure" and primary_plot.is_inside(ancestor)
        ]
        captions = [
            element
            for element in elements
            if element.tag == "figcaption" and any(element.is_inside(figure) for figure in figures)
        ]
        if len(captions) != 1 or not captions[0].visible:
            failures.append(f"detection Figure 5.1 caption inventory changed: {len(captions)}")
        else:
            caption = captions[0]

    comparisons = [
        element for element in elements if "data-detection-comparison" in element.attributes
    ]
    comparison: Element | None = None
    if len(comparisons) != 1 or not comparisons[0].visible:
        failures.append(f"detection comparison inventory changed: {len(comparisons)}")
    else:
        comparison = comparisons[0]
        if comparison.tag != "dl":
            failures.append("detection comparison is not a description list")
        if _is_inside_disclosure(comparison):
            failures.append("detection comparison is user-collapsible")
        semantic_rows = comparison.children
        hooked_rows = [
            element
            for element in elements
            if "data-detection-event" in element.attributes and element.is_inside(comparison)
        ]
        rows_are_hooks = len(semantic_rows) == len(hooked_rows) and all(
            row is hooked for row, hooked in zip(semantic_rows, hooked_rows, strict=True)
        )
        if len(semantic_rows) != len(expected_events) or not rows_are_hooks:
            failures.append(
                "detection semantic-row inventory changed: "
                f"{len(semantic_rows)} rows, {len(hooked_rows)} hooks"
            )
        observed_events = [entry.attributes.get("data-detection-event") for entry in semantic_rows]
        expected_identities = [event.event for event in expected_events]
        if observed_events != expected_identities or any(
            not entry.visible for entry in semantic_rows
        ):
            failures.append(f"detection event inventory changed: {observed_events!r}")
        else:
            for entry, expected_event in zip(semantic_rows, expected_events, strict=True):
                observed = str(expected_event.n_credible)
                expected = str(expected_event.n_expected_by_chance)
                kind_label = DETECTION_KIND_LABELS[expected_event.kind]
                if entry.attributes.get("data-detection-kind") != expected_event.kind:
                    failures.append("detection event kind changed")
                if entry.attributes.get("data-detection-observed") != observed:
                    failures.append("detection event observed value changed")
                if entry.attributes.get("data-detection-expected") != expected:
                    failures.append("detection event expected value changed")
                if entry.tag != "div" or [child.tag for child in entry.children] != ["dt", "dd"]:
                    failures.append("detection event description structure changed")
                    continue
                term, description = entry.children
                expected_term = f"{expected_event.event} · {kind_label}"
                expected_description = f"實際通過 {observed} 站；純靠機率的預期為 {expected} 站。"
                if (
                    not term.visible
                    or not description.visible
                    or term.rendered_text() != expected_term
                    or description.rendered_text() != expected_description
                    or "".join(entry.rendered_text().split())
                    != "".join(f"{expected_term}{expected_description}".split())
                ):
                    failures.append("detection event accessible text changed")

    boundaries = [
        element for element in elements if "data-detection-inference-boundary" in element.attributes
    ]
    boundary: Element | None = None
    if len(boundaries) != 1 or not boundaries[0].visible:
        failures.append(f"detection inference-boundary inventory changed: {len(boundaries)}")
    else:
        boundary = boundaries[0]
        if boundary.tag != "aside":
            failures.append("detection inference boundary is not an <aside>")
        if _is_inside_disclosure(boundary):
            failures.append("detection inference boundary is user-collapsible")
        boundary_text = boundary.rendered_text()
        if any(claim not in boundary_text for claim in DETECTION_BOUNDARY_CLAIMS):
            failures.append("detection inference boundary claim changed")

    if comparison is not None and boundary is not None:
        parent = comparison.parent
        if (
            parent is None
            or boundary.parent is not parent
            or parent.children.index(boundary) != parent.children.index(comparison) + 1
        ):
            failures.append("detection comparison and inference boundary are not adjacent")

    method_evidence = [
        element for element in elements if "data-detection-method-evidence" in element.attributes
    ]
    results = [element for element in elements if "data-detection-results" in element.attributes]
    if len(method_evidence) != 1 or not method_evidence[0].visible:
        failures.append(f"detection method-evidence inventory changed: {len(method_evidence)}")
    if len(results) != 1 or not results[0].visible:
        failures.append(f"detection later evidence inventory changed: {len(results)}")
    elif (
        len(method_evidence) == 1
        and figure_title is not None
        and reading_key is not None
        and primary_plot is not None
        and caption is not None
        and comparison is not None
        and boundary is not None
    ):
        opening = [
            figure_title.start_order,
            reading_key.start_order,
            primary_plot.start_order,
            caption.start_order,
            comparison.start_order,
            boundary.start_order,
            method_evidence[0].start_order,
            results[0].start_order,
        ]
        if any(not isinstance(position, int) or position < 0 for position in opening):
            failures.append("detection opening source index is invalid")
        elif opening != sorted(opening) or len(set(opening)) != len(opening):
            failures.append("detection opening order changed")
    return failures


def trend_reading_map_failures_for_text(html: str) -> list[str]:
    parser = StructureParser()
    parser.feed(html)
    parser.close()
    parser.finish()
    failures = list(parser.errors)
    visible = [element for element in parser.elements if element.visible]
    maps = [element for element in visible if "data-chapter-reading-map" in element.attributes]
    if len(maps) != 1:
        return [
            *failures,
            f"expected exactly one visible trend reading map, found {len(maps)}",
        ]
    reading_map = maps[0]
    if reading_map.tag != "nav":
        failures.append("trend reading map is not a <nav>")
    if reading_map.attributes.get("aria-label") != "本章閱讀地圖":
        failures.append("trend reading map accessible label changed")
    links = [
        element for element in visible if element.tag == "a" and element.is_inside(reading_map)
    ]
    marked_links = [
        element
        for element in visible
        if "data-chapter-reading-link" in element.attributes and element.is_inside(reading_map)
    ]
    if marked_links != links:
        failures.append("trend reading-map link markers changed")
    observed = [(link.attributes.get("href", ""), link.rendered_text()) for link in links]
    if observed != list(TREND_READING_MAP):
        failures.append(f"trend reading-map links changed: {observed!r}")
    targets: list[Element] = []
    for href, _question in TREND_READING_MAP:
        target_id = href.removeprefix("#")
        candidates = [element for element in visible if element.attributes.get("id") == target_id]
        if len(candidates) != 1:
            failures.append(
                f"trend reading-map target inventory changed for {href}: {len(candidates)}"
            )
        else:
            targets.append(candidates[0])
    if len(targets) == len(TREND_READING_MAP):
        starts = [target.start_order for target in targets]
        if starts != sorted(starts):
            failures.append("trend reading-map target order changed")
    primary = [element for element in visible if "data-primary-evidence" in element.attributes]
    if len(primary) != 1:
        failures.append(f"trend primary-evidence inventory changed: {len(primary)}")
    elif reading_map.end_order is None or reading_map.end_order >= primary[0].start_order:
        failures.append("trend primary evidence precedes the reading map")
    return failures


def visible_reading_map_count(html: str) -> int:
    parser = StructureParser()
    parser.feed(html)
    parser.close()
    parser.finish()
    return sum(
        element.visible and "data-chapter-reading-map" in element.attributes
        for element in parser.elements
    )


def failures_for_text(html: str, required_thesis_fragments: tuple[str, ...] = ()) -> list[str]:
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

    thesis_candidates = [element for element in visible if "chapter-thesis" in element.classes]
    thesis: Element | None = None
    if not thesis_candidates:
        failures.append("missing visible .chapter-thesis")
    elif len(thesis_candidates) != 1:
        failures.append(
            f"expected exactly one visible .chapter-thesis, found {len(thesis_candidates)}"
        )
    else:
        thesis = thesis_candidates[0]
        if intro is not None and not thesis.is_inside(intro):
            failures.append("visible .chapter-thesis is not inside header.chapter-intro")
        elif intro is not None and thesis.parent is not intro:
            failures.append("visible .chapter-thesis is not a direct child of header.chapter-intro")
        thesis_text = thesis.rendered_text()
        if not thesis_text:
            failures.append("visible .chapter-thesis has no rendered text")
        for fragment in required_thesis_fragments:
            if fragment not in thesis_text:
                failures.append(f"chapter thesis is missing required finding fragment: {fragment}")
        if h1 is not None and (h1.end_order is None or h1.end_order >= thesis.start_order):
            failures.append("chapter thesis must follow chapter <h1>")

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


def failures_for(page: pathlib.Path, required_thesis_fragments: tuple[str, ...] = ()) -> list[str]:
    return failures_for_text(page.read_text(encoding="utf-8"), required_thesis_fragments)


def _nearest_ancestor_with_class(element: Element, class_name: str) -> Element | None:
    ancestor = element.parent
    while ancestor is not None:
        if class_name in ancestor.classes:
            return ancestor
        ancestor = ancestor.parent
    return None


def analytical_figure_failures_for_text(
    html: str,
    expected: tuple[tuple[str, str], ...] | None = None,
) -> list[str]:
    parser = StructureParser()
    parser.feed(html)
    parser.close()
    parser.finish()
    visible = [element for element in parser.elements if element.visible]
    figures = [element for element in visible if element.tag == "figure"]
    failures: list[str] = []

    if expected is not None and len(figures) != len(expected):
        failures.append(
            f"expected exactly {len(expected)} visible analytical figures, found {len(figures)}"
        )

    used_shells: dict[int, Element] = {}
    checked_shells: set[int] = set()
    observed: list[tuple[str, str] | None] = []
    for figure_index, figure in enumerate(figures, start=1):
        shell = _nearest_ancestor_with_class(figure, "evidence-figure")
        if shell is None:
            failures.append(f"unframed figure {figure_index}")
            observed.append(None)
            continue
        used_shells[id(shell)] = shell
        if shell.tag != "section":
            failures.append("figure evidence ancestor is not a <section>")
        if figure.parent is not shell:
            failures.append("figure is not a direct child of its nearest .evidence-figure ancestor")

        shell_id = id(shell)
        if shell_id in checked_shells:
            continue
        checked_shells.add(shell_id)
        shell_figures = [
            candidate
            for candidate in figures
            if _nearest_ancestor_with_class(candidate, "evidence-figure") is shell
        ]
        if len(shell_figures) != 1:
            failures.append(
                "evidence figure must contain exactly one native figure, "
                f"found {len(shell_figures)}"
            )

        numbers = [
            candidate
            for candidate in visible
            if "evidence-number" in candidate.classes
            and _nearest_ancestor_with_class(candidate, "evidence-figure") is shell
        ]
        number_text = ""
        if not numbers:
            failures.append("evidence figure has no visible .evidence-number")
        elif len(numbers) != 1:
            failures.append(
                "evidence figure must contain exactly one visible .evidence-number, "
                f"found {len(numbers)}"
            )
        else:
            number = numbers[0]
            number_text = number.rendered_text()
            if number.tag != "p":
                failures.append("visible .evidence-number is not a <p>")
            if not number_text:
                failures.append("visible .evidence-number has no rendered text")
            if number.is_inside(figure):
                failures.append("visible .evidence-number is inside the native figure")

        titles = [
            candidate
            for candidate in visible
            if "evidence-title" in candidate.classes
            and _nearest_ancestor_with_class(candidate, "evidence-figure") is shell
        ]
        title_text = ""
        title: Element | None = None
        if not titles:
            failures.append("evidence figure has no visible .evidence-title")
        elif len(titles) != 1:
            failures.append(
                "evidence figure must contain exactly one visible .evidence-title, "
                f"found {len(titles)}"
            )
        else:
            title = titles[0]
            title_text = title.rendered_text()
            if title.tag != "p":
                failures.append("visible .evidence-title is not a <p>")
            if not title_text:
                failures.append("visible .evidence-title has no rendered text")
            if title.is_inside(figure):
                failures.append("visible .evidence-title is inside the native figure")

        labelledby = (shell.attributes.get("aria-labelledby") or "").split()
        if len(labelledby) != 1:
            failures.append("evidence figure must have exactly one aria-labelledby target")
        elif title is not None:
            title_id = (title.attributes.get("id") or "").strip()
            if not title_id:
                failures.append("visible .evidence-title has no id")
            elif labelledby[0] != title_id:
                failures.append("evidence figure aria-labelledby does not reference its title")
            targets = [
                candidate
                for candidate in visible
                if (candidate.attributes.get("id") or "").strip() == labelledby[0]
            ]
            if len(targets) != 1:
                failures.append(
                    "evidence figure aria-labelledby must resolve to exactly one visible element, "
                    f"found {len(targets)}"
                )
            elif targets[0] is not title:
                failures.append("evidence figure aria-labelledby resolves outside its title")

        observed.append((number_text, title_text))

    shells = [element for element in visible if "evidence-figure" in element.classes]
    unused_shells = [shell for shell in shells if id(shell) not in used_shells]
    if unused_shells:
        failures.append(f"unused visible .evidence-figure: {len(unused_shells)}")
    if len(used_shells) != len(
        [figure for figure in figures if _nearest_ancestor_with_class(figure, "evidence-figure")]
    ):
        failures.append("each framed figure must have its own .evidence-figure ancestor")

    complete = [item for item in observed if item is not None]
    observed_numbers = [number for number, _ in complete if number]
    if len(observed_numbers) != len(set(observed_numbers)):
        failures.append("evidence figure numbers must be unique within chapter")
    observed_titles = [title for _, title in complete if title]
    if len(observed_titles) != len(set(observed_titles)):
        failures.append("evidence figure titles must be unique within chapter")

    if expected is not None:
        for index, (expected_number, expected_title) in enumerate(expected):
            if index >= len(observed):
                continue
            observed_item = observed[index]
            if observed_item is None:
                continue
            actual_number, actual_title = observed_item
            number_label = f"圖 {expected_number}"
            if actual_number and actual_number != number_label:
                failures.append(
                    f"analytical figure {index + 1} number must be {number_label!r}, "
                    f"found {actual_number!r}"
                )
            if actual_title and actual_title != expected_title:
                failures.append(
                    f"analytical figure {index + 1} title must be {expected_title!r}, "
                    f"found {actual_title!r}"
                )

    return failures


def analytical_figure_failures_for(
    page: pathlib.Path,
    expected: tuple[tuple[str, str], ...] | None = None,
) -> list[str]:
    return analytical_figure_failures_for_text(page.read_text(encoding="utf-8"), expected)


def heading_outline_failures_for_text(html: str) -> list[str]:
    parser = StructureParser()
    parser.feed(html)
    parser.close()
    parser.finish()
    failures = list(parser.errors)
    headings = [
        element
        for element in parser.elements
        if element.visible and element.tag in {f"h{level}" for level in range(1, 7)}
    ]
    for previous, current in itertools.pairwise(headings):
        previous_level = int(previous.tag[1])
        current_level = int(current.tag[1])
        if current_level > previous_level + 1:
            failures.append(f"heading level jumps from <{previous.tag}> to <{current.tag}>")
    return failures


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

    chapter_groups = [element for element in visible if "data-chapter-group" in element.attributes]
    if len(chapter_groups) != 3:
        failures.append(
            f"expected exactly three visible chapter intent groups, found {len(chapter_groups)}"
        )

    chapter_links = [
        element
        for element in visible
        if element.tag == "a" and "data-chapter-index-link" in element.attributes
    ]
    if len(chapter_links) != 10:
        failures.append(
            f"expected exactly ten visible chapter index links, found {len(chapter_links)}"
        )
    elif any(not any(link.is_inside(group) for group in chapter_groups) for link in chapter_links):
        failures.append("every visible chapter index link must belong to an intent group")

    chapter_destinations = []
    for link in chapter_links:
        destination = _site_destination_from_href(link.attributes.get("href"))
        chapter_destinations.append(destination[1] if destination else None)
    if tuple(chapter_destinations) != REQUIRED_CHAPTER_INDEX_DESTINATIONS:
        found = ", ".join(str(destination) for destination in chapter_destinations)
        failures.append(f"chapter index destinations must match canonical order; found {found}")

    return failures


def home_failures_for(page: pathlib.Path) -> list[str]:
    return home_failures_for_text(page.read_text(encoding="utf-8"))


def _unquote_rounds(value: str) -> list[str]:
    rounds = [value]
    while True:
        decoded = unquote(rounds[-1])
        if decoded == rounds[-1]:
            return rounds
        rounds.append(decoded)


def _site_destination_from_href(href: str | None) -> tuple[str, str] | None:
    if not href:
        return None
    href = href.strip(URL_C0_AND_SPACE)
    href = href.replace("\t", "").replace("\n", "").replace("\r", "")
    if not href:
        return None
    for decoded_href in _unquote_rounds(href):
        if decoded_href.replace("\\", "/").startswith("//"):
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
        segment = raw_segment
        for decoded_segment in _unquote_rounds(raw_segment):
            normalized_segment = decoded_segment.replace("\\", "/")
            if normalized_segment in {".", ".."} or "/" in normalized_segment:
                return None
            segment = decoded_segment
        segments.append(segment)
    if not segments:
        return None
    base = f"/{'/'.join(segments[:-1])}" if len(segments) > 1 else ""
    return base, f"/{segments[-1]}/"


def _run_preflight() -> None:
    url_controls = [
        " ///trend/",
        "\t///trend/",
        "\r\n///trend/",
        "/\t//host/trend/",
        "/\n//host/trend/",
        "/\r//host/trend/",
        "/%09///trend/",
        "/%09//host/trend/",
    ]
    expected_url_results = [
        ["https://trend", "/"],
        ["https://trend", "/"],
        ["https://trend", "/"],
        ["https://host", "/trend/"],
        ["https://host", "/trend/"],
        ["https://host", "/trend/"],
        ["https://local.invalid", "/%09///trend/"],
        ["https://local.invalid", "/%09//host/trend/"],
    ]
    url_results = _whatwg_url_results(url_controls)
    if url_results != expected_url_results:
        raise RuntimeError(
            "WHATWG URL preflight no longer distinguishes literal C0 prefixes "
            f"from percent-encoded path data: {url_results}"
        )

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

    valid_outline = "<main><h1>Page</h1><h2>Index</h2><h3>Group</h3></main>"
    if heading_outline_failures_for_text(valid_outline):
        raise RuntimeError("heading-outline preflight rejected the valid control")
    jump_failures = heading_outline_failures_for_text(
        valid_outline.replace("<h2>Index</h2>", "<h3>Index</h3>")
    )
    if not any("jumps from <h1> to <h3>" in failure for failure in jump_failures):
        raise RuntimeError("heading-outline preflight accepted a skipped level")

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
        '<div class="chapter-thesis"><strong>Thesis</strong></div>'
        "</header><h2>Evidence</h2>"
    )
    valid_failures = failures_for_text(valid_html)
    if valid_failures:
        raise RuntimeError(f"HTML preflight rejected the valid control: {valid_failures}")
    required_fragment_failures = failures_for_text(valid_html, ("Correction",))
    if required_fragment_failures != [
        "chapter thesis is missing required finding fragment: Correction"
    ]:
        raise RuntimeError(
            "HTML preflight did not isolate a missing required thesis finding: "
            f"{required_fragment_failures}"
        )

    mutations = {
        "missing thesis": (
            "missing visible .chapter-thesis",
            '<header class="chapter-intro"><h1>Title</h1></header><h2>Evidence</h2>',
        ),
        "empty thesis": (
            "visible .chapter-thesis has no rendered text",
            '<header class="chapter-intro"><h1>Title</h1>'
            '<div class="chapter-thesis"><span hidden>Hidden</span></div>'
            "</header><h2>Evidence</h2>",
        ),
        "duplicated thesis": (
            "expected exactly one visible .chapter-thesis, found 2",
            '<header class="chapter-intro"><h1>Title</h1>'
            '<div class="chapter-thesis">Thesis</div>'
            '<div class="chapter-thesis">Copy</div></header><h2>Evidence</h2>',
        ),
        "nested thesis": (
            "visible .chapter-thesis is not a direct child of header.chapter-intro",
            '<header class="chapter-intro"><h1>Title</h1>'
            '<div><div class="chapter-thesis">Thesis</div></div>'
            "</header><h2>Evidence</h2>",
        ),
        "thesis before h1": (
            "chapter thesis must follow chapter <h1>",
            '<header class="chapter-intro"><div class="chapter-thesis">Thesis</div>'
            "<h1>Title</h1></header><h2>Evidence</h2>",
        ),
        "nested headings": (
            "nested heading <h2> inside <h1>",
            '<header class="chapter-intro"><h1>Title<h2>Nested</h2></h1>'
            '<div class="chapter-thesis">Thesis</div></header><h2>Evidence</h2>',
        ),
        "mismatched headings": (
            "mismatched closing </h2> while <h1> is open",
            valid_html.replace("</h1>", "</h2>", 1),
        ),
    }
    wrapped_contracts = {
        "h1": '<div><h1>Title</h1></div><div class="chapter-thesis">Thesis</div>',
    }
    for class_name, markup in wrapped_contracts.items():
        selector = f"<{class_name}>"
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

    valid_trend_map = """
<header class="chapter-intro"><h1>趨勢</h1><div class="chapter-thesis">結論</div>
<nav aria-label="本章閱讀地圖" data-chapter-reading-map><ol>
<li><a data-chapter-reading-link href="#evidence-1-1-title">固定測站後，下降是否仍成立？</a></li>
<li><a data-chapter-reading-link href="#trend-weather-adjustment">排除天氣後，下降幅度剩多少？</a></li>
<li><a data-chapter-reading-link href="#trend-airzones">各空品區是否同步改善？</a></li>
</ol></nav></header>
<section data-primary-evidence><p id="evidence-1-1-title">圖一</p></section>
<h2 id="trend-weather-adjustment">天氣</h2><h2 id="trend-airzones">空品區</h2>
"""
    valid_trend_failures = trend_reading_map_failures_for_text(valid_trend_map)
    if valid_trend_failures:
        raise RuntimeError(
            f"trend reading-map preflight rejected the valid control: {valid_trend_failures}"
        )

    first_link = (
        '<li><a data-chapter-reading-link href="#evidence-1-1-title">'
        "固定測站後，下降是否仍成立？</a></li>"
    )
    second_link = (
        '<li><a data-chapter-reading-link href="#trend-weather-adjustment">'
        "排除天氣後，下降幅度剩多少？</a></li>"
    )
    third_link = (
        '<li><a data-chapter-reading-link href="#trend-airzones">各空品區是否同步改善？</a></li>'
    )
    trend_mutations = {
        "missing link": (
            "trend reading-map links changed",
            valid_trend_map.replace(third_link, "", 1),
        ),
        "non-anchor marker": (
            "trend reading-map links changed",
            valid_trend_map.replace(
                first_link,
                first_link.replace("<a ", "<span ", 1).replace("</a>", "</span>", 1),
                1,
            ),
        ),
        "unmarked fourth anchor": (
            "trend reading-map links changed",
            valid_trend_map.replace(
                "</ol>", '<li><a href="#trend-airzones">額外連結</a></li></ol>', 1
            ),
        ),
        "duplicate target": (
            "target inventory changed",
            valid_trend_map.replace('id="trend-airzones"', 'id="trend-weather-adjustment"', 1),
        ),
        "reordered links": (
            "trend reading-map links changed",
            valid_trend_map.replace(
                second_link + "\n" + third_link, third_link + "\n" + second_link, 1
            ),
        ),
        "hidden target": (
            "target inventory changed",
            valid_trend_map.replace(
                '<h2 id="trend-airzones">', '<h2 id="trend-airzones" hidden>', 1
            ),
        ),
        "reordered targets": (
            "target order changed",
            valid_trend_map.replace(
                '<h2 id="trend-weather-adjustment">天氣</h2><h2 id="trend-airzones">空品區</h2>',
                '<h2 id="trend-airzones">空品區</h2><h2 id="trend-weather-adjustment">天氣</h2>',
                1,
            ),
        ),
        "primary before map": (
            "primary evidence precedes the reading map",
            valid_trend_map.replace(
                '<nav aria-label="本章閱讀地圖" data-chapter-reading-map><ol>',
                '<section data-primary-evidence><p id="evidence-1-1-title">圖一</p></section>'
                '<nav aria-label="本章閱讀地圖" data-chapter-reading-map><ol>',
                1,
            ).replace(
                "</header>\n<section data-primary-evidence>"
                '<p id="evidence-1-1-title">圖一</p></section>',
                "</header>",
                1,
            ),
        ),
    }
    for name, (expected_failure, html) in trend_mutations.items():
        if html == valid_trend_map:
            raise RuntimeError(f"trend reading-map preflight did not apply {name}")
        mutation_failures = trend_reading_map_failures_for_text(html)
        if not any(expected_failure in failure for failure in mutation_failures):
            raise RuntimeError(
                f"trend reading-map preflight did not reject {name} for the expected reason: "
                f"{mutation_failures}"
            )

    detection_preflight_misses: list[str] = []
    valid_detection_payload = json.loads(DETECTION_LIMIT.read_text(encoding="utf-8"))
    invalid_payload_mutations: dict[str, tuple[str, Callable[[Any], object]]] = {
        "missing event inventory": (
            "event inventory changed",
            lambda payload: payload["events"].pop(),
        ),
        "extra event inventory": (
            "event inventory changed",
            lambda payload: payload["events"].append({**payload["events"][0], "event": "額外事件"}),
        ),
        "reordered event inventory": (
            "event identity/order changed",
            lambda payload: payload["events"].reverse(),
        ),
        "wrong event identity": (
            "event identity/order changed",
            lambda payload: payload["events"][0].__setitem__("event", "錯誤事件"),
        ),
        "wrong event kind": (
            "event kind changed",
            lambda payload: payload["events"][2].__setitem__("kind", "window"),
        ),
        "negative observed count": (
            "observed count is invalid",
            lambda payload: payload["events"][0].__setitem__("n_credible", -1),
        ),
        "negative expected count": (
            "expected count is invalid",
            lambda payload: payload["events"][0].__setitem__("n_expected_by_chance", -0.1),
        ),
        "below-chance relationship no longer holds": (
            "no longer supports the below-chance claim",
            lambda payload: payload["events"][0].__setitem__("n_credible", 4),
        ),
    }
    for name, (expected_error, mutate) in invalid_payload_mutations.items():
        payload = json.loads(json.dumps(valid_detection_payload))
        mutate(payload)
        try:
            _detection_expected_events_from_payload(payload)
        except ValueError as exc:
            if expected_error not in str(exc):
                detection_preflight_misses.append(f"payload {name} misdiagnosed as {exc!s}")
        else:
            detection_preflight_misses.append(f"payload {name} accepted")

    expected_detection_events = (
        DetectionExpectedEvent("COVID-19 全國三級警戒", "window", 1, 3.3),
        DetectionExpectedEvent("台中電廠 2、3 號機生煤許可爭議", "window", 1, 3.3),
        DetectionExpectedEvent("2018 空氣污染防制法修正", "trend_break", 1, 3.3),
    )
    kind_contract = (
        ("window", "窗口事件：觀測－預測差額"),
        ("window", "窗口事件：觀測－預測差額"),
        ("trend_break", "趨勢斷點：斜率差"),
    )

    def detection_event_row(
        event: DetectionExpectedEvent,
        kind: str,
        kind_label: str,
    ) -> str:
        return (
            f'<div data-detection-event="{event.event}" data-detection-kind="{kind}" '
            f'data-detection-observed="{event.n_credible}" '
            f'data-detection-expected="{event.n_expected_by_chance}">'
            f"<dt>{event.event} · {kind_label}</dt>"
            f"<dd>實際通過 {event.n_credible} 站；"
            f"純靠機率的預期為 {event.n_expected_by_chance} 站。</dd></div>"
        )

    event_rows = [
        detection_event_row(event, kind, label)
        for event, (kind, label) in zip(expected_detection_events, kind_contract, strict=True)
    ]
    valid_detection_brief = f"""
<main>
<section class="evidence-figure"><header><p class="evidence-number">圖 5.1</p><p class="evidence-title">事件估計值能否離開安慰劑散布？</p></header>
<figure>
<ol data-detection-reading-key><li data-detection-reading-step="placebo">先看灰線：沒有事件標記時，同一程序仍會算出的差額。</li><li data-detection-reading-step="event">再看橘點：事件窗口各測站的觀測－預測差額。</li><li data-detection-reading-step="threshold">最後看門檻：通過數是否高於純靠機率的預期。</li></ol>
<div data-primary-plot></div>
<figcaption>Figure 5.1 caption</figcaption>
</figure></section>
<dl data-detection-comparison>
{chr(10).join(event_rows)}
</dl>
<aside data-detection-inference-boundary>「測不到」不等於「等於零」。每個事件的實際通過數都低於各自純靠機率的預期。這個方法在這些日曆窗口的噪音底線是 2.5–3.5 μg/m³，而待測的效應量是 0.5–1.6 μg/m³。噪音底線高於訊號。這批資料與這個方法，無法分辨這種大小的效應——不是「這些事件沒有影響」。非偵測不是「事件沒有發生」或「介入無效」的證明。本分析<strong>沒有驗證機組的逐時操作或燃料狀態</strong>，因此無法區分「介入沒有依事件標籤發生」、「介入發生但環境訊號太小」，或「模型與測站配置無法辨識」。這三種情況都與目前的非偵測相容。</aside>
<p data-detection-method-evidence>First method evidence</p>
<table data-detection-results><tr><td>Later evidence</td></tr></table>
</main>
"""
    valid_detection_failures = detection_limitation_brief_failures_for_text(
        valid_detection_brief, expected_detection_events
    )
    if valid_detection_failures:
        raise RuntimeError(
            "detection limitation brief preflight rejected the valid control: "
            f"{valid_detection_failures}"
        )

    def move_boundary_after(html: str, marker: str) -> str:
        start = html.index("<aside data-detection-inference-boundary>")
        end = html.index("</aside>", start) + len("</aside>")
        boundary = html[start:end]
        without_boundary = html[:start] + html[end:]
        return without_boundary.replace(marker, f"{marker}{boundary}", 1)

    def move_opening_pair_after(html: str, marker: str) -> str:
        start = html.index("<dl data-detection-comparison>")
        end = html.index("</aside>", start) + len("</aside>")
        pair = html[start:end]
        without_pair = html[:start] + html[end:]
        return without_pair.replace(marker, f"{marker}{pair}", 1)

    def wrap_detection_region(
        html: str,
        start_marker: str,
        end_marker: str,
        *,
        opened: bool,
    ) -> str:
        start = html.index(start_marker)
        end = html.index(end_marker, start) + len(end_marker)
        details = "<details open>" if opened else "<details>"
        return html[:start] + details + html[start:end] + "</details>" + html[end:]

    detection_mutations = {
        "visible plus hidden Figure 5.1 title": (
            "detection Figure 5.1 title changed",
            valid_detection_brief.replace(
                "</p></header>",
                '</p><p class="evidence-title" hidden>Copy</p></header>',
                1,
            ),
        ),
        "missing reading key": (
            "detection reading key inventory changed",
            valid_detection_brief.replace(" data-detection-reading-key", "", 1),
        ),
        "duplicate reading key": (
            "detection reading key inventory changed",
            valid_detection_brief.replace(
                "</ol>\n<div data-primary-plot>",
                "</ol><ol data-detection-reading-key></ol>\n<div data-primary-plot>",
                1,
            ),
        ),
        "missing reading step": (
            "detection reading-step inventory changed",
            valid_detection_brief.replace(' data-detection-reading-step="threshold"', "", 1),
        ),
        "replaced reading step text": (
            "detection reading step text changed",
            valid_detection_brief.replace("先看灰線", "先看別的", 1),
        ),
        "reordered reading steps": (
            "detection reading-step inventory changed",
            valid_detection_brief.replace(
                '<li data-detection-reading-step="event">再看橘點：事件窗口各測站的觀測－預測差額。</li><li data-detection-reading-step="threshold">最後看門檻：通過數是否高於純靠機率的預期。</li>',
                '<li data-detection-reading-step="threshold">最後看門檻：通過數是否高於純靠機率的預期。</li><li data-detection-reading-step="event">再看橘點：事件窗口各測站的觀測－預測差額。</li>',
                1,
            ),
        ),
        "reading key after primary plot": (
            "detection opening order changed",
            valid_detection_brief.replace(
                '<ol data-detection-reading-key><li data-detection-reading-step="placebo">先看灰線：沒有事件標記時，同一程序仍會算出的差額。</li><li data-detection-reading-step="event">再看橘點：事件窗口各測站的觀測－預測差額。</li><li data-detection-reading-step="threshold">最後看門檻：通過數是否高於純靠機率的預期。</li></ol>\n<div data-primary-plot></div>',
                '<div data-primary-plot></div>\n<ol data-detection-reading-key><li data-detection-reading-step="placebo">先看灰線：沒有事件標記時，同一程序仍會算出的差額。</li><li data-detection-reading-step="event">再看橘點：事件窗口各測站的觀測－預測差額。</li><li data-detection-reading-step="threshold">最後看門檻：通過數是否高於純靠機率的預期。</li></ol>',
                1,
            ),
        ),
        "missing comparison": (
            "detection comparison inventory changed",
            valid_detection_brief.replace(" data-detection-comparison", "", 1),
        ),
        "missing event": (
            "detection event inventory changed",
            valid_detection_brief.replace(f"{event_rows[2]}\n", "", 1),
        ),
        "duplicate event": (
            "detection event inventory changed",
            valid_detection_brief.replace(event_rows[2], event_rows[2] * 2, 1),
        ),
        "extra event": (
            "detection event inventory changed",
            valid_detection_brief.replace(
                "</dl>",
                '<div data-detection-event="extra" data-detection-kind="window" '
                'data-detection-observed="1" data-detection-expected="3.3">'
                "<dt>extra · 窗口事件：觀測－預測差額</dt><dd>實際通過 1 站；"
                "純靠機率的預期為 3.3 站。</dd></div></dl>",
                1,
            ),
        ),
        "reordered events": (
            "detection event inventory changed",
            valid_detection_brief.replace(
                f"{event_rows[1]}\n{event_rows[2]}",
                f"{event_rows[2]}\n{event_rows[1]}",
                1,
            ),
        ),
        "changed exact event identity": (
            "detection event inventory changed",
            valid_detection_brief.replace("COVID-19 全國三級警戒", "COVID-19 三級警戒", 1),
        ),
        "changed observed": (
            "detection event observed value changed",
            valid_detection_brief.replace(
                'data-detection-observed="1"', 'data-detection-observed="2"', 1
            ),
        ),
        "conflicting duplicate observed attribute": (
            "duplicate HTML attribute",
            valid_detection_brief.replace(
                'data-detection-observed="1"',
                'data-detection-observed="2" data-detection-observed="1"',
                1,
            ),
        ),
        "changed expected": (
            "detection event expected value changed",
            valid_detection_brief.replace(
                'data-detection-expected="3.3"', 'data-detection-expected="4"', 1
            ),
        ),
        "wrong rendered event kind": (
            "detection event kind changed",
            valid_detection_brief.replace(
                'data-detection-kind="trend_break"', 'data-detection-kind="window"', 1
            ),
        ),
        "unhooked extra semantic row": (
            "detection semantic-row inventory changed",
            valid_detection_brief.replace(
                "</dl>",
                "<div><dt>額外說明</dt><dd>實際通過 9 站。</dd></div></dl>",
                1,
            ),
        ),
        "malformed direct description pair": (
            "detection event description structure changed",
            valid_detection_brief.replace("<dd>實際通過 1 站", "<p>實際通過 1 站", 1).replace(
                "站。</dd></div>", "站。</p></div>", 1
            ),
        ),
        "conflicting event copy": (
            "detection event accessible text changed",
            valid_detection_brief.replace(
                "純靠機率的預期為 3.3 站。</dd>",
                "純靠機率的預期為 3.3 站。實際通過 9 站。</dd>",
                1,
            ),
        ),
        "event value elsewhere": (
            "detection event accessible text changed",
            valid_detection_brief.replace(
                "純靠機率的預期為 3.3 站。</dd>",
                "</dd>",
                1,
            ).replace(
                "<p data-detection-method-evidence>First method evidence</p>",
                "<p>純靠機率的預期為 3.3 站。</p>"
                "<p data-detection-method-evidence>First method evidence</p>",
                1,
            ),
        ),
        "missing boundary": (
            "detection inference-boundary inventory changed",
            valid_detection_brief.replace(" data-detection-inference-boundary", "", 1),
        ),
        "duplicate boundary": (
            "detection inference-boundary inventory changed",
            valid_detection_brief.replace(
                "</aside>", "</aside><aside data-detection-inference-boundary>Copy</aside>", 1
            ),
        ),
        "hidden boundary": (
            "detection inference-boundary inventory changed",
            valid_detection_brief.replace(
                "data-detection-inference-boundary>",
                "data-detection-inference-boundary hidden>",
                1,
            ),
        ),
        "visible plus hidden boundary": (
            "detection inference-boundary inventory changed",
            valid_detection_brief.replace(
                "</aside>",
                "</aside><aside data-detection-inference-boundary hidden>Copy</aside>",
                1,
            ),
        ),
        "weakened inference claim": (
            "detection inference boundary claim changed",
            valid_detection_brief.replace("噪音底線高於訊號。", "噪音底線接近訊號。", 1),
        ),
        "missing below-chance inference claim": (
            "detection inference boundary claim changed",
            valid_detection_brief.replace("每個事件的實際通過數都低於各自純靠機率的預期。", "", 1),
        ),
        "missing event-occurrence inference claim": (
            "detection inference boundary claim changed",
            valid_detection_brief.replace(
                "非偵測不是「事件沒有發生」或「介入無效」的證明。", "", 1
            ),
        ),
        "below-chance inference claim outside boundary": (
            "detection inference boundary claim changed",
            valid_detection_brief.replace(
                "每個事件的實際通過數都低於各自純靠機率的預期。", "", 1
            ).replace(
                "<p data-detection-method-evidence>",
                "<p>每個事件的實際通過數都低於各自純靠機率的預期。</p>"
                "<p data-detection-method-evidence>",
                1,
            ),
        ),
        "event-occurrence inference claim outside boundary": (
            "detection inference boundary claim changed",
            valid_detection_brief.replace(
                "非偵測不是「事件沒有發生」或「介入無效」的證明。", "", 1
            ).replace(
                "<p data-detection-method-evidence>",
                "<p>非偵測不是「事件沒有發生」或「介入無效」的證明。</p>"
                "<p data-detection-method-evidence>",
                1,
            ),
        ),
        "inference claim outside boundary": (
            "detection inference boundary claim changed",
            valid_detection_brief.replace("噪音底線高於訊號。", "", 1).replace(
                "<table data-detection-results>",
                "<p>噪音底線高於訊號。</p><table data-detection-results>",
                1,
            ),
        ),
        "boundary after later evidence": (
            "detection opening order changed",
            move_boundary_after(
                valid_detection_brief,
                "<table data-detection-results><tr><td>Later evidence</td></tr></table>",
            ),
        ),
        "boundary after method evidence": (
            "detection comparison and inference boundary are not adjacent",
            move_boundary_after(
                valid_detection_brief,
                "<p data-detection-method-evidence>First method evidence</p>",
            ),
        ),
        "comparison and boundary after method evidence": (
            "detection opening order changed",
            move_opening_pair_after(
                valid_detection_brief,
                "<p data-detection-method-evidence>First method evidence</p>",
            ),
        ),
        "open disclosure around reading key": (
            "detection reading key is user-collapsible",
            wrap_detection_region(
                valid_detection_brief,
                "<ol data-detection-reading-key>",
                "</ol>",
                opened=True,
            ),
        ),
        "closed disclosure around reading key": (
            "detection reading key is user-collapsible",
            wrap_detection_region(
                valid_detection_brief,
                "<ol data-detection-reading-key>",
                "</ol>",
                opened=False,
            ),
        ),
        "open disclosure around comparison": (
            "detection comparison is user-collapsible",
            wrap_detection_region(
                valid_detection_brief,
                "<dl data-detection-comparison>",
                "</dl>",
                opened=True,
            ),
        ),
        "closed disclosure around comparison": (
            "detection comparison is user-collapsible",
            wrap_detection_region(
                valid_detection_brief,
                "<dl data-detection-comparison>",
                "</dl>",
                opened=False,
            ),
        ),
        "open disclosure around boundary": (
            "detection inference boundary is user-collapsible",
            wrap_detection_region(
                valid_detection_brief,
                "<aside data-detection-inference-boundary>",
                "</aside>",
                opened=True,
            ),
        ),
        "closed disclosure around boundary": (
            "detection inference boundary is user-collapsible",
            wrap_detection_region(
                valid_detection_brief,
                "<aside data-detection-inference-boundary>",
                "</aside>",
                opened=False,
            ),
        ),
        "stale Figure 5.1 title": (
            "detection Figure 5.1 title changed",
            valid_detection_brief.replace("事件估計值能否離開安慰劑散布？", "舊標題", 1),
        ),
    }
    for name, (expected_failure, html) in detection_mutations.items():
        if html == valid_detection_brief:
            raise RuntimeError(f"detection limitation brief preflight did not apply {name}")
        mutation_failures = detection_limitation_brief_failures_for_text(
            html, expected_detection_events
        )
        if not any(failure.startswith(expected_failure) for failure in mutation_failures):
            detection_preflight_misses.append(
                f"markup {name} -> {expected_failure} (received {mutation_failures})"
            )
    if detection_preflight_misses:
        raise RuntimeError(
            "detection limitation brief preflight misses: " + "; ".join(detection_preflight_misses)
        )

    def evidence_shell(number: str, title: str, body: str = "Chart") -> str:
        title_id = f"evidence-{number.replace('.', '-')}-title"
        return (
            f'<section class="evidence-figure" aria-labelledby="{title_id}">'
            '<header class="evidence-header">'
            f'<p class="evidence-number">圖 {number}</p>'
            f'<p class="evidence-title" id="{title_id}">{title}</p>'
            f"</header><figure>{body}</figure></section>"
        )

    expected_evidence = (
        ("1.1", "Question one"),
        ("1.2", "Question two"),
        ("1.3", "Question three"),
    )
    first_evidence_shell = evidence_shell(*expected_evidence[0])
    valid_evidence = "".join(evidence_shell(*item) for item in expected_evidence)
    valid_evidence_failures = analytical_figure_failures_for_text(valid_evidence, expected_evidence)
    if valid_evidence_failures:
        raise RuntimeError(
            f"analytical figure preflight rejected the valid control: {valid_evidence_failures}"
        )
    ignored_figure_failures = analytical_figure_failures_for_text(
        valid_evidence
        + "<figure hidden>Hidden chart</figure>"
        + "<template><figure>Template chart</figure></template>",
        expected_evidence,
    )
    if ignored_figure_failures:
        raise RuntimeError(
            "analytical figure preflight counted hidden or template figures: "
            f"{ignored_figure_failures}"
        )

    evidence_mutations = {
        "unframed figure": (
            "unframed figure 1",
            "<figure>Chart</figure>"
            + evidence_shell(*expected_evidence[1])
            + evidence_shell(*expected_evidence[2]),
        ),
        "hidden evidence shell": (
            "expected exactly 3 visible analytical figures, found 2",
            valid_evidence.replace(
                '<section class="evidence-figure"',
                '<section class="evidence-figure" hidden',
                1,
            ),
        ),
        "template evidence shell": (
            "expected exactly 3 visible analytical figures, found 2",
            valid_evidence.replace(
                first_evidence_shell,
                f"<template>{first_evidence_shell}</template>",
                1,
            ),
        ),
        "non-section evidence shell": (
            "figure evidence ancestor is not a <section>",
            valid_evidence.replace("<section ", "<div ", 1).replace(
                "</figure></section>", "</figure></div>", 1
            ),
        ),
        "hidden number": (
            "evidence figure has no visible .evidence-number",
            valid_evidence.replace('class="evidence-number"', 'class="evidence-number" hidden', 1),
        ),
        "template number": (
            "evidence figure has no visible .evidence-number",
            valid_evidence.replace(
                '<p class="evidence-number">圖 1.1</p>',
                '<template><p class="evidence-number">圖 1.1</p></template>',
                1,
            ),
        ),
        "empty number": (
            "visible .evidence-number has no rendered text",
            valid_evidence.replace("圖 1.1</p>", "</p>", 1),
        ),
        "number outside its shell": (
            "evidence figure has no visible .evidence-number",
            valid_evidence.replace(
                '<section class="evidence-figure" aria-labelledby="evidence-1-1-title">'
                '<header class="evidence-header"><p class="evidence-number">圖 1.1</p>',
                '<p class="evidence-number">圖 1.1</p>'
                '<section class="evidence-figure" aria-labelledby="evidence-1-1-title">'
                '<header class="evidence-header">',
                1,
            ),
        ),
        "hidden title": (
            "evidence figure has no visible .evidence-title",
            valid_evidence.replace('class="evidence-title"', 'class="evidence-title" hidden', 1),
        ),
        "template title": (
            "evidence figure has no visible .evidence-title",
            valid_evidence.replace(
                '<p class="evidence-title" id="evidence-1-1-title">Question one</p>',
                '<template><p class="evidence-title" '
                'id="evidence-1-1-title">Question one</p></template>',
                1,
            ),
        ),
        "empty title": (
            "visible .evidence-title has no rendered text",
            valid_evidence.replace(">Question one</p>", "></p>", 1),
        ),
        "title outside its shell": (
            "evidence figure has no visible .evidence-title",
            valid_evidence.replace(
                '<section class="evidence-figure" aria-labelledby="evidence-1-1-title">'
                '<header class="evidence-header"><p class="evidence-number">圖 1.1</p>'
                '<p class="evidence-title" id="evidence-1-1-title">Question one</p></header>',
                '<p class="evidence-title" id="evidence-1-1-title">Question one</p>'
                '<section class="evidence-figure" aria-labelledby="evidence-1-1-title">'
                '<header class="evidence-header"><p class="evidence-number">圖 1.1</p></header>',
                1,
            ),
        ),
        "figure below an intervening wrapper": (
            "figure is not a direct child of its nearest .evidence-figure ancestor",
            valid_evidence.replace(
                "<figure>Chart</figure>", "<div><figure>Chart</figure></div>", 1
            ),
        ),
        "duplicate title node": (
            "evidence figure must contain exactly one visible .evidence-title, found 2",
            valid_evidence.replace(
                '<p class="evidence-title" id="evidence-1-1-title">Question one</p>',
                '<p class="evidence-title" id="evidence-1-1-title">Question one</p>'
                '<p class="evidence-title">Question duplicate</p>',
                1,
            ),
        ),
        "duplicate number node": (
            "evidence figure must contain exactly one visible .evidence-number, found 2",
            valid_evidence.replace(
                '<p class="evidence-number">圖 1.1</p>',
                '<p class="evidence-number">圖 1.1</p><p class="evidence-number">圖 1.1 copy</p>',
                1,
            ),
        ),
        "duplicate chapter-local number": (
            "evidence figure numbers must be unique within chapter",
            valid_evidence.replace("圖 1.2</p>", "圖 1.1</p>", 1),
        ),
        "duplicate chapter-local title": (
            "evidence figure titles must be unique within chapter",
            valid_evidence.replace(">Question two</p>", ">Question one</p>", 1),
        ),
        "missing aria-labelledby": (
            "evidence figure must have exactly one aria-labelledby target",
            valid_evidence.replace(' aria-labelledby="evidence-1-1-title"', "", 1),
        ),
        "missing title id": (
            "visible .evidence-title has no id",
            valid_evidence.replace(' id="evidence-1-1-title"', "", 1),
        ),
        "aria-labelledby outside its shell": (
            "evidence figure aria-labelledby does not reference its title",
            '<p id="outside-title">Outside</p>'
            + valid_evidence.replace(
                'aria-labelledby="evidence-1-1-title"',
                'aria-labelledby="outside-title"',
                1,
            ),
        ),
        "duplicate aria-labelledby target": (
            "evidence figure aria-labelledby must resolve to exactly one visible element, found 2",
            '<span id="evidence-1-1-title">Duplicate</span>' + valid_evidence,
        ),
        "duplicate figure": (
            "evidence figure must contain exactly one native figure, found 2",
            valid_evidence.replace(
                "<figure>Chart</figure>", "<figure>Chart</figure><figure>Copy</figure>", 1
            ),
        ),
        "unused duplicate shell": (
            "unused visible .evidence-figure: 1",
            valid_evidence + '<section class="evidence-figure" aria-labelledby="copy-title">'
            '<p class="evidence-number">圖 1.4</p>'
            '<p class="evidence-title" id="copy-title">Copy</p></section>',
        ),
    }
    if not evidence_mutations:
        raise RuntimeError("analytical figure preflight has no negative controls")
    for name, (expected_failure, html) in evidence_mutations.items():
        mutation_failures = analytical_figure_failures_for_text(html, expected_evidence)
        if expected_failure not in mutation_failures:
            raise RuntimeError(
                f"analytical figure preflight did not reject {name} for the expected reason: "
                f"{mutation_failures}"
            )

    start_here_destinations = ("/trend/", "/stations/", "/methods/", "/explore/")
    start_here_links = "".join(
        f'<a href="{destination}">Path {index}</a>'
        for index, destination in enumerate(start_here_destinations, start=1)
    )
    chapter_index_groups = "".join(
        "<section data-chapter-group><h3>Intent</h3>"
        + "".join(
            f'<a href="{destination}" data-chapter-index-link>Chapter {index}</a>'
            for index, destination in indexed_destinations
        )
        + "</section>"
        for indexed_destinations in (
            tuple(enumerate(REQUIRED_CHAPTER_INDEX_DESTINATIONS[:4], start=1)),
            tuple(enumerate(REQUIRED_CHAPTER_INDEX_DESTINATIONS[4:7], start=5)),
            tuple(enumerate(REQUIRED_CHAPTER_INDEX_DESTINATIONS[7:], start=8)),
        )
    )
    valid_home = (
        '<h2 id="start-here-heading">Start here</h2>'
        '<nav class="start-here" aria-labelledby="start-here-heading">'
        f"{start_here_links}</nav>{chapter_index_groups}"
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
    valid_padded_home = valid_home.replace('href="/', 'href=" \t/').replace(
        '/">Path', '/ \r\n">Path'
    )
    valid_padded_home_failures = home_failures_for_text(valid_padded_home)
    if valid_padded_home_failures:
        raise RuntimeError(
            "home HTML preflight rejected the valid C0-padded control: "
            f"{valid_padded_home_failures}"
        )
    valid_encoded_tab_home = valid_home.replace('href="/', 'href="/%09///')
    valid_encoded_tab_home_failures = home_failures_for_text(valid_encoded_tab_home)
    if valid_encoded_tab_home_failures:
        raise RuntimeError(
            "home HTML preflight rejected the valid percent-encoded tab path control: "
            f"{valid_encoded_tab_home_failures}"
        )
    valid_embedded_encoded_tab_home = valid_home.replace('href="/', 'href="/%09//host/')
    valid_embedded_encoded_tab_home_failures = home_failures_for_text(
        valid_embedded_encoded_tab_home
    )
    if valid_embedded_encoded_tab_home_failures:
        raise RuntimeError(
            "home HTML preflight rejected the valid embedded percent-encoded tab path "
            f"control: {valid_embedded_encoded_tab_home_failures}"
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
        "missing chapter group": (
            "expected exactly three visible chapter intent groups, found 2",
            valid_home.replace("<section data-chapter-group>", "<section>", 1),
        ),
        "duplicate chapter": (
            "chapter index destinations must match canonical order",
            valid_home.replace(
                '<a href="/data/" data-chapter-index-link>Chapter 10</a>',
                '<a href="/trend/" data-chapter-index-link>Chapter 10</a>',
            ),
        ),
        "reordered chapters": (
            "chapter index destinations must match canonical order",
            valid_home.replace(
                '<a href="/trend/" data-chapter-index-link>Chapter 1</a>'
                '<a href="/stations/" data-chapter-index-link>Chapter 2</a>',
                '<a href="/stations/" data-chapter-index-link>Chapter 2</a>'
                '<a href="/trend/" data-chapter-index-link>Chapter 1</a>',
            ),
        ),
        "hidden chapter": (
            "expected exactly ten visible chapter index links, found 9",
            valid_home.replace(
                '<a href="/data/" data-chapter-index-link>Chapter 10</a>',
                '<a href="/data/" data-chapter-index-link hidden>Chapter 10</a>',
            ),
        ),
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
        "space-prefixed triple-slash network path": (
            "nav.start-here links must target ",
            valid_home.replace('href="/', 'href=" ///'),
        ),
        "tab-prefixed triple-slash network path": (
            "nav.start-here links must target ",
            valid_home.replace('href="/', 'href="\t///'),
        ),
        "line-break-prefixed triple-slash network path": (
            "nav.start-here links must target ",
            valid_home.replace('href="/', 'href="\r\n///'),
        ),
        "decimal-tab-entity-prefixed triple-slash network path": (
            "nav.start-here links must target ",
            valid_home.replace('href="/', 'href="&#9;///'),
        ),
        "hex-space-entity-prefixed triple-slash network path": (
            "nav.start-here links must target ",
            valid_home.replace('href="/', 'href="&#x20;///'),
        ),
        "embedded decimal-tab entity network path": (
            "nav.start-here links must target ",
            valid_home.replace('href="/', 'href="/&#9;//host/'),
        ),
        "embedded literal-tab network path": (
            "nav.start-here links must target ",
            valid_home.replace('href="/', 'href="/\t//host/'),
        ),
        "embedded literal-line-feed network path": (
            "nav.start-here links must target ",
            valid_home.replace('href="/', 'href="/\n//host/'),
        ),
        "embedded literal-carriage-return network path": (
            "nav.start-here links must target ",
            valid_home.replace('href="/', 'href="/\r//host/'),
        ),
        "embedded decimal-line-feed entity network path": (
            "nav.start-here links must target ",
            valid_home.replace('href="/', 'href="/&#10;//host/'),
        ),
        "embedded decimal-carriage-return entity network path": (
            "nav.start-here links must target ",
            valid_home.replace('href="/', 'href="/&#13;//host/'),
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

    not_found_page = dist / "404.html"
    if not not_found_page.exists():
        not_found_failures = [f"missing {not_found_page.relative_to(ROOT).as_posix()}"]
    else:
        not_found_failures = heading_outline_failures_for_text(
            not_found_page.read_text(encoding="utf-8")
        )
    for failure in not_found_failures:
        print(f"404: {failure}")

    failed_chapters = 0
    slugs = chapter_slugs()
    for slug in slugs:
        page = dist / slug / "index.html"
        if not page.exists():
            failures = [f"missing {page.relative_to(ROOT).as_posix()}"]
        else:
            html = page.read_text(encoding="utf-8")
            failures = failures_for_text(html, EXPECTED_THESIS_FRAGMENTS.get(slug, ()))
            failures.extend(
                analytical_figure_failures_for(page, EXPECTED_ANALYTICAL_FIGURES.get(slug))
            )
            if slug == "trend":
                failures.extend(trend_reading_map_failures_for_text(html))
            elif slug == "detection":
                try:
                    expected_events = load_detection_expected_events()
                except ValueError as exc:
                    failures.append(f"detection payload is invalid: {exc}")
                else:
                    failures.extend(
                        detection_limitation_brief_failures_for_text(html, expected_events)
                    )
            elif visible_reading_map_count(html):
                failures.append("chapter unexpectedly contains a visible trend reading map")

        if failures:
            failed_chapters += 1
            for failure in failures:
                print(f"{slug}: {failure}")

    print(f"chapters checked: {len(slugs)}")
    print(f"chapters with structure failures: {failed_chapters}")
    print(f"home with structure failures: {int(bool(home_failures))}")
    print(f"404 with structure failures: {int(bool(not_found_failures))}")
    return 1 if failed_chapters or home_failures or not_found_failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
