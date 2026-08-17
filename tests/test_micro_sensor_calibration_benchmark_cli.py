"""The benchmark CLI is reproducible and keeps its claim boundary visible."""

from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from twair import cli


def _module() -> Any:
    return importlib.import_module("twair.analysis.micro_sensor_calibration_benchmark")


def _result() -> SimpleNamespace:
    return SimpleNamespace(
        summary={
            "eligible_rows": 271_138,
            "devices": 470,
            "reference_stations": 60,
            "dates": 25,
            "folds": 35,
        },
        manifest={"output_identity_sha256": "a" * 64},
    )


def test_the_benchmark_cli_persists_before_printing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _module()
    events: list[str] = []
    monkeypatch.setattr(module, "run_micro_sensor_calibration_benchmark", lambda: _result())

    def write_result(_result: Any) -> dict[str, Path]:
        events.append("write")
        return {"manifest": tmp_path / "manifest.json"}

    monkeypatch.setattr(module, "write_micro_sensor_calibration_benchmark_result", write_result)
    monkeypatch.setattr(cli.console, "print", lambda *_args, **_kwargs: events.append("print"))

    result = CliRunner().invoke(cli.app, ["analyze", "micro-sensor-benchmark"])

    assert result.exit_code == 0
    assert events[0] == "write"
    assert "print" in events[1:]


def test_the_benchmark_cli_describes_grouped_prediction_not_calibration_or_fusion(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _module()
    monkeypatch.setattr(module, "run_micro_sensor_calibration_benchmark", lambda: _result())
    monkeypatch.setattr(
        module,
        "write_micro_sensor_calibration_benchmark_result",
        lambda _result: {"manifest": tmp_path / "manifest.json"},
    )

    result = CliRunner().invoke(cli.app, ["analyze", "micro-sensor-benchmark"])

    assert result.exit_code == 0
    assert "held-date" in result.output.lower()
    assert "held-station" in result.output.lower()
    assert "january" in result.output.lower()
    assert "not validated calibration or sensor fusion" in result.output.lower()
