// @ts-check
import { defineConfig } from "astro/config";

// GitHub Pages serves a project site under /<repo>/, so every asset URL needs
// that prefix. Both values come from the environment so a fork, a custom
// domain, or a local preview can all build without editing this file.
const site = process.env.PUBLIC_SITE_URL ?? "https://example.github.io";
const base = process.env.PUBLIC_BASE_PATH ?? "/";

export default defineConfig({
  site,
  base,
  trailingSlash: "ignore",
  build: {
    // One stylesheet beats a request per component on a site this small.
    inlineStylesheets: "auto",
  },
  vite: {
    build: {
      // The data files are already minified JSON; the charts are the only
      // meaningful JS. Keeping chunks whole avoids a waterfall on first paint.
      chunkSizeWarningLimit: 800,
    },
  },
});
