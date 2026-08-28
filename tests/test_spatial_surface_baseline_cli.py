"""CLI contract for the spatial baseline readiness gate."""

from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import polars as pl
import pytest
from typer.testing import CliRunner

from twair import cli


def _module() -> Any:
    return importlib.import_module("twair.analysis.spatial_surface_baseline")


def _result() -> SimpleNamespace:
    """Return deliberately unordered cells so the CLI must render its own order."""
    rows: list[dict[str, object]] = []
    for evaluation in ("spatial_cluster", "buffer_40km", "buffer_20km"):
        for year in (2025, 2024):
            for method in (
                "station_mean",
                "nearest",
                "idw2",
                "kriging_spherical",
                "kriging_hole_effect",
            ):
                rows.append(
                    {
                        "evaluation": evaluation,
                        "year": year,
                        "method": method,
                        "n_intended": 7,
                        "n_scored": 6,
                        "n_failed": 1,
                        "station_clustered_mae": 2.5,
                    }
                )
    return SimpleNamespace(
        scores=pl.DataFrame(rows),
        summary={"gate": {"state": "stop"}},
    )


def test_the_cli_is_plan_only_without_production_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Removing the confirmation guard would compute from an ordinary status command."""
    module = _module()
    monkeypatch.setattr(
        module,
        "run_spatial_surface_baseline",
        lambda **_kwargs: pytest.fail("plan-only mode ran the spatial baseline"),
    )
    monkeypatch.setattr(
        module,
        "write_spatial_surface_baseline_result",
        lambda *_args, **_kwargs: pytest.fail("plan-only mode wrote a generation"),
    )
    monkeypatch.setattr(
        "twair.paths.data_root",
        lambda: pytest.fail("plan-only mode resolved the data root"),
    )

    result = CliRunner().invoke(cli.app, ["analyze", "spatial-surface-baseline"])

    assert result.exit_code == 0
    assert "PLAN ONLY" in result.output
    assert "--confirm-production" in result.output


def test_existing_analysis_commands_still_receive_directory_setup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The plan-only exception must not silently alter other analysis commands."""
    baseline = importlib.import_module("twair.analysis.baseline")
    events: list[str] = []
    monkeypatch.setattr(cli, "ensure_dirs", lambda: events.append("ensure-dirs"))
    monkeypatch.setattr(
        baseline,
        "run_baseline",
        lambda **_kwargs: SimpleNamespace(n=1, n_stations=1, ols=pl.DataFrame()),
    )
    monkeypatch.setattr(
        baseline,
        "write_baseline_report",
        lambda _result: {"report": tmp_path / "report.md"},
    )

    result = CliRunner().invoke(cli.app, ["analyze", "m1"])

    assert result.exit_code == 0
    assert events == ["ensure-dirs"]


def test_the_confirmed_cli_persists_once_before_rendering_all_score_cells(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A display-first change could discard a complete immutable generation."""
    module = _module()
    events: list[str] = []

    def run(*, data_root: Path) -> SimpleNamespace:
        events.append("run")
        assert data_root == tmp_path
        return _result()

    def write(result: SimpleNamespace) -> dict[str, Path]:
        assert result is not None
        events.append("write")
        return {"manifest": tmp_path / "manifest.json"}

    monkeypatch.setattr(module, "run_spatial_surface_baseline", run)
    monkeypatch.setattr(module, "write_spatial_surface_baseline_result", write)
    monkeypatch.setattr("twair.paths.data_root", lambda: tmp_path)
    actual_print = cli.console.print

    def record_print(*args: object, **kwargs: Any) -> None:
        events.append("print")
        actual_print(*args, **kwargs)

    monkeypatch.setattr(cli.console, "print", record_print)

    result = CliRunner().invoke(
        cli.app,
        ["analyze", "spatial-surface-baseline", "--confirm-production"],
    )

    assert result.exit_code == 0
    assert events.count("run") == 1
    assert events.count("write") == 1
    assert events.index("write") < events.index("print")
    assert "gate: stop" in result.output
    assert "no concentration surface" in result.output.lower()
    assert "no population exposure" in result.output.lower()
    assert result.output.count("n_intended=7, n_scored=6, n_failed=1") == 30
    assert result.output.index("buffer_20km 2024 idw2") < result.output.index(
        "buffer_20km 2024 kriging_hole_effect"
    )
    assert "spatial_cluster 2025 station_mean" in result.output
    counts = result.output.index("n_intended=7, n_scored=6, n_failed=1")
    assert counts < result.output.index("MAE=2.500000")


@pytest.mark.parametrize("failure", ["run", "write"])
def test_the_confirmed_cli_does_not_render_a_partial_result_when_work_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: str,
) -> None:
    """A failure before persistence must not look like a reported gate verdict."""
    module = _module()
    events: list[str] = []

    def run(*, data_root: Path) -> SimpleNamespace:
        assert data_root == tmp_path
        events.append("run")
        if failure == "run":
            raise RuntimeError("runner failed")
        return _result()

    def write(_result: SimpleNamespace) -> dict[str, Path]:
        events.append("write")
        if failure == "write":
            raise RuntimeError("writer failed")
        return {"manifest": tmp_path / "manifest.json"}

    monkeypatch.setattr(module, "run_spatial_surface_baseline", run)
    monkeypatch.setattr(module, "write_spatial_surface_baseline_result", write)
    monkeypatch.setattr("twair.paths.data_root", lambda: tmp_path)
    monkeypatch.setattr(cli.console, "print", lambda *_args, **_kwargs: events.append("print"))

    result = CliRunner().invoke(
        cli.app,
        ["analyze", "spatial-surface-baseline", "--confirm-production"],
    )

    assert result.exit_code != 0
    assert events == (["run"] if failure == "run" else ["run", "write"])
