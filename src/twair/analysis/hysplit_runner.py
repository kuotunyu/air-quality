"""Owner-provided HYSPLIT installation and isolated execution boundary."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import polars as pl

from twair.analysis.hysplit_io import (
    MeteorologyFile,
    parse_trajectory_endpoints,
    render_trajectory_control,
    validate_complete_trajectory,
)
from twair.analysis.hysplit_protocol import validate_ascii_external_path
from twair.paths import REPO_ROOT

Executor = Callable[..., subprocess.CompletedProcess[str]]
_RUN_ID = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?")
_RESERVED_RUN_NAMES = {"ASCDATA.CFG", "CONTROL", "MESSAGE", "tdump"}


@dataclass(frozen=True, slots=True)
class ObservedFile:
    relative_path: str
    bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class HysplitInstallation:
    root: Path
    hyts_std: Path
    hyts_ens: Path
    ascdata: Path
    sample_traj: Path
    hyts_std_identity: ObservedFile
    hyts_ens_identity: ObservedFile
    ascdata_identity: ObservedFile
    sample_traj_identity: ObservedFile


@dataclass(frozen=True, slots=True)
class PreparedRun:
    installation: HysplitInstallation
    run_id: str
    directory: Path
    control_bytes: bytes
    control_sha256: str
    meteorology: tuple[MeteorologyFile, ...]
    duration_hours: int


@dataclass(frozen=True, slots=True)
class HysplitExecution:
    success: bool
    problem: str | None
    returncode: int | None
    stdout: str
    stderr: str
    control_bytes: bytes
    control_sha256: str
    message_bytes: bytes | None
    message_sha256: str | None
    endpoint_bytes: bytes | None
    endpoint_sha256: str | None
    endpoints: pl.DataFrame | None


def _is_link_like(path: Path) -> bool:
    try:
        attributes = int(getattr(path.lstat(), "st_file_attributes", 0))
    except OSError:
        return True
    is_junction = getattr(path, "is_junction", None)
    return (
        path.is_symlink()
        or (is_junction is not None and is_junction())
        or bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    )


def _identity(path: Path, *, relative_to: Path) -> ObservedFile:
    payload = path.read_bytes()
    return ObservedFile(
        relative_path=path.relative_to(relative_to).as_posix(),
        bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def _validated_regular_file(path: Path, *, root: Path, label: str) -> Path:
    if not path.exists():
        raise RuntimeError(f"HYSPLIT installation member is missing: {label}")
    if _is_link_like(path) or not path.is_file():
        raise RuntimeError(f"HYSPLIT installation member is linked or non-regular: {label}")
    resolved = path.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise RuntimeError(f"HYSPLIT installation member escapes its root: {label}") from exc
    if resolved.stat().st_size <= 0:
        raise RuntimeError(f"HYSPLIT installation member is empty: {label}")
    return resolved


def inspect_hysplit_installation(root: Path) -> HysplitInstallation:
    """Inspect an explicit installation without accepting, downloading, or copying it."""
    selected = validate_ascii_external_path(root, label="HYSPLIT installation root")
    if not selected.exists():
        raise RuntimeError("HYSPLIT installation root is missing")
    if _is_link_like(selected) or not selected.is_dir():
        raise RuntimeError("HYSPLIT installation root is linked or not a directory")
    resolved = selected.resolve(strict=True)
    if resolved != selected:
        raise RuntimeError("HYSPLIT installation root resolves elsewhere")

    hyts_std = _validated_regular_file(
        selected / "exec" / "hyts_std.exe",
        root=selected,
        label="exec/hyts_std.exe",
    )
    hyts_ens = _validated_regular_file(
        selected / "exec" / "hyts_ens.exe",
        root=selected,
        label="exec/hyts_ens.exe",
    )
    ascdata = _validated_regular_file(
        selected / "bdyfiles" / "ASCDATA.CFG",
        root=selected,
        label="bdyfiles/ASCDATA.CFG",
    )
    sample_traj = _validated_regular_file(
        selected / "working" / "sample_traj",
        root=selected,
        label="working/sample_traj",
    )
    return HysplitInstallation(
        root=selected,
        hyts_std=hyts_std,
        hyts_ens=hyts_ens,
        ascdata=ascdata,
        sample_traj=sample_traj,
        hyts_std_identity=_identity(hyts_std, relative_to=selected),
        hyts_ens_identity=_identity(hyts_ens, relative_to=selected),
        ascdata_identity=_identity(ascdata, relative_to=selected),
        sample_traj_identity=_identity(sample_traj, relative_to=selected),
    )


def _verify_observed(
    path: Path,
    expected: ObservedFile,
    *,
    root: Path,
    label: str,
) -> bytes:
    if path != root / Path(expected.relative_path):
        raise RuntimeError(f"{label} path changed after inspection")
    if _is_link_like(path) or not path.is_file():
        raise RuntimeError(f"{label} is missing, linked, or non-regular")
    payload = path.read_bytes()
    if len(payload) != expected.bytes or hashlib.sha256(payload).hexdigest() != expected.sha256:
        raise RuntimeError(f"{label} identity changed after inspection")
    return payload


def _meteorology_payload(member: MeteorologyFile) -> tuple[Path, bytes]:
    directory = validate_ascii_external_path(
        member.directory,
        label="HYSPLIT meteorology source directory",
    )
    if (
        not member.filename
        or member.filename in {".", ".."}
        or "/" in member.filename
        or "\\" in member.filename
    ):
        raise ValueError("HYSPLIT meteorology filename must be one path component")
    if (
        not directory.is_dir()
        or _is_link_like(directory)
        or directory.resolve(strict=True) != directory
    ):
        raise RuntimeError("HYSPLIT meteorology source directory is missing or linked")
    source = directory / member.filename
    if _is_link_like(source) or not source.is_file():
        raise RuntimeError(f"HYSPLIT meteorology source is missing or linked: {member.filename}")
    payload = source.read_bytes()
    observed_md5 = hashlib.md5(payload, usedforsecurity=False).hexdigest()
    observed_sha256 = hashlib.sha256(payload).hexdigest()
    if (
        len(payload) != member.bytes
        or observed_md5 != member.md5
        or observed_sha256 != member.sha256
    ):
        raise RuntimeError(f"HYSPLIT meteorology identity differs: {member.filename}")
    return source, payload


def _write_payload(path: Path, payload: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def prepare_external_run(
    installation: HysplitInstallation,
    run: Mapping[str, object],
    meteorology: Sequence[MeteorologyFile],
    *,
    work_root: Path,
) -> PreparedRun:
    """Create one isolated external run directory after validating every input."""
    root = validate_ascii_external_path(work_root, label="HYSPLIT external work root")
    if not root.is_dir() or _is_link_like(root) or root.resolve(strict=True) != root:
        raise ValueError("HYSPLIT external work root must be an existing regular directory")
    try:
        root.relative_to(REPO_ROOT.resolve(strict=True))
    except ValueError:
        pass
    else:
        raise ValueError("HYSPLIT work root must be outside the repository")

    raw_run_id = run.get("run_id")
    if not isinstance(raw_run_id, str) or _RUN_ID.fullmatch(raw_run_id) is None:
        raise ValueError("HYSPLIT run_id must be a lowercase ASCII slug")
    run_directory = root / raw_run_id
    if run_directory.exists():
        if run_directory.is_dir() and any(run_directory.iterdir()):
            raise RuntimeError("HYSPLIT per-run directory is non-empty")
        raise RuntimeError("HYSPLIT per-run directory already exists")

    ascdata_payload = _verify_observed(
        installation.ascdata,
        installation.ascdata_identity,
        root=installation.root,
        label="HYSPLIT boundary data",
    )
    sources = [_meteorology_payload(member) for member in meteorology]
    if any(member.filename in _RESERVED_RUN_NAMES for member in meteorology):
        raise ValueError("HYSPLIT meteorology filename collides with a run output")
    staged_members = tuple(
        MeteorologyFile(
            run_directory,
            member.filename,
            member.md5,
            member.sha256,
            member.bytes,
        )
        for member in meteorology
    )
    control = render_trajectory_control(
        run,
        staged_members,
        output_directory=run_directory,
        output_filename="tdump",
    ).encode("ascii")

    run_directory.mkdir()
    try:
        _write_payload(run_directory / "ASCDATA.CFG", ascdata_payload)
        for member, (_, payload) in zip(staged_members, sources, strict=True):
            _write_payload(run_directory / member.filename, payload)
        _write_payload(run_directory / "CONTROL", control)
    except Exception:
        if run_directory.parent == root and run_directory.name == raw_run_id:
            shutil.rmtree(run_directory)
        raise
    duration = run.get("duration_hours")
    if isinstance(duration, bool) or not isinstance(duration, int):
        raise ValueError("HYSPLIT run duration must be an integer")
    return PreparedRun(
        installation=installation,
        run_id=raw_run_id,
        directory=run_directory,
        control_bytes=control,
        control_sha256=hashlib.sha256(control).hexdigest(),
        meteorology=staged_members,
        duration_hours=duration,
    )


def _default_executor(
    command: list[str],
    **kwargs: Any,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, **kwargs)


def _optional_payload(path: Path) -> tuple[bytes | None, str | None]:
    if not path.is_file() or _is_link_like(path):
        return None, None
    payload = path.read_bytes()
    return payload, hashlib.sha256(payload).hexdigest()


def _execution_result(
    prepared: PreparedRun,
    *,
    success: bool,
    problem: str | None,
    returncode: int | None,
    stdout: str,
    stderr: str,
    endpoints: pl.DataFrame | None = None,
) -> HysplitExecution:
    message, message_sha256 = _optional_payload(prepared.directory / "MESSAGE")
    endpoint, endpoint_sha256 = _optional_payload(prepared.directory / "tdump")
    return HysplitExecution(
        success=success,
        problem=problem,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        control_bytes=prepared.control_bytes,
        control_sha256=prepared.control_sha256,
        message_bytes=message,
        message_sha256=message_sha256,
        endpoint_bytes=endpoint,
        endpoint_sha256=endpoint_sha256,
        endpoints=endpoints,
    )


def execute_prepared_run(
    prepared: PreparedRun,
    *,
    executor: Executor = _default_executor,
) -> HysplitExecution:
    """Execute one prepared run without a shell and reject every partial result."""
    try:
        _verify_observed(
            prepared.installation.hyts_std,
            prepared.installation.hyts_std_identity,
            root=prepared.installation.root,
            label="HYSPLIT standard executable",
        )
        _verify_observed(
            prepared.installation.ascdata,
            prepared.installation.ascdata_identity,
            root=prepared.installation.root,
            label="HYSPLIT boundary data",
        )
        control = prepared.directory / "CONTROL"
        if (
            not control.is_file()
            or control.read_bytes() != prepared.control_bytes
            or hashlib.sha256(prepared.control_bytes).hexdigest() != prepared.control_sha256
        ):
            raise RuntimeError("HYSPLIT prepared CONTROL identity changed")
        staged_ascdata = prepared.directory / "ASCDATA.CFG"
        if (
            _is_link_like(staged_ascdata)
            or not staged_ascdata.is_file()
            or len(ascdata_payload := staged_ascdata.read_bytes())
            != prepared.installation.ascdata_identity.bytes
            or hashlib.sha256(ascdata_payload).hexdigest()
            != prepared.installation.ascdata_identity.sha256
        ):
            raise RuntimeError("HYSPLIT prepared boundary data identity changed")
        expected_before = {
            "ASCDATA.CFG",
            "CONTROL",
            *(member.filename for member in prepared.meteorology),
        }
        if {path.name for path in prepared.directory.iterdir()} != expected_before:
            raise RuntimeError("HYSPLIT prepared directory has unexpected members")
        for member in prepared.meteorology:
            _meteorology_payload(member)
    except (OSError, RuntimeError, ValueError) as exc:
        return _execution_result(
            prepared,
            success=False,
            problem=str(exc),
            returncode=None,
            stdout="",
            stderr="",
        )

    environment = {
        name: value
        for name in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP")
        if (value := os.environ.get(name)) is not None
    }
    try:
        completed = executor(
            [str(prepared.installation.hyts_std)],
            cwd=prepared.directory,
            shell=False,
            timeout=300,
            capture_output=True,
            text=True,
            check=False,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _execution_result(
            prepared,
            success=False,
            problem=f"HYSPLIT execution failed: {exc}",
            returncode=None,
            stdout="",
            stderr="",
        )
    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    if completed.returncode != 0:
        return _execution_result(
            prepared,
            success=False,
            problem=f"HYSPLIT process returned exit code {completed.returncode}",
            returncode=completed.returncode,
            stdout=stdout,
            stderr=stderr,
        )

    message_path = prepared.directory / "MESSAGE"
    endpoint_path = prepared.directory / "tdump"
    if not message_path.is_file() or _is_link_like(message_path):
        return _execution_result(
            prepared,
            success=False,
            problem="HYSPLIT MESSAGE output is missing",
            returncode=completed.returncode,
            stdout=stdout,
            stderr=stderr,
        )
    if not endpoint_path.is_file() or _is_link_like(endpoint_path):
        return _execution_result(
            prepared,
            success=False,
            problem="HYSPLIT tdump output is missing",
            returncode=completed.returncode,
            stdout=stdout,
            stderr=stderr,
        )
    expected_after = {
        "ASCDATA.CFG",
        "CONTROL",
        "MESSAGE",
        "tdump",
        *(member.filename for member in prepared.meteorology),
    }
    if {path.name for path in prepared.directory.iterdir()} != expected_after:
        return _execution_result(
            prepared,
            success=False,
            problem="HYSPLIT run directory has unexpected output members",
            returncode=completed.returncode,
            stdout=stdout,
            stderr=stderr,
        )
    try:
        endpoint_text = endpoint_path.read_text(encoding="ascii")
        endpoints = parse_trajectory_endpoints(endpoint_text)
        validate_complete_trajectory(endpoints, duration_hours=prepared.duration_hours)
    except (OSError, UnicodeError, RuntimeError) as exc:
        return _execution_result(
            prepared,
            success=False,
            problem=str(exc),
            returncode=completed.returncode,
            stdout=stdout,
            stderr=stderr,
        )
    return _execution_result(
        prepared,
        success=True,
        problem=None,
        returncode=completed.returncode,
        stdout=stdout,
        stderr=stderr,
        endpoints=endpoints,
    )
