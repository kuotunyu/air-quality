"""Run what CI runs, in CI's order, from CI's own file.

`AGENTS.md` used to list the gates to run before calling a task done. It listed
six; CI runs seventeen. An agent following it could finish every check it named
and still turn the build red — which happened twice on 2026-08-18, once on a
missing Astro type and once on a prose gate nobody had thought to run locally.

Copying the list into `AGENTS.md` would only move the problem: two lists, one of
them quietly behind. So this reads `.github/workflows/ci.yml` and executes the
`run:` steps it finds. There is exactly one list of gates in this repository and
it is the workflow.

    uv run python scripts/check_like_ci.py            # everything
    uv run python scripts/check_like_ci.py --job test # the Python half only
    uv run python scripts/check_like_ci.py --list     # show, do not run

**Three differences from CI, each deliberate:**

*Environment setup is skipped* — installing Python, `uv sync`, `npm ci`. A local
checkout already has those, and re-running them mid-session has locked files on
Windows before. Skipped steps are listed at the end, so nothing is silently
dropped.

*`${{ github… }}` becomes `HEAD`.* CI audits the proposed head revision; locally
that is HEAD.

*It does not stop at the first failure.* CI does, because a runner's time is
worth more than the extra signal. Locally the extra signal is worth more than the
minutes: knowing that three gates fail rather than one changes what you do next.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import time
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

# Setup, not verification. Matched on the step name because that is what the
# workflow calls them; a renamed setup step shows up as an unexpected run and is
# better seen than silently skipped.
SETUP_STEPS = {
    "Set up Python",
    "Sync Dependencies",
    "Install",
}

_GITHUB_SHA = re.compile(r"\$\{\{[^}]*\}\}")


def steps(job: dict[str, Any]) -> list[tuple[str, str, str | None]]:
    """(name, command, working-directory) for every `run:` step in a job."""
    out = []
    for step in job.get("steps", []):
        run = step.get("run")
        if not run:
            continue  # a `uses:` action — checkout, setup-uv, setup-node
        out.append((str(step.get("name", "(unnamed)")), str(run), step.get("working-directory")))
    return out


def local(command: str) -> str:
    """CI audits the proposed head revision; locally that is HEAD."""
    return _GITHUB_SHA.sub("HEAD", command)


def run_one(name: str, command: str, cwd: str | None) -> tuple[bool, float]:
    where = REPO_ROOT / cwd if cwd else REPO_ROOT
    started = time.monotonic()
    completed = subprocess.run(command, shell=True, cwd=where, check=False)
    return completed.returncode == 0, time.monotonic() - started


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", help="run only this job key (test, web)")
    parser.add_argument("--list", action="store_true", help="print the steps and exit")
    args = parser.parse_args()

    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    jobs: dict[str, Any] = workflow["jobs"]
    if args.job:
        if args.job not in jobs:
            raise SystemExit(f"no job {args.job!r}; workflow has {sorted(jobs)}")
        jobs = {args.job: jobs[args.job]}

    planned: list[tuple[str, str, str, str | None]] = []
    skipped: list[str] = []
    for key, job in jobs.items():
        for name, command, cwd in steps(job):
            if name in SETUP_STEPS:
                skipped.append(f"{key}: {name}")
                continue
            planned.append((key, name, local(command), cwd))

    if args.list:
        for key, name, command, cwd in planned:
            location = f" (in {cwd})" if cwd else ""
            print(f"[{key}] {name}{location}\n    {command.strip()}")
        print(f"\nskipped as environment setup: {len(skipped)}")
        for entry in skipped:
            print(f"  {entry}")
        return 0

    failures: list[str] = []
    for index, (key, name, command, cwd) in enumerate(planned, start=1):
        print(f"\n=== [{index}/{len(planned)}] {key}: {name} ===", flush=True)
        ok, seconds = run_one(name, command, cwd)
        print(f"--- {'ok' if ok else 'FAILED'} in {seconds:.1f}s", flush=True)
        if not ok:
            failures.append(f"{key}: {name}")

    print(f"\n{'=' * 60}")
    print(f"ran {len(planned)} step(s); {len(failures)} failed")
    for entry in skipped:
        print(f"  skipped (setup): {entry}")
    for entry in failures:
        print(f"  FAILED: {entry}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
