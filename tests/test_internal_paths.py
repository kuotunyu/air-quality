from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from scripts.check_internal_paths import internal_path_violations

from twair.config import load_conf


def _run_git(repo: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        check=False,
        timeout=30,
    )
    if result.returncode:
        raise AssertionError(f"synthetic git setup failed with exit {result.returncode}")
    return result.stdout


def _owner_identity() -> tuple[str, str]:
    project: dict[str, Any] = load_conf("project")
    identity = project["history"]["allowed_identity"]
    return identity["name"], identity["email"]


def _commit(repo: Path, message: str) -> None:
    _run_git(repo, "add", "-A")
    _run_git(repo, "commit", "-q", "-m", message)


def _new_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _run_git(repo, "init", "-q")
    owner_name, owner_email = _owner_identity()
    _run_git(repo, "config", "user.name", owner_name)
    _run_git(repo, "config", "user.email", owner_email)
    (repo / "README.txt").write_text("safe seed\n", encoding="utf-8")
    _commit(repo, "safe seed")
    return repo


def test_a_top_level_project_memory_file_in_head_is_rejected(tmp_path: Path) -> None:
    repo = _new_repo(tmp_path)
    (repo / "HANDOFF.md").write_text("local handoff\n", encoding="utf-8")
    _commit(repo, "add project memory")

    assert internal_path_violations(repo) == ["HANDOFF.md"]


def test_a_deleted_project_memory_file_remains_rejected_through_history(
    tmp_path: Path,
) -> None:
    repo = _new_repo(tmp_path)
    (repo / "PROGRESS.md").write_text("local progress\n", encoding="utf-8")
    _commit(repo, "add project memory")
    _run_git(repo, "rm", "-q", "PROGRESS.md")
    _commit(repo, "remove project memory")

    assert internal_path_violations(repo) == ["PROGRESS.md"]


def test_an_internal_working_root_is_rejected_even_when_gitignore_would_hide_it(
    tmp_path: Path,
) -> None:
    repo = _new_repo(tmp_path)
    (repo / ".gitignore").write_text("/.superpowers/\n/docs/superpowers/\n", encoding="utf-8")
    private_plan = repo / ".superpowers" / "project-memory" / "notes.md"
    private_review = repo / "docs" / "superpowers" / "review.md"
    private_plan.parent.mkdir(parents=True)
    private_review.parent.mkdir(parents=True)
    private_plan.write_text("local notes\n", encoding="utf-8")
    private_review.write_text("local review\n", encoding="utf-8")
    _run_git(repo, "add", ".gitignore")
    _run_git(repo, "add", "-f", private_plan.relative_to(repo).as_posix())
    _run_git(repo, "add", "-f", private_review.relative_to(repo).as_posix())
    _commit(repo, "force-add ignored project memory")

    assert internal_path_violations(repo) == [
        ".superpowers/project-memory",
        ".superpowers/project-memory/notes.md",
        "docs/superpowers/review.md",
    ]


def test_a_blocked_alias_of_a_safe_blob_remains_rejected(tmp_path: Path) -> None:
    repo = _new_repo(tmp_path)
    shared_content = "the same blob under two paths\n"
    (repo / "AAA.txt").write_text(shared_content, encoding="utf-8")
    (repo / "HANDOFF.md").write_text(shared_content, encoding="utf-8")
    _commit(repo, "add safe and blocked aliases")

    object_inventory = _run_git(repo, "rev-list", "--objects", "HEAD").decode()
    assert " AAA.txt" in object_inventory
    assert " HANDOFF.md" not in object_inventory
    assert internal_path_violations(repo) == ["HANDOFF.md"]


def test_a_history_without_internal_paths_passes(tmp_path: Path) -> None:
    repo = _new_repo(tmp_path)

    assert internal_path_violations(repo) == []


def test_a_deleted_legacy_design_spec_remains_rejected_through_history(
    tmp_path: Path,
) -> None:
    repo = _new_repo(tmp_path)
    design = repo / "docs" / "design" / "approved-layout.md"
    design.parent.mkdir(parents=True)
    design.write_text("internal design\n", encoding="utf-8")
    _commit(repo, "add internal design")
    _run_git(repo, "rm", "-q", design.relative_to(repo).as_posix())
    _commit(repo, "remove internal design")

    assert internal_path_violations(repo) == ["docs/design/approved-layout.md"]


def test_a_force_added_legacy_implementation_plan_is_rejected(
    tmp_path: Path,
) -> None:
    repo = _new_repo(tmp_path)
    (repo / ".gitignore").write_text("/docs/plans/\n", encoding="utf-8")
    plan = repo / "docs" / "plans" / "implementation.md"
    plan.parent.mkdir(parents=True)
    plan.write_text("internal plan\n", encoding="utf-8")
    _run_git(repo, "add", ".gitignore")
    _run_git(repo, "add", "-f", plan.relative_to(repo).as_posix())
    _commit(repo, "force-add internal plan")

    assert internal_path_violations(repo) == ["docs/plans/implementation.md"]
