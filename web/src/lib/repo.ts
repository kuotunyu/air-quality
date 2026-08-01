/**
 * Where this site's source lives, and the two links that need it.
 *
 * The footer has always said 「管線與分析全部開源」 and printed the git sha the
 * data layer was exported at — and nothing on any of the twelve pages linked to
 * the repository. A reader was shown a commit hash and told the work was open,
 * with no way to reach it. `git grep github.com -- web/src` returned nothing.
 *
 * From the environment with a default, for the same reason `site` and `base` are
 * in `astro.config.mjs`: a fork should build without editing a file. CI passes
 * `github.server_url`/`github.repository`, so a fork's own build links to the
 * fork. The default is only what a local preview shows.
 */
const RAW = import.meta.env.PUBLIC_REPO_URL ?? "https://github.com/kuotunyu/air-quality";

/** No trailing slash, so the helpers below can join without doubling it. */
export const repoUrl: string = RAW.replace(/\/+$/, "");

/**
 * A file on the default branch, or at a commit when a caller has a reason.
 *
 * The default is HEAD, and that is a decision rather than laziness. Pinning the
 * licence links to `meta.git_sha` was the first version, on the argument that a
 * licence link beside a data layer should show the terms as they stood when
 * that layer was exported. It is the wrong argument twice over: a reader who
 * clicks 「CC BY 4.0」 wants the licence in force, not a snapshot of it; and
 * `meta.git_sha` is not reliably the commit the payload came from — the export
 * stamps the sha of the tree it ran in, so a payload generated from a dirty
 * working tree carries the sha of the commit BEFORE the change that produced
 * it. `web/public/data/` currently records a0d4260 for payloads whose content
 * only exists from 309f74a onward.
 */
export function repoFile(path: string, sha: string | null = null): string {
  return `${repoUrl}/blob/${sha ?? "HEAD"}/${path.replace(/^\/+/, "")}`;
}

/**
 * The commit the data layer says it was exported at. The footer already prints
 * it, so linking it costs nothing and makes the claim checkable — including
 * when it is wrong, which is the case worth being able to see.
 */
export function repoCommit(sha: string): string {
  return `${repoUrl}/commit/${sha}`;
}
