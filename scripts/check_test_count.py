"""Require pytest's full collection to match the reviewed inventory."""

from __future__ import annotations

import re
import subprocess
import sys
from typing import Any

from twair.config import load_conf
from twair.paths import REPO_ROOT

COLLECTION_RE = re.compile(r"(?m)^(?:\d+/)?(?P<total>\d+) tests collected")


def parse_collected_total(output: str) -> int:
    matches = list(COLLECTION_RE.finditer(output))
    if len(matches) != 1:
        raise ValueError("pytest output must contain one collection summary")
    return int(matches[0].group("total"))


def mismatch(actual: int, expected: int) -> str | None:
    if actual == expected:
        return None
    return (
        f"collected {actual} tests, but conf/project.yaml records {expected}; "
        "review the inventory and update both in the same commit"
    )


def collect_total() -> int:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-o",
            "addopts=",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"pytest collection failed with exit code {result.returncode}")
    return parse_collected_total(f"{result.stdout}\n{result.stderr}")


def _expected_total(config: dict[str, Any]) -> int:
    try:
        expected = config["quality_gates"]["expected_collected_tests"]
    except (KeyError, TypeError) as exc:
        raise ValueError(
            "project config must define quality_gates.expected_collected_tests"
        ) from exc
    if isinstance(expected, bool) or not isinstance(expected, int) or expected <= 0:
        raise ValueError("expected_collected_tests must be a positive integer")
    return expected


def main() -> int:
    try:
        expected = _expected_total(load_conf("project"))
        actual = collect_total()
    except (
        OSError,
        RuntimeError,
        subprocess.SubprocessError,
        TypeError,
        UnicodeError,
        ValueError,
    ) as exc:
        print(
            f"test inventory check could not run ({type(exc).__name__})",
            file=sys.stderr,
        )
        return 2

    problem = mismatch(actual, expected)
    if problem is not None:
        print(problem, file=sys.stderr)
        return 1

    print(f"test inventory check passed: expected {expected}, actual {actual}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
