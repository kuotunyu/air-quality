/**
 * Verify the exact data subset promised and shipped by GitHub Pages.
 *
 * The export manifest describes everything the local pipeline generated. That
 * is deliberately broader than the Pages distribution decision, which lives
 * in web/src/data/pages-publication.json. This gate joins those two identities
 * to the tracked public tree and the built dist tree.
 *
 * 2026-09-02 — it also joined them to the download links readers saw: every
 * registered member had to be linked from a built page and no page could link
 * outside the register. Chapter 10's download table is gone (the owner's
 * decision; a reader gets data through chapter 9's in-browser query), so the
 * link half of the join is gone with it. The register still says what Pages
 * serves, because chapter 9 and the charts read those files.
 *
 *     node scripts/check_pages_publication.mjs
 *     node scripts/check_pages_publication.mjs --register path --public path --dist path
 */

import { execFileSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
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

  return problems;
}

function registeredCount(registerPath) {
  const register = json(registerPath);
  return LAYERS.reduce((total, layer) => total + register[layer].length, 0);
}

let paths;
try {
  paths = argumentsOf(process.argv.slice(2));
} catch (error) {
  console.error(error instanceof Error ? error.message : String(error));
  process.exit(2);
}

const problems = inspect(paths);
console.log(`Pages members registered : ${problems.length ? "invalid" : registeredCount(paths.register)}`);
console.log(`Pages publication problems : ${problems.length}`);
for (const problem of problems) console.log(`  FAIL: ${problem}`);
process.exit(problems.length ? 1 : 0);
