"""The satellite-context benchmark CLI persists before making bounded claims."""

from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from typer.testing import CliRunner

from twair import cli


def _module() -> Any:
    return importlib.import_module("twair.analysis.micro_sensor_satellite_value")


def _result() -> SimpleNamespace:
    return SimpleNamespace(
        summary={
            "source_rows": 271_138,
            "cohort_rows": 269_952,
            "excluded_rows": 1_186,
            "reference_stations": 58,
            "dates": 25,
            "folds": 35,
        },
        manifest={"output_identity_sha256": "a" * 64},
    )


def test_the_satellite_value_cli_persists_before_printing(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    module = _module()
    events: list[str] = []
    monkeypatch.setattr(module, "run_micro_sensor_satellite_value", lambda: _result())

    def write_result(_result: Any) -> dict[str, Path]:
        events.append("write")
        return {"manifest": tmp_path / "manifest.json"}

    monkeypatch.setattr(module, "write_micro_sensor_satellite_value_result", write_result)
    monkeypatch.setattr(cli.console, "print", lambda *_args, **_kwargs: events.append("print"))

    result = CliRunner().invoke(cli.app, ["analyze", "micro-sensor-satellite-value"])

    assert result.exit_code == 0
    assert events[0] == "write"
    assert "print" in events[1:]


def test_the_satellite_value_cli_names_reference_station_month_context_not_fusion(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    module = _module()
    monkeypatch.setattr(module, "run_micro_sensor_satellite_value", lambda: _result())
    monkeypatch.setattr(
        module,
        "write_micro_sensor_satellite_value_result",
        lambda _result: {"manifest": tmp_path / "manifest.json"},
    )

    result = CliRunner().invoke(cli.app, ["analyze", "micro-sensor-satellite-value"])

    assert result.exit_code == 0
    output = " ".join(result.output.lower().split())
    assert "held-station" in output
    assert "primary" in output
    assert "reference-station monthly context" in output
    assert "not calibration, fusion, or a micro-sensor-location satellite value" in output
