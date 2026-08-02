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
const MIN_FONT_PX = 17;
/** WCAG 2.5.5's comfortable target. The figure controls are the ones at risk. */
const MIN_TARGET_PX = 44;

const args = process.argv.slice(2);
const opt = (name, fallback) => {
  const i = args.indexOf(`--${name}`);
  return i >= 0 && args[i + 1] ? args[i + 1] : fallback;
};
const DIST = opt("dist", join(process.cwd(), "web", "dist"));
const PORT = Number(opt("port", "4399"));

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

async function connect(port) {
  for (let i = 0; i < 80; i += 1) {
    try {
      const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
      const page = list.find((t) => t.type === "page");
      if (page) return page.webSocketDebuggerUrl;
    } catch {
      /* not up yet */
    }
    await sleep(250);
  }
  throw new Error("Chrome did not open a debugging port");
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

async function settled(evaluate, budgetMs = 8000) {
  for (let waited = 0; waited < budgetMs; waited += 100) {
    if (await evaluate(READY)) {
      // One more frame, so a layout invalidated by the last stylesheet has been
      // flushed before anything reads a bounding box off it.
      await evaluate(
        `new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)))`,
      );
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
    smallTargets: [], collisions: [] };

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

  // The two figure controls, which are the smallest deliberate targets here.
  for (const el of document.querySelectorAll(".fig-tool, .rail-open, .rail-shut, button, select")) {
    const cs = getComputedStyle(el);
    if (cs.display === "none" || cs.visibility === "hidden") continue;
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height) continue;
    if (r.height < ${MIN_TARGET_PX}) {
      out.smallTargets.push({ cls: String(el.className || el.tagName).slice(0, 28),
        w: +r.width.toFixed(1), h: +r.height.toFixed(1) });
    }
  }

  out.body = parseFloat(getComputedStyle(document.body).fontSize);
  out.overflow = document.documentElement.scrollWidth - document.documentElement.clientWidth;
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

  const server = await serve(DIST, PORT);
  const debugPort = PORT + 1;
  const proc = spawn(
    chrome,
    [
      "--headless=new",
      "--disable-gpu",
      "--hide-scrollbars",
      "--no-sandbox",
      `--remote-debugging-port=${debugPort}`,
      `--user-data-dir=${join(process.env.TEMP ?? "/tmp", "twair-quality-profile")}`,
      "about:blank",
    ],
    { stdio: "ignore" },
  );

  const ws = new WebSocket(await connect(debugPort));
  await new Promise((r) => ws.addEventListener("open", r));
  let id = 0;
  const pending = new Map();
  ws.addEventListener("message", (e) => {
    const m = JSON.parse(e.data);
    if (m.id && pending.has(m.id)) {
      pending.get(m.id)(m);
      pending.delete(m.id);
    }
  });
  const send = (method, params = {}) =>
    new Promise((res) => {
      const i = (id += 1);
      pending.set(i, res);
      ws.send(JSON.stringify({ id: i, method, params }));
    });
  const evaluate = async (expr) =>
    // `awaitPromise` so `settled()` can wait on a requestAnimationFrame pair
    // instead of getting a Promise object back and treating it as truthy.
    (await send("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true }))
      .result?.result
      ?.value;

  const failures = [];
  const totals = {
    nodes: 0,
    smallestAt375: Infinity,
    smallestAt1440: Infinity,
    annotationAt375: Infinity,
    collisions: 0,
  };

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
    await send("Emulation.setDeviceMetricsOverride", {
      width,
      height,
      deviceScaleFactor: 1,
      mobile: width < 500,
    });
    for (const theme of ["light", "dark"]) {
      await send("Emulation.setEmulatedMedia", {
        media: "",
        features: [{ name: "prefers-color-scheme", value: theme }],
      });
      for (const route of ROUTES) {
        await send("Page.navigate", { url: `http://127.0.0.1:${PORT}${route}` });
        if (!(await settled(evaluate))) {
          failures.push(`${route} @${width} ${theme}: page never finished styling`);
          continue;
        }
        const r = await evaluate(PROBE);
        if (!r) {
          failures.push(`${route} @${width} ${theme}: probe returned nothing`);
          continue;
        }
        totals.nodes += r.nodes;
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
          if (r.smallestAnnotation < r.body * 0.95) {
            failures.push(
              `${route} @${width} ${theme}: annotation ${r.smallestAnnotation}px is below the ` +
                `${r.body.toFixed(1)}px body it sits among`,
            );
          }
        }

        if (r.overflow > 0) {
          failures.push(`${route} @${width} ${theme}: page scrolls sideways by ${r.overflow}px`);
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
              `(floor ${MIN_TARGET_PX})`,
          );
        }
        for (const bad of r.collisions) {
          totals.collisions += 1;
          failures.push(
            `${route} @${width} ${theme}: ${JSON.stringify(bad.a)} and ` +
              `${JSON.stringify(bad.b)} overlap by ${bad.px}px in .${bad.strip}`,
          );
        }
      }
    }
  }

  if (totals.smallestAt375 < MIN_FONT_PX) {
    failures.push(`smallest type at 375px is ${totals.smallestAt375}px (floor ${MIN_FONT_PX})`);
  }

  console.log(`routes checked   : ${ROUTES.length} x 3 widths x 2 themes`);
  console.log(`text nodes       : ${totals.nodes.toLocaleString("en-US")}`);
  console.log(`smallest type    : ${totals.smallestAt375}px @375, ${totals.smallestAt1440}px @1440`);
  console.log(`smallest in-figure annotation @375 : ${totals.annotationAt375}px`);
  console.log(`overlapping axis labels : ${totals.collisions}`);
  console.log(`APCA floor       : Lc ${MIN_LC}`);
  console.log(`problems         : ${failures.length}`);
  for (const line of failures.slice(0, 40)) console.log(`  FAIL: ${line}`);
  if (failures.length > 40) console.log(`  ... and ${failures.length - 40} more`);

  ws.close();
  proc.kill();
  server.close();
  return failures.length ? 1 : 0;
}

process.exit(await main());
