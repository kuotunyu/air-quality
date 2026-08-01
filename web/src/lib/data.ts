/**
 * Typed access to the exported data layers.
 *
 * Everything here is read at build time by Astro from `public/data/`, which is
 * produced by `uv run twair export web`. The types mirror
 * `src/twair/viz/export.py` and `src/twair/viz/story.py`; if a field moves
 * there, the build breaks here, which is the intent.
 */

import meta from "../../public/data/meta.json";
import manifest from "../../public/data/manifest.json";
import l0Index from "../../public/data/l0/index.json";
import trendNational from "../../public/data/story/trend-national.json";
import trendAirzone from "../../public/data/story/trend-airzone.json";
import trendCounty from "../../public/data/story/trend-county.json";
import stationCards from "../../public/data/story/station-cards.json";
import pitfalls from "../../public/data/story/pitfalls.json";
import replication from "../../public/data/story/replication.json";
import imputation from "../../public/data/story/imputation.json";
import deweather from "../../public/data/story/deweather.json";
import sarima from "../../public/data/story/sarima.json";
import { COUNTIES } from "./taiwan";

/**
 * Stations grouped by county, counties running north to south.
 *
 * A `<select>` of 74 stations sorted by name puts 二林（彰化）next to
 * 三義（苗栗）next to 土城（新北）: three counties and 300km in three rows. The
 * order a reader can actually use is geographic, and the coordinates to build it
 * are already in the register — so it is derived rather than typed out. A
 * hand-written county list would be correct today and wrong the first time the
 * register changes, on a site whose whole claim is that its numbers can be
 * checked.
 *
 * Order, in three tiers:
 *
 * 1. Main-island counties, by the mean latitude of their own stations. That is
 *    literally north to south, so the east coast interleaves with the west —
 *    宜蘭 sits between 新竹市 and 苗栗, 花蓮 between 彰化 and 南投. True to the
 *    latitude, and the alternative would mean inventing a west-then-east
 *    convention rather than reading one off the data.
 * 2. Offshore counties. "Offshore" is not a list here either: it is any county
 *    the map's own boundary data has no main-island geometry for, which is the
 *    same test `StationMap` uses to decide what it can draw. Otherwise 連江縣
 *    would lead the whole select on the strength of being at 26.15°N.
 * 3. Stations the register has no county for at all — six of them, all with
 *    decades of observations and no coordinate. Grouped and labelled rather
 *    than dropped, for the same reason the map lists them.
 */
export interface CountyGroup<T> {
  label: string;
  items: T[];
}

const MAIN_ISLAND = new Set(COUNTIES.map((county) => county.name));
const NO_COUNTY = "無縣市紀錄";

/**
 * Each county's latitude, averaged over every station the register places in
 * it — not over the stations of whichever list is being sorted.
 *
 * Computed from the list, chapter 3 and chapter 2 disagreed about 臺北市 and
 * 新北市: chapter 3 has 富貴角 at 25.30 but no 淡水, chapter 2 has 陽明 at 25.18,
 * and the two means crossed over. Same rule, different data, two different
 * orders — which is the complaint this ordering exists to answer, one level up.
 */
const countyLatitude = new Map<string, number>();
{
  const sums = new Map<string, { total: number; n: number }>();
  for (const station of meta.stations as Station[]) {
    if (station.lat == null || !station.county) continue;
    const acc = sums.get(station.county) ?? { total: 0, n: 0 };
    acc.total += station.lat;
    acc.n += 1;
    sums.set(station.county, acc);
  }
  for (const [county, { total, n }] of sums) countyLatitude.set(county, total / n);
}

export function groupByCountyNorthToSouth<T>(
  items: T[],
  nameOf: (item: T) => string,
): CountyGroup<T>[] {
  const placed = new Map(stations.map((s) => [s.station_name, s]));

  const groups = new Map<string, T[]>();
  for (const item of items) {
    const county = placed.get(nameOf(item))?.county || NO_COUNTY;
    if (!groups.has(county)) groups.set(county, []);
    groups.get(county)!.push(item);
  }

  const latitudeOf = (item: T) => placed.get(nameOf(item))?.lat ?? null;

  // Tier first, then latitude, then name — so the result is total and stable
  // rather than depending on Map insertion order for the ties.
  const tier = (county: string) =>
    county === NO_COUNTY ? 2 : MAIN_ISLAND.has(county) ? 0 : 1;

  return [...groups.entries()]
    .map(([label, group]) => ({ label, items: group, lat: countyLatitude.get(label) ?? null }))
    .sort(
      (a, b) =>
        tier(a.label) - tier(b.label) ||
        (b.lat ?? -Infinity) - (a.lat ?? -Infinity) ||
        a.label.localeCompare(b.label, "zh-Hant"),
    )
    .map(({ label, items: group }) => ({
      label,
      items: [...group].sort(
        (a, b) =>
          (latitudeOf(b) ?? -Infinity) - (latitudeOf(a) ?? -Infinity) ||
          nameOf(a).localeCompare(nameOf(b), "zh-Hant"),
      ),
    }));
}

export interface Station {
  station_name: string;
  station_name_en?: string | null;
  county?: string | null;
  township?: string | null;
  lon?: number | null;
  lat?: number | null;
  airzone: string | null;
  station_type: string;
  first_year: number;
  last_year: number;
  years_present: number;
}

export interface Pollutant {
  code: string;
  name_zh: string;
  unit: string;
  valid_range: [number, number];
  circular: boolean;
  caveat: string | null;
}

export interface StationCard {
  station_name: string;
  county?: string | null;
  township?: string | null;
  airzone?: string | null;
  station_type?: string | null;
  lon?: number | null;
  lat?: number | null;
  year: number;
  annual_mean: number;
  worst_day: number;
  days_with_data: number;
  days_over_who: number | null;
  days_over_taiwan: number | null;
  rank: number | null;
  stations_ranked: number | null;
  times_who_annual: number;
  cigarettes_per_day: number;
}

/**
 * A station-month grid for one measurand.
 *
 * `mean[s][m]` is null in two distinct cases and `n_days[s][m]` is what tells
 * them apart: 0 means the station was not measuring, greater than 0 means the
 * aggregate was withheld because coverage fell below threshold. A chart must
 * break its line in both cases and must never bridge them.
 */
export interface PollutantGrid {
  pollutant: string;
  name_zh: string;
  unit: string;
  precision: number;
  months: string[];
  stations: string[];
  mean: (number | null)[][];
  n_days: number[][];
}

export const stations = meta.stations as Station[];
export const pollutants = meta.pollutants as Pollutant[];
export const stationsWithoutCoordinates = meta.stations_without_coordinates as string[];
export const generatedAt = meta.generated_at as string;

/**
 * The record's span, measured rather than typed.
 *
 * The entry page's opening line stated 「1982 – 2025 · 82 個測站」 as literal text
 * in the markup. Two of those three figures are derivable from data the site
 * already ships, and this repository has a rule about the third kind: a figure
 * typed into a file is a figure with no test behind it, correct on the day it was
 * written and quietly wrong after the next pipeline run. A station added to the
 * network would have left 「82」 sitting there.
 *
 * Taken across every station's own first and last year, so it is the span of the
 * whole record and not of any one measurand.
 */
export const recordSpan = ((): { first: number; last: number } => {
  const rows = meta.stations as Station[];
  const first = rows.reduce((lo, s) => Math.min(lo, s.first_year), Infinity);
  const last = rows.reduce((hi, s) => Math.max(hi, s.last_year), -Infinity);
  return { first, last };
})();

/**
 * How many hourly observations the record holds, or `null` when the export could
 * not say.
 *
 * This is the one figure on the entry page that the published data cannot check:
 * it counts L2 station-hours, and L2 is deliberately not published here. It was
 * written into the markup as 341,442,552 — a number that appears nowhere else in
 * this repository, so nothing could have contradicted it.
 *
 * `export_meta` now records it, and until a pipeline run fills it in the opening
 * line simply leaves the clause out. An absent figure is recoverable; a wrong one
 * that nobody can check is not.
 */
/*
 * Declared, pending a run that measures it.
 *
 * This figure was typed into SIX user-visible strings across five files, in two
 * formats — 「341,442,552」 in the footer and 「3.41 億」 in three notes, a meta
 * description and the entry page. Six copies of a number nothing can check: if it
 * ever changed, six edits would have to happen by hand and a missed one would be
 * silent.
 *
 * It is almost certainly right — it came from a real run — so it stays on the
 * page rather than being deleted for being unprovable. But it lives in one place
 * now, it is labelled as the one declared figure on the site, and the measured
 * value supersedes it automatically the moment `export_meta` provides one.
 *
 * It was not right. The run it came from counted the 1999 archive twice for ten
 * 雲嘉南 stations, which the agency filed under 北部空品區 as well — 1,071,168
 * station-hours stored twice, 0.31% of the total. "It came from a real run" was
 * the whole of the argument for it, and a real run is exactly what produced it.
 * The store now holds 340,371,384 rows and no duplicate keys.
 */
const DECLARED_HOURLY_OBSERVATIONS = 340_371_384;

export const hourlyObservations =
  (meta as { hourly_observations?: number | null }).hourly_observations ??
  DECLARED_HOURLY_OBSERVATIONS;

/** Whether the figure above was measured by the export or is still the declared one. */
export const hourlyObservationsMeasured =
  (meta as { hourly_observations?: number | null }).hourly_observations != null;

/**
 * The same count in the form running prose wants: 「3.40 億」.
 *
 * Two spellings of one number is how the six copies drifted apart in the first
 * place, so both spellings come from here.
 */
export const hourlyObservationsShort = `${(hourlyObservations / 1e8).toFixed(2)} 億`;

/** Full digits, grouped: 「340,371,384」. */
export const hourlyObservationsFull = hourlyObservations.toLocaleString("en-US");
export const gitSha = meta.git_sha as string | null;

/**
 * Whether the tree had uncommitted changes when this data was exported.
 *
 * The export stamps `rev-parse HEAD`, which names the last COMMIT, not the
 * tree. Running it over uncommitted work — which is what happens every time,
 * since the export necessarily precedes the commit that carries its own output
 * — records the commit BEFORE the change that produced the data. The footer
 * prints that sha and links it, so without this flag the link is an assertion
 * nobody checked and the audit found it pointing at the wrong commit.
 *
 * `?? true` because the honest default for an unknown tree state is the one
 * that claims less. A payload exported before this field existed cannot prove
 * it was clean.
 */
export const gitDirty = ((meta as { git_dirty?: boolean }).git_dirty ?? true) as boolean;

export const pollutantIndex = l0Index.pollutants as {
  pollutant: string;
  name_zh: string;
  unit: string;
  file: string;
  months: [string, string];
  bytes: number;
}[];

export const trend = trendNational;
export const trendByAirzone = trendAirzone;
export const trendByCounty = trendCounty;
export const cards = stationCards.cards as StationCard[];
/**
 * The limits the exceedance counts were actually made against.
 *
 * Typed rather than `Record<string, number | object>` because the card prints
 * these numbers, and the whole point of reading them from the payload is that
 * the label cannot disagree with the count. A loose type puts that guarantee
 * back in the author's hands.
 */
export interface Guidelines {
  who_2021_annual: number;
  who_2021_daily: number;
  taiwan_annual: number;
  taiwan_daily: number;
  source: { who: string; taiwan: string };
}

export const guidelines = stationCards.guidelines as unknown as Guidelines;
export const cigaretteCaveat = stationCards.cigarette_caveat as string;
export const pitfallTables = pitfalls.tables as Record<string, Record<string, unknown>[]>;
/** Chapter 1's second correction: how much of the fall was the weather.
 *
 * `series` carries both lines and they come from the same rows — M4's own
 * monthly output — so the difference between them cannot be the station set.
 */
export const deweatherEvidence = deweather as {
  method: string;
  series: { year: number; observed: number; normalised: number }[];
  panel: {
    balanced_since: number;
    n_stations: number;
    station_years: number;
    selection_rule: string;
    why_same_source: string;
    observed_fall: number;
    normalised_fall: number;
    weather_share_of_fall: number;
  };
  n_stations: number;
  n_significant: number;
  median_observed_slope: number;
  median_normalised_slope: number;
  median_weather_share: number;
  weather_share_p10: number;
  weather_share_p90: number;
  median_holdout_r2: number;
  caveat: string;
  by_zone: { airzone: string; n: number; observed: number; normalised: number; weather_share: number }[];
};

/**
 * Chapter 5's coda: D10 — the model the 2018 project set aside as inconvenient.
 *
 * `not_comparable` is a field rather than a comment because the chapter prints
 * it. These origins are not M9's origins, and the one thing a reader will want
 * to do with this table is set it beside the LightGBM numbers above it.
 */
export const sarimaEvidence = sarima as {
  quote: string;
  quote_page: number;
  period: number[];
  order: string;
  stations: number;
  splits: number;
  origin_stride_hours: number;
  fits: { total: number; converged: number; median_seconds: number; median_observed: number };
  selection_cost: {
    points: number;
    auto_seconds: number;
    fixed_seconds: number;
    multiple: number;
  }[];
  horizons: {
    horizon: number;
    origins: number;
    sarima_rmse: number;
    persistence_rmse: number;
    climatology_rmse: number;
    best_baseline: string;
    /** Signed so positive always means SARIMA won. */
    margin: number;
  }[];
  why_multiple_grows: string;
  why_it_loses_at_one_hour: string;
  verdict: string;
  not_comparable: string;
  no_lightgbm: string;
};

/** Pitfall 07: what the 2018 project's gap-filling sentence cost. */
export interface GapScore {
  strategy: string;
  gap_bucket: string;
  n: number;
  mae: number | null;
  rmse: number | null;
}

export const imputationEvidence = imputation as {
  period: string;
  stations_measured: number;
  stations_compared: number;
  hidden: number;
  /** Physical order, not alphabetical — ">48h" sorts first as a string. */
  buckets: string[];
  distribution: {
    gap_bucket: string;
    gaps: number;
    hours: number;
    share_of_gaps: number;
    share_of_missing_hours: number;
  }[];
  pooled: {
    strategy: string;
    recovered: number;
    recovery_rate: number | null;
    mae: number | null;
    rmse: number | null;
    bias: number | null;
  }[];
  by_gap: GapScore[];
  method: Record<string, unknown>;
  not_reported: Record<string, string>;
};

export const replicationRows = replication.rows as {
  kind: string;
  item: string;
  published_2018: number | null;
  reproduced: number | null;
  difference: number | null;
  pct_difference: number | null;
}[];

/** Stations that can be drawn on a map, sorted north to south. */
export function mappableStations(): Station[] {
  return stations
    .filter((s) => s.lat != null && s.lon != null)
    .sort((a, b) => (b.lat as number) - (a.lat as number));
}

/** Prefix a public asset path with the configured base, without doubling slashes. */
export function asset(path: string): string {
  const base = import.meta.env.BASE_URL ?? "/";
  return `${base.replace(/\/$/, "")}/${path.replace(/^\//, "")}`;
}

/**
 * The published data layers, as things a reader can actually download.
 *
 * The site shipped 60 MB of data and linked to none of it. Counted across all
 * eleven routes: zero `a[download]`, zero hrefs ending in `.json` or `.parquet`,
 * zero containing `/l0/` or `/l1/`. Chapter 10 said the L1 layer was 「共 54.6
 * MB。供 DuckDB-WASM 或桌面工具使用」 and then offered nothing to click, so a
 * reader who had accepted the site's central claim — that every number can be
 * recomputed — reached the last chapter and had to guess a URL or leave.
 *
 * Built from `manifest.json` rather than from a list typed here, for the reason
 * this file exists at all: the manifest is what `twair export web` wrote and
 * what `scripts/check_web_export.py` checks in CI, so a file that moves or
 * changes size cannot leave a stale number on the page. The Chinese names come
 * from the L0 index, which is the same export's own register of measurands.
 *
 * The two layers are matched on the filename stem and the pairing is asserted,
 * because a half-listed measurand would be worse than an unlisted one: it would
 * look like the Parquet did not exist.
 */
export interface DataFile {
  /**
   * Site-relative, ready for `asset()`.
   *
   * NOT the manifest's own spelling. `manifest.json` records
   * `l0/amb_temp.json`, relative to `public/data/`, because that is where the
   * exporter wrote it — but the file is SERVED at `data/l0/amb_temp.json`.
   * Passing the manifest path straight to `asset()` produced 42 links to
   * `/l0/…` that all 404, and every one of them looked right in the markup.
   * The prefix is added once, here, where the difference between "where the
   * exporter put it" and "where a reader fetches it" is the whole subject.
   */
  path: string;
  /** The manifest's own key, kept so a lookup can be checked against it. */
  file: string;
  bytes: number;
}

export interface MeasurandFiles {
  pollutant: string;
  name_zh: string;
  unit: string;
  months: [string, string];
  l0: DataFile;
  l1: DataFile;
}

const manifestBytes = new Map(
  (manifest.files as { file: string; bytes: number }[]).map(
    (f) => [f.file, f.bytes] as const,
  ),
);

export const dataFiles: MeasurandFiles[] = pollutantIndex.map((p) => {
  const stem = p.file.replace(/^l0\//, "").replace(/\.json$/, "");
  const l1File = `l1/${stem}.parquet`;
  const l1Bytes = manifestBytes.get(l1File);
  if (l1Bytes === undefined) {
    throw new Error(`${p.pollutant}: ${l1File} is not in manifest.json`);
  }
  return {
    pollutant: p.pollutant,
    name_zh: p.name_zh,
    unit: p.unit,
    months: p.months,
    l0: { path: `data/${p.file}`, file: p.file, bytes: p.bytes },
    l1: { path: `data/${l1File}`, file: l1File, bytes: l1Bytes },
  };
});

/** Layer totals, summed from the same manifest the page links to. */
export const layerBytes = {
  l0: dataFiles.reduce((a, f) => a + f.l0.bytes, 0),
  l1: dataFiles.reduce((a, f) => a + f.l1.bytes, 0),
};

/**
 * Decimal MB, because that is what a browser's download panel shows and the
 * reader is about to compare the two numbers.
 */
export function mb(bytes: number): string {
  return `${(bytes / 1e6).toFixed(bytes >= 1e7 ? 1 : 2)} MB`;
}
