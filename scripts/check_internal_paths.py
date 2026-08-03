from __future__ import annotations

import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from twair.paths import REPO_ROOT

BLOCKED_EXACT_PATHS = frozenset({"HANDOFF.md", "PROGRESS.md"})
BLOCKED_PATH_PREFIXES = (".superpowers/", "docs/superpowers/")


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        check=False,
        timeout=30,
    )
    if result.returncode:
        raise RuntimeError(f"git {' '.join(args[:2])} failed with exit code {result.returncode}")
    return result.stdout.decode("utf-8", errors="strict")


def _resolved_repository(repo: Path) -> Path:
    requested = repo.resolve(strict=True)
    root = _git(requested, "rev-parse", "--show-toplevel").strip()
    if not root:
        raise RuntimeError("git rev-parse returned an empty repository path")
    return Path(root).resolve(strict=True)


def _resolved_revision(repo: Path, revision: str) -> str:
    resolved = _git(repo, "rev-parse", "--verify", f"{revision}^{{commit}}").strip()
    if not resolved:
        raise RuntimeError("git rev-parse returned an empty revision")
    return resolved


def internal_path_violations(repo: Path, revision: str = "HEAD") -> list[str]:
    repository = _resolved_repository(repo)
    resolved_revision = _resolved_revision(repository, revision)
    inventory = _git(repository, "rev-list", "--objects", resolved_revision)
    violations: set[str] = set()
    for record in inventory.splitlines():
        fields = record.split(" ", 1)
        if len(fields) != 2:
            continue
        path = fields[1].replace("\\", "/")
        if path in BLOCKED_EXACT_PATHS or path.startswith(BLOCKED_PATH_PREFIXES):
            violations.add(path)

    # Object IDs are content-addressed, so rev-list keeps only one path hint
    # when a blob has aliases; commit diffs preserve every historical path.
    path_history = _git(
        repository,
        "log",
        "--format=",
        "--name-only",
        "--root",
        "-m",
        "--no-renames",
        "-z",
        resolved_revision,
    )
    if path_history and not path_history.endswith("\0"):
        raise RuntimeError("git log returned a malformed path inventory")
    for raw_path in path_history.split("\0"):
        if not raw_path:
            continue
        path = raw_path.replace("\\", "/")
        if path in BLOCKED_EXACT_PATHS or path.startswith(BLOCKED_PATH_PREFIXES):
            violations.add(path)
    return sorted(violations)


def main(argv: Sequence[str] = ()) -> int:
    args = list(argv)
    if len(args) > 1:
        print("internal path check accepts at most one revision", file=sys.stderr)
        return 2
    revision = args[0] if args else "HEAD"
    try:
        violations = internal_path_violations(REPO_ROOT, revision)
    except (OSError, RuntimeError, subprocess.SubprocessError, UnicodeError) as exc:
        print(f"internal path check could not run: {exc}", file=sys.stderr)
        return 2
    if violations:
        for path in violations:
            print(path, file=sys.stderr)
        return 1
    print(f"internal path check passed for {revision}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
