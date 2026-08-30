/**
 * Verify the exact data subset promised and shipped by GitHub Pages.
 *
 * The export manifest describes everything the local pipeline generated. That
 * is deliberately broader than the Pages distribution decision, which lives
 * in web/src/data/pages-publication.json. This gate joins those two identities
 * to the tracked public tree, the built dist tree, and the links readers see.
 *
 *     node scripts/check_pages_publication.mjs
 *     node scripts/check_pages_publication.mjs --register path --public path --dist path
 */

import { execFileSync } from "node:child_process";
import { existsSync, readFileSync, readdirSync } from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const DEFAULTS = {
  register: join(REPO_ROOT, "web", "src", "data", "pages-publication.json"),
  public: join(REPO_ROOT, "web", "public", "data"),
  dist: join(REPO_ROOT, "web", "dist"),
};
const REQUIRED_KEYS = ["l0", "l1", "l2", "metadata", "schema_version"];
const LAYERS = ["metadata", "l0", "l1", "l2"];
const DATA_HREF = /<a\b(?=[^>]*\bdownload\b)[^>]*\bhref=(?:"([^"]+)"|'([^']+)')[^>]*>/giu;

function argumentsOf(argv) {
  const values = { ...DEFAULTS };
  for (let index = 0; index < argv.length; index += 2) {
    const flag = argv[index];
    const value = argv[index + 1];
    if (!value || !["--register", "--public", "--dist"].includes(flag)) {
      throw new Error(`usage: check_pages_publication.mjs [--register path] [--public path] [--dist path]`);
    }
    values[flag.slice(2)] = resolve(value);
  }
  return values;
}

function json(path) {
  return JSON.parse(readFileSync(path, "utf8"));
}

function htmlFiles(root) {
  const found = [];
  const visit = (directory) => {
    for (const entry of readdirSync(directory, { withFileTypes: true })) {
      const path = join(directory, entry.name);
      if (entry.isDirectory()) visit(path);
      else if (entry.isFile() && entry.name.endsWith(".html")) found.push(path);
    }
  };
  if (existsSync(root)) visit(root);
  return found;
}

function dataMemberFromHref(href) {
  let decoded;
  try {
    decoded = decodeURIComponent(href).replaceAll("\\", "/");
  } catch {
    return null;
  }
  const marker = "/data/";
  const at = decoded.indexOf(marker);
  return at < 0 ? null : decoded.slice(at + marker.length).split(/[?#]/u, 1)[0];
}

function renderedDownloads(distRoot) {
  const found = new Set();
  for (const path of htmlFiles(distRoot)) {
    const html = readFileSync(path, "utf8");
    for (const match of html.matchAll(DATA_HREF)) {
      const member = dataMemberFromHref(match[1] ?? match[2]);
      if (member) found.add(member);
    }
  }
  return found;
}

function tracked(path) {
  try {
    execFileSync("git", ["ls-files", "--error-unmatch", "--", path], {
      cwd: REPO_ROOT,
      stdio: "ignore",
    });
    return true;
  } catch {
    return false;
  }
}

function inspect(paths) {
  const problems = [];
  if (!existsSync(paths.register)) return [`register is missing: ${paths.register}`];
  if (!existsSync(paths.public)) return [`public data root is missing: ${paths.public}`];
  if (!existsSync(paths.dist)) return [`dist root is missing: ${paths.dist}`];

  let register;
  let manifest;
  try {
    register = json(paths.register);
    manifest = json(join(paths.public, "manifest.json"));
  } catch (error) {
    return [`publication JSON is unreadable: ${error instanceof Error ? error.message : String(error)}`];
  }

  const keys = register && typeof register === "object" && !Array.isArray(register)
    ? Object.keys(register).sort()
    : [];
  if (JSON.stringify(keys) !== JSON.stringify(REQUIRED_KEYS)) {
    problems.push("register keys changed");
  }
  if (register?.schema_version !== 1) problems.push("register schema_version must be 1");

  for (const layer of LAYERS) {
    if (!Array.isArray(register?.[layer]) || register[layer].some((item) => typeof item !== "string" || !item)) {
      problems.push(`register ${layer} must be a non-empty-string array`);
    }
  }
  if (problems.length) return problems;

  const selected = LAYERS.flatMap((layer) => register[layer]);
  const selectedSet = new Set(selected);
  if (selectedSet.size !== selected.length) problems.push("register has duplicate members");
  for (const member of selected) {
    if (member.startsWith("/") || member.includes("..") || member.includes("\\")) {
      problems.push(`unsafe register member: ${member}`);
    }
  }

  const rows = Array.isArray(manifest?.files) ? manifest.files : [];
  const manifestFiles = new Set(
    rows.filter((row) => row && typeof row.file === "string").map((row) => row.file),
  );
  if (!Array.isArray(manifest?.files)) problems.push("manifest files inventory is invalid");

  const defaultTree =
    resolve(paths.register) === resolve(DEFAULTS.register) &&
    resolve(paths.public) === resolve(DEFAULTS.public);
  for (const member of selected) {
    if (!manifestFiles.has(member)) problems.push(`manifest missing selected member: ${member}`);
    if (!existsSync(join(paths.public, member))) problems.push(`public source missing: ${member}`);
    if (!existsSync(join(paths.dist, "data", member))) problems.push(`dist missing: ${member}`);
    if (defaultTree) {
      const repoPath = relative(REPO_ROOT, join(paths.public, member)).replaceAll("\\", "/");
      if (!tracked(repoPath)) problems.push(`selected source is not Git-tracked: ${member}`);
    }
  }

  const rendered = renderedDownloads(paths.dist);
  for (const member of rendered) {
    if (!selectedSet.has(member)) problems.push(`rendered download is outside register: ${member}`);
  }
  for (const member of selected) {
    if (!rendered.has(member)) problems.push(`registered member has no rendered download: ${member}`);
  }
  return problems;
}

let paths;
try {
  paths = argumentsOf(process.argv.slice(2));
} catch (error) {
  console.error(error instanceof Error ? error.message : String(error));
  process.exit(2);
}

const problems = inspect(paths);
console.log(`Pages members registered : ${problems.length ? "invalid" : renderedDownloads(paths.dist).size}`);
console.log(`Pages publication problems : ${problems.length}`);
for (const problem of problems) console.log(`  FAIL: ${problem}`);
process.exit(problems.length ? 1 : 0);
