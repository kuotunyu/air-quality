from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest
from typer.testing import CliRunner

from twair import cli
from twair.analysis.satellite_value import SatelliteValueResult


def _result() -> SatelliteValueResult:
    return SatelliteValueResult(
        scores=pl.DataFrame({"evaluation": ["quarter_transfer"]}),
        deltas=pl.DataFrame({"comparison": ["all_satellite_minus_baseline"]}),
        coverage=pl.DataFrame({"common_complete_rows": [12]}),
        station_folds=pl.DataFrame({"station_name": ["富貴角"], "station_fold": [0]}),
        summary={"evaluations": {}},
        manifest={
            "year": 2025,
            "common_complete_rows": 12,
            "evaluations": [
                "quarter_transfer",
                "station_transfer",
                "spatiotemporal_transfer",
            ],
        },
    )


def test_the_satellite_value_cli_requires_an_inventory_generation() -> None:
    result = CliRunner().invoke(cli.app, ["analyze", "satellite-value"])

    assert result.exit_code != 0
    assert "--generation" in result.output


def test_the_satellite_value_cli_rejects_a_malformed_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "twair.analysis.satellite_value.run_satellite_value",
        lambda **_kwargs: pytest.fail("analysis ran with a malformed generation"),
    )
    result = CliRunner().invoke(
        cli.app,
        ["analyze", "satellite-value", "--generation", "not-a-sha256"],
    )

    assert result.exit_code != 0


def test_the_satellite_value_cli_persists_before_rendering_results(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    generation = "a" * 64
    events: list[str] = []
    monkeypatch.setattr(
        "twair.analysis.satellite_value.run_satellite_value",
        lambda **_kwargs: _result(),
    )

    def write_result(_result: SatelliteValueResult, **_kwargs: object) -> dict[str, Path]:
        events.append("write")
        return {"manifest": tmp_path / "manifest.json"}

    monkeypatch.setattr(
        "twair.analysis.satellite_value.write_satellite_value_result",
        write_result,
    )
    monkeypatch.setattr(cli.console, "print", lambda *_args, **_kwargs: events.append("print"))

    result = CliRunner().invoke(
        cli.app,
        ["analyze", "satellite-value", "--generation", generation, "--year", "2025"],
    )

    assert result.exit_code == 0
    assert events[0] == "write"
    assert "print" in events[1:]
