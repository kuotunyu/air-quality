"""Strict import of the small CSV tables produced by MAIAC batch exports.

The numeric cells are contract examples, not measured environmental results.
Their shape mirrors the five selectors sent to Earth Engine; a blank `value`
represents a masked sample and must remain distinct from an absent station row.
"""

from __future__ import annotations

import json
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import polars as pl
import pytest

from twair.ingest.maiac import ExportLedger, MaiacConfig, load_maiac_config, plan_exports
from twair.ingest.maiac_import import (
    MaiacResult,
    import_exported_files,
    read_maiac_result,
    write_maiac_result,
)
from twair.ingest.station_inventory import station_inventory_generation


def stations() -> pl.DataFrame:
    return pl.DataFrame(
        [
            {"station_name": "二林", "lon": 120.409653, "lat": 23.925175},
            {"station_name": "關山", "lon": 121.161933, "lat": 23.045083},
        ]
    )


def moved_stations() -> pl.DataFrame:
    return stations().with_columns(
        pl.when(pl.col("station_name") == "二林")
        .then(pl.lit(120.5))
        .otherwise(pl.col("lon"))
        .alias("lon")
    )


def completed_ledger(
    months: tuple[int, ...] = (1,),
    *,
    station_data: pl.DataFrame | None = None,
    config: MaiacConfig | None = None,
    inventory_generation: bool = False,
) -> ExportLedger:
    source = station_data if station_data is not None else stations()
    if inventory_generation:
        ledger = plan_exports(
            source,
            project="test-project",
            year=2025,
            months=months,
            config=config,
            planned_at="2026-08-10T01:02:03+00:00",
            inventory_generation=True,
        )
    else:
        ledger = plan_exports(
            source,
            project="test-project",
            year=2025,
            months=months,
            config=config,
            planned_at="2026-08-10T01:02:03+00:00",
        )
    for entry in ledger.entries:
        entry.task_id = f"task-{entry.month}"
        entry.state = "COMPLETED"
        entry.submitted_at = "2026-08-10T02:00:00+00:00"
        entry.updated_at = "2026-08-10T03:00:00+00:00"
    return ledger


def write_export_csv(
    source_dir: Path,
    ledger: ExportLedger,
    month: int,
    *,
    rows: list[str] | None = None,
    header: str = "station_name,year,month,value,source_images",
) -> Path:
    entry = next(item for item in ledger.entries if item.month == month)
    source_dir.mkdir(parents=True, exist_ok=True)
    default_rows = [
        f"二林,2025,{month},-0.012,62",
        f"關山,2025,{month},,62",
    ]
    path = source_dir / f"{entry.file_name_prefix}.csv"
    path.write_text(
        "\n".join([header, *(rows if rows is not None else default_rows)]) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return path


def imported_month(
    tmp_path: Path,
    month: int,
    *,
    station_data: pl.DataFrame | None = None,
    config: MaiacConfig | None = None,
    inventory_generation: bool = False,
) -> MaiacResult:
    frame = station_data if station_data is not None else stations()
    ledger = completed_ledger(
        (month,),
        station_data=frame,
        config=config,
        inventory_generation=inventory_generation,
    )
    source_dir = tmp_path / f"source-{month}"
    write_export_csv(source_dir, ledger, month)
    return import_exported_files(
        ledger,
        frame,
        source_dir=source_dir,
        months=(month,),
        config=config,
        imported_at=f"2026-08-10T0{month}:00:00+00:00",
    )


def test_a_blank_exported_aod_is_a_null_row_and_a_negative_value_survives(
    tmp_path: Path,
) -> None:
    ledger = completed_ledger()
    source_dir = tmp_path / "exports"
    write_export_csv(source_dir, ledger, 1)

    result = import_exported_files(
        ledger,
        stations(),
        source_dir=source_dir,
        imported_at="2026-08-10T04:00:00+00:00",
    )

    assert result.values["station_name"].to_list() == ["二林", "關山"]
    assert result.values["value"].to_list() == [-0.012, None]
    assert result.coverage["n_valid"].to_list() == [1]
    assert result.coverage["n_null"].to_list() == [1]


def test_a_missing_station_row_is_not_repaired_into_a_null(tmp_path: Path) -> None:
    ledger = completed_ledger()
    source_dir = tmp_path / "exports"
    write_export_csv(source_dir, ledger, 1, rows=["二林,2025,1,0.12,62"])

    with pytest.raises(RuntimeError, match="missing 1 expected station"):
        import_exported_files(ledger, stations(), source_dir=source_dir)


def test_a_duplicate_station_row_fails_before_any_result_is_built(tmp_path: Path) -> None:
    ledger = completed_ledger()
    source_dir = tmp_path / "exports"
    write_export_csv(
        source_dir,
        ledger,
        1,
        rows=[
            "二林,2025,1,0.12,62",
            "二林,2025,1,0.13,62",
            "關山,2025,1,0.14,62",
        ],
    )

    with pytest.raises(RuntimeError, match="duplicate station"):
        import_exported_files(ledger, stations(), source_dir=source_dir)


def test_an_unexpected_station_row_is_not_silently_discarded(tmp_path: Path) -> None:
    ledger = completed_ledger()
    source_dir = tmp_path / "exports"
    write_export_csv(
        source_dir,
        ledger,
        1,
        rows=[
            "二林,2025,1,0.12,62",
            "關山,2025,1,0.14,62",
            "額外站,2025,1,0.16,62",
        ],
    )

    with pytest.raises(RuntimeError, match="unexpected station"):
        import_exported_files(ledger, stations(), source_dir=source_dir)


def test_the_csv_columns_must_match_the_export_selectors_exactly(tmp_path: Path) -> None:
    ledger = completed_ledger()
    source_dir = tmp_path / "exports"
    write_export_csv(
        source_dir,
        ledger,
        1,
        header="station_name,year,month,value",
        rows=["二林,2025,1,0.12", "關山,2025,1,0.14"],
    )

    with pytest.raises(RuntimeError, match="exactly the selectors"):
        import_exported_files(ledger, stations(), source_dir=source_dir)


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        (["二林,2024,1,0.12,62", "關山,2024,1,0.14,62"], "year 2025"),
        (["二林,2025,2,0.12,62", "關山,2025,2,0.14,62"], "month 1"),
        (["二林,2025,1,0.12,62", "關山,2025,1,0.14,63"], "source_images"),
        (["二林,2025,1,NaN,62", "關山,2025,1,0.14,62"], "finite or blank"),
    ],
)
def test_wrong_export_metadata_or_nonfinite_values_fail(
    tmp_path: Path,
    rows: list[str],
    message: str,
) -> None:
    ledger = completed_ledger()
    source_dir = tmp_path / "exports"
    write_export_csv(source_dir, ledger, 1, rows=rows)

    with pytest.raises(RuntimeError, match=message):
        import_exported_files(ledger, stations(), source_dir=source_dir)


def test_multiple_downloads_with_the_same_prefix_are_ambiguous(tmp_path: Path) -> None:
    ledger = completed_ledger()
    source_dir = tmp_path / "exports"
    path = write_export_csv(source_dir, ledger, 1)
    path.with_name(f"{path.stem} duplicate.csv").write_bytes(path.read_bytes())

    with pytest.raises(RuntimeError, match="exactly one CSV"):
        import_exported_files(ledger, stations(), source_dir=source_dir)


def test_a_month_must_be_completed_before_its_file_can_be_imported(tmp_path: Path) -> None:
    ledger = completed_ledger()
    ledger.entries[0].state = "RUNNING"
    source_dir = tmp_path / "exports"
    write_export_csv(source_dir, ledger, 1)

    with pytest.raises(RuntimeError, match="is RUNNING, not COMPLETED"):
        import_exported_files(ledger, stations(), source_dir=source_dir)


def test_the_manifest_carries_the_exact_input_checksum_and_task_id(tmp_path: Path) -> None:
    ledger = completed_ledger()
    source_dir = tmp_path / "exports"
    path = write_export_csv(source_dir, ledger, 1)

    result = import_exported_files(
        ledger,
        stations(),
        source_dir=source_dir,
        imported_at="2026-08-10T04:00:00+00:00",
    )

    input_file = result.manifest["input_files"]["1"]
    assert input_file == {
        "name": path.name,
        "sha256": sha256(path.read_bytes()).hexdigest(),
    }
    assert result.manifest["tasks"] == {"1": "task-1"}
    assert result.manifest["rows"] == 2
    assert result.manifest["null_values"] == 1


def test_the_result_writer_round_trips_values_coverage_and_manifest(tmp_path: Path) -> None:
    result = imported_month(tmp_path, 1)
    destination = tmp_path / "result"

    paths = write_maiac_result(result, destination=destination)

    assert pl.read_parquet(paths["values"]).equals(result.values)
    assert pl.read_parquet(paths["coverage"]).equals(result.coverage)
    assert json.loads(paths["manifest"].read_text(encoding="utf-8"))["months"] == [1]


def test_the_public_reader_returns_only_a_fully_validated_maiac_result(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "result"
    expected = imported_month(tmp_path, 1)
    write_maiac_result(expected, destination=destination)

    observed = read_maiac_result(destination)

    assert observed.values.equals(expected.values)
    assert observed.coverage.equals(expected.coverage)
    assert observed.manifest == expected.manifest

    manifest_path = destination / "manifest.json"
    damaged = json.loads(manifest_path.read_text(encoding="utf-8"))
    damaged["null_values"] += 1
    manifest_path.write_text(json.dumps(damaged), encoding="utf-8")
    with pytest.raises(RuntimeError, match="null count is inconsistent"):
        read_maiac_result(destination)


def test_the_public_maiac_reader_distinguishes_an_absent_result(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="MAIAC result not found"):
        read_maiac_result(tmp_path / "missing")


def test_a_generation_import_keeps_the_ledger_identity_through_its_result_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TWAIR_DATA_DIR", str(tmp_path))
    result = imported_month(tmp_path, 1, inventory_generation=True)
    generation = station_inventory_generation(stations()).sha256

    paths = write_maiac_result(result)

    assert result.manifest["schema_version"] == 2
    assert result.manifest["inventory_generation_sha256"] == generation
    assert paths["manifest"] == (
        tmp_path
        / "interim"
        / "maiac"
        / "generations"
        / generation
        / "year=2025"
        / "result"
        / "manifest.json"
    )


def test_a_generation_import_cannot_write_into_another_generation(
    tmp_path: Path,
) -> None:
    result = imported_month(tmp_path, 1, inventory_generation=True)
    wrong = tmp_path / "generations" / ("0" * 64) / "year=2025" / "result"

    with pytest.raises(RuntimeError, match="destination generation does not match"):
        write_maiac_result(result, destination=wrong)

    assert not wrong.exists()


def test_a_malformed_result_generation_fails_as_a_manifest_contract_before_path_selection(
    tmp_path: Path,
) -> None:
    result = imported_month(tmp_path, 1, inventory_generation=True)
    result.manifest["inventory_generation_sha256"] = "not-a-sha256"

    with pytest.raises(RuntimeError, match="inventory generation is invalid"):
        write_maiac_result(result)


def test_a_generation_result_cannot_publish_inconsistent_station_counts(
    tmp_path: Path,
) -> None:
    result = imported_month(tmp_path, 1, inventory_generation=True)
    result.manifest["stations_without_coordinates"] = 1

    with pytest.raises(RuntimeError, match="station counts are inconsistent"):
        write_maiac_result(result)


def test_importing_february_preserves_an_existing_january_result(tmp_path: Path) -> None:
    destination = tmp_path / "result"
    write_maiac_result(imported_month(tmp_path, 1), destination=destination)

    paths = write_maiac_result(imported_month(tmp_path, 2), destination=destination)

    values = pl.read_parquet(paths["values"])
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    assert values["month"].dt.month().to_list() == [1, 1, 2, 2]
    assert values["value"].to_list() == [-0.012, None, -0.012, None]
    assert manifest["months"] == [1, 2]
    assert manifest["tasks"] == {"1": "task-1", "2": "task-2"}
    assert len(manifest["import_runs"]) == 2


def test_a_partial_import_cannot_cross_a_station_inventory_change(tmp_path: Path) -> None:
    destination = tmp_path / "result"
    write_maiac_result(imported_month(tmp_path, 1), destination=destination)

    with pytest.raises(RuntimeError, match="station inventory"):
        write_maiac_result(
            imported_month(tmp_path, 2, station_data=moved_stations()),
            destination=destination,
        )


def test_a_generation_result_cannot_change_the_unresolved_station_count(
    tmp_path: Path,
) -> None:
    first = imported_month(tmp_path, 1, inventory_generation=True)
    generation = str(first.manifest["inventory_generation_sha256"])
    destination = tmp_path / "generations" / generation / "year=2025" / "result"
    write_maiac_result(first, destination=destination)
    with_unplaced = pl.concat(
        [
            stations(),
            pl.DataFrame([{"station_name": "Unresolved", "lon": None, "lat": None}]),
        ],
        how="diagonal_relaxed",
    )

    with pytest.raises(RuntimeError, match="station counts"):
        write_maiac_result(
            imported_month(
                tmp_path,
                2,
                station_data=with_unplaced,
                inventory_generation=True,
            ),
            destination=destination,
        )


def test_a_partial_import_cannot_cross_a_source_contract_change(tmp_path: Path) -> None:
    destination = tmp_path / "result"
    write_maiac_result(imported_month(tmp_path, 1), destination=destination)
    changed = replace(load_maiac_config(), tile_scale=2)

    with pytest.raises(RuntimeError, match="source contract"):
        write_maiac_result(
            imported_month(tmp_path, 2, config=changed),
            destination=destination,
        )


def test_an_interrupted_result_swap_is_recovered_before_a_partial_import(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "result"
    write_maiac_result(imported_month(tmp_path, 1), destination=destination)
    backup = destination.with_name(f".{destination.name}.backup-interrupted")
    destination.replace(backup)

    paths = write_maiac_result(imported_month(tmp_path, 2), destination=destination)

    assert pl.read_parquet(paths["values"]).height == 4
    assert not backup.exists()


def test_a_failed_result_swap_restores_every_previous_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "result"
    paths = write_maiac_result(imported_month(tmp_path, 1), destination=destination)
    baseline = {name: path.read_bytes() for name, path in paths.items()}
    real_replace = Path.replace
    replace_calls = 0

    def fail_second_replace(path: Path, target: Path) -> Path:
        nonlocal replace_calls
        replace_calls += 1
        if replace_calls == 2:
            raise OSError("injected MAIAC result swap failure")
        return real_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_second_replace)

    with pytest.raises(OSError, match="injected MAIAC result swap failure"):
        write_maiac_result(imported_month(tmp_path, 2), destination=destination)

    assert {name: path.read_bytes() for name, path in paths.items()} == baseline
