"""The local CI runner must not report success for having run nothing.

`check_like_ci.py` reads `.github/workflows/ci.yml` and executes the `run:` steps
it finds. If the workflow ever changes shape — a restructure, a different key, a
job template — the extraction could quietly yield nothing, print
「ran 0 step(s); 0 failed」 and exit 0. A green result from doing no work is the
same defect the prose gates shipped with, and the contributor instructions point
at this script, so the result would be believed.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import yaml
from scripts import check_like_ci as runner

from twair.paths import REPO_ROOT


def test_the_real_workflow_still_parses_into_steps() -> None:
    """If this breaks, the extraction has stopped understanding the workflow."""
    workflow: dict[str, Any] = yaml.safe_load(runner.WORKFLOW.read_text(encoding="utf-8"))

    for key, job in workflow["jobs"].items():
        found = runner.steps(job)
        assert found, f"job {key} yielded no run steps"
        assert all(isinstance(name, str) and command for name, command, _ in found)


def test_tracked_short_rules_have_one_canonical_source() -> None:
    workflow = runner.WORKFLOW.read_text(encoding="utf-8")
    instructions = (REPO_ROOT / ".github" / "copilot-instructions.md").read_text(encoding="utf-8")
    retired_script = "mirror_" + "agents.py"

    assert not (REPO_ROOT / "scripts" / retired_script).exists()
    assert retired_script not in workflow
    assert "MIRROR OF" not in instructions
    assert "sole tracked short-rule source" in instructions


def test_every_setup_step_named_for_skipping_exists_in_the_workflow() -> None:
    """A renamed setup step would otherwise be run as if it were a gate — or,
    worse, a stale name here would silently protect nothing."""
    workflow: dict[str, Any] = yaml.safe_load(runner.WORKFLOW.read_text(encoding="utf-8"))
    names = {name for job in workflow["jobs"].values() for name, _, _ in runner.steps(job)}

    assert names >= runner.SETUP_STEPS, f"stale skip names: {runner.SETUP_STEPS - names}"


def test_the_github_sha_expression_becomes_head() -> None:
    command = 'uv run python scripts/x.py "${{ github.event.pull_request.head.sha || github.sha }}"'

    assert runner.local(command) == 'uv run python scripts/x.py "HEAD"'


def test_a_job_without_run_steps_is_refused(tmp_path: Path) -> None:
    """The failure this file exists for: a workflow the extraction cannot read
    must not produce a green run."""
    workflow = tmp_path / "ci.yml"
    workflow.write_text(
        yaml.safe_dump(
            {"jobs": {"test": {"steps": [{"name": "Checkout", "uses": "actions/checkout@v4"}]}}}
        ),
        encoding="utf-8",
    )

    result = _run("--list", "--workflow", str(workflow))

    assert result.returncode != 0, result.stdout
    assert "no `run:` steps" in (result.stdout + result.stderr)


def test_listing_the_real_workflow_succeeds() -> None:
    result = _run("--list")

    assert result.returncode == 0, result.stderr
    assert "skipped as environment setup" in result.stdout


def test_a_deployment_job_is_skipped_not_run(tmp_path: Path) -> None:
    """A job with an `environment` publishes what the gates verified. The local
    runner must neither run its steps nor count them as gates, and must say so."""
    workflow = tmp_path / "ci.yml"
    workflow.write_text(
        yaml.safe_dump(
            {
                "jobs": {
                    "test": {"steps": [{"name": "Gate", "run": "true"}]},
                    "deploy": {
                        "environment": {"name": "github-pages"},
                        "steps": [{"name": "Build", "run": "echo build"}],
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    result = _run("--list", "--workflow", str(workflow))

    assert result.returncode == 0, result.stdout + result.stderr
    assert "[test] Gate" in result.stdout
    assert "[deploy]" not in result.stdout
    assert "skipped (deployment): deploy" in result.stdout


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["uv", "run", "python", str(REPO_ROOT / "scripts" / "check_like_ci.py"), *args],
        cwd=REPO_ROOT,
        capture_output=True,
        # Both ends pinned: the child prints step names containing CJK and this
        # repository's path does too. See docs/working-rules.md.
        encoding="utf-8",
        errors="replace",
        text=True,
        check=False,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        timeout=300,
    )
