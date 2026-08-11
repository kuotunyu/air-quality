from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest
from click import unstyle
from typer import rich_utils
from typer.testing import CliRunner

from twair import cli
from twair.analysis.era5_robustness import Era5RobustnessResult


def _result() -> Era5RobustnessResult:
    return Era5RobustnessResult(
        scores=pl.DataFrame({"station_name": ["萬里"], "evaluation": ["temporal_transfer"]}),
        deltas=pl.DataFrame({"station_name": ["萬里"], "rmse_delta": [-0.1]}),
        coverage=pl.DataFrame({"year": [2025], "station_name": ["萬里"]}),
        station_folds=pl.DataFrame({"station_name": ["萬里"], "station_fold": [0]}),
        summary={"evaluations": {}},
        manifest={
            "mode": "pilot",
            "common_stations": ["萬里"],
            "paired_rows_by_year": {"2024": 100, "2025": 100},
        },
    )


def test_the_robustness_cli_requires_an_inventory_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rich_utils, "FORCE_TERMINAL", True)
    monkeypatch.setattr(rich_utils, "COLOR_SYSTEM", "standard")
    result = CliRunner().invoke(cli.app, ["analyze", "era5-robustness", "--pilot"])

    assert result.exit_code != 0
    assert "--generation" in unstyle(result.output)


def test_the_robustness_cli_rejects_a_malformed_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "twair.analysis.era5_robustness.run_era5_robustness",
        lambda **_kwargs: pytest.fail("analysis ran with a malformed generation"),
    )
    result = CliRunner().invoke(
        cli.app,
        ["analyze", "era5-robustness", "--generation", "not-a-sha256", "--pilot"],
    )

    assert result.exit_code != 0


def test_the_robustness_cli_persists_before_rendering_results(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    generation = "a" * 64
    events: list[str] = []
    monkeypatch.setattr(
        "twair.ingest.station_inventory.validate_generation_sha256",
        lambda value: value,
    )
    monkeypatch.setattr(
        "twair.analysis.era5_robustness.run_era5_robustness",
        lambda **_kwargs: _result(),
    )

    def write_result(_result: Era5RobustnessResult, **_kwargs: object) -> dict[str, Path]:
        events.append("write")
        return {"manifest": tmp_path / "manifest.json"}

    monkeypatch.setattr(
        "twair.analysis.era5_robustness.write_era5_robustness_result",
        write_result,
    )
    monkeypatch.setattr(cli.console, "print", lambda *_args, **_kwargs: events.append("print"))

    result = CliRunner().invoke(
        cli.app,
        ["analyze", "era5-robustness", "--generation", generation, "--pilot"],
    )

    assert result.exit_code == 0
    assert events[0] == "write"
    assert "print" in events[1:]
