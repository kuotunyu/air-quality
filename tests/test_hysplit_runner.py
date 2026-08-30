"""Offline execution-boundary tests; no NOAA binary is ever invoked."""

from __future__ import annotations

import hashlib
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from twair.analysis.hysplit_io import MeteorologyFile
from twair.analysis.hysplit_runner import (
    HysplitInstallation,
    execute_prepared_run,
    inspect_hysplit_installation,
    prepare_external_run,
)


def _installation(root: Path) -> HysplitInstallation:
    (root / "exec").mkdir(parents=True)
    (root / "bdyfiles").mkdir()
    (root / "working").mkdir()
    (root / "exec" / "hyts_std.exe").write_bytes(b"synthetic-standard-executable")
    (root / "exec" / "hyts_ens.exe").write_bytes(b"synthetic-ensemble-executable")
    (root / "bdyfiles" / "ASCDATA.CFG").write_bytes(b"synthetic-boundary-data")
    (root / "working" / "sample_traj").write_bytes(b"synthetic-sample-meteorology")
    return inspect_hysplit_installation(root)


def _run() -> dict[str, object]:
    return {
        "run_id": "fugueijiao-202503040530-event",
        "arrival_utc": datetime(2025, 3, 4, 5, 30, tzinfo=UTC),
        "latitude": 25.298,
        "longitude": 121.536,
        "start_heights_m_agl": [100, 300, 500],
        "duration_hours": -72,
        "vertical_motion": 0,
        "model_top_m_agl": 10000.0,
        "meteorology_dataset": "gdas1",
    }


def _meteorology(root: Path) -> list[MeteorologyFile]:
    root.mkdir()
    payload = b"synthetic-gdas1-member"
    path = root / "gdas1.mar25.w1"
    path.write_bytes(payload)
    return [
        MeteorologyFile(
            root,
            path.name,
            hashlib.md5(payload, usedforsecurity=False).hexdigest(),
            hashlib.sha256(payload).hexdigest(),
            len(payload),
        )
    ]


def _endpoint(duration: int = -72) -> str:
    arrival = datetime(2025, 3, 4, 5, 30, tzinfo=UTC)
    lines = [
        "1 2",
        "GDAS 25 03 01 00 00",
        "3 BACKWARD OMEGA",
        "25 03 04 05 25.298 121.536 100.00",
        "25 03 04 05 25.298 121.536 300.00",
        "25 03 04 05 25.298 121.536 500.00",
        "1 PRESSURE",
    ]
    for age in range(0, duration - 1, -1):
        point = arrival + timedelta(hours=age)
        for trajectory_id, height in enumerate((100.0, 300.0, 500.0), start=1):
            lines.append(
                f"{trajectory_id} 1 {point:%y %m %d %H %M} 0 {age:.2f} "
                f"25.298 121.536 {height:.2f} 1000.00"
            )
    return "\n".join([*lines, ""])


def test_installation_inspection_is_read_only_and_binds_executable_identity(
    tmp_path: Path,
) -> None:
    installation = _installation(tmp_path / "hysplit")

    assert installation.hyts_std == tmp_path / "hysplit" / "exec" / "hyts_std.exe"
    assert installation.hyts_ens == tmp_path / "hysplit" / "exec" / "hyts_ens.exe"
    assert installation.hyts_std_identity.bytes == len(b"synthetic-standard-executable")
    assert (
        installation.hyts_std_identity.sha256
        == hashlib.sha256(b"synthetic-standard-executable").hexdigest()
    )
    assert installation.hyts_ens_identity.bytes == len(b"synthetic-ensemble-executable")
    assert installation.ascdata == tmp_path / "hysplit" / "bdyfiles" / "ASCDATA.CFG"
    assert installation.ascdata_identity.bytes == len(b"synthetic-boundary-data")
    assert (
        installation.ascdata_identity.sha256
        == hashlib.sha256(b"synthetic-boundary-data").hexdigest()
    )
    assert not list(tmp_path.rglob("*.copied"))


@pytest.mark.parametrize("missing", ["hyts_std.exe", "hyts_ens.exe", "sample_traj", "ASCDATA.CFG"])
def test_installation_inspection_rejects_missing_members(tmp_path: Path, missing: str) -> None:
    root = tmp_path / "hysplit"
    installation = _installation(root)
    if missing == "sample_traj":
        target = root / "working" / missing
    elif missing == "ASCDATA.CFG":
        target = root / "bdyfiles" / missing
    else:
        target = root / "exec" / missing
    target.unlink()
    assert installation.root == root

    with pytest.raises(RuntimeError, match="missing"):
        inspect_hysplit_installation(root)


@pytest.mark.parametrize("leaf", ["hysplit install", "軌跡"])
def test_installation_inspection_rejects_non_ascii_or_spaced_roots(
    tmp_path: Path,
    leaf: str,
) -> None:
    root = tmp_path / leaf
    root.mkdir()

    with pytest.raises(ValueError, match="ASCII-only"):
        inspect_hysplit_installation(root)


def test_installation_inspection_rejects_linked_members(tmp_path: Path) -> None:
    root = tmp_path / "hysplit"
    _installation(root)
    target = root / "exec" / "hyts_std.exe"
    real = root / "exec" / "real.exe"
    target.replace(real)
    try:
        target.symlink_to(real)
    except OSError:
        pytest.skip("Windows symlink privilege is unavailable")

    with pytest.raises(RuntimeError, match="linked"):
        inspect_hysplit_installation(root)


def test_prepare_rejects_a_work_root_inside_the_repository(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import twair.analysis.hysplit_runner as runner

    repository = tmp_path / "repository"
    repository.mkdir()
    work_root = repository / "external"
    work_root.mkdir()
    monkeypatch.setattr(runner, "REPO_ROOT", repository)

    with pytest.raises(ValueError, match="outside the repository"):
        prepare_external_run(
            _installation(tmp_path / "hysplit"),
            _run(),
            _meteorology(tmp_path / "met"),
            work_root=work_root,
        )


def test_prepare_rejects_a_nonempty_per_run_directory(tmp_path: Path) -> None:
    work_root = tmp_path / "work"
    work_root.mkdir()
    run_dir = work_root / str(_run()["run_id"])
    run_dir.mkdir()
    (run_dir / "residue").write_text("old", encoding="ascii")

    with pytest.raises(RuntimeError, match="non-empty"):
        prepare_external_run(
            _installation(tmp_path / "hysplit"),
            _run(),
            _meteorology(tmp_path / "met"),
            work_root=work_root,
        )


def test_injected_execution_uses_no_shell_and_binds_all_outputs(tmp_path: Path) -> None:
    installation = _installation(tmp_path / "hysplit")
    work_root = tmp_path / "work"
    work_root.mkdir()
    prepared = prepare_external_run(
        installation,
        _run(),
        _meteorology(tmp_path / "met"),
        work_root=work_root,
    )

    def fake_executor(
        command: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        assert command == [str(installation.hyts_std)]
        assert kwargs["shell"] is False
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        cwd = kwargs["cwd"]
        assert isinstance(cwd, Path)
        assert {path.name for path in cwd.iterdir()} == {
            "ASCDATA.CFG",
            "CONTROL",
            "gdas1.mar25.w1",
        }
        (cwd / "tdump").write_text(_endpoint(), encoding="ascii")
        (cwd / "MESSAGE").write_text("synthetic success", encoding="ascii")
        (cwd / "TRAJ.CFG").write_text("synthetic trajectory configuration", encoding="ascii")
        (cwd / "WARNING").write_text("synthetic warning record", encoding="ascii")
        return subprocess.CompletedProcess(command, 0, "synthetic stdout", "")

    result = execute_prepared_run(prepared, executor=fake_executor)

    assert result.success is True
    assert result.problem is None
    assert result.returncode == 0
    assert result.stdout == "synthetic stdout"
    assert result.control_bytes == (prepared.directory / "CONTROL").read_bytes()
    assert result.control_sha256 == hashlib.sha256(result.control_bytes).hexdigest()
    assert result.message_bytes == b"synthetic success"
    assert result.message_sha256 == hashlib.sha256(result.message_bytes).hexdigest()
    assert result.endpoint_bytes == (prepared.directory / "tdump").read_bytes()
    assert result.endpoint_sha256 == hashlib.sha256(result.endpoint_bytes).hexdigest()
    assert result.trajectory_config_bytes == b"synthetic trajectory configuration"
    assert (
        result.trajectory_config_sha256
        == hashlib.sha256(result.trajectory_config_bytes).hexdigest()
    )
    assert result.warning_bytes == b"synthetic warning record"
    assert result.warning_sha256 == hashlib.sha256(result.warning_bytes).hexdigest()
    assert result.endpoints is not None
    assert result.endpoints.height == 3 * 73


def test_execution_rejects_boundary_identity_change_after_inspection(
    tmp_path: Path,
) -> None:
    installation = _installation(tmp_path / "hysplit")
    work_root = tmp_path / "work"
    work_root.mkdir()
    prepared = prepare_external_run(
        installation,
        _run(),
        _meteorology(tmp_path / "met"),
        work_root=work_root,
    )
    installation.ascdata.write_bytes(b"changed-after-inspection")

    result = execute_prepared_run(prepared)

    assert result.success is False
    assert result.problem is not None
    assert "boundary data identity changed" in result.problem
    assert result.returncode is None


@pytest.mark.parametrize(
    ("scenario", "problem"),
    [
        ("nonzero", "exit code"),
        ("missing_message", "MESSAGE"),
        ("missing_tdump", "tdump"),
        ("missing_traj_cfg", "TRAJ.CFG"),
        ("missing_warning", "WARNING"),
        ("early", "complete"),
        ("extra", "unexpected"),
    ],
)
def test_execution_marks_partial_or_unexpected_results_failed(
    tmp_path: Path,
    scenario: str,
    problem: str,
) -> None:
    work_root = tmp_path / "work"
    work_root.mkdir()
    prepared = prepare_external_run(
        _installation(tmp_path / "hysplit"),
        _run(),
        _meteorology(tmp_path / "met"),
        work_root=work_root,
    )

    def fake_executor(
        command: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        cwd = kwargs["cwd"]
        assert isinstance(cwd, Path)
        if scenario != "missing_tdump" and scenario != "nonzero":
            endpoint = _endpoint(-2 if scenario == "early" else -72)
            (cwd / "tdump").write_text(endpoint, encoding="ascii")
        if scenario != "missing_message" and scenario != "nonzero":
            (cwd / "MESSAGE").write_text("diagnostic", encoding="ascii")
        if scenario != "missing_traj_cfg" and scenario != "nonzero":
            (cwd / "TRAJ.CFG").write_text("configuration", encoding="ascii")
        if scenario != "missing_warning" and scenario != "nonzero":
            (cwd / "WARNING").write_text("warning", encoding="ascii")
        if scenario == "extra":
            (cwd / "unexpected.bin").write_bytes(b"unexpected")
        return subprocess.CompletedProcess(command, 7 if scenario == "nonzero" else 0, "", "bad")

    result = execute_prepared_run(prepared, executor=fake_executor)

    assert result.success is False
    assert result.problem is not None
    assert problem in result.problem
