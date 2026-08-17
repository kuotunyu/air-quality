"""Do this repository's own markdown links point at files that exist?

`docs/methodology.md` shipped seventeen broken ones. Every link written as
`[src/twair/analysis/pitfalls.py](src/twair/analysis/pitfalls.py)` resolves
relative to `docs/`, so on GitHub they were links to `docs/src/twair/...` — all
404. The same file also held four correct `../` links, added later, so it had
been inconsistent for a long time with nothing able to notice.

Only repository-relative links are checked. External URLs are not fetched: this
must run offline, in CI, without turning an upstream outage into a red build.

    uv run python scripts/check_markdown_links.py
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


def markdown_files() -> list[Path]:
    return [
        path
        for path in sorted(REPO_ROOT.rglob("*.md"))
        if not any(part in SKIP_PARTS for part in path.relative_to(REPO_ROOT).parts)
    ]


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

    checked = 0
    broken: list[str] = []
    for path in markdown_files():
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

    print(f"markdown files   : {len(markdown_files())}")
    print(f"internal links   : {checked}")
    print(f"broken           : {len(broken)}")
    for entry in broken:
        print(f"  {entry}")
    return 1 if broken else 0


if __name__ == "__main__":
    raise SystemExit(main())
