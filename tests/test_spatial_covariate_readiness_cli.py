"""CLI contract for the spatial covariate readiness gate."""

from __future__ import annotations

import importlib
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import polars as pl
import pytest
from typer.testing import CliRunner

from twair import cli


def _module() -> Any:
    return importlib.import_module("twair.analysis.spatial_covariate_readiness")


def _result() -> SimpleNamespace:
    """Return unordered score cells so the CLI must render a stable order."""
    rows: list[dict[str, object]] = []
    for evaluation in ("spatial_cluster", "buffer_40km", "buffer_20km"):
        for training_period, target_year in (("same_year", 2025), ("2024_to_2025", 2025)):
            for method in ("idw2", "covariate_gbm_idw2", "covariate_gbm"):
                rows.append(
                    {
                        "evaluation": evaluation,
                        "training_period": training_period,
                        "target_year": target_year,
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
        "run_spatial_covariate_readiness",
        lambda **_kwargs: pytest.fail("plan-only mode ran the covariate gate"),
    )
    monkeypatch.setattr(
        module,
        "write_spatial_covariate_readiness_result",
        lambda *_args, **_kwargs: pytest.fail("plan-only mode wrote a generation"),
    )
    monkeypatch.setattr(
        "twair.paths.data_root",
        lambda: pytest.fail("plan-only mode resolved the data root"),
    )
    monkeypatch.setattr(
        cli, "ensure_dirs", lambda: pytest.fail("plan-only mode created directories")
    )

    result = CliRunner().invoke(cli.app, ["analyze", "spatial-covariate-readiness"])

    assert result.exit_code == 0
    assert "PLAN ONLY" in result.output
    assert "--confirm-production" in result.output


def test_the_confirmed_cli_persists_once_before_rendering_every_score_cell(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A display-first change could discard a complete immutable generation."""
    module = _module()
    events: list[str] = []
    generation = tmp_path / "generation"

    def run(*, data_root: Path) -> SimpleNamespace:
        events.append("run")
        assert data_root == tmp_path
        return _result()

    def write(result: SimpleNamespace) -> dict[str, Path]:
        assert result is not None
        events.append("write")
        return {"manifest": generation / "manifest.json"}

    monkeypatch.setattr(module, "run_spatial_covariate_readiness", run)
    monkeypatch.setattr(module, "write_spatial_covariate_readiness_result", write)
    monkeypatch.setattr("twair.paths.data_root", lambda: tmp_path)
    actual_print = cli.console.print

    def record_print(*args: object, **kwargs: Any) -> None:
        events.append("print")
        actual_print(*args, **kwargs)

    monkeypatch.setattr(cli.console, "print", record_print)

    result = CliRunner().invoke(
        cli.app,
        ["analyze", "spatial-covariate-readiness", "--confirm-production"],
    )

    assert result.exit_code == 0
    assert events.count("run") == 1
    assert events.count("write") == 1
    assert events.index("write") < events.index("print")
    assert "gate: stop" in result.output
    assert "covariate-model readiness only" in result.output.lower()
    normalized_output = re.sub(r"\s+", " ", result.output)
    assert normalized_output.count("n_intended=7, n_scored=6, n_failed=1") == 18
    assert "buffer_20km 2024_to_2025 2025 covariate_gbm" in normalized_output
    assert normalized_output.index(
        "buffer_20km 2024_to_2025 2025 covariate_gbm"
    ) < normalized_output.index("buffer_20km 2024_to_2025 2025 covariate_gbm_idw2")
    counts = normalized_output.index("n_intended=7, n_scored=6, n_failed=1")
    assert counts < normalized_output.index("MAE=2.500000")
    assert result.output.count("generation:") == 1


@pytest.mark.parametrize("failure", ["run", "write"])
def test_the_confirmed_cli_does_not_render_partial_results_when_work_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: str,
) -> None:
    """A failed run must not look like a reported gate verdict or metric."""
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

    monkeypatch.setattr(module, "run_spatial_covariate_readiness", run)
    monkeypatch.setattr(module, "write_spatial_covariate_readiness_result", write)
    monkeypatch.setattr("twair.paths.data_root", lambda: tmp_path)
    monkeypatch.setattr(cli.console, "print", lambda *_args, **_kwargs: events.append("print"))

    result = CliRunner().invoke(
        cli.app,
        ["analyze", "spatial-covariate-readiness", "--confirm-production"],
    )

    assert result.exit_code != 0
    assert events == (["run"] if failure == "run" else ["run", "write"])
    assert "gate:" not in result.output
    assert "MAE=" not in result.output
