"""Check that every built chapter has the shared publication opening.

The chapter registry owns the route set.  This gate reads that set from the
TypeScript literal, then checks the HTML readers actually receive rather than
the Astro source that produced it.

    python scripts/check_publication_structure.py [web/dist]
"""

from __future__ import annotations

import copy
import hashlib
import itertools
import json
import math
import pathlib
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, cast
from unittest.mock import patch
from urllib.parse import unquote, urlsplit

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT = ROOT / "web" / "dist"
REGISTRY = ROOT / "web" / "src" / "lib" / "chapters.ts"
DETECTION_LIMIT = ROOT / "web" / "public" / "data" / "story" / "detection-limit.json"
FORECAST_STORY = ROOT / "web" / "public" / "data" / "story" / "forecast.json"
HEALTH_STORY = ROOT / "web" / "public" / "data" / "story" / "health.json"
DATA_INDEX = ROOT / "web" / "public" / "data" / "l0" / "index.json"
DATA_MANIFEST = ROOT / "web" / "public" / "data" / "manifest.json"
DATA_META = ROOT / "web" / "public" / "data" / "meta.json"
DATA_PUBLICATION = ROOT / "web" / "src" / "data" / "pages-publication.json"
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
        ("7.2", "比較基準造成的落差佔估計值多少？"),
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


def shown(path: pathlib.Path) -> str:
    """A path as a reader of the output can use it.

    Repo-relative when it is inside the repo, absolute when it is not.
    `relative_to` raises rather than falling back, and the usage line above
    offers a relative argument — so `python scripts/check_publication_structure.py web/dist` crashed with a
    `ValueError` while reporting, which is the worst moment to lose the report.
    """
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


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
    locally_visible: bool
    visible: bool
    start_order: int
    end_order: int | None = None
    text: list[str] = field(default_factory=list)
    source_text: list[str] = field(default_factory=list)
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

    def source_rendered_text(self) -> str:
        parts = [*self.source_text]
        for child in self.children:
            parts.append(child.source_rendered_text())
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
        locally_visible = (
            lowered not in IGNORED_SUBTREES
            and "hidden" not in attribute_names
            and (attributes.get("aria-hidden") or "").strip().lower() != "true"
            and not _hidden_by_inline_style(attributes.get("style"))
        )
        visible = (parent is None or parent.visible) and locally_visible

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
            locally_visible=locally_visible,
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
        if not self._stack:
            return
        self._stack[-1].source_text.append(data)
        if not self._stack[-1].visible:
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
SPACE_READING_MAP = (
    ("#space-distance", "距離增加後，殘差相依如何改變？"),
    ("#space-controls", "哪一種分層真正移除了大部分相依？"),
    ("#space-inference", "剩餘相依對推論與空白區預測有什麼代價？"),
)
SPACE_SUPPORTING_HEADINGS = (
    "官方分區比純地理多知道什麼？",
    "相依散在整個場，不在少數熱點",
    "把 t 統計量重新標價",
    "測站之間的空白能不能誠實地補？",
)

STATION_STAT_KEYS = ("annual-mean", "who-annual", "who-days", "taiwan-days")
STATION_COMPARISON_KEYS = ("rank", "worst-day")


def station_dossier_failures_for_text(html: str) -> list[str]:
    parser = StructureParser()
    parser.feed(html)
    parser.close()
    parser.finish()
    failures = list(parser.errors)
    elements = parser.elements

    pickers = [element for element in elements if "data-station-picker" in element.attributes]
    if len(pickers) != 1:
        return [*failures, f"station picker inventory changed: {len(pickers)}"]
    picker = pickers[0]

    controls = [
        element
        for element in elements
        if "data-station-controls" in element.attributes and element.is_inside(picker)
    ]
    if len(controls) != 1:
        failures.append(f"station controls inventory changed: {len(controls)}")
    selects = [
        element
        for element in elements
        if element.tag == "select"
        and element.attributes.get("id") == "station-select"
        and element.is_inside(picker)
    ]
    if len(selects) != 1:
        return [*failures, f"station select inventory changed: {len(selects)}"]
    select = selects[0]
    options = [
        element for element in elements if element.tag == "option" and element.is_inside(select)
    ]
    option_values = [element.attributes.get("value") or "" for element in options]
    if not option_values or any(not value for value in option_values):
        failures.append("station option values are empty")
    if len(set(option_values)) != len(option_values):
        failures.append("station option values are not unique")

    reports = [
        element
        for element in elements
        if "data-station-report" in element.attributes and element.is_inside(picker)
    ]
    report_values = [element.attributes.get("data-station") or "" for element in reports]
    if not report_values or any(not value for value in report_values):
        failures.append("station report identities are empty")
    if len(set(report_values)) != len(report_values):
        failures.append("station report identities are not unique")
    if option_values != report_values:
        failures.append("station selector and report order changed")

    selected_options = [element for element in options if "selected" in element.attributes]
    visible_reports = [element for element in reports if element.visible]
    if len(selected_options) != 1:
        failures.append(f"station selected-option inventory changed: {len(selected_options)}")
    if len(visible_reports) != 1:
        failures.append(f"station visible report inventory changed: {len(visible_reports)}")
    if len(selected_options) == 1 and len(visible_reports) == 1:
        selected_value = selected_options[0].attributes.get("value") or ""
        visible_value = visible_reports[0].attributes.get("data-station") or ""
        if selected_value != visible_value:
            failures.append("station selector and visible identity disagree")

    for report in reports:
        station = report.attributes.get("data-station") or "<empty>"
        identities = [
            element
            for element in elements
            if "data-station-identity" in element.attributes and element.is_inside(report)
        ]
        if len(identities) != 1:
            failures.append(f"station identity inventory changed for {station}: {len(identities)}")
        displayed_names = [
            element
            for element in elements
            if "data-station-name" in element.attributes
            and element.is_inside(report)
            and len(identities) == 1
            and element.is_inside(identities[0])
        ]
        if len(displayed_names) != 1:
            failures.append(
                f"station displayed-name inventory changed for {station}: {len(displayed_names)}"
            )
        elif not displayed_names[0].locally_visible:
            failures.append(f"station displayed name is locally hidden for {station}")
        elif displayed_names[0].source_rendered_text() != station:
            failures.append(f"station displayed identity disagrees for {station}")
        years = [
            element
            for element in elements
            if "data-station-year" in element.attributes and element.is_inside(report)
        ]
        if len(years) != 1:
            failures.append(f"station year inventory changed for {station}: {len(years)}")
        stats_wrappers = [
            element
            for element in elements
            if "data-station-stats" in element.attributes and element.is_inside(report)
        ]
        if len(stats_wrappers) != 1:
            failures.append(
                f"station primary-stat inventory changed for {station}: {len(stats_wrappers)}"
            )
        else:
            stat_keys = [
                element.attributes.get("data-station-stat") or ""
                for element in elements
                if "data-station-stat" in element.attributes
                and element.is_inside(stats_wrappers[0])
            ]
            if stat_keys != list(STATION_STAT_KEYS):
                failures.append(f"station primary-stat keys changed for {station}: {stat_keys!r}")
        comparison_wrappers = [
            element
            for element in elements
            if "data-station-comparisons" in element.attributes and element.is_inside(report)
        ]
        if len(comparison_wrappers) != 1:
            failures.append(
                f"station comparison inventory changed for {station}: {len(comparison_wrappers)}"
            )
        else:
            comparison_keys = [
                element.attributes.get("data-station-comparison") or ""
                for element in elements
                if "data-station-comparison" in element.attributes
                and element.is_inside(comparison_wrappers[0])
            ]
            if comparison_keys != list(STATION_COMPARISON_KEYS):
                failures.append(
                    f"station comparison keys changed for {station}: {comparison_keys!r}"
                )
        if (
            len(identities) == 1
            and len(years) == 1
            and len(stats_wrappers) == 1
            and len(comparison_wrappers) == 1
        ):
            identity = identities[0]
            year = years[0]
            stats = stats_wrappers[0]
            comparisons = comparison_wrappers[0]
            direct_children = (
                identity.parent is report
                and stats.parent is report
                and comparisons.parent is report
                and year.is_inside(identity)
            )
            ordered = (
                identity.end_order is not None
                and stats.end_order is not None
                and identity.end_order < stats.start_order
                and stats.end_order < comparisons.start_order
            )
            if not direct_children or not ordered:
                failures.append(f"station report source order changed for {station}")

    standard_notes = [
        element
        for element in elements
        if "data-station-standard-note" in element.attributes and element.is_inside(picker)
    ]
    conversion_notes = [
        element
        for element in elements
        if "data-station-conversion-note" in element.attributes and element.is_inside(picker)
    ]
    if len(standard_notes) != 1:
        failures.append(f"station standard-note inventory changed: {len(standard_notes)}")
    if conversion_notes:
        failures.append(f"station conversion-note returned: {len(conversion_notes)}")
    if reports and len(standard_notes) == 1:
        standard_note = standard_notes[0]
        final_report_end = max(report.end_order or report.start_order for report in reports)
        if standard_note.parent is not picker or standard_note.start_order <= final_report_end:
            failures.append("station interpretation notes do not follow reports")
    if any(
        element.visible
        and "data-chapter-reading-map" in element.attributes
        and element.is_inside(picker)
        for element in elements
    ):
        failures.append("station chapter unexpectedly contains a reading map")
    return failures


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
DETECTION_BOUNDARY_LOCAL_CLAIMS = (
    "每個事件的實際通過數都低於各自純靠機率的預期。",
    "非偵測不是「事件沒有發生」或「介入無效」的證明。",
)
DETECTION_LEGACY_BELOW_CHANCE_CLAIM = "三個事件的實際通過數都低於機率預期。"
DETECTION_BOUNDARY_CLAIMS = (
    "「測不到」不等於「等於零」。",
    DETECTION_BOUNDARY_LOCAL_CLAIMS[0],
    "這個方法在這些日曆窗口的噪音底線是 2.5–3.5 μg/m³，",
    "而待測的效應量是 0.5–1.6 μg/m³。",
    "噪音底線高於訊號。",
    "這批資料與這個方法，無法分辨這種大小的效應",
    "不是「這些事件沒有影響」。",
    DETECTION_BOUNDARY_LOCAL_CLAIMS[1],
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


METHOD_CASE_ROWS = (
    ("01", "月平均抹掉了六成的變異", "#method-case-01"),
    ("02", "拿 PM10 預測 PM2.5", "#method-case-02"),
    ("03", "把風向當成 0 到 360 的普通數字", "#method-case-03"),
    ("04", "把及格標準調低，好讓資料通過檢定", "#method-case-04"),
    ("05", "NO + NO₂ = NOx，三個一起放進模型", "#method-case-05"),
    ("06", "只用模型學過的資料評斷它", "#method-case-06"),
    ("07", "用一句話處理掉所有缺漏值", "#method-case-07"),
)
METHOD_FIGURE_TITLES = (
    ("evidence-8-1-title", "月平均隱藏了多少逐時變異？", "01"),
    ("evidence-8-2-title", "不同補值方法對不同缺口長度付出什麼代價？", "07"),
)


def methods_case_index_failures_for_text(html: str) -> list[str]:
    parser = StructureParser()
    parser.feed(html)
    parser.close()
    parser.finish()
    failures = list(parser.errors)
    elements = parser.elements

    indexes = [
        element
        for element in elements
        if "data-method-case-index" in element.attributes and element.visible
    ]
    ordered_list: Element | None = None
    if len(indexes) != 1:
        failures.append(f"methods case index inventory changed: {len(indexes)}")
        index = None
    else:
        index = indexes[0]
        if _is_inside_disclosure(index):
            failures.append("methods case index is user-collapsible")
        children = [child for child in index.children if child.visible]
        if (
            index.tag != "nav"
            or index.attributes.get("aria-labelledby") != "method-case-index-title"
            or [child.tag for child in children] != ["h2", "ol"]
            or children[0].attributes.get("id") != "method-case-index-title"
            or children[0].rendered_text() != "七個案例索引"
        ):
            failures.append("methods case index semantics changed")
        else:
            ordered_list = children[1]
        label_targets = [
            element
            for element in elements
            if element.attributes.get("id") == "method-case-index-title"
        ]
        expected_label = children[0] if children and children[0].tag == "h2" else None
        if len(label_targets) != 1 or label_targets[0] is not expected_label:
            failures.append("methods case index label inventory changed")

    index_rows = list(ordered_list.children) if ordered_list is not None else []
    if len(index_rows) != len(METHOD_CASE_ROWS) or any(
        row.tag != "li" or not row.visible for row in index_rows
    ):
        failures.append(f"methods case index row structure changed: {len(index_rows)}")

    all_links = [element for element in elements if "data-method-case-link" in element.attributes]
    links = [element for element in all_links if element.visible]
    if len(links) != len(METHOD_CASE_ROWS) or links != all_links:
        failures.append(f"methods case link inventory changed: {len(links)}")
    observed_link_cases = [link.attributes.get("data-case") for link in links]
    if observed_link_cases != [row[0] for row in METHOD_CASE_ROWS]:
        failures.append(f"methods case link order changed: {observed_link_cases!r}")
    for link, (number, title, expected_href) in zip(links, METHOD_CASE_ROWS, strict=False):
        if (
            link.tag != "a"
            or index is None
            or not link.is_inside(index)
            or link.parent is None
            or link.parent.tag != "li"
            or link.parent not in index_rows
        ):
            failures.append("methods case link structure changed")
            continue
        if link.attributes.get("data-case") != number:
            failures.append("methods case link identity changed")
        if link.attributes.get("href") != expected_href:
            failures.append("methods case link destination changed")
        if link.rendered_text() != title:
            failures.append("methods case link text changed")
        children = link.children
        if (
            [child.tag for child in children] != ["span", "span"]
            or children[0].attributes.get("aria-hidden") != "true"
            or children[0].visible
            or children[0].source_rendered_text() != number
            or not children[1].visible
            or children[1].rendered_text() != title
        ):
            failures.append("methods case link structure changed")

    all_destinations = [element for element in elements if "data-method-case" in element.attributes]
    destinations = [element for element in all_destinations if element.visible]
    if len(destinations) != len(METHOD_CASE_ROWS) or destinations != all_destinations:
        failures.append(f"methods case destination inventory changed: {len(destinations)}")
    observed_destination_cases = [
        destination.attributes.get("data-method-case") for destination in destinations
    ]
    if observed_destination_cases != [row[0] for row in METHOD_CASE_ROWS]:
        failures.append(f"methods case destination order changed: {observed_destination_cases!r}")
    destination_by_case: dict[str, Element] = {}
    for case_element, (number, title, href) in zip(destinations, METHOD_CASE_ROWS, strict=False):
        expected_id = href.removeprefix("#")
        if case_element.tag != "article" or _is_inside_disclosure(case_element):
            failures.append("methods case destination structure changed")
        if case_element.attributes.get("id") != expected_id:
            failures.append("methods case destination identity changed")
        anchor_matches = [
            element for element in elements if element.attributes.get("id") == expected_id
        ]
        if len(anchor_matches) != 1 or anchor_matches[0] is not case_element:
            failures.append("methods case destination anchor inventory changed")
        headings = [child for child in case_element.children if child.visible and child.tag == "h2"]
        if len(headings) != 1 or headings[0].rendered_text() != title:
            failures.append("methods case destination heading changed")
        elif (
            [child.tag for child in headings[0].children] != ["span", "span"]
            or headings[0].children[0].attributes.get("aria-hidden") != "true"
            or headings[0].children[0].visible
            or headings[0].children[0].source_rendered_text() != number
            or not headings[0].children[1].visible
            or headings[0].children[1].rendered_text() != title
        ):
            failures.append("methods case destination heading structure changed")
        destination_by_case[number] = case_element

    if index is not None and destinations and index.start_order >= destinations[0].start_order:
        failures.append("methods case index no longer precedes case 01")
    ledes = [element for element in elements if "lede" in element.classes and element.visible]
    if index is not None and len(ledes) == 1 and ledes[0].start_order >= index.start_order:
        failures.append("methods case index no longer follows the chapter lede")

    for identifier, expected_title, case_number in METHOD_FIGURE_TITLES:
        matches = [element for element in elements if element.attributes.get("id") == identifier]
        if (
            len(matches) != 1
            or not matches[0].visible
            or matches[0].rendered_text() != expected_title
        ):
            failures.append(f"methods Figure {identifier[9:12].replace('-', '.')} title changed")
            continue
        figure_case = destination_by_case.get(case_number)
        if figure_case is None or not matches[0].is_inside(figure_case):
            failures.append(f"methods Figure {identifier[9:12].replace('-', '.')} moved cases")

    return failures


EXPLORER_STEPS = (
    ("choose", "選一個問題", "從六個現有範例開始；需要時再展開 SQL。"),
    ("execute", "在瀏覽器內執行", "按下按鈕後才載入查詢引擎與可用資料。"),
    ("read", "讀結果與限制", "把表格、空結果或錯誤，和下方限制一起讀。"),
)
EXPLORER_EXAMPLES = (
    (
        "PM2.5 日均值最高的十個站日",
        "e735ae4b9a33e6023ccd2eab276857aa1f1cf374f28fdfdcb14344f4b059977e",
    ),
    (
        "各站年均，2024 年，由高至低",
        "b266c697fd77dccf298f4e4accf5abcd28c4b260303a7d71b3e48f3010cce348",
    ),
    (
        "超過日均標準的天數，逐月分布",
        "bf115aa1b3ea851436eaacad70d089be906f39b0b1679209a4064f7e53da1dd2",
    ),
    (
        "PM2.5 佔 PM10 的比例，逐年",
        "ed756618fb4392be2f9cbdc4f3e352252daa1090ac4843398bdb4e308915b8bc",
    ),
    (
        "PM2.5 大於 PM10 的比率（物理上不可能）",
        "a4a9ca9cdbd02c7f8b82f0bb9a814e5961517f06d76afb6621cbd8539a98dae7",
    ),
    (
        "覆蓋率不足而被扣住的日均值",
        "a36aa61adc4041faf03ef9ceddab0c2f4a742c51357241ee70c6ecd2bd52c91d",
    ),
)


def explorer_guided_workspace_failures_for_text(
    html: str,
    expected_examples: Sequence[tuple[str, str]] = EXPLORER_EXAMPLES,
) -> list[str]:
    parser = StructureParser()
    parser.feed(html)
    parser.close()
    parser.finish()
    failures = list(parser.errors)
    elements = parser.elements

    all_workspaces = [
        element for element in elements if "data-explorer-workspace" in element.attributes
    ]
    workspaces = [element for element in all_workspaces if element.visible]
    if len(workspaces) != 1 or workspaces != all_workspaces:
        failures.append(f"explore workspace inventory changed: {len(workspaces)}")
        workspace = None
    else:
        workspace = workspaces[0]
        if workspace.attributes.get("data-explorer-state") != "initial":
            failures.append("explore workspace initial state changed")

    all_paths = [element for element in elements if "data-explorer-path" in element.attributes]
    paths = [element for element in all_paths if element.visible]
    if len(paths) != 1 or paths != all_paths:
        failures.append(f"explore guide inventory changed: {len(paths)}")
        path = None
    else:
        path = paths[0]
        parent = path.parent
        direct_children = [child for child in parent.children if child.visible] if parent else []
        path_index = direct_children.index(path) if path in direct_children else -1
        if (
            path.tag != "ol"
            or workspace is None
            or not path.is_inside(workspace)
            or parent is None
            or "primary-tool" not in parent.classes
            or "data-primary-evidence" not in parent.attributes
            or path.attributes.get("aria-label") != "查詢步驟"
            or path_index != 1
            or direct_children[0].tag != "h2"
            or _is_inside_disclosure(path)
        ):
            failures.append("explore guide structure changed")

    all_steps = [element for element in elements if "data-explorer-step" in element.attributes]
    steps = [element for element in all_steps if element.visible]
    if len(steps) != len(EXPLORER_STEPS) or steps != all_steps:
        failures.append(f"explore guide step inventory changed: {len(steps)}")
    observed_keys = [step.attributes.get("data-explorer-step") for step in steps]
    if observed_keys != [step[0] for step in EXPLORER_STEPS]:
        failures.append(f"explore guide step order changed: {observed_keys!r}")
    if path is not None:
        direct_steps = [child for child in path.children if child.visible and child.tag == "li"]
        if direct_steps != steps or len(direct_steps) != len(EXPLORER_STEPS):
            failures.append("explore guide steps are not exact direct list items")

    for index, (key, title, description) in enumerate(EXPLORER_STEPS):
        if index >= len(steps):
            continue
        step = steps[index]
        children = [child for child in step.children if child.visible]
        if (
            step.tag != "li"
            or path is None
            or step.parent is not path
            or step.attributes.get("data-explorer-step") != key
            or [child.tag for child in children] != ["span", "strong", "span"]
            or "explorer-step-number" not in children[0].classes
            or children[0].rendered_text() != f"{index + 1:02d}"
            or children[1].rendered_text() != title
            or children[2].rendered_text() != description
        ):
            failures.append(f"explore guide step {key} content changed")
        if _is_inside_disclosure(step):
            failures.append(f"explore guide step {key} became user-collapsible")

    hook_contract = (
        ("controls", "data-explorer-controls"),
        ("sql", "data-explorer-sql"),
        ("tables", "data-explorer-tables"),
        ("result", "data-explorer-result"),
        ("caveat", "data-explorer-caveat"),
        ("no-js notice", "data-explorer-nojs"),
    )
    hooks: dict[str, Element | None] = {}
    for label, attribute in hook_contract:
        all_matches = [element for element in elements if attribute in element.attributes]
        matches = [element for element in all_matches if element.visible]
        if len(matches) != 1 or matches != all_matches:
            failures.append(f"explore {label} inventory changed: {len(matches)}")
            hooks[label] = None
        else:
            hooks[label] = matches[0]
            if workspace is None or not matches[0].is_inside(workspace):
                failures.append(f"explore {label} moved outside the workspace")

    order_labels = ("controls", "sql", "tables", "result", "caveat")
    ordered = [hooks[label] for label in order_labels]
    if path is not None and all(element is not None for element in ordered):
        observed_order = [
            path.start_order,
            *(element.start_order for element in ordered if element),
        ]
        if observed_order != sorted(observed_order):
            failures.append("explore guide-to-result source order changed")

    ids = {
        identifier: [e for e in elements if e.attributes.get("id") == identifier]
        for identifier in (
            "example-select",
            "run",
            "status",
            "sql",
            "tables",
            "result",
            "explorer-examples",
        )
    }
    for identifier in ("example-select", "run", "status", "sql", "tables", "result"):
        visible = [element for element in ids[identifier] if element.visible]
        if len(visible) != 1 or visible != ids[identifier]:
            failures.append(f"explore #{identifier} inventory changed: {len(visible)}")

    controls = hooks.get("controls")
    selector = next((e for e in ids["example-select"] if e.visible), None)
    run = next((e for e in ids["run"] if e.visible), None)
    status = next((e for e in ids["status"] if e.visible), None)
    if controls is not None:
        for label, element in (("selector", selector), ("run control", run), ("status", status)):
            if element is None or not element.is_inside(controls):
                failures.append(f"explore {label} moved outside the controls")
    if run is not None and (
        run.tag != "button"
        or run.attributes.get("type") != "button"
        or run.rendered_text() != "執行查詢"
    ):
        failures.append("explore run control changed")
    if status is not None and (
        status.attributes.get("role") != "status" or status.attributes.get("aria-live") != "polite"
    ):
        failures.append("explore live status semantics changed")

    labels: list[str] = []
    if selector is not None:
        options = [child for child in selector.children if child.visible and child.tag == "option"]
        labels = [option.rendered_text() for option in options]
        values = [option.attributes.get("value") for option in options]
        if values != [str(index) for index in range(len(expected_examples))]:
            failures.append(f"explore example option values changed: {values!r}")
    expected_labels = [label for label, _ in expected_examples]
    if labels != expected_labels:
        failures.append(f"explore example labels changed: {labels!r}")

    scripts = ids["explorer-examples"]
    if len(scripts) != 1:
        failures.append(f"explore SQL inventory changed: {len(scripts)}")
    else:
        script = scripts[0]
        raw_json = "".join(script.source_text)
        if script.tag != "script" or script.attributes.get("type") != "application/json":
            failures.append("explore SQL script semantics changed")
        try:
            sql_values = json.loads(raw_json)
        except json.JSONDecodeError:
            failures.append("explore SQL inventory is not valid JSON")
        else:
            if not isinstance(sql_values, list) or any(
                not isinstance(value, str) for value in sql_values
            ):
                failures.append("explore SQL inventory shape changed")
            else:
                digests = [
                    hashlib.sha256(value.encode("utf-8")).hexdigest() for value in sql_values
                ]
                expected_digests = [digest for _, digest in expected_examples]
                if len(sql_values) != len(expected_examples) or digests != expected_digests:
                    failures.append(f"explore SQL identity or order changed: {digests!r}")

    return failures


DATA_LAYER_ROWS = (
    (
        "L0",
        "L0 站-月",
        "閱讀者 · 快速查值與網站圖表",
    ),
    (
        "L1",
        "L1 站-日",
        "分析者 · 逐日查詢與桌面分析",
    ),
    (
        "L2",
        "L2 站-時",
        "重現者 · 逐時稽核與管線重建",
    ),
)


@dataclass(frozen=True)
class DataDownloadRow:
    name: str
    period: str
    l0_href: str
    l0_size: str
    l1_href: str | None
    l1_size: str
    l1_label: str


@dataclass(frozen=True)
class DataProvenanceContract:
    descriptions: Mapping[str, str]
    downloads: tuple[DataDownloadRow, ...]


def _data_mb(value: int) -> str:
    return f"{value / 1e6:.1f} MB" if value >= 10_000_000 else f"{value / 1e6:.2f} MB"


def _data_json_object(path: pathlib.Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{shown(path)} is unreadable: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{shown(path)} must contain an object")
    return cast(dict[str, Any], value)


def load_data_provenance_contract() -> DataProvenanceContract:
    index = _data_json_object(DATA_INDEX)
    manifest = _data_json_object(DATA_MANIFEST)
    meta = _data_json_object(DATA_META)
    publication = _data_json_object(DATA_PUBLICATION)
    manifest_rows = manifest.get("files")
    pollutant_rows = index.get("pollutants")
    hourly_observations = meta.get("hourly_observations")
    publication_layers = [publication.get(key) for key in ("metadata", "l0", "l1", "l2")]
    if (
        not isinstance(manifest_rows, list)
        or not isinstance(pollutant_rows, list)
        or any(not isinstance(layer, list) for layer in publication_layers)
    ):
        raise ValueError("data provenance sources have invalid row inventories")
    if type(hourly_observations) is not int or hourly_observations <= 0:
        raise ValueError("data provenance meta hourly_observations is invalid")

    manifest_bytes: dict[str, int] = {}
    for row in manifest_rows:
        if not isinstance(row, dict):
            raise ValueError("data manifest row is invalid")
        file = row.get("file")
        size = row.get("bytes")
        if (
            not isinstance(file, str)
            or not file
            or type(size) is not int
            or size < 0
            or file in manifest_bytes
        ):
            raise ValueError("data manifest file identity is invalid")
        manifest_bytes[file] = size

    published = {
        file
        for layer in publication_layers
        for file in cast(list[Any], layer)
        if isinstance(file, str)
    }

    downloads: list[DataDownloadRow] = []
    l1_total = 0
    published_l1_codes: list[str] = []
    for row in pollutant_rows:
        if not isinstance(row, dict):
            raise ValueError("data index pollutant row is invalid")
        pollutant = row.get("pollutant")
        name = row.get("name_zh")
        file = row.get("file")
        months = row.get("months")
        l0_size = row.get("bytes")
        if (
            not isinstance(pollutant, str)
            or not pollutant
            or not isinstance(name, str)
            or not name
            or not isinstance(file, str)
            or not file.startswith("l0/")
            or not file.endswith(".json")
            or not isinstance(months, list)
            or len(months) != 2
            or any(not isinstance(month, str) or not month for month in months)
            or type(l0_size) is not int
            or l0_size < 0
            or manifest_bytes.get(file) != l0_size
            or file not in published
        ):
            raise ValueError("data index pollutant identity is invalid")
        stem = file.removeprefix("l0/").removesuffix(".json")
        l1_file = f"l1/{stem}.parquet"
        l1_size = manifest_bytes.get(l1_file)
        l1_selected = l1_file in published
        if l1_selected and l1_size is None:
            raise ValueError(f"data manifest is missing {l1_file}")
        if l1_selected:
            l1_total += cast(int, l1_size)
            published_l1_codes.append(pollutant)
        downloads.append(
            DataDownloadRow(
                name=f"{name}{pollutant}",
                period=f"{months[0]}–{months[1]}",
                l0_href=f"/data/{file}",
                l0_size=_data_mb(l0_size),
                l1_href=f"/data/{l1_file}" if l1_selected else None,
                l1_size=_data_mb(cast(int, l1_size)) if l1_selected else "",
                l1_label="Parquet" if l1_selected else "Pages 未發布",
            )
        )

    descriptions = {
        "L0": "每個測項一個 JSON，含月均值與該月的有效天數。網站直接讀這一層。",
        "L1": (
            f"Pages 目前發布 {'、'.join(published_l1_codes)} 的 Parquet，共 {_data_mb(l1_total)}；"
            "其餘測項可由本機管線產生。"
        ),
        "L2": (
            f"{hourly_observations / 1e8:.2f} 億筆完整逐時觀測，含每一筆的品管旗標。"
            "不發布— 只發衍生產物與完整管線，執行一次 twair ingest 加 twair build 即可獨立重建。"
        ),
    }
    return DataProvenanceContract(descriptions=descriptions, downloads=tuple(downloads))


def data_provenance_register_failures_for_text(
    html: str,
    expected_descriptions: Mapping[str, str],
    expected_downloads: Sequence[DataDownloadRow],
) -> list[str]:
    parser = StructureParser()
    parser.feed(html)
    parser.close()
    parser.finish()
    failures = list(parser.errors)
    elements = parser.elements

    all_registers = [
        element for element in elements if "data-data-layer-register" in element.attributes
    ]
    registers = [element for element in all_registers if element.visible]
    if len(registers) != 1 or registers != all_registers:
        failures.append(f"data provenance register inventory changed: {len(registers)}")
        register = None
    else:
        register = registers[0]
        if register.tag != "dl":
            failures.append("data provenance register semantics changed")
        if _is_inside_disclosure(register):
            failures.append("data provenance register is user-collapsible")

    all_terms = [element for element in elements if "data-data-layer" in element.attributes]
    terms = [element for element in all_terms if element.visible]
    all_descriptions = [
        element for element in elements if "data-data-layer-description" in element.attributes
    ]
    descriptions = [element for element in all_descriptions if element.visible]
    if len(terms) != len(DATA_LAYER_ROWS) or terms != all_terms:
        failures.append(f"data layer term inventory changed: {len(terms)}")
    if len(descriptions) != len(DATA_LAYER_ROWS) or descriptions != all_descriptions:
        failures.append(f"data layer description inventory changed: {len(descriptions)}")

    observed_levels = [term.attributes.get("data-data-layer") for term in terms]
    if observed_levels != [row[0] for row in DATA_LAYER_ROWS]:
        failures.append(f"data layer order changed: {observed_levels!r}")
    if register is not None:
        direct_children = [child for child in register.children if child.visible]
        expected_children = [
            item for pair in zip(terms, descriptions, strict=False) for item in pair
        ]
        if direct_children != expected_children or len(direct_children) != 6:
            failures.append("data layer term-description pairing changed")

    for index, (level, term_text, use_text) in enumerate(DATA_LAYER_ROWS):
        if index >= len(terms) or index >= len(descriptions):
            continue
        term = terms[index]
        description = descriptions[index]
        term_children = [child for child in term.children if child.visible]
        if (
            term.tag != "dt"
            or register is None
            or term.parent is not register
            or term.attributes.get("data-data-layer") != level
            or [child.tag for child in term_children] != ["span", "span"]
            or "data-data-layer-term" not in term_children[0].attributes
            or term_children[0].rendered_text() != term_text
            or "data-data-layer-use" not in term_children[1].attributes
            or term_children[1].rendered_text() != use_text
        ):
            failures.append(f"data layer {level} term or use label changed")
        if (
            description.tag != "dd"
            or register is None
            or description.parent is not register
            or description.attributes.get("data-data-layer-description") != level
            or _is_inside_disclosure(description)
        ):
            failures.append(f"data layer {level} description structure changed")
        description_text = description.rendered_text()
        if description_text != expected_descriptions.get(level):
            failures.append(f"data layer {level} description changed")
        if level == "L2" and any(
            "download" in element.attributes and element.is_inside(description)
            for element in elements
        ):
            failures.append("data layer L2 became downloadable")

    tables = [
        element
        for element in elements
        if element.visible and element.tag == "table" and "dense" in element.classes
    ]
    if len(tables) != 1:
        failures.append(f"data download table inventory changed: {len(tables)}")
        table = None
    else:
        table = tables[0]
        if register is not None and register.start_order >= table.start_order:
            failures.append("data provenance register no longer precedes the download table")

    table_rows = [
        element
        for element in elements
        if element.visible
        and element.tag == "tr"
        and table is not None
        and element.is_inside(table)
    ]
    table_bodies = [
        element
        for element in elements
        if element.visible
        and element.tag == "tbody"
        and table is not None
        and element.is_inside(table)
    ]
    body_rows = [row for row in table_rows if any(row.is_inside(body) for body in table_bodies)]
    if len(body_rows) != 21:
        failures.append(f"data download row inventory changed: {len(body_rows)}")
    downloads = [
        element
        for element in elements
        if element.visible and element.tag == "a" and "download" in element.attributes
    ]
    table_downloads = [link for link in downloads if table is not None and link.is_inside(table)]
    if len(downloads) != 25 or len(table_downloads) != 23:
        failures.append(f"data download link inventory changed: {len(downloads)}")

    if len(expected_downloads) != 21:
        failures.append(f"data expected download row inventory changed: {len(expected_downloads)}")
    for index, expected in enumerate(expected_downloads):
        if index >= len(body_rows):
            continue
        row = body_rows[index]
        cells = [child for child in row.children if child.visible]
        observed: DataDownloadRow | None = None
        if len(cells) == 4 and all(cell.tag == "td" for cell in cells):
            l0_links = [link for link in table_downloads if link.is_inside(cells[2])]
            l1_links = [link for link in table_downloads if link.is_inside(cells[3])]
            l0_sizes = [
                element
                for element in elements
                if element.visible and "size" in element.classes and element.is_inside(cells[2])
            ]
            l1_sizes = [
                element
                for element in elements
                if element.visible and "size" in element.classes and element.is_inside(cells[3])
            ]
            unavailable = [
                element
                for element in elements
                if element.visible
                and "data-pages-unavailable" in element.attributes
                and element.is_inside(cells[3])
            ]
            l0_label = " ".join("".join(l0_links[0].text).split()) if len(l0_links) == 1 else ""
            l1_label = " ".join("".join(l1_links[0].text).split()) if len(l1_links) == 1 else ""
            if (
                len(l0_links) == len(l0_sizes) == 1
                and l0_label == "JSON"
                and (
                    (
                        expected.l1_href is not None
                        and len(l1_links) == len(l1_sizes) == 1
                        and not unavailable
                        and l1_label == "Parquet"
                    )
                    or (
                        expected.l1_href is None
                        and not l1_links
                        and not l1_sizes
                        and len(unavailable) == 1
                        and unavailable[0].rendered_text() == "Pages 未發布"
                    )
                )
            ):
                observed = DataDownloadRow(
                    name=cells[0].rendered_text(),
                    period=cells[1].rendered_text(),
                    l0_href=l0_links[0].attributes.get("href") or "",
                    l0_size=l0_sizes[0].rendered_text(),
                    l1_href=(l1_links[0].attributes.get("href") or "") if l1_links else None,
                    l1_size=l1_sizes[0].rendered_text() if l1_sizes else "",
                    l1_label="Parquet" if l1_links else "Pages 未發布",
                )
        if observed != expected:
            failures.append(f"data download row {index + 1} changed")

    page_text = " ".join(
        element.rendered_text()
        for element in elements
        if element.parent is None and element.visible
    )
    for label, fragment in (
        ("licensing", "授權與再散布"),
        ("L2 boundary", "L2 不發布，理由不是檔案太大"),
        ("missing-value caveat", "關於缺值"),
        ("hourly PM2.5 caveat", "逐時 PM2.5 的官方但書"),
    ):
        if fragment not in page_text:
            failures.append(f"data {label} statement changed")
    disagreements = [
        element
        for element in elements
        if element.visible and "data-publication-disagreement" in element.attributes
    ]
    if len(disagreements) != 1 or disagreements[0].tag != "details":
        failures.append("data publication-disagreement evidence changed")

    return failures


FORECAST_PAYLOAD_KEYS = frozenset(
    {
        "period",
        "target",
        "validation",
        "skill_formula",
        "baselines",
        "reading",
        "leakage_note",
        "horizons",
    }
)
FORECAST_BASELINE_KEYS = frozenset({"name", "label", "what", "why"})
FORECAST_READING_KEYS = frozenset({"claim", "detail"})
FORECAST_HORIZON_KEYS = frozenset(
    {
        "horizon",
        "n",
        "stations",
        "splits",
        "model_r2",
        "skill_persistence",
        "skill_persistence_worst",
        "skill_climatology",
        "skill_climatology_worst",
        "splits_not_beating_persistence",
        "model_rmse",
        "persistence_rmse",
        "climatology_rmse",
        # The conformal band. `check_site_quality.mjs` carries the same list and
        # they have to move together — this one was left behind for exactly one
        # mirror run, which is how long it took to find out.
        "band_nominal",
        "band_half_width",
        "band_coverage",
        "band_coverage_worst",
        "band_splits_below_nominal",
        "band_model_rmse",
        "per_split",
    }
)
FORECAST_SPLIT_KEYS = frozenset(
    {
        "split",
        "skill_persistence",
        "skill_climatology",
        "model_r2",
        "band_half_width",
        "band_coverage",
    }
)
FORECAST_HORIZONS = (1, 6, 24, 48)
FORECAST_READING_IDENTITIES = (
    "r2-skill",
    "two-baselines",
    "split-instability",
    "shared-feature-bug",
)
FORECAST_DECISION_ROWS = (
    (
        "error",
        "誤差",
        "先看圖 6.1：模型、persistence 與 climatology 的 RMSE 隨期距如何變化。",
        "#evidence-6-1-title",
    ),
    (
        "skill",
        "基準優勢",
        "再看圖 6.2：同一批預測相對 persistence 與 climatology 還剩多少優勢。",
        "#evidence-6-2-title",
    ),
    (
        "cost",
        "計算代價",
        "最後看成本表與圖 6.3：額外計算是否換得可用的準確度。",
        "#forecast-cost",
    ),
)


@dataclass(frozen=True)
class ForecastExpectedEvidence:
    horizons: tuple[int, ...]
    readings: tuple[tuple[str, str], ...]
    baselines: tuple[tuple[str, str, str, str], ...]


def _finite_forecast_number(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _forecast_expected_evidence_from_payload(payload: object) -> ForecastExpectedEvidence:
    if not isinstance(payload, dict) or set(payload) != FORECAST_PAYLOAD_KEYS:
        raise ValueError("forecast payload top-level shape changed")
    period = payload["period"]
    if (
        not isinstance(period, list)
        or len(period) != 2
        or any(isinstance(year, bool) or not isinstance(year, int) for year in period)
        or period != sorted(period)
    ):
        raise ValueError("forecast payload period changed")
    for key in ("target", "validation", "skill_formula", "leakage_note"):
        if not isinstance(payload[key], str) or not payload[key].strip():
            raise ValueError(f"forecast payload {key} changed")

    baselines = payload["baselines"]
    if not isinstance(baselines, list) or len(baselines) != 2:
        raise ValueError("forecast payload baseline inventory changed")
    expected_baseline_names = ("persistence", "climatology")
    baseline_evidence: list[tuple[str, str, str, str]] = []
    for index, (row, expected_name) in enumerate(
        zip(baselines, expected_baseline_names, strict=True), start=1
    ):
        if not isinstance(row, dict) or set(row) != FORECAST_BASELINE_KEYS:
            raise ValueError(f"forecast payload baseline {index} shape changed")
        values = tuple(row[key] for key in ("name", "label", "what", "why"))
        if any(not isinstance(value, str) or not value.strip() for value in values):
            raise ValueError(f"forecast payload baseline {index} text changed")
        if row["name"] != expected_name:
            raise ValueError("forecast payload baseline identity or order changed")
        baseline_evidence.append(cast(tuple[str, str, str, str], values))

    readings = payload["reading"]
    if not isinstance(readings, list) or len(readings) != len(FORECAST_READING_IDENTITIES):
        raise ValueError("forecast payload reading inventory changed")
    reading_evidence: list[tuple[str, str]] = []
    for index, row in enumerate(readings, start=1):
        if not isinstance(row, dict) or set(row) != FORECAST_READING_KEYS:
            raise ValueError(f"forecast payload reading {index} shape changed")
        claim = row["claim"]
        detail = row["detail"]
        if (
            not isinstance(claim, str)
            or not claim.strip()
            or not isinstance(detail, str)
            or not detail.strip()
        ):
            raise ValueError(f"forecast payload reading {index} text changed")
        reading_evidence.append((claim, detail))

    horizons = payload["horizons"]
    if not isinstance(horizons, list) or len(horizons) != len(FORECAST_HORIZONS):
        raise ValueError("forecast payload horizon inventory changed")
    observed_horizons: list[int] = []
    numeric_keys = (
        "model_r2",
        "skill_persistence",
        "skill_persistence_worst",
        "skill_climatology",
        "skill_climatology_worst",
        "model_rmse",
        "persistence_rmse",
        "climatology_rmse",
    )
    for index, row in enumerate(horizons, start=1):
        if not isinstance(row, dict) or set(row) != FORECAST_HORIZON_KEYS:
            raise ValueError(f"forecast payload horizon {index} shape changed")
        for key in ("horizon", "n", "stations", "splits", "splits_not_beating_persistence"):
            value = row[key]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"forecast payload horizon {index} {key} is invalid")
        if any(not _finite_forecast_number(row[key]) for key in numeric_keys):
            raise ValueError(f"forecast payload horizon {index} metric is invalid")
        per_split = row["per_split"]
        if not isinstance(per_split, list) or len(per_split) != row["splits"]:
            raise ValueError(f"forecast payload horizon {index} split inventory changed")
        split_names: list[str] = []
        for split_index, split in enumerate(per_split, start=1):
            if not isinstance(split, dict) or set(split) != FORECAST_SPLIT_KEYS:
                raise ValueError(
                    f"forecast payload horizon {index} split {split_index} shape changed"
                )
            name = split["split"]
            if not isinstance(name, str) or not name.strip() or name in split_names:
                raise ValueError(f"forecast payload horizon {index} split identity changed")
            split_names.append(name)
            if any(
                not _finite_forecast_number(split[key])
                for key in ("skill_persistence", "skill_climatology", "model_r2")
            ):
                raise ValueError(f"forecast payload horizon {index} split metric is invalid")
        observed_horizons.append(row["horizon"])
    if tuple(observed_horizons) != FORECAST_HORIZONS:
        raise ValueError("forecast payload horizon identity or order changed")

    return ForecastExpectedEvidence(
        horizons=tuple(observed_horizons),
        readings=tuple(reading_evidence),
        baselines=tuple(baseline_evidence),
    )


def load_forecast_expected_evidence() -> ForecastExpectedEvidence:
    try:
        with FORECAST_STORY.open(encoding="utf-8") as handle:
            payload = json.load(handle, parse_constant=_reject_nonfinite_json_constant)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read forecast payload: {exc}") from exc
    return _forecast_expected_evidence_from_payload(payload)


def _forecast_text_identity(value: str) -> str:
    return "".join(value.split())


def _forecast_direct_rows(region: Element, attribute: str) -> list[Element]:
    return [child for child in region.children if attribute in child.attributes]


def forecast_horizon_decision_failures_for_text(
    html: str,
    expected: ForecastExpectedEvidence,
) -> list[str]:
    parser = StructureParser()
    parser.feed(html)
    parser.close()
    parser.finish()
    failures = list(parser.errors)
    elements = parser.elements

    def visible_region(attribute: str, label: str) -> Element | None:
        matches = [element for element in elements if attribute in element.attributes]
        if len(matches) != 1 or not matches[0].visible:
            failures.append(f"forecast {label} inventory changed: {len(matches)}")
            return None
        region = matches[0]
        if _is_inside_disclosure(region):
            failures.append(f"forecast {label} is user-collapsible")
        return region

    sheet = visible_region("data-forecast-decision-sheet", "decision sheet")
    reading_band = visible_region("data-forecast-reading-band", "reading band")
    baseline_band = visible_region("data-forecast-baseline-band", "baseline band")

    if sheet is not None:
        children = [child for child in sheet.children if child.visible]
        if (
            sheet.tag != "nav"
            or sheet.attributes.get("aria-labelledby") != "forecast-decision-title"
            or [child.tag for child in children] != ["h2", "ol"]
            or children[0].attributes.get("id") != "forecast-decision-title"
            or children[0].rendered_text() != "三步決定這個預測還值不值得用"
        ):
            failures.append("forecast decision sheet semantics changed")
        ordered_list = children[1] if len(children) == 2 and children[1].tag == "ol" else None
        rows = (
            _forecast_direct_rows(ordered_list, "data-forecast-decision")
            if ordered_list is not None
            else []
        )
        all_rows = [
            element for element in elements if "data-forecast-decision" in element.attributes
        ]
        if (
            len(rows) != len(FORECAST_DECISION_ROWS)
            or rows != all_rows
            or any(row.tag != "li" or not row.visible for row in rows)
        ):
            failures.append(f"forecast decision row inventory changed: {len(rows)}")
        observed_keys = [row.attributes.get("data-forecast-decision") for row in rows]
        if observed_keys != [row[0] for row in FORECAST_DECISION_ROWS]:
            failures.append(f"forecast decision row order changed: {observed_keys!r}")
        for row, (_key, label, explanation, destination) in zip(
            rows, FORECAST_DECISION_ROWS, strict=False
        ):
            links = [child for child in row.children if child.visible]
            if len(links) != 1 or links[0].tag != "a":
                failures.append("forecast decision row structure changed")
                continue
            link = links[0]
            link_children = [child for child in link.children if child.visible]
            if [child.tag for child in link_children] != ["strong", "p"]:
                failures.append("forecast decision row structure changed")
                continue
            if link.attributes.get("href") != destination:
                failures.append("forecast decision destination changed")
            if link_children[0].rendered_text() != label:
                failures.append("forecast decision row text changed")
            if link_children[1].rendered_text() != explanation:
                failures.append("forecast decision row text changed")

    if reading_band is not None:
        rows = _forecast_direct_rows(reading_band, "data-forecast-reading")
        all_rows = [
            element for element in elements if "data-forecast-reading" in element.attributes
        ]
        if (
            len(rows) != len(expected.readings)
            or rows != all_rows
            or any(row.tag != "section" or not row.visible for row in rows)
        ):
            failures.append(f"forecast reading row inventory changed: {len(rows)}")
        observed_keys = [row.attributes.get("data-forecast-reading") for row in rows]
        if observed_keys != list(FORECAST_READING_IDENTITIES):
            failures.append(f"forecast reading row order changed: {observed_keys!r}")
        for row, (claim, detail) in zip(rows, expected.readings, strict=False):
            children = [child for child in row.children if child.visible]
            if [child.tag for child in children] != ["h2", "p"]:
                failures.append("forecast reading row structure changed")
                continue
            if children[0].rendered_text() != claim or children[1].rendered_text() != detail:
                failures.append("forecast reading row text changed")

    if baseline_band is not None:
        rows = _forecast_direct_rows(baseline_band, "data-forecast-baseline")
        all_rows = [
            element for element in elements if "data-forecast-baseline" in element.attributes
        ]
        if (
            len(rows) != len(expected.baselines)
            or rows != all_rows
            or any(row.tag != "section" or not row.visible for row in rows)
        ):
            failures.append(f"forecast baseline row inventory changed: {len(rows)}")
        observed_keys = [row.attributes.get("data-forecast-baseline") for row in rows]
        if observed_keys != [row[0] for row in expected.baselines]:
            failures.append(f"forecast baseline row order changed: {observed_keys!r}")
        for row, (name, label, what, why) in zip(rows, expected.baselines, strict=False):
            children = [child for child in row.children if child.visible]
            if [child.tag for child in children] != ["h2", "p", "p"]:
                failures.append("forecast baseline row structure changed")
                continue
            if _forecast_text_identity(children[0].rendered_text()) != _forecast_text_identity(
                name + label
            ):
                failures.append("forecast baseline row text changed")
            if children[1].rendered_text() != what or children[2].rendered_text() != why:
                failures.append("forecast baseline row text changed")

    def visible_id(identifier: str, label: str) -> Element | None:
        matches = [
            element
            for element in elements
            if element.attributes.get("id") == identifier and element.visible
        ]
        if len(matches) != 1:
            failures.append(f"forecast {label} anchor inventory changed: {len(matches)}")
            return None
        return matches[0]

    figure_1 = visible_id("evidence-6-1-title", "Figure 6.1")
    figure_2 = visible_id("evidence-6-2-title", "Figure 6.2")
    cost = visible_id("forecast-cost", "cost")
    if figure_1 is not None and sheet is not None and figure_1.start_order >= sheet.start_order:
        failures.append("forecast decision sheet no longer follows Figure 6.1")
    if sheet is not None and figure_2 is not None and sheet.start_order >= figure_2.start_order:
        failures.append("forecast decision sheet no longer precedes Figure 6.2")
    if (
        figure_2 is not None
        and reading_band is not None
        and baseline_band is not None
        and cost is not None
        and not (
            figure_2.start_order
            < reading_band.start_order
            < baseline_band.start_order
            < cost.start_order
        )
    ):
        failures.append("forecast evidence bands changed source order")

    return failures


HEALTH_PAYLOAD_KEYS = frozenset(
    {
        "panel",
        "formula",
        "functions",
        "series",
        "years",
        "mean_median",
        "spread_share",
        "headline",
        "extrapolation",
        "not_reported",
    }
)
HEALTH_FUNCTION_KEYS = frozenset(
    {
        "name",
        "rr_per_10",
        "rr_per_10_low",
        "rr_per_10_high",
        "outcome",
        "source",
        "source_url",
        "caveat",
    }
)
HEALTH_SERIES_KEYS = frozenset({"name", "label", "value", "why", "years", "paf"})
HEALTH_HEADLINE_KEYS = frozenset(
    {"first_year", "last_year", "first_share", "last_share", "first_range", "last_range"}
)
HEALTH_ASSUMPTION_ROWS = (
    (
        "counterfactual",
        "比較基準",
        "圖 7.1 與圖 7.2 量化四種反事實濃度造成的差異。",
    ),
    (
        "response",
        "暴露反應函數",
        "本章只採用一條具可追溯來源的函數；適用範圍與外推界線在後文公開。",
    ),
    (
        "population",
        "暴露人口",
        "本專案沒有人口與個人暴露資料，因此不報死亡人數，也不把測站中位數稱為誰的暴露。",
    ),
)
HEALTH_READING_ROWS = (
    ("robust", "下降幅度對比較基準穩健"),
    ("sensitive", "當前水準對比較基準敏感"),
)
HEALTH_INFERENCE_ROWS = (
    ("deaths", "不報死亡人數"),
    ("exposure", "不宣稱這是誰的暴露"),
)
HEALTH_FIGURE_TITLES = (
    "比較基準如何改變可歸因比例？",
    "比較基準造成的落差佔估計值多少？",
)


@dataclass(frozen=True)
class HealthExpectedEvidence:
    series_count: int
    function_count: int
    years_count: int
    spread_count: int
    deaths: str
    exposure: str
    reading_bodies: tuple[str, str]


def _finite_health_number(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _health_number(value: float) -> str:
    return f"{value:.1f}".rstrip("0").rstrip(".")


def _health_expected_evidence_from_payload(payload: object) -> HealthExpectedEvidence:
    if not isinstance(payload, dict) or set(payload) != HEALTH_PAYLOAD_KEYS:
        raise ValueError("health payload top-level shape changed")

    functions = payload["functions"]
    if not isinstance(functions, list) or len(functions) != 1:
        count = len(functions) if isinstance(functions, list) else "not a list"
        raise ValueError(f"health payload response-function inventory changed: {count}")
    function = functions[0]
    if not isinstance(function, dict) or set(function) != HEALTH_FUNCTION_KEYS:
        raise ValueError("health payload response-function shape changed")
    for key in ("name", "outcome", "source", "source_url", "caveat"):
        if not isinstance(function[key], str) or not function[key].strip():
            raise ValueError(f"health payload response-function {key} changed")
    for key in ("rr_per_10", "rr_per_10_low", "rr_per_10_high"):
        if not _finite_health_number(function[key]):
            raise ValueError(f"health payload response-function {key} is invalid")

    years = payload["years"]
    if (
        not isinstance(years, list)
        or not years
        or any(isinstance(year, bool) or not isinstance(year, int) for year in years)
        or years != sorted(set(years))
    ):
        raise ValueError("health payload year inventory changed")
    spread = payload["spread_share"]
    if not isinstance(spread, list) or len(spread) != len(years):
        raise ValueError("health payload years/spread inventory changed")
    if any(not _finite_health_number(value) for value in spread):
        raise ValueError("health payload spread value is invalid")

    series = payload["series"]
    if not isinstance(series, list) or len(series) != 4:
        count = len(series) if isinstance(series, list) else "not a list"
        raise ValueError(f"health payload counterfactual-series inventory changed: {count}")
    identities: set[str] = set()
    series_by_name: dict[str, dict[str, object]] = {}
    for index, row in enumerate(series, start=1):
        if not isinstance(row, dict) or set(row) != HEALTH_SERIES_KEYS:
            raise ValueError(f"health payload counterfactual series {index} shape changed")
        for key in ("name", "label", "why"):
            if not isinstance(row[key], str) or not row[key].strip():
                raise ValueError(f"health payload counterfactual series {index} {key} changed")
        if row["name"] in identities:
            raise ValueError("health payload counterfactual series identity is duplicated")
        identities.add(row["name"])
        series_by_name[row["name"]] = row
        if not _finite_health_number(row["value"]):
            raise ValueError(f"health payload counterfactual series {index} value is invalid")
        if row["years"] != years:
            raise ValueError(f"health payload counterfactual series {index} years changed")
        paf = row["paf"]
        if (
            not isinstance(paf, list)
            or len(paf) != len(years)
            or any(not _finite_health_number(value) for value in paf)
        ):
            raise ValueError(f"health payload counterfactual series {index} values changed")

    headline = payload["headline"]
    if not isinstance(headline, dict) or set(headline) != HEALTH_HEADLINE_KEYS:
        raise ValueError("health payload headline shape changed")
    for key in ("first_year", "last_year"):
        if isinstance(headline[key], bool) or not isinstance(headline[key], int):
            raise ValueError(f"health payload headline {key} is invalid")
    for key in ("first_share", "last_share"):
        if not _finite_health_number(headline[key]):
            raise ValueError(f"health payload headline {key} is invalid")
    for key in ("first_range", "last_range"):
        value = headline[key]
        if (
            not isinstance(value, list)
            or len(value) != 2
            or any(not _finite_health_number(item) for item in value)
        ):
            raise ValueError(f"health payload headline {key} is invalid")
    if headline["first_year"] not in years or headline["last_year"] not in years:
        raise ValueError("health payload headline years changed")
    first_range = headline["first_range"]
    last_range = headline["last_range"]
    robust_body = (
        f"{headline['first_year']} 年是 {_health_number(first_range[0] * 100)}–"
        f"{_health_number(first_range[1] * 100)}%，{headline['last_year']} 年是 "
        f"{_health_number(last_range[0] * 100)}–{_health_number(last_range[1] * 100)}%。"
        "無論選哪個基準，都下降了大約一半到三分之二。"
        "這一點跟第五章的政策效應不一樣—那裡的訊號被方法的噪音蓋過去，這裡沒有。"
    )
    # The two concentrations the range actually spans, resolved from the range
    # rather than read by name.
    #
    # This asserted `gbd_low` and `gbd_high` — 2.4 and 5.9 — beside percentages
    # taken from `last_range`, and `analysis/health.py` builds that range as
    # min/max of `paf_median` across EVERY counterfactual, so its upper end is
    # the zero-exposure assumption. 2.4 gives 7.7% where the sentence stood
    # beside 9.4%. The chapter said the two ends were 「同一份 published TMREL 區間
    # 的兩端」 and this gate held it to saying so, which is the more serious half:
    # a correct repair would have been reported as the regression.
    last_index = years.index(headline["last_year"])
    range_ends: list[float] = []
    for target in last_range:
        matched = [
            entry
            for entry in series
            if isinstance(entry.get("paf"), list)
            and _finite_health_number(entry["paf"][last_index])
            and abs(entry["paf"][last_index] - target) < 5e-5
        ]
        if len(matched) != 1 or not _finite_health_number(matched[0]["value"]):
            raise ValueError("health payload headline range does not resolve to one counterfactual")
        range_ends.append(float(matched[0]["value"]))

    sensitive_body = (
        f"{headline['last_year']} 年的答案是 {_health_number(last_range[0] * 100)}% 還是 "
        f"{_health_number(last_range[1] * 100)}%，差了將近一倍，而唯一的差別是把 "
        f"{_health_number(range_ends[0])} 還是 "
        f"{_health_number(range_ends[1])} μg/m³ 當作比較基準—"
        f"這是上圖 {len(series)} 條假設線的兩個極端，落差來自方法選擇，不是來自資料。"
    )

    not_reported = payload["not_reported"]
    if not isinstance(not_reported, dict) or set(not_reported) != {"deaths", "exposure"}:
        raise ValueError("health payload no-inference boundary changed")
    deaths = not_reported["deaths"]
    exposure = not_reported["exposure"]
    if (
        not isinstance(deaths, str)
        or not deaths.strip()
        or not isinstance(exposure, str)
        or not exposure.strip()
    ):
        raise ValueError("health payload no-inference boundary changed")

    return HealthExpectedEvidence(
        series_count=len(series),
        function_count=len(functions),
        years_count=len(years),
        spread_count=len(spread),
        deaths=deaths,
        exposure=exposure,
        reading_bodies=(robust_body, sensitive_body),
    )


def load_health_expected_evidence() -> HealthExpectedEvidence:
    try:
        with HEALTH_STORY.open(encoding="utf-8") as handle:
            payload = json.load(handle, parse_constant=_reject_nonfinite_json_constant)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read health payload: {exc}") from exc
    return _health_expected_evidence_from_payload(payload)


def _health_text_identity(value: str) -> str:
    return "".join(value.split())


def _health_direct_rows(region: Element, attribute: str) -> list[Element]:
    return [child for child in region.children if attribute in child.attributes]


def health_assumption_ledger_failures_for_text(
    html: str,
    expected: HealthExpectedEvidence,
) -> list[str]:
    parser = StructureParser()
    parser.feed(html)
    parser.close()
    parser.finish()
    failures = list(parser.errors)
    elements = parser.elements

    def visible_region(attribute: str, label: str) -> Element | None:
        matches = [element for element in elements if attribute in element.attributes]
        if len(matches) != 1 or not matches[0].visible:
            failures.append(f"health {label} inventory changed: {len(matches)}")
            return None
        region = matches[0]
        if _is_inside_disclosure(region):
            verb = "are" if label == "inference boundaries" else "is"
            failures.append(f"health {label} {verb} user-collapsible")
        return region

    ledger = visible_region("data-health-assumption-ledger", "assumption ledger")
    reading_band = visible_region("data-health-reading-band", "reading band")
    boundaries = visible_region("data-health-inference-boundaries", "inference boundaries")
    primary = visible_region("data-primary-evidence", "primary evidence")

    if ledger is not None:
        if ledger.tag != "ol" or ledger.attributes.get("aria-label") != "本章三項假設":
            failures.append("health assumption ledger semantics changed")
        rows = _health_direct_rows(ledger, "data-health-assumption")
        all_marked_rows = [
            element for element in elements if "data-health-assumption" in element.attributes
        ]
        if (
            len(rows) != len(HEALTH_ASSUMPTION_ROWS)
            or rows != all_marked_rows
            or any(row.tag != "li" or not row.visible for row in rows)
        ):
            failures.append(f"health assumption row inventory changed: {len(rows)}")
        observed_keys = [row.attributes.get("data-health-assumption") for row in rows]
        expected_keys = [row[0] for row in HEALTH_ASSUMPTION_ROWS]
        if observed_keys != expected_keys:
            failures.append(f"health assumption row order changed: {observed_keys!r}")
        for row, (_key, label, explanation) in zip(rows, HEALTH_ASSUMPTION_ROWS, strict=False):
            visible_children = [child for child in row.children if child.visible]
            if [child.tag for child in visible_children] != ["strong", "p"]:
                failures.append("health assumption row structure changed")
                continue
            if visible_children[0].rendered_text() != label:
                failures.append("health assumption row text changed")
            if visible_children[1].rendered_text() != explanation:
                failures.append("health assumption row text changed")

    if reading_band is not None:
        rows = _health_direct_rows(reading_band, "data-health-reading")
        all_marked_rows = [
            element for element in elements if "data-health-reading" in element.attributes
        ]
        if (
            len(rows) != len(HEALTH_READING_ROWS)
            or rows != all_marked_rows
            or any(not row.visible for row in rows)
        ):
            failures.append(f"health reading row inventory changed: {len(rows)}")
        observed_keys = [row.attributes.get("data-health-reading") for row in rows]
        expected_keys = [row[0] for row in HEALTH_READING_ROWS]
        if observed_keys != expected_keys:
            failures.append(f"health reading row order changed: {observed_keys!r}")
        for row, (_key, heading), body in zip(
            rows, HEALTH_READING_ROWS, expected.reading_bodies, strict=False
        ):
            visible_children = [child for child in row.children if child.visible]
            if [child.tag for child in visible_children] != ["h2", "p"]:
                failures.append("health reading row body changed")
                continue
            if visible_children[0].rendered_text() != heading:
                failures.append("health reading row text changed")
            if _health_text_identity(visible_children[1].rendered_text()) != _health_text_identity(
                body
            ):
                failures.append("health reading row body changed")

    if boundaries is not None:
        rows = _health_direct_rows(boundaries, "data-health-inference")
        all_marked_rows = [
            element for element in elements if "data-health-inference" in element.attributes
        ]
        if (
            len(rows) != len(HEALTH_INFERENCE_ROWS)
            or rows != all_marked_rows
            or any(not row.visible for row in rows)
        ):
            failures.append(f"health inference row inventory changed: {len(rows)}")
        observed_keys = [row.attributes.get("data-health-inference") for row in rows]
        expected_keys = [row[0] for row in HEALTH_INFERENCE_ROWS]
        if observed_keys != expected_keys:
            failures.append(f"health inference row order changed: {observed_keys!r}")
        boundary_texts = (expected.deaths, expected.exposure)
        for row, (_key, heading), body in zip(
            rows, HEALTH_INFERENCE_ROWS, boundary_texts, strict=False
        ):
            if _health_text_identity(row.rendered_text()) != _health_text_identity(heading + body):
                failures.append("health inference row text changed")

    figure_titles = [
        element for element in elements if "evidence-title" in element.classes and element.visible
    ]
    observed_titles = [element.rendered_text() for element in figure_titles]
    if len(figure_titles) != 2 or observed_titles != list(HEALTH_FIGURE_TITLES):
        failures.append("health Figure 7.2 title changed")

    ledes = [element for element in elements if "lede" in element.classes and element.visible]
    if (
        len(ledes) != 1
        or ledger is None
        or primary is None
        or reading_band is None
        or boundaries is None
        or len(figure_titles) != 2
        or ledes[0].end_order is None
        or primary.end_order is None
        or reading_band.end_order is None
        or not (
            ledes[0].end_order < ledger.start_order < primary.start_order
            and primary.end_order < reading_band.start_order
            and reading_band.end_order < figure_titles[1].start_order < boundaries.start_order
        )
    ):
        failures.append("health opening order changed")

    if expected.series_count != 4 or expected.function_count != 1:
        failures.append("health payload no longer supports the assumption ledger")
    if expected.years_count != expected.spread_count or expected.years_count <= 0:
        failures.append("health payload no longer supports Figure 7.2")
    return failures


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
        scope_text = boundary.parent.rendered_text() if boundary.parent is not None else ""
        if any(
            boundary_text.count(claim) != 1 or scope_text.count(claim) != 1
            for claim in DETECTION_BOUNDARY_LOCAL_CLAIMS
        ):
            failures.append("detection boundary-local inference locality changed")
        if DETECTION_LEGACY_BELOW_CHANCE_CLAIM in scope_text:
            failures.append("detection boundary-local inference is duplicated")

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


def space_field_note_failures_for_text(html: str) -> list[str]:
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
            f"expected exactly one visible space reading map, found {len(maps)}",
        ]

    reading_map = maps[0]
    if reading_map.tag != "nav":
        failures.append("space reading map is not a <nav>")
    if reading_map.attributes.get("aria-label") != "本章閱讀地圖":
        failures.append("space reading map accessible label changed")
    links = [
        element for element in visible if element.tag == "a" and element.is_inside(reading_map)
    ]
    marked_links = [
        element
        for element in visible
        if "data-chapter-reading-link" in element.attributes and element.is_inside(reading_map)
    ]
    if marked_links != links:
        failures.append("space reading-map link markers changed")
    observed = [(link.attributes.get("href", ""), link.rendered_text()) for link in links]
    if observed != list(SPACE_READING_MAP):
        failures.append(f"space reading-map links changed: {observed!r}")

    targets: list[Element] = []
    for href, question in SPACE_READING_MAP:
        target_id = href.removeprefix("#")
        candidates = [
            element
            for element in parser.elements
            if element.attributes.get("id") == target_id
            and element.tag not in IGNORED_SUBTREES
            and not any(
                ancestor.tag in IGNORED_SUBTREES
                for ancestor in parser.elements
                if element.is_inside(ancestor)
            )
        ]
        visible_candidates = [element for element in candidates if element.visible]
        if len(candidates) != 1 or len(visible_candidates) != 1:
            failures.append(
                "space reading-map target inventory changed for "
                f"{href}: {len(candidates)} total, {len(visible_candidates)} visible"
            )
            continue
        target = visible_candidates[0]
        targets.append(target)
        if target.tag != "h2" or "data-space-field-question" not in target.attributes:
            failures.append(f"space field-question heading hierarchy changed for {href}")
        if target.rendered_text() != question:
            failures.append(f"space field-question text changed for {href}")

    marked_targets = [
        element for element in visible if "data-space-field-question" in element.attributes
    ]
    if len(marked_targets) != len(SPACE_READING_MAP) or any(
        target not in marked_targets for target in targets
    ):
        failures.append("space field-question heading inventory changed")
    targets_are_ordered = False
    if len(targets) == len(SPACE_READING_MAP):
        starts = [target.start_order for target in targets]
        targets_are_ordered = starts == sorted(starts)
        if not targets_are_ordered:
            failures.append("space reading-map target order changed")

    intros = [
        element
        for element in visible
        if element.tag == "header" and "chapter-intro" in element.classes
    ]
    theses = [element for element in visible if "chapter-thesis" in element.classes]
    if (
        len(intros) != 1
        or len(theses) != 1
        or reading_map.parent is not intros[0]
        or theses[0].parent is not intros[0]
        or theses[0].end_order is None
        or reading_map.end_order is None
        or theses[0].end_order >= reading_map.start_order
        or len(targets) != len(SPACE_READING_MAP)
        or reading_map.end_order >= targets[0].start_order
    ):
        failures.append("space reading map source order changed")

    figures = [element for element in visible if "evidence-figure" in element.classes]
    if len(figures) != 2:
        failures.append(f"space evidence-figure inventory changed: {len(figures)}")
    primary = [element for element in visible if "data-primary-evidence" in element.attributes]
    if len(primary) != 1:
        failures.append(f"space primary-evidence inventory changed: {len(primary)}")
    elif len(figures) != 2 or primary[0] is not figures[0]:
        failures.append("space primary evidence is not Figure 3.1")
    elif len(targets) == len(SPACE_READING_MAP):
        if primary[0].start_order <= targets[0].start_order:
            failures.append("space primary evidence precedes its question")
        if primary[0].start_order >= targets[1].start_order:
            failures.append("space primary evidence no longer follows the first question")
    if len(figures) == 2 and len(targets) == len(SPACE_READING_MAP) and targets_are_ordered:
        if figures[1].start_order <= targets[1].start_order:
            failures.append("space Figure 3.2 precedes its question")
        if figures[1].start_order >= targets[2].start_order:
            failures.append("space Figure 3.2 no longer precedes the third question")

    tables = [element for element in visible if element.tag == "table"]
    if len(tables) != 2:
        failures.append(f"space table inventory changed: {len(tables)}")
    elif (
        len(targets) == len(SPACE_READING_MAP)
        and targets_are_ordered
        and any(table.start_order <= targets[2].start_order for table in tables)
    ):
        failures.append("space table precedes its question")

    supporting: list[Element] = []
    headings = [
        element for element in visible if element.tag in {"h1", "h2", "h3", "h4", "h5", "h6"}
    ]
    for text in SPACE_SUPPORTING_HEADINGS:
        candidates = [element for element in headings if element.rendered_text() == text]
        if len(candidates) != 1 or candidates[0].tag != "h3":
            failures.append(f"space supporting heading hierarchy changed for {text!r}")
        if len(candidates) == 1:
            supporting.append(candidates[0])
    if len(supporting) == len(SPACE_SUPPORTING_HEADINGS):
        starts = [heading.start_order for heading in supporting]
        if starts != sorted(starts):
            failures.append("space supporting heading order changed")
        if (
            len(targets) == len(SPACE_READING_MAP)
            and targets_are_ordered
            and not (
                targets[1].start_order
                < supporting[0].start_order
                < supporting[1].start_order
                < targets[2].start_order
                < supporting[2].start_order
                < supporting[3].start_order
            )
        ):
            failures.append("space supporting heading grouping changed")
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


def sources_atlas_failures_for_text(html: str) -> list[str]:
    parser = StructureParser()
    parser.feed(html)
    parser.close()
    parser.finish()
    failures = list(parser.errors)
    visible = [element for element in parser.elements if element.visible]

    intros = [
        element
        for element in visible
        if element.tag == "header" and "chapter-intro" in element.classes
    ]
    if len(intros) != 1:
        failures.append(f"sources chapter intro inventory changed: {len(intros)}")
        return failures
    intro = intros[0]

    boundaries = [
        element for element in visible if "data-sources-method-boundary" in element.attributes
    ]
    if len(boundaries) != 1:
        failures.append(f"sources method-boundary inventory changed: {len(boundaries)}")
        return failures
    boundary = boundaries[0]
    if not boundary.is_inside(intro):
        failures.append("sources method boundary left the chapter intro")
    boundary_text = boundary.rendered_text()
    for claim in (
        "CBPF 描述條件機率，不識別污染來源",
        "尖峰風速不等於來源距離",
    ):
        if claim not in boundary_text:
            failures.append(f"sources method boundary claim changed: missing {claim!r}")

    ledes = [
        element for element in visible if "lede" in element.classes and element.is_inside(intro)
    ]
    if len(ledes) != 1:
        failures.append(f"sources lede inventory changed: {len(ledes)}")
        return failures
    lede = ledes[0]

    pickers = [element for element in visible if "data-sources-picker" in element.attributes]
    if len(pickers) != 1:
        failures.append(f"sources picker inventory changed: {len(pickers)}")
        return failures
    picker = pickers[0]
    selects = [
        element
        for element in visible
        if element.tag == "select"
        and element.attributes.get("id") == "cbpf-station"
        and element.is_inside(picker)
    ]
    if not selects:
        failures.append("sources picker inventory changed: missing visible select#cbpf-station")

    primary_evidence = [
        element for element in visible if "data-primary-evidence" in element.attributes
    ]
    if len(primary_evidence) != 1:
        failures.append(f"sources primary evidence inventory changed: {len(primary_evidence)}")
        return failures
    primary = primary_evidence[0]
    primary_text = primary.rendered_text()
    if "圖 4.1" not in primary_text or "高濃度空氣在什麼風向與風速條件下出現？" not in primary_text:
        failures.append("sources primary evidence identity changed")

    if (
        lede.end_order is None
        or lede.end_order >= boundary.start_order
        or boundary.start_order >= picker.start_order
        or picker.start_order >= primary.start_order
    ):
        failures.append("sources opening source order changed")
    return failures


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


def _run_explorer_preflight() -> None:
    labels = [label for label, _ in EXPLORER_EXAMPLES]
    sql_values = [f"SELECT {index};" for index in range(1, len(labels) + 1)]
    expected_examples = tuple(
        (label, hashlib.sha256(sql.encode("utf-8")).hexdigest())
        for label, sql in zip(labels, sql_values, strict=True)
    )
    steps = [
        (
            f'<li data-explorer-step="{key}"><span class="explorer-step-number">'
            f"{index + 1:02d}</span><strong>{title}</strong><span>{description}</span></li>"
        )
        for index, (key, title, description) in enumerate(EXPLORER_STEPS)
    ]
    guide = '<ol data-explorer-path aria-label="查詢步驟">' + "".join(steps) + "</ol>"
    options = [f'<option value="{index}">{label}</option>' for index, label in enumerate(labels)]
    controls = (
        '<div data-explorer-controls><select id="example-select">'
        + "".join(options)
        + '</select><button id="run" type="button">執行查詢</button>'
        '<span id="status" role="status" aria-live="polite"></span></div>'
    )
    sql_panel = (
        '<details data-explorer-sql><summary>SQL</summary><textarea id="sql"></textarea></details>'
    )
    tables = '<div id="tables" data-explorer-tables>tables</div>'
    result = '<div id="result" data-explorer-result tabindex="-1"></div>'
    caveat = "<div data-explorer-caveat>caveat</div>"
    nojs = "<p data-explorer-nojs>no JavaScript</p>"
    script_json = json.dumps(sql_values, ensure_ascii=False, separators=(",", ":"))
    script = f'<script id="explorer-examples" type="application/json">{script_json}</script>'
    primary = (
        '<div class="primary-tool" data-primary-evidence><h2>Tool</h2>'
        f"{guide}{nojs}{controls}</div>"
    )
    valid = (
        '<section data-explorer-workspace data-explorer-state="initial">'
        f"{primary}{sql_panel}{tables}{result}{caveat}</section>{script}"
    )
    valid_failures = explorer_guided_workspace_failures_for_text(valid, expected_examples)
    if valid_failures:
        raise RuntimeError(
            f"explore guided-workspace preflight rejected the valid control: {valid_failures}"
        )

    def changed(name: str, original: str, old: str, new: str) -> str:
        if old not in original:
            raise RuntimeError(f"explore mutation {name} did not reach its fixture seam")
        mutated = original.replace(old, new, 1)
        if mutated == original:
            raise RuntimeError(f"explore mutation {name} did not change the fixture")
        return mutated

    reordered_steps = guide.replace(steps[0] + steps[1], steps[1] + steps[0], 1)
    reordered_options = controls.replace(options[0] + options[1], options[1] + options[0], 1)
    reordered_sql = json.dumps(
        [sql_values[1], sql_values[0], *sql_values[2:]],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    mutations = {
        "missing guide": (
            "explore guide inventory changed",
            changed("missing guide", valid, guide, ""),
        ),
        "duplicate guide": (
            "explore guide inventory changed",
            changed("duplicate guide", valid, guide, guide + guide),
        ),
        "missing step": (
            "explore guide step inventory changed",
            changed("missing step", valid, steps[0], ""),
        ),
        "extra step": (
            "explore guide step inventory changed",
            changed(
                "extra step",
                valid,
                "</ol>",
                '<li data-explorer-step="extra"><strong>Extra</strong><span>Extra</span></li></ol>',
            ),
        ),
        "duplicate step": (
            "explore guide step inventory changed",
            changed("duplicate step", valid, steps[0], steps[0] + steps[0]),
        ),
        "reordered steps": (
            "explore guide step order changed",
            changed("reordered steps", valid, guide, reordered_steps),
        ),
        "renamed step": (
            "explore guide step choose content changed",
            changed(
                "renamed step", valid, "<strong>選一個問題</strong>", "<strong>先選資料</strong>"
            ),
        ),
        "hidden step": (
            "explore guide step inventory changed",
            changed(
                "hidden step",
                valid,
                '<li data-explorer-step="choose">',
                '<li data-explorer-step="choose" hidden>',
            ),
        ),
        "template-only step": (
            "explore guide step inventory changed",
            changed("template-only step", valid, steps[0], f"<template>{steps[0]}</template>"),
        ),
        "disclosure-wrapped step": (
            "explore guide steps are not exact direct list items",
            changed(
                "disclosure-wrapped step",
                valid,
                steps[0],
                f"<details open>{steps[0]}</details>",
            ),
        ),
        "guide after controls": (
            "explore guide-to-result source order changed",
            changed(
                "guide after controls", valid, guide + nojs + controls, nojs + controls + guide
            ),
        ),
        "SQL before controls": (
            "explore guide-to-result source order changed",
            changed(
                "SQL before controls",
                valid,
                controls + "</div>" + sql_panel,
                sql_panel + controls + "</div>",
            ),
        ),
        "tables before SQL": (
            "explore guide-to-result source order changed",
            changed("tables before SQL", valid, sql_panel + tables, tables + sql_panel),
        ),
        "result before tables": (
            "explore guide-to-result source order changed",
            changed("result before tables", valid, tables + result, result + tables),
        ),
        "caveat before result": (
            "explore guide-to-result source order changed",
            changed("caveat before result", valid, result + caveat, caveat + result),
        ),
        "missing label": (
            "explore example option values changed",
            changed("missing label", valid, options[0], ""),
        ),
        "duplicate label": (
            "explore example option values changed",
            changed("duplicate label", valid, options[0], options[0] + options[0]),
        ),
        "reordered labels": (
            "explore example option values changed",
            changed("reordered labels", valid, controls, reordered_options),
        ),
        "changed label": (
            "explore example labels changed",
            changed("changed label", valid, labels[0], f"{labels[0]}（改）"),
        ),
        "missing SQL": (
            "explore SQL identity or order changed",
            changed(
                "missing SQL",
                valid,
                script_json,
                json.dumps(sql_values[1:], ensure_ascii=False, separators=(",", ":")),
            ),
        ),
        "duplicate SQL": (
            "explore SQL identity or order changed",
            changed(
                "duplicate SQL",
                valid,
                script_json,
                json.dumps([sql_values[0], *sql_values], ensure_ascii=False, separators=(",", ":")),
            ),
        ),
        "reordered SQL": (
            "explore SQL identity or order changed",
            changed("reordered SQL", valid, script_json, reordered_sql),
        ),
        "changed SQL": (
            "explore SQL identity or order changed",
            changed(
                "changed SQL",
                valid,
                script_json,
                json.dumps(
                    ["SELECT 99;", *sql_values[1:]],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            ),
        ),
        "duplicate controls": (
            "explore controls inventory changed",
            changed("duplicate controls", valid, controls, controls + controls),
        ),
        "duplicate result": (
            "explore result inventory changed",
            changed("duplicate result", valid, result, result + result),
        ),
    }
    for name, (expected_prefix, mutation) in mutations.items():
        mutation_failures = explorer_guided_workspace_failures_for_text(mutation, expected_examples)
        if not any(failure.startswith(expected_prefix) for failure in mutation_failures):
            raise RuntimeError(
                f"explore preflight did not reject {name} for the expected reason: "
                f"{mutation_failures}"
            )


def _run_preflight() -> None:
    _run_explorer_preflight()
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

    valid_station_dossier = """
<div data-station-picker>
<p id="station-say" role="status" aria-live="polite"></p>
<div data-station-controls><label><span>測站</span><select id="station-select">
<option value="甲站" selected>甲站</option><option value="乙站">乙站</option>
</select></label></div>
<article data-station-report data-station="甲站">
<header data-station-identity><h2><span data-station-name>甲站</span></h2><span data-station-year>2025 年</span></header>
<div data-station-stats>
<div data-station-stat="annual-mean">10</div>
<div data-station-stat="who-annual">2×</div>
<div data-station-stat="who-days">20</div>
<div data-station-stat="taiwan-days">3</div>
</div>
<div data-station-comparisons>
<p data-station-comparison="rank">2</p>
<p data-station-comparison="worst-day">30</p>
</div>
</article>
<article data-station-report data-station="乙站" hidden>
<header data-station-identity><h2><span data-station-name>乙站</span></h2><span data-station-year>2024 年</span></header>
<div data-station-stats>
<div data-station-stat="annual-mean">11</div>
<div data-station-stat="who-annual">2.2×</div>
<div data-station-stat="who-days">21</div>
<div data-station-stat="taiwan-days">4</div>
</div>
<div data-station-comparisons>
<p data-station-comparison="rank">3</p>
<p data-station-comparison="worst-day">31</p>
</div>
</article>
<p data-station-standard-note>同一把尺</p>
</div>
"""
    valid_station_failures = station_dossier_failures_for_text(valid_station_dossier)
    if valid_station_failures:
        raise RuntimeError(
            f"station dossier preflight rejected the valid control: {valid_station_failures}"
        )

    first_option = '<option value="甲站" selected>甲站</option>'
    second_option = '<option value="乙站">乙站</option>'
    select_block = f'<select id="station-select">\n{first_option}{second_option}\n</select>'
    first_report = '<article data-station-report data-station="甲站">'
    second_report = '<article data-station-report data-station="乙站" hidden>'
    first_identity = (
        "<header data-station-identity><h2><span data-station-name>甲站</span></h2>"
        "<span data-station-year>2025 年</span></header>"
    )
    first_stats = """<div data-station-stats>
<div data-station-stat="annual-mean">10</div>
<div data-station-stat="who-annual">2×</div>
<div data-station-stat="who-days">20</div>
<div data-station-stat="taiwan-days">3</div>
</div>"""
    first_comparisons = """<div data-station-comparisons>
<p data-station-comparison="rank">2</p>
<p data-station-comparison="worst-day">30</p>
</div>"""
    first_report_block = (
        f"{first_report}\n{first_identity}\n{first_stats}\n{first_comparisons}\n</article>"
    )
    second_report_block = """<article data-station-report data-station="乙站" hidden>
<header data-station-identity><h2><span data-station-name>乙站</span></h2><span data-station-year>2024 年</span></header>
<div data-station-stats>
<div data-station-stat="annual-mean">11</div>
<div data-station-stat="who-annual">2.2×</div>
<div data-station-stat="who-days">21</div>
<div data-station-stat="taiwan-days">4</div>
</div>
<div data-station-comparisons>
<p data-station-comparison="rank">3</p>
<p data-station-comparison="worst-day">31</p>
</div>
</article>"""
    annual_stat = '<div data-station-stat="annual-mean">10</div>'
    rank_comparison = '<p data-station-comparison="rank">2</p>'
    standard_note = "<p data-station-standard-note>同一把尺</p>"
    without_standard_note = valid_station_dossier.replace(
        "\n" + standard_note,
        "",
        1,
    )
    station_mutations = {
        "missing picker": (
            "station picker inventory changed",
            valid_station_dossier.replace(" data-station-picker", "", 1),
        ),
        "missing select": (
            "station select inventory changed",
            valid_station_dossier.replace(' id="station-select"', "", 1),
        ),
        "duplicate select": (
            "station select inventory changed",
            valid_station_dossier.replace(select_block, select_block + select_block, 1),
        ),
        "duplicate option": (
            "station option values are not unique",
            valid_station_dossier.replace(second_option, first_option.replace(" selected", ""), 1),
        ),
        "missing report": (
            "station selector and report order changed",
            valid_station_dossier.replace(second_report, '<article data-station="乙站" hidden>', 1),
        ),
        "duplicate report": (
            "station report identities are not unique",
            valid_station_dossier.replace(
                second_report,
                '<article data-station-report data-station="甲站" hidden>',
                1,
            ),
        ),
        "reordered options": (
            "station selector and report order changed",
            valid_station_dossier.replace(
                first_option + second_option, second_option + first_option, 1
            ),
        ),
        "reordered reports": (
            "station selector and report order changed",
            valid_station_dossier.replace(
                first_report_block + "\n" + second_report_block,
                second_report_block + "\n" + first_report_block,
                1,
            ),
        ),
        "zero visible reports": (
            "station visible report inventory changed",
            valid_station_dossier.replace(first_report, first_report[:-1] + " hidden>", 1),
        ),
        "two visible reports": (
            "station visible report inventory changed",
            valid_station_dossier.replace(second_report, second_report.replace(" hidden", ""), 1),
        ),
        "selected report mismatch": (
            "station selector and visible identity disagree",
            valid_station_dossier.replace(" selected", "", 1).replace(
                second_option, '<option value="乙站" selected>乙站</option>', 1
            ),
        ),
        "missing identity": (
            "station identity inventory changed",
            valid_station_dossier.replace(" data-station-identity", "", 1),
        ),
        "missing displayed station name": (
            "station displayed-name inventory changed",
            valid_station_dossier.replace(" data-station-name", "", 1),
        ),
        "wrong displayed station name": (
            "station displayed identity disagrees",
            valid_station_dossier.replace(">甲站</span>", ">錯站</span>", 1),
        ),
        "hidden displayed station name": (
            "station displayed name is locally hidden",
            valid_station_dossier.replace(" data-station-name", " data-station-name hidden", 1),
        ),
        "aria-hidden displayed station name": (
            "station displayed name is locally hidden",
            valid_station_dossier.replace(
                " data-station-name", ' data-station-name aria-hidden="true"', 1
            ),
        ),
        "inline-hidden displayed station name": (
            "station displayed name is locally hidden",
            valid_station_dossier.replace(
                " data-station-name", ' data-station-name style="display: none"', 1
            ),
        ),
        "missing year": (
            "station year inventory changed",
            valid_station_dossier.replace(" data-station-year", "", 1),
        ),
        "missing stat": (
            "station primary-stat keys changed",
            valid_station_dossier.replace(annual_stat, "", 1),
        ),
        "duplicate stat": (
            "station primary-stat keys changed",
            valid_station_dossier.replace(annual_stat, annual_stat + annual_stat, 1),
        ),
        "identity after stats": (
            "station report source order changed",
            valid_station_dossier.replace(
                first_identity + "\n" + first_stats,
                first_stats + "\n" + first_identity,
                1,
            ),
        ),
        "comparisons before stats": (
            "station report source order changed",
            valid_station_dossier.replace(
                first_stats + "\n" + first_comparisons,
                first_comparisons + "\n" + first_stats,
                1,
            ),
        ),
        "missing comparisons": (
            "station comparison inventory changed",
            valid_station_dossier.replace(" data-station-comparisons", "", 1),
        ),
        "missing comparison": (
            "station comparison keys changed",
            valid_station_dossier.replace(rank_comparison, "", 1),
        ),
        "duplicate standard note": (
            "station standard-note inventory changed",
            valid_station_dossier.replace(standard_note, standard_note + standard_note, 1),
        ),
        "conversion note returned": (
            "station conversion-note returned",
            valid_station_dossier.replace(
                standard_note,
                standard_note + "<p data-station-conversion-note>粗略換算</p>",
                1,
            ),
        ),
        "note before reports": (
            "station interpretation notes do not follow reports",
            valid_station_dossier.replace(standard_note, "", 1).replace(
                first_report, standard_note + first_report, 1
            ),
        ),
        "note nested in final report": (
            "station interpretation notes do not follow reports",
            without_standard_note.replace(
                second_report_block,
                second_report_block.replace("</article>", standard_note + "</article>", 1),
                1,
            ),
        ),
        "visible reading map": (
            "station chapter unexpectedly contains a reading map",
            valid_station_dossier.replace(
                '<p id="station-say"',
                '<nav data-chapter-reading-map>Map</nav><p id="station-say"',
                1,
            ),
        ),
    }

    valid_space_field_note = """
<header class="chapter-intro"><h1>空間結構與官方分區</h1><div class="chapter-thesis">既有 thesis</div>
<nav aria-label="本章閱讀地圖" data-chapter-reading-map><ol>
<li><a data-chapter-reading-link href="#space-distance">距離增加後，殘差相依如何改變？</a></li>
<li><a data-chapter-reading-link href="#space-controls">哪一種分層真正移除了大部分相依？</a></li>
<li><a data-chapter-reading-link href="#space-inference">剩餘相依對推論與空白區預測有什麼代價？</a></li>
</ol></nav></header>
<h2 id="space-distance" data-space-field-question>距離增加後，殘差相依如何改變？</h2>
<section class="evidence-figure" data-primary-evidence><figure>圖 3.1</figure></section>
<h2 id="space-controls" data-space-field-question>哪一種分層真正移除了大部分相依？</h2>
<section class="evidence-figure"><figure>圖 3.2</figure></section>
<h3>官方分區比純地理多知道什麼？</h3>
<h3>相依散在整個場，不在少數熱點</h3>
<h2 id="space-inference" data-space-field-question>剩餘相依對推論與空白區預測有什麼代價？</h2>
<h3>把 t 統計量重新標價</h3><table><tbody><tr><td>推論</td></tr></tbody></table>
<h3>測站之間的空白能不能誠實地補？</h3><table><tbody><tr><td>外推</td></tr></tbody></table>
"""
    valid_space_failures = space_field_note_failures_for_text(valid_space_field_note)
    if valid_space_failures:
        raise RuntimeError(
            f"space field-note preflight rejected the valid control: {valid_space_failures}"
        )

    space_map = valid_space_field_note[
        valid_space_field_note.index(
            '<nav aria-label="本章閱讀地圖"'
        ) : valid_space_field_note.index("</nav>") + len("</nav>")
    ]
    space_second_link = (
        '<li><a data-chapter-reading-link href="#space-controls">'
        "哪一種分層真正移除了大部分相依？</a></li>"
    )
    space_third_link = (
        '<li><a data-chapter-reading-link href="#space-inference">'
        "剩餘相依對推論與空白區預測有什麼代價？</a></li>"
    )
    space_first_target = (
        '<h2 id="space-distance" data-space-field-question>距離增加後，殘差相依如何改變？</h2>'
    )
    space_second_target = (
        '<h2 id="space-controls" data-space-field-question>哪一種分層真正移除了大部分相依？</h2>'
    )
    space_third_target = (
        '<h2 id="space-inference" data-space-field-question>'
        "剩餘相依對推論與空白區預測有什麼代價？</h2>"
    )
    space_second_figure = '<section class="evidence-figure"><figure>圖 3.2</figure></section>'
    space_first_table = "<table><tbody><tr><td>推論</td></tr></tbody></table>"
    space_second_table = "<table><tbody><tr><td>外推</td></tr></tbody></table>"
    space_mutations = {
        "missing map": (
            "expected exactly one visible space reading map",
            valid_space_field_note.replace(space_map, "", 1),
        ),
        "duplicate map": (
            "expected exactly one visible space reading map",
            valid_space_field_note.replace(space_map, space_map + space_map, 1),
        ),
        "map outside intro after first target": (
            "space reading map source order changed",
            valid_space_field_note.replace(space_map, "", 1).replace(
                space_first_target, space_first_target + space_map, 1
            ),
        ),
        "missing link": (
            "space reading-map links changed",
            valid_space_field_note.replace(space_third_link, "", 1),
        ),
        "reordered links": (
            "space reading-map links changed",
            valid_space_field_note.replace(
                space_second_link + "\n" + space_third_link,
                space_third_link + "\n" + space_second_link,
                1,
            ),
        ),
        "wrong href": (
            "space reading-map links changed",
            valid_space_field_note.replace('href="#space-controls"', 'href="#space-distance"', 1),
        ),
        "duplicate target": (
            "space reading-map target inventory changed",
            valid_space_field_note.replace('id="space-inference"', 'id="space-controls"', 1),
        ),
        "hidden target": (
            "space reading-map target inventory changed",
            valid_space_field_note.replace(
                '<h2 id="space-inference"', '<h2 hidden id="space-inference"', 1
            ),
        ),
        "hidden duplicate target": (
            "space reading-map target inventory changed",
            valid_space_field_note.replace(
                space_first_target,
                space_first_target + '<h2 hidden id="space-distance">重複目標</h2>',
                1,
            ),
        ),
        "wrong target heading level": (
            "space field-question heading hierarchy changed",
            valid_space_field_note.replace(
                space_second_target, space_second_target.replace("h2", "h3"), 1
            ),
        ),
        "reordered targets": (
            "space reading-map target order changed",
            valid_space_field_note.replace(space_second_target, "__SPACE_SECOND_TARGET__", 1)
            .replace(space_third_target, space_second_target, 1)
            .replace("__SPACE_SECOND_TARGET__", space_third_target, 1),
        ),
        "primary before first target": (
            "space primary evidence precedes its question",
            valid_space_field_note.replace(
                space_first_target + '\n<section class="evidence-figure" data-primary-evidence>'
                "<figure>圖 3.1</figure></section>",
                '<section class="evidence-figure" data-primary-evidence><figure>圖 3.1</figure>'
                f"</section>\n{space_first_target}",
                1,
            ),
        ),
        "primary marker rebound away from Figure 3.1": (
            "space primary evidence is not Figure 3.1",
            valid_space_field_note.replace(
                '<section class="evidence-figure" data-primary-evidence>',
                '<p data-primary-evidence>假證據</p><section class="evidence-figure">',
                1,
            ),
        ),
        "changed figure inventory": (
            "space evidence-figure inventory changed",
            valid_space_field_note.replace('class="evidence-figure"', 'class="other-figure"', 1),
        ),
        "changed table inventory": (
            "space table inventory changed",
            valid_space_field_note.replace("<table>", "<div>", 1).replace("</table>", "</div>", 1),
        ),
        "Figure 3.2 before its question": (
            "space Figure 3.2 precedes its question",
            valid_space_field_note.replace(space_second_figure, "", 1).replace(
                space_second_target, space_second_figure + "\n" + space_second_target, 1
            ),
        ),
        "first table before its question": (
            "space table precedes its question",
            valid_space_field_note.replace(space_first_table, "", 1).replace(
                space_third_target, space_first_table + "\n" + space_third_target, 1
            ),
        ),
        "second table before its question": (
            "space table precedes its question",
            valid_space_field_note.replace(space_second_table, "", 1).replace(
                space_third_target, space_second_table + "\n" + space_third_target, 1
            ),
        ),
        "supporting heading promoted to h2": (
            "space supporting heading hierarchy changed",
            valid_space_field_note.replace(
                "<h3>官方分區比純地理多知道什麼？</h3>",
                "<h2>官方分區比純地理多知道什麼？</h2>",
                1,
            ),
        ),
    }
    for key in STATION_STAT_KEYS:
        station_mutations[f"missing {key} stat key"] = (
            "station primary-stat keys changed",
            valid_station_dossier.replace(
                f'data-station-stat="{key}"', f'data-removed-station-stat="{key}"', 1
            ),
        )
    for key in STATION_COMPARISON_KEYS:
        station_mutations[f"missing {key} comparison key"] = (
            "station comparison keys changed",
            valid_station_dossier.replace(
                f'data-station-comparison="{key}"',
                f'data-removed-station-comparison="{key}"',
                1,
            ),
        )
    for name, (expected_failure, html) in station_mutations.items():
        if html == valid_station_dossier:
            raise RuntimeError(f"station dossier preflight did not apply {name}")
        mutation_failures = station_dossier_failures_for_text(html)
        if not any(expected_failure in failure for failure in mutation_failures):
            raise RuntimeError(
                f"station dossier preflight did not reject {name} for the expected reason: "
                f"{mutation_failures}"
            )
    for name, (expected_failure, html) in space_mutations.items():
        if html == valid_space_field_note:
            raise RuntimeError(f"space field-note preflight did not apply {name}")
        mutation_failures = space_field_note_failures_for_text(html)
        if not any(expected_failure in failure for failure in mutation_failures):
            raise RuntimeError(
                f"space field-note preflight did not reject {name} for the expected reason: "
                f"{mutation_failures}"
            )
    reordered_failures = space_field_note_failures_for_text(space_mutations["reordered targets"][1])
    if reordered_failures != ["space reading-map target order changed"]:
        raise RuntimeError(
            f"space field-note preflight did not isolate reordered targets: {reordered_failures}"
        )

    valid_sources_atlas = """
<section id="sources" class="page">
<header class="chapter-intro"><h1>污染來向與風速條件</h1>
<div class="chapter-thesis">既有 thesis</div>
<p class="lede">既有 lede</p>
<aside data-sources-method-boundary><strong>先讀方法界線</strong>
<p>CBPF 描述條件機率，不識別污染來源；尖峰風速不等於來源距離。</p></aside>
</header>
<div class="picker-row" data-sources-picker><select id="cbpf-station"><option selected>甲站</option></select></div>
<section class="evidence-figure" data-primary-evidence><figure>
<p class="figure-number">圖 4.1</p><h2>高濃度空氣在什麼風向與風速條件下出現？</h2>
</figure></section>
</section>
"""
    valid_sources_failures = sources_atlas_failures_for_text(valid_sources_atlas)
    if valid_sources_failures:
        raise RuntimeError(
            f"sources atlas preflight rejected the valid control: {valid_sources_failures}"
        )

    sources_mutations = {
        "missing boundary": "sources method-boundary inventory changed",
        "duplicate boundary": "sources method-boundary inventory changed",
        "hidden boundary": "sources method-boundary inventory changed",
        "aria-hidden boundary": "sources method-boundary inventory changed",
        "missing conditional-probability claim": "sources method boundary claim changed",
        "replaced source-attribution claim": "sources method boundary claim changed",
        "relocated approved claims": "sources method boundary claim changed",
        "boundary outside intro": "sources method boundary left the chapter intro",
        "picker before boundary": "sources opening source order changed",
        "figure before boundary": "sources opening source order changed",
        "figure before picker": "sources opening source order changed",
        "missing picker marker": "sources picker inventory changed",
        "non-primary Figure 4.1": "sources primary evidence inventory changed",
    }
    boundary = (
        "<aside data-sources-method-boundary><strong>先讀方法界線</strong>"
        "\n<p>CBPF 描述條件機率，不識別污染來源；尖峰風速不等於來源距離。</p></aside>"
    )
    picker = (
        '<div class="picker-row" data-sources-picker><select id="cbpf-station">'
        "<option selected>甲站</option></select></div>"
    )
    primary = (
        '<section class="evidence-figure" data-primary-evidence><figure>\n'
        '<p class="figure-number">圖 4.1</p><h2>高濃度空氣在什麼風向與風速條件下出現？</h2>\n'
        "</figure></section>"
    )
    sources_mutated_html = {
        "missing boundary": valid_sources_atlas.replace(boundary, "", 1),
        "duplicate boundary": valid_sources_atlas.replace(boundary, boundary + boundary, 1),
        "hidden boundary": valid_sources_atlas.replace(
            "<aside data-sources-method-boundary>",
            "<aside data-sources-method-boundary hidden>",
            1,
        ),
        "aria-hidden boundary": valid_sources_atlas.replace(
            "<aside data-sources-method-boundary>",
            '<aside data-sources-method-boundary aria-hidden="true">',
            1,
        ),
        "missing conditional-probability claim": valid_sources_atlas.replace(
            "CBPF 描述條件機率，不識別污染來源；", "", 1
        ),
        "replaced source-attribution claim": valid_sources_atlas.replace(
            "不識別污染來源", "識別污染來源", 1
        ),
        "relocated approved claims": valid_sources_atlas.replace(
            boundary + "\n</header>",
            "<aside data-sources-method-boundary><strong>先讀方法界線</strong>"
            "\n<p>先確認方法可回答的問題。</p></aside>\n</header>\n"
            "<p>CBPF 描述條件機率，不識別污染來源；尖峰風速不等於來源距離。</p>",
            1,
        ),
        "boundary outside intro": valid_sources_atlas.replace(
            boundary + "\n</header>", "</header>\n" + boundary, 1
        ),
        "picker before boundary": valid_sources_atlas.replace(
            boundary + "\n</header>\n" + picker,
            picker + "\n" + boundary + "\n</header>",
            1,
        ),
        "figure before boundary": valid_sources_atlas.replace(
            boundary + "\n</header>\n" + picker + "\n" + primary,
            primary + "\n" + boundary + "\n</header>\n" + picker,
            1,
        ),
        "figure before picker": valid_sources_atlas.replace(
            picker + "\n" + primary, primary + "\n" + picker, 1
        ),
        "missing picker marker": valid_sources_atlas.replace(" data-sources-picker", "", 1),
        "non-primary Figure 4.1": valid_sources_atlas.replace(" data-primary-evidence", "", 1),
    }
    for name, expected_failure in sources_mutations.items():
        mutation_html = sources_mutated_html[name]
        if mutation_html == valid_sources_atlas:
            raise RuntimeError(f"sources atlas preflight did not apply {name}")
        mutation_failures = sources_atlas_failures_for_text(mutation_html)
        if not any(expected_failure in failure for failure in mutation_failures):
            raise RuntimeError(
                f"sources atlas preflight did not reject {name} for the expected reason: "
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
        "fractional observed count": (
            "observed count is invalid",
            lambda payload: payload["events"][0].__setitem__("n_credible", 1.5),
        ),
        "observed NaN": (
            "observed count is invalid",
            lambda payload: payload["events"][0].__setitem__("n_credible", float("nan")),
        ),
        "observed positive infinity": (
            "observed count is invalid",
            lambda payload: payload["events"][0].__setitem__("n_credible", float("inf")),
        ),
        "observed negative infinity": (
            "observed count is invalid",
            lambda payload: payload["events"][0].__setitem__("n_credible", float("-inf")),
        ),
        "negative expected count": (
            "expected count is invalid",
            lambda payload: payload["events"][0].__setitem__("n_expected_by_chance", -0.1),
        ),
        "expected NaN": (
            "expected count is invalid",
            lambda payload: payload["events"][0].__setitem__("n_expected_by_chance", float("nan")),
        ),
        "expected positive infinity": (
            "expected count is invalid",
            lambda payload: payload["events"][0].__setitem__("n_expected_by_chance", float("inf")),
        ),
        "expected negative infinity": (
            "expected count is invalid",
            lambda payload: payload["events"][0].__setitem__("n_expected_by_chance", float("-inf")),
        ),
        "below-chance relationship no longer holds": (
            "no longer supports the below-chance claim",
            lambda payload: payload["events"][0].__setitem__("n_credible", 4),
        ),
    }
    required_numeric_mutations = {
        "fractional observed count",
        "observed NaN",
        "observed positive infinity",
        "observed negative infinity",
        "expected NaN",
        "expected positive infinity",
        "expected negative infinity",
    }
    missing_numeric_mutations = required_numeric_mutations - invalid_payload_mutations.keys()
    if missing_numeric_mutations:
        detection_preflight_misses.append(
            "payload numeric mutation coverage missing: "
            + ", ".join(sorted(missing_numeric_mutations))
        )
    numeric_rejection_counts = dict.fromkeys(required_numeric_mutations, 0)
    for name, (expected_error, mutate) in invalid_payload_mutations.items():
        payload = json.loads(json.dumps(valid_detection_payload))
        mutate(payload)
        try:
            _detection_expected_events_from_payload(payload)
        except ValueError as exc:
            if expected_error not in str(exc):
                detection_preflight_misses.append(f"payload {name} misdiagnosed as {exc!s}")
            elif name in numeric_rejection_counts:
                numeric_rejection_counts[name] += 1
        else:
            detection_preflight_misses.append(f"payload {name} accepted")
    if any(count != 1 for count in numeric_rejection_counts.values()):
        detection_preflight_misses.append(
            "payload numeric mutation branch counts changed: "
            + ", ".join(
                f"{name}={count}" for name, count in sorted(numeric_rejection_counts.items())
            )
        )

    raw_nonfinite_rejection_counts = dict.fromkeys(("NaN", "Infinity", "-Infinity"), 0)
    raw_payload = DETECTION_LIMIT.read_text(encoding="utf-8")
    raw_marker = '"n_credible":1'
    if raw_marker not in raw_payload:
        detection_preflight_misses.append("raw JSON non-finite loader marker changed")
    else:
        with tempfile.TemporaryDirectory(prefix="twair-detection-preflight-") as directory:
            raw_path = pathlib.Path(directory) / "detection-limit.json"
            for constant in raw_nonfinite_rejection_counts:
                raw_path.write_text(
                    raw_payload.replace(raw_marker, f'"n_credible":{constant}', 1),
                    encoding="utf-8",
                )
                with patch.object(sys.modules[__name__], "DETECTION_LIMIT", raw_path):
                    try:
                        load_detection_expected_events()
                    except ValueError as exc:
                        expected_error = (
                            f"detection payload contains invalid JSON number: {constant}"
                        )
                        if str(exc) == expected_error:
                            raw_nonfinite_rejection_counts[constant] += 1
                        else:
                            detection_preflight_misses.append(
                                f"raw JSON {constant} misdiagnosed as {exc!s}"
                            )
                    else:
                        detection_preflight_misses.append(
                            f"raw JSON non-finite constant {constant} accepted"
                        )
    if any(count != 1 for count in raw_nonfinite_rejection_counts.values()):
        detection_preflight_misses.append(
            "raw JSON non-finite loader branch counts changed: "
            + ", ".join(
                f"{constant}={count}" for constant, count in raw_nonfinite_rejection_counts.items()
            )
        )

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
        "legacy below-chance conclusion outside boundary": (
            "detection boundary-local inference is duplicated",
            valid_detection_brief.replace(
                "<p data-detection-method-evidence>",
                "<p>三個事件的實際通過數都低於機率預期。</p><p data-detection-method-evidence>",
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

    valid_methods_index = """
<p class="lede">這一章的對照組是一組常見但有缺陷的分析做法。</p>
<nav class="method-case-index" data-method-case-index aria-labelledby="method-case-index-title">
<h2 id="method-case-index-title">七個案例索引</h2>
<ol>
<li><a data-method-case-link data-case="01" href="#method-case-01"><span aria-hidden="true">01</span><span>月平均抹掉了六成的變異</span></a></li>
<li><a data-method-case-link data-case="02" href="#method-case-02"><span aria-hidden="true">02</span><span>拿 PM10 預測 PM2.5</span></a></li>
<li><a data-method-case-link data-case="03" href="#method-case-03"><span aria-hidden="true">03</span><span>把風向當成 0 到 360 的普通數字</span></a></li>
<li><a data-method-case-link data-case="04" href="#method-case-04"><span aria-hidden="true">04</span><span>把及格標準調低，好讓資料通過檢定</span></a></li>
<li><a data-method-case-link data-case="05" href="#method-case-05"><span aria-hidden="true">05</span><span>NO + NO₂ = NOx，三個一起放進模型</span></a></li>
<li><a data-method-case-link data-case="06" href="#method-case-06"><span aria-hidden="true">06</span><span>只用模型學過的資料評斷它</span></a></li>
<li><a data-method-case-link data-case="07" href="#method-case-07"><span aria-hidden="true">07</span><span>用一句話處理掉所有缺漏值</span></a></li>
</ol>
</nav>
<article class="mistake" id="method-case-01" data-method-case="01"><h2><span aria-hidden="true">01</span><span>月平均抹掉了六成的變異</span></h2><p id="evidence-8-1-title">月平均隱藏了多少逐時變異？</p></article>
<article class="mistake" id="method-case-02" data-method-case="02"><h2><span aria-hidden="true">02</span><span>拿 PM10 預測 PM2.5</span></h2></article>
<article class="mistake" id="method-case-03" data-method-case="03"><h2><span aria-hidden="true">03</span><span>把風向當成 0 到 360 的普通數字</span></h2></article>
<article class="mistake" id="method-case-04" data-method-case="04"><h2><span aria-hidden="true">04</span><span>把及格標準調低，好讓資料通過檢定</span></h2></article>
<article class="mistake" id="method-case-05" data-method-case="05"><h2><span aria-hidden="true">05</span><span>NO + NO₂ = NOx，三個一起放進模型</span></h2></article>
<article class="mistake" id="method-case-06" data-method-case="06"><h2><span aria-hidden="true">06</span><span>只用模型學過的資料評斷它</span></h2></article>
<article class="mistake" id="method-case-07" data-method-case="07"><h2><span aria-hidden="true">07</span><span>用一句話處理掉所有缺漏值</span></h2><p id="evidence-8-2-title">不同補值方法對不同缺口長度付出什麼代價？</p></article>
<article class="mistake epilogue"><h2>兩項經全量資料否證的原始主張</h2></article>
"""
    valid_methods_failures = methods_case_index_failures_for_text(valid_methods_index)
    if valid_methods_failures:
        raise RuntimeError(
            "methods seven-case index preflight rejected the valid control: "
            f"{valid_methods_failures}"
        )

    methods_nav_start = valid_methods_index.index('<nav class="method-case-index"')
    methods_nav_end = valid_methods_index.index("</nav>", methods_nav_start) + len("</nav>")
    methods_nav = valid_methods_index[methods_nav_start:methods_nav_end]
    methods_without_nav = (
        valid_methods_index[:methods_nav_start] + valid_methods_index[methods_nav_end:]
    )
    methods_case_1_start = methods_without_nav.index('<article class="mistake" id="method-case-01"')
    methods_case_1_end = methods_without_nav.index("</article>", methods_case_1_start) + len(
        "</article>"
    )
    methods_index_after_case_1 = (
        methods_without_nav[:methods_case_1_end]
        + "\n"
        + methods_nav
        + methods_without_nav[methods_case_1_end:]
    )

    methods_markup_mutations = {
        "missing link": (
            "methods case link inventory changed",
            valid_methods_index.replace(
                '<li><a data-method-case-link data-case="04" href="#method-case-04"><span aria-hidden="true">04</span><span>把及格標準調低，好讓資料通過檢定</span></a></li>\n',
                "",
                1,
            ),
        ),
        "extra link": (
            "methods case link inventory changed",
            valid_methods_index.replace(
                "</ol>",
                '<li><a data-method-case-link data-case="08" href="#method-case-08"><span aria-hidden="true">08</span><span>多餘案例</span></a></li></ol>',
                1,
            ),
        ),
        "duplicate link identity": (
            "methods case link order changed",
            valid_methods_index.replace('data-case="07"', 'data-case="06"', 1),
        ),
        "unhooked extra row": (
            "methods case index row structure changed",
            valid_methods_index.replace(
                "</ol>", '<li><a href="#method-case-01">未綁定的複本</a></li></ol>', 1
            ),
        ),
        "link changes child semantics": (
            "methods case link structure changed",
            valid_methods_index.replace(
                '<span aria-hidden="true">01</span><span>月平均抹掉了六成的變異</span>',
                '<span aria-hidden="true">01</span><strong>月平均抹掉了六成的變異</strong>',
                1,
            ),
        ),
        "reordered links": (
            "methods case link order changed",
            valid_methods_index.replace('data-case="01"', 'data-case="temporary"', 1)
            .replace('data-case="02"', 'data-case="01"', 1)
            .replace('data-case="temporary"', 'data-case="02"', 1),
        ),
        "renamed link": (
            "methods case link text changed",
            valid_methods_index.replace("拿 PM10 預測 PM2.5", "拿相關性當預測", 1),
        ),
        "redirected link": (
            "methods case link destination changed",
            valid_methods_index.replace('href="#method-case-06"', 'href="#method-case-05"', 1),
        ),
        "missing destination": (
            "methods case destination inventory changed",
            valid_methods_index.replace(' data-method-case="03"', "", 1),
        ),
        "duplicate destination anchor": (
            "methods case destination anchor inventory changed",
            valid_methods_index + '<span id="method-case-01">複本</span>',
        ),
        "destination heading changes child semantics": (
            "methods case destination heading structure changed",
            valid_methods_index.replace(
                '<h2><span aria-hidden="true">01</span><span>月平均抹掉了六成的變異</span></h2>',
                '<h2><span aria-hidden="true">01</span><strong>月平均抹掉了六成的變異</strong></h2>',
                1,
            ),
        ),
        "reordered destinations": (
            "methods case destination order changed",
            valid_methods_index.replace('data-method-case="01"', 'data-method-case="temporary"', 1)
            .replace('data-method-case="02"', 'data-method-case="01"', 1)
            .replace('data-method-case="temporary"', 'data-method-case="02"', 1),
        ),
        "hidden link": (
            "methods case link inventory changed",
            valid_methods_index.replace(
                "<a data-method-case-link", "<a hidden data-method-case-link", 1
            ),
        ),
        "collapsible index": (
            "methods case index is user-collapsible",
            valid_methods_index.replace(
                '<nav class="method-case-index"', '<details open><nav class="method-case-index"', 1
            ).replace("</nav>\n", "</nav></details>\n", 1),
        ),
        "index after case 01": (
            "methods case index no longer precedes case 01",
            methods_index_after_case_1,
        ),
        "changed Figure 8.1": (
            "methods Figure 8.1 title changed",
            valid_methods_index.replace("月平均隱藏了多少逐時變異？", "舊標題", 1),
        ),
        "changed Figure 8.2": (
            "methods Figure 8.2 title changed",
            valid_methods_index.replace("不同補值方法對不同缺口長度付出什麼代價？", "舊標題", 1),
        ),
    }
    for name, (expected_failure, html) in methods_markup_mutations.items():
        if html == valid_methods_index:
            raise RuntimeError(f"methods seven-case index preflight did not apply {name}")
        mutation_failures = methods_case_index_failures_for_text(html)
        if not any(failure.startswith(expected_failure) for failure in mutation_failures):
            raise RuntimeError(
                f"methods seven-case index preflight did not reject {name} for "
                f"{expected_failure}: {mutation_failures}"
            )

    data_download_rows = "".join(
        f"<tr><td>測項 {index:02d}</td><td>1982–2025</td>"
        f'<td><a href="/data/l0/{index:02d}.json" download>JSON'
        f'<span class="size">0.{index:02d} MB</span></a></td>'
        + (
            f'<td><a href="/data/l1/{index:02d}.parquet" download>Parquet'
            f'<span class="size">1.{index:02d} MB</span></a></td></tr>'
            if index <= 2
            else "<td><span data-pages-unavailable>Pages 未發布</span></td></tr>"
        )
        for index in range(1, 22)
    )
    data_expected_descriptions = {
        "L0": "每個測項一個 JSON，含月均值與該月的有效天數。網站直接讀這一層。",
        "L1": "Pages 目前發布 PM10、PM2.5 的 Parquet，共 2.03 MB；其餘測項可由本機管線產生。",
        "L2": (
            "3.40 億筆完整逐時觀測，含每一筆的品管旗標。"
            "不發布— 只發衍生產物與完整管線，執行一次 twair ingest 加 twair build 即可獨立重建。"
        ),
    }
    data_expected_downloads = tuple(
        DataDownloadRow(
            name=f"測項 {index:02d}",
            period="1982–2025",
            l0_href=f"/data/l0/{index:02d}.json",
            l0_size=f"0.{index:02d} MB",
            l1_href=f"/data/l1/{index:02d}.parquet" if index <= 2 else None,
            l1_size=f"1.{index:02d} MB" if index <= 2 else "",
            l1_label="Parquet" if index <= 2 else "Pages 未發布",
        )
        for index in range(1, 22)
    )
    valid_data_register = f"""
<p class="lede">所有數字都可以獨立重算。</p>
<dl class="layers" data-data-layer-register>
<dt data-data-layer="L0"><span data-data-layer-term>L0 站-月</span><span data-data-layer-use>閱讀者 · 快速查值與網站圖表</span></dt>
<dd data-data-layer-description="L0">每個測項一個 JSON，含月均值與該月的有效天數。網站直接讀這一層。</dd>
<dt data-data-layer="L1"><span data-data-layer-term>L1 站-日</span><span data-data-layer-use>分析者 · 逐日查詢與桌面分析</span></dt>
<dd data-data-layer-description="L1">Pages 目前發布 PM10、PM2.5 的 Parquet，共 2.03 MB；其餘測項可由本機管線產生。</dd>
<dt data-data-layer="L2"><span data-data-layer-term>L2 站-時</span><span data-data-layer-use>重現者 · 逐時稽核與管線重建</span></dt>
<dd data-data-layer-description="L2">3.40 億筆完整逐時觀測，含每一筆的品管旗標。<strong>不發布</strong>— 只發衍生產物與完整管線，執行一次 <code>twair ingest</code> 加 <code>twair build</code> 即可獨立重建。</dd>
</dl>
<h2>下載</h2>
<a href="/data/meta.json" download>資料與產製資訊</a>
<a href="/data/l0/index.json" download>L0 測項索引</a>
<table class="dense"><caption>三層資料的 L0 與 L1，逐測項。</caption><tbody>{data_download_rows}</tbody></table>
<h2>授權與再散布</h2>
<p><strong>L2 不發布，理由不是檔案太大。</strong></p>
<p><strong>關於缺值。</strong>這個專案不補值。</p>
<details data-publication-disagreement><summary>官方值不一致</summary><p>這是未解的來源歧異。</p></details>
<p><strong>逐時 PM2.5 的官方但書。</strong>小時值僅供預警參考。</p>
"""
    valid_data_failures = data_provenance_register_failures_for_text(
        valid_data_register, data_expected_descriptions, data_expected_downloads
    )
    if valid_data_failures:
        raise RuntimeError(
            f"data provenance register preflight rejected the valid control: {valid_data_failures}"
        )
    data_l0_pair = (
        '<dt data-data-layer="L0"><span data-data-layer-term>L0 站-月</span>'
        "<span data-data-layer-use>閱讀者 · 快速查值與網站圖表</span></dt>\n"
        '<dd data-data-layer-description="L0">每個測項一個 JSON，含月均值與該月的有效天數。'
        "網站直接讀這一層。</dd>"
    )
    data_l1_pair = (
        '<dt data-data-layer="L1"><span data-data-layer-term>L1 站-日</span>'
        "<span data-data-layer-use>分析者 · 逐日查詢與桌面分析</span></dt>\n"
        '<dd data-data-layer-description="L1">Pages 目前發布 PM10、PM2.5 的 Parquet，共 2.03 MB；'
        "其餘測項可由本機管線產生。</dd>"
    )
    data_row_1 = (
        "<tr><td>測項 01</td><td>1982–2025</td>"
        '<td><a href="/data/l0/01.json" download>JSON<span class="size">0.01 MB</span></a></td>'
        '<td><a href="/data/l1/01.parquet" download>Parquet<span class="size">1.01 MB</span></a></td></tr>'
    )
    data_row_2 = (
        "<tr><td>測項 02</td><td>1982–2025</td>"
        '<td><a href="/data/l0/02.json" download>JSON<span class="size">0.02 MB</span></a></td>'
        '<td><a href="/data/l1/02.parquet" download>Parquet<span class="size">1.02 MB</span></a></td></tr>'
    )
    data_table = (
        '<table class="dense"><caption>三層資料的 L0 與 L1，逐測項。</caption>'
        f"<tbody>{data_download_rows}</tbody></table>"
    )
    data_mutations = {
        "missing level hook": (
            "data layer term inventory changed",
            valid_data_register.replace(' data-data-layer="L0"', "", 1),
        ),
        "extra unhooked pair": (
            "data layer term-description pairing changed",
            valid_data_register.replace("</dl>", "<dt>額外層</dt><dd>額外說明</dd></dl>", 1),
        ),
        "duplicate level": (
            "data layer order changed",
            valid_data_register.replace('data-data-layer="L2"', 'data-data-layer="L1"', 1),
        ),
        "reordered levels": (
            "data layer order changed",
            valid_data_register.replace(
                data_l0_pair + "\n" + data_l1_pair, data_l1_pair + "\n" + data_l0_pair, 1
            ),
        ),
        "renamed use label": (
            "data layer L0 term or use label changed",
            valid_data_register.replace("閱讀者 · 快速查值與網站圖表", "其他用途", 1),
        ),
        "hidden use label": (
            "data layer L0 term or use label changed",
            valid_data_register.replace(
                "<span data-data-layer-use>", "<span hidden data-data-layer-use>", 1
            ),
        ),
        "hidden register": (
            "data provenance register inventory changed",
            valid_data_register.replace('<dl class="layers"', '<dl hidden class="layers"', 1),
        ),
        "collapsible register": (
            "data provenance register is user-collapsible",
            valid_data_register.replace(
                '<dl class="layers"', '<details open><dl class="layers"', 1
            ).replace("</dl>", "</dl></details>", 1),
        ),
        "mismatched description": (
            "data layer L1 description structure changed",
            valid_data_register.replace(
                'data-data-layer-description="L1"', 'data-data-layer-description="L0"', 1
            ),
        ),
        "changed description": (
            "data layer L0 description changed",
            valid_data_register.replace("網站直接讀這一層", "改用其他層", 1),
        ),
        "extended contradictory description": (
            "data layer L0 description changed",
            valid_data_register.replace(
                "網站直接讀這一層。", "網站直接讀這一層，但內容規格已改變。", 1
            ),
        ),
        "table before register": (
            "data provenance register no longer precedes the download table",
            valid_data_register.replace(data_table, "", 1).replace(
                '<dl class="layers"', data_table + '\n<dl class="layers"', 1
            ),
        ),
        "lost download": (
            "data download link inventory changed",
            valid_data_register.replace(" download>JSON", ">JSON", 1),
        ),
        "changed download destination": (
            "data download row 1 changed",
            valid_data_register.replace("/data/l0/01.json", "/data/l0/wrong.json", 1),
        ),
        "reordered download rows": (
            "data download row 1 changed",
            valid_data_register.replace(data_row_1 + data_row_2, data_row_2 + data_row_1, 1),
        ),
        "extra table row": (
            "data download row inventory changed",
            valid_data_register.replace("</tbody>", "<tr><td>額外</td></tr></tbody>", 1),
        ),
        "L2 became downloadable": (
            "data layer L2 became downloadable",
            valid_data_register.replace(
                "<strong>不發布</strong>", '<a href="/l2" download><strong>不發布</strong></a>', 1
            ),
        ),
        "lost L2 boundary": (
            "data L2 boundary statement changed",
            valid_data_register.replace("L2 不發布，理由不是檔案太大。", "L2 不發布。", 1),
        ),
        "lost disagreement": (
            "data publication-disagreement evidence changed",
            valid_data_register.replace(" data-publication-disagreement", "", 1),
        ),
    }
    for name, (expected_failure, html) in data_mutations.items():
        if html == valid_data_register:
            raise RuntimeError(f"data provenance register preflight did not apply {name}")
        mutation_failures = data_provenance_register_failures_for_text(
            html, data_expected_descriptions, data_expected_downloads
        )
        if not any(failure.startswith(expected_failure) for failure in mutation_failures):
            raise RuntimeError(
                f"data provenance register preflight did not reject {name} for "
                f"{expected_failure}: {mutation_failures}"
            )

    valid_forecast_brief = """
<p class="lede">這一章量的是往前看能走多遠還算有用。</p>
<section data-primary-evidence><p id="evidence-6-1-title" class="evidence-title">各預測期距的誤差如何變化？</p><div data-primary-plot>Chart</div><figcaption>Caption</figcaption></section>
<nav aria-labelledby="forecast-decision-title" data-forecast-decision-sheet>
<h2 id="forecast-decision-title">三步決定這個預測還值不值得用</h2>
<ol>
<li data-forecast-decision="error"><a href="#evidence-6-1-title"><strong>誤差</strong><p>先看圖 6.1：模型、persistence 與 climatology 的 RMSE 隨期距如何變化。</p></a></li>
<li data-forecast-decision="skill"><a href="#evidence-6-2-title"><strong>基準優勢</strong><p>再看圖 6.2：同一批預測相對 persistence 與 climatology 還剩多少優勢。</p></a></li>
<li data-forecast-decision="cost"><a href="#forecast-cost"><strong>計算代價</strong><p>最後看成本表與圖 6.3：額外計算是否換得可用的準確度。</p></a></li>
</ol>
</nav>
<section><p id="evidence-6-2-title" class="evidence-title">模型相對兩條基準線何時失去優勢？</p></section>
<div data-forecast-reading-band>
<section data-forecast-reading="r2-skill"><h2>R² 與 skill</h2><p>第一段讀法。</p></section>
<section data-forecast-reading="two-baselines"><h2>兩條基準線</h2><p>第二段讀法。</p></section>
<section data-forecast-reading="split-instability"><h2>分割不穩定</h2><p>第三段讀法。</p></section>
<section data-forecast-reading="shared-feature-bug"><h2>共用特徵管線</h2><p>第四段讀法。</p></section>
</div>
<div data-forecast-baseline-band>
<section data-forecast-baseline="persistence"><h2>persistence <span>「跟現在一樣」</span></h2><p>直接使用此刻濃度。</p><p>這是要超越的門檻。</p></section>
<section data-forecast-baseline="climatology"><h2>climatology <span>「這站這時候的平均」</span></h2><p>使用同測站同月份同小時平均。</p><p>它不看今天發生什麼事。</p></section>
</div>
<h2 id="forecast-cost">被跳過的那個模型</h2>
<p>以及它買到了什麼。</p>
"""
    valid_forecast_expected = ForecastExpectedEvidence(
        horizons=FORECAST_HORIZONS,
        readings=(
            ("R² 與 skill", "第一段讀法。"),
            ("兩條基準線", "第二段讀法。"),
            ("分割不穩定", "第三段讀法。"),
            ("共用特徵管線", "第四段讀法。"),
        ),
        baselines=(
            (
                "persistence",
                "「跟現在一樣」",
                "直接使用此刻濃度。",
                "這是要超越的門檻。",
            ),
            (
                "climatology",
                "「這站這時候的平均」",
                "使用同測站同月份同小時平均。",
                "它不看今天發生什麼事。",
            ),
        ),
    )
    valid_forecast_failures = forecast_horizon_decision_failures_for_text(
        valid_forecast_brief, valid_forecast_expected
    )
    if valid_forecast_failures:
        raise RuntimeError(
            "forecast decision-sheet preflight rejected the valid control: "
            f"{valid_forecast_failures}"
        )

    forecast_markup_mutations = {
        "missing decision row": (
            "forecast decision row inventory changed",
            valid_forecast_brief.replace(
                '<li data-forecast-decision="skill"><a href="#evidence-6-2-title"><strong>基準優勢</strong><p>再看圖 6.2：同一批預測相對 persistence 與 climatology 還剩多少優勢。</p></a></li>\n',
                "",
                1,
            ),
        ),
        "wrong decision destination": (
            "forecast decision destination changed",
            valid_forecast_brief.replace("#forecast-cost", "#evidence-6-3-title", 1),
        ),
        "hidden decision sheet": (
            "forecast decision sheet inventory changed",
            valid_forecast_brief.replace(
                "<nav aria-labelledby=", "<nav hidden aria-labelledby=", 1
            ),
        ),
        "decision loses semantic label": (
            "forecast decision row structure changed",
            valid_forecast_brief.replace("<strong>誤差</strong>", "<span>誤差</span>", 1),
        ),
        "decision before Figure 6.1": (
            "forecast decision sheet no longer follows Figure 6.1",
            valid_forecast_brief.replace(' id="evidence-6-1-title"', "", 1).replace(
                "</nav>\n",
                '</nav>\n<span id="evidence-6-1-title">各預測期距的誤差如何變化？</span>\n',
                1,
            ),
        ),
        "missing reading body": (
            "forecast reading row structure changed",
            valid_forecast_brief.replace("<p>第一段讀法。</p>", "", 1),
        ),
        "reordered reading rows": (
            "forecast reading row order changed",
            valid_forecast_brief.replace(
                'data-forecast-reading="r2-skill"', 'data-forecast-reading="temporary"', 1
            )
            .replace('data-forecast-reading="two-baselines"', 'data-forecast-reading="r2-skill"', 1)
            .replace(
                'data-forecast-reading="temporary"', 'data-forecast-reading="two-baselines"', 1
            ),
        ),
        "changed baseline text": (
            "forecast baseline row text changed",
            valid_forecast_brief.replace("這是要超越的門檻。", "這是可忽略的門檻。", 1),
        ),
        "baseline inside disclosure": (
            "forecast baseline band is user-collapsible",
            valid_forecast_brief.replace(
                "<div data-forecast-baseline-band>",
                "<details open><div data-forecast-baseline-band>",
                1,
            ).replace(
                '</div>\n<h2 id="forecast-cost">', '</div></details>\n<h2 id="forecast-cost">', 1
            ),
        ),
    }
    for name, (expected_failure, html) in forecast_markup_mutations.items():
        if html == valid_forecast_brief:
            raise RuntimeError(f"forecast decision-sheet preflight did not apply {name}")
        mutation_failures = forecast_horizon_decision_failures_for_text(
            html, valid_forecast_expected
        )
        if not any(failure.startswith(expected_failure) for failure in mutation_failures):
            raise RuntimeError(
                f"forecast decision-sheet preflight did not reject {name} for "
                f"{expected_failure}: {mutation_failures}"
            )

    valid_forecast_payload: dict[str, object] = {
        "period": [2024, 2025],
        "target": "PM2.5",
        "validation": "rolling-origin",
        "skill_formula": "skill formula",
        "baselines": [
            {
                "name": name,
                "label": label,
                "what": what,
                "why": why,
            }
            for name, label, what, why in valid_forecast_expected.baselines
        ],
        "reading": [
            {"claim": claim, "detail": detail} for claim, detail in valid_forecast_expected.readings
        ],
        "leakage_note": "lag safety",
        "horizons": [
            {
                "horizon": horizon,
                "n": 100,
                "stations": 2,
                "splits": 2,
                "model_r2": 0.5,
                "skill_persistence": 0.2,
                "skill_persistence_worst": 0.1,
                "skill_climatology": 0.3,
                "skill_climatology_worst": 0.1,
                "splits_not_beating_persistence": 0,
                "model_rmse": 5.0,
                "persistence_rmse": 6.0,
                "climatology_rmse": 7.0,
                "band_nominal": 0.8,
                "band_half_width": 6.0,
                "band_coverage": 0.81,
                "band_coverage_worst": 0.77,
                "band_splits_below_nominal": 1,
                "band_model_rmse": 5.1,
                "per_split": [
                    {
                        "split": f"rolling_{split}",
                        "skill_persistence": 0.2,
                        "skill_climatology": 0.3,
                        "model_r2": 0.5,
                        "band_half_width": 6.0,
                        "band_coverage": 0.81,
                    }
                    for split in (1, 2)
                ],
            }
            for horizon in FORECAST_HORIZONS
        ],
    }
    forecast_payload_control = _forecast_expected_evidence_from_payload(valid_forecast_payload)
    if forecast_payload_control != valid_forecast_expected:
        raise RuntimeError(
            f"forecast payload preflight returned {forecast_payload_control!r}, "
            f"expected {valid_forecast_expected!r}"
        )

    def changed_forecast_payload(**changes: object) -> dict[str, object]:
        payload = copy.deepcopy(valid_forecast_payload)
        payload.update(changes)
        if payload == valid_forecast_payload:
            raise RuntimeError("forecast payload preflight mutation did not change the control")
        return payload

    reordered_horizons = copy.deepcopy(
        cast(list[dict[str, object]], valid_forecast_payload["horizons"])
    )
    reordered_horizons[0], reordered_horizons[1] = reordered_horizons[1], reordered_horizons[0]
    boolean_metric_horizons = copy.deepcopy(
        cast(list[dict[str, object]], valid_forecast_payload["horizons"])
    )
    boolean_metric_horizons[0]["model_r2"] = True
    invalid_split_horizons = copy.deepcopy(
        cast(list[dict[str, object]], valid_forecast_payload["horizons"])
    )
    cast(list[dict[str, object]], invalid_split_horizons[0]["per_split"])[0][
        "skill_persistence"
    ] = math.nan
    forecast_payload_mutations = {
        "top-level shape": (
            "forecast payload top-level shape changed",
            changed_forecast_payload(extra="not reviewed"),
        ),
        "baseline identity": (
            "forecast payload baseline identity or order changed",
            changed_forecast_payload(
                baselines=[
                    {
                        "name": "climatology" if index == 0 else "persistence",
                        "label": label,
                        "what": what,
                        "why": why,
                    }
                    for index, (_name, label, what, why) in enumerate(
                        valid_forecast_expected.baselines
                    )
                ]
            ),
        ),
        "reading count": (
            "forecast payload reading inventory changed",
            changed_forecast_payload(
                reading=[
                    {"claim": claim, "detail": detail}
                    for claim, detail in valid_forecast_expected.readings[:-1]
                ]
            ),
        ),
        "horizon order": (
            "forecast payload horizon identity or order changed",
            changed_forecast_payload(horizons=reordered_horizons),
        ),
        "boolean metric": (
            "forecast payload horizon 1 metric is invalid",
            changed_forecast_payload(horizons=boolean_metric_horizons),
        ),
        "non-finite split metric": (
            "forecast payload horizon 1 split metric is invalid",
            changed_forecast_payload(horizons=invalid_split_horizons),
        ),
    }
    for name, (expected_message, payload) in forecast_payload_mutations.items():
        try:
            _forecast_expected_evidence_from_payload(payload)
        except ValueError as exc:
            if expected_message not in str(exc):
                raise RuntimeError(
                    f"forecast payload preflight rejected {name} for {exc!s}, "
                    f"expected {expected_message}"
                ) from exc
        else:
            raise RuntimeError(f"forecast payload preflight accepted {name}")

    valid_health_brief = """
<header><p class="lede">健康負擔的三項選擇。</p></header>
<ol aria-label="本章三項假設" data-health-assumption-ledger>
<li data-health-assumption="counterfactual"><strong>比較基準</strong><p>圖 7.1 與圖 7.2 量化四種反事實濃度造成的差異。</p></li>
<li data-health-assumption="response"><strong>暴露反應函數</strong><p>本章只採用一條具可追溯來源的函數；適用範圍與外推界線在後文公開。</p></li>
<li data-health-assumption="population"><strong>暴露人口</strong><p>本專案沒有人口與個人暴露資料，因此不報死亡人數，也不把測站中位數稱為誰的暴露。</p></li>
</ol>
<section data-primary-evidence><p class="evidence-title">比較基準如何改變可歸因比例？</p><div data-primary-plot>Chart</div><figcaption>Caption</figcaption></section>
<div data-health-reading-band>
<section data-health-reading="robust"><h2>下降幅度對比較基準穩健</h2><p>2024 年是 12–20%，2025 年是 5–10%。無論選哪個基準，都下降了大約一半到三分之二。這一點跟第五章的政策效應不一樣—那裡的訊號被方法的噪音蓋過去，這裡沒有。</p></section>
<section data-health-reading="sensitive"><h2>當前水準對比較基準敏感</h2><p>2025 年的答案是 5% 還是 10%，差了將近一倍，而唯一的差別是把 5.9 還是 0 μg/m³ 當作比較基準—這是上圖 4 條假設線的兩個極端，落差來自方法選擇，不是來自資料。</p></section>
</div>
<section><p class="evidence-title">比較基準造成的落差佔估計值多少？</p></section>
<div data-health-inference-boundaries>
<section data-health-inference="deaths"><h2>不報死亡人數</h2><p>沒有死亡人數。</p></section>
<section data-health-inference="exposure"><h2>不宣稱這是誰的暴露</h2><p>測站平均不是人口加權暴露。</p></section>
</div>
"""
    valid_health_expected = HealthExpectedEvidence(
        series_count=4,
        function_count=1,
        years_count=2,
        spread_count=2,
        deaths="沒有死亡人數。",
        exposure="測站平均不是人口加權暴露。",
        reading_bodies=(
            "2024 年是 12–20%，2025 年是 5–10%。無論選哪個基準，都下降了大約一半到三分之二。這一點跟第五章的政策效應不一樣—那裡的訊號被方法的噪音蓋過去，這裡沒有。",
            "2025 年的答案是 5% 還是 10%，差了將近一倍，而唯一的差別是把 5.9 還是 0 μg/m³ 當作比較基準—這是上圖 4 條假設線的兩個極端，落差來自方法選擇，不是來自資料。",
        ),
    )
    valid_health_failures = health_assumption_ledger_failures_for_text(
        valid_health_brief, valid_health_expected
    )
    if valid_health_failures:
        raise RuntimeError(
            f"health assumption-ledger preflight rejected the valid control: "
            f"{valid_health_failures}"
        )

    def wrap_health_region(html: str, start: str, end: str, *, opened: bool) -> str:
        start_index = html.index(start)
        end_index = html.index(end, start_index) + len(end)
        return (
            html[:start_index]
            + f"<details{' open' if opened else ''}>"
            + html[start_index:end_index]
            + "</details>"
            + html[end_index:]
        )

    def move_health_region_after(html: str, start: str, end: str, destination: str) -> str:
        start_index = html.index(start)
        end_index = html.index(end, start_index) + len(end)
        region = html[start_index:end_index]
        without = html[:start_index] + html[end_index:]
        return without.replace(destination, destination + region, 1)

    health_mutations = {
        "missing ledger row": (
            "health assumption row inventory changed",
            valid_health_brief.replace(
                '<li data-health-assumption="response"><strong>暴露反應函數</strong><p>本章只採用一條具可追溯來源的函數；適用範圍與外推界線在後文公開。</p></li>\n',
                "",
                1,
            ),
        ),
        "extra ledger row": (
            "health assumption row inventory changed",
            valid_health_brief.replace(
                "</ol>", '<li data-health-assumption="extra">Extra</li></ol>', 1
            ),
        ),
        "duplicate ledger key": (
            "health assumption row order changed",
            valid_health_brief.replace(
                'data-health-assumption="response"', 'data-health-assumption="counterfactual"', 1
            ),
        ),
        "reordered ledger rows": (
            "health assumption row order changed",
            valid_health_brief.replace(
                '<li data-health-assumption="counterfactual"',
                '<li data-health-assumption="temporary"',
                1,
            )
            .replace(
                '<li data-health-assumption="response"',
                '<li data-health-assumption="counterfactual"',
                1,
            )
            .replace(
                '<li data-health-assumption="temporary"',
                '<li data-health-assumption="response"',
                1,
            ),
        ),
        "wrong ledger text": (
            "health assumption row text changed",
            valid_health_brief.replace("圖 7.1 與圖 7.2 量化", "圖 7.1 與圖 7.2 猜測", 1),
        ),
        "ledger label loses its structure": (
            "health assumption row structure changed",
            valid_health_brief.replace(
                "<strong>比較基準</strong><p>圖 7.1 與圖 7.2 量化四種反事實濃度造成的差異。</p>",
                "<p>比較基準圖 7.1 與圖 7.2 量化四種反事實濃度造成的差異。</p>",
                1,
            ),
        ),
        "hidden ledger": (
            "health assumption ledger inventory changed",
            valid_health_brief.replace(
                "data-health-assumption-ledger>", "data-health-assumption-ledger hidden>", 1
            ),
        ),
        "aria-hidden reading band": (
            "health reading band inventory changed",
            valid_health_brief.replace(
                "data-health-reading-band>", 'data-health-reading-band aria-hidden="true">', 1
            ),
        ),
        "ledger after Figure 7.1": (
            "health opening order changed",
            move_health_region_after(
                valid_health_brief,
                '<ol aria-label="本章三項假設" data-health-assumption-ledger>',
                "</ol>",
                '<section data-primary-evidence><p class="evidence-title">比較基準如何改變可歸因比例？</p><div data-primary-plot>Chart</div><figcaption>Caption</figcaption></section>',
            ),
        ),
        "open disclosure around ledger": (
            "health assumption ledger is user-collapsible",
            wrap_health_region(
                valid_health_brief,
                '<ol aria-label="本章三項假設" data-health-assumption-ledger>',
                "</ol>",
                opened=True,
            ),
        ),
        "closed disclosure around reading band": (
            "health reading band is user-collapsible",
            wrap_health_region(
                valid_health_brief,
                "<div data-health-reading-band>",
                "</div>",
                opened=False,
            ),
        ),
        "open disclosure around inference boundaries": (
            "health inference boundaries are user-collapsible",
            wrap_health_region(
                valid_health_brief,
                "<div data-health-inference-boundaries>",
                "</div>",
                opened=True,
            ),
        ),
        "missing reading row": (
            "health reading row inventory changed",
            valid_health_brief.replace(
                '<section data-health-reading="robust"><h2>下降幅度對比較基準穩健</h2><p>2024 年是 12–20%，2025 年是 5–10%。無論選哪個基準，都下降了大約一半到三分之二。這一點跟第五章的政策效應不一樣—那裡的訊號被方法的噪音蓋過去，這裡沒有。</p></section>\n',
                "",
                1,
            ),
        ),
        "missing reading body": (
            "health reading row body changed",
            valid_health_brief.replace(
                "<p>2024 年是 12–20%，2025 年是 5–10%。無論選哪個基準，都下降了大約一半到三分之二。這一點跟第五章的政策效應不一樣—那裡的訊號被方法的噪音蓋過去，這裡沒有。</p>",
                "",
                1,
            ),
        ),
        "reordered inference rows": (
            "health inference row order changed",
            valid_health_brief.replace(
                'data-health-inference="deaths"', 'data-health-inference="temporary"', 1
            )
            .replace('data-health-inference="exposure"', 'data-health-inference="deaths"', 1)
            .replace('data-health-inference="temporary"', 'data-health-inference="exposure"', 1),
        ),
        "stale Figure 7.2 title": (
            "health Figure 7.2 title changed",
            valid_health_brief.replace(
                "比較基準造成的落差佔估計值多少？",
                "不同暴露反應函數會把結果推動多少？",
                1,
            ),
        ),
    }
    health_preflight_misses: list[str] = []
    for name, (expected_failure, html) in health_mutations.items():
        if html == valid_health_brief:
            raise RuntimeError(f"health assumption-ledger preflight did not apply {name}")
        mutation_failures = health_assumption_ledger_failures_for_text(html, valid_health_expected)
        if not any(failure.startswith(expected_failure) for failure in mutation_failures):
            health_preflight_misses.append(
                f"{name} -> {expected_failure} (received {mutation_failures})"
            )
    if health_preflight_misses:
        raise RuntimeError(
            "health assumption-ledger preflight misses: " + "; ".join(health_preflight_misses)
        )

    valid_health_payload: dict[str, object] = {
        "panel": {"start_year": 2024, "stations": 2, "station_years": 4, "why": "why"},
        "formula": "formula",
        "functions": [
            {
                "name": "one",
                "rr_per_10": 1.08,
                "rr_per_10_low": 1.06,
                "rr_per_10_high": 1.09,
                "outcome": "outcome",
                "source": "source",
                "source_url": "https://example.test/source",
                "caveat": "caveat",
            }
        ],
        "series": [
            {
                "name": ("zero", "gbd_low", "who_guideline", "gbd_high")[index],
                "label": f"label-{index}",
                "value": (0.0, 2.4, 5.0, 5.9)[index],
                "why": "why",
                "years": [2024, 2025],
                # Distinct per counterfactual, as the real export is: a lower
                # comparison concentration attributes more. Identical values here
                # made the headline range resolve to all four series at once,
                # which is exactly the shape the derivation has to refuse.
                "paf": [(0.2, 0.17, 0.14, 0.12)[index], (0.1, 0.08, 0.06, 0.05)[index]],
            }
            for index in range(4)
        ],
        "years": [2024, 2025],
        "mean_median": [20.0, 10.0],
        "spread_share": [0.2, 0.4],
        "headline": {
            "first_year": 2024,
            "last_year": 2025,
            "first_share": 0.2,
            "last_share": 0.4,
            "first_range": [0.12, 0.2],
            "last_range": [0.05, 0.1],
        },
        "extrapolation": {"ceiling_ugm3": 30.0, "share_above": 0.2, "why": "why"},
        "not_reported": {
            "deaths": "沒有死亡人數。",
            "exposure": "測站平均不是人口加權暴露。",
        },
    }
    payload_control = _health_expected_evidence_from_payload(valid_health_payload)
    if payload_control != valid_health_expected:
        raise RuntimeError(
            f"health payload preflight returned {payload_control!r}, "
            f"expected {valid_health_expected!r}"
        )

    def changed_health_payload(**changes: object) -> dict[str, object]:
        payload = copy.deepcopy(valid_health_payload)
        payload.update(changes)
        if payload == valid_health_payload:
            raise RuntimeError("health payload preflight mutation did not change the control")
        return payload

    health_payload_mutations = {
        "top-level shape": (
            "health payload top-level shape changed",
            changed_health_payload(extra="not reviewed"),
        ),
        "function count": (
            "health payload response-function inventory changed",
            changed_health_payload(functions=[]),
        ),
        "series count": (
            "health payload counterfactual-series inventory changed",
            changed_health_payload(
                series=[
                    {
                        "name": ("zero", "gbd_low", "who_guideline")[index],
                        "label": f"label-{index}",
                        "value": (0.0, 2.4, 5.0)[index],
                        "why": "why",
                        "years": [2024, 2025],
                        "paf": [0.2, 0.1],
                    }
                    for index in range(3)
                ]
            ),
        ),
        "headline shape": (
            "health payload headline shape changed",
            changed_health_payload(headline={"first_year": 2024}),
        ),
        # These two replace a fixture that renamed `gbd_high` and expected a
        # rejection. The sentence used to name the TMREL bounds by hand and this
        # gate held it to that, beside percentages taken from a range spanning
        # every counterfactual — so it was enforcing the error. The range ends
        # are now resolved by value, which a rename cannot break and these two
        # can.
        "headline range off every counterfactual": (
            "health payload headline range does not resolve to one counterfactual",
            changed_health_payload(
                headline={
                    **cast(dict[str, object], valid_health_payload["headline"]),
                    "last_range": [0.05, 0.42],
                }
            ),
        ),
        "counterfactuals indistinguishable at the last year": (
            "health payload headline range does not resolve to one counterfactual",
            changed_health_payload(
                series=[
                    {**row, "paf": [0.2, 0.1]}
                    for row in cast(list[dict[str, object]], valid_health_payload["series"])
                ]
            ),
        ),
        "empty deaths boundary": (
            "health payload no-inference boundary changed",
            changed_health_payload(not_reported={"deaths": "", "exposure": "present"}),
        ),
        "years and spread disagree": (
            "health payload years/spread inventory changed",
            changed_health_payload(spread_share=[0.2]),
        ),
        "non-finite spread": (
            "health payload spread value is invalid",
            changed_health_payload(spread_share=[0.2, math.nan]),
        ),
    }
    for name, (expected_message, payload) in health_payload_mutations.items():
        try:
            _health_expected_evidence_from_payload(payload)
        except ValueError as exc:
            if expected_message not in str(exc):
                raise RuntimeError(
                    f"health payload preflight rejected {name} for {exc!s}, "
                    f"expected {expected_message}"
                ) from exc
        else:
            raise RuntimeError(f"health payload preflight accepted {name}")

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
        home_failures = [f"missing {shown(home_page)}"]
    else:
        home_failures = home_failures_for(home_page)
    for failure in home_failures:
        print(f"home: {failure}")

    not_found_page = dist / "404.html"
    if not not_found_page.exists():
        not_found_failures = [f"missing {shown(not_found_page)}"]
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
            failures = [f"missing {shown(page)}"]
        else:
            html = page.read_text(encoding="utf-8")
            failures = failures_for_text(html, EXPECTED_THESIS_FRAGMENTS.get(slug, ()))
            failures.extend(
                analytical_figure_failures_for(page, EXPECTED_ANALYTICAL_FIGURES.get(slug))
            )
            if slug == "trend":
                failures.extend(trend_reading_map_failures_for_text(html))
            elif slug == "space":
                failures.extend(space_field_note_failures_for_text(html))
            elif slug == "sources":
                failures.extend(sources_atlas_failures_for_text(html))
            elif slug == "detection":
                try:
                    expected_events = load_detection_expected_events()
                except ValueError as exc:
                    failures.append(f"detection payload is invalid: {exc}")
                else:
                    failures.extend(
                        detection_limitation_brief_failures_for_text(html, expected_events)
                    )
            elif slug == "forecast":
                try:
                    expected_forecast = load_forecast_expected_evidence()
                except ValueError as exc:
                    failures.append(f"forecast payload is invalid: {exc}")
                else:
                    failures.extend(
                        forecast_horizon_decision_failures_for_text(html, expected_forecast)
                    )
            elif slug == "health":
                try:
                    expected_health = load_health_expected_evidence()
                except ValueError as exc:
                    failures.append(f"health payload is invalid: {exc}")
                else:
                    failures.extend(
                        health_assumption_ledger_failures_for_text(html, expected_health)
                    )
            elif slug == "methods":
                failures.extend(methods_case_index_failures_for_text(html))
            elif slug == "data":
                try:
                    data_contract = load_data_provenance_contract()
                except ValueError as exc:
                    failures.append(f"data provenance sources are invalid: {exc}")
                else:
                    failures.extend(
                        data_provenance_register_failures_for_text(
                            html, data_contract.descriptions, data_contract.downloads
                        )
                    )
            elif slug == "explore":
                failures.extend(explorer_guided_workspace_failures_for_text(html))
            elif visible_reading_map_count(html):
                failures.append("chapter unexpectedly contains a visible reading map")
            if slug == "stations":
                failures.extend(station_dossier_failures_for_text(html))

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
