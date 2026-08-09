"""The resumable acquisition boundary for masked MAIAC station-month AOD.

The source and QA fields mirror the Earth Engine catalog contract inspected on
2026-08-10. The tests keep provider absence distinct from an explicitly blank
sample: a missing station row is an error, while a blank value remains null.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import polars as pl
import pytest

from twair.config import ConfigError
from twair.ingest.maiac import (
    EarthEngineMaiacBackend,
    ExportEntry,
    ExportLedger,
    MaiacConfig,
    RemoteTask,
    load_maiac_config,
    plan_exports,
    read_export_ledger,
    refresh_export_status,
    submit_exports,
    write_export_ledger,
)


def valid_config() -> dict[str, object]:
    return {
        "maiac": {
            "collection_id": "MODIS/061/MCD19A2_GRANULES",
            "aod_band": "Optical_Depth_055",
            "qa_band": "AOD_QA",
            "unit": "1",
            "sample_scale_m": 1000,
            "scale_factor": 0.001,
            "qa_shift": 8,
            "qa_mask": 15,
            "qa_best": 0,
            "tile_scale": 4,
            "tiles": ["h28v06", "h29v06"],
            "drive_folder": "twair-earth-engine",
            "description_prefix": "twair_maiac",
            "max_active_tasks": 2,
        }
    }


def stations(*, include_unplaced: bool = False) -> pl.DataFrame:
    rows: list[dict[str, object]] = [
        {"station_name": "二林", "lon": 120.409653, "lat": 23.925175},
        {"station_name": "關山", "lon": 121.161933, "lat": 23.045083},
    ]
    if include_unplaced:
        rows.append({"station_name": "台中", "lon": None, "lat": None})
    return pl.DataFrame(rows)


def moved_stations() -> pl.DataFrame:
    return stations().with_columns(
        pl.when(pl.col("station_name") == "二林")
        .then(pl.lit(120.5))
        .otherwise(pl.col("lon"))
        .alias("lon")
    )


@dataclass
class FakePreparedTask:
    backend: FakeTaskBackend
    entry: ExportEntry

    def start(self) -> RemoteTask:
        attempt = len(self.backend.started) + 1
        if self.backend.fail_on_start == attempt:
            raise RuntimeError(f"injected failure on start {attempt}")
        self.backend.started.append(self.entry.description)
        remote = RemoteTask(
            task_id=f"task-{attempt}",
            description=self.entry.description,
            state="READY",
            error_message=None,
        )
        self.backend.remote.append(remote)
        return remote


@dataclass
class FakeTaskBackend:
    remote: list[RemoteTask] = field(default_factory=list)
    fail_on_start: int | None = None
    started: list[str] = field(default_factory=list)
    prepared_stations: list[list[str]] = field(default_factory=list)

    def list_tasks(self) -> list[RemoteTask]:
        return list(self.remote)

    def prepare_task(
        self,
        entry: ExportEntry,
        config: MaiacConfig,
        station_frame: pl.DataFrame,
    ) -> FakePreparedTask:
        assert config.collection_id == "MODIS/061/MCD19A2_GRANULES"
        self.prepared_stations.append(station_frame["station_name"].to_list())
        return FakePreparedTask(self, entry)


class RecordingDate:
    def __init__(self, recorder: list[tuple[object, ...]], year: int, month: int, day: int) -> None:
        self.recorder = recorder
        self.value = f"{year:04d}-{month:02d}-{day:02d}"

    def advance(self, amount: int, unit: str) -> str:
        self.recorder.append(("date.advance", amount, unit))
        year, month, _ = (int(part) for part in self.value.split("-"))
        if month == 12:
            return f"{year + 1:04d}-01-01"
        return f"{year:04d}-{month + 1:02d}-01"


class RecordingImage:
    def __init__(self, recorder: list[tuple[object, ...]]) -> None:
        self.recorder = recorder
        self.rightShift = self._right_shift
        self.bitwiseAnd = self._bitwise_and
        self.updateMask = self._update_mask
        self.reduceRegions = self._reduce_regions

    def select(self, band: str) -> RecordingImage:
        self.recorder.append(("image.select", band))
        return self

    def _right_shift(self, amount: int) -> RecordingImage:
        self.recorder.append(("image.rightShift", amount))
        return self

    def _bitwise_and(self, mask: int) -> RecordingImage:
        self.recorder.append(("image.bitwiseAnd", mask))
        return self

    def eq(self, value: int) -> RecordingImage:
        self.recorder.append(("image.eq", value))
        return self

    def _update_mask(self, mask: RecordingImage) -> RecordingImage:
        assert isinstance(mask, RecordingImage)
        self.recorder.append(("image.updateMask",))
        return self

    def multiply(self, factor: float) -> RecordingImage:
        self.recorder.append(("image.multiply", factor))
        return self

    def rename(self, name: str) -> RecordingImage:
        self.recorder.append(("image.rename", name))
        return self

    def _reduce_regions(self, **kwargs: object) -> RecordingFeatureCollection:
        self.recorder.append(
            (
                "image.reduceRegions",
                kwargs["scale"],
                kwargs["tileScale"],
                kwargs["reducer"],
            )
        )
        collection = kwargs["collection"]
        assert isinstance(collection, RecordingFeatureCollection)
        return RecordingFeatureCollection(self.recorder, collection.features)


class RecordingFeature:
    def __init__(
        self,
        recorder: list[tuple[object, ...]],
        properties: dict[str, object],
    ) -> None:
        self.recorder = recorder
        self.properties = properties

    def set(self, properties: dict[str, object]) -> RecordingFeature:
        self.recorder.append(("feature.set", properties))
        self.properties.update(properties)
        return self


class RecordingFeatureCollection:
    def __init__(
        self,
        recorder: list[tuple[object, ...]],
        features: list[RecordingFeature],
    ) -> None:
        self.recorder = recorder
        self.features = features

    def map(self, function: Any) -> RecordingFeatureCollection:
        self.recorder.append(("features.map",))
        if self.features:
            function(self.features[0])
        return self


class RecordingCollection:
    def __init__(self, recorder: list[tuple[object, ...]]) -> None:
        self.recorder = recorder
        self.filterDate = self._filter_date

    def filter(self, value: object) -> RecordingCollection:
        self.recorder.append(("collection.filter", value))
        return self

    def _filter_date(self, start: RecordingDate, end: str) -> RecordingCollection:
        self.recorder.append(("collection.filterDate", start.value, end))
        return self

    def map(self, function: Any) -> RecordingCollection:
        self.recorder.append(("collection.map",))
        function(RecordingImage(self.recorder))
        return self

    def size(self) -> str:
        self.recorder.append(("collection.size",))
        return "recorded-image-count"

    def mean(self) -> RecordingImage:
        self.recorder.append(("collection.mean",))
        return RecordingImage(self.recorder)


class RecordingReducer:
    def __init__(self, recorder: list[tuple[object, ...]]) -> None:
        self.recorder = recorder
        self.setOutputs = self._set_outputs

    def _set_outputs(self, outputs: list[str]) -> tuple[str, tuple[str, ...]]:
        self.recorder.append(("reducer.setOutputs", tuple(outputs)))
        return ("first", tuple(outputs))


class RecordingExportTask:
    def __init__(self, config: dict[str, object]) -> None:
        self.config = config
        self.id = "recorded-task"
        self.started = False

    def start(self) -> None:
        self.started = True

    def status(self) -> dict[str, object]:
        return {
            "id": self.id,
            "description": self.config["description"],
            "state": "READY" if self.started else "UNSUBMITTED",
        }


class RecordingEE:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.export_config: dict[str, object] | None = None
        self.export_task: RecordingExportTask | None = None
        self.Initialize = self._initialize
        self.Feature = self._feature
        self.FeatureCollection = self._feature_collection
        self.ImageCollection = self._image_collection
        self.Image = self._image
        self.Geometry = SimpleNamespace(Point=self.point)
        self.Filter = SimpleNamespace(stringContains=self.string_contains, Or=self.or_filter)
        self.Date = SimpleNamespace(fromYMD=self.from_ymd)
        self.Reducer = SimpleNamespace(first=self.first_reducer)
        self.batch = SimpleNamespace(
            Task=SimpleNamespace(list=lambda: []),
            Export=SimpleNamespace(
                table=SimpleNamespace(toDrive=self.to_drive),
            ),
        )

    def _initialize(self, *, project: str) -> None:
        self.calls.append(("initialize", project))

    def point(self, coordinates: list[float]) -> tuple[str, tuple[float, ...]]:
        self.calls.append(("point", tuple(coordinates)))
        return ("point", tuple(coordinates))

    def _feature(
        self,
        geometry: tuple[str, tuple[float, ...]],
        properties: dict[str, object],
    ) -> RecordingFeature:
        self.calls.append(("feature", geometry, properties))
        return RecordingFeature(self.calls, dict(properties))

    def _feature_collection(
        self,
        features: list[RecordingFeature],
    ) -> RecordingFeatureCollection:
        self.calls.append(("feature_collection", len(features)))
        return RecordingFeatureCollection(self.calls, features)

    def _image_collection(self, collection_id: str) -> RecordingCollection:
        self.calls.append(("image_collection", collection_id))
        return RecordingCollection(self.calls)

    def _image(self, image: RecordingImage) -> RecordingImage:
        assert isinstance(image, RecordingImage)
        return image

    def string_contains(self, field_name: str, value: str) -> tuple[str, str, str]:
        self.calls.append(("filter.stringContains", field_name, value))
        return ("contains", field_name, value)

    def or_filter(self, *values: object) -> tuple[str, tuple[object, ...]]:
        self.calls.append(("filter.Or", values))
        return ("or", values)

    def from_ymd(self, year: int, month: int, day: int) -> RecordingDate:
        self.calls.append(("date.fromYMD", year, month, day))
        return RecordingDate(self.calls, year, month, day)

    def first_reducer(self) -> RecordingReducer:
        self.calls.append(("reducer.first",))
        return RecordingReducer(self.calls)

    def to_drive(self, **kwargs: object) -> RecordingExportTask:
        self.calls.append(("export.table.toDrive",))
        self.export_config = dict(kwargs)
        self.export_task = RecordingExportTask(self.export_config)
        return self.export_task


def test_the_maiac_contract_pins_the_catalog_qa_and_scale() -> None:
    config = load_maiac_config()

    assert config.collection_id == "MODIS/061/MCD19A2_GRANULES"
    assert config.aod_band == "Optical_Depth_055"
    assert config.qa_band == "AOD_QA"
    assert (config.qa_shift, config.qa_mask, config.qa_best) == (8, 15, 0)
    assert config.scale_factor == 0.001
    assert config.sample_scale_m == 1000
    assert config.tiles == ("h28v06", "h29v06")


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("sample_scale_m", True, "positive integer"),
        ("qa_shift", False, "non-negative integer"),
        ("qa_mask", True, "positive integer"),
        ("qa_best", False, "non-negative integer"),
        ("tile_scale", True, "positive integer"),
        ("max_active_tasks", True, "integer from 1 through 2"),
    ],
)
def test_a_boolean_cannot_pose_as_a_maiac_integer(
    field: str, value: object, message: str
) -> None:
    raw = valid_config()
    group = raw["maiac"]
    assert isinstance(group, dict)
    group[field] = value

    with pytest.raises(ConfigError, match=message):
        load_maiac_config(raw)


@pytest.mark.parametrize("field", ["collection_id", "aod_band", "qa_band", "unit"])
def test_maiac_source_text_must_be_non_empty(field: str) -> None:
    raw = valid_config()
    group = raw["maiac"]
    assert isinstance(group, dict)
    group[field] = " "

    with pytest.raises(ConfigError, match=f"{field} must be a non-empty string"):
        load_maiac_config(raw)


def test_the_description_prefix_rejects_characters_earth_engine_does_not_accept() -> None:
    raw = valid_config()
    group = raw["maiac"]
    assert isinstance(group, dict)
    group["description_prefix"] = "twair maiac"

    with pytest.raises(ConfigError, match="letters, numbers, hyphens, or underscores"):
        load_maiac_config(raw)


def test_the_two_taiwan_tiles_must_be_unique_non_empty_strings() -> None:
    raw = valid_config()
    group = raw["maiac"]
    assert isinstance(group, dict)
    group["tiles"] = ["h28v06", "h28v06"]

    with pytest.raises(ConfigError, match="two unique non-empty strings"):
        load_maiac_config(raw)


def test_each_planned_month_has_a_deterministic_earth_engine_safe_name() -> None:
    ledger = plan_exports(
        stations(),
        project="test-project",
        year=2025,
        months=(1, 12),
        planned_at="2026-08-10T01:02:03+00:00",
    )

    assert [entry.month for entry in ledger.entries] == [1, 12]
    assert ledger.entries[0].description.startswith("twair_maiac_2025_01_")
    assert ledger.entries[0].description == ledger.entries[0].file_name_prefix
    assert ledger.entries[0].state == "PLANNED"
    assert ledger.entries[0].task_id is None
    assert all(
        character.isascii() and (character.isalnum() or character in "-_")
        for entry in ledger.entries
        for character in entry.description
    )


def test_the_plan_name_changes_when_the_station_inventory_changes() -> None:
    first = plan_exports(stations(), project="test-project", year=2025, months=(1,))
    second = plan_exports(moved_stations(), project="test-project", year=2025, months=(1,))

    assert first.station_inventory_sha256 != second.station_inventory_sha256
    assert first.entries[0].description != second.entries[0].description


def test_station_input_order_does_not_change_the_inventory_hash_or_plan_name() -> None:
    ordered = plan_exports(stations(), project="test-project", year=2025, months=(1,))
    reversed_frame = stations().reverse()
    reversed_plan = plan_exports(
        reversed_frame,
        project="test-project",
        year=2025,
        months=(1,),
    )

    assert ordered.station_inventory_sha256 == reversed_plan.station_inventory_sha256
    assert ordered.entries[0].description == reversed_plan.entries[0].description


def test_an_unplaced_station_is_counted_but_not_hashed_as_a_remote_point() -> None:
    placed = plan_exports(stations(), project="test-project", year=2025, months=(1,))
    with_unplaced = plan_exports(
        stations(include_unplaced=True),
        project="test-project",
        year=2025,
        months=(1,),
    )

    assert with_unplaced.stations_total == 3
    assert with_unplaced.stations_with_coordinates == 2
    assert with_unplaced.stations_without_coordinates == 1
    assert with_unplaced.station_inventory_sha256 == placed.station_inventory_sha256
    assert with_unplaced.entries[0].description == placed.entries[0].description


def test_a_nonfinite_coordinate_fails_even_when_the_other_coordinate_is_null() -> None:
    station_frame = stations().with_columns(
        pl.when(pl.col("station_name") == "關山")
        .then(pl.lit(float("inf")))
        .otherwise(pl.col("lon"))
        .alias("lon"),
        pl.when(pl.col("station_name") == "關山")
        .then(pl.lit(None, dtype=pl.Float64))
        .otherwise(pl.col("lat"))
        .alias("lat"),
    )

    with pytest.raises(RuntimeError, match="not finite"):
        plan_exports(station_frame, project="test-project", year=2025, months=(1,))


def test_duplicate_station_names_fail_before_a_plan_is_created() -> None:
    duplicated = pl.concat([stations(), stations().head(1)])

    with pytest.raises(RuntimeError, match="not unique"):
        plan_exports(duplicated, project="test-project", year=2025, months=(1,))


@pytest.mark.parametrize("bad_months", [(), (0,), (13,), (1, 1), (1, True)])
def test_invalid_or_duplicate_months_fail_before_a_plan_is_created(
    bad_months: Any,
) -> None:
    with pytest.raises(ValueError, match="months"):
        plan_exports(
            stations(),
            project="test-project",
            year=2025,
            months=bad_months,
        )


def test_the_default_ledger_path_stays_below_the_ignored_data_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TWAIR_DATA_DIR", str(tmp_path))
    ledger = plan_exports(stations(), project="test-project", year=2025, months=(1,))

    assert ledger.default_path == tmp_path / "interim" / "maiac" / "year=2025" / "export-ledger.json"


def test_the_export_ledger_round_trips_all_local_intent(tmp_path: Path) -> None:
    ledger = plan_exports(
        stations(include_unplaced=True),
        project="test-project",
        year=2025,
        months=(1, 2),
        planned_at="2026-08-10T01:02:03+00:00",
    )
    destination = tmp_path / "year=2025" / "export-ledger.json"

    written = write_export_ledger(ledger, destination=destination)

    assert written == destination
    assert read_export_ledger(written) == ledger
    payload = json.loads(written.read_text(encoding="utf-8"))
    assert payload["stations_without_coordinates"] == 1
    assert payload["entries"][0]["state"] == "PLANNED"


def test_replanning_the_same_month_preserves_the_remote_task_identity(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "export-ledger.json"
    first = plan_exports(stations(), project="test-project", year=2025, months=(1, 2))
    first.entries[0].task_id = "task-1"
    first.entries[0].state = "READY"
    first.entries[0].submitted_at = "2026-08-10T02:00:00+00:00"
    first.entries[0].updated_at = "2026-08-10T02:00:01+00:00"
    write_export_ledger(first, destination=destination)

    write_export_ledger(
        plan_exports(stations(), project="test-project", year=2025, months=(1, 2)),
        destination=destination,
    )

    merged = read_export_ledger(destination)
    assert merged.entries[0].task_id == "task-1"
    assert merged.entries[0].state == "READY"
    assert merged.entries[0].submitted_at == "2026-08-10T02:00:00+00:00"
    assert merged.entries[1].state == "PLANNED"


def test_planning_an_additional_month_preserves_the_existing_month(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "export-ledger.json"
    january = plan_exports(stations(), project="test-project", year=2025, months=(1,))
    january.entries[0].task_id = "task-1"
    january.entries[0].state = "COMPLETED"
    write_export_ledger(january, destination=destination)

    write_export_ledger(
        plan_exports(stations(), project="test-project", year=2025, months=(2,)),
        destination=destination,
    )

    merged = read_export_ledger(destination)
    assert merged.months == [1, 2]
    assert [entry.state for entry in merged.entries] == ["COMPLETED", "PLANNED"]


def test_a_changed_station_inventory_cannot_merge_into_an_existing_ledger(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "export-ledger.json"
    write_export_ledger(
        plan_exports(stations(), project="test-project", year=2025, months=(1,)),
        destination=destination,
    )

    with pytest.raises(RuntimeError, match="station inventory"):
        write_export_ledger(
            plan_exports(moved_stations(), project="test-project", year=2025, months=(2,)),
            destination=destination,
        )


def test_a_changed_source_contract_cannot_merge_into_an_existing_ledger(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "export-ledger.json"
    base = load_maiac_config()
    write_export_ledger(
        plan_exports(
            stations(),
            project="test-project",
            year=2025,
            months=(1,),
            config=base,
        ),
        destination=destination,
    )

    with pytest.raises(RuntimeError, match="source contract"):
        write_export_ledger(
            plan_exports(
                stations(),
                project="test-project",
                year=2025,
                months=(2,),
                config=replace(base, tile_scale=2),
            ),
            destination=destination,
        )


def test_an_interrupted_ledger_swap_restores_the_previous_file_before_merging(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "export-ledger.json"
    first = plan_exports(stations(), project="test-project", year=2025, months=(1,))
    first.entries[0].task_id = "task-1"
    first.entries[0].state = "READY"
    write_export_ledger(first, destination=destination)
    backup = destination.with_name(f".{destination.name}.backup-interrupted")
    destination.replace(backup)

    write_export_ledger(
        plan_exports(stations(), project="test-project", year=2025, months=(2,)),
        destination=destination,
    )

    restored = read_export_ledger(destination)
    assert restored.months == [1, 2]
    assert restored.entries[0].task_id == "task-1"
    assert not backup.exists()


def test_a_failed_ledger_generation_swap_restores_the_previous_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "export-ledger.json"
    write_export_ledger(
        plan_exports(stations(), project="test-project", year=2025, months=(1,)),
        destination=destination,
    )
    baseline = destination.read_bytes()
    real_replace = Path.replace
    replace_calls = 0

    def fail_second_replace(path: Path, target: Path) -> Path:
        nonlocal replace_calls
        replace_calls += 1
        if replace_calls == 2:
            raise OSError("injected ledger swap failure")
        return real_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_second_replace)

    with pytest.raises(OSError, match="injected ledger swap failure"):
        write_export_ledger(
            plan_exports(stations(), project="test-project", year=2025, months=(2,)),
            destination=destination,
        )

    assert destination.read_bytes() == baseline


def test_multiple_interrupted_ledger_backups_are_ambiguous(tmp_path: Path) -> None:
    destination = tmp_path / "export-ledger.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.with_name(f".{destination.name}.backup-one").write_text("{}", encoding="utf-8")
    destination.with_name(f".{destination.name}.backup-two").write_text("{}", encoding="utf-8")

    with pytest.raises(RuntimeError, match="multiple interrupted MAIAC ledger backups"):
        write_export_ledger(
            plan_exports(stations(), project="test-project", year=2025, months=(1,)),
            destination=destination,
        )


def test_an_invalid_remote_state_cannot_be_written_as_if_it_were_authoritative(
    tmp_path: Path,
) -> None:
    ledger = plan_exports(stations(), project="test-project", year=2025, months=(1,))
    ledger.entries[0].state = "done-ish"

    with pytest.raises(RuntimeError, match="unsupported MAIAC task state"):
        write_export_ledger(ledger, destination=tmp_path / "export-ledger.json")


def test_mutating_a_copy_does_not_change_the_original_plan_fixture() -> None:
    original = plan_exports(stations(), project="test-project", year=2025, months=(1,))
    copied = copy.deepcopy(original)
    copied.entries[0].state = "READY"

    assert original.entries[0].state == "PLANNED"


def test_an_existing_remote_description_is_reconciled_not_resubmitted() -> None:
    ledger = plan_exports(stations(), project="test-project", year=2025, months=(1,))
    backend = FakeTaskBackend(
        remote=[
            RemoteTask(
                task_id="task-existing",
                description=ledger.entries[0].description,
                state="READY",
                error_message=None,
            )
        ]
    )

    updated = submit_exports(
        ledger,
        stations(),
        backend=backend,
        confirm=True,
        updated_at="2026-08-10T03:00:00+00:00",
    )

    assert backend.started == []
    assert updated.entries[0].task_id == "task-existing"
    assert updated.entries[0].state == "READY"


def test_account_wide_active_tasks_consume_the_local_submission_budget() -> None:
    ledger = plan_exports(stations(), project="test-project", year=2025, months=(1, 2))
    backend = FakeTaskBackend(
        remote=[
            RemoteTask(
                task_id="other",
                description="another_project_task",
                state="RUNNING",
                error_message=None,
            )
        ]
    )

    updated = submit_exports(
        ledger,
        stations(),
        backend=backend,
        confirm=True,
        updated_at="2026-08-10T03:00:00+00:00",
    )

    assert len(backend.started) == 1
    assert [entry.state for entry in updated.entries] == ["READY", "PLANNED"]


def test_submit_refuses_without_the_explicit_drive_confirmation() -> None:
    ledger = plan_exports(stations(), project="test-project", year=2025, months=(1,))
    backend = FakeTaskBackend()

    with pytest.raises(RuntimeError, match="confirm-drive-export"):
        submit_exports(ledger, stations(), backend=backend, confirm=False)

    assert backend.started == []
    assert backend.remote == []


def test_duplicate_remote_descriptions_stop_before_any_submission() -> None:
    ledger = plan_exports(stations(), project="test-project", year=2025, months=(1, 2))
    duplicate = RemoteTask(
        task_id="task-one",
        description=ledger.entries[0].description,
        state="READY",
        error_message=None,
    )
    backend = FakeTaskBackend(remote=[duplicate, replace(duplicate, task_id="task-two")])

    with pytest.raises(RuntimeError, match="duplicate remote MAIAC task description"):
        submit_exports(ledger, stations(), backend=backend, confirm=True)

    assert backend.started == []


def test_a_second_start_failure_keeps_the_first_task_id_in_a_durable_snapshot() -> None:
    ledger = plan_exports(stations(), project="test-project", year=2025, months=(1, 2))
    backend = FakeTaskBackend(fail_on_start=2)
    snapshots: list[ExportLedger] = []

    with pytest.raises(RuntimeError, match="injected failure on start 2"):
        submit_exports(
            ledger,
            stations(),
            backend=backend,
            confirm=True,
            updated_at="2026-08-10T03:00:00+00:00",
            persist=lambda value: snapshots.append(copy.deepcopy(value)),
        )

    assert len(snapshots) == 1
    assert snapshots[0].entries[0].task_id == "task-1"
    assert snapshots[0].entries[1].task_id is None


def test_a_failed_task_is_reported_but_never_retried_implicitly() -> None:
    ledger = plan_exports(stations(), project="test-project", year=2025, months=(1, 2))
    ledger.entries[0].task_id = "task-failed"
    ledger.entries[0].state = "FAILED"
    ledger.entries[0].error_message = "provider computation failed"
    backend = FakeTaskBackend(
        remote=[
            RemoteTask(
                task_id="task-failed",
                description=ledger.entries[0].description,
                state="FAILED",
                error_message="provider computation failed",
            )
        ]
    )

    updated = submit_exports(ledger, stations(), backend=backend, confirm=True)

    assert backend.started == [ledger.entries[1].description]
    assert updated.entries[0].state == "FAILED"
    assert updated.entries[0].task_id == "task-failed"


def test_only_coordinate_bearing_stations_reach_the_task_backend() -> None:
    ledger = plan_exports(
        stations(include_unplaced=True),
        project="test-project",
        year=2025,
        months=(1,),
    )
    backend = FakeTaskBackend()

    submit_exports(ledger, stations(include_unplaced=True), backend=backend, confirm=True)

    assert backend.prepared_stations == [["二林", "關山"]]


def test_submission_rejects_a_station_inventory_that_differs_from_the_plan() -> None:
    ledger = plan_exports(stations(), project="test-project", year=2025, months=(1,))
    backend = FakeTaskBackend()

    with pytest.raises(RuntimeError, match="station inventory"):
        submit_exports(ledger, moved_stations(), backend=backend, confirm=True)

    assert backend.started == []


def test_submission_rejects_a_changed_unplaced_station_count() -> None:
    ledger = plan_exports(stations(), project="test-project", year=2025, months=(1,))
    backend = FakeTaskBackend()

    with pytest.raises(RuntimeError, match="station counts"):
        submit_exports(
            ledger,
            stations(include_unplaced=True),
            backend=backend,
            confirm=True,
        )

    assert backend.started == []


def test_status_refresh_recovers_a_remote_task_after_a_local_save_was_missed() -> None:
    ledger = plan_exports(stations(), project="test-project", year=2025, months=(1,))
    backend = FakeTaskBackend(
        remote=[
            RemoteTask(
                task_id="task-recovered",
                description=ledger.entries[0].description,
                state="RUNNING",
                error_message=None,
            )
        ]
    )

    updated = refresh_export_status(
        ledger,
        backend=backend,
        updated_at="2026-08-10T03:00:00+00:00",
    )

    assert updated.entries[0].task_id == "task-recovered"
    assert updated.entries[0].state == "RUNNING"
    assert backend.started == []


def test_a_known_task_absent_from_the_remote_list_becomes_unknown_not_completed() -> None:
    ledger = plan_exports(stations(), project="test-project", year=2025, months=(1,))
    ledger.entries[0].task_id = "task-old"
    ledger.entries[0].state = "RUNNING"

    updated = refresh_export_status(
        ledger,
        backend=FakeTaskBackend(),
        updated_at="2026-08-10T03:00:00+00:00",
    )

    assert updated.entries[0].state == "UNKNOWN"
    assert updated.entries[0].task_id == "task-old"


def test_an_unsupported_provider_state_never_enters_the_local_ledger() -> None:
    ledger = plan_exports(stations(), project="test-project", year=2025, months=(1,))
    backend = FakeTaskBackend(
        remote=[
            RemoteTask(
                task_id="task-strange",
                description=ledger.entries[0].description,
                state="ALMOST_DONE",
                error_message=None,
            )
        ]
    )

    with pytest.raises(RuntimeError, match="unsupported remote MAIAC task state"):
        refresh_export_status(ledger, backend=backend)


def test_the_earth_engine_backend_builds_the_reviewed_monthly_export_unstarted() -> None:
    recording = RecordingEE()
    config = load_maiac_config()
    ledger = plan_exports(
        stations(),
        project="test-project",
        year=2025,
        months=(1,),
        config=config,
    )
    backend = EarthEngineMaiacBackend("test-project", ee_module=recording)

    prepared = backend.prepare_task(ledger.entries[0], config, stations())

    assert recording.export_task is not None
    assert recording.export_task.started is False
    assert ("initialize", "test-project") in recording.calls
    assert ("image_collection", "MODIS/061/MCD19A2_GRANULES") in recording.calls
    assert ("filter.stringContains", "system:index", "h28v06") in recording.calls
    assert ("filter.stringContains", "system:index", "h29v06") in recording.calls
    assert ("collection.filterDate", "2025-01-01", "2025-02-01") in recording.calls
    assert ("image.select", "AOD_QA") in recording.calls
    assert ("image.rightShift", 8) in recording.calls
    assert ("image.bitwiseAnd", 15) in recording.calls
    assert ("image.eq", 0) in recording.calls
    assert ("image.select", "Optical_Depth_055") in recording.calls
    assert ("image.multiply", 0.001) in recording.calls
    assert ("image.reduceRegions", 1000, 4, ("first", ("value",))) in recording.calls
    export_config = recording.export_config
    assert export_config is not None
    assert isinstance(export_config["collection"], RecordingFeatureCollection)
    assert {key: value for key, value in export_config.items() if key != "collection"} == {
        "description": ledger.entries[0].description,
        "folder": "twair-earth-engine",
        "fileNamePrefix": ledger.entries[0].file_name_prefix,
        "fileFormat": "CSV",
        "selectors": ["station_name", "year", "month", "value", "source_images"],
    }

    remote = prepared.start()

    assert recording.export_task.started is True
    assert remote == RemoteTask(
        task_id="recorded-task",
        description=ledger.entries[0].description,
        state="READY",
        error_message=None,
    )
