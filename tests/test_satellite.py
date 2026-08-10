"""The first bounded contract for M8's deferred satellite source.

The values below are the January 2025 monthly means sampled at 二林 during the
2026-08-10 feasibility pilot. In particular, the negative SO2 value is not a
synthetic edge case: the Earth Engine catalog says clean regions can be
negative, and the live query returned one here. Filtering it would manufacture
a different dataset.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import polars as pl
import pytest
import typer
from typer.testing import CliRunner

from twair import cli
from twair.config import ConfigError, get_settings
from twair.ingest.satellite import (
    SatelliteResponse,
    SatelliteResult,
    SatelliteSource,
    acquire_s5p,
    load_s5p_sources,
    parse_months,
    write_satellite_result,
)
from twair.ingest.station_inventory import station_inventory_generation

NO2_ERLIN_JAN_2025 = 9.133056758755411e-05
NO2_GUANSHAN_JAN_2025 = 3.559830656738045e-05
NO2_ERLIN_FEB_2025 = 6.603150329454865e-05
SO2_ERLIN_JAN_2025 = -6.765262489262953e-06
SO2_GUANSHAN_JAN_2025 = -0.00023828168757477984
SO2_ERLIN_FEB_2025 = -3.6952021608063694e-05
SO2_GUANSHAN_FEB_2025 = 0.0001764232874846576


def stations(*, include_unplaced: bool = False) -> pl.DataFrame:
    rows: list[dict[str, object]] = [
        {"station_name": "二林", "lon": 120.409653, "lat": 23.925175},
        {"station_name": "關山", "lon": 121.161933, "lat": 23.045083},
    ]
    if include_unplaced:
        rows.append({"station_name": "台中", "lon": None, "lat": None})
    return pl.DataFrame(rows)


def rows_for(source: SatelliteSource, months: tuple[int, ...] = (1,)) -> list[dict[str, object]]:
    measured: dict[str, dict[int, dict[str, float | None]]] = {
        "s5p_no2": {
            1: {"二林": NO2_ERLIN_JAN_2025, "關山": NO2_GUANSHAN_JAN_2025},
            2: {"二林": NO2_ERLIN_FEB_2025, "關山": None},
        },
        "s5p_so2": {
            1: {"二林": SO2_ERLIN_JAN_2025, "關山": SO2_GUANSHAN_JAN_2025},
            2: {"二林": SO2_ERLIN_FEB_2025, "關山": SO2_GUANSHAN_FEB_2025},
        },
    }
    return [
        {"station_name": station_name, "month": month, "value": value}
        for month in months
        for station_name, value in measured[source.key][month].items()
    ]


@dataclass
class FakeBackend:
    override: dict[str, list[dict[str, object]]] = field(default_factory=dict)
    seen_stations: dict[str, list[str]] = field(default_factory=dict)

    def fetch(
        self,
        source: SatelliteSource,
        station_frame: pl.DataFrame,
        *,
        year: int,
        months: tuple[int, ...],
    ) -> SatelliteResponse:
        assert year == 2025
        self.seen_stations[source.key] = station_frame["station_name"].to_list()
        return SatelliteResponse(
            rows=self.override.get(source.key, rows_for(source, months)),
            image_counts={month: {1: 440, 2: 397}[month] for month in months},
            wall_seconds=0.5,
        )


def acquire(
    backend: FakeBackend | None = None,
    *,
    include_unplaced: bool = False,
    months: tuple[int, ...] = (1,),
    station_data: pl.DataFrame | None = None,
    project: str = "twair-air-quality",
    inventory_generation: bool = False,
) -> SatelliteResult:
    source = (
        station_data if station_data is not None else stations(include_unplaced=include_unplaced)
    )
    if inventory_generation:
        return acquire_s5p(
            source,
            backend=backend or FakeBackend(),
            project=project,
            year=2025,
            months=months,
            generated_at="2026-08-10T00:00:00+00:00",
            inventory_generation=True,
        )
    return acquire_s5p(
        source,
        backend=backend or FakeBackend(),
        project=project,
        year=2025,
        months=months,
        generated_at="2026-08-10T00:00:00+00:00",
    )


def test_the_reviewed_source_contract_names_columns_not_ground_concentrations() -> None:
    sources = load_s5p_sources()

    assert [source.key for source in sources] == ["s5p_no2", "s5p_so2"]
    assert sources[0].collection_id == "COPERNICUS/S5P/OFFL/L3_NO2"
    assert sources[0].band == "tropospheric_NO2_column_number_density"
    assert all(source.unit == "mol/m^2" for source in sources)


def test_a_source_contract_rejects_empty_required_text() -> None:
    config = {
        "s5p": {
            "s5p_no2": {
                "collection_id": " ",
                "band": "tropospheric_NO2_column_number_density",
                "unit": "mol/m^2",
                "sample_scale_m": 1113,
                "column_kind": "tropospheric_column",
            }
        }
    }

    with pytest.raises(ConfigError, match="collection_id must be a non-empty string"):
        load_s5p_sources(config)


def test_a_source_contract_rejects_an_empty_source_key() -> None:
    config = {
        "s5p": {
            " ": {
                "collection_id": "COPERNICUS/S5P/OFFL/L3_NO2",
                "band": "tropospheric_NO2_column_number_density",
                "unit": "mol/m^2",
                "sample_scale_m": 1113,
                "column_kind": "tropospheric_column",
            }
        }
    }

    with pytest.raises(ConfigError, match=r"source key.*non-empty string"):
        load_s5p_sources(config)


def test_month_ranges_expand_and_duplicate_months_keep_their_first_position() -> None:
    assert parse_months("1,3,6:8,3") == (1, 3, 6, 7, 8)


def test_invalid_month_specs_fail_before_any_remote_query() -> None:
    with pytest.raises(ValueError, match="1 through 12"):
        parse_months("0,13")
    with pytest.raises(ValueError, match="starts after"):
        parse_months("8:6")


def test_a_masked_satellite_sample_remains_a_null_row() -> None:
    result = acquire(months=(2,))

    assert result.values.height == 4
    assert result.values["value"].null_count() == 1
    assert result.coverage["n_null"].to_list() == [1, 0]


def test_a_negative_column_value_is_not_repaired_or_dropped() -> None:
    result = acquire()
    so2 = result.values.filter((pl.col("source") == "s5p_so2") & (pl.col("station_name") == "二林"))

    assert so2.height == 1
    assert so2["value"][0] == SO2_ERLIN_JAN_2025


def test_a_missing_station_month_fails_instead_of_inventing_a_null() -> None:
    backend = FakeBackend(override={"s5p_no2": rows_for(load_s5p_sources()[0])[:1]})

    with pytest.raises(RuntimeError, match="missing 1 expected row"):
        acquire(backend)


def test_a_duplicate_station_month_fails_before_any_file_is_written() -> None:
    source = load_s5p_sources()[0]
    source_rows = rows_for(source)
    duplicate = [*source_rows, source_rows[0]]
    backend = FakeBackend(override={"s5p_no2": duplicate})

    with pytest.raises(RuntimeError, match="duplicate"):
        acquire(backend)


@pytest.mark.parametrize("provider_month", [1.9, True])
def test_a_provider_month_must_be_an_exact_non_boolean_integer(
    provider_month: object,
) -> None:
    source = load_s5p_sources()[0]
    source_rows = rows_for(source)
    source_rows[0]["month"] = provider_month
    backend = FakeBackend(override={"s5p_no2": source_rows})

    with pytest.raises(RuntimeError, match="exact integer"):
        acquire(backend)


def test_an_unplaced_station_is_counted_but_never_sent_to_earth_engine() -> None:
    backend = FakeBackend()
    result = acquire(backend, include_unplaced=True)

    assert backend.seen_stations == {
        "s5p_no2": ["二林", "關山"],
        "s5p_so2": ["二林", "關山"],
    }
    assert result.manifest["stations_with_coordinates"] == 2
    assert result.manifest["stations_without_coordinates"] == 1


def test_generation_mode_records_the_shared_identity_without_relabelling_legacy_results() -> None:
    source = stations(include_unplaced=True)

    legacy = acquire(include_unplaced=True)
    generated = acquire(include_unplaced=True, inventory_generation=True)

    assert legacy.manifest["schema_version"] == 2
    assert "inventory_generation_sha256" not in legacy.manifest
    assert generated.manifest["schema_version"] == 3
    assert (
        generated.manifest["inventory_generation_sha256"]
        == station_inventory_generation(source).sha256
    )
    assert (
        generated.manifest["station_inventory_sha256"]
        == legacy.manifest["station_inventory_sha256"]
    )


def test_a_nonfinite_coordinate_is_rejected_even_when_the_other_coordinate_is_null() -> None:
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
        acquire_s5p(
            station_frame,
            backend=FakeBackend(),
            project="twair-air-quality",
            year=2025,
            months=(1,),
        )


def test_the_writer_round_trips_nulls_coverage_and_provenance(tmp_path: Path) -> None:
    result = acquire(months=(1, 2))
    destination = tmp_path / "year=2025"

    paths = write_satellite_result(result, destination=destination)

    written = pl.read_parquet(paths["values"])
    coverage = pl.read_parquet(paths["coverage"])
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    assert written.equals(result.values)
    assert coverage.equals(result.coverage)
    assert written["value"].null_count() == 1
    assert manifest["year"] == 2025
    assert manifest["rows"] == 8


def test_a_generation_result_can_only_write_beneath_its_own_full_identity(
    tmp_path: Path,
) -> None:
    result = acquire(inventory_generation=True)
    wrong = tmp_path / "interim" / "satellite" / "generations" / ("0" * 64) / "year=2025"

    with pytest.raises(RuntimeError, match="destination generation does not match"):
        write_satellite_result(result, destination=wrong)

    assert not wrong.exists()


def test_generation_subset_reruns_keep_the_same_generation_identity(tmp_path: Path) -> None:
    first = acquire(months=(1, 2), inventory_generation=True)
    generation = str(first.manifest["inventory_generation_sha256"])
    destination = tmp_path / "generations" / generation / "year=2025"
    write_satellite_result(first, destination=destination)

    paths = write_satellite_result(
        acquire(months=(1,), inventory_generation=True), destination=destination
    )

    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 3
    assert manifest["inventory_generation_sha256"] == generation
    assert manifest["months"] == [1, 2]


def test_a_subset_rerun_replaces_requested_months_without_erasing_other_months(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "year=2025"
    write_satellite_result(acquire(months=(1, 2)), destination=destination)

    paths = write_satellite_result(acquire(months=(1,)), destination=destination)

    written = pl.read_parquet(paths["values"])
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    assert written.height == 8
    assert written["month"].unique().sort().to_list() == [
        date(2025, 1, 1),
        date(2025, 2, 1),
    ]
    assert manifest["months"] == [1, 2]
    assert manifest["rows"] == 8
    assert len(manifest["acquisition_runs"]) == 2


def test_a_subset_rerun_cannot_mix_two_station_coordinate_inventories(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "year=2025"
    write_satellite_result(acquire(months=(1, 2)), destination=destination)
    moved = stations().with_columns(
        pl.when(pl.col("station_name") == "關山")
        .then(pl.col("lon") + 0.01)
        .otherwise(pl.col("lon"))
        .alias("lon")
    )

    with pytest.raises(RuntimeError, match="station coordinate inventory"):
        write_satellite_result(acquire(months=(1,), station_data=moved), destination=destination)


def test_an_interrupted_directory_swap_is_recovered_before_a_subset_rerun(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "year=2025"
    write_satellite_result(acquire(months=(1, 2)), destination=destination)
    interrupted_backup = destination.with_name(f".{destination.name}.backup-interrupted")
    destination.replace(interrupted_backup)

    paths = write_satellite_result(acquire(months=(1,)), destination=destination)

    assert pl.read_parquet(paths["values"]).height == 8
    assert not interrupted_backup.exists()


def test_a_failed_generation_swap_restores_all_previous_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "year=2025"
    paths = write_satellite_result(acquire(months=(1, 2)), destination=destination)
    baseline = {name: path.read_bytes() for name, path in paths.items()}
    real_replace = Path.replace
    replace_calls = 0

    def fail_second_replace(path: Path, target: Path) -> Path:
        nonlocal replace_calls
        replace_calls += 1
        if replace_calls == 2:
            raise OSError("injected generation swap failure")
        return real_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_second_replace)

    with pytest.raises(OSError, match="injected generation swap failure"):
        write_satellite_result(acquire(months=(1,)), destination=destination)

    assert {name: path.read_bytes() for name, path in paths.items()} == baseline


def test_the_cli_reuses_the_current_station_snapshot_instead_of_rescanning_the_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TWAIR_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GEE_PROJECT_ID", "test-project")
    get_settings.cache_clear()
    snapshot = tmp_path / "outputs" / "qc" / "stations.parquet"
    snapshot.parent.mkdir(parents=True)
    stations().write_parquet(snapshot)

    from twair.ingest import satellite
    from twair.store import stations as station_store

    monkeypatch.setattr(satellite, "EarthEngineBackend", lambda _: FakeBackend())
    monkeypatch.setattr(
        station_store,
        "build_station_table",
        lambda: pytest.fail("the acquisition command rescanned the canonical store"),
    )

    try:
        result = CliRunner().invoke(
            cli.app, ["ingest", "satellite", "--year", "2025", "--months", "1"]
        )
    finally:
        get_settings.cache_clear()

    assert result.exit_code == 0, result.output
    assert (tmp_path / "interim" / "satellite" / "year=2025" / "manifest.json").exists()


def test_the_cli_only_uses_an_immutable_generation_path_when_explicitly_requested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TWAIR_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GEE_PROJECT_ID", "test-project")
    get_settings.cache_clear()
    snapshot = tmp_path / "outputs" / "qc" / "stations.parquet"
    snapshot.parent.mkdir(parents=True)
    source = stations(include_unplaced=True)
    source.write_parquet(snapshot)
    generation = station_inventory_generation(source).sha256

    from twair.ingest import satellite

    monkeypatch.setattr(satellite, "EarthEngineBackend", lambda _: FakeBackend())

    try:
        result = CliRunner().invoke(
            cli.app,
            [
                "ingest",
                "satellite",
                "--year",
                "2025",
                "--months",
                "1",
                "--inventory-generation",
            ],
        )
    finally:
        get_settings.cache_clear()

    assert result.exit_code == 0, result.output
    generated = (
        tmp_path
        / "interim"
        / "satellite"
        / "generations"
        / generation
        / "year=2025"
        / "manifest.json"
    )
    assert generated.exists()
    assert not (tmp_path / "interim" / "satellite" / "year=2025").exists()
    assert generation in result.output


def test_the_cli_reports_the_merged_output_after_a_subset_rerun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TWAIR_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GEE_PROJECT_ID", "test-project")
    get_settings.cache_clear()
    snapshot = tmp_path / "outputs" / "qc" / "stations.parquet"
    snapshot.parent.mkdir(parents=True)
    stations().write_parquet(snapshot)
    destination = tmp_path / "interim" / "satellite" / "year=2025"
    write_satellite_result(acquire(months=(1, 2), project="test-project"), destination=destination)

    from twair.ingest import satellite

    monkeypatch.setattr(satellite, "EarthEngineBackend", lambda _: FakeBackend())

    try:
        result = CliRunner().invoke(
            cli.app, ["ingest", "satellite", "--year", "2025", "--months", "1"]
        )
    finally:
        get_settings.cache_clear()

    assert result.exit_code == 0, result.output
    assert "8 station-month rows" in result.output
    assert "1 masked/null values" in result.output


def test_the_cli_rejects_an_unsupported_year_before_loading_data_or_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TWAIR_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda: pytest.fail("settings were loaded before the year was rejected"),
    )

    try:
        with pytest.raises(typer.BadParameter, match="do not cover a complete pre-2018 year"):
            cli.ingest_satellite(year=2017, months="1:12")
    finally:
        get_settings.cache_clear()


def test_the_cli_explains_how_to_create_a_missing_station_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TWAIR_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GEE_PROJECT_ID", "test-project")
    get_settings.cache_clear()

    try:
        with pytest.raises(typer.BadParameter, match="run `twair stations` first"):
            cli.ingest_satellite(year=2025, months="1:12")
    finally:
        get_settings.cache_clear()


def test_the_cli_reports_a_missing_gee_project_before_initialising_the_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TWAIR_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    snapshot = tmp_path / "outputs" / "qc" / "stations.parquet"
    snapshot.parent.mkdir(parents=True)
    stations().write_parquet(snapshot)

    class MissingSettings:
        def require(self, field: str) -> str:
            assert field == "gee_project_id"
            raise RuntimeError("GEE_PROJECT_ID is not configured")

    from twair.ingest import satellite

    monkeypatch.setattr(cli, "get_settings", lambda: MissingSettings())
    monkeypatch.setattr(
        satellite,
        "EarthEngineBackend",
        lambda _: pytest.fail("the backend was initialised without a GEE project"),
    )

    with pytest.raises(typer.BadParameter, match="GEE_PROJECT_ID is not configured"):
        cli.ingest_satellite(year=2025, months="1:12")
