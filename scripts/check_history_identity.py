"""Reject repository history that would add another contributor identity."""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from twair.config import load_conf
from twair.paths import REPO_ROOT

COAUTHOR_RE = re.compile(r"^[ \t]*co-authored-by\s*:", re.MULTILINE | re.IGNORECASE)


@dataclass(frozen=True)
class CommitIdentity:
    sha: str
    author_name: str
    author_email: str
    committer_name: str
    committer_email: str
    body: str


def parse_history(raw: bytes) -> list[CommitIdentity]:
    """Parse six NUL-separated fields per commit without flattening its body."""
    if not raw:
        raise ValueError("git history is empty")
    fields = raw.split(b"\x00")
    if fields[-1] != b"":
        raise ValueError("git history is missing its NUL terminator")
    fields.pop()
    if len(fields) % 6:
        raise ValueError("git history must contain complete six fields records")
    decoded = [field.decode("utf-8", errors="strict") for field in fields]
    return [
        CommitIdentity(
            sha=decoded[index],
            author_name=decoded[index + 1],
            author_email=decoded[index + 2],
            committer_name=decoded[index + 3],
            committer_email=decoded[index + 4],
            body=decoded[index + 5],
        )
        for index in range(0, len(decoded), 6)
    ]


def find_violations(
    commits: Sequence[CommitIdentity], expected_name: str, expected_email: str
) -> list[str]:
    """Name each offending commit and field without echoing an unexpected identity."""
    violations: list[str] = []
    expected = {
        "author_name": expected_name,
        "author_email": expected_email,
        "committer_name": expected_name,
        "committer_email": expected_email,
    }
    for commit in commits:
        for field, wanted in expected.items():
            if getattr(commit, field) != wanted:
                violations.append(f"{commit.sha[:8]} {field}: does not match the allowed identity")
        if COAUTHOR_RE.search(commit.body):
            violations.append(f"{commit.sha[:8]} body: forbidden co-author trailer")
    return violations


def read_history(revision: str = "HEAD") -> list[CommitIdentity]:
    """Read every commit reachable from a revision in one git process."""
    result = subprocess.run(
        [
            "git",
            "log",
            "-z",
            "--encoding=UTF-8",
            "--format=%H%x00%an%x00%ae%x00%cn%x00%ce%x00%B",
            revision,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        # Git's diagnostic can contain the unexpected identity or local path;
        # the exit code is enough for this gate and safer for a public CI log.
        raise RuntimeError(f"git log failed with exit code {result.returncode}")
    return parse_history(result.stdout)


def _expected_identity(config: dict[str, Any]) -> tuple[str, str]:
    try:
        identity = config["history"]["allowed_identity"]
    except (KeyError, TypeError) as exc:
        raise ValueError("project config must define history.allowed_identity") from exc
    if not isinstance(identity, dict):
        raise ValueError("history.allowed_identity must be a mapping")
    name = identity.get("name")
    email = identity.get("email")
    if not isinstance(name, str) or not name or not isinstance(email, str) or not email:
        raise ValueError("the allowed identity must have a non-empty name and email")
    return name, email


def main() -> int:
    try:
        expected_name, expected_email = _expected_identity(load_conf("project"))
        commits = read_history()
    except (
        OSError,
        RuntimeError,
        subprocess.SubprocessError,
        TypeError,
        UnicodeError,
        ValueError,
    ) as exc:
        print(
            f"history identity check could not run ({type(exc).__name__})",
            file=sys.stderr,
        )
        return 2

    violations = find_violations(commits, expected_name, expected_email)
    if violations:
        for violation in violations:
            print(violation, file=sys.stderr)
        return 1

    print(f"history identity check passed: {len(commits)} commits reachable from HEAD")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
