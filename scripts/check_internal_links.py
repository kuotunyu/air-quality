"""Every link this repository makes to its own files — do the targets exist?

Two surfaces, one question:

**Markdown.** `docs/methodology.md` shipped seventeen broken links. Every one
written as `[src/twair/analysis/pitfalls.py](src/twair/analysis/pitfalls.py)`
resolves relative to `docs/`, so on GitHub they pointed at `docs/src/twair/...`
— all 404. The same file also held four correct `../` links, so it had been
half right for a long time with nothing able to notice. Nothing about a broken
one looks wrong in the source; the only way to find it is to click.

**The website.** `web/src/lib/repo.ts` builds GitHub blob URLs from
`repoFile("path")`, and the site links to `LICENSE`, `LICENSE-DATA` and
`docs/legal.md` that way. Those paths are strings: moving `docs/legal.md` during
a documentation tidy-up would 404 the live site with nothing to catch it, and the
Astro build cannot help because a wrong URL is still a valid string.

External URLs are never fetched. This has to run offline in CI, and an upstream
outage must not turn the build red.

    uv run python scripts/check_internal_links.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote

REPO_ROOT = Path(__file__).resolve().parents[1]

# `data/` is gitignored bulk; `web/dist` and `node_modules` are build output;
# `.superpowers/` and `docs/superpowers/` are ignored internal notes that
# deliberately reference files which may not exist yet.
SKIP_PARTS = {
    ".git",
    ".superpowers",
    ".venv",
    ".worktrees",
    "data",
    "dist",
    "node_modules",
    "superpowers",
}

LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
EXTERNAL = ("http://", "https://", "mailto:", "#")

WEB_SRC = REPO_ROOT / "web" / "src"
REPO_FILE = re.compile(r"""repoFile\(\s*["']([^"']+)["']""")


def markdown_files() -> list[Path]:
    return [
        path
        for path in sorted(REPO_ROOT.rglob("*.md"))
        if not any(part in SKIP_PARTS for part in path.relative_to(REPO_ROOT).parts)
    ]


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

    files = markdown_files()

    checked = 0
    broken: list[str] = []
    for path in files:
        relative = path.relative_to(REPO_ROOT).as_posix()
        for match in LINK.finditer(path.read_text(encoding="utf-8", errors="replace")):
            target = match.group(1)
            if target.startswith(EXTERNAL):
                continue
            # A trailing #anchor is a position inside the file, not part of it.
            clean = unquote(target.split("#", 1)[0])
            if not clean:
                continue
            checked += 1
            if not (path.parent / clean).resolve().exists():
                broken.append(f"{relative}  ->  {target}")

    site_links = 0
    for path in sorted(WEB_SRC.rglob("*")) if WEB_SRC.exists() else []:
        if path.suffix not in {".astro", ".ts", ".tsx", ".js"} or not path.is_file():
            continue
        relative = path.relative_to(REPO_ROOT).as_posix()
        for match in REPO_FILE.finditer(path.read_text(encoding="utf-8", errors="replace")):
            target = match.group(1).lstrip("/")
            # `repo.ts` itself contains the template, not a call with a path.
            if not target or "${" in target:
                continue
            site_links += 1
            if not (REPO_ROOT / target).exists():
                broken.append(f"{relative}  ->  repoFile({target!r})")

    # Finding nothing is not the same as finding nothing wrong. If SKIP_PARTS
    # ever excluded the whole tree, or `repoFile` were renamed, this would print
    # three zeroes and exit 0 — the defect `check_like_ci.py` shipped with, in a
    # file written an hour after that one was fixed and the rule written down.
    if not files:
        raise SystemExit("no markdown files found — refusing to report success for scanning none")
    if not checked:
        raise SystemExit(f"{len(files)} markdown files but no internal links — has the tree moved?")
    if not site_links:
        raise SystemExit("no repoFile() calls found in web/src — was the helper renamed?")

    print(f"markdown files   : {len(files)}")
    print(f"markdown links   : {checked}")
    print(f"site repoFile()  : {site_links}")
    print(f"broken           : {len(broken)}")
    for entry in broken:
        print(f"  {entry}")
    return 1 if broken else 0


if __name__ == "__main__":
    raise SystemExit(main())
