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

/** Pad a domain so the extremes are not glued to the frame. */
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
  min -= span * pad;
  max += span * pad;
  if (zero) min = Math.min(0, min);
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
  const named = /^var\(--(c[0-6])\)$/.exec(colour.trim());
  return named ? `var(--${named[1]}-ink)` : "var(--text-muted)";
}

export function concentrationColour(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "var(--line)";
  let index = 0;
  while (index < PM25_BREAKS.length && value >= PM25_BREAKS[index]) index += 1;
  return `var(${RAMP[index]})`;
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
export function labelGutter(labels: (string | number)[], size = 18, pad = 16): number {
  const width = (label: string | number) =>
    [...String(label)].reduce(
      (sum, ch) => sum + (/[⺀-鿿＀-￯]/.test(ch) ? 1 : 0.56),
      0,
    );
  return Math.ceil(Math.max(0, ...labels.map(width)) * size + pad);
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
 * order.
 */
export function spreadLabels(values: number[], gap = 17): number[] {
  const order = values.map((y, i) => ({ y, i })).sort((a, b) => a.y - b.y);
  for (let k = 1; k < order.length; k += 1) {
    const overlap = order[k - 1].y + gap - order[k].y;
    if (overlap > 0) order[k].y += overlap;
  }
  const out = new Array<number>(values.length);
  order.forEach(({ y, i }) => (out[i] = y));
  return out;
}
