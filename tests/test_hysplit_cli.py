"""CLI boundary for the HYSPLIT C0 preparation generation."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from twair import cli
from twair.analysis import hysplit_protocol


def test_hysplit_plan_command_states_counts_and_external_stop_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    reported = SimpleNamespace(
        summary={
            "selected_events": 120,
            "matched_pairs": 54,
            "unmatched_events": 66,
            "standard_runs": 324,
        },
    )
    generation = tmp_path / ("a" * 64)
    generation.mkdir()
    manifest = generation / "manifest.json"
    manifest.write_text("{}", encoding="ascii")
    monkeypatch.setattr(hysplit_protocol, "prepare_hysplit_pilot_plan", lambda: reported)
    monkeypatch.setattr(
        hysplit_protocol,
        "write_hysplit_pilot_plan",
        lambda prepared: {"manifest": manifest},
    )
    monkeypatch.setattr(cli.console, "width", 60)

    result = CliRunner().invoke(cli.app, ["analyze", "hysplit-plan"])

    assert result.exit_code == 0, result.output
    assert "HYSPLIT C0 preparation only" in result.output
    assert "120 selected events; 54 matched pairs; 66 unmatched events" in result.output
    assert "324 standard trajectories planned" in result.output
    assert "no HYSPLIT binary or GDAS1 data downloaded or executed" in result.output
    assert "generation:" in result.output
    assert generation.name in result.output
