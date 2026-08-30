/**
 * The document's table of contents, and the only place its order lives.
 *
 * This exists because the site stopped being one page. Ten chapters in one
 * scroll measured 38,291px — 42 screens at a laptop height — and the top nav's
 * eleven anchors were the only way through it. Long-form does not mean
 * unbounded: at that length the scrollbar thumb is four pixels tall, "where am
 * I" has no answer, and a reviewer who wants to judge the work in thirty
 * seconds has nothing to judge it from but the first fold.
 *
 * So each chapter is a route, and this array is what the nav, the entry page's
 * index, and every chapter's prev/next footer all read from. One array means
 * the three can never disagree about the order or the numbering — which they
 * would, because the numbering has already been renumbered once by hand across
 * eight files when a chapter was inserted in the middle.
 *
 * `claim` is deliberately not a number. A figure typed into a navigation file
 * is a figure with no test behind it, and this repository has a rule about
 * that: it would be correct on the day it was written and quietly wrong after
 * the next pipeline run. So each line says what the chapter settles, and the
 * chapter itself carries the measurement.
 */

export interface Chapter {
  /** Position in the document. Rendered as 「第 n 章」, so it is the numbering. */
  n: number;
  /** URL segment. Also the `id` of the chapter's own section, so in-page
   *  anchors from other chapters keep working as `/slug#id`. */
  slug: string;
  /** Short enough for the top nav on a phone. */
  nav: string;
  /** The chapter's heading, repeated here for the index and for `<title>`. */
  title: string;
  question: string;
  /** One sentence: what a reader gets by opening it. */
  claim: string;
}

export const CHAPTERS: readonly Chapter[] = [
  {
    n: 1,
    slug: "trend",
    nav: "趨勢",
    title: "長期趨勢與氣象校正",
    question: "監測網擴張與天氣條件扣除後，長期下降還成立嗎？",
    claim: "全台 PM2.5 確實在降；固定測站並調整站內氣象後，下降仍存在，但剩餘趨勢不能直接等同排放變化。",
  },
  {
    n: 2,
    slug: "stations",
    nav: "測站統計",
    title: "測站個別統計",
    question: "選定測站最近哪一年有足夠完整、可比較的資料？",
    claim: "選定測站後，顯示該站最近一個資料完整到可以比較的年份，以及它在全國的位置。",
  },
  {
    n: 3,
    slug: "space",
    nav: "空間結構",
    title: "空間結構與官方分區",
    question: "官方分區是否足以處理測站殘差的空間相依？",
    claim: "按空品區分層其實移除了大部分的空間相依——但合併式模型的 t 值高估了顯著性。",
  },
  {
    n: 4,
    slug: "sources",
    nav: "污染來向",
    /*
     * 污染來向與風速條件, matching the h1 — and this entry was the stale one.
     *
     * The rename 「壞空氣是從哪個方向來的」 → 「污染來向與風速條件」 is recorded in
     * `docs/working-rules.md`; the component took it and this file did
     * not, so the registry sat on a THIRD name that was neither. It named half
     * the method: the chart is a CBPF, conditioned on wind speed as well as
     * direction, and 風速條件 is the half that makes the finding a finding.
     *
     * Nine characters, which is exactly what the 206px rail label holds on one
     * line — see the note on the mismatch gate in `chapterTitleMismatches`.
     */
    title: "污染來向與風速條件",
    question: "高濃度空氣通常從什麼方位、在什麼風速下抵達？",
    claim: "把濃度按風速與風向拆開，即可看出高濃度集中在哪幾個方位。",
  },
  {
    n: 5,
    slug: "detection",
    nav: "偵測極限",
    /*
     * 事件效應, matching the h1 — not 政策效應.
     *
     * The rail printed 政策效應的偵測極限 while the h1 sixty pixels to its right
     * read 事件效應的偵測極限, so two names for the chapter were visible at once.
     * The h1's is the one to keep: the drafting note at the top of the component
     * records that the subject is the METHOD and not the government, and 事件 is
     * what the chapter actually tests — a law amendment, a permit dispute and a
     * lockdown.
     */
    title: "事件效應的偵測極限",
    question: "現有方法能分辨多小的事件效應？",
    claim: "一個方法能看見多小的效應，是可以先量出來的——量測之後才能分辨哪些「沒有顯著」其實是偵測不到。",
  },
  {
    n: 6,
    slug: "forecast",
    nav: "預測技巧",
    title: "預測技巧與有效期距",
    question: "預測往前走多久後不再比簡單基準有用？",
    claim: "預報到多長的期距仍然有用，以及為什麼一小時後最該有用的模型會不如一行規則。",
  },
  {
    n: 7,
    slug: "health",
    nav: "健康負擔",
    title: "健康負擔與它的假設",
    question: "暴露反應函數與比較基準會把可歸因比例推動多少？",
    claim: "可歸因比例算得出來，但它取決於一個幾乎從未被明說的選擇：以什麼作為比較基準。",
  },
  {
    n: 8,
    slug: "methods",
    nav: "方法學對照",
    title: "方法選擇的量化代價",
    question: "每一個方法選擇各自付出了什麼可量化代價？",
    claim: "同一份資料做兩次，一次用常見但有缺陷的做法，一次用修正後的——逐項量出每個選擇的代價。",
  },
  {
    n: 9,
    slug: "explore",
    nav: "資料查詢",
    title: "資料查詢",
    question: "如何直接查詢公開 Parquet，而不經過伺服器？",
    claim: "以 SQL 直接查詢 Parquet 檔，不經過任何伺服器。",
  },
  {
    n: 10,
    slug: "data",
    nav: "資料下載",
    title: "資料下載與方法",
    question: "哪些資料可下載、如何重建，以及缺值為何保留？",
    claim: "三層資料的內容、授權，以及這個專案為什麼不補值。",
  },
] as const;

export interface ChapterGroup {
  label: string;
  chapters: readonly Chapter[];
}

export const CHAPTER_GROUPS: readonly ChapterGroup[] = [
  { label: "發生了什麼", chapters: CHAPTERS.slice(0, 4) },
  { label: "我們能知道多少", chapters: CHAPTERS.slice(4, 7) },
  { label: "如何查驗", chapters: CHAPTERS.slice(7, 10) },
] as const;

/** 「第一章」 rather than 「第 1 章」: the numeral is Chinese in running text. */
const NUMERALS = ["零", "一", "二", "三", "四", "五", "六", "七", "八", "九", "十"] as const;

export function chapterLabel(n: number): string {
  return `第${NUMERALS[n] ?? n}章`;
}

/** The numeral alone, for the rail — where 「第一章」 plus a title will not fit. */
export function chapterNumeral(n: number): string {
  return NUMERALS[n] ?? String(n);
}

export function chapterBySlug(slug: string): Chapter {
  const found = CHAPTERS.find((c) => c.slug === slug);
  if (!found) throw new Error(`unknown chapter slug: ${slug}`);
  return found;
}

/** The chapters either side, for the footer. `null` at the two ends. */
export function neighbours(slug: string): { prev: Chapter | null; next: Chapter | null } {
  const at = CHAPTERS.findIndex((c) => c.slug === slug);
  if (at === -1) throw new Error(`unknown chapter slug: ${slug}`);
  return { prev: CHAPTERS[at - 1] ?? null, next: CHAPTERS[at + 1] ?? null };
}

/**
 * A site-root-relative href.
 *
 * GitHub Pages serves this under `/<repo>/`, so a bare `/trend` would 404 there
 * while working locally — the failure mode that only appears in production.
 * `BASE_URL` carries the prefix Astro was configured with.
 */
export function href(path = ""): string {
  const base = import.meta.env.BASE_URL.replace(/\/+$/, "");
  if (!path) return `${base}/`;
  const clean = path.replace(/^\/+/, "");
  /*
   * A route gets the trailing slash, because that is the spelling the host
   * actually serves it at.
   *
   * This returned `/air-quality/trend`, and GitHub Pages answers that with a 301
   * to `/air-quality/trend/`. Every internal link on the site therefore cost a
   * redirect round-trip before the page began loading, and every canonical URL,
   * `og:url` and sitemap `<loc>` named the spelling the host redirects away from
   * — which is the exact failure a canonical link exists to prevent. Measured on
   * the live site: 11 of 11 routes.
   *
   * A path with an extension is a file, not a route, so `sitemap.xml` keeps its
   * name.
   */
  return /\.[a-z0-9]+$/i.test(clean) ? `${base}/${clean}` : `${base}/${clean}/`;
}
