"""The resumable acquisition boundary for masked MAIAC station-month AOD.

The source and QA fields mirror the Earth Engine catalog contract inspected on
2026-08-10. The tests keep provider absence distinct from an explicitly blank
sample: a missing station row is an error, while a blank value remains null.
"""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from twair.config import ConfigError
from twair.ingest.maiac import (
    load_maiac_config,
    plan_exports,
    read_export_ledger,
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
