/**
 * Remove the spaces HTML invents inside Chinese sentences.
 *
 * The problem is structural, not a typo. A newline in the source is whitespace
 * in HTML, and in Latin text that is harmless because words are space-separated
 * anyway. Chinese has no inter-word spaces, so **every place a source line
 * wrapped becomes a visible gap in the middle of a sentence** — and the gaps
 * land wherever the author happened to hit 100 columns, which is why the
 * alignment looked different in every paragraph on the page. Measured before
 * this file existed: 248 of them, in every chapter, worst in 測站統計 (77) and
 * 方法學對照 (60).
 *
 * Doing it here rather than at 248 call sites is the point. The alternative is
 * writing every Chinese paragraph on one unwrapped source line and remembering
 * to keep doing it forever; this needs remembering nothing, and a new paragraph
 * written normally comes out right.
 *
 * The rule is deliberately the narrowest one that fixes it: whitespace is
 * removed **only between two CJK characters**. A space between Chinese and
 * Latin or a digit is good typography and stays — 「第 1 版」, 「8 年月平均」,
 * 「N = 3.41 億」 are all untouched. So is U+3000, which is a real character
 * someone might have typed on purpose rather than a wrap artefact.
 *
 * `<script>`, `<style>`, `<pre>`, `<code>` and `<textarea>` are skipped
 * wholesale. The first matters most: chapter 3 ships its CBPF data as an inline
 * JSON payload, and rewriting bytes inside it would corrupt data to fix
 * typography.
 */
import type { MiddlewareHandler } from "astro";

/**
 * Han, kana, CJK punctuation and fullwidth forms — but not U+3000 (ideographic
 * space), which is whitespace and must not be treated as a character to close
 * up against.
 */
const CJK = "\\u2E80-\\u2FFF\\u3001-\\u303F\\u3041-\\u33FF\\u3400-\\u4DBF\\u4E00-\\u9FFF\\uF900-\\uFAFF\\uFE30-\\uFE4F\\uFF01-\\uFF60\\uFFE0-\\uFFE6";

/** Regions whose bytes are data or code, never prose. */
const OPAQUE = /<(script|style|pre|code|textarea)\b[^>]*>[\s\S]*?<\/\1>/gi;

/** A run of ASCII whitespace with a CJK character on both sides. */
const WRAPPED = new RegExp(`([${CJK}])[ \\t\\r\\n]+(?=[${CJK}])`, "g");

export function closeCjkGaps(html: string): string {
  let out = "";
  let cursor = 0;

  OPAQUE.lastIndex = 0;
  for (let block = OPAQUE.exec(html); block !== null; block = OPAQUE.exec(html)) {
    out += html.slice(cursor, block.index).replace(WRAPPED, "$1");
    out += block[0];
    cursor = block.index + block[0].length;
  }

  return out + html.slice(cursor).replace(WRAPPED, "$1");
}

export const onRequest: MiddlewareHandler = async (_context, next) => {
  const response = await next();
  if (!response.headers.get("content-type")?.includes("text/html")) return response;

  const html = await response.text();
  return new Response(closeCjkGaps(html), {
    status: response.status,
    statusText: response.statusText,
    headers: response.headers,
  });
};
