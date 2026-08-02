"""The repository history has one allowed identity and no co-author trailers."""

from __future__ import annotations

import subprocess

import pytest
from scripts import check_history_identity
from scripts.check_history_identity import find_violations, parse_history

from twair.paths import REPO_ROOT

EXPECTED_NAME = "kuotunyu"
EXPECTED_EMAIL = "61350295+kuotunyu@users.noreply.github.com"


def _record(
    *,
    sha: str = "a" * 40,
    author_name: str = EXPECTED_NAME,
    author_email: str = EXPECTED_EMAIL,
    committer_name: str = EXPECTED_NAME,
    committer_email: str = EXPECTED_EMAIL,
    body: str = "A measured change",
) -> bytes:
    fields = [sha, author_name, author_email, committer_name, committer_email, body]
    return b"\x00".join(field.encode() for field in fields) + b"\x00"


def test_the_allowed_author_and_committer_pass() -> None:
    commits = parse_history(_record())

    assert find_violations(commits, EXPECTED_NAME, EXPECTED_EMAIL) == []


def test_an_unexpected_author_reports_the_commit_and_field() -> None:
    commits = parse_history(_record(author_name="unexpected"))

    assert find_violations(commits, EXPECTED_NAME, EXPECTED_EMAIL) == [
        "aaaaaaaa author_name: does not match the allowed identity"
    ]


def test_an_unexpected_committer_reports_the_commit_and_field() -> None:
    commits = parse_history(_record(committer_email="unexpected@example.invalid"))

    assert find_violations(commits, EXPECTED_NAME, EXPECTED_EMAIL) == [
        "aaaaaaaa committer_email: does not match the allowed identity"
    ]


def test_a_case_varied_coauthor_trailer_is_still_rejected() -> None:
    commits = parse_history(_record(body="Subject\n\n  cO-aUtHoReD-bY: unexpected"))

    assert find_violations(commits, EXPECTED_NAME, EXPECTED_EMAIL) == [
        "aaaaaaaa body: forbidden co-author trailer"
    ]


def test_an_empty_commit_body_remains_a_complete_record() -> None:
    commits = parse_history(_record(body=""))

    assert commits[0].body == ""


def test_a_malformed_git_record_fails_loudly() -> None:
    with pytest.raises(ValueError, match="six fields"):
        parse_history(b"sha\x00author\x00")


def test_a_git_failure_fails_closed_without_reprinting_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failed_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess([], 128, b"", b"private git diagnostic")

    monkeypatch.setattr(subprocess, "run", failed_run)

    with pytest.raises(RuntimeError, match="git log failed") as error:
        check_history_identity.read_history()

    assert "private git diagnostic" not in str(error.value)


def test_the_cli_reports_how_many_reachable_commits_it_checked(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        check_history_identity,
        "load_conf",
        lambda _name: {
            "history": {"allowed_identity": {"name": EXPECTED_NAME, "email": EXPECTED_EMAIL}}
        },
    )
    monkeypatch.setattr(
        check_history_identity,
        "read_history",
        lambda _revision="HEAD": parse_history(_record()),
    )

    assert check_history_identity.main() == 0
    assert "1 commits reachable from HEAD" in capsys.readouterr().out


def test_the_cli_can_audit_an_explicit_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []
    monkeypatch.setattr(
        check_history_identity,
        "load_conf",
        lambda _name: {
            "history": {"allowed_identity": {"name": EXPECTED_NAME, "email": EXPECTED_EMAIL}}
        },
    )

    def read_revision(revision: str = "HEAD") -> list[check_history_identity.CommitIdentity]:
        seen.append(revision)
        return parse_history(_record())

    monkeypatch.setattr(check_history_identity, "read_history", read_revision)

    assert check_history_identity.main(["candidate-sha"]) == 0
    assert seen == ["candidate-sha"]


def test_prs_audit_the_head_but_test_the_default_merge_checkout() -> None:
    workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    checkout = workflow.split("      - name: Checkout Code", 1)[1].split(
        "      - name: Install Astral uv", 1
    )[0]

    assert "\n          ref:" not in checkout
    assert (
        'run: uv run python scripts/check_history_identity.py "${{ '
        'github.event.pull_request.head.sha || github.sha }}"'
    ) in workflow


def test_the_cli_lists_every_bad_commit_but_not_the_unexpected_identity(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    raw = _record(author_name="private-name") + _record(
        sha="b" * 40, body="Co-Authored-By: private-name"
    )
    monkeypatch.setattr(
        check_history_identity,
        "load_conf",
        lambda _name: {
            "history": {"allowed_identity": {"name": EXPECTED_NAME, "email": EXPECTED_EMAIL}}
        },
    )
    monkeypatch.setattr(
        check_history_identity,
        "read_history",
        lambda _revision="HEAD": parse_history(raw),
    )

    assert check_history_identity.main() == 1
    stderr = capsys.readouterr().err
    assert "aaaaaaaa author_name" in stderr
    assert "bbbbbbbb body" in stderr
    assert "private-name" not in stderr


def test_an_incomplete_identity_config_fails_closed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        check_history_identity,
        "load_conf",
        lambda _name: {"history": {"allowed_identity": {"name": EXPECTED_NAME}}},
    )

    assert check_history_identity.main() == 2
    stderr = capsys.readouterr().err
    assert "could not run" in stderr
    assert EXPECTED_NAME not in stderr


def test_an_operational_git_error_is_a_quiet_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        check_history_identity,
        "load_conf",
        lambda _name: {
            "history": {"allowed_identity": {"name": EXPECTED_NAME, "email": EXPECTED_EMAIL}}
        },
    )

    def fail(_revision: str = "HEAD") -> list[check_history_identity.CommitIdentity]:
        raise RuntimeError("private git diagnostic")

    monkeypatch.setattr(check_history_identity, "read_history", fail)

    assert check_history_identity.main() == 2
    stderr = capsys.readouterr().err
    assert "could not run" in stderr
    assert "private git diagnostic" not in stderr
