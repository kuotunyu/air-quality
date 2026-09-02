/**
 * The drawn strips of the seven method plates (`ConceptFlow.astro`).
 *
 * Seven concept diagrams used to be one template — four cards, four discs,
 * three arrows — and the owner asked whether the site's diagrams could differ
 * by what each chapter actually argues. What differs is not the reading order
 * (an ordered list, every time) but the geometry: on /trend/ each correction
 * narrows a bracket; on /space/ one residual row becomes a cloud, a cut and a
 * fan of standard errors; on /sources/ one CBPF grid holds, marks, doubts and
 * empties; on /detection/ one procedure runs in two lanes and meets one band;
 * on /health/ one concentration axis gains four baselines, then four lengths;
 * on /explore/ two wires cross an edge and nothing comes back; on /data/ one
 * month is drawn at three densities. This module draws exactly that, one
 * strip per step.
 *
 * Rules every strip obeys. The first is pinned by `check_site_quality.mjs`;
 * the rest are why the plates survive print, forced colours and the PNG
 * export:
 *
 * - **No `<text>` inside the SVG.** Every word is an HTML span placed beside
 *   the drawing, because the glossary, CJK-spacing and no-JavaScript gates
 *   read the page's HTML and treat `<svg>` as opaque. A word inside a strip
 *   would be a word no gate can see and no reader can select.
 * - **No digits, and no proportion the payload does not hold.** What is drawn
 *   from a payload is drawn from that payload alone, each scaled to itself:
 *   /space/'s `se_inflation` by `cov_type`; /sources/' held cells and peak
 *   cell of the opening station; /detection/'s placebo band and effect tick
 *   of the first event; /health/'s counterfactual concentrations and their
 *   attributable fractions. A digit still never enters a strip; a size or a
 *   name may appear in a label, which is HTML.
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
 * /trend/ — four brackets nested inside every card, each inset one step from
 * the one before, so the narrowing is a width the reader sees within a card.
 * Card k holds layers 0–k: the earlier ones as hairlines, its own in the
 * step's tone. The fourth is dashed and never closes, because the chapter
 * does not estimate it; only that one carries a word.
 *
 * 2026-09-02 — until this evening each bracket ran across the row, opening
 * one card later than the last and closing on the fourth. Three 48px gutters
 * cut a 1.5px line into four pieces, every piece the width of its card, and
 * the owner asked what the lines were. The inset is a constant step, the same
 * convention the row-spanning version had, so no width here is a proportion.
 */
export function trendPlate(): StepArt[] {
  const colour = ["c-cool", "c-warm", "c-warm"];
  const inset = 6;
  const tick = 6;
  const arts: StepArt[] = [];
  for (let card = 0; card < 4; card += 1) {
    let body = "";
    for (let layer = 0; layer <= Math.min(card, 2); layer += 1) {
      const y = BRACKET_ROWS[layer];
      const x0 = inset * layer;
      const x1 = 100 - inset * layer;
      const classes = layer === card ? colour[layer] : "hair c-hair";
      body +=
        line(x0, y, x0, y + tick, classes) +
        line(x0, y, x1, y, classes) +
        line(x1, y, x1, y + tick, classes);
    }
    if (card === 3) {
      const y = BRACKET_ROWS[3];
      body += dashes(inset * 3, y, 100 - inset * 3, y, "c-limit", 8, 6);
    }
    arts.push({ figure: svg(body) });
  }
  arts[3].labels = [
    { text: "本章未估計", x: inset * 3 + 1, y: BRACKET_ROWS[3] - 1, vertical: "bottom" },
  ];
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
 * boundary separates drawn dashed. What a spatial control does IS that
 * removal, so it is the only thing that changes between the two cards. The
 * last card is one coefficient under four covariance assumptions, and its
 * whisker half-lengths are measured: `se_inflation` by `cov_type`, scaled to
 * its own maximum.
 *
 * 2026-09-02 — the severed pairs used to be left out of card 3, and a reader
 * who did not hold card 2 in mind saw nothing removed. Dashed is the plates'
 * word for unheld, so the removal is now drawn.
 */
export function spacePlate(whiskers: readonly Whisker[]): StepArt[] {
  const xs = [12, 30, 48, 66, 84];
  const ys = [12, 27, 16, 30, 23];
  let residual = line(2, 20, 98, 20, "c-hair");
  xs.forEach((x, i) => {
    residual += line(x, 20, x, ys[i]) + dot(x, ys[i]);
  });

  // The focal station and the six it is compared with. The two that the
  // boundary in card 3 separates from it are listed apart, because the whole
  // of card 3 is those two edges going dashed.
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
    severed.map(([x, y]) => dashes(focal[0], focal[1], x, y, "c-limit", 2, 1.4)).join("") +
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
 * /sources/ — the CBPF grid itself, twelve wind sectors by six speed bins,
 * on every card at a different degree of hold: the cells with enough hours
 * to carry a probability (the rest are suppressed, `null` in the payload),
 * then the one cell where high values are most frequent, then that cell as
 * a hypothesis, dashed, then the frame alone, dashed, because attribution
 * is what this grid cannot reach. Which cells hold and where the peak sits
 * are the opening station's own fields; the probabilities are 圖 4.1's to
 * draw, so no cell here is shaded by one.
 *
 * 2026-09-02 — replaced a four-rung ladder that climbed one plateau per
 * step and went dashed where the evidence stopped. It carried the shape of
 * the argument and nothing of its object: no reader could tell from it
 * what a CBPF cell was, which is the chapter's method.
 */
export interface CbpfStation {
  probability: readonly (readonly (number | null)[])[];
  peak_sector: number | null;
  peak_speed: string | null;
}

export function sourcesPlate(
  station: CbpfStation,
  sectors: readonly number[],
  speedBins: readonly string[],
): StepArt[] {
  const columns = sectors.length;
  const rows = speedBins.length;
  if (
    station.probability.length !== columns ||
    station.probability.some((row) => row.length !== rows)
  ) {
    throw new Error("sourcesPlate: probability grid does not match sectors × speed bins");
  }
  /* The top twelve units are for words — the axis names on card 1, the stop
     on card 3 — so the grid starts below them and no label sits on a cell. */
  const ox = 10;
  const oy = 12;
  const gap = 0.9;
  const cw = (86 - (columns - 1) * gap) / columns;
  const ch = (24 - (rows - 1) * gap) / rows;
  const cellX = (column: number) => ox + column * (cw + gap);
  /* Slow wind at the bottom, so speed reads upward like an axis. */
  const cellY = (row: number) => oy + (rows - 1 - row) * (ch + gap);
  const peakColumn = station.peak_sector == null ? -1 : sectors.indexOf(station.peak_sector);
  const peakRow = station.peak_speed == null ? -1 : speedBins.indexOf(station.peak_speed);
  const hasPeak = peakColumn >= 0 && peakRow >= 0;

  const grid = (held: string, suppressed: string): string => {
    let body = "";
    for (let column = 0; column < columns; column += 1) {
      for (let row = 0; row < rows; row += 1) {
        const classes = station.probability[column][row] == null ? suppressed : held;
        body += rect(cellX(column), cellY(row), cw, ch, classes);
      }
    }
    return body;
  };
  const peakCell = (classes: string): string =>
    hasPeak ? rect(cellX(peakColumn), cellY(peakRow), cw, ch, classes) : "";
  const peakDashed = (): string => {
    if (!hasPeak) return "";
    const x = cellX(peakColumn);
    const y = cellY(peakRow);
    return (
      dashes(x, y, x + cw, y, "c-warm", 1.4, 0.9) +
      dashes(x + cw, y, x + cw, y + ch, "c-warm", 1.4, 0.9) +
      dashes(x + cw, y + ch, x, y + ch, "c-warm", 1.4, 0.9) +
      dashes(x, y + ch, x, y, "c-warm", 1.4, 0.9)
    );
  };
  const frameDashed = (): string => {
    const x0 = ox - gap;
    const y0 = oy - gap;
    const x1 = cellX(columns - 1) + cw + gap;
    const y1 = cellY(0) + ch + gap;
    return (
      dashes(x0, y0, x1, y0, "c-limit") +
      dashes(x1, y0, x1, y1, "c-limit") +
      dashes(x1, y1, x0, y1, "c-limit") +
      dashes(x0, y1, x0, y0, "c-limit")
    );
  };

  return [
    {
      figure: svg(grid("fill c-neutral", "hair c-hair")),
      labels: [
        { text: "風速", x: 0, y: 0 },
        { text: "風向", x: 100, y: 0, anchor: "end" },
      ],
    },
    { figure: svg(grid("fill c-neutral", "hair c-hair") + peakCell("fill c-cool heavy")) },
    { figure: svg(grid("hair c-hair", "hair c-hair") + peakDashed()) },
    { figure: svg(frameDashed()) },
  ];
}

/* ────────────────────────────────────────────────────────────────────────── */

/**
 * /detection/ — one calendar window enters, the same procedure runs on the
 * marked window and on every unmarked one, and the two lanes meet a band.
 * The band is the placebo mean ± 2 sd — the method's own threshold — and the
 * estimate's tick sits at its median effect, both from the event passed in,
 * so where the tick lands is read rather than drawn. For the first event it
 * lands inside, which is the chapter's finding: the method cannot tell this
 * effect from its own noise.
 *
 * 2026-09-02 — until now the band and the tick were drawn to land inside.
 */
export interface DetectionEvent {
  event: string;
  median_effect: number;
  median_placebo_mean: number;
  median_placebo_sd: number;
}

export function detectionPlate(event: DetectionEvent): StepArt[] {
  if (!(event.median_placebo_sd > 0)) {
    throw new Error("detectionPlate: the placebo sd must be positive");
  }
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
  const bandTop = 8;
  const bandBottom = 32;
  const low = event.median_placebo_mean - 2 * event.median_placebo_sd;
  const high = event.median_placebo_mean + 2 * event.median_placebo_sd;
  const yOf = (value: number): number =>
    Math.min(36, Math.max(4, bandBottom - ((value - low) / (high - low)) * (bandBottom - bandTop)));
  const effectY = yOf(event.median_effect);
  const meanY = yOf(event.median_placebo_mean);
  const range =
    line(0, LANE_WARM, bandX - 10, LANE_WARM, "c-warm") +
    line(bandX - 10, LANE_WARM, bandX, effectY, "c-warm") +
    line(0, LANE_GREY, bandX - 10, LANE_GREY, "c-neutral") +
    line(bandX - 10, LANE_GREY, bandX, meanY, "c-neutral") +
    rect(bandX, bandTop, 100 - bandX, bandBottom - bandTop, "fill c-neutral") +
    line(bandX + 4, effectY, bandX + 16, effectY, "c-warm heavy");

  return [
    { figure: svg(window) },
    { figure: svg(estimate) },
    { figure: svg(placebo) },
    {
      figure: svg(range),
      labels: [
        { text: "可辨識", x: 98, y: 1, anchor: "end" },
        { text: "不可辨識", x: 98, y: 21, anchor: "end" },
        { text: event.event, x: 98, y: 40, anchor: "end", vertical: "bottom" },
      ],
    },
  ];
}

/* ────────────────────────────────────────────────────────────────────────── */

/**
 * /health/ — one concentration axis, drawn at the same place on every card:
 * the reading, then the baselines the analyst may choose, then the length
 * from the reading down to each, then the attributable fractions those
 * lengths become, beside an edge this project does not cross. The reading
 * is the panel's last-year median, the baselines the four counterfactual
 * concentrations and the fractions their last-year PAFs, all from
 * `health.json`; the axis is scaled so the reading sits where it always did.
 *
 * 2026-09-02 — until now the strip drew one baseline as a band and one
 * bracket, and carried no value. Ticks at their true heights and bars at
 * their true lengths are what 「一段範圍」 means; 圖 7.1 keeps the curves and
 * the numbers, and no baseline is named here.
 */
export function healthPlate(
  reading: number,
  baselines: readonly number[],
  fractions: readonly number[],
): StepArt[] {
  if (baselines.length < 2 || baselines.length !== fractions.length) {
    throw new Error("healthPlate: one attributable fraction per baseline, at least two");
  }
  if (!(reading > Math.max(...baselines)) || fractions.some((f) => !(f > 0))) {
    throw new Error("healthPlate: the reading must exceed every baseline; fractions positive");
  }
  const x = AXIS_X;
  const top = 9;
  const base = 37;
  const yOf = (concentration: number) => base - (concentration / reading) * (base - top);
  const axis = line(x, 3, x, base, "c-neutral") + line(x - 4, base, x + 4, base, "c-neutral");
  const readingDot = dot(x, top, "c-text");
  const ticks = baselines.map((b) => line(x - 5, yOf(b), x + 5, yOf(b), "c-cool")).join("");
  const spans = (classes: string): string =>
    baselines
      .map((b, i) => {
        const bx = x + 10 + i * 5;
        return (
          line(bx, top, bx, yOf(b), classes) + line(bx - 1.5, yOf(b), bx + 1.5, yOf(b), classes)
        );
      })
      .join("");
  const maximum = Math.max(...fractions);
  const bars = fractions
    .map((f, i) => {
      const bx = 14 + i * 12;
      const h = (f / maximum) * (base - top);
      return (
        line(bx, base, bx, base - h, "c-neutral") +
        line(bx - 3, base - h, bx + 3, base - h, "c-neutral")
      );
    })
    .join("");
  return [
    { figure: svg(axis + readingDot), labels: [{ text: "測得濃度", x: 26, y: 5 }] },
    {
      figure: svg(axis + readingDot + ticks),
      labels: [{ text: "比較基準", x: 28, y: 24, ink: "role" }],
    },
    {
      figure: svg(axis + readingDot + ticks + spans("c-warm")),
      labels: [{ text: "可歸因的一截", x: 50, y: 13, ink: "role" }],
    },
    {
      figure: svg(bars + line(6, base, 60, base, "c-hair") + dashes(90, 3, 90, 37, "c-limit")),
      labels: [{ text: "一段範圍", x: 60, y: 13 }],
    },
  ];
}

/* ────────────────────────────────────────────────────────────────────────── */

/**
 * /explore/ — two wires come in from the host, the query and its result stay
 * inside the tab, the return track is dashed and stopped, and the only thing
 * that leaves goes to the reader's own disk.
 */
export function explorePlate(engineSize: string, dataSize: string): StepArt[] {
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
        { text: `下載一次 · ${engineSize}`, x: 30, y: 1, ink: "role" },
        { text: `依範圍 · ${dataSize} 內`, x: 30, y: 20, ink: "role" },
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
