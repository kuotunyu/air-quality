/**
 * DuckDB-WASM, loaded on demand.
 *
 * Nothing here runs until the reader asks for it. The engine is 35.9 MB of
 * WebAssembly that transfers as 8.1 MB gzipped, plus a 0.2 MB worker — which is a
 * reasonable price for querying four decades of measurements in a browser tab and
 * an absurd one for a page the reader only scrolled past.
 *
 * The page used to promise 「約 30 MB」, the uncompressed figure, and Pages serves
 * this gzipped. Overstating the cost 3.6x on a site whose whole register is that
 * numbers are measured makes readers decline something cheaper than advertised,
 * so the prose quotes the transfer and this comment carries both.
 *
 * The `eh` bundle (exception handling, single-threaded) is chosen deliberately
 * over `coi`: the threaded build needs cross-origin isolation headers, and
 * GitHub Pages cannot set headers. A site that only works behind a custom
 * server would defeat the point of publishing it.
 *
 * The version in package.json is pinned exactly, without a caret, and the
 * reason is upstream's dist-tags: `latest` is `1.33.1-dev57.0` and `next` is
 * `1.33.1-dev64.0`. duckdb-wasm has published no stable release above 1.32.0,
 * so `npm i` gives you a prerelease whether or not you asked for one — and
 * `^1.33.1-dev57.0` matches every later dev build of 1.x, of which four
 * already exist. package-lock.json holds the deployed build steady, but any
 * `npm install` would have moved these four imports, and they are the bytes
 * the reader downloads.
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

/**
 * How long the engine may go without loading a single byte before we call it.
 *
 * Not a total timeout, and that distinction is the whole design. A total
 * timeout has to be longer than the slowest honest load, and the slowest
 * honest load here is long: 8.13 MB gzipped over a 250 kbps link is about 260
 * seconds. Any cap short enough to catch a hang would turn a slow success into
 * a false failure — and worse, the worker keeps downloading after the promise
 * rejects, so a second press would open a second 8 MB transfer.
 *
 * Liveness is the property that actually separates the two. A load that is
 * working reports progress continuously: a successful instantiation fires the
 * callback hundreds of times, once per chunk, so the gap between ticks is set
 * by how fast bytes arrive and not by how many there are. A load that has been
 * blocked, or whose response was truncated mid-flight, reports nothing ever
 * again. Twenty seconds of complete silence is not a slow link; it is a link
 * that has stopped.
 */
const STALL_MS = 20_000;

/**
 * `db.instantiate`, but it settles.
 *
 * Measured on the shipped page: blocking the `.wasm`, blocking the worker
 * script, and sending a Content-Length and then cutting the connection at 30%
 * all leave this promise pending forever — 60.4 s, 40.2 s and 40.0 s of polling
 * with no change — so the button stays disabled, the status stays 「啟動資料庫」
 * and there is no error, no retry and no way for the reader to know the page
 * will never finish.
 *
 * The obvious repair, `worker.addEventListener('error')`, does not work here:
 * measured across those same three failures the handler never fires once. The
 * worker is alive; it is waiting on a fetch that will not complete.
 */
function instantiateOrGiveUp(
  db: AsyncDuckDB,
  mainModule: string,
  pthreadWorker: string | null | undefined,
  onTick: (p: DuckDB.InstantiationProgress) => void,
): Promise<void> {
  return new Promise<void>((resolve, reject) => {
    let timer: ReturnType<typeof setTimeout>;
    let settled = false;
    const finish = (fn: () => void) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      fn();
    };
    const arm = () => {
      clearTimeout(timer);
      timer = setTimeout(
        () =>
          finish(() =>
            reject(
              new Error(
                `查詢引擎載入停住了：已經 ${STALL_MS / 1000} 秒沒有收到任何資料。\n` +
                  "常見原因是網路中斷，或是瀏覽器擴充功能／公司網路擋掉了 .wasm 檔。\n" +
                  "再按一次「執行查詢」會重新下載。",
              ),
            ),
          ),
        STALL_MS,
      );
    };
    arm();
    db.instantiate(mainModule, pthreadWorker, (p) => {
      arm();
      onTick(p);
    }).then(
      () => finish(resolve),
      (error) => finish(() => reject(error)),
    );
  });
}

export async function getDb(onProgress?: (message: string) => void): Promise<AsyncDuckDB> {
  if (dbPromise) return dbPromise;

  /*
   * Cleared if it rejects, or the failure is permanent.
   *
   * `if (dbPromise) return dbPromise` returns the cached promise whether it
   * resolved OR rejected — so a reader whose connection dropped partway through
   * the 8 MB of WebAssembly saw an error, pressed the button again, and got the
   * identical error without a single byte being re-requested. The body ran once
   * no matter how many times they tried.
   */
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

    /*
     * The message changes while the wait does not.
     *
     * 「啟動資料庫」 was set at t = 37 ms and not replaced until t = 45,649 ms on
     * a throttled link — 45.6 seconds of one unchanging string, whose only
     * animation is the three dots in `.status::after`. A reader cannot tell
     * that from the hang above, and the hang is the state this same string
     * described for as long as they were willing to wait.
     *
     * Elapsed seconds, and deliberately NOT a percentage. `bytesLoaded` counts
     * decompressed bytes and `bytesTotal` is the compressed `Content-Length`,
     * so the obvious readout prints a fraction like 「34.2 / 7.8 MB」 — and on a
     * site whose whole register is that its numbers are measured, a number that
     * is visibly nonsense costs more than the wait it was meant to explain.
     * A counter that only goes up says the one thing the reader needs: this is
     * still moving.
     */
    const started = Date.now();
    await instantiateOrGiveUp(db, bundle.mainModule, bundle.pthreadWorker, () => {
      const seconds = Math.floor((Date.now() - started) / 1000);
      onProgress?.(seconds < 2 ? "啟動資料庫" : `啟動資料庫（${seconds} 秒）`);
    });
    return db;
  })();

  dbPromise = dbPromise.catch((error) => {
    dbPromise = null;
    throw error;
  });

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
 * DuckDB then reads them over HTTP, and Parquet is the format because its footer
 * lets a reader seek rather than scan.
 *
 * What is MEASURED: Pages serves these with `Accept-Ranges: bytes` and does not
 * gzip them (`Content-Type: application/octet-stream`, no `Content-Encoding`),
 * and a mid-file `Range: bytes=1000000-1000031` returns `206 Partial Content`
 * with exactly 32 bytes. So range reads are possible here.
 *
 * What is NOT measured, and therefore not claimed: how much a given query
 * actually transfers. This comment used to say a query touching one station and
 * one year "fetches kilobytes rather than the whole file". DuckDB issues those
 * requests from inside its Web Worker, where neither a patched `window.fetch`
 * nor the page's network log can see them, so that sentence was a reasonable
 * belief about a library rather than something anyone here had checked — and on
 * this site a number nobody has checked does not get stated.
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
