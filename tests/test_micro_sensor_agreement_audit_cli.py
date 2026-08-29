from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import polars as pl
import pytest
from typer.testing import CliRunner

from twair import cli


def audit_cli_module(_monkeypatch: pytest.MonkeyPatch) -> Any:
    return importlib.import_module("twair.analysis.micro_sensor_agreement_audit")


def published_audit_result(tmp_path: Path) -> SimpleNamespace:
    generation = "a" * 64
    directory = tmp_path / generation
    directory.mkdir()
    module = importlib.import_module("twair.analysis.micro_sensor_agreement_audit")
    written = {name: directory / name for name in module.AUDIT_MEMBER_NAMES}
    for path in written.values():
        path.write_bytes(b"persisted")
    gate = pl.DataFrame(
        {
            "condition_id": list(module.FUSION_CONDITION_IDS),
            "state": ["fail"] * 6 + ["unmet"],
            "overall_verdict": ["stop"] * 7,
        }
    )
    result = SimpleNamespace(
        manifest={"complete": True, "generation_sha256": generation},
        summary={
            "primary_station_day_rmse": {
                "raw_micro": 4.189404,
                "pooled_micro_ridge": 4.668848,
                "pooled_weather_ridge": 4.720668,
            },
            "secondary_device_day_delta_rmse": {
                "pooled_micro_ridge": -1.327584,
                "pooled_weather_ridge": -1.251138,
            },
            "overall_verdict": "stop",
        },
        fusion_gate=gate,
    )
    return SimpleNamespace(result=result, directory=directory, written=written)


def test_command_defaults_to_plan_without_computation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = audit_cli_module(monkeypatch)
    run = Mock(side_effect=AssertionError("plan invoked production"))
    monkeypatch.setattr(module, "run_and_write_micro_sensor_agreement_audit", run)
    result = CliRunner().invoke(cli.app, ["analyze", "micro-sensor-agreement-audit"])
    assert result.exit_code == 0
    assert "PLAN ONLY" in result.stdout
    assert "station-day primary" in result.stdout
    assert "999" in result.stdout and "1,999" in result.stdout
    assert (
        "c74ec40428a907e98821efbaf36c36386d2c1b99de69791b49f157eb7947e5bb"
        in result.stdout
    )
    assert (
        "df61b34157461f8eca13a119bab88136902aa4e70d8d9794a56a20e422e4c624"
        in result.stdout
    )
    assert (
        "58e00bb5ab951c9afd1a95e9e98aacdab4e90762e32904ca6d79d198efe6d788"
        in result.stdout
    )
    assert "network: disabled" in result.stdout
    run.assert_not_called()


def test_confirmed_command_calls_only_the_locked_run_entry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = audit_cli_module(monkeypatch)
    run = Mock(return_value=published_audit_result(tmp_path))
    monkeypatch.setattr(module, "run_and_write_micro_sensor_agreement_audit", run)
    result = CliRunner().invoke(
        cli.app,
        ["analyze", "micro-sensor-agreement-audit", "--confirm-production"],
    )
    assert result.exit_code == 0
    run.assert_called_once_with()


def test_success_text_reports_primary_scale_and_stop_verdict(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = audit_cli_module(monkeypatch)
    monkeypatch.setattr(
        module,
        "run_and_write_micro_sensor_agreement_audit",
        lambda: published_audit_result(tmp_path),
    )
    result = CliRunner().invoke(
        cli.app,
        ["analyze", "micro-sensor-agreement-audit", "--confirm-production"],
    )
    assert result.stdout.index("station-day") < result.stdout.index("device-day")
    assert "verdict: stop" in result.stdout
    assert "generation:" in result.stdout


def test_failed_run_prints_no_generation_or_success_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = audit_cli_module(monkeypatch)
    monkeypatch.setattr(
        module,
        "run_and_write_micro_sensor_agreement_audit",
        Mock(side_effect=RuntimeError("frozen input changed")),
    )
    result = CliRunner().invoke(
        cli.app,
        ["analyze", "micro-sensor-agreement-audit", "--confirm-production"],
    )
    assert result.exit_code != 0
    assert "generation:" not in result.stdout
    assert "verdict:" not in result.stdout
