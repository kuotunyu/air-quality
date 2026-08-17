"""Calibration-readiness keeps every measured state visible before any model exists."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import polars as pl
import pytest

from twair.analysis import micro_sensor_calibration as module
from twair.config import ConfigError


def _config() -> module.MicroSensorReadinessConfig:
    return module.MicroSensorReadinessConfig(
        coverage_thresholds=(15, 30, 45, 60),
        primary_minimum_rows=45,
        distance_bands_km=(0.5, 1.0, 2.0, 3.0, 5.0, 10.0),
        primary_distance_km=1.0,
        threads=1,
        extreme_ranges={
            "pm25": (0.0, 1000.0),
            "humidity": (0.0, 100.0),
            "temperature": (-100.0, 100.0),
        },
    )


def _rows(
    device: str,
    hour: int,
    variable: str,
    *,
    count: int = 45,
    lon: float | None = 121.0,
    lat: float | None = 25.0,
    duplicate_last: bool = False,
    extreme: bool = False,
    null_value: bool = False,
) -> list[dict[str, object]]:
    start = datetime(2025, 1, 1, hour)
    rows: list[dict[str, object]] = []
    for index in range(count):
        minute = index if not (duplicate_last and index == count - 1) else index - 1
        rows.append(
            {
                "source_row_number": index + 1,
                "device_id": device,
                "variable": variable,
                "ts_local": start + timedelta(minutes=minute),
                "value": (
                    None if null_value else 1001.0 if extreme and index == 0 else float(index + 1)
                ),
                "lon": lon,
                "lat": lat,
                "coordinate_wgs84_valid": (
                    None
                    if lon is None or lat is None
                    else -180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0
                ),
            }
        )
    return rows


def _write_micro_inputs(tmp_path: Path) -> dict[str, tuple[Path, ...]]:
    all_rows: dict[str, list[dict[str, object]]] = {
        "pm25": [],
        "humidity": [],
        "temperature": [],
    }
    specifications = {
        "eligible": (0, 45, False, False, True, True),
        "ground-withheld": (1, 45, False, False, True, True),
        "ground-absent": (2, 45, False, False, True, True),
        "missing-humidity": (3, 45, False, False, False, True),
        "low-pm": (4, 44, False, False, True, True),
        "duplicate-pm": (5, 45, True, False, True, True),
        "extreme-pm": (6, 45, False, True, True, True),
        "missing-temperature": (7, 45, False, False, True, False),
        "ground-flagged": (11, 45, False, False, True, True),
        "ground-null-flag": (12, 45, False, False, True, True),
    }
    for device, (
        hour,
        pm_count,
        duplicate,
        extreme,
        humidity,
        temperature,
    ) in specifications.items():
        all_rows["pm25"].extend(
            _rows(
                device,
                hour,
                "pm25",
                count=pm_count,
                duplicate_last=duplicate,
                extreme=extreme,
            )
        )
        if humidity:
            all_rows["humidity"].extend(_rows(device, hour, "humidity"))
        if temperature:
            all_rows["temperature"].extend(_rows(device, hour, "temperature"))

    for hour, missing_variable in enumerate(("pm25", "humidity", "temperature"), start=13):
        device = f"null-{missing_variable}"
        for variable in ("pm25", "humidity", "temperature"):
            all_rows[variable].extend(
                _rows(
                    device,
                    hour,
                    variable,
                    null_value=variable == missing_variable,
                )
            )

    all_rows["pm25"].extend(_rows("moving", 8, "pm25", lon=121.0, lat=25.0, count=1))
    all_rows["pm25"].extend(_rows("moving", 8, "pm25", lon=121.1, lat=25.1, count=1))
    all_rows["pm25"].extend(_rows("invalid", 9, "pm25", lon=None, lat=None, count=1))
    all_rows["pm25"].extend(_rows("outside", 10, "pm25", lon=130.0, lat=25.0, count=1))

    outputs: dict[str, tuple[Path, ...]] = {}
    for variable, rows in all_rows.items():
        path = tmp_path / f"{variable}.parquet"
        pl.DataFrame(rows).write_parquet(path)
        outputs[variable] = (path,)
    return outputs


def _geography() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "station_name": ["參考站"],
            "lon": [121.0],
            "lat": [25.0],
            "geo_source": ["current_aqx_p_07"],
            "geo_source_record_namespace": ["MOENV_AQX_P_07"],
            "geo_source_record_id": ["1"],
        }
    )


def _ground(tmp_path: Path) -> Path:
    path = tmp_path / "ground.parquet"
    frame = pl.DataFrame(
        {
            "station_name": ["參考站", "參考站", "參考站", "參考站"],
            "pollutant": ["PM2.5", "PM2.5", "PM2.5", "PM2.5"],
            "ts_local": [
                datetime(2025, 1, 1, 0),
                datetime(2025, 1, 1, 1),
                datetime(2025, 1, 1, 11),
                datetime(2025, 1, 1, 12),
            ],
            "value": [12.0, None, 24.0, 18.0],
            "flag": ["valid", "below_coverage", "rain_present", None],
        },
        schema_overrides={"value": pl.Float64},
    )
    extra = pl.DataFrame(
        {
            "station_name": [frame["station_name"][0]] * 3,
            "pollutant": ["PM2.5"] * 3,
            "ts_local": [datetime(2025, 1, 1, hour) for hour in (13, 14, 15)],
            "value": [12.0] * 3,
            "flag": ["valid"] * 3,
        },
        schema_overrides={"value": pl.Float64},
    )
    pl.concat([frame, extra]).write_parquet(path)
    return path


def _satellite(tmp_path: Path) -> Path:
    path = tmp_path / "satellite.parquet"
    pl.DataFrame(
        {
            "source": ["maiac_aod", "s5p_no2", "s5p_so2"],
            "station_name": ["參考站"] * 3,
            "month": [datetime(2025, 1, 1).date()] * 3,
            "satellite_value": [0.2, 0.3, None],
            "satellite_observed": [True, True, False],
            "pair_observed": [True, True, False],
            "satellite_unit": ["unit"] * 3,
            "collection_id": ["collection"] * 3,
            "band": ["band"] * 3,
            "sample_scale_m": [1000] * 3,
        },
        schema_overrides={"satellite_value": pl.Float64, "month": pl.Date},
    ).write_parquet(path)
    return path


def _result(tmp_path: Path) -> module.MicroSensorReadinessResult:
    return module.prepare_micro_sensor_calibration_readiness(
        micro_paths=_write_micro_inputs(tmp_path),
        geography=_geography(),
        ground_path=_ground(tmp_path),
        satellite_path=_satellite(tmp_path),
        config=_config(),
        temp_dir=tmp_path / "duckdb-temp",
        input_identity={"parsed_generations": [f"{value:064x}" for value in range(1, 26)]},
        generated_at="2026-08-12T00:00:00+00:00",
        git_sha="b" * 40,
        git_dirty=False,
    )


def test_the_reviewed_config_rejects_unknown_or_changed_thresholds() -> None:
    raw: dict[str, Any] = {
        "coverage_thresholds": [15, 30, 45, 60],
        "primary_minimum_rows": 45,
        "distance_bands_km": [0.5, 1, 2, 3, 5, 10],
        "primary_distance_km": 1,
        "threads": 1,
        "extreme_ranges": {
            "pm25": {"minimum": 0, "maximum": 1000},
            "humidity": {"minimum": 0, "maximum": 100},
            "temperature": {"minimum": -100, "maximum": 100},
        },
    }
    assert module.load_micro_sensor_readiness_config({"analysis": raw}) == _config()

    with pytest.raises(ConfigError, match="unknown"):
        module.load_micro_sensor_readiness_config({"analysis": {**raw, "surprise": 1}})
    with pytest.raises(ConfigError, match="coverage_thresholds"):
        module.load_micro_sensor_readiness_config(
            {"analysis": {**raw, "coverage_thresholds": [15, 30, 60]}}
        )
    with pytest.raises(ConfigError, match="threads"):
        module.load_micro_sensor_readiness_config({"analysis": {**raw, "threads": True}})
    with pytest.raises(ConfigError, match="primary_minimum_rows"):
        module.load_micro_sensor_readiness_config(
            {"analysis": {**raw, "primary_minimum_rows": 45.0}}
        )


def test_the_panel_config_requires_25_unique_lowercase_generation_identities() -> None:
    generations = [f"{value:064x}" for value in range(1, 26)]
    raw = {
        "schema_version": 1,
        "catalog_generation_sha256": "a" * 64,
        "parsed_generations": generations,
        "satellite_generation_sha256": "b" * 64,
        "satellite_year": 2025,
    }

    selected = module.load_micro_sensor_panel_config(raw)

    assert selected.parsed_generations == tuple(generations)
    assert selected.satellite_year == 2025
    with pytest.raises(ConfigError, match="25 unique"):
        module.load_micro_sensor_panel_config(
            {**raw, "parsed_generations": [*generations[:-1], generations[0]]}
        )
    with pytest.raises(ConfigError, match="unknown"):
        module.load_micro_sensor_panel_config({**raw, "extra": True})
    with pytest.raises(ConfigError, match="schema_version"):
        module.load_micro_sensor_panel_config({**raw, "schema_version": True})
    with pytest.raises(ConfigError, match="satellite_year"):
        module.load_micro_sensor_panel_config({**raw, "satellite_year": 2025.0})


@pytest.mark.parametrize("failure", ["duplicate", "wrong_order", "outside_january"])
def test_panel_dates_come_from_manifests_and_must_be_unique_sorted_january_days(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    generations = tuple(f"{value:064x}" for value in range(1, 26))
    dates = [datetime(2025, 1, 1).date() + timedelta(days=index) for index in range(25)]
    if failure == "duplicate":
        dates[-1] = dates[-2]
    elif failure == "wrong_order":
        dates[-1], dates[-2] = dates[-2], dates[-1]
    else:
        dates[-1] = datetime(2025, 2, 1).date()
    by_generation = dict(zip(generations, dates, strict=True))

    def load(generation: str, **_kwargs: object) -> object:
        return SimpleNamespace(
            directory=tmp_path / generation,
            manifest={"date": by_generation[generation].isoformat()},
        )

    monkeypatch.setattr(module, "load_micro_sensor_observation_generation", load)
    panel = module.MicroSensorPanelConfig("a" * 64, generations, "b" * 64, 2025)

    with pytest.raises(RuntimeError, match="unique, sorted"):
        module._validated_panel_days(panel, parsed_root=tmp_path)


def test_the_runner_binds_all_25_generations_with_only_data_root_relative_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "data"
    source = tmp_path / "source"
    source.mkdir()
    source_paths = _write_micro_inputs(source)
    generations = tuple(f"{value:064x}" for value in range(1, 26))
    raw_generations = tuple(f"{value:064x}" for value in range(101, 126))
    dates = [datetime(2025, 1, 1).date() + timedelta(days=index) for index in range(25)]
    parsed_root = root / "interim" / "micro_sensors" / "observations" / "generations"
    parsed: dict[str, object] = {}
    raw: dict[str, object] = {}
    for generation, raw_generation, day in zip(generations, raw_generations, dates, strict=True):
        directory = parsed_root / generation
        directory.mkdir(parents=True)
        for variable in ("pm25", "humidity", "temperature"):
            target = directory / f"{variable}.parquet"
            target.write_bytes(source_paths[variable][0].read_bytes())
        raw_members = {"archive.zip": {"sha256": raw_generation}}
        parsed[generation] = SimpleNamespace(
            directory=directory,
            manifest={
                "date": day.isoformat(),
                "raw_observation_generation_sha256": raw_generation,
                "raw_members": raw_members,
            },
        )
        raw[raw_generation] = SimpleNamespace(
            manifest={
                "catalog_generation_sha256": "a" * 64,
                "date": day.isoformat(),
                "members": raw_members,
            }
        )
    ground = root / "processed" / "observations" / "year=2025" / "month=01"
    ground.mkdir(parents=True)
    (ground / "part-0.parquet").write_bytes(_ground(tmp_path).read_bytes())
    satellite_dir = root / "outputs" / "m8_satellite" / "generations" / ("b" * 64) / "year=2025"
    satellite_dir.mkdir(parents=True)
    (satellite_dir / "panel.parquet").write_bytes(_satellite(tmp_path).read_bytes())
    (satellite_dir / "manifest.json").write_text(
        json.dumps(
            {
                "analysis": "m8_satellite_association",
                "year": 2025,
                "inventory_generation_sha256": "b" * 64,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "configured_data_root", lambda: root)
    monkeypatch.setattr(module, "resolve_station_geo", _geography)
    monkeypatch.setattr(
        module,
        "load_micro_sensor_observation_generation",
        lambda generation, **_kwargs: parsed[generation],
    )
    monkeypatch.setattr(
        module,
        "load_observation_generation",
        lambda generation, **_kwargs: raw[generation],
    )

    result = module.run_micro_sensor_calibration_readiness(
        data_root=root,
        panel=module.MicroSensorPanelConfig("a" * 64, generations, "b" * 64, 2025),
        config=_config(),
        generated_at="2026-08-12T00:00:00+00:00",
    )

    assert result.manifest["panel_dates"] == 25
    assert len(result.manifest["inputs"]["parsed_generations"]) == 25
    assert len(result.manifest["input_files"]) == 77
    assert all(not Path(item["path"]).is_absolute() for item in result.manifest["input_files"])
    assert not Path(result.manifest["inputs"]["satellite_manifest"]["path"]).is_absolute()


def test_every_pm_device_has_one_explicit_spatial_state_and_no_coordinate_repair(
    tmp_path: Path,
) -> None:
    result = _result(tmp_path)

    assert result.device_links.height == 16
    assert result.device_links.group_by("device_id").len()["len"].to_list() == [1] * 16
    states = dict(result.device_links.select("device_id", "spatial_state").iter_rows())
    assert states["moving"] == "moving_coordinate"
    assert states["invalid"] == "invalid_or_null_coordinate"
    assert states["outside"] == "outside_taiwan"
    assert states["eligible"] == "eligible"
    assert result.device_links.filter(pl.col("device_id") == "invalid")["lon"][0] is None


def test_primary_pairs_keep_missing_low_duplicate_extreme_and_ground_states(
    tmp_path: Path,
) -> None:
    result = _result(tmp_path)
    reasons = dict(result.hourly_pairs.select("device_id", "eligibility_reason").iter_rows())

    assert result.hourly_pairs.height == 13
    assert reasons == {
        "duplicate-pm": "duplicate_pm25_timestamp",
        "eligible": "eligible",
        "extreme-pm": "extreme_pm25",
        "ground-absent": "ground_absent",
        "ground-flagged": "ground_present_but_ineligible",
        "ground-null-flag": "ground_present_but_ineligible",
        "ground-withheld": "ground_present_but_ineligible",
        "low-pm": "insufficient_pm25_rows",
        "missing-humidity": "missing_humidity",
        "missing-temperature": "missing_temperature",
        "null-humidity": "missing_humidity_value",
        "null-pm25": "missing_pm25_value",
        "null-temperature": "missing_temperature_value",
    }
    withheld = result.hourly_pairs.filter(pl.col("device_id") == "ground-withheld").row(
        0, named=True
    )
    flagged = result.hourly_pairs.filter(pl.col("device_id") == "ground-flagged").row(0, named=True)
    null_flag = result.hourly_pairs.filter(pl.col("device_id") == "ground-null-flag").row(
        0, named=True
    )
    absent = result.hourly_pairs.filter(pl.col("device_id") == "ground-absent").row(0, named=True)
    assert withheld["ground_row_present"] is True
    assert withheld["ground_pm25"] is None
    assert flagged["ground_row_present"] is True
    assert flagged["ground_pm25"] == 24.0
    assert flagged["ground_flag"] == "rain_present"
    assert flagged["ground_eligible"] is False
    assert null_flag["ground_row_present"] is True
    assert null_flag["ground_pm25"] == 18.0
    assert null_flag["ground_flag"] is None
    assert null_flag["ground_eligible"] is False
    assert absent["ground_row_present"] is False
    assert absent["ground_pm25"] is None
    for device, column in (
        ("null-pm25", "pm25_mean"),
        ("null-humidity", "humidity_mean"),
        ("null-temperature", "temperature_mean"),
    ):
        assert result.hourly_pairs.filter(pl.col("device_id") == device)[column][0] is None


def test_coverage_folds_exclusions_and_satellite_context_are_measured_not_joined(
    tmp_path: Path,
) -> None:
    result = _result(tmp_path)

    primary = result.coverage.filter(
        (pl.col("radius_km") == 1.0) & (pl.col("minimum_rows") == 45)
    ).row(0, named=True)
    assert primary["eligible_pairs"] == 1
    assert result.exclusions.filter(pl.col("eligibility_reason") == "eligible")["rows"][0] == 1
    assert result.fold_coverage.filter(pl.col("fold_kind") == "date")["rows"][0] == 1
    assert result.satellite_context.height == 3
    assert "device_id" not in result.satellite_context.columns
    assert result.manifest["claim_boundary"]["satellite_is_micro_location"] is False
    assert result.manifest["output_rows"]["hourly_pairs"] == 13


def test_ground_pm25_values_must_be_finite_or_null(tmp_path: Path) -> None:
    ground = _ground(tmp_path)
    pl.read_parquet(ground).with_columns(
        pl.when(pl.col("ts_local") == datetime(2025, 1, 1, 0))
        .then(pl.lit(float("inf")))
        .otherwise(pl.col("value"))
        .alias("value")
    ).write_parquet(ground)

    with pytest.raises(RuntimeError, match="finite or null"):
        module.prepare_micro_sensor_calibration_readiness(
            micro_paths=_write_micro_inputs(tmp_path),
            geography=_geography(),
            ground_path=ground,
            satellite_path=_satellite(tmp_path),
            config=_config(),
            temp_dir=tmp_path / "duckdb-temp",
            input_identity={"parsed_generations": ["a" * 64]},
        )


@pytest.mark.parametrize(
    "malformation", ["null_observation_state", "duplicate_key", "wrong_value_type"]
)
def test_satellite_context_rejects_malformed_observation_states_and_duplicate_keys(
    tmp_path: Path,
    malformation: str,
) -> None:
    satellite = _satellite(tmp_path)
    frame = pl.read_parquet(satellite)
    if malformation == "null_observation_state":
        frame = frame.with_columns(
            pl.when(pl.col("source") == "maiac_aod")
            .then(pl.lit(None, dtype=pl.Boolean))
            .otherwise(pl.col("satellite_observed"))
            .alias("satellite_observed")
        )
    elif malformation == "duplicate_key":
        frame = pl.concat([frame, frame.head(1)])
    else:
        frame = frame.with_columns(pl.col("satellite_value").cast(pl.String))
    frame.write_parquet(satellite)

    with pytest.raises(RuntimeError, match="satellite"):
        module.prepare_micro_sensor_calibration_readiness(
            micro_paths=_write_micro_inputs(tmp_path),
            geography=_geography(),
            ground_path=_ground(tmp_path),
            satellite_path=satellite,
            config=_config(),
            temp_dir=tmp_path / "duckdb-temp",
            input_identity={"parsed_generations": ["a" * 64]},
        )


def test_the_writer_rejects_a_manifest_that_does_not_match_result_members(
    tmp_path: Path,
) -> None:
    result = _result(tmp_path)
    output_rows = {**result.manifest["output_rows"], "hourly_pairs": 0}
    tampered = replace(result, manifest={**result.manifest, "output_rows": output_rows})

    with pytest.raises(RuntimeError, match="row counts"):
        module.write_micro_sensor_calibration_readiness_result(
            tampered,
            destination=tmp_path / "published",
        )

    assert not (tmp_path / "published").exists()

    changed_config = {
        **result.manifest["config"],
        "primary_minimum_rows": 60,
    }
    tampered_config = replace(
        result,
        manifest={**result.manifest, "config": changed_config},
    )
    with pytest.raises(RuntimeError, match="config identity"):
        module.write_micro_sensor_calibration_readiness_result(
            tampered_config,
            destination=tmp_path / "published",
        )

    changed_primary = {
        **result.summary["primary"],
        "distance_km": 2.0,
    }
    tampered_summary = replace(
        result,
        summary={**result.summary, "primary": changed_primary},
    )
    with pytest.raises(RuntimeError, match="primary summary"):
        module.write_micro_sensor_calibration_readiness_result(
            tampered_summary,
            destination=tmp_path / "published",
        )


def test_the_writer_rejects_a_result_member_that_lost_a_required_column(
    tmp_path: Path,
) -> None:
    result = _result(tmp_path)
    malformed = replace(result, hourly_pairs=result.hourly_pairs.drop("ground_flag"))

    with pytest.raises(RuntimeError, match="schema"):
        module.write_micro_sensor_calibration_readiness_result(
            malformed,
            destination=tmp_path / "published",
        )

    assert not (tmp_path / "published").exists()


def test_an_interrupt_after_the_first_rename_restores_the_previous_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _result(tmp_path)
    destination = tmp_path / "published"
    destination.mkdir()
    baseline = destination / "baseline.txt"
    baseline.write_text("complete", encoding="utf-8")
    real_replace = Path.replace
    calls = 0

    def interrupt_after_first_move(path: Path, target: Path) -> Path:
        nonlocal calls
        calls += 1
        moved = real_replace(path, target)
        if calls == 1:
            raise KeyboardInterrupt
        return moved

    monkeypatch.setattr(Path, "replace", interrupt_after_first_move)
    with pytest.raises(KeyboardInterrupt):
        module.write_micro_sensor_calibration_readiness_result(
            result,
            destination=destination,
        )

    assert baseline.read_text(encoding="utf-8") == "complete"
    assert not list(tmp_path.glob(".published.staging-*"))
    assert not list(tmp_path.glob(".published.backup-*"))


def test_an_interrupt_before_the_second_rename_restores_the_previous_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _result(tmp_path)
    destination = tmp_path / "published"
    destination.mkdir()
    baseline = destination / "baseline.txt"
    baseline.write_text("complete", encoding="utf-8")
    real_replace = Path.replace
    calls = 0

    def interrupt_second_rename(path: Path, target: Path) -> Path:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt
        return real_replace(path, target)

    monkeypatch.setattr(Path, "replace", interrupt_second_rename)
    with pytest.raises(KeyboardInterrupt):
        module.write_micro_sensor_calibration_readiness_result(
            result,
            destination=destination,
        )

    assert baseline.read_text(encoding="utf-8") == "complete"
    assert not list(tmp_path.glob(".published.staging-*"))
    assert not list(tmp_path.glob(".published.backup-*"))


def test_an_input_change_during_analysis_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    micro_paths = _write_micro_inputs(tmp_path)
    ground = _ground(tmp_path)
    satellite = _satellite(tmp_path)
    original_frame = module._frame
    changed = False

    def mutate_after_satellite_query(connection: Any, query: str) -> pl.DataFrame:
        nonlocal changed
        result = original_frame(connection, query)
        if "FROM satellite s" in query and not changed:
            ground.write_bytes(ground.read_bytes() + b"changed")
            changed = True
        return result

    monkeypatch.setattr(module, "_frame", mutate_after_satellite_query)

    with pytest.raises(RuntimeError, match="input changed"):
        module.prepare_micro_sensor_calibration_readiness(
            micro_paths=micro_paths,
            geography=_geography(),
            ground_path=ground,
            satellite_path=satellite,
            config=_config(),
            temp_dir=tmp_path / "duckdb-temp",
            input_identity={"parsed_generations": ["a" * 64]},
        )


def test_the_writer_publishes_only_the_declared_members(tmp_path: Path) -> None:
    result = _result(tmp_path)
    destination = tmp_path / "published"

    written = module.write_micro_sensor_calibration_readiness_result(
        result,
        destination=destination,
    )

    assert set(written) == {
        "device_links",
        "hourly_pairs",
        "coverage",
        "exclusions",
        "fold_coverage",
        "satellite_context",
        "summary",
        "manifest",
    }
    assert {path.name for path in destination.iterdir()} == {
        "device_links.parquet",
        "hourly_pairs.parquet",
        "coverage.parquet",
        "exclusions.parquet",
        "fold_coverage.parquet",
        "satellite_context.parquet",
        "summary.json",
        "manifest.json",
    }
    assert json.loads(written["manifest"].read_text(encoding="utf-8"))["complete"] is True

    repeated = module.write_micro_sensor_calibration_readiness_result(
        result,
        destination=destination,
    )
    assert repeated == written
    assert not list(tmp_path.glob(".published.staging-*"))
    assert not list(tmp_path.glob(".published.backup-*"))
