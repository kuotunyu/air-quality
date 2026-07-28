/**
 * Human labels for the enumerations the pipeline stores as codes.
 *
 * The store keeps machine values on purpose — they are stable across renames
 * and safe to group by. Translation belongs at the edge, here, rather than in
 * the data, so the same Parquet serves a Chinese page and an English one.
 */

export const STATION_TYPE: Record<string, string> = {
  general: "一般站",
  traffic: "交通站",
  industrial: "工業站",
  background: "背景站",
  national_park: "國家公園站",
  reference: "參考站",
};

export function stationType(code: string | null | undefined): string {
  if (!code) return "";
  return STATION_TYPE[code] ?? code;
}

/**
 * Twelve-point compass, in the meteorological convention.
 *
 * Wind direction is the direction the wind blows *from*, so 330° is wind
 * arriving out of the north-north-west — which for Taiwan in winter means the
 * north-east monsoon carrying continental air down the strait. Labelling the
 * sector by where the air came from is the whole reason this chart is
 * interesting.
 */
const COMPASS: Record<number, string> = {
  0: "北",
  30: "北北東",
  60: "東北東",
  90: "東",
  120: "東南東",
  150: "南南東",
  180: "南",
  210: "南南西",
  240: "西南西",
  270: "西",
  300: "西北西",
  330: "北北西",
};

export function compass(degrees: number): string {
  const label = COMPASS[degrees];
  return label ? `${degrees}° ${label}` : `${degrees}°`;
}

/** Whether a synthetic normality sample was drawn from a normal distribution. */
export const SYNTHETIC_DATA: Record<string, string> = {
  truly_normal: "真的是常態",
  slightly_skewed: "輕微偏態",
};
