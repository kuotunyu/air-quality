"""Protected synthetic tokens stay out of every reachable Git text surface."""

from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Collection
from pathlib import Path
from typing import Any

import pytest
import yaml
from scripts import check_repository_anonymity as anonymity
from scripts.check_repository_anonymity import (
    NORMALIZATION_ID,
    GitTextObject,
    ProtectedIdentity,
    RepositoryAuditError,
    _git,
    _parse_inventory,
    audit_repository,
    format_violation,
    normalise_identity,
)

SYNTHETIC_TOKEN = "GuardToken42"
SYNTHETIC_ROLE = "project_author"


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


def _commit(repo: Path, message: str = "synthetic fixture commit") -> None:
    _run_git(repo, "add", "-A")
    _run_git(repo, "commit", "-q", "-m", message)


def _commit_with_body(repo: Path, subject: str, body: str) -> None:
    _run_git(repo, "add", "-A")
    _run_git(repo, "commit", "-q", "-m", subject, "-m", body)


def _new_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _run_git(repo, "init", "-q")
    _run_git(repo, "config", "user.name", "Synthetic Fixture")
    _run_git(repo, "config", "user.email", "fixture@example.invalid")
    (repo / "README.txt").write_text("safe seed\n", encoding="utf-8")
    _commit(repo, "safe seed")
    return repo


def _protected(token: str = SYNTHETIC_TOKEN, role: str = SYNTHETIC_ROLE) -> ProtectedIdentity:
    normalized = normalise_identity(token)
    return ProtectedIdentity(
        role=role,
        normalized_length=len(normalized),
        sha256=hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
    )


def _inventory(records: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    protected = _protected()
    if records is None:
        records = [
            {
                "role": protected.role,
                "normalized_length": protected.normalized_length,
                "sha256": protected.sha256,
            }
        ]
    return {
        "version": 1,
        "normalization": NORMALIZATION_ID,
        "protected_identities": records,
    }


def _surfaces(repo: Path) -> tuple[list[anonymity.Violation], anonymity.AuditCounts]:
    return audit_repository(repo, "HEAD", [_protected()])


def test_normalization_uses_nfkc_casefolding_and_only_alphanumeric_characters() -> None:
    assert normalise_identity("Ｇｕａｒｄ— Token_４２!") == "guardtoken42"


def test_an_empty_protected_inventory_is_rejected() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        _parse_inventory(_inventory([]))


@pytest.mark.parametrize("version", [True, 1.0, "1", None])
def test_inventory_version_must_be_the_actual_non_boolean_integer_one(version: object) -> None:
    with pytest.raises(ValueError, match="version"):
        _parse_inventory(_inventory() | {"version": version})


@pytest.mark.parametrize(
    "config",
    [
        {"version": 1, "normalization": NORMALIZATION_ID},
        _inventory() | {"unexpected": "safe"},
    ],
)
def test_inventory_top_level_shape_is_exact(config: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="top-level shape"):
        _parse_inventory(config)


def test_duplicate_inventory_records_are_rejected() -> None:
    record = _inventory()["protected_identities"][0]

    with pytest.raises(ValueError, match="duplicate"):
        _parse_inventory(_inventory([record, dict(record)]))


@pytest.mark.parametrize("digest", ["a" * 63, "A" * 64, "g" * 64])
def test_a_digest_must_be_exactly_64_lowercase_hexadecimal_characters(
    digest: str,
) -> None:
    record = _inventory()["protected_identities"][0] | {"sha256": digest}

    with pytest.raises(ValueError, match="digest") as error:
        _parse_inventory(_inventory([record]))

    assert digest not in str(error.value)


@pytest.mark.parametrize("length", [True, False, 0, -1])
def test_a_normalized_length_must_be_a_positive_non_boolean_integer(
    length: object,
) -> None:
    record = _inventory()["protected_identities"][0] | {"normalized_length": length}

    with pytest.raises(ValueError, match="length"):
        _parse_inventory(_inventory([record]))


def test_an_unknown_protected_role_is_rejected_without_echoing_it() -> None:
    record = _inventory()["protected_identities"][0] | {"role": "private-role"}

    with pytest.raises(ValueError, match="role") as error:
        _parse_inventory(_inventory([record]))

    assert "private-role" not in str(error.value)


def test_an_unknown_normalization_contract_is_rejected_without_echoing_it() -> None:
    config = _inventory() | {"normalization": "private-normalization"}

    with pytest.raises(ValueError, match="normalization") as error:
        _parse_inventory(config)

    assert "private-normalization" not in str(error.value)


def test_yaml_config_errors_are_redacted_at_the_cli_boundary(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def raise_protected_yaml_error(_name: str) -> dict[str, Any]:
        raise yaml.YAMLError(SYNTHETIC_TOKEN)

    monkeypatch.setattr(anonymity, "load_conf", raise_protected_yaml_error)
    escaped_exception: str | None = None
    result: int | None = None
    try:
        result = anonymity.main(())
    except yaml.YAMLError as exc:
        escaped_exception = type(exc).__name__
    stderr = capsys.readouterr().err

    assert escaped_exception is None
    assert result == 2
    assert SYNTHETIC_TOKEN not in stderr
    assert "Traceback" not in stderr


def test_a_protected_match_cli_result_includes_safe_coverage_counts(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    repo = _new_repo(tmp_path)
    (repo / "current.txt").write_text(SYNTHETIC_TOKEN, encoding="utf-8")
    _commit(repo)
    monkeypatch.setattr(anonymity, "REPO_ROOT", repo)
    monkeypatch.setattr(anonymity, "_load_protected_inventory", lambda: [_protected()])

    result = anonymity.main(())
    captured = capsys.readouterr()

    assert result == 1
    assert "2 current-tree texts" in captured.err
    assert "0 historical text blobs" in captured.err
    assert "2 reachable commits" in captured.err
    assert SYNTHETIC_TOKEN not in captured.err
    assert SYNTHETIC_TOKEN not in captured.out


def test_a_token_in_current_tracked_text_is_a_current_tree_violation(
    tmp_path: Path,
) -> None:
    repo = _new_repo(tmp_path)
    (repo / "current.txt").write_text(SYNTHETIC_TOKEN, encoding="utf-8")
    _commit(repo)

    violations, counts = _surfaces(repo)

    assert [item.surface for item in violations] == ["current-tree"]
    assert counts.current_tree_texts > 0
    assert counts.commits > 0


def test_a_token_in_a_current_path_is_redacted_and_rejected(tmp_path: Path) -> None:
    repo = _new_repo(tmp_path)
    raw_path = f"private-{SYNTHETIC_TOKEN}.txt"
    (repo / raw_path).write_text("safe content", encoding="utf-8")
    _commit(repo)

    violations, _counts = _surfaces(repo)
    rendered = "\n".join(format_violation(item) for item in violations)

    assert [item.surface for item in violations] == ["current-path"]
    assert raw_path not in rendered
    assert SYNTHETIC_TOKEN not in rendered


def test_a_current_gitlink_path_is_scanned_without_the_historical_path_reader(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _new_repo(tmp_path)
    gitlink_target = _run_git(repo, "rev-parse", "HEAD").decode("ascii").strip()
    raw_path = f"private-{SYNTHETIC_TOKEN}"
    _run_git(
        repo,
        "update-index",
        "--add",
        "--cacheinfo",
        "160000",
        gitlink_target,
        raw_path,
    )
    _run_git(repo, "commit", "-q", "-m", "safe gitlink subject")

    def no_historical_paths(_repo: Path, _revision: str) -> list[bytes]:
        return []

    monkeypatch.setattr(anonymity, "_read_historical_paths", no_historical_paths)

    violations, _counts = _surfaces(repo)
    rendered = "\n".join(format_violation(item) for item in violations)

    assert [item.surface for item in violations] == ["current-path"]
    assert raw_path not in rendered
    assert SYNTHETIC_TOKEN not in rendered


def test_a_token_only_in_an_ancestor_blob_remains_a_violation(tmp_path: Path) -> None:
    repo = _new_repo(tmp_path)
    target = repo / "historical.txt"
    target.write_text(SYNTHETIC_TOKEN, encoding="utf-8")
    _commit(repo, "unsafe content fixture")
    target.write_text("safe replacement", encoding="utf-8")
    _commit(repo, "safe replacement")

    violations, counts = _surfaces(repo)

    assert [item.surface for item in violations] == ["historical-blob"]
    assert counts.historical_text_blobs > 0


def test_a_token_only_in_an_ancestor_path_remains_a_violation(tmp_path: Path) -> None:
    repo = _new_repo(tmp_path)
    old_path = repo / f"historical-{SYNTHETIC_TOKEN}.txt"
    old_path.write_text("safe content", encoding="utf-8")
    _commit(repo, "unsafe path fixture")
    _run_git(repo, "mv", old_path.name, "safe-path.txt")
    _commit(repo, "safe rename")

    violations, _counts = _surfaces(repo)
    rendered = "\n".join(format_violation(item) for item in violations)

    assert [item.surface for item in violations] == ["historical-path"]
    assert old_path.name not in rendered
    assert SYNTHETIC_TOKEN not in rendered


def test_a_token_only_in_an_ancestor_commit_body_remains_a_violation(
    tmp_path: Path,
) -> None:
    repo = _new_repo(tmp_path)
    (repo / "first.txt").write_text("safe", encoding="utf-8")
    _commit_with_body(repo, "safe ancestor subject", f"protected body {SYNTHETIC_TOKEN}")
    assert SYNTHETIC_TOKEN not in _run_git(repo, "show", "-s", "--format=%s", "HEAD").decode()
    assert SYNTHETIC_TOKEN in _run_git(repo, "show", "-s", "--format=%b", "HEAD").decode()
    (repo / "second.txt").write_text("safe", encoding="utf-8")
    _commit(repo, "safe current body")

    violations, counts = _surfaces(repo)

    assert [item.surface for item in violations] == ["commit-message"]
    assert counts.commits == 3


@pytest.mark.parametrize(
    "variant",
    [
        "guardtoken42",
        "GUARDTOKEN42",
        "Guard Token 42",
        "Guard-Token_42",
        "ＧｕａｒｄＴｏｋｅｎ４２",
    ],
)
def test_normalized_spelling_variants_are_detected(tmp_path: Path, variant: str) -> None:
    repo = _new_repo(tmp_path)
    (repo / "variant.txt").write_text(variant, encoding="utf-8")
    _commit(repo)

    violations, _counts = _surfaces(repo)

    assert [item.surface for item in violations] == ["current-tree"]


def test_a_clean_repository_reports_nonzero_coverage_for_all_three_surfaces(
    tmp_path: Path,
) -> None:
    repo = _new_repo(tmp_path)
    (repo / "README.txt").write_text("different safe text\n", encoding="utf-8")
    _commit(repo, "safe second commit")

    violations, counts = _surfaces(repo)

    assert violations == []
    assert counts.current_tree_texts > 0
    assert counts.historical_text_blobs > 0
    assert counts.commits > 0


def test_a_measured_binary_blob_does_not_create_a_text_match(tmp_path: Path) -> None:
    repo = _new_repo(tmp_path)
    (repo / "image.bin").write_bytes(b"\x89PNG\r\n\x1a\n" + SYNTHETIC_TOKEN.encode("ascii"))
    _commit(repo)

    violations, counts = _surfaces(repo)

    assert violations == []
    assert counts.current_tree_texts > 0


@pytest.mark.parametrize(
    ("encoding", "byte_order_mark"),
    [
        ("utf-16-le", b"\xff\xfe"),
        ("utf-16-be", b"\xfe\xff"),
        ("utf-16-le", b""),
        ("utf-16-be", b""),
    ],
)
def test_utf16_text_is_scanned_with_or_without_a_byte_order_mark(
    tmp_path: Path, encoding: str, byte_order_mark: bytes
) -> None:
    repo = _new_repo(tmp_path)
    payload = SYNTHETIC_TOKEN if byte_order_mark else f"{SYNTHETIC_TOKEN}\u6587"
    (repo / "utf16.txt").write_bytes(byte_order_mark + payload.encode(encoding))
    _commit(repo)

    violations, _counts = _surfaces(repo)

    assert any(item.surface == "current-tree" for item in violations)


@pytest.mark.parametrize(
    ("encoding", "raw_hex"),
    [
        ("utf-16-le", "21 4e 22 4e 23 4e"),
        ("utf-16-be", "4e 21 4e 22 4e 23"),
    ],
)
def test_ambiguous_printable_ascii_bytes_are_scanned_as_bomless_utf16_candidates(
    tmp_path: Path, encoding: str, raw_hex: str
) -> None:
    repo = _new_repo(tmp_path)
    ambiguous_bytes = bytes.fromhex(raw_hex)
    protected_token = ambiguous_bytes.decode(encoding)
    protected = _protected(protected_token)
    assert all(32 <= byte < 127 for byte in ambiguous_bytes)
    assert ambiguous_bytes.decode("utf-8")
    assert anonymity._find_matches(ambiguous_bytes.decode("utf-8"), [protected]) == []
    (repo / "ambiguous.txt").write_bytes(ambiguous_bytes)
    _commit(repo)

    violations, _counts = audit_repository(repo, "HEAD", [protected])

    assert any(item.surface == "current-tree" for item in violations)


def test_distinct_decode_candidates_cannot_form_a_match_across_their_boundary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _new_repo(tmp_path)
    (repo / "split.txt").write_bytes(b"split-candidate")
    _commit(repo)
    original_decode = anonymity._decode_candidate_text

    def split_decode(data: bytes) -> tuple[str, ...] | None:
        if data == b"split-candidate":
            return ("Guard", "Token42")
        return original_decode(data)

    monkeypatch.setattr(anonymity, "_decode_candidate_text", split_decode)

    violations, _counts = _surfaces(repo)

    assert violations == []


def test_even_length_valid_utf8_with_an_unknown_control_fails_closed() -> None:
    with pytest.raises(RepositoryAuditError, match="control"):
        anonymity._decode_candidate_text(b"abc\x01")


def test_a_utf8_protected_token_cannot_be_discarded_for_a_utf16_candidate() -> None:
    controlled = f"x{SYNTHETIC_TOKEN}\x01".encode()
    assert len(controlled) % 2 == 0

    with pytest.raises(RepositoryAuditError, match="control") as error:
        anonymity._decode_candidate_text(controlled)

    assert SYNTHETIC_TOKEN not in str(error.value)


def test_the_measured_bell_delimiter_remains_scannable_text(tmp_path: Path) -> None:
    repo = _new_repo(tmp_path)
    (repo / "measured-control.txt").write_bytes(f"prefix\x07{SYNTHETIC_TOKEN}".encode())
    _commit(repo)

    violations, _counts = _surfaces(repo)

    assert any(item.surface == "current-tree" for item in violations)


def test_a_nonbinary_undecodable_blob_fails_closed_without_raw_bytes(
    tmp_path: Path,
) -> None:
    repo = _new_repo(tmp_path)
    (repo / "undecodable.txt").write_bytes(b"\x80\x81\xfe")
    _commit(repo)

    with pytest.raises(RepositoryAuditError, match="undecodable") as error:
        _surfaces(repo)

    assert repr(b"\x80\x81\xfe") not in str(error.value)


def test_a_depth_one_clone_is_rejected_before_audit(tmp_path: Path) -> None:
    source = _new_repo(tmp_path / "source-root")
    (source / "second.txt").write_text("safe", encoding="utf-8")
    _commit(source, "safe second commit")
    shallow = tmp_path / "shallow"
    _run_git(
        tmp_path,
        "clone",
        "-q",
        "--depth",
        "1",
        source.as_uri(),
        str(shallow),
    )

    with pytest.raises(RepositoryAuditError, match="shallow"):
        audit_repository(shallow, "HEAD", [_protected()])


def test_a_git_failure_is_redacted_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def failed_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess([], 128, b"", f"private {SYNTHETIC_TOKEN}".encode())

    monkeypatch.setattr(subprocess, "run", failed_run)

    with pytest.raises(RepositoryAuditError, match="git operation failed") as error:
        _git(tmp_path, ["status"])

    assert SYNTHETIC_TOKEN not in str(error.value)
    assert "private" not in str(error.value)


def test_diagnostics_expose_only_safe_location_and_digest_prefixes(
    tmp_path: Path,
) -> None:
    repo = _new_repo(tmp_path)
    raw_path = f"private-{SYNTHETIC_TOKEN}.txt"
    (repo / raw_path).write_text(SYNTHETIC_TOKEN, encoding="utf-8")
    _commit(repo)

    violations, _counts = _surfaces(repo)
    rendered = "\n".join(format_violation(item) for item in violations)
    digest_prefix = _protected().sha256[:12]

    assert "current-path" in rendered
    assert "current-tree" in rendered
    assert SYNTHETIC_ROLE in rendered
    assert digest_prefix in rendered
    assert raw_path not in rendered
    assert SYNTHETIC_TOKEN not in rendered


def test_all_git_surface_readers_are_called_before_empty_tree_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _new_repo(tmp_path)
    calls: list[str] = []
    original_blobs = anonymity._read_reachable_blobs
    original_paths = anonymity._read_historical_paths
    original_commits = anonymity._read_commit_bodies

    monkeypatch.setattr(anonymity, "_read_current_tree", lambda *_args: [])

    def blobs(
        repo_arg: Path,
        revision_arg: str,
        current_object_ids: Collection[str] = (),
    ) -> list[GitTextObject]:
        calls.append("historical-blobs")
        return original_blobs(repo_arg, revision_arg, current_object_ids)

    def paths(repo_arg: Path, revision_arg: str) -> list[bytes]:
        calls.append("historical-paths")
        return original_paths(repo_arg, revision_arg)

    def commits(repo_arg: Path, revision_arg: str) -> list[anonymity.CommitText]:
        calls.append("commit-bodies")
        return original_commits(repo_arg, revision_arg)

    monkeypatch.setattr(anonymity, "_read_reachable_blobs", blobs)
    monkeypatch.setattr(anonymity, "_read_historical_paths", paths)
    monkeypatch.setattr(anonymity, "_read_commit_bodies", commits)

    with pytest.raises(RepositoryAuditError, match="current tree"):
        _surfaces(repo)

    assert calls == ["historical-blobs", "historical-paths", "commit-bodies"]


def test_the_current_tree_reader_is_a_mutation_sentinel(tmp_path: Path) -> None:
    repo = _new_repo(tmp_path)
    (repo / "sentinel.txt").write_text(SYNTHETIC_TOKEN, encoding="utf-8")
    _commit(repo)

    violations, _counts = _surfaces(repo)

    assert any(item.surface == "current-tree" for item in violations)


def test_the_historical_blob_reader_is_a_mutation_sentinel(tmp_path: Path) -> None:
    repo = _new_repo(tmp_path)
    target = repo / "sentinel.txt"
    target.write_text(SYNTHETIC_TOKEN, encoding="utf-8")
    _commit(repo, "historical sentinel")
    target.write_text("safe current text", encoding="utf-8")
    _commit(repo, "safe current text")

    violations, _counts = _surfaces(repo)

    assert any(item.surface == "historical-blob" for item in violations)


def test_the_commit_body_reader_is_a_mutation_sentinel(tmp_path: Path) -> None:
    repo = _new_repo(tmp_path)
    (repo / "sentinel.txt").write_text("safe", encoding="utf-8")
    _commit_with_body(repo, "safe sentinel subject", f"sentinel body {SYNTHETIC_TOKEN}")
    (repo / "safe.txt").write_text("safe", encoding="utf-8")
    _commit(repo, "safe current body")

    violations, _counts = _surfaces(repo)

    assert any(item.surface == "commit-message" for item in violations)
