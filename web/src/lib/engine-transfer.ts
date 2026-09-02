/**
 * What the reader actually downloads when they press 執行查詢, in megabytes.
 *
 * One number, read wherever the page states the cost, because the page used to
 * type it. `duck.ts` measured it — 35.9 MB of WebAssembly that Pages serves as
 * 8.1 MB gzipped, plus a 0.2 MB worker — and recorded in its own header why the
 * transfer figure rather than the uncompressed one is the honest quote: the
 * page had promised 「約 30 MB」, which overstates the cost 3.6× and makes a
 * reader decline something cheaper than advertised.
 *
 * That correction left the measured figure in a comment and a rounded copy of
 * it in the prose, which is the arrangement every other number on this site is
 * not allowed to have. It is here instead.
 */
export const ENGINE_TRANSFER_MB = 8.1;
