/**
 * The drawn strips of the seven method plates (`ConceptFlow.astro`).
 *
 * Seven concept diagrams used to be one template — four cards, four discs,
 * three arrows — and the owner asked whether the site's diagrams could differ
 * by what each chapter actually argues. What differs is not the reading order
 * (an ordered list, every time) but the geometry: on /trend/ each correction
 * narrows a bracket; on /space/ one residual row becomes a cloud, a cut and a
 * fan of standard errors; on /sources/ the evidence climbs and stops; on
 * /detection/ one procedure runs in two lanes and meets one band; on /health/
 * one concentration axis gains a band, then a cut; on /explore/ two wires
 * cross an edge and nothing comes back; on /data/ one month is drawn at three
 * densities. This module draws exactly that, one strip per step.
 *
 * Rules every strip obeys. The first is pinned by `check_site_quality.mjs`;
 * the rest are why the plates survive print, forced colours and the PNG
 * export:
 *
 * - **No `<text>` inside the SVG.** Every word is an HTML span placed beside
 *   the drawing, because the glossary, CJK-spacing and no-JavaScript gates
 *   read the page's HTML and treat `<svg>` as opaque. A word inside a strip
 *   would be a word no gate can see and no reader can select.
 * - **No digits, and no proportion the payload does not hold.** The single
 *   measured quantity drawn anywhere is /space/'s `se_inflation`, read by
 *   `cov_type` and scaled to its own maximum.
 * - **One coordinate system: 100 × 40 units stretched to the strip's box**
 *   (`preserveAspectRatio="none"`). A label's `x`/`y` are those same units, so
 *   a word placed beside a mark stays beside it at every width —
 *   `docs/working-rules.md` records what a percentage position over a pixel
 *   mark costs. The price is anisotropy, so every shape here is a line, a
 *   rectangle or a round-capped dot: the stretch maps straight lines to
 *   straight lines, and `vector-effect: non-scaling-stroke` keeps every stroke
 *   and every dot the same weight and size whatever the card's width. Angles
 *   are the one thing that changes, so chevrons are drawn wide (4 units
 *   across, 2.2 down) and stay legible from 53° at the tablet boundary to 31°
 *   at 1440.
 * - **Solid means held or measured; dashed means unheld, unreached or not
 *   estimated.** Dashes are explicit segments rather than `stroke-dasharray`,
 *   which the stretch would give each card a different rhythm of.
 * - **A fill only ever duplicates a stroke**, so a print that lightens fills
 *   and a forced-colours mode that flattens them both keep the meaning.
 * - **A line that leaves card k at height y re-enters card k+1 at the same
 *   y.** The eye carries it across the gutter and no DOM element spans the
 *   row, which is what keeps every card in one grid row band. `<g data-wide>`
 *   and `<g data-narrow>` hold the parts that differ when the plate is one
 *   column and there is no neighbour to carry a line into.
 *
 * Colour classes name roles rather than hues: `c-cool` is a structure the
 * analyst imposes, `c-warm` what a procedure produces, `c-neutral` the object
 * as it enters or a reference, `c-hair` structure that is not the role's,
 * `c-limit` a limit (always dashed). Without a class a shape takes the step's
 * own mark colour.
 */

export interface StripLabel {
  text: string;
  /** 0–100, the strip's horizontal unit. */
  x: number;
  /** 0–40, the strip's vertical unit. */
  y: number;
  /** Which edge of the label `x` names. Default `start`. */
  anchor?: "start" | "middle" | "end";
  /** Which edge of the label `y` names. Default `top`. */
  vertical?: "top" | "bottom";
  /** `faint` (default) is a note, `role` takes the step's tone, `text` is ink. */
  ink?: "faint" | "role" | "text";
}

export interface StepArt {
  figure: string;
  labels?: readonly StripLabel[];
}

/* Shared heights, so adjacent cards agree at their edges. */
const BRACKET_ROWS = [5, 15, 25, 36] as const;
const LADDER_PLATEAUS = [34, 24, 14, 4] as const;
const LANE_WARM = 12;
const LANE_GREY = 28;
const AXIS_X = 18;

const NS = 'vector-effect="non-scaling-stroke"';

const n = (value: number): string => String(Math.round(value * 100) / 100);

const cls = (classes: string): string => (classes ? ` class="${classes}"` : "");

function line(x1: number, y1: number, x2: number, y2: number, classes = ""): string {
  return `<line x1="${n(x1)}" y1="${n(y1)}" x2="${n(x2)}" y2="${n(y2)}"${cls(classes)} ${NS}/>`;
}

/** A dot is a zero-length round-capped stroke, so the stretch cannot flatten it. */
function dot(x: number, y: number, classes = ""): string {
  return line(x, y, x, y, `dot ${classes}`.trim());
}

function rect(x: number, y: number, w: number, h: number, classes = ""): string {
  return `<rect x="${n(x)}" y="${n(y)}" width="${n(w)}" height="${n(h)}"${cls(classes)} ${NS}/>`;
}

/** Explicit dash segments along any line, so every card keeps one rhythm. */
function dashes(
  x1: number,
  y1: number,
  x2: number,
  y2: number,
  classes = "",
  segment = 5,
  gap = 3.5,
): string {
  const length = Math.hypot(x2 - x1, y2 - y1);
  if (length === 0) return "";
  const ux = (x2 - x1) / length;
  const uy = (y2 - y1) / length;
  const parts: string[] = [];
  for (let start = 0; start < length; start += segment + gap) {
    const end = Math.min(start + segment, length);
    parts.push(line(x1 + ux * start, y1 + uy * start, x1 + ux * end, y1 + uy * end, classes));
  }
  return parts.join("");
}

function group(attribute: "data-wide" | "data-narrow", body: string): string {
  return `<g ${attribute}>${body}</g>`;
}

function svg(body: string): string {
  return (
    '<svg class="concept-figure" viewBox="0 0 100 40" preserveAspectRatio="none" ' +
    'aria-hidden="true" focusable="false">' +
    body +
    "</svg>"
  );
}

/** An open chevron pointing right, tip at (x, y). */
function chevronRight(x: number, y: number, classes = ""): string {
  return line(x - 4, y - 2.2, x, y, classes) + line(x - 4, y + 2.2, x, y, classes);
}

/** An open chevron pointing down, tip at (x, y). */
function chevronDown(x: number, y: number, classes = ""): string {
  return line(x - 4, y - 2.2, x, y, classes) + line(x + 4, y - 2.2, x, y, classes);
}

/* ────────────────────────────────────────────────────────────────────────── */

/**
 * /trend/ — four over-brackets, each opening one column later and all closing
 * at the last: the range of conclusions narrows as each correction is applied.
 * The fourth is dashed and never closes, because the chapter does not estimate
 * it. Only that one carries a word; the three solid brackets are named by the
 * step labels directly beneath them.
 */
export function trendPlate(): StepArt[] {
  const rows = [BRACKET_ROWS[0], BRACKET_ROWS[1], BRACKET_ROWS[2]];
  const open = BRACKET_ROWS[3];
  const colour = ["c-cool", "c-warm", "c-warm"];
  const tick = 6;
  const arts: StepArt[] = [];
  for (let card = 0; card < 4; card += 1) {
    let wide = "";
    let narrow = "";
    for (let b = 0; b < Math.min(card + 1, 3); b += 1) {
      const y = rows[b];
      if (b === card) wide += line(0, y, 0, y + tick, colour[b]);
      wide += line(0, y, 100, y, colour[b]);
      if (card === 3) wide += line(100, y, 100, y + tick, colour[b]);
    }
    if (card < 3) {
      const y = rows[card];
      narrow =
        line(0, y, 0, y + tick, colour[card]) +
        line(0, y, 100, y, colour[card]) +
        line(100, y, 100, y + tick, colour[card]);
    } else {
      const dashed = dashes(0, open, 92, open, "c-limit", 8, 6);
      wide += dashed;
      narrow = dashed;
    }
    arts.push({ figure: svg(group("data-wide", wide) + group("data-narrow", narrow)) });
  }
  arts[3].labels = [{ text: "本章未估計", x: 2, y: open - 1, vertical: "bottom" }];
  return arts;
}

/* ────────────────────────────────────────────────────────────────────────── */

/** One station cloud, drawn twice: ringed by distance, then cut by a boundary. */
const CLOUD: readonly (readonly [number, number])[] = [
  [8, 10], [15, 26], [22, 6], [27, 18], [33, 31], [40, 12], [46, 24],
  [52, 7], [58, 33], [64, 17], [72, 27], [79, 9], [87, 21], [94, 32],
];

export interface Whisker {
  /** The payload's `cov_type`, so a missing row is a build failure. */
  key: string;
  label: string;
  value: number;
}

/**
 * /space/ — one residual row, then one station cloud with the same six
 * comparisons drawn on it twice: once whole, once with the two pairs a
 * boundary separates removed. What a spatial control does IS that removal, so
 * it is the only thing that changes between the two cards. The last card is
 * one coefficient under four covariance assumptions, and its whisker
 * half-lengths are the only measured proportion on any of the seven plates:
 * `se_inflation` by `cov_type`, scaled to its own maximum.
 */
export function spacePlate(whiskers: readonly Whisker[]): StepArt[] {
  const xs = [12, 30, 48, 66, 84];
  const ys = [12, 27, 16, 30, 23];
  let residual = line(2, 20, 98, 20, "c-hair");
  xs.forEach((x, i) => {
    residual += line(x, 20, x, ys[i]) + dot(x, ys[i]);
  });

  // The focal station and the six it is compared with. The three that the
  // boundary in card 3 separates from it are listed apart, because the whole
  // of card 3 is those three edges going away.
  const focal = CLOUD[6];
  const kept = [CLOUD[5], CLOUD[7], CLOUD[9], CLOUD[3]];
  const severed = [CLOUD[4], CLOUD[8]];
  const cloud = CLOUD.map(([x, y]) => dot(x, y, "c-neutral")).join("");
  const star = (pairs: readonly (readonly [number, number])[]) =>
    pairs.map(([x, y]) => line(focal[0], focal[1], x, y)).join("");

  const ringed = star([...kept, ...severed]) + cloud + dot(focal[0], focal[1], "big");
  const cut =
    line(0, 38, 50, 24, "c-cool") +
    line(50, 24, 100, 2, "c-cool") +
    star(kept) +
    cloud +
    dot(focal[0], focal[1], "big");

  const maximum = Math.max(...whiskers.map((whisker) => whisker.value));
  if (!(maximum > 0)) throw new Error("spacePlate: whisker values must be positive");
  const centre = 15;
  const wx = [14, 38, 62, 86];
  let fan = "";
  const fanLabels: StripLabel[] = [];
  whiskers.forEach((whisker, index) => {
    const half = (whisker.value / maximum) * 12;
    const x = wx[index];
    fan +=
      line(x, centre - half, x, centre + half, "c-neutral") +
      line(x - 3, centre - half, x + 3, centre - half, "c-neutral") +
      line(x - 3, centre + half, x + 3, centre + half, "c-neutral") +
      dot(x, centre, "c-text");
    fanLabels.push({ text: whisker.label, x, y: 33, anchor: "middle" });
  });

  return [
    { figure: svg(residual) },
    { figure: svg(ringed) },
    { figure: svg(cut) },
    { figure: svg(fan), labels: fanLabels },
  ];
}

/* ────────────────────────────────────────────────────────────────────────── */

/**
 * /sources/ — a profile that climbs one rung per step and turns dashed where
 * this chapter's evidence stops. The last rung never closes: attribution is
 * the rung the figure cannot reach.
 */
export function sourcesPlate(): StepArt[] {
  const [p0, p1, p2, p3] = LADDER_PLATEAUS;
  const riser = 5;
  const cards = [
    line(0, p0, 100, p0),
    line(0, p0, riser, p0, "c-hair") + line(riser, p0, riser, p1) + line(riser, p1, 100, p1),
    line(0, p1, riser, p1, "c-hair") +
      dashes(riser, p1, riser, p2) +
      dashes(riser, p2, 100, p2),
    dashes(0, p2, riser, p2, "c-limit") +
      dashes(riser, p2, riser, p3, "c-limit") +
      dashes(riser, p3, 88, p3, "c-limit") +
      dot(92, p3, "c-limit"),
  ];
  const narrow = [
    line(0, p0, 100, p0),
    line(0, p1, 100, p1),
    dashes(0, p2, 100, p2),
    dashes(0, p3, 88, p3, "c-limit") + dot(92, p3, "c-limit"),
  ];
  return cards.map((wide, index) => ({
    figure: svg(group("data-wide", wide) + group("data-narrow", narrow[index])),
  }));
}

/* ────────────────────────────────────────────────────────────────────────── */

/**
 * /detection/ — one calendar window enters, the same procedure runs on the
 * marked window and on every unmarked one, and the two lanes meet a band. The
 * estimate's tick lands inside the band, which is the chapter's finding: the
 * method cannot tell this effect from its own noise.
 */
export function detectionPlate(): StepArt[] {
  const columns = 7;
  const rows = 5;
  const cw = 4.6;
  const ch = 3.6;
  const gap = 1;
  const ox = 30;
  const oy = 15;
  const marked = 3;
  const cellX = (column: number) => ox + column * (cw + gap);
  const cellY = (row: number) => oy + row * (ch + gap);
  let grid = "";
  for (let row = 0; row < rows; row += 1) {
    for (let column = 0; column < columns; column += 1) {
      grid += rect(cellX(column), cellY(row), cw, ch, "c-hair");
    }
  }
  const wx = cellX(marked);
  const wmid = wx + cw / 2;
  const gridRight = cellX(columns - 1) + cw;

  const window =
    grid + rect(wx - 0.9, oy - 0.9, cw + 1.8, rows * (ch + gap) - gap + 1.8, "c-cool");
  const estimate =
    grid +
    rect(wx, cellY(0), cw, ch, "fill c-warm") +
    line(wmid, cellY(0), wmid, LANE_WARM, "c-warm") +
    line(wmid, LANE_WARM, gridRight, LANE_WARM, "c-warm") +
    group("data-wide", line(gridRight, LANE_WARM, 100, LANE_WARM, "c-warm"));
  let placeboGrid = grid;
  for (let row = 1; row < rows; row += 1) {
    placeboGrid += rect(wx, cellY(row), cw, ch, "fill c-neutral");
  }
  const placebo =
    group("data-wide", line(0, LANE_WARM, 100, LANE_WARM, "c-warm")) +
    placeboGrid +
    line(wmid, cellY(rows - 1) + ch, wmid, LANE_GREY, "c-neutral") +
    line(wmid, LANE_GREY, gridRight, LANE_GREY, "c-neutral") +
    group("data-wide", line(gridRight, LANE_GREY, 100, LANE_GREY, "c-neutral"));
  const bandX = 46;
  const range =
    line(0, LANE_WARM, bandX, LANE_WARM, "c-warm") +
    line(0, LANE_GREY, bandX, LANE_GREY, "c-neutral") +
    rect(bandX, 8, 100 - bandX, 24, "fill c-neutral") +
    line(bandX + 4, LANE_WARM, bandX + 16, LANE_WARM, "c-warm heavy");

  return [
    { figure: svg(window) },
    { figure: svg(estimate) },
    { figure: svg(placebo) },
    {
      figure: svg(range),
      labels: [
        { text: "可辨識", x: 98, y: 1, anchor: "end" },
        { text: "不可辨識", x: 98, y: 21, anchor: "end" },
      ],
    },
  ];
}

/* ────────────────────────────────────────────────────────────────────────── */

/**
 * /health/ — one concentration axis, drawn at the same place on every card: a
 * reading, then the baseline the analyst chooses, then the length between
 * them, then that length alone beside an edge this project does not cross.
 */
export function healthPlate(): StepArt[] {
  const x = AXIS_X;
  const axis = line(x, 3, x, 37, "c-neutral") + line(x - 4, 37, x + 4, 37, "c-neutral");
  const readingY = 9;
  const reading = dot(x, readingY, "c-text");
  const bandTop = 27;
  const band = rect(x - 7, bandTop, 14, 6, "fill c-cool");
  const bx = x + 9;
  const bracket = (classes: string) =>
    line(bx, readingY, bx, bandTop, classes) +
    line(bx, readingY, bx + 4, readingY, classes) +
    line(bx, bandTop, bx + 4, bandTop, classes);
  return [
    { figure: svg(axis + reading), labels: [{ text: "測得濃度", x: 26, y: 5 }] },
    {
      figure: svg(axis + reading + band),
      labels: [{ text: "比較基準", x: 28, y: 24, ink: "role" }],
    },
    {
      figure: svg(axis + reading + band + bracket("c-warm")),
      labels: [{ text: "可歸因的一截", x: 34, y: 13, ink: "role" }],
    },
    {
      figure: svg(bracket("c-neutral") + dashes(78, 3, 78, 37, "c-limit")),
      labels: [{ text: "一段範圍", x: 34, y: 13 }],
    },
  ];
}

/* ────────────────────────────────────────────────────────────────────────── */

/**
 * /explore/ — two wires come in from the host, the query and its result stay
 * inside the tab, the return track is dashed and stopped, and the only thing
 * that leaves goes to the reader's own disk.
 */
export function explorePlate(): StepArt[] {
  const engineY = 12;
  const dataY = 31;
  const stack = (x: number, y: number, classes = "") =>
    line(x, y, x + 12, y, classes) +
    line(x, y + 4, x + 12, y + 4, classes) +
    line(x, y + 8, x + 12, y + 8, classes);
  const table = (x: number, y: number, w: number, h: number, classes = "") =>
    rect(x, y, w, h, classes) +
    line(x, y + h / 3, x + w, y + h / 3, classes) +
    line(x, y + (2 * h) / 3, x + w, y + (2 * h) / 3, classes) +
    line(x + w / 3, y, x + w / 3, y + h, classes) +
    line(x + (2 * w) / 3, y, x + (2 * w) / 3, y + h, classes);

  const host =
    rect(4, engineY - 4, 12, 8) +
    stack(4, dataY - 4) +
    line(16, engineY, 46, engineY) +
    line(16, dataY, 46, dataY) +
    group("data-wide", line(46, engineY, 100, engineY) + line(46, dataY, 100, dataY));

  const inside =
    line(0, engineY, 26, engineY) +
    chevronRight(26, engineY) +
    rect(28, engineY - 4, 12, 8, "c-cool") +
    line(0, dataY, 26, dataY) +
    chevronRight(26, dataY) +
    line(26, dataY, 62, dataY) +
    group("data-wide", line(62, dataY, 100, dataY));

  const queryY = 10;
  const query =
    line(0, dataY, 6, dataY) +
    line(6, dataY, 6, queryY) +
    line(6, queryY, 14, queryY) +
    rect(14, queryY - 4, 12, 8, "c-warm") +
    line(26, queryY, 32, queryY, "c-warm") +
    chevronRight(36, queryY, "c-warm") +
    table(38, 3, 24, 14, "c-warm") +
    group("data-wide", line(62, queryY, 100, queryY, "c-warm")) +
    dashes(100, 36, 24, 36, "c-limit") +
    line(24, 32, 24, 40, "c-limit");

  const out =
    line(0, 10, 4, 10) +
    table(4, 3, 24, 14, "c-neutral") +
    line(28, 10, 48, 10) +
    line(48, 10, 48, 28) +
    chevronDown(48, 32);

  return [
    {
      figure: svg(host),
      labels: [
        { text: "引擎", x: 18, y: 1 },
        { text: "資料檔", x: 18, y: 20 },
      ],
    },
    {
      figure: svg(inside),
      labels: [
        { text: "下載一次", x: 42, y: 1, ink: "role" },
        { text: "依範圍", x: 42, y: 20, ink: "role" },
      ],
    },
    { figure: svg(query), labels: [{ text: "回傳：無", x: 28, y: 24 }] },
    { figure: svg(out), labels: [{ text: "你的電腦", x: 6, y: 24 }] },
  ];
}

/* ────────────────────────────────────────────────────────────────────────── */

/**
 * /data/ — one month at three resolutions, drawn in each layer's own head
 * row: hours as a hatch of dashed hairlines nobody publishes, days as a run
 * of cells with one withheld for coverage, the month as a single cell. None
 * of the three claims a count.
 */
export function dataPlate(): StepArt[] {
  let hours = "";
  for (let x = 0.5; x < 100; x += 1) {
    hours += line(x, 6, x, 17, "c-limit hair") + line(x, 23, x, 34, "c-limit hair");
  }
  let days = "";
  const cells = 30;
  const pitch = 100 / cells;
  const withheld = 19;
  for (let index = 0; index < cells; index += 1) {
    days += rect(
      index * pitch + 0.35,
      6,
      pitch - 0.7,
      28,
      index === withheld ? "c-warm" : "fill c-warm",
    );
  }
  return [
    { figure: svg(hours) },
    { figure: svg(days) },
    { figure: svg(rect(0.35, 6, 99.3, 28, "fill c-cool")) },
  ];
}
