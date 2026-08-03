"""Keep protected project identities out of every reachable Git text surface."""

from __future__ import annotations

import hashlib
import io
import re
import subprocess
import sys
import unicodedata
from collections import defaultdict
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from twair.config import load_conf
from twair.paths import REPO_ROOT

NORMALIZATION_ID = "NFKC-casefold-alphanumeric-v1"
ALLOWED_ROLES = frozenset({"project_author", "project_supervisor"})
_HEX_OBJECT_RE = re.compile(rb"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_HEX_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
# U+0007 occurs in tracked Markdown history, so it stays scannable text rather
# than causing the entire blob to be mislabeled as binary.
_ALLOWED_TEXT_CONTROLS = frozenset("\a\t\n\f\r")
_BINARY_SIGNATURES = (
    b"\x89PNG\r\n\x1a\n",
    b"\xff\xd8\xff",
    b"GIF87a",
    b"GIF89a",
    b"%PDF-",
    b"PK\x03\x04",
    b"PK\x05\x06",
    b"PK\x07\x08",
    b"\x1f\x8b",
    b"7z\xbc\xaf\x27\x1c",
    b"\x7fELF",
    b"MZ",
    b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",
    b"PAR1",
    b"\x00asm",
)


class RepositoryAuditError(RuntimeError):
    """An audit could not prove its result without exposing private input."""


@dataclass(frozen=True)
class ProtectedIdentity:
    role: str
    normalized_length: int
    sha256: str


@dataclass(frozen=True)
class GitTextObject:
    object_id: str
    path_bytes: bytes
    data: bytes | None


@dataclass(frozen=True)
class CommitText:
    object_id: str
    text: str


@dataclass(frozen=True)
class AuditCounts:
    current_tree_texts: int
    historical_text_blobs: int
    commits: int


@dataclass(frozen=True)
class Violation:
    surface: str
    location: str
    role: str
    digest_prefix: str


def normalise_identity(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return "".join(character for character in normalized if character.isalnum())


def _parse_inventory(config: dict[str, Any]) -> list[ProtectedIdentity]:
    if set(config) != {"version", "normalization", "protected_identities"}:
        raise ValueError("anonymity config has an invalid top-level shape")
    version = config.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version != 1:
        raise ValueError("anonymity config has an invalid version")
    if config.get("normalization") != NORMALIZATION_ID:
        raise ValueError("anonymity config has an invalid normalization contract")
    raw_records = config.get("protected_identities")
    if not isinstance(raw_records, list) or not raw_records:
        raise ValueError("protected identity inventory must be a non-empty list")

    protected: list[ProtectedIdentity] = []
    seen: set[tuple[str, int, str]] = set()
    for raw in raw_records:
        if not isinstance(raw, dict) or set(raw) != {
            "role",
            "normalized_length",
            "sha256",
        }:
            raise ValueError("protected identity record has an invalid shape")
        role = raw.get("role")
        length = raw.get("normalized_length")
        digest = raw.get("sha256")
        if not isinstance(role, str) or role not in ALLOWED_ROLES:
            raise ValueError("protected identity record has an invalid role")
        if isinstance(length, bool) or not isinstance(length, int) or length <= 0:
            raise ValueError("protected identity record has an invalid length")
        if not isinstance(digest, str) or _HEX_DIGEST_RE.fullmatch(digest) is None:
            raise ValueError("protected identity record has an invalid digest")
        key = (role, length, digest)
        if key in seen:
            raise ValueError("protected identity inventory has a duplicate record")
        seen.add(key)
        protected.append(ProtectedIdentity(role=role, normalized_length=length, sha256=digest))
    return protected


def _load_protected_inventory() -> list[ProtectedIdentity]:
    try:
        return _parse_inventory(load_conf("anonymity"))
    except Exception as exc:
        # YAML parser errors may quote the offending private scalar, so every
        # configuration failure crosses this boundary as type-only metadata.
        raise RepositoryAuditError(
            f"anonymity inventory could not be loaded ({type(exc).__name__})"
        ) from None


def _git(repo: Path, args: Sequence[str], input_bytes: bytes | None = None) -> bytes:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=repo,
            input=input_bytes,
            capture_output=True,
            check=False,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RepositoryAuditError(f"git operation failed ({type(exc).__name__})") from None
    if result.returncode != 0:
        # Git stderr may contain a protected path, commit body, or local path.
        raise RepositoryAuditError(f"git operation failed with exit code {result.returncode}")
    return result.stdout


def _assert_complete_history(repo: Path, revision: str) -> None:
    shallow = _git(repo, ["rev-parse", "--is-shallow-repository"])
    if shallow.strip() != b"false":
        raise RepositoryAuditError("repository history is shallow or incomplete")
    _git(repo, ["cat-file", "-e", f"{revision}^{{commit}}"])
    _git(
        repo,
        ["rev-list", "--objects", "--no-object-names", "--missing=error", revision],
    )


def _parse_object_id(raw: bytes) -> str:
    if _HEX_OBJECT_RE.fullmatch(raw) is None:
        raise RepositoryAuditError("Git returned a malformed object identifier")
    return raw.decode("ascii")


def _read_batch_blobs(repo: Path, object_ids: Sequence[str]) -> dict[str, bytes]:
    if not object_ids:
        return {}
    request = b"".join(object_id.encode("ascii") + b"\n" for object_id in object_ids)
    raw = _git(repo, ["cat-file", "--batch"], request)
    stream = io.BytesIO(raw)
    blobs: dict[str, bytes] = {}
    for expected in object_ids:
        header = stream.readline()
        parts = header.rstrip(b"\n").split(b" ")
        if len(parts) != 3 or parts[1] != b"blob":
            raise RepositoryAuditError("Git returned a malformed blob header")
        object_id = _parse_object_id(parts[0])
        if object_id != expected:
            raise RepositoryAuditError("Git returned blobs in an unexpected order")
        try:
            size = int(parts[2])
        except ValueError:
            raise RepositoryAuditError("Git returned a malformed blob size") from None
        if size < 0:
            raise RepositoryAuditError("Git returned a malformed blob size")
        data = stream.read(size)
        if len(data) != size or stream.read(1) != b"\n":
            raise RepositoryAuditError("Git returned a truncated blob")
        blobs[object_id] = data
    if stream.read(1):
        raise RepositoryAuditError("Git returned unexpected trailing blob data")
    return blobs


def _read_current_tree(repo: Path, revision: str) -> list[GitTextObject]:
    raw = _git(repo, ["ls-tree", "-r", "-z", "--full-tree", revision])
    entries = raw.split(b"\x00")
    if not entries or entries[-1] != b"":
        raise RepositoryAuditError("Git returned a malformed current tree")
    entries.pop()
    parsed: list[tuple[str, bytes, bool]] = []
    for entry in entries:
        try:
            header, path_bytes = entry.split(b"\t", 1)
        except ValueError:
            raise RepositoryAuditError("Git returned a malformed tree entry") from None
        fields = header.split(b" ")
        if len(fields) != 3:
            raise RepositoryAuditError("Git returned a malformed tree entry")
        _mode, object_type, raw_object_id = fields
        if object_type not in {b"blob", b"commit"}:
            raise RepositoryAuditError("Git returned an unsupported tree entry")
        parsed.append((_parse_object_id(raw_object_id), path_bytes, object_type == b"blob"))
    blobs = _read_batch_blobs(repo, [object_id for object_id, _path, is_blob in parsed if is_blob])
    return [
        GitTextObject(
            object_id=object_id,
            path_bytes=path_bytes,
            data=blobs[object_id] if is_blob else None,
        )
        for object_id, path_bytes, is_blob in parsed
    ]


def _read_reachable_blobs(
    repo: Path, revision: str, current_object_ids: Collection[str] = ()
) -> list[GitTextObject]:
    raw_ids = _git(repo, ["rev-list", "--objects", "--no-object-names", revision]).splitlines()
    object_ids: list[str] = []
    seen: set[str] = set()
    for raw_object_id in raw_ids:
        object_id = _parse_object_id(raw_object_id)
        if object_id not in seen:
            seen.add(object_id)
            object_ids.append(object_id)
    if not object_ids:
        raise RepositoryAuditError("reachable Git object inventory is empty")

    request = b"".join(object_id.encode("ascii") + b"\n" for object_id in object_ids)
    raw_types = _git(repo, ["cat-file", "--batch-check"], request).splitlines()
    if len(raw_types) != len(object_ids):
        raise RepositoryAuditError("Git returned an incomplete object inventory")
    historical_blob_ids: list[str] = []
    current = set(current_object_ids)
    for expected, raw_type in zip(object_ids, raw_types, strict=True):
        fields = raw_type.split(b" ")
        if len(fields) < 2 or _parse_object_id(fields[0]) != expected:
            raise RepositoryAuditError("Git returned a malformed object inventory")
        if fields[1] == b"blob" and expected not in current:
            historical_blob_ids.append(expected)
    blobs = _read_batch_blobs(repo, historical_blob_ids)
    return [
        GitTextObject(object_id=object_id, path_bytes=b"", data=blobs[object_id])
        for object_id in historical_blob_ids
    ]


def _read_historical_paths(repo: Path, revision: str) -> list[bytes]:
    raw = _git(
        repo,
        ["log", "--no-renames", "--format=", "--name-only", "-z", revision],
    )
    entries = raw.split(b"\x00")
    if not entries or entries[-1] != b"":
        raise RepositoryAuditError("Git returned malformed historical paths")
    entries.pop()
    if any(not entry for entry in entries):
        raise RepositoryAuditError("Git returned malformed historical paths")
    return list(dict.fromkeys(entries))


def _read_commit_bodies(repo: Path, revision: str) -> list[CommitText]:
    raw = _git(
        repo,
        ["log", "-z", "--encoding=UTF-8", "--format=%H%x00%B", revision],
    )
    fields = raw.split(b"\x00")
    if not fields or fields[-1] != b"":
        raise RepositoryAuditError("Git returned malformed commit bodies")
    fields.pop()
    if not fields or len(fields) % 2:
        raise RepositoryAuditError("Git returned malformed commit bodies")
    commits: list[CommitText] = []
    for index in range(0, len(fields), 2):
        object_id = _parse_object_id(fields[index])
        try:
            body = fields[index + 1].decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise RepositoryAuditError("Git returned an undecodable commit body") from None
        commits.append(CommitText(object_id=object_id, text=body))
    return commits


def _decode_candidate_text(data: bytes) -> tuple[str, ...] | None:
    if any(data.startswith(signature) for signature in _BINARY_SIGNATURES):
        return None

    bom_encoding: str | None = None
    if data.startswith(b"\xff\xfe"):
        bom_encoding = "utf-16-le"
    elif data.startswith(b"\xfe\xff"):
        bom_encoding = "utf-16-be"
    if bom_encoding is not None:
        try:
            text = data[2:].decode(bom_encoding, errors="strict")
        except UnicodeDecodeError:
            raise RepositoryAuditError("encountered malformed UTF-16 text") from None
        _reject_unsupported_controls(text)
        return (text,)

    candidates: list[str] = []
    utf8_control_error: RepositoryAuditError | None = None
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        pass
    else:
        try:
            _reject_unsupported_controls(text)
        except RepositoryAuditError as exc:
            utf8_control_error = exc
        else:
            candidates.append(text)

    if utf8_control_error is not None:
        raise utf8_control_error

    # Printable ASCII byte pairs can also be meaningful UTF-16 code points, so
    # byte-range and NUL-pattern heuristics cannot safely discard either endian.
    candidates.extend(_decode_bomless_utf16(data))
    if candidates:
        return tuple(dict.fromkeys(candidates))
    raise RepositoryAuditError("encountered an undecodable non-binary blob")


def _reject_unsupported_controls(text: str) -> None:
    if any(
        unicodedata.category(character) == "Cc" and character not in _ALLOWED_TEXT_CONTROLS
        for character in text
    ):
        raise RepositoryAuditError("encountered an unsupported control character")


def _decode_bomless_utf16(data: bytes) -> list[str]:
    if len(data) < 4 or len(data) % 2:
        return []
    candidates: list[str] = []
    for encoding in ("utf-16-le", "utf-16-be"):
        try:
            text = data.decode(encoding, errors="strict")
        except UnicodeDecodeError:
            continue
        if not text or any(unicodedata.category(character).startswith("C") for character in text):
            continue
        candidates.append(text)
    return candidates


def _find_matches(text: str, protected: Sequence[ProtectedIdentity]) -> list[ProtectedIdentity]:
    normalized = normalise_identity(text)
    by_length: dict[int, dict[str, list[ProtectedIdentity]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for identity in protected:
        by_length[identity.normalized_length][identity.sha256].append(identity)

    matched: dict[tuple[str, int, str], ProtectedIdentity] = {}
    for length, identities_by_digest in by_length.items():
        if len(normalized) < length:
            continue
        for index in range(len(normalized) - length + 1):
            digest = hashlib.sha256(normalized[index : index + length].encode("utf-8")).hexdigest()
            for identity in identities_by_digest.get(digest, ()):
                matched[(identity.role, identity.normalized_length, identity.sha256)] = identity
    return list(matched.values())


def _path_location(path_bytes: bytes) -> str:
    digest = hashlib.sha256(path_bytes).hexdigest()
    return f"path:{digest[:12]}"


def _content_location(item: GitTextObject) -> str:
    location = f"blob:{item.object_id[:12]}"
    if item.path_bytes:
        location += f" {_path_location(item.path_bytes)}"
    return location


def _violations_for(
    surface: str,
    location: str,
    text: str,
    protected: Sequence[ProtectedIdentity],
) -> list[Violation]:
    return [
        Violation(
            surface=surface,
            location=location,
            role=identity.role,
            digest_prefix=identity.sha256[:12],
        )
        for identity in _find_matches(text, protected)
    ]


def _decode_path(path_bytes: bytes) -> str:
    try:
        return path_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise RepositoryAuditError("encountered an undecodable Git path") from None


def audit_repository(
    repo: Path, revision: str, protected: Sequence[ProtectedIdentity]
) -> tuple[list[Violation], AuditCounts]:
    if not protected:
        raise RepositoryAuditError("protected identity inventory is empty")
    _assert_complete_history(repo, revision)

    current = _read_current_tree(repo, revision)
    current_object_ids = {item.object_id for item in current}
    historical_blobs = _read_reachable_blobs(repo, revision, current_object_ids)
    all_historical_paths = _read_historical_paths(repo, revision)
    commits = _read_commit_bodies(repo, revision)
    if not current:
        raise RepositoryAuditError("current tree is empty")
    if not commits:
        raise RepositoryAuditError("reachable commit inventory is empty")

    violations: list[Violation] = []
    current_texts = 0
    current_paths = {item.path_bytes for item in current}
    for item in current:
        path_text = _decode_path(item.path_bytes)
        violations.extend(
            _violations_for(
                "current-path",
                _path_location(item.path_bytes),
                path_text,
                protected,
            )
        )
        if item.data is None:
            continue
        text_candidates = _decode_candidate_text(item.data)
        if text_candidates is None:
            continue
        current_texts += 1
        for text in text_candidates:
            violations.extend(
                _violations_for("current-tree", _content_location(item), text, protected)
            )

    historical_texts = 0
    for item in historical_blobs:
        if item.data is None:
            raise RepositoryAuditError("historical blob inventory omitted blob data")
        text_candidates = _decode_candidate_text(item.data)
        if text_candidates is None:
            continue
        historical_texts += 1
        for text in text_candidates:
            violations.extend(
                _violations_for("historical-blob", _content_location(item), text, protected)
            )

    for path_bytes in all_historical_paths:
        if path_bytes in current_paths:
            continue
        path_text = _decode_path(path_bytes)
        violations.extend(
            _violations_for(
                "historical-path",
                _path_location(path_bytes),
                path_text,
                protected,
            )
        )

    for commit in commits:
        violations.extend(
            _violations_for(
                "commit-message",
                f"commit:{commit.object_id[:12]}",
                commit.text,
                protected,
            )
        )

    unique = list(dict.fromkeys(violations))
    return unique, AuditCounts(
        current_tree_texts=current_texts,
        historical_text_blobs=historical_texts,
        commits=len(commits),
    )


def format_violation(violation: Violation) -> str:
    return (
        f"{violation.surface} {violation.location} "
        f"role={violation.role} digest={violation.digest_prefix}"
    )


def format_coverage(counts: AuditCounts) -> str:
    return (
        f"{counts.current_tree_texts} current-tree texts, "
        f"{counts.historical_text_blobs} historical text blobs, "
        f"{counts.commits} reachable commits"
    )


def main(argv: Sequence[str] = ()) -> int:
    try:
        args = list(argv)
        if len(args) > 1:
            raise ValueError("repository anonymity check accepts at most one revision")
        revision = args[0] if args else "HEAD"
        protected = _load_protected_inventory()
        violations, counts = audit_repository(REPO_ROOT, revision, protected)
    except (
        OSError,
        RepositoryAuditError,
        subprocess.SubprocessError,
        TypeError,
        UnicodeError,
        ValueError,
    ) as exc:
        print(
            f"repository anonymity check could not run ({type(exc).__name__})",
            file=sys.stderr,
        )
        return 2

    if violations:
        for violation in violations:
            print(format_violation(violation), file=sys.stderr)
        print(
            f"repository anonymity check found protected matches: {format_coverage(counts)}",
            file=sys.stderr,
        )
        return 1

    print(f"repository anonymity check passed: {format_coverage(counts)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
