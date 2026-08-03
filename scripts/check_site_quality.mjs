/**
 * Re-derive the design claims PRODUCT.md makes, and fail if they stop holding.
 *
 * PRODUCT.md's accessibility section states four measured properties:
 *
 *   * every text node clears APCA Lc 60 in both themes — and records that the
 *     dark theme once had 26 nodes below it, the worst at 47.3;
 *   * no page scrolls horizontally at 375px, in either theme;
 *   * the smallest rendered type is 18.7px at 375 and 20px at 1440;
 *   * the two figure controls are at least 44px tall.
 *
 * Those numbers came from nineteen throwaway browser scripts, and `.gitignore`
 * records — in prose — that they were deleted on purpose. So the one regression
 * the section itself documents, 26 dark-mode nodes falling under Lc 60, became
 * a class of defect this repository could no longer detect. A claim with no
 * verifier is a claim that will drift, and this project's whole argument is
 * that its numbers are re-derivable.
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

import { spawn } from "node:child_process";
import { createReadStream, existsSync, statSync } from "node:fs";
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
const READOUT_ROUTES = new Set(["/trend/", "/forecast/", "/health/", "/methods/"]);
const TEXT_ZOOM_ROUTES = ["/", "/trend/", "/stations/", "/methods/", "/explore/", "/data/"];

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
const STATIC_SECONDARY_DISCLOSURES = new Map([
  ["/", 0],
  ["/trend/", 0],
  ["/stations/", 0],
  ["/space/", 0],
  ["/sources/", 0],
  ["/detection/", 2],
  ["/forecast/", 0],
  ["/health/", 0],
  ["/methods/", 6],
  ["/explore/", 0],
  ["/data/", 0],
]);
const STATIC_SQL_DISCLOSURES = new Map(ROUTES.map((route) => [route, route === "/explore/" ? 1 : 0]));
const EXPECTED_NATIVE_FIGURES = 14;
const EXPECTED_SECONDARY_DISCLOSURES = 8;
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
  ["/methods/", 6],
  ["/explore/", 0],
  ["/data/", 1],
]);
/**
 * 2026-08-03 — the normal 11-route × 3-width × 2-theme matrix measured 90
 * visible wrappers (15 route-DOM wrappers repeated six times) and 22 cases
 * where a table was genuinely wider than its local frame. These exact totals
 * make an empty probe a failure rather than a vacuous success.
 */
const EXPECTED_TABLE_WRAPS = 90;
const EXPECTED_TABLE_SCROLLERS = 22;

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
const MIN_FONT_PX = 17;
/** WCAG 2.5.5's comfortable target. The figure controls are the ones at risk. */
const MIN_TARGET_PX = 44;
// 2026-08-03 — Linux Chrome quantised a declared 44px figure control one
// layout unit below the interaction floor while Windows Chrome kept it at 44.
// Require one 1/64px layout unit of reserve so an exact CSS boundary cannot
// pass locally and fail after deployment on another rasterisation path.
const TARGET_LAYOUT_RESERVE_PX = 1 / 64;
const CSS_PX_SERIALIZATION_EPSILON = 0.0001;
const READOUT_OVERLAP_TOLERANCE_PX = 1;

const args = process.argv.slice(2);
const opt = (name, fallback) => {
  const i = args.indexOf(`--${name}`);
  return i >= 0 && args[i + 1] ? args[i + 1] : fallback;
};
const DIST = opt("dist", join(process.cwd(), "web", "dist"));
const PORT = Number(opt("port", "4399"));
const SELF_TEST = args.includes("--self-test");
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

async function lifecycleSelfTest() {
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
    !renderExpression.includes("setTimeout")
  ) {
    throw new Error("the render wait has no timer fallback for a paused frame clock");
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
  console.log("site quality homepage first-viewport self-test passed");

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
  const finish = () => {
    if (finished) return;
    finished = true;
    document.documentElement.getBoundingClientRect();
    resolve(true);
  };
  const timer = setTimeout(finish, 250);
  requestAnimationFrame(() => requestAnimationFrame(() => {
    clearTimeout(timer);
    finish();
  }));
})`;

async function settlePaint(evaluate, label = "render wait") {
  return evaluate(RENDER_SETTLED, label);
}

async function settled(evaluate, budgetMs = 8000, label = "page") {
  for (let waited = 0; waited < budgetMs; waited += 100) {
    if (await evaluate(READY, `${label} readiness`)) {
      // One more frame, so a layout invalidated by the last stylesheet has been
      // flushed before anything reads a bounding box off it.
      await settlePaint(evaluate, `${label} render wait`);
      return true;
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
    invalidTableScrollers: [], invalidTableRules: [] };

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
        const dx = Math.min(a.right, b.right) - Math.max(a.left, b.left);
        const dy = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
        // A pixel of touching is kerning and antialiasing, not a collision.
        if (dx > 1 && dy > 1) {
          out.collisions.push({
            strip: String(strip.className || "").slice(0, 20),
            a: marks[i].text.slice(0, 14),
            b: marks[j].text.slice(0, 14),
            px: +Math.min(dx, dy).toFixed(1),
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

  out.body = parseFloat(getComputedStyle(document.body).fontSize);
  out.overflow = document.documentElement.scrollWidth - document.documentElement.clientWidth;
  const rail = document.querySelector(".rail");
  const main = document.querySelector("main");
  const handle = document.querySelector(".handle");
  const handleStyle = handle ? getComputedStyle(handle) : null;
  out.railWidth = rail ? +rail.getBoundingClientRect().width.toFixed(1) : 0;
  out.mainWidth = main ? +main.getBoundingClientRect().width.toFixed(1) : 0;
  out.handleVisible = Boolean(
    handle && handleStyle?.display !== "none" && handleStyle?.visibility !== "hidden" &&
      handle.getClientRects().length,
  );
  if (out.smallestFont === Infinity) out.smallestFont = 0;
  return out;
})()`;

// ── run ──────────────────────────────────────────────────────────────────────

async function main() {
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
      const legend = document.querySelector("[data-homepage-map-legend]");
      const scaleBar = legend?.querySelector(".scale-bar") ?? null;
      const scaleTicks = legend?.querySelector(".scale-ticks") ?? null;
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
        legend: inspect(legend),
        scaleBar: inspect(scaleBar, legend, false, "inline"),
        scaleTicks: inspect(scaleTicks, legend, false, "inline"),
        scaleSegments: inspectAll("[data-homepage-map-legend] .scale-bar > span", scaleBar),
        tickMarks: inspectAnchoredAll(
          "[data-homepage-map-legend] .scale-ticks > span",
          scaleTicks,
          "inline",
        ).filter((tick) => tick.visible),
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
    return firstViewportProblems({ ...geometry, requireVerticalViewport });
  };

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
        !structure?.toolsOutsideMap || !structure?.enhancedOrder
      )
    ) {
      problems.push("homepage enhanced figure tools precede the intended post-map content");
    }
    return problems;
  };

  const failures = [];
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
    zoomRoutes: 0,
  };

  const origin = `http://127.0.0.1:${PORT}`;
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
      const introPart = (selector) => {
        const element = intro?.querySelector(selector) ?? null;
        return Boolean(element && visible(element) && element.textContent.trim());
      };
      return {
        theme: document.documentElement.dataset.theme ?? null,
        hasJs: document.documentElement.classList.contains("has-js"),
        visibleToggles: [...document.querySelectorAll("[data-theme-toggle]")].filter(visible).length,
        startLinks: [...document.querySelectorAll("nav.start-here a")].filter(visible).length,
        chapterLinks: [...document.querySelectorAll("ol.toc a")].filter(visible).length,
        intro: {
          container: Boolean(intro && visible(intro)),
          heading: introPart("h1"),
          question: introPart(".chapter-question"),
          finding: introPart(".chapter-finding"),
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
    } else {
      const incompleteIntro = ["container", "heading", "question", "finding"].filter(
        (part) => !noScript?.intro?.[part],
      );
      if (incompleteIntro.length) {
        failures.push(
          `${route}: no-JavaScript chapter intro has unreadable parts ` +
            `(${incompleteIntro.join(", ") || "unknown"})`,
        );
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
        if (route === "/" && (width === 768 || width === 1440)) {
          for (const problem of await homepageFirstViewport()) {
            failures.push(`${route} @${width}x${height} ${theme}: ${problem}`);
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
            const charts = plots.map((plot) => ({
              lines: [...plot.querySelectorAll(".plot-line")].map((line) => ({
                weight: parseFloat(getComputedStyle(line).strokeWidth),
                dash: getComputedStyle(line).strokeDasharray,
              })),
              payloadHasEmphasis: (() => {
                try {
                  const raw = plot.querySelector(".plot-readout-data")?.textContent ?? "";
                  return JSON.parse(raw).series.some((series) => "emphasis" in series);
                } catch { return true; }
              })(),
            }));
            return charts;
          })()`);
          const twoLineCharts = trendMarks?.slice(0, 2) ?? [];
          if (
            twoLineCharts.length !== 2 ||
            twoLineCharts.some(
              (chart) =>
                chart.lines.length !== 2 ||
                Math.abs(chart.lines[0].weight - 2.5) > 0.01 ||
                Math.abs(chart.lines[1].weight - 1.75) > 0.01 ||
                chart.lines[1].dash === "none" ||
                chart.payloadHasEmphasis,
            )
          ) {
            failures.push("/trend/ @375 light: primary/comparison line emphasis is not visual-only");
          }
          const zones = trendMarks?.[2];
          if (
            !zones ||
            zones.lines.length !== 8 ||
            zones.lines.some((line) => Math.abs(line.weight - zones.lines[0].weight) > 0.01) ||
            zones.payloadHasEmphasis
          ) {
            failures.push("/trend/ @375 light: eight-zone lines do not retain a uniform weight");
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
        if (width === 1440) {
          if (r.railWidth > 272) {
            failures.push(`${route} @${width} ${theme}: rail width exceeds 272px`);
          }
          if (r.mainWidth < 720) {
            failures.push(`${route} @${width} ${theme}: main content is narrower than 720px`);
          }
          if (r.handleVisible) {
            failures.push(`${route} @${width} ${theme}: handle remains visible on desktop`);
          }
        }
        if (width === 375 && !r.handleVisible) {
          failures.push(`${route} @${width} ${theme}: handle is hidden on mobile`);
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
              `${JSON.stringify(bad.b)} overlap by ${bad.px}px in .${bad.strip}`,
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

  for (const [width, height] of [
    [390, 844],
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

  await send("Emulation.setEmulatedMedia", { media: "", features: [] });
  await evaluate('localStorage.setItem("twair-theme", "light")');
  console.log("site-quality stage: 200% text zoom");
  const checkTextZoom = async (route, width, height, suffix = "") => {
    const state = `${route} @${width}x${height} 200% text${suffix}`;
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
            const map = document.querySelector("[data-homepage-map]");
            const frame = document.querySelector("[data-homepage-map-frame]");
            const labels = [...document.querySelectorAll(".county-label")];
            const visibleLabels = labels.filter(visible);
            const labelFont = labels.length ? parseFloat(getComputedStyle(labels[0]).fontSize) : 0;
            const safeLabelWidth = 260 * (labelFont / 18);
            const boundaryProbe = frame?.cloneNode(true) ?? null;
            let labelsHiddenAtSafeBoundary = false;
            let labelsVisibleAboveSafeBoundary = false;
            if (boundaryProbe && safeLabelWidth > 0) {
              boundaryProbe.style.position = "fixed";
              boundaryProbe.style.left = "-10000px";
              boundaryProbe.style.top = "0";
              boundaryProbe.style.width = (safeLabelWidth - 1) + "px";
              document.body.append(boundaryProbe);
              const probeLabels = [...boundaryProbe.querySelectorAll(".county-label")];
              labelsHiddenAtSafeBoundary = Boolean(
                probeLabels.length &&
                probeLabels.every((label) => getComputedStyle(label).display === "none")
              );
              boundaryProbe.style.width = (safeLabelWidth * 1.2) + "px";
              labelsVisibleAboveSafeBoundary = probeLabels.some(
                (label) => getComputedStyle(label).display !== "none"
              );
              boundaryProbe.remove();
            }
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
              mapWidth: map?.getBoundingClientRect().width ?? 0,
              labelFont,
              visibleLabels: visibleLabels.length,
              tickOverlaps,
              labelsHiddenAtSafeBoundary,
              labelsVisibleAboveSafeBoundary,
            };
          })() : null,
        };
    })()`);
    totals.zoomRoutes += 1;
    if (zoomed?.overflow > 0) {
      failures.push(`${state}: document scrolls sideways by ${zoomed.overflow}px`);
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
      const safeLabelWidth = 260 * ((zoomed?.homepage?.labelFont ?? 0) / 18);
      if (
        zoomed?.homepage?.visibleLabels > 0 &&
        zoomed?.homepage?.mapWidth + 1 < safeLabelWidth
      ) {
        failures.push(
          `${state}: county labels remain visible below their effective safe map width`,
        );
      }
      if (zoomed?.homepage?.tickOverlaps > 0) {
        failures.push(
          `${state}: ${zoomed.homepage.tickOverlaps} adjacent legend tick pairs overlap`,
        );
      }
      if (!zoomed?.homepage?.labelsHiddenAtSafeBoundary) {
        failures.push(`${state}: county labels remain visible at their measured safe boundary`);
      }
      if (!zoomed?.homepage?.labelsVisibleAboveSafeBoundary) {
        failures.push(`${state}: county labels remain hidden well above their measured safe boundary`);
      }
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
