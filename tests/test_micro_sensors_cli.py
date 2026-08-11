"""The micro-sensor catalogue CLI makes its small network boundary explicit."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from click import unstyle
from typer import rich_utils
from typer.testing import CliRunner

from twair import cli
from twair.ingest import micro_sensors

from .test_micro_sensors import (
    _ArchiveBackend,
    _directory,
    _file,
    _observation_catalog_snapshot,
    _observation_payloads,
    _station_csv,
)


class FakeHistoryBackend:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __enter__(self) -> FakeHistoryBackend:
        self.calls.append("enter")
        return self

    def __exit__(self, *exc: object) -> None:
        self.calls.append("exit")

    def fetch_station_metadata(self) -> bytes:
        self.calls.append("station_metadata")
        return _station_csv(
            "12796768701,GR0112,,24,120,,,,苗栗縣,108年及109年度苗栗縣空氣品質感測物聯網維運計畫",
            "12783787849,GR0155,,24,120,,,,苗栗縣,108年及109年度苗栗縣空氣品質感測物聯網維運計畫",
        )

    def list_month(self, month: str) -> dict[str, Any]:
        self.calls.append(f"month:{month}")
        return _directory(
            _file("moenv_micro_humidity_20250101.zip", size=207_751_024, time=1_735_754_403),
            _file("moenv_micro_pm25_20250101.zip", size=215_145_898, time=1_735_754_405),
            _file("moenv_micro_temperature_20250101.zip", size=202_534_238, time=1_735_754_404),
        )


class FakeObservationBackend(_ArchiveBackend):
    def __enter__(self) -> FakeObservationBackend:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def test_cli_refuses_network_before_initialising_the_history_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rich_utils, "FORCE_TERMINAL", True)
    monkeypatch.setattr(rich_utils, "COLOR_SYSTEM", "standard")
    monkeypatch.setenv("TWAIR_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(
        micro_sensors,
        "FileGatorHistoryBackend",
        lambda *_args, **_kwargs: pytest.fail("backend initialised without confirmation"),
    )

    result = CliRunner().invoke(
        cli.app,
        ["ingest", "micro-sensor-catalog", "--month", "202501"],
    )

    assert result.exit_code == 2
    assert "--confirm-network" in unstyle(result.output)
    assert not (tmp_path / "raw" / "micro_sensors").exists()


def test_cli_rejects_an_invalid_month_before_initialising_the_history_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TWAIR_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(
        micro_sensors,
        "FileGatorHistoryBackend",
        lambda *_args, **_kwargs: pytest.fail("backend initialised for an invalid month"),
    )

    result = CliRunner().invoke(
        cli.app,
        [
            "ingest",
            "micro-sensor-catalog",
            "--month",
            "2025-01",
            "--confirm-network",
        ],
    )

    assert result.exit_code == 2
    assert "YYYYMM" in unstyle(result.output)


def test_cli_persists_before_reporting_measured_catalog_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TWAIR_DATA_DIR", str(tmp_path))
    backend = FakeHistoryBackend()
    monkeypatch.setattr(
        micro_sensors,
        "FileGatorHistoryBackend",
        lambda *_args, **_kwargs: backend,
    )

    result = CliRunner().invoke(
        cli.app,
        [
            "ingest",
            "micro-sensor-catalog",
            "--month",
            "202501",
            "--confirm-network",
        ],
    )

    output = unstyle(result.output)
    assert result.exit_code == 0, output
    assert backend.calls == ["enter", "station_metadata", "month:202501", "exit"]
    assert "2 station rows" in output
    assert "2 rows in duplicate coordinates" in output
    assert "3 archive files present" in output
    assert "90 archive files absent" in output
    generations = list(
        (tmp_path / "interim" / "micro_sensors" / "catalog" / "generations").iterdir()
    )
    assert len(generations) == 1
    assert (generations[0] / "manifest.json").is_file()


def test_day_cli_refuses_download_before_initialising_the_history_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TWAIR_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(
        micro_sensors,
        "FileGatorHistoryBackend",
        lambda *_args, **_kwargs: pytest.fail("backend initialised without confirmation"),
    )

    result = CliRunner().invoke(
        cli.app,
        [
            "ingest",
            "micro-sensor-day",
            "--catalog-generation",
            "a" * 64,
            "--date",
            "2025-01-01",
        ],
    )

    assert result.exit_code == 2
    assert "--confirm-download" in unstyle(result.output)
    assert not (tmp_path / "raw" / "micro_sensors" / "observations").exists()


def test_day_cli_rejects_a_missing_catalogue_before_initialising_the_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rich_utils, "MAX_WIDTH", 240)
    monkeypatch.setenv("TWAIR_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(
        micro_sensors,
        "FileGatorHistoryBackend",
        lambda *_args, **_kwargs: pytest.fail("backend initialised for a missing catalogue"),
    )

    result = CliRunner().invoke(
        cli.app,
        [
            "ingest",
            "micro-sensor-day",
            "--catalog-generation",
            "a" * 64,
            "--date",
            "2025-01-01",
            "--confirm-download",
        ],
    )

    assert result.exit_code == 2
    assert "catalogue generation is missing" in unstyle(result.output)


def test_day_cli_reports_only_after_all_three_archives_are_persisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TWAIR_DATA_DIR", str(tmp_path))
    payloads = _observation_payloads()
    snapshot = _observation_catalog_snapshot(payloads)
    micro_sensors.write_catalog_snapshot(snapshot)
    backend = FakeObservationBackend(payloads)
    monkeypatch.setattr(
        micro_sensors,
        "FileGatorHistoryBackend",
        lambda *_args, **_kwargs: backend,
    )

    result = CliRunner().invoke(
        cli.app,
        [
            "ingest",
            "micro-sensor-day",
            "--catalog-generation",
            snapshot.generation_sha256,
            "--date",
            "2025-01-01",
            "--confirm-download",
        ],
    )

    output = unstyle(result.output)
    assert result.exit_code == 0, output
    assert len(backend.calls) == 3
    assert "3 archive files" in output
    assert f"{sum(map(len, payloads.values())):,} bytes" in output
    generations = list(
        (tmp_path / "raw" / "micro_sensors" / "observations" / "generations").iterdir()
    )
    assert len(generations) == 1
    assert (generations[0] / "manifest.json").is_file()
