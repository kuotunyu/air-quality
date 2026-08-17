from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import polars as pl
import pytest
from typer.testing import CliRunner

from twair import cli


def _module() -> Any:
    return importlib.import_module("twair.analysis.micro_sensor_calibration")


def _result() -> Any:
    module = _module()
    return module.MicroSensorReadinessResult(
        device_links=pl.DataFrame({"device_id": ["d"]}),
        hourly_pairs=pl.DataFrame({"eligibility_reason": ["eligible"]}),
        coverage=pl.DataFrame({"eligible_pairs": [1]}),
        exclusions=pl.DataFrame({"eligibility_reason": ["eligible"], "rows": [1]}),
        fold_coverage=pl.DataFrame({"fold_kind": ["date"], "rows": [1]}),
        satellite_context=pl.DataFrame({"source": ["maiac_aod"]}),
        summary={"primary": {"eligible_pairs": 1}},
        manifest={"complete": True, "panel_dates": 25, "output_identity_sha256": "a" * 64},
    )


def test_the_micro_sensor_readiness_cli_persists_before_printing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _module()
    events: list[str] = []
    monkeypatch.setattr(
        module, "run_micro_sensor_calibration_readiness", lambda **_kwargs: _result()
    )

    def write_result(_result: Any, **_kwargs: object) -> dict[str, Path]:
        events.append("write")
        return {"manifest": tmp_path / "manifest.json"}

    monkeypatch.setattr(module, "write_micro_sensor_calibration_readiness_result", write_result)
    monkeypatch.setattr(cli.console, "print", lambda *_args, **_kwargs: events.append("print"))

    result = CliRunner().invoke(cli.app, ["analyze", "micro-sensor-readiness"])

    assert result.exit_code == 0
    assert events[0] == "write"
    assert "print" in events[1:]


def test_the_micro_sensor_readiness_cli_describes_readiness_not_fusion(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _module()
    monkeypatch.setattr(
        module, "run_micro_sensor_calibration_readiness", lambda **_kwargs: _result()
    )
    monkeypatch.setattr(
        module,
        "write_micro_sensor_calibration_readiness_result",
        lambda _result, **_kwargs: {"manifest": tmp_path / "manifest.json"},
    )

    result = CliRunner().invoke(cli.app, ["analyze", "micro-sensor-readiness"])

    assert result.exit_code == 0
    assert "readiness" in result.output.lower()
    assert "not calibration or fusion" in result.output.lower()
