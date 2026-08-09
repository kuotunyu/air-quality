"""The MAIAC CLI keeps local planning separate from external Drive writes."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import polars as pl
import pytest
import typer
from typer.testing import CliRunner

from twair import cli
from twair.config import get_settings
from twair.ingest.maiac import (
    ExportEntry,
    MaiacConfig,
    RemoteTask,
    plan_exports,
    read_export_ledger,
    write_export_ledger,
)


def stations() -> pl.DataFrame:
    return pl.DataFrame(
        [
            {"station_name": "二林", "lon": 120.409653, "lat": 23.925175},
            {"station_name": "關山", "lon": 121.161933, "lat": 23.045083},
        ]
    )


def prepare_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TWAIR_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GEE_PROJECT_ID", "test-project")
    get_settings.cache_clear()
    snapshot = tmp_path / "outputs" / "qc" / "stations.parquet"
    snapshot.parent.mkdir(parents=True)
    stations().write_parquet(snapshot)
    return snapshot


@dataclass
class FakePreparedTask:
    backend: FakeTaskBackend
    entry: ExportEntry

    def start(self) -> RemoteTask:
        remote = RemoteTask(
            task_id=f"task-{self.entry.month}",
            description=self.entry.description,
            state="READY",
            error_message=None,
        )
        self.backend.remote.append(remote)
        self.backend.started.append(self.entry.month)
        return remote


@dataclass
class FakeTaskBackend:
    remote: list[RemoteTask] = field(default_factory=list)
    started: list[int] = field(default_factory=list)

    def list_tasks(self) -> list[RemoteTask]:
        return list(self.remote)

    def prepare_task(
        self,
        entry: ExportEntry,
        config: MaiacConfig,
        station_frame: pl.DataFrame,
    ) -> FakePreparedTask:
        assert config.collection_id == "MODIS/061/MCD19A2_GRANULES"
        assert station_frame["station_name"].to_list() == ["二林", "關山"]
        return FakePreparedTask(self, entry)


def test_plan_writes_local_intent_without_initialising_earth_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepare_environment(tmp_path, monkeypatch)
    from twair.ingest import maiac

    monkeypatch.setattr(
        maiac,
        "EarthEngineMaiacBackend",
        lambda _: pytest.fail("planning initialised the remote backend"),
    )
    try:
        result = CliRunner().invoke(
            cli.app,
            ["ingest", "maiac", "plan", "--year", "2025", "--months", "1,3:4"],
        )
    finally:
        get_settings.cache_clear()

    assert result.exit_code == 0, result.output
    ledger = read_export_ledger(tmp_path / "interim" / "maiac" / "year=2025" / "export-ledger.json")
    assert ledger.months == [1, 3, 4]
    assert all(entry.state == "PLANNED" for entry in ledger.entries)
    assert "3 month(s) planned" in result.output


def test_submit_requires_the_explicit_drive_export_flag_before_remote_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepare_environment(tmp_path, monkeypatch)
    ledger = plan_exports(stations(), project="test-project", year=2025, months=(1,))
    write_export_ledger(ledger)
    from twair.ingest import maiac

    monkeypatch.setattr(
        maiac,
        "EarthEngineMaiacBackend",
        lambda _: pytest.fail("remote access occurred without confirmation"),
    )
    try:
        result = CliRunner().invoke(
            cli.app,
            ["ingest", "maiac", "submit", "--year", "2025"],
        )
    finally:
        get_settings.cache_clear()

    assert result.exit_code != 0
    assert "--confirm-drive-export" in result.output


def test_submit_starts_only_the_configured_account_wide_capacity_and_persists_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepare_environment(tmp_path, monkeypatch)
    ledger = plan_exports(stations(), project="test-project", year=2025, months=(1, 2, 3))
    write_export_ledger(ledger)
    backend = FakeTaskBackend()
    from twair.ingest import maiac

    monkeypatch.setattr(maiac, "EarthEngineMaiacBackend", lambda _: backend)
    try:
        result = CliRunner().invoke(
            cli.app,
            [
                "ingest",
                "maiac",
                "submit",
                "--year",
                "2025",
                "--confirm-drive-export",
            ],
        )
    finally:
        get_settings.cache_clear()

    assert result.exit_code == 0, result.output
    assert backend.started == [1, 2]
    persisted = read_export_ledger(ledger.default_path)
    assert [entry.task_id for entry in persisted.entries] == ["task-1", "task-2", None]
    assert [entry.state for entry in persisted.entries] == ["READY", "READY", "PLANNED"]
    assert "READY: 2" in result.output
    assert "PLANNED: 1" in result.output


def test_status_refreshes_remote_state_without_preparing_a_new_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepare_environment(tmp_path, monkeypatch)
    ledger = plan_exports(stations(), project="test-project", year=2025, months=(1,))
    ledger.entries[0].task_id = "task-1"
    ledger.entries[0].state = "READY"
    write_export_ledger(ledger)
    backend = FakeTaskBackend(
        remote=[
            RemoteTask(
                task_id="task-1",
                description=ledger.entries[0].description,
                state="COMPLETED",
                error_message=None,
            )
        ]
    )
    from twair.ingest import maiac

    monkeypatch.setattr(maiac, "EarthEngineMaiacBackend", lambda _: backend)
    try:
        result = CliRunner().invoke(
            cli.app,
            ["ingest", "maiac", "status", "--year", "2025"],
        )
    finally:
        get_settings.cache_clear()

    assert result.exit_code == 0, result.output
    assert backend.started == []
    assert read_export_ledger(ledger.default_path).entries[0].state == "COMPLETED"
    assert "COMPLETED: 1" in result.output


def test_import_files_accepts_completed_csvs_and_reports_the_persisted_total(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepare_environment(tmp_path, monkeypatch)
    ledger = plan_exports(stations(), project="test-project", year=2025, months=(1,))
    ledger.entries[0].task_id = "task-1"
    ledger.entries[0].state = "COMPLETED"
    write_export_ledger(ledger)
    source_dir = tmp_path / "drive-download"
    source_dir.mkdir()
    (source_dir / f"{ledger.entries[0].file_name_prefix}.csv").write_text(
        "station_name,year,month,value,source_images\n二林,2025,1,0.17,62\n關山,2025,1,,62\n",
        encoding="utf-8",
        newline="\n",
    )
    try:
        result = CliRunner().invoke(
            cli.app,
            [
                "ingest",
                "maiac",
                "import-files",
                "--year",
                "2025",
                "--from-dir",
                str(source_dir),
                "--months",
                "1",
            ],
        )
    finally:
        get_settings.cache_clear()

    assert result.exit_code == 0, result.output
    destination = tmp_path / "interim" / "maiac" / "year=2025" / "result"
    values = pl.read_parquet(destination / "maiac_station_month.parquet")
    assert values.height == 2
    assert values["value"].null_count() == 1
    assert "2 station-month rows" in result.output
    assert "1 masked/null values" in result.output


def test_a_missing_station_snapshot_explains_the_required_preparation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TWAIR_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GEE_PROJECT_ID", "test-project")
    get_settings.cache_clear()
    try:
        with pytest.raises(typer.BadParameter, match="run `twair stations` first"):
            cli.plan_maiac(year=2025, months="1")
    finally:
        get_settings.cache_clear()
