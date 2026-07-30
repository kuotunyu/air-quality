/**
 * robots.txt, mostly so the sitemap can be found without being guessed.
 *
 * Everything here is public and meant to be read, so nothing is disallowed —
 * except the L1 Parquet directory, and that is not secrecy either. Those files
 * are served for DuckDB to read over HTTP range requests, a crawler that fetches
 * them whole gets tens of megabytes of binary it cannot index, and the same data
 * is already published in a form it can: the JSON under `data/l0`.
 */
import type { APIRoute } from "astro";

export const GET: APIRoute = ({ site }) => {
  const origin = site ?? new URL("http://localhost:4321/");
  const base = import.meta.env.BASE_URL.replace(/\/*$/, "/");
  const sitemap = new URL(`${base}sitemap.xml`, origin).href;

  return new Response(
    `User-agent: *
Allow: /
Disallow: ${base}data/l1/

Sitemap: ${sitemap}
`,
    { headers: { "Content-Type": "text/plain; charset=utf-8" } },
  );
};
