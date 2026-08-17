from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from twair import cli


def _module() -> Any:
    return importlib.import_module("twair.analysis.micro_sensor_annual_readiness")


def _result() -> SimpleNamespace:
    return SimpleNamespace(
        summary={
            "calendar": {"complete_dates": 322, "catalogue_absent_dates": 43},
            "devices": 17,
        },
        manifest={"complete": True, "generation_sha256": "a" * 64},
    )


def test_the_annual_micro_sensor_cli_persists_before_reporting_readiness(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _module()
    events: list[str] = []

    def run_and_write(**_kwargs: object) -> tuple[SimpleNamespace, dict[str, Path]]:
        events.append("run-and-write")
        return _result(), {"manifest": tmp_path / "manifest.json"}

    monkeypatch.setattr(module, "run_and_write_annual_micro_sensor_readiness", run_and_write)
    monkeypatch.setattr(
        module,
        "write_annual_micro_sensor_readiness_result",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("split publish escaped")),
    )
    monkeypatch.setattr(cli.console, "print", lambda *_args, **_kwargs: events.append("print"))

    result = CliRunner().invoke(cli.app, ["analyze", "micro-sensor-annual-readiness"])

    assert result.exit_code == 0
    assert events[0] == "run-and-write"
    assert "print" in events[1:]


def test_the_annual_micro_sensor_cli_never_describes_calibration_or_fusion_as_completed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _module()
    monkeypatch.setattr(
        module,
        "run_and_write_annual_micro_sensor_readiness",
        lambda **_kwargs: (_result(), {"manifest": tmp_path / "manifest.json"}),
    )
    monkeypatch.setattr(
        module,
        "write_annual_micro_sensor_readiness_result",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("split publish escaped")),
    )

    result = CliRunner().invoke(cli.app, ["analyze", "micro-sensor-annual-readiness"])

    assert result.exit_code == 0
    assert "322 complete" in result.output
    assert "43 catalogue-absent" in result.output
    assert "readiness evidence only" in result.output.lower()
    assert "not calibration, fusion" in result.output.lower()
