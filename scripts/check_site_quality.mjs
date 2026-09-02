/**
 * Re-derive the executable site design contract, and fail if it stops holding.
 *
 * The measured accessibility contract originally stated four properties:
 *
 *   * every text node clears APCA Lc 60 in both themes — and records that the
 *     dark theme once had 26 nodes below it, the worst at 47.3;
 *   * no page scrolls horizontally at 375px, in either theme;
 *   * the smallest rendered type was 18.7px at 375 and 20px at 1440;
 *   * the two figure controls are at least 44px tall.
 *
 * Those numbers came from nineteen throwaway browser scripts, and `.gitignore`
 * records — in prose — that they were deleted on purpose. So the one regression
 * the section itself documents, 26 dark-mode nodes falling under Lc 60, became
 * a class of defect this repository could no longer detect. A claim with no
 * verifier is a claim that will drift, and this project's whole argument is
 * that its numbers are re-derivable.
 *
 * The 2026-08-04 current-state measurement supersedes those old size and
 * shell measurements. The final built site records 20.1375px root/body,
 * 18.3251px smallest visible text and 19.1306px smallest in-figure annotation
 * at 375px; the corresponding 1440px values are 22px, 20.02px and 20.9px.
 * At the adaptive-shell boundary, 1599px has only the handle/drawer while
 * 1600px has only a 264px persistent rail. At 1280x720, all seven chart-route
 * primary plots begin before 396px and retain at least 180px of visible plot;
 * the homepage map and legend end at 710.9776px, leaving 9.0224px below them.
 *
 * This serves `web/dist` itself and drives headless Chrome over CDP. No
 * dependency beyond Node and a Chrome binary: `sharp`, `puppeteer` and friends
 * are exactly the weight that made the previous attempts throwaway.
 *
 *     node scripts/check_site_quality.mjs [--dist web/dist] [--port 4399]
 *
 * Chrome is found at $CHROME_PATH, then the usual Windows/macOS/Linux
 * locations. If none exists the script says so and exits 0 — a machine without
 * a browser cannot answer, and refusing to answer is not the same as failing.
 */

import { execFileSync, spawn } from "node:child_process";
import { createReadStream, existsSync, readFileSync, statSync } from "node:fs";
import { createServer } from "node:http";
import { extname, join, normalize } from "node:path";

// ── executable site design contract ─────────────────────────────────────────

const ROUTES = [
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
];
const FORBIDDEN_CIGARETTE_ANALOGY = /\u652f\u83f8|\u9999\u83f8|cigarette/iu;
const PUBLIC_OPERATIONAL_METADATA_RULES = Object.freeze([
  ["data export timestamp", /\u8cc7\u6599\u532f\u51fa\u65bc/u],
  ["uncommitted worktree state", /\u672a\u63d0\u4ea4(?:\u7684)?\u8b8a\u66f4|dirty worktree/iu],
  ["Git metadata field", /\bgit[_ -]?(?:sha|dirty)\b/iu],
  ["bare revision hash", /\b(?=[0-9a-f]{7,40}\b)(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b/iu],
  ["local development address", /\b(?:localhost|127\.0\.0\.1)(?::\d{1,5})?\b/iu],
  ["local filesystem path", /(?:file:\/\/|\b[A-Z]:\\(?:Users|AI-Portfolio)\\)/iu],
]);

function publicOperationalMetadataProblems(text) {
  const copy = String(text ?? "");
  return PUBLIC_OPERATIONAL_METADATA_RULES
    .filter(([, pattern]) => pattern.test(copy))
    .map(([label]) => `public copy exposes ${label}`);
}
const STATION_TYPE_LABELS = Object.freeze({
  general: "一般站",
  traffic: "交通站",
  industrial: "工業站",
  background: "背景站",
  national_park: "國家公園站",
  reference: "參考站",
});
const HOMEPAGE_STATION_CARDS = JSON.parse(
  readFileSync(
    join(process.cwd(), "web", "public", "data", "story", "station-cards.json"),
    "utf8",
  ),
).cards;
const HOMEPAGE_LATEST_YEAR = Math.max(...HOMEPAGE_STATION_CARDS.map((card) => card.year));
const HOMEPAGE_LATEST_CARDS = HOMEPAGE_STATION_CARDS
  .filter((card) => card.year >= HOMEPAGE_LATEST_YEAR - 1)
  .sort((first, second) => second.annual_mean - first.annual_mean);
const HOMEPAGE_EXTREMA = Object.freeze({
  dirtiest: HOMEPAGE_LATEST_CARDS[0].station_name,
  dirtiestType: STATION_TYPE_LABELS[HOMEPAGE_LATEST_CARDS[0].station_type]
    ?? HOMEPAGE_LATEST_CARDS[0].station_type,
  cleanest: HOMEPAGE_LATEST_CARDS.at(-1).station_name,
  cleanestType: STATION_TYPE_LABELS[HOMEPAGE_LATEST_CARDS.at(-1).station_type]
    ?? HOMEPAGE_LATEST_CARDS.at(-1).station_type,
  ratio: (HOMEPAGE_LATEST_CARDS[0].annual_mean / HOMEPAGE_LATEST_CARDS.at(-1).annual_mean)
    .toFixed(1),
  // Whether the two ends of the range are the same category. The page says why
  // its ratio is not a spatial difference, and the reason differs between the
  // two cases, so the contract below has to follow the data rather than assume
  // the categories will keep differing.
  sameType: HOMEPAGE_LATEST_CARDS[0].station_type === HOMEPAGE_LATEST_CARDS.at(-1).station_type,
});
const COMPACT_IDENTITY_ACCESSIBLE_NAMES = new Map([
  ["/", "台灣空氣品質再分析"],
  ["/trend/", "第一章　長期趨勢與氣象校正"],
  ["/stations/", "第二章　測站個別統計"],
  ["/space/", "第三章　空間結構與官方分區"],
  ["/sources/", "第四章　污染來向與風速條件"],
  ["/detection/", "第五章　事件效應的偵測極限"],
  ["/forecast/", "第六章　預測技巧與有效期距"],
  ["/health/", "第七章　健康負擔與它的假設"],
  ["/methods/", "第八章　方法選擇的量化代價"],
  ["/explore/", "第九章　資料查詢"],
  ["/data/", "第十章　資料下載與方法"],
]);
const CHAPTER_ROUTES = ROUTES.filter((route) => route !== "/");
const CHAPTER_OPENING_VIEWPORTS = [
  { width: 375, height: 812 },
  { width: 768, height: 1024 },
  { width: 1280, height: 720 },
  { width: 1440, height: 900 },
  { width: 1600, height: 900 },
  { width: 1920, height: 1080 },
];
const READOUT_ROUTES = new Set(["/trend/", "/forecast/", "/health/", "/methods/"]);
const TEXT_ZOOM_ROUTES = [
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
];

/**
 * 2026-08-03 — measured from the built route DOM, before these inventories
 * became assertions. Native figures/captions originally totalled 14; the seven
 * 2026-08-31 semantic concept figures raise that inventory to 21 without
 * turning the Explore and Data chapters into chart routes. Secondary disclosures
 * total 8 (Detection 2, Methods 6); Explorer contributes the one SQL
 * disclosure. Keeping every zero is deliberate: a route cannot silently gain
 * or lose an object while the site-wide sum happens to stay constant.
 */
const STATIC_NATIVE_FIGURES = new Map([
  ["/", 0],
  ["/trend/", 4],
  ["/stations/", 0],
  ["/space/", 3],
  ["/sources/", 2],
  ["/detection/", 2],
  ["/forecast/", 3],
  ["/health/", 3],
  ["/methods/", 2],
  ["/explore/", 1],
  ["/data/", 1],
]);
const CHART_ROUTES = new Set([
  "/trend/", "/space/", "/sources/", "/detection/", "/forecast/", "/health/", "/methods/",
]);
const STATIC_SECONDARY_DISCLOSURES = new Map([
  // 2026-09-02 — one: the map's source and boundary notes are a disclosure
  // now, folded under a summary that keeps the finding (how many stations are
  // not drawn, and 萬里's historical placement) visible.
  ["/", 1],
  ["/trend/", 0],
  ["/stations/", 0],
  ["/space/", 0],
  ["/sources/", 0],
  ["/detection/", 2],
  ["/forecast/", 0],
  ["/health/", 0],
  ["/methods/", 4],
  ["/explore/", 0],
  ["/data/", 1],
]);
// Zero everywhere: chapter 9 no longer shows its query. A disclosure that
// reappeared would fail this inventory, which is what now holds the decision.
const STATIC_SQL_DISCLOSURES = new Map(ROUTES.map((route) => [route, 0]));
const STATIC_CONCEPT_DIAGRAMS = new Map([
  ["/", 0],
  ["/trend/", 1],
  ["/stations/", 0],
  ["/space/", 1],
  ["/sources/", 1],
  ["/detection/", 1],
  ["/forecast/", 0],
  ["/health/", 1],
  ["/methods/", 0],
  ["/explore/", 1],
  ["/data/", 1],
]);
const EXPECTED_NATIVE_FIGURES = 21;
// 2026-08-17: 9 -> 7. /methods/ lost two <details> — the transcribed K-S table
// and the published-vs-reproduced comparison table.
const EXPECTED_SECONDARY_DISCLOSURES = 8;
const EXPECTED_SQL_DISCLOSURES = 0;
const STATIC_TABLE_WRAPS = new Map([
  ["/", 0],
  ["/trend/", 0],
  ["/stations/", 0],
  ["/space/", 2],
  ["/sources/", 0],
  ["/detection/", 4],
  ["/forecast/", 3],
  ["/health/", 0],
  ["/methods/", 4],
  ["/explore/", 0],
  ["/data/", 1],
]);
/**
 * 2026-08-03 — the normal 11-route × 3-width × 2-theme matrix measured 90
 * visible wrappers (15 route-DOM wrappers repeated six times) and 22 cases
 * where a table was genuinely wider than its local frame. These exact totals
 * make an empty probe a failure rather than a vacuous success.
 *
 * 2026-08-17 — re-measured at 78 and 18. /methods/ lost two wrappers: a
 * thirteen-row table of externally transcribed K-S statistics, and the
 * published-vs-reproduced comparison table. 13 wrappers × 6 route-viewports =
 * 78, and each removed table had been a genuine scroller at two of the six.
 *
 * 2026-08-23 — re-measured at 84 and 20, and /forecast/ from 2 wrappers to 3.
 * The prediction-interval table is five columns of Chinese headers: one wrapper
 * over six route-viewports is the +6, and it is a genuine scroller at the two
 * 375px ones, which is the +2. Unwrapped it took the page 243px sideways at
 * 375px, and this gate is what said so.
 *
 * These numbers are re-measured when content legitimately changes, never
 * loosened to make a red run green. A wrapper vanishing for any other reason
 * still fails here, which is the whole point of pinning them.
 */
const EXPECTED_TABLE_WRAPS = 84;
const EXPECTED_TABLE_SCROLLERS = 20;

/** APCA Lc 60 is the floor below which text stops carrying meaning reliably. */
const MIN_LC = 60;
/**
 * Two floors, because the project's rule is about a RELATIONSHIP, not a size.
 *
 * The design principle is 「一張圖的註記不該是整份文件裡最小的字」 — a chart's
 * annotation must not be the smallest type in the document — and it records the
 * annotations having once been 17px against a body of 21. Measured now at 375:
 * `.plot-x` and `.plot-y` are **24px**, comfortably above the 20.73px body, so
 * that fix held.
 *
 * The smallest type on the site is `figcaption` at **17.41px** (`--text-xs`,
 * 0.84rem). That is a different thing from an annotation: a caption sits below
 * the figure and reads as apparatus, and one step down is what apparatus is
 * for.
 *
 * So the annotation is held to a RELATION rather than to a number — it must not
 * be the smallest type on its own page, and it must not fall below the body it
 * sits among. A fixed pixel floor here would be a number I picked; my first
 * attempt was 22 and the real minimum across all eleven routes is 20 (I had
 * measured 24 on one route and generalised from it). The relation is what the
 * principle actually says, and it cannot be wrong by a couple of pixels.
 *
 * A retired design note said the smallest type was 18.7px at 375. It is not,
 * and this is the check that would have said so — 0.84rem against a 20.725px root is 17.41,
 * and no width makes it 18.7. The line is corrected there.
 */
/**
 * 2026-08-03 — supersedes the fixed-pixel measurements above: the current
 * chart tokens are rem-based, and their live contract is that every in-figure
 * annotation retains at least 95% of the body size at the same viewport.
 */
const MIN_FONT_PX = 18;
/** WCAG 2.5.5's comfortable target. The figure controls are the ones at risk. */
const MIN_TARGET_PX = 44;
// 2026-08-03 — Linux Chrome quantised a declared 44px figure control one
// layout unit below the interaction floor while Windows Chrome kept it at 44.
// Require one 1/64px layout unit of reserve so an exact CSS boundary cannot
// pass locally and fail after deployment on another rasterisation path.
const TARGET_LAYOUT_RESERVE_PX = 1 / 64;
const CSS_PX_SERIALIZATION_EPSILON = 0.0001;
const READOUT_OVERLAP_TOLERANCE_PX = 1;
const CHART_TEXT_CONTRACTS = [
  { role: "tick", selector: ".plot-x span, .plot-y span", ratio: 1.10 },
  {
    role: "label",
    selector: ".plot-keys span, .plot-note, .chart-key li, .readout-row",
    ratio: 1.10,
  },
  {
    role: "micro",
    selector:
      ".axis span, .readout-when, .radial-scale, .radial-unit, .bearing, " +
      ".ramp-title, .ramp-ticks, .ramp-foot, .scale-title, .scale-ticks, " +
      ".county-label, .corr-label, .ctrl-label, .corr-value, .ctrl-value, " +
      ".ctrl-value small",
    ratio: 1.05,
  },
];
const CHART_STROKE_EPSILON = 0.01;
const IDLE_READOUT_BLANK_LIMIT_PX = 96;

function trendPickerInteractionProblems(state) {
  if (!state) return ["air-zone picker state is missing"];
  const problems = [];
  const hidden = state.filtered?.displays?.slice(0, 2) ?? [];
  const retained = state.filtered?.displays?.slice(2) ?? [];
  if (state.filtered?.checked !== 6) problems.push("filtered checkbox count changed");
  if (hidden.length !== 2 || hidden.some((display) => display !== "none")) {
    problems.push("unchecked paths are not both hidden");
  }
  if (retained.length !== 6 || retained.some((display) => display === "none")) {
    problems.push("checked paths are not all rendered");
  }
  if (state.filtered?.rows !== 6) problems.push("filtered readout row count changed");
  if (!state.filtered?.resetVisible) problems.push("filtered reset control is hidden");
  if (!state.filtered?.announcement?.includes("顯示 6 條")) {
    problems.push("filtered announcement changed");
  }
  if (state.empty?.checked !== 0) problems.push("empty checkbox count changed");
  if (
    state.empty?.displays?.length !== 8 ||
    state.empty.displays.some((display) => display !== "none")
  ) {
    problems.push("empty selection leaves a series path rendered");
  }
  if (state.empty?.rows !== 0) problems.push("empty readout still lists a series");
  if (state.empty?.announcement !== "目前未顯示任何空品區") {
    problems.push("empty announcement changed");
  }
  if (state.restored?.checked !== 8) problems.push("reset checkbox count changed");
  if (state.restored?.rows !== 8) problems.push("reset readout row count changed");
  if (state.restored?.resetVisible) problems.push("enhanced idle reset control remains visible");
  if (!state.restored?.focusReturned) problems.push("enhanced reset focus did not return");
  return problems;
}

function trendNoScriptPickerProblems(state) {
  if (!state) return ["no-JavaScript air-zone picker state is missing"];
  const problems = [];
  const displays = state.filtered?.displays ?? [];
  if (!state.resetVisibleBeforeFiltering) problems.push("native reset is hidden before filtering");
  if (state.filtered?.checked !== 6) problems.push("native picker did not uncheck two series");
  if (
    displays.length !== 8 ||
    displays.slice(0, 2).some((display) => display !== "none") ||
    displays.slice(2).some((display) => display === "none")
  ) {
    problems.push("native picker did not hide only the two unchecked paths");
  }
  if (state.restored?.checked !== 8) problems.push("native reset did not restore all series");
  if (!state.restored?.resetVisible) problems.push("native reset disappeared after reset");
  if (!state.restored?.focusStayedOnReset) problems.push("native reset lost keyboard focus");
  return problems;
}

function trendFilteredExportProblems(state) {
  if (!state) return ["filtered PNG state is missing"];
  const problems = [];
  const checked = state.checkedAttributes ?? [];
  const displays = state.pathDisplays ?? [];
  if (
    checked.length !== 8 || checked.slice(0, 2).some(Boolean) ||
    checked.slice(2).some((value) => !value)
  ) {
    problems.push("serialized checkbox state does not preserve the six-series filter");
  }
  if (
    displays.length !== 8 ||
    displays.slice(0, 2).some((display) => display !== "none") ||
    displays.slice(2).some((display) => display === "none")
  ) {
    problems.push("serialized path state does not preserve the six-series filter");
  }
  return problems;
}

function trendZoomFitProblems(state) {
  if (!state) return ["enlarged trend figure state is missing"];
  const problems = [];
  if (
    !Number.isFinite(state.stageClientHeight) ||
    !Number.isFinite(state.stageScrollHeight) ||
    state.stageClientHeight <= 0 ||
    state.stageScrollHeight > state.stageClientHeight + 1
  ) {
    problems.push("enlarged trend figure overflows the dialog stage");
  }
  if (!Number.isFinite(state.plotHeight) || state.plotHeight + CSS_PX_SERIALIZATION_EPSILON < 320) {
    problems.push("enlarged trend figure plot fell below its readability floor");
  }
  return problems;
}
const AXIS_LABEL_CLEARANCE_PX = 4;
const EXPECTED_DESKTOP_COUNTY_LABELS = 15;
const MIN_LABELLED_MAP_WIDTH_PX = 329;

const args = process.argv.slice(2);
const opt = (name, fallback) => {
  const i = args.indexOf(`--${name}`);
  return i >= 0 && args[i + 1] ? args[i + 1] : fallback;
};
const DIST = opt("dist", join(process.cwd(), "web", "dist"));
const PORT = Number(opt("port", "4399"));
const SELF_TEST = args.includes("--self-test");
const DETECTION_BROWSER_SELF_TEST = args.includes("--detection-browser-self-test");
const HEALTH_BROWSER_SELF_TEST = args.includes("--health-browser-self-test");
const FORECAST_BROWSER_SELF_TEST = args.includes("--forecast-browser-self-test");
const METHODS_BROWSER_SELF_TEST = args.includes("--methods-browser-self-test");
const DATA_BROWSER_SELF_TEST = args.includes("--data-browser-self-test");
const EXPLORE_BROWSER_SELF_TEST = args.includes("--explorer-browser-self-test");
const requestedCdpTimeout = Number(opt("cdp-timeout-ms", "15000"));
const CDP_TIMEOUT_MS =
  Number.isFinite(requestedCdpTimeout) && requestedCdpTimeout > 0 ? requestedCdpTimeout : 15000;
const CHROME_TEST_FLAGS = [
  "--disable-back-forward-cache",
  "--disable-background-timer-throttling",
  "--disable-backgrounding-occluded-windows",
  "--disable-renderer-backgrounding",
];

async function withDeadline(promise, timeoutMs, label) {
  let timer;
  try {
    return await Promise.race([
      promise,
      new Promise((_, reject) => {
        timer = setTimeout(
          () => reject(new Error(`${label} did not answer within ${timeoutMs}ms`)),
          timeoutMs,
        );
      }),
    ]);
  } finally {
    clearTimeout(timer);
  }
}

async function waitForWebSocketOpen(socket, timeoutMs, label) {
  let cleanup = () => {};
  const opened = new Promise((resolve, reject) => {
    const onOpen = () => resolve();
    const onError = () => reject(new Error(`${label} failed`));
    const onClose = () => reject(new Error(`${label} closed before opening`));
    cleanup = () => {
      socket.removeEventListener("open", onOpen);
      socket.removeEventListener("error", onError);
      socket.removeEventListener("close", onClose);
    };
    socket.addEventListener("open", onOpen, { once: true });
    socket.addEventListener("error", onError, { once: true });
    socket.addEventListener("close", onClose, { once: true });
  });
  return withDeadline(opened, timeoutMs, label).finally(cleanup);
}

async function navigateWithoutPageScripts(sendCommand, waitForEvent, url, inspectReady) {
  await sendCommand("Emulation.setScriptExecutionDisabled", { value: true });
  try {
    const loaded = waitForEvent("Page.loadEventFired", `${url} load event`);
    await sendCommand("Page.navigate", { url });
    await loaded;
  } finally {
    await sendCommand("Emulation.setScriptExecutionDisabled", { value: false });
  }
  return inspectReady();
}

async function replaceBrowser(current, openReplacement) {
  if (current) await current.close();
  return openReplacement();
}

async function closeServer(server) {
  const closed = new Promise((resolve, reject) => {
    server.close((error) => (error ? reject(error) : resolve()));
  });
  return withDeadline(closed, CDP_TIMEOUT_MS, "static server close");
}

async function withRuntimeCleanup(resources, work) {
  try {
    return await work();
  } finally {
    try {
      if (resources.browser) await resources.browser.close();
    } finally {
      await closeServer(resources.server);
    }
  }
}

function firstViewportProblems({
  map,
  mapSvg,
  stationMarks,
  legend,
  scaleBar,
  scaleTicks,
  scaleSegments,
  tickMarks,
  viewport,
  requireVerticalViewport = true,
}) {
  const problems = [];
  const finiteRect = (rect) =>
    rect && ["top", "right", "bottom", "left", "width", "height"]
      .every((edge) => Number.isFinite(rect[edge]));
  const checkPart = (name, part, plural = false) => {
    const be = plural ? "are" : "is";
    const have = plural ? "have" : "has";
    if (!part) {
      problems.push(`homepage ${name} ${be} missing`);
      return;
    }
    if (!finiteRect(part)) {
      problems.push(`homepage ${name} ${have} non-finite geometry`);
      return;
    }
    if (part.width <= 0 || part.height <= 0) {
      problems.push(`homepage ${name} ${have} no rendered area`);
      return;
    }
    if (part.visible === false) {
      problems.push(`homepage ${name} ${be} not visibly rendered`);
    }
    if (part.clipped) {
      problems.push(`homepage ${name} ${be} clipped by an ancestor`);
    }
    if (part.contained === false) {
      const identifier = part.identifier ? ` (${JSON.stringify(part.identifier)})` : "";
      const overflow = Number.isFinite(part.containerOverflow)
        ? ` by ${part.containerOverflow.toFixed(3)}px` : "";
      problems.push(`homepage ${name} leaves its container${identifier}${overflow}`);
    }
    if (part.fillsContainer === false) {
      problems.push(`homepage ${name} no longer fills its container`);
    }
    if (part.anchored === false) {
      problems.push(`homepage ${name} is displaced from its anchor`);
    }
    if (part.left < -1 || part.right > viewport.width + 1) {
      problems.push(`homepage ${name} leaves the horizontal viewport`);
    }
    if (requireVerticalViewport && (part.top < -1 || part.bottom > viewport.height + 1)) {
      problems.push(`homepage ${name} leaves the initial vertical viewport`);
    }
  };
  const checkParts = (plural, singular, parts) => {
    if (!Array.isArray(parts) || parts.length === 0) {
      problems.push(`homepage ${plural} are missing`);
      return;
    }
    for (const part of parts) checkPart(singular, part);
  };
  if (!Number.isFinite(viewport?.width) || !Number.isFinite(viewport?.height)) {
    return ["homepage viewport has non-finite geometry"];
  }
  checkPart("map", map);
  checkPart("map SVG", mapSvg);
  checkParts("station marks", "station mark", stationMarks);
  checkPart("map legend", legend);
  checkPart("scale bar", scaleBar);
  checkPart("scale ticks", scaleTicks, true);
  checkParts("scale segments", "scale segment", scaleSegments);
  checkParts("tick marks", "tick mark", tickMarks);
  return problems;
}

function countyLabelProblems({ map, labels, expectedVisible = null }) {
  if (
    !map || !Number.isFinite(map.width) || map.width <= 0 ||
    !Array.isArray(labels) || labels.length === 0
  ) {
    return ["homepage county-label geometry is missing"];
  }
  const problems = [];
  const visible = labels.filter((label) => label?.visible);
  if (expectedVisible !== null && visible.length !== expectedVisible) {
    problems.push(
      `homepage visible county-label inventory is ${visible.length}, expected ${expectedVisible}`,
    );
  }
  for (const label of visible) {
    if (
      !["top", "right", "bottom", "left", "width", "height"]
        .every((edge) => Number.isFinite(label?.[edge])) ||
      label.width <= 0 || label.height <= 0
    ) {
      problems.push("homepage county label has invalid geometry");
    } else if (label.contained === false) {
      problems.push(
        `homepage county label leaves the map (${JSON.stringify(label.identifier ?? "")})`,
      );
    }
  }

  let overlaps = 0;
  for (let index = 0; index < visible.length; index += 1) {
    const first = visible[index];
    for (let other = index + 1; other < visible.length; other += 1) {
      const second = visible[other];
      const overlapX = Math.min(first.right, second.right) - Math.max(first.left, second.left);
      const overlapY = Math.min(first.bottom, second.bottom) - Math.max(first.top, second.top);
      if (overlapX > 1 && overlapY > 1) overlaps += 1;
    }
  }
  if (overlaps) {
    problems.push(
      `homepage ${overlaps} county label pairs overlap on a ${map.width.toFixed(3)}px map`,
    );
  }
  return problems;
}

function chapterOpeningProblems(state) {
  const problems = [];
  const viewport = state?.viewport;
  const openingKinds = new Set(["evidence", "index"]);
  if (!openingKinds.has(state?.openingKind)) {
    problems.push("chapter opening kind is invalid");
  }
  if (
    !Number.isFinite(viewport?.width) || !Number.isFinite(viewport?.height) ||
    viewport.width <= 0 || viewport.height <= 0
  ) {
    return ["chapter viewport has invalid geometry"];
  }

  if (!Number.isFinite(state?.smallestVisibleText) || state.smallestVisibleText <= 0) {
    problems.push("chapter smallest visible text has invalid geometry");
  } else if (state.smallestVisibleText < MIN_FONT_PX) {
    problems.push(
      `chapter smallest visible text is ${state.smallestVisibleText}px (18px floor)`,
    );
  }

  const shellPart = (name, part, dimension) => {
    if (!part || typeof part.visible !== "boolean" || !Number.isFinite(part[dimension])) {
      problems.push(`chapter ${name} has invalid geometry`);
      return false;
    }
    if (part[dimension] < 0 || (part.visible && part[dimension] <= 0)) {
      problems.push(`chapter ${name} has invalid geometry`);
      return false;
    }
    return true;
  };
  const railValid = shellPart("rail", state?.rail, "width");
  const handleValid = shellPart("handle", state?.handle, "height");
  if (viewport.width < 1600) {
    if (railValid && state.rail.visible) {
      problems.push("chapter persistent rail is visible below 1600px");
    }
    if (handleValid && !state.handle.visible) {
      problems.push("chapter handle is hidden below 1600px");
    }
  } else {
    if (railValid && !state.rail.visible) {
      problems.push("chapter persistent rail is hidden at or above 1600px");
    }
    if (handleValid && state.handle.visible) {
      problems.push("chapter handle remains visible at or above 1600px");
    }
  }

  const geometryPart = (name, part) => {
    if (!part) {
      problems.push(`chapter ${name} is missing`);
      return false;
    }
    if (
      typeof part.visible !== "boolean" || !Number.isFinite(part.top) ||
      !Number.isFinite(part.bottom) || part.bottom <= part.top
    ) {
      problems.push(`chapter ${name} has invalid geometry`);
      return false;
    }
    if (!part.visible) {
      problems.push(`chapter ${name} is not visibly rendered`);
      return false;
    }
    return true;
  };
  if (geometryPart("primary evidence", state?.primary)) {
    if (
      state?.openingKind === "evidence" &&
      (state.primary.top >= viewport.height || state.primary.bottom <= 0)
    ) {
      problems.push("chapter primary evidence is outside the initial viewport");
    }
  }

  if (typeof state?.chartRoute !== "boolean") {
    problems.push("chapter chart-route state is missing");
  } else if (state.chartRoute) {
    if (geometryPart("primary plot", state.primaryPlot)) {
      if (!Number.isFinite(state.primaryPlot.dataAreaVisible) || state.primaryPlot.dataAreaVisible < 0) {
        problems.push("chapter primary plot has invalid visible-data geometry");
      } else if (
        state?.openingKind === "evidence" &&
        viewport.width === 1280 && viewport.height === 720
      ) {
        if (state.primaryPlot.top >= viewport.height * 55 / 100) {
          problems.push("chapter primary plot starts at or below 55vh");
        }
        if (state.primaryPlot.dataAreaVisible < 180) {
          problems.push("chapter less than 180px of plot data is visible");
        }
      }
    }
  }

  return problems;
}

function chapterEndingProblems(state, viewport) {
  const problems = [];
  const compactHeightCeiling = viewport?.width < 768 ? 320 : 220;
  const requiredLinks = state?.expectedPreviousLinks + state?.expectedNextLinks;

  if (state?.navCount !== 1) {
    problems.push(`chapter ending exposes ${state?.navCount ?? "unknown"} navigators`);
  }
  if (state?.panelCount !== 1) {
    problems.push(`chapter ending exposes ${state?.panelCount ?? "unknown"} grouped panels`);
  }
  if (state?.progressCount !== 1 || state?.progressText !== state?.expectedProgressText) {
    problems.push("chapter ending progress marker is missing or incorrect");
  }
  if (state?.position !== state?.expectedPosition) {
    problems.push(
      `chapter ending position is ${JSON.stringify(state?.position)}, ` +
        `expected ${JSON.stringify(state?.expectedPosition)}`,
    );
  }
  if (state?.indexLinks !== 1 || state?.indexLabel !== "全部章節") {
    problems.push("chapter ending does not expose one clearly labelled chapter-index link");
  }
  if (state?.inertEndpoints !== 0) {
    problems.push(`chapter ending still renders ${state?.inertEndpoints ?? "unknown"} inert endpoints`);
  }
  if (state?.linkCount !== state?.expectedLinkCount) {
    problems.push(
      `chapter ending exposes ${state?.linkCount ?? "unknown"} links, ` +
        `expected ${state?.expectedLinkCount ?? "unknown"}`,
    );
  }
  if (state?.previousLinks !== state?.expectedPreviousLinks) {
    problems.push("chapter ending previous-link inventory does not match its chapter position");
  }
  if (state?.nextLinks !== state?.expectedNextLinks) {
    problems.push("chapter ending next-link inventory does not match its chapter position");
  }
  if (state?.directionLabels !== requiredLinks) {
    problems.push("chapter ending neighbour links do not state their reading direction");
  }
  if (state?.hiddenArrows !== requiredLinks) {
    problems.push("chapter ending arrows are missing or exposed to assistive technology");
  }
  if (state?.outwardArrows !== requiredLinks) {
    problems.push("chapter ending arrows do not sit on their outward navigation edges");
  }
  if ((state?.linkHeights ?? []).some((height) => !Number.isFinite(height) || height < 44)) {
    problems.push("chapter ending has a link shorter than the 44px target floor");
  }
  if (state?.containedLinks !== state?.linkCount) {
    problems.push("chapter ending has a link outside its grouped panel");
  }
  if (state?.clippedTitles !== 0) {
    problems.push(`chapter ending clips ${state?.clippedTitles ?? "unknown"} chapter titles`);
  }
  if (state?.horizontalOverflow !== 0) {
    problems.push(`chapter ending adds ${state?.horizontalOverflow ?? "unknown"}px horizontal overflow`);
  }
  if (
    !Number.isFinite(state?.navHeight) ||
    !Number.isFinite(compactHeightCeiling) ||
    state.navHeight > compactHeightCeiling
  ) {
    problems.push(
      `chapter ending is ${state?.navHeight ?? "unknown"}px tall ` +
        `(ceiling ${compactHeightCeiling}px)`,
    );
  }
  return problems;
}

/*
 * The enlarged view's header holds two controls, and they must be one control.
 *
 * 下載 moves into that header while the dialog is open, so it and 關閉 sit on a
 * single line. They were sized by different rules — `.fig-tool` to the 45px
 * interaction floor with no block padding, `.fig-shut` by the base button's
 * `0.5rem 0.8rem` — and shipped as 83.41 x 45 beside 79 x 59.53. Nothing here
 * looked, because every check the toolbar had was about ITS OWN buttons.
 *
 * The predicate is about agreement rather than about either measurement: the
 * floor is already checked elsewhere, and a pair that both drifted to 60px
 * would still be wrong in the way the owner noticed. One CSS pixel of slack,
 * for the same sub-pixel serialisation the toolbar's own 45px reserve exists
 * for.
 */
function zoomHeadControlProblems(controls) {
  const problems = [];
  const { download, shut } = controls ?? {};
  if (!download || !shut) {
    return ["enlarged view header controls are missing"];
  }
  for (const [name, box] of [["download", download], ["close", shut]]) {
    if (!["width", "height", "top"].every((edge) => Number.isFinite(box[edge]))) {
      problems.push(`enlarged view ${name} control geometry is invalid`);
    }
  }
  if (problems.length) return problems;
  if (Math.abs(download.height - shut.height) > 1) {
    problems.push(
      `enlarged view header controls differ in height: ` +
        `${download.height.toFixed(2)} against ${shut.height.toFixed(2)}`,
    );
  }
  if (Math.abs(download.width - shut.width) > 1) {
    problems.push(
      `enlarged view header controls differ in width: ` +
        `${download.width.toFixed(2)} against ${shut.width.toFixed(2)}`,
    );
  }
  if (Math.abs(download.top - shut.top) > 1) {
    problems.push("enlarged view header controls do not share one baseline");
  }
  return problems;
}

function conceptDiagramProblems(state, expectedCount, viewport) {
  const problems = [];
  const diagrams = state?.diagrams;
  if (!Array.isArray(diagrams)) return ["concept diagram snapshot is missing"];
  if (diagrams.length !== expectedCount) {
    problems.push(
      `concept diagram inventory is ${diagrams.length}, expected ${expectedCount}`,
    );
  }
  if (!Number.isFinite(state?.documentOverflow) || state.documentOverflow > 0) {
    problems.push(`concept diagram page scrolls sideways by ${state?.documentOverflow ?? "unknown"}px`);
  }
  for (const [index, diagram] of diagrams.entries()) {
    const name = `concept diagram ${index + 1}`;
    if (diagram?.tagName !== "FIGURE") problems.push(`${name} is not a native figure`);
    if (!diagram?.visible || diagram.width <= 0 || diagram.height <= 0) {
      problems.push(`${name} is not visibly rendered`);
    }
    if (diagram?.captionCount !== 1) problems.push(`${name} lacks exactly one direct figcaption`);
    if (!diagram?.title) problems.push(`${name} has no visible title`);
    if (!diagram?.summary) problems.push(`${name} has no visible reading summary`);
    // `boundary` (a drawn trust edge) keeps the same DOM contract as the
    // sequence variants — the reading order stays an ordered list — and differs
    // only in geometry, so every check below applies to it unchanged. `fork`
    // was here too and is gone: its only caller repeated a figure's legend, and
    // an unrendered variant is CSS no page exercises.
    if (!['process', 'timeline', 'layers', 'boundary'].includes(diagram?.variant)) {
      problems.push(`${name} has an invalid layout variant`);
    }
    if (!Number.isFinite(diagram?.stepCount) || diagram.stepCount < 3 || diagram.stepCount > 5) {
      problems.push(`${name} has ${diagram?.stepCount ?? "unknown"} steps; expected 3–5`);
    }
    if (diagram?.orderedListCount !== 1) problems.push(`${name} lacks one direct ordered sequence`);
    if (diagram?.listRoleAttribute !== "list") {
      problems.push(`${name} lacks an explicit list role for markerless-list accessibility`);
    }
    if (diagram?.listAxRole !== "list") {
      problems.push(`${name} ordered sequence is missing from the accessibility tree`);
    }
    if (diagram?.directItemCount !== diagram?.stepCount) {
      problems.push(`${name} has untracked or non-direct ordered-list items`);
    }
    if (diagram?.nonListSteps !== 0) {
      problems.push(`${name} has ${diagram?.nonListSteps ?? "unknown"} steps that are not list items`);
    }
    if (
      !Array.isArray(diagram?.stepAxRoles) ||
      diagram.stepAxRoles.length !== diagram.stepCount ||
      diagram.stepAxRoles.some((role) => role !== "listitem")
    ) {
      problems.push(`${name} steps are not exposed as accessibility-tree list items`);
    }
    if (diagram?.incompleteSteps !== 0) {
      problems.push(`${name} has ${diagram?.incompleteSteps ?? "unknown"} incomplete steps`);
    }
    if (diagram?.hiddenSteps !== 0) {
      problems.push(`${name} has ${diagram?.hiddenSteps ?? "unknown"} hidden steps`);
    }
    if (diagram?.clippedSteps !== 0) {
      problems.push(`${name} has ${diagram?.clippedSteps ?? "unknown"} clipped steps`);
    }
    if (diagram?.selfOverflowX > 1 || diagram?.selfOverflowY > 1) {
      problems.push(`${name} clips its own diagram content`);
    }
    if (diagram?.stepOverflowCount !== 0) {
      problems.push(`${name} has ${diagram?.stepOverflowCount ?? "unknown"} internally clipped steps`);
    }
    if (diagram?.hiddenOptions !== 0 || diagram?.clippedOptions !== 0) {
      problems.push(
        `${name} has ${diagram?.hiddenOptions ?? "unknown"} hidden and ` +
        `${diagram?.clippedOptions ?? "unknown"} clipped branch options`,
      );
    }
    if (diagram?.connectorCount !== Math.max(0, diagram.stepCount - 1)) {
      problems.push(`${name} connector inventory does not match its sequence`);
    }
    if (diagram?.hiddenConnectors !== 0) {
      problems.push(`${name} has ${diagram?.hiddenConnectors ?? "unknown"} invisible connectors`);
    }
    if (diagram?.boxedConnectors !== 0) {
      problems.push(`${name} has ${diagram?.boxedConnectors ?? "unknown"} boxed connectors`);
    }
    // 2026-09-02 — the drawn strips. A plate draws on every step or on none,
    // so a row of strips with a gap in it is a step whose drawing failed to
    // render, not a design; `timeline` is the one variant whose geometry IS
    // the strip (two lanes), so it must draw. SVG text is forbidden outright:
    // the glossary, CJK-spacing and no-JavaScript text gates read the page's
    // HTML and skip <svg>, so a label inside one is a label no gate can see.
    if (diagram?.figureTextCount !== 0) {
      problems.push(`${name} carries SVG text inside a step`);
    }
    if (diagram?.figureCount > 0 && diagram.figureCount < diagram.stepCount) {
      problems.push(`${name} drawing row is incomplete`);
    }
    if (diagram?.variant === "timeline" && diagram?.figureCount !== diagram?.stepCount) {
      problems.push(`${name} timeline draws no lanes`);
    }
    if (diagram?.hiddenFigures !== 0) {
      problems.push(`${name} has ${diagram?.hiddenFigures ?? "unknown"} hidden drawing strips`);
    }
    if (
      !Number.isFinite(diagram?.minimumTitleWidthRatio) ||
      diagram.minimumTitleWidthRatio < 0.9
    ) {
      problems.push(`${name} step titles do not use the available card width`);
    }
    if (diagram?.outOfOrderSteps !== 0) {
      problems.push(`${name} has ${diagram?.outOfOrderSteps ?? "unknown"} visually reordered steps`);
    }
    if (viewport?.width <= 768 && diagram?.nonVerticalTransitions !== 0) {
      problems.push(`${name} does not reflow to one vertical sequence`);
    }
    if (viewport?.width <= 768 && diagram?.gridColumnCount !== 1) {
      problems.push(`${name} keeps multiple columns at the narrow-layout boundary`);
    }
    if (
      viewport?.width > 768 &&
      diagram?.gridColumnCount !== (diagram?.variant === "layers" ? 1 : diagram?.stepCount)
    ) {
      problems.push(`${name} has the wrong desktop/tablet column count`);
    }
    if (viewport?.width > 768 && diagram?.variant !== "layers") {
      const tops = Array.isArray(diagram?.stepTops) ? diagram.stepTops : [];
      if (tops.length !== diagram?.stepCount) {
        problems.push(`${name} step top-edge inventory is incomplete`);
      } else if (Math.max(...tops) - Math.min(...tops) > 1) {
        problems.push(`${name} steps do not share one row band`);
      }
    }
    if (diagram?.toolCount > 0) {
      if (diagram.toolCount !== 1) {
        problems.push(`${name} has ${diagram.toolCount} toolbars; expected one`);
      }
      if (
        !Array.isArray(diagram?.toolLabels) ||
        diagram.toolLabels.length !== 2 ||
        diagram.toolLabels[0] !== "放大" || diagram.toolLabels[1] !== "下載"
      ) {
        problems.push(`${name} toolbar labels changed`);
      }
      if (
        viewport?.width > 768 &&
        !diagram?.toolsShareCaptionRow
      ) {
        problems.push(`${name} wide toolbar does not share the caption row`);
      }
      // 2026-09-02 — the toolbar moved from the panel's top-left reading edge
      // to its top-right corner, where every evidence figure keeps its own;
      // the caption reserves that corner and the gap between them is the
      // 8px title-ink clearance the evidence figures are held to.
      if (
        viewport?.width > 768 &&
        (!Number.isFinite(diagram?.captionToolGap) || diagram.captionToolGap < 8)
      ) {
        problems.push(`${name} wide caption runs under the toolbar`);
      }
      if (
        viewport?.width > 768 &&
        Number.isFinite(diagram?.captionToolGap) && diagram.captionToolGap > 64
      ) {
        problems.push(`${name} wide caption leaves unused header space`);
      }
      if (viewport?.width <= 768 && !diagram?.toolsAfterSteps) {
        problems.push(`${name} narrow toolbar is not below its steps`);
      }
      if (!Number.isFinite(diagram?.toolRightInset) || diagram.toolRightInset > 1) {
        problems.push(`${name} toolbar is not aligned to the top-right corner`);
      }
      if (diagram?.toolsOverlapCaption) {
        problems.push(`${name} toolbar overlaps its caption`);
      }
      if (
        !Array.isArray(diagram?.toolButtonHeights) ||
        diagram.toolButtonHeights.length !== 2 ||
        diagram.toolButtonHeights.some((height) => !Number.isFinite(height) || height < 44)
      ) {
        problems.push(`${name} toolbar controls fall below the 44px interaction floor`);
      }
    }
  }
  return problems;
}

const transparentCssColour = (value) =>
  ["transparent", "rgba(0, 0, 0, 0)"].includes(String(value ?? "").trim());

function conceptDiagramPrintProblems(state) {
  const problems = [];
  for (const [index, diagram] of (state?.diagrams ?? []).entries()) {
    const name = `concept diagram ${index + 1}`;
    const media = diagram?.media;
    if (media?.printMarker !== "ready") problems.push(`${name} print rules are not active`);
    if (!["avoid", "avoid-page"].includes(media?.breakInside)) {
      problems.push(`${name} can split across printed pages`);
    }
    if (!transparentCssColour(media?.figureBackground)) {
      problems.push(`${name} keeps its screen surface in print`);
    }
    if (media?.stepBackgrounds?.some((colour) => !transparentCssColour(colour))) {
      problems.push(`${name} keeps step surfaces in print`);
    }
    if (media?.indexBackgrounds?.some((colour) => !transparentCssColour(colour))) {
      problems.push(`${name} keeps index surfaces in print`);
    }
    if (media?.figureBackgrounds?.some((colour) => !transparentCssColour(colour))) {
      problems.push(`${name} keeps strip surfaces in print`);
    }
    if (media?.stepBreakInside?.some((value) => !["avoid", "avoid-page"].includes(value))) {
      problems.push(`${name} permits a printed step to split`);
    }
  }
  return problems;
}

function conceptDiagramForcedColorsProblems(state) {
  const problems = [];
  for (const [index, diagram] of (state?.diagrams ?? []).entries()) {
    const name = `concept diagram ${index + 1}`;
    const media = diagram?.media;
    if (!media?.forcedColorsActive) problems.push(`${name} forced-colors emulation is inactive`);
    if (media?.forcedMarker !== "active") problems.push(`${name} forced-colors rules are not active`);
    if (transparentCssColour(media?.figureBackground)) {
      problems.push(`${name} has no forced-colors Canvas surface`);
    }
    if (
      !media?.canvasText ||
      media.borderColors?.some((colour) => colour !== media.canvasText) ||
      media.connectorColors?.some((colour) => colour !== media.canvasText) ||
      media.textColors?.some((colour) => colour !== media.canvasText)
    ) {
      problems.push(`${name} does not use CanvasText for forced-colors structure`);
    }
    if (
      media?.stepBackgrounds?.some((colour) => colour !== media.figureBackground) ||
      media?.indexBackgrounds?.some((colour) => colour !== media.figureBackground)
    ) {
      problems.push(`${name} does not use one forced-colors Canvas surface`);
    }
    // 2026-09-02 — every stroke in a strip is `currentColor`, so the strip's
    // own `color` is the one value that decides whether the drawing survives
    // forced colors.
    if (media?.figureColors?.some((colour) => colour !== media.canvasText)) {
      problems.push(`${name} paints strips outside CanvasText`);
    }
  }
  return problems;
}

const CONCEPT_DIAGRAM_PROBE = `(() => {
  const visible = (element) => {
    const style = getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.display !== "none" && style.visibility !== "hidden" &&
      Number(style.opacity) > 0 && rect.width > 0 && rect.height > 0;
  };
  const diagrams = [...document.querySelectorAll("[data-concept-diagram]")].map((diagram) => {
    const rect = diagram.getBoundingClientRect();
    const captions = diagram.querySelectorAll(":scope > figcaption");
    const title = diagram.querySelector("[data-concept-title]");
    const summary = diagram.querySelector("[data-concept-summary]");
    const orderedLists = diagram.querySelectorAll(":scope > ol");
    const steps = [...diagram.querySelectorAll(":scope > ol > [data-concept-step]")];
    const directItems = diagram.querySelectorAll(":scope > ol > li");
    const stepRects = steps.map((step) => step.getBoundingClientRect());
    const optionItems = [...diagram.querySelectorAll(":scope > ol > li > ul > li")];
    // 2026-09-02 — the drawn strip: at most one per step, an inline SVG that
    // carries the chapter's geometry. Its text lives in HTML spans beside it,
    // never inside the SVG, because every text gate reads the page's HTML and
    // treats <svg> as opaque.
    const figures = steps.map((step) => step.querySelector(".concept-figure")).filter(Boolean);
    const figureStyles = figures.map((figure) => getComputedStyle(figure));
    const diagramStyle = getComputedStyle(diagram);
    const diagramPaddingInlineEnd = Number.parseFloat(diagramStyle.paddingInlineEnd) || 0;
    const diagramPaddingInlineStart = Number.parseFloat(diagramStyle.paddingInlineStart) || 0;
    const stepStyles = steps.map((step) => getComputedStyle(step));
    const indexStyles = steps.map((step) =>
      getComputedStyle(step.querySelector(".concept-index"))
    );
    const connectorStyles = steps.slice(0, -1).map((step) => getComputedStyle(step, "::after"));
    const tools = [...diagram.querySelectorAll(":scope > .fig-tools")].filter(visible);
    const tool = tools[0] ?? null;
    const toolRect = tool?.getBoundingClientRect() ?? null;
    const captionRect = captions[0]?.getBoundingClientRect() ?? null;
    const toolButtons = [...(tool?.querySelectorAll(":scope > .fig-tool") ?? [])];
    const incompleteSteps = steps.filter((step) =>
      !step.querySelector("[data-concept-label]")?.textContent.trim() ||
      !step.querySelector("[data-concept-step-title]")?.textContent.trim() ||
      !step.querySelector("[data-concept-detail]")?.textContent.trim()
    ).length;
    const hiddenSteps = steps.filter((step) => !visible(step)).length;
    // Measure real content children rather than the step's scroll box. The
    // sequence arrow is an intentionally outboard pseudo-element; it expands
    // scrollWidth on every non-final card even when neither the arrow nor the
    // card content is clipped.
    const stepOverflowCount = steps.filter((step) => {
      const stepRect = step.getBoundingClientRect();
      return [...step.children].some((child) => {
        const childRect = child.getBoundingClientRect();
        return childRect.left < stepRect.left - 1 || childRect.right > stepRect.right + 1 ||
          childRect.top < stepRect.top - 1 || childRect.bottom > stepRect.bottom + 1;
      });
    }).length;
    const clippedSteps = stepRects.filter((stepRect) =>
      stepRect.left < rect.left - 1 || stepRect.right > rect.right + 1 ||
      stepRect.top < rect.top - 1 || stepRect.bottom > rect.bottom + 1
    ).length;
    const outOfOrderSteps = stepRects.slice(1).filter((stepRect, stepIndex) => {
      const previous = stepRects[stepIndex];
      return stepRect.top < previous.top - 1 ||
        (Math.abs(stepRect.top - previous.top) <= 1 && stepRect.left < previous.left - 1);
    }).length;
    const nonVerticalTransitions = stepRects.slice(1).filter((stepRect, stepIndex) => {
      const previous = stepRects[stepIndex];
      return stepRect.top < previous.bottom - 1 || Math.abs(stepRect.left - previous.left) > 1;
    }).length;
    const clippedOptions = optionItems.filter((option) => {
      const optionRect = option.getBoundingClientRect();
      const stepRect = option.closest("[data-concept-step]")?.getBoundingClientRect();
      return !stepRect || optionRect.left < stepRect.left - 1 ||
        optionRect.right > stepRect.right + 1 || optionRect.top < stepRect.top - 1 ||
        optionRect.bottom > stepRect.bottom + 1 ||
        option.scrollWidth - option.clientWidth > 1 || option.scrollHeight - option.clientHeight > 1;
    }).length;
    const hiddenConnectors = connectorStyles.filter((style) =>
      !style || style.content === "none" || style.content === "normal" ||
      style.display === "none" || style.visibility === "hidden" ||
      Number(style.opacity) <= 0 || style.color === "rgba(0, 0, 0, 0)"
    ).length;
    const boxedConnectors = connectorStyles.filter((style) =>
      Number.parseFloat(style.borderTopWidth) > 0 ||
      !["0px", "0%"].includes(style.borderRadius)
    ).length;
    const titleWidthRatios = steps.map((step) => {
      const titleRect = step.querySelector("[data-concept-step-title]")?.getBoundingClientRect();
      const stepRect = step.getBoundingClientRect();
      const style = getComputedStyle(step);
      const innerWidth = stepRect.width -
        (Number.parseFloat(style.paddingInlineStart) || 0) -
        (Number.parseFloat(style.paddingInlineEnd) || 0);
      return titleRect && innerWidth > 0 ? titleRect.width / innerWidth : 0;
    });
    return {
      tagName: diagram.tagName,
      visible: visible(diagram),
      width: rect.width,
      height: rect.height,
      captionCount: captions.length,
      title: title && visible(title) ? title.textContent.trim() : "",
      summary: summary && visible(summary) ? summary.textContent.trim() : "",
      variant: diagram.dataset.variant ?? "",
      orderedListCount: orderedLists.length,
      listRoleAttribute: orderedLists[0]?.getAttribute("role") ?? null,
      stepCount: steps.length,
      directItemCount: directItems.length,
      nonListSteps: steps.filter((step) => step.tagName !== "LI").length,
      // Rounded top edges, one per step. A grid pseudo-element placed into the
      // card columns OCCUPIES them for auto-placement, and the boundary
      // variant's first draft learned it: cards 2-4 were displaced into rows
      // below the zone — visible, ordered, unclipped, and wrong — and nothing
      // here failed. Row-band membership is the property that catches it.
      stepTops: steps.map((step) => Math.round(step.getBoundingClientRect().top)),
      gridColumnCount: orderedLists[0]
        ? getComputedStyle(orderedLists[0]).gridTemplateColumns.trim().split(/\\s+/u).length
        : 0,
      incompleteSteps,
      hiddenSteps,
      clippedSteps,
      selfOverflowX: diagram.scrollWidth - diagram.clientWidth,
      selfOverflowY: diagram.scrollHeight - diagram.clientHeight,
      stepOverflowCount,
      optionCount: optionItems.length,
      hiddenOptions: optionItems.filter((option) => !visible(option)).length,
      clippedOptions,
      connectorCount: connectorStyles.length,
      hiddenConnectors,
      boxedConnectors,
      figureCount: figures.length,
      figureTextCount: diagram.querySelectorAll(":scope > ol > li svg text").length,
      hiddenFigures: figures.filter((figure) => !visible(figure)).length,
      minimumTitleWidthRatio: titleWidthRatios.length ? Math.min(...titleWidthRatios) : 0,
      outOfOrderSteps,
      nonVerticalTransitions,
      toolCount: tools.length,
      toolLabels: toolButtons.map((button) => button.textContent.trim()),
      toolButtonHeights: toolButtons.map((button) => button.getBoundingClientRect().height),
      toolsAfterSteps: Boolean(
        toolRect && stepRects.length &&
        toolRect.top >= Math.max(...stepRects.map((r) => r.bottom)) - 1
      ),
      toolsShareCaptionRow: Boolean(
        toolRect && captionRect &&
        toolRect.top < captionRect.bottom && toolRect.bottom > captionRect.top
      ),
      toolRightInset: toolRect
        ? Math.abs((rect.right - diagramPaddingInlineEnd) - toolRect.right)
        : null,
      captionToolGap: toolRect && captionRect ? toolRect.left - captionRect.right : null,
      toolsOverlapCaption: Boolean(
        toolRect && captionRect &&
        toolRect.left < captionRect.right && toolRect.right > captionRect.left &&
        toolRect.top < captionRect.bottom && toolRect.bottom > captionRect.top
      ),
      media: {
        printMarker: diagramStyle.getPropertyValue("--concept-print").trim(),
        forcedMarker: diagramStyle.getPropertyValue("--concept-forced-colors").trim(),
        forcedColorsActive: matchMedia("(forced-colors: active)").matches,
        breakInside: diagramStyle.breakInside,
        figureBackground: diagramStyle.backgroundColor,
        stepBackgrounds: stepStyles.map((style) => style.backgroundColor),
        indexBackgrounds: indexStyles.map((style) => style.backgroundColor),
        stepBreakInside: stepStyles.map((style) => style.breakInside),
        figureBackgrounds: figureStyles.map((style) => style.backgroundColor),
        figureColors: figureStyles.map((style) => style.color),
        borderColors: [diagramStyle, ...stepStyles, ...indexStyles]
          .map((style) => style.borderTopColor),
        connectorColors: connectorStyles.map((style) => style.color),
        textColors: steps.flatMap((step) => [
          getComputedStyle(step.querySelector("[data-concept-step-title]")).color,
          getComputedStyle(step.querySelector("[data-concept-detail]")).color,
        ]),
        canvasText: getComputedStyle(document.body).color,
      },
    };
  });
  return {
    documentOverflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    diagrams,
  };
})()`;

const TREND_READING_MAP_CONTRACT = Object.freeze({
  label: "trend",
  targetIds: Object.freeze(["evidence-1-1-title", "trend-weather-adjustment", "trend-airzones"]),
  firstQuestionMustFitPhoneViewport: true,
  requiresFullEvidenceOrder: false,
});
const SPACE_READING_MAP_CONTRACT = Object.freeze({
  label: "space",
  targetIds: Object.freeze(["space-distance", "space-controls", "space-inference"]),
  firstQuestionMustFitPhoneViewport: true,
  requiresFullEvidenceOrder: true,
});

function readingMapProblems(state, contract) {
  const problems = [];
  const label = contract.label;
  if (![375, 768, 1024, 1440].includes(state?.viewportWidth)) {
    return [`${label} reading map viewport is not one of the reviewed widths`];
  }
  const map = state?.map;
  const thesis = state?.thesis;
  if (map?.ariaHidden) {
    problems.push(`${label} reading map is aria-hidden`);
  } else if (map?.opacityZero) {
    problems.push(`${label} reading map has zero opacity`);
  } else if (
    !map?.visible || map.width <= 0 || map.height <= 0 ||
    !Number.isFinite(map.left) || !Number.isFinite(map.right)
  ) {
    problems.push(`${label} reading map is not visible`);
  } else if (map.clippedByAncestor) {
    problems.push(`${label} reading map is clipped by an ancestor`);
  }
  if (!thesis?.visible || thesis.width <= 0 || thesis.height <= 0) {
    problems.push(`${label} chapter thesis is not visible`);
  } else if (state.viewportWidth < 1024 && map?.top < thesis.bottom - 1) {
    problems.push(`${label} reading map no longer follows the thesis on narrow screens`);
  } else if (
    state.viewportWidth >= 1024 &&
    (map?.left < thesis.right - 1 || map?.top >= thesis.bottom)
  ) {
    problems.push(`${label} chapter desktop opening is not a two-column composition`);
  }
  if (state?.links?.length !== contract.targetIds.length) {
    problems.push(`${label} reading map link inventory changed`);
  }
  if (state?.targets?.length !== contract.targetIds.length) {
    problems.push(`${label} reading map target inventory changed`);
  }
  if (
    !Array.isArray(state?.targetIds) ||
    state.targetIds.length !== contract.targetIds.length ||
    state.targetIds.some((targetId, index) => targetId !== contract.targetIds[index])
  ) {
    problems.push(`${label} reading map target IDs changed`);
  }
  for (const [index, link] of (state?.links ?? []).entries()) {
    if (link.ariaHidden) {
      problems.push(`${label} reading map link ${index + 1} is aria-hidden`);
    } else if (link.opacityZero) {
      problems.push(`${label} reading map link ${index + 1} has zero opacity`);
    } else if (!link.visible || link.width < 44 || link.height < 44 || link.clippedByAncestor) {
      problems.push(`${label} reading map link ${index + 1} target is too small`);
    }
  }
  for (const [index, target] of (state?.targets ?? []).entries()) {
    if (target.ariaHidden) {
      problems.push(`${label} reading map target ${index + 1} is aria-hidden`);
    } else if (target.opacityZero) {
      problems.push(`${label} reading map target ${index + 1} has zero opacity`);
    } else if (!target.visible || target.width <= 0 || target.height <= 0) {
      problems.push(`${label} reading map target ${index + 1} is not visible`);
    } else if (target.clippedByAncestor) {
      problems.push(`${label} reading map target ${index + 1} is clipped by an ancestor`);
    }
    if (state?.anchorsMeasured) {
      if (!Number.isFinite(target.afterJumpTop)) {
        problems.push(`${label} reading map target ${index + 1} landing geometry is invalid`);
      } else if (target.afterJumpTop < state.stickyBottom - 1) {
        problems.push(`${label} reading map target ${index + 1} is obscured after jump`);
      } else if (target.afterJumpTop >= state.viewportHeight) {
        problems.push(`${label} reading map target ${index + 1} is outside the viewport after jump`);
      }
    }
  }
  if (!state?.sourceOrdered) problems.push(`${label} reading map source order changed`);
  if (contract.requiresFullEvidenceOrder && !state?.evidenceOrdered) {
    problems.push(`${label} field-note evidence order changed`);
  }
  if (!state?.primary?.visible || state.primary.width <= 0 || state.primary.height <= 0) {
    problems.push(`${label} primary evidence is not visible`);
  } else if (state.primary.clippedByAncestor) {
    problems.push(`${label} primary evidence is clipped by an ancestor`);
  }
  if (
    contract.firstQuestionMustFitPhoneViewport &&
    state?.viewportWidth === 375 &&
    state?.targets?.[0]?.top > state.viewportHeight + 1
  ) {
    problems.push(`${label} primary evidence question leaves the first phone viewport`);
  }
  if (state?.horizontalOverflow > 1) {
    problems.push(`${label} reading map causes horizontal overflow`);
  }
  return problems;
}

function readingMapPrintProblems(state, contract) {
  const problems = [];
  if (!state?.thesisVisible) problems.push(`${contract.label} print thesis is not visible`);
  if (!state?.mapVisible) problems.push(`${contract.label} print reading map is not visible`);
  if (!state?.primaryVisible) problems.push(`${contract.label} print primary evidence is not visible`);
  if (state?.linksVisible?.length !== contract.targetIds.length) {
    problems.push(`${contract.label} print link inventory changed`);
  }
  for (const [index, visible] of (state?.linksVisible ?? []).entries()) {
    if (!visible) problems.push(`${contract.label} print link ${index + 1} is not visible`);
  }
  if (state?.targetsVisible?.length !== contract.targetIds.length) {
    problems.push(`${contract.label} print target inventory changed`);
  }
  for (const [index, visible] of (state?.targetsVisible ?? []).entries()) {
    if (!visible) problems.push(`${contract.label} print target ${index + 1} is not visible`);
  }
  if (!state?.sourceOrdered) problems.push(`${contract.label} print source order changed`);
  if (contract.requiresFullEvidenceOrder && !state?.evidenceOrdered) {
    problems.push(`${contract.label} print evidence order changed`);
  }
  return problems;
}

function stationRegisterProblems(state, mode) {
  const problems = [];
  if (mode !== "no-JavaScript" && mode !== "print") {
    return ["station register mode is not reviewed"];
  }
  if (state?.selectorVisible || (mode === "print" && state?.liveVisible)) {
    problems.push(`station ${mode} controls remain visible`);
  }
  if (mode === "no-JavaScript" && !state?.noScriptVisible) {
    problems.push("station no-JavaScript notice is not visible");
  }
  if (mode === "print" && state?.noScriptVisible) {
    problems.push("station print controls remain visible");
  }
  if (
    !Number.isInteger(state?.reportCount) || state.reportCount <= 0 ||
    state?.visibleReportCount !== state.reportCount
  ) {
    problems.push(`station ${mode} complete report register is unavailable`);
  }
  if (state?.stationNameCount !== state?.reportCount) {
    problems.push(`station ${mode} station-name inventory changed`);
  }
  if (state?.visibleStationNameCount !== state?.reportCount) {
    problems.push(`station ${mode} visible station-name register is unavailable`);
  }
  if (state?.matchingStationNameCount !== state?.reportCount) {
    problems.push(`station ${mode} displayed station identities disagree`);
  }
  if (!state?.ordered) problems.push(`station ${mode} report order changed`);
  if (state?.standardNotes !== 1 || state?.conversionNotes !== 0) {
    problems.push(`station ${mode} interpretation notes changed`);
  }
  return problems;
}

function stationDossierProblems(state) {
  const problems = [];
  const expectedColumns = new Map([[375, 1], [768, 2], [1024, 4], [1440, 4]]);
  const expectedStatisticTops = [0, 0, 0, 0];
  if (!expectedColumns.has(state?.viewportWidth)) {
    return ["station dossier viewport is not one of the reviewed widths"];
  }
  const visibleBox = (part) =>
    Boolean(part?.visible && part.width > 0 && part.height > 0);
  if (!visibleBox(state?.picker)) problems.push("station dossier is not visibly rendered");
  if (!visibleBox(state?.controls)) problems.push("station locator is not visibly rendered");
  else {
    /*
     * Bounded, not spanning.
     *
     * The old contract was that the controls matched the picker's width
     * exactly, written when there were two fields side by side and the pair
     * had to fill the row. One field does not: a search box stretched to
     * 1012px is a line a station name crosses a tenth of. It takes the picker's
     * width while the picker is narrow and stops at a readable measure once
     * there is room — measured 329/329 at 375, and 660 of 1012 at 1440.
     *
     * What still has to hold is that it never exceeds the picker and never
     * collapses below something a name can be typed into.
     */
    if (state.controls.width > state.picker.width + 1) {
      problems.push("station locator is wider than the picker");
    }
    const floor = Math.min(320, state.picker.width);
    if (state.controls.width < floor - 1) {
      problems.push(
        `station locator is ${Math.round(state.controls.width)}px against a ${Math.round(floor)}px floor`,
      );
    }
  }
  if (!state?.supportingRowsFollowFields) {
    problems.push("station locator support rows are detached from their fields");
  }
  if (!state?.controlsFollowDomOrder) {
    problems.push("station locator keyboard order changed");
  }
  // The field itself: still the one thing a finger has to land on, so it keeps
  // the 44px target the menu was held to.
  if (!visibleBox(state?.select)) problems.push("station selector is not visibly rendered");
  else if (state.select.height < 44) problems.push("station selector target is shorter than 44px");
  /*
   * One control now, not two.
   *
   * The search box beside the menu narrowed the menu and nothing else, so a
   * reader typed a county, read a count off to the right and still faced a
   * closed menu — reported as a control that does nothing. The box and the menu
   * are one combobox: typing filters the list in place and the list is what
   * chooses. What used to be a two-column contract is a combobox contract.
   */
  const combo = state?.combo;
  if (!combo) {
    problems.push("station combobox is missing");
  } else {
    if (combo.role !== "combobox") problems.push("station control is not a combobox");
    if (combo.autocomplete !== "list") {
      problems.push("station combobox does not advertise list autocomplete");
    }
    if (combo.controlsListbox !== "station-listbox") {
      problems.push("station combobox does not point at its listbox");
    }
    if (!combo.listboxPresent) problems.push("station combobox has no listbox");
    if (combo.listboxRole !== "listbox") {
      problems.push("station option list is not exposed as a listbox");
    }
    // Closed on arrival: 79 stations unrolled under the field would push the
    // card the chapter exists to show below the fold.
    if (combo.listboxHiddenAtRest !== true) {
      problems.push("station combobox opens before it is asked to");
    }
    if (combo.expanded !== "false") {
      problems.push("station combobox reports itself expanded while closed");
    }
    if (combo.optionCountInList !== state.optionCount) {
      problems.push("station combobox option inventory differs from the register");
    }
    // The groups are how a reader finds a station they cannot name; losing them
    // is what the menu was kept for in the first place.
    if (!Number.isInteger(combo.groupCountInList) || combo.groupCountInList < 2) {
      problems.push("station combobox lost its county grouping");
    }
    if (
      !Array.isArray(combo.selectedOptions) || combo.selectedOptions.length !== 1 ||
      combo.selectedOptions[0] !== state.visibleStation
    ) {
      problems.push("station combobox selection and visible report disagree");
    }
  }
  // The search box, when the page carries one. Checked for the same touch
  // target the menu gets, and for the property that matters more: filtering
  // must narrow the menu without moving the selection or the card.
  if (state?.filter) {
    if (!(state.filter.height >= 44)) {
      problems.push("station filter target is shorter than 44px");
    }
    if (!state.filter.narrowedByCounty) {
      problems.push(
        `station filter did not narrow by county (${state.filter.narrowedTo} of ${state.filter.total})`,
      );
    }
    if (!state.filter.heldWhileFiltered) {
      problems.push("station filter moved the selection or the visible report");
    }
    if (!state.filter.restored) {
      problems.push("station filter did not restore the whole menu when cleared");
    }
  }
  if (
    !Number.isInteger(state?.optionCount) || !Number.isInteger(state?.reportCount) ||
    state.optionCount <= 0 || state.optionCount !== state.reportCount
  ) {
    problems.push("station option and report inventories differ");
  }
  if (state?.visibleReportCount !== 1) problems.push("station dossier does not show exactly one report");
  if (!state?.selectedValue || state.selectedValue !== state.visibleStation) {
    problems.push("station selection and visible report differ");
  }
  if (!state?.identityText || state.identityText !== state.visibleStation) {
    problems.push("station displayed identity disagrees");
  }
  if (!visibleBox(state?.identityName)) {
    problems.push("station displayed station name is not visible");
  }
  if (!state?.identityVisible) problems.push("station identity is not visible");
  if (!state?.yearVisible) problems.push("station year is not visible");
  if (state?.stats?.length !== 4) problems.push("station statistic inventory changed");
  for (const [index, stat] of (state?.stats ?? []).entries()) {
    if (!visibleBox(stat)) problems.push(`station statistic ${index + 1} is not visible`);
  }
  if (state?.comparisons?.length !== 2) problems.push("station comparison inventory changed");
  for (const [index, comparison] of (state?.comparisons ?? []).entries()) {
    if (!visibleBox(comparison)) {
      problems.push(`station comparison ${index + 1} is not visible`);
    }
  }
  const strip = state?.rankStrip;
  if (!visibleBox(strip)) {
    problems.push("station rank strip is not visible");
  } else {
    // Rank 1 is the lowest annual mean, so it sits at 0% and the last rank at
    // 100%. Solved against the two integers the same line prints, because a
    // picture that disagrees with its own caption is worse than no picture.
    const expected = strip.total > 1
      ? ((strip.rank - 1) / (strip.total - 1)) * 100 : 0;
    if (!Number.isFinite(strip.position) || Math.abs(strip.position - expected) > 0.5) {
      problems.push("station rank strip position disagrees with the stated rank");
    }
    // Where the mark actually lands, not where the attribute says it should.
    // The first version of this strip agreed with its own caption and still put
    // 2.8px of a 5.6px mark outside the track at rank 1 of 77, because it was
    // centred on the value rather than inset by its own width. The station a
    // reader looks up first is the cleanest or the dirtiest one, so both ends
    // are exactly where it mattered.
    if (
      !Number.isFinite(strip.markLeft) || !Number.isFinite(strip.markRight) ||
      strip.markLeft < -0.01 || strip.markRight > strip.trackWidth + 0.01
    ) {
      problems.push("station rank strip mark is drawn outside its track");
    }
  }
  const locator = state?.locator;
  if (!visibleBox(locator)) {
    problems.push("station locator map is not visible");
  } else {
    if (locator.countyCount !== 19) {
      problems.push(
        `station locator draws ${locator.countyCount} counties instead of 19`,
      );
    }
    if (locator.markStation === "") {
      /*
       * Two reasons a mark cannot be drawn: the card carries no coordinate (2
       * of 79), or the station is offshore and projects outside the main-island
       * frame (measured cx -432, -103 and -1 against a 0-550 viewBox). Either
       * is a fine answer. Showing nothing and saying nothing is not, and
       * leaving the previous station's mark up is the failure this exists to
       * prevent — so an empty mark name must come with no mark and with exactly
       * one of the two reasons on screen.
       */
      if (locator.markVisible) {
        problems.push("station locator shows a mark for a station it cannot place");
      }
      if (!locator.unplacedNoteVisible && !locator.offshoreNoteVisible) {
        problems.push("station locator does not say why the station is unplaced");
      }
      if (locator.unplacedNoteVisible && locator.offshoreNoteVisible) {
        problems.push("station locator gives two reasons for one unplaced station");
      }
    } else {
      if (!locator.markVisible) problems.push("station locator map has no visible mark");
      // The mark must name the station the card shows. A locator still pointing
      // at the station before last is the defect this chapter's select already
      // has a comment about: a control that confirms a choice and then shows
      // another station's numbers is a lie, and a map is no different.
      if (locator.markStation !== state.visibleStation) {
        problems.push("station locator mark and visible report disagree");
      }
      if (locator.unplacedNoteVisible || locator.offshoreNoteVisible) {
        problems.push("station locator claims a placed station is unplaced");
      }
    }
  }
  if (state?.afterChange?.performed) {
    /*
     * The changed station may be one the project cannot place, and here that is
     * not hypothetical: this harness picks the last option that is not the
     * current one, which is one of the two cards carrying no coordinate. So the
     * mark following the change means one of two things — it names the new
     * station, or it names nothing and the figure says why.
     */
    const after = state.afterChange;
    if (after.locatorMarkStation === "") {
      if (!after.locatorUnplacedVisible) {
        problems.push("station locator went silent on a station it cannot place");
      }
    } else if (after.locatorMarkStation !== after.visibleStation) {
      problems.push("station locator mark did not follow the station change");
    }
  }
  if (state?.columns !== expectedColumns.get(state.viewportWidth)) {
    problems.push(`station statistics use the wrong ${state.viewportWidth}px column count`);
  }
  const ruleIs = (actual, expected) =>
    Number.isFinite(actual) && Math.abs(actual - expected) <= 0.01;
  const separators = state?.separators;
  if (!ruleIs(separators?.reportTop, 0) || !ruleIs(separators?.reportBottom, 0)) {
    problems.push("station dossier retains a decorative outer frame");
  }
  if (!ruleIs(separators?.identityBottom, 0)) {
    problems.push("station identity retains a redundant underline");
  }
  if (!ruleIs(separators?.statisticsTop, 1)) {
    problems.push("station statistics entry separator changed");
  }
  const expectedTops = expectedStatisticTops;
  if (
    separators?.statisticTops?.length !== expectedTops.length ||
    expectedTops.some((expected, index) =>
      !ruleIs(separators?.statisticTops?.[index], expected))
  ) {
    problems.push(
      `station responsive statistic-row separators changed: expected ${JSON.stringify(expectedTops)}, ` +
      `got ${JSON.stringify(separators?.statisticTops ?? null)}`,
    );
  }
  if (!ruleIs(separators?.comparisonsTop, 1)) {
    problems.push("station comparison entry separator changed");
  }
  if (!ruleIs(separators?.noteTop, 0) || !ruleIs(separators?.noteBottom, 0)) {
    problems.push("station explanatory note retains decorative framing");
  }
  if (!visibleBox(state?.standardNote)) problems.push("station standard note is not visible");
  if (state?.horizontalOverflow > 1) problems.push("station dossier causes horizontal overflow");
  const changed = state?.afterChange;
  if (!changed?.performed) problems.push("station selection change was not exercised");
  if (changed?.visibleReportCount !== 1) {
    problems.push("station selection change does not leave exactly one visible report");
  }
  if (!changed?.selectedMatchesVisible) {
    problems.push("station selection change shows a different report");
  }
  if (
    !changed?.identityText || changed.identityText !== changed.visibleStation ||
    changed.identityText !== changed.selectedValue
  ) {
    problems.push("changed displayed identity disagrees");
  }
  if (!visibleBox(changed?.identityName)) {
    problems.push("changed displayed station name is not visible");
  }
  if (!visibleBox(changed?.identity)) {
    problems.push("changed station identity is not visible");
  }
  if (!visibleBox(changed?.year)) problems.push("changed station year is not visible");
  if (changed?.stats?.length !== 4) {
    problems.push("changed station statistic inventory changed");
  }
  for (const [index, stat] of (changed?.stats ?? []).entries()) {
    if (!visibleBox(stat)) {
      problems.push(`changed station statistic ${index + 1} is not visible`);
    }
  }
  if (changed?.comparisons?.length !== 2) {
    problems.push("changed station comparison inventory changed");
  }
  for (const [index, comparison] of (changed?.comparisons ?? []).entries()) {
    if (!visibleBox(comparison)) {
      problems.push(`changed station comparison ${index + 1} is not visible`);
    }
  }
  if (!changed?.liveIncludesStation) problems.push("station live update omits the station");
  if (!changed?.liveIncludesYear) problems.push("station live update omits the year");
  if (!changed?.liveIncludesFirstStat) {
    problems.push("station live update omits the first statistic");
  }
  if (!changed?.liveIncludesThirdStat) {
    problems.push("station live update omits the third statistic");
  }
  const restored = state?.restored;
  if (!restored?.performed) problems.push("station selection restoration was not exercised");
  if (restored?.visibleReportCount !== 1) {
    problems.push("station selection restoration does not leave exactly one visible report");
  }
  if (!restored?.selectedMatchesVisible) {
    problems.push("station selection restoration shows a different report");
  }
  if (
    !restored?.identityText || restored.identityText !== restored.visibleStation ||
    restored.identityText !== restored.selectedValue
  ) {
    problems.push("station restored displayed identity disagrees");
  }
  if (!visibleBox(restored?.identityName)) {
    problems.push("station restored displayed station name is not visible");
  }
  if (!restored?.liveIncludesStation) {
    problems.push("station restored live update omits the station");
  }
  if (!restored?.liveIncludesYear) problems.push("station restored live update omits the year");
  if (!restored?.liveIncludesFirstStat) {
    problems.push("station restored live update omits the first statistic");
  }
  if (!restored?.liveIncludesThirdStat) {
    problems.push("station restored live update omits the third statistic");
  }
  return problems;
}

function compactIdentityProblems(state, expected) {
  const problems = [];
  if (!state?.visible) problems.push("compact site identity is missing");
  if (state?.accessibilitySource !== "accessibility-tree") {
    problems.push("compact site identity accessibility tree was not checked");
  }
  if (state?.accessibleText !== expected) problems.push("compact site identity changed");
  if (!state?.visibleText?.trim()) problems.push("compact site identity has no visible text");
  if (state?.clientWidth < state?.scrollWidth) problems.push("compact site identity is clipped");
  if (state?.textOverflow === "ellipsis") problems.push("compact site identity uses ellipsis");
  return problems;
}

function editorialHomepageOrderProblems({ opening, routes, primary, map, postMap }) {
  const positions = [opening, routes, primary, map, postMap];
  if (positions.some((value) => !Number.isInteger(value))) {
    return ["homepage editorial source order is incomplete"];
  }
  if (!(opening < routes && routes < primary && primary < map && map < postMap)) {
    return ["homepage editorial source order changed"];
  }
  return [];
}

function homepageMobileTypeProblems({
  viewportWidth,
  root,
  finding,
  routeLabel,
  routeIntro,
  routeClaim,
}) {
  const problems = [];
  const ratio = (value) => value / root;
  const close = (actual, expected) =>
    Number.isFinite(actual) && Number.isFinite(expected) && Math.abs(actual - expected) <= 0.01;
  const expected = viewportWidth <= 480
    ? { finding: 1.1, routeLabel: 1.1, routeIntro: 0.95, routeClaim: 0.95 }
    : { finding: 1.2, routeLabel: 1.2, routeIntro: 1, routeClaim: 1 };
  for (const [role, value] of Object.entries({ finding, routeLabel, routeIntro, routeClaim })) {
    if (!close(ratio(value), expected[role])) {
      problems.push(`homepage ${role} type ratio changed`);
    }
  }
  return problems;
}

function trendIdleReadoutBlankProblems(state) {
  if (!state) return ["610px trend idle-readout state is missing"];
  const problems = [];
  if (state.viewport?.width !== 610 || state.viewport?.height !== 900) {
    problems.push("trend readout-reservation probe viewport changed");
  }
  if (!Array.isArray(state.figures) || state.figures.length !== 3) {
    problems.push("trend readout-reservation figure inventory changed");
    return problems;
  }
  for (const figure of state.figures) {
    const scope = `Figure 1.${Number(figure.index) + 1}`;
    if (!figure.hasDock) {
      problems.push(`${scope} readout dock is missing`);
      continue;
    }
    if (figure.readingBefore !== "false") {
      problems.push(`${scope} idle readout state changed`);
    }
    if (!figure.idleOptIn) {
      problems.push(`${scope} latest-value panel is not opted in`);
    }
    if (!String(figure.idleWhen ?? "").startsWith("2025")) {
      problems.push(`${scope} idle readout does not identify the latest year`);
    }
    if (!Number.isFinite(figure.idleUnoccupiedReserve) ||
        figure.idleUnoccupiedReserve >= IDLE_READOUT_BLANK_LIMIT_PX) {
      problems.push(
        `${scope} idle .readout-dock leaves ` +
          `${Number(figure.idleUnoccupiedReserve).toFixed(1)}px unused`,
      );
    }
    if (!Number.isFinite(figure.idlePanelHeight) || figure.idlePanelHeight < 44 ||
        !Number.isFinite(figure.idlePanelOpacity) || figure.idlePanelOpacity < 0.99) {
      problems.push(`${scope} idle latest-value panel is not visible at full size`);
    }
  }
  return problems;
}

function semanticBoundaryProblems(state, label, required) {
  const problems = [];
  if (state?.count !== 1) problems.push(`${label} boundary inventory changed`);
  if (!state?.visible) problems.push(`${label} boundary is not visible`);
  const compact = String(state?.text ?? "").replace(/[「」\s]/gu, "");
  for (const phrase of required) {
    if (!compact.includes(phrase.replace(/\s/gu, ""))) {
      problems.push(`${label} boundary claim changed: missing ${JSON.stringify(phrase)}`);
    }
  }
  return problems;
}

function deweatherContrastBoundaryProblems(state) {
  return semanticBoundaryProblems(state, "trend deweather contrast", [
    "氣象標準化差額",
    "不是天氣造成",
    "不是排放或政策貢獻估計",
  ]);
}

function homepageStationTypeBoundaryProblems(state) {
  return semanticBoundaryProblems(state, "homepage station-type", [
    HOMEPAGE_EXTREMA.dirtiest,
    HOMEPAGE_EXTREMA.dirtiestType,
    HOMEPAGE_EXTREMA.cleanest,
    HOMEPAGE_EXTREMA.cleanestType,
    `${HOMEPAGE_EXTREMA.ratio}×是測站觀測值對比`,
    "不是純空間",
    /*
     * The reason, not only the disclaimer.
     *
     * The five phrases above survived a version that listed what the ratio was
     * not and never said why, which left a first-screen reader with three
     * negations and no mechanism. What makes them land is the classification:
     * different categories of station have different PM2.5 drivers, which is
     * this project's own stated reason for treating type as a model variable.
     * When both extrema happen to share a category that reason does not apply
     * and the confound is location alone, so the required phrase follows the
     * data the same way the sentence does.
     */
    HOMEPAGE_EXTREMA.sameType ? "地點不同" : "驅動因子本來就不同",
  ]);
}

function figureDownloadLabelProblems(state) {
  const problems = [];
  if (
    !Number.isInteger(state?.toolbarCount) ||
    !Array.isArray(state?.downloadLabels) ||
    !Array.isArray(state?.downloadAriaLabels) ||
    !Array.isArray(state?.downloadWhiteSpaces)
  ) {
    return ["figure download-label state is invalid"];
  }
  if (
    state.toolbarCount &&
    (state.downloadLabels.length !== state.toolbarCount ||
      state.downloadAriaLabels.length !== state.toolbarCount ||
      state.downloadWhiteSpaces.length !== state.toolbarCount)
  ) {
    problems.push("figure download control inventory changed");
  }
  if (state.downloadLabels.some((label) => label !== "下載")) {
    problems.push("figure download control does not use the concise visible label");
  }
  if (state.downloadAriaLabels.some((label) => !/^下載 PNG：\S/u.test(label))) {
    problems.push("figure download accessible name omits its PNG format or figure identity");
  }
  if (state.downloadWhiteSpaces.some((value) => value !== "nowrap")) {
    problems.push("figure download label can wrap inside a constrained toolbar");
  }
  return problems;
}

function trendControlClarificationProblems(state) {
  const problems = [];
  if (state?.hintCount !== 1 || !state?.hintVisible) {
    problems.push("trend zone-emphasis instruction is not visible");
  }
  if (
    !String(state?.hintText ?? "").includes("取消勾選會隱藏該線")
  ) {
    problems.push("trend zone-emphasis instruction changed");
  }
  if (state?.uncheckedLineDisplay !== "none") {
    problems.push("trend unchecked series remains rendered");
  }
  if (!state?.checkedLineDisplay || state.checkedLineDisplay === "none") {
    problems.push("trend checked series is hidden");
  }
  return problems;
}

function stationFilterHelperProblems(state) {
  const problems = [];
  if (state?.count !== 1 || !state?.visible) {
    problems.push("station filter helper is not visible");
  }
  /*
   * The promise the copy has to keep changed with the control.
   *
   * It used to say the search narrowed a menu the reader then had to open —
   * true of the two-field version, and the reason it read as doing nothing.
   * One combobox does both, so the line has to say that typing narrows the list
   * and that the list is what chooses.
   */
  if (
    !String(state?.text ?? "").includes("縮小清單") ||
    !String(state?.text ?? "").includes("從清單選")
  ) {
    problems.push("station filter helper text changed");
  }
  const describedBy = new Set(String(state?.describedBy ?? "").split(/\s+/u).filter(Boolean));
  if (!describedBy.has("station-filter-help") || !describedBy.has("station-filter-count")) {
    problems.push("station filter helper is not programmatically described");
  }
  return problems;
}

function homepageMapStationRouteProblems(state) {
  const problems = [];
  if (state?.count !== 1 || !state?.visible) {
    problems.push("homepage map station route is not visible");
  }
  if (!String(state?.text ?? "").includes("前往第二章查一個測站")) {
    problems.push("homepage map station route text changed");
  }
  try {
    if (!new URL(state?.href ?? "https://invalid.example/").pathname.endsWith("/stations/")) {
      problems.push("homepage map station route destination changed");
    }
  } catch {
    problems.push("homepage map station route destination changed");
  }
  return problems;
}

function sourcesClaimBoundaryProblems(text) {
  const required = [
    "低風速高值型",
    "中風速高值型",
    "高風速高值型",
    "不等於來源距離",
    "軌跡",
    "排放清冊",
    "化學組成",
  ];
  const forbidden = [
    "本地型",
    "傳輸型",
    "近處的污染源只有",
    "遠處的污染源要有風",
    "高值時數只能是風送來的",
  ];
  const problems = required.filter((phrase) => !text.includes(phrase))
    .map((phrase) => `missing required claim-boundary text ${JSON.stringify(phrase)}`);
  return problems.concat(
    forbidden.filter((phrase) => text.includes(phrase))
      .map((phrase) => `contains unsupported source claim ${JSON.stringify(phrase)}`),
  );
}

const SOURCES_METHOD_BOUNDARY_CLAIMS = [
  "CBPF 描述條件機率，不識別污染來源",
  "尖峰風速不等於來源距離",
];

function sourcesRestorationSnapshotProblems(snapshot, width, height, label) {
  const problems = [];
  if (!snapshot || typeof snapshot !== "object" || Array.isArray(snapshot)) {
    return [`Sources station restoration ${label} snapshot is invalid`];
  }
  for (const key of ["atlas", "focus", "url", "scroll"]) {
    if (!Object.hasOwn(snapshot, key)) {
      problems.push(`Sources station restoration snapshot is missing ${key} in ${label}`);
    }
  }
  if (snapshot.atlas && typeof snapshot.atlas === "object" && !Array.isArray(snapshot.atlas)) {
    for (const problem of sourcesAtlasProblems(snapshot.atlas, width, height)) {
      problems.push(`Sources station restoration ${label} atlas: ${problem}`);
    }
  } else if (Object.hasOwn(snapshot, "atlas")) {
    problems.push(`Sources station restoration ${label} atlas is invalid`);
  }
  if (
    Object.hasOwn(snapshot, "focus") &&
    (!snapshot.focus || typeof snapshot.focus !== "object" || Array.isArray(snapshot.focus) ||
      !["tag", "id", "name"].every((key) => typeof snapshot.focus[key] === "string"))
  ) {
    problems.push(`Sources station restoration ${label} focus is invalid`);
  }
  if (Object.hasOwn(snapshot, "url") &&
      (typeof snapshot.url !== "string" || !snapshot.url)) {
    problems.push(`Sources station restoration ${label} URL is invalid`);
  }
  if (Object.hasOwn(snapshot, "scroll") &&
      (!Array.isArray(snapshot.scroll) || snapshot.scroll.length !== 2 ||
        !snapshot.scroll.every(Number.isFinite))) {
    problems.push(`Sources station restoration ${label} scroll is invalid`);
  }
  return problems;
}

function sourcesAtlasProblems(
  state,
  width,
  height,
  { allowPickerHidden = false, noScript = false, requireRestoration = false } = {},
) {
  const problems = [];
  const visible = (part) => part?.visible && part?.rect?.width > 0 && part?.rect?.height > 0;
  const exact = (actual, expected, label) => {
    if (actual !== expected) problems.push(`Sources ${label} changed`);
  };
  if (state?.boundary?.count !== 1) problems.push("Sources method boundary inventory changed");
  if (!visible(state?.boundary)) problems.push("Sources method boundary is not visible");
  if (state?.boundary?.ariaHidden === "true") problems.push("Sources method boundary is aria-hidden");
  if (state?.boundary?.opacity === 0) problems.push("Sources method boundary is transparent");
  if (state?.boundary?.clipped) problems.push("Sources method boundary is clipped");
  for (const claim of SOURCES_METHOD_BOUNDARY_CLAIMS) {
    if (!state?.boundary?.text?.includes(claim)) {
      problems.push(`Sources method boundary claim changed: missing ${JSON.stringify(claim)}`);
    }
  }
  if (state?.picker?.count !== 1) problems.push("Sources picker inventory changed");
  if (noScript && visible(state?.picker)) {
    problems.push("Sources no-JavaScript picker remains visible");
  } else if (!noScript && !allowPickerHidden && !visible(state?.picker)) {
    problems.push("Sources picker is not visible");
  }
  if (noScript) {
    if (state?.fallback?.count !== 1) {
      problems.push("Sources no-JavaScript fallback inventory changed");
    }
    if (!visible(state?.fallback)) {
      problems.push("Sources no-JavaScript fallback is not visible");
    }
    exact(
      state?.fallback?.station,
      state?.expected?.initialStation,
      "no-JavaScript fallback station identity",
    );
    exact(
      state?.fallback?.classification,
      state?.expected?.badge?.text,
      "no-JavaScript fallback classification",
    );
  }
  if (state?.primary?.count !== 1) problems.push("Sources primary figure inventory changed");
  if (!visible(state?.primary)) problems.push("Sources primary figure is not visible");
  if (!visible(state?.primary?.title)) problems.push("Sources primary figure title is not visible");
  if (!visible(state?.primary?.plot)) problems.push("Sources primary plot is not visible");
  const order = state?.sourceIndexes;
  if (!order || ![order.lede, order.boundary, order.picker, order.primary].every(Number.isInteger)) {
    problems.push("Sources source order is incomplete");
  } else if (!(order.lede < order.boundary && order.boundary < order.picker && order.picker < order.primary)) {
    problems.push("Sources boundary, picker, and primary source order changed");
  }
  if (!state?.skipPhoneEntry && width <= 375 && height <= 812) {
    if (state?.primary?.title?.rect?.top >= height) {
      problems.push("Sources phone primary title enters below 812px");
    }
    if (state?.primary?.plot?.rect?.top >= height) {
      problems.push("Sources phone primary plot enters below 812px");
    }
  }
  if ((state?.overflow ?? 0) > 0.1) problems.push("Sources document has horizontal overflow");
  const expected = state?.expected;
  if (!expected) {
    problems.push("Sources expected station state is missing");
    return problems;
  }
  exact(state?.selectedStation, expected.station, "selected station identity");
  exact(state?.initialStation, expected.initialStation, "initial station identity");
  exact(state?.badge?.text, expected.badge.text, "wind-peak badge text");
  exact(state?.badge?.windPeakClass, expected.badge.windPeakClass, "wind-peak class");
  exact(state?.captionStation, expected.station, "caption station");
  for (const key of ["threshold", "peak", "peakSpeed", "resultant", "calm"]) {
    exact(state?.readouts?.[key], expected.readouts[key], `${key} readout`);
    if (state?.readoutVisibility?.[key] !== true) {
      problems.push(`Sources ${key} readout is not visible`);
    }
  }
  if (state?.cells?.length !== expected.cells.length) {
    problems.push("Sources CBPF cell inventory changed");
  } else {
    for (let index = 0; index < expected.cells.length; index += 1) {
      const actual = state.cells[index];
      const cell = expected.cells[index];
      if (actual?.key !== cell.key) problems.push(`Sources CBPF cell order changed at ${index}`);
      if (actual?.fill !== cell.fill) problems.push(`Sources CBPF cell fill changed at ${cell.key}`);
      if (actual?.title !== cell.title) problems.push(`Sources CBPF cell title changed at ${cell.key}`);
    }
  }
  if (requireRestoration) {
    if (!state?.restoration) {
      problems.push("Sources station restoration is missing");
    } else {
      problems.push(...sourcesRestorationSnapshotProblems(
        state.restoration.before,
        width,
        height,
        "before",
      ));
      problems.push(...sourcesRestorationSnapshotProblems(
        state.restoration.after,
        width,
        height,
        "after",
      ));
      if (JSON.stringify(state.restoration.before) !== JSON.stringify(state.restoration.after)) {
        problems.push(
          "Sources station restoration changed selected station, focus, URL, scroll, or snapshot",
        );
      }
    }
  }
  return problems;
}

function detectionClaimBoundaryProblems(text) {
  const required = [
    "觀測－預測差額",
    "不等同於已識別的因果效應",
    "沒有驗證機組的逐時操作或燃料狀態",
  ];
  const forbidden = [
    "什麼都沒發生",
    "幾乎確定是邊際報酬遞減",
    "從未真的停止燃煤",
    "事情沒發生",
  ];
  const problems = required.filter((phrase) => !text.includes(phrase))
    .map((phrase) => `missing required detection-limit text ${JSON.stringify(phrase)}`);
  return problems.concat(
    forbidden.filter((phrase) => text.includes(phrase))
      .map((phrase) => `contains unsupported detection claim ${JSON.stringify(phrase)}`),
  );
}

const DETECTION_LIMIT_PAYLOAD = JSON.parse(
  readFileSync(join(process.cwd(), "web", "public", "data", "story", "detection-limit.json"), "utf8"),
);

const DETECTION_EVENT_CONTRACT = [
  { event: "COVID-19 全國三級警戒", kind: "window" },
  { event: "台中電廠 2、3 號機生煤許可爭議", kind: "window" },
  { event: "2018 空氣污染防制法修正", kind: "trend_break" },
];

function detectionEventKindLabel(kind) {
  if (kind === "window") return "窗口事件：觀測－預測差額";
  if (kind === "trend_break") return "趨勢斷點：斜率差";
  return null;
}

function detectionTextIdentity(value) {
  return String(value ?? "").replace(/\s+/gu, "");
}

function detectionExpectedEventsFromPayload(payload) {
  if (!payload || !Array.isArray(payload.events)) {
    throw new Error("detection-limit payload has no events array");
  }
  if (payload.events.length !== DETECTION_EVENT_CONTRACT.length) {
    throw new Error(
      `detection-limit event inventory is ${payload.events.length}, ` +
        `expected ${DETECTION_EVENT_CONTRACT.length}`,
    );
  }
  const identities = new Set();
  for (const [index, row] of payload.events.entries()) {
    if (!row || typeof row !== "object" || Array.isArray(row)) {
      throw new Error(`detection-limit event ${index + 1} is not an object`);
    }
    if (
      !Object.prototype.hasOwnProperty.call(row, "event") ||
      typeof row.event !== "string" || !row.event
    ) {
      throw new Error(`detection-limit event ${index + 1} has no exact identity`);
    }
    if (identities.has(row.event)) {
      throw new Error(`detection-limit event identity ${JSON.stringify(row.event)} is duplicated`);
    }
    identities.add(row.event);
  }
  return payload.events.map((row, index) => {
    const contract = DETECTION_EVENT_CONTRACT[index];
    if (row.event !== contract.event) {
      throw new Error(
        `detection-limit event ${index + 1} identity is ${JSON.stringify(row.event)}, ` +
          `expected ${JSON.stringify(contract.event)}`,
      );
    }
    if (row.kind !== contract.kind) {
      throw new Error(
        `detection-limit event ${index + 1} kind is ${JSON.stringify(row.kind)}, ` +
          `expected ${JSON.stringify(contract.kind)}`,
      );
    }
    const has = (key) => Object.prototype.hasOwnProperty.call(row, key);
    if (
      !has("n_credible") || typeof row.n_credible !== "number" ||
      !Number.isFinite(row.n_credible) || !Number.isInteger(row.n_credible) || row.n_credible < 0
    ) {
      throw new Error(
        `detection-limit event ${index + 1} n_credible is not a nonnegative integer`,
      );
    }
    if (
      !has("n_expected_by_chance") || typeof row.n_expected_by_chance !== "number" ||
      !Number.isFinite(row.n_expected_by_chance) || row.n_expected_by_chance < 0
    ) {
      throw new Error(
        `detection-limit event ${index + 1} n_expected_by_chance is not nonnegative`,
      );
    }
    if (row.n_credible >= row.n_expected_by_chance) {
      throw new Error(
        `detection-limit event ${index + 1} no longer supports the below-chance claim`,
      );
    }
    return {
      event: row.event,
      kind: row.kind,
      observed: row.n_credible,
      expected: row.n_expected_by_chance,
    };
  });
}

const EXPECTED_DETECTION_EVENTS = detectionExpectedEventsFromPayload(DETECTION_LIMIT_PAYLOAD);

function detectionLimitationBriefProblems(state, expectedEvents, viewport) {
  const problems = [];
  const modeLabels = {
    normal: "",
    "no-js": "no-JavaScript ",
    print: "print ",
    zoom: "zoom ",
  };
  const mode = state?.mode;
  if (!Object.prototype.hasOwnProperty.call(modeLabels, mode)) {
    return ["detection limitation-brief mode is invalid"];
  }
  const scope = modeLabels[mode];
  const regionLabels = {
    readingKey: "reading key",
    comparison: "comparison",
    boundary: "boundary",
  };
  for (const [key, label] of Object.entries(regionLabels)) {
    const count = state?.counts?.[key];
    if (count !== 1) {
      problems.push(`${scope}${label} count is ${String(count)}, expected 1`);
    }
    const region = state?.regions?.[key];
    if (!region) continue;
    if (region.hidden) problems.push(`${scope}${label} is hidden`);
    if (region.ariaHidden) problems.push(`${scope}${label} is aria-hidden`);
    if (region.display === "none") problems.push(`${scope}${label} display is none`);
    if (["hidden", "collapse"].includes(region.visibility)) {
      problems.push(`${scope}${label} visibility is hidden`);
    }
    if (!region.rendered) problems.push(`${scope}${label} is not rendered`);
    if (!Number.isFinite(region.opacity) || region.opacity <= 0) {
      problems.push(`${scope}${label} opacity is zero`);
    }
    if (
      !Number.isFinite(region.width) || !Number.isFinite(region.height) ||
      region.width <= 0 || region.height <= 0
    ) {
      problems.push(`${scope}${label} has no rendered area`);
    }
    if (region.selfOverflowX > 1 || region.selfOverflowY > 1) {
      problems.push(`${scope}${label} clips its own content`);
    }
    if (region.ancestorClipped) problems.push(`${scope}${label} is clipped by an ancestor`);
    if (region.cssClip) problems.push(`${scope}${label} uses CSS clip`);
    if (region.cssClipPath) problems.push(`${scope}${label} uses CSS clip-path`);
    if (region.inert || !region.accessible) {
      problems.push(`${scope}${label} is excluded from accessibility`);
    }
    if (region.detailsAncestor) problems.push(`${scope}${label} is user-collapsible`);
  }

  const landmarkLabels = {
    title: "title",
    key: "reading key",
    primaryPlot: "primary plot",
    caption: "caption",
    comparison: "comparison",
    boundary: "boundary",
    methodEvidence: "method evidence",
  };
  for (const [key, label] of Object.entries(landmarkLabels)) {
    const landmark = state?.landmarks?.[key];
    if (!landmark) {
      problems.push(`${scope}${label} landmark is missing`);
      continue;
    }
    const geometry = ["top", "right", "bottom", "left", "width", "height"];
    if (
      !geometry.every((edge) => Number.isFinite(landmark[edge])) ||
      !Number.isInteger(landmark.sourceIndex) || landmark.sourceIndex < 0
    ) {
      problems.push(`${scope}${label} landmark geometry is invalid`);
    }
    if (landmark.cssOrder !== 0) problems.push(`${scope}${label} uses CSS order`);
  }

  const landmarks = state?.landmarks ?? {};
  const openingKeys = [
    "title",
    "key",
    "primaryPlot",
    "caption",
    "comparison",
    "boundary",
    "methodEvidence",
  ];
  const openingParts = openingKeys.map((key) => landmarks[key]);
  if (openingParts.every(Boolean)) {
    const sourceOrdered = openingParts.every(
      (part, index) => index === 0 || openingParts[index - 1].sourceIndex < part.sourceIndex,
    );
    const visuallyOrdered = openingParts.every(
      (part, index) => index === 0 || openingParts[index - 1].top < part.top,
    );
    if (!sourceOrdered || !visuallyOrdered) {
      problems.push(`${scope}opening order changed`);
    }
  }
  if (
    landmarks.key && landmarks.primaryPlot &&
    !(
      landmarks.key.sourceIndex < landmarks.primaryPlot.sourceIndex &&
      landmarks.key.top < landmarks.primaryPlot.top
    )
  ) {
    problems.push(`${scope}reading key no longer precedes primary plot`);
  }
  if (
    landmarks.comparison && landmarks.boundary &&
    !(
      landmarks.comparison.sourceIndex < landmarks.boundary.sourceIndex &&
      landmarks.comparison.top < landmarks.boundary.top
    )
  ) {
    problems.push(`${scope}boundary no longer follows comparison`);
  }
  if (
    landmarks.boundary && landmarks.methodEvidence &&
    !(
      landmarks.boundary.sourceIndex < landmarks.methodEvidence.sourceIndex &&
      landmarks.boundary.top < landmarks.methodEvidence.top
    )
  ) {
    problems.push(`${scope}boundary no longer precedes method evidence`);
  }

  const expectedSteps = [
    ["placebo", "先看灰線：沒有事件標記時，同一程序仍會算出的差額。"],
    ["event", "再看橘點：事件窗口各測站的觀測－預測差額。"],
    ["threshold", "最後看門檻：通過數是否高於純靠機率的預期。"],
  ];
  if (state?.readingSteps?.length !== expectedSteps.length) {
    problems.push(`${scope}reading step inventory changed`);
  }
  for (const [index, [key, text]] of expectedSteps.entries()) {
    const step = state?.readingSteps?.[index];
    if (!step) continue;
    if (step.key !== key) problems.push(`${scope}reading step ${index + 1} key changed`);
    if (step.accessibleText !== text) {
      problems.push(`${scope}reading step ${index + 1} text changed`);
    }
    const geometry = ["top", "right", "bottom", "left", "width", "height"];
    if (
      !geometry.every((edge) => Number.isFinite(step[edge])) ||
      !Number.isInteger(step.sourceIndex) || step.sourceIndex < 0
    ) {
      problems.push(`${scope}reading step ${index + 1} geometry is invalid`);
    }
    if (step.cssOrder !== 0) {
      problems.push(`${scope}reading step ${index + 1} uses CSS order`);
    }
    const next = state?.readingSteps?.[index + 1];
    if (next) {
      const followsVisually =
        step.top < next.top - 1 ||
        (Math.abs(step.top - next.top) <= 1 && step.left < next.left);
      if (!(step.sourceIndex < next.sourceIndex && followsVisually)) {
        problems.push(`${scope}reading step ${index + 1} visual order changed`);
      }
    }
  }

  if (!Array.isArray(expectedEvents) || !Array.isArray(state?.eventRows)) {
    problems.push(`${scope}event rows are missing`);
  } else {
    if (state?.counts?.semanticRows !== expectedEvents.length) {
      problems.push(
        `${scope}semantic row inventory is ${String(state?.counts?.semanticRows)}, ` +
          `expected ${expectedEvents.length}`,
      );
    }
    if (state?.counts?.eventHooks !== expectedEvents.length) {
      problems.push(
        `${scope}event hook inventory is ${String(state?.counts?.eventHooks)}, ` +
          `expected ${expectedEvents.length}`,
      );
    }
    if (state.eventRows.length !== expectedEvents.length) {
      problems.push(`${scope}event row inventory is ${state.eventRows.length}, expected ${expectedEvents.length}`);
    }
    const identities = state.eventRows.map((row) => row?.event);
    if (new Set(identities).size !== identities.length) {
      problems.push(`${scope}event identity is duplicated`);
    }
    const trendBreak = expectedEvents.find((event) => event.kind === "trend_break")?.event;
    if (trendBreak && !identities.includes(trendBreak)) {
      problems.push(`${scope}trend-break event row is missing`);
    }
    for (const [index, expected] of expectedEvents.entries()) {
      const row = state.eventRows[index];
      if (!row) continue;
      const keys = Object.keys(row).sort();
      if (
        JSON.stringify(keys) !==
        JSON.stringify([
          "accessibleText",
          "directChildTags",
          "event",
          "expected",
          "hooked",
          "inspection",
          "kind",
          "observed",
          "rowTag",
          "visibleText",
        ])
      ) {
        problems.push(`${scope}event row ${index + 1} keys changed`);
      }
      if (
        typeof row.observed !== "number" || !Number.isFinite(row.observed) ||
        !Number.isInteger(row.observed) || row.observed < 0
      ) {
        problems.push(`${scope}event row ${index + 1} observed value is not a nonnegative integer`);
      }
      if (
        typeof row.expected !== "number" || !Number.isFinite(row.expected) || row.expected < 0
      ) {
        problems.push(`${scope}event row ${index + 1} expected value is not nonnegative`);
      }
      if (row.event !== expected.event) {
        problems.push(`${scope}event row ${index + 1} identity changed`);
      }
      if (row.kind !== expected.kind) {
        problems.push(`${scope}event row ${index + 1} kind changed`);
      }
      if (row.observed !== expected.observed) {
        problems.push(`${scope}event row ${index + 1} observed value changed`);
      }
      if (row.expected !== expected.expected) {
        problems.push(`${scope}event row ${index + 1} expected value changed`);
      }
      if (
        row.rowTag !== "DIV" || !row.hooked ||
        JSON.stringify(row.directChildTags) !== JSON.stringify(["DT", "DD"])
      ) {
        problems.push(`${scope}event row ${index + 1} description structure changed`);
      }
      const kindLabel = detectionEventKindLabel(expected.kind);
      const exactText =
        `${expected.event} · ${kindLabel}` +
        `實際通過 ${expected.observed} 站；純靠機率的預期為 ${expected.expected} 站。`;
      if (detectionTextIdentity(row.visibleText) !== detectionTextIdentity(exactText)) {
        problems.push(`${scope}event row ${index + 1} visible text changed`);
      }
      if (detectionTextIdentity(row.accessibleText) !== detectionTextIdentity(exactText)) {
        problems.push(`${scope}event row ${index + 1} accessible text changed`);
      }
      const inspection = row.inspection;
      const geometry = ["top", "right", "bottom", "left", "width", "height"];
      if (
        !inspection || !geometry.every((edge) => Number.isFinite(inspection[edge])) ||
        !Number.isInteger(inspection.sourceIndex) || inspection.sourceIndex < 0
      ) {
        problems.push(`${scope}event row ${index + 1} geometry is invalid`);
      } else {
        if (inspection.hidden) problems.push(`${scope}event row ${index + 1} is hidden`);
        if (inspection.ariaHidden) problems.push(`${scope}event row ${index + 1} is aria-hidden`);
        if (inspection.display === "none") {
          problems.push(`${scope}event row ${index + 1} display is none`);
        }
        if (["hidden", "collapse"].includes(inspection.visibility)) {
          problems.push(`${scope}event row ${index + 1} visibility is hidden`);
        }
        if (!inspection.rendered || inspection.width <= 0 || inspection.height <= 0) {
          problems.push(`${scope}event row ${index + 1} has no rendered area`);
        }
        if (!Number.isFinite(inspection.opacity) || inspection.opacity <= 0) {
          problems.push(`${scope}event row ${index + 1} opacity is zero`);
        }
        if (inspection.selfOverflowX > 1 || inspection.selfOverflowY > 1) {
          problems.push(`${scope}event row ${index + 1} clips its own content`);
        }
        if (inspection.ancestorClipped) {
          problems.push(`${scope}event row ${index + 1} is clipped by an ancestor`);
        }
        if (inspection.cssClip) problems.push(`${scope}event row ${index + 1} uses CSS clip`);
        if (inspection.cssClipPath) {
          problems.push(`${scope}event row ${index + 1} uses CSS clip-path`);
        }
        if (inspection.inert || !inspection.accessible) {
          problems.push(`${scope}event row ${index + 1} is excluded from accessibility`);
        }
        if (
          Number.isFinite(viewport?.width) &&
          (inspection.right <= 0 || inspection.left >= viewport.width)
        ) {
          problems.push(`${scope}event row ${index + 1} is horizontally off-canvas`);
        }
        if (inspection.cssOrder !== 0) {
          problems.push(`${scope}event row ${index + 1} uses CSS order`);
        }
      }
    }
  }

  const requiredBoundaryClaims = [
    "「測不到」不等於「等於零」",
    "每個事件的實際通過數都低於各自純靠機率的預期",
    "噪音底線高於訊號",
    "這批資料與這個方法，無法分辨這種大小的效應",
    "不是「這些事件沒有影響」",
    "非偵測不是「事件沒有發生」或「介入無效」的證明",
    "沒有驗證機組的逐時操作或燃料狀態",
  ];
  for (const claim of requiredBoundaryClaims) {
    if (!state?.boundaryText?.includes(claim)) {
      problems.push(`${scope}boundary is missing required claim ${JSON.stringify(claim)}`);
    }
  }
  const boundaryLocalClaims = [
    "每個事件的實際通過數都低於各自純靠機率的預期。",
    "非偵測不是「事件沒有發生」或「介入無效」的證明。",
  ];
  const occurrenceCount = (text, claim) => String(text ?? "").split(claim).length - 1;
  for (const claim of boundaryLocalClaims) {
    if (
      occurrenceCount(state?.boundaryText, claim) !== 1 ||
      occurrenceCount(state?.pageText, claim) !== 1
    ) {
      problems.push(`${scope}boundary-local inference locality changed ${JSON.stringify(claim)}`);
    }
  }
  if (String(state?.pageText ?? "").includes("三個事件的實際通過數都低於機率預期。")) {
    problems.push(`${scope}boundary-local inference is duplicated outside the boundary`);
  }
  if (state?.regions?.boundary?.collapsed || state?.regions?.boundary?.tagName === "DETAILS") {
    problems.push(`${scope}boundary became a collapsed disclosure`);
  }

  if (
    viewport?.width === 375 && viewport?.height === 812 &&
    (!Number.isFinite(landmarks.primaryPlot?.top) || landmarks.primaryPlot.top >= viewport.height)
  ) {
    problems.push(`${scope}primary plot does not enter the first viewport`);
  }
  if (
    !Number.isFinite(state?.document?.clientWidth) ||
    !Number.isFinite(state?.document?.scrollWidth) ||
    state.document.scrollWidth > state.document.clientWidth
  ) {
    problems.push(`${scope}document scrolls sideways`);
  }
  return problems;
}

const HEALTH_STORY_PAYLOAD = JSON.parse(
  readFileSync(join(process.cwd(), "web", "public", "data", "story", "health.json"), "utf8"),
);
const HEALTH_PAYLOAD_KEYS = [
  // The pooled coefficient's own confidence interval, at the reference
  // counterfactual. M10 always fitted it; only the central bound used to be
  // exported, so every percentage in the chapter was a point estimate.
  "coefficient_band",
  "extrapolation",
  "formula",
  "functions",
  "headline",
  "mean_median",
  "not_reported",
  "panel",
  "series",
  "spread_share",
  "years",
];
const HEALTH_FUNCTION_KEYS = [
  "caveat",
  "name",
  "outcome",
  "rr_per_10",
  "rr_per_10_high",
  "rr_per_10_low",
  "source",
  "source_url",
];
const HEALTH_SERIES_KEYS = [
  "label", "name", "paf",
  // The pooled coefficient at its low and high bound, held at this
  // counterfactual — beside `paf` so the three cannot be paired wrongly.
  "paf_high", "paf_low",
  "value", "why", "years",
];
const HEALTH_HEADLINE_KEYS = [
  "first_range",
  "first_share",
  "first_year",
  "last_range",
  "last_share",
  "last_year",
];
const HEALTH_ASSUMPTION_ROWS = [
  [
    "counterfactual",
    "比較基準",
    "圖 7.1 與圖 7.2 量化四種反事實濃度造成的差異。",
  ],
  [
    "response",
    "暴露反應函數",
    "本章只採用一條具可追溯來源的函數；適用範圍與外推界線在後文公開。",
  ],
  [
    "population",
    "暴露人口",
    "本專案沒有人口與個人暴露資料，因此不報死亡人數，也不把測站中位數稱為誰的暴露。",
  ],
];
const HEALTH_READING_ROWS = [
  ["robust", "下降幅度對比較基準穩健"],
  ["sensitive", "當前水準對比較基準敏感"],
];
const HEALTH_INFERENCE_ROWS = [
  ["deaths", "不報死亡人數"],
  ["exposure", "不宣稱這是誰的暴露"],
];
const HEALTH_FIGURE_2_TITLE = "比較基準造成的落差佔估計值多少？";

function healthTextIdentity(value) {
  return String(value ?? "").replace(/\s+/gu, "");
}

function healthExactKeys(value, expected) {
  return value && typeof value === "object" && !Array.isArray(value) &&
    JSON.stringify(Object.keys(value).sort()) === JSON.stringify(expected);
}

function healthNumber(value) {
  return Number(value.toFixed(1)).toString();
}

function healthExpectedEvidenceFromPayload(payload) {
  if (!healthExactKeys(payload, HEALTH_PAYLOAD_KEYS)) {
    throw new Error("health payload top-level shape changed");
  }
  if (!Array.isArray(payload.functions) || payload.functions.length !== 1) {
    throw new Error("health payload response-function inventory changed");
  }
  const response = payload.functions[0];
  if (!healthExactKeys(response, HEALTH_FUNCTION_KEYS)) {
    throw new Error("health payload response-function shape changed");
  }
  for (const key of ["name", "outcome", "source", "source_url", "caveat"]) {
    if (typeof response[key] !== "string" || !response[key].trim()) {
      throw new Error(`health payload response-function ${key} changed`);
    }
  }
  for (const key of ["rr_per_10", "rr_per_10_low", "rr_per_10_high"]) {
    if (typeof response[key] !== "number" || !Number.isFinite(response[key])) {
      throw new Error(`health payload response-function ${key} is invalid`);
    }
  }
  if (
    !Array.isArray(payload.years) || !payload.years.length ||
    payload.years.some((year) => !Number.isInteger(year)) ||
    JSON.stringify(payload.years) !==
      JSON.stringify([...new Set(payload.years)].sort((left, right) => left - right))
  ) {
    throw new Error("health payload year inventory changed");
  }
  if (
    !Array.isArray(payload.spread_share) ||
    payload.spread_share.length !== payload.years.length
  ) {
    throw new Error("health payload years/spread inventory changed");
  }
  if (payload.spread_share.some((value) => typeof value !== "number" || !Number.isFinite(value))) {
    throw new Error("health payload spread value is invalid");
  }
  if (!Array.isArray(payload.series) || payload.series.length !== 4) {
    throw new Error("health payload counterfactual-series inventory changed");
  }
  const identities = new Set();
  const seriesByName = new Map();
  for (const [index, row] of payload.series.entries()) {
    if (!healthExactKeys(row, HEALTH_SERIES_KEYS)) {
      throw new Error(`health payload counterfactual series ${index + 1} shape changed`);
    }
    for (const key of ["name", "label", "why"]) {
      if (typeof row[key] !== "string" || !row[key].trim()) {
        throw new Error(`health payload counterfactual series ${index + 1} ${key} changed`);
      }
    }
    if (identities.has(row.name)) {
      throw new Error("health payload counterfactual series identity is duplicated");
    }
    identities.add(row.name);
    seriesByName.set(row.name, row);
    if (typeof row.value !== "number" || !Number.isFinite(row.value)) {
      throw new Error(`health payload counterfactual series ${index + 1} value is invalid`);
    }
    if (JSON.stringify(row.years) !== JSON.stringify(payload.years)) {
      throw new Error(`health payload counterfactual series ${index + 1} years changed`);
    }
    if (
      !Array.isArray(row.paf) || row.paf.length !== payload.years.length ||
      row.paf.some((value) => typeof value !== "number" || !Number.isFinite(value))
    ) {
      throw new Error(`health payload counterfactual series ${index + 1} values changed`);
    }
  }
  const headline = payload.headline;
  if (!healthExactKeys(headline, HEALTH_HEADLINE_KEYS)) {
    throw new Error("health payload headline shape changed");
  }
  for (const key of ["first_year", "last_year"]) {
    if (!Number.isInteger(headline[key]) || !payload.years.includes(headline[key])) {
      throw new Error(`health payload headline ${key} is invalid`);
    }
  }
  for (const key of ["first_share", "last_share"]) {
    if (typeof headline[key] !== "number" || !Number.isFinite(headline[key])) {
      throw new Error(`health payload headline ${key} is invalid`);
    }
  }
  for (const key of ["first_range", "last_range"]) {
    if (
      !Array.isArray(headline[key]) || headline[key].length !== 2 ||
      headline[key].some((value) => typeof value !== "number" || !Number.isFinite(value))
    ) {
      throw new Error(`health payload headline ${key} is invalid`);
    }
  }
  /*
   * The two concentrations the headline range spans, resolved from the range.
   *
   * This read gbd_low and gbd_high by name — 2.4 and 5.9 — and asserted the
   * chapter named them, beside percentages taken from `last_range`.
   * `analysis/health.py` builds that range as min/max across EVERY
   * counterfactual, so its upper end is the zero-exposure assumption; 2.4 gives
   * 7.7% where the sentence stood beside 9.4%. The same mistaken pairing was in
   * `check_publication_structure.py`, which is why neither gate could see it:
   * two independent checks agreed with each other and with the error, and a
   * correct repair would have been reported by both as a regression.
   */
  const lastIndex = payload.years.indexOf(headline.last_year);
  const rangeEnds = headline.last_range.map((target) => {
    const matched = payload.series.filter((row) => Math.abs(row.paf[lastIndex] - target) < 5e-5);
    if (matched.length !== 1) {
      throw new Error("health payload headline range does not resolve to one counterfactual");
    }
    return matched[0];
  });
  const robustBody =
    `${headline.first_year} 年是 ${healthNumber(headline.first_range[0] * 100)}–` +
    `${healthNumber(headline.first_range[1] * 100)}%，${headline.last_year} 年是 ` +
    `${healthNumber(headline.last_range[0] * 100)}–` +
    `${healthNumber(headline.last_range[1] * 100)}%。` +
    "無論選哪個基準，都下降了大約一半到三分之二。" +
    "這一點跟第五章的政策效應不一樣—那裡的訊號被方法的噪音蓋過去，這裡沒有。";
  const sensitiveBody =
    `${headline.last_year} 年的答案是 ${healthNumber(headline.last_range[0] * 100)}% 還是 ` +
    `${healthNumber(headline.last_range[1] * 100)}%，差了將近一倍，而唯一的差別是把 ` +
    `${healthNumber(rangeEnds[0].value)} 還是 ${healthNumber(rangeEnds[1].value)} μg/m³ ` +
    `當作比較基準—這是上圖 ${payload.series.length} 條假設線的兩個極端，落差來自方法選擇，不是來自資料。`;
  if (!healthExactKeys(payload.not_reported, ["deaths", "exposure"])) {
    throw new Error("health payload no-inference boundary changed");
  }
  if (
    typeof payload.not_reported.deaths !== "string" || !payload.not_reported.deaths.trim() ||
    typeof payload.not_reported.exposure !== "string" || !payload.not_reported.exposure.trim()
  ) {
    throw new Error("health payload no-inference boundary changed");
  }
  return Object.freeze({
    seriesCount: payload.series.length,
    functionCount: payload.functions.length,
    yearsCount: payload.years.length,
    spreadCount: payload.spread_share.length,
    deaths: payload.not_reported.deaths,
    exposure: payload.not_reported.exposure,
    readingBodies: Object.freeze([robustBody, sensitiveBody]),
  });
}

const EXPECTED_HEALTH_EVIDENCE = healthExpectedEvidenceFromPayload(HEALTH_STORY_PAYLOAD);

const FORECAST_STORY_PAYLOAD = JSON.parse(
  readFileSync(join(process.cwd(), "web", "public", "data", "story", "forecast.json"), "utf8"),
);
const FORECAST_PAYLOAD_KEYS = [
  "baselines",
  "horizons",
  "leakage_note",
  "period",
  "reading",
  "skill_formula",
  "target",
  "validation",
];
const FORECAST_BASELINE_KEYS = ["label", "name", "what", "why"];
const FORECAST_READING_KEYS = ["claim", "detail"];
const FORECAST_HORIZON_KEYS = [
  // Sorted, because `forecastExactKeys` compares sorted key lists.
  "band_coverage",
  "band_coverage_worst",
  "band_half_width",
  "band_model_rmse",
  "band_nominal",
  "band_splits_below_nominal",
  "climatology_rmse",
  "horizon",
  "model_r2",
  "model_rmse",
  "n",
  "per_split",
  "persistence_rmse",
  "skill_climatology",
  "skill_climatology_worst",
  "skill_persistence",
  "skill_persistence_worst",
  "splits",
  "splits_not_beating_persistence",
  "stations",
];
const FORECAST_SPLIT_KEYS = [
  "band_coverage",
  "band_half_width",
  "model_r2",
  "skill_climatology",
  "skill_persistence",
  "split",
];
const FORECAST_HORIZONS = [1, 6, 24, 48];
const FORECAST_READING_KEYS_ORDERED = [
  "r2-skill",
  "two-baselines",
  "split-instability",
  "shared-feature-bug",
];
const FORECAST_DECISION_ROWS = [
  [
    "error",
    "誤差",
    "先看圖 6.1：模型、persistence 與 climatology 的 RMSE 隨期距如何變化。",
    "#evidence-6-1-title",
  ],
  [
    "skill",
    "基準優勢",
    "再看圖 6.2：同一批預測相對 persistence 與 climatology 還剩多少優勢。",
    "#evidence-6-2-title",
  ],
  [
    "cost",
    "計算代價",
    "最後看成本表與圖 6.3：額外計算是否換得可用的準確度。",
    "#forecast-cost",
  ],
];
const METHOD_CASE_ROWS = [
  ["01", "月平均抹掉了六成的變異", "#method-case-01"],
  ["02", "拿 PM10 預測 PM2.5", "#method-case-02"],
  ["03", "把風向當成 0 到 360 的普通數字", "#method-case-03"],
  ["04", "把及格標準調低，好讓資料通過檢定", "#method-case-04"],
  ["05", "NO + NO₂ = NOx，三個一起放進模型", "#method-case-05"],
  ["06", "只用模型學過的資料評斷它", "#method-case-06"],
  ["07", "用一句話處理掉所有缺漏值", "#method-case-07"],
];
function dataMegabytes(value) {
  return `${(value / 1e6).toFixed(value >= 1e7 ? 1 : 2)} MB`;
}

function loadDataProvenanceContract() {
  const root = join(process.cwd(), "web", "public", "data");
  const publication = JSON.parse(readFileSync(
    join(process.cwd(), "web", "src", "data", "pages-publication.json"),
    "utf8",
  ));
  const index = JSON.parse(readFileSync(join(root, "l0", "index.json"), "utf8"));
  const manifest = JSON.parse(readFileSync(join(root, "manifest.json"), "utf8"));
  const meta = JSON.parse(readFileSync(join(root, "meta.json"), "utf8"));
  if (
    !Array.isArray(index?.pollutants) || !Array.isArray(manifest?.files) ||
    !["metadata", "l0", "l1", "l2"].every((key) => Array.isArray(publication?.[key]))
  ) {
    throw new Error("data provenance source inventory is invalid");
  }
  if (!Number.isInteger(meta?.hourly_observations) || meta.hourly_observations <= 0) {
    throw new Error("data provenance hourly observation count is invalid");
  }
  const manifestBytes = new Map();
  for (const row of manifest.files) {
    if (
      !row || typeof row !== "object" || Array.isArray(row) ||
      typeof row.file !== "string" || !row.file ||
      !Number.isInteger(row.bytes) || row.bytes < 0 || manifestBytes.has(row.file)
    ) {
      throw new Error("data manifest file identity is invalid");
    }
    manifestBytes.set(row.file, row.bytes);
  }
  const published = new Set([
    ...publication.metadata,
    ...publication.l0,
    ...publication.l1,
    ...publication.l2,
  ]);
  let l1Total = 0;
  const publishedL1Codes = [];
  const downloads = index.pollutants.map((row) => {
    if (
      !row || typeof row !== "object" || Array.isArray(row) ||
      typeof row.pollutant !== "string" || !row.pollutant ||
      typeof row.name_zh !== "string" || !row.name_zh ||
      typeof row.file !== "string" || !row.file.startsWith("l0/") ||
      !row.file.endsWith(".json") || !Array.isArray(row.months) ||
      row.months.length !== 2 || row.months.some((month) => typeof month !== "string" || !month) ||
      !Number.isInteger(row.bytes) || row.bytes < 0 || manifestBytes.get(row.file) !== row.bytes ||
      !published.has(row.file)
    ) {
      throw new Error("data index pollutant identity is invalid");
    }
    const stem = row.file.slice(3, -5);
    const l1File = `l1/${stem}.parquet`;
    const l1Bytes = manifestBytes.get(l1File);
    const l1Selected = published.has(l1File);
    if (l1Selected && (!Number.isInteger(l1Bytes) || l1Bytes < 0)) {
      throw new Error(`data manifest is missing ${l1File}`);
    }
    if (l1Selected) {
      l1Total += l1Bytes;
      publishedL1Codes.push(row.pollutant);
    }
    return Object.freeze({
      name: `${row.name_zh} ${row.pollutant}`,
      period: `${row.months[0]}–${row.months[1]}`,
      l0Href: `/data/${row.file}`,
      l0Size: dataMegabytes(row.bytes),
      l1Href: l1Selected ? `/data/${l1File}` : null,
      l1Size: l1Selected ? dataMegabytes(l1Bytes) : "",
      l1Label: l1Selected ? `Parquet ${dataMegabytes(l1Bytes)}` : "Pages 未發布",
    });
  });
  const layers = [
    ["L0", "L0 站-月", "閱讀者 · 快速查值與網站圖表", "每個測項一個 JSON，含月均值與該月的有效天數。網站直接讀這一層。"],
    ["L1", "L1 站-日", "分析者 · 逐日查詢與桌面分析", `Pages 目前發布 ${publishedL1Codes.join("、")} 的 Parquet，共 ${dataMegabytes(l1Total)}；其餘測項可由本機管線產生。`],
    ["L2", "L2 站-時", "重現者 · 逐時稽核與管線重建", `${(meta.hourly_observations / 1e8).toFixed(2)} 億筆完整逐時觀測，含每一筆的品管旗標。不發布— 只發衍生產物與完整管線，跑一次匯入與建置即可獨立重建。`],
  ].map((row) => Object.freeze(row));
  return Object.freeze({ layers: Object.freeze(layers), downloads: Object.freeze(downloads) });
}

const DATA_PROVENANCE_CONTRACT = loadDataProvenanceContract();
const DATA_LAYER_ROWS = DATA_PROVENANCE_CONTRACT.layers;
const DATA_DOWNLOAD_ROWS = DATA_PROVENANCE_CONTRACT.downloads;

function forecastExactKeys(value, expected) {
  return value && typeof value === "object" && !Array.isArray(value) &&
    JSON.stringify(Object.keys(value).sort()) === JSON.stringify(expected);
}

function forecastFiniteNumber(value) {
  return typeof value === "number" && Number.isFinite(value);
}

function forecastExpectedEvidenceFromPayload(payload) {
  if (!forecastExactKeys(payload, FORECAST_PAYLOAD_KEYS)) {
    throw new Error("forecast payload top-level shape changed");
  }
  if (
    !Array.isArray(payload.period) || payload.period.length !== 2 ||
    payload.period.some((year) => !Number.isInteger(year)) ||
    JSON.stringify(payload.period) !== JSON.stringify([...payload.period].sort((a, b) => a - b))
  ) {
    throw new Error("forecast payload period changed");
  }
  for (const key of ["target", "validation", "skill_formula", "leakage_note"]) {
    if (typeof payload[key] !== "string" || !payload[key].trim()) {
      throw new Error(`forecast payload ${key} changed`);
    }
  }
  if (!Array.isArray(payload.baselines) || payload.baselines.length !== 2) {
    throw new Error("forecast payload baseline inventory changed");
  }
  const baselineNames = ["persistence", "climatology"];
  const baselines = payload.baselines.map((row, index) => {
    if (!forecastExactKeys(row, FORECAST_BASELINE_KEYS)) {
      throw new Error(`forecast payload baseline ${index + 1} shape changed`);
    }
    for (const key of ["name", "label", "what", "why"]) {
      if (typeof row[key] !== "string" || !row[key].trim()) {
        throw new Error(`forecast payload baseline ${index + 1} text changed`);
      }
    }
    if (row.name !== baselineNames[index]) {
      throw new Error("forecast payload baseline identity or order changed");
    }
    return Object.freeze([row.name, row.label, row.what, row.why]);
  });
  if (!Array.isArray(payload.reading) || payload.reading.length !== 4) {
    throw new Error("forecast payload reading inventory changed");
  }
  const readings = payload.reading.map((row, index) => {
    if (!forecastExactKeys(row, FORECAST_READING_KEYS)) {
      throw new Error(`forecast payload reading ${index + 1} shape changed`);
    }
    if (
      typeof row.claim !== "string" || !row.claim.trim() ||
      typeof row.detail !== "string" || !row.detail.trim()
    ) {
      throw new Error(`forecast payload reading ${index + 1} text changed`);
    }
    return Object.freeze([row.claim, row.detail]);
  });
  if (!Array.isArray(payload.horizons) || payload.horizons.length !== FORECAST_HORIZONS.length) {
    throw new Error("forecast payload horizon inventory changed");
  }
  const observedHorizons = [];
  for (const [index, row] of payload.horizons.entries()) {
    if (!forecastExactKeys(row, FORECAST_HORIZON_KEYS)) {
      throw new Error(`forecast payload horizon ${index + 1} shape changed`);
    }
    for (const key of ["horizon", "n", "stations", "splits", "splits_not_beating_persistence"]) {
      if (!Number.isInteger(row[key]) || row[key] < 0) {
        throw new Error(`forecast payload horizon ${index + 1} ${key} is invalid`);
      }
    }
    for (const key of [
      "model_r2",
      "skill_persistence",
      "skill_persistence_worst",
      "skill_climatology",
      "skill_climatology_worst",
      "model_rmse",
      "persistence_rmse",
      "climatology_rmse",
    ]) {
      if (!forecastFiniteNumber(row[key])) {
        throw new Error(`forecast payload horizon ${index + 1} metric is invalid`);
      }
    }
    if (!Array.isArray(row.per_split) || row.per_split.length !== row.splits) {
      throw new Error(`forecast payload horizon ${index + 1} split inventory changed`);
    }
    const splitNames = new Set();
    for (const [splitIndex, split] of row.per_split.entries()) {
      if (!forecastExactKeys(split, FORECAST_SPLIT_KEYS)) {
        throw new Error(
          `forecast payload horizon ${index + 1} split ${splitIndex + 1} shape changed`,
        );
      }
      if (typeof split.split !== "string" || !split.split.trim() || splitNames.has(split.split)) {
        throw new Error(`forecast payload horizon ${index + 1} split identity changed`);
      }
      splitNames.add(split.split);
      for (const key of ["skill_persistence", "skill_climatology", "model_r2"]) {
        if (!forecastFiniteNumber(split[key])) {
          throw new Error(`forecast payload horizon ${index + 1} split metric is invalid`);
        }
      }
    }
    observedHorizons.push(row.horizon);
  }
  if (JSON.stringify(observedHorizons) !== JSON.stringify(FORECAST_HORIZONS)) {
    throw new Error("forecast payload horizon identity or order changed");
  }
  return Object.freeze({
    horizons: Object.freeze(observedHorizons),
    readings: Object.freeze(readings),
    baselines: Object.freeze(baselines),
  });
}

const EXPECTED_FORECAST_EVIDENCE = forecastExpectedEvidenceFromPayload(FORECAST_STORY_PAYLOAD);

function healthInspectionProblems(inspection, label, scope, viewport) {
  const problems = [];
  if (!inspection) return [`${scope}${label} inspection is missing`];
  if (inspection.hidden) problems.push(`${scope}${label} is hidden`);
  if (inspection.ariaHidden) problems.push(`${scope}${label} is aria-hidden`);
  if (inspection.display === "none") problems.push(`${scope}${label} display is none`);
  if (["hidden", "collapse"].includes(inspection.visibility)) {
    problems.push(`${scope}${label} visibility is hidden`);
  }
  if (!inspection.rendered) problems.push(`${scope}${label} is not rendered`);
  if (!Number.isFinite(inspection.opacity) || inspection.opacity <= 0) {
    problems.push(`${scope}${label} opacity is zero`);
  }
  if (
    !Number.isFinite(inspection.width) || !Number.isFinite(inspection.height) ||
    inspection.width <= 0 || inspection.height <= 0
  ) {
    problems.push(`${scope}${label} has no rendered area`);
  }
  if (inspection.selfOverflowX > 1 || inspection.selfOverflowY > 1) {
    problems.push(`${scope}${label} clips its own content`);
  }
  if (inspection.ancestorClipped) problems.push(`${scope}${label} is clipped by an ancestor`);
  if (inspection.cssClip) problems.push(`${scope}${label} uses CSS clip`);
  if (inspection.cssClipPath) problems.push(`${scope}${label} uses CSS clip-path`);
  if (inspection.inert || !inspection.accessible) {
    problems.push(`${scope}${label} is excluded from accessibility`);
  }
  if (inspection.detailsAncestor) problems.push(`${scope}${label} is user-collapsible`);
  if (
    Number.isFinite(viewport?.width) &&
    (inspection.right <= 0 || inspection.left >= viewport.width)
  ) {
    problems.push(`${scope}${label} is horizontally off-canvas`);
  }
  if (inspection.cssOrder !== 0) problems.push(`${scope}${label} uses CSS order`);
  return problems;
}

function healthRowsAreVisuallyOrdered(rows) {
  return rows.every((row, index) => {
    if (index === 0) return true;
    const previous = rows[index - 1]?.inspection;
    const current = row?.inspection;
    if (
      !previous || !current ||
      !Number.isFinite(previous.top) || !Number.isFinite(previous.left) ||
      !Number.isFinite(current.top) || !Number.isFinite(current.left)
    ) return false;
    if (Math.abs(current.top - previous.top) <= 1) return current.left > previous.left + 1;
    return current.top > previous.top + 1;
  });
}

function healthAssumptionLedgerProblems(state, expected, viewport) {
  const modeLabels = { normal: "", "no-js": "no-JavaScript ", print: "print ", zoom: "zoom " };
  const mode = state?.mode;
  if (!Object.prototype.hasOwnProperty.call(modeLabels, mode)) {
    return ["health assumption-ledger mode is invalid"];
  }
  const scope = modeLabels[mode];
  const problems = [];
  const regions = [
    ["ledger", "ledger"],
    ["readingBand", "reading band"],
    ["boundaries", "boundary"],
  ];
  for (const [key, label] of regions) {
    if (state?.counts?.[key] !== 1) {
      problems.push(`${scope}${label} count is ${String(state?.counts?.[key])}, expected 1`);
    }
    if (state?.regions?.[key]) {
      problems.push(...healthInspectionProblems(state.regions[key], label, scope, viewport));
    }
  }

  const landmarks = state?.landmarks ?? {};
  const openingKeys = [
    "lede",
    "ledger",
    "primaryTitle",
    "primaryPlot",
    "caption",
    "readingBand",
    "figure2Title",
    "boundaries",
  ];
  const landmarkLabels = {
    lede: "lede",
    ledger: "ledger",
    primaryTitle: "primary title",
    primaryPlot: "primary plot",
    caption: "caption",
    readingBand: "reading band",
    figure2Title: "Figure 7.2 title",
    boundaries: "boundary",
  };
  for (const key of openingKeys) {
    const landmark = landmarks[key];
    const label = landmarkLabels[key];
    if (!landmark) {
      problems.push(`${scope}${label} landmark is missing`);
      continue;
    }
    const geometry = ["top", "right", "bottom", "left", "width", "height"];
    if (
      !geometry.every((edge) => Number.isFinite(landmark[edge])) ||
      !Number.isInteger(landmark.sourceIndex) || landmark.sourceIndex < 0
    ) {
      problems.push(`${scope}${label} landmark geometry is invalid`);
    }
    if (landmark.cssOrder !== 0) problems.push(`${scope}${label} uses CSS order`);
  }
  const openingParts = openingKeys.map((key) => landmarks[key]);
  if (openingParts.every(Boolean)) {
    const sourceOrdered = openingParts.every(
      (part, index) => index === 0 || openingParts[index - 1].sourceIndex < part.sourceIndex,
    );
    const visuallyOrdered = openingParts.every(
      (part, index) => index === 0 || openingParts[index - 1].top < part.top,
    );
    if (!sourceOrdered || !visuallyOrdered) problems.push(`${scope}opening order changed`);
  }

  const rowContracts = [
    ["assumptionRows", "assumption", HEALTH_ASSUMPTION_ROWS],
    ["readingRows", "reading", HEALTH_READING_ROWS],
    ["inferenceRows", "inference", HEALTH_INFERENCE_ROWS],
  ];
  for (const [stateKey, label, contracts] of rowContracts) {
    const rows = state?.[stateKey];
    if (!Array.isArray(rows) || rows.length !== contracts.length) {
      problems.push(`${scope}${label} row inventory changed`);
      continue;
    }
    if (state?.counts?.[stateKey] !== contracts.length) {
      problems.push(`${scope}${label} row hook inventory changed`);
    }
    for (const [index, contract] of contracts.entries()) {
      const row = rows[index];
      if (row?.key !== contract[0]) {
        problems.push(`${scope}${label} row ${index + 1} key changed`);
      }
      if (label === "assumption") {
        const expectedText = contract[1] + contract[2];
        if (healthTextIdentity(row?.visibleText) !== healthTextIdentity(expectedText)) {
          problems.push(`${scope}${label} row ${index + 1} visible text changed`);
        }
        if (healthTextIdentity(row?.accessibleText) !== healthTextIdentity(expectedText)) {
          problems.push(`${scope}${label} row ${index + 1} accessible text changed`);
        }
      } else if (label === "reading") {
        if (row?.heading !== contract[1]) {
          problems.push(`${scope}${label} row ${index + 1} heading changed`);
        }
        if (healthTextIdentity(row?.accessibleHeading) !== healthTextIdentity(contract[1])) {
          problems.push(`${scope}${label} row ${index + 1} accessible heading changed`);
        }
        const body = expected?.readingBodies?.[index];
        if (healthTextIdentity(row?.bodyText) !== healthTextIdentity(body)) {
          problems.push(`${scope}${label} row ${index + 1} body changed`);
        }
        if (healthTextIdentity(row?.accessibleBody) !== healthTextIdentity(body)) {
          problems.push(`${scope}${label} row ${index + 1} accessible body changed`);
        }
      } else {
        const body = index === 0 ? expected?.deaths : expected?.exposure;
        const expectedText = contract[1] + body;
        if (healthTextIdentity(row?.visibleText) !== healthTextIdentity(expectedText)) {
          problems.push(`${scope}${label} row ${index + 1} visible text changed`);
        }
        if (healthTextIdentity(row?.accessibleText) !== healthTextIdentity(expectedText)) {
          problems.push(`${scope}${label} row ${index + 1} accessible text changed`);
        }
      }
      problems.push(...healthInspectionProblems(row?.inspection, `${label} row ${index + 1}`, scope, viewport));
    }
    if (Array.isArray(rows) && rows.length === contracts.length && !healthRowsAreVisuallyOrdered(rows)) {
      problems.push(`${scope}${label} row visual order changed`);
    }
  }

  if (state?.figure2Title !== HEALTH_FIGURE_2_TITLE) {
    problems.push(`${scope}Figure 7.2 title changed`);
  }
  if (expected?.seriesCount !== 4 || expected?.functionCount !== 1) {
    problems.push(`${scope}payload no longer supports the assumption ledger`);
  }
  if (expected?.yearsCount !== expected?.spreadCount || expected?.yearsCount <= 0) {
    problems.push(`${scope}payload no longer supports Figure 7.2`);
  }
  if (
    viewport?.width === 375 && viewport?.height === 812 &&
    (!Number.isFinite(landmarks.primaryTitle?.top) || landmarks.primaryTitle.top >= viewport.height)
  ) {
    problems.push(`${scope}primary evidence does not enter the first viewport`);
  }
  if (viewport?.width === 1280 && viewport?.height === 720) {
    if (
      !Number.isFinite(landmarks.primaryPlot?.top) ||
      landmarks.primaryPlot.top >= viewport.height * 0.55
    ) {
      problems.push(
        `${scope}primary plot starts at or below 55vh ` +
          `(top=${String(landmarks.primaryPlot?.top)}, threshold=${viewport.height * 0.55})`,
      );
    }
    const visiblePlotHeight =
      Math.min(viewport.height, landmarks.primaryPlot?.bottom ?? 0) -
      Math.max(0, landmarks.primaryPlot?.top ?? viewport.height);
    if (!Number.isFinite(visiblePlotHeight) || visiblePlotHeight < 180) {
      problems.push(`${scope}less than 180px of primary plot data is visible`);
    }
  }
  if (
    !Number.isFinite(state?.document?.clientWidth) ||
    !Number.isFinite(state?.document?.scrollWidth) ||
    state.document.scrollWidth > state.document.clientWidth
  ) {
    problems.push(`${scope}document scrolls sideways`);
  }
  return problems;
}

function forecastHorizonDecisionProblems(state, expected, viewport) {
  const modeLabels = { normal: "", "no-js": "no-JavaScript ", print: "print ", zoom: "zoom " };
  if (!Object.prototype.hasOwnProperty.call(modeLabels, state?.mode)) {
    return ["forecast horizon-decision mode is invalid"];
  }
  const scope = modeLabels[state.mode];
  const problems = [];
  const regions = [
    ["decisionSheet", "decision sheet"],
    ["readingBand", "reading band"],
    ["baselineBand", "baseline band"],
  ];
  for (const [key, label] of regions) {
    if (state?.counts?.[key] !== 1) {
      problems.push(`${scope}${label} count is ${String(state?.counts?.[key])}, expected 1`);
    }
    if (state?.regions?.[key]) {
      problems.push(...healthInspectionProblems(state.regions[key], label, scope, viewport));
    }
  }

  const landmarks = state?.landmarks ?? {};
  const landmarkKeys = [
    "figure1Title",
    "primaryPlot",
    "decisionSheet",
    "figure2Title",
    "readingBand",
    "baselineBand",
    "cost",
  ];
  const landmarkLabels = {
    figure1Title: "Figure 6.1 title",
    primaryPlot: "primary plot",
    decisionSheet: "decision sheet",
    figure2Title: "Figure 6.2 title",
    readingBand: "reading band",
    baselineBand: "baseline band",
    cost: "cost heading",
  };
  for (const key of landmarkKeys) {
    const landmark = landmarks[key];
    const label = landmarkLabels[key];
    if (!landmark) {
      problems.push(`${scope}${label} landmark is missing`);
      continue;
    }
    if (
      !["top", "right", "bottom", "left", "width", "height"].every(
        (edge) => Number.isFinite(landmark[edge]),
      ) ||
      !Number.isInteger(landmark.sourceIndex) || landmark.sourceIndex < 0
    ) {
      problems.push(`${scope}${label} landmark geometry is invalid`);
    }
    if (landmark.cssOrder !== 0) problems.push(`${scope}${label} uses CSS order`);
  }
  const orderedLandmarks = landmarkKeys.map((key) => landmarks[key]);
  if (orderedLandmarks.every(Boolean)) {
    const sourceOrdered = orderedLandmarks.every(
      (part, index) => index === 0 || orderedLandmarks[index - 1].sourceIndex < part.sourceIndex,
    );
    const visuallyOrdered = orderedLandmarks.every(
      (part, index) => index === 0 || orderedLandmarks[index - 1].top < part.top,
    );
    if (!sourceOrdered || !visuallyOrdered) problems.push(`${scope}evidence order changed`);
  }

  const decisionRows = state?.decisionRows;
  if (!Array.isArray(decisionRows) || decisionRows.length !== FORECAST_DECISION_ROWS.length) {
    problems.push(`${scope}decision row inventory changed`);
  } else {
    if (state?.counts?.decisionRows !== FORECAST_DECISION_ROWS.length) {
      problems.push(`${scope}decision row hook inventory changed`);
    }
    for (const [index, contract] of FORECAST_DECISION_ROWS.entries()) {
      const row = decisionRows[index];
      const expectedText = contract[1] + contract[2];
      if (row?.key !== contract[0]) problems.push(`${scope}decision row ${index + 1} key changed`);
      if (row?.label !== contract[1]) {
        problems.push(`${scope}decision row ${index + 1} label changed`);
      }
      if (row?.bodyText !== contract[2]) {
        problems.push(`${scope}decision row ${index + 1} body changed`);
      }
      if (row?.href !== contract[3]) {
        problems.push(`${scope}decision row ${index + 1} link changed`);
      }
      if (healthTextIdentity(row?.accessibleText) !== healthTextIdentity(expectedText)) {
        problems.push(`${scope}decision row ${index + 1} accessible text changed`);
      }
      problems.push(
        ...healthInspectionProblems(row?.inspection, `decision row ${index + 1}`, scope, viewport),
      );
    }
    if (!healthRowsAreVisuallyOrdered(decisionRows)) {
      problems.push(`${scope}decision row visual order changed`);
    }
  }

  const readingRows = state?.readingRows;
  if (!Array.isArray(readingRows) || readingRows.length !== expected?.readings?.length) {
    problems.push(`${scope}reading row inventory changed`);
  } else {
    if (state?.counts?.readingRows !== expected.readings.length) {
      problems.push(`${scope}reading row hook inventory changed`);
    }
    for (const [index, contract] of expected.readings.entries()) {
      const row = readingRows[index];
      if (row?.key !== FORECAST_READING_KEYS_ORDERED[index]) {
        problems.push(`${scope}reading row ${index + 1} key changed`);
      }
      if (row?.heading !== contract[0]) {
        problems.push(`${scope}reading row ${index + 1} heading changed`);
      }
      if (row?.bodyText !== contract[1]) {
        problems.push(`${scope}reading row ${index + 1} body changed`);
      }
      if (healthTextIdentity(row?.accessibleHeading) !== healthTextIdentity(contract[0])) {
        problems.push(`${scope}reading row ${index + 1} accessible heading changed`);
      }
      if (healthTextIdentity(row?.accessibleBody) !== healthTextIdentity(contract[1])) {
        problems.push(`${scope}reading row ${index + 1} accessible body changed`);
      }
      problems.push(
        ...healthInspectionProblems(row?.inspection, `reading row ${index + 1}`, scope, viewport),
      );
    }
    if (!healthRowsAreVisuallyOrdered(readingRows)) {
      problems.push(`${scope}reading row visual order changed`);
    }
  }

  const baselineRows = state?.baselineRows;
  if (!Array.isArray(baselineRows) || baselineRows.length !== expected?.baselines?.length) {
    problems.push(`${scope}baseline row inventory changed`);
  } else {
    if (state?.counts?.baselineRows !== expected.baselines.length) {
      problems.push(`${scope}baseline row hook inventory changed`);
    }
    for (const [index, contract] of expected.baselines.entries()) {
      const row = baselineRows[index];
      if (row?.key !== contract[0]) problems.push(`${scope}baseline row ${index + 1} key changed`);
      if (healthTextIdentity(row?.heading) !== healthTextIdentity(contract[0] + contract[1])) {
        problems.push(`${scope}baseline row ${index + 1} heading changed`);
      }
      if (row?.whatText !== contract[2]) {
        problems.push(`${scope}baseline row ${index + 1} what changed`);
      }
      if (row?.whyText !== contract[3]) {
        problems.push(`${scope}baseline row ${index + 1} why changed`);
      }
      if (
        healthTextIdentity(row?.accessibleText) !==
        healthTextIdentity(contract[0] + contract[1] + contract[2] + contract[3])
      ) {
        problems.push(`${scope}baseline row ${index + 1} accessible text changed`);
      }
      problems.push(
        ...healthInspectionProblems(row?.inspection, `baseline row ${index + 1}`, scope, viewport),
      );
    }
    if (!healthRowsAreVisuallyOrdered(baselineRows)) {
      problems.push(`${scope}baseline row visual order changed`);
    }
  }

  const occurrenceCount = (text, value) => String(text ?? "").split(value).length - 1;
  for (const [, , body] of FORECAST_DECISION_ROWS) {
    if (occurrenceCount(state?.pageText, body) !== 1) {
      problems.push(`${scope}decision sentence locality changed ${JSON.stringify(body)}`);
    }
  }
  if (JSON.stringify(expected?.horizons) !== JSON.stringify(FORECAST_HORIZONS)) {
    problems.push(`${scope}payload no longer supports the horizon decision`);
  }
  if (
    viewport?.width === 375 && viewport?.height === 812 &&
    (!Number.isFinite(landmarks.primaryPlot?.top) || landmarks.primaryPlot.top >= viewport.height)
  ) {
    problems.push(`${scope}primary plot does not enter the first viewport`);
  }
  if (
    !Number.isFinite(state?.document?.clientWidth) ||
    !Number.isFinite(state?.document?.scrollWidth) ||
    state.document.scrollWidth > state.document.clientWidth
  ) {
    problems.push(`${scope}document scrolls sideways`);
  }
  return problems;
}

function methodsCaseIndexProblems(state, viewport) {
  const modeLabels = { normal: "", "no-js": "no-JavaScript ", print: "print ", zoom: "zoom " };
  if (!Object.prototype.hasOwnProperty.call(modeLabels, state?.mode)) {
    return ["methods seven-case index mode is invalid"];
  }
  const scope = modeLabels[state.mode];
  const problems = [];
  if (state?.counts?.indexes !== 1) {
    problems.push(`${scope}case index count is ${String(state?.counts?.indexes)}, expected 1`);
  }
  if (state?.counts?.labelTargets !== 1) {
    problems.push(
      `${scope}case index label count is ${String(state?.counts?.labelTargets)}, expected 1`,
    );
  }
  if (state?.index) {
    problems.push(...healthInspectionProblems(state.index, "case index", scope, viewport));
  }
  if (state?.indexHeading !== "七個案例索引") {
    problems.push(`${scope}case index heading changed`);
  }
  if (healthTextIdentity(state?.indexAccessibleName) !== healthTextIdentity("七個案例索引")) {
    problems.push(`${scope}case index accessible name changed`);
  }

  const links = state?.links;
  if (!Array.isArray(links) || links.length !== METHOD_CASE_ROWS.length) {
    problems.push(`${scope}case link inventory changed`);
  } else {
    if (state?.counts?.links !== METHOD_CASE_ROWS.length) {
      problems.push(`${scope}case link hook inventory changed`);
    }
    for (const [index, contract] of METHOD_CASE_ROWS.entries()) {
      const row = links[index];
      const [number, title, href] = contract;
      if (row?.number !== number) problems.push(`${scope}case link ${index + 1} number changed`);
      if (row?.title !== title) problems.push(`${scope}case link ${index + 1} title changed`);
      if (row?.href !== href) problems.push(`${scope}case link ${index + 1} href changed`);
      if (row?.targetId !== href.slice(1)) {
        problems.push(`${scope}case link ${index + 1} target identity changed`);
      }
      if (healthTextIdentity(row?.visibleText) !== healthTextIdentity(number + title)) {
        problems.push(`${scope}case link ${index + 1} visible text changed`);
      }
      if (healthTextIdentity(row?.accessibleText) !== healthTextIdentity(title)) {
        problems.push(`${scope}case link ${index + 1} accessible text changed`);
      }
      problems.push(
        ...healthInspectionProblems(row?.inspection, `case link ${index + 1}`, scope, viewport),
      );
      if (!Number.isFinite(row?.inspection?.height) || row.inspection.height < 44) {
        problems.push(`${scope}case link ${index + 1} target is shorter than 44px`);
      }
    }
    if (!healthRowsAreVisuallyOrdered(links)) {
      problems.push(`${scope}case link visual order changed`);
    }
  }

  const destinations = state?.destinations;
  if (!Array.isArray(destinations) || destinations.length !== METHOD_CASE_ROWS.length) {
    problems.push(`${scope}case destination inventory changed`);
  } else {
    if (state?.counts?.destinations !== METHOD_CASE_ROWS.length) {
      problems.push(`${scope}case destination hook inventory changed`);
    }
    for (const [index, contract] of METHOD_CASE_ROWS.entries()) {
      const row = destinations[index];
      const [number, title, href] = contract;
      if (row?.number !== number) {
        problems.push(`${scope}case destination ${index + 1} number changed`);
      }
      if (row?.id !== href.slice(1)) {
        problems.push(`${scope}case destination ${index + 1} id changed`);
      }
      if (row?.heading !== title) {
        problems.push(`${scope}case destination ${index + 1} heading changed`);
      }
      if (healthTextIdentity(row?.accessibleHeading) !== healthTextIdentity(title)) {
        problems.push(`${scope}case destination ${index + 1} accessible heading changed`);
      }
      problems.push(
        ...healthInspectionProblems(
          row?.inspection,
          `case destination ${index + 1}`,
          scope,
          viewport,
        ),
      );
    }
    if (!healthRowsAreVisuallyOrdered(destinations)) {
      problems.push(`${scope}case destination visual order changed`);
    }
  }

  const landmarks = state?.landmarks ?? {};
  const ordered = [landmarks.lede, landmarks.index, ...(destinations ?? []).map((row) => row.inspection)];
  if (ordered.some((part) => !part)) {
    problems.push(`${scope}casebook source-order landmark is missing`);
  } else {
    const sourceOrdered = ordered.every(
      (part, index) => index === 0 || ordered[index - 1].sourceIndex < part.sourceIndex,
    );
    if (!sourceOrdered) problems.push(`${scope}casebook source order changed`);
  }
  if (
    (viewport?.width === 375 && viewport?.height === 812) ||
    (viewport?.width === 1280 && viewport?.height === 720)
  ) {
    if (!Number.isFinite(landmarks.index?.top) || landmarks.index.top >= viewport.height) {
      problems.push(`${scope}case index does not enter the first viewport`);
    }
  }
  if (
    !Number.isFinite(state?.document?.clientWidth) ||
    !Number.isFinite(state?.document?.scrollWidth) ||
    state.document.scrollWidth > state.document.clientWidth
  ) {
    problems.push(`${scope}document scrolls sideways`);
  }
  return problems;
}

function dataProvenanceRegisterProblems(state, viewport) {
  const modeLabels = { normal: "", "no-js": "no-JavaScript ", print: "print ", zoom: "zoom " };
  if (!Object.prototype.hasOwnProperty.call(modeLabels, state?.mode)) {
    return ["data provenance register mode is invalid"];
  }
  const scope = modeLabels[state.mode];
  const problems = [];
  if (state?.counts?.taskRegisters !== 1) problems.push(`${scope}task register changed`);
  if (state?.counts?.schemaRegisters !== 1) problems.push(`${scope}schema register changed`);
  if (state?.counts?.registers !== 1) {
    problems.push(`${scope}register count is ${String(state?.counts?.registers)}, expected 1`);
  }
  if (state?.register) {
    problems.push(...healthInspectionProblems(state.register, "register", scope, viewport));
  }
  if (state?.counts?.terms !== DATA_LAYER_ROWS.length) {
    problems.push(`${scope}layer term hook inventory changed`);
  }
  if (state?.counts?.uses !== DATA_LAYER_ROWS.length) {
    problems.push(`${scope}layer use hook inventory changed`);
  }
  if (state?.counts?.descriptions !== DATA_LAYER_ROWS.length) {
    problems.push(`${scope}layer description hook inventory changed`);
  }
  if (!Array.isArray(state?.layers) || state.layers.length !== DATA_LAYER_ROWS.length) {
    problems.push(`${scope}layer inventory changed`);
  } else {
    for (const [index, contract] of DATA_LAYER_ROWS.entries()) {
      const row = state.layers[index];
      const [level, term, useText, descriptionText] = contract;
      if (row?.level !== level) problems.push(`${scope}layer ${index + 1} identity changed`);
      if (healthTextIdentity(row?.term) !== healthTextIdentity(term)) {
        problems.push(`${scope}layer ${index + 1} term changed`);
      }
      if (healthTextIdentity(row?.useText) !== healthTextIdentity(useText)) {
        problems.push(`${scope}layer ${index + 1} use changed`);
      }
      if (healthTextIdentity(row?.accessibleUse) !== healthTextIdentity(useText)) {
        problems.push(`${scope}layer ${index + 1} accessible use changed`);
      }
      if (healthTextIdentity(row?.descriptionText) !== healthTextIdentity(descriptionText)) {
        problems.push(`${scope}layer ${index + 1} description changed`);
      }
      problems.push(
        ...healthInspectionProblems(
          row?.termInspection,
          `layer ${index + 1} term`,
          scope,
          viewport,
        ),
        ...healthInspectionProblems(
          row?.useInspection,
          `layer ${index + 1} use`,
          scope,
          viewport,
        ),
        ...healthInspectionProblems(
          row?.descriptionInspection,
          `layer ${index + 1} description`,
          scope,
          viewport,
        ),
      );
    }
    const visualRows = state.layers.map((row) => ({ inspection: row?.termInspection }));
    if (!healthRowsAreVisuallyOrdered(visualRows)) {
      problems.push(`${scope}layer visual order changed`);
    }
    for (const [index, row] of state.layers.entries()) {
      if (
        !Number.isFinite(row?.termInspection?.sourceIndex) ||
        !Number.isFinite(row?.useInspection?.sourceIndex) ||
        !Number.isFinite(row?.descriptionInspection?.sourceIndex) ||
        row.termInspection.sourceIndex >= row.useInspection.sourceIndex ||
        row.useInspection.sourceIndex >= row.descriptionInspection.sourceIndex
      ) {
        problems.push(`${scope}layer ${index + 1} definition pairing changed`);
      }
      if (
        !Number.isFinite(row?.termInspection?.top) ||
        !Number.isFinite(row?.useInspection?.top) ||
        row.useInspection.top <= row.termInspection.top
      ) {
        problems.push(`${scope}layer ${index + 1} term/use visual order changed`);
      }
    }
  }

  if (state?.counts?.tables !== 1) {
    problems.push(`${scope}download table count is ${String(state?.counts?.tables)}, expected 1`);
  }
  if (state?.table) {
    problems.push(...healthInspectionProblems(state.table, "download table", scope, viewport));
  }
  if (state?.counts?.bodyRows !== 21) {
    problems.push(`${scope}download row count is ${String(state?.counts?.bodyRows)}, expected 21`);
  }
  if (state?.counts?.downloads !== 25) problems.push(`${scope}registered download count changed`);
  if (state?.counts?.unavailable !== 19) problems.push(`${scope}unavailable L1 count changed`);
  if (!Array.isArray(state?.downloadRows) || state.downloadRows.length !== DATA_DOWNLOAD_ROWS.length) {
    problems.push(`${scope}download row evidence inventory changed`);
  } else {
    for (const [index, expected] of DATA_DOWNLOAD_ROWS.entries()) {
      const row = state.downloadRows[index];
      const observedIdentity = {
        name: healthTextIdentity(row?.name),
        period: healthTextIdentity(row?.period),
        l0Href: row?.l0Href,
        l0Size: healthTextIdentity(row?.l0Size),
        l1Href: row?.l1Href,
        l1Size: healthTextIdentity(row?.l1Size),
        l1Label: healthTextIdentity(row?.l1Label),
      };
      const expectedIdentity = {
        name: healthTextIdentity(expected.name),
        period: healthTextIdentity(expected.period),
        l0Href: expected.l0Href,
        l0Size: healthTextIdentity(expected.l0Size),
        l1Href: expected.l1Href,
        l1Size: healthTextIdentity(expected.l1Size),
        l1Label: healthTextIdentity(expected.l1Label),
      };
      if (JSON.stringify(observedIdentity) !== JSON.stringify(expectedIdentity)) {
        problems.push(`${scope}download row ${index + 1} changed`);
      }
      problems.push(
        ...healthInspectionProblems(
          row?.rowInspection,
          `download row ${index + 1}`,
          scope,
          {},
        ),
      );
      const actionLabels = [
        `JSON ${expected.l0Size}`,
        expected.l1Href ? `Parquet ${expected.l1Size}` : null,
      ];
      for (const [linkIndex, label] of actionLabels.entries()) {
        problems.push(
          ...healthInspectionProblems(
            row?.downloadInspections?.[linkIndex],
            `download row ${index + 1} link ${linkIndex + 1}`,
            scope,
            viewport?.width <= 610 ? viewport : {},
          ),
        );
        if (
          label !== null &&
          healthTextIdentity(row?.downloadAccessibleTexts?.[linkIndex]) !== healthTextIdentity(label)
        ) {
          problems.push(`${scope}download row ${index + 1} link ${linkIndex + 1} accessible name changed`);
        }
      }
    }
  }
  if (state?.counts?.l2Downloads !== 0) {
    problems.push(`${scope}L2 unexpectedly has ${String(state?.counts?.l2Downloads)} download`);
  }
  const tableWrapper = state?.tableWrapper;
  if (tableWrapper?.inspection) {
    problems.push(
      ...healthInspectionProblems(
        tableWrapper.inspection,
        "download table wrapper",
        scope,
        viewport,
      ),
    );
  }
  const compactTable = viewport?.width <= 610;
  if (
    !tableWrapper ||
    !Number.isFinite(tableWrapper.clientWidth) ||
    !Number.isFinite(tableWrapper.scrollWidth) ||
    tableWrapper.scrollWidth < tableWrapper.clientWidth ||
    (compactTable && (
      tableWrapper.scrollWidth > tableWrapper.clientWidth + 1 ||
      ["auto", "scroll"].includes(tableWrapper.overflowX)
    )) ||
    (!compactTable && tableWrapper.scrollWidth > tableWrapper.clientWidth &&
      !["auto", "scroll"].includes(tableWrapper.overflowX))
  ) {
    problems.push(`${scope}download table local scroller changed`);
  }

  if (state?.counts?.boundaries !== 1) {
    problems.push(`${scope}L2 boundary count is ${String(state?.counts?.boundaries)}, expected 1`);
  }
  if (state?.l2Boundary) {
    problems.push(
      ...healthInspectionProblems(state.l2Boundary, "L2 boundary", scope, viewport),
    );
  }
  if (
    !state?.l2BoundaryText?.includes("L2 不發布，理由不是檔案太大") ||
    !state?.l2BoundaryText?.includes("繞過這個矛盾而不是解決它")
  ) {
    problems.push(`${scope}L2 boundary text changed`);
  }

  const landmarks = state?.landmarks ?? {};
  const ordered = [
    landmarks.lede,
    landmarks.primary,
    landmarks.register,
    landmarks.table,
    landmarks.licensing,
    landmarks.l2Boundary,
  ];
  if (ordered.some((part) => !part)) {
    problems.push(`${scope}provenance source-order landmark is missing`);
  } else if (
    !ordered.every(
      (part, index) => index === 0 || ordered[index - 1].sourceIndex < part.sourceIndex,
    )
  ) {
    problems.push(`${scope}provenance source order changed`);
  }
  if (
    ((viewport?.width === 375 && viewport?.height === 812) ||
      (viewport?.width === 1280 && viewport?.height === 720)) &&
    (!Number.isFinite(landmarks.primary?.top) || landmarks.primary.top >= viewport.height)
  ) {
    problems.push(`${scope}task register does not enter the first viewport`);
  }
  if (
    !Number.isFinite(state?.document?.clientWidth) ||
    !Number.isFinite(state?.document?.scrollWidth) ||
    state.document.scrollWidth > state.document.clientWidth
  ) {
    problems.push(`${scope}document scrolls sideways`);
  }
  return problems;
}

const EXPLORER_GUIDED_STEPS = Object.freeze([
  Object.freeze({
    key: "choose",
    number: "01",
    title: "選一個問題",
    text: "六個問題，每一個都問得出一張可以下載的表。",
  }),
  Object.freeze({
    key: "execute",
    number: "02",
    title: "在瀏覽器內執行",
    text: "按下按鈕後才載入查詢引擎與可用資料。",
  }),
  Object.freeze({
    key: "read",
    number: "03",
    title: "讀結果與限制",
    text: "把表格、空結果或錯誤，和下方限制一起讀。",
  }),
]);
const EXPLORER_STATE_KEYS = Object.freeze([
  "caveat", "counts", "document", "mode", "noJs", "result", "run", "state",
  "status", "steps", "tables",
]);
const EXPLORER_COUNT_KEYS = Object.freeze([
  "caveats", "controls", "paths", "results", "steps", "tables", "workspace",
]);
const EXPLORER_INSPECTION_KEYS = Object.freeze([
  "accessible", "ancestorClipped", "ariaHidden", "bottom", "cssClip", "cssClipPath",
  "cssOrder", "detailsAncestor", "display", "height", "hidden", "inert", "left",
  "opacity", "rendered", "right", "selfOverflowX", "selfOverflowY", "sourceIndex", "top",
  "visibility", "width",
]);

function explorerExactKeys(value, expected) {
  return value && typeof value === "object" && !Array.isArray(value) &&
    JSON.stringify(Object.keys(value).sort()) === JSON.stringify(expected);
}

function explorerInspectionSchemaProblems(inspection, label) {
  const problems = [];
  if (!explorerExactKeys(inspection, EXPLORER_INSPECTION_KEYS)) {
    return [`explore ${label} inspection shape changed`];
  }
  for (const key of [
    "accessible", "ancestorClipped", "ariaHidden", "cssClip", "cssClipPath",
    "detailsAncestor", "hidden", "inert", "rendered",
  ]) {
    if (typeof inspection[key] !== "boolean") {
      problems.push(`explore ${label} inspection ${key} is not boolean`);
    }
  }
  for (const key of [
    "bottom", "cssOrder", "height", "left", "opacity", "right", "selfOverflowX",
    "selfOverflowY", "sourceIndex", "top", "width",
  ]) {
    if (typeof inspection[key] !== "number" || !Number.isFinite(inspection[key])) {
      problems.push(`explore ${label} inspection ${key} is not finite`);
    }
  }
  for (const key of ["display", "visibility"]) {
    if (typeof inspection[key] !== "string") {
      problems.push(`explore ${label} inspection ${key} is not a string`);
    }
  }
  return problems;
}

function explorerVisibleInspectionProblems(inspection, label, scope, viewport, allowDetails = false) {
  const problems = explorerInspectionSchemaProblems(inspection, label);
  if (problems.length) return problems;
  return healthInspectionProblems(inspection, label, scope, viewport).filter(
    (problem) => !allowDetails || !problem.endsWith(" is user-collapsible"),
  );
}

function explorerHiddenInspectionProblems(inspection, label, scope) {
  const problems = explorerInspectionSchemaProblems(inspection, label);
  if (problems.length) return problems;
  if (inspection.rendered) problems.push(`${scope}${label} is visibly rendered`);
  if (inspection.accessible) problems.push(`${scope}${label} remains in accessibility`);
  return problems;
}

function explorerGuidedWorkspaceProblems(state, viewport) {
  if (!explorerExactKeys(state, EXPLORER_STATE_KEYS)) {
    return ["explore state shape changed"];
  }
  const modeLabels = { normal: "", "no-js": "no-JavaScript ", print: "print ", zoom: "zoom " };
  if (!Object.prototype.hasOwnProperty.call(modeLabels, state.mode)) {
    return ["explore mode is invalid"];
  }
  const scope = modeLabels[state.mode];
  const problems = [];
  const validStates = state.mode === "no-js"
    ? ["no-js"]
    : state.mode === "normal"
      ? ["initial", "loading", "success", "empty", "failure"]
      : ["initial"];
  if (!validStates.includes(state.state)) problems.push(`${scope}explore state is invalid`);

  if (!explorerExactKeys(state.counts, EXPLORER_COUNT_KEYS)) {
    problems.push(`${scope}explore count shape changed`);
  } else {
    const expectedCounts = {
      caveats: 1,
      controls: 1,
      paths: 1,
      results: 1,
      steps: EXPLORER_GUIDED_STEPS.length,
      tables: 1,
      workspace: 1,
    };
    for (const [key, expected] of Object.entries(expectedCounts)) {
      if (!Number.isInteger(state.counts[key]) || state.counts[key] !== expected) {
        problems.push(`${scope}explore ${key} count is ${String(state.counts[key])}, expected ${expected}`);
      }
    }
  }

  if (!Array.isArray(state.steps) || state.steps.length !== EXPLORER_GUIDED_STEPS.length) {
    problems.push(`${scope}explore step inventory changed`);
  } else {
    for (const [index, expected] of EXPLORER_GUIDED_STEPS.entries()) {
      const step = state.steps[index];
      if (!explorerExactKeys(step, ["accessibleText", "inspection", "key", "text", "title"])) {
        problems.push(`${scope}explore step ${index + 1} shape changed`);
        continue;
      }
      if (step.key !== expected.key) problems.push(`${scope}explore step ${index + 1} key changed`);
      if (healthTextIdentity(step.title) !== healthTextIdentity(expected.title)) {
        problems.push(`${scope}explore step ${index + 1} title changed`);
      }
      if (healthTextIdentity(step.text) !== healthTextIdentity(expected.text)) {
        problems.push(`${scope}explore step ${index + 1} text changed`);
      }
      const expectedAccessible = `${expected.number}${expected.title}${expected.text}`;
      if (healthTextIdentity(step.accessibleText) !== healthTextIdentity(expectedAccessible)) {
        problems.push(`${scope}explore step ${index + 1} accessible text changed`);
      }
      problems.push(
        ...explorerVisibleInspectionProblems(
          step.inspection,
          `step ${index + 1}`,
          scope,
          viewport,
        ),
      );
    }
    if (!healthRowsAreVisuallyOrdered(state.steps)) {
      problems.push(`${scope}explore step visual order changed`);
    }
  }

  const objectContracts = [
    ["run", state.run, ["accessibleText", "disabled", "inspection"]],
    ["status", state.status, ["busy", "failed", "inspection", "text"]],
    ["tables", state.tables, ["inspection", "text"]],
    ["result", state.result, ["emptyMessage", "errorDetail", "focused", "hasRows", "inspection", "text"]],
    ["caveat", state.caveat, ["inspection", "text"]],
    ["no-JavaScript notice", state.noJs, ["inspection", "text"]],
    ["document", state.document, ["clientWidth", "scrollWidth"]],
  ];
  for (const [label, value, keys] of objectContracts) {
    if (!explorerExactKeys(value, keys)) problems.push(`${scope}explore ${label} shape changed`);
  }
  if (problems.some((problem) => problem.includes(" shape changed"))) return problems;

  for (const [label, value] of [
    ["run disabled", state.run.disabled],
    ["status busy", state.status.busy],
    ["status failed", state.status.failed],
    ["result hasRows", state.result.hasRows],
    ["result emptyMessage", state.result.emptyMessage],
    ["result focused", state.result.focused],
  ]) {
    if (typeof value !== "boolean") problems.push(`${scope}explore ${label} is not boolean`);
  }
  for (const [label, value] of [
    ["status text", state.status.text],
    ["tables text", state.tables.text],
    ["result text", state.result.text],
    ["caveat text", state.caveat.text],
    ["no-JavaScript text", state.noJs.text],
  ]) {
    if (typeof value !== "string") problems.push(`${scope}explore ${label} is not a string`);
  }
  if (state.run.accessibleText !== null && typeof state.run.accessibleText !== "string") {
    problems.push(`${scope}explore run accessibleText is invalid`);
  }
  if (state.result.errorDetail !== null && typeof state.result.errorDetail !== "string") {
    problems.push(`${scope}explore result errorDetail is invalid`);
  }
  if (
    typeof state.document.clientWidth !== "number" || !Number.isFinite(state.document.clientWidth) ||
    typeof state.document.scrollWidth !== "number" || !Number.isFinite(state.document.scrollWidth)
  ) {
    problems.push(`${scope}explore document dimensions are invalid`);
  } else if (state.document.scrollWidth > state.document.clientWidth) {
    problems.push(`${scope}explore document scrolls sideways`);
  }

  /*
   * There is no SQL disclosure to check any more.
   *
   * The query box, its summary and the share-a-query link were removed on the
   * owner's call that a reader has no need to meet SQL. What used to be checked
   * here — that the disclosure was present, visible and still said 「檢視或修改
   * 查詢語句」 — is now checked by its absence: `STATIC_SQL_DISCLOSURES` is zero
   * on every route, so a box that came back would fail the inventory.
   */
  problems.push(
    ...explorerVisibleInspectionProblems(state.caveat.inspection, "caveat", scope, viewport),
  );
  if (
    !healthTextIdentity(state.caveat.text).includes("Pages目前公開PM10、PM2.5兩張L1表") ||
    !healthTextIdentity(state.caveat.text).includes("不是目前GitHubPages的發布承諾") ||
    !healthTextIdentity(state.caveat.text).includes("逐時原始資料另有授權問題待確認")
  ) {
    problems.push(`${scope}explore caveat text changed`);
  }

  const interactive = state.mode === "normal" || state.mode === "zoom";
  if (interactive) {
    problems.push(
      ...explorerVisibleInspectionProblems(state.run.inspection, "run control", scope, viewport),
      ...explorerVisibleInspectionProblems(state.status.inspection, "status", scope, viewport),
      ...explorerVisibleInspectionProblems(state.tables.inspection, "table inventory", scope, viewport),
      ...explorerHiddenInspectionProblems(state.noJs.inspection, "no-JavaScript notice", scope),
    );
    if (healthTextIdentity(state.run.accessibleText) !== "執行查詢") {
      problems.push(`${scope}explore run accessible text changed`);
    }
    if (!healthTextIdentity(state.tables.text)) {
      problems.push(`${scope}explore table inventory text is empty`);
    }
  } else {
    problems.push(
      ...explorerHiddenInspectionProblems(state.run.inspection, "run control", scope),
      ...explorerHiddenInspectionProblems(state.status.inspection, "status", scope),
      ...explorerHiddenInspectionProblems(state.tables.inspection, "table inventory", scope),
      ...explorerHiddenInspectionProblems(state.result.inspection, "result", scope),
    );
    if (state.mode === "no-js") {
      problems.push(
        ...explorerVisibleInspectionProblems(
          state.noJs.inspection,
          "no-JavaScript notice",
          scope,
          viewport,
        ),
      );
      const noJsText = healthTextIdentity(state.noJs.text);
      if (!noJsText.includes("瀏覽器內查詢需要JavaScript") || !noJsText.includes("不會下載查詢引擎")) {
        problems.push(`${scope}explore no-JavaScript notice changed`);
      }
    } else {
      problems.push(
        ...explorerHiddenInspectionProblems(state.noJs.inspection, "no-JavaScript notice", scope),
      );
      /* Print used to require the query to be spelled out on paper, on the
         argument that a printed page cannot expand a disclosure. There is no
         query on the page now, in any medium. */
    }
  }

  const inspections = [
    ...(state.steps ?? []).map((step) => step.inspection),
    state.run.inspection,
    state.tables.inspection,
    state.result.inspection,
    state.caveat.inspection,
  ];
  if (
    inspections.some((inspection) => !Number.isFinite(inspection?.sourceIndex)) ||
    !inspections.every(
      (inspection, index) => index === 0 || inspections[index - 1].sourceIndex < inspection.sourceIndex,
    )
  ) {
    problems.push(`${scope}explore source order changed`);
  }

  const blankResult =
    state.result.text === "" && !state.result.hasRows && !state.result.emptyMessage &&
    state.result.errorDetail === null && !state.result.focused;
  if (state.mode === "normal") {
    if (state.state === "initial") {
      if (state.run.disabled || state.status.text !== "" || state.status.busy || state.status.failed) {
        problems.push("explore initial controls changed");
      }
      if (!blankResult) problems.push("explore initial result is not empty");
    } else if (state.state === "loading") {
      if (!state.run.disabled || !state.status.busy || state.status.failed || !state.status.text) {
        problems.push("explore loading semantics changed");
      }
      if (!blankResult) problems.push("explore loading presents a prior result");
    } else if (state.state === "success") {
      if (
        state.run.disabled || state.status.busy || state.status.failed || !state.status.text.includes("列") ||
        !state.result.hasRows || state.result.emptyMessage || state.result.errorDetail !== null ||
        !state.result.focused
      ) problems.push("explore success semantics changed");
    } else if (state.state === "empty") {
      if (
        state.run.disabled || state.status.busy || state.status.failed || !state.status.text.includes("0 列") ||
        state.result.hasRows || !state.result.emptyMessage || state.result.errorDetail !== null ||
        !state.result.focused || !state.result.text.includes("查詢成立，但")
      ) problems.push("explore empty semantics changed");
    } else if (state.state === "failure") {
      if (
        state.run.disabled || state.status.busy || !state.status.failed ||
        !state.status.text.startsWith("查詢失敗：") || state.result.hasRows ||
        state.result.emptyMessage || !state.result.errorDetail || !state.result.focused ||
        !healthTextIdentity(state.result.text).includes(
          healthTextIdentity(state.result.errorDetail),
        )
      ) problems.push("explore failure semantics changed");
    }
    if (["success", "empty", "failure"].includes(state.state)) {
      problems.push(
        ...explorerVisibleInspectionProblems(state.result.inspection, "result", scope, viewport),
      );
    }
  } else if (state.mode === "zoom" || state.mode === "print") {
    if (state.run.disabled || state.status.text !== "" || state.status.busy || state.status.failed) {
      problems.push(`${scope}explore initial controls changed`);
    }
    if (!blankResult) problems.push(`${scope}explore initial result is not empty`);
  } else if (
    state.run.disabled || state.status.text !== "" || state.status.busy || state.status.failed ||
    !blankResult
  ) {
    problems.push("no-JavaScript explore inactive state changed");
  }
  return problems;
}

const HISTORICAL_STATION_ROUTES = new Set(["/", "/space/", "/data/"]);

function historicalStationCopyProblems(route, text, hrefs = null) {
  const compact = String(text ?? "").replace(/\s+/g, "").trim();
  const required = new Map([
    [
      "/",
      [
        "萬里測站不在環境部現行測站清冊",
        "環境部歷史測站紀錄",
        "台中、崇倫、阿里山、泰山、三民",
        "本專案尚未能定位",
      ],
    ],
    [
      // This chapter used to disclose that 萬里 was excluded for want of
      // coordinates and that its results had not been recomputed. Both stopped
      // being true once M6 read the reviewed historical supplement, so the gate
      // now pins the replacement claim: the station is in, on a named authority
      // that is not the current register, and the chapter was recomputed.
      "/space/",
      [
        "萬里",
        "不是來自環境部現行測站清冊",
        "審閱過的環境部歷史測站紀錄",
        "本章結果是在納入之後重算的",
      ],
    ],
    [
      "/data/",
      [
        "官方停測公告與年度封存值不一致",
        "2025年5月1日",
        "未解的來源歧異",
        "沒有判定這些值有效或無效",
      ],
    ],
  ]).get(route) ?? [];
  const problems = required.filter((phrase) => !compact.includes(phrase.replace(/\s+/g, "")))
    .map((phrase) => `missing historical-station disclosure ${JSON.stringify(phrase)}`);

  /*
   * The citation is pinned by where it points, and it points at the authority.
   *
   * Three shapes so far. It required the literal string
   * `conf/station_geo_historical.yaml` in the prose, on the argument that a
   * named authority is what makes 萬里's substituted coordinate auditable; then
   * a link to that same file, once the owner asked for the bare path to go.
   * Both sent a reader into this project's repository to read raw YAML for one
   * date, and the owner then asked for repository file links to go too.
   *
   * So the requirement is unchanged and the target moved: the disclosure must
   * still cite where the coordinate came from, but that is 環境部's own station
   * record, which is what the supplement was quoting in the first place. The
   * host is pinned rather than the full URL — the record id belongs to the
   * payload, not to this gate — and the review date is required as a date on
   * the page, which the file link used to hide behind a click.
   */
  const citations = new Map([
    ["/space/", "airtw.moenv.gov.tw"],
  ]);
  const citation = citations.get(route);
  if (citation && Array.isArray(hrefs)) {
    if (!hrefs.some((href) => String(href ?? "").includes(citation))) {
      problems.push(
        `missing historical-station citation link to ${JSON.stringify(citation)}`,
      );
    }
    if (!/\d{4}-\d{2}-\d{2}\s*查證/.test(text)) {
      problems.push("missing historical-station review date");
    }
  }


  const forbidden = [
    {
      pattern: /萬里(?:測站)?(?:就是|等於|其實是|改名為)富貴角/,
      label: "aliases Wanli to Fugui Cape",
    },
    {
      pattern: /(?:未繪出|未定位)[^。]{0,160}(?:它們|全部|所有)[^。]{0,80}(?:已|都|全)?停用/,
      label: "treats every unplaced station as retired",
    },
    {
      pattern: /(?:萬里|停測後|停止監測後)[^。]{0,100}(?:資料|數值)[^。]{0,40}(?:判定為|屬於|就是)(?:無效|錯誤)/,
      label: "declares the post-announcement values invalid",
    },
  ];
  return problems.concat(
    forbidden.filter(({ pattern }) => pattern.test(compact))
      .map(({ label }) => `contains unsupported historical-station claim: ${label}`),
  );
}

const HISTORICAL_STATION_DISCLOSURE_PROBE = `(() => {
  const visible = (element) => {
    if (!element) return false;
    const style = getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.display !== "none" && style.visibility !== "hidden" &&
      rect.width > 0 && rect.height > 0;
  };
  const disclosure = document.querySelector("[data-publication-disagreement]");
  const summary = disclosure?.querySelector(":scope > summary") ?? null;
  const body = disclosure?.querySelector("[data-publication-disagreement-body]") ?? null;
  const defaultCollapsed = disclosure ? !disclosure.open : null;
  if (disclosure) disclosure.open = true;
  const result = {
    text: document.querySelector("main")?.textContent?.replace(/\\s+/g, " ").trim() ?? "",
    // Every link target in the chapter. A citation can be checkable without
    // its path being printed at the reader, so what has to be pinned is where
    // the link goes, not what the sentence spells out.
    hrefs: [...document.querySelectorAll("main a[href]")].map((a) => a.getAttribute("href")),
    disclosure: disclosure ? {
      defaultCollapsed,
      summaryVisible: visible(summary),
      summaryText: summary?.textContent?.replace(/\\s+/g, " ").trim() ?? "",
      bodyVisibleWhenOpen: visible(body),
      bodyText: body?.textContent?.replace(/\\s+/g, " ").trim() ?? "",
    } : null,
  };
  if (disclosure) disclosure.open = !defaultCollapsed;
  return result;
})()`;

function sourcesElementIsClipped(element, rectFor, styleFor) {
  if (!element) return false;
  const clipsWithCss = (style) => Boolean(
    style &&
    ((style.clip && style.clip !== "auto") ||
      (style.clipPath && style.clipPath !== "none") ||
      (style.webkitClipPath && style.webkitClipPath !== "none")),
  );
  const clipsOwnOverflow = (node, style) => Boolean(
    style &&
    ((["hidden", "clip"].includes(style.overflowX) &&
      node.scrollWidth > node.clientWidth + 0.1) ||
      (["hidden", "clip"].includes(style.overflowY) &&
        node.scrollHeight > node.clientHeight + 0.1)),
  );
  const box = rectFor(element);
  const ownStyle = styleFor(element);
  if (clipsWithCss(ownStyle) || clipsOwnOverflow(element, ownStyle)) return true;
  for (let parent = element.parentElement; box && parent; parent = parent.parentElement) {
    const style = styleFor(parent);
    if (clipsWithCss(style)) return true;
    if (["hidden", "clip"].includes(style?.overflowX) ||
        ["hidden", "clip"].includes(style?.overflowY)) {
      const parentBox = rectFor(parent);
      if (parentBox &&
          (box.left < parentBox.left - 0.1 || box.right > parentBox.right + 0.1 ||
            box.top < parentBox.top - 0.1 || box.bottom > parentBox.bottom + 0.1)) return true;
    }
  }
  return false;
}

const SOURCES_ATLAS_STATE_PROBE = `(() => {
  const rect = (element) => {
    const box = element?.getBoundingClientRect();
    return box ? { top: box.top, right: box.right, bottom: box.bottom, left: box.left, width: box.width, height: box.height } : null;
  };
  const visible = (element) => {
    const style = element ? getComputedStyle(element) : null;
    const box = rect(element);
    return Boolean(style && box && style.display !== "none" && style.visibility !== "hidden" && box.width > 0 && box.height > 0);
  };
  const clipped = (element) => (${sourcesElementIsClipped.toString()})(
    element,
    rect,
    (node) => getComputedStyle(node),
  );
  const node = document.querySelector("#cbpf-data");
  const select = document.querySelector("#cbpf-station");
  let data = null;
  try { data = node?.textContent ? JSON.parse(node.textContent) : null; } catch {}
  const classes = { low_wind_peak: "低風速高值型", mid_wind_peak: "中風速高值型", high_wind_peak: "高風速高值型" };
  const compass = { 0: "北", 30: "北北東", 60: "東北東", 90: "東", 120: "東南東", 150: "南南東", 180: "南", 210: "南南西", 240: "西南西", 270: "西", 300: "西北西", 330: "北北西" };
  const bearing = (degrees) => (String(degrees) + "° " + (compass[degrees] ?? "")).trim();
  const fill = (probability) => {
    if (probability == null) return "none";
    let index = 0;
    while (index < data.breaks.length && probability >= data.breaks[index]) index += 1;
    return "var(" + data.ramp[index] + ")";
  };
  const expectedFor = (station) => {
    const record = data?.stations?.[station];
    if (!record) return null;
    return {
      station,
      initialStation: select?.options?.[select.selectedIndex]?.defaultSelected ? station : select?.querySelector("option[selected]")?.value ?? select?.value ?? "",
      badge: { text: classes[record.wind_peak_class] ?? record.wind_peak_class, windPeakClass: record.wind_peak_class },
      readouts: {
        threshold: record.threshold.toFixed(1), peak: record.peak_sector == null ? "—" : bearing(record.peak_sector),
        peakSpeed: "風速 " + record.peak_speed + " m/s", resultant: record.resultant.toFixed(3),
        calm: (record.calm_fraction * 100).toFixed(1) + "%",
      },
      cells: data.sectors.flatMap((sector, sectorIndex) => data.speed_bins.map((speed, speedIndex) => {
        const probability = record.probability[sectorIndex][speedIndex];
        const hours = record.n[sectorIndex][speedIndex];
        return {
          key: sectorIndex + "-" + speedIndex,
          fill: fill(probability),
          title: bearing(sector) + " · " + speed.replace("-", "–") + " m/s — " +
            (probability == null ? "時數不足（" + hours + " 小時）" : "機率 " + probability + "（" + hours + " 小時）"),
        };
      })),
    };
  };
  const initialStation = select?.querySelector("option[selected]")?.value ?? select?.value ?? "";
  const selectedStation = select?.value ?? "";
  const boundary = document.querySelector("[data-sources-method-boundary]");
  const picker = document.querySelector("[data-sources-picker]");
  const fallbacks = [...document.querySelectorAll("[data-sources-nojs-fallback]")];
  const fallback = fallbacks.find(visible) ?? fallbacks[0] ?? null;
  const primary = document.querySelector("[data-primary-evidence]");
  const title = primary?.querySelector(".evidence-title") ?? null;
  const plot = primary?.querySelector("[data-primary-plot]") ?? null;
  const sourceOrder = [...document.querySelectorAll("main *")];
  const badge = document.querySelector("#cbpf-wind-peak-class");
  const readoutElements = {
    threshold: document.querySelector("#cbpf-threshold"),
    peak: document.querySelector("#cbpf-peak"),
    peakSpeed: document.querySelector("#cbpf-peak-speed"),
    resultant: document.querySelector("#cbpf-resultant"),
    calm: document.querySelector("#cbpf-calm"),
  };
  return {
    boundary: boundary ? { count: document.querySelectorAll("[data-sources-method-boundary]").length, visible: visible(boundary), ariaHidden: boundary.getAttribute("aria-hidden"), opacity: Number(getComputedStyle(boundary).opacity), clipped: clipped(boundary), text: boundary.textContent?.replace(/\\s+/g, " ").trim() ?? "", rect: rect(boundary) } : { count: 0 },
    picker: picker ? { count: document.querySelectorAll("[data-sources-picker]").length, visible: visible(picker), rect: rect(picker) } : { count: 0 },
    fallback: fallback ? {
      count: fallbacks.length,
      visible: visible(fallback),
      station: fallback.querySelector("[data-sources-nojs-station]")?.textContent?.trim() ?? "",
      classification: fallback.querySelector("[data-sources-nojs-classification]")?.textContent?.trim() ?? "",
      rect: rect(fallback),
    } : { count: 0 },
    primary: { count: document.querySelectorAll("[data-primary-evidence]").length, visible: visible(primary), rect: rect(primary), title: { visible: visible(title), rect: rect(title) }, plot: { visible: visible(plot), rect: rect(plot) } },
    sourceIndexes: { lede: sourceOrder.indexOf(document.querySelector("#sources .lede")), boundary: sourceOrder.indexOf(boundary), picker: sourceOrder.indexOf(picker), primary: sourceOrder.indexOf(primary) },
    overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    selectedStation,
    initialStation,
    badge: { text: badge?.textContent?.trim() ?? "", windPeakClass: badge?.getAttribute("data-wind-peak-class") ?? "" },
    captionStation: document.querySelector("#cbpf-where")?.textContent?.trim() ?? "",
    readouts: Object.fromEntries(Object.entries(readoutElements).map(
      ([key, element]) => [key, element?.textContent?.trim() ?? ""],
    )),
    readoutVisibility: Object.fromEntries(Object.entries(readoutElements).map(
      ([key, element]) => [key, visible(element)],
    )),
    cells: [...document.querySelectorAll(".cell[data-key]")].map((cell) => ({ key: cell.getAttribute("data-key") ?? "", fill: cell.getAttribute("fill") ?? "", title: cell.querySelector("title")?.textContent?.replace(/\\s+/g, " ").trim() ?? "" })),
    expected: expectedFor(selectedStation),
  };
})()`;

const SOURCES_ATLAS_TRANSITION_PROBE = `(() => {
  const snapshot = () => ${SOURCES_ATLAS_STATE_PROBE};
  const focus = () => {
    const active = document.activeElement;
    return active ? { tag: active.tagName, id: active.id, name: active.getAttribute("name") ?? "" } : null;
  };
  const completeSnapshot = () => ({ atlas: snapshot(), focus: focus(), url: location.href, scroll: [scrollX, scrollY] });
  const select = document.querySelector("#cbpf-station");
  const before = completeSnapshot();
  const initial = before.atlas.selectedStation;
  const target = [...(select?.options ?? [])].map((option) => option.value).find((value) => value !== initial) ?? "";
  if (!select || !initial || !target) throw new Error("Sources station transition has no non-default option");
  const originalActive = document.activeElement;
  const matchesExpected = () => {
    const state = snapshot();
    const expected = state.expected;
    return Boolean(expected && state.selectedStation === expected.station &&
      state.badge.text === expected.badge.text && state.badge.windPeakClass === expected.badge.windPeakClass &&
      state.captionStation === expected.station &&
      ["threshold", "peak", "peakSpeed", "resultant", "calm"].every((key) => state.readouts[key] === expected.readouts[key]) &&
      JSON.stringify(state.cells) === JSON.stringify(expected.cells));
  };
  const waitForExpected = () => new Promise((resolve, reject) => {
    let frames = 0;
    const check = () => {
      if (matchesExpected()) return resolve();
      frames += 1;
      if (frames === 20) return reject(new Error("Sources station transition did not reach payload-derived DOM state"));
      requestAnimationFrame(check);
    };
    requestAnimationFrame(check);
  });
  return (async () => {
    try {
      select.value = target;
      select.dispatchEvent(new Event("change", { bubbles: true }));
      await waitForExpected();
      const transitioned = snapshot();
      if (transitioned.selectedStation === initial) throw new Error("Sources station transition retained its initial option");
      return transitioned;
    } finally {
      select.value = initial;
      select.dispatchEvent(new Event("change", { bubbles: true }));
      await waitForExpected();
      if (document.activeElement !== originalActive && originalActive instanceof HTMLElement) originalActive.focus();
      scrollTo(before.scroll[0], before.scroll[1]);
      await new Promise((resolve) => requestAnimationFrame(resolve));
    }
  })().then((transitioned) => {
    transitioned.restoration = { before, after: completeSnapshot() };
    return transitioned;
  });
})()`;

function publicationDisclosureProblems(state) {
  const disclosure = state?.disclosure;
  if (!disclosure) return ["missing expandable publication-disagreement disclosure"];
  const problems = [];
  if (!disclosure.defaultCollapsed) {
    problems.push("publication-disagreement disclosure is not collapsed by default");
  }
  if (!disclosure.summaryVisible || !disclosure.summaryText) {
    problems.push("publication-disagreement summary is not visible");
  }
  if (!disclosure.bodyVisibleWhenOpen || !disclosure.bodyText) {
    problems.push("publication-disagreement body is not readable after opening");
  }
  return problems;
}

const DETECTION_ESTIMATE_TABLE_PROBE = `(() => {
  const table = document.querySelector("[data-detection-results]");
  if (!table) return null;
  return {
    heading: table.querySelector("thead th:nth-child(3)")?.textContent.trim() ?? "",
    rows: [...table.querySelectorAll("tbody tr")].map((row) => {
      const estimate = row.querySelector("[data-estimate-cell]");
      return {
        kind: row.dataset.eventKind ?? "",
        label: estimate?.querySelector("[data-estimate-label]")?.textContent.trim() ?? "",
        value: estimate?.querySelector("[data-estimate-value]")?.textContent.trim() ?? "",
        unit: estimate?.querySelector("[data-estimate-unit]")?.textContent.trim() ?? "",
      };
    }),
  };
})()`;

function detectionEstimateTableProblems(table) {
  if (!table) return ["missing rendered detection results table contract"];
  const problems = [];
  if (table.heading !== "中位估計值") {
    problems.push(`results estimate heading is ${JSON.stringify(table.heading)}, expected "中位估計值"`);
  }
  const expected = new Map([
    ["window", { count: 2, label: "觀測－預測差額", unit: "μg/m³" }],
    ["trend_break", { count: 1, label: "斜率差", unit: "μg/m³/年" }],
  ]);
  for (const [kind, contract] of expected) {
    const rows = table.rows?.filter((row) => row.kind === kind) ?? [];
    if (rows.length !== contract.count) {
      problems.push(`${kind} estimate rows total ${rows.length}, expected ${contract.count}`);
    }
    for (const row of rows) {
      if (row.label !== contract.label || row.unit !== contract.unit || !row.value) {
        problems.push(
          `${kind} estimate is ${JSON.stringify(row)}, expected ` +
            `${JSON.stringify(contract.label)}, a value, and ${JSON.stringify(contract.unit)}`,
        );
      }
    }
  }
  return problems;
}

const detectionLimitationBriefSnapshotExpression = (mode) => `(() => {
  const mode = ${JSON.stringify(mode)};
  const compact = (value) => String(value ?? "").replace(/\\s+/g, " ").trim();
  const allElements = [...document.querySelectorAll("main *")];
  const sourceIndex = (element) => element ? allElements.indexOf(element) : -1;
  const clippingOverflow = new Set(["auto", "clip", "hidden", "scroll"]);
  const inspect = (element) => {
    if (!element) return null;
    const rect = element.getBoundingClientRect();
    const ownStyle = getComputedStyle(element);
    let rendered = rect.width > 0 && rect.height > 0;
    let opacity = 1;
    let hidden = false;
    let ariaHidden = false;
    let inert = false;
    let detailsAncestor = false;
    let cssClip = false;
    let cssClipPath = false;
    let visibleLeft = rect.left;
    let visibleRight = rect.right;
    let visibleTop = rect.top;
    let visibleBottom = rect.bottom;
    for (let node = element; node; node = node.parentElement) {
      const style = getComputedStyle(node);
      const nodeOpacity = Number(style.opacity);
      if (
        style.display === "none" || style.visibility === "hidden" ||
        style.visibility === "collapse" || !Number.isFinite(nodeOpacity) || nodeOpacity <= 0
      ) rendered = false;
      if (Number.isFinite(nodeOpacity)) opacity *= nodeOpacity;
      hidden ||= node.hasAttribute("hidden");
      ariaHidden ||= node.getAttribute("aria-hidden") === "true";
      inert ||= node.hasAttribute("inert");
      detailsAncestor ||= node instanceof HTMLDetailsElement;
      cssClip ||= style.clip !== "auto";
      cssClipPath ||= style.clipPath !== "none";
      if (node !== element) {
        const bounds = node.getBoundingClientRect();
        if (clippingOverflow.has(style.overflowX)) {
          visibleLeft = Math.max(visibleLeft, bounds.left);
          visibleRight = Math.min(visibleRight, bounds.right);
        }
        if (clippingOverflow.has(style.overflowY)) {
          visibleTop = Math.max(visibleTop, bounds.top);
          visibleBottom = Math.min(visibleBottom, bounds.bottom);
        }
      }
    }
    const selfOverflowX = clippingOverflow.has(ownStyle.overflowX)
      ? Math.max(0, element.scrollWidth - element.clientWidth) : 0;
    const selfOverflowY = clippingOverflow.has(ownStyle.overflowY)
      ? Math.max(0, element.scrollHeight - element.clientHeight) : 0;
    return {
      display: ownStyle.display,
      visibility: ownStyle.visibility,
      rendered,
      hidden,
      ariaHidden,
      inert,
      accessible: rendered && !hidden && !ariaHidden && !inert,
      opacity,
      top: rect.top,
      right: rect.right,
      bottom: rect.bottom,
      left: rect.left,
      width: rect.width,
      height: rect.height,
      sourceIndex: sourceIndex(element),
      cssOrder: Number(ownStyle.order) || 0,
      selfOverflowX,
      selfOverflowY,
      ancestorClipped:
        visibleRight - visibleLeft < rect.width - 1 ||
        visibleBottom - visibleTop < rect.height - 1,
      cssClip,
      cssClipPath,
      detailsAncestor,
      collapsed: element instanceof HTMLDetailsElement && !element.open,
      tagName: element.tagName,
    };
  };
  const parseNumberAttribute = (element, name) => {
    const raw = element?.getAttribute(name);
    if (raw === null || raw === undefined) return null;
    if (!/^-?(?:0|[1-9]\\d*)(?:\\.\\d+)?$/u.test(raw)) return raw;
    const value = Number(raw);
    return Number.isFinite(value) ? value : raw;
  };
  const keys = [...document.querySelectorAll("[data-detection-reading-key]")];
  const comparisons = [...document.querySelectorAll("[data-detection-comparison]")];
  const boundaries = [...document.querySelectorAll("[data-detection-inference-boundary]")];
  const key = keys[0] ?? null;
  const comparison = comparisons[0] ?? null;
  const boundary = boundaries[0] ?? null;
  const primaryEvidence = document.querySelector("[data-primary-evidence]");
  const title = primaryEvidence?.querySelector(".evidence-title") ?? null;
  const primaryPlot = primaryEvidence?.querySelector("[data-primary-plot]") ?? null;
  const caption = primaryEvidence?.querySelector("figcaption") ?? null;
  const methodEvidence = document.querySelector("[data-detection-method-evidence]");
  const semanticRows = [...(comparison?.children ?? [])];
  return {
    mode,
    theme: document.documentElement.dataset.theme ?? "light",
    counts: {
      readingKey: keys.length,
      comparison: comparisons.length,
      boundary: boundaries.length,
      semanticRows: semanticRows.length,
      eventHooks:
        comparison?.querySelectorAll("[data-detection-event]").length ?? 0,
    },
    regions: {
      readingKey: inspect(key),
      comparison: inspect(comparison),
      boundary: inspect(boundary),
    },
    landmarks: {
      title: inspect(title),
      key: inspect(key),
      primaryPlot: inspect(primaryPlot),
      caption: inspect(caption),
      comparison: inspect(comparison),
      boundary: inspect(boundary),
      methodEvidence: inspect(methodEvidence),
    },
    readingSteps: [...(key?.querySelectorAll("[data-detection-reading-step]") ?? [])]
      .map((step) => {
        const geometry = inspect(step);
        return {
          key: step.getAttribute("data-detection-reading-step") ?? "",
          visibleText: compact(step.innerText),
          accessibleText: null,
          top: geometry?.top ?? null,
          right: geometry?.right ?? null,
          bottom: geometry?.bottom ?? null,
          left: geometry?.left ?? null,
          width: geometry?.width ?? null,
          height: geometry?.height ?? null,
          sourceIndex: geometry?.sourceIndex ?? null,
          cssOrder: geometry?.cssOrder ?? null,
        };
      }),
    eventRows: semanticRows.map((row) => ({
      event: row.getAttribute("data-detection-event") ?? "",
      kind: row.getAttribute("data-detection-kind") ?? "",
      observed: parseNumberAttribute(row, "data-detection-observed"),
      expected: parseNumberAttribute(row, "data-detection-expected"),
      hooked: row.hasAttribute("data-detection-event"),
      rowTag: row.tagName,
      directChildTags: [...row.children].map((child) => child.tagName),
      visibleText: compact(row.innerText),
      accessibleText: null,
      inspection: inspect(row),
    })),
    boundaryText: compact(boundary?.innerText),
    pageText: compact(document.querySelector("main")?.innerText),
    viewport: { width: innerWidth, height: innerHeight },
    document: {
      clientWidth: document.documentElement.clientWidth,
      scrollWidth: document.documentElement.scrollWidth,
    },
  };
})()`;

const healthAssumptionLedgerSnapshotExpression = (mode) => `(() => {
  const mode = ${JSON.stringify(mode)};
  const compact = (value) => String(value ?? "").replace(/\\s+/g, " ").trim();
  const allElements = [...document.querySelectorAll("main *")];
  const sourceIndex = (element) => element ? allElements.indexOf(element) : -1;
  const clippingOverflow = new Set(["auto", "clip", "hidden", "scroll"]);
  const inspect = (element) => {
    if (!element) return null;
    const rect = element.getBoundingClientRect();
    const ownStyle = getComputedStyle(element);
    let rendered = rect.width > 0 && rect.height > 0;
    let opacity = 1;
    let hidden = false;
    let ariaHidden = false;
    let inert = false;
    let detailsAncestor = false;
    let cssClip = false;
    let cssClipPath = false;
    let visibleLeft = rect.left;
    let visibleRight = rect.right;
    let visibleTop = rect.top;
    let visibleBottom = rect.bottom;
    for (let node = element; node; node = node.parentElement) {
      const style = getComputedStyle(node);
      const nodeOpacity = Number(style.opacity);
      if (
        style.display === "none" || style.visibility === "hidden" ||
        style.visibility === "collapse" || !Number.isFinite(nodeOpacity) || nodeOpacity <= 0
      ) rendered = false;
      if (Number.isFinite(nodeOpacity)) opacity *= nodeOpacity;
      hidden ||= node.hasAttribute("hidden");
      ariaHidden ||= node.getAttribute("aria-hidden") === "true";
      inert ||= node.hasAttribute("inert");
      detailsAncestor ||= node instanceof HTMLDetailsElement;
      cssClip ||= style.clip !== "auto";
      cssClipPath ||= style.clipPath !== "none";
      if (node !== element) {
        const bounds = node.getBoundingClientRect();
        if (clippingOverflow.has(style.overflowX)) {
          visibleLeft = Math.max(visibleLeft, bounds.left);
          visibleRight = Math.min(visibleRight, bounds.right);
        }
        if (clippingOverflow.has(style.overflowY)) {
          visibleTop = Math.max(visibleTop, bounds.top);
          visibleBottom = Math.min(visibleBottom, bounds.bottom);
        }
      }
    }
    const selfOverflowX = clippingOverflow.has(ownStyle.overflowX)
      ? Math.max(0, element.scrollWidth - element.clientWidth) : 0;
    const selfOverflowY = clippingOverflow.has(ownStyle.overflowY)
      ? Math.max(0, element.scrollHeight - element.clientHeight) : 0;
    return {
      display: ownStyle.display,
      visibility: ownStyle.visibility,
      rendered,
      hidden,
      ariaHidden,
      inert,
      accessible: rendered && !hidden && !ariaHidden && !inert,
      opacity,
      top: rect.top,
      right: rect.right,
      bottom: rect.bottom,
      left: rect.left,
      width: rect.width,
      height: rect.height,
      sourceIndex: sourceIndex(element),
      cssOrder: Number(ownStyle.order) || 0,
      selfOverflowX,
      selfOverflowY,
      ancestorClipped:
        visibleRight - visibleLeft < rect.width - 1 ||
        visibleBottom - visibleTop < rect.height - 1,
      cssClip,
      cssClipPath,
      detailsAncestor,
    };
  };
  const ledgers = [...document.querySelectorAll("[data-health-assumption-ledger]")];
  const readingBands = [...document.querySelectorAll("[data-health-reading-band]")];
  const boundaries = [...document.querySelectorAll("[data-health-inference-boundaries]")];
  const ledger = ledgers[0] ?? null;
  const readingBand = readingBands[0] ?? null;
  const boundary = boundaries[0] ?? null;
  const assumptionRows = [...(ledger?.children ?? [])];
  const readingRows = [...(readingBand?.children ?? [])];
  const inferenceRows = [...(boundary?.children ?? [])];
  const primary = document.querySelector("[data-primary-evidence]");
  const figures = [...document.querySelectorAll("main .evidence-figure")];
  const primaryTitle = primary?.querySelector(".evidence-title") ?? null;
  const primaryPlot = primary?.querySelector("[data-primary-plot]") ?? null;
  const caption = primary?.querySelector("figcaption") ?? null;
  const figure2Title = figures[1]?.querySelector(".evidence-title") ?? null;
  const lede = document.querySelector("main .chapter-intro .lede");
  return {
    mode,
    theme: document.documentElement.dataset.theme ?? "light",
    counts: {
      ledger: ledgers.length,
      readingBand: readingBands.length,
      boundaries: boundaries.length,
      assumptionRows: assumptionRows.length,
      readingRows: readingRows.length,
      inferenceRows: inferenceRows.length,
    },
    regions: {
      ledger: inspect(ledger),
      readingBand: inspect(readingBand),
      boundaries: inspect(boundary),
    },
    landmarks: {
      lede: inspect(lede),
      ledger: inspect(ledger),
      primaryTitle: inspect(primaryTitle),
      primaryPlot: inspect(primaryPlot),
      caption: inspect(caption),
      readingBand: inspect(readingBand),
      figure2Title: inspect(figure2Title),
      boundaries: inspect(boundary),
    },
    assumptionRows: assumptionRows.map((row) => ({
      key: row.getAttribute("data-health-assumption") ?? "",
      visibleText: compact(row.innerText),
      accessibleText: null,
      inspection: inspect(row),
    })),
    readingRows: readingRows.map((row) => ({
      key: row.getAttribute("data-health-reading") ?? "",
      heading: compact(row.querySelector(":scope > h2")?.innerText),
      accessibleHeading: null,
      bodyText: compact(row.querySelector(":scope > p")?.innerText),
      accessibleBody: null,
      inspection: inspect(row),
    })),
    inferenceRows: inferenceRows.map((row) => ({
      key: row.getAttribute("data-health-inference") ?? "",
      visibleText: compact(row.innerText),
      accessibleText: null,
      inspection: inspect(row),
    })),
    figure2Title: compact(figure2Title?.innerText),
    viewport: { width: innerWidth, height: innerHeight },
    document: {
      clientWidth: document.documentElement.clientWidth,
      scrollWidth: document.documentElement.scrollWidth,
    },
  };
})()`;

const forecastHorizonDecisionSnapshotExpression = (mode) => `(() => {
  const mode = ${JSON.stringify(mode)};
  const compact = (value) => String(value ?? "").replace(/\\s+/g, " ").trim();
  const allElements = [...document.querySelectorAll("main *")];
  const sourceIndex = (element) => element ? allElements.indexOf(element) : -1;
  const clippingOverflow = new Set(["auto", "clip", "hidden", "scroll"]);
  const inspect = (element) => {
    if (!element) return null;
    const rect = element.getBoundingClientRect();
    const ownStyle = getComputedStyle(element);
    let rendered = rect.width > 0 && rect.height > 0;
    let opacity = 1;
    let hidden = false;
    let ariaHidden = false;
    let inert = false;
    let detailsAncestor = false;
    let cssClip = false;
    let cssClipPath = false;
    let visibleLeft = rect.left;
    let visibleRight = rect.right;
    let visibleTop = rect.top;
    let visibleBottom = rect.bottom;
    for (let node = element; node; node = node.parentElement) {
      const style = getComputedStyle(node);
      const nodeOpacity = Number(style.opacity);
      if (
        style.display === "none" || style.visibility === "hidden" ||
        style.visibility === "collapse" || !Number.isFinite(nodeOpacity) || nodeOpacity <= 0
      ) rendered = false;
      if (Number.isFinite(nodeOpacity)) opacity *= nodeOpacity;
      hidden ||= node.hasAttribute("hidden");
      ariaHidden ||= node.getAttribute("aria-hidden") === "true";
      inert ||= node.hasAttribute("inert");
      detailsAncestor ||= node instanceof HTMLDetailsElement;
      cssClip ||= style.clip !== "auto";
      cssClipPath ||= style.clipPath !== "none";
      if (node !== element) {
        const bounds = node.getBoundingClientRect();
        if (clippingOverflow.has(style.overflowX)) {
          visibleLeft = Math.max(visibleLeft, bounds.left);
          visibleRight = Math.min(visibleRight, bounds.right);
        }
        if (clippingOverflow.has(style.overflowY)) {
          visibleTop = Math.max(visibleTop, bounds.top);
          visibleBottom = Math.min(visibleBottom, bounds.bottom);
        }
      }
    }
    return {
      display: ownStyle.display,
      visibility: ownStyle.visibility,
      rendered,
      hidden,
      ariaHidden,
      inert,
      accessible: rendered && !hidden && !ariaHidden && !inert,
      opacity,
      top: rect.top,
      right: rect.right,
      bottom: rect.bottom,
      left: rect.left,
      width: rect.width,
      height: rect.height,
      sourceIndex: sourceIndex(element),
      cssOrder: Number(ownStyle.order) || 0,
      selfOverflowX: clippingOverflow.has(ownStyle.overflowX)
        ? Math.max(0, element.scrollWidth - element.clientWidth) : 0,
      selfOverflowY: clippingOverflow.has(ownStyle.overflowY)
        ? Math.max(0, element.scrollHeight - element.clientHeight) : 0,
      ancestorClipped:
        visibleRight - visibleLeft < rect.width - 1 ||
        visibleBottom - visibleTop < rect.height - 1,
      cssClip,
      cssClipPath,
      detailsAncestor,
    };
  };
  const sheets = [...document.querySelectorAll("[data-forecast-decision-sheet]")];
  const readingBands = [...document.querySelectorAll("[data-forecast-reading-band]")];
  const baselineBands = [...document.querySelectorAll("[data-forecast-baseline-band]")];
  const sheet = sheets[0] ?? null;
  const readingBand = readingBands[0] ?? null;
  const baselineBand = baselineBands[0] ?? null;
  const decisionRows = [...(sheet?.querySelectorAll(":scope > ol > li") ?? [])];
  const readingRows = [...(readingBand?.children ?? [])];
  const baselineRows = [...(baselineBand?.children ?? [])];
  const figure1Title = document.querySelector("#evidence-6-1-title");
  const primaryPlot = document.querySelector("[data-primary-evidence] [data-primary-plot]");
  const figure2Title = document.querySelector("#evidence-6-2-title");
  const cost = document.querySelector("#forecast-cost");
  return {
    mode,
    counts: {
      decisionSheet: sheets.length,
      readingBand: readingBands.length,
      baselineBand: baselineBands.length,
      decisionRows: decisionRows.length,
      readingRows: readingRows.length,
      baselineRows: baselineRows.length,
    },
    regions: {
      decisionSheet: inspect(sheet),
      readingBand: inspect(readingBand),
      baselineBand: inspect(baselineBand),
    },
    landmarks: {
      figure1Title: inspect(figure1Title),
      primaryPlot: inspect(primaryPlot),
      decisionSheet: inspect(sheet),
      figure2Title: inspect(figure2Title),
      readingBand: inspect(readingBand),
      baselineBand: inspect(baselineBand),
      cost: inspect(cost),
    },
    decisionRows: decisionRows.map((row) => {
      const link = row.querySelector(":scope > a");
      return {
        key: row.getAttribute("data-forecast-decision") ?? "",
        label: compact(link?.querySelector(":scope > strong")?.innerText),
        bodyText: compact(link?.querySelector(":scope > p")?.innerText),
        href: link?.getAttribute("href") ?? "",
        accessibleText: null,
        inspection: inspect(row),
      };
    }),
    readingRows: readingRows.map((row) => ({
      key: row.getAttribute("data-forecast-reading") ?? "",
      heading: compact(row.querySelector(":scope > h2")?.innerText),
      bodyText: compact(row.querySelector(":scope > p")?.innerText),
      accessibleHeading: null,
      accessibleBody: null,
      inspection: inspect(row),
    })),
    baselineRows: baselineRows.map((row) => ({
      key: row.getAttribute("data-forecast-baseline") ?? "",
      heading: compact(row.querySelector(":scope > h2")?.innerText),
      whatText: compact(row.querySelector(":scope > p:first-of-type")?.innerText),
      whyText: compact(row.querySelector(":scope > p:last-of-type")?.innerText),
      accessibleText: null,
      inspection: inspect(row),
    })),
    pageText: compact(document.querySelector("main")?.innerText),
    viewport: { width: innerWidth, height: innerHeight },
    document: {
      clientWidth: document.documentElement.clientWidth,
      scrollWidth: document.documentElement.scrollWidth,
    },
  };
})()`;

const methodsCaseIndexSnapshotExpression = (mode) => `(() => {
  const mode = ${JSON.stringify(mode)};
  const compact = (value) => String(value ?? "").replace(/\\s+/g, " ").trim();
  const allElements = [...document.querySelectorAll("main *")];
  const sourceIndex = (element) => element ? allElements.indexOf(element) : -1;
  const clippingOverflow = new Set(["auto", "clip", "hidden", "scroll"]);
  const inspect = (element) => {
    if (!element) return null;
    const rect = element.getBoundingClientRect();
    const ownStyle = getComputedStyle(element);
    let rendered = rect.width > 0 && rect.height > 0;
    let opacity = 1;
    let hidden = false;
    let ariaHidden = false;
    let inert = false;
    let detailsAncestor = false;
    let cssClip = false;
    let cssClipPath = false;
    let visibleLeft = rect.left;
    let visibleRight = rect.right;
    let visibleTop = rect.top;
    let visibleBottom = rect.bottom;
    for (let node = element; node; node = node.parentElement) {
      const style = getComputedStyle(node);
      const nodeOpacity = Number(style.opacity);
      if (
        style.display === "none" || style.visibility === "hidden" ||
        style.visibility === "collapse" || !Number.isFinite(nodeOpacity) || nodeOpacity <= 0
      ) rendered = false;
      if (Number.isFinite(nodeOpacity)) opacity *= nodeOpacity;
      hidden ||= node.hasAttribute("hidden");
      ariaHidden ||= node.getAttribute("aria-hidden") === "true";
      inert ||= node.hasAttribute("inert");
      detailsAncestor ||= node instanceof HTMLDetailsElement;
      cssClip ||= style.clip !== "auto";
      cssClipPath ||= style.clipPath !== "none";
      if (node !== element) {
        const bounds = node.getBoundingClientRect();
        if (clippingOverflow.has(style.overflowX)) {
          visibleLeft = Math.max(visibleLeft, bounds.left);
          visibleRight = Math.min(visibleRight, bounds.right);
        }
        if (clippingOverflow.has(style.overflowY)) {
          visibleTop = Math.max(visibleTop, bounds.top);
          visibleBottom = Math.min(visibleBottom, bounds.bottom);
        }
      }
    }
    return {
      display: ownStyle.display,
      visibility: ownStyle.visibility,
      rendered,
      hidden,
      ariaHidden,
      inert,
      accessible: rendered && !hidden && !ariaHidden && !inert,
      opacity,
      top: rect.top,
      right: rect.right,
      bottom: rect.bottom,
      left: rect.left,
      width: rect.width,
      height: rect.height,
      sourceIndex: sourceIndex(element),
      cssOrder: Number(ownStyle.order) || 0,
      selfOverflowX: clippingOverflow.has(ownStyle.overflowX)
        ? Math.max(0, element.scrollWidth - element.clientWidth) : 0,
      selfOverflowY: clippingOverflow.has(ownStyle.overflowY)
        ? Math.max(0, element.scrollHeight - element.clientHeight) : 0,
      ancestorClipped:
        visibleRight - visibleLeft < rect.width - 1 ||
        visibleBottom - visibleTop < rect.height - 1,
      cssClip,
      cssClipPath,
      detailsAncestor,
    };
  };
  const indexes = [...document.querySelectorAll("[data-method-case-index]")];
  const labels = [...document.querySelectorAll("#method-case-index-title")];
  const links = [...document.querySelectorAll("[data-method-case-link]")];
  const destinations = [...document.querySelectorAll("[data-method-case]")];
  const index = indexes[0] ?? null;
  const lede = document.querySelector("main .chapter-intro .lede");
  const primary = document.querySelector("[data-primary-evidence]");
  const primaryPlot = primary?.querySelector("[data-primary-plot]") ?? null;
  return {
    mode,
    counts: {
      indexes: indexes.length,
      labelTargets: labels.length,
      links: links.length,
      destinations: destinations.length,
    },
    index: inspect(index),
    indexHeading: compact(labels[0]?.innerText),
    indexAccessibleName: null,
    links: links.map((link) => ({
      number: link.getAttribute("data-case") ?? "",
      title: compact(link.querySelector(":scope > span:last-child")?.innerText),
      href: link.getAttribute("href") ?? "",
      targetId: document.getElementById((link.getAttribute("href") ?? "").replace(/^#/, ""))?.id ?? "",
      visibleText: compact(link.innerText),
      accessibleText: null,
      inspection: inspect(link),
    })),
    destinations: destinations.map((destination) => ({
      number: destination.getAttribute("data-method-case") ?? "",
      id: destination.id,
      heading: compact(destination.querySelector(":scope > h2 > span:last-child")?.innerText),
      accessibleHeading: null,
      inspection: inspect(destination),
    })),
    landmarks: {
      lede: inspect(lede),
      index: inspect(index),
      primary: inspect(primary),
      primaryPlot: inspect(primaryPlot),
    },
    viewport: { width: innerWidth, height: innerHeight },
    document: {
      clientWidth: document.documentElement.clientWidth,
      scrollWidth: document.documentElement.scrollWidth,
    },
  };
})()`;

const dataProvenanceRegisterSnapshotExpression = (mode) => `(() => {
  const mode = ${JSON.stringify(mode)};
  const compact = (value) => String(value ?? "").replace(/\\s+/g, " ").trim();
  const allElements = [...document.querySelectorAll("main *")];
  const sourceIndex = (element) => element ? allElements.indexOf(element) : -1;
  const clippingOverflow = new Set(["auto", "clip", "hidden", "scroll"]);
  const inspect = (element, allowedClipAncestor = null, allowSelfOverflow = false) => {
    if (!element) return null;
    const rect = element.getBoundingClientRect();
    const ownStyle = getComputedStyle(element);
    let rendered = rect.width > 0 && rect.height > 0;
    let opacity = 1;
    let hidden = false;
    let ariaHidden = false;
    let inert = false;
    let detailsAncestor = false;
    let cssClip = false;
    let cssClipPath = false;
    let visibleLeft = rect.left;
    let visibleRight = rect.right;
    let visibleTop = rect.top;
    let visibleBottom = rect.bottom;
    for (let node = element; node; node = node.parentElement) {
      const style = getComputedStyle(node);
      const nodeOpacity = Number(style.opacity);
      if (
        style.display === "none" || style.visibility === "hidden" ||
        style.visibility === "collapse" || !Number.isFinite(nodeOpacity) || nodeOpacity <= 0
      ) rendered = false;
      if (Number.isFinite(nodeOpacity)) opacity *= nodeOpacity;
      hidden ||= node.hasAttribute("hidden");
      ariaHidden ||= node.getAttribute("aria-hidden") === "true";
      inert ||= node.hasAttribute("inert");
      detailsAncestor ||= node instanceof HTMLDetailsElement;
      cssClip ||= style.clip !== "auto";
      cssClipPath ||= style.clipPath !== "none";
      if (node !== element) {
        const bounds = node.getBoundingClientRect();
        if (node !== allowedClipAncestor && clippingOverflow.has(style.overflowX)) {
          visibleLeft = Math.max(visibleLeft, bounds.left);
          visibleRight = Math.min(visibleRight, bounds.right);
        }
        if (node !== allowedClipAncestor && clippingOverflow.has(style.overflowY)) {
          visibleTop = Math.max(visibleTop, bounds.top);
          visibleBottom = Math.min(visibleBottom, bounds.bottom);
        }
      }
    }
    return {
      display: ownStyle.display,
      visibility: ownStyle.visibility,
      rendered,
      hidden,
      ariaHidden,
      inert,
      accessible: rendered && !hidden && !ariaHidden && !inert,
      opacity,
      top: rect.top,
      right: rect.right,
      bottom: rect.bottom,
      left: rect.left,
      width: rect.width,
      height: rect.height,
      sourceIndex: sourceIndex(element),
      cssOrder: Number(ownStyle.order) || 0,
      selfOverflowX: !allowSelfOverflow && clippingOverflow.has(ownStyle.overflowX)
        ? Math.max(0, element.scrollWidth - element.clientWidth) : 0,
      selfOverflowY: !allowSelfOverflow && clippingOverflow.has(ownStyle.overflowY)
        ? Math.max(0, element.scrollHeight - element.clientHeight) : 0,
      ancestorClipped:
        visibleRight - visibleLeft < rect.width - 1 ||
        visibleBottom - visibleTop < rect.height - 1,
      cssClip,
      cssClipPath,
      detailsAncestor,
    };
  };
  const taskRegisters = [...document.querySelectorAll("[data-data-task-register]")];
  const schemaRegisters = [...document.querySelectorAll("[data-data-schema-register]")];
  const registers = [...document.querySelectorAll("[data-data-layer-register]")];
  const terms = [...document.querySelectorAll("[data-data-layer]")];
  const uses = [...document.querySelectorAll("[data-data-layer-use]")];
  const descriptions = [...document.querySelectorAll("[data-data-layer-description]")];
  const tables = [...document.querySelectorAll("main .table-wrap table")];
  const table = tables[0] ?? null;
  const tableWrapper = table?.closest(".table-wrap") ?? null;
  const downloadRows = [...(table?.querySelectorAll("tbody > tr") ?? [])].map((row) => {
    const cells = [...row.querySelectorAll(":scope > td")];
    const l0Link = cells[2]?.querySelector("a[download]") ?? null;
    const l1Link = cells[3]?.querySelector("a[download]") ?? null;
    const l1Action = l1Link ?? cells[3]?.querySelector("[data-pages-unavailable]") ?? null;
    return {
      name: compact(cells[0]?.innerText),
      period: compact(cells[1]?.innerText),
      l0Href: l0Link?.getAttribute("href") ?? null,
      l0Size: compact(cells[2]?.querySelector(".size")?.innerText),
      l1Href: l1Link?.getAttribute("href") ?? null,
      l1Size: compact(cells[3]?.querySelector(".size")?.innerText),
      l1Label: compact(l1Action?.innerText),
      rowInspection: inspect(row, tableWrapper),
      downloadInspections: [inspect(l0Link, tableWrapper), inspect(l1Action, tableWrapper)],
      downloadAccessibleTexts: [null, null],
    };
  });
  const boundaries = [...document.querySelectorAll("main .note")].filter((element) =>
    compact(element.innerText).includes("L2 不發布，理由不是檔案太大")
  );
  const register = registers[0] ?? null;
  const lede = document.querySelector("main .chapter-intro .lede");
  const licensing = [...document.querySelectorAll("main h2")].find((element) =>
    compact(element.innerText) === "授權與再散布"
  ) ?? null;
  return {
    mode,
    counts: {
      taskRegisters: taskRegisters.length,
      schemaRegisters: schemaRegisters.length,
      registers: registers.length,
      terms: terms.length,
      uses: uses.length,
      descriptions: descriptions.length,
      tables: tables.length,
      bodyRows: table?.querySelectorAll("tbody > tr").length ?? 0,
      downloads: document.querySelectorAll("main a[download]").length,
      unavailable: document.querySelectorAll("[data-pages-unavailable]").length,
      l2Downloads: document.querySelectorAll(
        '[data-data-layer="L2"] a[download], [data-data-layer="L2"] + [data-data-layer-description="L2"] a[download]'
      ).length,
      boundaries: boundaries.length,
    },
    register: inspect(register),
    layers: terms.map((term) => {
      const level = term.getAttribute("data-data-layer") ?? "";
      const use = term.querySelector("[data-data-layer-use]");
      const description = document.querySelector(
        '[data-data-layer-description="' + CSS.escape(level) + '"]'
      );
      return {
        level,
        term: compact(term.querySelector("[data-data-layer-term]")?.innerText),
        useText: compact(use?.innerText),
        accessibleUse: null,
        descriptionText: compact(description?.innerText),
        termInspection: inspect(term.querySelector("[data-data-layer-term]")),
        useInspection: inspect(use),
        descriptionInspection: inspect(description),
      };
    }),
    table: inspect(table, tableWrapper),
    downloadRows,
    tableWrapper: tableWrapper ? {
      inspection: inspect(tableWrapper, null, true),
      clientWidth: tableWrapper.clientWidth,
      scrollWidth: tableWrapper.scrollWidth,
      overflowX: getComputedStyle(tableWrapper).overflowX,
    } : null,
    l2BoundaryText: compact(boundaries[0]?.innerText),
    l2Boundary: inspect(boundaries[0] ?? null),
    landmarks: {
      lede: inspect(lede),
      primary: inspect(taskRegisters[0] ?? null),
      register: inspect(register),
      table: inspect(table, tableWrapper),
      licensing: inspect(licensing),
      l2Boundary: inspect(boundaries[0] ?? null),
    },
    viewport: { width: innerWidth, height: innerHeight },
    document: {
      clientWidth: document.documentElement.clientWidth,
      scrollWidth: document.documentElement.scrollWidth,
    },
  };
})()`;

const explorerGuidedWorkspaceSnapshotExpression = (mode) => `(() => {
  const mode = ${JSON.stringify(mode)};
  const compact = (value) => String(value ?? "").replace(/\\s+/g, " ").trim();
  const allElements = [...document.querySelectorAll("main *")];
  const sourceIndex = (element) => element ? allElements.indexOf(element) : -1;
  const clippingOverflow = new Set(["auto", "clip", "hidden", "scroll"]);
  const inspect = (element) => {
    if (!element) return null;
    const rect = element.getBoundingClientRect();
    const ownStyle = getComputedStyle(element);
    let rendered = rect.width > 0 && rect.height > 0;
    let opacity = 1;
    let hidden = false;
    let ariaHidden = false;
    let inert = false;
    let detailsAncestor = false;
    let cssClip = false;
    let cssClipPath = false;
    let visibleLeft = rect.left;
    let visibleRight = rect.right;
    let visibleTop = rect.top;
    let visibleBottom = rect.bottom;
    for (let node = element; node; node = node.parentElement) {
      const style = getComputedStyle(node);
      const nodeOpacity = Number(style.opacity);
      if (
        style.display === "none" || style.visibility === "hidden" ||
        style.visibility === "collapse" || !Number.isFinite(nodeOpacity) || nodeOpacity <= 0
      ) rendered = false;
      if (Number.isFinite(nodeOpacity)) opacity *= nodeOpacity;
      hidden ||= node.hasAttribute("hidden");
      ariaHidden ||= node.getAttribute("aria-hidden") === "true";
      inert ||= node.hasAttribute("inert");
      detailsAncestor ||= node instanceof HTMLDetailsElement;
      cssClip ||= style.clip !== "auto";
      cssClipPath ||= style.clipPath !== "none";
      if (node !== element) {
        const bounds = node.getBoundingClientRect();
        if (clippingOverflow.has(style.overflowX)) {
          visibleLeft = Math.max(visibleLeft, bounds.left);
          visibleRight = Math.min(visibleRight, bounds.right);
        }
        if (clippingOverflow.has(style.overflowY)) {
          visibleTop = Math.max(visibleTop, bounds.top);
          visibleBottom = Math.min(visibleBottom, bounds.bottom);
        }
      }
    }
    return {
      display: ownStyle.display,
      visibility: ownStyle.visibility,
      rendered,
      hidden,
      ariaHidden,
      inert,
      accessible: rendered && !hidden && !ariaHidden && !inert,
      opacity,
      top: rect.top,
      right: rect.right,
      bottom: rect.bottom,
      left: rect.left,
      width: rect.width,
      height: rect.height,
      sourceIndex: sourceIndex(element),
      cssOrder: Number(ownStyle.order) || 0,
      selfOverflowX: clippingOverflow.has(ownStyle.overflowX)
        ? Math.max(0, element.scrollWidth - element.clientWidth) : 0,
      selfOverflowY: clippingOverflow.has(ownStyle.overflowY)
        ? Math.max(0, element.scrollHeight - element.clientHeight) : 0,
      ancestorClipped:
        visibleRight - visibleLeft < rect.width - 1 ||
        visibleBottom - visibleTop < rect.height - 1,
      cssClip,
      cssClipPath,
      detailsAncestor,
    };
  };
  const workspaces = [...document.querySelectorAll("[data-explorer-workspace]")];
  const paths = [...document.querySelectorAll("[data-explorer-path]")];
  const steps = [...document.querySelectorAll("[data-explorer-step]")];
  const controls = [...document.querySelectorAll("[data-explorer-controls]")];
  const tables = [...document.querySelectorAll("[data-explorer-tables]")];
  const results = [...document.querySelectorAll("[data-explorer-result]")];
  const caveats = [...document.querySelectorAll("[data-explorer-caveat]")];
  const noJsNotices = [...document.querySelectorAll("[data-explorer-nojs]")];
  const workspace = workspaces[0] ?? null;
  const run = document.querySelector("#run");
  const status = document.querySelector("#status");
  const tableInventory = tables[0] ?? null;
  const result = results[0] ?? null;
  const caveat = caveats[0] ?? null;
  const noJs = noJsNotices[0] ?? null;
  return {
    mode,
    state: mode === "no-js" ? "no-js" : workspace?.dataset.explorerState ?? "",
    counts: {
      workspace: workspaces.length,
      paths: paths.length,
      steps: steps.length,
      controls: controls.length,
      tables: tables.length,
      results: results.length,
      caveats: caveats.length,
    },
    steps: steps.map((step) => ({
      key: step.getAttribute("data-explorer-step") ?? "",
      title: compact(step.querySelector(":scope > strong")?.innerText),
      text: compact(step.querySelector(":scope > span:last-child")?.innerText),
      accessibleText: null,
      inspection: inspect(step),
    })),
    run: {
      disabled: run instanceof HTMLButtonElement ? run.disabled : null,
      accessibleText: null,
      inspection: inspect(run),
    },
    status: {
      text: compact(status?.innerText),
      busy: status?.getAttribute("data-busy") === "true",
      failed: status?.getAttribute("data-failed") === "true",
      inspection: inspect(status),
    },
    tables: {
      text: compact(tableInventory?.innerText),
      inspection: inspect(tableInventory),
    },
    result: {
      text: compact(result?.innerText),
      hasRows: Boolean(result?.querySelector("tbody > tr")),
      emptyMessage: Boolean(result?.querySelector(".no-rows")),
      errorDetail: result?.querySelector(".error")?.textContent ?? null,
      inspection: inspect(result),
      focused: document.activeElement === result,
    },
    caveat: {
      text: compact(caveat?.innerText),
      inspection: inspect(caveat),
    },
    noJs: {
      text: compact(noJs?.innerText),
      inspection: inspect(noJs),
    },
    document: {
      clientWidth: document.documentElement.clientWidth,
      scrollWidth: document.documentElement.scrollWidth,
    },
  };
})()`;

function textZoomRouteMatrixProblems(routes = TEXT_ZOOM_ROUTES) {
  return ["/data/", "/detection/", "/explore/", "/forecast/", "/health/", "/methods/"]
    .filter((route) => !routes.includes(route))
    .map((route) => `200% text-zoom route matrix does not exercise ${route}`);
}

function englishClaimPattern(phrase) {
  const escaped = phrase
    .replace(/[.*+?^${}()|[\]\\]/gu, "\\$&")
    .replace(/\s+/gu, "\\s+");
  return new RegExp(`(?<![A-Za-z0-9_])${escaped}(?![A-Za-z0-9_])`, "iu");
}

const ENGLISH_SOURCE_NONATTRIBUTION_PATTERN = new RegExp(
  [
    String.raw`(?:\b(?:do|does)\s+not\s+(?:identify|establish)|`,
    String.raw`\bcannot\s+(?:identify|establish)|\bnot)[^.!?。！？]{0,80}`,
    String.raw`\bsource(?:'s)? identit(?:y|ies)\b[^.!?。！？]{0,160}`,
    String.raw`\b(?:position|location)\b[^.!?。！？]{0,160}`,
    String.raw`\b(?:transport|travel)[- ]distance\b[^.!?。！？]{0,160}\bcontribution\b`,
  ].join(""),
  "iu",
);
const ENGLISH_SOURCE_BOUNDARY_PATTERNS = [ENGLISH_SOURCE_NONATTRIBUTION_PATTERN];
const ENGLISH_OBSERVED_WIND_PATTERNS = [
  /\b(?:observed|measured|observations?)\b/iu,
  /\bhigh[- ]value\b/iu,
  /\bwind[- ]speed\b/iu,
  /\bdirection\b/iu,
];
const ENGLISH_NONCAUSAL_M5_PATTERN = new RegExp(
  [
    String.raw`(?:\b(?:not|cannot)\b[^.!?。！？]{0,50}\bidentif(?:y|ied)\b`,
    String.raw`[^.!?。！？]{0,50}\bcausal\b[^.!?。！？]{0,30}\bpolicy\b`,
    String.raw`[^.!?。！？]{0,30}\beffects?\b|\bcausal\b[^.!?。！？]{0,30}\bpolicy\b`,
    String.raw`[^.!?。！？]{0,30}\beffects?\b[^.!?。！？]{0,50}\bnot\b`,
    String.raw`[^.!?。！？]{0,50}\bidentif(?:y|ied)\b)`,
  ].join(""),
  "iu",
);
const ENGLISH_M5_BOUNDARY_PATTERNS = [
  /\bmarked[- ]window\b/iu,
  /\bobserved[- ]minus[- ]predicted\b/iu,
  ENGLISH_NONCAUSAL_M5_PATTERN,
];
const CJK_NONCAUSAL_M5_PATTERN =
  /(?:不是|不能|無法)[^。！？]{0,40}識別[^。！？]{0,40}政策因果效應/u;
const ENGLISH_PM_RATIO_NONATTRIBUTION_PATTERN = new RegExp(
  [
    String.raw`\b(?:cannot|does not|do not)\b[^.!?。！？]{0,50}\buniquely\b`,
    String.raw`[^.!?。！？]{0,50}\b(?:identify|distinguish)\b[^.!?。！？]{0,80}`,
    String.raw`\bquantify\b[^.!?。！？]{0,50}\b(?:a\s+)?source\b`,
  ].join(""),
  "iu",
);
const ENGLISH_PM_RATIO_BOUNDARY_PATTERNS = [
  /\bparticle[- ]size\b/iu,
  /\b(?:composition|makeup)\b/iu,
  /\b(?:screen|evaluate)\b/iu,
  /\bsource hypotheses\b/iu,
  ENGLISH_PM_RATIO_NONATTRIBUTION_PATTERN,
];

const NEGATION_SCOPE_MODIFIERS = new Set([
  "alone",
  "by",
  "directly",
  "independently",
  "itself",
  "merely",
  "reliably",
  "themselves",
  "uniquely",
]);

function containsOnlyNegationModifiers(text) {
  const words = text.match(/[A-Za-z]+/gu) ?? [];
  return words.every((word) => NEGATION_SCOPE_MODIFIERS.has(word.toLowerCase()));
}

function actionIsDirectlyNegated(prefix) {
  const negators = [...prefix.matchAll(/\b(?:cannot|can't|not)\b/giu)];
  if (!negators.length) return false;
  const last = negators.at(-1);
  const between = prefix.slice((last.index ?? 0) + last[0].length);
  return containsOnlyNegationModifiers(between);
}

function actionInheritsCoordinatedNegation(betweenActions, previousActionWasNegated) {
  if (!previousActionWasNegated) return false;
  const coordinators = [...betweenActions.matchAll(/\b(?:or|nor)\b/giu)];
  if (!coordinators.length) return false;
  const last = coordinators.at(-1);
  const afterCoordinator = betweenActions.slice((last.index ?? 0) + last[0].length);
  return containsOnlyNegationModifiers(afterCoordinator);
}

const CJK_NEGATION_MODIFIERS = /^(?:(?:唯一|直接|獨立|單獨|自行|明確|進一步|再|已|被|能夠|能|可|得))*$/u;
const CJK_COORDINATED_NEGATION_MODIFIERS =
  /^(?:(?:唯一|直接|獨立|單獨|自行|明確|進一步|再|已))*$/u;

function cjkActionIsDirectlyNegated(prefix) {
  const negators = [
    ...prefix.matchAll(/(?:不能|無法|不是|並非|沒有|不得|不可|未能|未|不|非)/gu),
  ];
  if (!negators.length) return false;
  const last = negators.at(-1);
  const between = prefix
    .slice((last.index ?? 0) + last[0].length)
    .replace(/[\s\u3000]+/gu, "");
  if (!CJK_NEGATION_MODIFIERS.test(between)) return false;

  let scopedNegatorCount = 1;
  let currentNegatorStart = last.index ?? 0;
  for (let index = negators.length - 2; index >= 0; index -= 1) {
    const candidate = negators[index];
    const separation = prefix
      .slice((candidate.index ?? 0) + candidate[0].length, currentNegatorStart)
      .replace(/[\s\u3000]+/gu, "");
    if (!CJK_NEGATION_MODIFIERS.test(separation)) break;
    scopedNegatorCount += 1;
    currentNegatorStart = candidate.index ?? 0;
  }
  return scopedNegatorCount % 2 === 1;
}

function cjkActionInheritsCoordinatedNegation(
  betweenActions,
  previousActionWasNegated,
) {
  if (!previousActionWasNegated) return false;
  const coordinators = [...betweenActions.matchAll(/(?:或者|或)/gu)];
  if (!coordinators.length) return false;
  const last = coordinators.at(-1);
  const afterCoordinator = betweenActions
    .slice((last.index ?? 0) + last[0].length)
    .replace(/[\s\u3000]+/gu, "");
  return CJK_COORDINATED_NEGATION_MODIFIERS.test(afterCoordinator);
}

function hasAffirmativeAttribution(
  text,
  {
    actions,
    objects,
    affirmativePatterns = [],
    directlyNegated = actionIsDirectlyNegated,
    inheritsCoordinatedNegation = actionInheritsCoordinatedNegation,
  },
) {
  const clauses = text.split(
    /[.!?。！？;；]|\r?\n(?=[ \t]*\|)|\b(?:but|however|whereas|although|though|yet)\b|(?:但是|但|然而|可是|不過|不过|卻|却)/iu,
  );
  return clauses.some((clause) => {
    if (affirmativePatterns.some((pattern) => pattern.test(clause))) return true;
    const flags = actions.flags.includes("g") ? actions.flags : `${actions.flags}g`;
    let previousActionEnd = null;
    let previousActionWasNegated = false;
    for (const action of clause.matchAll(new RegExp(actions.source, flags))) {
      const start = action.index ?? 0;
      const proposition = clause.slice(start, start + 240);
      if (!objects.test(proposition)) continue;
      const actionDirectlyNegated = directlyNegated(clause.slice(0, start));
      const betweenActions =
        previousActionEnd === null ? "" : clause.slice(previousActionEnd, start);
      const actionIsNegated =
        actionDirectlyNegated ||
        inheritsCoordinatedNegation(betweenActions, previousActionWasNegated);
      if (!actionIsNegated) return true;
      previousActionEnd = start + action[0].length;
      previousActionWasNegated = actionIsNegated;
    }
    return false;
  });
}

const M7_AFFIRMATIVE_ATTRIBUTION_REJECTS = [
  {
    description: "affirmative source identity, position, distance, or contribution attribution",
    test: (text) =>
      hasAffirmativeAttribution(text, {
        actions:
          /\b(?:identif(?:y|ies)|establish(?:es)?|locat(?:e|es)|attribut(?:e|es)|determin(?:e|es)|infer(?:s)?|quantif(?:y|ies))\b/iu,
        objects:
          /\b(?:source(?:'s)? identit(?:y|ies)|position|location|(?:transport|travel)[- ]distance|contribution)\b/iu,
      }),
  },
];
const M5_AFFIRMATIVE_CAUSAL_REJECTS = [
  {
    description: "affirmative causal policy-effect attribution",
    test: (text) =>
      hasAffirmativeAttribution(text, {
        actions:
          /\b(?:identif(?:y|ies)|establish(?:es)?|demonstrat(?:e|es)|show(?:s)?|prov(?:e|es))\b/iu,
        objects: /\bcausal\s+policy\s+effects?\b/iu,
        affirmativePatterns: [
          /\b(?:is|are|was|were|constitutes?|represents?)\b(?:(?!\bnot\b)[^.!?。！？;；]){0,100}\bcausal\s+policy\s+effects?\b/iu,
        ],
      }),
  },
];
const PM_RATIO_AFFIRMATIVE_ATTRIBUTION_REJECTS = [
  {
    description: "affirmative source identification or quantification",
    test: (text) =>
      hasAffirmativeAttribution(text, {
        actions:
          /\b(?:identif(?:y|ies)|distinguish(?:es)?|quantif(?:y|ies)|attribut(?:e|es))\b/iu,
        objects: /\b(?:(?:a|an|one|the)\s+)?source\b/iu,
      }),
  },
];
const CJK_M7_AFFIRMATIVE_ATTRIBUTION_REJECTS = [
  {
    description: "affirmative Chinese source identity, position, distance, or contribution attribution",
    test: (text) =>
      hasAffirmativeAttribution(text, {
        actions: /(?:識別|辨識|定位|判定|推斷|歸因|量化)/u,
        objects: /(?:來源身分|來源身份|位置|傳輸距離|貢獻)/u,
        directlyNegated: cjkActionIsDirectlyNegated,
        inheritsCoordinatedNegation: cjkActionInheritsCoordinatedNegation,
      }),
  },
];
const CJK_M7_SPACE_AFFIRMATIVE_SOURCE_REJECTS = [
  ...CJK_M7_AFFIRMATIVE_ATTRIBUTION_REJECTS,
  {
    description: "affirmative Chinese pollution-source classification",
    test: (text) =>
      hasAffirmativeAttribution(text, {
        actions: /是(?=[\s\u3000]*(?:一個|某個)?[\s\u3000]*污染來源(?:[\s\u3000]*(?:$|[，,])))/u,
        objects: /污染來源/u,
        directlyNegated: cjkActionIsDirectlyNegated,
        inheritsCoordinatedNegation: cjkActionInheritsCoordinatedNegation,
      }),
  },
];
const CJK_M7_SPACE_CLAIM_REQUIREMENT = {
  description: "observed CBPF peak-wind groups are not source classifications",
  patterns: [/CBPF/u, /高值/u, /觀測/u, /風速/u, /不是污染來源/u],
};
const CJK_M5_AFFIRMATIVE_CAUSAL_REJECTS = [
  {
    description: "affirmative Chinese causal policy-effect attribution",
    test: (text) =>
      hasAffirmativeAttribution(text, {
        actions: /(?:識別|辨識|證明|顯示|判定|是|屬於|構成|代表)/u,
        objects: /政策(?:的)?因果效應/u,
        directlyNegated: cjkActionIsDirectlyNegated,
        inheritsCoordinatedNegation: cjkActionInheritsCoordinatedNegation,
      }),
  },
];
const CJK_PM_RATIO_AFFIRMATIVE_ATTRIBUTION_REJECTS = [
  {
    description: "affirmative Chinese source identification or quantification",
    test: (text) =>
      hasAffirmativeAttribution(text, {
        actions: /(?:識別|辨識|量化|歸因)/u,
        objects: /來源/u,
        directlyNegated: cjkActionIsDirectlyNegated,
        inheritsCoordinatedNegation: cjkActionInheritsCoordinatedNegation,
      }),
  },
];

function claimRegion(text, claim) {
  if (!claim) return { text, problem: null };
  const start = text.indexOf(claim.start);
  if (start < 0) {
    return {
      text: "",
      problem: `missing claim-region start ${JSON.stringify(claim.start)}`,
    };
  }
  if (text.indexOf(claim.start, start + claim.start.length) >= 0) {
    return {
      text: "",
      problem: `ambiguous claim-region start ${JSON.stringify(claim.start)}`,
    };
  }
  const contentStart = start + claim.start.length;
  const end = claim.end ? text.indexOf(claim.end, contentStart) : text.length;
  if (end < 0) {
    return {
      text: "",
      problem: `missing claim-region end ${JSON.stringify(claim.end)}`,
    };
  }
  return { text: text.slice(contentStart, end), problem: null };
}

function claimSurfaceProblems({
  path,
  label,
  text,
  retired,
  claim,
  required = [],
  affirmativeRejects = [],
}) {
  const retiredProblems = retired.filter((pattern) => pattern.test(text))
    .map((pattern) => `${path}: contains retired ${label} claim ${pattern}`);
  const affirmativeRejectProblems = affirmativeRejects
    .filter((reject) => reject.test(text))
    .map((reject) => `${path}: ${label} surface contains ${reject.description}`);
  const requiredProblems = required.flatMap((requirement) => {
    const region = claimRegion(text, requirement.claim ?? claim);
    if (region.problem) return [`${path}: ${label} ${region.problem}`];
    const missing = requirement.patterns.filter((pattern) => !pattern.test(region.text));
    return missing.length
      ? [
        `${path}: ${label} claim region lacks ${requirement.description}: ` +
          missing.join(", "),
      ]
      : [];
  });
  return retiredProblems.concat(affirmativeRejectProblems, requiredProblems);
}

function repositoryClaimBoundaryProblems(surfaceTextOverrides = new Map()) {
  const trackedPaths = execFileSync("git", ["ls-files", "-z"], { encoding: "buffer" })
    .toString("utf8")
    .split("\0")
    .filter(Boolean);
  const tracked = new Set(trackedPaths);
  const inspect = ({ label, surfaces, retired, required, affirmativeRejects }) =>
    surfaces.flatMap((path) => {
      if (!tracked.has(path)) {
        return [`${path}: ${label} claim-boundary surface is not tracked`];
      }
      const surfaceAffirmativeRejects = affirmativeRejects[path] ?? [];
      const wiringProblems = surfaceAffirmativeRejects.length
        ? []
        : [`${path}: ${label} claim-boundary surface lacks affirmative rejects`];
      const text = surfaceTextOverrides.get(path) ?? readFileSync(path, "utf8");
      return wiringProblems.concat(
        claimSurfaceProblems({
          path,
          label,
          text,
          retired,
          required: required[path],
          affirmativeRejects: surfaceAffirmativeRejects,
        }),
      );
    });

  const m7Surfaces = [
    "src/twair/models/deploy.py",
    "src/twair/status.py",
    "README.md",
    "README.en.md",
    "spaces/forecast/app.py",
    "spaces/forecast/README.md",
    "web/src/components/ChapterSources.astro",
  ];
  const m7Problems = inspect({
    label: "M7",
    surfaces: m7Surfaces,
    retired: [
      englishClaimPattern("transport-dominated"),
      englishClaimPattern("locally-dominated"),
      englishClaimPattern("source direction"),
      /污染來源方位/u,
      /污染源方位/u,
      /本地型/u,
      /傳輸型/u,
      /近處的污染源只有在風弱時才顯現/u,
    ],
    affirmativeRejects: {
      "src/twair/models/deploy.py": M7_AFFIRMATIVE_ATTRIBUTION_REJECTS,
      "src/twair/status.py": M7_AFFIRMATIVE_ATTRIBUTION_REJECTS,
      "README.md": CJK_M7_AFFIRMATIVE_ATTRIBUTION_REJECTS,
      "README.en.md": M7_AFFIRMATIVE_ATTRIBUTION_REJECTS,
      "spaces/forecast/app.py": CJK_M7_SPACE_AFFIRMATIVE_SOURCE_REJECTS,
      "spaces/forecast/README.md": CJK_M7_SPACE_AFFIRMATIVE_SOURCE_REJECTS,
      "web/src/components/ChapterSources.astro": [
        ...M7_AFFIRMATIVE_ATTRIBUTION_REJECTS,
        ...CJK_M7_AFFIRMATIVE_ATTRIBUTION_REJECTS,
      ],
    },
    required: {
      "src/twair/models/deploy.py": [
        {
          claim: { start: "# Six stations chosen", end: "DEMO_STATIONS" },
          description: "measured high/low-wind station mapping and attribution boundary",
          patterns: [
            /measured/iu,
            /富貴角[\s\S]*馬公[\s\S]*high-wind/iu,
            /忠明[\s\S]*前金[\s\S]*潮州[\s\S]*埔里[\s\S]*low-wind/iu,
            ...ENGLISH_SOURCE_BOUNDARY_PATTERNS,
          ],
        },
      ],
      "src/twair/status.py": [
        {
          claim: { start: '"m7_sources",', end: 'Module("m9_forecast"' },
          description: "observed high-value wind pattern and attribution boundary",
          patterns: [
            ...ENGLISH_OBSERVED_WIND_PATTERNS,
            ...ENGLISH_SOURCE_BOUNDARY_PATTERNS,
          ],
        },
      ],
      "README.md": [
        {
          claim: { start: "| 🌐 **互動網站**", end: "| 🔮 **預測 demo**" },
          description: "dashboard M7 observed high-value wind boundary",
          patterns: [
            /高值時段/u,
            /風速/u,
            /風向/u,
            /不識別來源身分/u,
            /位置/u,
            /傳輸距離/u,
            /貢獻/u,
          ],
        },
        {
          claim: { start: "| Phase 5 |", end: "| Phase 6 |" },
          description: "Phase 5 M7 observed high-value wind boundary",
          patterns: [
            /高值時段/u,
            /風速/u,
            /風向/u,
            /不識別來源身分/u,
            /位置/u,
            /傳輸距離/u,
            /貢獻/u,
          ],
        },
      ],
      "README.en.md": [
        {
          claim: { start: "| 🌐 **Interactive Dashboard**", end: "| 🔮 **Forecast Demo**" },
          description: "dashboard M7 observed high-value wind boundary",
          patterns: [
            ...ENGLISH_OBSERVED_WIND_PATTERNS,
            ...ENGLISH_SOURCE_BOUNDARY_PATTERNS,
          ],
        },
        {
          claim: { start: "| **Phase 5** |", end: "| **Phase 6** |" },
          description: "Phase 5 M7 observed high-value wind boundary",
          patterns: [
            ...ENGLISH_OBSERVED_WIND_PATTERNS,
            ...ENGLISH_SOURCE_BOUNDARY_PATTERNS,
          ],
        },
      ],
      "spaces/forecast/app.py": [
        {
          ...CJK_M7_SPACE_CLAIM_REQUIREMENT,
          claim: { start: "六個測站涵蓋不同監測情境", end: "盆地（空氣容易滯留）。" },
        },
      ],
      "spaces/forecast/README.md": [
        {
          ...CJK_M7_SPACE_CLAIM_REQUIREMENT,
          claim: { start: "六個測站涵蓋不同監測情境", end: "盆地（空氣容易滯留）。" },
        },
      ],
      "web/src/components/ChapterSources.astro": [
        {
          claim: { start: "What shipped was", end: "`<noscript>` with a `<style>`" },
          description: "no-JavaScript mismatch why and observed wind attribution boundary",
          patterns: [
            /without JavaScript/iu,
            /mismatch/iu,
            ...ENGLISH_OBSERVED_WIND_PATTERNS,
            ...ENGLISH_SOURCE_BOUNDARY_PATTERNS,
          ],
        },
      ],
    },
  });

  const m5Problems = inspect({
    label: "M5",
    surfaces: ["src/twair/cli.py", "src/twair/analysis/causal.py", "docs/methodology.md"],
    retired: [
      englishClaimPattern("did the policy do anything"),
      englishClaimPattern("median effect"),
      /(?<![A-Za-z0-9_])(?:years?\s+)?when\s+nothing\s+happened(?![A-Za-z0-9_])/iu,
      englishClaimPattern("shows an effect"),
      /沒有事件的年份/u,
      /沒有封城的年份/u,
      /效應中位數/u,
    ],
    affirmativeRejects: {
      "src/twair/cli.py": M5_AFFIRMATIVE_CAUSAL_REJECTS,
      "src/twair/analysis/causal.py": M5_AFFIRMATIVE_CAUSAL_REJECTS,
      "docs/methodology.md": CJK_M5_AFFIRMATIVE_CAUSAL_REJECTS,
    },
    required: {
      "src/twair/cli.py": [
        {
          claim: { start: '@analysis_app.command("m5")', end: '@analysis_app.command("m3")' },
          description: "marked-window contrast, unmarked-control yardstick, and non-causal boundary",
          patterns: [
            ...ENGLISH_M5_BOUNDARY_PATTERNS,
            /unmarked/iu,
            /control/iu,
            /spread/iu,
            /median/iu,
            /not detected/iu,
            /not as zero/iu,
          ],
        },
      ],
      "src/twair/analysis/causal.py": [
        {
          claim: { start: '"""M5', end: '"""' },
          description: "marked-window contrast, same-calendar controls, and non-causal boundary",
          patterns: [
            ...ENGLISH_M5_BOUNDARY_PATTERNS,
            /unmarked/iu,
            /same-calendar/iu,
            /control windows/iu,
            /not an identified causal policy effect/iu,
          ],
        },
        {
          claim: { start: "def placebo_distribution(", end: "def _segmented_slopes(" },
          description: "unmarked same-calendar control semantics",
          patterns: [
            /unmarked/iu,
            /same-calendar/iu,
            /nothing else happened/iu,
            /absence of this[\s\S]{0,60}event label/iu,
          ],
        },
      ],
      "docs/methodology.md": [
        {
          claim: {
            start: "### 4. 第二個答案：能不能把標記窗口訊號從背景變異中分出來",
            end: "## D9: GUI 工具不可重現",
          },
          description: "implemented marked-window contrast and non-causal interpretation",
          patterns: [
            /\[[^\]]*causal\.py\]\(\.\.\/src\/twair\/analysis\/causal\.py\)/u,
            /標記窗口/u,
            /觀測－預測差額/u,
            /未標記/u,
            /同日曆控制窗口/u,
            CJK_NONCAUSAL_M5_PATTERN,
          ],
        },
        {
          claim: {
            start: "### 4. 第二個答案：能不能把標記窗口訊號從背景變異中分出來",
            end: "## D9: GUI 工具不可重現",
          },
          description: "window-control versus candidate-break reference distributions",
          patterns: [
            /參考分布均值/u,
            /參考分布 SD/u,
            /前兩列/u,
            /未標記/u,
            /同日曆控制窗口/u,
            /斜率差列/u,
            /同一正規化序列/u,
            /其他候選斷點月份/u,
            /COVID-19[^\n]*μg\/m³/u,
            /台中電廠[^\n]*μg\/m³/u,
            /2018 空污法修正[^\n]*μg\/m³\/年/u,
          ],
        },
      ],
    },
  });

  const pmRatioProblems = inspect({
    label: "PM-ratio",
    surfaces: [
      "src/twair/features/chem.py",
      "docs/methodology.md",
      "tests/test_drivers.py",
    ],
    retired: [
      englishClaimPattern("source fingerprint"),
      englishClaimPattern("emission fingerprint"),
      /來源指紋/u,
    ],
    affirmativeRejects: {
      "src/twair/features/chem.py": PM_RATIO_AFFIRMATIVE_ATTRIBUTION_REJECTS,
      "docs/methodology.md": CJK_PM_RATIO_AFFIRMATIVE_ATTRIBUTION_REJECTS,
      "tests/test_drivers.py": PM_RATIO_AFFIRMATIVE_ATTRIBUTION_REJECTS,
    },
    required: {
      "src/twair/features/chem.py": [
        {
          claim: {
            start: 'if {"PM2.5", "PM10"} <= columns:',
            end: 'if {"SO2", "NOx"} <= columns:',
          },
          description: "PM ratio composition screen and nonunique-source boundary",
          patterns: [
            ...ENGLISH_PM_RATIO_BOUNDARY_PATTERNS,
            /pm_ratio/u,
          ],
        },
      ],
      "docs/methodology.md": [
        {
          claim: { start: "## D2:", end: "## D3:" },
          description: "PM ratio composition screen and independent source evidence",
          patterns: [
            /\\text\{PM\}_\{2\.5\}\/\\text\{PM\}_\{10\}/u,
            /粒徑組成指標/u,
            /篩選來源假說/u,
            /不能唯一辨識或量化來源/u,
            /chemical speciation/iu,
            /受體模式/u,
            /軌跡/u,
            /擴散/u,
            /排放清冊/u,
          ],
        },
      ],
      "tests/test_drivers.py": [
        {
          claim: {
            start: "def test_pm_ratio_is_available_even_though_pm10_is_not_a_predictor(",
            end: 'root = _store(tmp_path)',
          },
          description: "PM ratio test composition screen, predictor, and source boundary",
          patterns: [
            ...ENGLISH_PM_RATIO_BOUNDARY_PATTERNS,
            /not a PM2\.5 predictor/iu,
          ],
        },
      ],
    },
  });

  return m7Problems.concat(m5Problems, pmRatioProblems);
}

function editorialHomepageLayoutProblems({ mode, opening, routes, map, postMap, viewport }) {
  const problems = [];
  for (const [name, rect] of Object.entries({ opening, routes, map, postMap })) {
    if (
      !rect || !["top", "right", "bottom", "left", "width", "height"]
        .every((key) => Number.isFinite(rect[key]))
    ) {
      problems.push(`homepage editorial ${name} geometry is missing`);
    } else if (!rect.visible || rect.width <= 0 || rect.height <= 0) {
      problems.push(`homepage editorial ${name} is not visible`);
    }
  }
  if (problems.length) return problems;
  if (mode === "wide") {
    if (opening.right > map.left + 1) problems.push("homepage editorial columns overlap");
    if (routes.bottom > viewport.height + 1) {
      problems.push("homepage primary reading block leaves the first viewport");
    }
  } else if (mode === "stacked") {
    if (!(opening.bottom <= map.top + 1 && map.bottom <= postMap.top + 1)) {
      problems.push("homepage editorial mobile stack changed");
    }
  } else {
    problems.push("homepage editorial layout mode is missing");
  }
  return problems;
}

async function lifecycleSelfTest() {
  const markdownRowBoundaryProblems = claimSurfaceProblems({
    path: "synthetic-markdown-table-boundary.md",
    label: "synthetic",
    text: [
      "| 可重現研究 | 逐項修正並量化差異 |",
      "| M7 | 觀測不識別來源身分、位置、傳輸距離或貢獻 |",
    ].join("\n"),
    retired: [],
    affirmativeRejects: CJK_M7_AFFIRMATIVE_ATTRIBUTION_REJECTS,
  });
  if (markdownRowBoundaryProblems.length) {
    throw new Error("surface-wide affirmative rejects cross Markdown table rows");
  }

  const causalSurfacePath = "src/twair/analysis/causal.py";
  const causalSurfaceText = readFileSync(causalSurfacePath, "utf8");
  const trendBreakLimitation =
    "It does not by itself identify the marked event as the cause of that shift.";
  const trendBreakOverclaim = "This analysis demonstrates a causal policy effect.";
  if (!causalSurfaceText.includes(trendBreakLimitation)) {
    throw new Error("the actual M5 trend-break limitation mutation target is missing");
  }
  const causalSurfaceMutationProblems = repositoryClaimBoundaryProblems(
    new Map([
      [
        causalSurfacePath,
        causalSurfaceText.replace(trendBreakLimitation, trendBreakOverclaim),
      ],
    ]),
  );
  const causalSurfaceMutationMatches = causalSurfaceMutationProblems.filter(
    (problem) =>
      problem.startsWith(`${causalSurfacePath}:`) &&
      problem.includes("affirmative causal policy-effect attribution"),
  );
  if (causalSurfaceMutationMatches.length !== 1) {
    throw new Error("the actual M5 trend-break surface bypasses shared affirmative rejects");
  }
  if (causalSurfaceMutationProblems.length !== 1) {
    throw new Error("current claim surfaces trigger unrelated affirmative rejects");
  }

  const chapterSourcesSurfacePath = "web/src/components/ChapterSources.astro";
  const chapterSourcesSurfaceText = readFileSync(chapterSourcesSurfacePath, "utf8");
  const chapterSourcesEnglishBoundary =
    "    source identity or position, transport distance, or contribution.";
  const chapterSourcesEnglishOverclaim =
    "    CBPF identifies source identity, position, transport distance, and contribution.";
  const chapterSourcesPublicBoundary = "  <h2>尖峰風速是觀測分類</h2>";
  const chapterSourcesOverclaim = "<p>但可識別來源身分、位置、傳輸距離與貢獻。</p>";
  if (
    !chapterSourcesSurfaceText.includes(chapterSourcesEnglishBoundary) ||
    !chapterSourcesSurfaceText.includes(chapterSourcesPublicBoundary)
  ) {
    throw new Error("the actual ChapterSources public-region mutation target is missing");
  }
  const chapterSourcesMutationProblems = repositoryClaimBoundaryProblems(
    new Map([
      [
        chapterSourcesSurfacePath,
        chapterSourcesSurfaceText
          .replace(
            chapterSourcesEnglishBoundary,
            `${chapterSourcesEnglishBoundary}\n${chapterSourcesEnglishOverclaim}`,
          )
          .replace(
            chapterSourcesPublicBoundary,
            `${chapterSourcesPublicBoundary}\n\n  ${chapterSourcesOverclaim}`,
          ),
      ],
    ]),
  );
  const chapterSourcesEnglishMutationMatches = chapterSourcesMutationProblems.filter(
    (problem) =>
      problem.startsWith(`${chapterSourcesSurfacePath}:`) &&
      problem.includes("affirmative source identity, position, distance, or contribution attribution"),
  );
  const chapterSourcesChineseMutationMatches = chapterSourcesMutationProblems.filter(
    (problem) =>
      problem.startsWith(`${chapterSourcesSurfacePath}:`) &&
      problem.includes(
        "affirmative Chinese source identity, position, distance, or contribution attribution",
      ),
  );
  if (chapterSourcesEnglishMutationMatches.length !== 1) {
    throw new Error(
      "the actual ChapterSources English technical overclaim bypasses mixed-language M7 rejects",
    );
  }
  if (chapterSourcesChineseMutationMatches.length !== 1) {
    throw new Error(
      "the actual ChapterSources Chinese public overclaim bypasses mixed-language M7 rejects",
    );
  }
  if (chapterSourcesMutationProblems.length !== 2) {
    throw new Error("current claim surfaces trigger unrelated ChapterSources rejects");
  }

  const sourceDirectionPattern = englishClaimPattern("source direction");
  const sourceFingerprintPattern = englishClaimPattern("source fingerprint");
  const englishBoundaryProblems = claimSurfaceProblems({
    path: "synthetic-boundary.txt",
    label: "synthetic",
    text: "resource direction and resource fingerprint",
    retired: [sourceDirectionPattern, sourceFingerprintPattern],
  });
  if (
    englishBoundaryProblems.length ||
    !sourceDirectionPattern.test("source direction") ||
    !sourceFingerprintPattern.test("source fingerprint")
  ) {
    throw new Error("English retired-claim matching crosses token boundaries");
  }

  const scopedFalseGreenProblems = claimSurfaceProblems({
    path: "synthetic-scope.txt",
    label: "synthetic",
    text: [
      "// observed high-value wind-speed/direction patterns",
      "BEGIN CLAIM",
      "high values changed with the weather",
      "END CLAIM",
    ].join("\n"),
    claim: { start: "BEGIN CLAIM", end: "END CLAIM" },
    retired: [],
    required: [
      {
        description: "observed high-value wind pattern",
        patterns: [/observed/iu, /high-value/iu, /wind-speed/iu, /direction/iu],
      },
    ],
  });
  if (!scopedFalseGreenProblems.length) {
    throw new Error("required claim prose can be satisfied by an unrelated comment");
  }

  const preciseParaphraseProblems = claimSurfaceProblems({
    path: "synthetic-paraphrase.txt",
    label: "synthetic",
    text: [
      "BEGIN CLAIM",
      "Measured wind speed and wind direction patterns during high-value hours do not establish",
      "a source's identity, location, travel distance, or contribution.",
      "END CLAIM",
    ].join("\n"),
    claim: { start: "BEGIN CLAIM", end: "END CLAIM" },
    retired: [],
    affirmativeRejects: M7_AFFIRMATIVE_ATTRIBUTION_REJECTS,
    required: [
      {
        description: "observed high-value wind pattern and attribution boundary",
        patterns: [...ENGLISH_OBSERVED_WIND_PATTERNS, ...ENGLISH_SOURCE_BOUNDARY_PATTERNS],
      },
    ],
  });
  if (preciseParaphraseProblems.length) {
    throw new Error("an equally precise claim-boundary paraphrase is rejected");
  }

  const m5ParaphraseProblems = claimSurfaceProblems({
    path: "synthetic-m5-paraphrase.txt",
    label: "synthetic",
    text: [
      "BEGIN CLAIM",
      "The observed minus predicted difference in the marked window is compared with unmarked",
      "control windows. This comparison cannot identify a causal policy effect.",
      "END CLAIM",
    ].join("\n"),
    claim: { start: "BEGIN CLAIM", end: "END CLAIM" },
    retired: [],
    affirmativeRejects: M5_AFFIRMATIVE_CAUSAL_REJECTS,
    required: [
      {
        description: "observational M5 boundary",
        patterns: ENGLISH_M5_BOUNDARY_PATTERNS,
      },
    ],
  });
  const pmRatioParaphraseProblems = claimSurfaceProblems({
    path: "synthetic-pm-paraphrase.txt",
    label: "synthetic",
    text: [
      "BEGIN CLAIM",
      "Particle size makeup can evaluate source hypotheses, but does not uniquely distinguish",
      "or quantify a source.",
      "END CLAIM",
    ].join("\n"),
    claim: { start: "BEGIN CLAIM", end: "END CLAIM" },
    retired: [],
    affirmativeRejects: PM_RATIO_AFFIRMATIVE_ATTRIBUTION_REJECTS,
    required: [
      {
        description: "PM ratio hypothesis-screen boundary",
        patterns: ENGLISH_PM_RATIO_BOUNDARY_PATTERNS,
      },
    ],
  });
  if (m5ParaphraseProblems.length || pmRatioParaphraseProblems.length) {
    throw new Error("an equally precise M5 or PM-ratio boundary paraphrase is rejected");
  }

  const coordinatedNegativeParaphraseProblems = [
    claimSurfaceProblems({
      path: "synthetic-m7-coordinated-negative-paraphrase.txt",
      label: "synthetic",
      text: [
        "BEGIN CLAIM",
        "Observed high-value wind-speed and direction patterns do not identify source identity,",
        "position, transport distance, or quantify contribution.",
        "END CLAIM",
      ].join("\n"),
      claim: { start: "BEGIN CLAIM", end: "END CLAIM" },
      retired: [],
      affirmativeRejects: M7_AFFIRMATIVE_ATTRIBUTION_REJECTS,
      required: [
        {
          description: "observed high-value wind pattern and attribution boundary",
          patterns: [...ENGLISH_OBSERVED_WIND_PATTERNS, ...ENGLISH_SOURCE_BOUNDARY_PATTERNS],
        },
      ],
    }),
    claimSurfaceProblems({
      path: "synthetic-pm-coordinated-negative-paraphrase.txt",
      label: "synthetic",
      text: [
        "BEGIN CLAIM",
        "Particle-size composition can screen source hypotheses but cannot uniquely identify",
        "one source or quantify a source.",
        "END CLAIM",
      ].join("\n"),
      claim: { start: "BEGIN CLAIM", end: "END CLAIM" },
      retired: [],
      affirmativeRejects: PM_RATIO_AFFIRMATIVE_ATTRIBUTION_REJECTS,
      required: [
        {
          description: "PM ratio hypothesis-screen boundary",
          patterns: ENGLISH_PM_RATIO_BOUNDARY_PATTERNS,
        },
      ],
    }),
  ].flat();
  if (coordinatedNegativeParaphraseProblems.length) {
    throw new Error("a coordinated negative attribution paraphrase is rejected");
  }

  const cjkCoordinatedNegativeParaphraseProblems = [
    claimSurfaceProblems({
      path: "synthetic-m7-zh-coordinated-negative-paraphrase.txt",
      label: "synthetic",
      text: "BEGIN CLAIM\n高值時段風速與風向觀測不識別來源身分、位置、傳輸距離或量化貢獻。\nEND CLAIM",
      claim: { start: "BEGIN CLAIM", end: "END CLAIM" },
      retired: [],
      affirmativeRejects: CJK_M7_AFFIRMATIVE_ATTRIBUTION_REJECTS,
      required: [
        {
          description: "Chinese observed wind non-attribution boundary",
          patterns: [/高值時段/u, /風速/u, /風向/u, /不識別來源身分/u, /位置/u, /傳輸距離/u, /貢獻/u],
        },
      ],
    }),
    claimSurfaceProblems({
      path: "synthetic-m5-zh-coordinated-negative-paraphrase.txt",
      label: "synthetic",
      text: "BEGIN CLAIM\n標記窗口比較不能識別或證明政策因果效應。\nEND CLAIM",
      claim: { start: "BEGIN CLAIM", end: "END CLAIM" },
      retired: [],
      affirmativeRejects: CJK_M5_AFFIRMATIVE_CAUSAL_REJECTS,
      required: [
        {
          description: "Chinese observational M5 boundary",
          patterns: [CJK_NONCAUSAL_M5_PATTERN],
        },
      ],
    }),
    claimSurfaceProblems({
      path: "synthetic-pm-zh-coordinated-negative-paraphrase.txt",
      label: "synthetic",
      text: "BEGIN CLAIM\n粒徑組成指標可篩選來源假說，但不能唯一辨識或量化來源。\nEND CLAIM",
      claim: { start: "BEGIN CLAIM", end: "END CLAIM" },
      retired: [],
      affirmativeRejects: CJK_PM_RATIO_AFFIRMATIVE_ATTRIBUTION_REJECTS,
      required: [
        {
          description: "Chinese PM ratio hypothesis-screen boundary",
          patterns: [/粒徑組成指標/u, /篩選來源假說/u, /不能唯一辨識或量化來源/u],
        },
      ],
    }),
  ].flat();
  if (cjkCoordinatedNegativeParaphraseProblems.length) {
    throw new Error("a coordinated Chinese negative attribution paraphrase is rejected");
  }

  const lexicalPrefixNegativeProblems = [
    claimSurfaceProblems({
      path: "synthetic-m7-zh-different-methods-negative.txt",
      label: "synthetic",
      text: "BEGIN CLAIM\n不同方法不能識別來源身分、位置、傳輸距離或量化貢獻。\nEND CLAIM",
      claim: { start: "BEGIN CLAIM", end: "END CLAIM" },
      retired: [],
      affirmativeRejects: CJK_M7_AFFIRMATIVE_ATTRIBUTION_REJECTS,
      required: [
        {
          description: "Chinese lexical-prefix negative attribution boundary",
          patterns: [/不同方法不能識別來源身分/u, /位置/u, /傳輸距離/u, /貢獻/u],
        },
      ],
    }),
    claimSurfaceProblems({
      path: "synthetic-m7-zh-future-data-negative.txt",
      label: "synthetic",
      text: "BEGIN CLAIM\n未來資料不能識別來源身分、位置、傳輸距離或量化貢獻。\nEND CLAIM",
      claim: { start: "BEGIN CLAIM", end: "END CLAIM" },
      retired: [],
      affirmativeRejects: CJK_M7_AFFIRMATIVE_ATTRIBUTION_REJECTS,
      required: [
        {
          description: "Chinese lexical-prefix negative attribution boundary",
          patterns: [/未來資料不能識別來源身分/u, /位置/u, /傳輸距離/u, /貢獻/u],
        },
      ],
    }),
  ].flat();
  const spaceCopularOverclaimProblems = claimSurfaceProblems({
    path: "synthetic-m7-zh-space-copular-overclaim.txt",
    label: "synthetic",
    text: "BEGIN CLAIM\nCBPF 高值時段的風速觀測不是污染來源，但這是污染來源。\nEND CLAIM",
    claim: { start: "BEGIN CLAIM", end: "END CLAIM" },
    retired: [],
    affirmativeRejects: CJK_M7_SPACE_AFFIRMATIVE_SOURCE_REJECTS,
    required: [CJK_M7_SPACE_CLAIM_REQUIREMENT],
  });
  const spaceCopularParaphraseProblems = claimSurfaceProblems({
    path: "synthetic-m7-zh-space-copular-paraphrase.txt",
    label: "synthetic",
    text: "BEGIN CLAIM\nCBPF 高值風速觀測是描述性型態，不是污染來源。\nEND CLAIM",
    claim: { start: "BEGIN CLAIM", end: "END CLAIM" },
    retired: [],
    affirmativeRejects: CJK_M7_SPACE_AFFIRMATIVE_SOURCE_REJECTS,
    required: [CJK_M7_SPACE_CLAIM_REQUIREMENT],
  });
  const roundFourBoundaryFailures = [];
  if (lexicalPrefixNegativeProblems.length) {
    roundFourBoundaryFailures.push("lexical-prefix legitimate negatives are rejected");
  }
  if (!spaceCopularOverclaimProblems.length) {
    roundFourBoundaryFailures.push("the Space copular adversative overclaim is accepted");
  }
  if (spaceCopularParaphraseProblems.length) {
    roundFourBoundaryFailures.push("a precise Space copular-negative paraphrase is rejected");
  }
  if (roundFourBoundaryFailures.length) {
    throw new Error(`round-4 CJK boundary probes failed: ${roundFourBoundaryFailures.join("; ")}`);
  }

  const affirmativeOverclaims = [
    {
      label: "M7",
      text: [
        "Observed high-value wind-speed and direction patterns identify source identity,",
        "position, transport distance, and contribution. This is not merely descriptive.",
      ].join("\n"),
      patterns: [...ENGLISH_OBSERVED_WIND_PATTERNS, ...ENGLISH_SOURCE_BOUNDARY_PATTERNS],
      affirmativeRejects: M7_AFFIRMATIVE_ATTRIBUTION_REJECTS,
    },
    {
      label: "M5",
      text: [
        "The marked-window observed-minus-predicted contrast can identify a causal policy effect.",
        "It is not merely a contrast.",
      ].join("\n"),
      patterns: ENGLISH_M5_BOUNDARY_PATTERNS,
      affirmativeRejects: M5_AFFIRMATIVE_CAUSAL_REJECTS,
    },
    {
      label: "M5-zh",
      text: "標記窗口差額可識別政策因果效應。這不是單純的差額。",
      patterns: [CJK_NONCAUSAL_M5_PATTERN],
      affirmativeRejects: [],
    },
    {
      label: "PM-ratio",
      text: [
        "Particle-size composition can screen source hypotheses and uniquely identify and",
        "quantify a source. This is not merely descriptive.",
      ].join("\n"),
      patterns: ENGLISH_PM_RATIO_BOUNDARY_PATTERNS,
      affirmativeRejects: PM_RATIO_AFFIRMATIVE_ATTRIBUTION_REJECTS,
    },
    {
      label: "M7-mixed-predicate",
      text: [
        "Observed high-value wind-speed and direction patterns do not identify source identity,",
        "but do establish position, transport distance, and contribution.",
      ].join("\n"),
      patterns: [...ENGLISH_OBSERVED_WIND_PATTERNS, ...ENGLISH_SOURCE_BOUNDARY_PATTERNS],
      affirmativeRejects: M7_AFFIRMATIVE_ATTRIBUTION_REJECTS,
    },
    {
      label: "M5-postpositive-negation",
      text: [
        "The marked-window observed-minus-predicted contrast is a causal policy effect not",
        "identified by this model.",
      ].join("\n"),
      patterns: ENGLISH_M5_BOUNDARY_PATTERNS,
      affirmativeRejects: M5_AFFIRMATIVE_CAUSAL_REJECTS,
    },
    {
      label: "PM-ratio-mixed-predicate",
      text: [
        "Particle-size composition can screen source hypotheses and does not uniquely identify",
        "one source, but it can quantify a source.",
      ].join("\n"),
      patterns: ENGLISH_PM_RATIO_BOUNDARY_PATTERNS,
      affirmativeRejects: PM_RATIO_AFFIRMATIVE_ATTRIBUTION_REJECTS,
    },
    {
      label: "M7-zh-mixed-predicate",
      text: [
        "高值時段風速與風向觀測不識別來源身分、位置、傳輸距離或貢獻，",
        "但可識別來源身分、位置、傳輸距離與貢獻。",
      ].join("\n"),
      patterns: [/高值時段/u, /風速/u, /風向/u, /不識別來源身分/u, /位置/u, /傳輸距離/u, /貢獻/u],
      affirmativeRejects: CJK_M7_AFFIRMATIVE_ATTRIBUTION_REJECTS,
    },
    {
      label: "M5-zh-mixed-predicate",
      text: "不是已識別的政策因果效應，但可識別政策因果效應。",
      patterns: [CJK_NONCAUSAL_M5_PATTERN],
      affirmativeRejects: CJK_M5_AFFIRMATIVE_CAUSAL_REJECTS,
    },
    {
      label: "PM-ratio-zh-mixed-predicate",
      text: [
        "PM2.5/PM10 粒徑組成指標可篩選來源假說，不能唯一辨識或量化來源，",
        "但可唯一辨識並量化來源。",
      ].join("\n"),
      patterns: [/粒徑組成指標/u, /篩選來源假說/u, /不能唯一辨識或量化來源/u],
      affirmativeRejects: CJK_PM_RATIO_AFFIRMATIVE_ATTRIBUTION_REJECTS,
    },
    {
      label: "M7-zh-double-negation",
      text: "高值時段風速與風向觀測不是不識別來源身分、位置、傳輸距離或貢獻。",
      patterns: [/高值時段/u, /風速/u, /風向/u, /不識別來源身分/u, /位置/u, /傳輸距離/u, /貢獻/u],
      affirmativeRejects: CJK_M7_AFFIRMATIVE_ATTRIBUTION_REJECTS,
    },
    {
      label: "M5-zh-double-negation",
      text: "不是不能識別政策因果效應。",
      patterns: [CJK_NONCAUSAL_M5_PATTERN],
      affirmativeRejects: CJK_M5_AFFIRMATIVE_CAUSAL_REJECTS,
    },
    {
      label: "PM-ratio-zh-double-negation",
      text: "粒徑組成指標可篩選來源假說，並非不能唯一辨識或量化來源。",
      patterns: [/粒徑組成指標/u, /篩選來源假說/u, /不能唯一辨識或量化來源/u],
      affirmativeRejects: CJK_PM_RATIO_AFFIRMATIVE_ATTRIBUTION_REJECTS,
    },
  ];
  const acceptedOverclaims = [];
  for (const overclaim of affirmativeOverclaims) {
    const problems = claimSurfaceProblems({
      path: `synthetic-${overclaim.label}-overclaim.txt`,
      label: "synthetic",
      text: `BEGIN CLAIM\n${overclaim.text}\nEND CLAIM`,
      claim: { start: "BEGIN CLAIM", end: "END CLAIM" },
      retired: [],
      affirmativeRejects: overclaim.affirmativeRejects,
      required: [
        {
          description: `${overclaim.label} non-attribution boundary`,
          patterns: overclaim.patterns,
        },
      ],
    });
    if (!problems.length) {
      acceptedOverclaims.push(overclaim.label);
    }
  }
  if (acceptedOverclaims.length) {
    throw new Error(
      `affirmative overclaims pass their boundary contracts: ${acceptedOverclaims.join(", ")}`,
    );
  }

  if (
    CHROME_TEST_FLAGS.length !== 4 ||
    !CHROME_TEST_FLAGS.includes("--disable-back-forward-cache") ||
    CHROME_TEST_FLAGS.some((flag) => !flag.startsWith("--"))
  ) {
    throw new Error("the deterministic Chrome test flags are incomplete");
  }
  let bounded = false;
  try {
    await withDeadline(new Promise(() => {}), 10, "synthetic CDP request");
  } catch (error) {
    bounded = error instanceof Error && error.message.includes("synthetic CDP request");
  }
  if (!bounded) throw new Error("a non-responsive CDP request was not bounded");
  console.log("site quality browser lifecycle self-test passed");

  let endpointBounded = false;
  let socketBounded = false;
  try {
    await connect(0, 10, () => new Promise(() => {}));
  } catch (error) {
    endpointBounded = error instanceof Error && error.message.includes("within 10ms");
  }
  try {
    await waitForWebSocketOpen(new EventTarget(), 10, "synthetic WebSocket handshake");
  } catch (error) {
    socketBounded = error instanceof Error && error.message.includes("synthetic WebSocket handshake");
  }
  if (!endpointBounded || !socketBounded) {
    throw new Error("a browser startup wait was not bounded");
  }
  console.log("site quality browser startup self-test passed");

  const calls = [];
  const ready = await navigateWithoutPageScripts(
    async (method, params) => calls.push([method, params]),
    async (method) => {
      calls.push(["waitForEvent", method]);
      return new Promise((resolve) =>
        setTimeout(() => {
          calls.push(["event", method]);
          resolve();
        }, 0),
      );
    },
    "http://127.0.0.1/no-script/",
    async () => {
      calls.push(["inspect"]);
      return true;
    },
  );
  const expected = [
    ["Emulation.setScriptExecutionDisabled", { value: true }],
    ["waitForEvent", "Page.loadEventFired"],
    ["Page.navigate", { url: "http://127.0.0.1/no-script/" }],
    ["event", "Page.loadEventFired"],
    ["Emulation.setScriptExecutionDisabled", { value: false }],
    ["inspect"],
  ];
  if (!ready || JSON.stringify(calls) !== JSON.stringify(expected)) {
    throw new Error("the no-JavaScript navigation lifecycle is not inspectable");
  }
  console.log("site quality no-JavaScript navigation self-test passed");

  let renderExpression = "";
  const painted = await settlePaint(async (expression) => {
    renderExpression = expression;
    return true;
  });
  if (
    !painted ||
    !renderExpression.includes("requestAnimationFrame") ||
    !renderExpression.includes("setTimeout") ||
    !renderExpression.includes("document.getAnimations") ||
    !renderExpression.includes("animation.timeline !== document.timeline") ||
    !renderExpression.includes("Number.isFinite(timing.endTime)") ||
    !renderExpression.includes("animation.finished") ||
    !renderExpression.includes('style.visibility !== "hidden"') ||
    !renderExpression.includes("finish(false)")
  ) {
    throw new Error(
      "the render wait can return while a finite visible document-timeline animation is running",
    );
  }
  console.log("site quality render wait self-test passed");

  const viewport = { width: 390, height: 844 };
  const part = (rect, extra = {}) => ({
    ...rect,
    visible: true,
    clipped: false,
    contained: true,
    fillsContainer: true,
    anchored: true,
    ...extra,
  });
  const mapRect = { top: 150, right: 320, bottom: 740, left: 70, width: 250, height: 590 };
  const legendRect = { top: 750, right: 320, bottom: 810, left: 70, width: 250, height: 60 };
  const completeGeometry = {
    map: part(mapRect),
    mapSvg: part(mapRect),
    stationMarks: [
      part({ top: 300, right: 205, bottom: 309, left: 196, width: 9, height: 9 }),
    ],
    legend: part(legendRect),
    scaleBar: part({ top: 770, right: 320, bottom: 780, left: 70, width: 250, height: 10 }),
    scaleTicks: part({ top: 780, right: 320, bottom: 806, left: 70, width: 250, height: 26 }),
    scaleSegments: [
      part({ top: 770, right: 106, bottom: 780, left: 70, width: 36, height: 10 }),
    ],
    tickMarks: [
      part({ top: 782, right: 115, bottom: 802, left: 90, width: 25, height: 20 }),
    ],
    viewport,
  };
  const missedGeometryProblems = [];
  const expectGeometryProblem = (name, geometry, expected) => {
    const problems = firstViewportProblems(geometry);
    if (!problems.some((item) => item.includes(expected))) {
      missedGeometryProblems.push(name);
    }
  };
  if (firstViewportProblems(completeGeometry).length) {
    throw new Error("the homepage first-viewport predicate rejects complete geometry");
  }
  expectGeometryProblem(
    "missing inner content",
    { ...completeGeometry, mapSvg: null },
    "map SVG is missing",
  );
  expectGeometryProblem(
    "zero-area inner content",
    {
      ...completeGeometry,
      scaleBar: part({ top: 770, right: 70, bottom: 780, left: 70, width: 0, height: 10 }),
    },
    "scale bar has no rendered area",
  );
  expectGeometryProblem(
    "hidden inner content",
    { ...completeGeometry, scaleTicks: part(completeGeometry.scaleTicks, { visible: false }) },
    "scale ticks are not visibly rendered",
  );
  expectGeometryProblem(
    "horizontal inner overflow",
    {
      ...completeGeometry,
      stationMarks: [
        part({ top: 300, right: 399, bottom: 309, left: 390, width: 9, height: 9 }),
      ],
    },
    "station mark leaves the horizontal viewport",
  );
  expectGeometryProblem(
    "malformed edge coordinates",
    {
      ...completeGeometry,
      map: part({ ...mapRect, right: Number.NaN }),
    },
    "map has non-finite geometry",
  );
  expectGeometryProblem(
    "internally clipped content",
    { ...completeGeometry, mapSvg: part(mapRect, { clipped: true }) },
    "map SVG is clipped by an ancestor",
  );
  expectGeometryProblem(
    "transformed content outside its container",
    { ...completeGeometry, scaleBar: part(completeGeometry.scaleBar, { contained: false }) },
    "scale bar leaves its container",
  );
  expectGeometryProblem(
    "transformed content shrunk inside its container",
    { ...completeGeometry, scaleBar: part(completeGeometry.scaleBar, { fillsContainer: false }) },
    "scale bar no longer fills its container",
  );
  expectGeometryProblem(
    "station content displaced from its anchor",
    {
      ...completeGeometry,
      stationMarks: [part(completeGeometry.stationMarks[0], { anchored: false })],
    },
    "station mark is displaced from its anchor",
  );
  expectGeometryProblem(
    "transformed station content outside its container",
    {
      ...completeGeometry,
      stationMarks: [part(completeGeometry.stationMarks[0], { contained: false })],
    },
    "station mark leaves its container",
  );
  expectGeometryProblem(
    "transformed tick content outside its container",
    {
      ...completeGeometry,
      tickMarks: [part(completeGeometry.tickMarks[0], { contained: false })],
    },
    "tick mark leaves its container",
  );
  expectGeometryProblem(
    "vertical cropping",
    {
      ...completeGeometry,
      map: part({ top: 300, right: 320, bottom: 890, left: 70, width: 250, height: 590 }),
    },
    "vertical viewport",
  );
  if (missedGeometryProblems.length) {
    throw new Error(
      `the homepage first-viewport predicate accepts ${missedGeometryProblems.join(", ")}`,
    );
  }

  const normalLabelMap = {
    top: 100, right: 350, bottom: 645, left: 50, width: 300, height: 545,
  };
  const normalLabels = [
    part({ top: 200, right: 140, bottom: 222, left: 100, width: 40, height: 22 }, {
      identifier: "甲縣",
    }),
    part({ top: 200, right: 200, bottom: 222, left: 160, width: 40, height: 22 }, {
      identifier: "乙縣",
    }),
  ];
  const missedCountyLabelProblems = [];
  const expectCountyLabelProblem = (name, state, expected) => {
    const problems = countyLabelProblems(state);
    if (!problems.some((problem) => problem.includes(expected))) {
      missedCountyLabelProblems.push(name);
    }
  };
  if (countyLabelProblems({ map: normalLabelMap, labels: normalLabels }).length) {
    throw new Error("the homepage county-label predicate rejects clear geometry");
  }
  if (
    countyLabelProblems({
      map: normalLabelMap,
      labels: normalLabels,
      expectedVisible: 2,
    }).length
  ) {
    throw new Error("the homepage county-label predicate rejects its exact inventory");
  }
  expectCountyLabelProblem(
    "missing visible county name",
    { map: normalLabelMap, labels: normalLabels, expectedVisible: 3 },
    "visible county-label inventory",
  );
  expectCountyLabelProblem(
    "overlapping names on a normal map",
    {
      map: normalLabelMap,
      labels: [
        normalLabels[0],
        part({ top: 200, right: 160, bottom: 222, left: 120, width: 40, height: 22 }, {
          identifier: "乙縣",
        }),
      ],
    },
    "county label pairs overlap",
  );
  if (
    countyLabelProblems({
      map: { ...normalLabelMap, right: 248, width: 198 },
      labels: normalLabels.map((label) => ({ ...label, visible: false })),
    }).length
  ) {
    throw new Error("the homepage county-label predicate rejects hidden small-map labels");
  }
  expectCountyLabelProblem(
    "name outside its map",
    {
      map: normalLabelMap,
      labels: [normalLabels[0], { ...normalLabels[1], contained: false }],
    },
    "leaves the map",
  );
  if (missedCountyLabelProblems.length) {
    throw new Error(
      `the homepage county-label predicate accepts ${missedCountyLabelProblems.join(", ")}`,
    );
  }
  console.log("site quality homepage first-viewport self-test passed");

  const atlasPart = ({ top, right, bottom, left }) => ({
    top,
    right,
    bottom,
    left,
    width: right - left,
    height: bottom - top,
    visible: true,
  });
  const completeWideEditorial = {
    mode: "wide",
    opening: atlasPart({ top: 20, right: 700, bottom: 760, left: 40 }),
    routes: atlasPart({ top: 400, right: 700, bottom: 760, left: 40 }),
    map: atlasPart({ top: 20, right: 1160, bottom: 700, left: 760 }),
    postMap: atlasPart({ top: 800, right: 1160, bottom: 1100, left: 40 }),
    viewport: { width: 1200, height: 900 },
  };
  const completeStackedEditorial = {
    mode: "stacked",
    opening: atlasPart({ top: 20, right: 360, bottom: 400, left: 20 }),
    routes: atlasPart({ top: 180, right: 360, bottom: 400, left: 20 }),
    map: atlasPart({ top: 420, right: 320, bottom: 800, left: 60 }),
    postMap: atlasPart({ top: 820, right: 360, bottom: 1100, left: 20 }),
    viewport: { width: 380, height: 820 },
  };
  if (
    editorialHomepageLayoutProblems(completeWideEditorial).length ||
    editorialHomepageLayoutProblems(completeStackedEditorial).length
  ) {
    throw new Error("the homepage editorial-layout predicate rejects complete geometry");
  }
  const missedEditorialLayoutProblems = [];
  const expectEditorialLayoutProblem = (name, geometry, expected) => {
    const problems = editorialHomepageLayoutProblems(geometry);
    if (!problems.some((item) => item.includes(expected))) {
      missedEditorialLayoutProblems.push(name);
    }
  };
  expectEditorialLayoutProblem(
    "missing map geometry",
    { ...completeWideEditorial, map: null },
    "map geometry is missing",
  );
  expectEditorialLayoutProblem(
    "hidden primary routes",
    { ...completeStackedEditorial, routes: { ...completeStackedEditorial.routes, visible: false } },
    "routes is not visible",
  );
  expectEditorialLayoutProblem(
    "overlapping desktop columns",
    {
      ...completeWideEditorial,
      map: atlasPart({ top: 20, right: 1160, bottom: 700, left: 650 }),
    },
    "columns overlap",
  );
  expectEditorialLayoutProblem(
    "primary block below desktop viewport",
    {
      ...completeWideEditorial,
      routes: atlasPart({ top: 700, right: 700, bottom: 902, left: 40 }),
    },
    "primary reading block leaves the first viewport",
  );
  expectEditorialLayoutProblem(
    "reversed mobile stack",
    {
      ...completeStackedEditorial,
      postMap: atlasPart({ top: 700, right: 360, bottom: 980, left: 20 }),
    },
    "mobile stack changed",
  );
  if (missedEditorialLayoutProblems.length) {
    throw new Error(
      `the homepage editorial-layout predicate accepts ${missedEditorialLayoutProblems.join(", ")}`,
    );
  }
  console.log("site quality homepage editorial layout self-test passed");

  const completeEditorialOrder = { opening: 0, routes: 1, primary: 2, map: 3, postMap: 4 };
  if (editorialHomepageOrderProblems(completeEditorialOrder).length) {
    throw new Error("the homepage editorial-order predicate rejects complete source order");
  }
  const missedEditorialOrderProblems = [];
  const expectEditorialOrderProblem = (name, state) => {
    const problems = editorialHomepageOrderProblems(state);
    if (!problems.some((problem) => problem.includes("source order changed"))) {
      missedEditorialOrderProblems.push(name);
    }
  };
  expectEditorialOrderProblem("opening after routes", { ...completeEditorialOrder, opening: 1 });
  expectEditorialOrderProblem("routes before opening", { ...completeEditorialOrder, routes: 0 });
  expectEditorialOrderProblem("primary before routes", { ...completeEditorialOrder, primary: 1 });
  expectEditorialOrderProblem("map before primary", { ...completeEditorialOrder, map: 2 });
  expectEditorialOrderProblem("post-map before map", { ...completeEditorialOrder, postMap: 3 });
  if (missedEditorialOrderProblems.length) {
    throw new Error(
      `the homepage editorial-order predicate accepts ${missedEditorialOrderProblems.join(", ")}`,
    );
  }
  console.log("site quality homepage editorial order self-test passed");

  const completeMobileType = {
    viewportWidth: 375,
    root: 20,
    finding: 22,
    routeLabel: 22,
    routeIntro: 19,
    routeClaim: 19,
  };
  const completeWideType = {
    viewportWidth: 1440,
    root: 22,
    finding: 26.4,
    routeLabel: 26.4,
    routeIntro: 22,
    routeClaim: 22,
  };
  if (
    homepageMobileTypeProblems(completeMobileType).length ||
    homepageMobileTypeProblems({ ...completeMobileType, viewportWidth: 480 }).length ||
    homepageMobileTypeProblems({ ...completeWideType, viewportWidth: 481 }).length ||
    homepageMobileTypeProblems(completeWideType).length
  ) {
    throw new Error("the homepage mobile-type predicate rejects a complete type scale");
  }
  const missedMobileTypeProblems = [];
  for (const role of ["finding", "routeLabel", "routeIntro", "routeClaim"]) {
    const problems = homepageMobileTypeProblems({
      ...completeMobileType,
      [role]: completeMobileType[role] + 1,
    });
    if (!problems.some((problem) => problem.includes(`homepage ${role} type ratio changed`))) {
      missedMobileTypeProblems.push(role);
    }
  }
  if (missedMobileTypeProblems.length) {
    throw new Error(
      `the homepage mobile-type predicate accepts ${missedMobileTypeProblems.join(", ")}`,
    );
  }
  console.log("site quality homepage mobile type self-test passed");

  const completeDeweatherBoundary = {
    count: 1,
    visible: true,
    text: "氣象標準化差額。不是「天氣造成 43%」的因果證明；剩餘比例也不是排放或政策貢獻估計。",
  };
  if (deweatherContrastBoundaryProblems(completeDeweatherBoundary).length) {
    throw new Error("the trend deweather-contrast predicate rejects its control");
  }
  const missingDeweatherBoundary = deweatherContrastBoundaryProblems({
    ...completeDeweatherBoundary,
    count: 0,
    visible: false,
    text: "",
  });
  if (
    !missingDeweatherBoundary.some((problem) => problem.includes("inventory changed")) ||
    !missingDeweatherBoundary.some((problem) => problem.includes("is not visible")) ||
    !missingDeweatherBoundary.some((problem) => problem.includes("boundary claim changed"))
  ) {
    throw new Error("the trend deweather-contrast predicate accepts a deleted boundary");
  }

  const completeHomepageStationTypeBoundary = {
    count: 1,
    visible: true,
    text: HOMEPAGE_EXTREMA.sameType
      ? `最高與最低同屬${HOMEPAGE_EXTREMA.dirtiestType}（${HOMEPAGE_EXTREMA.dirtiest}、` +
        `${HOMEPAGE_EXTREMA.cleanest}），差別在地點不同。${HOMEPAGE_EXTREMA.ratio}× 是測站觀測值` +
        `對比，兩地的排放、地形與土地使用都混在裡面，不是純空間差距，也不是任何因果效果。`
      : `最高與最低不是同一類測站：${HOMEPAGE_EXTREMA.dirtiest}屬${HOMEPAGE_EXTREMA.dirtiestType}，` +
        `${HOMEPAGE_EXTREMA.cleanest}屬${HOMEPAGE_EXTREMA.cleanestType}—不同類別的測站，` +
        `PM2.5 的驅動因子本來就不同。${HOMEPAGE_EXTREMA.ratio}× 是測站觀測值對比，` +
        `站型與地點的差距混在一起，不是純空間差距，也不是任何因果效果。`,
  };
  if (homepageStationTypeBoundaryProblems(completeHomepageStationTypeBoundary).length) {
    throw new Error("the homepage station-type predicate rejects its control");
  }
  const missingHomepageStationTypeBoundary = homepageStationTypeBoundaryProblems({
    ...completeHomepageStationTypeBoundary,
    count: 0,
    visible: false,
    text: "",
  });
  if (
    !missingHomepageStationTypeBoundary.some((problem) => problem.includes("inventory changed")) ||
    !missingHomepageStationTypeBoundary.some((problem) => problem.includes("is not visible")) ||
    !missingHomepageStationTypeBoundary.some((problem) => problem.includes("boundary claim changed"))
  ) {
    throw new Error("the homepage station-type predicate accepts a deleted boundary");
  }

  const completeFigureDownloadLabels = {
    toolbarCount: 2,
    downloadLabels: ["下載", "下載"],
    downloadAriaLabels: ["下載 PNG：圖 1.1", "下載 PNG：圖 1.2"],
    downloadWhiteSpaces: ["nowrap", "nowrap"],
  };
  if (figureDownloadLabelProblems(completeFigureDownloadLabels).length) {
    throw new Error("the figure download-label predicate rejects its control");
  }
  if (!figureDownloadLabelProblems({
    ...completeFigureDownloadLabels,
    downloadLabels: ["下載", "下載 PNG"],
  }).some((problem) => problem.includes("concise visible label"))) {
    throw new Error("the figure download-label predicate accepts the verbose visible label");
  }
  if (!figureDownloadLabelProblems({
    ...completeFigureDownloadLabels,
    downloadAriaLabels: ["下載：圖 1.1", "下載 PNG：圖 1.2"],
  }).some((problem) => problem.includes("accessible name omits"))) {
    throw new Error("the figure download-label predicate accepts an accessible name without format");
  }
  if (!figureDownloadLabelProblems({
    ...completeFigureDownloadLabels,
    downloadWhiteSpaces: ["normal", "nowrap"],
  }).some((problem) => problem.includes("can wrap"))) {
    throw new Error("the figure download-label predicate accepts a wrappable control label");
  }

  const completeTrendControlClarification = {
    hintCount: 1,
    hintVisible: true,
    hintText: "勾選以顯示空品區；取消勾選會隱藏該線。",
    uncheckedLineDisplay: "none",
    checkedLineDisplay: "inline",
  };
  if (trendControlClarificationProblems(completeTrendControlClarification).length) {
    throw new Error("the trend control-clarification predicate rejects its control");
  }
  if (!trendControlClarificationProblems({
    ...completeTrendControlClarification,
    uncheckedLineDisplay: "inline",
  }).some((problem) => problem.includes("unchecked series remains rendered"))) {
    throw new Error("the trend control-clarification predicate accepts a rendered unchecked line");
  }
  if (!trendControlClarificationProblems({
    ...completeTrendControlClarification,
    checkedLineDisplay: "none",
  }).some((problem) => problem.includes("checked series is hidden"))) {
    throw new Error("the trend control-clarification predicate accepts a hidden checked line");
  }
  if (!trendControlClarificationProblems({
    ...completeTrendControlClarification,
    hintText: "勾選想比較的空品區。",
  }).some((problem) => problem.includes("instruction changed"))) {
    throw new Error("the trend control-clarification predicate accepts ambiguous instructions");
  }

  const completeTrendIdleReadout = {
    viewport: { width: 610, height: 900 },
    figures: Array.from({ length: 3 }, (_, index) => ({
      index,
      hasDock: true,
      readingBefore: "false",
      idleOptIn: true,
      idleWhen: "2025・μg/m³",
      idleReserve: 160,
      idlePanelHeight: 148,
      idlePanelOpacity: 1,
      idleUnoccupiedReserve: 12,
    })),
  };
  if (trendIdleReadoutBlankProblems(completeTrendIdleReadout).length) {
    throw new Error("the trend idle-readout predicate rejects its control");
  }
  const blankTrendReadout = structuredClone(completeTrendIdleReadout);
  blankTrendReadout.figures[2].idlePanelOpacity = 0;
  blankTrendReadout.figures[2].idleUnoccupiedReserve = IDLE_READOUT_BLANK_LIMIT_PX;
  if (!trendIdleReadoutBlankProblems(blankTrendReadout)
    .some((problem) => problem.includes("unused"))) {
    throw new Error("the trend idle-readout predicate accepts a 96px blank interval");
  }

  const completeStationFilterHelper = {
    count: 1,
    visible: true,
    text: "輸入以縮小清單，或直接從清單選一站。",
    describedBy: "station-filter-help station-filter-count",
  };
  if (stationFilterHelperProblems(completeStationFilterHelper).length) {
    throw new Error("the station filter-helper predicate rejects its control");
  }
  if (!stationFilterHelperProblems({
    ...completeStationFilterHelper,
    text: "輸入站名或縣市。",
  }).some((problem) => problem.includes("helper text changed"))) {
    throw new Error("the station filter-helper predicate accepts ambiguous instructions");
  }

  const completeHomepageMapStationRoute = {
    count: 1,
    visible: true,
    text: "前往第二章查一個測站 →",
    href: "https://example.test/air-quality/stations/",
  };
  if (homepageMapStationRouteProblems(completeHomepageMapStationRoute).length) {
    throw new Error("the homepage map station-route predicate rejects its control");
  }
  if (!homepageMapStationRouteProblems({
    ...completeHomepageMapStationRoute,
    href: "https://example.test/air-quality/data/",
  }).some((problem) => problem.includes("destination changed"))) {
    throw new Error("the homepage map station-route predicate accepts the wrong destination");
  }

  const completeChapterOpening = {
    viewport: { width: 1280, height: 720 },
    smallestVisibleText: 18,
    rail: { visible: false, width: 272 },
    handle: { visible: true, height: 48 },
    primary: { visible: true, top: 250, bottom: 650 },
    primaryPlot: { visible: true, top: 300, bottom: 520, dataAreaVisible: 180 },
    chartRoute: true,
    openingKind: "evidence",
  };
  const missedChapterOpeningProblems = [];
  const expectChapterOpeningProblem = (name, state, expected) => {
    const problems = chapterOpeningProblems(state);
    if (!problems.some((problem) => problem.includes(expected))) {
      missedChapterOpeningProblems.push(name);
    }
  };
  if (chapterOpeningProblems(completeChapterOpening).length) {
    throw new Error("the chapter-opening predicate rejects complete geometry");
  }
  expectChapterOpeningProblem(
    "17.99px visible text",
    { ...completeChapterOpening, smallestVisibleText: 17.99 },
    "18px floor",
  );
  expectChapterOpeningProblem(
    "persistent rail at 1599px",
    {
      ...completeChapterOpening,
      viewport: { width: 1599, height: 900 },
      rail: { visible: true, width: 272 },
    },
    "persistent rail is visible below 1600px",
  );
  expectChapterOpeningProblem(
    "visible handle at 1600px",
    {
      ...completeChapterOpening,
      viewport: { width: 1600, height: 900 },
      rail: { visible: true, width: 272 },
      handle: { visible: true, height: 48 },
    },
    "handle remains visible at or above 1600px",
  );
  expectChapterOpeningProblem(
    "missing primary evidence",
    { ...completeChapterOpening, primary: null },
    "primary evidence is missing",
  );
  expectChapterOpeningProblem(
    "evidence opening below viewport",
    {
      ...completeChapterOpening,
      primary: { visible: true, top: 720, bottom: 1120 },
    },
    "primary evidence is outside the initial viewport",
  );
  const completeIndexedOpening = {
    ...completeChapterOpening,
    openingKind: "index",
    primary: { visible: true, top: 900, bottom: 1300 },
    primaryPlot: { visible: true, top: 1000, bottom: 1220, dataAreaVisible: 0 },
  };
  if (chapterOpeningProblems(completeIndexedOpening).length) {
    throw new Error("the chapter-opening predicate rejects a complete indexed opening");
  }
  expectChapterOpeningProblem(
    "invalid opening kind",
    { ...completeChapterOpening, openingKind: "other" },
    "chapter opening kind is invalid",
  );
  expectChapterOpeningProblem(
    "plot starting at 55vh",
    {
      ...completeChapterOpening,
      primaryPlot: { visible: true, top: 396, bottom: 600, dataAreaVisible: 180 },
    },
    "plot starts at or below 55vh",
  );
  expectChapterOpeningProblem(
    "less than 180px of visible plot data",
    {
      ...completeChapterOpening,
      primaryPlot: { visible: true, top: 300, bottom: 520, dataAreaVisible: 179.99 },
    },
    "less than 180px of plot data is visible",
  );
  expectChapterOpeningProblem(
    "non-finite primary geometry",
    {
      ...completeChapterOpening,
      primary: { visible: true, top: Number.NaN, bottom: 650 },
    },
    "primary evidence has invalid geometry",
  );
  expectGeometryProblem(
    "homepage map or legend below the first viewport",
    {
      ...completeGeometry,
      legend: part({ top: 820, right: 320, bottom: 880, left: 70, width: 250, height: 60 }),
    },
    "map legend leaves the initial vertical viewport",
  );
  if (missedChapterOpeningProblems.length || missedGeometryProblems.length) {
    throw new Error(
      `the chapter-opening predicate accepts ${[
        ...missedChapterOpeningProblems,
        ...missedGeometryProblems,
      ].join(", ")}`,
    );
  }
  console.log("site quality chapter opening self-test passed");

  const completeChapterEnding = {
    navCount: 1,
    panelCount: 1,
    progressCount: 1,
    progressText: "5 / 10",
    expectedProgressText: "5 / 10",
    position: "middle",
    expectedPosition: "middle",
    indexLinks: 1,
    indexLabel: "全部章節",
    inertEndpoints: 0,
    linkCount: 3,
    expectedLinkCount: 3,
    previousLinks: 1,
    expectedPreviousLinks: 1,
    nextLinks: 1,
    expectedNextLinks: 1,
    directionLabels: 2,
    hiddenArrows: 2,
    outwardArrows: 2,
    linkHeights: [44, 72, 72],
    containedLinks: 3,
    clippedTitles: 0,
    horizontalOverflow: 0,
    navHeight: 180,
  };
  if (chapterEndingProblems(completeChapterEnding, { width: 1440, height: 900 }).length) {
    throw new Error("the chapter-ending predicate rejects a complete reading handoff");
  }
  if (!chapterEndingProblems({
    ...completeChapterEnding,
    inertEndpoints: 1,
  }, { width: 1440, height: 900 }).some((problem) => problem.includes("inert endpoints"))) {
    throw new Error("the chapter-ending predicate accepts an inert endpoint placeholder");
  }
  if (!chapterEndingProblems({
    ...completeChapterEnding,
    navHeight: 476,
  }, { width: 390, height: 844 }).some((problem) => problem.includes("476px tall"))) {
    throw new Error("the chapter-ending predicate accepts the old phone-height band");
  }
  if (!chapterEndingProblems({
    ...completeChapterEnding,
    outwardArrows: 1,
  }, { width: 1440, height: 900 }).some((problem) => problem.includes("outward navigation edges"))) {
    throw new Error("the chapter-ending predicate accepts an inward next arrow");
  }
  console.log("site quality chapter ending self-test passed");

  const completeTrendReadingMapFor = (viewportWidth) => {
    const wide = viewportWidth >= 1024;
    const thesis = wide
      ? { visible: true, top: 80, bottom: 250, left: 20, right: 480, width: 460, height: 170 }
      : { visible: true, top: 80, bottom: 170, left: 20, right: 355, width: 335, height: 90 };
    const map = wide
      ? { visible: true, clippedByAncestor: false, top: 90, bottom: 250, left: 520, right: 855, width: 335, height: 160 }
      : { visible: true, clippedByAncestor: false, top: 180, bottom: 340, left: 20, right: 355, width: 335, height: 160 };
    return {
      viewportWidth,
      viewportHeight: 812,
      targetIds: [...TREND_READING_MAP_CONTRACT.targetIds],
      thesis,
      map: { ...map, ariaHidden: false, opacityZero: false },
      links: Array.from({ length: 3 }, (_value, index) => ({
        visible: true, ariaHidden: false, opacityZero: false, clippedByAncestor: false,
        top: map.top + index * 48,
        bottom: map.top + 48 + index * 48,
        width: 335,
        height: 48,
      })),
      targets: Array.from({ length: 3 }, (_value, index) => ({
        visible: true, ariaHidden: false, opacityZero: false, clippedByAncestor: false,
        top: 400 + index * 500,
        bottom: 448 + index * 500,
        width: 335,
        height: 48,
        afterJumpTop: 72,
      })),
      primary: {
        visible: true, clippedByAncestor: false, top: 360, bottom: 760,
        left: 20, right: 855, width: 835, height: 400,
      },
      stickyBottom: 64,
      anchorsMeasured: true,
      sourceOrdered: true,
      evidenceOrdered: true,
      horizontalOverflow: 0,
    };
  };
  if (
    !Object.isFrozen(TREND_READING_MAP_CONTRACT.targetIds) ||
    !Object.isFrozen(SPACE_READING_MAP_CONTRACT.targetIds)
  ) {
    throw new Error("reading-map target IDs are not immutable");
  }
  for (const viewportWidth of [375, 768, 1024, 1440]) {
    const state = completeTrendReadingMapFor(viewportWidth);
    if (readingMapProblems(state, TREND_READING_MAP_CONTRACT).length) {
      throw new Error(`trend reading-map predicate rejected ${viewportWidth}px control`);
    }
  }
  const missedTrendReadingMapProblems = [];
  const expectTrendReadingMapProblem = (name, viewportWidth, mutate, expected) => {
    const state = completeTrendReadingMapFor(viewportWidth);
    mutate(state);
    const problems = readingMapProblems(state, TREND_READING_MAP_CONTRACT);
    if (!problems.some((problem) => problem.includes(expected))) {
      missedTrendReadingMapProblems.push(name);
    }
  };
  expectTrendReadingMapProblem("hidden map", 375, (state) => {
    state.map.visible = false;
  }, "map is not visible");
  expectTrendReadingMapProblem("clipped map", 1440, (state) => {
    state.map.clippedByAncestor = true;
  }, "map is clipped");
  expectTrendReadingMapProblem("43px second link", 375, (state) => {
    state.links[1].height = 43;
  }, "link 2 target is too small");
  expectTrendReadingMapProblem("43px-wide first link", 375, (state) => {
    state.links[0].width = 43;
  }, "link 1 target is too small");
  expectTrendReadingMapProblem("hidden third target", 375, (state) => {
    state.targets[2].visible = false;
  }, "target 3 is not visible");
  expectTrendReadingMapProblem("obscured second target", 375, (state) => {
    state.targets[1].afterJumpTop = state.stickyBottom - 2;
  }, "target 2 is obscured after jump");
  expectTrendReadingMapProblem("reordered source", 375, (state) => {
    state.sourceOrdered = false;
  }, "source order changed");
  expectTrendReadingMapProblem("horizontal overflow", 375, (state) => {
    state.horizontalOverflow = 2;
  }, "causes horizontal overflow");
  expectTrendReadingMapProblem("primary question below phone viewport", 375, (state) => {
    state.targets[0].top = 900;
  }, "leaves the first phone viewport");
  expectTrendReadingMapProblem("overlapping narrow stack", 768, (state) => {
    state.map.top = 100;
  }, "no longer follows the thesis on narrow screens");
  expectTrendReadingMapProblem("overlapping desktop columns", 1024, (state) => {
    state.map.left = 200;
  }, "desktop opening is not a two-column composition");
  expectTrendReadingMapProblem("clipped primary evidence", 1440, (state) => {
    state.primary.clippedByAncestor = true;
  }, "primary evidence is clipped");
  if (missedTrendReadingMapProblems.length) {
    throw new Error(
      `the trend reading-map predicate accepts ${missedTrendReadingMapProblems.join(", ")}`,
    );
  }
  console.log("site quality trend reading map self-test passed");

  const completeSpaceFieldNoteFor = (viewportWidth) => ({
    ...completeTrendReadingMapFor(viewportWidth),
    targetIds: [...SPACE_READING_MAP_CONTRACT.targetIds],
  });
  for (const viewportWidth of [375, 768, 1024, 1440]) {
    const state = completeSpaceFieldNoteFor(viewportWidth);
    if (readingMapProblems(state, SPACE_READING_MAP_CONTRACT).length) {
      throw new Error(`space field-note predicate rejected ${viewportWidth}px control`);
    }
  }
  const missedSpaceFieldNoteProblems = [];
  const expectSpaceFieldNoteProblem = (name, viewportWidth, mutate, expected) => {
    const state = completeSpaceFieldNoteFor(viewportWidth);
    const before = JSON.stringify(state);
    mutate(state);
    if (JSON.stringify(state) === before) {
      throw new Error(`space field-note mutation did not apply: ${name}`);
    }
    const problems = readingMapProblems(state, SPACE_READING_MAP_CONTRACT);
    if (!problems.some((problem) => problem.includes(expected))) {
      missedSpaceFieldNoteProblems.push(name);
    }
  };
  expectSpaceFieldNoteProblem("hidden map", 375, (state) => {
    state.map.visible = false;
  }, "map is not visible");
  expectSpaceFieldNoteProblem("aria-hidden map", 375, (state) => {
    state.map.ariaHidden = true;
  }, "map is aria-hidden");
  expectSpaceFieldNoteProblem("opacity-zero map", 375, (state) => {
    state.map.opacityZero = true;
  }, "map has zero opacity");
  expectSpaceFieldNoteProblem("clipped map", 1440, (state) => {
    state.map.clippedByAncestor = true;
  }, "map is clipped");
  expectSpaceFieldNoteProblem("43px link", 375, (state) => {
    state.links[1].height = 43;
  }, "link 2 target is too small");
  expectSpaceFieldNoteProblem("zero-area target", 375, (state) => {
    state.targets[2].width = 0;
  }, "target 3 is not visible");
  expectSpaceFieldNoteProblem("obscured target", 375, (state) => {
    state.targets[1].afterJumpTop = state.stickyBottom - 2;
  }, "target 2 is obscured after jump");
  expectSpaceFieldNoteProblem("target below jump viewport", 375, (state) => {
    state.targets[1].afterJumpTop = state.viewportHeight + 1;
  }, "target 2 is outside the viewport after jump");
  expectSpaceFieldNoteProblem("same-length target ID drift", 375, (state) => {
    state.targetIds[1] = "space-unreviewed";
  }, "target IDs changed");
  expectSpaceFieldNoteProblem("reordered source", 375, (state) => {
    state.sourceOrdered = false;
  }, "source order changed");
  expectSpaceFieldNoteProblem("reordered framed evidence", 375, (state) => {
    state.evidenceOrdered = false;
  }, "evidence order changed");
  expectSpaceFieldNoteProblem("hidden primary", 1440, (state) => {
    state.primary.visible = false;
  }, "primary evidence is not visible");
  expectSpaceFieldNoteProblem("phone first question below viewport", 375, (state) => {
    state.targets[0].top = 900;
  }, "primary evidence question leaves the first phone viewport");
  expectSpaceFieldNoteProblem("horizontal overflow", 375, (state) => {
    state.horizontalOverflow = 2;
  }, "causes horizontal overflow");
  if (missedSpaceFieldNoteProblems.length) {
    throw new Error(
      `the space field-note predicate accepts ${missedSpaceFieldNoteProblems.join(", ")}`,
    );
  }
  console.log("site quality space field note self-test passed");

  const completeTrendPrint = {
    thesisVisible: true,
    mapVisible: true,
    primaryVisible: true,
    linksVisible: [true, true, true],
    targetsVisible: [true, true, true],
    sourceOrdered: true,
    evidenceOrdered: true,
  };
  if (readingMapPrintProblems(completeTrendPrint, TREND_READING_MAP_CONTRACT).length) {
    throw new Error("trend print predicate rejected its complete control");
  }
  const missedTrendPrintProblems = [];
  const expectTrendPrintProblem = (name, mutate, expected) => {
    const state = {
      ...completeTrendPrint,
      linksVisible: [...completeTrendPrint.linksVisible],
      targetsVisible: [...completeTrendPrint.targetsVisible],
    };
    mutate(state);
    const problems = readingMapPrintProblems(state, TREND_READING_MAP_CONTRACT);
    if (!problems.some((problem) => problem.includes(expected))) {
      missedTrendPrintProblems.push(name);
    }
  };
  expectTrendPrintProblem("hidden thesis", (state) => {
    state.thesisVisible = false;
  }, "thesis is not visible");
  expectTrendPrintProblem("hidden map", (state) => {
    state.mapVisible = false;
  }, "reading map is not visible");
  expectTrendPrintProblem("hidden primary evidence", (state) => {
    state.primaryVisible = false;
  }, "primary evidence is not visible");
  expectTrendPrintProblem("hidden second question link", (state) => {
    state.linksVisible[1] = false;
  }, "link 2 is not visible");
  expectTrendPrintProblem("hidden third question target", (state) => {
    state.targetsVisible[2] = false;
  }, "target 3 is not visible");
  expectTrendPrintProblem("reordered source", (state) => {
    state.sourceOrdered = false;
  }, "source order changed");
  if (missedTrendPrintProblems.length) {
    throw new Error(
      `the trend print predicate accepts ${missedTrendPrintProblems.join(", ")}`,
    );
  }
  const completeSpacePrint = {
    ...completeTrendPrint,
    linksVisible: [...completeTrendPrint.linksVisible],
    targetsVisible: [...completeTrendPrint.targetsVisible],
    evidenceOrdered: false,
  };
  const missedSpacePrintProblems = readingMapPrintProblems(
    completeSpacePrint,
    SPACE_READING_MAP_CONTRACT,
  );
  if (!missedSpacePrintProblems.some((problem) => problem.includes("evidence order changed"))) {
    throw new Error("the space print predicate accepts reordered framed evidence");
  }
  console.log("site quality trend print contract self-test passed");

  const completeStationDossierFor = (viewportWidth) => ({
    viewportWidth,
    picker: { visible: true, width: viewportWidth - 32, height: 520 },
    controls: {
      visible: true,
      width: viewportWidth - 32,
      height: viewportWidth === 375 ? 280 : 132,
    },
    searchField: { visible: true, width: 300, height: 76 },
    help: { visible: true, width: 300, height: 34 },
    count: { visible: true, width: 300, height: 34 },
    combo: {
      role: "combobox", expanded: "false", controlsListbox: "station-listbox",
      autocomplete: "list", listboxPresent: true, listboxRole: "listbox",
      listboxHiddenAtRest: true, optionCountInList: 79, groupCountInList: 23,
      selectedOptions: ["西屯"],
    },
    supportingRowsFollowFields: true,
    controlsFollowDomOrder: true,
    select: { visible: true, width: 320, height: 44 },
    optionCount: 79,
    reportCount: 79,
    visibleReportCount: 1,
    selectedValue: "西屯",
    visibleStation: "西屯",
    identityText: "西屯",
    identityName: { visible: true, width: 80, height: 32 },
    identityVisible: true,
    yearVisible: true,
    stats: Array.from({ length: 4 }, () => ({ visible: true, width: 120, height: 96 })),
    comparisons: Array.from(
      { length: 2 },
      () => ({ visible: true, width: 180, height: 52 }),
    ),
    // 西屯 is rank 43 of 77, and (43 - 1) / (77 - 1) * 100 = 55.263…
    rankStrip: {
      visible: true, width: 240, height: 8, position: 55.26, rank: 43, total: 77,
      markLeft: 129.9, markRight: 134.4, trackWidth: 240,
    },
    locator: {
      visible: true, width: 260, height: 380, markVisible: true,
      markStation: "西屯", countyCount: 19,
      unplacedNoteVisible: false, offshoreNoteVisible: false,
    },
    columns: viewportWidth === 375 ? 1 : viewportWidth === 768 ? 2 : 4,
    separators: {
      reportTop: 0,
      reportBottom: 0,
      identityBottom: 0,
      statisticsTop: 1,
      statisticTops: [0, 0, 0, 0],
      comparisonsTop: 1,
      noteTop: 0,
      noteBottom: 0,
    },
    standardNote: { visible: true, width: 320, height: 120 },
    horizontalOverflow: 0,
    afterChange: {
      performed: true,
      selectedValue: "基隆",
      visibleStation: "基隆",
      identityText: "基隆",
      identityName: { visible: true, width: 80, height: 32 },
      visibleReportCount: 1,
      selectedMatchesVisible: true,
      identity: { visible: true, width: 180, height: 52 },
      year: { visible: true, width: 80, height: 24 },
      stats: Array.from({ length: 4 }, () => ({ visible: true, width: 120, height: 96 })),
      comparisons: Array.from(
        { length: 2 },
        () => ({ visible: true, width: 180, height: 52 }),
      ),
      liveIncludesStation: true,
      liveIncludesYear: true,
      liveIncludesFirstStat: true,
      liveIncludesThirdStat: true,
      locatorMarkStation: "基隆",
      locatorUnplacedVisible: false,
    },
    restored: {
      performed: true,
      selectedValue: "西屯",
      visibleStation: "西屯",
      identityText: "西屯",
      identityName: { visible: true, width: 80, height: 32 },
      visibleReportCount: 1,
      selectedMatchesVisible: true,
      liveIncludesStation: true,
      liveIncludesYear: true,
      liveIncludesFirstStat: true,
      liveIncludesThirdStat: true,
    },
  });
  for (const viewportWidth of [375, 768, 1024, 1440]) {
    if (stationDossierProblems(completeStationDossierFor(viewportWidth)).length) {
      throw new Error(`station dossier predicate rejected ${viewportWidth}px control`);
    }
  }
  const missedStationDossierProblems = [];
  const expectStationDossierProblem = (name, viewportWidth, mutate, expected) => {
    const state = completeStationDossierFor(viewportWidth);
    mutate(state);
    const problems = stationDossierProblems(state);
    if (!problems.some((problem) => problem.includes(expected))) {
      missedStationDossierProblems.push(name);
    }
  };
  expectStationDossierProblem("hidden picker", 375, (state) => {
    state.picker.visible = false;
  }, "dossier is not visibly rendered");
  expectStationDossierProblem("hidden field", 375, (state) => {
    state.select.visible = false;
  }, "selector is not visibly rendered");
  expectStationDossierProblem("zero-area field", 375, (state) => {
    state.select.width = 0;
  }, "selector is not visibly rendered");
  expectStationDossierProblem("43px field", 375, (state) => {
    state.select.height = 43;
  }, "shorter than 44px");
  expectStationDossierProblem("locator wider than its picker", 1440, (state) => {
    state.controls.width = state.picker.width + 40;
  }, "wider than the picker");
  expectStationDossierProblem("collapsed locator", 1440, (state) => {
    state.controls.width = 120;
  }, "against a 320px floor");
  expectStationDossierProblem("missing combobox", 1440, (state) => {
    state.combo = null;
  }, "station combobox is missing");
  expectStationDossierProblem("plain text field", 1440, (state) => {
    state.combo.role = "textbox";
  }, "is not a combobox");
  expectStationDossierProblem("combobox without its listbox", 1440, (state) => {
    state.combo.listboxPresent = false;
  }, "has no listbox");
  // 79 stations unrolled under the field would push the card below the fold.
  expectStationDossierProblem("list open on arrival", 375, (state) => {
    state.combo.listboxHiddenAtRest = false;
  }, "opens before it is asked to");
  expectStationDossierProblem("expanded while closed", 375, (state) => {
    state.combo.expanded = "true";
  }, "reports itself expanded while closed");
  // The groups are how a reader finds a station they cannot name.
  expectStationDossierProblem("flattened county groups", 1440, (state) => {
    state.combo.groupCountInList = 0;
  }, "lost its county grouping");
  expectStationDossierProblem("partial option list", 1440, (state) => {
    state.combo.optionCountInList = 40;
  }, "option inventory differs from the register");
  expectStationDossierProblem("selection disagrees with the card", 1440, (state) => {
    state.combo.selectedOptions = ["基隆"];
  }, "selection and visible report disagree");
  expectStationDossierProblem("detached locator support", 1440, (state) => {
    state.supportingRowsFollowFields = false;
  }, "support rows are detached");
  expectStationDossierProblem("reversed locator DOM order", 375, (state) => {
    state.controlsFollowDomOrder = false;
  }, "keyboard order changed");
  expectStationDossierProblem("hidden rank strip", 375, (state) => {
    state.rankStrip.visible = false;
  }, "rank strip is not visible");
  expectStationDossierProblem("misplaced rank strip", 1440, (state) => {
    state.rankStrip.position = 12;
  }, "disagrees with the stated rank");
  // The measured original defect: rank 1 of 77 with the mark centred on 0%.
  expectStationDossierProblem("rank mark off the track start", 375, (state) => {
    state.rankStrip.markLeft = -2.8;
    state.rankStrip.markRight = 2.8;
  }, "drawn outside its track");
  expectStationDossierProblem("rank mark off the track end", 1440, (state) => {
    state.rankStrip.markLeft = state.rankStrip.trackWidth - 2.8;
    state.rankStrip.markRight = state.rankStrip.trackWidth + 2.8;
  }, "drawn outside its track");
  expectStationDossierProblem("hidden locator map", 375, (state) => {
    state.locator.visible = false;
  }, "locator map is not visible");
  expectStationDossierProblem("markless locator map", 375, (state) => {
    state.locator.markVisible = false;
  }, "has no visible mark");
  expectStationDossierProblem("stale locator mark", 1440, (state) => {
    state.locator.markStation = "基隆";
  }, "mark and visible report disagree");
  expectStationDossierProblem("marked but unplaceable station", 1440, (state) => {
    state.locator.markStation = "";
    state.locator.unplacedNoteVisible = true;
  }, "shows a mark for a station it cannot place");
  expectStationDossierProblem("silent unplaceable station", 1440, (state) => {
    state.locator.markStation = "";
    state.locator.markVisible = false;
  }, "does not say why the station is unplaced");
  // The offshore reason is a complete answer on its own, so this state must
  // produce NO problems. Asserted as a positive, because the failure-case
  // helper can only prove that something is rejected.
  {
    const offshore = completeStationDossierFor(1440);
    offshore.locator.markStation = "";
    offshore.locator.markVisible = false;
    offshore.locator.offshoreNoteVisible = true;
    if (stationDossierProblems(offshore).length) {
      throw new Error("station dossier predicate rejected a stated offshore station");
    }
  }
  expectStationDossierProblem("two reasons at once", 1440, (state) => {
    state.locator.markStation = "";
    state.locator.markVisible = false;
    state.locator.unplacedNoteVisible = true;
    state.locator.offshoreNoteVisible = true;
  }, "two reasons for one unplaced station");
  expectStationDossierProblem("unplaced note on a placed station", 375, (state) => {
    state.locator.unplacedNoteVisible = true;
  }, "claims a placed station is unplaced");
  expectStationDossierProblem("partial locator basemap", 1440, (state) => {
    state.locator.countyCount = 18;
  }, "counties instead of 19");
  expectStationDossierProblem("locator mark left behind", 1440, (state) => {
    state.afterChange.locatorMarkStation = "西屯";
  }, "did not follow the station change");
  expectStationDossierProblem("locator silent after change", 1440, (state) => {
    state.afterChange.locatorMarkStation = "";
    state.afterChange.locatorUnplacedVisible = false;
  }, "went silent on a station it cannot place");
  expectStationDossierProblem("option mismatch", 375, (state) => {
    state.optionCount = 78;
  }, "inventories differ");
  expectStationDossierProblem("two visible reports", 375, (state) => {
    state.visibleReportCount = 2;
  }, "exactly one report");
  expectStationDossierProblem("zero visible reports", 375, (state) => {
    state.visibleReportCount = 0;
  }, "exactly one report");
  expectStationDossierProblem("selection mismatch", 375, (state) => {
    state.visibleStation = "基隆";
  }, "selection and visible report differ");
  expectStationDossierProblem("wrong displayed station name", 375, (state) => {
    state.identityText = "錯站";
  }, "displayed identity disagrees");
  expectStationDossierProblem("hidden displayed station name", 375, (state) => {
    state.identityName.visible = false;
  }, "displayed station name is not visible");
  expectStationDossierProblem("hidden identity", 375, (state) => {
    state.identityVisible = false;
  }, "identity is not visible");
  expectStationDossierProblem("hidden year", 375, (state) => {
    state.yearVisible = false;
  }, "year is not visible");
  expectStationDossierProblem("three statistics", 375, (state) => {
    state.stats.pop();
  }, "statistic inventory changed");
  expectStationDossierProblem("hidden second statistic", 375, (state) => {
    state.stats[1].visible = false;
  }, "statistic 2 is not visible");
  expectStationDossierProblem("two comparisons", 375, (state) => {
    state.comparisons.pop();
  }, "comparison inventory changed");
  expectStationDossierProblem("hidden second comparison", 375, (state) => {
    state.comparisons[1].visible = false;
  }, "comparison 2 is not visible");
  for (const [viewportWidth, wrongColumns] of [[375, 2], [768, 1], [1024, 2], [1440, 2]]) {
    expectStationDossierProblem(`wrong ${viewportWidth}px columns`, viewportWidth, (state) => {
      state.columns = wrongColumns;
    }, `${viewportWidth}px column count`);
  }
  expectStationDossierProblem("outer bottom frame", 1440, (state) => {
    state.separators.reportBottom = 1;
  }, "decorative outer frame");
  expectStationDossierProblem("identity underline", 1440, (state) => {
    state.separators.identityBottom = 1;
  }, "redundant underline");
  expectStationDossierProblem("missing statistics entry rule", 1440, (state) => {
    state.separators.statisticsTop = 0;
  }, "statistics entry separator changed");
  expectStationDossierProblem("extra desktop statistic rule", 1440, (state) => {
    state.separators.statisticTops[0] = 1;
  }, "statistic-row separators changed");
  expectStationDossierProblem("missing comparison entry rule", 1440, (state) => {
    state.separators.comparisonsTop = 0;
  }, "comparison entry separator changed");
  expectStationDossierProblem("framed explanatory note", 1440, (state) => {
    state.separators.noteTop = 1;
  }, "explanatory note retains decorative framing");
  expectStationDossierProblem("hidden standard note", 375, (state) => {
    state.standardNote.visible = false;
  }, "standard note is not visible");
  expectStationDossierProblem("horizontal overflow", 375, (state) => {
    state.horizontalOverflow = 2;
  }, "causes horizontal overflow");
  expectStationDossierProblem("selection change not exercised", 375, (state) => {
    state.afterChange.performed = false;
  }, "change was not exercised");
  expectStationDossierProblem("two reports after change", 375, (state) => {
    state.afterChange.visibleReportCount = 2;
  }, "leave exactly one visible report");
  expectStationDossierProblem("mismatched report after change", 375, (state) => {
    state.afterChange.selectedMatchesVisible = false;
  }, "shows a different report");
  expectStationDossierProblem("wrong displayed station after change", 375, (state) => {
    state.afterChange.identityText = "錯站";
  }, "changed displayed identity disagrees");
  expectStationDossierProblem("hidden displayed station after change", 375, (state) => {
    state.afterChange.identityName.visible = false;
  }, "changed displayed station name is not visible");
  expectStationDossierProblem("hidden identity after change", 375, (state) => {
    state.afterChange.identity.visible = false;
  }, "changed station identity is not visible");
  expectStationDossierProblem("hidden year after change", 375, (state) => {
    state.afterChange.year.visible = false;
  }, "changed station year is not visible");
  expectStationDossierProblem("three statistics after change", 375, (state) => {
    state.afterChange.stats.pop();
  }, "changed station statistic inventory changed");
  expectStationDossierProblem("hidden second statistic after change", 375, (state) => {
    state.afterChange.stats[1].visible = false;
  }, "changed station statistic 2 is not visible");
  expectStationDossierProblem("two comparisons after change", 375, (state) => {
    state.afterChange.comparisons.pop();
  }, "changed station comparison inventory changed");
  expectStationDossierProblem("hidden second comparison after change", 375, (state) => {
    state.afterChange.comparisons[1].visible = false;
  }, "changed station comparison 2 is not visible");
  expectStationDossierProblem("live station omitted", 375, (state) => {
    state.afterChange.liveIncludesStation = false;
  }, "live update omits the station");
  expectStationDossierProblem("live year omitted", 375, (state) => {
    state.afterChange.liveIncludesYear = false;
  }, "live update omits the year");
  expectStationDossierProblem("live first statistic omitted", 375, (state) => {
    state.afterChange.liveIncludesFirstStat = false;
  }, "live update omits the first statistic");
  expectStationDossierProblem("live third statistic omitted", 375, (state) => {
    state.afterChange.liveIncludesThirdStat = false;
  }, "live update omits the third statistic");
  expectStationDossierProblem("selection restore not exercised", 375, (state) => {
    state.restored.performed = false;
  }, "selection restoration was not exercised");
  expectStationDossierProblem("two reports after restore", 375, (state) => {
    state.restored.visibleReportCount = 2;
  }, "restoration does not leave exactly one visible report");
  expectStationDossierProblem("mismatched report after restore", 375, (state) => {
    state.restored.selectedMatchesVisible = false;
  }, "restoration shows a different report");
  expectStationDossierProblem("wrong displayed station after restore", 375, (state) => {
    state.restored.identityText = "錯站";
  }, "restored displayed identity disagrees");
  expectStationDossierProblem("hidden displayed station after restore", 375, (state) => {
    state.restored.identityName.visible = false;
  }, "restored displayed station name is not visible");
  expectStationDossierProblem("restored live station omitted", 375, (state) => {
    state.restored.liveIncludesStation = false;
  }, "restored live update omits the station");
  if (missedStationDossierProblems.length) {
    throw new Error(
      `the station dossier predicate accepts ${missedStationDossierProblems.join(", ")}`,
    );
  }

  const completeStationRegister = {
    selectorVisible: false,
    liveVisible: false,
    noScriptVisible: true,
    reportCount: 79,
    visibleReportCount: 79,
    stationNameCount: 79,
    visibleStationNameCount: 79,
    matchingStationNameCount: 79,
    ordered: true,
    standardNotes: 1,
    conversionNotes: 0,
  };
  const missedStationRegisterProblems = [];
  const expectStationRegisterProblem = (name, mode, mutate, expected) => {
    const state = structuredClone(completeStationRegister);
    if (mode === "print") state.noScriptVisible = false;
    mutate(state);
    const problems = stationRegisterProblems(state, mode);
    if (!problems.some((problem) => problem.includes(expected))) {
      missedStationRegisterProblems.push(name);
    }
  };
  if (stationRegisterProblems(completeStationRegister, "no-JavaScript").length) {
    throw new Error("station no-JavaScript register predicate rejected the control");
  }
  const completePrintRegister = { ...completeStationRegister, noScriptVisible: false };
  if (stationRegisterProblems(completePrintRegister, "print").length) {
    throw new Error("station print register predicate rejected the control");
  }
  expectStationRegisterProblem("visible no-JavaScript controls", "no-JavaScript", (state) => {
    state.selectorVisible = true;
  }, "controls remain visible");
  expectStationRegisterProblem("missing no-JavaScript notice", "no-JavaScript", (state) => {
    state.noScriptVisible = false;
  }, "notice is not visible");
  expectStationRegisterProblem("incomplete no-JavaScript reports", "no-JavaScript", (state) => {
    state.visibleReportCount = 78;
  }, "complete report register is unavailable");
  expectStationRegisterProblem("hidden no-JavaScript station name", "no-JavaScript", (state) => {
    state.visibleStationNameCount = 78;
  }, "visible station-name register is unavailable");
  expectStationRegisterProblem("missing no-JavaScript station name", "no-JavaScript", (state) => {
    state.stationNameCount = 78;
  }, "station-name inventory changed");
  expectStationRegisterProblem("wrong no-JavaScript station name", "no-JavaScript", (state) => {
    state.matchingStationNameCount = 78;
  }, "displayed station identities disagree");
  expectStationRegisterProblem("reordered no-JavaScript reports", "no-JavaScript", (state) => {
    state.ordered = false;
  }, "report order changed");
  expectStationRegisterProblem("missing no-JavaScript note", "no-JavaScript", (state) => {
    state.standardNotes = 0;
  }, "interpretation notes changed");
  expectStationRegisterProblem("unexpected no-JavaScript conversion note", "no-JavaScript", (state) => {
    state.conversionNotes = 1;
  }, "interpretation notes changed");
  expectStationRegisterProblem("visible print selector", "print", (state) => {
    state.selectorVisible = true;
  }, "controls remain visible");
  expectStationRegisterProblem("visible print live region", "print", (state) => {
    state.liveVisible = true;
  }, "controls remain visible");
  expectStationRegisterProblem("visible print no-JavaScript notice", "print", (state) => {
    state.noScriptVisible = true;
  }, "controls remain visible");
  expectStationRegisterProblem("missing print reports", "print", (state) => {
    state.reportCount = 0;
    state.visibleReportCount = 0;
  }, "complete report register is unavailable");
  expectStationRegisterProblem("hidden print station name", "print", (state) => {
    state.visibleStationNameCount = 78;
  }, "visible station-name register is unavailable");
  expectStationRegisterProblem("missing print station name", "print", (state) => {
    state.stationNameCount = 78;
  }, "station-name inventory changed");
  expectStationRegisterProblem("wrong print station name", "print", (state) => {
    state.matchingStationNameCount = 78;
  }, "displayed station identities disagree");
  expectStationRegisterProblem("reordered print reports", "print", (state) => {
    state.ordered = false;
  }, "report order changed");
  expectStationRegisterProblem("missing print note", "print", (state) => {
    state.standardNotes = 0;
  }, "interpretation notes changed");
  expectStationRegisterProblem("unexpected print conversion note", "print", (state) => {
    state.conversionNotes = 1;
  }, "interpretation notes changed");
  if (missedStationRegisterProblems.length) {
    throw new Error(
      `the station register predicate accepts ${missedStationRegisterProblems.join(", ")}`,
    );
  }
  console.log("site quality station dossier self-test passed");
  console.log("site quality station locator self-test passed");
  const detectionPart = (top, bottom, sourceIndex, extra = {}) => ({
    display: "block",
    visibility: "visible",
    rendered: true,
    hidden: false,
    ariaHidden: false,
    inert: false,
    accessible: true,
    opacity: 1,
    top,
    right: 355,
    bottom,
    left: 20,
    width: 335,
    height: bottom - top,
    sourceIndex,
    cssOrder: 0,
    selfOverflowX: 0,
    selfOverflowY: 0,
    ancestorClipped: false,
    cssClip: false,
    cssClipPath: false,
    detailsAncestor: false,
    ...extra,
  });
  const title = detectionPart(90, 125, 10);
  const key = detectionPart(135, 265, 20);
  const primaryPlot = detectionPart(285, 465, 30);
  const caption = detectionPart(475, 545, 40);
  const comparison = detectionPart(560, 650, 50);
  const boundary = detectionPart(665, 770, 60, { collapsed: false, tagName: "ASIDE" });
  const methodEvidence = detectionPart(790, 850, 70);
  const detectionBoundaryText =
    "「測不到」不等於「等於零」。" +
    "每個事件的實際通過數都低於各自純靠機率的預期。噪音底線高於訊號。" +
    "這批資料與這個方法，無法分辨這種大小的效應—不是「這些事件沒有影響」。" +
    "非偵測不是「事件沒有發生」或「介入無效」的證明。" +
    "本分析沒有驗證機組的逐時操作或燃料狀態。";
  const completeDetectionBrief = {
    mode: "normal",
    theme: "light",
    counts: {
      readingKey: 1,
      comparison: 1,
      boundary: 1,
      semanticRows: 3,
      eventHooks: 3,
    },
    regions: { readingKey: key, comparison, boundary },
    landmarks: {
      title,
      key,
      primaryPlot,
      caption,
      comparison,
      boundary,
      methodEvidence,
    },
    readingSteps: [
      {
        key: "placebo",
        visibleText: "先看灰線：沒有事件標記時，同一程序仍會算出的差額。",
        accessibleText: "先看灰線：沒有事件標記時，同一程序仍會算出的差額。",
        top: 145,
        right: 355,
        bottom: 175,
        left: 40,
        width: 315,
        height: 30,
        sourceIndex: 21,
        cssOrder: 0,
      },
      {
        key: "event",
        visibleText: "再看橘點：事件窗口各測站的觀測－預測差額。",
        accessibleText: "再看橘點：事件窗口各測站的觀測－預測差額。",
        top: 180,
        right: 355,
        bottom: 210,
        left: 40,
        width: 315,
        height: 30,
        sourceIndex: 22,
        cssOrder: 0,
      },
      {
        key: "threshold",
        visibleText: "最後看門檻：通過數是否高於純靠機率的預期。",
        accessibleText: "最後看門檻：通過數是否高於純靠機率的預期。",
        top: 215,
        right: 355,
        bottom: 245,
        left: 40,
        width: 315,
        height: 30,
        sourceIndex: 23,
        cssOrder: 0,
      },
    ],
    eventRows: EXPECTED_DETECTION_EVENTS.map((event, index) => {
      const exactText =
        `${event.event} · ${detectionEventKindLabel(event.kind)}` +
        `實際通過 ${event.observed} 站；純靠機率的預期為 ${event.expected} 站。`;
      return {
        event: event.event,
        kind: event.kind,
        observed: event.observed,
        expected: event.expected,
        hooked: true,
        rowTag: "DIV",
        directChildTags: ["DT", "DD"],
        visibleText: exactText,
        accessibleText: exactText,
        inspection: detectionPart(570 + index * 25, 590 + index * 25, 51 + index),
      };
    }),
    boundaryText: detectionBoundaryText,
    pageText: detectionBoundaryText,
    viewport: { width: 375, height: 812 },
    document: { clientWidth: 375, scrollWidth: 375 },
  };
  const invalidDetectionPayloads = [
    ["boolean observed value", "n_credible is not a nonnegative integer", (payload) => {
      payload.events[0].n_credible = true;
    }],
    ["numeric-string expected value", "n_expected_by_chance is not nonnegative", (payload) => {
      payload.events[0].n_expected_by_chance = "3.3";
    }],
    ["NaN observed value", "n_credible is not a nonnegative integer", (payload) => {
      payload.events[0].n_credible = Number.NaN;
    }],
    ["missing observed key", "n_credible is not a nonnegative integer", (payload) => {
      delete payload.events[0].n_credible;
    }],
    ["duplicate event identity", "is duplicated", (payload) => {
      payload.events[1].event = payload.events[0].event;
    }],
    ["missing event inventory", "event inventory is 2, expected 3", (payload) => {
      payload.events.pop();
    }],
    ["extra event inventory", "event inventory is 4, expected 3", (payload) => {
      payload.events.push({
        ...structuredClone(payload.events[0]),
        event: "額外事件",
      });
    }],
    ["reordered event inventory", "event 1 identity", (payload) => {
      [payload.events[0], payload.events[1]] = [payload.events[1], payload.events[0]];
    }],
    ["wrong event identity", "event 1 identity", (payload) => {
      payload.events[0].event = "錯誤事件";
    }],
    ["wrong event kind", "event 3 kind", (payload) => {
      payload.events[2].kind = "window";
    }],
    ["negative observed value", "n_credible is not a nonnegative integer", (payload) => {
      payload.events[0].n_credible = -1;
    }],
    ["fractional observed value", "n_credible is not a nonnegative integer", (payload) => {
      payload.events[0].n_credible = 1.5;
    }],
    ["negative expected value", "n_expected_by_chance is not nonnegative", (payload) => {
      payload.events[0].n_expected_by_chance = -0.1;
    }],
    ["below-chance relationship no longer holds", "no longer supports the below-chance claim", (payload) => {
      payload.events[0].n_credible = 4;
    }],
  ];
  const acceptedInvalidDetectionPayloads = [];
  const misdiagnosedInvalidDetectionPayloads = [];
  for (const [name, expectedError, mutate] of invalidDetectionPayloads) {
    const payload = structuredClone(DETECTION_LIMIT_PAYLOAD);
    mutate(payload);
    try {
      detectionExpectedEventsFromPayload(payload);
      acceptedInvalidDetectionPayloads.push(name);
    } catch (error) {
      // Expected: the public payload is the independent numerical authority.
      if (!(error instanceof Error) || !error.message.includes(expectedError)) {
        misdiagnosedInvalidDetectionPayloads.push(
          `${name} -> ${expectedError} (received ${error instanceof Error ? error.message : String(error)})`,
        );
      }
    }
  }
  const detectionPreflightMisses = [];
  if (acceptedInvalidDetectionPayloads.length || misdiagnosedInvalidDetectionPayloads.length) {
    detectionPreflightMisses.push(
      `payload parser accepts ${acceptedInvalidDetectionPayloads.join(", ")}; ` +
        `misdiagnoses ${misdiagnosedInvalidDetectionPayloads.join(", ")}`,
    );
  }
  if (
    detectionLimitationBriefProblems(
      completeDetectionBrief,
      EXPECTED_DETECTION_EVENTS,
      completeDetectionBrief.viewport,
    ).length
  ) {
    throw new Error("the detection limitation-brief predicate rejects its complete control");
  }
  const detectionMutations = [
    ["missing reading key", "reading key count is 0", (state) => {
      state.counts.readingKey = 0;
      state.regions.readingKey = null;
    }],
    ["duplicate reading key", "reading key count is 2", (state) => {
      state.counts.readingKey = 2;
    }],
    ["missing comparison", "comparison count is 0", (state) => {
      state.counts.comparison = 0;
      state.regions.comparison = null;
    }],
    ["duplicate comparison", "comparison count is 2", (state) => {
      state.counts.comparison = 2;
    }],
    ["missing boundary", "boundary count is 0", (state) => {
      state.counts.boundary = 0;
      state.regions.boundary = null;
    }],
    ["duplicate boundary", "boundary count is 2", (state) => {
      state.counts.boundary = 2;
    }],
    ["hidden region", "reading key is hidden", (state) => {
      state.regions.readingKey.hidden = true;
      state.regions.readingKey.accessible = false;
    }],
    ["aria-hidden region", "comparison is aria-hidden", (state) => {
      state.regions.comparison.ariaHidden = true;
      state.regions.comparison.accessible = false;
    }],
    ["display-none region", "boundary display is none", (state) => {
      state.regions.boundary.display = "none";
      state.regions.boundary.accessible = false;
    }],
    ["visibility-hidden region", "comparison visibility is hidden", (state) => {
      state.regions.comparison.visibility = "hidden";
      state.regions.comparison.accessible = false;
    }],
    ["non-rendered ancestor", "boundary is not rendered", (state) => {
      state.regions.boundary.rendered = false;
      state.regions.boundary.accessible = false;
    }],
    ["zero-opacity region", "reading key opacity is zero", (state) => {
      state.regions.readingKey.opacity = 0;
    }],
    ["zero-width region", "comparison has no rendered area", (state) => {
      state.regions.comparison.width = 0;
    }],
    ["zero-height region", "boundary has no rendered area", (state) => {
      state.regions.boundary.height = 0;
    }],
    ["inert ancestor", "boundary is excluded from accessibility", (state) => {
      state.regions.boundary.inert = true;
      state.regions.boundary.accessible = false;
    }],
    ["missing reading step", "reading step inventory changed", (state) => {
      state.readingSteps.splice(1, 1);
    }],
    ["replaced reading step", "reading step 2 key changed", (state) => {
      state.readingSteps[1].key = "result";
    }],
    ["reordered reading step", "reading step 1 key changed", (state) => {
      [state.readingSteps[0], state.readingSteps[1]] =
        [state.readingSteps[1], state.readingSteps[0]];
    }],
    ["key relocated after primary plot", "reading key no longer precedes primary plot", (state) => {
      state.landmarks.key.sourceIndex = 35;
      state.landmarks.key.top = 470;
    }],
    ["wrong step accessible text despite decorative copy", "reading step 1 text changed", (state) => {
      state.pageText += state.readingSteps[0].visibleText;
      state.readingSteps[0].accessibleText = "先看裝飾圖示。";
    }],
    ["missing event", "event row inventory is 2", (state) => {
      state.eventRows.splice(1, 1);
    }],
    ["duplicate event", "event identity is duplicated", (state) => {
      state.eventRows.push(structuredClone(state.eventRows[0]));
    }],
    ["extra event", "event row inventory is 4", (state) => {
      const row = structuredClone(state.eventRows[0]);
      row.event = "額外事件";
      row.visibleText = "額外事件 · 窗口事件：觀測－預測差額實際通過 1 站；純靠機率的預期為 3.3 站。";
      row.accessibleText = row.visibleText;
      state.eventRows.push(row);
      state.counts.semanticRows = 4;
      state.counts.eventHooks = 4;
    }],
    ["reordered event", "event row 1 identity changed", (state) => {
      [state.eventRows[0], state.eventRows[1]] = [state.eventRows[1], state.eventRows[0]];
    }],
    ["wrong exact event identity", "event row 1 identity changed", (state) => {
      state.eventRows[0].event += " ";
    }],
    ["wrong observed passes", "event row 1 observed value changed", (state) => {
      state.eventRows[0].observed = 2;
    }],
    ["wrong chance expectation", "event row 1 expected value changed", (state) => {
      state.eventRows[0].expected = 3.4;
    }],
    ["wrong rendered event kind", "event row 1 kind changed", (state) => {
      state.eventRows[0].kind = "trend_break";
    }],
    ["unhooked extra semantic row", "semantic row inventory is 4", (state) => {
      state.eventRows.push({
        event: "",
        kind: "",
        observed: null,
        expected: null,
        visibleText: "額外說明 實際通過 9 站。",
        accessibleText: "額外說明 實際通過 9 站。",
        hooked: false,
        rowTag: "DIV",
        directChildTags: ["DT", "DD"],
        inspection: detectionPart(620, 650, 59),
      });
      state.counts.semanticRows = 4;
    }],
    ["malformed direct description pair", "event row 1 description structure changed", (state) => {
      state.eventRows[0].directChildTags = ["DT", "P"];
    }],
    ["conflicting visible event quantity", "event row 1 visible text changed", (state) => {
      state.eventRows[0].visibleText += "實際通過 9 站。";
    }],
    ["conflicting accessible event quantity", "event row 1 accessible text changed", (state) => {
      state.eventRows[0].accessibleText += "實際通過 9 站。";
    }],
    ["zero-opacity event row", "event row 1 opacity is zero", (state) => {
      state.eventRows[0].inspection = detectionPart(570, 595, 51, { opacity: 0 });
    }],
    ["hidden event row", "event row 1 is hidden", (state) => {
      state.eventRows[0].inspection = detectionPart(570, 595, 51, {
        hidden: true,
        accessible: false,
      });
    }],
    ["off-canvas event row", "event row 1 is horizontally off-canvas", (state) => {
      state.eventRows[0].inspection = detectionPart(570, 595, 51, {
        left: 400,
        right: 500,
      });
    }],
    ["clipped event row", "event row 1 is clipped by an ancestor", (state) => {
      state.eventRows[0].inspection = detectionPart(570, 595, 51, { ancestorClipped: true });
    }],
    ["boolean observed substitution", "event row 1 observed value is not a nonnegative integer", (state) => {
      state.eventRows[0].observed = true;
    }],
    ["numeric-string expected substitution", "event row 1 expected value is not nonnegative", (state) => {
      state.eventRows[0].expected = "3.3";
    }],
    ["event row missing exact key", "event row 1 keys changed", (state) => {
      delete state.eventRows[0].expected;
    }],
    ["event row has extra key", "event row 1 keys changed", (state) => {
      state.eventRows[0].unexpected = 3.3;
    }],
    ["event row NaN substitution", "event row 1 observed value is not a nonnegative integer", (state) => {
      state.eventRows[0].observed = Number.NaN;
    }],
    ["event accessible text omits expected quantity", "event row 1 accessible text changed", (state) => {
      state.eventRows[0].accessibleText = `${state.eventRows[0].event} 實際通過 1 站。`;
    }],
    ["trend-break event omitted because only two plots exist", "trend-break event row is missing", (state) => {
      state.eventRows.pop();
    }],
    ["missing required boundary claim", "boundary is missing required claim", (state) => {
      state.boundaryText = state.boundaryText.replace("噪音底線高於訊號。", "");
    }],
    ["missing below-chance boundary claim", "boundary is missing required claim", (state) => {
      state.boundaryText = state.boundaryText.replace(
        "每個事件的實際通過數都低於各自純靠機率的預期。",
        "",
      );
    }],
    ["missing event-occurrence boundary claim", "boundary is missing required claim", (state) => {
      state.boundaryText = state.boundaryText.replace(
        "非偵測不是「事件沒有發生」或「介入無效」的證明。",
        "",
      );
    }],
    ["weakened required boundary claim", "boundary is missing required claim", (state) => {
      state.boundaryText = state.boundaryText.replace("無法分辨", "不容易分辨");
    }],
    ["approved phrase outside boundary", "boundary is missing required claim", (state) => {
      state.boundaryText = state.boundaryText.replace("沒有驗證機組的逐時操作或燃料狀態", "");
      state.pageText += "沒有驗證機組的逐時操作或燃料狀態";
    }],
    ["below-chance phrase outside boundary", "boundary is missing required claim", (state) => {
      const claim = "每個事件的實際通過數都低於各自純靠機率的預期。";
      state.boundaryText = state.boundaryText.replace(claim, "");
      state.pageText += claim;
    }],
    ["event-occurrence phrase outside boundary", "boundary is missing required claim", (state) => {
      const claim = "非偵測不是「事件沒有發生」或「介入無效」的證明。";
      state.boundaryText = state.boundaryText.replace(claim, "");
      state.pageText += claim;
    }],
    ["legacy below-chance conclusion outside boundary", "boundary-local inference is duplicated", (state) => {
      state.pageText += "三個事件的實際通過數都低於機率預期。";
    }],
    ["boundary before comparison", "boundary no longer follows comparison", (state) => {
      state.landmarks.boundary.sourceIndex = 45;
      state.landmarks.boundary.top = 540;
    }],
    ["boundary after method evidence", "boundary no longer precedes method evidence", (state) => {
      state.landmarks.boundary.sourceIndex = 75;
      state.landmarks.boundary.top = 860;
    }],
    ["opening pair after independent method evidence", "opening order changed", (state) => {
      state.landmarks.comparison.sourceIndex = 75;
      state.landmarks.comparison.top = 865;
      state.landmarks.boundary.sourceIndex = 80;
      state.landmarks.boundary.top = 940;
    }],
    ["title after reading key", "opening order changed", (state) => {
      state.landmarks.title.sourceIndex = 25;
      state.landmarks.title.top = 275;
    }],
    ["caption before primary plot", "opening order changed", (state) => {
      state.landmarks.caption.sourceIndex = 25;
      state.landmarks.caption.top = 275;
    }],
    ["negative source index", "title landmark geometry is invalid", (state) => {
      state.landmarks.title.sourceIndex = -1;
    }],
    ["reading key in disclosure", "reading key is user-collapsible", (state) => {
      state.regions.readingKey.detailsAncestor = true;
    }],
    ["comparison in disclosure", "comparison is user-collapsible", (state) => {
      state.regions.comparison.detailsAncestor = true;
    }],
    ["boundary in disclosure", "boundary is user-collapsible", (state) => {
      state.regions.boundary.detailsAncestor = true;
    }],
    ["boundary collapsed disclosure", "boundary became a collapsed disclosure", (state) => {
      state.regions.boundary.collapsed = true;
      state.regions.boundary.tagName = "DETAILS";
    }],
    ["375x812 plot at viewport boundary", "primary plot does not enter the first viewport", (state) => {
      state.landmarks.primaryPlot.top = 812;
      state.landmarks.primaryPlot.bottom = 992;
    }],
    ["horizontal page overflow", "document scrolls sideways", (state) => {
      state.document.scrollWidth = 377;
    }],
    ["missing no-JavaScript region", "no-JavaScript reading key count is 0", (state) => {
      state.mode = "no-js";
      state.counts.readingKey = 0;
      state.regions.readingKey = null;
    }],
    ["hidden print region", "print boundary display is none", (state) => {
      state.mode = "print";
      state.regions.boundary.display = "none";
    }],
    ["reordered zoom region", "zoom boundary no longer follows comparison", (state) => {
      state.mode = "zoom";
      state.landmarks.boundary.sourceIndex = 45;
      state.landmarks.boundary.top = 540;
    }],
    ["visual order manufactured with CSS order", "reading key uses CSS order", (state) => {
      state.landmarks.key.cssOrder = 2;
    }],
    ["reading step visual order manufactured with CSS order", "reading step 1 uses CSS order", (state) => {
      state.readingSteps[0].cssOrder = 2;
    }],
  ];
  const missedDetectionMutations = [];
  for (const [name, expectedProblem, mutate] of detectionMutations) {
    const state = structuredClone(completeDetectionBrief);
    mutate(state);
    const problems = detectionLimitationBriefProblems(
      state,
      EXPECTED_DETECTION_EVENTS,
      state.viewport,
    );
    if (!problems.some((problem) => problem.includes(expectedProblem))) {
      missedDetectionMutations.push(`${name} -> ${expectedProblem}`);
    }
  }
  if (missedDetectionMutations.length) {
    detectionPreflightMisses.push(
      `limitation-brief predicate accepts ${missedDetectionMutations.join(", ")}`,
    );
  }
  if (detectionPreflightMisses.length) {
    throw new Error(`the detection preflight misses ${detectionPreflightMisses.join("; ")}`);
  }
  console.log("site quality detection limitation brief self-test passed");

  const healthPart = (top, sourceIndex, extra = {}) => ({
    display: "block",
    visibility: "visible",
    rendered: true,
    hidden: false,
    ariaHidden: false,
    inert: false,
    accessible: true,
    opacity: 1,
    top,
    right: 700,
    bottom: top + 40,
    left: 100,
    width: 600,
    height: 40,
    sourceIndex,
    cssOrder: 0,
    selfOverflowX: 0,
    selfOverflowY: 0,
    ancestorClipped: false,
    cssClip: false,
    cssClipPath: false,
    detailsAncestor: false,
    ...extra,
  });
  const healthAssumptionRows = [
    [
      "counterfactual",
      "比較基準圖 7.1 與圖 7.2 量化四種反事實濃度造成的差異。",
    ],
    [
      "response",
      "暴露反應函數本章只採用一條具可追溯來源的函數；適用範圍與外推界線在後文公開。",
    ],
    [
      "population",
      "暴露人口本專案沒有人口與個人暴露資料，因此不報死亡人數，也不把測站中位數稱為誰的暴露。",
    ],
  ];
  const healthExpected = {
    seriesCount: 4,
    functionCount: 1,
    yearsCount: 2,
    spreadCount: 2,
    deaths: "死亡人數需要人口與基礎死亡率。",
    exposure: "測站平均不是人口加權暴露。",
    readingBodies: ["範圍與結論", "端點與說明"],
  };
  const invalidHealthPayloads = [
    ["top-level shape", "top-level shape changed", (payload) => { payload.extra = true; }],
    ["function count", "response-function inventory changed", (payload) => {
      payload.functions = [];
    }],
    ["boolean response value", "rr_per_10 is invalid", (payload) => {
      payload.functions[0].rr_per_10 = true;
    }],
    ["series count", "counterfactual-series inventory changed", (payload) => {
      payload.series.pop();
    }],
    ["duplicate series identity", "series identity is duplicated", (payload) => {
      payload.series[1].name = payload.series[0].name;
    }],
    ["headline shape", "headline shape changed", (payload) => {
      delete payload.headline.last_range;
    }],
    // Replaces a fixture that renamed gbd_high and expected a rejection. The
    // range ends are resolved by value now, which a rename cannot break — and
    // these two can. See the note beside the resolution above for why the old
    // pairing was wrong in both this gate and check_publication_structure.py.
    ["headline range off every counterfactual", "does not resolve to one counterfactual", (payload) => {
      payload.headline.last_range = [payload.headline.last_range[0], 0.4242];
    }],
    ["counterfactuals indistinguishable", "does not resolve to one counterfactual", (payload) => {
      for (const row of payload.series) row.paf = payload.series[0].paf.slice();
    }],
    ["empty deaths boundary", "no-inference boundary changed", (payload) => {
      payload.not_reported.deaths = "";
    }],
    ["years/spread mismatch", "years/spread inventory changed", (payload) => {
      payload.spread_share.pop();
    }],
    ["non-finite spread", "spread value is invalid", (payload) => {
      payload.spread_share[0] = Number.NaN;
    }],
  ];
  for (const [name, expectedError, mutate] of invalidHealthPayloads) {
    const payload = structuredClone(HEALTH_STORY_PAYLOAD);
    const before = JSON.stringify(payload);
    mutate(payload);
    if (JSON.stringify(payload) === before) {
      throw new Error(`health payload mutation ${name} did not change the control`);
    }
    try {
      healthExpectedEvidenceFromPayload(payload);
    } catch (error) {
      if (!String(error?.message ?? error).includes(expectedError)) {
        throw new Error(
          `health payload mutation ${name} raised ${String(error)}, expected ${expectedError}`,
        );
      }
      continue;
    }
    throw new Error(`health payload parser accepts ${name}`);
  }
  const completeHealthLedger = {
    mode: "normal",
    counts: {
      ledger: 1,
      readingBand: 1,
      boundaries: 1,
      assumptionRows: 3,
      readingRows: 2,
      inferenceRows: 2,
    },
    regions: {
      ledger: healthPart(180, 20),
      readingBand: healthPart(520, 70),
      boundaries: healthPart(760, 100),
    },
    landmarks: {
      lede: healthPart(100, 10),
      ledger: healthPart(180, 20),
      primaryTitle: healthPart(280, 40),
      primaryPlot: healthPart(340, 50),
      caption: healthPart(460, 60),
      readingBand: healthPart(520, 70),
      figure2Title: healthPart(680, 90),
      boundaries: healthPart(760, 100),
    },
    assumptionRows: healthAssumptionRows.map(([key, text], index) => ({
      key,
      visibleText: text,
      accessibleText: text,
      inspection: healthPart(190 + index * 30, 21 + index),
    })),
    readingRows: [
      {
        key: "robust",
        heading: "下降幅度對比較基準穩健",
        accessibleHeading: "下降幅度對比較基準穩健",
        bodyText: "範圍與結論",
        accessibleBody: "範圍與結論",
        inspection: healthPart(530, 71),
      },
      {
        key: "sensitive",
        heading: "當前水準對比較基準敏感",
        accessibleHeading: "當前水準對比較基準敏感",
        bodyText: "端點與說明",
        accessibleBody: "端點與說明",
        inspection: healthPart(580, 72),
      },
    ],
    inferenceRows: [
      {
        key: "deaths",
        visibleText: "不報死亡人數死亡人數需要人口與基礎死亡率。",
        accessibleText: "不報死亡人數死亡人數需要人口與基礎死亡率。",
        inspection: healthPart(770, 101),
      },
      {
        key: "exposure",
        visibleText: "不宣稱這是誰的暴露測站平均不是人口加權暴露。",
        accessibleText: "不宣稱這是誰的暴露測站平均不是人口加權暴露。",
        inspection: healthPart(820, 102),
      },
    ],
    figure2Title: "比較基準造成的落差佔估計值多少？",
    viewport: { width: 375, height: 812 },
    document: { clientWidth: 375, scrollWidth: 375 },
  };
  const healthPreflightMisses = [];
  const controlHealthProblems = healthAssumptionLedgerProblems(
    completeHealthLedger,
    healthExpected,
    completeHealthLedger.viewport,
  );
  if (controlHealthProblems.length) {
    healthPreflightMisses.push(`complete control: ${controlHealthProblems.join(", ")}`);
  }
  const healthMutations = [
    ["invalid mode", "mode is invalid", (state) => { state.mode = "unsupported"; }],
    ["missing ledger", "ledger count is 0", (state) => {
      state.counts.ledger = 0;
      state.regions.ledger = null;
    }],
    ["extra reading band", "reading band count is 2", (state) => {
      state.counts.readingBand = 2;
    }],
    ["hidden boundary", "boundary is hidden", (state) => {
      state.regions.boundaries.hidden = true;
    }],
    ["aria-hidden ledger", "ledger is aria-hidden", (state) => {
      state.regions.ledger.ariaHidden = true;
    }],
    ["inert reading band", "reading band is excluded from accessibility", (state) => {
      state.regions.readingBand.inert = true;
    }],
    ["zero-opacity boundary", "boundary opacity is zero", (state) => {
      state.regions.boundaries.opacity = 0;
    }],
    ["zero-area ledger", "ledger has no rendered area", (state) => {
      state.regions.ledger.width = 0;
    }],
    ["self overflow", "reading band clips its own content", (state) => {
      state.regions.readingBand.selfOverflowX = 4;
    }],
    ["ancestor clipping", "boundary is clipped by an ancestor", (state) => {
      state.regions.boundaries.ancestorClipped = true;
    }],
    ["CSS clip", "ledger uses CSS clip", (state) => {
      state.regions.ledger.cssClip = true;
    }],
    ["CSS clip-path", "reading band uses CSS clip-path", (state) => {
      state.regions.readingBand.cssClipPath = true;
    }],
    ["off-canvas boundary", "boundary is horizontally off-canvas", (state) => {
      state.regions.boundaries.left = 400;
      state.regions.boundaries.right = 700;
    }],
    ["CSS order", "ledger uses CSS order", (state) => {
      state.regions.ledger.cssOrder = 1;
    }],
    ["open disclosure", "ledger is user-collapsible", (state) => {
      state.regions.ledger.detailsAncestor = true;
    }],
    ["missing assumption row", "assumption row inventory changed", (state) => {
      state.assumptionRows.pop();
      state.counts.assumptionRows = 2;
    }],
    ["wrong assumption key", "assumption row 1 key changed", (state) => {
      state.assumptionRows[0].key = "response";
    }],
    ["wrong assumption text", "assumption row 1 visible text changed", (state) => {
      state.assumptionRows[0].visibleText = "比較基準";
    }],
    ["wrong assumption AX text", "assumption row 1 accessible text changed", (state) => {
      state.assumptionRows[0].accessibleText = "另一個名稱";
    }],
    ["hidden assumption row", "assumption row 1 is hidden", (state) => {
      state.assumptionRows[0].inspection.hidden = true;
    }],
    ["reordered reading row", "reading row 1 key changed", (state) => {
      state.readingRows.reverse();
    }],
    ["wrong reading heading", "reading row 1 heading changed", (state) => {
      state.readingRows[0].heading = "假設穩健";
    }],
    ["wrong reading AX heading", "reading row 1 accessible heading changed", (state) => {
      state.readingRows[0].accessibleHeading = "假設穩健";
    }],
    ["missing reading body", "reading row 1 body changed", (state) => {
      state.readingRows[0].bodyText = "";
    }],
    ["wrong reading AX body", "reading row 1 accessible body changed", (state) => {
      state.readingRows[0].accessibleBody = "另一段解讀";
    }],
    ["assumption rows visually reversed", "assumption row visual order changed", (state) => {
      state.assumptionRows[0].inspection.left = 500;
      state.assumptionRows[1].inspection.left = 300;
      state.assumptionRows[2].inspection.left = 100;
      for (const row of state.assumptionRows) row.inspection.top = 190;
    }],
    ["missing inference row", "inference row inventory changed", (state) => {
      state.inferenceRows.pop();
      state.counts.inferenceRows = 1;
    }],
    ["wrong inference key", "inference row 1 key changed", (state) => {
      state.inferenceRows[0].key = "exposure";
    }],
    ["wrong inference text", "inference row 1 visible text changed", (state) => {
      state.inferenceRows[0].visibleText = "不報死亡人數";
    }],
    ["wrong inference AX text", "inference row 1 accessible text changed", (state) => {
      state.inferenceRows[0].accessibleText = "不報人數";
    }],
    ["stale Figure 7.2 title", "Figure 7.2 title changed", (state) => {
      state.figure2Title = "不同暴露反應函數會把結果推動多少？";
    }],
    ["source order", "opening order changed", (state) => {
      state.landmarks.ledger.sourceIndex = 45;
    }],
    ["visual order", "opening order changed", (state) => {
      state.landmarks.ledger.top = 360;
    }],
    ["manufactured landmark order", "primary plot uses CSS order", (state) => {
      state.landmarks.primaryPlot.cssOrder = 2;
    }],
    ["mobile primary title below viewport", "primary evidence does not enter the first viewport", (state) => {
      state.landmarks.primaryTitle.top = 812;
    }],
    ["short-desktop plot below 55vh", "primary plot starts at or below 55vh", (state) => {
      state.viewport = { width: 1280, height: 720 };
      state.document = { clientWidth: 1280, scrollWidth: 1280 };
      state.landmarks.primaryPlot.top = 397;
      state.landmarks.primaryPlot.bottom = 596;
    }],
    ["page overflow", "document scrolls sideways", (state) => {
      state.document.scrollWidth = 376;
    }],
    ["missing no-JavaScript ledger", "no-JavaScript ledger count is 0", (state) => {
      state.mode = "no-js";
      state.counts.ledger = 0;
      state.regions.ledger = null;
    }],
    ["hidden print boundary", "print boundary display is none", (state) => {
      state.mode = "print";
      state.regions.boundaries.display = "none";
    }],
    ["zoom reading reorder", "zoom opening order changed", (state) => {
      state.mode = "zoom";
      state.landmarks.readingBand.top = 720;
    }],
  ];
  for (const [name, expectedProblem, mutate] of healthMutations) {
    const state = structuredClone(completeHealthLedger);
    const before = JSON.stringify(state);
    mutate(state);
    if (JSON.stringify(state) === before) {
      healthPreflightMisses.push(`${name} mutation did not change the control`);
      continue;
    }
    const problems = healthAssumptionLedgerProblems(state, healthExpected, state.viewport);
    if (!problems.some((problem) => problem.includes(expectedProblem))) {
      healthPreflightMisses.push(`${name} -> ${expectedProblem}`);
    }
  }
  const missingHealthZoomProblems = textZoomRouteMatrixProblems(
    TEXT_ZOOM_ROUTES.filter((route) => route !== "/health/"),
  );
  if (!missingHealthZoomProblems.some((problem) => problem.includes("/health/"))) {
    throw new Error("the text-zoom route contract accepts a missing /health/");
  }
  if (healthPreflightMisses.length) {
    throw new Error(`the health preflight misses ${healthPreflightMisses.join("; ")}`);
  }
  console.log("site quality health assumption-ledger self-test passed");

  const invalidForecastPayloads = [
    ["top-level shape", "top-level shape changed", (payload) => { payload.extra = true; }],
    ["baseline count", "baseline inventory changed", (payload) => { payload.baselines.pop(); }],
    ["baseline order", "baseline identity or order changed", (payload) => {
      payload.baselines.reverse();
    }],
    ["reading count", "reading inventory changed", (payload) => { payload.reading.pop(); }],
    ["empty reading", "reading 1 text changed", (payload) => { payload.reading[0].claim = ""; }],
    ["horizon count", "horizon inventory changed", (payload) => { payload.horizons.pop(); }],
    ["horizon order", "horizon identity or order changed", (payload) => {
      payload.horizons.reverse();
    }],
    ["boolean horizon", "horizon 1 horizon is invalid", (payload) => {
      payload.horizons[0].horizon = true;
    }],
    ["non-finite metric", "horizon 1 metric is invalid", (payload) => {
      payload.horizons[0].model_r2 = Number.NaN;
    }],
    ["duplicate split", "horizon 1 split identity changed", (payload) => {
      payload.horizons[0].per_split[1].split = payload.horizons[0].per_split[0].split;
    }],
  ];
  for (const [name, expectedError, mutate] of invalidForecastPayloads) {
    const payload = structuredClone(FORECAST_STORY_PAYLOAD);
    const before = JSON.stringify(payload);
    mutate(payload);
    if (JSON.stringify(payload) === before) {
      throw new Error(`forecast payload mutation ${name} did not change the control`);
    }
    try {
      forecastExpectedEvidenceFromPayload(payload);
    } catch (error) {
      if (!String(error?.message ?? error).includes(expectedError)) {
        throw new Error(
          `forecast payload mutation ${name} raised ${String(error)}, expected ${expectedError}`,
        );
      }
      continue;
    }
    throw new Error(`forecast payload parser accepts ${name}`);
  }
  const forecastPart = (top, sourceIndex, extra = {}) => healthPart(top, sourceIndex, extra);
  const completeForecastDecision = {
    mode: "normal",
    counts: {
      decisionSheet: 1,
      readingBand: 1,
      baselineBand: 1,
      decisionRows: 3,
      readingRows: 4,
      baselineRows: 2,
    },
    regions: {
      decisionSheet: forecastPart(390, 30),
      readingBand: forecastPart(900, 80),
      baselineBand: forecastPart(1120, 100),
    },
    landmarks: {
      figure1Title: forecastPart(180, 10),
      primaryPlot: forecastPart(250, 20),
      decisionSheet: forecastPart(390, 30),
      figure2Title: forecastPart(720, 60),
      readingBand: forecastPart(900, 80),
      baselineBand: forecastPart(1120, 100),
      cost: forecastPart(1400, 120),
    },
    decisionRows: FORECAST_DECISION_ROWS.map((row, index) => ({
      key: row[0],
      label: row[1],
      bodyText: row[2],
      href: row[3],
      accessibleText: row[1] + row[2],
      inspection: forecastPart(410 + index * 50, 31 + index),
    })),
    readingRows: EXPECTED_FORECAST_EVIDENCE.readings.map((row, index) => ({
      key: FORECAST_READING_KEYS_ORDERED[index],
      heading: row[0],
      bodyText: row[1],
      accessibleHeading: row[0],
      accessibleBody: row[1],
      inspection: forecastPart(920 + index * 50, 81 + index),
    })),
    baselineRows: EXPECTED_FORECAST_EVIDENCE.baselines.map((row, index) => ({
      key: row[0],
      heading: row[0] + " " + row[1],
      whatText: row[2],
      whyText: row[3],
      accessibleText: row.join(""),
      inspection: forecastPart(1140 + index * 70, 101 + index),
    })),
    pageText: FORECAST_DECISION_ROWS.map((row) => row[2]).join(" "),
    viewport: { width: 375, height: 812 },
    document: { clientWidth: 375, scrollWidth: 375 },
  };
  const forecastPreflightMisses = [];
  const forecastControlProblems = forecastHorizonDecisionProblems(
    completeForecastDecision,
    EXPECTED_FORECAST_EVIDENCE,
    completeForecastDecision.viewport,
  );
  if (forecastControlProblems.length) {
    forecastPreflightMisses.push(`complete control: ${forecastControlProblems.join(", ")}`);
  }
  const forecastMutations = [
    ["invalid mode", "mode is invalid", (state) => { state.mode = "other"; }],
    ["missing sheet", "decision sheet count is 0", (state) => {
      state.counts.decisionSheet = 0;
      state.regions.decisionSheet = null;
    }],
    ["extra reading band", "reading band count is 2", (state) => {
      state.counts.readingBand = 2;
    }],
    ["hidden sheet", "decision sheet is hidden", (state) => {
      state.regions.decisionSheet.hidden = true;
    }],
    ["aria-hidden band", "reading band is aria-hidden", (state) => {
      state.regions.readingBand.ariaHidden = true;
    }],
    ["zero-opacity baseline", "baseline band opacity is zero", (state) => {
      state.regions.baselineBand.opacity = 0;
    }],
    ["zero area", "decision sheet has no rendered area", (state) => {
      state.regions.decisionSheet.width = 0;
    }],
    ["self overflow", "reading band clips its own content", (state) => {
      state.regions.readingBand.selfOverflowX = 3;
    }],
    ["ancestor clip", "baseline band is clipped by an ancestor", (state) => {
      state.regions.baselineBand.ancestorClipped = true;
    }],
    ["CSS clip", "decision sheet uses CSS clip", (state) => {
      state.regions.decisionSheet.cssClip = true;
    }],
    ["off canvas", "baseline band is horizontally off-canvas", (state) => {
      state.regions.baselineBand.left = 400;
      state.regions.baselineBand.right = 700;
    }],
    ["details", "reading band is user-collapsible", (state) => {
      state.regions.readingBand.detailsAncestor = true;
    }],
    ["missing decision", "decision row inventory changed", (state) => {
      state.decisionRows.pop();
      state.counts.decisionRows = 2;
    }],
    ["decision key", "decision row 1 key changed", (state) => {
      state.decisionRows[0].key = "skill";
    }],
    ["decision text", "decision row 1 body changed", (state) => {
      state.decisionRows[0].bodyText = "不同說明";
    }],
    ["decision AX", "decision row 1 accessible text changed", (state) => {
      state.decisionRows[0].accessibleText = "另一個名稱";
    }],
    ["decision link", "decision row 1 link changed", (state) => {
      state.decisionRows[0].href = "#forecast-cost";
    }],
    ["decision visual reorder", "decision row visual order changed", (state) => {
      state.decisionRows[0].inspection.top = 600;
    }],
    ["reading reorder", "reading row 1 key changed", (state) => {
      state.readingRows.reverse();
    }],
    ["reading body", "reading row 1 body changed", (state) => {
      state.readingRows[0].bodyText = "不同解讀";
    }],
    ["reading AX", "reading row 1 accessible heading changed", (state) => {
      state.readingRows[0].accessibleHeading = "不同標題";
    }],
    ["baseline text", "baseline row 1 what changed", (state) => {
      state.baselineRows[0].whatText = "不同基準";
    }],
    ["baseline AX", "baseline row 1 accessible text changed", (state) => {
      state.baselineRows[0].accessibleText = "不同基準";
    }],
    ["source order", "evidence order changed", (state) => {
      state.landmarks.decisionSheet.sourceIndex = 70;
    }],
    ["computed order", "Figure 6.2 title uses CSS order", (state) => {
      state.landmarks.figure2Title.cssOrder = 2;
    }],
    ["duplicate sentence", "decision sentence locality changed", (state) => {
      state.pageText += " " + FORECAST_DECISION_ROWS[0][2];
    }],
    ["mobile plot below viewport", "primary plot does not enter the first viewport", (state) => {
      state.landmarks.primaryPlot.top = 812;
    }],
    ["page overflow", "document scrolls sideways", (state) => {
      state.document.scrollWidth = 376;
    }],
    ["no-JavaScript missing sheet", "no-JavaScript decision sheet count is 0", (state) => {
      state.mode = "no-js";
      state.counts.decisionSheet = 0;
      state.regions.decisionSheet = null;
    }],
    ["print hidden band", "print baseline band display is none", (state) => {
      state.mode = "print";
      state.regions.baselineBand.display = "none";
    }],
    ["zoom order", "zoom evidence order changed", (state) => {
      state.mode = "zoom";
      state.landmarks.cost.top = 800;
      state.landmarks.baselineBand.top = 900;
    }],
  ];
  for (const [name, expectedProblem, mutate] of forecastMutations) {
    const state = structuredClone(completeForecastDecision);
    const before = JSON.stringify(state);
    mutate(state);
    if (JSON.stringify(state) === before) {
      forecastPreflightMisses.push(`${name} mutation did not change the control`);
      continue;
    }
    const problems = forecastHorizonDecisionProblems(
      state,
      EXPECTED_FORECAST_EVIDENCE,
      state.viewport,
    );
    if (!problems.some((problem) => problem.includes(expectedProblem))) {
      forecastPreflightMisses.push(`${name} -> ${expectedProblem}`);
    }
  }
  const missingForecastZoomProblems = textZoomRouteMatrixProblems(
    TEXT_ZOOM_ROUTES.filter((route) => route !== "/forecast/"),
  );
  if (!missingForecastZoomProblems.some((problem) => problem.includes("/forecast/"))) {
    throw new Error("the text-zoom route contract accepts a missing /forecast/");
  }
  if (forecastPreflightMisses.length) {
    throw new Error(`the forecast preflight misses ${forecastPreflightMisses.join("; ")}`);
  }
  console.log("site quality forecast horizon decision self-test passed");

  const methodsPart = (top, sourceIndex, extra = {}) =>
    healthPart(top, sourceIndex, { left: 20, right: 620, width: 600, height: 48, ...extra });
  const completeMethodsIndex = {
    mode: "normal",
    counts: { indexes: 1, labelTargets: 1, links: 7, destinations: 7 },
    index: methodsPart(180, 20, { height: 260 }),
    indexHeading: "七個案例索引",
    indexAccessibleName: "七個案例索引",
    links: METHOD_CASE_ROWS.map(([number, title, href], index) => ({
      number,
      title,
      href,
      targetId: href.slice(1),
      visibleText: number + title,
      accessibleText: title,
      inspection: methodsPart(240 + Math.floor(index / 2) * 52, 30 + index, {
        left: index % 2 === 0 ? 20 : 330,
        right: index % 2 === 0 ? 310 : 620,
        width: 290,
        height: 48,
      }),
    })),
    destinations: METHOD_CASE_ROWS.map(([number, title, href], index) => ({
      number,
      id: href.slice(1),
      heading: title,
      accessibleHeading: title,
      inspection: methodsPart(520 + index * 520, 100 + index, { height: 480 }),
    })),
    landmarks: {
      lede: methodsPart(100, 10),
      index: methodsPart(180, 20, { height: 260 }),
    },
    viewport: { width: 1280, height: 720 },
    document: { clientWidth: 1280, scrollWidth: 1280 },
  };
  const methodsPreflightMisses = [];
  const methodsControlProblems = methodsCaseIndexProblems(
    completeMethodsIndex,
    completeMethodsIndex.viewport,
  );
  if (methodsControlProblems.length) {
    methodsPreflightMisses.push(`complete control: ${methodsControlProblems.join(", ")}`);
  }
  const methodsMutations = [
    ["invalid mode", "mode is invalid", (state) => { state.mode = "other"; }],
    ["missing index", "case index count is 0", (state) => {
      state.counts.indexes = 0;
      state.index = null;
    }],
    ["duplicate label", "case index label count is 2", (state) => {
      state.counts.labelTargets = 2;
    }],
    ["hidden index", "case index is hidden", (state) => { state.index.hidden = true; }],
    ["aria-hidden index", "case index is aria-hidden", (state) => {
      state.index.ariaHidden = true;
    }],
    ["zero-opacity index", "case index opacity is zero", (state) => {
      state.index.opacity = 0;
    }],
    ["index self overflow", "case index clips its own content", (state) => {
      state.index.selfOverflowX = 2;
    }],
    ["index ancestor clip", "case index is clipped by an ancestor", (state) => {
      state.index.ancestorClipped = true;
    }],
    ["index CSS clip", "case index uses CSS clip", (state) => { state.index.cssClip = true; }],
    ["index off canvas", "case index is horizontally off-canvas", (state) => {
      state.index.left = 1300;
      state.index.right = 1900;
    }],
    ["index disclosure", "case index is user-collapsible", (state) => {
      state.index.detailsAncestor = true;
    }],
    ["wrong heading", "case index heading changed", (state) => {
      state.indexHeading = "方法摘要";
    }],
    ["wrong index AX", "case index accessible name changed", (state) => {
      state.indexAccessibleName = "另一個索引";
    }],
    ["missing link", "case link inventory changed", (state) => {
      state.links.pop();
      state.counts.links = 6;
    }],
    ["duplicate hook", "case link hook inventory changed", (state) => {
      state.counts.links = 8;
    }],
    ["wrong link number", "case link 1 number changed", (state) => {
      state.links[0].number = "02";
    }],
    ["wrong link title", "case link 1 title changed", (state) => {
      state.links[0].title = "不同案例";
    }],
    ["wrong link href", "case link 1 href changed", (state) => {
      state.links[0].href = "#method-case-02";
    }],
    ["wrong target identity", "case link 1 target identity changed", (state) => {
      state.links[0].targetId = "method-case-02";
    }],
    ["wrong visible text", "case link 1 visible text changed", (state) => {
      state.links[0].visibleText = "01不同案例";
    }],
    ["wrong link AX", "case link 1 accessible text changed", (state) => {
      state.links[0].accessibleText = "另一個名稱";
    }],
    ["short target", "case link 1 target is shorter than 44px", (state) => {
      state.links[0].inspection.height = 43;
    }],
    ["hidden link", "case link 1 is hidden", (state) => {
      state.links[0].inspection.hidden = true;
    }],
    ["visual link reorder", "case link visual order changed", (state) => {
      state.links[0].inspection.top = 400;
    }],
    ["missing destination", "case destination inventory changed", (state) => {
      state.destinations.pop();
      state.counts.destinations = 6;
    }],
    ["destination id", "case destination 1 id changed", (state) => {
      state.destinations[0].id = "method-case-02";
    }],
    ["destination heading", "case destination 1 heading changed", (state) => {
      state.destinations[0].heading = "不同案例";
    }],
    ["destination AX", "case destination 1 accessible heading changed", (state) => {
      state.destinations[0].accessibleHeading = "不同案例";
    }],
    ["destination hidden", "case destination 1 is hidden", (state) => {
      state.destinations[0].inspection.hidden = true;
    }],
    ["destination reorder", "case destination visual order changed", (state) => {
      state.destinations[0].inspection.top = 1200;
    }],
    ["source reorder", "casebook source order changed", (state) => {
      state.landmarks.index.sourceIndex = 101;
    }],
    ["index below viewport", "case index does not enter the first viewport", (state) => {
      state.landmarks.index.top = 720;
    }],
    ["document overflow", "document scrolls sideways", (state) => {
      state.document.scrollWidth = 1281;
    }],
    ["no-JavaScript hidden index", "no-JavaScript case index is hidden", (state) => {
      state.mode = "no-js";
      state.index.hidden = true;
    }],
    ["print disclosure", "print case index is user-collapsible", (state) => {
      state.mode = "print";
      state.index.detailsAncestor = true;
    }],
    ["zoom AX", "zoom case link 1 accessible text changed", (state) => {
      state.mode = "zoom";
      state.links[0].accessibleText = "另一個名稱";
    }],
  ];
  for (const [name, expectedProblem, mutate] of methodsMutations) {
    const state = structuredClone(completeMethodsIndex);
    const before = JSON.stringify(state);
    mutate(state);
    if (JSON.stringify(state) === before) {
      methodsPreflightMisses.push(`${name} mutation did not change the control`);
      continue;
    }
    const problems = methodsCaseIndexProblems(state, state.viewport);
    if (!problems.some((problem) => problem.includes(expectedProblem))) {
      methodsPreflightMisses.push(`${name} -> ${expectedProblem}`);
    }
  }
  const missingMethodsZoomProblems = textZoomRouteMatrixProblems(
    TEXT_ZOOM_ROUTES.filter((route) => route !== "/methods/"),
  );
  if (!missingMethodsZoomProblems.some((problem) => problem.includes("/methods/"))) {
    methodsPreflightMisses.push("text-zoom route contract accepts a missing /methods/");
  }
  if (methodsPreflightMisses.length) {
    throw new Error(`the Methods preflight misses ${methodsPreflightMisses.join("; ")}`);
  }
  console.log("site quality methods seven-case index self-test passed");

  const dataPart = (top, sourceIndex, extra = {}) =>
    healthPart(top, sourceIndex, { left: 20, right: 620, width: 600, height: 48, ...extra });
  const completeDataRegister = {
    mode: "normal",
    counts: {
      taskRegisters: 1,
      schemaRegisters: 1,
      registers: 1,
      terms: 3,
      uses: 3,
      descriptions: 3,
      tables: 1,
      bodyRows: 21,
      downloads: 25,
      unavailable: 19,
      l2Downloads: 0,
      boundaries: 1,
    },
    register: dataPart(460, 30, { height: 270 }),
    layers: DATA_LAYER_ROWS.map(([level, term, useText, descriptionText], index) => ({
      level,
      term,
      useText,
      accessibleUse: useText,
      descriptionText,
      termInspection: dataPart(500 + index * 70, 40 + index * 3),
      useInspection: dataPart(530 + index * 70, 41 + index * 3, { height: 24 }),
      descriptionInspection: dataPart(560 + index * 70, 42 + index * 3),
    })),
    table: dataPart(540, 100, { height: 900 }),
    tableWrapper: { inspection: dataPart(520, 99, { height: 940 }), clientWidth: 600, scrollWidth: 900, overflowX: "auto" },
    downloadRows: DATA_DOWNLOAD_ROWS.map((row, index) => ({
      ...row,
      rowInspection: dataPart(560 + index * 40, 120 + index * 3),
      downloadInspections: [
        dataPart(560 + index * 40, 121 + index * 3),
        dataPart(560 + index * 40, 122 + index * 3),
      ],
      downloadAccessibleTexts: [
        `JSON ${row.l0Size}`,
        row.l1Href ? `Parquet ${row.l1Size}` : "Pages 未發布",
      ],
    })),
    l2BoundaryText: "L2 不發布，理由不是檔案太大。這個專案繞過這個矛盾而不是解決它。",
    l2Boundary: dataPart(1620, 110, { height: 140 }),
    landmarks: {
      lede: dataPart(100, 10),
      primary: dataPart(180, 20, { height: 230 }),
      register: dataPart(460, 30, { height: 270 }),
      table: dataPart(540, 100, { height: 900 }),
      licensing: dataPart(1500, 105, { height: 80 }),
      l2Boundary: dataPart(1620, 110, { height: 140 }),
    },
    viewport: { width: 1280, height: 720 },
    document: { clientWidth: 1280, scrollWidth: 1280 },
  };
  const dataPreflightMisses = [];
  const dataControlProblems = dataProvenanceRegisterProblems(completeDataRegister, completeDataRegister.viewport);
  if (dataControlProblems.length) dataPreflightMisses.push(`complete control: ${dataControlProblems.join(", ")}`);
  const dataMutations = [
    ["invalid mode", "mode is invalid", (state) => { state.mode = "other"; }],
    ["missing task register", "task register changed", (state) => { state.counts.taskRegisters = 0; }],
    ["missing schema register", "schema register changed", (state) => { state.counts.schemaRegisters = 0; }],
    ["missing register", "register count is 0", (state) => { state.counts.registers = 0; state.register = null; }],
    ["hidden register", "register is hidden", (state) => { state.register.hidden = true; }],
    ["register disclosure", "register is user-collapsible", (state) => { state.register.detailsAncestor = true; }],
    ["missing level", "layer inventory changed", (state) => { state.layers.pop(); state.counts.terms = 2; state.counts.uses = 2; state.counts.descriptions = 2; }],
    ["duplicate use hook", "layer use hook inventory changed", (state) => { state.counts.uses = 4; }],
    ["reordered levels", "layer 1 identity changed", (state) => { [state.layers[0], state.layers[1]] = [state.layers[1], state.layers[0]]; }],
    ["wrong term", "layer 1 term changed", (state) => { state.layers[0].term = "L1 站-日"; }],
    ["wrong use", "layer 1 use changed", (state) => { state.layers[0].useText = "另一種用途"; }],
    ["wrong use AX", "layer 1 accessible use changed", (state) => { state.layers[0].accessibleUse = "另一種用途"; }],
    ["changed description", "layer 1 description changed", (state) => { state.layers[0].descriptionText = "不同描述"; }],
    ["extended contradictory description", "layer 1 description changed", (state) => { state.layers[0].descriptionText += "但內容規格已改變"; }],
    ["hidden use", "layer 1 use is hidden", (state) => { state.layers[0].useInspection.hidden = true; }],
    ["off-canvas use", "layer 1 use is horizontally off-canvas", (state) => { state.layers[0].useInspection.left = 1300; state.layers[0].useInspection.right = 1900; }],
    ["clipped use", "layer 1 use is clipped by an ancestor", (state) => { state.layers[0].useInspection.ancestorClipped = true; }],
    ["zero-area use", "layer 1 use has no rendered area", (state) => { state.layers[0].useInspection.height = 0; }],
    ["visual level reorder", "layer visual order changed", (state) => { state.layers[0].termInspection.top = 800; }],
    ["missing table", "download table count is 0", (state) => { state.counts.tables = 0; state.table = null; }],
    ["lost table row", "download row count is 20", (state) => { state.counts.bodyRows = 20; }],
    ["lost download", "registered download count changed", (state) => { state.counts.downloads = 24; }],
    ["lost unavailable state", "unavailable L1 count changed", (state) => { state.counts.unavailable = 18; }],
    ["changed download destination", "download row 1 changed", (state) => { state.downloadRows[0].l0Href = "/data/l0/wrong.json"; }],
    ["reordered download rows", "download row 1 changed", (state) => { [state.downloadRows[0], state.downloadRows[1]] = [state.downloadRows[1], state.downloadRows[0]]; }],
    ["hidden download", "download row 1 link 1 is hidden", (state) => { state.downloadRows[0].downloadInspections[0].hidden = true; }],
    ["compact off-canvas action", "download row 1 link 1 is horizontally off-canvas", (state) => {
      state.viewport = { width: 610, height: 900 };
      state.downloadRows[0].downloadInspections[0].left = 620;
      state.downloadRows[0].downloadInspections[0].right = 720;
    }],
    ["L2 download", "L2 unexpectedly has 1 download", (state) => { state.counts.l2Downloads = 1; }],
    ["broken local scroller", "download table local scroller changed", (state) => { state.tableWrapper.overflowX = "visible"; }],
    ["compact horizontal scroller", "download table local scroller changed", (state) => {
      state.viewport = { width: 610, height: 900 };
      state.tableWrapper.overflowX = "auto";
      state.tableWrapper.clientWidth = 580;
      state.tableWrapper.scrollWidth = 900;
    }],
    ["missing L2 boundary", "L2 boundary count is 0", (state) => { state.counts.boundaries = 0; state.l2Boundary = null; }],
    ["changed L2 boundary", "L2 boundary text changed", (state) => { state.l2BoundaryText = "L2 不發布。"; }],
    ["source reorder", "provenance source order changed", (state) => { state.landmarks.register.sourceIndex = 106; }],
    ["task register below viewport", "task register does not enter the first viewport", (state) => { state.landmarks.primary.top = 720; }],
    ["document overflow", "document scrolls sideways", (state) => { state.document.scrollWidth = 1281; }],
    ["no-JavaScript hidden use", "no-JavaScript layer 1 use is hidden", (state) => { state.mode = "no-js"; state.layers[0].useInspection.hidden = true; }],
    ["print disclosure", "print register is user-collapsible", (state) => { state.mode = "print"; state.register.detailsAncestor = true; }],
    ["zoom overflow", "zoom document scrolls sideways", (state) => { state.mode = "zoom"; state.document.scrollWidth = 1281; }],
  ];
  for (const [name, expectedProblem, mutate] of dataMutations) {
    const state = structuredClone(completeDataRegister);
    const before = JSON.stringify(state);
    mutate(state);
    if (JSON.stringify(state) === before) {
      dataPreflightMisses.push(`${name} mutation did not change the control`);
      continue;
    }
    const problems = dataProvenanceRegisterProblems(state, state.viewport);
    if (!problems.some((problem) => problem.includes(expectedProblem))) dataPreflightMisses.push(`${name} -> ${expectedProblem}`);
  }
  const missingDataZoomProblems = textZoomRouteMatrixProblems(TEXT_ZOOM_ROUTES.filter((route) => route !== "/data/"));
  if (!missingDataZoomProblems.some((problem) => problem.includes("/data/"))) dataPreflightMisses.push("text-zoom route contract accepts a missing /data/");
  const missingExploreZoomProblems = textZoomRouteMatrixProblems(
    TEXT_ZOOM_ROUTES.filter((route) => route !== "/explore/"),
  );
  if (!missingExploreZoomProblems.some((problem) => problem.includes("/explore/"))) {
    dataPreflightMisses.push("text-zoom route contract accepts a missing /explore/");
  }
  if (dataPreflightMisses.length) throw new Error(`the Data preflight misses ${dataPreflightMisses.join("; ")}`);
  console.log("site quality data provenance register self-test passed");

  const explorerPart = (top, sourceIndex, extra = {}) =>
    healthPart(top, sourceIndex, { left: 100, right: 700, width: 600, height: 40, ...extra });
  const explorerHiddenPart = (top, sourceIndex) => explorerPart(top, sourceIndex, {
    display: "none",
    rendered: false,
    accessible: false,
    width: 0,
    height: 0,
    right: 100,
    bottom: top,
  });
  const explorerFixture = (state = "initial", mode = "normal") => {
    const hiddenForMode = mode === "no-js" || mode === "print";
    const terminal = ["success", "empty", "failure"].includes(state);
    const fixture = {
      mode,
      state,
      counts: {
        workspace: 1,
        paths: 1,
        steps: 3,
        controls: 1,
        tables: 1,
        results: 1,
        caveats: 1,
      },
      steps: EXPLORER_GUIDED_STEPS.map((step, index) => ({
        key: step.key,
        title: step.title,
        text: step.text,
        accessibleText: `${step.number}${step.title}${step.text}`,
        inspection: explorerPart(200, 20 + index, {
          left: 100 + index * 200,
          right: 280 + index * 200,
          width: 180,
        }),
      })),
      run: {
        disabled: state === "loading",
        accessibleText: hiddenForMode ? null : "執行查詢",
        inspection: hiddenForMode ? explorerHiddenPart(320, 30) : explorerPart(320, 30),
      },
      status: {
        text: state === "loading"
          ? "準備查詢"
          : state === "success"
            ? "10 列 · 12 ms"
            : state === "empty"
              ? "0 列 · 8 ms"
              : state === "failure"
                ? "查詢失敗：Catalog Error"
                : "",
        busy: state === "loading",
        failed: state === "failure",
        inspection: hiddenForMode ? explorerHiddenPart(360, 31) : explorerPart(360, 31),
      },
      tables: {
        text: "按下執行之後，這裡會列出實際可以查的表。",
        inspection: hiddenForMode ? explorerHiddenPart(480, 50) : explorerPart(480, 50),
      },
      result: {
        text: state === "success"
          ? "station_name date mean"
          : state === "empty"
            ? "查詢成立，但沒有任何一列符合條件。"
            : state === "failure"
              ? "Catalog Error"
              : "",
        hasRows: state === "success",
        emptyMessage: state === "empty",
        errorDetail: state === "failure" ? "Catalog Error" : null,
        inspection: hiddenForMode || !terminal
          ? explorerHiddenPart(560, 60)
          : explorerPart(560, 60),
        focused: terminal,
      },
      caveat: {
        text: "Pages 目前公開 PM10、PM2.5 兩張 L1 表。本機匯出可產生完整 21 個測項，但那不是目前 GitHub Pages 的發布承諾。逐時原始資料另有授權問題待確認。",
        inspection: explorerPart(640, 70),
      },
      noJs: {
        text: "瀏覽器內查詢需要 JavaScript。本頁不會下載查詢引擎或執行查詢。",
        inspection: mode === "no-js" ? explorerPart(280, 25) : explorerHiddenPart(280, 25),
      },
      document: { clientWidth: 1280, scrollWidth: 1280 },
    };
    return fixture;
  };
  const explorerPreflightMisses = [];
  for (const control of [
    explorerFixture("initial"),
    explorerFixture("loading"),
    explorerFixture("success"),
    explorerFixture("empty"),
    explorerFixture("failure"),
    explorerFixture("initial", "zoom"),
    explorerFixture("initial", "print"),
    explorerFixture("no-js", "no-js"),
  ]) {
    const controlProblems = explorerGuidedWorkspaceProblems(control, { width: 1280, height: 720 });
    if (controlProblems.length) {
      explorerPreflightMisses.push(`complete ${control.mode}/${control.state}: ${controlProblems.join(", ")}`);
    }
  }
  const multilineFailure = explorerFixture("failure");
  multilineFailure.result.errorDetail = "Catalog Error\nLINE 1: SELECT";
  multilineFailure.result.text = "Catalog Error LINE 1: SELECT";
  const multilineFailureProblems = explorerGuidedWorkspaceProblems(
    multilineFailure,
    { width: 1280, height: 720 },
  );
  if (multilineFailureProblems.length) {
    explorerPreflightMisses.push(
      `valid multiline failure rejected: ${multilineFailureProblems.join(", ")}`,
    );
  }
  const explorerMutations = [
    ["missing top-level key", "state shape changed", (state) => { delete state.caveat; }],
    ["extra top-level key", "state shape changed", (state) => { state.extra = true; }],
    ["invalid mode", "mode is invalid", (state) => { state.mode = "other"; }],
    ["boolean count", "workspace count is true", (state) => { state.counts.workspace = true; }],
    ["missing step", "step inventory changed", (state) => { state.steps.pop(); state.counts.steps = 2; }],
    ["extra step key", "step 1 shape changed", (state) => { state.steps[0].extra = true; }],
    ["reordered steps", "step 1 key changed", (state) => { [state.steps[0], state.steps[1]] = [state.steps[1], state.steps[0]]; }],
    ["wrong step title", "step 1 title changed", (state) => { state.steps[0].title = "另一個問題"; }],
    ["wrong step text", "step 1 text changed", (state) => { state.steps[0].text = "另一段說明"; }],
    ["wrong step AX", "step 1 accessible text changed", (state) => { state.steps[0].accessibleText = "另一段名稱"; }],
    ["hidden step", "step 1 is hidden", (state) => { state.steps[0].inspection.hidden = true; }],
    ["zero-area step", "step 1 has no rendered area", (state) => { state.steps[0].inspection.height = 0; }],
    ["off-canvas step", "step 1 is horizontally off-canvas", (state) => { state.steps[0].inspection.left = 1300; state.steps[0].inspection.right = 1480; }],
    ["clipped step", "step 1 is clipped by an ancestor", (state) => { state.steps[0].inspection.ancestorClipped = true; }],
    ["disclosure step", "step 1 is user-collapsible", (state) => { state.steps[0].inspection.detailsAncestor = true; }],
    ["non-finite geometry", "step 1 inspection width is not finite", (state) => { state.steps[0].inspection.width = Number.NaN; }],
    ["visual step reorder", "step visual order changed", (state) => { state.steps[0].inspection.left = 700; }],
    ["source reorder", "source order changed", (state) => { state.run.inspection.sourceIndex = 19; }],
    ["integer run disabled", "run disabled is not boolean", (state) => { state.run.disabled = 1; }],
    ["wrong run AX", "run accessible text changed", (state) => { state.run.accessibleText = "開始"; }],
    ["hidden run", "run control is hidden", (state) => { state.run.inspection.hidden = true; }],
    ["status busy type", "status busy is not boolean", (state) => { state.status.busy = 1; }],
    ["loading enabled run", "loading semantics changed", (state) => { state.state = "loading"; state.status.text = "準備查詢"; state.status.busy = true; }],
    ["loading prior result", "loading presents a prior result", (state) => { state.state = "loading"; state.run.disabled = true; state.status.text = "準備查詢"; state.status.busy = true; state.result.text = "舊答案"; }],
    ["success without rows", "success semantics changed", (state) => { Object.assign(state, explorerFixture("success")); state.result.hasRows = false; }],
    ["empty without message", "empty semantics changed", (state) => { Object.assign(state, explorerFixture("empty")); state.result.emptyMessage = false; }],
    ["failure without detail", "failure semantics changed", (state) => { Object.assign(state, explorerFixture("failure")); state.result.errorDetail = null; }],
    ["lost result focus", "success semantics changed", (state) => { Object.assign(state, explorerFixture("success")); state.result.focused = false; }],
    ["no-JavaScript active run", "no-JavaScript run control is visibly rendered", (state) => { Object.assign(state, explorerFixture("no-js", "no-js")); state.run.inspection = explorerPart(320, 30); }],
    ["no-JavaScript hidden notice", "no-JavaScript no-JavaScript notice is hidden", (state) => { Object.assign(state, explorerFixture("no-js", "no-js")); state.noJs.inspection.hidden = true; }],
    ["document overflow", "document scrolls sideways", (state) => { state.document.scrollWidth = 1281; }],
    ["zoom contradictory result", "zoom explore initial result is not empty", (state) => { Object.assign(state, explorerFixture("initial", "zoom")); state.result.text = "舊答案"; state.result.hasRows = true; state.result.inspection = explorerPart(560, 60); }],
  ];
  for (const [name, expectedProblem, mutate] of explorerMutations) {
    const state = explorerFixture("initial");
    const before = JSON.stringify(state);
    mutate(state);
    if (JSON.stringify(state) === before) {
      explorerPreflightMisses.push(`${name} mutation did not change the control`);
      continue;
    }
    const problems = explorerGuidedWorkspaceProblems(state, { width: 1280, height: 720 });
    if (!problems.some((problem) => problem.includes(expectedProblem))) {
      explorerPreflightMisses.push(`${name} -> ${expectedProblem}`);
    }
  }
  if (explorerPreflightMisses.length) {
    throw new Error(`the Explore preflight misses ${explorerPreflightMisses.join("; ")}`);
  }
  console.log("site quality explore guided local workspace self-test passed");

  const completeConceptDiagram = {
    documentOverflow: 0,
    diagrams: [{
      tagName: "FIGURE",
      visible: true,
      width: 980,
      height: 260,
      captionCount: 1,
      title: "從觀測走到可以支持的結論",
      summary: "依序讀取資料、方法、結果與界線。",
      variant: "process",
      orderedListCount: 1,
      listRoleAttribute: "list",
      listAxRole: "list",
      stepCount: 4,
      directItemCount: 4,
      nonListSteps: 0,
      stepTops: [120, 120, 120, 120],
      stepAxRoles: ["listitem", "listitem", "listitem", "listitem"],
      gridColumnCount: 4,
      incompleteSteps: 0,
      hiddenSteps: 0,
      clippedSteps: 0,
      selfOverflowX: 0,
      selfOverflowY: 0,
      stepOverflowCount: 0,
      optionCount: 0,
      hiddenOptions: 0,
      clippedOptions: 0,
      connectorCount: 3,
      hiddenConnectors: 0,
      boxedConnectors: 0,
      figureCount: 0,
      figureTextCount: 0,
      hiddenFigures: 0,
      minimumTitleWidthRatio: 1,
      outOfOrderSteps: 0,
      nonVerticalTransitions: 0,
      toolCount: 1,
      toolLabels: ["放大", "下載"],
      toolButtonHeights: [45, 45],
      toolsAfterSteps: true,
      toolsShareCaptionRow: true,
      toolRightInset: 0,
      captionToolGap: 34,
      toolsOverlapCaption: false,
      media: {},
    }],
  };
  if (conceptDiagramProblems(completeConceptDiagram, 1, { width: 1440 }).length) {
    throw new Error("the concept-diagram predicate rejects complete diagram evidence");
  }
  if (conceptDiagramProblems({ documentOverflow: 0, diagrams: [] }, 0, { width: 375 }).length) {
    throw new Error("the concept-diagram predicate rejects an intentional zero-diagram route");
  }
  const conceptDiagramMutations = [
    ["missing diagram", "inventory is 0", (state) => { state.diagrams = []; }],
    ["wrong element", "not a native figure", (state) => { state.diagrams[0].tagName = "DIV"; }],
    ["missing caption", "exactly one direct figcaption", (state) => { state.diagrams[0].captionCount = 0; }],
    ["duplicate caption", "exactly one direct figcaption", (state) => { state.diagrams[0].captionCount = 2; }],
    ["missing title", "no visible title", (state) => { state.diagrams[0].title = ""; }],
    ["missing summary", "no visible reading summary", (state) => { state.diagrams[0].summary = ""; }],
    ["missing ordered list", "one direct ordered sequence", (state) => { state.diagrams[0].orderedListCount = 0; }],
    ["too few steps", "expected 3–5", (state) => { state.diagrams[0].stepCount = 2; }],
    ["missing list role", "explicit list role", (state) => { state.diagrams[0].listRoleAttribute = null; }],
    ["missing AX list", "missing from the accessibility tree", (state) => { state.diagrams[0].listAxRole = null; }],
    ["untracked list item", "untracked or non-direct", (state) => { state.diagrams[0].directItemCount = 5; }],
    ["non-list step", "steps that are not list items", (state) => { state.diagrams[0].nonListSteps = 1; }],
    ["missing AX list item", "accessibility-tree list items", (state) => { state.diagrams[0].stepAxRoles[0] = null; }],
    ["incomplete step", "incomplete steps", (state) => { state.diagrams[0].incompleteSteps = 1; }],
    ["hidden step", "hidden steps", (state) => { state.diagrams[0].hiddenSteps = 1; }],
    ["clipped step", "clipped steps", (state) => { state.diagrams[0].clippedSteps = 1; }],
    ["self-clipped diagram", "clips its own", (state) => { state.diagrams[0].selfOverflowX = 2; }],
    ["internally clipped step", "internally clipped", (state) => { state.diagrams[0].stepOverflowCount = 1; }],
    ["hidden option", "hidden and", (state) => { state.diagrams[0].hiddenOptions = 1; }],
    ["clipped option", "clipped branch options", (state) => { state.diagrams[0].clippedOptions = 1; }],
    ["missing connector", "connector inventory", (state) => { state.diagrams[0].connectorCount = 2; }],
    ["hidden connector", "invisible connectors", (state) => { state.diagrams[0].hiddenConnectors = 1; }],
    ["boxed connector", "boxed connectors", (state) => { state.diagrams[0].boxedConnectors = 1; }],
    ["svg text label", "carries SVG text", (state) => { state.diagrams[0].figureTextCount = 1; }],
    ["partial drawing row", "drawing row is incomplete", (state) => { state.diagrams[0].figureCount = 2; }],
    ["timeline without lanes", "draws no lanes", (state) => { state.diagrams[0].variant = "timeline"; }],
    ["hidden strip", "hidden drawing strips", (state) => { state.diagrams[0].hiddenFigures = 1; }],
    ["narrow title", "available card width", (state) => { state.diagrams[0].minimumTitleWidthRatio = 0.7; }],
    ["reordered step", "visually reordered", (state) => { state.diagrams[0].outOfOrderSteps = 1; }],
    // The boundary variant's first draft shipped exactly this shape: a zone
    // pseudo-element occupied the card columns and auto-placement pushed cards
    // 2-4 into rows below it — visible, ordered, unclipped, and wrong.
    ["wide displaced step", "share one row band", (state) => { state.diagrams[0].stepTops = [120, 120, 120, 386]; }],
    ["wide missing top inventory", "top-edge inventory", (state) => { state.diagrams[0].stepTops = [120, 120]; }],
    ["phone row", "vertical sequence", (state) => { state.diagrams[0].nonVerticalTransitions = 1; }],
    ["phone columns", "narrow-layout boundary", (state) => { state.diagrams[0].gridColumnCount = 4; }],
    ["tablet columns", "desktop/tablet column count", (state) => { state.diagrams[0].gridColumnCount = 1; }],
    ["duplicate toolbar", "toolbars; expected one", (state) => { state.diagrams[0].toolCount = 2; }],
    ["verbose download label", "toolbar labels changed", (state) => { state.diagrams[0].toolLabels[1] = "下載 PNG"; }],
    ["narrow toolbar above steps", "not below its steps", (state) => { state.diagrams[0].toolsAfterSteps = false; }],
    ["wide toolbar own row", "does not share the caption row", (state) => { state.diagrams[0].toolsShareCaptionRow = false; }],
    ["wide caption under toolbar", "runs under the toolbar", (state) => { state.diagrams[0].captionToolGap = 2; }],
    ["wide caption unused space", "leaves unused header space", (state) => { state.diagrams[0].captionToolGap = 180; }],
    ["toolbar left drift", "not aligned to the top-right", (state) => { state.diagrams[0].toolRightInset = 18; }],
    ["toolbar overlap", "overlaps its caption", (state) => { state.diagrams[0].toolsOverlapCaption = true; }],
    ["short toolbar target", "44px interaction floor", (state) => { state.diagrams[0].toolButtonHeights[0] = 43; }],
    ["document overflow", "scrolls sideways", (state) => { state.documentOverflow = 1; }],
  ];
  const conceptDiagramPreflightMisses = [];
  for (const [name, expectedProblem, mutate] of conceptDiagramMutations) {
    const state = structuredClone(completeConceptDiagram);
    mutate(state);
    const viewport = name === "tablet columns"
      ? { width: 769 }
      : name.startsWith("wide ")
        ? { width: 1440 }
        : { width: 375 };
    if (viewport.width <= 768 && name !== "phone columns" && state.diagrams[0]) {
      state.diagrams[0].gridColumnCount = 1;
    }
    const problems = conceptDiagramProblems(state, 1, viewport);
    if (!problems.some((problem) => problem.includes(expectedProblem))) {
      conceptDiagramPreflightMisses.push(name);
    }
  }
  if (conceptDiagramPreflightMisses.length) {
    throw new Error(
      `the concept-diagram predicate accepts ${conceptDiagramPreflightMisses.join(", ")}`,
    );
  }

  const completePrintConceptDiagram = structuredClone(completeConceptDiagram);
  completePrintConceptDiagram.diagrams[0].media = {
    printMarker: "ready",
    breakInside: "avoid",
    figureBackground: "rgba(0, 0, 0, 0)",
    stepBackgrounds: Array(4).fill("rgba(0, 0, 0, 0)"),
    indexBackgrounds: Array(4).fill("rgba(0, 0, 0, 0)"),
    figureBackgrounds: [],
    stepBreakInside: Array(4).fill("avoid"),
  };
  if (conceptDiagramPrintProblems(completePrintConceptDiagram).length) {
    throw new Error("the concept-diagram print predicate rejects complete print evidence");
  }
  const printMutations = [
    ["missing print marker", "print rules are not active", (media) => { media.printMarker = ""; }],
    ["splittable figure", "split across printed pages", (media) => { media.breakInside = "auto"; }],
    ["screen figure fill", "screen surface in print", (media) => { media.figureBackground = "rgb(250, 250, 250)"; }],
    ["screen step fill", "step surfaces in print", (media) => { media.stepBackgrounds[0] = "rgb(250, 250, 250)"; }],
    ["screen index fill", "index surfaces in print", (media) => { media.indexBackgrounds[0] = "rgb(250, 250, 250)"; }],
    ["screen strip fill", "strip surfaces in print", (media) => { media.figureBackgrounds = ["rgb(250, 250, 250)"]; }],
    ["splittable step", "printed step to split", (media) => { media.stepBreakInside[0] = "auto"; }],
  ];
  for (const [name, expectedProblem, mutate] of printMutations) {
    const state = structuredClone(completePrintConceptDiagram);
    mutate(state.diagrams[0].media);
    if (!conceptDiagramPrintProblems(state).some((problem) => problem.includes(expectedProblem))) {
      conceptDiagramPreflightMisses.push(name);
    }
  }

  const completeForcedConceptDiagram = structuredClone(completeConceptDiagram);
  completeForcedConceptDiagram.diagrams[0].media = {
    forcedColorsActive: true,
    forcedMarker: "active",
    figureBackground: "rgb(255, 255, 255)",
    stepBackgrounds: Array(4).fill("rgb(255, 255, 255)"),
    indexBackgrounds: Array(4).fill("rgb(255, 255, 255)"),
    borderColors: Array(9).fill("rgb(0, 0, 0)"),
    connectorColors: Array(3).fill("rgb(0, 0, 0)"),
    textColors: Array(8).fill("rgb(0, 0, 0)"),
    figureColors: [],
    canvasText: "rgb(0, 0, 0)",
  };
  if (conceptDiagramForcedColorsProblems(completeForcedConceptDiagram).length) {
    throw new Error("the concept-diagram forced-colors predicate rejects complete evidence");
  }
  const forcedMutations = [
    ["inactive forced colors", "emulation is inactive", (media) => { media.forcedColorsActive = false; }],
    ["missing forced marker", "rules are not active", (media) => { media.forcedMarker = ""; }],
    ["transparent Canvas", "no forced-colors Canvas", (media) => { media.figureBackground = "transparent"; }],
    ["wrong structure color", "CanvasText", (media) => { media.connectorColors[0] = "rgb(1, 2, 3)"; }],
    ["wrong surface color", "one forced-colors Canvas", (media) => { media.stepBackgrounds[0] = "rgb(1, 2, 3)"; }],
    ["tinted strip", "strips outside CanvasText", (media) => { media.figureColors = ["rgb(1, 2, 3)"]; }],
  ];
  for (const [name, expectedProblem, mutate] of forcedMutations) {
    const state = structuredClone(completeForcedConceptDiagram);
    mutate(state.diagrams[0].media);
    if (!conceptDiagramForcedColorsProblems(state).some((problem) => problem.includes(expectedProblem))) {
      conceptDiagramPreflightMisses.push(name);
    }
  }
  if (conceptDiagramPreflightMisses.length) {
    throw new Error(
      `the concept-diagram media predicates accept ${conceptDiagramPreflightMisses.join(", ")}`,
    );
  }
  {
    const even = { width: 83.41, height: 45, top: 50.2 };
    if (zoomHeadControlProblems({ download: { ...even }, shut: { ...even } }).length) {
      throw new Error("the enlarged-view header predicate rejects an even pair");
    }
    const cases = [
      ["missing control", "controls are missing", { download: { ...even }, shut: null }],
      // The pair that shipped, to the measurement.
      ["shipped mismatch", "differ in height", {
        download: { ...even }, shut: { width: 79, height: 59.53, top: 50.2 },
      }],
      ["width drift", "differ in width", {
        download: { ...even }, shut: { ...even, width: 120 },
      }],
      ["off baseline", "one baseline", {
        download: { ...even }, shut: { ...even, top: 70 },
      }],
      ["invalid geometry", "geometry is invalid", {
        download: { ...even }, shut: { width: Number.NaN, height: 45, top: 50.2 },
      }],
    ];
    for (const [name, expected, controls] of cases) {
      if (!zoomHeadControlProblems(controls).some((problem) => problem.includes(expected))) {
        throw new Error(`the enlarged-view header predicate accepts ${name}`);
      }
    }
    console.log("site quality enlarged-view header self-test passed");
  }

  console.log("site quality concept diagrams self-test passed");

  const completeCompactIdentity = {
    visible: true,
    accessibleText: "台灣空氣品質再分析",
    accessibilitySource: "accessibility-tree",
    visibleText: "空氣品質再分析",
    clientWidth: 144,
    scrollWidth: 144,
    textOverflow: "clip",
  };
  if (compactIdentityProblems(completeCompactIdentity, "台灣空氣品質再分析").length) {
    throw new Error("the compact-identity predicate rejects complete identity evidence");
  }
  const completeChapterCompactIdentity = {
    ...completeCompactIdentity,
    accessibleText: "第八章　方法選擇的量化代價",
    visibleText: "第八章 方法學對照",
  };
  if (
    compactIdentityProblems(
      completeChapterCompactIdentity,
      "第八章　方法選擇的量化代價",
    ).length
  ) {
    throw new Error("the compact-identity predicate rejects a complete chapter identity");
  }
  const missedCompactIdentityProblems = [];
  const expectCompactIdentityProblem = (name, state, expected) => {
    const problems = compactIdentityProblems(state, "台灣空氣品質再分析");
    if (!problems.some((problem) => problem.includes(expected))) {
      missedCompactIdentityProblems.push(name);
    }
  };
  expectCompactIdentityProblem("missing identity", null, "is missing");
  expectCompactIdentityProblem(
    "wrong accessible identity",
    { ...completeCompactIdentity, accessibleText: "空氣品質" },
    "identity changed",
  );
  expectCompactIdentityProblem(
    "unverified accessible identity",
    { ...completeCompactIdentity, accessibilitySource: "dom-attribute" },
    "accessibility tree was not checked",
  );
  expectCompactIdentityProblem(
    "empty visual identity",
    { ...completeCompactIdentity, visibleText: "" },
    "no visible text",
  );
  expectCompactIdentityProblem(
    "clipped visual identity",
    { ...completeCompactIdentity, scrollWidth: 145 },
    "is clipped",
  );
  expectCompactIdentityProblem(
    "ellipsized visual identity",
    { ...completeCompactIdentity, textOverflow: "ellipsis" },
    "uses ellipsis",
  );
  if (missedCompactIdentityProblems.length) {
    throw new Error(
      `the compact-identity predicate accepts ${missedCompactIdentityProblems.join(", ")}`,
    );
  }
  console.log("site quality compact identity self-test passed");

  const sourcesClipNode = (overrides = {}, parentElement = null) => ({
    parentElement,
    clientWidth: 100,
    clientHeight: 40,
    scrollWidth: 100,
    scrollHeight: 40,
    box: { top: 20, right: 120, bottom: 60, left: 20, width: 100, height: 40 },
    style: {
      overflowX: "visible",
      overflowY: "visible",
      clip: "auto",
      clipPath: "none",
      webkitClipPath: "none",
    },
    ...overrides,
  });
  const sourcesRectFor = (element) => element?.box ?? null;
  const sourcesStyleFor = (element) => element?.style ?? null;
  const unclippedSourcesBoundary = sourcesClipNode();
  if (sourcesElementIsClipped(unclippedSourcesBoundary, sourcesRectFor, sourcesStyleFor)) {
    throw new Error("the Sources clipping extractor rejects its unclipped control");
  }
  const missedSourcesClipCases = [];
  const expectSourcesClip = (name, element) => {
    if (!sourcesElementIsClipped(element, sourcesRectFor, sourcesStyleFor)) {
      missedSourcesClipCases.push(name);
    }
  };
  expectSourcesClip("self vertical overflow", sourcesClipNode({
    scrollHeight: 64,
    style: { ...unclippedSourcesBoundary.style, overflowY: "hidden" },
  }));
  expectSourcesClip("legacy CSS clip", sourcesClipNode({
    style: { ...unclippedSourcesBoundary.style, clip: "rect(0px, 80px, 40px, 0px)" },
  }));
  expectSourcesClip("CSS clip-path", sourcesClipNode({
    style: { ...unclippedSourcesBoundary.style, clipPath: "inset(0 20px 0 0)" },
  }));
  if (missedSourcesClipCases.length) {
    throw new Error(`the Sources clipping extractor accepts ${missedSourcesClipCases.join(", ")}`);
  }

  const sourcesRect = { top: 120, right: 355, bottom: 180, left: 20, width: 335, height: 60 };
  const sourcesCells = [
    { key: "0-0", fill: "var(--c0)", title: "0° 北 · 0.5–1.5 m/s — 機率 0.1（4 小時）" },
    { key: "0-1", fill: "none", title: "0° 北 · 1.5–2.5 m/s — 時數不足（3 小時）" },
  ];
  const sourcesInitialReadouts = {
    threshold: "11.1",
    peak: "0° 北",
    peakSpeed: "風速 1.5 m/s",
    resultant: "0.123",
    calm: "3.0%",
  };
  const completeSourcesAtlas = {
    boundary: {
      count: 1, visible: true, ariaHidden: "false", opacity: 1, clipped: false,
      text: "先讀方法界線 CBPF 描述條件機率，不識別污染來源；尖峰風速不等於來源距離。",
      rect: sourcesRect,
    },
    picker: { count: 1, visible: true, rect: { ...sourcesRect, top: 200, bottom: 244 } },
    primary: {
      count: 1, visible: true, rect: { ...sourcesRect, top: 280, bottom: 720, height: 440 },
      title: { visible: true, rect: { ...sourcesRect, top: 280, bottom: 320 } },
      plot: { visible: true, rect: { ...sourcesRect, top: 360, bottom: 720, height: 360 } },
    },
    sourceIndexes: { lede: 1, boundary: 2, picker: 3, primary: 4 },
    overflow: 0,
    selectedStation: "乙站",
    initialStation: "甲站",
    badge: { text: "中風速高值型", windPeakClass: "mid_wind_peak" },
    captionStation: "乙站",
    readouts: {
      threshold: "12.3", peak: "90° 東", peakSpeed: "風速 2.5 m/s", resultant: "0.456", calm: "7.8%",
    },
    readoutVisibility: {
      threshold: true,
      peak: true,
      peakSpeed: true,
      resultant: true,
      calm: true,
    },
    cells: sourcesCells,
    expected: {
      station: "乙站",
      initialStation: "甲站",
      badge: { text: "中風速高值型", windPeakClass: "mid_wind_peak" },
      readouts: {
        threshold: "12.3", peak: "90° 東", peakSpeed: "風速 2.5 m/s", resultant: "0.456", calm: "7.8%",
      },
      cells: sourcesCells.map((cell) => ({ ...cell })),
    },
  };
  const restoredSourcesAtlas = JSON.parse(JSON.stringify(completeSourcesAtlas));
  restoredSourcesAtlas.selectedStation = "甲站";
  restoredSourcesAtlas.badge = { text: "低風速高值型", windPeakClass: "low_wind_peak" };
  restoredSourcesAtlas.captionStation = "甲站";
  restoredSourcesAtlas.readouts = { ...sourcesInitialReadouts };
  restoredSourcesAtlas.expected.station = "甲站";
  restoredSourcesAtlas.expected.badge = {
    text: "低風速高值型",
    windPeakClass: "low_wind_peak",
  };
  restoredSourcesAtlas.expected.readouts = { ...sourcesInitialReadouts };
  const restoredRuntimeSnapshot = {
    atlas: restoredSourcesAtlas,
    focus: { tag: "BODY", id: "", name: "" },
    url: "http://example.test/sources/",
    scroll: [0, 0],
  };
  completeSourcesAtlas.restoration = {
    before: restoredRuntimeSnapshot,
    after: JSON.parse(JSON.stringify(restoredRuntimeSnapshot)),
  };
  if (sourcesAtlasProblems(completeSourcesAtlas, 375, 812, { requireRestoration: true }).length) {
    throw new Error("the Sources conditional-atlas predicate rejects complete state");
  }
  const ordinarySourcesAtlas = JSON.parse(JSON.stringify(completeSourcesAtlas));
  delete ordinarySourcesAtlas.restoration;
  if (sourcesAtlasProblems(ordinarySourcesAtlas, 375, 812, { requireRestoration: false }).length) {
    throw new Error("the Sources conditional-atlas predicate requires restoration for an ordinary state");
  }
  const completeNoScriptSourcesAtlas = JSON.parse(JSON.stringify(ordinarySourcesAtlas));
  completeNoScriptSourcesAtlas.picker.visible = false;
  completeNoScriptSourcesAtlas.selectedStation = "甲站";
  completeNoScriptSourcesAtlas.initialStation = "甲站";
  completeNoScriptSourcesAtlas.badge = {
    text: "低風速高值型",
    windPeakClass: "low_wind_peak",
  };
  completeNoScriptSourcesAtlas.captionStation = "甲站";
  completeNoScriptSourcesAtlas.expected.station = "甲站";
  completeNoScriptSourcesAtlas.expected.initialStation = "甲站";
  completeNoScriptSourcesAtlas.expected.badge = {
    text: "低風速高值型",
    windPeakClass: "low_wind_peak",
  };
  completeNoScriptSourcesAtlas.fallback = {
    count: 1,
    visible: true,
    station: "甲站",
    classification: "低風速高值型",
    rect: sourcesRect,
  };
  const completeNoScriptSourcesProblems = sourcesAtlasProblems(
    completeNoScriptSourcesAtlas,
    375,
    800,
    {
      noScript: true,
    },
  );
  if (completeNoScriptSourcesProblems.length) {
    throw new Error(
      `the Sources conditional-atlas predicate rejects complete no-JavaScript state: ${completeNoScriptSourcesProblems.join(", ")}`,
    );
  }
  const missedSourcesAtlasProblems = [];
  const ordinaryWithRestorationMetadata = JSON.parse(JSON.stringify(ordinarySourcesAtlas));
  ordinaryWithRestorationMetadata.restoration = {
    before: { atlas: {}, focus: null, url: "before", scroll: [0, 0] },
    after: { atlas: {}, focus: null, url: "after", scroll: [0, 0] },
  };
  if (sourcesAtlasProblems(ordinaryWithRestorationMetadata, 375, 812).some(
    (problem) => problem.includes("restoration"),
  )) {
    missedSourcesAtlasProblems.push("ordinary-state restoration metadata");
  }
  const expectSourcesAtlasProblem = (name, mutate, applied, expected, options = {}) => {
    const state = JSON.parse(JSON.stringify(completeSourcesAtlas));
    mutate(state);
    if (!applied(state)) throw new Error(`Sources mutation ${name} did not apply`);
    const problems = sourcesAtlasProblems(state, 375, 812, options);
    if (!problems.some((problem) => problem.includes(expected))) missedSourcesAtlasProblems.push(name);
  };
  expectSourcesAtlasProblem("missing boundary", (state) => { state.boundary.count = 0; }, (state) => state.boundary.count === 0, "boundary inventory");
  expectSourcesAtlasProblem("duplicate boundary", (state) => { state.boundary.count = 2; }, (state) => state.boundary.count === 2, "boundary inventory");
  expectSourcesAtlasProblem("display none boundary", (state) => { state.boundary.visible = false; }, (state) => !state.boundary.visible, "boundary is not visible");
  expectSourcesAtlasProblem("aria hidden boundary", (state) => { state.boundary.ariaHidden = "true"; }, (state) => state.boundary.ariaHidden === "true", "aria-hidden");
  expectSourcesAtlasProblem("transparent boundary", (state) => { state.boundary.opacity = 0; }, (state) => state.boundary.opacity === 0, "transparent");
  expectSourcesAtlasProblem("zero area boundary", (state) => { state.boundary.rect.width = 0; }, (state) => state.boundary.rect.width === 0, "boundary is not visible");
  expectSourcesAtlasProblem("clipped boundary", (state) => { state.boundary.clipped = true; }, (state) => state.boundary.clipped, "clipped");
  expectSourcesAtlasProblem("missing conditional-probability claim", (state) => {
    state.boundary.text = "先讀方法界線 尖峰風速不等於來源距離。";
  }, (state) => !state.boundary.text.includes("CBPF 描述條件機率，不識別污染來源"), "boundary claim");
  expectSourcesAtlasProblem("replaced source-attribution claim", (state) => {
    state.boundary.text = state.boundary.text.replace("不識別污染來源", "識別污染來源");
  }, (state) => state.boundary.text.includes("識別污染來源"), "boundary claim");
  expectSourcesAtlasProblem("relocated approved claims", (state) => {
    state.mainText = state.boundary.text;
    state.boundary.text = "先讀方法界線 先確認方法可回答的問題。";
  }, (state) => state.mainText.includes("尖峰風速不等於來源距離") && !state.boundary.text.includes("尖峰風速不等於來源距離"), "boundary claim");
  expectSourcesAtlasProblem("source order drift", (state) => { state.sourceIndexes.picker = 2; }, (state) => state.sourceIndexes.picker === 2, "source order changed");
  expectSourcesAtlasProblem("phone title below viewport", (state) => { state.primary.title.rect.top = 812; }, (state) => state.primary.title.rect.top === 812, "title enters below");
  expectSourcesAtlasProblem("phone plot below viewport", (state) => { state.primary.plot.rect.top = 812; }, (state) => state.primary.plot.rect.top === 812, "plot enters below");
  expectSourcesAtlasProblem("document overflow", (state) => { state.overflow = 1; }, (state) => state.overflow === 1, "horizontal overflow");
  expectSourcesAtlasProblem("same length caption drift", (state) => { state.captionStation = "甲站"; }, (state) => state.captionStation === "甲站", "caption station");
  expectSourcesAtlasProblem("badge text drift", (state) => { state.badge.text = "低風速高值型"; }, (state) => state.badge.text === "低風速高值型", "badge text");
  expectSourcesAtlasProblem("badge class drift", (state) => { state.badge.windPeakClass = "low_wind_peak"; }, (state) => state.badge.windPeakClass === "low_wind_peak", "class");
  for (const key of ["threshold", "peak", "peakSpeed", "resultant", "calm"]) {
    expectSourcesAtlasProblem(`initial ${key} copied`, (state) => { state.readouts[key] = sourcesInitialReadouts[key]; }, (state) => state.readouts[key] !== state.expected.readouts[key], `${key} readout`);
  }
  for (const key of ["threshold", "peak", "peakSpeed", "resultant", "calm"]) {
    expectSourcesAtlasProblem(`hidden ${key} readout`, (state) => {
      state.readoutVisibility[key] = false;
    }, (state) => !state.readoutVisibility[key], `${key} readout is not visible`);
  }
  expectSourcesAtlasProblem("stale cell fill", (state) => { state.cells[0].fill = "var(--c6)"; }, (state) => state.cells[0].fill === "var(--c6)", "cell fill");
  expectSourcesAtlasProblem("stale cell title", (state) => { state.cells[0].title = "stale"; }, (state) => state.cells[0].title === "stale", "cell title");
  const expectNoScriptSourcesProblem = (name, mutate, applied, expected) => {
    const state = JSON.parse(JSON.stringify(completeNoScriptSourcesAtlas));
    mutate(state);
    if (!applied(state)) throw new Error(`Sources no-JavaScript mutation ${name} did not apply`);
    const problems = sourcesAtlasProblems(state, 375, 800, {
      noScript: true,
    });
    if (!problems.some((problem) => problem.includes(expected))) {
      missedSourcesAtlasProblems.push(`no-JavaScript ${name}`);
    }
  };
  expectNoScriptSourcesProblem("visible picker", (state) => {
    state.picker.visible = true;
  }, (state) => state.picker.visible, "picker remains visible");
  expectNoScriptSourcesProblem("missing fallback", (state) => {
    delete state.fallback;
  }, (state) => !("fallback" in state), "fallback inventory");
  expectNoScriptSourcesProblem("duplicate fallback", (state) => {
    state.fallback.count = 2;
  }, (state) => state.fallback.count === 2, "fallback inventory");
  expectNoScriptSourcesProblem("hidden fallback", (state) => {
    state.fallback.visible = false;
  }, (state) => !state.fallback.visible, "fallback is not visible");
  expectNoScriptSourcesProblem("wrong fallback station", (state) => {
    state.fallback.station = "乙站";
  }, (state) => state.fallback.station === "乙站", "fallback station identity");
  expectNoScriptSourcesProblem("wrong fallback classification", (state) => {
    state.fallback.classification = "中風速高值型";
  }, (state) => state.fallback.classification === "中風速高值型", "fallback classification");
  for (const key of ["atlas", "focus", "url", "scroll"]) {
    expectSourcesAtlasProblem(`restoration ${key} drift`, (state) => {
      if (key === "atlas") state.restoration.after.atlas.selectedStation = "changed";
      else if (key === "focus") state.restoration.after.focus.tag = "BUTTON";
      else state.restoration.after[key] = key === "scroll" ? [1, 0] : "changed";
    }, (state) => JSON.stringify(state.restoration.before[key]) !== JSON.stringify(state.restoration.after[key]), "station restoration changed", { requireRestoration: true });
  }
  for (const key of ["atlas", "focus", "url", "scroll"]) {
    expectSourcesAtlasProblem(`restoration missing ${key}`, (state) => {
      delete state.restoration.before[key];
      delete state.restoration.after[key];
    }, (state) => !(key in state.restoration.before) && !(key in state.restoration.after), `restoration snapshot is missing ${key}`, { requireRestoration: true });
  }
  expectSourcesAtlasProblem("missing restoration", (state) => { delete state.restoration; }, (state) => !("restoration" in state), "station restoration is missing", { requireRestoration: true });
  if (missedSourcesAtlasProblems.length) {
    throw new Error(`the Sources conditional-atlas predicate accepts ${missedSourcesAtlasProblems.join(", ")}`);
  }
  console.log("site quality sources conditional-atlas self-test passed");

  const operationalMetadataFixtures = [
    ["reader-facing provenance", "\u8cc7\u6599\uff1a\u74b0\u5883\u90e8\u7a7a\u6c23\u54c1\u8cea\u76e3\u6e2c\u7db2 1982\u20132025 \u9010\u6642\u89c0\u6e2c\uff0c340,371,384 \u7b46\u3002\u7ba1\u7dda\u8207\u5206\u6790\u5168\u90e8\u958b\u6e90\u3002", []],
    ["export timestamp", "\u8cc7\u6599\u532f\u51fa\u65bc 2026-08-17", ["data export timestamp"]],
    ["dirty export", "\u532f\u51fa\u6642\u5c1a\u6709\u672a\u63d0\u4ea4\u7684\u8b8a\u66f4", ["uncommitted worktree state"]],
    ["bare revision", "723d9dc", ["bare revision hash"]],
    ["local preview", "http://127.0.0.1:4328/trend/", ["local development address"]],
    ["local path", "C:\\Users\\reader\\site", ["local filesystem path"]],
  ];
  for (const [name, text, expectedLabels] of operationalMetadataFixtures) {
    const problems = publicOperationalMetadataProblems(text);
    const expected = expectedLabels.map((label) => `public copy exposes ${label}`);
    if (JSON.stringify(problems) !== JSON.stringify(expected)) {
      throw new Error(`public operational-copy predicate misses ${name}`);
    }
  }
  console.log("site quality public operational copy self-test passed");

  const restartOrder = [];
  const replacement = await replaceBrowser(
    { close: async () => restartOrder.push("close") },
    async () => {
      restartOrder.push("open");
      return { generation: 2 };
    },
  );
  if (replacement.generation !== 2 || restartOrder.join(",") !== "close,open") {
    throw new Error("the browser replacement opened before the old process closed");
  }
  console.log("site quality browser restart self-test passed");

  const cleanupOrder = [];
  let cleanupFailurePreserved = false;
  try {
    await withRuntimeCleanup(
      {
        browser: { close: async () => cleanupOrder.push("browser") },
        server: { close: (callback) => { cleanupOrder.push("server"); callback(); } },
      },
      async () => {
        cleanupOrder.push("work");
        throw new Error("synthetic runtime failure");
      },
    );
  } catch (error) {
    cleanupFailurePreserved =
      error instanceof Error && error.message === "synthetic runtime failure";
  }
  if (!cleanupFailurePreserved || cleanupOrder.join(",") !== "work,browser,server") {
    throw new Error("a failed browser run did not close both resources in order");
  }
  console.log("site quality failure cleanup self-test passed");

  const historicalCopyFixtures = new Map([
    [
      "/",
      "萬里測站不在環境部現行測站清冊，圖上位置來自環境部歷史測站紀錄。" +
        "台中、崇倫、阿里山、泰山、三民在本專案尚未能定位。",
    ],
    [
      "/space/",
      "其中萬里的座標不是來自環境部現行測站清冊—該站已停用而從清冊消失，" +
        "位置改依審閱過的環境部歷史測站紀錄補上，來源是環境部空品監測網的" +
        "該站測站資料，2026-08-10 查證。本章結果是在納入之後重算的。",
    ],
    [
      "/data/",
      "官方停測公告與年度封存值不一致。自2025年5月1日起的資料形成未解的來源歧異；" +
        "本專案保留封存值，沒有判定這些值有效或無效。",
    ],
  ]);
  for (const [route, text] of historicalCopyFixtures) {
    // /space/ additionally has to cite the file by link; the others carry no
    // citation contract, and an empty list is fine for them.
    const fixtureHrefs = route === "/space/"
      ? ["https://airtw.moenv.gov.tw/CHT/EnvMonitoring/Central/article_station.aspx?SiteID=61"]
      : [];
    const copyProblems = historicalStationCopyProblems(route, text, fixtureHrefs);
    if (copyProblems.length) {
      throw new Error(`${route} precise historical-station copy is rejected: ${copyProblems.join("; ")}`);
    }
  }
  // Both halves must fail closed on their own: the citation when the link goes
  // even though the prose is intact, and the review date when the date goes
  // even though the link is intact. Moving the pin off the visible string is
  // only safe while each is checked separately.
  {
    const spaceText = historicalCopyFixtures.get("/space/") ?? "";
    const spaceHrefs = [
      "https://airtw.moenv.gov.tw/CHT/EnvMonitoring/Central/article_station.aspx?SiteID=61",
    ];
    if (!historicalStationCopyProblems("/space/", spaceText, []).some((problem) =>
      problem.includes("missing historical-station citation link")
    )) {
      throw new Error("/space/ accepts the historical-station disclosure with no citation link");
    }
    // A link back into this project's own tree is not the authority the
    // sentence claims, and must not satisfy the citation.
    if (!historicalStationCopyProblems("/space/", spaceText, [
      "https://example.test/repo/blob/HEAD/conf/station_geo_historical.yaml",
    ]).some((problem) => problem.includes("missing historical-station citation link"))) {
      throw new Error("/space/ accepts a repository file in place of the 環境部 record");
    }
    const undated = spaceText.replace("2026-08-10 查證", "已查證");
    if (!historicalStationCopyProblems("/space/", undated, spaceHrefs).some((problem) =>
      problem.includes("missing historical-station review date")
    )) {
      throw new Error("/space/ accepts the historical-station disclosure with no review date");
    }
  }

  const unsupportedHistoricalClaims = [
    ["/", "萬里測站就是富貴角。"],
    ["/", "五個測站未繪出，它們都已停用。"],
    ["/data/", "停止監測後的資料就是無效。"],
  ];
  for (const [route, text] of unsupportedHistoricalClaims) {
    if (!historicalStationCopyProblems(route, text).some((problem) =>
      problem.includes("unsupported historical-station claim")
    )) {
      throw new Error(`${route} accepts unsupported historical-station copy ${JSON.stringify(text)}`);
    }
  }
  if (
    publicationDisclosureProblems({
      disclosure: {
        defaultCollapsed: true,
        summaryVisible: true,
        summaryText: "資料來源為何不一致？",
        bodyVisibleWhenOpen: true,
        bodyText: "完整說明",
      },
    }).length ||
    !publicationDisclosureProblems({ disclosure: null }).length
  ) {
    throw new Error("the publication-disagreement disclosure predicate is incomplete");
  }
  return 0;
}

// ── a static server, because the built site is what ships ───────────────────

const MIME = {
  ".html": "text/html; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".wasm": "application/wasm",
  ".parquet": "application/octet-stream",
};

function serve(root, port) {
  const server = createServer((req, res) => {
    // `normalize` after stripping the query, so `..` cannot escape `dist`.
    const path = decodeURIComponent(req.url.split("?")[0]);
    let file = normalize(join(root, path));
    if (!file.startsWith(normalize(root))) {
      res.writeHead(403).end();
      return;
    }
    if (existsSync(file) && statSync(file).isDirectory()) file = join(file, "index.html");
    if (!existsSync(file)) {
      res.writeHead(404).end();
      return;
    }
    res.writeHead(200, { "content-type": MIME[extname(file)] ?? "application/octet-stream" });
    createReadStream(file).pipe(res);
  });
  return new Promise((resolve) => server.listen(port, () => resolve(server)));
}

// ── Chrome ───────────────────────────────────────────────────────────────────

const CHROME_CANDIDATES = [
  process.env.CHROME_PATH,
  "C:/Program Files/Google/Chrome/Application/chrome.exe",
  "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  "/usr/bin/google-chrome",
  "/usr/bin/google-chrome-stable",
  "/usr/bin/chromium",
  "/usr/bin/chromium-browser",
].filter(Boolean);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function terminateProcess(proc) {
  if (proc.exitCode !== null) return;
  const exited = new Promise((resolve) => proc.once("exit", resolve));
  proc.kill();
  await Promise.race([exited, sleep(2000)]);
  if (proc.exitCode === null) {
    proc.kill("SIGKILL");
    await Promise.race([exited, sleep(2000)]);
  }
}

async function connect(port, timeoutMs = CDP_TIMEOUT_MS, request = fetch) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const remaining = deadline - Date.now();
    const attemptMs = Math.min(1000, remaining);
    try {
      const response = await withDeadline(
        request(`http://127.0.0.1:${port}/json/list`, {
          signal: AbortSignal.timeout(attemptMs),
        }),
        attemptMs,
        "Chrome debugging endpoint",
      );
      const list = await withDeadline(
        response.json(),
        Math.max(1, deadline - Date.now()),
        "Chrome debugging endpoint JSON",
      );
      const page = list.find((t) => t.type === "page");
      if (page) return page.webSocketDebuggerUrl;
    } catch {
      /* not up yet */
    }
    const pauseMs = Math.min(250, deadline - Date.now());
    if (pauseMs > 0) await sleep(pauseMs);
  }
  throw new Error(`Chrome did not open a debugging port within ${timeoutMs}ms`);
}

async function openBrowser(chrome, debugPort) {
  const proc = spawn(
    chrome,
    [
      "--headless=new",
      "--disable-gpu",
      "--hide-scrollbars",
      "--no-sandbox",
      ...CHROME_TEST_FLAGS,
      `--remote-debugging-port=${debugPort}`,
      `--user-data-dir=${join(process.env.TEMP ?? "/tmp", `twair-quality-profile-${debugPort}`)}`,
      "about:blank",
    ],
    { stdio: "ignore" },
  );

  let ws;
  try {
    ws = new WebSocket(await connect(debugPort));
    await waitForWebSocketOpen(ws, CDP_TIMEOUT_MS, "Chrome WebSocket handshake");
  } catch (error) {
    ws?.close();
    await terminateProcess(proc);
    throw error;
  }

  let id = 0;
  const pending = new Map();
  const eventWaiters = new Map();
  const eventHandlers = new Map();
  ws.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (message.id && pending.has(message.id)) {
      pending.get(message.id)(message);
      pending.delete(message.id);
    }
    if (message.method && eventWaiters.has(message.method)) {
      for (const resolve of eventWaiters.get(message.method)) resolve(message.params);
      eventWaiters.delete(message.method);
    }
    if (message.method && eventHandlers.has(message.method)) {
      for (const handler of eventHandlers.get(message.method)) handler(message.params);
    }
  });
  const send = (method, params = {}, label = method) => {
    let requestId;
    const response = new Promise((resolve) => {
      const i = (id += 1);
      requestId = i;
      pending.set(i, resolve);
      ws.send(JSON.stringify({ id: i, method, params }));
    });
    return withDeadline(response, CDP_TIMEOUT_MS, label).finally(() => pending.delete(requestId));
  };
  const evaluate = async (expr, label = "Runtime.evaluate") =>
    // `awaitPromise` so `settled()` can wait on a requestAnimationFrame pair
    // instead of getting a Promise object back and treating it as truthy.
    (await send(
      "Runtime.evaluate",
      { expression: expr, returnByValue: true, awaitPromise: true },
      label,
    ))
      .result?.result
      ?.value;
  const waitForEvent = (method, label = method) => {
    let resolveEvent;
    const response = new Promise((resolve) => {
      resolveEvent = resolve;
      const waiters = eventWaiters.get(method) ?? new Set();
      waiters.add(resolve);
      eventWaiters.set(method, waiters);
    });
    return withDeadline(response, CDP_TIMEOUT_MS, label).finally(() => {
      const waiters = eventWaiters.get(method);
      waiters?.delete(resolveEvent);
      if (!waiters?.size) eventWaiters.delete(method);
    });
  };
  const onEvent = (method, handler) => {
    const handlers = eventHandlers.get(method) ?? new Set();
    handlers.add(handler);
    eventHandlers.set(method, handlers);
    return () => {
      handlers.delete(handler);
      if (!handlers.size) eventHandlers.delete(method);
    };
  };

  return {
    send,
    evaluate,
    waitForEvent,
    onEvent,
    async close() {
      ws.close();
      await terminateProcess(proc);
    },
  };
}

/**
 * Wait for the page to be *styled*, not merely for 600ms to have passed.
 *
 * This was `await sleep(600)`, and a fixed sleep is a guess about a machine.
 * Three identical runs on a loaded workstation produced 309, 73 and 0 problems:
 * the probe was measuring pages whose stylesheet had not applied yet, so it saw
 * the browser defaults — 16px body type, a 13x17 unstyled `<summary>` — and
 * reported them as design regressions. Every one of those "failures" was the
 * check racing the page.
 *
 * A gate that fails at random is worse than no gate, because the first response
 * to a spurious red is to stop believing the next one. So: poll for signals the
 * page itself can only produce once it is dressed — the document parsed, the
 * site's own custom property resolving, and the webfonts settled, since every
 * measurement here is of glyph boxes.
 */
const READY = `(() => {
  if (document.readyState !== "complete") return false;
  const tick = getComputedStyle(document.documentElement).getPropertyValue("--chart-tick");
  if (!tick.trim()) return false;
  return !document.fonts || document.fonts.status === "loaded";
})()`;
const RENDER_SETTLED = `new Promise((resolve) => {
  let finished = false;
  const finish = (value) => {
    if (finished) return;
    finished = true;
    clearTimeout(timer);
    document.documentElement.getBoundingClientRect();
    resolve(value);
  };
  const visibleFiniteAnimations = () => document.getAnimations().filter((animation) => {
    const effect = animation.effect;
    const target = effect?.target;
    const timing = effect?.getComputedTiming();
    if (
      !(target instanceof Element) ||
      animation.timeline !== document.timeline ||
      !timing ||
      !Number.isFinite(timing.endTime) ||
      !["pending", "running"].includes(animation.playState)
    ) return false;
    const style = getComputedStyle(target);
    const box = target.getBoundingClientRect();
    return style.display !== "none" && style.visibility !== "hidden" &&
      style.visibility !== "collapse" && box.width > 0 && box.height > 0 &&
      box.right > 0 && box.bottom > 0 && box.left < innerWidth && box.top < innerHeight;
  });
  const afterPaint = () => requestAnimationFrame(() => requestAnimationFrame(() => {
    if (visibleFiniteAnimations().length) wait();
    else finish(true);
  }));
  const wait = () => {
    const animations = visibleFiniteAnimations();
    if (!animations.length) {
      afterPaint();
      return;
    }
    Promise.allSettled(animations.map((animation) => animation.finished)).then(afterPaint);
  };
  const timer = setTimeout(() => finish(false), 1500);
  wait();
})`;

async function settlePaint(evaluate, label = "render wait") {
  return evaluate(RENDER_SETTLED, label);
}

async function settled(evaluate, budgetMs = 8000, label = "page") {
  for (let waited = 0; waited < budgetMs; waited += 100) {
    if (await evaluate(READY, `${label} readiness`)) {
      // READY can arrive while the opening transform is still moving a boundary
      // that the next probe is about to measure.
      // One more frame, so a layout invalidated by the last stylesheet has been
      // flushed before anything reads a bounding box off it.
      return Boolean(await settlePaint(evaluate, `${label} render wait`));
    }
    await sleep(100);
  }
  return false;
}

// ── the probe, run inside the page ──────────────────────────────────────────
//
// APCA is written out rather than imported for the same reason the colour maths
// in `check_palette.py` is: a contrast check is not worth a dependency, and the
// formula has to match the one that produced the published numbers.
const PROBE = `(() => {
  const cv = document.createElement("canvas");
  cv.width = cv.height = 1;
  const cx = cv.getContext("2d", { willReadFrequently: true });

  // Any CSS colour, composited over a known ground, as sRGB bytes. Reading a
  // computed colour with a regex breaks: the page keeps oklch() verbatim.
  const paint = (over, colour) => {
    cx.clearRect(0, 0, 1, 1);
    cx.fillStyle = over;
    cx.fillRect(0, 0, 1, 1);
    cx.fillStyle = colour;
    cx.fillRect(0, 0, 1, 1);
    const d = cx.getImageData(0, 0, 1, 1).data;
    return [d[0], d[1], d[2]];
  };

  const y = ([r, g, b]) => {
    const f = (v) => Math.pow(v / 255, 2.4);
    const v = 0.2126729 * f(r) + 0.7151522 * f(g) + 0.0721750 * f(b);
    return v < 0.022 ? v + Math.pow(0.022 - v, 1.414) : v;
  };

  // APCA 0.98G-4g, absolute value: the sign says which way round the pair is,
  // and the floor applies either way.
  const lc = (txt, bg) => {
    const yt = y(txt), yb = y(bg);
    if (Math.abs(yb - yt) < 0.0005) return 0;
    if (yb > yt) {
      const s = (Math.pow(yb, 0.56) - Math.pow(yt, 0.57)) * 1.14;
      return s < 0.1 ? 0 : (s - 0.027) * 100;
    }
    const s = (Math.pow(yb, 0.65) - Math.pow(yt, 0.62)) * 1.14;
    return s > -0.1 ? 0 : Math.abs((s + 0.027) * 100);
  };

  const groundOf = (el) => {
    let node = el;
    while (node) {
      const bg = getComputedStyle(node).backgroundColor;
      if (bg && bg !== "rgba(0, 0, 0, 0)" && bg !== "transparent") return bg;
      node = node.parentElement;
    }
    return getComputedStyle(document.body).backgroundColor;
  };

  const out = { nodes: 0, lowContrast: [], smallestFont: Infinity, smallestAnnotation: Infinity,
    smallTargets: [], collisions: [], markCollisions: [], hyphenSigns: [], toolClashes: [],
    tableWraps: 0, tableScrollers: 0,
    invalidTableScrollers: [], invalidTableRules: [], invalidChartText: [],
    invalidChartStrokes: [] };
  out.body = parseFloat(getComputedStyle(document.body).fontSize);

  for (const contract of ${JSON.stringify(CHART_TEXT_CONTRACTS)}) {
    let smallest = Infinity;
    for (const element of document.querySelectorAll(contract.selector)) {
      const style = getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      if (
        style.display === "none" || style.visibility === "hidden" ||
        rect.width <= 0 || rect.height <= 0
      ) continue;
      smallest = Math.min(smallest, parseFloat(style.fontSize));
    }
    if (
      smallest !== Infinity &&
      smallest + ${CSS_PX_SERIALIZATION_EPSILON} < out.body * contract.ratio
    ) {
      out.invalidChartText.push({
        role: contract.role,
        size: +smallest.toFixed(3),
        required: +(out.body * contract.ratio).toFixed(3),
      });
    }
  }

  for (const element of document.querySelectorAll(".plot-line, .plot-grid, .plot-axis")) {
    const actual = parseFloat(getComputedStyle(element).strokeWidth);
    const expected = element.classList.contains("plot-grid")
      ? 1
      : element.classList.contains("plot-axis")
        ? 1.5
        : element.classList.contains("emphasis-primary")
          ? 3
          : element.classList.contains("emphasis-comparison")
            ? 2.25
            : 2.5;
    if (Math.abs(actual - expected) > ${CHART_STROKE_EPSILON}) {
      out.invalidChartStrokes.push({
        cls: String(element.getAttribute("class") ?? ""),
        actual,
        expected,
      });
    }
  }

  const MARKS = ".plot-x span, .plot-y span, .plot-keys span, .axis span";

  // The marks a reader has to read INSIDE a figure, as opposed to the caption
  // underneath it. This is the set the "not the smallest type" rule is about.
  for (const el of document.querySelectorAll(MARKS)) {
    const cs = getComputedStyle(el);
    if (cs.display === "none" || cs.visibility === "hidden") continue;
    const size = parseFloat(cs.fontSize);
    if (size < out.smallestAnnotation) out.smallestAnnotation = size;
  }
  if (out.smallestAnnotation === Infinity) out.smallestAnnotation = 0;

  // Do two labels on the same axis strip land on top of each other?
  //
  // Everything else here is measured per node — a size, a contrast, a target.
  // This is the one chart defect that only exists BETWEEN nodes, and it is the
  // one that appears at some widths and not others: the marks are positioned in
  // percentages and the figure is fluid, so a tick strip that reads cleanly at
  // 1440 can pile up at 375 without any single number changing.
  //
  // Compared only within one strip. An x label and a y label sharing a pixel is
  // the plot's bottom-left corner, which is where they are supposed to be.
  for (const strip of document.querySelectorAll(".plot-x, .plot-y, .plot-keys, .axis")) {
    const marks = [];
    for (const el of strip.querySelectorAll("span")) {
      const cs = getComputedStyle(el);
      if (cs.display === "none" || cs.visibility === "hidden") continue;
      const text = el.textContent.trim();
      if (!text) continue;
      const r = el.getBoundingClientRect();
      if (!r.width || !r.height) continue;
      marks.push({ text, r });
    }
    for (let i = 0; i < marks.length; i += 1) {
      for (let j = i + 1; j < marks.length; j += 1) {
        const a = marks[i].r;
        const b = marks[j].r;
        // A pixel of touching is kerning and antialiasing, not a collision.
        // 2026-08-04 — the approved legibility contract supersedes the one-pixel
        // touching tolerance: visible labels in one axis strip need 4 CSS px of air.
        const horizontal = strip.matches(".plot-x, .axis");
        const orthogonalOverlap = horizontal
          ? Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top)
          : Math.min(a.right, b.right) - Math.max(a.left, b.left);
        const clearance = horizontal
          ? Math.max(a.left, b.left) - Math.min(a.right, b.right)
          : Math.max(a.top, b.top) - Math.min(a.bottom, b.bottom);
        if (orthogonalOverlap > 1 && clearance < ${AXIS_LABEL_CLEARANCE_PX}) {
          out.collisions.push({
            strip: String(strip.className || "").slice(0, 20),
            a: marks[i].text.slice(0, 14),
            b: marks[j].text.slice(0, 14),
            px: +clearance.toFixed(1),
          });
        }
      }
    }
  }

  // Data marks pile up the same way, and nothing was watching them.
  //
  // The note above is written about axis labels, but the mechanism it describes
  // is not specific to text: a position in percent against a size in pixels
  // reads cleanly at one width and collapses at another, with no number in the
  // source changing. Figure 6.2 spread its four cross-validation folds across a
  // band of 3.2% while a mark and its ring occupy 8.5 CSS px, so the pitch went
  // 9.97px at 1280, 5.89 at 768 and 2.28 at 375 — where 88 pairs overlapped and
  // four folds read as one mark. That figure exists to say the four folds
  // disagree, so the width at which they stop being four marks is the width at
  // which it stops making its argument.
  //
  // Same series only, compared by rendered colour. Two series landing on one
  // pixel is two models agreeing, which is the finding rather than a defect,
  // and a horizon's summary mark is meant to sit among its own folds.
  for (const figure of document.querySelectorAll("main figure")) {
    const marks = [];
    for (const el of figure.querySelectorAll(".plot-pt.fold")) {
      const cs = getComputedStyle(el);
      if (cs.display === "none" || cs.visibility === "hidden") continue;
      const r = el.getBoundingClientRect();
      if (!r.width || !r.height) continue;
      marks.push({ colour: cs.color, r });
    }
    const label = figure.closest("section")
      ? (figure.closest("section").querySelector(".evidence-number") || {}).textContent || "?"
      : "?";
    for (let i = 0; i < marks.length; i += 1) {
      for (let j = i + 1; j < marks.length; j += 1) {
        if (marks[i].colour !== marks[j].colour) continue;
        const a = marks[i].r;
        const b = marks[j].r;
        const dx = Math.abs(a.left - b.left);
        const dy = Math.abs(a.top - b.top);
        if (dx < a.width && dy < a.height) {
          out.markCollisions.push({
            figure: String(label).trim().slice(0, 10),
            dx: +dx.toFixed(1),
            w: +a.width.toFixed(1),
          });
        }
      }
    }
  }

  // A negative axis label has to carry a minus sign, not a hyphen.
  //
  // The chart helpers n and nFixed emit U+002D, because they also build SVG path
  // data and a d attribute cannot take a typographic minus. An axis that prints
  // their output raw prints a hyphen. Measured in this site's numeric face at
  // 24px: a digit is 12.94px, U+002D is 9.61px, U+2212 is 16.42px — the hyphen
  // reads as a dash joined to the number rather than as its sign. Figure 3.1 put
  // a full-width plus against a half-width hyphen at equal distances either side
  // of a zero, and fixed it inline; axisNumber is that fix as a function, and
  // this is what notices the next axis that forgets to call it.
  //
  // Leading only. Chapter 8's x axis reads 2-3h, 4-12h, 13-48h, where the hyphen
  // is a range and not a sign.
  for (const el of document.querySelectorAll(
    "main .plot-x span, main .plot-y span, main .axis span, main .plot-keys span",
  )) {
    const cs = getComputedStyle(el);
    if (cs.display === "none" || cs.visibility === "hidden") continue;
    const text = el.textContent.trim();
    if (/^-[0-9]/.test(text)) {
      out.hyphenSigns.push({
        text: text.slice(0, 14),
        strip: String(el.parentElement ? el.parentElement.className || "" : "").slice(0, 20),
      });
    }
  }

  // A figure's toolbar sits at its top right, and must not land on its title.
  //
  // Above 1080px the two controls are lifted out of flow into the figure's top
  // right corner, level with the number and the question. What decides whether
  // that is safe is the length of the title, which is prose and changes: the
  // threshold was measured against the longest one on the site, and a longer one
  // written later would slide underneath the toolbar with nothing to say so.
  // Chapter 8's case titles were all rewritten the day before this check.
  //
  // Ink, not boxes. The title is a block that spans the header whatever it says,
  // so its own rectangle always reaches the toolbar and would report a clash on
  // every figure.
  for (const section of document.querySelectorAll("main .evidence-figure")) {
    const tools = section.querySelector(".fig-tools");
    if (!tools) continue;
    if (getComputedStyle(tools).position !== "absolute") continue;
    const box = tools.getBoundingClientRect();
    const card = section.getBoundingClientRect();
    const label = (section.querySelector(".evidence-number") || {}).textContent || "?";
    if (box.right > card.right + 1 || box.left < card.left - 1) {
      out.toolClashes.push({figure: String(label).trim().slice(0, 10), why: "outside the card"});
      continue;
    }
    let ink = 0;
    for (const part of section.querySelectorAll(".evidence-header")) {
      const wk = document.createTreeWalker(part, NodeFilter.SHOW_TEXT);
      let node;
      while ((node = wk.nextNode())) {
        if (!node.textContent.trim()) continue;
        const range = document.createRange();
        range.selectNodeContents(node);
        for (const r of range.getClientRects()) {
          if (r.top < box.bottom && r.bottom > box.top) ink = Math.max(ink, r.right);
        }
      }
    }
    if (ink && box.left < ink + 8) {
      out.toolClashes.push({
        figure: String(label).trim().slice(0, 10),
        why: "title ink reaches within " + Math.round(box.left - ink) + "px of the toolbar",
      });
    }
  }

  const walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let n;
  while ((n = walk.nextNode())) {
    const text = n.textContent.trim();
    if (!text) continue;
    const el = n.parentElement;
    if (!el) continue;
    const cs = getComputedStyle(el);
    if (cs.display === "none" || cs.visibility === "hidden" || parseFloat(cs.opacity) === 0) continue;
    const box = el.getBoundingClientRect();
    if (!box.width || !box.height) continue;

    out.nodes += 1;
    const size = parseFloat(cs.fontSize);
    if (size < out.smallestFont) out.smallestFont = size;

    const ground = groundOf(el);
    const value = lc(paint(ground, cs.color), paint("#fff", ground));
    if (value < ${MIN_LC}) {
      out.lowContrast.push({ text: text.slice(0, 26), lc: +value.toFixed(1), size: +size.toFixed(1),
        cls: String(el.className || "").slice(0, 28) });
    }
  }

  // Native controls and disclosures all need the same finger-sized floor. The
  // rail labels are included because they act as the no-script drawer buttons.
  for (const el of document.querySelectorAll(".rail-open, .rail-shut, button, select, summary")) {
    const cs = getComputedStyle(el);
    if (cs.display === "none" || cs.visibility === "hidden") continue;
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height) continue;
    const floor = el.classList.contains("fig-tool")
      ? ${MIN_TARGET_PX + TARGET_LAYOUT_RESERVE_PX}
      : ${MIN_TARGET_PX};
    if (r.height < floor) {
      out.smallTargets.push({ cls: String(el.className || el.tagName).slice(0, 28),
        w: +r.width.toFixed(1), h: +r.height.toFixed(1), floor });
    }
  }

  // A wide table is allowed to be wider than its frame; the document is not.
  // Record the two separately so a legitimate local scroller can never excuse
  // page-level overflow, and so a clipped table cannot masquerade as contained.
  for (const wrap of document.querySelectorAll(".table-wrap")) {
    const cs = getComputedStyle(wrap);
    const r = wrap.getBoundingClientRect();
    if (cs.display === "none" || cs.visibility === "hidden" || !r.width || !r.height) continue;
    out.tableWraps += 1;
    if (!cs.scrollbarColor || cs.scrollbarColor === "auto") {
      out.invalidTableRules.push("table wrapper has no persistent scrollbar affordance");
    }
    const overflow = wrap.scrollWidth - wrap.clientWidth;
    if (overflow <= 1) continue;
    out.tableScrollers += 1;
    if (cs.overflowX !== "auto" && cs.overflowX !== "scroll") {
      out.invalidTableScrollers.push({
        overflow: +overflow.toFixed(1),
        overflowX: cs.overflowX,
      });
    }
  }

  for (const table of document.querySelectorAll("table")) {
    const header = table.querySelector("thead th");
    const cell = table.querySelector("tbody td");
    if (!header || !cell) continue;
    const hs = getComputedStyle(header);
    const cs = getComputedStyle(cell);
    const padding = ["paddingTop", "paddingRight", "paddingBottom", "paddingLeft"];
    if (padding.some((name) => Math.abs(parseFloat(hs[name]) - parseFloat(cs[name])) > 0.1)) {
      out.invalidTableRules.push("table header/body padding differs");
    }
    for (const numeric of table.querySelectorAll("th.num, td.num")) {
      const ns = getComputedStyle(numeric);
      if (ns.textAlign !== "right" || !ns.fontVariantNumeric.includes("tabular-nums")) {
        out.invalidTableRules.push("numeric table cell is not right/tabular aligned");
        break;
      }
    }
    const rows = table.tBodies[0]?.rows ?? [];
    if (rows.length > 1) {
      const first = getComputedStyle(rows[0]).backgroundColor;
      const second = getComputedStyle(rows[1]).backgroundColor;
      if (first === second) out.invalidTableRules.push("table zebra rows have no contrast");
    }
  }

  out.overflow = document.documentElement.scrollWidth - document.documentElement.clientWidth;
  const rail = document.querySelector(".rail");
  const main = document.querySelector("main");
  const handle = document.querySelector(".handle");
  const railStyle = rail ? getComputedStyle(rail) : null;
  const handleStyle = handle ? getComputedStyle(handle) : null;
  const railRect = rail?.getBoundingClientRect() ?? null;
  out.railWidth = railRect ? +railRect.width.toFixed(1) : 0;
  out.railVisible = Boolean(
    railRect && railStyle?.display !== "none" && railStyle?.visibility !== "hidden" &&
      railRect.width > 0 && railRect.height > 0 && railRect.right > 1 &&
      railRect.left < innerWidth - 1 && railRect.bottom > 1 && railRect.top < innerHeight - 1,
  );
  out.mainWidth = main ? +main.getBoundingClientRect().width.toFixed(1) : 0;
  out.handleVisible = Boolean(
    handle && handleStyle?.display !== "none" && handleStyle?.visibility !== "hidden" &&
      handle.getClientRects().length,
  );
  const compactIdentity = document.querySelector("[data-site-identity]");
  const compactIdentityStyle = compactIdentity ? getComputedStyle(compactIdentity) : null;
  const compactIdentityRect = compactIdentity?.getBoundingClientRect() ?? null;
  out.compactIdentity = compactIdentity && compactIdentityStyle && compactIdentityRect
    ? {
        visible: compactIdentityStyle.display !== "none" &&
          compactIdentityStyle.visibility !== "hidden" &&
          compactIdentityRect.width > 0 && compactIdentityRect.height > 0,
        accessibleName: compactIdentity.getAttribute("aria-label") ?? "",
        visibleText: compactIdentity.innerText.trim(),
        clientWidth: compactIdentity.clientWidth,
        scrollWidth: compactIdentity.scrollWidth,
        textOverflow: compactIdentityStyle.textOverflow,
      }
    : null;
  if (out.smallestFont === Infinity) out.smallestFont = 0;
  return out;
})()`;

// ── run ──────────────────────────────────────────────────────────────────────

async function main() {
  console.log("site-quality stage: repository claim boundary");
  const repositoryClaimProblems = repositoryClaimBoundaryProblems();
  if (repositoryClaimProblems.length) {
    for (const problem of repositoryClaimProblems) console.log(`  FAIL: ${problem}`);
    return 1;
  }
  console.log("site-quality stage: text-zoom route contract");
  const textZoomRouteProblems = textZoomRouteMatrixProblems();
  if (textZoomRouteProblems.length) {
    for (const problem of textZoomRouteProblems) console.log(`  FAIL: ${problem}`);
    return 1;
  }
  console.log("site-quality stage: trend interaction detector mutations");
  const completePickerState = {
    filtered: {
      checked: 6,
      displays: ["none", "none", "inline", "inline", "inline", "inline", "inline", "inline"],
      rows: 6,
      resetVisible: true,
      announcement: "顯示 6 條",
    },
    empty: {
      checked: 0,
      displays: Array(8).fill("none"),
      rows: 0,
      announcement: "目前未顯示任何空品區",
    },
    restored: {
      checked: 8,
      rows: 8,
      resetVisible: false,
      focusReturned: true,
    },
  };
  if (trendPickerInteractionProblems(completePickerState).length) {
    throw new Error("the trend picker predicate rejected the complete interaction");
  }
  const retainedUncheckedPickerState = structuredClone(completePickerState);
  retainedUncheckedPickerState.filtered.displays[0] = "inline";
  if (
    !trendPickerInteractionProblems(retainedUncheckedPickerState).some((problem) =>
      problem.includes("unchecked paths are not both hidden"))
  ) {
    throw new Error("the trend picker predicate accepts a rendered unchecked path");
  }
  const completeNoScriptPicker = {
    resetVisibleBeforeFiltering: true,
    filtered: {
      checked: 6,
      displays: ["none", "none", "inline", "inline", "inline", "inline", "inline", "inline"],
    },
    restored: { checked: 8, resetVisible: true, focusStayedOnReset: true },
  };
  if (trendNoScriptPickerProblems(completeNoScriptPicker).length) {
    throw new Error("the no-JavaScript trend picker predicate rejected the complete interaction");
  }
  const inertNoScriptPicker = structuredClone(completeNoScriptPicker);
  inertNoScriptPicker.filtered.checked = 8;
  inertNoScriptPicker.filtered.displays.fill("inline");
  if (trendNoScriptPickerProblems(inertNoScriptPicker).length !== 2) {
    throw new Error("the no-JavaScript trend picker predicate accepts inert label activation");
  }
  const completeFilteredExport = {
    checkedAttributes: [false, false, true, true, true, true, true, true],
    pathDisplays: ["none", "none", "inline", "inline", "inline", "inline", "inline", "inline"],
  };
  if (trendFilteredExportProblems(completeFilteredExport).length) {
    throw new Error("the filtered PNG predicate rejected the complete export");
  }
  const unsynchronisedExport = structuredClone(completeFilteredExport);
  unsynchronisedExport.checkedAttributes.fill(true);
  unsynchronisedExport.pathDisplays.fill("inline");
  if (trendFilteredExportProblems(unsynchronisedExport).length !== 2) {
    throw new Error("the filtered PNG predicate accepts unsynchronised checkbox or path state");
  }
  const completeZoomFit = { stageClientHeight: 682, stageScrollHeight: 682, plotHeight: 320 };
  if (trendZoomFitProblems(completeZoomFit).length) {
    throw new Error("the trend zoom-fit predicate rejected the complete dialog");
  }
  const overflowingZoom = { ...completeZoomFit, stageScrollHeight: 684 };
  if (
    !trendZoomFitProblems(overflowingZoom).some((problem) =>
      problem.includes("overflows the dialog stage"))
  ) {
    throw new Error("the trend zoom-fit predicate accepts dialog overflow");
  }
  if (!existsSync(DIST)) {
    console.error(`${DIST} not found — run \`npm --prefix web run build\` first`);
    return 1;
  }
  const chrome = CHROME_CANDIDATES.find((p) => existsSync(p));
  if (!chrome) {
    console.log("no Chrome binary found; skipping (set CHROME_PATH to run this check)");
    return 0;
  }

  const resources = { server: await serve(DIST, PORT), browser: null };
  return withRuntimeCleanup(resources, async () => {
  let debugPort = PORT + 1;
  let browser = await openBrowser(chrome, debugPort);
  resources.browser = browser;
  let send = browser.send;
  let evaluate = browser.evaluate;
  let waitForEvent = browser.waitForEvent;
  let onEvent = browser.onEvent;
  const restartBrowser = async () => {
    debugPort += 1;
    browser = await replaceBrowser(browser, () => openBrowser(chrome, debugPort));
    resources.browser = browser;
    send = browser.send;
    evaluate = browser.evaluate;
    waitForEvent = browser.waitForEvent;
    onEvent = browser.onEvent;
  };

  const pressKey = async (key) => {
    await send("Input.dispatchKeyEvent", { type: "keyDown", key, code: key });
    await send("Input.dispatchKeyEvent", { type: "keyUp", key, code: key });
    await settlePaint(evaluate);
  };

  const focusVisibleStates = async (selectors) => {
    // `:focus-visible` follows the input modality. Programmatic focus from an
    // untouched CDP page is pointer-like, so establish keyboard modality first.
    await pressKey("Tab");
    return evaluate(`(() => {
      const visible = (element) => {
        const style = getComputedStyle(element);
        const rect = element.getBoundingClientRect();
        return style.display !== "none" && style.visibility !== "hidden" &&
          rect.width > 0 && rect.height > 0;
      };
      const oldScrollBehavior = document.documentElement.style.scrollBehavior;
      document.documentElement.style.scrollBehavior = "auto";
      const result = ${JSON.stringify(selectors)}.map(({ name, selector, required }) => {
        const elements = [...document.querySelectorAll(selector)].filter(visible);
        const states = elements.map((element) => {
          element.focus();
          element.scrollIntoView({ block: "nearest", inline: "nearest" });
          const style = getComputedStyle(element);
          const rect = element.getBoundingClientRect();
          const state = {
            label: String(element.id || element.className || element.textContent || element.tagName)
              .trim().slice(0, 28),
            active: document.activeElement === element,
            focusVisible: element.matches(":focus-visible"),
            outlineStyle: style.outlineStyle,
            outlineWidth: parseFloat(style.outlineWidth),
            inViewport: rect.left >= -1 && rect.right <= innerWidth + 1 &&
              rect.top >= -1 && rect.bottom <= innerHeight + 1,
          };
          element.blur();
          return state;
        });
        return { name, required, count: elements.length, states };
      });
      document.documentElement.style.scrollBehavior = oldScrollBehavior;
      return result;
    })()`);
  };

  const readoutState = () =>
    evaluate(`(() => {
      const plot = document.querySelector(".plot[data-readout]");
      if (!plot) return null;
      const panel = plot.querySelector(".readout-panel") ??
        (plot.nextElementSibling?.matches(".readout-dock")
          ? plot.nextElementSibling.querySelector(".readout-panel")
          : null);
      const dock = panel?.closest(".readout-dock") ?? null;
      const area = plot.querySelector(".plot-area");
      const figure = plot.closest("figure");
      const holder = plot.querySelector(".plot-readout-data");
      let data = null;
      try { data = holder?.textContent ? JSON.parse(holder.textContent) : null; } catch {}
      const panelBox = panel?.getBoundingClientRect();
      const areaBox = area?.getBoundingClientRect();
      const rect = (element) => {
        const box = element?.getBoundingClientRect();
        return box ? {
          top: box.top, right: box.right, bottom: box.bottom, left: box.left,
          width: box.width, height: box.height,
        } : null;
      };
      return {
        panelReady: Boolean(panel),
        panelInDock: Boolean(dock && dock.previousElementSibling === plot),
        overlaysInArea: Boolean(
          area?.querySelector(".readout-rule") && area?.querySelectorAll(".readout-dot").length,
        ),
        active: document.activeElement === plot,
        role: plot.getAttribute("role"),
        reading: plot.dataset.reading ?? null,
        visible: panel ? getComputedStyle(panel).opacity !== "0" : false,
        when: panel?.querySelector(".readout-when")?.textContent?.trim() ?? null,
        first: data?.x?.[0] == null
          ? null
          : data.unit ? String(data.x[0]) + "・" + data.unit : String(data.x[0]),
        second: data?.x?.length > 1
          ? data.unit ? String(data.x[1]) + "・" + data.unit : String(data.x[1])
          : null,
        penultimate: data?.x?.length > 1
          ? data.unit
            ? String(data.x[data.x.length - 2]) + "・" + data.unit
            : String(data.x[data.x.length - 2])
          : null,
        last: data?.x?.length
          ? data.unit
            ? String(data.x[data.x.length - 1]) + "・" + data.unit
            : String(data.x[data.x.length - 1])
          : null,
        points: data?.x?.length ?? 0,
        dockWidth: dock?.getBoundingClientRect().width ?? 0,
        dockHeight: dock?.getBoundingClientRect().height ?? 0,
        dockMinBlock: dock ? parseFloat(getComputedStyle(dock).minBlockSize) : 0,
        panelHeight: panelBox?.height ?? 0,
        rowHeights: panel
          ? [...panel.querySelectorAll(".readout-row")].map(
              (row) => +row.getBoundingClientRect().height.toFixed(3),
            )
          : [],
        whenBox: rect(panel?.querySelector(".readout-when")),
        rowBoxes: panel
          ? [...panel.querySelectorAll(".readout-row")].map(rect)
          : [],
        rowMetrics: panel
          ? [...panel.querySelectorAll(".readout-row")].map((row) => {
              const name = row.querySelector(".readout-name")?.getBoundingClientRect();
              const value = row.querySelector(".readout-value")?.getBoundingClientRect();
              return {
                row: +row.getBoundingClientRect().width.toFixed(3),
                name: name ? +name.width.toFixed(3) : 0,
                value: value ? +value.width.toFixed(3) : 0,
              };
            })
          : [],
        figureHeight: figure?.getBoundingClientRect().height ?? 0,
        overlapX: panelBox && areaBox
          ? Math.max(0, Math.min(panelBox.right, areaBox.right) - Math.max(panelBox.left, areaBox.left))
          : null,
        overlapY: panelBox && areaBox
          ? Math.max(0, Math.min(panelBox.bottom, areaBox.bottom) - Math.max(panelBox.top, areaBox.top))
          : null,
      };
    })()`);

  const homepageFirstViewport = async ({
    requireVerticalViewport = true,
    requireScrollZero = true,
    geometryMutation = null,
  } = {}) => {
    const geometry = await evaluate(`(() => {
      const rectOf = (element) => {
        const rect = element.getBoundingClientRect();
        return {
          top: rect.top, right: rect.right, bottom: rect.bottom, left: rect.left,
          width: rect.width, height: rect.height,
        };
      };
      const visiblyRendered = (element) => {
        for (let node = element; node; node = node.parentElement) {
          const style = getComputedStyle(node);
          if (
            style.display === "none" || style.visibility === "hidden" ||
            style.visibility === "collapse" || Number(style.opacity) === 0
          ) return false;
        }
        return true;
      };
      const clippedByAncestor = (element) => {
        const rect = element.getBoundingClientRect();
        for (let ancestor = element.parentElement; ancestor; ancestor = ancestor.parentElement) {
          const style = getComputedStyle(ancestor);
          const bounds = ancestor.getBoundingClientRect();
          const clipsX = ["auto", "clip", "hidden", "scroll"].includes(style.overflowX);
          const clipsY = ["auto", "clip", "hidden", "scroll"].includes(style.overflowY);
          if (clipsX && (rect.left < bounds.left - 1 || rect.right > bounds.right + 1)) return true;
          if (clipsY && (rect.top < bounds.top - 1 || rect.bottom > bounds.bottom + 1)) return true;
        }
        return false;
      };
      const inside = (element, container, centreOnly = false) => {
        if (!container) return true;
        const rect = element.getBoundingClientRect();
        const bounds = container.getBoundingClientRect();
        if (centreOnly) {
          const x = (rect.left + rect.right) / 2;
          const y = (rect.top + rect.bottom) / 2;
          return x >= bounds.left - 1 && x <= bounds.right + 1 &&
            y >= bounds.top - 1 && y <= bounds.bottom + 1;
        }
        return rect.left >= bounds.left - 1 && rect.right <= bounds.right + 1 &&
          rect.top >= bounds.top - 1 && rect.bottom <= bounds.bottom + 1;
      };
      const containerOverflow = (element, container) => {
        if (!container) return 0;
        const rect = element.getBoundingClientRect();
        const bounds = container.getBoundingClientRect();
        return Math.max(
          bounds.left - rect.left,
          rect.right - bounds.right,
          bounds.top - rect.top,
          rect.bottom - bounds.bottom,
          0,
        );
      };
      const fills = (element, container, axis) => {
        if (!container || !axis) return true;
        const rect = element.getBoundingClientRect();
        const bounds = container.getBoundingClientRect();
        const inline = Math.abs(rect.left - bounds.left) <= 1 &&
          Math.abs(rect.right - bounds.right) <= 1;
        const block = Math.abs(rect.top - bounds.top) <= 1 &&
          Math.abs(rect.bottom - bounds.bottom) <= 1;
        return inline && (axis === "inline" || block);
      };
      const inspect = (element, container = null, centreOnly = false, fillAxis = null) => element ? {
        ...rectOf(element),
        identifier: element.getAttribute("data-station") ?? element.textContent.trim() ?? null,
        visible: visiblyRendered(element),
        clipped: clippedByAncestor(element),
        contained: inside(element, container, centreOnly),
        containerOverflow: containerOverflow(element, container),
        fillsContainer: fills(element, container, fillAxis),
      } : null;
      const inspectAll = (selector, container = null, centreOnly = false) =>
        [...document.querySelectorAll(selector)].map((element) =>
          inspect(element, container, centreOnly));
      const fontSize = (selector) => {
        const element = document.querySelector(selector);
        return element ? Number.parseFloat(getComputedStyle(element).fontSize) : Number.NaN;
      };
      const offset = (value, size) => value.endsWith("%")
        ? parseFloat(value) / 100 * size : parseFloat(value);
      const anchored = (element, container, axes) => {
        const rect = element.getBoundingClientRect();
        const bounds = container.getBoundingClientRect();
        const expectedX = bounds.left + offset(element.style.left || getComputedStyle(element).left, bounds.width);
        const expectedY = bounds.top + offset(element.style.top || getComputedStyle(element).top, bounds.height);
        const actualX = (rect.left + rect.right) / 2;
        const actualY = (rect.top + rect.bottom) / 2;
        return Number.isFinite(expectedX) && Number.isFinite(expectedY) &&
          Math.abs(actualX - expectedX) <= 1 &&
          (axes === "inline" || Math.abs(actualY - expectedY) <= 1);
      };
      const inspectAnchoredAll = (selector, container, axes, centreOnly = false) =>
        [...document.querySelectorAll(selector)].map((element) => ({
          ...inspect(element, container, centreOnly),
          anchored: anchored(element, container, axes),
        }));
      const map = document.querySelector("[data-homepage-map]");
      const atlas = document.querySelector("[data-homepage-atlas]");
      const legend = document.querySelector("[data-homepage-map-legend]");
      const scaleBar = legend?.querySelector(".scale-bar") ?? null;
      const scaleTicks = legend?.querySelector(".scale-ticks") ?? null;
      const opening = document.querySelector("[data-homepage-opening]");
      const routes = document.querySelector("[data-homepage-routes]");
      const primaryRoutes = [...document.querySelectorAll("[data-homepage-primary-route]")];
      const mapFrame = document.querySelector("[data-homepage-map-frame]");
      const postMap = document.querySelector("[data-homepage-post-map]");
      const sourceIndexes = new Map();
      const sourceWalker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
      let sourceIndex = 0;
      for (let node = sourceWalker.currentNode; node; node = sourceWalker.nextNode()) {
        sourceIndexes.set(node, sourceIndex);
        sourceIndex += 1;
      }
      const sourceIndexOf = (element) => sourceIndexes.has(element)
        ? sourceIndexes.get(element) : null;
      const geometryMutation = ${JSON.stringify(geometryMutation)};
      const mutated = geometryMutation
        ? document.querySelector(geometryMutation.selector) : null;
      const originalStyle = mutated?.getAttribute("style") ?? null;
      if (mutated) {
        mutated.style.setProperty(geometryMutation.property, geometryMutation.value);
      }
      const result = {
        map: inspect(map),
        mapSvg: inspect(map?.querySelector(":scope > svg") ?? null, map, false, "both"),
        stationMarks: inspectAnchoredAll("[data-homepage-map] .dot", map, "both"),
        countyLabels: inspectAll("[data-homepage-map] .county-label", map),
        legend: inspect(legend),
        scaleBar: inspect(scaleBar, legend, false, "inline"),
        scaleTicks: inspect(scaleTicks, legend, false, "inline"),
        scaleSegments: inspectAll("[data-homepage-map-legend] .scale-bar > span", scaleBar),
        tickMarks: inspectAnchoredAll(
          "[data-homepage-map-legend] .scale-ticks > span",
          scaleTicks,
          "inline",
        ).filter((tick) => tick.visible),
        mobileType: {
          viewportWidth: innerWidth,
          root: Number.parseFloat(getComputedStyle(document.documentElement).fontSize),
          finding: fontSize("#hero .hero-finding"),
          routeLabel: fontSize("#hero .start-here-label"),
          routeIntro: fontSize("#hero .start-here-intro"),
          routeClaim: fontSize("#hero .start-here-claim"),
        },
        editorialLayout: {
          mode: atlas && getComputedStyle(atlas).gridTemplateColumns.trim().split(/\\s+/).length > 1
            ? "wide" : "stacked",
          opening: inspect(opening),
          routes: inspect(routes),
          map: inspect(mapFrame),
          postMap: inspect(postMap),
          viewport: { width: innerWidth, height: innerHeight },
        },
        editorialOrder: {
          opening: sourceIndexOf(opening),
          routes: sourceIndexOf(routes),
          primary: sourceIndexOf(primaryRoutes[0] ?? null),
          map: sourceIndexOf(mapFrame),
          postMap: sourceIndexOf(postMap),
        },
        primaryRoute: inspect(primaryRoutes[0] ?? null),
        primaryRouteCount: primaryRoutes.length,
        viewport: { width: innerWidth, height: innerHeight },
        scrollY,
      };
      if (mutated) {
        if (originalStyle === null) mutated.removeAttribute("style");
        else mutated.setAttribute("style", originalStyle);
      }
      return result;
    })()`);
    if (requireScrollZero && geometry?.scrollY !== 0) {
      return ["homepage did not start at scroll position zero"];
    }
    const problems = [
      ...firstViewportProblems({
        ...geometry,
        requireVerticalViewport:
          requireVerticalViewport && geometry.editorialLayout.mode === "wide",
      }),
      ...countyLabelProblems({
        map: geometry.map,
        labels: geometry.countyLabels,
        expectedVisible:
          geometry.map?.width >= MIN_LABELLED_MAP_WIDTH_PX
            ? EXPECTED_DESKTOP_COUNTY_LABELS
            : null,
      }),
      ...editorialHomepageLayoutProblems(geometry.editorialLayout),
      ...editorialHomepageOrderProblems(geometry.editorialOrder),
      ...homepageMobileTypeProblems(geometry.mobileType),
    ];
    if (geometry.primaryRouteCount !== 1) {
      problems.push(`homepage has ${geometry.primaryRouteCount} primary routes, expected one`);
    }
    if (
      geometry.viewport.width <= 390 &&
      (
        !geometry.primaryRoute || !geometry.primaryRoute.visible ||
        geometry.primaryRoute.width <= 0 || geometry.primaryRoute.height <= 0 ||
        geometry.primaryRoute.top < -1 ||
        geometry.primaryRoute.bottom > geometry.viewport.height + 1
      )
    ) {
      problems.push("homepage primary route is not visible within the first mobile viewport");
    }
    return problems;
  };

  const accessibilityTextsForSelectors = async (selectors) => {
    const documentResult = await send("DOM.getDocument", { depth: 0, pierce: true });
    const documentNodeId = documentResult.result?.root?.nodeId;
    if (!documentNodeId) return selectors.map(() => []);
    const backendNodeGroups = [];
    for (const selector of selectors) {
      const queryResult = await send("DOM.querySelectorAll", {
        nodeId: documentNodeId,
        selector,
      });
      const backendNodeIds = [];
      for (const nodeId of queryResult.result?.nodeIds ?? []) {
        const described = await send("DOM.describeNode", { nodeId });
        backendNodeIds.push(described.result?.node?.backendNodeId ?? null);
      }
      backendNodeGroups.push(backendNodeIds);
    }
    const tree = await send("Accessibility.getFullAXTree", {});
    const nodes = tree.result?.nodes ?? [];
    const byId = new Map(nodes.map((node) => [node.nodeId, node]));
    const byBackendId = new Map(
      nodes
        .filter((node) => node.backendDOMNodeId)
        .map((node) => [node.backendDOMNodeId, node]),
    );
    const compact = (value) => String(value ?? "").replace(/\s+/gu, " ").trim();
    const accessibleText = (root) => {
      if (!root) return null;
      const directName = !root.ignored ? compact(root.name?.value) : "";
      if (directName) return directName;
      const staticText = [];
      const visit = (node) => {
        if (!node) return;
        if (!node.ignored && node.role?.value === "StaticText" && node.name?.value) {
          staticText.push(node.name.value);
        }
        for (const childId of node.childIds ?? []) visit(byId.get(childId));
      };
      visit(root);
      return compact(staticText.join(""));
    };
    return backendNodeGroups.map((backendNodeIds) =>
      backendNodeIds.map((backendNodeId) => {
        if (!backendNodeId) return null;
        return accessibleText(byBackendId.get(backendNodeId));
      }),
    );
  };

  const accessibilityRolesForSelectors = async (selectors) => {
    const documentResult = await send("DOM.getDocument", { depth: 0, pierce: true });
    const documentNodeId = documentResult.result?.root?.nodeId;
    if (!documentNodeId) return selectors.map(() => []);
    const backendNodeGroups = [];
    for (const selector of selectors) {
      const queryResult = await send("DOM.querySelectorAll", {
        nodeId: documentNodeId,
        selector,
      });
      const backendNodeIds = [];
      for (const nodeId of queryResult.result?.nodeIds ?? []) {
        const described = await send("DOM.describeNode", { nodeId });
        backendNodeIds.push(described.result?.node?.backendNodeId ?? null);
      }
      backendNodeGroups.push(backendNodeIds);
    }
    const tree = await send("Accessibility.getFullAXTree", {});
    const byBackendId = new Map(
      (tree.result?.nodes ?? [])
        .filter((node) => node.backendDOMNodeId)
        .map((node) => [node.backendDOMNodeId, node]),
    );
    return backendNodeGroups.map((backendNodeIds) =>
      backendNodeIds.map((backendNodeId) =>
        backendNodeId ? byBackendId.get(backendNodeId)?.role?.value ?? null : null
      ),
    );
  };

  const conceptDiagramSnapshot = async () => {
    const state = await evaluate(CONCEPT_DIAGRAM_PROBE);
    if (!state) return state;
    const [listRoles, stepRoles] = await accessibilityRolesForSelectors([
      "[data-concept-diagram] > ol",
      "[data-concept-diagram] > ol > [data-concept-step]",
    ]);
    let stepOffset = 0;
    for (const [index, diagram] of state.diagrams.entries()) {
      diagram.listAxRole = listRoles[index] ?? null;
      diagram.stepAxRoles = stepRoles.slice(stepOffset, stepOffset + diagram.stepCount);
      stepOffset += diagram.stepCount;
    }
    return state;
  };

  const accessibilityTextForSelector = async (selector) => {
    const documentResult = await send("DOM.getDocument", { depth: 0, pierce: true });
    const documentNodeId = documentResult.result?.root?.nodeId;
    if (!documentNodeId) return null;
    const queryResult = await send("DOM.querySelector", { nodeId: documentNodeId, selector });
    const nodeId = queryResult.result?.nodeId;
    if (!nodeId) return null;
    const described = await send("DOM.describeNode", { nodeId });
    const backendNodeId = described.result?.node?.backendNodeId;
    if (!backendNodeId) return null;
    const tree = await send("Accessibility.getFullAXTree", {});
    const nodes = tree.result?.nodes ?? [];
    const identityNode = nodes.find((node) => node.backendDOMNodeId === backendNodeId);
    if (!identityNode) return null;
    const byId = new Map(nodes.map((node) => [node.nodeId, node]));
    const staticText = [];
    const visit = (node) => {
      if (!node) return;
      if (!node.ignored && node.role?.value === "StaticText" && node.name?.value) {
        staticText.push(node.name.value);
      }
      for (const childId of node.childIds ?? []) visit(byId.get(childId));
    };
    visit(identityNode);
    return staticText.join("").trim();
  };

  const detectionLimitationBriefSnapshot = async (mode) => {
    const state = await evaluate(detectionLimitationBriefSnapshotExpression(mode));
    if (!state) return state;
    const [readingStepTexts, eventRowTexts] = await accessibilityTextsForSelectors([
      "[data-detection-reading-key] [data-detection-reading-step]",
      "[data-detection-comparison] > *",
    ]);
    for (const [index, step] of state.readingSteps.entries()) {
      step.accessibleText = readingStepTexts[index] ?? null;
    }
    for (const [index, row] of state.eventRows.entries()) {
      row.accessibleText = eventRowTexts[index] ?? null;
    }
    return state;
  };

  const healthAssumptionLedgerSnapshot = async (mode) => {
    const state = await evaluate(healthAssumptionLedgerSnapshotExpression(mode));
    if (!state) return state;
    const [assumptionTexts, readingHeadingTexts, readingBodyTexts, inferenceTexts] =
      await accessibilityTextsForSelectors([
        "[data-health-assumption-ledger] > [data-health-assumption]",
        "[data-health-reading-band] > [data-health-reading] > h2",
        "[data-health-reading-band] > [data-health-reading] > p",
        "[data-health-inference-boundaries] > [data-health-inference]",
      ]);
    for (const [index, row] of state.assumptionRows.entries()) {
      row.accessibleText = assumptionTexts[index] ?? null;
    }
    for (const [index, row] of state.readingRows.entries()) {
      row.accessibleHeading = readingHeadingTexts[index] ?? null;
      row.accessibleBody = readingBodyTexts[index] ?? null;
    }
    for (const [index, row] of state.inferenceRows.entries()) {
      row.accessibleText = inferenceTexts[index] ?? null;
    }
    return state;
  };

  const forecastHorizonDecisionSnapshot = async (mode) => {
    const state = await evaluate(forecastHorizonDecisionSnapshotExpression(mode));
    if (!state) return state;
    const [decisionTexts, readingHeadingTexts, readingBodyTexts, baselineTexts] =
      await accessibilityTextsForSelectors([
        "[data-forecast-decision-sheet] > ol > [data-forecast-decision] > a",
        "[data-forecast-reading-band] > [data-forecast-reading] > h2",
        "[data-forecast-reading-band] > [data-forecast-reading] > p",
        "[data-forecast-baseline-band] > [data-forecast-baseline]",
      ]);
    for (const [index, row] of state.decisionRows.entries()) {
      row.accessibleText = decisionTexts[index] ?? null;
    }
    for (const [index, row] of state.readingRows.entries()) {
      row.accessibleHeading = readingHeadingTexts[index] ?? null;
      row.accessibleBody = readingBodyTexts[index] ?? null;
    }
    for (const [index, row] of state.baselineRows.entries()) {
      row.accessibleText = baselineTexts[index] ?? null;
    }
    return state;
  };

  const methodsCaseIndexSnapshot = async (mode) => {
    const state = await evaluate(methodsCaseIndexSnapshotExpression(mode));
    if (!state) return state;
    const [indexNames, linkTexts, destinationHeadings] = await accessibilityTextsForSelectors([
      "[data-method-case-index]",
      "[data-method-case-link]",
      "[data-method-case] > h2 > span:last-child",
    ]);
    state.indexAccessibleName = indexNames[0] ?? null;
    for (const [index, row] of state.links.entries()) {
      row.accessibleText = linkTexts[index] ?? null;
    }
    for (const [index, row] of state.destinations.entries()) {
      row.accessibleHeading = destinationHeadings[index] ?? null;
    }
    return state;
  };

  const dataProvenanceRegisterSnapshot = async (mode) => {
    const state = await evaluate(dataProvenanceRegisterSnapshotExpression(mode));
    if (!state) return state;
    const [useTexts, downloadTexts] = await accessibilityTextsForSelectors([
      "[data-data-layer-use]",
      ".table-wrap tbody td:nth-child(3) a[download], .table-wrap tbody td:nth-child(4) > :is(a[download], [data-pages-unavailable])",
    ]);
    for (const [index, row] of state.layers.entries()) {
      row.accessibleUse = useTexts[index] ?? null;
    }
    for (const [index, row] of state.downloadRows.entries()) {
      row.downloadAccessibleTexts = downloadTexts.slice(index * 2, index * 2 + 2);
    }
    return state;
  };

  const explorerGuidedWorkspaceSnapshot = async (mode) => {
    const state = await evaluate(explorerGuidedWorkspaceSnapshotExpression(mode));
    if (!state) return state;
    const [stepTexts, runTexts] = await accessibilityTextsForSelectors([
      "[data-explorer-step]",
      "#run",
    ]);
    for (const [index, step] of state.steps.entries()) {
      step.accessibleText = stepTexts[index] ?? null;
    }
    state.run.accessibleText = runTexts[0] ?? null;
    return state;
  };

  const detectionBrowserMutationFailures = async (origin) => {
    const failures = [];
    await send("Emulation.setDeviceMetricsOverride", {
      width: 1280,
      height: 720,
      deviceScaleFactor: 1,
      mobile: false,
    });
    await send("Emulation.setEmulatedMedia", {
      media: "",
      features: [{ name: "prefers-color-scheme", value: "light" }],
    });
    const mutations = [
      {
        name: "self horizontal overflow",
        expected: "reading key clips its own content",
        script: `(() => {
          const element = document.querySelector("[data-detection-reading-key]");
          element.style.setProperty("width", "40px", "important");
          element.style.setProperty("overflow-x", "hidden", "important");
          element.style.setProperty("white-space", "nowrap", "important");
        })()`,
      },
      {
        name: "self vertical overflow",
        expected: "comparison clips its own content",
        script: `(() => {
          const element = document.querySelector("[data-detection-comparison]");
          element.style.setProperty("height", "10px", "important");
          element.style.setProperty("overflow-y", "hidden", "important");
        })()`,
      },
      {
        name: "ancestor overflow clipping",
        expected: "boundary is clipped by an ancestor",
        script: `(() => {
          const element = document.querySelector("[data-detection-inference-boundary]");
          const wrapper = document.createElement("div");
          wrapper.style.setProperty("height", "1px", "important");
          wrapper.style.setProperty("overflow", "hidden", "important");
          element.before(wrapper);
          wrapper.append(element);
        })()`,
      },
      {
        name: "zoom ancestor overflow clipping",
        mode: "zoom",
        expected: "zoom comparison is clipped by an ancestor",
        script: `(() => {
          const root = document.documentElement;
          const base = Number.parseFloat(getComputedStyle(root).fontSize);
          root.style.setProperty("font-size", String(base * 2) + "px", "important");
          const element = document.querySelector("[data-detection-comparison]");
          const wrapper = document.createElement("div");
          wrapper.style.setProperty("height", "1px", "important");
          wrapper.style.setProperty("overflow", "hidden", "important");
          element.before(wrapper);
          wrapper.append(element);
        })()`,
      },
      {
        name: "CSS clip",
        expected: "reading key uses CSS clip",
        script: `(() => {
          const element = document.querySelector("[data-detection-reading-key]");
          element.style.setProperty("position", "absolute", "important");
          element.style.setProperty("clip", "rect(0px, 1px, 1px, 0px)", "important");
        })()`,
      },
      {
        name: "CSS clip-path",
        expected: "comparison uses CSS clip-path",
        script: `document.querySelector("[data-detection-comparison]")
          .style.setProperty("clip-path", "inset(50%)", "important")`,
      },
      {
        name: "wrong reading-step ARIA label with unchanged visible copy",
        expected: "reading step 1 text changed",
        script: `document.querySelector('[data-detection-reading-step="placebo"]')
          .setAttribute("aria-label", "先看裝飾圖示。")`,
      },
      {
        name: "aria-hidden event copy with unchanged visible copy",
        expected: "event row 1 accessible text changed",
        script: `document.querySelectorAll("[data-detection-event]:first-child > *")
          .forEach((element) => element.setAttribute("aria-hidden", "true"))`,
      },
      {
        name: "reading-step CSS order",
        expected: "reading step 1 uses CSS order",
        script: `document.querySelector('[data-detection-reading-step="placebo"]')
          .style.setProperty("order", "2", "important")`,
      },
      {
        name: "wrong rendered event kind",
        expected: "event row 1 kind changed",
        script: `document.querySelector("[data-detection-event]")
          .setAttribute("data-detection-kind", "trend_break")`,
      },
      {
        name: "unhooked extra semantic row",
        expected: "semantic row inventory is 4",
        script: `(() => {
          const row = document.createElement("div");
          row.innerHTML = "<dt>額外說明</dt><dd>實際通過 9 站。</dd>";
          document.querySelector("[data-detection-comparison]").append(row);
        })()`,
      },
      {
        name: "malformed direct description pair",
        expected: "event row 1 description structure changed",
        script: `(() => {
          const row = document.querySelector("[data-detection-event]");
          const dd = row.querySelector(":scope > dd");
          const replacement = document.createElement("p");
          replacement.textContent = dd.textContent;
          dd.replaceWith(replacement);
        })()`,
      },
      {
        name: "conflicting event copy",
        expected: "event row 1 visible text changed",
        script: `document.querySelector("[data-detection-event] > dd")
          .append(document.createTextNode("實際通過 9 站。"))`,
      },
      {
        name: "zero-opacity event row",
        expected: "event row 1 opacity is zero",
        script: `document.querySelector("[data-detection-event]")
          .style.setProperty("opacity", "0", "important")`,
      },
      {
        name: "hidden event row",
        expected: "event row 1 is hidden",
        script: `document.querySelector("[data-detection-event]").hidden = true`,
      },
      {
        name: "off-canvas event row",
        expected: "event row 1 is horizontally off-canvas",
        script: `document.querySelector("[data-detection-event]")
          .style.setProperty("transform", "translateX(200vw)", "important")`,
      },
      {
        name: "clipped event row",
        expected: "event row 1 is clipped by an ancestor",
        script: `(() => {
          const row = document.querySelector("[data-detection-event]");
          const comparison = document.querySelector("[data-detection-comparison]");
          comparison.style.setProperty("height", String(row.getBoundingClientRect().height / 2) + "px", "important");
          comparison.style.setProperty("overflow", "hidden", "important");
        })()`,
      },
      {
        name: "opening pair after method evidence",
        expected: "opening order changed",
        script: `(() => {
          const comparison = document.querySelector("[data-detection-comparison]");
          const boundary = document.querySelector("[data-detection-inference-boundary]");
          const method = document.querySelector("[data-detection-method-evidence]");
          method.after(comparison, boundary);
        })()`,
      },
      ...[
        "每個事件的實際通過數都低於各自純靠機率的預期。",
        "非偵測不是「事件沒有發生」或「介入無效」的證明。",
      ].map((claim) => ({
        name: `boundary-local claim ${claim}`,
        expected: "boundary is missing required claim",
        script: `(() => {
          const boundary = document.querySelector("[data-detection-inference-boundary]");
          boundary.innerHTML = boundary.innerHTML.replace(${JSON.stringify(claim)}, "");
          const elsewhere = document.createElement("p");
          elsewhere.textContent = ${JSON.stringify(claim)};
          document.querySelector("[data-detection-method-evidence]").after(elsewhere);
        })()`,
      })),
      {
        name: "legacy below-chance conclusion outside boundary",
        expected: "boundary-local inference is duplicated",
        script: `(() => {
          const duplicate = document.createElement("p");
          duplicate.textContent = "三個事件的實際通過數都低於機率預期。";
          document.querySelector("[data-detection-method-evidence]").after(duplicate);
        })()`,
      },
      ...[
        ["reading key", "[data-detection-reading-key]"],
        ["comparison", "[data-detection-comparison]"],
        ["boundary", "[data-detection-inference-boundary]"],
      ].flatMap(([label, selector]) => [
        {
          name: `open disclosure around ${label}`,
          expected: `${label} is user-collapsible`,
          script: `(() => {
            const element = document.querySelector(${JSON.stringify(selector)});
            const details = document.createElement("details");
            details.open = true;
            element.before(details);
            details.append(element);
          })()`,
        },
        {
          name: `closed disclosure around ${label}`,
          expected: `${label} is user-collapsible`,
          script: `(() => {
            const element = document.querySelector(${JSON.stringify(selector)});
            const details = document.createElement("details");
            element.before(details);
            details.append(element);
          })()`,
        },
      ]),
    ];
    const loadDetection = async (label) => {
      await send("Page.navigate", { url: `${origin}/detection/` });
      return settled(evaluate, 8000, `/detection/ ${label}`);
    };
    if (!(await loadDetection("browser mutation control"))) {
      return ["browser mutation control never finished styling"];
    }
    const control = await detectionLimitationBriefSnapshot("normal");
    const controlProblems = detectionLimitationBriefProblems(
      control,
      EXPECTED_DETECTION_EVENTS,
      control?.viewport,
    );
    if (controlProblems.length) {
      failures.push(`clean production snapshot rejected: ${controlProblems.join(", ")}`);
    }
    for (const mutation of mutations) {
      if (!(await loadDetection(`browser mutation ${mutation.name}`))) {
        failures.push(`${mutation.name} page never finished styling`);
        continue;
      }
      await evaluate(mutation.script);
      await settlePaint(evaluate);
      const state = await detectionLimitationBriefSnapshot(mutation.mode ?? "normal");
      const problems = detectionLimitationBriefProblems(
        state,
        EXPECTED_DETECTION_EVENTS,
        state?.viewport,
      );
      if (!problems.some((problem) => problem.includes(mutation.expected))) {
        failures.push(
          `${mutation.name} did not reach ${JSON.stringify(mutation.expected)} ` +
            `(received ${problems.join(", ") || "no problems"})`,
        );
      }
    }
    return failures;
  };

  const healthBrowserMutationFailures = async (origin) => {
    const failures = [];
    await send("Emulation.setDeviceMetricsOverride", {
      width: 1280,
      height: 720,
      deviceScaleFactor: 1,
      mobile: false,
    });
    await send("Emulation.setEmulatedMedia", {
      media: "",
      features: [{ name: "prefers-color-scheme", value: "light" }],
    });
    const mutations = [
      {
        name: "hidden ledger",
        expected: "ledger is hidden",
        script: `document.querySelector("[data-health-assumption-ledger]").hidden = true`,
      },
      {
        name: "aria-hidden reading band",
        expected: "reading band is aria-hidden",
        script: `document.querySelector("[data-health-reading-band]")
          .setAttribute("aria-hidden", "true")`,
      },
      {
        name: "inert boundary",
        expected: "boundary is excluded from accessibility",
        script: `document.querySelector("[data-health-inference-boundaries]")
          .setAttribute("inert", "")`,
      },
      {
        name: "zero-opacity boundary",
        expected: "boundary opacity is zero",
        script: `document.querySelector("[data-health-inference-boundaries]")
          .style.setProperty("opacity", "0", "important")`,
      },
      {
        name: "ledger self overflow",
        expected: "ledger clips its own content",
        script: `(() => {
          const element = document.querySelector("[data-health-assumption-ledger]");
          element.style.setProperty("width", "40px", "important");
          element.style.setProperty("overflow-x", "hidden", "important");
          element.style.setProperty("white-space", "nowrap", "important");
        })()`,
      },
      {
        name: "reading-band ancestor clipping",
        expected: "reading band is clipped by an ancestor",
        script: `(() => {
          const element = document.querySelector("[data-health-reading-band]");
          const wrapper = document.createElement("div");
          wrapper.style.setProperty("height", "1px", "important");
          wrapper.style.setProperty("overflow", "hidden", "important");
          element.before(wrapper);
          wrapper.append(element);
        })()`,
      },
      {
        name: "ledger CSS clip",
        expected: "ledger uses CSS clip",
        script: `(() => {
          const element = document.querySelector("[data-health-assumption-ledger]");
          element.style.setProperty("position", "absolute", "important");
          element.style.setProperty("clip", "rect(0px, 1px, 1px, 0px)", "important");
        })()`,
      },
      {
        name: "boundary CSS clip-path",
        expected: "boundary uses CSS clip-path",
        script: `document.querySelector("[data-health-inference-boundaries]")
          .style.setProperty("clip-path", "inset(50%)", "important")`,
      },
      {
        name: "off-canvas boundary",
        expected: "boundary is horizontally off-canvas",
        script: `document.querySelector("[data-health-inference-boundaries]")
          .style.setProperty("transform", "translateX(200vw)", "important")`,
      },
      {
        name: "ledger CSS order",
        expected: "ledger uses CSS order",
        script: `document.querySelector("[data-health-assumption-ledger]")
          .style.setProperty("order", "2", "important")`,
      },
      {
        name: "extra unhooked assumption row",
        expected: "assumption row inventory changed",
        script: `(() => {
          const row = document.createElement("li");
          row.textContent = "未審閱的額外假設";
          document.querySelector("[data-health-assumption-ledger]").append(row);
        })()`,
      },
      {
        name: "wrong assumption key",
        expected: "assumption row 1 key changed",
        script: `document.querySelector("[data-health-assumption]")
          .setAttribute("data-health-assumption", "response")`,
      },
      {
        name: "wrong assumption visible text",
        expected: "assumption row 1 visible text changed",
        script: `document.querySelector("[data-health-assumption] p").textContent = "只看一個數字。"`,
      },
      {
        name: "wrong assumption AX text",
        expected: "assumption row 1 accessible text changed",
        script: `document.querySelector("[data-health-assumption]")
          .setAttribute("aria-label", "另一個假設")`,
      },
      {
        name: "reordered reading rows",
        expected: "reading row 1 key changed",
        script: `(() => {
          const region = document.querySelector("[data-health-reading-band]");
          region.prepend(region.lastElementChild);
        })()`,
      },
      {
        name: "wrong reading heading AX text",
        expected: "reading row 1 accessible heading changed",
        script: `document.querySelector("[data-health-reading] h2")
          .setAttribute("aria-label", "假設穩健")`,
      },
      {
        name: "missing reading body",
        expected: "reading row 1 body changed",
        script: `document.querySelector("[data-health-reading] p").remove()`,
      },
      {
        name: "wrong reading body AX text",
        expected: "reading row 1 accessible body changed",
        script: `document.querySelector("[data-health-reading] p")
          .setAttribute("aria-label", "另一段解讀")`,
      },
      {
        name: "assumption rows visually reversed",
        expected: "assumption row visual order changed",
        script: `(() => {
          const ledger = document.querySelector("[data-health-assumption-ledger]");
          ledger.style.setProperty("display", "flex", "important");
          ledger.style.setProperty("flex-direction", "row-reverse", "important");
          for (const row of ledger.children) row.style.setProperty("flex", "1", "important");
        })()`,
      },
      {
        name: "wrong inference visible text",
        expected: "inference row 1 visible text changed",
        script: `document.querySelector("[data-health-inference] p").textContent = "死亡人數是零。"`,
      },
      {
        name: "wrong inference AX text",
        expected: "inference row 1 accessible text changed",
        script: `document.querySelector("[data-health-inference]")
          .setAttribute("aria-label", "死亡人數是零")`,
      },
      {
        name: "stale Figure 7.2 title",
        expected: "Figure 7.2 title changed",
        script: `document.querySelector("#evidence-7-2-title").textContent =
          "不同暴露反應函數會把結果推動多少？"`,
      },
      {
        name: "ledger moved after primary evidence",
        expected: "opening order changed",
        script: `(() => {
          const ledger = document.querySelector("[data-health-assumption-ledger]");
          document.querySelector("[data-primary-evidence]").after(ledger);
        })()`,
      },
      ...[
        ["ledger", "[data-health-assumption-ledger]"],
        ["reading band", "[data-health-reading-band]"],
        ["boundary", "[data-health-inference-boundaries]"],
      ].flatMap(([label, selector]) => [
        {
          name: `open disclosure around ${label}`,
          expected: `${label} is user-collapsible`,
          script: `(() => {
            const element = document.querySelector(${JSON.stringify(selector)});
            const details = document.createElement("details");
            details.open = true;
            element.before(details);
            details.append(element);
          })()`,
        },
        {
          name: `closed disclosure around ${label}`,
          expected: `${label} is user-collapsible`,
          script: `(() => {
            const element = document.querySelector(${JSON.stringify(selector)});
            const details = document.createElement("details");
            element.before(details);
            details.append(element);
          })()`,
        },
      ]),
    ];
    const loadHealth = async (label) => {
      await send("Page.navigate", { url: `${origin}/health/` });
      return settled(evaluate, 8000, `/health/ ${label}`);
    };
    if (!(await loadHealth("browser mutation control"))) {
      return ["browser mutation control never finished styling"];
    }
    const control = await healthAssumptionLedgerSnapshot("normal");
    const controlProblems = healthAssumptionLedgerProblems(
      control,
      EXPECTED_HEALTH_EVIDENCE,
      control?.viewport,
    );
    if (controlProblems.length) {
      failures.push(`clean production snapshot rejected: ${controlProblems.join(", ")}`);
    }
    for (const mutation of mutations) {
      if (!(await loadHealth(`browser mutation ${mutation.name}`))) {
        failures.push(`${mutation.name} page never finished styling`);
        continue;
      }
      await evaluate(mutation.script);
      await settlePaint(evaluate);
      const state = await healthAssumptionLedgerSnapshot(mutation.mode ?? "normal");
      const problems = healthAssumptionLedgerProblems(
        state,
        EXPECTED_HEALTH_EVIDENCE,
        state?.viewport,
      );
      if (!problems.some((problem) => problem.includes(mutation.expected))) {
        failures.push(
          `${mutation.name} did not reach ${JSON.stringify(mutation.expected)} ` +
            `(received ${problems.join(", ") || "no problems"})`,
        );
      }
    }
    return failures;
  };

  const forecastBrowserMutationFailures = async (origin) => {
    const failures = [];
    await send("Emulation.setDeviceMetricsOverride", {
      width: 1280,
      height: 720,
      deviceScaleFactor: 1,
      mobile: false,
    });
    await send("Emulation.setEmulatedMedia", {
      media: "",
      features: [{ name: "prefers-color-scheme", value: "light" }],
    });
    const mutations = [
      {
        name: "hidden decision sheet",
        expected: "decision sheet is hidden",
        script: `document.querySelector("[data-forecast-decision-sheet]").hidden = true`,
      },
      {
        name: "aria-hidden reading band",
        expected: "reading band is aria-hidden",
        script: `document.querySelector("[data-forecast-reading-band]")
          .setAttribute("aria-hidden", "true")`,
      },
      {
        name: "zero-opacity baseline band",
        expected: "baseline band opacity is zero",
        script: `document.querySelector("[data-forecast-baseline-band]")
          .style.setProperty("opacity", "0", "important")`,
      },
      {
        name: "decision sheet self overflow",
        expected: "decision sheet clips its own content",
        script: `(() => {
          const element = document.querySelector("[data-forecast-decision-sheet]");
          element.style.setProperty("width", "40px", "important");
          element.style.setProperty("overflow-x", "hidden", "important");
          element.style.setProperty("white-space", "nowrap", "important");
        })()`,
      },
      {
        name: "reading ancestor clipping",
        expected: "reading band is clipped by an ancestor",
        script: `(() => {
          const element = document.querySelector("[data-forecast-reading-band]");
          const wrapper = document.createElement("div");
          wrapper.style.setProperty("height", "1px", "important");
          wrapper.style.setProperty("overflow", "hidden", "important");
          element.before(wrapper);
          wrapper.append(element);
        })()`,
      },
      {
        name: "decision CSS clip",
        expected: "decision sheet uses CSS clip",
        script: `(() => {
          const element = document.querySelector("[data-forecast-decision-sheet]");
          element.style.setProperty("position", "absolute", "important");
          element.style.setProperty("clip", "rect(0px, 1px, 1px, 0px)", "important");
        })()`,
      },
      {
        name: "baseline CSS clip-path",
        expected: "baseline band uses CSS clip-path",
        script: `document.querySelector("[data-forecast-baseline-band]")
          .style.setProperty("clip-path", "inset(50%)", "important")`,
      },
      {
        name: "off-canvas baseline",
        expected: "baseline band is horizontally off-canvas",
        script: `document.querySelector("[data-forecast-baseline-band]")
          .style.setProperty("transform", "translateX(200vw)", "important")`,
      },
      {
        name: "decision row key",
        expected: "decision row 1 key changed",
        script: `document.querySelector("[data-forecast-decision]")
          .setAttribute("data-forecast-decision", "skill")`,
      },
      {
        name: "decision visible body",
        expected: "decision row 1 body changed",
        script: `document.querySelector("[data-forecast-decision] p").textContent = "不同說明。"`,
      },
      {
        name: "decision AX text",
        expected: "decision row 1 accessible text changed",
        script: `document.querySelector("[data-forecast-decision] a")
          .setAttribute("aria-label", "另一個決策")`,
      },
      {
        name: "decision link",
        expected: "decision row 1 link changed",
        script: `document.querySelector("[data-forecast-decision] a")
          .setAttribute("href", "#forecast-cost")`,
      },
      {
        name: "extra decision row",
        expected: "decision row inventory changed",
        script: `document.querySelector("[data-forecast-decision-sheet] ol")
          .append(document.querySelector("[data-forecast-decision]").cloneNode(true))`,
      },
      {
        name: "decision visual reverse",
        expected: "decision row visual order changed",
        script: `(() => {
          const list = document.querySelector("[data-forecast-decision-sheet] ol");
          list.style.setProperty("display", "flex", "important");
          list.style.setProperty("flex-direction", "row-reverse", "important");
          for (const row of list.children) row.style.setProperty("flex", "1", "important");
        })()`,
      },
      {
        name: "reading order",
        expected: "reading row 1 key changed",
        script: `(() => {
          const band = document.querySelector("[data-forecast-reading-band]");
          band.prepend(band.lastElementChild);
        })()`,
      },
      {
        name: "reading body",
        expected: "reading row 1 body changed",
        script: `document.querySelector("[data-forecast-reading] p").textContent = "不同解讀。"`,
      },
      {
        name: "reading AX heading",
        expected: "reading row 1 accessible heading changed",
        script: `document.querySelector("[data-forecast-reading] h2")
          .setAttribute("aria-label", "不同標題")`,
      },
      {
        name: "baseline text",
        expected: "baseline row 1 what changed",
        script: `document.querySelector("[data-forecast-baseline] p").textContent = "不同基準。"`,
      },
      {
        name: "baseline AX",
        expected: "baseline row 1 accessible text changed",
        script: `document.querySelector("[data-forecast-baseline]")
          .setAttribute("aria-label", "另一條基準")`,
      },
      {
        name: "sheet after Figure 6.2",
        expected: "evidence order changed",
        script: `(() => {
          const sheet = document.querySelector("[data-forecast-decision-sheet]");
          document.querySelector("#evidence-6-2-title").closest(".evidence-figure").after(sheet);
        })()`,
      },
      {
        name: "duplicate decision sentence",
        expected: "decision sentence locality changed",
        script: `(() => {
          const duplicate = document.createElement("p");
          duplicate.textContent = ${JSON.stringify(FORECAST_DECISION_ROWS[0][2])};
          document.querySelector("main").append(duplicate);
        })()`,
      },
      ...[
        ["decision sheet", "[data-forecast-decision-sheet]"],
        ["reading band", "[data-forecast-reading-band]"],
        ["baseline band", "[data-forecast-baseline-band]"],
      ].flatMap(([label, selector]) => [
        {
          name: `open disclosure around ${label}`,
          expected: `${label} is user-collapsible`,
          script: `(() => {
            const element = document.querySelector(${JSON.stringify(selector)});
            const details = document.createElement("details");
            details.open = true;
            element.before(details);
            details.append(element);
          })()`,
        },
        {
          name: `closed disclosure around ${label}`,
          expected: `${label} is user-collapsible`,
          script: `(() => {
            const element = document.querySelector(${JSON.stringify(selector)});
            const details = document.createElement("details");
            element.before(details);
            details.append(element);
          })()`,
        },
      ]),
    ];
    const loadForecast = async (label) => {
      await send("Page.navigate", { url: `${origin}/forecast/` });
      return settled(evaluate, 8000, `/forecast/ ${label}`);
    };
    if (!(await loadForecast("browser mutation control"))) {
      return ["browser mutation control never finished styling"];
    }
    const control = await forecastHorizonDecisionSnapshot("normal");
    const controlProblems = forecastHorizonDecisionProblems(
      control,
      EXPECTED_FORECAST_EVIDENCE,
      control?.viewport,
    );
    if (controlProblems.length) {
      failures.push(`clean production snapshot rejected: ${controlProblems.join(", ")}`);
    }
    for (const mutation of mutations) {
      if (!(await loadForecast(`browser mutation ${mutation.name}`))) {
        failures.push(`${mutation.name} page never finished styling`);
        continue;
      }
      await evaluate(mutation.script);
      await settlePaint(evaluate);
      const state = await forecastHorizonDecisionSnapshot(mutation.mode ?? "normal");
      const problems = forecastHorizonDecisionProblems(
        state,
        EXPECTED_FORECAST_EVIDENCE,
        state?.viewport,
      );
      if (!problems.some((problem) => problem.includes(mutation.expected))) {
        failures.push(
          `${mutation.name} did not reach ${JSON.stringify(mutation.expected)} ` +
            `(received ${problems.join(", ") || "no problems"})`,
        );
      }
    }
    return failures;
  };

  const methodsBrowserMutationFailures = async (origin) => {
    const failures = [];
    await send("Emulation.setDeviceMetricsOverride", {
      width: 1280,
      height: 720,
      deviceScaleFactor: 1,
      mobile: false,
    });
    await send("Emulation.setEmulatedMedia", {
      media: "",
      features: [{ name: "prefers-color-scheme", value: "light" }],
    });
    const mutations = [
      {
        name: "hidden index",
        expected: "case index is hidden",
        script: `document.querySelector("[data-method-case-index]").hidden = true`,
      },
      {
        name: "wrong index accessible name",
        expected: "case index accessible name changed",
        script: `(() => {
          const label = document.createElement("span");
          label.id = "method-case-index-alternate-label";
          label.textContent = "另一個索引";
          const index = document.querySelector("[data-method-case-index]");
          index.prepend(label);
          index.setAttribute("aria-labelledby", label.id);
        })()`,
      },
      {
        name: "zero-opacity link",
        expected: "case link 1 opacity is zero",
        script: `document.querySelector("[data-method-case-link]").style.opacity = "0"`,
      },
      {
        name: "off-canvas case link",
        expected: "case link 1 is horizontally off-canvas",
        script: `(() => {
          const link = document.querySelector("[data-method-case-link]");
          link.style.setProperty("position", "fixed", "important");
          link.style.setProperty("left", "1400px", "important");
          link.style.setProperty("width", "280px", "important");
        })()`,
      },
      {
        name: "clipped case link",
        expected: "case link 1 is clipped by an ancestor",
        script: `(() => {
          const link = document.querySelector("[data-method-case-link]");
          const wrapper = document.createElement("div");
          wrapper.style.cssText = "height:8px;overflow:hidden";
          link.before(wrapper);
          wrapper.append(link);
        })()`,
      },
      {
        name: "link accessible text",
        expected: "case link 1 accessible text changed",
        script: `document.querySelector("[data-method-case-link]").setAttribute("aria-label", "另一個案例")`,
      },
      {
        name: "link target height",
        expected: "case link 1 target is shorter than 44px",
        script: `(() => {
          const link = document.querySelector("[data-method-case-link]");
          link.style.setProperty("min-height", "0", "important");
          link.style.setProperty("height", "20px", "important");
          link.style.setProperty("padding", "0", "important");
          link.style.setProperty("overflow", "hidden", "important");
        })()`,
      },
      {
        name: "link destination",
        expected: "case link 1 href changed",
        script: `document.querySelector("[data-method-case-link]").setAttribute("href", "#method-case-02")`,
      },
      {
        name: "extra link",
        expected: "case link inventory changed",
        script: `(() => {
          const link = document.querySelector("[data-method-case-link]");
          link.closest("ol").append(link.closest("li").cloneNode(true));
        })()`,
      },
      {
        name: "missing destination",
        expected: "case destination inventory changed",
        script: `document.querySelector("[data-method-case='07']").remove()`,
      },
      {
        name: "hidden case destination",
        expected: "case destination 1 is hidden",
        script: `document.querySelector("[data-method-case='01']").hidden = true`,
      },
      {
        name: "destination heading",
        expected: "case destination 1 heading changed",
        script: `document.querySelector("[data-method-case='01'] > h2 > span:last-child").textContent = "不同案例"`,
      },
      {
        name: "destination accessible heading",
        expected: "case destination 1 accessible heading changed",
        script: `document.querySelector("[data-method-case='01'] > h2 > span:last-child").setAttribute("aria-label", "不同案例")`,
      },
      {
        name: "index overflow",
        expected: "case index clips its own content",
        script: `(() => {
          const index = document.querySelector("[data-method-case-index]");
          index.style.setProperty("width", "40px", "important");
          index.style.setProperty("overflow-x", "hidden", "important");
          index.style.setProperty("white-space", "nowrap", "important");
        })()`,
      },
      {
        name: "ancestor clipping",
        expected: "case index is clipped by an ancestor",
        script: `(() => {
          const index = document.querySelector("[data-method-case-index]");
          const wrapper = document.createElement("div");
          wrapper.style.cssText = "height:20px;overflow:hidden";
          index.before(wrapper);
          wrapper.append(index);
        })()`,
      },
      {
        name: "CSS clip",
        expected: "case index uses CSS clip",
        script: `document.querySelector("[data-method-case-index]").style.clipPath = "inset(20px)"`,
      },
      {
        name: "link visual reorder",
        expected: "case link visual order changed",
        script: `(() => {
          const list = document.querySelector("[data-method-case-index] ol");
          list.style.display = "flex";
          list.style.flexDirection = "column-reverse";
        })()`,
      },
      {
        name: "destination source reorder",
        expected: "casebook source order changed",
        script: `(() => {
          const index = document.querySelector("[data-method-case-index]");
          const first = document.querySelector("[data-method-case='01']");
          first.after(index);
        })()`,
      },
      ...["open", "closed"].map((state) => ({
        name: `${state} disclosure around index`,
        expected: "case index is user-collapsible",
        script: `(() => {
          const index = document.querySelector("[data-method-case-index]");
          const details = document.createElement("details");
          details.open = ${state === "open" ? "true" : "false"};
          index.before(details);
          details.append(index);
        })()`,
      })),
    ];
    for (const requiredName of [
      "off-canvas case link",
      "clipped case link",
      "hidden case destination",
    ]) {
      if (!mutations.some((mutation) => mutation.name === requiredName)) {
        failures.push(`methods browser mutation coverage is missing ${requiredName}`);
      }
    }
    if (failures.length) return failures;
    const loadMethods = async (label) => {
      await send("Page.navigate", { url: `${origin}/methods/` });
      return settled(evaluate, 8000, `/methods/ ${label}`);
    };
    if (!(await loadMethods("browser mutation control"))) {
      return ["browser mutation control never finished styling"];
    }
    const control = await methodsCaseIndexSnapshot("normal");
    console.log(
      "site-quality methods browser control " +
        JSON.stringify({
          viewport: control?.viewport ?? null,
          indexTop: control?.landmarks?.index?.top ?? null,
          indexBottom: control?.landmarks?.index?.bottom ?? null,
          primaryTop: control?.landmarks?.primary?.top ?? null,
          plotTop: control?.landmarks?.primaryPlot?.top ?? null,
          plotBottom: control?.landmarks?.primaryPlot?.bottom ?? null,
        }),
    );
    const controlProblems = methodsCaseIndexProblems(control, control?.viewport);
    if (controlProblems.length) {
      failures.push(`clean production snapshot rejected: ${controlProblems.join(", ")}`);
    }
    for (const mutation of mutations) {
      if (!(await loadMethods(`browser mutation ${mutation.name}`))) {
        failures.push(`${mutation.name} page never finished styling`);
        continue;
      }
      await evaluate(mutation.script);
      await settlePaint(evaluate);
      const state = await methodsCaseIndexSnapshot("normal");
      const problems = methodsCaseIndexProblems(state, state?.viewport);
      if (!problems.some((problem) => problem.includes(mutation.expected))) {
        failures.push(
          `${mutation.name} did not reach ${JSON.stringify(mutation.expected)} ` +
            `(received ${problems.join(", ") || "no problems"})`,
        );
      }
    }
    return failures;
  };

  const dataBrowserMutationFailures = async (origin) => {
    const failures = [];
    await send("Emulation.setDeviceMetricsOverride", {
      width: 1280,
      height: 720,
      deviceScaleFactor: 1,
      mobile: false,
    });
    await send("Emulation.setEmulatedMedia", {
      media: "",
      features: [{ name: "prefers-color-scheme", value: "light" }],
    });
    const mutations = [
      { name: "missing level", expected: "layer inventory changed", script: `document.querySelector('[data-data-layer="L2"]').remove()` },
      { name: "duplicate level", expected: "layer term hook inventory changed", script: `document.querySelector('[data-data-layer="L0"]').after(document.querySelector('[data-data-layer="L0"]').cloneNode(true))` },
      { name: "reordered level", expected: "layer 1 identity changed", script: `(() => { const a=document.querySelector('[data-data-layer="L0"]'); const b=document.querySelector('[data-data-layer="L1"]'); b.after(a); })()` },
      { name: "wrong term", expected: "layer 1 term changed", script: `document.querySelector('[data-data-layer="L0"] [data-data-layer-term]').textContent="另一層"` },
      { name: "wrong use", expected: "layer 1 use changed", script: `document.querySelector('[data-data-layer="L0"] [data-data-layer-use]').textContent="另一種用途"` },
      { name: "inaccessible use", expected: "layer 1 accessible use changed", script: `document.querySelector('[data-data-layer="L0"] [data-data-layer-use]').setAttribute("aria-label","另一種用途")` },
      { name: "hidden use", expected: "layer 1 use is hidden", script: `document.querySelector('[data-data-layer="L0"] [data-data-layer-use]').hidden=true` },
      { name: "off-canvas use", expected: "layer 1 use is horizontally off-canvas", script: `(() => { const e=document.querySelector('[data-data-layer="L0"] [data-data-layer-use]'); e.style.cssText="position:fixed;left:1400px;width:280px"; })()` },
      { name: "clipped use", expected: "layer 1 use is clipped by an ancestor", script: `(() => { const e=document.querySelector('[data-data-layer="L0"] [data-data-layer-use]'); const w=document.createElement("span"); w.style.cssText="display:block;height:5px;overflow:hidden"; e.before(w); w.append(e); })()` },
      { name: "description change", expected: "layer 1 description changed", script: `document.querySelector('[data-data-layer-description="L0"]').textContent="不同描述"` },
      { name: "extended contradictory description", expected: "layer 1 description changed", script: `document.querySelector('[data-data-layer-description="L0"]').append("但內容規格已改變")` },
      { name: "disclosure around register", expected: "register is user-collapsible", script: `(() => { const e=document.querySelector("[data-data-layer-register]"); const d=document.createElement("details"); d.open=true; e.before(d); d.append(e); })()` },
      { name: "table relocation", expected: "provenance source order changed", script: `document.querySelector("[data-data-layer-register]").before(document.querySelector(".table-wrap"))` },
      { name: "lost download", expected: "registered download count changed", script: `document.querySelector(".table-wrap a[download]").remove()` },
      { name: "changed download destination", expected: "download row 1 changed", script: `document.querySelector(".table-wrap a[download]").setAttribute("href", "/data/l0/wrong.json")` },
      { name: "reordered download rows", expected: "download row 1 changed", script: `(() => { const rows=document.querySelectorAll(".table-wrap tbody > tr"); rows[1].after(rows[0]); })()` },
      { name: "hidden download", expected: "download row 1 link 1 is hidden", script: `document.querySelector(".table-wrap a[download]").hidden=true` },
      { name: "added L2 download", expected: "L2 unexpectedly has 1 download", script: `(() => { const a=document.createElement("a"); a.download=""; a.href="#"; document.querySelector('[data-data-layer-description="L2"]').append(a); })()` },
      { name: "lost L2 boundary", expected: "L2 boundary count is 0", script: `(() => { [...document.querySelectorAll(".note")].find((e)=>e.innerText.includes("L2 不發布"))?.remove(); })()` },
    ];
    const requiredNames = [
      "missing level", "duplicate level", "reordered level", "wrong term", "wrong use",
      "inaccessible use", "hidden use", "off-canvas use", "clipped use", "description change",
      "extended contradictory description",
      "disclosure around register", "table relocation", "lost download", "added L2 download",
      "changed download destination", "reordered download rows", "hidden download",
      "lost L2 boundary",
    ];
    for (const name of requiredNames) {
      if (!mutations.some((mutation) => mutation.name === name)) {
        failures.push(`data browser mutation coverage is missing ${name}`);
      }
    }
    if (failures.length) return failures;
    const loadData = async (label) => {
      await send("Page.navigate", { url: `${origin}/data/` });
      return settled(evaluate, 8000, `/data/ ${label}`);
    };
    if (!(await loadData("browser mutation control"))) {
      return ["browser mutation control never finished styling"];
    }
    const control = await dataProvenanceRegisterSnapshot("normal");
    const controlProblems = dataProvenanceRegisterProblems(control, control?.viewport);
    if (controlProblems.length) {
      failures.push(`clean production snapshot rejected: ${controlProblems.join(", ")}`);
    }
    for (const mutation of mutations) {
      if (!(await loadData(`browser mutation ${mutation.name}`))) {
        failures.push(`${mutation.name} page never finished styling`);
        continue;
      }
      await evaluate(mutation.script);
      await settlePaint(evaluate);
      const state = await dataProvenanceRegisterSnapshot("normal");
      const problems = dataProvenanceRegisterProblems(state, state?.viewport);
      if (!problems.some((problem) => problem.includes(mutation.expected))) {
        failures.push(`${mutation.name} did not reach ${JSON.stringify(mutation.expected)} (received ${problems.join(", ") || "no problems"})`);
      }
    }
    return failures;
  };

  const explorerBrowserMutationFailures = async (origin) => {
    const failures = [];
    await send("Emulation.setDeviceMetricsOverride", {
      width: 1280,
      height: 720,
      deviceScaleFactor: 1,
      mobile: false,
    });
    await send("Emulation.setEmulatedMedia", {
      media: "",
      features: [{ name: "prefers-color-scheme", value: "light" }],
    });
    await send("Page.enable");
    await send("Network.enable");
    await send("Network.setCacheDisabled", { cacheDisabled: true });
    const requests = [];
    const stopNetwork = onEvent("Network.requestWillBeSent", (event) => {
      const url = event?.request?.url;
      if (typeof url === "string") requests.push(url);
    });
    const deferredRequest = (url) =>
      /(?:\/data\/l1\/|\bduckdb|\/duck\.|\.wasm(?:$|\?))/iu.test(url);
    const loadExplore = async (label) => {
      await send("Page.navigate", { url: `${origin}/explore/` });
      return settled(evaluate, 8000, `/explore/ ${label}`);
    };
    const checkSnapshot = async (label, mode = "normal") => {
      const state = await explorerGuidedWorkspaceSnapshot(mode);
      const problems = explorerGuidedWorkspaceProblems(state, { width: 1280, height: 720 });
      if (problems.length) {
        failures.push(
          `${label}: ${problems.join(", ")} ` +
            `(snapshot=${JSON.stringify({
              state: state?.state,
              run: state?.run,
              status: state?.status,
              result: state?.result,
              sql: state?.sql,
            })})`,
        );
      }
      return state;
    };
    const waitForOutcome = async (expected, label) => {
      let last = null;
      for (let waited = 0; waited <= 30000; waited += 100) {
        last = await evaluate(`(() => ({
          state: document.querySelector("[data-explorer-workspace]")?.dataset.explorerState ?? "",
          disabled: document.querySelector("#run")?.disabled ?? null,
          hasRows: Boolean(document.querySelector("[data-explorer-result] tbody > tr")),
          empty: Boolean(document.querySelector("[data-explorer-result] .no-rows")),
          failure: Boolean(document.querySelector("[data-explorer-result] .error")),
        }))()`);
        if (last?.state === expected) return explorerGuidedWorkspaceSnapshot("normal");
        if (!last?.disabled && (last?.hasRows || last?.empty || last?.failure)) {
          return explorerGuidedWorkspaceSnapshot("normal");
        }
        await sleep(100);
      }
      failures.push(`${label} did not reach ${expected}; last state ${JSON.stringify(last)}`);
      return explorerGuidedWorkspaceSnapshot("normal");
    };
    /* The page no longer carries a query box; `__twairSetQuery` is the seam
       chapter 9 exposes so these states stay reachable. See the note beside it. */
    const clickWithSql = (sql) => evaluate(`(() => {
      if (${JSON.stringify(sql)} !== null) window.__twairSetQuery(${JSON.stringify(sql)});
      const run = document.querySelector("#run");
      run.focus();
      run.click();
      return {
        state: document.querySelector("[data-explorer-workspace]")?.dataset.explorerState ?? "",
        disabled: run.disabled,
        status: document.querySelector("#status")?.innerText?.trim() ?? "",
        busy: document.querySelector("#status")?.dataset.busy === "true",
        result: document.querySelector("[data-explorer-result]")?.innerText?.trim() ?? "",
        focused: document.activeElement === document.querySelector("[data-explorer-result]"),
      };
    })()`);
    try {
      if (!(await loadExplore("production-state control"))) {
        return ["production-state control never finished styling"];
      }
      const initialRequestCount = requests.length;
      const initialDeferred = requests.slice(0, initialRequestCount).filter(deferredRequest);
      if (initialDeferred.length) {
        failures.push(`initial page eagerly requested ${initialDeferred.join(", ")}`);
      }
      const initial = await checkSnapshot("initial production snapshot");
      if (initial?.state !== "initial") failures.push("initial production state changed");
      /*
       * Nothing to export yet, so nothing offering to. A page that opens with a
       * download button has promised the reader an answer it does not have.
       */
      const idleExport = await evaluate(`(() => {
        const el = document.querySelector(".explorer-actions");
        return { present: Boolean(el), hidden: el ? el.hidden : null };
      })()`);
      if (idleExport?.present && idleExport.hidden !== true) {
        failures.push("explore export controls are visible before any query has run");
      }

      const noJsRequestIndex = requests.length;
      const noJs = await navigateWithoutPageScripts(
        send,
        waitForEvent,
        `${origin}/explore/`,
        async () => {
          await settlePaint(evaluate, "/explore/ no-JavaScript render wait");
          return explorerGuidedWorkspaceSnapshot("no-js");
        },
      );
      const noJsProblems = explorerGuidedWorkspaceProblems(noJs, { width: 1280, height: 720 });
      if (noJsProblems.length) failures.push(`no-JavaScript query: ${noJsProblems.join(", ")}`);
      const noJsDeferred = requests.slice(noJsRequestIndex).filter(deferredRequest);
      if (noJsDeferred.length) {
        failures.push(`no-JavaScript page requested ${noJsDeferred.join(", ")}`);
      }
      if (!(await loadExplore("post-no-JavaScript production-state control"))) {
        failures.push("post-no-JavaScript production-state control never finished styling");
      }
      const actionRequestIndex = requests.length;

      const firstLoading = await clickWithSql(null);
      if (
        firstLoading?.state !== "loading" || firstLoading?.disabled !== true ||
        firstLoading?.busy !== true || !firstLoading?.status || firstLoading?.result !== "" ||
        firstLoading?.focused
      ) {
        failures.push(`click did not synchronously enter loading: ${JSON.stringify(firstLoading)}`);
      }
      const success = await waitForOutcome("success", "default query");
      if (success?.state !== "success") {
        failures.push(`default query state is ${String(success?.state)}, expected success`);
      }
      const successProblems = explorerGuidedWorkspaceProblems(success, { width: 1280, height: 720 });
      if (successProblems.length) failures.push(`default query: ${successProblems.join(", ")}`);

      /*
       * The export controls, which only exist once there is something to export.
       *
       * They are built by script rather than shipped in the markup, so a reader
       * without JavaScript never meets a button that cannot work — which also
       * means nothing in the static HTML can be checked for them, and this is
       * the only place they can be seen at all.
       *
       * The CSV label is checked for the row count rather than for being
       * non-empty. A capped result reports the cap in the status line already;
       * a download button is where that omission would be easiest to make and
       * hardest to notice, because the file outlives the page that explained it.
       */
      const exportControls = await evaluate(`(() => {
        const el = document.querySelector(".explorer-actions");
        if (!el) return { present: false };
        const labels = [...el.querySelectorAll("button")].map((b) => b.textContent ?? "");
        return { present: true, hidden: el.hidden, labels };
      })()`);
      if (!exportControls?.present) {
        failures.push("explore export controls are missing after a successful query");
      } else if (exportControls.hidden) {
        failures.push("explore export controls stayed hidden after a successful query");
      } else {
        /* One control, not two. The share-a-query link went with the query
           box: a link carrying SQL nobody can read or edit is a link to
           nothing. The CSV button is the whole export surface now. */
        if (exportControls.labels.length !== 1) {
          failures.push(`explore export controls show ${exportControls.labels.length} buttons, expected 1`);
        }
        if (!exportControls.labels.some((label) => /CSV/u.test(label) && /\d/u.test(label))) {
          failures.push(`explore CSV button names no row count: ${JSON.stringify(exportControls.labels)}`);
        }
      }
      const deferredAfterClick = requests.slice(actionRequestIndex).filter(deferredRequest);
      if (
        !deferredAfterClick.some((url) => /\/_astro\/duckdb-browser\.[^/]+\.js(?:$|\?)/iu.test(url)) ||
        !deferredAfterClick.some((url) => /\.worker\.[^/]+\.js(?:$|\?)/iu.test(url))
      ) {
        failures.push(
          `default query did not request the DuckDB module and worker after action ` +
            `(requests=${JSON.stringify(requests.slice(actionRequestIndex))})`,
        );
      }
      if (!deferredAfterClick.some((url) => /\/data\/l1\//iu.test(url))) {
        failures.push("default query did not probe an L1 data file after action");
      }

      const staleFailureLoading = await clickWithSql("SELECT * FROM definitely_not_a_table;");
      if (staleFailureLoading?.state !== "loading" || staleFailureLoading?.result !== "") {
        failures.push(
          `stale-result setup did not clear and enter loading: ${JSON.stringify(staleFailureLoading)}`,
        );
      }
      const staleFailure = await waitForOutcome("failure", "stale-result setup query");
      if (staleFailure?.state !== "failure") {
        failures.push(
          `stale-result setup state is ${String(staleFailure?.state)}, expected failure`,
        );
      }
      const staleDefaultSql = await evaluate(`JSON.parse(
        document.querySelector("#explorer-examples")?.textContent ?? "[]"
      )[0] ?? null`);
      await send("Network.emulateNetworkConditions", {
        offline: false,
        latency: 1500,
        downloadThroughput: -1,
        uploadThroughput: -1,
      });
      try {
        const slowLoading = await clickWithSql(staleDefaultSql);
        if (slowLoading?.state !== "loading" || slowLoading?.result !== "") {
          failures.push(
            `slow retry did not clear and enter loading: ${JSON.stringify(slowLoading)}`,
          );
        }
        await send("Emulation.setDeviceMetricsOverride", {
          width: 900,
          height: 720,
          deviceScaleFactor: 1,
          mobile: false,
        });
        await sleep(250);
        const resizedLoading = await explorerGuidedWorkspaceSnapshot("normal");
        if (resizedLoading?.state !== "loading") {
          failures.push(
            `slow retry did not remain loading through the resize seam: ` +
              `${String(resizedLoading?.state)}`,
          );
        } else {
          const resizedLoadingProblems = explorerGuidedWorkspaceProblems(
            resizedLoading,
            { width: 900, height: 720 },
          );
          if (resizedLoadingProblems.length) {
            failures.push(
              `loading resize restored a prior answer: ${resizedLoadingProblems.join(", ")} ` +
                `(snapshot=${JSON.stringify({
                  state: resizedLoading.state,
                  status: resizedLoading.status,
                  result: resizedLoading.result,
                })})`,
            );
          }
        }
      } finally {
        await send("Network.emulateNetworkConditions", {
          offline: false,
          latency: 0,
          downloadThroughput: -1,
          uploadThroughput: -1,
        });
        await send("Emulation.setDeviceMetricsOverride", {
          width: 1280,
          height: 720,
          deviceScaleFactor: 1,
          mobile: false,
        });
      }
      const staleRetry = await waitForOutcome("success", "stale-result resize retry");
      if (staleRetry?.state !== "success") {
        failures.push(
          `stale-result resize retry state is ${String(staleRetry?.state)}, expected success`,
        );
      }

      const emptyLoading = await clickWithSql(
        'SELECT station_name FROM "PM2.5" WHERE 1 = 0;',
      );
      if (emptyLoading?.state !== "loading" || emptyLoading?.result !== "") {
        failures.push(`empty query did not clear and enter loading: ${JSON.stringify(emptyLoading)}`);
      }
      const empty = await waitForOutcome("empty", "zero-row query");
      if (empty?.state !== "empty") {
        failures.push(`zero-row query state is ${String(empty?.state)}, expected empty`);
      }
      const emptyProblems = explorerGuidedWorkspaceProblems(empty, { width: 1280, height: 720 });
      if (emptyProblems.length) failures.push(`zero-row query: ${emptyProblems.join(", ")}`);

      const failureLoading = await clickWithSql("SELECT * FROM definitely_not_a_table;");
      if (failureLoading?.state !== "loading" || failureLoading?.result !== "") {
        failures.push(`invalid query did not clear and enter loading: ${JSON.stringify(failureLoading)}`);
      }
      const failure = await waitForOutcome("failure", "invalid query");
      if (failure?.state !== "failure") {
        failures.push(`invalid query state is ${String(failure?.state)}, expected failure`);
      }
      const failureProblems = explorerGuidedWorkspaceProblems(failure, { width: 1280, height: 720 });
      if (failureProblems.length) {
        failures.push(
          `invalid query: ${failureProblems.join(", ")} ` +
            `(snapshot=${JSON.stringify({
              state: failure?.state,
              run: failure?.run,
              status: failure?.status,
              result: failure?.result,
            })})`,
        );
      }

      const defaultSql = await evaluate(`JSON.parse(
        document.querySelector("#explorer-examples")?.textContent ?? "[]"
      )[0] ?? null`);
      const retryLoading = await clickWithSql(defaultSql);
      if (retryLoading?.state !== "loading" || retryLoading?.result !== "") {
        failures.push(`retry did not clear and enter loading: ${JSON.stringify(retryLoading)}`);
      }
      const retry = await waitForOutcome("success", "retry query");
      if (retry?.state !== "success") {
        failures.push(`retry state is ${String(retry?.state)}, expected success`);
      }
      const retryProblems = explorerGuidedWorkspaceProblems(retry, { width: 1280, height: 720 });
      if (retryProblems.length) failures.push(`retry query: ${retryProblems.join(", ")}`);

      await send("Emulation.setEmulatedMedia", { media: "print" });
      if (!(await loadExplore("print contract"))) {
        failures.push("print page never finished styling");
      } else {
        await checkSnapshot("print query", "print");
      }
      await send("Emulation.setEmulatedMedia", { media: "", features: [] });
      if (!(await loadExplore("zoom contract"))) {
        failures.push("zoom page never finished styling");
      } else {
        await evaluate(`(() => {
          const base = parseFloat(getComputedStyle(document.documentElement).fontSize);
          document.documentElement.style.setProperty("font-size", String(base * 2) + "px", "important");
        })()`);
        await settlePaint(evaluate, "/explore/ 200% text render wait");
        await checkSnapshot("zoom query", "zoom");
      }

      const mutations = [
        { name: "hidden step", expected: "step 1 is hidden", script: `document.querySelector("[data-explorer-step]").hidden = true` },
        { name: "wrong step AX", expected: "step 1 accessible text changed", script: `document.querySelector("[data-explorer-step]").setAttribute("aria-label", "另一個步驟")` },
        { name: "step self overflow", expected: "step 1 clips its own content", script: `(() => { const e=document.querySelector("[data-explorer-step]"); e.style.cssText="width:40px;overflow:hidden;white-space:nowrap"; })()` },
        { name: "step ancestor clipping", expected: "step 1 is clipped by an ancestor", script: `(() => { const e=document.querySelector("[data-explorer-step]"); const w=document.createElement("div"); w.style.cssText="height:1px;overflow:hidden"; e.before(w); w.append(e); })()` },
        { name: "step CSS clip", expected: "step 1 uses CSS clip", script: `document.querySelector("[data-explorer-step]").style.cssText="position:absolute;clip:rect(0px,1px,1px,0px)"` },
        { name: "step CSS clip-path", expected: "step 1 uses CSS clip-path", script: `document.querySelector("[data-explorer-step]").style.clipPath="inset(50%)"` },
        { name: "off-canvas step", expected: "step 1 is horizontally off-canvas", script: `document.querySelector("[data-explorer-step]").style.transform="translateX(200vw)"` },
        { name: "step disclosure", expected: "step 1 is user-collapsible", script: `(() => { const e=document.querySelector("[data-explorer-step]"); const d=document.createElement("details"); d.open=true; e.before(d); d.append(e); })()` },
        { name: "reordered steps", expected: "step 1 key changed", script: `(() => { const p=document.querySelector("[data-explorer-path]"); p.prepend(p.lastElementChild); })()` },
        { name: "hidden run", expected: "run control is hidden", script: `document.querySelector("#run").hidden=true` },
        { name: "wrong run AX", expected: "run accessible text changed", script: `document.querySelector("#run").setAttribute("aria-label", "開始")` },
        { name: "result before tables", expected: "source order changed", script: `document.querySelector("[data-explorer-tables]").before(document.querySelector("[data-explorer-result]"))` },
        { name: "caveat before result", expected: "source order changed", script: `document.querySelector("[data-explorer-result]").before(document.querySelector("[data-explorer-caveat]"))` },
        { name: "duplicate result", expected: "results count is 2", script: `document.querySelector("[data-explorer-result]").after(document.querySelector("[data-explorer-result]").cloneNode(true))` },
        { name: "document overflow", expected: "document scrolls sideways", script: `document.body.style.width="200vw"` },
      ];
      for (const mutation of mutations) {
        if (!(await loadExplore(`browser mutation ${mutation.name}`))) {
          failures.push(`${mutation.name} page never finished styling`);
          continue;
        }
        await evaluate(mutation.script);
        await settlePaint(evaluate);
        const state = await explorerGuidedWorkspaceSnapshot("normal");
        const problems = explorerGuidedWorkspaceProblems(state, { width: 1280, height: 720 });
        if (!problems.some((problem) => problem.includes(mutation.expected))) {
          failures.push(
            `${mutation.name} did not reach ${JSON.stringify(mutation.expected)} ` +
              `(received ${problems.join(", ") || "no problems"})`,
          );
        }
      }
    } finally {
      stopNetwork();
      await send("Network.disable");
    }
    return failures;
  };

  const chapterOpeningSnapshot = async (chartRoute, openingKind = "evidence") => evaluate(`(() => {
    const rendered = (element) => {
      if (!element) return false;
      for (let node = element; node; node = node.parentElement) {
        const style = getComputedStyle(node);
        if (
          node.getAttribute("aria-hidden") === "true" ||
          style.display === "none" || style.visibility === "hidden" ||
          style.visibility === "collapse" || Number(style.opacity) === 0
        ) return false;
      }
      const rect = element.getBoundingClientRect();
      return rect.width > 0 && rect.height > 0;
    };
    const inViewport = (element) => {
      if (!rendered(element)) return false;
      const rect = element.getBoundingClientRect();
      return rect.right > 1 && rect.left < innerWidth - 1 &&
        rect.bottom > 1 && rect.top < innerHeight - 1;
    };
    const inspect = (element) => {
      if (!element) return null;
      const rect = element.getBoundingClientRect();
      return {
        visible: rendered(element),
        top: rect.top,
        bottom: rect.bottom,
      };
    };
    const visibleDataArea = (element) => {
      if (!element) return null;
      const rect = element.getBoundingClientRect();
      let top = Math.max(0, rect.top);
      let bottom = Math.min(innerHeight, rect.bottom);
      for (let ancestor = element.parentElement; ancestor; ancestor = ancestor.parentElement) {
        const style = getComputedStyle(ancestor);
        if (["auto", "clip", "hidden", "scroll"].includes(style.overflowY)) {
          const bounds = ancestor.getBoundingClientRect();
          top = Math.max(top, bounds.top);
          bottom = Math.min(bottom, bounds.bottom);
        }
      }
      return Math.max(0, bottom - top);
    };
    const intro = document.querySelector("main .chapter-intro");
    const directHeadings = intro
      ? [...intro.querySelectorAll(":scope > h1")].filter(rendered) : [];
    const directTheses = intro
      ? [...intro.querySelectorAll(":scope > .chapter-thesis")].filter(rendered) : [];
    const h1 = directHeadings[0] ?? null;
    const thesis = directTheses[0] ?? null;
    const primaryElements = [...document.querySelectorAll("main [data-primary-evidence]")];
    const primary = primaryElements[0] ?? null;
    const primaryPlots = [...document.querySelectorAll("main [data-primary-plot]")];
    const plotsInsidePrimary = primary
      ? primaryPlots.filter((plot) => primary.contains(plot)) : [];
    const primaryPlot = plotsInsidePrimary[0] ?? null;
    let smallestVisibleText = Infinity;
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const parent = node.parentElement;
      if (
        !node.nodeValue.trim() || !parent || parent.closest("script, style, template") ||
        !rendered(parent)
      ) continue;
      const size = parseFloat(getComputedStyle(parent).fontSize);
      if (Number.isFinite(size)) smallestVisibleText = Math.min(smallestVisibleText, size);
    }
    const rail = document.querySelector(".rail");
    const handle = document.querySelector(".handle");
    const railRect = rail?.getBoundingClientRect() ?? null;
    const handleRect = handle?.getBoundingClientRect() ?? null;
    return {
      state: {
        viewport: { width: innerWidth, height: innerHeight },
        smallestVisibleText: Number.isFinite(smallestVisibleText) ? smallestVisibleText : null,
        rail: railRect ? { visible: inViewport(rail), width: railRect.width } : null,
        handle: handleRect ? { visible: inViewport(handle), height: handleRect.height } : null,
        primary: inspect(primary),
        primaryPlot: primaryPlot
          ? { ...inspect(primaryPlot), dataAreaVisible: visibleDataArea(primaryPlot) }
          : null,
        chartRoute: ${JSON.stringify(chartRoute)},
        openingKind: ${JSON.stringify(openingKind)},
      },
      intro: {
        visible: rendered(intro),
        headingCount: directHeadings.length,
        thesisCount: directTheses.length,
        thesisText: thesis?.textContent.trim() ?? "",
        thesisAfterHeading: Boolean(
          h1 && thesis && (h1.compareDocumentPosition(thesis) & Node.DOCUMENT_POSITION_FOLLOWING)
        ),
      },
      primaryCount: primaryElements.length,
      primaryPlotCount: primaryPlots.length,
      primaryPlotsInside: plotsInsidePrimary.length,
    };
  })()`);

  const chapterEndingSnapshot = async (expected) => evaluate(`(() => {
    const navs = [...document.querySelectorAll("main .chapter-nav")];
    const nav = navs[0] ?? null;
    const panel = nav?.querySelector(".chapter-nav-panel") ?? null;
    const links = [...(nav?.querySelectorAll("a") ?? [])];
    const neighbourLinks = links.filter((link) =>
      link.matches('[data-dir="prev"], [data-dir="next"]')
    );
    const panelBox = panel?.getBoundingClientRect() ?? null;
    const navBox = nav?.getBoundingClientRect() ?? null;
    const insidePanel = (link) => {
      if (!panelBox) return false;
      const box = link.getBoundingClientRect();
      return box.left >= panelBox.left - 1 && box.right <= panelBox.right + 1 &&
        box.top >= panelBox.top - 1 && box.bottom <= panelBox.bottom + 1;
    };
    const arrowAtOutwardEdge = (link) => {
      const linkBox = link.getBoundingClientRect();
      const arrow = link.querySelector(".step-arrow");
      const arrowRange = arrow ? document.createRange() : null;
      if (arrowRange && arrow) arrowRange.selectNodeContents(arrow);
      const arrowBox = arrowRange?.getBoundingClientRect() ?? null;
      const copyBox = link.querySelector(".step-copy")?.getBoundingClientRect() ?? null;
      if (!arrowBox || !copyBox) return false;
      if (link.dataset.dir === "prev") {
        return arrowBox.right <= copyBox.left + 1 && arrowBox.left - linkBox.left <= 44;
      }
      return copyBox.right <= arrowBox.left + 1 && linkBox.right - arrowBox.right <= 44;
    };
    const normalText = (element) => element?.textContent?.replace(/\\s+/g, " ").trim() ?? "";
    return {
      ...${JSON.stringify(expected)},
      navCount: navs.length,
      panelCount: nav?.querySelectorAll(".chapter-nav-panel").length ?? 0,
      progressCount: nav?.querySelectorAll("[data-chapter-progress]").length ?? 0,
      progressText: normalText(nav?.querySelector("[data-chapter-progress]")),
      position: nav?.getAttribute("data-chapter-position") ?? null,
      indexLinks: nav?.querySelectorAll('a[data-dir="up"]').length ?? 0,
      indexLabel: normalText(nav?.querySelector('a[data-dir="up"]')),
      inertEndpoints: nav?.querySelectorAll(".is-end").length ?? 0,
      linkCount: links.length,
      previousLinks: nav?.querySelectorAll('a[data-dir="prev"]').length ?? 0,
      nextLinks: nav?.querySelectorAll('a[data-dir="next"]').length ?? 0,
      directionLabels: neighbourLinks.filter((link) =>
        normalText(link).includes(link.dataset.dir === "prev" ? "上一章" : "下一章")
      ).length,
      hiddenArrows: neighbourLinks.filter((link) =>
        link.querySelector('.step-arrow[aria-hidden="true"]')
      ).length,
      outwardArrows: neighbourLinks.filter(arrowAtOutwardEdge).length,
      linkHeights: links.map((link) => link.getBoundingClientRect().height),
      containedLinks: links.filter(insidePanel).length,
      clippedTitles: [...(nav?.querySelectorAll(".step-title") ?? [])].filter((title) =>
        title.scrollWidth - title.clientWidth > 1 || title.scrollHeight - title.clientHeight > 1
      ).length,
      horizontalOverflow: document.documentElement.scrollWidth -
        document.documentElement.clientWidth,
      navHeight: navBox?.height ?? null,
    };
  })()`);

  const readingMapSnapshot = async ({ targetIds, measureAnchors }) => {
    const state = await evaluate(`(() => {
      const rendered = (element) => {
        if (!element) return false;
        for (let node = element; node; node = node.parentElement) {
          const style = getComputedStyle(node);
          if (
            style.display === "none" || style.visibility === "hidden" ||
            style.visibility === "collapse" || Number(style.opacity) === 0
          ) return false;
        }
        const rect = element.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0;
      };
      const inspect = (element) => {
        if (!element) return null;
        const rect = element.getBoundingClientRect();
        let visibleLeft = rect.left;
        let visibleRight = rect.right;
        let visibleTop = rect.top;
        let visibleBottom = rect.bottom;
        let ariaHidden = false;
        let opacityZero = false;
        for (let ancestor = element; ancestor; ancestor = ancestor.parentElement) {
          const style = getComputedStyle(ancestor);
          if (ancestor.getAttribute("aria-hidden") === "true") ariaHidden = true;
          if (Number(style.opacity) === 0) opacityZero = true;
          if (ancestor === element) continue;
          const bounds = ancestor.getBoundingClientRect();
          if (["auto", "clip", "hidden", "scroll"].includes(style.overflowX)) {
            visibleLeft = Math.max(visibleLeft, bounds.left);
            visibleRight = Math.min(visibleRight, bounds.right);
          }
          if (["auto", "clip", "hidden", "scroll"].includes(style.overflowY)) {
            visibleTop = Math.max(visibleTop, bounds.top);
            visibleBottom = Math.min(visibleBottom, bounds.bottom);
          }
        }
        return {
          visible: rendered(element) && !ariaHidden, ariaHidden, opacityZero,
          top: rect.top, bottom: rect.bottom,
          left: rect.left, right: rect.right, width: rect.width, height: rect.height,
          clippedByAncestor: visibleRight - visibleLeft < rect.width - 1 ||
            visibleBottom - visibleTop < rect.height - 1,
        };
      };
      const map = document.querySelector("[data-chapter-reading-map]");
      const primary = document.querySelector("[data-primary-evidence]");
      const ids = ${JSON.stringify(targetIds)};
      const targets = ids.map((id) => document.getElementById(id));
      const links = [...document.querySelectorAll("[data-chapter-reading-link]")];
      const figures = [...document.querySelectorAll(".evidence-figure")];
      const tables = [...document.querySelectorAll("table")];
      const follows = (first, second) => Boolean(
        first && second && (first.compareDocumentPosition(second) & Node.DOCUMENT_POSITION_FOLLOWING)
      );
      return {
        viewportWidth: innerWidth,
        viewportHeight: innerHeight,
        thesis: inspect(document.querySelector(".chapter-thesis")),
        map: inspect(map),
        links: links.map(inspect),
        targets: targets.map(inspect),
        targetIds: targets.map((target) => target?.id ?? null),
        primary: inspect(primary),
        stickyBottom: document.querySelector(".handle")?.getBoundingClientRect().bottom ?? 0,
        anchorsMeasured: ${JSON.stringify(measureAnchors)},
        sourceOrdered: follows(document.querySelector(".chapter-thesis"), map) &&
          follows(map, targets[0]) &&
          Boolean(primary && targets[0] && (primary.contains(targets[0]) || follows(targets[0], primary))) &&
          follows(primary, targets[1]) && follows(targets[1], targets[2]),
        evidenceOrdered: figures.length === 2 && tables.length === 2 &&
          follows(targets[0], figures[0]) && follows(figures[0], targets[1]) &&
          follows(targets[1], figures[1]) && follows(figures[1], targets[2]) &&
          follows(targets[2], tables[0]) && follows(targets[2], tables[1]),
        horizontalOverflow: document.documentElement.scrollWidth -
          document.documentElement.clientWidth,
      };
    })()`);
    if (!measureAnchors || !state) return state;
    const afterJumpTop = await evaluate(`(async () => {
      const ids = ${JSON.stringify(targetIds)};
      const originalY = scrollY;
      const originalUrl = location.href;
      const root = document.documentElement;
      const originalScrollBehavior = root.style.scrollBehavior;
      const tops = [];
      root.style.scrollBehavior = "auto";
      try {
        for (const id of ids) {
          const target = document.getElementById(id);
          if (!target) { tops.push(null); continue; }
          history.replaceState(null, "", "#" + id);
          target.scrollIntoView({ block: "start" });
          await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
          tops.push(target.getBoundingClientRect().top);
        }
      } finally {
        history.replaceState(null, "", originalUrl);
        scrollTo(0, originalY);
        await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
        root.style.scrollBehavior = originalScrollBehavior;
      }
      return tops;
    })()`);
    state.targets = state.targets.map((target, index) => ({
      ...target,
      afterJumpTop: afterJumpTop[index],
    }));
    return state;
  };

  const readingMapPrintSnapshot = async (targetIds) => evaluate(`(() => {
    const rendered = (element) => {
      if (!element) return false;
      for (let node = element; node; node = node.parentElement) {
        const style = getComputedStyle(node);
        if (
          style.display === "none" || style.visibility === "hidden" ||
          style.visibility === "collapse" || Number(style.opacity) === 0
        ) return false;
      }
      const rect = element.getBoundingClientRect();
      return rect.width > 0 && rect.height > 0;
    };
    const follows = (first, second) => Boolean(
      first && second && (first.compareDocumentPosition(second) & Node.DOCUMENT_POSITION_FOLLOWING)
    );
    const thesis = document.querySelector(".chapter-thesis");
    const map = document.querySelector("[data-chapter-reading-map]");
    const primary = document.querySelector("[data-primary-evidence]");
    const targets = ${JSON.stringify(targetIds)}.map((id) => document.getElementById(id));
    const links = [...document.querySelectorAll("[data-chapter-reading-link]")];
    const figures = [...document.querySelectorAll(".evidence-figure")];
    const tables = [...document.querySelectorAll("table")];
    return {
      thesisVisible: rendered(thesis),
      mapVisible: rendered(map),
      primaryVisible: rendered(primary),
      linksVisible: links.map(rendered),
      targetsVisible: targets.map(rendered),
      sourceOrdered: follows(thesis, map) && follows(map, targets[0]) &&
        Boolean(primary && targets[0] && (primary.contains(targets[0]) || follows(targets[0], primary))) &&
        follows(primary, targets[1]) && follows(targets[1], targets[2]),
      evidenceOrdered: figures.length === 2 && tables.length === 2 &&
        follows(targets[0], figures[0]) && follows(figures[0], targets[1]) &&
        follows(targets[1], figures[1]) && follows(figures[1], targets[2]) &&
        follows(targets[2], tables[0]) && follows(targets[2], tables[1]),
    };
  })()`);

  const stationDossierSnapshot = async ({ changeStation }) => evaluate(`(async () => {
    const rendered = (element) => {
      if (!element) return false;
      for (let node = element; node; node = node.parentElement) {
        const style = getComputedStyle(node);
        if (
          style.display === "none" || style.visibility === "hidden" ||
          style.visibility === "collapse" || Number(style.opacity) === 0
        ) return false;
      }
      const rect = element.getBoundingClientRect();
      return rect.width > 0 && rect.height > 0;
    };
    const inspect = (element) => {
      if (!element) return null;
      const rect = element.getBoundingClientRect();
      return {
        visible: rendered(element), width: rect.width, height: rect.height,
        top: rect.top, right: rect.right, bottom: rect.bottom, left: rect.left,
      };
    };
    const borderWidth = (element, side) => {
      if (!element) return null;
      return Number.parseFloat(getComputedStyle(element)["border" + side + "Width"]);
    };
    const filterBox = document.querySelector("#station-filter");
    const controls = document.querySelector("[data-station-controls]");
    const searchField = document.querySelector(".station-search-field");
    const help = document.querySelector("#station-filter-help");
    const count = document.querySelector("#station-filter-count");
    const listbox = document.querySelector("#station-listbox");
    const searchRect = searchField?.getBoundingClientRect() ?? null;
    const helpRect = help?.getBoundingClientRect() ?? null;
    const countRect = count?.getBoundingClientRect() ?? null;
    const reports = [...document.querySelectorAll("[data-station-report]")];
    const text = (element) => element?.textContent?.trim() ?? "";
    const visibleReports = () => reports.filter(rendered);
    const shown = visibleReports()[0] ?? null;
    const stats = shown ? [...shown.querySelectorAll("[data-station-stat]")] : [];
    const comparisons = shown
      ? [...shown.querySelectorAll("[data-station-comparison]")] : [];
    const identity = shown?.querySelector("[data-station-identity]") ?? null;
    const statistics = shown?.querySelector("[data-station-stats]") ?? null;
    const comparisonGroup = shown?.querySelector("[data-station-comparisons]") ?? null;
    // Inside the rank comparison, not beside it: the comparisons list above
    // counts data-station-comparison and the predicate expects exactly two.
    // (No backticks in here — this whole block is a template literal.)
    const rankStrip = shown?.querySelector("[data-station-rank-strip]") ?? null;
    // One locator for 79 cards, so it is queried from the document rather than
    // from the shown report, and its mark has to be told which station it is on.
    const locator = document.querySelector("[data-station-locator]");
    const locatorMark = locator?.querySelector("[data-station-locator-mark]") ?? null;
    const standardNote = document.querySelector("[data-station-standard-note]");
    const statLefts = [...new Set(stats.map((item) =>
      Math.round(item.getBoundingClientRect().left * 10) / 10))];
    const result = {
      viewportWidth: innerWidth,
      picker: inspect(document.querySelector("[data-station-picker]")),
      controls: inspect(controls),
      searchField: inspect(searchField),
      help: inspect(help),
      count: inspect(count),
      combo: filterBox ? {
        role: filterBox.getAttribute("role"),
        expanded: filterBox.getAttribute("aria-expanded"),
        controlsListbox: filterBox.getAttribute("aria-controls"),
        autocomplete: filterBox.getAttribute("aria-autocomplete"),
        // The list must exist in the document while closed: aria-activedescendant
        // points into it, and a list rebuilt per keystroke loses its target.
        listboxPresent: Boolean(listbox),
        listboxRole: listbox?.getAttribute("role") ?? "",
        listboxHiddenAtRest: listbox ? listbox.hasAttribute("hidden") : null,
        optionCountInList: document.querySelectorAll("[data-station-option]").length,
        groupCountInList: document.querySelectorAll("[data-county-group]").length,
        selectedOptions: [...document.querySelectorAll("[data-station-option]")]
          .filter((option) => option.getAttribute("aria-selected") === "true")
          .map((option) => option.getAttribute("data-station-option")),
      } : null,
      // One field, two support rows, stacked under it.
      supportingRowsFollowFields: Boolean(
        searchRect && helpRect && countRect &&
        helpRect.top >= searchRect.bottom - 1 && countRect.top >= helpRect.bottom - 1
      ),
      // One field now, so the order that matters is the field before the two
      // lines that describe it — the pair the arrow keys and a screen reader
      // walk in turn.
      controlsFollowDomOrder: Boolean(
        filterBox && help &&
        (filterBox.compareDocumentPosition(help) & Node.DOCUMENT_POSITION_FOLLOWING)
      ),
      select: inspect(filterBox),
      optionCount: document.querySelectorAll("[data-station-option]").length,
      reportCount: reports.length,
      visibleReportCount: visibleReports().length,
      selectedValue: [...document.querySelectorAll("[data-station-option]")]
        .find((option) => option.getAttribute("aria-selected") === "true")
        ?.getAttribute("data-station-option") ?? null,
      visibleStation: shown?.getAttribute("data-station") ?? null,
      identityText: text(shown?.querySelector("[data-station-name]")),
      identityName: inspect(shown?.querySelector("[data-station-name]")),
      identityVisible: rendered(shown?.querySelector("[data-station-identity]")),
      yearVisible: rendered(shown?.querySelector("[data-station-year]")),
      stats: stats.map(inspect),
      comparisons: comparisons.map(inspect),
      rankStrip: rankStrip ? (() => {
        // The mark is a pseudo-element, so its box comes from the computed
        // style rather than from a node. Solved in pixels because the defect
        // this catches was a pixel one: a mark centred on its value hangs half
        // its width off the track at the first and last rank.
        const markStyle = getComputedStyle(rankStrip, "::after");
        const markLeft = Number.parseFloat(markStyle.left);
        const markWidth = Number.parseFloat(markStyle.width);
        const trackWidth = rankStrip.getBoundingClientRect().width;
        return {
          ...inspect(rankStrip),
          position: Number(rankStrip.getAttribute("data-rank-position")),
          rank: Number(rankStrip.getAttribute("data-rank")),
          total: Number(rankStrip.getAttribute("data-rank-total")),
          markLeft,
          markRight: markLeft + markWidth,
          trackWidth,
        };
      })() : null,
      locator: locator ? {
        ...inspect(locator),
        markVisible: rendered(locatorMark),
        markStation: locatorMark?.getAttribute("data-station") ?? "",
        countyCount: locator.querySelectorAll("[data-locator-county]").length,
        unplacedNoteVisible: rendered(
          locator.querySelector("[data-locator-unplaced-note]"),
        ),
        offshoreNoteVisible: rendered(
          locator.querySelector("[data-locator-offshore-note]"),
        ),
      } : null,
      columns: statLefts.length,
      separators: {
        reportTop: borderWidth(shown, "Top"),
        reportBottom: borderWidth(shown, "Bottom"),
        identityBottom: borderWidth(identity, "Bottom"),
        statisticsTop: borderWidth(statistics, "Top"),
        statisticTops: stats.map((stat) => borderWidth(stat, "Top")),
        comparisonsTop: borderWidth(comparisonGroup, "Top"),
        noteTop: borderWidth(standardNote, "Top"),
        noteBottom: borderWidth(standardNote, "Bottom"),
      },
      standardNote: inspect(standardNote),
      horizontalOverflow: document.documentElement.scrollWidth -
        document.documentElement.clientWidth,
      reportStyle: shown ? {
        background: getComputedStyle(shown).backgroundColor,
        borderRadius: getComputedStyle(shown).borderRadius,
      } : null,
      afterChange: {
        performed: false,
        selectedValue: null,
        visibleStation: null,
        identityText: null,
        identityName: null,
        visibleReportCount: null,
        selectedMatchesVisible: false,
        identity: null,
        year: null,
        stats: [],
        comparisons: [],
        liveIncludesStation: false,
        liveIncludesYear: false,
        liveIncludesFirstStat: false,
        liveIncludesThirdStat: false,
        locatorMarkStation: null,
        locatorUnplacedVisible: false,
      },
      restored: {
        performed: false,
        selectedValue: null,
        visibleStation: null,
        identityText: null,
        identityName: null,
        visibleReportCount: null,
        selectedMatchesVisible: false,
        liveIncludesStation: false,
        liveIncludesYear: false,
        liveIncludesFirstStat: false,
        liveIncludesThirdStat: false,
      },
    };
    /*
     * The search box narrows the menu and moves nothing else.
     *
     * A filter that changed the selection would recreate the failure this
     * chapter's <noscript> block describes: the control agreeing with you while
     * the card underneath is another station. Typing a county name is the case
     * that matters, because matching only the option text answered
     * 「沒有測站符合」 to the most obvious thing a reader would type.
     */
    if (filterBox && listbox) {
      const heldReport = visibleReports()[0]?.getAttribute("data-station") ?? null;
      const allOptions = [...document.querySelectorAll("[data-station-option]")];
      const total = allOptions.length;
      const county = document.querySelector("[data-county-group]")?.getAttribute("aria-label") ?? "";
      const settle = () =>
        new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      const stillShown = () => allOptions.filter((option) =>
        !option.hidden && !option.closest("[data-county-group]").hidden).length;
      const selectedNow = () => allOptions
        .filter((option) => option.getAttribute("aria-selected") === "true")
        .map((option) => option.getAttribute("data-station-option"))
        .join(",");
      const heldSelection = selectedNow();
      filterBox.value = county;
      filterBox.dispatchEvent(new Event("input", { bubbles: true }));
      await settle();
      const narrowedTo = stillShown();
      const heldWhileFiltered =
        selectedNow() === heldSelection &&
        (visibleReports()[0]?.getAttribute("data-station") ?? null) === heldReport;
      filterBox.value = "";
      filterBox.dispatchEvent(new Event("input", { bubbles: true }));
      await settle();
      result.filter = {
        height: filterBox.getBoundingClientRect().height,
        total,
        narrowedTo,
        narrowedByCounty: narrowedTo > 0 && narrowedTo < total,
        heldWhileFiltered,
        restored: stillShown() === total,
      };
    }
    const allStationOptions = [...document.querySelectorAll("[data-station-option]")];
    const selectedOptionName = () => allStationOptions
      .find((option) => option.getAttribute("aria-selected") === "true")
      ?.getAttribute("data-station-option") ?? null;
    if (${JSON.stringify(changeStation)} && filterBox && allStationOptions.length > 1) {
      const original = visibleReports()[0]?.getAttribute("data-station") ?? null;
      // Last option that is not the current one, the same choice the select
      // version made — which lands on a card carrying no coordinate, so the
      // locator's unplaced branch is exercised on every run.
      const replacement = [...allStationOptions].reverse()
        .find((option) => option.getAttribute("data-station-option") !== original);
      if (replacement) {
        const replacementName = replacement.getAttribute("data-station-option");
        replacement.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true }));
        await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
        const changedReports = visibleReports();
        const changed = changedReports[0] ?? null;
        const changedStats = changed
          ? [...changed.querySelectorAll("[data-station-stat]")] : [];
        const changedComparisons = changed
          ? [...changed.querySelectorAll("[data-station-comparison]")] : [];
        const live = text(document.querySelector("#station-say"));
        const year = text(changed?.querySelector("[data-station-year]"));
        const firstStat = text(changedStats[0]?.querySelector(".stat-value"));
        const thirdStat = text(changedStats[2]?.querySelector(".stat-value"));
        result.afterChange = {
          performed: true,
          selectedValue: selectedOptionName(),
          visibleStation: changed?.getAttribute("data-station") ?? null,
          identityText: text(changed?.querySelector("[data-station-name]")),
          identityName: inspect(changed?.querySelector("[data-station-name]")),
          visibleReportCount: changedReports.length,
          selectedMatchesVisible: selectedOptionName() === changed?.getAttribute("data-station"),
          identity: inspect(changed?.querySelector("[data-station-identity]")),
          year: inspect(changed?.querySelector("[data-station-year]")),
          stats: changedStats.map(inspect),
          comparisons: changedComparisons.map(inspect),
          liveIncludesStation: Boolean(replacementName && live.includes(replacementName)),
          liveIncludesYear: Boolean(year && live.includes(year)),
          liveIncludesFirstStat: Boolean(firstStat && live.includes(firstStat)),
          liveIncludesThirdStat: Boolean(thirdStat && live.includes(thirdStat)),
          locatorMarkStation:
            document.querySelector("[data-station-locator-mark]")
              ?.getAttribute("data-station") ?? null,
          locatorUnplacedVisible: rendered(
            document.querySelector("[data-locator-unplaced-note]"),
          ) || rendered(
            document.querySelector("[data-locator-offshore-note]"),
          ),
        };
        allStationOptions
          .find((option) => option.getAttribute("data-station-option") === original)
          ?.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true }));
        await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
        const restoredReports = visibleReports();
        const restored = restoredReports[0] ?? null;
        const restoredStats = restored
          ? [...restored.querySelectorAll("[data-station-stat]")] : [];
        const restoredLive = text(document.querySelector("#station-say"));
        const restoredYear = text(restored?.querySelector("[data-station-year]"));
        const restoredFirstStat = text(restoredStats[0]?.querySelector(".stat-value"));
        const restoredThirdStat = text(restoredStats[2]?.querySelector(".stat-value"));
        result.restored = {
          performed: true,
          selectedValue: selectedOptionName(),
          visibleStation: restored?.getAttribute("data-station") ?? null,
          identityText: text(restored?.querySelector("[data-station-name]")),
          identityName: inspect(restored?.querySelector("[data-station-name]")),
          visibleReportCount: restoredReports.length,
          selectedMatchesVisible: selectedOptionName() === original &&
            original === restored?.getAttribute("data-station"),
          liveIncludesStation: Boolean(original && restoredLive.includes(original)),
          liveIncludesYear: Boolean(restoredYear && restoredLive.includes(restoredYear)),
          liveIncludesFirstStat: Boolean(
            restoredFirstStat && restoredLive.includes(restoredFirstStat)
          ),
          liveIncludesThirdStat: Boolean(
            restoredThirdStat && restoredLive.includes(restoredThirdStat)
          ),
        };
      }
    }
    return result;
  })()`);

  const semanticBoundarySnapshot = async (selector) => evaluate(`(() => {
    const nodes = [...document.querySelectorAll(${JSON.stringify(selector)})];
    const rendered = (element) => {
      if (!element) return false;
      for (let node = element; node; node = node.parentElement) {
        const style = getComputedStyle(node);
        if (
          style.display === "none" || style.visibility === "hidden" ||
          style.visibility === "collapse" || Number(style.opacity) === 0
        ) return false;
      }
      const rect = element.getBoundingClientRect();
      return rect.width > 0 && rect.height > 0;
    };
    return {
      count: nodes.length,
      visible: nodes.length === 1 && rendered(nodes[0]),
      text: nodes.length === 1 ? nodes[0].innerText.replace(/\\s+/g, " ").trim() : "",
    };
  })()`);

  const interactionClarificationSnapshot = async () => evaluate(`(() => {
    const rendered = (element) => {
      if (!element) return false;
      for (let node = element; node; node = node.parentElement) {
        const style = getComputedStyle(node);
        if (
          style.display === "none" || style.visibility === "hidden" ||
          style.visibility === "collapse" || Number(style.opacity) === 0
        ) return false;
      }
      const rect = element.getBoundingClientRect();
      return rect.width > 0 && rect.height > 0;
    };
    const toolbars = [...document.querySelectorAll(".fig-tools")];
    const downloadButtons = toolbars.flatMap((toolbar) => {
      const button = [...toolbar.querySelectorAll(":scope > .fig-tool")]
        .find((item) => item.getAttribute("aria-label")?.startsWith("下載 PNG"));
      return button ? [button] : [];
    });
    const trendChart = document.querySelector(".chart:has([data-series-switch])");
    const trendHint = document.querySelector(".key-hint");
    const trendSwitches = [...(trendChart?.querySelectorAll("[data-series-switch]") ?? [])];
    const trendLines = [...(trendChart?.querySelectorAll("path.plot-line") ?? [])];
    let uncheckedLineDisplay = null;
    let checkedLineDisplay = null;
    if (trendSwitches.length === 8 && trendLines.length === 8) {
      trendSwitches[0].click();
      uncheckedLineDisplay = getComputedStyle(trendLines[0]).display;
      checkedLineDisplay = getComputedStyle(trendLines[1]).display;
      trendSwitches[0].click();
    }
    const stationHelper = document.querySelector("#station-filter-help");
    const stationFilter = document.querySelector("#station-filter");
    const mapRoutes = [...document.querySelectorAll("[data-homepage-map-station-route]")];
    return {
      figure: {
        toolbarCount: toolbars.length,
        downloadLabels: downloadButtons.map((button) => button.textContent.trim()),
        downloadAriaLabels: downloadButtons.map((button) => button.getAttribute("aria-label") ?? ""),
        downloadWhiteSpaces: downloadButtons.map((button) => getComputedStyle(button).whiteSpace),
      },
      trend: {
        hintCount: document.querySelectorAll(".key-hint").length,
        hintVisible: rendered(trendHint),
        hintText: trendHint?.innerText.replace(/\\s+/g, " ").trim() ?? "",
        uncheckedLineDisplay,
        checkedLineDisplay,
      },
      station: {
        count: document.querySelectorAll("#station-filter-help").length,
        visible: rendered(stationHelper),
        text: stationHelper?.innerText.replace(/\\s+/g, " ").trim() ?? "",
        describedBy: stationFilter?.getAttribute("aria-describedby") ?? "",
      },
      map: {
        count: mapRoutes.length,
        visible: mapRoutes.length === 1 && rendered(mapRoutes[0]),
        text: mapRoutes.length === 1 ? mapRoutes[0].innerText.replace(/\\s+/g, " ").trim() : "",
        href: mapRoutes.length === 1 ? mapRoutes[0].href : "",
      },
    };
  })()`);

  const stationRegisterSnapshot = async () => evaluate(`(() => {
    const rendered = (element) => {
      if (!element) return false;
      for (let node = element; node; node = node.parentElement) {
        const style = getComputedStyle(node);
        if (
          style.display === "none" || style.visibility === "hidden" ||
          style.visibility === "collapse" || Number(style.opacity) === 0
        ) return false;
      }
      const rect = element.getBoundingClientRect();
      return rect.width > 0 && rect.height > 0;
    };
    const reports = [...document.querySelectorAll("[data-station-report]")];
    const optionOrder = [...document.querySelectorAll("[data-station-option]")]
      .map((option) => option.getAttribute("data-station-option"));
    const reportOrder = reports.map((report) => report.getAttribute("data-station"));
    const stationNames = reports.flatMap((report) =>
      [...report.querySelectorAll("[data-station-name]")]);
    const matchingStationNameCount = reports.filter((report) => {
      const names = [...report.querySelectorAll("[data-station-name]")];
      return names.length === 1 &&
        names[0].textContent.trim() === report.getAttribute("data-station");
    }).length;
    return {
      selectorVisible: rendered(document.querySelector("[data-station-controls]")),
      liveVisible: rendered(document.querySelector("#station-say")),
      noScriptVisible: rendered(document.querySelector(".nojs")),
      reportCount: reports.length,
      visibleReportCount: reports.filter(rendered).length,
      stationNameCount: stationNames.length,
      visibleStationNameCount: stationNames.filter(rendered).length,
      matchingStationNameCount,
      ordered: optionOrder.length > 0 && optionOrder.join("\\n") === reportOrder.join("\\n"),
      standardNotes: [...document.querySelectorAll("[data-station-standard-note]")]
        .filter(rendered).length,
      conversionNotes: [...document.querySelectorAll("[data-station-conversion-note]")]
        .filter(rendered).length,
    };
  })()`);

  const homepageStructureProblems = async ({ enhanced }) => {
    const structure = await evaluate(`(() => {
      const map = document.querySelector("[data-homepage-map-frame]");
      const svg = map?.querySelector("svg[role='img']") ?? null;
      const descriptionIds = (svg?.getAttribute("aria-describedby") ?? "")
        .split(/\\s+/).filter(Boolean);
      const description = descriptionIds.length === 1
        ? document.getElementById(descriptionIds[0]) : null;
      const descriptionStyle = description ? getComputedStyle(description) : null;
      const titles = [...(svg?.querySelectorAll(".county > title") ?? [])]
        .map((title) => title.textContent.trim()).filter(Boolean).sort();
      const countyPathSubpaths = Object.fromEntries(
        [...(svg?.querySelectorAll(".county") ?? [])].map((path) => [
          path.querySelector("title")?.textContent.trim() ?? "",
          (path.getAttribute("d")?.match(/\\bM/g) ?? []).length,
        ]),
      );
      const describedNames = [...(description?.querySelectorAll("[data-homepage-map-county]") ?? [])]
        .map((name) => name.textContent.trim()).filter(Boolean).sort();
      const overlays = [...(map?.querySelectorAll(".county-label") ?? [])];
      const after = document.querySelector(".hero-after");
      const levelSummary = document.querySelector("[data-homepage-level-summary]");
      const routes = document.querySelector("[data-homepage-routes]");
      const notes = document.querySelector(".map-notes");
      const mountId = map?.getAttribute("data-figure-tools-mount") ?? "";
      const mounts = mountId ? [...document.querySelectorAll("#" + CSS.escape(mountId))] : [];
      const mount = mounts[0] ?? null;
      const tools = mount?.querySelector(":scope > .fig-tools") ?? null;
      const follows = (before, after) => Boolean(
        before && after && (before.compareDocumentPosition(after) & Node.DOCUMENT_POSITION_FOLLOWING)
      );
      return {
        titleNames: titles,
        countyPathSubpaths,
        describedNames,
        descriptionIds,
        descriptionExposed: Boolean(
          description && descriptionStyle && descriptionStyle.display !== "none" &&
          descriptionStyle.visibility !== "hidden" && description.getAttribute("aria-hidden") !== "true"
        ),
        visualLabelsHiddenFromAccessibility: Boolean(
          overlays.length && overlays.every((label) => label.getAttribute("aria-hidden") === "true")
        ),
        mountCount: mounts.length,
        toolCount: tools?.querySelectorAll(":scope > button").length ?? 0,
        toolsOutsideMap: Boolean(tools && map && !map.contains(tools)),
        enhancedOrder: Boolean(
          follows(map, after) && follows(after, notes) && follows(notes, tools)
        ),
        editorialReadingOrder: Boolean(
          follows(routes, map) && follows(map, levelSummary) &&
          routes && map && routes.getBoundingClientRect().bottom <=
            map.getBoundingClientRect().top + 1 &&
          levelSummary && map.getBoundingClientRect().bottom <=
            levelSummary.getBoundingClientRect().top + 1
        ),
        notesText: notes?.textContent.replace(/\s+/g, " ").trim() ?? "",
        apparatusLabelText: (
          notes?.querySelector("[data-map-apparatus-label]")?.textContent ?? ""
        ).replace(/\s+/g, " ").trim(),
        // 2026-09-02 — the notes are a disclosure and the two controls sit
        // beside its summary in one strip; the vertical centres of the two
        // say whether that row actually formed.
        apparatusRowOffset: (() => {
          const summary = notes?.querySelector("summary");
          if (!summary || !tools) return null;
          const s = summary.getBoundingClientRect();
          const t = tools.getBoundingClientRect();
          return Math.abs((s.top + s.bottom) / 2 - (t.top + t.bottom) / 2);
        })(),
        // Whether the strip is wide enough for the summary's ink and the two
        // controls on one row. At 200% text it is not, and the pair wrapping
        // there is the strip working, not failing — so the row is required
        // only where it fits.
        apparatusFits: (() => {
          const summary = notes?.querySelector("summary");
          const strip = notes?.parentElement ?? null;
          if (!summary || !tools || !strip) return null;
          const range = document.createRange();
          range.selectNodeContents(summary);
          const s = summary.getBoundingClientRect();
          const inkRight = range.getBoundingClientRect().right;
          const summaryStyle = getComputedStyle(summary);
          const stripStyle = getComputedStyle(strip);
          const needed = (inkRight - s.left) + (parseFloat(summaryStyle.paddingInlineEnd) || 0) +
            (parseFloat(stripStyle.columnGap) || 0) + tools.getBoundingClientRect().width;
          return needed <= strip.getBoundingClientRect().width;
        })(),
        viewportWidth: innerWidth,
      };
    })()`);
    const problems = [];
    if (!structure?.titleNames?.length) {
      problems.push("homepage county source names are missing");
    }
    if (
      structure?.descriptionIds?.length !== 1 || !structure?.descriptionExposed ||
      JSON.stringify(structure?.describedNames) !== JSON.stringify(structure?.titleNames)
    ) {
      problems.push("homepage county names are absent from the accessible map description");
    }
    if (!structure?.visualLabelsHiddenFromAccessibility) {
      problems.push("homepage visual county labels are exposed as duplicate accessible names");
    }
    if (structure?.countyPathSubpaths?.["雲林縣"] !== 1) {
      problems.push(
        `homepage Yunlin overview draws ${
          structure?.countyPathSubpaths?.["雲林縣"] ?? "unknown"
        } subpaths instead of the mainland outline only`,
      );
    }
    if (!structure?.notesText?.includes("省略無測站的雲林離岸沙洲")) {
      problems.push("homepage map notes do not disclose the omitted Yunlin offshore sandbars");
    }
    // The notes and the two buttons below them act on a 330px figure that, at
    // 1280, ends 908px and 1,276px above them at full page width. That distance
    // is not the defect — the block sits there to close a 510x620px hole beside
    // the statistics, and runs two columns to close a 326px one — but nothing
    // in it said which figure it belonged to.
    if (!structure?.apparatusLabelText) {
      problems.push("homepage map notes do not name the figure they belong to");
    }
    if (
      structure?.viewportWidth >= 1024 && structure?.apparatusFits &&
      (!Number.isFinite(structure?.apparatusRowOffset) || structure.apparatusRowOffset > 4)
    ) {
      problems.push("homepage map apparatus summary and tools do not share a row");
    }
    if (
      enhanced &&
      (
        structure?.mountCount !== 1 || structure?.toolCount !== 2 ||
        !structure?.toolsOutsideMap || !structure?.enhancedOrder ||
        !structure?.editorialReadingOrder
      )
    ) {
      problems.push("homepage post-map evidence order is incomplete");
    }
    return problems;
  };

  const chapterIndexProblems = async () => {
    const structure = await evaluate(`(() => {
      const visible = (element) => {
        const style = getComputedStyle(element);
        const rect = element.getBoundingClientRect();
        return style.display !== "none" && style.visibility !== "hidden" &&
          element.getAttribute("aria-hidden") !== "true" && rect.width > 0 && rect.height > 0;
      };
      const groups = [...document.querySelectorAll("[data-chapter-group]")].filter(visible);
      const links = [...document.querySelectorAll("[data-chapter-index-link]")].filter(visible);
      return {
        groupCount: groups.length,
        linkCount: links.length,
        groupSizes: groups.map((group) =>
          links.filter((link) => group.contains(link)).length
        ),
        destinations: links.map((link) => new URL(link.href, location.href).pathname),
      };
    })()`);
    const problems = [];
    if (structure?.groupCount !== 3) {
      problems.push(`chapter index has ${structure?.groupCount ?? "unknown"} intent groups`);
    }
    if (structure?.linkCount !== CHAPTER_ROUTES.length) {
      problems.push(
        `chapter index has ${structure?.linkCount ?? "unknown"} links, ` +
          `expected ${CHAPTER_ROUTES.length}`,
      );
    }
    if (JSON.stringify(structure?.groupSizes) !== JSON.stringify([4, 3, 3])) {
      problems.push("chapter index intent groups do not contain 4, 3 and 3 chapters");
    }
    if (JSON.stringify(structure?.destinations) !== JSON.stringify(CHAPTER_ROUTES)) {
      problems.push("chapter index destinations changed canonical order");
    }
    return problems;
  };

  const failures = [];
  const trendGuideSnapshot = () =>
    evaluate(`(() => {
      const visible = (element) => {
        if (!element) return false;
        const style = getComputedStyle(element);
        const rect = element.getBoundingClientRect();
        return style.display !== "none" && style.visibility !== "hidden" &&
          rect.width > 0 && rect.height > 0;
      };
      const rect = (element) => {
        const box = element?.getBoundingClientRect();
        return box ? {
          top: box.top, right: box.right, bottom: box.bottom, left: box.left,
          width: box.width, height: box.height,
        } : null;
      };
      const contentWidth = (element) => {
        if (!element) return null;
        const style = getComputedStyle(element);
        const width = element.getBoundingClientRect().width -
          parseFloat(style.paddingInlineStart) - parseFloat(style.paddingInlineEnd) -
          parseFloat(style.borderInlineStartWidth) - parseFloat(style.borderInlineEndWidth);
        return Number.isFinite(width) ? width : null;
      };
      const charts = [...document.querySelectorAll(".chart")].slice(0, 3);
      const firstPlot = document.querySelector(".plot[data-readout]");
      const chart = firstPlot?.closest(".chart");
      const plotArea = firstPlot?.querySelector(".plot-area");
      const transitionPlotAnnotation = [...(firstPlot?.querySelectorAll(
        '.guide-annotation[data-kind="transition"]',
      ) ?? [])].find(visible);
      const transitionKeyAnnotation = [...(chart?.querySelectorAll(
        '.chart-key .guide-annotation[data-kind="transition"]',
      ) ?? [])].find(visible);
      const transitionPlotHost = transitionPlotAnnotation?.closest(".plot-note-transition");
      const transitionKeyHost = transitionKeyAnnotation?.closest(".key-guide-transition");
      const transition = transitionPlotAnnotation ?? transitionKeyAnnotation;
      const transitionRows = transition?.querySelectorAll(".guide-row") ?? [];
      const transitionKey = chart?.querySelector(".chart-key .key-guide-transition");
      const plotWho = firstPlot?.querySelector('.guide-annotation[data-kind="value"]');
      const keyWho = [...(chart?.querySelectorAll(
        '.chart-key .guide-annotation[data-kind="value"]',
      ) ?? [])].find(visible);
      const who = [plotWho, keyWho].find(visible);
      const whoPlotNote = plotWho?.closest(".plot-note");
      const whoKeyItem = keyWho?.closest(".key-guide-on-line");
      const whoLine = firstPlot?.querySelector(
        '[data-guide-kind="level"][data-guide-level="5"]',
      );
      const startMarker = firstPlot?.querySelector('[data-guide-kind="start-marker"]');
      const startNote = firstPlot?.querySelector('[data-guide-kind="start-note"]');
      const startLeader = firstPlot?.querySelector('[data-guide-kind="start-leader"]');
      const readout = JSON.parse(firstPlot?.querySelector(".plot-readout-data")?.textContent ?? "{}");
      const renderedYears = (readout.x ?? []).map(Number);
      const yearX = (year) => {
        const index = renderedYears.indexOf(year);
        return index === -1 || renderedYears.length < 2
          ? NaN
          : plotArea.getBoundingClientRect().left +
            plotArea.getBoundingClientRect().width * index / (renderedYears.length - 1);
      };
      const oldValue = transition?.querySelector(".guide-value-old");
      const change = transition?.querySelector(".guide-change");
      const currentValue = transition?.querySelector(".guide-value-current");
      const whoTitle = who?.querySelector(".guide-title");
      const whoValue = who?.querySelector(".guide-value-who");
      const guidePaths = [...(firstPlot?.querySelectorAll("[data-guide-kind]") ?? [])]
        .filter((element) => element instanceof SVGElement);
      const transitionNote = firstPlot
        ?.querySelector('.guide-annotation[data-kind="transition"]')
        ?.closest(".plot-note");
      const annotationBoxes = [transitionNote, startNote]
        .filter(visible)
        .map((note) => note.getBoundingClientRect());
      const seriesOcclusionBoxes = [transitionNote, startNote, startLeader, startMarker]
        .filter(visible)
        .map((annotation) => annotation.getBoundingClientRect());
      const plotAnnotationBoxes = [transitionNote, startNote, whoPlotNote]
        .filter(visible)
        .map((note) => note.getBoundingClientRect());
      const plotBox = plotArea?.getBoundingClientRect();
      const annotationOverlapCount = plotAnnotationBoxes.reduce(
        (total, box, index) => total + plotAnnotationBoxes.slice(index + 1)
          .filter((other) =>
            Math.min(box.right, other.right) - Math.max(box.left, other.left) > 1 &&
            Math.min(box.bottom, other.bottom) - Math.max(box.top, other.top) > 1,
          ).length,
        0,
      );
      // Two of these three are anchored to the data and one is not.
      //
      // startNote names a point on the line and whoPlotNote names a level drawn
      // across it: both mean nothing outside the box whose coordinates place
      // them, so the drawing box is their boundary. The transition card is a
      // legend — notePlacement "top-right" puts it in a corner by fiat, not by
      // any value — and it is allowed the band .plot reserves above its drawing
      // box, which on this figure holds the unit label in its leftmost 107px and
      // nothing at all to the right of that. It stays bounded: the key row ends
      // where .plot begins, and seriesOcclusionCount below still requires it to
      // cover zero sampled points of any line.
      const plotElementBox = firstPlot?.getBoundingClientRect();
      const outsideBounds = (box, bounds) => !bounds ||
        box.left < bounds.left - 1 || box.right > bounds.right + 1 ||
        box.top < bounds.top - 1 || box.bottom > bounds.bottom + 1;
      const dataAnchoredBoxes = [startNote, whoPlotNote]
        .filter(visible)
        .map((note) => note.getBoundingClientRect());
      const transitionBoxes = [transitionNote]
        .filter(visible)
        .map((note) => note.getBoundingClientRect());
      const plotBoundaryViolationCount = plotBox
        ? dataAnchoredBoxes.filter((box) => outsideBounds(box, plotBox)).length +
          transitionBoxes.filter((box) => outsideBounds(box, plotElementBox)).length
        : plotAnnotationBoxes.length;
      const seriesOcclusionCount = seriesOcclusionBoxes.reduce(
        (annotationTotal, annotationBox) =>
          annotationTotal + [...(firstPlot?.querySelectorAll("path.plot-line") ?? [])]
            .reduce((total, path) => {
              const length = path.getTotalLength();
              const matrix = path.getScreenCTM();
              if (!matrix) return total;
              let hits = 0;
              for (let index = 0; index <= 1000; index += 1) {
                const point = path.getPointAtLength(length * index / 1000)
                  .matrixTransform(matrix);
                if (
                  point.x >= annotationBox.left && point.x <= annotationBox.right &&
                  point.y >= annotationBox.top && point.y <= annotationBox.bottom
                ) hits += 1;
              }
              return total + hits;
            }, 0),
        0,
      );
      const token = (name) => {
        const probe = document.createElement("span");
        probe.style.color = "var(" + name + ")";
        document.body.append(probe);
        const colour = getComputedStyle(probe).color;
        probe.remove();
        return colour;
      };
      const alphaOf = (colour) => {
        if (!colour) return null;
        const canvas = document.createElement("canvas");
        canvas.width = canvas.height = 1;
        const context = canvas.getContext("2d", { willReadFrequently: true });
        context.clearRect(0, 0, 1, 1);
        context.fillStyle = colour;
        context.fillRect(0, 0, 1, 1);
        return context.getImageData(0, 0, 1, 1).data[3];
      };
      const surface = (element) => {
        if (!element) return null;
        const style = getComputedStyle(element);
        const border = (side) => ({
          width: parseFloat(style["border" + side + "Width"]),
          style: style["border" + side + "Style"],
          colour: style["border" + side + "Color"],
        });
        return {
          box: rect(element),
          background: style.backgroundColor,
          backgroundAlpha: alphaOf(style.backgroundColor),
          borders: {
            top: border("Top"),
            right: border("Right"),
            bottom: border("Bottom"),
            left: border("Left"),
          },
          boxShadow: style.boxShadow,
          paddingInline: parseFloat(style.paddingInlineStart) + parseFloat(style.paddingInlineEnd),
          paddingBlock: parseFloat(style.paddingBlockStart) + parseFloat(style.paddingBlockEnd),
        };
      };
      const mark = (element) => {
        if (!element) return null;
        const style = getComputedStyle(element);
        return {
          stroke: style.stroke,
          weight: parseFloat(style.strokeWidth),
          dash: style.strokeDasharray
            .replaceAll("px", "")
            .replaceAll(",", " ")
            .replace(/\\s+/g, " ")
            .trim(),
          cap: style.strokeLinecap,
        };
      };
      const whoBackground = whoPlotNote
        ? getComputedStyle(whoPlotNote).backgroundColor
        : null;
      const guideCopies = charts.map((currentChart) => {
        const plotCopies = [...currentChart.querySelectorAll(".plot .plot-note")];
        const keyCopies = [...currentChart.querySelectorAll(
          ".chart-key .key-guide",
        )];
        const chartTransitionPlot = [...currentChart.querySelectorAll(
          ".plot .plot-note-transition",
        )].find(visible);
        const chartTransitionKey = [...currentChart.querySelectorAll(
          ".chart-key .key-guide-transition",
        )].find(visible);
        const currentKey = currentChart.querySelector(".chart-key");
        const activeTransition = chartTransitionPlot ?? chartTransitionKey;
        const activeTransitionAnnotation = activeTransition?.querySelector(
          '.guide-annotation[data-kind="transition"]',
        );
        const activeTransitionRows = activeTransitionAnnotation?.querySelectorAll(
          ".guide-row",
        ) ?? [];
        const activeTransitionBox = activeTransition?.getBoundingClientRect() ?? null;
        const currentPlot = currentChart.querySelector(".plot-area");
        const currentPlotBox = currentPlot?.getBoundingClientRect() ?? null;
        const transitionOverlapsPlot = Boolean(
          activeTransitionBox && currentPlotBox &&
          Math.min(activeTransitionBox.right, currentPlotBox.right) -
            Math.max(activeTransitionBox.left, currentPlotBox.left) > 1 &&
          Math.min(activeTransitionBox.bottom, currentPlotBox.bottom) -
            Math.max(activeTransitionBox.top, currentPlotBox.top) > 1
        );
        const transitionSeriesOcclusionCount = activeTransitionBox
          ? [...(currentPlot?.querySelectorAll("path.plot-line") ?? [])]
              .reduce((total, path) => {
                const length = path.getTotalLength();
                const matrix = path.getScreenCTM();
                if (!matrix) return total;
                let hits = 0;
                for (let index = 0; index <= 1000; index += 1) {
                  const point = path.getPointAtLength(length * index / 1000)
                    .matrixTransform(matrix);
                  if (
                    point.x >= activeTransitionBox.left &&
                    point.x <= activeTransitionBox.right &&
                    point.y >= activeTransitionBox.top &&
                    point.y <= activeTransitionBox.bottom
                  ) hits += 1;
                }
                return total + hits;
              }, 0)
          : 0;
        const configuredSource = currentChart.getAttribute("data-guide-count");
        const plotVisible = plotCopies.filter(visible).length;
        const keyVisible = keyCopies.filter(visible).length;
        const chartStyle = getComputedStyle(currentChart);
        return {
          configured: configuredSource === null ? null : Number(configuredSource),
          chartContentWidth: contentWidth(currentChart),
          rootSize: parseFloat(getComputedStyle(document.documentElement).fontSize),
          plotRendered: plotCopies.length,
          keyRendered: keyCopies.length,
          plotVisible,
          keyVisible,
          visible: plotVisible + keyVisible,
          transitionPlotVisible: chartTransitionPlot ? 1 : 0,
          transitionKeyVisible: chartTransitionKey ? 1 : 0,
          transitionOverlapsPlot,
          transitionSeriesOcclusionCount,
          keyBox: rect(currentKey),
          keyContentWidth: contentWidth(currentKey),
          transitionKeyBox: rect(chartTransitionKey),
          timelineBoxes: {
            title: rect(activeTransitionAnnotation?.querySelector(".guide-title")),
            old: rect(activeTransitionRows[0]),
            change: rect(activeTransitionAnnotation?.querySelector(".guide-change")),
            current: rect(activeTransitionRows[1]),
          },
          containerName: chartStyle.containerName,
          transitionText: [...currentChart.querySelectorAll(
            '.guide-annotation[data-kind="transition"]',
          )].map((node) => node.textContent).join(" ").replace(/\s+/g, " ").trim(),
        };
      });
      return {
        chartContainerName: chart ? getComputedStyle(chart).containerName : null,
        chartContentWidth: contentWidth(chart),
        plotContentWidth: contentWidth(firstPlot),
        guideCopies,
        transitionVisible: visible(transition),
        whoVisible: visible(who),
        plotNotesVisible: [...(firstPlot?.querySelectorAll(".plot-note") ?? [])]
          .filter(visible).length,
        text: [transition?.textContent, who?.textContent]
          .filter(Boolean).join(" ").replace(/\\s+/g, " ").trim(),
        boxes: {
          chart: rect(chart), plotArea: rect(plotArea), plot: rect(firstPlot),
          transitionNote: rect(transitionNote),
          transition: rect(transition), who: rect(who), old: rect(oldValue),
          change: rect(change), current: rect(currentValue), startMarker: rect(startMarker),
          startNote: rect(startNote),
          startLeader: rect(startLeader),
        },
        expectedX2010: yearX(2010),
        expectedX2012: yearX(2012),
        expectedX2024: yearX(2024),
        standardPathEndpoints: Object.fromEntries(
          guidePaths
            .filter((path) => path.getAttribute("data-guide-kind") === "level")
            .map((path) => [path.getAttribute("data-guide-level"), rect(path)]),
        ),
        colours: {
          old: oldValue ? getComputedStyle(oldValue).color : null,
          current: currentValue ? getComputedStyle(currentValue).color : null,
          whoTitle: whoTitle ? getComputedStyle(whoTitle).color : null,
          whoValue: whoValue ? getComputedStyle(whoValue).color : null,
        },
        valueBackgroundAlpha: {
          old: alphaOf(oldValue ? getComputedStyle(oldValue).backgroundColor : null),
          current: alphaOf(currentValue ? getComputedStyle(currentValue).backgroundColor : null),
        },
        guidePaths: guidePaths.map((path) => ({
          kind: path.getAttribute("data-guide-kind"),
          level: path.getAttribute("data-guide-level"),
          ...mark(path),
        })),
        samples: {
          old: mark(transition?.querySelector(".guide-line-sample-old line")),
          current: mark(transition?.querySelector(".guide-line-sample-current line")),
        },
        transitionTypography: {
          fontSize: transitionNote ? parseFloat(getComputedStyle(transitionNote).fontSize) : null,
          rootSize: parseFloat(getComputedStyle(document.documentElement).fontSize),
        },
        seriesStrokes: [...(firstPlot?.querySelectorAll("path.plot-line") ?? [])]
          .slice(0, 2)
          .map((path) => getComputedStyle(path).stroke),
        guideTokens: {
          oldMark: token("--k2"),
          oldInk: token("--k2-ink"),
          currentMark: token("--k5"),
          currentInk: token("--k5-ink"),
          who: token("--c1-ink"),
          seriesAll: token("--k0"),
          seriesBalanced: token("--k1"),
          riser: token("--rule"),
          line: token("--line"),
          bgRaised: token("--bg-raised"),
        },
        whoPlacement: {
          label: rect(whoPlotNote),
          key: rect(whoKeyItem),
          line: rect(whoLine),
          background: whoBackground,
          backgroundAlpha: alphaOf(whoBackground),
          plotCount: [...(firstPlot?.querySelectorAll(
            '.guide-annotation[data-kind="value"]',
          ) ?? [])].filter(visible).length,
          keyCount: [...(chart?.querySelectorAll(
            '.chart-key .guide-annotation[data-kind="value"]',
          ) ?? [])].filter(visible).length,
        },
        startPlacement: {
          markerVisible: visible(startMarker),
          noteVisible: visible(startNote),
          leaderVisible: visible(startLeader),
          markerYear: startMarker?.getAttribute("data-guide-year") ?? null,
          noteText: startNote?.textContent?.replace(/\\s+/g, " ").trim() ?? null,
          noteAriaLabel: startNote?.getAttribute("aria-label") ?? null,
        },
        transitionPlacement: {
          plotCount: [...(firstPlot?.querySelectorAll(
            '.guide-annotation[data-kind="transition"]',
          ) ?? [])].filter(visible).length,
          keyCount: [...(chart?.querySelectorAll(
            '.chart-key .guide-annotation[data-kind="transition"]',
          ) ?? [])].filter(visible).length,
          keyOuterMarkCount: [...(transitionKey?.children ?? [])]
            .filter((element) => element.matches(".key-mark") && visible(element)).length,
          keyOldSampleCount: [...(transitionKey?.querySelectorAll(
            ".guide-line-sample-old",
          ) ?? [])].filter(visible).length,
          keyCurrentSampleCount: [...(transitionKey?.querySelectorAll(
            ".guide-line-sample-current",
          ) ?? [])].filter(visible).length,
          activeSampleCount: [...(transition?.querySelectorAll(
            ".guide-line-sample",
          ) ?? [])].filter(visible).length,
          activeOldSampleCount: [...(transition?.querySelectorAll(
            ".guide-line-sample-old",
          ) ?? [])].filter(visible).length,
          activeCurrentSampleCount: [...(transition?.querySelectorAll(
            ".guide-line-sample-current",
          ) ?? [])].filter(visible).length,
          activeChangeSampleCount: [...(transition?.querySelectorAll(
            ".guide-change .guide-line-sample",
          ) ?? [])].filter(visible).length,
          activeOtherSampleCount: [...(transition?.querySelectorAll(
            ".guide-line-sample",
          ) ?? [])].filter((sample) =>
            visible(sample) && !sample.matches(
              ".guide-line-sample-old, .guide-line-sample-current",
            )
          ).length,
          plotSurface: surface(transitionPlotHost),
          keySurface: surface(transitionKeyHost),
          titleText: transition?.querySelector(".guide-title")
            ?.textContent?.replace(/\\s+/g, " ").trim() ?? null,
          fromText: transitionRows[0]?.textContent
            ?.replace(/(?<=\\S)(?<!\\d)(?=\\d+$)/, " ").replace(/\\s+/g, " ").trim() ?? null,
          changeText: transition?.querySelector(".guide-change")
            ?.textContent?.replace(/^\\s*↓\\s*/, "").replace(/\\s+/g, " ").trim() ?? null,
        },
        plotHeight: firstPlot?.querySelector(".plot-area")?.getBoundingClientRect().height ?? null,
        horizontalOverflow:
          document.documentElement.scrollWidth - document.documentElement.clientWidth,
        annotationOverlapCount,
        plotBoundaryViolationCount,
        seriesOcclusionCount,
      };
    })()`);
  const forceFirstTrendChartContentWidth = (targetWidth) =>
    evaluate(`(() => {
      const chart = document.querySelector(".chart");
      if (!chart) return false;
      const style = getComputedStyle(chart);
      const inlineFrame =
        parseFloat(style.paddingInlineStart) + parseFloat(style.paddingInlineEnd) +
        parseFloat(style.borderInlineStartWidth) + parseFloat(style.borderInlineEndWidth);
      chart.style.width = String(
        ${JSON.stringify(targetWidth)} + (style.boxSizing === "border-box" ? inlineFrame : 0),
      ) + "px";
      chart.style.maxWidth = "none";
      return true;
    })()`);
  const trendGuideCopyProblems = (snapshot, width, theme) => {
    const problems = [];
    const contentBoxes =
      " (chart content " + String(snapshot?.chartContentWidth) +
      "px, plot content " + String(snapshot?.plotContentWidth) + "px)";
    for (const [index, copies] of (snapshot?.guideCopies ?? []).entries()) {
      const configuredIsValid =
        Number.isInteger(copies.configured) && copies.configured > 0;
      if (
        !configuredIsValid ||
        copies.plotRendered !== copies.configured ||
        copies.keyRendered !== copies.configured ||
        copies.visible !== copies.configured
      ) {
        problems.push(
          "/trend/ @" + width + " " + theme + ": Figure 1." + (index + 1) +
            " guide copies disagree with source count " + String(copies.configured) +
            " (plot rendered " + copies.plotRendered + ", visible " +
            copies.plotVisible + "; key rendered " + copies.keyRendered +
            ", visible " + copies.keyVisible + "; combined visible " +
            copies.visible + ")" + contentBoxes,
        );
      }
      if (
        index < 2 &&
        !["台灣 PM2.5 年均標準", "2012.05.14", "15", "2024.09.30", "12"]
          .every((part) => copies.transitionText.includes(part))
      ) {
        problems.push(
          "/trend/ @" + width + " " + theme + ": Figure 1." + (index + 1) +
            " lacks the shared Taiwan-standard transition legend" + contentBoxes,
        );
      }
      if (index < 2) {
        const enlargedTextNeedsKey =
          Number.isFinite(copies.chartContentWidth) &&
          Number.isFinite(copies.rootSize) &&
          copies.chartContentWidth > 888 && copies.chartContentWidth <= copies.rootSize * 40;
        // 2026-08-26 — both figures now follow one rule: the corner where there
        // is room for it, the key row where there is not. Figure 1.2 used to
        // carry `index === 1` here and sit in the key row at every width, which
        // cost it a 101px key row against 1.1's 37 and started its drawing box
        // 64px lower, on a chapter whose primary evidence has to clear 55vh.
        // Nothing recorded why the two differed. Measured before changing it:
        // the corner card covers 0 of 400 sampled points on either series at
        // 1120, 1280, 1440 and 1920, and the occlusion check below still holds
        // it to that at every width this gate renders.
        const expectedKey = copies.chartContentWidth <= 888 || enlargedTextNeedsKey;
        if (
          copies.transitionPlotVisible !== (expectedKey ? 0 : 1) ||
          copies.transitionKeyVisible !== (expectedKey ? 1 : 0)
        ) {
          problems.push(
            "/trend/ @" + width + " " + theme + ": Figure 1." + (index + 1) +
              " transition legend is in the wrong chart region" + contentBoxes,
          );
        }
        // Only while it is in the key row. In the corner it is over the plot by
        // design, and `transitionSeriesOcclusionCount` is what bounds it there.
        if (
          index === 1 && expectedKey &&
          (copies.transitionOverlapsPlot || copies.transitionSeriesOcclusionCount !== 0)
        ) {
          problems.push(
            "/trend/ @" + width + " " + theme +
              ": Figure 1.2 transition legend overlaps its plot or covers " +
              copies.transitionSeriesOcclusionCount + " sampled series points" + contentBoxes,
          );
        }
        if (index === 1 && expectedKey) {
          const rightGap = copies.keyBox && copies.transitionKeyBox
            ? copies.keyBox.right - copies.transitionKeyBox.right
            : NaN;
          const expectedWidth = Math.min(
            copies.rootSize * 23,
            copies.keyContentWidth,
          );
          if (!Number.isFinite(rightGap) || Math.abs(rightGap) > 1) {
            problems.push(
              "/trend/ @" + width + " " + theme +
                ": Figure 1.2 standard legend does not use the key's right edge (gap " +
                String(rightGap) + "px)" + contentBoxes,
            );
          }
          if (
            !Number.isFinite(copies.transitionKeyBox?.width) ||
            !Number.isFinite(expectedWidth) ||
            copies.transitionKeyBox.width < expectedWidth - 1
          ) {
            problems.push(
              "/trend/ @" + width + " " + theme +
                ": Figure 1.2 standard legend is narrower than its available 23rem register (" +
                String(copies.transitionKeyBox?.width) + "px, expected " +
                String(expectedWidth) + "px)" + contentBoxes,
            );
          }
          const compactTimelineExpected =
            copies.chartContentWidth >= copies.rootSize * 44;
          const overlapsVertically = (first, second) =>
            first && second &&
            Math.min(first.bottom, second.bottom) - Math.max(first.top, second.top) > 1;
          const timeline = copies.timelineBoxes;
          if (
            compactTimelineExpected &&
            (!overlapsVertically(timeline?.title, timeline?.old) ||
              !overlapsVertically(timeline?.change, timeline?.current))
          ) {
            problems.push(
              "/trend/ @" + width + " " + theme +
                ": Figure 1.2 standard timeline still consumes four vertical rows" +
                contentBoxes,
            );
          }
        }
      }
    }
    return problems;
  };
  const wideReadingLayoutSnapshot = () =>
    evaluate(`(() => {
      const visible = (element) => {
        if (!element) return false;
        const style = getComputedStyle(element);
        const box = element.getBoundingClientRect();
        return style.display !== "none" && style.visibility !== "hidden" &&
          box.width > 0 && box.height > 0;
      };
      const box = (element) => {
        const rect = element?.getBoundingClientRect();
        return rect ? {
          top: rect.top, right: rect.right, bottom: rect.bottom, left: rect.left,
          width: rect.width, height: rect.height,
        } : null;
      };
      const main = document.querySelector("main");
      const page = document.querySelector("main > .page");
      const pageStyle = page ? getComputedStyle(page) : null;
      const pageBox = box(page);
      const content = pageBox && pageStyle ? {
        left: pageBox.left + parseFloat(pageStyle.paddingInlineStart),
        right: pageBox.right - parseFloat(pageStyle.paddingInlineEnd),
        width: pageBox.width - parseFloat(pageStyle.paddingInlineStart) -
          parseFloat(pageStyle.paddingInlineEnd),
      } : null;
      const directProse = [...(page?.querySelectorAll(":scope > p:not(.eyebrow)") ?? [])]
        .find(visible);
      const introProse = [...(page?.querySelectorAll(":scope > .chapter-intro .lede") ?? [])]
        .find(visible);
      const prose = directProse ?? introProse;
      const evidence = [...(page?.querySelectorAll(":scope > .evidence-figure") ?? [])]
        .find(visible);
      const evidenceStyle = evidence ? getComputedStyle(evidence) : null;
      const evidenceBox = box(evidence);
      const evidenceHeader = evidence?.querySelector(":scope > .evidence-header");
      const evidenceCaption = evidence?.querySelector("figcaption");
      let next = evidence?.nextElementSibling ?? null;
      while (next && !visible(next)) next = next.nextElementSibling;
      const nextBox = box(next);
      const evidencePaddingEnd = evidenceStyle
        ? parseFloat(evidenceStyle.paddingBlockEnd)
        : null;
      const rail = document.querySelector(".rail");
      return {
        main: box(main),
        page: pageBox,
        content,
        prose: box(prose),
        evidence: evidenceBox,
        evidenceHeader: box(evidenceHeader),
        evidenceCaption: box(evidenceCaption),
        evidencePaddingEnd,
        evidenceMarginEnd: evidenceStyle
          ? parseFloat(evidenceStyle.marginBlockEnd)
          : null,
        evidenceExitGap: evidenceBox && nextBox && Number.isFinite(evidencePaddingEnd)
          ? nextBox.top - (evidenceBox.bottom - evidencePaddingEnd)
          : null,
        rail: visible(rail) ? box(rail) : null,
        tables: [...(page?.querySelectorAll(":scope > .table-wrap") ?? [])]
          .filter(visible).map(box),
        overflow: document.documentElement.scrollWidth -
          document.documentElement.clientWidth,
      };
    })()`);
  const wideReadingLayoutProblems = (snapshot, route) => {
    const problems = [];
    const centre = (part) => part ? (part.left + part.right) / 2 : NaN;
    if (
      !snapshot?.main || !snapshot?.page || !snapshot?.content || !snapshot?.prose ||
      !Number.isFinite(centre(snapshot.main)) ||
      Math.abs(centre(snapshot.main) - centre(snapshot.page)) > 1
    ) {
      problems.push(`${route} wide layout does not centre the reading page in main`);
    }
    if (
      !Number.isFinite(snapshot?.content?.width) ||
      !Number.isFinite(snapshot?.prose?.width) ||
      Math.abs(snapshot.content.width - snapshot.prose.width) > 1
    ) {
      problems.push(`${route} wide layout page is not the prose reading register`);
    }
    const evidence = snapshot?.evidence;
    const content = snapshot?.content;
    if (
      !evidence || !content || evidence.width <= content.width ||
      Math.abs((content.left - evidence.left) - (evidence.right - content.right)) > 1
    ) {
      problems.push(`${route} wide evidence does not expand symmetrically from the reading register`);
    }
    if (
      !snapshot?.evidenceHeader ||
      Math.abs(snapshot.evidenceHeader.left - content.left) > 1
    ) {
      problems.push(`${route} evidence heading does not return to the prose reading spine`);
    }
    if (
      !snapshot?.evidenceCaption ||
      Math.abs(snapshot.evidenceCaption.left - content.left) > 1
    ) {
      problems.push(`${route} evidence caption does not return to the prose reading spine`);
    }
    if (
      !Number.isFinite(snapshot?.evidencePaddingEnd) ||
      Math.abs(snapshot.evidencePaddingEnd) > 0.5
    ) {
      problems.push(
        `${route} evidence inherits ${snapshot?.evidencePaddingEnd}px of duplicate end padding`,
      );
    }
    if (
      !Number.isFinite(snapshot?.evidenceExitGap) ||
      !Number.isFinite(snapshot?.evidenceMarginEnd) ||
      Math.abs(snapshot.evidenceExitGap - snapshot.evidenceMarginEnd) > 1
    ) {
      problems.push(
        `${route} evidence exit gap is not governed by its end margin ` +
          `(${snapshot?.evidenceExitGap}px vs ${snapshot?.evidenceMarginEnd}px)`,
      );
    }
    for (const table of snapshot?.tables ?? []) {
      if (
        table.left < content.left - 1 || table.right > content.right + 1 ||
        (snapshot.rail && table.left < snapshot.rail.right - 1)
      ) {
        problems.push(`${route} direct table leaves the reading register or intersects the rail`);
      }
    }
    if (snapshot?.overflow !== 0) {
      problems.push(`${route} wide layout adds ${snapshot?.overflow}px horizontal overflow`);
    }
    return problems;
  };
  const recordTrendGuideGeometry = (snapshot, width, theme) => {
    const contentBoxes =
      " (chart content " + String(snapshot?.chartContentWidth) +
      "px, plot content " + String(snapshot?.plotContentWidth) + "px)";
    const height = snapshot?.plotHeight;
    const chartContentWidth = snapshot?.chartContentWidth;
    const plotContentWidth = snapshot?.plotContentWidth;
    const rootSize = snapshot?.transitionTypography?.rootSize;
    const enlargedTextNeedsKey =
      Number.isFinite(chartContentWidth) &&
      Number.isFinite(rootSize) &&
      chartContentWidth > 888 && chartContentWidth <= rootSize * 40;
    if (!Number.isFinite(height) || height < 320 || height > 520) {
      failures.push(
        "/trend/ @" + width + " " + theme +
          ": figure 1.1 plot height is outside 320–520px (" + String(height) + ")",
      );
    }
    if (snapshot?.seriesOcclusionCount !== 0) {
      failures.push(
        "/trend/ @" + width + " " + theme +
          ": the Taiwan standard annotation covers " +
          String(snapshot?.seriesOcclusionCount) + " sampled trend points" + contentBoxes,
      );
    }
    if (snapshot?.annotationOverlapCount !== 0) {
      failures.push(
        "/trend/ @" + width + " " + theme +
          ": Figure 1.1 has " + String(snapshot?.annotationOverlapCount) +
          " annotation-to-annotation collisions",
      );
    }
    if (snapshot?.plotBoundaryViolationCount !== 0) {
      failures.push(
        "/trend/ @" + width + " " + theme +
          ": Figure 1.1 has " + String(snapshot?.plotBoundaryViolationCount) +
          " annotations outside the plot boundary",
      );
    }
    if (snapshot?.horizontalOverflow !== 0) {
      failures.push(
        "/trend/ @" + width + " " + theme +
          ": the refined guide chart adds " +
          String(snapshot?.horizontalOverflow) + "px horizontal overflow",
      );
    }
    if (snapshot?.chartContainerName !== "trend-chart") {
      failures.push(
        "/trend/ @" + width + " " + theme +
          ": TrendChart root does not expose the trend-chart container" + contentBoxes,
      );
    }
    failures.push(...trendGuideCopyProblems(snapshot, width, theme));
    const who = snapshot?.whoPlacement;
    const labelCentre = who?.label ? (who.label.top + who.label.bottom) / 2 : NaN;
    const lineCentre = who?.line ? (who.line.top + who.line.bottom) / 2 : NaN;
    if (!enlargedTextNeedsKey && (
      !Number.isFinite(labelCentre) || !Number.isFinite(lineCentre) ||
      Math.abs(labelCentre - lineCentre) > 1
    )) {
      failures.push(
        "/trend/ @" + width + " " + theme +
          ": WHO label is not centred on the 5 μg/m³ guide",
      );
    }
    if (!enlargedTextNeedsKey && who?.backgroundAlpha !== 255) {
      failures.push(
        "/trend/ @" + width + " " + theme +
          ": WHO on-line label background is not opaque",
      );
    }
    if (
      who?.plotCount !== (enlargedTextNeedsKey ? 0 : 1) ||
      who?.keyCount !== (enlargedTextNeedsKey ? 1 : 0)
    ) {
      failures.push(
        "/trend/ @" + width + " " + theme +
          ": WHO guide label is missing or duplicated between plot and key",
      );
    }
    const whoCentre = who?.label ? (who.label.left + who.label.right) / 2 : NaN;
    if (!enlargedTextNeedsKey && (
      !Number.isFinite(snapshot?.expectedX2010) || Math.abs(whoCentre - snapshot.expectedX2010) > 1
    )) {
      failures.push(
        "/trend/ @" + width + " " + theme +
          ": WHO guide label is not anchored to 2010 on the chart x scale",
      );
    }
    const start = snapshot?.startPlacement;
    const marker = snapshot?.boxes?.startMarker;
    const note = snapshot?.boxes?.startNote;
    const leader = snapshot?.boxes?.startLeader;
    const markerCentreX = marker ? (marker.left + marker.right) / 2 : NaN;
    const markerCentreY = marker ? (marker.top + marker.bottom) / 2 : NaN;
    const noteCentreX = note ? (note.left + note.right) / 2 : NaN;
    const noteGap = marker && note ? marker.top - note.bottom : NaN;
    const leaderCentreX = leader ? (leader.left + leader.right) / 2 : NaN;
    const oldStandard = snapshot?.standardPathEndpoints?.["15"];
    const currentStandard = snapshot?.standardPathEndpoints?.["12"];
    const oldStandardStartsAt2012 =
      Number.isFinite(snapshot?.expectedX2012) &&
      Number.isFinite(oldStandard?.left) &&
      Math.abs(oldStandard.left - snapshot.expectedX2012) <= 1;
    const currentStandardStartsAt2024 =
      Number.isFinite(snapshot?.expectedX2024) &&
      Number.isFinite(currentStandard?.left) &&
      Math.abs(currentStandard.left - snapshot.expectedX2024) <= 1;
    const markerAt2012 =
      Number.isFinite(snapshot?.expectedX2012) &&
      Number.isFinite(markerCentreX) &&
      Math.abs(markerCentreX - snapshot.expectedX2012) <= 1 &&
      Number.isFinite(markerCentreY) &&
      Number.isFinite(oldStandard?.top) &&
      Math.abs(markerCentreY - oldStandard.top) <= 1;
    const correctStartText = start?.noteText === "2012.05 標準開始生效";
    const startLabelAnchored =
      correctStartText &&
      (!start?.noteVisible || (
        Number.isFinite(noteCentreX) &&
        Math.abs(noteCentreX - markerCentreX) <= 1 &&
        Number.isFinite(noteGap) &&
        noteGap >= 8 &&
        noteGap <= 12
      ));
    const startLeaderConnects =
      start?.leaderVisible === start?.noteVisible &&
      (!start?.leaderVisible || (
        Number.isFinite(leaderCentreX) &&
        Math.abs(leaderCentreX - markerCentreX) <= 1 &&
        Math.abs(leader.top - note.bottom) <= 1 &&
        Math.abs(leader.bottom - marker.top) <= 1
      ));
    const correctStartAria =
      start?.noteAriaLabel ===
      "2012-05-14 PM2.5 年均標準開始生效，標準為 15 微克/立方公尺";
    if (
      start?.markerYear !== "2012" ||
      !markerAt2012 ||
      !oldStandardStartsAt2012 ||
      !currentStandardStartsAt2024 ||
      !startLabelAnchored ||
      !startLeaderConnects ||
      !correctStartAria
    ) {
      failures.push(
        "/trend/ @" + width + " " + theme +
          ": Figure 1.1 does not keep the 2012 start milestone on its measured year anchors",
      );
    }
    if (!startLabelAnchored) {
      const startLabelFailures = [];
      if (!correctStartText) startLabelFailures.push("text");
      if (
        start?.noteVisible &&
        (!Number.isFinite(noteCentreX) || Math.abs(noteCentreX - markerCentreX) > 1)
      ) {
        startLabelFailures.push("horizontal anchor");
      }
      if (start?.noteVisible && (!Number.isFinite(noteGap) || noteGap < 8 || noteGap > 12)) {
        startLabelFailures.push("vertical gap");
      }
      failures.push(
        "/trend/ @" + width + " " + theme +
          ": Figure 1.1 start label has wrong " + startLabelFailures.join(", "),
      );
    }
    if (!startLeaderConnects) {
      failures.push(
        "/trend/ @" + width + " " + theme +
          ": Figure 1.1 start leader does not connect the note to the marker",
      );
    }
    const transition = snapshot?.transitionPlacement;
    const intermediateTimelineInKey =
      Number.isFinite(chartContentWidth) &&
      (chartContentWidth <= 888 || enlargedTextNeedsKey);
    const expectedTransitionPlotCount = intermediateTimelineInKey ? 0 : 1;
    const expectedTransitionKeyCount = intermediateTimelineInKey ? 1 : 0;
    const expectedTransitionKeySampleCount = intermediateTimelineInKey ? 1 : 0;
    const expectedStartNoteVisible =
      Number.isFinite(plotContentWidth) && plotContentWidth > 720 && !enlargedTextNeedsKey;
    const transitionVisibleCount =
      (transition?.plotCount ?? 0) + (transition?.keyCount ?? 0);
    if (
      !Number.isFinite(chartContentWidth) ||
      !Number.isFinite(plotContentWidth) ||
      transitionVisibleCount !== 1 ||
      !snapshot?.transitionVisible ||
      transition?.plotCount !== expectedTransitionPlotCount ||
      transition?.keyCount !== expectedTransitionKeyCount ||
      !start?.markerVisible ||
      start?.noteVisible !== expectedStartNoteVisible
    ) {
      failures.push(
        "/trend/ @" + width + " " + theme +
          ": Taiwan standard transition has " + transitionVisibleCount +
          " visible copies or is rendered in the wrong responsive region" + contentBoxes,
      );
    }
    if (
      transition?.keyOuterMarkCount !== 0 ||
      transition?.keyOldSampleCount !== expectedTransitionKeySampleCount ||
      transition?.keyCurrentSampleCount !== expectedTransitionKeySampleCount
    ) {
      failures.push(
        "/trend/ @" + width + " " + theme +
          ": transition key marks are outer " +
          String(transition?.keyOuterMarkCount) + ", old " +
          String(transition?.keyOldSampleCount) + ", current " +
          String(transition?.keyCurrentSampleCount) +
          " (expected outer 0, old/current " +
          String(expectedTransitionKeySampleCount) + ")" + contentBoxes,
      );
    }
    if (
      transition?.activeSampleCount !== 2 ||
      transition?.activeOldSampleCount !== 1 ||
      transition?.activeCurrentSampleCount !== 1 ||
      transition?.activeChangeSampleCount !== 0 ||
      transition?.activeOtherSampleCount !== 0
    ) {
      failures.push(
        "/trend/ @" + width + " " + theme +
          ": active Taiwan standard legend must expose only the 15 and 12 swatches",
      );
    }
    const activeSurface = intermediateTimelineInKey
      ? transition?.keySurface
      : transition?.plotSurface;
    const tokens = snapshot?.guideTokens;
    const fullLegendBorder = Object.values(activeSurface?.borders ?? {}).every((border) =>
      Math.abs(border?.width - 1) <= 0.01 &&
      border?.style === "solid" &&
      border?.colour === tokens?.line
    );
    const boundedLegend =
      activeSurface?.backgroundAlpha === 255 &&
      activeSurface?.background === tokens?.bgRaised &&
      fullLegendBorder &&
      activeSurface?.boxShadow === "none" &&
      activeSurface?.paddingInline >= 20 &&
      activeSurface?.paddingBlock >= 12;
    const correctLegendCopy =
      transition?.titleText === "台灣 PM2.5 年均標準" &&
      transition?.fromText === "2012.05.14 起生效 15" &&
      transition?.changeText === "2024.09.30 調降";
    // `.plot`, not `.plot-area`: the transition card is a legend rather than a
    // data-anchored annotation, and it is seated in the band `.plot` reserves
    // above its drawing box. See the boundary split in the snapshot above.
    const activeSurfaceContainer = intermediateTimelineInKey
      ? snapshot?.boxes?.chart
      : snapshot?.boxes?.plot;
    const activeSurfaceBox = activeSurface?.box;
    const activeSurfaceIsContained =
      activeSurfaceBox && activeSurfaceContainer &&
      activeSurfaceBox.left >= activeSurfaceContainer.left - 1 &&
      activeSurfaceBox.right <= activeSurfaceContainer.right + 1 &&
      activeSurfaceBox.top >= activeSurfaceContainer.top - 1 &&
      activeSurfaceBox.bottom <= activeSurfaceContainer.bottom + 1;
    if (!boundedLegend) {
      failures.push(
        "/trend/ @" + width + " " + theme +
          ": Taiwan standard transition is not an opaque bounded legend surface" + contentBoxes,
      );
    }
    if (!correctLegendCopy) {
      failures.push(
        "/trend/ @" + width + " " + theme +
          ": Taiwan standard transition does not use the approved legend copy",
      );
    }
    if (!activeSurfaceIsContained) {
      failures.push(
        "/trend/ @" + width + " " + theme +
          ": Taiwan standard legend surface leaves its active responsive region" + contentBoxes,
      );
    }
    // 2026-08-26 — the approved inset is 16px from `.plot`'s own top, which is a
    // full `--plot-cap` above the drawing box.
    //
    // It was 8% of the plot's height below that box, then 16px below it. Both
    // left the whole band above the note empty, on a figure whose line falls
    // away from that corner and never enters it: 0 of 500 sampled line points
    // land inside the note at 1120–1920. The band is the room `.plot` reserves
    // for the unit label and half the topmost tick, and on this figure both of
    // those live in the leftmost 107px, so its right-hand side is empty by
    // construction. The note now sits in it.
    //
    // Asserted against `.plot` rather than as a number, because the lift is
    // `calc(16px - var(--plot-cap))` cancelling a padding of `var(--plot-cap)`
    // — this checks the result the two produce together, so neither can drift
    // without the other. It cannot reach the key row, which ends at `.plot`'s
    // border box, 16px above where the note starts.
    if (!intermediateTimelineInKey) {
      const plot = snapshot?.boxes?.plot;
      const drawing = snapshot?.boxes?.plotArea;
      const note = snapshot?.boxes?.transitionNote;
      const topInset = plot && note ? note.top - plot.top : NaN;
      const rightInset = drawing && note ? drawing.right - note.right : NaN;
      if (
        !Number.isFinite(topInset) ||
        !Number.isFinite(rightInset) ||
        Math.abs(topInset - 16) > 1 ||
        Math.abs(rightInset - 16) > 1 ||
        note.left < plot.left ||
        note.bottom > plot.bottom
      ) {
        failures.push(
          "/trend/ @" + width + " " + theme +
            ": Taiwan standard annotation is not anchored inside the approved upper-right inset",
        );
      }
    }
    const colours = snapshot?.colours;
    if (colours?.whoTitle !== tokens?.who || colours?.whoValue !== tokens?.who) {
      failures.push(
        "/trend/ @" + width + " " + theme +
          ": WHO title and value do not both use the WHO semantic colour",
      );
    }
    if (
      snapshot?.valueBackgroundAlpha?.old !== 0 ||
      snapshot?.valueBackgroundAlpha?.current !== 0
    ) {
      failures.push(
        "/trend/ @" + width + " " + theme +
          ": 15 and 12 values still use decorative background fills",
      );
    }
    const expectedLevels = [
      { level: "15", stroke: tokens?.oldMark, weight: 3 },
      { level: "12", stroke: tokens?.currentMark, weight: 3 },
      { level: "5", stroke: tokens?.who, weight: 2 },
    ];
    const levelPaths = snapshot?.guidePaths?.filter((path) => path.kind === "level") ?? [];
    const renderedMarkStrokes = [
      ...(snapshot?.seriesStrokes ?? []).slice(0, 2),
      ...["15", "12", "5"].map(
        (level) => levelPaths.find((path) => path.level === level)?.stroke,
      ),
    ];
    if (
      renderedMarkStrokes.length !== 5 ||
      renderedMarkStrokes.some((stroke) => !stroke) ||
      new Set(renderedMarkStrokes).size !== 5
    ) {
      failures.push(
        "/trend/ @" + width + " " + theme +
          ": all-stations, balanced, old, current, and WHO rendered strokes are not pairwise distinct",
      );
    }
    const levelsMatch = expectedLevels.every((expected) =>
      levelPaths.some((path) =>
        path.level === expected.level &&
        path.stroke === expected.stroke &&
        Math.abs(path.weight - expected.weight) <= 0.01 &&
        path.dash === "8 5" &&
        path.cap === "round",
      ),
    );
    if (!levelsMatch) {
      failures.push(
        "/trend/ @" + width + " " + theme +
          ": 15 and 12 guides are not 3px while WHO remains 2px",
      );
    }
    const riser = snapshot?.guidePaths?.find((path) => path.kind === "riser");
    if (
      riser?.stroke !== tokens?.riser ||
      Math.abs(riser?.weight - 2) > 0.01 ||
      riser?.dash !== "none"
    ) {
      failures.push(
        "/trend/ @" + width + " " + theme +
          ": the 2024 riser is not a neutral 2px solid line",
      );
    }
    const samplesMatch = [
      [snapshot?.samples?.old, tokens?.oldMark],
      [snapshot?.samples?.current, tokens?.currentMark],
    ].every(([sample, stroke]) =>
      sample?.stroke === stroke &&
      Math.abs(sample?.weight - 3) <= 0.01 &&
      sample?.dash === "8 5" &&
      sample?.cap === "round",
    );
    if (!samplesMatch) {
      failures.push(
        "/trend/ @" + width + " " + theme +
          ": the 15 and 12 annotation rows lack matching 3px dashed samples",
      );
    }
    if (!intermediateTimelineInKey) {
      const typography = snapshot?.transitionTypography;
      if (
        !Number.isFinite(typography?.fontSize) ||
        !Number.isFinite(typography?.rootSize) ||
        typography.fontSize < typography.rootSize * 1.2 - 0.02 ||
        typography.fontSize > typography.rootSize * 1.4 + 0.02
      ) {
        failures.push(
          "/trend/ @" + width + " " + theme +
            ": Taiwan standard transition card is not enlarged to the approved type scale",
        );
      }
    }
  };
  if (18.99 + CSS_PX_SERIALIZATION_EPSILON >= 20 * 0.95) {
    failures.push("annotation ratio gate accepts 94.95% of body size");
  }
  if (17.2757 + CSS_PX_SERIALIZATION_EPSILON < 18.185 * 0.95) {
    failures.push("annotation ratio gate rejects an exact 95% value serialized to four decimals");
  }
  const totals = {
    nodes: 0,
    smallestAt375: Infinity,
    smallestAt1440: Infinity,
    annotationAt375: Infinity,
    collisions: 0,
    markCollisions: 0,
    hyphenSigns: 0,
    toolClashes: 0,
    readouts: 0,
    tableWraps: 0,
    tableScrollers: 0,
    focusChecks: 0,
    evidenceFocusChecks: 0,
    noScriptRoutes: 0,
    noScriptNativeFigures: 0,
    noScriptSecondaryDisclosures: 0,
    noScriptSqlDisclosures: 0,
    chapterOpeningChecks: 0,
    chapterEndingChecks: 0,
    zoomRoutes: 0,
  };

  const origin = `http://127.0.0.1:${PORT}`;
  if (!HEALTH_BROWSER_SELF_TEST && !FORECAST_BROWSER_SELF_TEST && !METHODS_BROWSER_SELF_TEST && !DATA_BROWSER_SELF_TEST && !EXPLORE_BROWSER_SELF_TEST) {
    console.log("site-quality stage: detection browser mutations");
    const detectionMutationFailures = await detectionBrowserMutationFailures(origin);
    if (DETECTION_BROWSER_SELF_TEST) {
      if (detectionMutationFailures.length) {
        for (const problem of detectionMutationFailures) console.log(`  FAIL: ${problem}`);
        return 1;
      }
      console.log("site quality detection browser mutation self-test passed");
      return 0;
    }
    for (const problem of detectionMutationFailures) {
      failures.push(`/detection/ browser mutation: ${problem}`);
    }
  }
  if (!FORECAST_BROWSER_SELF_TEST && !METHODS_BROWSER_SELF_TEST && !DATA_BROWSER_SELF_TEST && !EXPLORE_BROWSER_SELF_TEST) {
    console.log("site-quality stage: health browser mutations");
    const healthMutationFailures = await healthBrowserMutationFailures(origin);
    if (HEALTH_BROWSER_SELF_TEST) {
      if (healthMutationFailures.length) {
        for (const problem of healthMutationFailures) console.log(`  FAIL: ${problem}`);
        return 1;
      }
      console.log("site quality health browser mutation self-test passed");
      return 0;
    }
    for (const problem of healthMutationFailures) {
      failures.push(`/health/ browser mutation: ${problem}`);
    }
  }
  if (!METHODS_BROWSER_SELF_TEST && !DATA_BROWSER_SELF_TEST && !EXPLORE_BROWSER_SELF_TEST) {
    console.log("site-quality stage: forecast browser mutations");
    const forecastMutationFailures = await forecastBrowserMutationFailures(origin);
    if (FORECAST_BROWSER_SELF_TEST) {
      if (forecastMutationFailures.length) {
        for (const problem of forecastMutationFailures) console.log(`  FAIL: ${problem}`);
        return 1;
      }
      console.log("site quality forecast browser mutation self-test passed");
      return 0;
    }
    for (const problem of forecastMutationFailures) {
      failures.push(`/forecast/ browser mutation: ${problem}`);
    }
  }
  if (!DATA_BROWSER_SELF_TEST && !EXPLORE_BROWSER_SELF_TEST) {
    console.log("site-quality stage: methods browser mutations");
    const methodsMutationFailures = await methodsBrowserMutationFailures(origin);
    if (METHODS_BROWSER_SELF_TEST) {
      if (methodsMutationFailures.length) {
        for (const problem of methodsMutationFailures) console.log(`  FAIL: ${problem}`);
        return 1;
      }
      console.log("site quality methods browser mutation self-test passed");
      return 0;
    }
    for (const problem of methodsMutationFailures) {
      failures.push(`/methods/ browser mutation: ${problem}`);
    }
  }
  if (!EXPLORE_BROWSER_SELF_TEST) {
    console.log("site-quality stage: data browser mutations");
    const dataMutationFailures = await dataBrowserMutationFailures(origin);
    if (DATA_BROWSER_SELF_TEST) {
      if (dataMutationFailures.length) {
        for (const problem of dataMutationFailures) console.log(`  FAIL: ${problem}`);
        return 1;
      }
      console.log("site quality data browser mutation self-test passed");
      return 0;
    }
    for (const problem of dataMutationFailures) {
      failures.push(`/data/ browser mutation: ${problem}`);
    }
  }
  console.log("site-quality stage: explore browser mutations and production states");
  const exploreMutationFailures = await explorerBrowserMutationFailures(origin);
  if (EXPLORE_BROWSER_SELF_TEST) {
    if (exploreMutationFailures.length) {
      for (const problem of exploreMutationFailures) console.log(`  FAIL: ${problem}`);
      return 1;
    }
    console.log("site quality explore browser mutation self-test passed");
    return 0;
  }
  for (const problem of exploreMutationFailures) {
    failures.push(`/explore/ browser mutation: ${problem}`);
  }
  console.log("site-quality stage: theme and storage contract");
  await send("Emulation.setDeviceMetricsOverride", {
    width: 1440,
    height: 900,
    deviceScaleFactor: 1,
    mobile: false,
  });
  await send("Storage.clearDataForOrigin", { origin, storageTypes: "local_storage" });
  await send("Emulation.setEmulatedMedia", {
    media: "",
    features: [{ name: "prefers-color-scheme", value: "dark" }],
  });
  await send("Page.navigate", { url: `${origin}/` });
  if (!(await settled(evaluate))) {
    failures.push("theme preflight page never finished styling");
  } else {
    const firstVisit = await evaluate(`(() => ({
      explicitTheme: document.documentElement.dataset.theme ?? null,
      resolvedTheme: document.documentElement.dataset.theme ??
        (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"),
      colourScheme: getComputedStyle(document.documentElement).colorScheme,
      metaScheme: document.querySelector('meta[name="color-scheme"]')?.getAttribute("content"),
      toggleCount: document.querySelectorAll("[data-theme-toggle]").length,
      stableNames: [...document.querySelectorAll("[data-theme-toggle]")].every(
        (button) => button.getAttribute("aria-label") === "深色模式（深色／淺色）",
      ),
      chromeSynchronized:
        document.querySelector("[data-theme-color]")?.getAttribute("content") === "#f5f7f5" &&
        document.querySelector("[data-theme-icon]")?.getAttribute("href") ===
          document.querySelector("[data-theme-icon]")?.getAttribute("data-light"),
    }))()`);
    if (firstVisit?.explicitTheme !== "light") {
      failures.push(
        `first visit under an OS dark preference resolved to ${firstVisit?.resolvedTheme ?? "unknown"} instead of light`,
      );
    }
    if (firstVisit?.colourScheme !== "light" || firstVisit?.metaScheme !== "light") {
      failures.push("first visit left native browser controls under the OS colour scheme");
    }
    if (firstVisit?.toggleCount !== 2) {
      failures.push(
        firstVisit?.toggleCount
          ? `expected two [data-theme-toggle] controls, found ${firstVisit.toggleCount}`
          : "no [data-theme-toggle] control was rendered",
      );
    } else if (!firstVisit?.stableNames) {
      failures.push("theme toggle accessible names do not state a stable pressed-state meaning");
    } else if (!firstVisit?.chromeSynchronized) {
      failures.push("theme-color and favicon did not retain the first-visit light theme");
    } else {
      const toggled = await evaluate(`(() => {
        const button = document.querySelector("[data-theme-toggle]");
        button.click();
        const buttons = [...document.querySelectorAll("[data-theme-toggle]")];
        const colour = document.querySelector("[data-theme-color]");
        const icon = document.querySelector("[data-theme-icon]");
        return {
          theme: document.documentElement.dataset.theme ?? null,
          pressed: button.getAttribute("aria-pressed"),
          stored: localStorage.getItem("twair-theme"),
          buttonsSynchronized: buttons.every((item) => item.getAttribute("aria-pressed") === "true"),
          labelsSynchronized: buttons.every(
            (item) => item.querySelector(".theme-toggle-label")?.textContent.trim() === "淺色",
          ),
          stableNames: buttons.every(
            (item) => item.getAttribute("aria-label") === "深色模式（深色／淺色）",
          ),
          colourScheme: getComputedStyle(document.documentElement).colorScheme,
          metaScheme: document.querySelector('meta[name="color-scheme"]')?.getAttribute("content"),
          chromeSynchronized:
            colour?.getAttribute("content") === colour?.getAttribute("data-dark") &&
            icon?.getAttribute("href") === icon?.getAttribute("data-dark"),
        };
      })()`);
      if (toggled?.theme !== "dark") {
        failures.push(`manual theme toggle resolved to ${toggled?.theme ?? "unknown"} instead of dark`);
      }
      if (toggled?.pressed !== "true") {
        failures.push("manual theme toggle did not set aria-pressed to true");
      }
      if (toggled?.stored !== "dark") {
        failures.push("manual dark choice was not stored as twair-theme");
      }
      if (!toggled?.buttonsSynchronized || !toggled?.labelsSynchronized) {
        failures.push("theme toggle controls did not stay synchronized");
      }
      if (!toggled?.stableNames) {
        failures.push("theme toggle accessible names changed with the visible action label");
      }
      if (toggled?.colourScheme !== "dark" || toggled?.metaScheme !== "dark") {
        failures.push("manual dark choice did not update the native browser colour scheme");
      }
      if (!toggled?.chromeSynchronized) {
        failures.push("theme-color and favicon did not follow the manual dark choice");
      }

      await send("Page.reload", { ignoreCache: true });
      if (!(await settled(evaluate))) {
        failures.push("theme persistence reload never finished styling");
      } else {
        const reloaded = await evaluate(`({
          theme: document.documentElement.dataset.theme,
          toggleCount: document.querySelectorAll("[data-theme-toggle]").length,
          stableNames: [...document.querySelectorAll("[data-theme-toggle]")].every(
            (button) => button.getAttribute("aria-label") === "深色模式（深色／淺色）",
          ),
          controlsSynchronized: [...document.querySelectorAll("[data-theme-toggle]")].every(
            (button) =>
              button.getAttribute("aria-pressed") === "true" &&
              button.querySelector(".theme-toggle-label")?.textContent.trim() === "淺色",
          ),
          chromeSynchronized:
            document.querySelector("[data-theme-color]")?.getAttribute("content") ===
              document.querySelector("[data-theme-color]")?.getAttribute("data-dark") &&
            document.querySelector("[data-theme-icon]")?.getAttribute("href") ===
              document.querySelector("[data-theme-icon]")?.getAttribute("data-dark"),
        })`);
        if (reloaded?.theme !== "dark") failures.push("manual dark choice did not survive a reload");
        if (!reloaded?.stableNames) failures.push("theme toggle accessible names changed after reload");
        if (
          reloaded?.toggleCount !== 2 ||
          !reloaded?.controlsSynchronized ||
          !reloaded?.chromeSynchronized
        ) {
          failures.push("theme controls and browser chrome did not synchronize after reload");
        }
      }

      await send("Page.navigate", { url: `${origin}/trend/` });
      if (!(await settled(evaluate))) {
        failures.push("theme persistence navigation never finished styling");
      } else {
        const navigated = await evaluate(`({
          theme: document.documentElement.dataset.theme,
          toggleCount: document.querySelectorAll("[data-theme-toggle]").length,
          stableNames: [...document.querySelectorAll("[data-theme-toggle]")].every(
            (button) => button.getAttribute("aria-label") === "深色模式（深色／淺色）",
          ),
          controlsSynchronized: [...document.querySelectorAll("[data-theme-toggle]")].every(
            (button) =>
              button.getAttribute("aria-pressed") === "true" &&
              button.querySelector(".theme-toggle-label")?.textContent.trim() === "淺色",
          ),
          chromeSynchronized:
            document.querySelector("[data-theme-color]")?.getAttribute("content") ===
              document.querySelector("[data-theme-color]")?.getAttribute("data-dark") &&
            document.querySelector("[data-theme-icon]")?.getAttribute("href") ===
              document.querySelector("[data-theme-icon]")?.getAttribute("data-dark"),
        })`);
        if (navigated?.theme !== "dark") failures.push("manual dark choice did not persist to /trend/");
        if (!navigated?.stableNames) {
          failures.push("theme toggle accessible names changed after navigation");
        }
        if (
          navigated?.toggleCount !== 2 ||
          !navigated?.controlsSynchronized ||
          !navigated?.chromeSynchronized
        ) {
          failures.push("theme controls and browser chrome did not synchronize after navigation");
        }
      }
    }
  }

  const invalidSeed = await evaluate(`(() => {
    localStorage.setItem("twair-theme", "sepia");
    return localStorage.getItem("twair-theme");
  })()`);
  if (invalidSeed !== "sepia") {
    failures.push("invalid stored theme preflight did not seed its control value");
  }
  await send("Emulation.setEmulatedMedia", {
    media: "",
    features: [{ name: "prefers-color-scheme", value: "dark" }],
  });
  await send("Page.navigate", { url: `${origin}/` });
  if (!(await settled(evaluate))) {
    failures.push("invalid stored theme preflight page never finished styling");
  } else if ((await evaluate("document.documentElement.dataset.theme")) !== "light") {
    failures.push("an invalid stored theme was accepted instead of resolving to light");
  }

  await send("Storage.clearDataForOrigin", { origin, storageTypes: "local_storage" });
  await send("Page.enable");
  const blockedStorage = await send("Page.addScriptToEvaluateOnNewDocument", {
    source: `globalThis.__twairStorageBlocked = true;
    for (const method of ["getItem", "setItem"]) {
      Object.defineProperty(Storage.prototype, method, {
        configurable: true,
        value() { throw new DOMException("storage blocked", "SecurityError"); },
      });
    }`,
  });
  await send("Page.navigate", { url: `${origin}/?storage-blocked=1` });
  if (!(await settled(evaluate))) {
    failures.push("blocked-storage theme preflight page never finished styling");
  } else {
    const blocked = await evaluate(`(() => {
      let storageReadThrows = false;
      let storageWriteThrows = false;
      try { localStorage.getItem("twair-theme"); } catch { storageReadThrows = true; }
      try { localStorage.setItem("twair-theme", "dark"); } catch { storageWriteThrows = true; }
      const initial = document.documentElement.dataset.theme ?? null;
      document.querySelector("[data-theme-toggle]")?.click();
      const buttons = [...document.querySelectorAll("[data-theme-toggle]")];
      const colour = document.querySelector("[data-theme-color]");
      const icon = document.querySelector("[data-theme-icon]");
      return {
        injectionRan: globalThis.__twairStorageBlocked === true,
        storageReadThrows,
        storageWriteThrows,
        initial,
        toggled: document.documentElement.dataset.theme ?? null,
        controlsSynchronized:
          buttons.length === 2 &&
          buttons.every(
            (button) =>
              button.getAttribute("aria-pressed") === "true" &&
              button.querySelector(".theme-toggle-label")?.textContent.trim() === "淺色",
          ),
        colourScheme: getComputedStyle(document.documentElement).colorScheme,
        metaScheme: document.querySelector('meta[name="color-scheme"]')?.getAttribute("content"),
        chromeSynchronized:
          colour?.getAttribute("content") === colour?.getAttribute("data-dark") &&
          icon?.getAttribute("href") === icon?.getAttribute("data-dark"),
      };
    })()`);
    if (
      !blocked?.injectionRan ||
      !blocked?.storageReadThrows ||
      !blocked?.storageWriteThrows ||
      blocked?.initial !== "light" ||
      blocked?.toggled !== "dark" ||
      !blocked?.controlsSynchronized ||
      blocked?.colourScheme !== "dark" ||
      blocked?.metaScheme !== "dark" ||
      !blocked?.chromeSynchronized
    ) {
      failures.push(
        "storage errors prevented the light default or in-page theme toggle " +
        `(registration=${blockedStorage.result?.identifier ?? blockedStorage.error?.message ?? "unknown"}, ` +
          `injected=${blocked?.injectionRan ?? "unknown"}, ` +
          `readThrows=${blocked?.storageReadThrows ?? "unknown"}, ` +
          `writeThrows=${blocked?.storageWriteThrows ?? "unknown"}, ` +
          `initial=${blocked?.initial ?? "unknown"}, ` +
          `toggled=${blocked?.toggled ?? "unknown"}, ` +
          `controls=${blocked?.controlsSynchronized ?? "unknown"}, ` +
          `scheme=${blocked?.colourScheme ?? "unknown"}/${blocked?.metaScheme ?? "unknown"}, ` +
          `chrome=${blocked?.chromeSynchronized ?? "unknown"})`,
      );
    }
  }
  await send("Page.removeScriptToEvaluateOnNewDocument", {
    identifier: blockedStorage.result?.identifier,
  });

  await send("Storage.clearDataForOrigin", { origin, storageTypes: "local_storage" });
  await send("Emulation.setDeviceMetricsOverride", {
    width: 375,
    height: 800,
    deviceScaleFactor: 1,
    mobile: true,
  });
  console.log("site-quality stage: no-JavaScript routes");
  for (const route of ROUTES) {
    if (
      !(await navigateWithoutPageScripts(send, waitForEvent, `${origin}${route}`, () =>
        settled(evaluate),
      ))
    ) {
      failures.push(`${route}: no-JavaScript page never finished styling`);
      continue;
    }
    if (route === "/") {
      for (const problem of await homepageFirstViewport()) {
        failures.push(`${route} @375x800 no-JavaScript light: ${problem}`);
      }
    }
    const noScript = await evaluate(`(() => {
      const visible = (element) => {
        const style = getComputedStyle(element);
        const rect = element.getBoundingClientRect();
        return style.display !== "none" && style.visibility !== "hidden" &&
          rect.width > 0 && rect.height > 0;
      };
      // 2026-08-03 — a caption can keep a figure shell visible, and borders can
      // keep an opened disclosure shell taller than its summary after every
      // body child is hidden. Readability therefore comes from the body child
      // itself, never shell geometry or textContent inherited from hidden DOM.
      const hasVisibleMeaningfulContent = (element) => {
        if (!visible(element)) return false;
        const media = [
          ...(element.matches("svg, canvas, img, picture, video") ? [element] : []),
          ...element.querySelectorAll("svg, canvas, img, picture, video"),
        ];
        if (media.some(visible)) return true;
        const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
        for (let node = walker.nextNode(); node; node = walker.nextNode()) {
          const parent = node.parentElement;
          if (
            node.nodeValue.trim() && parent &&
            !parent.closest("script, style, template") && visible(parent)
          ) return true;
        }
        return false;
      };
      for (const disclosure of document.querySelectorAll("details")) disclosure.open = true;
      const figures = [...document.querySelectorAll("main figure")];
      const figureStates = figures.map((figure) => {
        const caption = figure.querySelector(":scope > figcaption");
        const body = [...figure.children].filter(
          (child) => !child.matches("figcaption, script, style, template"),
        );
        return {
          visible: visible(figure),
          hasCaption: Boolean(caption),
          readableBody: body.some(hasVisibleMeaningfulContent),
          readableCaption: Boolean(caption && hasVisibleMeaningfulContent(caption)),
        };
      });
      const disclosures = [...document.querySelectorAll("main details")];
      const readableDisclosure = (disclosure) => {
        const summary = disclosure.querySelector(":scope > summary");
        const body = [...disclosure.children].filter(
          (child) => child !== summary && !child.matches("script, style, template"),
        );
        return Boolean(
          summary && visible(disclosure) && visible(summary) && summary.textContent.trim() &&
          disclosure.open && body.some(hasVisibleMeaningfulContent)
        );
      };
      const secondaryDisclosures = disclosures.filter((disclosure) => !disclosure.matches(".sql-panel"));
      const sqlDisclosures = disclosures.filter((disclosure) => disclosure.matches(".sql-panel"));
      const explorerNotice = document.querySelector("#explore .explorer-nojs");
      const explorerRun = document.querySelector("#explore #run");
      const intro = document.querySelector("main .chapter-intro");
      const introHeadings = intro
        ? [...intro.querySelectorAll(":scope > h1")].filter(visible) : [];
      const introTheses = intro
        ? [...intro.querySelectorAll(":scope > .chapter-thesis")].filter(visible) : [];
      const introHeading = introHeadings[0] ?? null;
      const introThesis = introTheses[0] ?? null;
      return {
        theme: document.documentElement.dataset.theme ?? null,
        hasJs: document.documentElement.classList.contains("has-js"),
        visibleToggles: [...document.querySelectorAll("[data-theme-toggle]")].filter(visible).length,
        startLinks: [...document.querySelectorAll("nav.start-here a")].filter(visible).length,
        chapterLinks: [...document.querySelectorAll("ol.toc a")].filter(visible).length,
        intro: {
          container: Boolean(intro && visible(intro)),
          heading: Boolean(introHeadings.length === 1 && introHeading.textContent.trim()),
          thesis: Boolean(
            introTheses.length === 1 && introThesis.textContent.trim() && introHeading &&
            (introHeading.compareDocumentPosition(introThesis) &
              Node.DOCUMENT_POSITION_FOLLOWING)
          ),
        },
        figures: figures.length,
        visibleFigures: figureStates.filter((state) => state.visible).length,
        visibleFigureBodies: figureStates.filter((state) => state.readableBody).length,
        captions: figureStates.filter((state) => state.hasCaption).length,
        readableCaptions: figureStates.filter((state) => state.readableCaption).length,
        secondaryDisclosures: secondaryDisclosures.length,
        readableSecondaryDisclosures: secondaryDisclosures.filter(readableDisclosure).length,
        sqlDisclosures: sqlDisclosures.length,
        readableSqlDisclosures: sqlDisclosures.filter(readableDisclosure).length,
        tables: [...document.querySelectorAll("main table")].filter(visible).length,
        downloads: [...document.querySelectorAll("main a[download]")].filter(visible).length,
        explorerInactive: Boolean(
          explorerNotice && visible(explorerNotice) &&
          (!explorerRun || !visible(explorerRun) || explorerRun.disabled)
        ),
        mainText: document.querySelector("main")?.innerText
          .replace(/\\s+/g, " ").trim() ?? "",
        mainSourceText: document.querySelector("main")?.textContent ?? "",
        detectionEstimateTable: ${DETECTION_ESTIMATE_TABLE_PROBE},
        sourcesAtlas: ${SOURCES_ATLAS_STATE_PROBE},
      };
    })()`);
    totals.noScriptRoutes += 1;
    if (noScript?.theme !== "light" || noScript?.hasJs) {
      failures.push(`${route}: no-JavaScript document did not retain its static light default`);
    }
    if (noScript?.visibleToggles) {
      failures.push(`${route}: theme toggle controls remain visible without JavaScript`);
    }
    if (noScript?.mainSourceText?.includes("——")) {
      failures.push(`${route}: main copy still contains a double em dash`);
    }
    if (route === "/") {
      if (noScript?.startLinks !== 4 || noScript?.chapterLinks !== 10) {
        failures.push(
          `/: no-JavaScript homepage paths are incomplete ` +
            `(start=${noScript?.startLinks ?? "unknown"}, chapters=${noScript?.chapterLinks ?? "unknown"})`,
        );
      }
      if (
        noScript?.mainText?.includes("剩下的才是排放") ||
        !noScript?.mainText?.includes("剩餘趨勢不能直接等同排放變化")
      ) {
        failures.push("/: chapter-one summary equates the normalised residual with emissions");
      }
      const boundary = await semanticBoundarySnapshot("[data-homepage-station-type-boundary]");
      for (const problem of homepageStationTypeBoundaryProblems(boundary)) {
        failures.push(`/: no-JavaScript ${problem}`);
      }
    } else {
      const incompleteIntro = ["container", "heading", "thesis"].filter(
        (part) => !noScript?.intro?.[part],
      );
      if (incompleteIntro.length) {
        failures.push(
          `${route}: no-JavaScript chapter intro has unreadable parts ` +
            `(${incompleteIntro.join(", ") || "unknown"})`,
        );
      }
    }
    if (route === "/trend/") {
      if (
        noScript?.mainText?.includes("排放本身") ||
        !noScript?.mainText?.includes("不是直接量測排放")
      ) {
        failures.push("/trend/: weather-normalised comparison is described as direct emissions");
      }
      const boundary = await semanticBoundarySnapshot("[data-deweather-contrast-boundary]");
      for (const problem of deweatherContrastBoundaryProblems(boundary)) {
        failures.push(`/trend/: no-JavaScript ${problem}`);
      }
      const pickerState = await evaluate(`(async () => {
        const chart = [...document.querySelectorAll("main .evidence-figure")][2];
        const switches = [...(chart?.querySelectorAll("[data-series-switch]") ?? [])];
        const pills = [...(chart?.querySelectorAll(".key-pill") ?? [])];
        const paths = [...(chart?.querySelectorAll("path.plot-line") ?? [])];
        const reset = chart?.querySelector(".key-reset") ?? null;
        const visible = (element) => {
          if (!element) return false;
          const style = getComputedStyle(element);
          const rect = element.getBoundingClientRect();
          return style.display !== "none" && style.visibility !== "hidden" &&
            rect.width > 0 && rect.height > 0;
        };
        if (
          !chart || switches.length !== 8 || pills.length !== 8 ||
          paths.length !== 8 || !reset
        ) return null;
        const resetVisibleBeforeFiltering = visible(reset);
        pills[0].click();
        pills[1].click();
        await new Promise((resolve) => setTimeout(resolve, 180));
        const filtered = {
          checked: switches.filter((input) => input.checked).length,
          displays: paths.map((path) => getComputedStyle(path).display),
        };
        reset.focus();
        reset.click();
        return {
          resetVisibleBeforeFiltering,
          filtered,
          restored: {
            checked: switches.filter((input) => input.checked).length,
            resetVisible: visible(reset),
            focusStayedOnReset: document.activeElement === reset,
          },
        };
      })()`);
      const pickerProblems = trendNoScriptPickerProblems(pickerState);
      if (pickerProblems.length) {
        failures.push(
          `/trend/: no-JavaScript air-zone picker did not round-trip ` +
            `${pickerProblems.join(", ")} ${JSON.stringify(pickerState)}`,
        );
      }
    }
    if (route === "/sources/") {
      for (const problem of sourcesClaimBoundaryProblems(noScript?.mainText ?? "")) {
        failures.push(`${route}: no-JavaScript ${problem}`);
      }
      const atlas = noScript?.sourcesAtlas
        ? { ...noScript.sourcesAtlas, skipPhoneEntry: true }
        : null;
      for (const problem of sourcesAtlasProblems(atlas, 375, 800, { noScript: true })) {
        failures.push(`${route}: no-JavaScript ${problem}`);
      }
    }
    if (route === "/detection/") {
      for (const problem of detectionClaimBoundaryProblems(noScript?.mainText ?? "")) {
        failures.push(`${route}: no-JavaScript ${problem}`);
      }
      for (const problem of detectionEstimateTableProblems(noScript?.detectionEstimateTable)) {
        failures.push(`${route}: no-JavaScript ${problem}`);
      }
      const detectionState = await detectionLimitationBriefSnapshot("no-js");
      for (const problem of detectionLimitationBriefProblems(
        detectionState,
        EXPECTED_DETECTION_EVENTS,
        detectionState?.viewport,
      )) {
        failures.push(`${route}: ${problem}`);
      }
    }
    if (route === "/health/") {
      const healthState = await healthAssumptionLedgerSnapshot("no-js");
      for (const problem of healthAssumptionLedgerProblems(
        healthState,
        EXPECTED_HEALTH_EVIDENCE,
        healthState?.viewport,
      )) {
        failures.push(`${route}: ${problem}`);
      }
    }
    if (route === "/forecast/") {
      const forecastState = await forecastHorizonDecisionSnapshot("no-js");
      for (const problem of forecastHorizonDecisionProblems(
        forecastState,
        EXPECTED_FORECAST_EVIDENCE,
        forecastState?.viewport,
      )) {
        failures.push(`${route}: ${problem}`);
      }
    }
    if (route === "/methods/") {
      const methodsState = await methodsCaseIndexSnapshot("no-js");
      for (const problem of methodsCaseIndexProblems(methodsState, methodsState?.viewport)) {
        failures.push(`${route}: ${problem}`);
      }
    }
    if (route === "/data/") {
      const dataState = await dataProvenanceRegisterSnapshot("no-js");
      for (const problem of dataProvenanceRegisterProblems(dataState, dataState?.viewport)) {
        failures.push(`${route}: ${problem}`);
      }
    }
    if (route === "/explore/") {
      const explorerState = await explorerGuidedWorkspaceSnapshot("no-js");
      for (const problem of explorerGuidedWorkspaceProblems(
        explorerState,
        { width: 375, height: 800 },
      )) {
        failures.push(`${route}: ${problem}`);
      }
    }
    const conceptState = await conceptDiagramSnapshot();
    for (const problem of conceptDiagramProblems(
      conceptState,
      STATIC_CONCEPT_DIAGRAMS.get(route),
      { width: 375, height: 800 },
    )) {
      failures.push(`${route}: no-JavaScript ${problem}`);
    }
    if (HISTORICAL_STATION_ROUTES.has(route)) {
      for (const problem of historicalStationCopyProblems(route, noScript?.mainText ?? "")) {
        failures.push(`${route}: no-JavaScript ${problem}`);
      }
    }
    const expectedFigures = STATIC_NATIVE_FIGURES.get(route);
    totals.noScriptNativeFigures += noScript?.figures ?? 0;
    if (
      noScript?.figures !== expectedFigures ||
      noScript?.visibleFigures !== expectedFigures ||
      noScript?.visibleFigureBodies !== expectedFigures ||
      noScript?.captions !== expectedFigures ||
      noScript?.readableCaptions !== expectedFigures
    ) {
      failures.push(
        `${route}: no-JavaScript native figure inventory is incomplete ` +
          `(expected=${expectedFigures}, DOM=${noScript?.figures ?? "unknown"}, ` +
          `visible=${noScript?.visibleFigures ?? "unknown"}, ` +
          `bodies=${noScript?.visibleFigureBodies ?? "unknown"}, ` +
          `captions=${noScript?.captions ?? "unknown"}, ` +
          `readable=${noScript?.readableCaptions ?? "unknown"})`,
      );
    }
    const expectedSecondary = STATIC_SECONDARY_DISCLOSURES.get(route);
    totals.noScriptSecondaryDisclosures += noScript?.secondaryDisclosures ?? 0;
    if (
      noScript?.secondaryDisclosures !== expectedSecondary ||
      noScript?.readableSecondaryDisclosures !== expectedSecondary
    ) {
      failures.push(
        `${route}: no-JavaScript secondary disclosure inventory is incomplete ` +
          `(expected=${expectedSecondary}, DOM=${noScript?.secondaryDisclosures ?? "unknown"}, ` +
          `readable=${noScript?.readableSecondaryDisclosures ?? "unknown"})`,
      );
    }
    const expectedSql = STATIC_SQL_DISCLOSURES.get(route);
    totals.noScriptSqlDisclosures += noScript?.sqlDisclosures ?? 0;
    if (
      noScript?.sqlDisclosures !== expectedSql ||
      noScript?.readableSqlDisclosures !== expectedSql
    ) {
      failures.push(
        `${route}: no-JavaScript SQL disclosure inventory is incomplete ` +
          `(expected=${expectedSql}, DOM=${noScript?.sqlDisclosures ?? "unknown"}, ` +
          `readable=${noScript?.readableSqlDisclosures ?? "unknown"})`,
      );
    }
    if (route === "/data/" && (!noScript?.tables || !noScript?.downloads)) {
      failures.push("/data/: no-JavaScript download table or links are unavailable");
    }
    if (route === "/explore/" && !noScript?.explorerInactive) {
      failures.push("/explore/: no-JavaScript Explorer does not identify itself as inactive");
    }
  }
  if (totals.noScriptNativeFigures !== EXPECTED_NATIVE_FIGURES) {
    failures.push(
      `no-JavaScript native figure inventory totals ${totals.noScriptNativeFigures}, ` +
        `expected ${EXPECTED_NATIVE_FIGURES}`,
    );
  }
  if (
    totals.noScriptSecondaryDisclosures === 0 ||
    totals.noScriptSecondaryDisclosures !== EXPECTED_SECONDARY_DISCLOSURES
  ) {
    failures.push(
      `no-JavaScript secondary disclosure inventory totals ` +
        `${totals.noScriptSecondaryDisclosures}, expected ${EXPECTED_SECONDARY_DISCLOSURES}`,
    );
  }
  /*
   * Zero is now the correct total, so the `=== 0` guard has to go with it.
   *
   * It was there to catch a selector that had stopped matching — a count of
   * zero used to mean the check had lost its subject rather than passed. There
   * is no subject any more: chapter 9 shows no query, and what this inventory
   * holds is that none comes back.
   */
  if (totals.noScriptSqlDisclosures !== EXPECTED_SQL_DISCLOSURES) {
    failures.push(
      `no-JavaScript SQL disclosure inventory totals ${totals.noScriptSqlDisclosures}, ` +
        `expected ${EXPECTED_SQL_DISCLOSURES}`,
    );
  }
  console.log("site-quality stage: no-JavaScript homepage acceptance");
  for (const [width, height] of [
    [390, 844],
    [768, 1024],
    [1280, 720],
    [1440, 900],
    [1920, 1000],
  ]) {
    await send("Emulation.setDeviceMetricsOverride", {
      width,
      height,
      deviceScaleFactor: 1,
      mobile: width < 500,
    });
    await send("Page.navigate", { url: `${origin}/data/` });
    if (!(await settled(evaluate, 8000, `/ @${width}px no-JavaScript preflight`))) {
      failures.push(
        `/ @${width}x${height} no-JavaScript light: preflight page never finished styling`,
      );
      continue;
    }
    await send("Storage.clearDataForOrigin", { origin, storageTypes: "local_storage" });
    if (
      !(await navigateWithoutPageScripts(send, waitForEvent, `${origin}/`, () =>
        settled(evaluate, 8000, `/ @${width}px no-JavaScript`),
      ))
    ) {
      failures.push(`/ @${width}x${height} no-JavaScript light: page never finished styling`);
      continue;
    }
    for (const problem of await homepageFirstViewport()) {
      failures.push(`/ @${width}x${height} no-JavaScript light: ${problem}`);
    }
    if (width === 390) {
      for (const problem of await homepageStructureProblems({ enhanced: false })) {
        failures.push(`/ @${width}x${height} no-JavaScript light: ${problem}`);
      }
      for (const problem of await chapterIndexProblems()) {
        failures.push(`/ @${width}x${height} no-JavaScript light: ${problem}`);
      }
    }
  }
  console.log("site-quality stage: shared chapter intent index");
  await send("Page.setScriptExecutionDisabled", { value: false });
  await send("Page.navigate", { url: `${origin}/404.html` });
  if (!(await settled(evaluate, 8000, "/404.html chapter intent index"))) {
    failures.push("/404.html: page never finished styling");
  } else {
    for (const problem of await chapterIndexProblems()) {
      failures.push(`/404.html: ${problem}`);
    }
  }
  console.log("site-quality stage: chapter opening matrix");
  await send("Storage.clearDataForOrigin", { origin, storageTypes: "local_storage" });
  await send("Emulation.setEmulatedMedia", {
    media: "",
    features: [{ name: "prefers-color-scheme", value: "light" }],
  });
  for (const { width, height } of CHAPTER_OPENING_VIEWPORTS) {
    await send("Emulation.setDeviceMetricsOverride", {
      width,
      height,
      deviceScaleFactor: 1,
      mobile: width < 500,
    });
    for (const route of CHAPTER_ROUTES) {
      await send("Page.navigate", { url: `${origin}${route}` });
      if (!(await settled(evaluate, 8000, `${route} @${width}x${height} opening`))) {
        failures.push(`${route} @${width}x${height} light: opening never finished styling`);
        continue;
      }
      const chartRoute = CHART_ROUTES.has(route);
      const snapshot = await chapterOpeningSnapshot(
        chartRoute,
        route === "/methods/" ? "index" : "evidence",
      );
      totals.chapterOpeningChecks += 1;
      if (!snapshot?.state) {
        failures.push(`${route} @${width}x${height} light: opening probe returned nothing`);
        continue;
      }
      if (
        !snapshot.intro?.visible || snapshot.intro?.headingCount !== 1 ||
        snapshot.intro?.thesisCount !== 1 || !snapshot.intro?.thesisText ||
        !snapshot.intro?.thesisAfterHeading
      ) {
        failures.push(
          `${route} @${width}x${height} light: chapter intro lacks one visible direct-child ` +
            `thesis after h1 (${JSON.stringify(snapshot.intro)})`,
        );
      }
      if (snapshot.primaryCount > 1) {
        failures.push(
          `${route} @${width}x${height} light: found ${snapshot.primaryCount} primary evidence surfaces`,
        );
      }
      if (
        chartRoute &&
        (
          snapshot.primaryPlotCount > 1 || snapshot.primaryPlotsInside > 1 ||
          (snapshot.primaryPlotCount === 1 && snapshot.primaryPlotsInside !== 1)
        )
      ) {
        failures.push(
          `${route} @${width}x${height} light: primary plot hook is not unique inside primary evidence`,
        );
      }
      if (!chartRoute && snapshot.primaryPlotCount !== 0) {
        failures.push(`${route} @${width}x${height} light: non-chart primary uses a plot hook`);
      }
      for (const problem of chapterOpeningProblems(snapshot.state)) {
        failures.push(`${route} @${width}x${height} light: ${problem}`);
      }
      const chapterIndex = CHAPTER_ROUTES.indexOf(route);
      const expectedPreviousLinks = chapterIndex === 0 ? 0 : 1;
      const expectedNextLinks = chapterIndex === CHAPTER_ROUTES.length - 1 ? 0 : 1;
      const ending = await chapterEndingSnapshot({
        expectedProgressText: `${chapterIndex + 1} / ${CHAPTER_ROUTES.length}`,
        expectedPosition: chapterIndex === 0
          ? "first"
          : chapterIndex === CHAPTER_ROUTES.length - 1 ? "last" : "middle",
        expectedLinkCount: 1 + expectedPreviousLinks + expectedNextLinks,
        expectedPreviousLinks,
        expectedNextLinks,
      });
      totals.chapterEndingChecks += 1;
      for (const problem of chapterEndingProblems(ending, { width, height })) {
        failures.push(`${route} @${width}x${height} light ending: ${problem}`);
      }
      if (route === "/detection/") {
        const detectionState = await detectionLimitationBriefSnapshot("normal");
        if ((width === 375 && height === 812) || (width === 1280 && height === 720)) {
          console.log(
            "site-quality detection opening " +
              JSON.stringify({
                width,
                height,
                plotTop: detectionState?.landmarks?.primaryPlot?.top ?? null,
                plotBottom: detectionState?.landmarks?.primaryPlot?.bottom ?? null,
                keyTop: detectionState?.landmarks?.key?.top ?? null,
                keyBottom: detectionState?.landmarks?.key?.bottom ?? null,
                horizontalOverflow:
                  (detectionState?.document?.scrollWidth ?? 0) -
                  (detectionState?.document?.clientWidth ?? 0),
              }),
          );
        }
        for (const problem of detectionLimitationBriefProblems(
          detectionState,
          EXPECTED_DETECTION_EVENTS,
          detectionState?.viewport,
        )) {
          failures.push(`${route} @${width}x${height} light opening: ${problem}`);
        }
      }
      if (route === "/health/") {
        const healthState = await healthAssumptionLedgerSnapshot("normal");
        if ((width === 375 && height === 812) || (width === 1280 && height === 720)) {
          console.log(
            "site-quality health opening " +
              JSON.stringify({
                width,
                height,
                ledgerTop: healthState?.landmarks?.ledger?.top ?? null,
                ledgerBottom: healthState?.landmarks?.ledger?.bottom ?? null,
                primaryTitleTop: healthState?.landmarks?.primaryTitle?.top ?? null,
                plotTop: healthState?.landmarks?.primaryPlot?.top ?? null,
                horizontalOverflow:
                  (healthState?.document?.scrollWidth ?? 0) -
                  (healthState?.document?.clientWidth ?? 0),
              }),
          );
        }
        for (const problem of healthAssumptionLedgerProblems(
          healthState,
          EXPECTED_HEALTH_EVIDENCE,
          healthState?.viewport,
        )) {
          failures.push(`${route} @${width}x${height} light opening: ${problem}`);
        }
      }
      if (route === "/forecast/") {
        const forecastState = await forecastHorizonDecisionSnapshot("normal");
        if ((width === 375 && height === 812) || (width === 1280 && height === 720)) {
          console.log(
            "site-quality forecast opening " +
              JSON.stringify({
                width,
                height,
                plotTop: forecastState?.landmarks?.primaryPlot?.top ?? null,
                plotBottom: forecastState?.landmarks?.primaryPlot?.bottom ?? null,
                sheetTop: forecastState?.landmarks?.decisionSheet?.top ?? null,
                horizontalOverflow:
                  (forecastState?.document?.scrollWidth ?? 0) -
                  (forecastState?.document?.clientWidth ?? 0),
              }),
          );
        }
        for (const problem of forecastHorizonDecisionProblems(
          forecastState,
          EXPECTED_FORECAST_EVIDENCE,
          forecastState?.viewport,
        )) {
          failures.push(`${route} @${width}x${height} light opening: ${problem}`);
        }
      }
      if (route === "/methods/") {
        const methodsState = await methodsCaseIndexSnapshot("normal");
        if ((width === 375 && height === 812) || (width === 1280 && height === 720)) {
          console.log(
            "site-quality methods opening " +
              JSON.stringify({
                width,
                height,
                indexTop: methodsState?.landmarks?.index?.top ?? null,
                indexBottom: methodsState?.landmarks?.index?.bottom ?? null,
                horizontalOverflow:
                  (methodsState?.document?.scrollWidth ?? 0) -
                  (methodsState?.document?.clientWidth ?? 0),
              }),
          );
        }
        for (const problem of methodsCaseIndexProblems(methodsState, methodsState?.viewport)) {
          failures.push(`${route} @${width}x${height} light opening: ${problem}`);
        }
      }
      if (route === "/data/") {
        const dataState = await dataProvenanceRegisterSnapshot("normal");
        if ((width === 375 && height === 812) || (width === 1280 && height === 720)) {
          console.log(
            "site-quality data opening " +
              JSON.stringify({
                width,
                height,
                registerTop: dataState?.landmarks?.register?.top ?? null,
                registerBottom: dataState?.landmarks?.register?.bottom ?? null,
                horizontalOverflow:
                  (dataState?.document?.scrollWidth ?? 0) -
                  (dataState?.document?.clientWidth ?? 0),
              }),
          );
        }
        for (const problem of dataProvenanceRegisterProblems(dataState, dataState?.viewport)) {
          failures.push(`${route} @${width}x${height} light opening: ${problem}`);
        }
      }
      if (route === "/explore/") {
        const explorerState = await explorerGuidedWorkspaceSnapshot("normal");
        for (const problem of explorerGuidedWorkspaceProblems(explorerState, { width, height })) {
          failures.push(`${route} @${width}x${height} light opening: ${problem}`);
        }
      }
    }
  }
  for (const width of [1599, 1600]) {
    const height = 900;
    await send("Emulation.setDeviceMetricsOverride", {
      width,
      height,
      deviceScaleFactor: 1,
      mobile: false,
    });
    await send("Page.navigate", { url: `${origin}/trend/` });
    if (!(await settled(evaluate, 8000, `/trend/ @${width}x${height} shell boundary`))) {
      failures.push(`/trend/ @${width}x${height} light: shell boundary never finished styling`);
      continue;
    }
    const snapshot = await chapterOpeningSnapshot(true);
    const shellProblems = chapterOpeningProblems(snapshot?.state).filter(
      (problem) => problem.includes("rail") || problem.includes("handle"),
    );
    for (const problem of shellProblems) {
      failures.push(`/trend/ @${width}x${height} light shell boundary: ${problem}`);
    }
  }
  console.log("site-quality stage: trend reading map");
  for (const [width, height] of [
    [375, 812],
    [768, 1024],
    [1024, 900],
    [1440, 900],
  ]) {
    await send("Emulation.setDeviceMetricsOverride", {
      width,
      height,
      deviceScaleFactor: 1,
      mobile: width < 500,
    });
    for (const theme of ["light", "dark"]) {
      await evaluate(`localStorage.setItem("twair-theme", ${JSON.stringify(theme)})`);
      await send("Emulation.setEmulatedMedia", {
        media: "",
        features: [
          { name: "prefers-color-scheme", value: theme === "light" ? "dark" : "light" },
        ],
      });
      await send("Page.navigate", { url: `${origin}/trend/` });
      if (!(await settled(evaluate, 8000, `/trend/ @${width}x${height} ${theme} reading map`))) {
        failures.push(`/trend/ @${width}x${height} ${theme}: reading map never finished styling`);
        continue;
      }
      const state = await readingMapSnapshot({
        targetIds: TREND_READING_MAP_CONTRACT.targetIds,
        measureAnchors: true,
      });
      for (const problem of readingMapProblems(state, TREND_READING_MAP_CONTRACT)) {
        failures.push(`/trend/ @${width}x${height} ${theme}: ${problem}`);
      }
    }
  }
  console.log("site-quality stage: space field note");
  for (const [width, height] of [
    [375, 812],
    [768, 1024],
    [1024, 900],
    [1440, 900],
  ]) {
    await send("Emulation.setDeviceMetricsOverride", {
      width,
      height,
      deviceScaleFactor: 1,
      mobile: width < 500,
    });
    for (const theme of ["light", "dark"]) {
      await evaluate(`localStorage.setItem("twair-theme", ${JSON.stringify(theme)})`);
      await send("Emulation.setEmulatedMedia", {
        media: "",
        features: [
          { name: "prefers-color-scheme", value: theme === "light" ? "dark" : "light" },
        ],
      });
      await send("Page.navigate", { url: `${origin}/space/` });
      if (!(await settled(evaluate, 8000, `/space/ @${width}x${height} ${theme} field note`))) {
        failures.push(`/space/ @${width}x${height} ${theme}: field note never finished styling`);
        continue;
      }
      const state = await readingMapSnapshot({
        targetIds: SPACE_READING_MAP_CONTRACT.targetIds,
        measureAnchors: true,
      });
      for (const problem of readingMapProblems(state, SPACE_READING_MAP_CONTRACT)) {
        failures.push(`/space/ @${width}x${height} ${theme}: ${problem}`);
      }
    }
  }
  await evaluate('localStorage.setItem("twair-theme", "light")');
  await send("Emulation.setEmulatedMedia", {
    media: "",
    features: [{ name: "prefers-color-scheme", value: "dark" }],
  });
  for (const [width, height] of [[375, 812], [1440, 900]]) {
    await send("Emulation.setDeviceMetricsOverride", {
      width,
      height,
      deviceScaleFactor: 1,
      mobile: width < 500,
    });
    if (
      !(await navigateWithoutPageScripts(send, waitForEvent, `${origin}/trend/`, () =>
        settled(evaluate, 8000, `/trend/ @${width}x${height} no-JavaScript reading map`),
      ))
    ) {
      failures.push(`/trend/ @${width}x${height} no-JavaScript light: reading map never styled`);
      continue;
    }
    const state = await readingMapSnapshot({
      targetIds: TREND_READING_MAP_CONTRACT.targetIds,
      measureAnchors: false,
    });
    for (const problem of readingMapProblems(state, TREND_READING_MAP_CONTRACT)) {
      failures.push(`/trend/ @${width}x${height} no-JavaScript light: ${problem}`);
    }
  }
  for (const [width, height] of [[375, 812], [1440, 900]]) {
    await send("Emulation.setDeviceMetricsOverride", {
      width,
      height,
      deviceScaleFactor: 1,
      mobile: width < 500,
    });
    if (
      !(await navigateWithoutPageScripts(send, waitForEvent, `${origin}/space/`, () =>
        settled(evaluate, 8000, `/space/ @${width}x${height} no-JavaScript field note`),
      ))
    ) {
      failures.push(`/space/ @${width}x${height} no-JavaScript light: field note never styled`);
      continue;
    }
    const state = await readingMapSnapshot({
      targetIds: SPACE_READING_MAP_CONTRACT.targetIds,
      measureAnchors: false,
    });
    for (const problem of readingMapProblems(state, SPACE_READING_MAP_CONTRACT)) {
      failures.push(`/space/ @${width}x${height} no-JavaScript light: ${problem}`);
    }
  }
  await evaluate('localStorage.setItem("twair-theme", "dark")');
  console.log("site-quality stage: station dossier");
  await send("Emulation.setEmulatedMedia", { media: "" });
  for (const [width, height] of [
    [375, 812],
    [768, 1024],
    [1024, 900],
    [1440, 900],
  ]) {
    await send("Emulation.setDeviceMetricsOverride", {
      width,
      height,
      deviceScaleFactor: 1,
      mobile: width < 500,
    });
    for (const theme of ["light", "dark"]) {
      await evaluate(`localStorage.setItem("twair-theme", ${JSON.stringify(theme)})`);
      await send("Emulation.setEmulatedMedia", {
        media: "",
        features: [
          { name: "prefers-color-scheme", value: theme === "light" ? "dark" : "light" },
        ],
      });
      await send("Page.navigate", { url: `${origin}/stations/` });
      if (!(await settled(evaluate, 8000, `/stations/ @${width}x${height} ${theme}`))) {
        failures.push(`/stations/ @${width}x${height} ${theme}: dossier never finished styling`);
        continue;
      }
      const state = await stationDossierSnapshot({ changeStation: true });
      const stationProblems = stationDossierProblems(state);
      if (stationProblems.length) {
        console.log("site-quality station dossier failure", JSON.stringify({
          width, theme, separators: state?.separators, stationProblems,
        }));
      }
      for (const problem of stationProblems) {
        failures.push(`/stations/ @${width}x${height} ${theme}: ${problem}`);
      }
      if (
        state?.reportStyle?.background !== "rgba(0, 0, 0, 0)" ||
        state?.reportStyle?.borderRadius !== "0px"
      ) {
        failures.push(`/stations/ @${width}x${height} ${theme}: dossier remains a raised card`);
      }
    }
  }
  await evaluate('localStorage.setItem("twair-theme", "light")');
  for (const [width, height] of [[375, 812], [1440, 900]]) {
    await send("Emulation.setDeviceMetricsOverride", {
      width,
      height,
      deviceScaleFactor: 1,
      mobile: width < 500,
    });
    if (
      !(await navigateWithoutPageScripts(send, waitForEvent, `${origin}/stations/`, () =>
        settled(evaluate, 8000, `/stations/ @${width}x${height} no-JavaScript dossier`),
      ))
    ) {
      failures.push(`/stations/ @${width}x${height} no-JavaScript: dossier never styled`);
      continue;
    }
    const state = await stationRegisterSnapshot();
    for (const problem of stationRegisterProblems(state, "no-JavaScript")) {
      failures.push(`/stations/ @${width}x${height} no-JavaScript: ${problem}`);
    }
  }
  await evaluate('localStorage.setItem("twair-theme", "dark")');
  console.log("site-quality stage: print contract");
  await send("Emulation.setEmulatedMedia", { media: "print" });
  await send("Page.navigate", { url: `${origin}/` });
  if (!(await settled(evaluate))) {
    failures.push("dark-theme print preflight page never finished styling");
  } else {
    const printed = await evaluate(`({
      theme: document.documentElement.dataset.theme ?? null,
      background: getComputedStyle(document.documentElement).getPropertyValue("--bg").trim(),
      colourScheme: getComputedStyle(document.documentElement).colorScheme,
    })`);
    if (printed?.theme !== "dark") {
      failures.push("print preflight did not retain the stored dark reading choice");
    }
    if (printed?.background !== "#fff" || printed?.colourScheme !== "light") {
      failures.push("manual dark theme overrode the light print palette");
    }
  }
  await send("Page.navigate", { url: `${origin}/trend/` });
  if (!(await settled(evaluate, 8000, "/trend/ print reading contract"))) {
    failures.push("trend print page never finished styling");
  } else {
    const state = await readingMapPrintSnapshot(TREND_READING_MAP_CONTRACT.targetIds);
    for (const problem of readingMapPrintProblems(state, TREND_READING_MAP_CONTRACT)) {
      failures.push(`/trend/ print: ${problem}`);
    }
  }
  await send("Page.navigate", { url: `${origin}/stations/` });
  if (!(await settled(evaluate, 8000, "/stations/ print dossier"))) {
    failures.push("station print page never finished styling");
  } else {
    const state = await stationRegisterSnapshot();
    for (const problem of stationRegisterProblems(state, "print")) {
      failures.push(`station print: ${problem}`);
    }
  }
  await send("Page.navigate", { url: `${origin}/space/` });
  if (!(await settled(evaluate, 8000, "/space/ print field-note contract"))) {
    failures.push("space print page never finished styling");
  } else {
    const state = await readingMapPrintSnapshot(SPACE_READING_MAP_CONTRACT.targetIds);
    for (const problem of readingMapPrintProblems(state, SPACE_READING_MAP_CONTRACT)) {
      failures.push(`/space/ print: ${problem}`);
    }
  }

  await send("Page.navigate", { url: `${origin}/sources/` });
  if (!(await settled(evaluate, 8000, "/sources/ print conditional atlas"))) {
    failures.push("/sources/ print conditional atlas never finished styling");
  } else {
    const printed = await evaluate(`(() => {
      const visible = (element) => {
        const style = element ? getComputedStyle(element) : null;
        const rect = element?.getBoundingClientRect();
        return Boolean(style && rect && style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0);
      };
      return {
        atlas: ${SOURCES_ATLAS_STATE_PROBE},
        caption: visible(document.querySelector("#cbpf-caption")),
        caveat: visible(document.querySelector("#sources .caveat")),
        readouts: ["#cbpf-threshold", "#cbpf-peak", "#cbpf-peak-speed", "#cbpf-resultant", "#cbpf-calm"].every((selector) => visible(document.querySelector(selector))),
      };
    })()`);
    const atlas = printed?.atlas ?? null;
    for (const problem of sourcesAtlasProblems(atlas, 1024, 900, {
      allowPickerHidden: true,
    })) {
      failures.push(`/sources/ print: ${problem}`);
    }
    if (!printed?.caption || !printed?.caveat || !printed?.readouts) {
      failures.push("/sources/ print: boundary, Figure 4.1 caption, four readouts, or caveat is not visible");
    }
  }

  await send("Page.navigate", { url: `${origin}/detection/` });
  if (!(await settled(evaluate, 8000, "/detection/ print limitation brief"))) {
    failures.push("detection print page never finished styling");
  } else {
    const detectionState = await detectionLimitationBriefSnapshot("print");
    for (const problem of detectionLimitationBriefProblems(
      detectionState,
      EXPECTED_DETECTION_EVENTS,
      detectionState?.viewport,
    )) {
      failures.push(`/detection/ print: ${problem}`);
    }
  }

  await send("Page.navigate", { url: `${origin}/health/` });
  if (!(await settled(evaluate, 8000, "/health/ print assumption ledger"))) {
    failures.push("health print page never finished styling");
  } else {
    const healthState = await healthAssumptionLedgerSnapshot("print");
    for (const problem of healthAssumptionLedgerProblems(
      healthState,
      EXPECTED_HEALTH_EVIDENCE,
      healthState?.viewport,
    )) {
      failures.push(`/health/ print: ${problem}`);
    }
  }

  await send("Page.navigate", { url: `${origin}/forecast/` });
  if (!(await settled(evaluate, 8000, "/forecast/ print horizon decision"))) {
    failures.push("forecast print page never finished styling");
  } else {
    const forecastState = await forecastHorizonDecisionSnapshot("print");
    for (const problem of forecastHorizonDecisionProblems(
      forecastState,
      EXPECTED_FORECAST_EVIDENCE,
      forecastState?.viewport,
    )) {
      failures.push(`/forecast/ print: ${problem}`);
    }
  }

  await send("Page.navigate", { url: `${origin}/methods/` });
  if (!(await settled(evaluate, 8000, "/methods/ print case index"))) {
    failures.push("methods print page never finished styling");
  } else {
    const methodsState = await methodsCaseIndexSnapshot("print");
    for (const problem of methodsCaseIndexProblems(methodsState, methodsState?.viewport)) {
      failures.push(`/methods/ print: ${problem}`);
    }
  }

  await send("Page.navigate", { url: `${origin}/data/` });
  if (!(await settled(evaluate, 8000, "/data/ print provenance register"))) {
    failures.push("data print page never finished styling");
  } else {
    const dataState = await dataProvenanceRegisterSnapshot("print");
    for (const problem of dataProvenanceRegisterProblems(dataState, dataState?.viewport)) {
      failures.push(`/data/ print: ${problem}`);
    }
  }

  await send("Page.navigate", { url: `${origin}/explore/` });
  if (!(await settled(evaluate, 8000, "/explore/ print guided workspace"))) {
    failures.push("explore print page never finished styling");
  } else {
    const explorerState = await explorerGuidedWorkspaceSnapshot("print");
    for (const problem of explorerGuidedWorkspaceProblems(
      explorerState,
      { width: 1440, height: 900 },
    )) {
      failures.push(`/explore/ print: ${problem}`);
    }
  }

  for (const [route, expectedCount] of STATIC_CONCEPT_DIAGRAMS) {
    if (expectedCount === 0) continue;
    await send("Page.navigate", { url: `${origin}${route}` });
    if (!(await settled(evaluate, 8000, `${route} print concept diagram`))) {
      failures.push(`${route} print concept diagram never finished styling`);
      continue;
    }
    const conceptState = await conceptDiagramSnapshot();
    for (const problem of conceptDiagramProblems(
      conceptState,
      expectedCount,
      { width: 1440, height: 900 },
    )) {
      failures.push(`${route} print: ${problem}`);
    }
    for (const problem of conceptDiagramPrintProblems(conceptState)) {
      failures.push(`${route} print: ${problem}`);
    }
  }

  console.log("site-quality stage: forced-colors concept diagrams");
  await send("Emulation.setDeviceMetricsOverride", {
    width: 1024,
    height: 900,
    deviceScaleFactor: 1,
    mobile: false,
  });
  await send("Emulation.setEmulatedMedia", {
    media: "",
    features: [
      { name: "forced-colors", value: "active" },
      { name: "prefers-color-scheme", value: "light" },
    ],
  });
  for (const route of ["/trend/", "/data/"]) {
    await send("Page.navigate", { url: `${origin}${route}` });
    if (!(await settled(evaluate, 8000, `${route} forced-colors concept diagram`))) {
      failures.push(`${route} forced-colors concept diagram never finished styling`);
      continue;
    }
    const conceptState = await conceptDiagramSnapshot();
    for (const problem of conceptDiagramProblems(conceptState, 1, { width: 1024, height: 900 })) {
      failures.push(`${route} forced-colors: ${problem}`);
    }
    for (const problem of conceptDiagramForcedColorsProblems(conceptState)) {
      failures.push(`${route} forced-colors: ${problem}`);
    }
  }

  console.log("site-quality stage: 610px trend idle-readout use");
  await send("Emulation.setDeviceMetricsOverride", {
    width: 610,
    height: 900,
    deviceScaleFactor: 1,
    mobile: false,
  });
  await send("Emulation.setEmulatedMedia", {
    media: "",
    features: [{ name: "prefers-color-scheme", value: "light" }],
  });
  await send("Page.navigate", { url: `${origin}/trend/` });
  const trendIdleStyled = await settled(evaluate, 8000, "/trend/ 610px idle readout use");
  let trendIdleReady = false;
  if (trendIdleStyled) {
    for (let attempt = 0; attempt < 80; attempt += 1) {
      trendIdleReady = Boolean(await evaluate(`(() => {
        const figures = [...document.querySelectorAll("main .evidence-figure")];
        return location.pathname.endsWith("/trend/") && figures.length === 3 &&
          figures.every((figure) => {
            const plot = figure.querySelector(".plot[data-readout]");
            return plot?.dataset.reading === "false" &&
              plot.nextElementSibling?.matches(".readout-dock") &&
              Boolean(plot.nextElementSibling.querySelector(".readout-panel"));
          });
      })()`));
      if (trendIdleReady) break;
      await sleep(50);
    }
  }
  if (!trendIdleStyled || !trendIdleReady) {
    failures.push("/trend/ @610 light: page never finished styling for idle readout use");
  } else {
    const state = await evaluate(`(() => {
      const reserve = (dock) => {
        if (!dock) return NaN;
        const style = getComputedStyle(dock);
        return dock.getBoundingClientRect().height +
          (Number.parseFloat(style.marginBlockStart) || 0);
      };
      const figures = [...document.querySelectorAll("main .evidence-figure")];
      const rows = [];
      for (const [index, figure] of figures.entries()) {
        const plot = figure.querySelector(".plot[data-readout]");
        const dock = plot?.nextElementSibling?.matches(".readout-dock")
          ? plot.nextElementSibling : null;
        const panel = dock?.querySelector(".readout-panel") ?? null;
        if (!plot || !dock || !panel) {
          rows.push({ index, hasDock: false });
          continue;
        }
        const readingBefore = plot.dataset.reading ?? null;
        const idleOptIn = plot.hasAttribute("data-idle-readout");
        const idleReserve = reserve(dock);
        const idlePanel = panel.getBoundingClientRect();
        const idlePanelOpacity = Number(getComputedStyle(panel).opacity);
        rows.push({
          index,
          hasDock: true,
          readingBefore,
          idleOptIn,
          idleWhen: panel.querySelector(".readout-when")?.textContent?.trim() ?? null,
          idleReserve,
          idlePanelHeight: idlePanel.height,
          idlePanelOpacity,
          idleUnoccupiedReserve: idlePanelOpacity < 0.99
            ? idleReserve
            : Math.max(0, idleReserve - idlePanel.height),
        });
      }
      return { viewport: { width: innerWidth, height: innerHeight }, figures: rows };
    })()`);
    for (const problem of trendIdleReadoutBlankProblems(state)) {
      failures.push(`/trend/ @610 light: ${problem}`);
    }
  }

  console.log("site-quality stage: concept diagram breakpoint boundaries");
  await evaluate('localStorage.setItem("twair-theme", "light")');
  await send("Emulation.setEmulatedMedia", {
    media: "",
    features: [{ name: "prefers-color-scheme", value: "dark" }],
  });
  for (const width of [769, 1024]) {
    await send("Emulation.setDeviceMetricsOverride", {
      width,
      height: 900,
      deviceScaleFactor: 1,
      mobile: false,
    });
    for (const [route, expectedCount] of STATIC_CONCEPT_DIAGRAMS) {
      if (expectedCount === 0) continue;
      await send("Page.navigate", { url: `${origin}${route}` });
      if (!(await settled(evaluate, 8000, `${route} @${width}px concept diagram`))) {
        failures.push(`${route} @${width}px concept diagram never finished styling`);
        continue;
      }
      const conceptState = await conceptDiagramSnapshot();
      for (const problem of conceptDiagramProblems(
        conceptState,
        expectedCount,
        { width, height: 900 },
      )) {
        failures.push(`${route} @${width}px concept diagram: ${problem}`);
      }
    }
  }

  // 768 is here for one defect only: two axis labels landing on each other.
  // The marks are positioned in percentages inside a fluid figure, so a strip
  // that reads cleanly at both ends can pile up in the middle — and the two
  // endpoints are exactly where a check written from screenshots would look.
  // It costs about fifteen seconds and covers the width nothing else does.
  for (const [width, height] of [
    [375, 800],
    [768, 1024],
    [1440, 900],
  ]) {
    for (const theme of ["light", "dark"]) {
      await restartBrowser();
      await send("Emulation.setDeviceMetricsOverride", {
        width,
        height,
        deviceScaleFactor: 1,
        mobile: width < 500,
      });
      await send("Page.navigate", { url: `${origin}/` });
      if (!(await settled(evaluate, 8000, `${width}px ${theme} browser restart`))) {
        failures.push(`${width} ${theme}: replacement browser never finished styling`);
      }
      console.log(`site-quality stage: route matrix ${width}px ${theme}`);
      await evaluate(`localStorage.setItem("twair-theme", ${JSON.stringify(theme)})`);
      const osTheme = theme === "light" ? "dark" : "light";
      await send("Emulation.setEmulatedMedia", {
        media: "",
        features: [{ name: "prefers-color-scheme", value: osTheme }],
      });
      for (const route of ROUTES) {
        console.log(`site-quality route: ${route} @${width}px ${theme}`);
        await send("Page.navigate", { url: `${origin}${route}` });
        if (!(await settled(evaluate, 8000, `${route} @${width}px ${theme}`))) {
          failures.push(`${route} @${width} ${theme}: page never finished styling`);
          continue;
        }
        console.log(`site-quality route styled: ${route} @${width}px ${theme}`);
        const bodyText = await evaluate("document.body.innerText");
        if (FORBIDDEN_CIGARETTE_ANALOGY.test(bodyText ?? "")) {
          failures.push(`${route} @${width} ${theme}: cigarette analogy remains`);
        }
        for (const problem of publicOperationalMetadataProblems(bodyText)) {
          failures.push(`${route} @${width} ${theme}: ${problem}`);
        }
        const conceptState = await conceptDiagramSnapshot();
        for (const problem of conceptDiagramProblems(
          conceptState,
          STATIC_CONCEPT_DIAGRAMS.get(route),
          { width, height },
        )) {
          failures.push(`${route} @${width} ${theme}: ${problem}`);
        }
        if (route === "/") {
          const boundary = await semanticBoundarySnapshot(
            "[data-homepage-station-type-boundary]",
          );
          for (const problem of homepageStationTypeBoundaryProblems(boundary)) {
            failures.push(`${route} @${width} ${theme}: ${problem}`);
          }
        }
        if (route === "/trend/") {
          const boundary = await semanticBoundarySnapshot("[data-deweather-contrast-boundary]");
          for (const problem of deweatherContrastBoundaryProblems(boundary)) {
            failures.push(`${route} @${width} ${theme}: ${problem}`);
          }
        }
        const clarification = await interactionClarificationSnapshot();
        if (clarification?.figure?.toolbarCount) {
          for (const problem of figureDownloadLabelProblems(clarification.figure)) {
            failures.push(`${route} @${width} ${theme}: ${problem}`);
          }
        }
        if (route === "/trend/") {
          for (const problem of trendControlClarificationProblems(clarification?.trend)) {
            failures.push(`${route} @${width} ${theme}: ${problem}`);
          }
        }
        if (route === "/stations/") {
          for (const problem of stationFilterHelperProblems(clarification?.station)) {
            failures.push(`${route} @${width} ${theme}: ${problem}`);
          }
        }
        if (route === "/") {
          for (const problem of homepageMapStationRouteProblems(clarification?.map)) {
            failures.push(`${route} @${width} ${theme}: ${problem}`);
          }
        }
        if (route === "/sources/") {
          const mainText = await evaluate(
            'document.querySelector("main")?.innerText.replace(/\\s+/g, " ").trim() ?? ""',
          );
          for (const problem of sourcesClaimBoundaryProblems(mainText ?? "")) {
            failures.push(`${route} @${width} ${theme}: ${problem}`);
          }
          const atlas = await evaluate(SOURCES_ATLAS_STATE_PROBE);
          for (const problem of sourcesAtlasProblems(atlas, width, height)) {
            failures.push(`${route} @${width} ${theme}: ${problem}`);
          }
        }
        if (route === "/detection/") {
          const mainText = await evaluate(
            'document.querySelector("main")?.innerText.replace(/\\s+/g, " ").trim() ?? ""',
          );
          for (const problem of detectionClaimBoundaryProblems(mainText ?? "")) {
            failures.push(`${route} @${width} ${theme}: ${problem}`);
          }
          const estimateTable = await evaluate(DETECTION_ESTIMATE_TABLE_PROBE);
          for (const problem of detectionEstimateTableProblems(estimateTable)) {
            failures.push(`${route} @${width} ${theme}: ${problem}`);
          }
          const detectionState = await detectionLimitationBriefSnapshot("normal");
          for (const problem of detectionLimitationBriefProblems(
            detectionState,
            EXPECTED_DETECTION_EVENTS,
            detectionState?.viewport,
          )) {
            failures.push(`${route} @${width} ${theme}: ${problem}`);
          }
        }
        if (route === "/health/") {
          const healthState = await healthAssumptionLedgerSnapshot("normal");
          for (const problem of healthAssumptionLedgerProblems(
            healthState,
            EXPECTED_HEALTH_EVIDENCE,
            healthState?.viewport,
          )) {
            failures.push(`${route} @${width} ${theme}: ${problem}`);
          }
        }
        if (route === "/forecast/") {
          const forecastState = await forecastHorizonDecisionSnapshot("normal");
          for (const problem of forecastHorizonDecisionProblems(
            forecastState,
            EXPECTED_FORECAST_EVIDENCE,
            forecastState?.viewport,
          )) {
            failures.push(`${route} @${width} ${theme}: ${problem}`);
          }
        }
        if (route === "/methods/") {
          const methodsState = await methodsCaseIndexSnapshot("normal");
          for (const problem of methodsCaseIndexProblems(methodsState, methodsState?.viewport)) {
            failures.push(`${route} @${width} ${theme}: ${problem}`);
          }
        }
        if (route === "/data/") {
          const dataState = await dataProvenanceRegisterSnapshot("normal");
          for (const problem of dataProvenanceRegisterProblems(dataState, dataState?.viewport)) {
            failures.push(`${route} @${width} ${theme}: ${problem}`);
          }
        }
        if (route === "/explore/") {
          const explorerState = await explorerGuidedWorkspaceSnapshot("normal");
          for (const problem of explorerGuidedWorkspaceProblems(explorerState, { width, height })) {
            failures.push(`${route} @${width} ${theme}: ${problem}`);
          }
        }
        if (HISTORICAL_STATION_ROUTES.has(route)) {
          const historicalState = await evaluate(HISTORICAL_STATION_DISCLOSURE_PROBE);
          for (const problem of historicalStationCopyProblems(
            route, historicalState?.text ?? "", historicalState?.hrefs ?? null,
          )) {
            failures.push(`${route} @${width} ${theme}: ${problem}`);
          }
          if (route === "/data/") {
            for (const problem of publicationDisclosureProblems(historicalState)) {
              failures.push(`${route} @${width} ${theme}: ${problem}`);
            }
          }
        }
        if (route === "/" && (width === 768 || width === 1440)) {
          for (const problem of await homepageFirstViewport()) {
            failures.push(`${route} @${width}x${height} ${theme}: ${problem}`);
          }
        }
        if (route === "/forecast/") {
          const palette = await evaluate(`(() => {
            const rootStyle = getComputedStyle(document.documentElement);
            const canvas = document.createElement("canvas");
            canvas.width = canvas.height = 1;
            const context = canvas.getContext("2d", { willReadFrequently: true });
            const rgb = (colour) => {
              context.clearRect(0, 0, 1, 1);
              context.fillStyle = colour;
              context.fillRect(0, 0, 1, 1);
              return [...context.getImageData(0, 0, 1, 1).data.slice(0, 3)]
                .map((channel) => channel / 255);
            };
            const linear = (value) => value <= 0.04045
              ? value / 12.92
              : ((value + 0.055) / 1.055) ** 2.4;
            const linearRgb = (colour) => colour.map(linear);
            const labFromLinear = ([red, green, blue]) => {
              const l = Math.cbrt(0.4122214708 * red + 0.5363325363 * green + 0.0514459929 * blue);
              const m = Math.cbrt(0.2119034982 * red + 0.6806995451 * green + 0.1073969566 * blue);
              const s = Math.cbrt(0.0883024619 * red + 0.2817188376 * green + 0.6299787005 * blue);
              return [
                0.2104542553 * l + 0.793617785 * m - 0.0040720468 * s,
                1.9779984951 * l - 2.428592205 * m + 0.4505937099 * s,
                0.0259040371 * l + 0.7827717662 * m - 0.808675766 * s,
              ];
            };
            const matrices = {
              protan: [
                [0.152286, 1.052583, -0.204868],
                [0.114503, 0.786281, 0.099216],
                [-0.003882, -0.048116, 1.051998],
              ],
              deutan: [
                [0.367322, 0.860646, -0.227968],
                [0.280085, 0.672501, 0.047413],
                [-0.01182, 0.04294, 0.968881],
              ],
            };
            const simulate = (colour, matrix) => {
              const source = linearRgb(colour);
              return labFromLinear(matrix.map((row) => Math.max(0, Math.min(1,
                row.reduce((sum, weight, index) => sum + weight * source[index], 0),
              ))));
            };
            const distance = (left, right) => Math.hypot(
              left[0] - right[0], left[1] - right[1], left[2] - right[2],
            );
            const minimumPairwise = (values) => {
              let minimum = Infinity;
              for (let left = 0; left < values.length; left += 1) {
                for (let right = left + 1; right < values.length; right += 1) {
                  minimum = Math.min(minimum, distance(values[left], values[right]));
                }
              }
              return minimum;
            };
            const colours = Array.from({ length: 8 }, (_, index) =>
              rgb(rootStyle.getPropertyValue("--k" + index))
            );
            const labs = colours.map((colour) => labFromLinear(linearRgb(colour)));
            const prefixes = Object.fromEntries([2, 3, 4, 8].map((count) => {
              const subset = colours.slice(0, count);
              return [count, {
                normal: minimumPairwise(labs.slice(0, count)),
                protan: minimumPairwise(subset.map((colour) => simulate(colour, matrices.protan))),
                deutan: minimumPairwise(subset.map((colour) => simulate(colour, matrices.deutan))),
              }];
            }));
            const luminance = (colour) => {
              const [red, green, blue] = linearRgb(colour);
              return 0.2126 * red + 0.7152 * green + 0.0722 * blue;
            };
            const surfaces = ["--bg", "--bg-raised", "--bg-sunken"]
              .map((token) => rgb(rootStyle.getPropertyValue(token)));
            const contrast = (left, right) => {
              const a = luminance(left);
              const b = luminance(right);
              return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
            };
            const lightnesses = labs.map((lab) => lab[0]);
            const chromas = labs.map((lab) => Math.hypot(lab[1], lab[2]));
            return {
              count: colours.length,
              unique: new Set(colours.map((colour) => colour.join(","))).size,
              prefixes,
              lightnessRange: Math.max(...lightnesses) - Math.min(...lightnesses),
              minimumChroma: Math.min(...chromas),
              minimumSurfaceContrast: Math.min(...colours.flatMap((colour) =>
                surfaces.map((surface) => contrast(colour, surface))
              )),
            };
          })()`);
          const commonFloor = theme === "light"
            ? { normal: 0.15, protan: 0.12, deutan: 0.11 }
            : { normal: 0.135, protan: 0.14, deutan: 0.13 };
          const fullFloor = theme === "light"
            ? { normal: 0.04, protan: 0.04, deutan: 0.04 }
            : { normal: 0.075, protan: 0.075, deutan: 0.075 };
          if (width === 1440) {
            console.log("site-quality categorical palette " + JSON.stringify({ theme, ...palette }));
          }
          if (palette?.count !== 8 || palette?.unique !== 8) {
            failures.push(`${route} @${width} ${theme}: categorical palette is not eight distinct colours`);
          }
          if (
            palette?.prefixes?.[4]?.normal < commonFloor.normal ||
            palette?.prefixes?.[4]?.protan < commonFloor.protan ||
            palette?.prefixes?.[4]?.deutan < commonFloor.deutan
          ) {
            failures.push(
              `${route} @${width} ${theme}: common categorical prefix remains difficult to distinguish ` +
                `(normal=${palette?.prefixes?.[4]?.normal?.toFixed(3)}, ` +
                `protan=${palette?.prefixes?.[4]?.protan?.toFixed(3)}, ` +
                `deutan=${palette?.prefixes?.[4]?.deutan?.toFixed(3)})`,
            );
          }
          if (
            palette?.prefixes?.[8]?.normal < fullFloor.normal ||
            palette?.prefixes?.[8]?.protan < fullFloor.protan ||
            palette?.prefixes?.[8]?.deutan < fullFloor.deutan
          ) {
            failures.push(
              `${route} @${width} ${theme}: full categorical palette contains a collapsed pair ` +
                `(normal=${palette?.prefixes?.[8]?.normal?.toFixed(3)}, ` +
                `protan=${palette?.prefixes?.[8]?.protan?.toFixed(3)}, ` +
                `deutan=${palette?.prefixes?.[8]?.deutan?.toFixed(3)})`,
            );
          }
          if (palette?.minimumSurfaceContrast < 3) {
            failures.push(
              `${route} @${width} ${theme}: categorical mark falls below 3:1 against a chart surface ` +
                `(${palette?.minimumSurfaceContrast?.toFixed(2)})`,
            );
          }
          if (palette?.minimumChroma < 0.055 || palette?.lightnessRange > 0.35) {
            failures.push(
              `${route} @${width} ${theme}: categorical palette loses its controlled colour rhythm ` +
                `(minimum-chroma=${palette?.minimumChroma?.toFixed(3)}, ` +
                `lightness-range=${palette?.lightnessRange?.toFixed(3)})`,
            );
          }
        }
        if (route === "/methods/") {
          const methodColour = await evaluate(`(() => {
            const line = document.querySelector(".plot-line");
            const probe = document.createElement("span");
            probe.style.color = "var(--k0)";
            document.body.append(probe);
            const result = {
              line: getComputedStyle(line).stroke,
              expected: getComputedStyle(probe).color,
            };
            probe.remove();
            return result;
          })()`);
          if (methodColour?.line !== methodColour?.expected) {
            failures.push(
              `${route} @${width} ${theme}: method series does not use the first categorical role ` +
                `(${methodColour?.line} instead of ${methodColour?.expected})`,
            );
          }
        }
        if (route === "/detection/") {
          const detectionColours = await evaluate(`(() => {
            const resolve = (token) => {
              const probe = document.createElement("span");
              probe.style.color = "var(" + token + ")";
              document.body.append(probe);
              const colour = getComputedStyle(probe).color;
              probe.remove();
              return colour;
            };
            return {
              real: getComputedStyle(document.querySelector(".plot-pt.real")).color,
              realExpected: resolve("--k1"),
              placebo: getComputedStyle(document.querySelector(".placebo-pool path")).stroke,
              placeboExpected: resolve("--text-faint"),
              adverse: getComputedStyle(document.querySelector("td.worse")).color,
              adverseExpected: resolve("--sign-neg-ink"),
            };
          })()`);
          if (detectionColours?.real !== detectionColours?.realExpected) {
            failures.push(
              `${route} @${width} ${theme}: actual event estimates do not use the categorical emphasis role`,
            );
          }
          if (detectionColours?.placebo !== detectionColours?.placeboExpected) {
            failures.push(
              `${route} @${width} ${theme}: placebo distribution no longer stays neutral`,
            );
          }
          if (detectionColours?.adverse !== detectionColours?.adverseExpected) {
            failures.push(
              `${route} @${width} ${theme}: adverse result does not use the negative semantic role`,
            );
          }
        }
        if (route === "/sources/") {
          const palette = await evaluate(`(() => {
            const swatches = [...document.querySelectorAll(".ramp-bar span")];
            const surface = document.querySelector(".chart.polar");
            const title = document.querySelector(".ramp-title")?.textContent ?? "";
            const scale = document.querySelector(".ramp-bar")?.getAttribute("aria-label") ?? "";
            const canvas = document.createElement("canvas");
            canvas.width = canvas.height = 1;
            const context = canvas.getContext("2d", { willReadFrequently: true });
            const rgb = (colour) => {
              context.clearRect(0, 0, 1, 1);
              context.fillStyle = colour;
              context.fillRect(0, 0, 1, 1);
              return [...context.getImageData(0, 0, 1, 1).data.slice(0, 3)]
                .map((channel) => channel / 255);
            };
            const linear = (value) => value <= 0.04045
              ? value / 12.92
              : ((value + 0.055) / 1.055) ** 2.4;
            const linearRgb = (colour) => colour.map(linear);
            const labFromLinear = ([red, green, blue]) => {
              const l = Math.cbrt(0.4122214708 * red + 0.5363325363 * green + 0.0514459929 * blue);
              const m = Math.cbrt(0.2119034982 * red + 0.6806995451 * green + 0.1073969566 * blue);
              const s = Math.cbrt(0.0883024619 * red + 0.2817188376 * green + 0.6299787005 * blue);
              return [
                0.2104542553 * l + 0.793617785 * m - 0.0040720468 * s,
                1.9779984951 * l - 2.428592205 * m + 0.4505937099 * s,
                0.0259040371 * l + 0.7827717662 * m - 0.808675766 * s,
              ];
            };
            const matrices = {
              protan: [
                [0.152286, 1.052583, -0.204868],
                [0.114503, 0.786281, 0.099216],
                [-0.003882, -0.048116, 1.051998],
              ],
              deutan: [
                [0.367322, 0.860646, -0.227968],
                [0.280085, 0.672501, 0.047413],
                [-0.01182, 0.04294, 0.968881],
              ],
            };
            const simulate = (colour, matrix) => {
              const source = linearRgb(colour);
              return labFromLinear(matrix.map((row) => Math.max(0, Math.min(1,
                row.reduce((sum, weight, index) => sum + weight * source[index], 0),
              ))));
            };
            const distance = (left, right) => Math.hypot(
              left[0] - right[0], left[1] - right[1], left[2] - right[2],
            );
            const adjacentDistances = (values) => values.slice(1)
              .map((value, index) => distance(values[index], value));
            const colours = swatches.map((swatch) => rgb(getComputedStyle(swatch).backgroundColor));
            const labs = colours.map((colour) => labFromLinear(linearRgb(colour)));
            const normalDistances = adjacentDistances(labs);
            const protanDistances = adjacentDistances(
              colours.map((colour) => simulate(colour, matrices.protan)),
            );
            const deutanDistances = adjacentDistances(
              colours.map((colour) => simulate(colour, matrices.deutan)),
            );
            const chromas = labs.map((lab) => Math.hypot(lab[1], lab[2]));
            const chromaSteps = chromas.slice(1)
              .map((chroma, index) => chroma - chromas[index]);
            const hues = labs.map((lab) => (
              Math.atan2(lab[2], lab[1]) * 180 / Math.PI + 360
            ) % 360);
            const hueSteps = hues.slice(1).map((hue, index) => (
              (hue - hues[index] + 540) % 360 - 180
            ));
            const totalHueTravel = hueSteps.reduce((sum, step) => sum + Math.abs(step), 0);
            const luminance = (colour) => {
              const [red, green, blue] = linearRgb(colour);
              return 0.2126 * red + 0.7152 * green + 0.0722 * blue;
            };
            const surfaceColour = rgb(getComputedStyle(surface).backgroundColor);
            const surfaceLuminance = luminance(surfaceColour);
            const contrast = (colour) => {
              const value = luminance(colour);
              return (Math.max(value, surfaceLuminance) + 0.05) /
                (Math.min(value, surfaceLuminance) + 0.05);
            };
            const steps = labs.slice(1).map((lab, index) => lab[0] - labs[index][0]);
            const lightnessStepMagnitudes = steps.map(Math.abs);
            return {
              count: colours.length,
              unique: new Set(colours.map((colour) => colour.join(","))).size,
              normalDistances,
              protanDistances,
              deutanDistances,
              minNormal: Math.min(...normalDistances),
              minProtan: Math.min(...protanDistances),
              minDeutan: Math.min(...deutanDistances),
              averageLightness: labs.reduce((sum, lab) => sum + lab[0], 0) / labs.length,
              minLightness: Math.min(...labs.map((lab) => lab[0])),
              averageChroma: chromas.reduce((sum, chroma) => sum + chroma, 0) / chromas.length,
              minChroma: Math.min(...chromas),
              chromaSteps,
              maxChromaStep: Math.max(...chromaSteps.map(Math.abs)),
              hueSteps,
              totalHueTravel,
              hueDirection: hueSteps.every((step) => step < 0) ? "descending" :
                hueSteps.every((step) => step > 0) ? "ascending" : "mixed",
              lightnessSteps: steps,
              minLightnessStep: Math.min(...steps.map(Math.abs)),
              lightnessStepSpread:
                Math.max(...lightnessStepMagnitudes) - Math.min(...lightnessStepMagnitudes),
              normalDistanceSpread:
                Math.max(...normalDistances) - Math.min(...normalDistances),
              direction: steps.every((step) => step < 0) ? "descending" :
                steps.every((step) => step > 0) ? "ascending" : "mixed",
              minSurfaceContrast: Math.min(...colours.map(contrast)),
              endpointCopy: /低\\s*→\\s*高/.test(title) && /由低到高/.test(scale),
            };
          })()`);
          const expectedDirection = theme === "light" ? "descending" : "ascending";
          const minimumLightnessStep = theme === "light" ? 0.03 : 0.052;
          if (width === 1440) {
            console.log("site-quality CBPF palette " + JSON.stringify({ theme, ...palette }));
          }
          if (palette?.count !== 7 || palette?.unique !== 7) {
            failures.push(`${route} @${width} ${theme}: CBPF ramp is not seven distinct colours`);
          }
          if (
            palette?.minNormal < 0.095 ||
            palette?.minProtan < 0.06 ||
            palette?.minDeutan < 0.06
          ) {
            failures.push(
              `${route} @${width} ${theme}: CBPF adjacent colours remain too close ` +
                `(normal=${palette?.minNormal?.toFixed(3)}, ` +
                `protan=${palette?.minProtan?.toFixed(3)}, ` +
                `deutan=${palette?.minDeutan?.toFixed(3)}; ` +
                `normal-pairs=${palette?.normalDistances?.map((value) => value.toFixed(3)).join("/")}; ` +
                `protan-pairs=${palette?.protanDistances?.map((value) => value.toFixed(3)).join("/")}; ` +
                `deutan-pairs=${palette?.deutanDistances?.map((value) => value.toFixed(3)).join("/")})`,
            );
          }
          if (
            palette?.direction !== expectedDirection ||
            palette?.minLightnessStep < minimumLightnessStep
          ) {
            failures.push(
              `${route} @${width} ${theme}: CBPF lightness ordering is not a clear ` +
                `${expectedDirection} sequence ` +
                `(direction=${palette?.direction}, step=${palette?.minLightnessStep?.toFixed(3)}, ` +
                `pairs=${palette?.lightnessSteps?.map((value) => value.toFixed(3)).join("/")})`,
            );
          }
          if (
            theme === "light" &&
            (palette?.averageLightness < 0.55 || palette?.minLightness < 0.42)
          ) {
            failures.push(
              `${route} @${width} ${theme}: CBPF Light palette remains too dark ` +
                `(average=${palette?.averageLightness?.toFixed(3)}, ` +
                `darkest=${palette?.minLightness?.toFixed(3)})`,
            );
          }
          if (
            theme === "light" &&
            (
              palette?.averageChroma < 0.11 || palette?.minChroma < 0.085 ||
              palette?.hueDirection !== "ascending" ||
              palette?.totalHueTravel < 140 || palette?.totalHueTravel > 230
            )
          ) {
            failures.push(
              `${route} @${width} ${theme}: CBPF Light palette remains foggy or incoherent ` +
                `(average-chroma=${palette?.averageChroma?.toFixed(3)}, ` +
                `minimum-chroma=${palette?.minChroma?.toFixed(3)}, ` +
                `hue-direction=${palette?.hueDirection}, ` +
                `hue-steps=${palette?.hueSteps?.map((value) => value.toFixed(1)).join("/")}, ` +
                `hue-travel=${palette?.totalHueTravel?.toFixed(1)})`,
            );
          }
          if (
            theme === "light" &&
            (
              palette?.lightnessStepSpread > 0.015 ||
              palette?.maxChromaStep > 0.035 ||
              palette?.normalDistanceSpread > 0.020
            )
          ) {
            failures.push(
              `${route} @${width} ${theme}: CBPF Light palette tonal rhythm is uneven ` +
                `(lightness-step-spread=${palette?.lightnessStepSpread?.toFixed(3)}, ` +
                `chroma-step=${palette?.maxChromaStep?.toFixed(3)}, ` +
                `normal-distance-spread=${palette?.normalDistanceSpread?.toFixed(3)}; ` +
                `chroma-pairs=${palette?.chromaSteps?.map((value) => value.toFixed(3)).join("/")})`,
            );
          }
          const minimumSurfaceContrast = theme === "light" ? 2.2 : 3;
          if (palette?.minSurfaceContrast < minimumSurfaceContrast) {
            failures.push(
              `${route} @${width} ${theme}: CBPF ramp falls below 3:1 against its surface ` +
                `(${palette?.minSurfaceContrast?.toFixed(2)})`,
            );
          }
          if (!palette?.endpointCopy) {
            failures.push(`${route} @${width} ${theme}: CBPF ramp does not state low to high`);
          }
        }
        const hasReadout = await evaluate(
          `Boolean(document.querySelector(".plot[data-readout]"))`,
        );
        if (width === 375 && (route === "/stations/" || route === "/explore/")) {
          const controlOrder = await evaluate(`(() => {
            /*
             * Chapter 2's control is a combobox now: a label carrying "for"
             * beside an input, rather than a select wrapped in its label. The
             * property being checked is unchanged — the label sits above the
             * control it names — so only the way the pair is found moves.
             * (No backticks: this whole block is a template literal.)
             */
            const isStations = ${route === "/stations/"};
            const field = document.querySelector(
              isStations ? ".station-search-field" : null,
            );
            const label = isStations ? field : document.querySelector("#example-select")?.closest("label");
            const labelText = isStations
              ? field?.querySelector(":scope > .control-label")
              : label?.querySelector(":scope > .control-label");
            const select = isStations
              ? document.querySelector("#station-filter")
              : label?.querySelector(":scope > select");
            const action = document.querySelector(${JSON.stringify(
              route === "/explore/" ? "#run" : ".selector-does-not-exist",
            )});
            const status = document.querySelector(${JSON.stringify(
              route === "/explore/" ? "#status" : ".selector-does-not-exist",
            )});
            const box = (element) => element?.getBoundingClientRect() ?? null;
            const labelBox = box(labelText);
            const selectBox = box(select);
            const actionBox = box(action);
            const statusBox = box(status);
            return {
              labelledAbove: Boolean(labelBox && selectBox && labelBox.bottom <= selectBox.top + 1),
              actionLast: ${route === "/explore/"}
                ? Boolean(selectBox && actionBox && selectBox.bottom <= actionBox.top + 1)
                : true,
              statusAfter: ${route === "/explore/"}
                ? Boolean(actionBox && statusBox && actionBox.bottom <= statusBox.top + 1)
                : true,
            };
          })()`);
          if (!controlOrder?.labelledAbove || !controlOrder?.actionLast || !controlOrder?.statusAfter) {
            failures.push(
              `${route} @375 ${theme}: controls do not follow label, control, primary action, status order`,
            );
          }
        }
        const requiredFocus = [
          { name: "theme toggle", selector: "[data-theme-toggle]", required: true },
          // The combobox input is what takes focus now; the select it replaced
          // is gone. Same requirement: it must show a visible focus ring.
          { name: "station control", selector: "#station-filter", required: route === "/stations/" },
          { name: "Explorer run button", selector: "#run", required: route === "/explore/" },
          {
            name: "evidence details",
            selector: "main details:not(.sql-panel) > summary",
            required: false,
          },
        ];
        const focusStates = await focusVisibleStates(requiredFocus);
        console.log(`site-quality route focus: ${route} @${width}px ${theme}`);
        for (const focus of focusStates ?? []) {
          if (focus.required && focus.count === 0) {
            failures.push(`${route} @${width} ${theme}: no visible ${focus.name} was found`);
          }
          for (const state of focus.states) {
            totals.focusChecks += 1;
            if (focus.name === "evidence details") totals.evidenceFocusChecks += 1;
            if (
              !state.active ||
              !state.focusVisible ||
              state.outlineStyle === "none" ||
              state.outlineWidth < 2
            ) {
              failures.push(
                `${route} @${width} ${theme}: ${focus.name} has no focus-visible outline`,
              );
            }
          }
        }
        if (READOUT_ROUTES.has(route) && !hasReadout) {
          failures.push(`${route} @${width} ${theme}: expected a keyboard readout but found none`);
        }
        if (hasReadout) {
          let ready = false;
          for (let attempt = 0; attempt < 40; attempt += 1) {
            const state = await readoutState();
            if (state?.panelReady) {
              ready = true;
              break;
            }
            await sleep(50);
          }
          if (!ready) {
            failures.push(`${route} @${width} ${theme}: .readout-panel was never equipped`);
          } else {
            await evaluate(`document.activeElement?.blur()`);
            await settlePaint(evaluate);
            const closed = await readoutState();
            await evaluate(`document.querySelector(".plot[data-readout]").focus()`);
            await settlePaint(evaluate);
            const focused = await readoutState();
            totals.readouts += 1;
            if (
              !focused?.active ||
              focused.role !== "group" ||
              !focused.panelInDock ||
              !focused.overlaysInArea
            ) {
              failures.push(
                `${route} @${width} ${theme}: the readout did not retain its group/dock/overlay structure`,
              );
            }
            if (
              focused?.points < 3 ||
              !focused.first ||
              !focused.second ||
              !focused.penultimate ||
              !focused.last
            ) {
              failures.push(`${route} @${width} ${theme}: readout keyboard probe had fewer than three x labels`);
            } else {
              await pressKey("End");
              let opened = await readoutState();
              for (let attempt = 0; attempt < 10 && !opened?.visible; attempt += 1) {
                await sleep(20);
                opened = await readoutState();
              }
              if (opened?.reading !== "true" || !opened.visible || opened.when !== focused.last) {
                failures.push(
                  `${route} @${width} ${theme}: keyboard focus did not open the readout ` +
                    `(reading=${opened?.reading ?? "unknown"}, visible=${opened?.visible ?? "unknown"}, ` +
                    `when=${JSON.stringify(opened?.when)}, expected=${JSON.stringify(focused.last)})`,
                );
              }
              await pressKey("ArrowLeft");
              const left = await readoutState();
              if (left?.when === opened?.when || left?.when !== focused.penultimate) {
                failures.push(`${route} @${width} ${theme}: ArrowLeft did not change .readout-when`);
              }
              await pressKey("ArrowRight");
              const right = await readoutState();
              if (right?.when === left?.when || right?.when !== focused.last) {
                failures.push(`${route} @${width} ${theme}: ArrowRight did not advance .readout-when`);
              }
              await pressKey("Home");
              const home = await readoutState();
              if (home?.when !== focused.first) {
                failures.push(`${route} @${width} ${theme}: Home did not reach the first x label`);
              }
              await pressKey("End");
              const end = await readoutState();
              if (end?.when !== focused.last) {
                failures.push(`${route} @${width} ${theme}: End did not reach the last x label`);
              }
              const dockHeights = [closed, focused, opened, left, right, home, end]
                .map((state) => state?.dockHeight)
                .filter(Number.isFinite);
              const figureHeights = [closed, focused, opened, left, right, home, end]
                .map((state) => state?.figureHeight)
                .filter(Number.isFinite);
              if (
                closed?.reading !== "false" ||
                closed?.dockMinBlock <= 0 ||
                dockHeights.length !== 7 ||
                figureHeights.length !== 7 ||
                Math.max(...dockHeights) - Math.min(...dockHeights) > 1 ||
                Math.max(...figureHeights) - Math.min(...figureHeights) > 1
              ) {
                const dockSpread = Math.max(...dockHeights) - Math.min(...dockHeights);
                const figureSpread = Math.max(...figureHeights) - Math.min(...figureHeights);
                const states = [closed, focused, opened, left, right, home, end].map((state) => ({
                  when: state?.when,
                  min: state?.dockMinBlock,
                  width: state?.dockWidth,
                  panel: state?.panelHeight,
                  rows: state?.rowHeights,
                  metrics: state?.rowMetrics,
                }));
                failures.push(
                  `${route} @${width} ${theme}: closed and open readouts changed reserved geometry ` +
                    `(dock spread ${dockSpread.toFixed(3)}px ${JSON.stringify(dockHeights)}, ` +
                    `figure spread ${figureSpread.toFixed(3)}px ${JSON.stringify(figureHeights)}, ` +
                    `states ${JSON.stringify(states)})`,
                );
              }
              if (route === "/trend/" && width === 1440) {
                const overlapsVertically = (first, second) =>
                  first && second &&
                  Math.min(first.bottom, second.bottom) - Math.max(first.top, second.top) > 1;
                const readoutSharesOneBand =
                  end?.rowBoxes?.length === 2 &&
                  end.rowBoxes.every((row) => overlapsVertically(end.whenBox, row));
                if (!readoutSharesOneBand) {
                  failures.push(
                    `/trend/ @1440 ${theme}: desktop readout still reserves multiple text rows`,
                  );
                }
              }
              if (
                end?.overlapX > READOUT_OVERLAP_TOLERANCE_PX &&
                end?.overlapY > READOUT_OVERLAP_TOLERANCE_PX
              ) {
                failures.push(
                  `${route} @${width} ${theme}: .readout-panel overlaps .plot-area ` +
                    `by ${end.overlapX.toFixed(1)}x${end.overlapY.toFixed(1)}px`,
                );
              }
              await pressKey("Escape");
              const escaped = await readoutState();
              if (escaped?.reading !== "false") {
                failures.push(`${route} @${width} ${theme}: Escape did not close the readout`);
              }
            }
          }
        }
        console.log(`site-quality route readout: ${route} @${width}px ${theme}`);
        if (route === "/trend/" && width === 375 && theme === "light") {
          const trendMarks = await evaluate(`(() => {
            const plots = [...document.querySelectorAll(".plot[data-readout]")].slice(0, 3);
            const resolveColour = (name) => {
              const probe = document.createElement("span");
              probe.style.color = "var(" + name + ")";
              document.body.append(probe);
              const colour = getComputedStyle(probe).color;
              probe.remove();
              return colour;
            };
            const taiwanStroke = resolveColour("--taiwan");
            const mark = (element) => {
              const style = getComputedStyle(element);
              return {
                weight: parseFloat(style.strokeWidth),
                dash: style.strokeDasharray
                  .replaceAll("px", "")
                  .replaceAll(",", " ")
                  .replace(/\\s+/g, " ")
                  .trim(),
                stroke: style.stroke,
              };
            };
            const charts = plots.map((plot) => {
              const chart = plot.closest(".chart");
              return {
                lines: [...plot.querySelectorAll(".plot-line")].map(mark),
                seriesKeys: [...(chart?.querySelectorAll(
                  ".chart-key > li:not(.key-guide) .key-mark line",
                ) ?? [])].map(mark),
                guideKeys: [...(chart?.querySelectorAll(
                  ".chart-key > li.key-guide .key-mark line",
                ) ?? [])].map(mark),
                guidePaths: [...plot.querySelectorAll(".plot-area svg path:not(.plot-line)")].map(mark),
                taiwanStroke,
                payloadHasEmphasis: (() => {
                  try {
                    const raw = plot.querySelector(".plot-readout-data")?.textContent ?? "";
                    return JSON.parse(raw).series.some((series) => "emphasis" in series);
                  } catch { return true; }
                })(),
              };
            });
            return charts;
          })()`);
          const twoLineCharts = trendMarks?.slice(0, 2) ?? [];
          if (
            twoLineCharts.length !== 2 ||
            twoLineCharts.some(
              (chart) =>
                chart.lines.length !== 2 ||
                chart.seriesKeys.length !== 2 ||
                Math.abs(chart.lines[0].weight - 3) > 0.01 ||
                Math.abs(chart.lines[1].weight - 2.25) > 0.01 ||
                Math.abs(chart.seriesKeys[0].weight - 3) > 0.01 ||
                Math.abs(chart.seriesKeys[1].weight - 2.25) > 0.01 ||
                chart.lines[1].dash === "none" ||
                chart.seriesKeys.some((key, index) => key.dash !== chart.lines[index].dash) ||
                chart.payloadHasEmphasis,
            )
          ) {
            failures.push("/trend/ @375 light: primary/comparison line emphasis is not visual-only");
          }
          const zones = trendMarks?.[2];
          if (
            !zones ||
            zones.lines.length !== 8 ||
            zones.seriesKeys.length !== 8 ||
            zones.lines.some((line) => Math.abs(line.weight - 2.5) > 0.01) ||
            zones.seriesKeys.some(
              (key, index) =>
                Math.abs(key.weight - 2.5) > 0.01 || key.dash !== zones.lines[index].dash,
            ) ||
            zones.payloadHasEmphasis
          ) {
            failures.push("/trend/ @375 light: eight-zone lines do not retain a uniform weight");
          }
          const guideKeys = trendMarks?.flatMap((chart) => chart.guideKeys) ?? [];
          const legacyCharts = trendMarks?.slice(2) ?? [];
          const legacyGuidesMatch =
            legacyCharts.length === 1 &&
            legacyCharts.every((chart) =>
              chart.guidePaths.length === 2 &&
              chart.guidePaths.every(
                (guide) =>
                  Math.abs(guide.weight - 1.5) <= 0.01 &&
                  guide.stroke === chart.taiwanStroke,
              ) &&
              chart.guidePaths[0].dash === "5 4" &&
              chart.guidePaths[1].dash === "none",
            );
          if (
            guideKeys.length === 0 ||
            guideKeys.some(
              (guide) => Math.abs(guide.weight - 1.5) > 0.01,
            ) ||
            !legacyGuidesMatch
          ) {
            failures.push(
              "/trend/ @375 light: guide keys or figure 1.3 changed its legacy weight, colour, or dash pattern",
            );
          }
        }
        const guideAnnotations = route === "/trend/" ? await trendGuideSnapshot() : null;
        if (route === "/trend/") {
          recordTrendGuideGeometry(guideAnnotations, width, theme);
        }
        if (route === "/trend/" && theme === "light" && (width === 375 || width === 1440)) {
          const requiredText = [
            "台灣 PM2.5 年均標準", "2012.05.14", "起生效", "15", "2024.09.30", "\u8abf\u964d", "\u73fe\u884c\u6a19\u6e96", "12",
            "WHO \u5e74\u5747\u6307\u5f15", "5",
          ];
          if (
            !guideAnnotations?.transitionVisible || !guideAnnotations?.whoVisible ||
            requiredText.some((part) => !guideAnnotations.text.includes(part))
          ) {
            failures.push(`/trend/ @${width} light: figure 1.1 lacks the structured standard annotations`);
          }
          const colours = guideAnnotations?.colours ?? {};
          const tokens = guideAnnotations?.guideTokens ?? {};
          const badgeColours = [colours.old, colours.current, colours.whoValue];
          const seriesStrokes = guideAnnotations?.seriesStrokes ?? [];
          if (
            badgeColours.some((colour) => !colour) ||
            new Set(badgeColours).size !== 3 ||
            colours.old !== tokens.oldInk ||
            colours.current !== tokens.currentInk ||
            colours.whoValue !== tokens.who ||
            colours.old === tokens.seriesAll ||
            colours.old === tokens.seriesBalanced ||
            colours.current === tokens.seriesAll ||
            colours.current === tokens.seriesBalanced ||
            seriesStrokes[0] !== tokens.seriesAll ||
            seriesStrokes[1] !== tokens.seriesBalanced
          ) {
            failures.push(`/trend/ @${width} light: figure 1.1 does not keep legal-status and series colours distinct`);
          }
          if (width === 375) {
            if (guideAnnotations?.plotNotesVisible !== 1) {
              failures.push(
                "/trend/ @375 light: the phone plot does not retain exactly the on-line WHO label",
              );
            }
          } else {
            const boxes = guideAnnotations?.boxes;
            const centre = (box) => box ? (box.top + box.bottom) / 2 : NaN;
            if (
              !(centre(boxes?.old) < centre(boxes?.change) &&
                centre(boxes?.change) < centre(boxes?.current))
            ) {
              failures.push("/trend/ @1440 light: the 15 to 12 standard change does not read vertically downward");
            }
            if (boxes?.transition && boxes?.who) {
              const overlapX = Math.min(boxes.transition.right, boxes.who.right) -
                Math.max(boxes.transition.left, boxes.who.left);
              const overlapY = Math.min(boxes.transition.bottom, boxes.who.bottom) -
                Math.max(boxes.transition.top, boxes.who.top);
              if (overlapX > 1 && overlapY > 1) {
                failures.push("/trend/ @1440 light: the Taiwan and WHO annotations overlap");
              }
            }
          }
        }
        const renderedTheme = await evaluate(`(() => {
          const colour = document.querySelector("[data-theme-color]");
          const canvas = document.createElement("canvas");
          canvas.width = canvas.height = 1;
          const context = canvas.getContext("2d", { willReadFrequently: true });
          const pixel = (value) => {
            context.clearRect(0, 0, 1, 1);
            context.fillStyle = value;
            context.fillRect(0, 0, 1, 1);
            return [...context.getImageData(0, 0, 1, 1).data];
          };
          return {
            theme: document.documentElement.dataset.theme ?? null,
            colourScheme: getComputedStyle(document.documentElement).colorScheme,
            metaScheme: document.querySelector('meta[name="color-scheme"]')?.getAttribute("content"),
            iconMatchesTheme:
              document.querySelector("[data-theme-icon]")?.getAttribute("href") ===
              document.querySelector("[data-theme-icon]")?.getAttribute("data-" + ${JSON.stringify(theme)}),
            chromeMatchesPage:
              colour !== null &&
              pixel(colour.getAttribute("content"))
                .every((channel, index) => channel === pixel(getComputedStyle(document.body).backgroundColor)[index]),
          };
        })()`);
        if (renderedTheme?.theme !== theme) {
          failures.push(
            `${route} @${width} ${theme}: stored theme resolved to ${renderedTheme?.theme ?? "unknown"}`,
          );
        }
        if (renderedTheme?.colourScheme !== theme || renderedTheme?.metaScheme !== theme) {
          failures.push(`${route} @${width} ${theme}: native controls did not follow the stored theme`);
        }
        if (!renderedTheme?.chromeMatchesPage) {
          failures.push(`${route} @${width} ${theme}: browser chrome and page theme disagree`);
        }
        if (!renderedTheme?.iconMatchesTheme) {
          failures.push(`${route} @${width} ${theme}: favicon did not follow the stored theme`);
        }
        const r = await evaluate(PROBE);
        console.log(`site-quality route probe: ${route} @${width}px ${theme}`);
        if (!r) {
          failures.push(`${route} @${width} ${theme}: probe returned nothing`);
          continue;
        }
        if (width === 375 && r.compactIdentity) {
          r.compactIdentity.accessibleText = await accessibilityTextForSelector(
            "[data-site-identity]",
          );
          r.compactIdentity.accessibilitySource = "accessibility-tree";
        }
        totals.nodes += r.nodes;
        totals.tableWraps += r.tableWraps;
        totals.tableScrollers += r.tableScrollers;
        const expectedTableWraps = STATIC_TABLE_WRAPS.get(route);
        if (r.tableWraps !== expectedTableWraps) {
          failures.push(
            `${route} @${width} ${theme}: visible table wrapper inventory is ` +
              `${r.tableWraps}, expected ${expectedTableWraps}`,
          );
        }
        // Only the two endpoint widths feed the reported extremes; 768 would
        // otherwise be folded into a figure labelled 1440.
        if (width === 375 || width === 1440) {
          const key = width === 375 ? "smallestAt375" : "smallestAt1440";
          totals[key] = Math.min(totals[key], r.smallestFont);
        }
        if (r.smallestAnnotation > 0) {
          if (width === 375) {
            totals.annotationAt375 = Math.min(totals.annotationAt375, r.smallestAnnotation);
          }
          // 「一張圖的註記不該是整份文件裡最小的字」, checked per page rather than
          // against a pixel constant, and against this page's own body size.
          if (r.smallestAnnotation <= r.smallestFont) {
            failures.push(
              `${route} @${width} ${theme}: the smallest type on the page is an in-figure ` +
                `annotation (${r.smallestAnnotation}px)`,
            );
          }
          // Computed styles are serialized to four decimals, so allow one
          // ten-thousandth of a CSS pixel for that serialization while keeping the
          // asserted ratio at 95%.
          if (r.smallestAnnotation + CSS_PX_SERIALIZATION_EPSILON < r.body * 0.95) {
            failures.push(
              `${route} @${width} ${theme}: annotation ${r.smallestAnnotation}px is below the ` +
                `${r.body.toFixed(1)}px body it sits among`,
            );
          }
        }

        if (r.overflow > 0) {
          failures.push(
            `${route} @${width} ${theme}: document scrolls sideways by ${r.overflow}px ` +
              `(${r.tableScrollers} intentional table scrollers)`,
          );
        }
        for (const bad of r.invalidTableScrollers) {
          failures.push(
            `${route} @${width} ${theme}: .table-wrap clips ${bad.overflow}px ` +
              `with overflow-x ${bad.overflowX}`,
          );
        }
        for (const problem of new Set(r.invalidTableRules)) {
          failures.push(`${route} @${width} ${theme}: ${problem}`);
        }
        for (const bad of r.invalidChartText) {
          failures.push(
            `${route} @${width} ${theme}: ${bad.role} chart text is ${bad.size}px, ` +
              `expected at least ${bad.required}px`,
          );
        }
        for (const bad of r.invalidChartStrokes) {
          failures.push(
            `${route} @${width} ${theme}: .${bad.cls} stroke is ${bad.actual}px, ` +
              `expected ${bad.expected}px`,
          );
        }
        if (width === 1440) {
          if (r.railWidth > 272) {
            failures.push(`${route} @${width} ${theme}: rail width exceeds 272px`);
          }
          if (r.mainWidth < 720) {
            failures.push(`${route} @${width} ${theme}: main content is narrower than 720px`);
          }
          if (!r.handleVisible) {
            failures.push(`${route} @${width} ${theme}: handle is hidden below 1600px`);
          }
          if (r.railVisible) {
            failures.push(`${route} @${width} ${theme}: persistent rail is visible below 1600px`);
          }
        }
        if (width === 375 && !r.handleVisible) {
          failures.push(`${route} @${width} ${theme}: handle is hidden on mobile`);
        }
        if (width === 375 && r.railVisible) {
          failures.push(`${route} @${width} ${theme}: persistent rail is visible on mobile`);
        }
        if (width === 375) {
          const expectedIdentity = COMPACT_IDENTITY_ACCESSIBLE_NAMES.get(route);
          if (!expectedIdentity) {
            failures.push(`${route} @${width} ${theme}: compact identity contract is missing`);
          } else {
            for (const problem of compactIdentityProblems(r.compactIdentity, expectedIdentity)) {
              failures.push(`${route} @${width} ${theme}: ${problem}`);
            }
          }
        }
        for (const bad of r.lowContrast) {
          failures.push(
            `${route} @${width} ${theme}: Lc ${bad.lc} on ${JSON.stringify(bad.text)} ` +
              `(${bad.size}px, .${bad.cls})`,
          );
        }
        for (const bad of r.smallTargets) {
          failures.push(
            `${route} @${width} ${theme}: target ${bad.w}x${bad.h} on .${bad.cls} ` +
              `(floor ${bad.floor})`,
          );
        }
        for (const bad of r.collisions) {
          totals.collisions += 1;
          failures.push(
            `${route} @${width} ${theme}: ${JSON.stringify(bad.a)} and ` +
              `${JSON.stringify(bad.b)} leave ${bad.px}px clearance in .${bad.strip} ` +
              `(minimum ${AXIS_LABEL_CLEARANCE_PX}px)`,
          );
        }
        for (const bad of r.toolClashes) {
          totals.toolClashes += 1;
          failures.push(
            `${route} @${width} ${theme}: ${bad.figure}'s toolbar ${bad.why} — the ` +
              `1080px threshold in global.css was measured against the titles as ` +
              `they were, and one of them has outgrown it`,
          );
        }
        for (const bad of r.hyphenSigns) {
          totals.hyphenSigns += 1;
          failures.push(
            `${route} @${width} ${theme}: axis label ${JSON.stringify(bad.text)} in ` +
              `.${bad.strip} opens with a hyphen where a minus sign belongs — ` +
              `wrap it in axisNumber()`,
          );
        }
        for (const bad of r.markCollisions) {
          totals.markCollisions += 1;
          failures.push(
            `${route} @${width} ${theme}: ${bad.figure} draws two marks of one ` +
              `series ${bad.dx}px apart against a ${bad.w}px mark — the folds it ` +
              `exists to separate render as one`,
          );
        }
        if (route === "/trend/" && width === 375 && theme === "light") {
          const pickerState = await evaluate(`(async () => {
            const chart = [...document.querySelectorAll("main .evidence-figure")][2];
            const switches = [...(chart?.querySelectorAll("[data-series-switch]") ?? [])];
            const pills = [...(chart?.querySelectorAll(".key-pill") ?? [])];
            const paths = [...(chart?.querySelectorAll("path.plot-line") ?? [])];
            const reset = chart?.querySelector(".key-reset") ?? null;
            const plot = chart?.querySelector(".plot") ?? null;
            const visible = (element) => {
              if (!element) return false;
              const style = getComputedStyle(element);
              const rect = element.getBoundingClientRect();
              return style.display !== "none" && style.visibility !== "hidden" &&
                rect.width > 0 && rect.height > 0;
            };
            const frame = () => new Promise((resolve) => requestAnimationFrame(resolve));
            if (!chart || switches.length !== 8 || pills.length !== 8 || paths.length !== 8 || !reset || !plot) {
              return null;
            }
            pills[0].click();
            pills[1].click();
            await new Promise((resolve) => setTimeout(resolve, 180));
            plot.focus();
            const filtered = {
              checked: switches.filter((input) => input.checked).length,
              displays: paths.map((path) => getComputedStyle(path).display),
              rows: chart.querySelectorAll(".readout-row").length,
              resetVisible: visible(reset),
              announcement: chart.querySelector("[data-pick-say]")?.textContent ?? "",
            };
            for (const pill of pills.slice(2)) pill.click();
            await new Promise((resolve) => setTimeout(resolve, 180));
            const empty = {
              checked: switches.filter((input) => input.checked).length,
              displays: paths.map((path) => getComputedStyle(path).display),
              rows: chart.querySelectorAll(".readout-row").length,
              announcement: chart.querySelector("[data-pick-say]")?.textContent ?? "",
            };
            reset.focus();
            reset.click();
            await frame();
            await frame();
            const restored = {
              checked: switches.filter((input) => input.checked).length,
              rows: chart.querySelectorAll(".readout-row").length,
              resetVisible: visible(reset),
              focusReturned: document.activeElement === switches[0],
            };
            return { filtered, empty, restored };
          })()`);
          const pickerProblems = trendPickerInteractionProblems(pickerState);
          if (pickerProblems.length) {
            failures.push(
              `/trend/ @375 light: air-zone picker and readout did not round-trip ` +
                `${pickerProblems.join(", ")} ${JSON.stringify(pickerState)}`,
            );
          }
          const zoomTools = await evaluate(`(() => {
            const root = document.querySelector("main figure:has(.plot[data-readout])");
            const enlarge = [...(root?.querySelectorAll(".fig-tool") ?? [])]
              .find((item) => item.textContent?.trim() === "放大");
            const visible = (element) => {
              if (!element) return false;
              const style = getComputedStyle(element);
              const rect = element.getBoundingClientRect();
              return style.display !== "none" && style.visibility !== "hidden" &&
                rect.width > 0 && rect.height > 0;
            };
            if (!root || !enlarge) return null;
            enlarge.click();
            const dialog = document.querySelector(".fig-zoom");
            const toolbar = dialog?.querySelector(".fig-tools") ?? null;
            const download = [...(toolbar?.querySelectorAll(".fig-tool") ?? [])]
              .find((item) => item.getAttribute("aria-label")?.startsWith("下載 PNG："));
            const plotArea = dialog?.querySelector(".plot-area") ?? null;
            /*
              The header's controls are one row, so they are one control.
              (No backticks: this whole block is a template literal.)

              下載 is moved into this header while the dialog is open and sits
              beside 關閉, and the two were sized by different rules: fig-tool to
              the interaction floor, fig-shut by the base button's padding.
              Measured 83.41 x 45 against 79 x 59.53 — a 14.5px difference in
              height between two buttons on one line, which the owner saw before
              anything here did. Both boxes are collected so the check is about
              the pair agreeing, not about either number.
            */
            const shutButton = dialog?.querySelector(".fig-shut") ?? null;
            const box = (element) => {
              if (!element) return null;
              const rect = element.getBoundingClientRect();
              return { width: rect.width, height: rect.height, top: rect.top };
            };
            const openState = {
              open: Boolean(dialog?.open),
              toolbarParent: toolbar?.parentElement?.className ?? "",
              enlargeVisible: visible(enlarge),
              downloadVisible: visible(download),
              plotHeight: plotArea?.getBoundingClientRect().height ?? 0,
              headControls: {
                download: box(download),
                shut: box(shutButton),
              },
            };
            dialog?.querySelector(".fig-shut")?.click();
            return {
              ...openState,
              restoredToolbarParent: toolbar?.parentElement?.tagName.toLowerCase() ?? "",
              restoredFocus: document.activeElement === enlarge,
            };
          })()`);
          if (
            !zoomTools?.open ||
            zoomTools.toolbarParent !== "fig-zoom-head" ||
            zoomTools.enlargeVisible ||
            !zoomTools.downloadVisible ||
            zoomTools.plotHeight + CSS_PX_SERIALIZATION_EPSILON < 320 ||
            zoomTools.restoredToolbarParent !== "figure" ||
            !zoomTools.restoredFocus
          ) {
            failures.push(
              `/trend/ @375 light: enlarged figure tools changed ${JSON.stringify(zoomTools)}`,
            );
          }
          for (const problem of zoomHeadControlProblems(zoomTools?.headControls)) {
            failures.push(`/trend/ @375 light: ${problem}`);
          }
          const exported = await evaluate(`(async () => {
            const shell = [...document.querySelectorAll("main .evidence-figure")][2];
            const root = shell?.matches("figure") ? shell : shell?.querySelector("figure");
            const button = [...(root?.querySelectorAll(".fig-tool") ?? [])]
              .find((item) => item.getAttribute("aria-label")?.startsWith("下載 PNG："));
            const pills = [...(root?.querySelectorAll(".key-pill") ?? [])];
            if (!root || !button || pills.length !== 8) return null;
            pills[0].click();
            pills[1].click();
            await new Promise((resolve) => setTimeout(resolve, 180));
            const outerBlockSize = (element) => {
              const style = getComputedStyle(element);
              return element.getBoundingClientRect().height +
                parseFloat(style.marginTop || "0") +
                parseFloat(style.marginBottom || "0");
            };
            const tools = root.querySelector(".fig-tools");
            const toolsShed = tools ? outerBlockSize(tools) : 0;
            const dockShed = [...root.querySelectorAll(".readout-dock")]
              .reduce((total, dock) => total + outerBlockSize(dock), 0);
            const expectedInner = Math.max(
              root.getBoundingClientRect().height - toolsShed - dockShed,
              1,
            );
            const originalSerialize = XMLSerializer.prototype.serializeToString;
            const originalEncode = window.encodeURIComponent;
            let transient = null;
            let filteredState = null;
            let serializedSvg = null;
            XMLSerializer.prototype.serializeToString = function(node) {
              transient = {
                dock: node.querySelectorAll?.(".readout-dock").length ?? -1,
                tools: node.querySelectorAll?.(".fig-tools").length ?? -1,
                payload: node.querySelectorAll?.(".plot-readout-data").length ?? -1,
                rule: node.querySelectorAll?.(".readout-rule").length ?? -1,
                dot: node.querySelectorAll?.(".readout-dot").length ?? -1,
                panel: node.querySelectorAll?.(".readout-panel").length ?? -1,
                say: node.querySelectorAll?.(".readout-say").length ?? -1,
              };
              const sandbox = document.createElement("div");
              sandbox.style.cssText =
                "position:fixed;left:-100000px;top:0;width:" +
                root.getBoundingClientRect().width + "px";
              sandbox.append(node.cloneNode(true));
              document.body.append(sandbox);
              filteredState = {
                checkedAttributes: [...sandbox.querySelectorAll("[data-series-switch]")]
                  .map((input) => input.hasAttribute("checked")),
                pathDisplays: [...sandbox.querySelectorAll("path.plot-line")]
                  .map((path) => getComputedStyle(path).display),
              };
              sandbox.remove();
              return originalSerialize.call(this, node);
            };
            window.encodeURIComponent = function(value) {
              if (String(value).startsWith("<svg ")) serializedSvg = String(value);
              return originalEncode(value);
            };
            try {
              button.click();
            } finally {
              XMLSerializer.prototype.serializeToString = originalSerialize;
              window.encodeURIComponent = originalEncode;
            }
            const actualInner = Number(
              serializedSvg?.match(/<foreignObject[^>]*height="([^"]+)"/)?.[1],
            );
            const actualFrame = Number(
              serializedSvg?.match(/viewBox="0 0 [^ ]+ ([^"]+)"/)?.[1],
            );
            return {
              transient,
              filteredState,
              dockShed,
              expectedInner,
              actualInner,
              expectedFrame: Math.ceil(expectedInner) + 36,
              actualFrame,
            };
          })()`);
          if (!exported?.transient || Object.values(exported.transient).some((count) => count !== 0)) {
            failures.push(
              `/trend/ @375 light: PNG export retained transient nodes ${JSON.stringify(exported?.transient)}`,
            );
          }
          for (const problem of trendFilteredExportProblems(exported?.filteredState)) {
            failures.push(
              `/trend/ @375 light: filtered PNG state changed: ${problem} ` +
                JSON.stringify(exported?.filteredState),
            );
          }
          if (
            !Number.isFinite(exported?.dockShed) ||
            exported.dockShed <= 0 ||
            !Number.isFinite(exported?.actualInner) ||
            !Number.isFinite(exported?.actualFrame) ||
            Math.abs(exported.actualInner - exported.expectedInner) > 1 ||
            Math.abs(exported.actualFrame - exported.expectedFrame) > 1
          ) {
            failures.push(
              `/trend/ @375 light: PNG export frame retained dock space ${JSON.stringify(exported)}`,
            );
          }
        }
      }
    }
  }

  console.log("site-quality stage: Figure 1.1 desktop zoom fit");
  await restartBrowser();
  await send("Emulation.setDeviceMetricsOverride", {
    width: 1600,
    height: 900,
    deviceScaleFactor: 1,
    mobile: false,
  });
  await send("Emulation.setEmulatedMedia", {
    media: "",
    features: [{ name: "prefers-color-scheme", value: "light" }],
  });
  await send("Page.navigate", { url: `${origin}/trend/` });
  if (!(await settled(evaluate, 8000, "/trend/ @1600x900 Figure 1.1 zoom fit"))) {
    failures.push("/trend/ @1600x900 light: Figure 1.1 zoom fit never finished styling");
  } else {
    const zoomFit = await evaluate(`(() => {
      const shell = [...document.querySelectorAll("main .evidence-figure")][0];
      const root = shell?.matches("figure") ? shell : shell?.querySelector("figure");
      const enlarge = [...(root?.querySelectorAll(".fig-tool") ?? [])]
        .find((item) => item.textContent?.trim() === "放大");
      if (!root || !enlarge) return null;
      enlarge.click();
      const dialog = document.querySelector(".fig-zoom");
      const stage = dialog?.querySelector(".fig-zoom-body") ?? null;
      const plotArea = dialog?.querySelector(".plot-area") ?? null;
      const block = (element) => {
        if (!element) return 0;
        const style = getComputedStyle(element);
        return element.getBoundingClientRect().height +
          parseFloat(style.marginTop || "0") + parseFloat(style.marginBottom || "0");
      };
      const state = {
        open: Boolean(dialog?.open),
        stageClientHeight: stage?.clientHeight ?? 0,
        stageScrollHeight: stage?.scrollHeight ?? 0,
        plotHeight: plotArea?.getBoundingClientRect().height ?? 0,
        rootHeight: root.getBoundingClientRect().height,
        keyHeight: block(root.querySelector(".key-form")),
        plotCardHeight: block(root.querySelector(".plot-card")),
        readoutHeight: block(root.querySelector(".readout-dock")),
        captionHeight: block(root.querySelector("figcaption")),
      };
      dialog?.querySelector(".fig-shut")?.click();
      return state;
    })()`);
    if (!zoomFit?.open) {
      failures.push(`/trend/ @1600x900 light: Figure 1.1 zoom did not open ${JSON.stringify(zoomFit)}`);
    }
    for (const problem of trendZoomFitProblems(zoomFit)) {
      failures.push(`/trend/ @1600x900 light: ${problem} ${JSON.stringify(zoomFit)}`);
    }
  }

  console.log("site-quality stage: wide reading layout");
  await restartBrowser();
  await send("Emulation.setDeviceMetricsOverride", {
    width: 1920,
    height: 1000,
    deviceScaleFactor: 1,
    mobile: false,
  });
  await send("Emulation.setEmulatedMedia", {
    media: "",
    features: [{ name: "prefers-color-scheme", value: "light" }],
  });
  await evaluate('localStorage.setItem("twair-theme", "light")');
  for (const route of ["/trend/", "/space/", "/detection/"]) {
    await send("Page.navigate", { url: `${origin}${route}` });
    if (!(await settled(evaluate, 8000, `${route} @1920px wide reading layout`))) {
      failures.push(`${route} @1920x1000 light: wide reading layout never finished styling`);
      continue;
    }
    const snapshot = await wideReadingLayoutSnapshot();
    for (const problem of wideReadingLayoutProblems(snapshot, route)) {
      failures.push(`${route} @1920x1000 light: ${problem}`);
    }
  }

  console.log("site-quality stage: focused trend guide widths");
  for (const [width, height] of [
    [480, 900],
    [588, 900],
    [600, 900],
    [719, 900],
    [720, 900],
    [721, 900],
    [753, 900],
    [754, 900],
    [768, 900],
    [1000, 900],
    [1280, 900],
    [1920, 900],
  ]) {
    for (const theme of ["light", "dark"]) {
      await restartBrowser();
      await send("Emulation.setDeviceMetricsOverride", {
        width,
        height,
        deviceScaleFactor: 1,
        mobile: false,
      });
      const osTheme = theme === "light" ? "dark" : "light";
      await send("Emulation.setEmulatedMedia", {
        media: "",
        features: [{ name: "prefers-color-scheme", value: osTheme }],
      });
      await send("Page.navigate", { url: `${origin}/` });
      if (!(await settled(evaluate, 8000, `/ @${width}px ${theme} trend setup`))) {
        failures.push(`/ @${width}x${height} ${theme}: trend setup never finished styling`);
        continue;
      }
      await evaluate(`localStorage.setItem("twair-theme", ${JSON.stringify(theme)})`);
      await send("Page.navigate", { url: `${origin}/trend/` });
      if (!(await settled(evaluate, 8000, `/trend/ @${width}px ${theme} focused guide`))) {
        failures.push(`/trend/ @${width}x${height} ${theme}: focused guide never finished styling`);
        continue;
      }
      const snapshot = await trendGuideSnapshot();
      recordTrendGuideGeometry(snapshot, width, theme);
      if (width === 588 && theme === "light") {
        await evaluate(`(() => {
          const chart = [...document.querySelectorAll(".chart")][1];
          chart?.querySelector(".plot .plot-note")?.remove();
          chart?.querySelector(".chart-key .key-guide")?.remove();
        })()`);
        const mutationProblems = trendGuideCopyProblems(
          await trendGuideSnapshot(),
          width,
          theme,
        );
        const removedCopiesDetected = mutationProblems.some((problem) =>
          problem.includes("Figure 1.2 guide copies disagree with source count 1") &&
          problem.includes("plot rendered 0") && problem.includes("key rendered 0"),
        );
        if (!removedCopiesDetected) {
          failures.push(
            `/trend/ @${width} ${theme}: guide-copy detector accepts simultaneous Figure 1.2 removal`,
          );
        }
      }
    }
  }

  console.log("site-quality stage: fractional trend guide widths");
  for (const targetWidth of [480.5, 888, 888.25]) {
    for (const theme of ["light", "dark"]) {
      await restartBrowser();
      await send("Emulation.setDeviceMetricsOverride", {
        width: 1920,
        height: 900,
        deviceScaleFactor: 1,
        mobile: false,
      });
      const osTheme = theme === "light" ? "dark" : "light";
      await send("Emulation.setEmulatedMedia", {
        media: "",
        features: [{ name: "prefers-color-scheme", value: osTheme }],
      });
      await send("Page.navigate", { url: `${origin}/` });
      if (!(await settled(evaluate, 8000, `/ @fractional ${targetWidth}px ${theme} trend setup`))) {
        failures.push(`/ @fractional ${targetWidth}px ${theme}: trend setup never finished styling`);
        continue;
      }
      await evaluate(`localStorage.setItem("twair-theme", ${JSON.stringify(theme)})`);
      await send("Page.navigate", { url: `${origin}/trend/` });
      if (!(await settled(
        evaluate,
        8000,
        `/trend/ @fractional ${targetWidth}px ${theme} focused guide`,
      ))) {
        failures.push(
          `/trend/ @fractional ${targetWidth}px ${theme}: focused guide never finished styling`,
        );
        continue;
      }
      if (!(await forceFirstTrendChartContentWidth(targetWidth))) {
        failures.push(`/trend/ @fractional ${targetWidth}px ${theme}: first chart is missing`);
        continue;
      }
      await settlePaint(evaluate, `/trend/ @fractional ${targetWidth}px ${theme} resize`);
      const snapshot = await trendGuideSnapshot();
      console.log("site-quality fractional trend " + JSON.stringify({
        viewportWidth: 1920,
        targetWidth,
        theme,
        chartContentWidth: snapshot?.chartContentWidth,
        plotContentWidth: snapshot?.plotContentWidth,
        transition: snapshot?.transitionPlacement,
        transitionFontSize: snapshot?.transitionTypography?.fontSize,
        rootFontSize: snapshot?.transitionTypography?.rootSize,
        seriesOcclusionCount: snapshot?.seriesOcclusionCount,
      }));
      if (
        !Number.isFinite(snapshot?.chartContentWidth) ||
        Math.abs(snapshot.chartContentWidth - targetWidth) > 0.02
      ) {
        failures.push(
          `/trend/ @fractional ${targetWidth}px ${theme}: measured chart content is ` +
          String(snapshot?.chartContentWidth) + "px",
        );
      }
      recordTrendGuideGeometry(snapshot, `fractional ${targetWidth}px`, theme);
    }
  }

  for (const [width, height] of [
    [390, 844],
    [1280, 720],
    [1920, 1000],
  ]) {
    for (const theme of ["light", "dark"]) {
      await restartBrowser();
      await send("Emulation.setDeviceMetricsOverride", {
        width,
        height,
        deviceScaleFactor: 1,
        mobile: width < 500,
      });
      await send("Page.navigate", { url: `${origin}/` });
      if (!(await settled(evaluate, 8000, `/ @${width}px ${theme} browser restart`))) {
        failures.push(`/ @${width}x${height} ${theme}: replacement browser never finished styling`);
      }
      await evaluate(`localStorage.setItem("twair-theme", ${JSON.stringify(theme)})`);
      const osTheme = theme === "light" ? "dark" : "light";
      await send("Emulation.setEmulatedMedia", {
        media: "",
        features: [{ name: "prefers-color-scheme", value: osTheme }],
      });
      await send("Page.navigate", { url: `${origin}/` });
      if (!(await settled(evaluate, 8000, `/ @${width}px ${theme}`))) {
        failures.push(`/ @${width}x${height} ${theme}: page never finished styling`);
        continue;
      }
      for (const problem of await homepageFirstViewport()) {
        failures.push(`/ @${width}x${height} ${theme}: ${problem}`);
      }
      if (width === 390 && theme === "light") {
        for (const problem of await homepageStructureProblems({ enhanced: true })) {
          failures.push(`/ @${width}x${height} ${theme}: ${problem}`);
        }
        for (const problem of await chapterIndexProblems()) {
          failures.push(`/ @${width}x${height} ${theme}: ${problem}`);
        }
        const geometryMutations = [
          {
            name: "scaled station mark",
            selector: "[data-homepage-map] .dot",
            property: "scale",
            value: "50",
            expected: "station mark leaves its container",
          },
          {
            name: "vertically translated tick mark",
            selector: "[data-homepage-map-legend] .scale-ticks > span",
            property: "transform",
            value: "translate(-50%, 1000px)",
            expected: "tick mark leaves its container",
          },
          {
            name: "disabled county-label container",
            selector: "[data-homepage-map]",
            property: "container-type",
            value: "normal",
            expected: "county label pairs overlap",
          },
          {
            name: "hidden primary route",
            selector: "[data-homepage-primary-route]",
            property: "display",
            value: "none",
            expected: "primary route is not visible within the first mobile viewport",
          },
        ];
        for (const mutation of geometryMutations) {
          const problems = await homepageFirstViewport({
            requireVerticalViewport: false,
            requireScrollZero: false,
            geometryMutation: mutation,
          });
          if (!problems.some((problem) => problem.includes(mutation.expected))) {
            failures.push(
              `/ @${width}x${height} ${theme}: homepage geometry accepts ${mutation.name}`,
            );
          }
        }
      }
    }
  }

  console.log("site-quality stage: homepage 480px type boundary");
  for (const width of [480, 481]) {
    await send("Emulation.setDeviceMetricsOverride", {
      width,
      height: 900,
      deviceScaleFactor: 1,
      mobile: false,
    });
    await send("Emulation.setEmulatedMedia", {
      media: "",
      features: [{ name: "prefers-color-scheme", value: "light" }],
    });
    await send("Page.navigate", { url: `${origin}/` });
    if (!(await settled(evaluate, 8000, `/ @${width}px mobile type boundary`))) {
      failures.push(`/ @${width}px mobile type boundary: page never finished styling`);
      continue;
    }
    for (const problem of await homepageFirstViewport({ requireVerticalViewport: false })) {
      if (problem.includes("type ratio changed")) {
        failures.push(`/ @${width}px mobile type boundary: ${problem}`);
      }
    }
  }

  console.log("site-quality stage: Sources conditional atlas");
  for (const [width, height] of [[375, 812], [768, 1024], [1024, 900], [1440, 900]]) {
    for (const theme of ["light", "dark"]) {
      await send("Emulation.setDeviceMetricsOverride", {
        width,
        height,
        deviceScaleFactor: 1,
        mobile: width < 500,
      });
      await evaluate(`localStorage.setItem("twair-theme", ${JSON.stringify(theme)})`);
      await send("Emulation.setEmulatedMedia", {
        media: "",
        features: [{ name: "prefers-color-scheme", value: theme === "light" ? "dark" : "light" }],
      });
      await send("Page.navigate", { url: `${origin}/sources/` });
      if (!(await settled(evaluate, 8000, `/sources/ @${width}px ${theme} conditional atlas`))) {
        failures.push(`/sources/ @${width} ${theme}: conditional atlas never finished styling`);
        continue;
      }
      const atlas = await evaluate(SOURCES_ATLAS_STATE_PROBE);
      for (const problem of sourcesAtlasProblems(atlas, width, height)) {
        failures.push(`/sources/ @${width} ${theme}: ${problem}`);
      }
    }
  }
  await send("Emulation.setDeviceMetricsOverride", {
    width: 1024,
    height: 900,
    deviceScaleFactor: 1,
    mobile: false,
  });
  await evaluate('localStorage.setItem("twair-theme", "light")');
  await send("Emulation.setEmulatedMedia", {
    media: "",
    features: [{ name: "prefers-color-scheme", value: "dark" }],
  });
  await send("Page.navigate", { url: `${origin}/sources/` });
  if (!(await settled(evaluate, 8000, "/sources/ conditional atlas station transition"))) {
    failures.push("/sources/ conditional atlas station transition never finished styling");
  } else {
    const transitioned = await evaluate(SOURCES_ATLAS_TRANSITION_PROBE);
    for (const problem of sourcesAtlasProblems(transitioned, 1024, 900, { requireRestoration: true })) {
      failures.push(`/sources/ station transition: ${problem}`);
    }
  }

  await send("Emulation.setEmulatedMedia", { media: "", features: [] });
  await evaluate('localStorage.setItem("twair-theme", "light")');
  console.log("site-quality stage: 200% text zoom");
  const checkTextZoom = async (route, width, height, suffix = "") => {
    const state = `${route} @${width}x${height} 200% text${suffix}`;
    console.log(`site-quality text zoom: ${state}`);
    await send("Emulation.setDeviceMetricsOverride", {
      width,
      height,
      deviceScaleFactor: 1,
      mobile: width < 500,
    });
    await send("Page.navigate", { url: `${origin}${route}` });
    if (!(await settled(evaluate))) {
      failures.push(`${state}: page never finished styling`);
      return;
    }
    await evaluate(`(() => {
      const base = parseFloat(getComputedStyle(document.documentElement).fontSize);
      document.documentElement.style.setProperty("font-size", String(base * 2) + "px", "important");
      return true;
    })()`);
    await settlePaint(evaluate);
    const zoomed = await evaluate(`(() => {
        const visible = (element) => {
          const style = getComputedStyle(element);
          const rect = element.getBoundingClientRect();
          return style.display !== "none" && style.visibility !== "hidden" &&
            rect.width > 0 && rect.height > 0;
        };
        const clippedLabels = [...document.querySelectorAll("label, button, summary, .status")]
          .filter(visible)
          .filter((element) => {
            const style = getComputedStyle(element);
            return (style.overflowX === "hidden" || style.overflowX === "clip") &&
              element.scrollWidth - element.clientWidth > 1;
          })
          .map((element) => String(element.textContent || element.tagName).trim().slice(0, 24));
        return {
          overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
          clippedLabels,
          homepage: ${JSON.stringify(route === "/")} ? (() => {
            const tickRects = [...document.querySelectorAll("[data-homepage-map-legend] .scale-ticks > span")]
              .filter(visible)
              .map((tick) => tick.getBoundingClientRect())
              .sort((a, b) => a.left - b.left);
            const tickOverlaps = tickRects.slice(1).filter((rect, index) => {
              const previous = tickRects[index];
              const overlapX = Math.min(previous.right, rect.right) - Math.max(previous.left, rect.left);
              const overlapY = Math.min(previous.bottom, rect.bottom) - Math.max(previous.top, rect.top);
              return overlapX > 1 && overlapY > 1;
            }).length;
            return {
              tickOverlaps,
            };
          })() : null,
        };
    })()`);
    totals.zoomRoutes += 1;
    if (zoomed?.overflow > 0) {
      failures.push(`${state}: document scrolls sideways by ${zoomed.overflow}px`);
    }
    if (route === "/sources/") {
      const mainText = await evaluate(
        'document.querySelector("main")?.innerText.replace(/\\s+/g, " ").trim() ?? ""',
      );
      for (const problem of sourcesClaimBoundaryProblems(mainText ?? "")) {
        failures.push(`${state}: ${problem}`);
      }
      const atlas = await evaluate(SOURCES_ATLAS_STATE_PROBE);
      for (const problem of sourcesAtlasProblems(atlas, width, height)) {
        failures.push(`${state}: ${problem}`);
      }
    }
    if (route === "/detection/") {
      const mainText = await evaluate(
        'document.querySelector("main")?.innerText.replace(/\\s+/g, " ").trim() ?? ""',
      );
      for (const problem of detectionClaimBoundaryProblems(mainText ?? "")) {
        failures.push(`${state}: ${problem}`);
      }
      const estimateTable = await evaluate(DETECTION_ESTIMATE_TABLE_PROBE);
      for (const problem of detectionEstimateTableProblems(estimateTable)) {
        failures.push(`${state}: ${problem}`);
      }
      const detectionState = await detectionLimitationBriefSnapshot("zoom");
      for (const problem of detectionLimitationBriefProblems(
        detectionState,
        EXPECTED_DETECTION_EVENTS,
        detectionState?.viewport,
      )) {
        failures.push(`${state}: ${problem}`);
      }
    }
    if (route === "/health/") {
      const healthState = await healthAssumptionLedgerSnapshot("zoom");
      for (const problem of healthAssumptionLedgerProblems(
        healthState,
        EXPECTED_HEALTH_EVIDENCE,
        healthState?.viewport,
      )) {
        failures.push(`${state}: ${problem}`);
      }
    }
    if (route === "/forecast/") {
      const forecastState = await forecastHorizonDecisionSnapshot("zoom");
      for (const problem of forecastHorizonDecisionProblems(
        forecastState,
        EXPECTED_FORECAST_EVIDENCE,
        forecastState?.viewport,
      )) {
        failures.push(`${state}: ${problem}`);
      }
    }
    if (route === "/methods/") {
      const methodsState = await methodsCaseIndexSnapshot("zoom");
      for (const problem of methodsCaseIndexProblems(methodsState, methodsState?.viewport)) {
        failures.push(`${state}: ${problem}`);
      }
    }
    if (route === "/data/") {
      const dataState = await dataProvenanceRegisterSnapshot("zoom");
      for (const problem of dataProvenanceRegisterProblems(dataState, dataState?.viewport)) {
        failures.push(`${state}: ${problem}`);
      }
    }
    if (route === "/explore/") {
      const explorerState = await explorerGuidedWorkspaceSnapshot("zoom");
      for (const problem of explorerGuidedWorkspaceProblems(explorerState, { width, height })) {
        failures.push(`${state}: ${problem}`);
      }
    }
    if (HISTORICAL_STATION_ROUTES.has(route)) {
      const historicalState = await evaluate(HISTORICAL_STATION_DISCLOSURE_PROBE);
      for (const problem of historicalStationCopyProblems(
        route, historicalState?.text ?? "", historicalState?.hrefs ?? null,
      )) {
        failures.push(`${state}: ${problem}`);
      }
      if (route === "/data/") {
        for (const problem of publicationDisclosureProblems(historicalState)) {
          failures.push(`${state}: ${problem}`);
        }
      }
    }
    for (const label of zoomed?.clippedLabels ?? []) {
      failures.push(`${state}: clipped label ${JSON.stringify(label)}`);
    }
    if (route === "/") {
      for (const problem of await homepageFirstViewport({
        requireVerticalViewport: false,
        requireScrollZero: false,
      })) {
        failures.push(`${state}: ${problem}`);
      }
      for (const problem of await homepageStructureProblems({ enhanced: true })) {
        failures.push(`${state}: ${problem}`);
      }
      if (zoomed?.homepage?.tickOverlaps > 0) {
        failures.push(
          `${state}: ${zoomed.homepage.tickOverlaps} adjacent legend tick pairs overlap`,
        );
      }
    }
    if (route === "/trend/" || route === "/space/") {
      const contract = route === "/trend/"
        ? TREND_READING_MAP_CONTRACT
        : SPACE_READING_MAP_CONTRACT;
      const readingMapZoomSnapshot = await readingMapSnapshot({
        targetIds: contract.targetIds,
        measureAnchors: false,
      });
      for (const problem of readingMapProblems(readingMapZoomSnapshot, contract)) {
        failures.push(`${state}: ${problem}`);
      }
    }
    if (route === "/trend/") {
      const trendZoomSnapshot = await trendGuideSnapshot();
      recordTrendGuideGeometry(
        trendZoomSnapshot,
        `${width}x${height} 200% text${suffix}`,
        "light",
      );
    }
    const zoomFocus = await focusVisibleStates([
      { name: "zoomed control", selector: "button, select, summary", required: true },
    ]);
    for (const state of zoomFocus?.[0]?.states ?? []) {
      if (!state.active || !state.focusVisible || state.outlineStyle === "none" || state.outlineWidth < 2) {
        failures.push(
          `${route} @${width}x${height} 200% text${suffix}: ` +
            `${JSON.stringify(state.label)} lost its focus outline`,
        );
      }
      if (!state.inViewport) {
        failures.push(
          `${route} @${width}x${height} 200% text${suffix}: focused ` +
            `${JSON.stringify(state.label)} is clipped outside the viewport`,
        );
      }
    }
  };
  for (const route of TEXT_ZOOM_ROUTES) {
    await checkTextZoom(route, 1440, 900);
  }
  await checkTextZoom("/", 900, 500, " short reflow");

  if (totals.chapterOpeningChecks !== 60) {
    failures.push(
      `chapter opening matrix exercised ${totals.chapterOpeningChecks} route-viewports, expected 60`,
    );
  }
  if (totals.chapterEndingChecks !== 60) {
    failures.push(
      `chapter ending matrix exercised ${totals.chapterEndingChecks} route-viewports, expected 60`,
    );
  }
  if (totals.smallestAt375 < MIN_FONT_PX) {
    failures.push(`smallest type at 375px is ${totals.smallestAt375}px (floor ${MIN_FONT_PX})`);
  }
  if (totals.readouts === 0) {
    failures.push("readout keyboard and overlap probe exercised no chart");
  }
  if (totals.evidenceFocusChecks === 0) {
    failures.push("focus-visible probe exercised no evidence disclosure");
  }
  if (totals.tableWraps === 0 || totals.tableWraps !== EXPECTED_TABLE_WRAPS) {
    failures.push(
      `table wrapper matrix totals ${totals.tableWraps}, expected ${EXPECTED_TABLE_WRAPS}`,
    );
  }
  if (totals.tableScrollers === 0 || totals.tableScrollers !== EXPECTED_TABLE_SCROLLERS) {
    failures.push(
      `intentional table scroller matrix totals ${totals.tableScrollers}, ` +
        `expected ${EXPECTED_TABLE_SCROLLERS}`,
    );
  }

  console.log(`routes checked   : ${ROUTES.length} x 3 widths x 2 themes`);
  console.log(`text nodes       : ${totals.nodes.toLocaleString("en-US")}`);
  console.log(`smallest type    : ${totals.smallestAt375}px @375, ${totals.smallestAt1440}px @1440`);
  console.log(`smallest in-figure annotation @375 : ${totals.annotationAt375}px`);
  console.log(`overlapping axis labels : ${totals.collisions}`);
  console.log(`overlapping fold marks  : ${totals.markCollisions}`);
  console.log(`hyphens used as minus   : ${totals.hyphenSigns}`);
  console.log(`figure toolbar clashes  : ${totals.toolClashes}`);
  console.log(`readouts exercised : ${totals.readouts}`);
  console.log(`table wraps       : ${totals.tableWraps} (${totals.tableScrollers} intentional scrollers)`);
  console.log(`focus checks      : ${totals.focusChecks}`);
  console.log(`no-JavaScript     : ${totals.noScriptRoutes} routes`);
  console.log(`chapter openings : ${totals.chapterOpeningChecks} route-viewports`);
  console.log(`chapter endings  : ${totals.chapterEndingChecks} route-viewports`);
  console.log(`200% text zoom    : ${totals.zoomRoutes} routes`);
  console.log(`APCA floor       : Lc ${MIN_LC}`);
  console.log(`problems         : ${failures.length}`);
  for (const line of failures.slice(0, 40)) console.log(`  FAIL: ${line}`);
  if (failures.length > 40) console.log(`  ... and ${failures.length - 40} more`);

  return failures.length ? 1 : 0;
  });
}

try {
  process.exit(await (SELF_TEST ? lifecycleSelfTest() : main()));
} catch (error) {
  console.error(
    `site quality preflight failed: ${error instanceof Error ? error.message : String(error)}`,
  );
  process.exit(1);
}
