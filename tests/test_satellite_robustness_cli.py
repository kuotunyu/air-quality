from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import polars as pl
import pytest
from click import unstyle
from typer import rich_utils
from typer.testing import CliRunner

from twair import cli


def _robustness() -> Any:
    return importlib.import_module("twair.analysis.satellite_robustness")


def _result() -> Any:
    return _robustness().SatelliteRobustnessResult(
        scores=pl.DataFrame({"evaluation": ["year_replication"]}),
        deltas=pl.DataFrame({"comparison": ["all_satellite_minus_baseline"]}),
        coverage=pl.DataFrame({"year": [2024], "common_complete_rows": [12]}),
        station_folds=pl.DataFrame({"station_name": ["s1"], "station_fold": [0]}),
        summary={"evaluations": {}},
        manifest={"years": [2024, 2025], "common_stations": ["s1"]},
    )


def test_the_satellite_robustness_cli_requires_an_inventory_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rich_utils, "FORCE_TERMINAL", True)
    monkeypatch.setattr(rich_utils, "COLOR_SYSTEM", "standard")
    result = CliRunner().invoke(cli.app, ["analyze", "satellite-robustness"])

    assert result.exit_code != 0
    assert "--generation" in unstyle(result.output)


def test_the_satellite_robustness_cli_rejects_a_malformed_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _robustness()
    monkeypatch.setattr(
        module,
        "run_satellite_robustness",
        lambda **_kwargs: pytest.fail("analysis ran with a malformed generation"),
    )

    result = CliRunner().invoke(
        cli.app,
        ["analyze", "satellite-robustness", "--generation", "not-a-sha256"],
    )

    assert result.exit_code != 0


def test_the_satellite_robustness_cli_persists_before_rendering_results(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _robustness()
    generation = "a" * 64
    events: list[str] = []
    monkeypatch.setattr(module, "run_satellite_robustness", lambda **_kwargs: _result())

    def write_result(_result: Any, **_kwargs: object) -> dict[str, Path]:
        events.append("write")
        return {"manifest": tmp_path / "manifest.json"}

    monkeypatch.setattr(module, "write_satellite_robustness_result", write_result)
    monkeypatch.setattr(cli.console, "print", lambda *_args, **_kwargs: events.append("print"))

    result = CliRunner().invoke(
        cli.app,
        ["analyze", "satellite-robustness", "--generation", generation],
    )

    assert result.exit_code == 0
    assert events[0] == "write"
    assert "print" in events[1:]
