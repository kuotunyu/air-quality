"""The ERA5 value-add CLI makes identity, scope, and persistence explicit."""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest
from typer.testing import CliRunner

from twair import cli
from twair.analysis import era5_value
from twair.analysis.era5_value import Era5ValueResult


def _result() -> Era5ValueResult:
    return Era5ValueResult(
        scores=pl.DataFrame(
            {
                "station_name": ["萬里"],
                "fold": ["q2"],
                "feature_set": ["combined"],
                "rmse": [1.0],
                "r2": [0.5],
            }
        ),
        deltas=pl.DataFrame(
            {
                "station_name": ["萬里"],
                "comparison": ["combined_minus_local"],
                "rmse_delta": [-0.1],
                "r2_delta": [0.02],
            }
        ),
        coverage=pl.DataFrame({"station_name": ["萬里"], "paired_rows": [100]}),
        summary={"comparisons": {"combined_minus_local": {"station_folds": 1}}},
        manifest={
            "schema_version": 1,
            "mode": "pilot",
            "inventory_generation_sha256": "a" * 64,
        },
    )


def test_the_cli_requires_an_explicit_inventory_generation() -> None:
    result = CliRunner().invoke(cli.app, ["analyze", "era5-value", "--pilot"])

    assert result.exit_code != 0
    assert "generation" in result.output.lower()


def test_the_cli_persists_before_display_and_keeps_the_pilot_explicit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    generation = "a" * 64

    def _run(**kwargs: object) -> Era5ValueResult:
        assert kwargs["generation_sha256"] == generation
        assert kwargs["pilot"] is True
        events.append("run")
        return _result()

    def _write(
        result: Era5ValueResult,
        *,
        destination: Path | None = None,
    ) -> dict[str, Path]:
        assert result.manifest["mode"] == "pilot"
        assert destination is None
        events.append("write")
        return {"manifest": tmp_path / "manifest.json"}

    def _print(*_args: object, **_kwargs: object) -> None:
        events.append("print")

    monkeypatch.setattr(era5_value, "run_era5_value", _run)
    monkeypatch.setattr(era5_value, "write_era5_value_result", _write)
    monkeypatch.setattr(cli.console, "print", _print)

    result = CliRunner().invoke(
        cli.app,
        ["analyze", "era5-value", "--generation", generation, "--pilot"],
    )

    assert result.exit_code == 0, result.output
    assert events[:2] == ["run", "write"]
    assert events.index("write") < events.index("print")


def test_a_malformed_generation_is_rejected_before_the_analysis_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        era5_value,
        "run_era5_value",
        lambda **_kwargs: pytest.fail("analysis ran with a malformed generation"),
    )

    result = CliRunner().invoke(
        cli.app,
        ["analyze", "era5-value", "--generation", "not-a-sha256"],
    )

    assert result.exit_code != 0
    assert "64" in result.output or "sha" in result.output.lower()
