/**
 * Minimal SVG chart primitives, evaluated at build time.
 *
 * No charting library. Three reasons, in order of weight:
 *
 * 1. **Gaps.** Every series here can be null in two different ways, and the
 *    single most important rendering rule on this site is that a line breaks
 *    rather than bridging a hole. Most libraries interpolate by default, and
 *    fighting that is more work than drawing the path.
 * 2. Charts rendered in the frontmatter are in the HTML, so they appear
 *    without JavaScript, print correctly, and cost nothing on first paint.
 * 3. The whole site is four chart shapes. A dependency would be larger than
 *    the thing it replaced.
 */

export interface Scale {
  (value: number): number;
  domain: [number, number];
  range: [number, number];
}

/**
 * The shortest a plot box gets, in px.
 *
 * Must match `--plot-min` in `global.css`. It is duplicated here because
 * `spreadLabels` works in percent of the plot and a plot no longer has one
 * height — it derives one from its own width. A gap expressed as a percentage
 * of some nominal height is only safe if that height is the SHORTEST the box
 * can be: too small a gap and the labels collide, which is a hard failure,
 * where too large a gap only separates labels that were already overlapping.
 */
export const PLOT_MIN_H = 244;

/** A px gap converted to the percentage of the plot box that guarantees it. */
export function gapPct(px: number): number {
  return (px / PLOT_MIN_H) * 100;
}

export function linear(
  domain: [number, number],
  range: [number, number],
): Scale {
  const [d0, d1] = domain;
  const [r0, r1] = range;
  const span = d1 - d0 || 1;
  const scale = ((value: number) =>
    r0 + ((value - d0) / span) * (r1 - r0)) as Scale;
  scale.domain = domain;
  scale.range = range;
  return scale;
}

/** Round to a fixed number of decimals and drop trailing zeroes. */
export function n(value: number, places = 1): string {
  return Number(value.toFixed(places)).toString();
}

/**
 * The same rounding, with the zeros kept.
 *
 * `n` trims trailing zeroes, which is right in prose, right on an axis label and
 * right in an SVG path — padding a `d` attribute would only make it longer. It
 * is wrong in a column. `td.num` is right-aligned and set in tabular figures, so
 * every digit occupies the same width and the decimal point is supposed to form
 * a line; drop a zero and that row's point moves. Measured on chapter 8's
 * thirteen-row K-S column at 1440: twelve rows put the point at x = 785.9 and the
 * 「0.3」 row put it 12.5px to the right, in a column whose whole purpose is
 * comparison down the page.
 */
export function nFixed(value: number, places = 1): string {
  return value.toFixed(places);
}

/**
 * Build a path, starting a new subpath at every null.
 *
 * This is the function the whole module exists for. A null is a hole in the
 * record, and a hole must look like a hole.
 */
export function linePath(
  values: (number | null | undefined)[],
  x: (index: number) => number,
  y: (value: number) => number,
): string {
  const parts: string[] = [];
  let open = false;

  values.forEach((value, index) => {
    if (value == null || !Number.isFinite(value)) {
      open = false;
      return;
    }
    const px = n(x(index), 2);
    const py = n(y(value), 2);
    parts.push(`${open ? "L" : "M"}${px} ${py}`);
    open = true;
  });

  return parts.join(" ");
}

/**
 * The same path, drawn as a smooth curve.
 *
 * Ported from the owner's own traffic-accident project, which sets ECharts'
 * `smooth: 0.25` on every series. It is most of why those charts read as calm:
 * a polyline through fifteen or twenty-eight annual points is a sequence of
 * corners, and the eye reads every corner as an event.
 *
 * Catmull-Rom through the points, converted to cubic Bézier, at the same 0.25
 * tension. Two properties matter and both are kept: the curve passes exactly
 * through every measured point — it is a smoothing of the LINE, never of the
 * data — and a null still ends the subpath, so a gap is still a gap.
 */
export function smoothPath(
  values: (number | null | undefined)[],
  x: (index: number) => number,
  y: (value: number) => number,
  tension = 0.25,
): string {
  const runs: { x: number; y: number }[][] = [];
  let run: { x: number; y: number }[] = [];
  values.forEach((value, index) => {
    if (value == null || !Number.isFinite(value)) {
      if (run.length) runs.push(run);
      run = [];
      return;
    }
    run.push({ x: x(index), y: y(value) });
  });
  if (run.length) runs.push(run);

  return runs
    .map((points) => {
      if (points.length === 1) {
        // A lone reading is still a reading; draw it as a zero-length segment
        // so it is visible rather than silently dropped.
        const p = points[0];
        return `M${n(p.x, 2)} ${n(p.y, 2)} L${n(p.x, 2)} ${n(p.y, 2)}`;
      }
      let d = `M${n(points[0].x, 2)} ${n(points[0].y, 2)}`;
      for (let i = 0; i < points.length - 1; i += 1) {
        const p0 = points[i - 1] ?? points[i];
        const p1 = points[i];
        const p2 = points[i + 1];
        const p3 = points[i + 2] ?? p2;
        const c1x = p1.x + ((p2.x - p0.x) / 6) * tension * 4;
        const c1y = p1.y + ((p2.y - p0.y) / 6) * tension * 4;
        const c2x = p2.x - ((p3.x - p1.x) / 6) * tension * 4;
        const c2y = p2.y - ((p3.y - p1.y) / 6) * tension * 4;
        d += ` C${n(c1x, 2)} ${n(c1y, 2)}, ${n(c2x, 2)} ${n(c2y, 2)}, ${n(p2.x, 2)} ${n(p2.y, 2)}`;
      }
      return d;
    })
    .join(" ");
}

/** Points for a scatter overlay, skipping nulls. */
export function points(
  values: (number | null | undefined)[],
  x: (index: number) => number,
  y: (value: number) => number,
): { x: number; y: number; index: number; value: number }[] {
  const out: { x: number; y: number; index: number; value: number }[] = [];
  values.forEach((value, index) => {
    if (value == null || !Number.isFinite(value)) return;
    out.push({ x: x(index), y: y(value), index, value });
  });
  return out;
}

/** "Nice" tick values covering a domain, at roughly `count` intervals. */
export function ticks(min: number, max: number, count = 5): number[] {
  if (!Number.isFinite(min) || !Number.isFinite(max) || min === max)
    return [min];
  const raw = (max - min) / count;
  const magnitude = 10 ** Math.floor(Math.log10(raw));
  const normalised = raw / magnitude;
  const step =
    (normalised >= 7.5
      ? 10
      : normalised >= 3.5
        ? 5
        : normalised >= 1.5
          ? 2
          : 1) * magnitude;

  const out: number[] = [];
  for (
    let v = Math.ceil(min / step) * step;
    v <= max + step * 1e-9;
    v += step
  ) {
    out.push(Number(v.toFixed(10)));
  }
  return out;
}

/**
 * Tick years at a round interval, not at every nth index.
 *
 * Both time axes on this site sampled the index —
 * `years[round(k * (n - 1) / (m - 1))]` — which lands wherever the arithmetic
 * falls: chapter 1 printed 1998, 2001, 2005, 2008, 2012, 2015, 2018, 2022,
 * 2025, so the gaps ran 3, 4, 3, 4, 3, 3, 4, 3 and no two steps in a row were
 * the same. A linear axis whose labels are unevenly spaced tells the eye the
 * scale is uneven, which is the one thing an axis exists not to do.
 *
 * The grid is anchored to the LAST year rather than the first. On a record that
 * ends now, the latest year is the one a reader looks for, so anchoring there
 * guarantees it is labelled. The first year joins only when it is at least half
 * a step from the tick after it — otherwise it sits a few pixels from its
 * neighbour, which is the collision this is meant to remove, and the line
 * visibly starting to the left of the first tick already says the record begins
 * before it.
 */
export function yearTicks(years: number[], most = 9): number[] {
  if (years.length <= 2) return [...years];
  const first = years[0];
  const last = years[years.length - 1];
  const span = last - first;

  // The smallest round step that keeps the count inside the budget.
  const step = [1, 2, 5, 10, 20, 25, 50].find((s) => span / s + 1 <= most) ?? 100;

  const out: number[] = [];
  for (let y = last; y >= first; y -= step) out.push(y);
  out.reverse();
  if (out[0] - first >= step / 2) out.unshift(first);
  return out;
}

/**
 * Pad a domain so the extremes are not glued to the frame.
 *
 * `zero` means "this scale starts at zero", and it now means it exactly. It used
 * to pad *past* zero: with values from 5 to 52.2 the domain came out
 * [-4.18, 56.38], so the bottom rule of the plot — the darker `.plot-axis`,
 * drawn at the foot of the box — sat at −4.18 μg/m³. A concentration cannot be
 * negative, the 0 gridline floated 21px above the line that looked like the
 * axis, and the one mark a reader takes as the origin was at a value that does
 * not exist. Padding the top is a courtesy to the topmost point; padding the
 * bottom of a zero-based scale invents room below the floor.
 */
export function padded(
  values: (number | null | undefined)[],
  { zero = false, pad = 0.08 }: { zero?: boolean; pad?: number } = {},
): [number, number] {
  const finite = values.filter(
    (v): v is number => v != null && Number.isFinite(v),
  );
  if (finite.length === 0) return [0, 1];

  let min = Math.min(...finite);
  let max = Math.max(...finite);
  if (zero) min = Math.min(0, min);
  const span = max - min || Math.abs(max) || 1;
  max += span * pad;
  // A domain that already reaches below zero is a signed scale (a residual, a
  // skill score) and keeps its bottom padding; one that starts at zero does not.
  if (zero && min >= 0) return [0, max];
  min -= span * pad;
  return [min, max];
}

/**
 * The concentration ramp, as CSS custom property names.
 *
 * Breaks are the ones a reader in Taiwan already has intuitions about: the WHO
 * annual guideline, the WHO 24-hour guideline, Taiwan's annual standard, and
 * Taiwan's 24-hour standard, then two levels above that.
 */
const PM25_BREAKS = [5, 10, 15, 25, 35, 55];

/** The same breaks, for a legend that draws the scale rather than listing it. */
export const PM25_LEGEND_BREAKS = PM25_BREAKS;
const RAMP = ["--c0", "--c1", "--c2", "--c3", "--c4", "--c5", "--c6"];

/**
 * The text token that goes with a mark token.
 *
 * A series label wearing its series colour is unreadable at the ramp's light
 * end, so every ramp colour has a solved `-ink` twin (see global.css). This maps
 * one to the other by name instead of asking every series definition to carry
 * both, which is how the two would drift apart.
 *
 * Reference guides fall through to `--text-muted` on purpose: they are neutral
 * now, so their label is annotation type and has no series hue to preserve.
 */
export function markInk(colour: string): string {
  // `k` as well as `c` since the categorical set arrived: every `--kN` has a
  // solved `-ink` twin by the same rule, and without this arm a categorical
  // series label would silently fall through to `--text-muted` and lose the
  // one thing tying it to its line. `0-7` because the categorical set grew to
  // eight — `0-6` would have silently dropped `--k7`, which is exactly the kind
  // of quiet fallback this comment exists to prevent.
  const named = /^var\(--([ck][0-7])\)$/.exec(colour.trim());
  return named ? `var(--${named[1]}-ink)` : "var(--text-muted)";
}

export function concentrationColour(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "var(--line)";
  let index = 0;
  while (index < PM25_BREAKS.length && value >= PM25_BREAKS[index]) index += 1;
  return `var(${RAMP[index]})`;
}

/**
 * The TEXT colour for a concentration, from the same bins as the mark colour.
 *
 * The entry page set its highest figure in `--c5-ink` as a literal, and
 * `concentrationColour` puts that value (19.61) in `--c3` — two bins out, dE00 31,
 * printed beside the legend that adjudicates the bins and directly above its own
 * `--c3` dot on the map. Hardcoding a bin is guessing at an answer this function
 * already has, and it cannot follow the data when the data moves.
 */
export function concentrationInk(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "var(--text-muted)";
  let index = 0;
  while (index < PM25_BREAKS.length && value >= PM25_BREAKS[index]) index += 1;
  return `var(${RAMP[index]}-ink)`;
}

export const concentrationLegend = PM25_BREAKS.map((breakpoint, index) => ({
  colour: `var(${RAMP[index]})`,
  label:
    index === 0 ? `< ${breakpoint}` : `${PM25_BREAKS[index - 1]}–${breakpoint}`,
})).concat([
  {
    colour: `var(${RAMP[RAMP.length - 1]})`,
    label: `≥ ${PM25_BREAKS[PM25_BREAKS.length - 1]}`,
  },
]);

/**
 * How wide the label gutters need to be, in px.
 *
 * The gutters hold absolutely positioned spans, and an absolutely positioned
 * child contributes nothing to the size of an `auto` grid track — so the track
 * collapses to zero and the labels hang outside the figure, which is what they
 * did on the first run of this layout. The track has to be told.
 *
 * The estimate is deliberately crude: a CJK glyph is one em, most Latin is a
 * bit over half. Being 10px generous costs nothing, and being short would put a
 * label back outside the card.
 */
export function labelGutter(labels: (string | number)[], size = 24, pad = 16): number {
  const width = (label: string | number) =>
    [...String(label)].reduce(
      (sum, ch) => sum + (/[⺀-鿿＀-￯]/.test(ch) ? 1 : 0.56),
      0,
    );
  return Math.ceil(Math.max(0, ...labels.map(width)) * size + pad);
}

/**
 * Width the y-axis gutter needs for its own labels.
 *
 * `.plot-y` reads `var(--y-w, calc(3.4 * var(--chart-tick)))`, and until now
 * NOTHING in this repository ever set `--y-w` — the token appeared exactly once,
 * as that fallback. So every chart on the site reserved 3.4 tick-heights for its
 * y labels whatever they said: a fixed 71px, whether the widest label was 「50」
 * or 「100%」. Measured at 375px that left the plot area 140px inside a 244px
 * card, 57% of the figure spent on a gutter three times wider than its contents.
 *
 * Same estimator as `labelGutter`, at the tick size rather than the default, and
 * with a smaller pad: this gutter holds one right-aligned number and a gap to the
 * axis, not a legend.
 */
export function yGutter(labels: (string | number)[]): number {
  // Must track `--chart-tick` in global.css. Both are px because the plot is.
  // 21 -> 24 on 2026-08-01 with the token; left behind, every y gutter on the
  // site would have been sized for type 14% smaller than the type in it.
  return labelGutter(labels, 24, 12);
}

/**
 * Push inline series labels apart so they stay readable where lines converge.
 *
 * Needed because convergence is usually the finding. In chapter 5 R² and skill
 * end 5px apart at 48h at an 11.5px font; in chapter 6 two counterfactual
 * labels end 7px apart. Both charts exist to show lines approaching each
 * other, so the collision is the normal case rather than an edge one.
 *
 * Takes and returns y positions in SVG user units, in the caller's series
 * order. `bounds` is the range the labels have to stay inside, in those same
 * units; without it the stack can only grow downwards.
 */
export function spreadLabels(values: number[], gap = 17, bounds?: [number, number]): number[] {
  const order = values.map((y, i) => ({ y, i })).sort((a, b) => a.y - b.y);
  for (let k = 1; k < order.length; k += 1) {
    const overlap = order[k - 1].y + gap - order[k].y;
    if (overlap > 0) order[k].y += overlap;
  }

  /*
   * The pass above only ever pushes DOWN, which is fine for two or three labels
   * and wrong for eight.
   *
   * The zone chart's lines converge into the bottom of the plot, so the stack
   * started near the floor and every push sent it further past it: measured,
   * the last two labels sat below the plot area, level with the x-axis row. The
   * labels were correct and the chart looked broken.
   *
   * So when the caller says how much room there is, the stack is pulled back
   * inside and re-spread upwards from the bottom. If the labels genuinely
   * cannot fit — more than `range / gap` of them — they end up evenly filling
   * the range instead of overflowing it, which is the least-bad answer and
   * still monotone, so the reading order never lies.
   */
  // An empty set is a legitimate call — a chart whose legend sits inside the
  // plot has no end labels to spread — and the bounds pass indexes the ends.
  if (bounds && order.length > 0) {
    const [lo, hi] = bounds;
    const last = order[order.length - 1];
    if (last.y > hi) {
      const shift = last.y - hi;
      order.forEach((o) => (o.y -= shift));
      for (let k = order.length - 2; k >= 0; k -= 1) {
        const overlap = order[k].y + gap - order[k + 1].y;
        if (overlap > 0) order[k].y -= overlap;
      }
    }
    if (order[0].y < lo) {
      const shift = lo - order[0].y;
      order.forEach((o) => (o.y += shift));
      // Squeeze from the top down; anything still past `hi` had no room to
      // begin with, and an even fill beats a stack hanging off the plot.
      for (let k = 1; k < order.length; k += 1) {
        order[k].y = Math.min(order[k].y, hi - (order.length - 1 - k) * gap);
        order[k].y = Math.max(order[k].y, order[k - 1].y);
      }
    }
  }

  const out = new Array<number>(values.length);
  order.forEach(({ y, i }) => (out[i] = y));
  return out;
}
