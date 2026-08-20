/**
 * Re-derive the design claims PRODUCT.md makes, and fail if they stop holding.
 *
 * PRODUCT.md's accessibility section originally stated four measured properties:
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
 * PRODUCT.md's 2026-08-04 current-state rule supersedes those old size and
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

// ── what PRODUCT.md claims ───────────────────────────────────────────────────

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
  ["/data/", "第十章　資料與方法"],
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
  "/methods/",
  "/explore/",
  "/data/",
];

/**
 * 2026-08-03 — measured from the built route DOM, before these inventories
 * became assertions. Native figures/captions total 14; secondary disclosures
 * total 8 (Detection 2, Methods 6); Explorer contributes the one SQL
 * disclosure. Keeping every zero is deliberate: a route cannot silently gain
 * or lose an object while the site-wide sum happens to stay constant.
 */
const STATIC_NATIVE_FIGURES = new Map([
  ["/", 0],
  ["/trend/", 3],
  ["/stations/", 0],
  ["/space/", 2],
  ["/sources/", 1],
  ["/detection/", 1],
  ["/forecast/", 3],
  ["/health/", 2],
  ["/methods/", 2],
  ["/explore/", 0],
  ["/data/", 0],
]);
const CHART_ROUTES = new Set(
  [...STATIC_NATIVE_FIGURES].filter(([, count]) => count > 0).map(([route]) => route),
);
const STATIC_SECONDARY_DISCLOSURES = new Map([
  ["/", 0],
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
const STATIC_SQL_DISCLOSURES = new Map(ROUTES.map((route) => [route, route === "/explore/" ? 1 : 0]));
const EXPECTED_NATIVE_FIGURES = 14;
// 2026-08-17: 9 -> 7. /methods/ lost two <details> — the transcribed K-S table
// and the published-vs-reproduced comparison table.
const EXPECTED_SECONDARY_DISCLOSURES = 7;
const EXPECTED_SQL_DISCLOSURES = 1;
const STATIC_TABLE_WRAPS = new Map([
  ["/", 0],
  ["/trend/", 0],
  ["/stations/", 0],
  ["/space/", 2],
  ["/sources/", 0],
  ["/detection/", 4],
  ["/forecast/", 2],
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
 * These numbers are re-measured when content legitimately changes, never
 * loosened to make a red run green. A wrapper vanishing for any other reason
 * still fails here, which is the whole point of pinning them.
 */
const EXPECTED_TABLE_WRAPS = 78;
const EXPECTED_TABLE_SCROLLERS = 18;

/** APCA Lc 60 is the floor below which text stops carrying meaning reliably. */
const MIN_LC = 60;
/**
 * Two floors, because the project's rule is about a RELATIONSHIP, not a size.
 *
 * PRODUCT.md's principle is 「一張圖的註記不該是整份文件裡最小的字」 — a chart's
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
 * PRODUCT.md said the smallest type was 18.7px at 375. It is not, and this is
 * the check that would have said so — 0.84rem against a 20.725px root is 17.41,
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
    if (state.primary.top >= viewport.height || state.primary.bottom <= 0) {
      problems.push("chapter primary evidence is outside the initial viewport");
    }
  }

  if (typeof state?.chartRoute !== "boolean") {
    problems.push("chapter chart-route state is missing");
  } else if (state.chartRoute) {
    if (geometryPart("primary plot", state.primaryPlot)) {
      if (!Number.isFinite(state.primaryPlot.dataAreaVisible) || state.primaryPlot.dataAreaVisible < 0) {
        problems.push("chapter primary plot has invalid visible-data geometry");
      } else if (viewport.width === 1280 && viewport.height === 720) {
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

function trendReadingMapProblems(state) {
  const problems = [];
  if (![375, 768, 1024, 1440].includes(state?.viewportWidth)) {
    return ["trend reading map viewport is not one of the reviewed widths"];
  }
  const map = state?.map;
  const thesis = state?.thesis;
  if (
    !map?.visible || map.width <= 0 || map.height <= 0 ||
    !Number.isFinite(map.left) || !Number.isFinite(map.right)
  ) {
    problems.push("trend reading map is not visible");
  } else if (map.clippedByAncestor) {
    problems.push("trend reading map is clipped by an ancestor");
  }
  if (!thesis?.visible || thesis.width <= 0 || thesis.height <= 0) {
    problems.push("trend chapter thesis is not visible");
  } else if (state.viewportWidth < 1024 && map?.top < thesis.bottom - 1) {
    problems.push("trend reading map no longer follows the thesis on narrow screens");
  } else if (
    state.viewportWidth >= 1024 &&
    (map?.left < thesis.right - 1 || map?.top >= thesis.bottom)
  ) {
    problems.push("trend chapter desktop opening is not a two-column composition");
  }
  if (state?.links?.length !== 3) problems.push("trend reading map link inventory changed");
  if (state?.targets?.length !== 3) problems.push("trend reading map target inventory changed");
  for (const [index, link] of (state?.links ?? []).entries()) {
    if (!link.visible || link.width < 44 || link.height < 44 || link.clippedByAncestor) {
      problems.push(`trend reading map link ${index + 1} target is too small`);
    }
  }
  for (const [index, target] of (state?.targets ?? []).entries()) {
    if (!target.visible || target.width <= 0 || target.height <= 0) {
      problems.push(`trend reading map target ${index + 1} is not visible`);
    } else if (target.clippedByAncestor) {
      problems.push(`trend reading map target ${index + 1} is clipped by an ancestor`);
    }
    if (state?.anchorsMeasured) {
      if (!Number.isFinite(target.afterJumpTop)) {
        problems.push(`trend reading map target ${index + 1} landing geometry is invalid`);
      } else if (target.afterJumpTop < state.stickyBottom - 1) {
        problems.push(`trend reading map target ${index + 1} is obscured after jump`);
      }
    }
  }
  if (!state?.sourceOrdered) problems.push("trend reading map source order changed");
  if (!state?.primary?.visible || state.primary.width <= 0 || state.primary.height <= 0) {
    problems.push("trend primary evidence is not visible");
  } else if (state.primary.clippedByAncestor) {
    problems.push("trend primary evidence is clipped by an ancestor");
  }
  if (
    state?.viewportWidth === 375 &&
    state?.targets?.[0]?.top > state.viewportHeight + 1
  ) {
    problems.push("trend primary evidence question leaves the first phone viewport");
  }
  if (state?.horizontalOverflow > 1) {
    problems.push("trend reading map causes horizontal overflow");
  }
  return problems;
}

function trendPrintProblems(state) {
  const problems = [];
  if (!state?.thesisVisible) problems.push("trend print thesis is not visible");
  if (!state?.mapVisible) problems.push("trend print reading map is not visible");
  if (!state?.primaryVisible) problems.push("trend print primary evidence is not visible");
  if (!state?.sourceOrdered) problems.push("trend print source order changed");
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
    for (const key of ["n_credible", "n_expected_by_chance"]) {
      if (!has(key) || typeof row[key] !== "number" || !Number.isFinite(row[key])) {
        throw new Error(`detection-limit event ${index + 1} ${key} is not a finite number`);
      }
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
  }

  const landmarkLabels = {
    title: "title",
    key: "reading key",
    primaryPlot: "primary plot",
    caption: "caption",
    comparison: "comparison",
    boundary: "boundary",
    laterEvidence: "later evidence",
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
      !Number.isInteger(landmark.sourceIndex)
    ) {
      problems.push(`${scope}${label} landmark geometry is invalid`);
    }
    if (landmark.cssOrder !== 0) problems.push(`${scope}${label} uses CSS order`);
  }

  const landmarks = state?.landmarks ?? {};
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
    landmarks.boundary && landmarks.laterEvidence &&
    !(
      landmarks.boundary.sourceIndex < landmarks.laterEvidence.sourceIndex &&
      landmarks.boundary.top < landmarks.laterEvidence.top
    )
  ) {
    problems.push(`${scope}boundary no longer precedes later evidence`);
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
      !Number.isInteger(step.sourceIndex)
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
        JSON.stringify(["accessibleText", "event", "expected", "observed", "visibleText"])
      ) {
        problems.push(`${scope}event row ${index + 1} keys changed`);
      }
      if (typeof row.observed !== "number" || !Number.isFinite(row.observed)) {
        problems.push(`${scope}event row ${index + 1} observed value is not a finite number`);
      }
      if (typeof row.expected !== "number" || !Number.isFinite(row.expected)) {
        problems.push(`${scope}event row ${index + 1} expected value is not a finite number`);
      }
      if (row.event !== expected.event) {
        problems.push(`${scope}event row ${index + 1} identity changed`);
      }
      if (row.observed !== expected.observed) {
        problems.push(`${scope}event row ${index + 1} observed value changed`);
      }
      if (row.expected !== expected.expected) {
        problems.push(`${scope}event row ${index + 1} expected value changed`);
      }
      const pairsQuantities =
        typeof row.accessibleText === "string" &&
        row.accessibleText.includes(expected.event) &&
        row.accessibleText.includes(`實際通過 ${expected.observed} 站`) &&
        row.accessibleText.includes(`純靠機率的預期為 ${expected.expected} 站`);
      if (!pairsQuantities) {
        problems.push(`${scope}event row ${index + 1} accessible text changed`);
      }
    }
  }

  const requiredBoundaryClaims = [
    "「測不到」不等於「等於零」",
    "噪音底線高於訊號",
    "這批資料與這個方法，無法分辨這種大小的效應",
    "不是「這些事件沒有影響」",
    "沒有驗證機組的逐時操作或燃料狀態",
  ];
  for (const claim of requiredBoundaryClaims) {
    if (!state?.boundaryText?.includes(claim)) {
      problems.push(`${scope}boundary is missing required claim ${JSON.stringify(claim)}`);
    }
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

const HISTORICAL_STATION_ROUTES = new Set(["/", "/space/", "/data/"]);

function historicalStationCopyProblems(route, text) {
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
        "conf/station_geo_historical.yaml",
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
  const laterEvidence = boundary?.nextElementSibling ?? null;
  return {
    mode,
    theme: document.documentElement.dataset.theme ?? "light",
    counts: {
      readingKey: keys.length,
      comparison: comparisons.length,
      boundary: boundaries.length,
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
      laterEvidence: inspect(laterEvidence),
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
    eventRows: [...(comparison?.querySelectorAll("[data-detection-event]") ?? [])]
      .map((row) => ({
        event: row.getAttribute("data-detection-event") ?? "",
        observed: parseNumberAttribute(row, "data-detection-observed"),
        expected: parseNumberAttribute(row, "data-detection-expected"),
        visibleText: compact(row.innerText),
        accessibleText: null,
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

function textZoomRouteMatrixProblems() {
  return TEXT_ZOOM_ROUTES.includes("/detection/")
    ? []
    : ["200% text-zoom route matrix does not exercise /detection/"];
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
    "PLAN.md",
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
      "PLAN.md": CJK_M7_AFFIRMATIVE_ATTRIBUTION_REJECTS,
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
      "PLAN.md": [
        {
          claim: { start: "#### M7 ", end: "**後續風險**" },
          description: "M7 observed high-value wind pattern and attribution boundary",
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
      "PLAN.md",
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
      "PLAN.md": CJK_PM_RATIO_AFFIRMATIVE_ATTRIBUTION_REJECTS,
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
      "PLAN.md": [
        {
          claim: { start: "| D2 |", end: "| D3 |" },
          description: "D2 PM ratio composition and nonunique-source boundary",
          patterns: [/PM2\.5\/PM10/u, /粒徑組成指標/u, /篩選來源假說/u, /不能唯一辨識或量化來源/u],
        },
        {
          claim: { start: "#### M7 ", end: "**後續風險**" },
          description: "M7 PM ratio cross-check and nonunique-source boundary",
          patterns: [/PM2\.5\/PM10/u, /組成觀測對照/u, /篩選來源假說/u, /不能唯一辨識或量化來源/u],
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

  const completeChapterOpening = {
    viewport: { width: 1280, height: 720 },
    smallestVisibleText: 18,
    rail: { visible: false, width: 272 },
    handle: { visible: true, height: 48 },
    primary: { visible: true, top: 250, bottom: 650 },
    primaryPlot: { visible: true, top: 300, bottom: 520, dataAreaVisible: 180 },
    chartRoute: true,
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
      thesis,
      map,
      links: Array.from({ length: 3 }, (_value, index) => ({
        visible: true, clippedByAncestor: false,
        top: map.top + index * 48,
        bottom: map.top + 48 + index * 48,
        width: 335,
        height: 48,
      })),
      targets: Array.from({ length: 3 }, (_value, index) => ({
        visible: true, clippedByAncestor: false,
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
      horizontalOverflow: 0,
    };
  };
  for (const viewportWidth of [375, 768, 1024, 1440]) {
    const state = completeTrendReadingMapFor(viewportWidth);
    if (trendReadingMapProblems(state).length) {
      throw new Error(`trend reading-map predicate rejected ${viewportWidth}px control`);
    }
  }
  const missedTrendReadingMapProblems = [];
  const expectTrendReadingMapProblem = (name, viewportWidth, mutate, expected) => {
    const state = completeTrendReadingMapFor(viewportWidth);
    mutate(state);
    const problems = trendReadingMapProblems(state);
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

  const completeTrendPrint = {
    thesisVisible: true,
    mapVisible: true,
    primaryVisible: true,
    sourceOrdered: true,
  };
  if (trendPrintProblems(completeTrendPrint).length) {
    throw new Error("trend print predicate rejected its complete control");
  }
  const missedTrendPrintProblems = [];
  const expectTrendPrintProblem = (name, mutate, expected) => {
    const state = { ...completeTrendPrint };
    mutate(state);
    const problems = trendPrintProblems(state);
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
  expectTrendPrintProblem("reordered source", (state) => {
    state.sourceOrdered = false;
  }, "source order changed");
  if (missedTrendPrintProblems.length) {
    throw new Error(
      `the trend print predicate accepts ${missedTrendPrintProblems.join(", ")}`,
    );
  }
  console.log("site quality trend print contract self-test passed");

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
    ...extra,
  });
  const title = detectionPart(90, 125, 10);
  const key = detectionPart(135, 265, 20);
  const primaryPlot = detectionPart(285, 465, 30);
  const caption = detectionPart(475, 545, 40);
  const comparison = detectionPart(560, 650, 50);
  const boundary = detectionPart(665, 770, 60, { collapsed: false, tagName: "ASIDE" });
  const laterEvidence = detectionPart(790, 850, 70);
  const completeDetectionBrief = {
    mode: "normal",
    theme: "light",
    counts: { readingKey: 1, comparison: 1, boundary: 1 },
    regions: { readingKey: key, comparison, boundary },
    landmarks: { title, key, primaryPlot, caption, comparison, boundary, laterEvidence },
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
    eventRows: EXPECTED_DETECTION_EVENTS.map((event) => ({
      event: event.event,
      observed: event.observed,
      expected: event.expected,
      visibleText:
        `${event.event} 實際通過 ${event.observed} 站；` +
        `純靠機率的預期為 ${event.expected} 站。`,
      accessibleText:
        `${event.event} 實際通過 ${event.observed} 站；` +
        `純靠機率的預期為 ${event.expected} 站。`,
    })),
    boundaryText:
      "「測不到」不等於「等於零」。噪音底線高於訊號。" +
      "這批資料與這個方法，無法分辨這種大小的效應——不是「這些事件沒有影響」。" +
      "本分析沒有驗證機組的逐時操作或燃料狀態。",
    pageText: "",
    viewport: { width: 375, height: 812 },
    document: { clientWidth: 375, scrollWidth: 375 },
  };
  const invalidDetectionPayloads = [
    ["boolean observed value", "n_credible is not a finite number", (payload) => {
      payload.events[0].n_credible = true;
    }],
    ["numeric-string expected value", "n_expected_by_chance is not a finite number", (payload) => {
      payload.events[0].n_expected_by_chance = "3.3";
    }],
    ["NaN observed value", "n_credible is not a finite number", (payload) => {
      payload.events[0].n_credible = Number.NaN;
    }],
    ["missing observed key", "n_credible is not a finite number", (payload) => {
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
  if (acceptedInvalidDetectionPayloads.length || misdiagnosedInvalidDetectionPayloads.length) {
    throw new Error(
      `the detection payload parser accepts ${acceptedInvalidDetectionPayloads.join(", ")}; ` +
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
      state.eventRows.push({
        event: "額外事件",
        observed: 1,
        expected: 3.3,
        visibleText: "額外事件 實際通過 1 站；純靠機率的預期為 3.3 站。",
        accessibleText: "額外事件 實際通過 1 站；純靠機率的預期為 3.3 站。",
      });
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
    ["boolean observed substitution", "event row 1 observed value is not a finite number", (state) => {
      state.eventRows[0].observed = true;
    }],
    ["numeric-string expected substitution", "event row 1 expected value is not a finite number", (state) => {
      state.eventRows[0].expected = "3.3";
    }],
    ["event row missing exact key", "event row 1 keys changed", (state) => {
      delete state.eventRows[0].expected;
    }],
    ["event row has extra key", "event row 1 keys changed", (state) => {
      state.eventRows[0].unexpected = 3.3;
    }],
    ["event row NaN substitution", "event row 1 observed value is not a finite number", (state) => {
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
    ["weakened required boundary claim", "boundary is missing required claim", (state) => {
      state.boundaryText = state.boundaryText.replace("無法分辨", "不容易分辨");
    }],
    ["approved phrase outside boundary", "boundary is missing required claim", (state) => {
      state.boundaryText = state.boundaryText.replace("沒有驗證機組的逐時操作或燃料狀態", "");
      state.pageText += "沒有驗證機組的逐時操作或燃料狀態";
    }],
    ["boundary before comparison", "boundary no longer follows comparison", (state) => {
      state.landmarks.boundary.sourceIndex = 45;
      state.landmarks.boundary.top = 540;
    }],
    ["boundary after later evidence", "boundary no longer precedes later evidence", (state) => {
      state.landmarks.boundary.sourceIndex = 75;
      state.landmarks.boundary.top = 860;
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
    throw new Error(
      `the detection limitation-brief predicate accepts ${missedDetectionMutations.join(", ")}`,
    );
  }
  console.log("site quality detection limitation brief self-test passed");

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
      "其中萬里的座標不是來自環境部現行測站清冊——該站已停用而從清冊消失，" +
        "位置改依審閱過的環境部歷史測站紀錄補上，來源與查證日期記在 " +
        "conf/station_geo_historical.yaml。本章結果是在納入之後重算的。",
    ],
    [
      "/data/",
      "官方停測公告與年度封存值不一致。自2025年5月1日起的資料形成未解的來源歧異；" +
        "本專案保留封存值，沒有判定這些值有效或無效。",
    ],
  ]);
  for (const [route, text] of historicalCopyFixtures) {
    const copyProblems = historicalStationCopyProblems(route, text);
    if (copyProblems.length) {
      throw new Error(`${route} precise historical-station copy is rejected: ${copyProblems.join("; ")}`);
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

  return {
    send,
    evaluate,
    waitForEvent,
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
    smallTargets: [], collisions: [], tableWraps: 0, tableScrollers: 0,
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
  const restartBrowser = async () => {
    debugPort += 1;
    browser = await replaceBrowser(browser, () => openBrowser(chrome, debugPort));
    resources.browser = browser;
    send = browser.send;
    evaluate = browser.evaluate;
    waitForEvent = browser.waitForEvent;
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
      "[data-detection-comparison] [data-detection-event]",
    ]);
    for (const [index, step] of state.readingSteps.entries()) {
      step.accessibleText = readingStepTexts[index] ?? null;
    }
    for (const [index, row] of state.eventRows.entries()) {
      row.accessibleText = eventRowTexts[index] ?? null;
    }
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

  const chapterOpeningSnapshot = async (chartRoute) => evaluate(`(() => {
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

  const trendReadingMapSnapshot = async ({ measureAnchors }) => {
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
        for (let ancestor = element.parentElement; ancestor; ancestor = ancestor.parentElement) {
          const style = getComputedStyle(ancestor);
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
          visible: rendered(element), top: rect.top, bottom: rect.bottom,
          left: rect.left, right: rect.right, width: rect.width, height: rect.height,
          clippedByAncestor: visibleRight - visibleLeft < rect.width - 1 ||
            visibleBottom - visibleTop < rect.height - 1,
        };
      };
      const map = document.querySelector("[data-chapter-reading-map]");
      const primary = document.querySelector("[data-primary-evidence]");
      const ids = ["evidence-1-1-title", "trend-weather-adjustment", "trend-airzones"];
      const targets = ids.map((id) => document.getElementById(id));
      const links = [...document.querySelectorAll("[data-chapter-reading-link]")];
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
        primary: inspect(primary),
        stickyBottom: document.querySelector(".handle")?.getBoundingClientRect().bottom ?? 0,
        anchorsMeasured: ${JSON.stringify(measureAnchors)},
        sourceOrdered: follows(map, primary) && follows(primary, targets[1]) &&
          follows(targets[1], targets[2]),
        horizontalOverflow: document.documentElement.scrollWidth -
          document.documentElement.clientWidth,
      };
    })()`);
    if (!measureAnchors || !state) return state;
    const afterJumpTop = await evaluate(`(async () => {
      const ids = ["evidence-1-1-title", "trend-weather-adjustment", "trend-airzones"];
      const originalY = scrollY;
      const tops = [];
      for (const id of ids) {
        const target = document.getElementById(id);
        if (!target) { tops.push(null); continue; }
        history.replaceState(null, "", "#" + id);
        target.scrollIntoView({ block: "start" });
        await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
        tops.push(target.getBoundingClientRect().top);
      }
      history.replaceState(null, "", location.pathname + location.search);
      scrollTo(0, originalY);
      await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      return tops;
    })()`);
    state.targets = state.targets.map((target, index) => ({
      ...target,
      afterJumpTop: afterJumpTop[index],
    }));
    return state;
  };

  const trendPrintSnapshot = async () => evaluate(`(() => {
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
    return {
      thesisVisible: rendered(thesis),
      mapVisible: rendered(map),
      primaryVisible: rendered(primary),
      sourceOrdered: follows(thesis, map) && follows(map, primary),
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
      const plotBoundaryViolationCount = plotBox
        ? plotAnnotationBoxes.filter((box) =>
            box.left < plotBox.left - 1 || box.right > plotBox.right + 1 ||
            box.top < plotBox.top - 1 || box.bottom > plotBox.bottom + 1,
          ).length
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
          chart: rect(chart), plotArea: rect(plotArea), transitionNote: rect(transitionNote),
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
        const expectedKey = index === 1 || copies.chartContentWidth <= 888 || enlargedTextNeedsKey;
        if (
          copies.transitionPlotVisible !== (expectedKey ? 0 : 1) ||
          copies.transitionKeyVisible !== (expectedKey ? 1 : 0)
        ) {
          problems.push(
            "/trend/ @" + width + " " + theme + ": Figure 1." + (index + 1) +
              " transition legend is in the wrong chart region" + contentBoxes,
          );
        }
        if (
          index === 1 &&
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
    const activeSurfaceContainer = intermediateTimelineInKey
      ? snapshot?.boxes?.chart
      : snapshot?.boxes?.plotArea;
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
    if (!intermediateTimelineInKey) {
      const plot = snapshot?.boxes?.plotArea;
      const note = snapshot?.boxes?.transitionNote;
      const topInset = plot && note ? note.top - plot.top : NaN;
      const rightInset = plot && note ? plot.right - note.right : NaN;
      if (
        !Number.isFinite(topInset) ||
        !Number.isFinite(rightInset) ||
        Math.abs(topInset - plot.height * 0.08) > 1 ||
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
    zoomRoutes: 0,
  };

  const origin = `http://127.0.0.1:${PORT}`;
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
        document.querySelector("[data-theme-color]")?.getAttribute("content") === "#f4f6f4" &&
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
        detectionEstimateTable: ${DETECTION_ESTIMATE_TABLE_PROBE},
      };
    })()`);
    totals.noScriptRoutes += 1;
    if (noScript?.theme !== "light" || noScript?.hasJs) {
      failures.push(`${route}: no-JavaScript document did not retain its static light default`);
    }
    if (noScript?.visibleToggles) {
      failures.push(`${route}: theme toggle controls remain visible without JavaScript`);
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
    }
    if (route === "/sources/") {
      for (const problem of sourcesClaimBoundaryProblems(noScript?.mainText ?? "")) {
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
  if (
    totals.noScriptSqlDisclosures === 0 ||
    totals.noScriptSqlDisclosures !== EXPECTED_SQL_DISCLOSURES
  ) {
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
      const snapshot = await chapterOpeningSnapshot(chartRoute);
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
      const state = await trendReadingMapSnapshot({ measureAnchors: true });
      for (const problem of trendReadingMapProblems(state)) {
        failures.push(`/trend/ @${width}x${height} ${theme}: ${problem}`);
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
    const state = await trendReadingMapSnapshot({ measureAnchors: false });
    for (const problem of trendReadingMapProblems(state)) {
      failures.push(`/trend/ @${width}x${height} no-JavaScript light: ${problem}`);
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
    const state = await trendPrintSnapshot();
    for (const problem of trendPrintProblems(state)) {
      failures.push(`/trend/ print: ${problem}`);
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
        if (route === "/sources/") {
          const mainText = await evaluate(
            'document.querySelector("main")?.innerText.replace(/\\s+/g, " ").trim() ?? ""',
          );
          for (const problem of sourcesClaimBoundaryProblems(mainText ?? "")) {
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
        if (HISTORICAL_STATION_ROUTES.has(route)) {
          const historicalState = await evaluate(HISTORICAL_STATION_DISCLOSURE_PROBE);
          for (const problem of historicalStationCopyProblems(route, historicalState?.text ?? "")) {
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
            const label = document.querySelector(${JSON.stringify(
              route === "/stations/" ? "#station-select" : "#example-select",
            )})?.closest("label");
            const labelText = label?.querySelector(":scope > .control-label");
            const select = label?.querySelector(":scope > select");
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
          { name: "station control", selector: "#station-select", required: route === "/stations/" },
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
        if (route === "/trend/" && width === 375 && theme === "light") {
          const exported = await evaluate(`(() => {
            const root = document.querySelector("main figure:has(.plot[data-readout])");
            const button = [...(root?.querySelectorAll(".fig-tool") ?? [])]
              .find((item) => item.textContent?.trim() === "下載 PNG");
            if (!root || !button) return null;
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
    if (HISTORICAL_STATION_ROUTES.has(route)) {
      const historicalState = await evaluate(HISTORICAL_STATION_DISCLOSURE_PROBE);
      for (const problem of historicalStationCopyProblems(route, historicalState?.text ?? "")) {
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
    if (route === "/trend/") {
      const readingMapZoomSnapshot = await trendReadingMapSnapshot({ measureAnchors: false });
      for (const problem of trendReadingMapProblems(readingMapZoomSnapshot)) {
        failures.push(`${state}: ${problem}`);
      }
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
  console.log(`readouts exercised : ${totals.readouts}`);
  console.log(`table wraps       : ${totals.tableWraps} (${totals.tableScrollers} intentional scrollers)`);
  console.log(`focus checks      : ${totals.focusChecks}`);
  console.log(`no-JavaScript     : ${totals.noScriptRoutes} routes`);
  console.log(`chapter openings : ${totals.chapterOpeningChecks} route-viewports`);
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
