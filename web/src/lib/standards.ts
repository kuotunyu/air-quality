/**
 * 空氣品質標準 for PM2.5, and the two dates it changed on.
 *
 * These live here rather than in the chapter that first needed them because two
 * chapters now disagree if they drift apart: chapter 1 draws the annual limit as
 * a step over time, and chapter 2 has to say which daily limit its exceedance
 * count was made against. The dates are the same dates.
 *
 * The values a chart DRAWS come from here. The value a card LABELS comes from
 * the exported payload — `guidelines.taiwan_daily` in `station-cards.json` —
 * because that number has to match what the pipeline actually counted against,
 * and the only way to guarantee that is to print the same constant the counting
 * used. This module carries the history; the payload carries the count's own
 * yardstick. See the note above `GUIDELINES` in `src/twair/viz/story.py` for why
 * a time series and a per-station snapshot are allowed to answer differently.
 */

/** 民國101年5月14日 — the year PM2.5 entered the standard at 15 annual / 35 daily. */
export const TW_PM25_ADDED = 2012;

export const TW_PM25_ADDED_DATE = "2012-05-14";

/** 民國113年9月30日 — the year it was tightened to 12 annual / 30 daily. */
export const TW_PM25_TIGHTENED = 2024;

export const TW_PM25_TIGHTENED_DATE = "2024-09-30";

/** The daily limit before the 2024 revision, for a caveat that names both. */
export const TW_PM25_DAILY_BEFORE = 35;

/**
 * The annual limit in force at the END of a given year, or null if there was
 * none. A null is drawn as a gap on this site, which is the honest mark for
 * fourteen of the twenty-eight years chapter 1 covers.
 */
export function taiwanAnnual(year: number): number | null {
  if (year < TW_PM25_ADDED) return null;
  return year < TW_PM25_TIGHTENED ? 15 : 12;
}
