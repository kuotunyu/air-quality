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
  ms: number;
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
    const rows: unknown[][] = [];

    for (const row of table.toArray().slice(0, limit)) {
      const record = row as Record<string, unknown>;
      rows.push(columns.map((name, i) => normalise(record[name], kinds[i])));
    }

    return { columns, rows, ms: performance.now() - started };
  } finally {
    await connection.close();
  }
}

const MS_PER_DAY = 86_400_000;

/** Arrow hands back typed values that do not stringify usefully on their own. */
function normalise(value: unknown, kind = ""): unknown {
  if (value == null) return null;

  if (value instanceof Date) return value.toISOString().slice(0, 10);

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
