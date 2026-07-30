/**
 * DuckDB-WASM, loaded on demand.
 *
 * Nothing here runs until the reader asks for it. The engine is roughly 30 MB
 * of WebAssembly, which is a reasonable price for querying four decades of
 * measurements in a browser tab and an absurd one for a page the reader only
 * scrolled past.
 *
 * The `eh` bundle (exception handling, single-threaded) is chosen deliberately
 * over `coi`: the threaded build needs cross-origin isolation headers, and
 * GitHub Pages cannot set headers. A site that only works behind a custom
 * server would defeat the point of publishing it.
 */

import type * as DuckDB from "@duckdb/duckdb-wasm";

// These resolve to URL strings at build time and cost nothing to import; the
// library itself is loaded dynamically below.
import mvpWorker from "@duckdb/duckdb-wasm/dist/duckdb-browser-mvp.worker.js?url";
import mvpWasm from "@duckdb/duckdb-wasm/dist/duckdb-mvp.wasm?url";
import ehWorker from "@duckdb/duckdb-wasm/dist/duckdb-browser-eh.worker.js?url";
import ehWasm from "@duckdb/duckdb-wasm/dist/duckdb-eh.wasm?url";

export type AsyncDuckDB = DuckDB.AsyncDuckDB;

let dbPromise: Promise<AsyncDuckDB> | null = null;

export async function getDb(onProgress?: (message: string) => void): Promise<AsyncDuckDB> {
  if (dbPromise) return dbPromise;

  dbPromise = (async () => {
    onProgress?.("載入查詢引擎");
    // Dynamic, not top-level. A static import puts ~190 KB of DuckDB's
    // JavaScript in the page's initial bundle, which every reader pays for and
    // only the ones who scroll to chapter 6 and press the button use.
    const duckdb = await import("@duckdb/duckdb-wasm");

    const bundle = await duckdb.selectBundle({
      mvp: { mainModule: mvpWasm, mainWorker: mvpWorker },
      eh: { mainModule: ehWasm, mainWorker: ehWorker },
    });

    onProgress?.("啟動資料庫");
    const worker = new Worker(bundle.mainWorker!);
    // The logger is silent on purpose: DuckDB's own console output is verbose
    // enough to bury a real page error.
    const db = new duckdb.AsyncDuckDB(new duckdb.VoidLogger(), worker);
    await db.instantiate(bundle.mainModule, bundle.pthreadWorker);
    return db;
  })();

  return dbPromise;
}

export interface Candidate {
  code: string;
  name: string;
  file: string;
}

/**
 * Register the Parquet files that are actually being served.
 *
 * Which L1 files ship alongside the site is a deployment decision — the full
 * set is 55 MB and only a subset is committed — so the candidate list is
 * *probed* rather than trusted. A table the reader can see in the list is a
 * table that exists; there is no way for this UI to advertise a 404.
 *
 * DuckDB then reads them over HTTP range requests, so a query touching one
 * station and one year fetches kilobytes rather than the whole file. That is
 * the entire reason L1 is Parquet and not JSON.
 */
export async function attachTables(
  db: AsyncDuckDB,
  base: string,
  candidates: Candidate[],
): Promise<Candidate[]> {
  const duckdb = await import("@duckdb/duckdb-wasm");
  const root = new URL(base, window.location.href);

  const probes = await Promise.all(
    candidates.map(async (candidate) => {
      const url = new URL(candidate.file, root).href;
      try {
        const response = await fetch(url, { method: "HEAD" });
        return response.ok ? { candidate, url } : null;
      } catch {
        return null;
      }
    }),
  );

  const present = probes.filter((p): p is { candidate: Candidate; url: string } => p !== null);
  const connection = await db.connect();

  try {
    for (const { candidate, url } of present) {
      await db.registerFileURL(candidate.file, url, duckdb.DuckDBDataProtocol.HTTP, false);
      // Quoted because "PM2.5" is not a bare identifier.
      await connection.query(
        `CREATE OR REPLACE VIEW "${candidate.code}" AS
           SELECT * FROM read_parquet('${candidate.file}')`,
      );
    }

    // One long view over everything, for questions that cross measurands.
    if (present.length > 0) {
      const union = present
        .map(
          ({ candidate }) =>
            `SELECT '${candidate.code}' AS pollutant, * FROM read_parquet('${candidate.file}')`,
        )
        .join(" UNION ALL ");
      await connection.query(`CREATE OR REPLACE VIEW daily AS ${union}`);
    }
  } finally {
    await connection.close();
  }

  return present.map((p) => p.candidate);
}

export interface QueryResult {
  columns: string[];
  rows: unknown[][];
  /**
   * Arrow's own description of each column, e.g. "Int32", "Float64",
   * "Date32<DAY>", "Utf8".
   *
   * Returned rather than discarded because the renderer has to align each column
   * and cannot do it from the values. `normalise` below turns a DATE into a
   * string and a BIGINT into a number, so by the time a value reaches the table
   * a year, a count and a measurement are all just `number`, and a date is
   * indistinguishable from a station name. The declared type is the only thing
   * that still knows which is which.
   */
  kinds: string[];
  /**
   * Rows the query actually produced, before the display cap.
   *
   * Reported because the cap was silent: a query matching 40,000 station-days
   * rendered 500 of them under a status line reading 「500 列」, which on this
   * site is the wrong kind of wrong — the whole page exists so a reader can
   * check a number, and a truncated answer that does not say it is truncated
   * invites them to check it against the wrong denominator.
   */
  total: number;
  /** Whether `rows` is a prefix of the result rather than the whole of it. */
  truncated: boolean;
  ms: number;
}

/** What a column is, for the purpose of laying it out. */
export type ColumnKind = "number" | "date" | "bool" | "text";

/**
 * Arrow type name to layout class.
 *
 * Date and Timestamp are deliberately NOT "number" even though they arrive as
 * integers from Arrow: `normalise` has already rendered them to ISO strings, and
 * an ISO string is fixed-width, so it aligns on the left without help. Treating
 * them as numbers would right-align a date against a column of measurements.
 */
export function columnKind(arrowType: string): ColumnKind {
  const t = arrowType.trim();
  if (/^(date|timestamp|time)/i.test(t)) return "date";
  if (/^bool/i.test(t)) return "bool";
  if (/^(int|uint|float|decimal|half|double)/i.test(t)) return "number";
  return "text";
}

export async function runQuery(
  db: AsyncDuckDB,
  sql: string,
  limit = 500,
): Promise<QueryResult> {
  const connection = await db.connect();
  const started = performance.now();
  try {
    const table = await connection.query(sql);
    const fields = table.schema.fields;
    const columns = fields.map((f) => f.name);
    // Arrow's own description of each column, e.g. "Date32<DAY>". Formatting
    // has to be driven by this rather than by the value: a DATE arrives as a
    // plain number and is indistinguishable from a measurement.
    const kinds = fields.map((f) => String(f.type));
    // DECIMAL carries its scale on the type rather than in the value, and the
    // value arrives unscaled. The type STRING is no help — Arrow renders
    // DECIMAL(2,1) as "Decimal[2e+1]" — so the number is read off the field.
    const scales = fields.map((f) => {
      const declared = (f.type as { scale?: unknown }).scale;
      return typeof declared === "number" ? declared : null;
    });

    /*
     * Read positionally, not by column name.
     *
     * This was `for (const row of table.toArray())` followed by
     * `record[name]`, which silently returns the wrong data whenever two
     * columns share a name: `SELECT 1 AS a, 2 AS a` printed 1 twice, and
     * `SELECT a.date, b.date FROM x a JOIN y b` — an ordinary join on this
     * schema — printed the first date in both columns. A name is not a key here;
     * the position is.
     */
    const vectors = fields.map((_, i) => table.getChildAt(i));
    const rows: unknown[][] = [];
    const total = table.numRows;
    const wanted = Math.min(total, limit);
    for (let r = 0; r < wanted; r += 1) {
      rows.push(vectors.map((vec, i) => normalise(vec?.get(r), kinds[i], scales[i])));
    }

    return {
      columns,
      kinds,
      rows,
      total,
      truncated: total > limit,
      ms: performance.now() - started,
    };
  } finally {
    await connection.close();
  }
}

const MS_PER_DAY = 86_400_000;

/** Arrow hands back typed values that do not stringify usefully on their own. */
function normalise(value: unknown, kind = "", scale: number | null = null): unknown {
  if (value == null) return null;

  if (value instanceof Date) return value.toISOString().slice(0, 10);

  /*
   * DECIMAL, and this one was printing wrong numbers on the page.
   *
   * Arrow hands a DECIMAL back as a `DecimalBigNum` object whose `String()` is
   * the UNSCALED integer, so `SELECT 0.6` reached the table as 6. Worse inside a
   * `VALUES` list, where DuckDB widens every row to one common type: a column of
   * (0.6, 0.637, 0.5834) becomes DECIMAL(5,4) and printed 6000, 6370, 5834. The
   * value fell through every branch below — it is an object, so the `bigint`
   * test never matched — and arrived as whatever `String()` made of it.
   *
   * The point is reinserted by string surgery rather than by dividing, because
   * DECIMAL(38,6) does not survive a round trip through a double. Trailing zeros
   * from the widening are dropped: they are DuckDB's artefact, not a precision
   * the reader asked for, and this file's whole policy is not to print digits
   * nobody measured.
   */
  if (/^decimal/i.test(kind)) {
    const s = scale ?? 0;
    const text = String(value);
    const negative = text.startsWith("-");
    const digits = negative ? text.slice(1) : text;
    if (!/^\d+$/.test(digits)) return text;
    let out: string;
    if (s <= 0) {
      out = digits;
    } else {
      const padded = digits.padStart(s + 1, "0");
      const whole = padded.slice(0, padded.length - s);
      const frac = padded.slice(padded.length - s).replace(/0+$/, "");
      out = frac ? `${whole}.${frac}` : whole;
    }
    if (negative) out = `-${out}`;
    // Only hand back a number when it survives the trip; otherwise the exact
    // decimal string, which the table right-aligns anyway because the COLUMN is
    // typed Decimal.
    const asNumber = Number(out);
    return String(asNumber) === out ? asNumber : out;
  }

  if (kind.startsWith("Date")) {
    // The declared unit lies. A DuckDB DATE arrives as Arrow `Date32<DAY>`,
    // but `Table.toArray()` has already multiplied it up to epoch
    // milliseconds — trusting the unit and multiplying again lands the row
    // somewhere around 45,000 BC.
    //
    // Arrow's behaviour here has changed between versions, so the scale is
    // read from the magnitude instead. The two ranges cannot be confused:
    // any date this project holds is under 50,000 as a day count and over
    // 3e11 as a millisecond count.
    const raw = typeof value === "bigint" ? Number(value) : (value as number);
    const ms = Math.abs(raw) < 1e7 ? raw * MS_PER_DAY : raw;
    const date = new Date(ms);
    return Number.isNaN(date.getTime()) ? raw : date.toISOString().slice(0, 10);
  }

  if (kind.startsWith("Timestamp")) {
    const raw = typeof value === "bigint" ? Number(value) : (value as number);
    return new Date(raw).toISOString().replace("T", " ").slice(0, 19);
  }

  // BigInt survives JSON but not `String()` in a table cell without a suffix.
  if (typeof value === "bigint") return Number(value);

  if (typeof value === "number" && !Number.isInteger(value)) {
    // L1 stores `mean` as Float32, which cannot represent a value already
    // rounded to two decimals: 328.59 comes back as 328.5899963378906. Float32
    // carries about seven significant decimal digits, so everything past the
    // seventh is representation noise rather than measurement, and printing it
    // implies a precision no instrument here has.
    return Number(value.toPrecision(7));
  }

  return value;
}
