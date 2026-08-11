"""The ERA5 CLI makes expensive remote access explicit and reproducible."""

from __future__ import annotations

from pathlib import Path

import pytest
from click import unstyle
from typer import rich_utils
from typer.testing import CliRunner

from twair import cli
from twair.config import get_settings
from twair.ingest.station_inventory import station_inventory_generation

from .test_era5 import FakeEra5Backend, _stations


def test_cli_refuses_download_without_confirmation_before_initialising_cds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rich_utils, "FORCE_TERMINAL", True)
    monkeypatch.setattr(rich_utils, "COLOR_SYSTEM", "standard")
    monkeypatch.setenv("TWAIR_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    from twair.ingest import era5

    monkeypatch.setattr(
        era5,
        "CdsEra5Backend",
        lambda *_: pytest.fail("CDS was initialised without confirmation"),
    )
    try:
        result = CliRunner().invoke(
            cli.app,
            ["ingest", "era5", "--year", "2025", "--months", "1", "--inventory-generation"],
        )
    finally:
        get_settings.cache_clear()

    assert result.exit_code == 2
    assert "--confirm-download" in unstyle(result.output)


def test_cli_requires_an_immutable_inventory_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rich_utils, "FORCE_TERMINAL", True)
    monkeypatch.setattr(rich_utils, "COLOR_SYSTEM", "standard")
    result = CliRunner().invoke(
        cli.app,
        ["ingest", "era5", "--year", "2025", "--months", "1", "--confirm-download"],
        env={"TWAIR_DATA_DIR": str(tmp_path)},
    )

    assert result.exit_code == 2
    assert "--inventory-generation" in unstyle(result.output)


def test_cli_writes_the_confirmed_generation_from_the_station_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TWAIR_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CDSAPI_KEY", "test-key")
    monkeypatch.setenv("CDSAPI_URL", "https://cds.example/api")
    get_settings.cache_clear()
    snapshot = tmp_path / "outputs" / "qc" / "stations.parquet"
    snapshot.parent.mkdir(parents=True)
    stations = _stations()
    stations.write_parquet(snapshot)
    backend = FakeEra5Backend()
    from twair.ingest import era5

    monkeypatch.setattr(era5, "CdsEra5Backend", lambda *_: backend)
    try:
        result = CliRunner().invoke(
            cli.app,
            [
                "ingest",
                "era5",
                "--year",
                "2025",
                "--months",
                "1",
                "--inventory-generation",
                "--confirm-download",
            ],
        )
    finally:
        get_settings.cache_clear()

    generation = station_inventory_generation(stations).sha256
    destination = tmp_path / "interim" / "era5" / "generations" / generation / "year=2025"
    assert result.exit_code == 0, result.output
    assert backend.calls == [1]
    assert (destination / "manifest.json").exists()
    assert generation in result.output
    assert "1,488 station-hour rows" in result.output
