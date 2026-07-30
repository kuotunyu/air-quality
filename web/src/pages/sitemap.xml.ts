/**
 * A sitemap, generated from the chapter list rather than written by hand.
 *
 * The site had none, which for a document meant to be found and cited is a gap
 * rather than a nicety: ten of the eleven routes exist only as links from the
 * entry page, and nothing told a crawler that they are the same document.
 *
 * Built from `CHAPTERS` for the same reason the rail and the index are — a
 * hand-kept list of eleven URLs is a list that goes stale the first time a
 * chapter is inserted, and this one would go stale silently.
 *
 * `lastmod` is the pipeline's own generation time, not the build time. A rebuild
 * that changed no data should not tell a crawler the document changed.
 */
import type { APIRoute } from "astro";
import { CHAPTERS } from "../lib/chapters";
import { generatedAt } from "../lib/data";

export const GET: APIRoute = ({ site }) => {
  const origin = site ?? new URL("http://localhost:4321/");
  const base = import.meta.env.BASE_URL.replace(/\/*$/, "/");
  const lastmod = new Date(generatedAt).toISOString().slice(0, 10);

  const paths = ["", ...CHAPTERS.map((c) => c.slug)];
  const urls = paths
    .map((slug) => {
      // Trailing slash kept, not stripped: Pages 301s the bare form, and a
      // sitemap should list the URL that answers 200.
      const loc = new URL(slug ? `${base}${slug}/` : base, origin).href;
      // The entry page is the way in, so it is the one that outranks the rest.
      const priority = slug === "" ? "1.0" : "0.8";
      return `  <url>
    <loc>${loc}</loc>
    <lastmod>${lastmod}</lastmod>
    <priority>${priority}</priority>
  </url>`;
    })
    .join("\n");

  return new Response(
    `<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
${urls}
</urlset>
`,
    { headers: { "Content-Type": "application/xml; charset=utf-8" } },
  );
};
