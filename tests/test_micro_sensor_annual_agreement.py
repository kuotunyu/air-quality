from __future__ import annotations

import importlib
import json
from copy import deepcopy
from dataclasses import replace
from datetime import date
from hashlib import sha256
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from twair.analysis.micro_sensor_annual_readiness import (
    ANNUAL_CALENDAR_SCHEMA,
    ANNUAL_COHORT_THRESHOLD_SCHEMA,
    ANNUAL_DEVICE_COHORT_SCHEMA,
    ANNUAL_DEVICE_DAY_SCHEMA,
    ANNUAL_EXCLUSION_SCHEMA,
)
from twair.config import ConfigError, load_conf
from twair.net import sha256_file

ANNUAL_GENERATION = "c74ec40428a907e98821efbaf36c36386d2c1b99de69791b49f157eb7947e5bb"
ANNUAL_MEMBER_SCHEMAS = {
    "calendar_coverage": ANNUAL_CALENDAR_SCHEMA,
    "device_days": ANNUAL_DEVICE_DAY_SCHEMA,
    "device_cohorts": ANNUAL_DEVICE_COHORT_SCHEMA,
    "cohort_thresholds": ANNUAL_COHORT_THRESHOLD_SCHEMA,
    "exclusions": ANNUAL_EXCLUSION_SCHEMA,
}


def _canonical_hash(value: object) -> str:
    payload = (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode()
    return sha256(payload).hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _reviewed_geography_fixture() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "station_name": ["station-a"],
            "lon": [121.5],
            "lat": [25.0],
            "geo_source": ["current_register"],
            "geo_source_record_namespace": ["aqx_p_07"],
            "geo_source_record_id": ["1"],
        }
    )


def _geography_hash(frame: pl.DataFrame) -> str:
    return _canonical_hash(
        frame.select(
            "station_name",
            "lon",
            "lat",
            "geo_source",
            "geo_source_record_namespace",
            "geo_source_record_id",
        )
        .sort("station_name")
        .to_dicts()
    )


def _empty_annual_fixture(tmp_path: Path) -> tuple[Path, str, pl.DataFrame]:
    generation = tmp_path / "annual-readiness-staging"
    generation.mkdir(parents=True)
    geography = _reviewed_geography_fixture()
    members: dict[str, dict[str, object]] = {}
    rows: dict[str, int] = {}
    for name, schema in ANNUAL_MEMBER_SCHEMAS.items():
        path = generation / f"{name}.parquet"
        pl.DataFrame(schema=dict(schema)).write_parquet(path)
        rows[name] = 0
        members[name] = {
            "path": path.name,
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }
    summary = {
        "calendar": {"complete_dates": 0, "catalogue_absent_dates": 0},
        "devices": 0,
        "threshold_grid_rows": 0,
        "output_rows": rows,
    }
    _write_json(generation / "summary.json", summary)
    summary_path = generation / "summary.json"
    identity = {
        "schema_version": 1,
        "analysis": "annual_micro_sensor_readiness",
        "config": {},
        "inputs": {"reviewed_geography_sha256": _geography_hash(geography)},
        "checkpoint_inventory": [],
        "claim_boundary": {
            "calibration_fitted": False,
            "bias_estimated": False,
            "fusion_performed": False,
            "satellite_acquired": False,
            "values_imputed": False,
            "nearest_reference_is_colocated_ground_truth": False,
            "high_resolution_pm25_created": False,
        },
        "output_rows": rows,
        "members": members,
        "summary_file": {
            "path": "summary.json",
            "bytes": summary_path.stat().st_size,
            "sha256": sha256_file(summary_path),
        },
    }
    generation_sha256 = _canonical_hash(identity)
    manifest = {
        **identity,
        "complete": True,
        "generated_at": "2026-08-13T00:00:00+00:00",
        "generation_sha256": generation_sha256,
        "git_sha": "0" * 40,
        "git_dirty": False,
        "checkpoint_run": [],
    }
    _write_json(generation / "manifest.json", manifest)
    final = tmp_path / generation_sha256
    generation.replace(final)
    generation = final
    return generation, generation_sha256, geography


def _fixture_config(
    agreement: Any,
    generation_sha256: str,
    *,
    primary_devices: int = 0,
    primary_stations: int = 0,
) -> Any:
    return replace(
        agreement.load_annual_agreement_config(),
        annual_generation_sha256=generation_sha256,
        primary_devices=primary_devices,
        primary_stations=primary_stations,
    )


def _rebind_generation(generation: Path) -> tuple[Path, str]:
    manifest_path = generation / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    rows: dict[str, int] = {}
    for name in ANNUAL_MEMBER_SCHEMAS:
        path = generation / f"{name}.parquet"
        manifest["members"][name] = {
            "path": path.name,
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        rows[name] = pl.scan_parquet(path).select(pl.len()).collect().item()
    summary_path = generation / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["output_rows"] = rows
    summary["devices"] = rows["device_cohorts"]
    summary["threshold_grid_rows"] = rows["cohort_thresholds"]
    _write_json(summary_path, summary)
    manifest["output_rows"] = rows
    manifest["summary_file"] = {
        "path": "summary.json",
        "bytes": summary_path.stat().st_size,
        "sha256": sha256_file(summary_path),
    }
    identity = {
        field: manifest[field]
        for field in manifest
        if field
        in {
            "schema_version",
            "analysis",
            "config",
            "inputs",
            "checkpoint_inventory",
            "claim_boundary",
            "output_rows",
            "members",
            "summary_file",
        }
    }
    generation_sha256 = _canonical_hash(identity)
    manifest["generation_sha256"] = generation_sha256
    _write_json(manifest_path, manifest)
    rebound = generation.parent / generation_sha256
    generation.replace(rebound)
    return rebound, generation_sha256


def _cohort_fixture(*, devices: int, stations: int) -> pl.DataFrame:
    values: dict[str, list[object]] = {}
    for name, dtype in ANNUAL_DEVICE_COHORT_SCHEMA:
        if name == "device_id":
            values[name] = [f"device-{index:03d}" for index in range(devices)]
        elif name == "station_name":
            values[name] = [f"station-{index % stations:02d}" for index in range(devices)]
        elif name == "distance_km":
            values[name] = [0.5] * devices
        elif name == "spatial_state":
            values[name] = ["eligible"] * devices
        elif name == "active_months":
            values[name] = [3] * devices
        elif name == "trio_dates":
            values[name] = [30] * devices
        elif name == "trio_observed_hours":
            values[name] = [360] * devices
        elif dtype == pl.String:
            values[name] = [None] * devices
        elif dtype == pl.Float64:
            values[name] = [0.0] * devices
        else:
            values[name] = [0] * devices
    return pl.DataFrame(values, schema=dict(ANNUAL_DEVICE_COHORT_SCHEMA))


def _device_day_fixture(device_id: str) -> pl.DataFrame:
    values: dict[str, list[object]] = {}
    for name, dtype in ANNUAL_DEVICE_DAY_SCHEMA:
        if name == "date":
            values[name] = [date(2025, 1, 1)]
        elif name == "device_id":
            values[name] = [device_id]
        elif name == "spatial_state":
            values[name] = ["eligible"]
        elif name == "station_name":
            values[name] = ["station-a"]
        elif dtype == pl.String:
            values[name] = [None]
        elif dtype == pl.Float64:
            values[name] = [0.0]
        else:
            values[name] = [0]
    return pl.DataFrame(values, schema=dict(ANNUAL_DEVICE_DAY_SCHEMA))


def test_the_reviewed_agreement_config_enforces_the_scientific_and_resource_boundary() -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")

    config = agreement.load_annual_agreement_config()

    assert config.annual_generation_sha256 == ANNUAL_GENERATION
    assert config.distance_bands_km == (0.5, 1.0, 2.0)
    assert config.primary_distance_km == 0.5
    assert config.primary_devices == 124
    assert config.primary_stations == 13
    assert config.minimum_active_months == 3
    assert config.minimum_trio_dates == 30
    assert config.minimum_trio_hours == 360
    assert config.minimum_source_rows == 1080
    assert config.minimum_observed_hours == 18
    assert config.station_folds == 5
    assert config.quarters == (1, 2, 3, 4)
    assert config.ridge_alpha == 1.0
    assert config.threads == 1
    assert config.memory_limit_gb == 6
    assert dict(config.claim_boundary) == {
        "reference_station_agreement_only": True,
        "validated_calibration": False,
        "sensor_bias_estimate": False,
        "sensor_fusion": False,
        "colocated_ground_truth": False,
        "high_resolution_field": False,
        "satellite_feature_used": False,
        "values_imputed": False,
        "causal_analysis": False,
    }


def test_another_well_formed_annual_generation_is_rejected() -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    raw = deepcopy(load_conf("micro_sensor_annual_agreement"))
    raw["analysis"]["annual_generation_sha256"] = "a" * 64

    with pytest.raises(ConfigError, match="annual generation"):
        agreement.load_annual_agreement_config(raw)


def test_the_annual_input_binds_all_five_members_and_rejects_changed_bytes(
    tmp_path: Path,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    generation, generation_sha256, geography = _empty_annual_fixture(tmp_path)

    loaded = agreement._load_annual_readiness_input(
        generation,
        expected_generation_sha256=generation_sha256,
        reviewed_geography=geography,
        config=_fixture_config(agreement, generation_sha256),
    )

    assert loaded.manifest["generation_sha256"] == generation_sha256
    assert loaded.device_cohorts.height == 0
    assert loaded.device_days.sha256 == sha256_file(generation / "device_days.parquet")
    assert tuple(cohort.radius_km for cohort in loaded.candidate_cohorts) == (0.5, 1.0, 2.0)
    changed = generation / "device_days.parquet"
    changed.write_bytes(changed.read_bytes() + b"changed")
    with pytest.raises(RuntimeError, match="annual readiness member changed"):
        agreement._load_annual_readiness_input(
            generation,
            expected_generation_sha256=generation_sha256,
            reviewed_geography=geography,
            config=_fixture_config(agreement, generation_sha256),
        )


def test_an_extra_file_cannot_enter_the_annual_readiness_generation(tmp_path: Path) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    generation, generation_sha256, geography = _empty_annual_fixture(tmp_path)
    (generation / "surprise.parquet").write_bytes(b"not reviewed")

    with pytest.raises(RuntimeError, match="file set changed"):
        agreement._load_annual_readiness_input(
            generation,
            expected_generation_sha256=generation_sha256,
            reviewed_geography=geography,
            config=_fixture_config(agreement, generation_sha256),
        )


def test_an_extra_directory_cannot_enter_the_annual_readiness_generation(
    tmp_path: Path,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    generation, generation_sha256, geography = _empty_annual_fixture(tmp_path)
    (generation / ".staging-residue").mkdir()

    with pytest.raises(RuntimeError, match="file set changed"):
        agreement._load_annual_readiness_input(
            generation,
            expected_generation_sha256=generation_sha256,
            reviewed_geography=geography,
            config=_fixture_config(agreement, generation_sha256),
        )


def test_a_relabelled_annual_generation_directory_is_rejected(tmp_path: Path) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    generation, generation_sha256, geography = _empty_annual_fixture(tmp_path)
    relabelled = tmp_path / ("b" * 64)
    generation.replace(relabelled)

    with pytest.raises(RuntimeError, match="directory identity changed"):
        agreement._load_annual_readiness_input(
            relabelled,
            expected_generation_sha256=generation_sha256,
            reviewed_geography=geography,
            config=_fixture_config(agreement, generation_sha256),
        )


def test_an_unbound_manifest_field_is_rejected(tmp_path: Path) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    generation, generation_sha256, geography = _empty_annual_fixture(tmp_path)
    manifest_path = generation / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["surprise"] = "not part of the generation identity"
    _write_json(manifest_path, manifest)

    with pytest.raises(RuntimeError, match="manifest fields changed"):
        agreement._load_annual_readiness_input(
            generation,
            expected_generation_sha256=generation_sha256,
            reviewed_geography=geography,
            config=_fixture_config(agreement, generation_sha256),
        )


def test_candidate_devices_are_derived_from_reviewed_annual_thresholds_not_ids() -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    config = replace(
        agreement.load_annual_agreement_config(),
        primary_devices=2,
        primary_stations=2,
    )
    cohorts = pl.DataFrame(
        {
            "device_id": ["near-a", "near-b", "mid", "sparse", "moving"],
            "station_name": ["station-a", "station-b", "station-b", "station-a", "station-a"],
            "distance_km": [0.1, 0.5, 1.5, 0.2, 0.2],
            "spatial_state": ["eligible", "eligible", "eligible", "eligible", "moving_coordinate"],
            "active_months": [3, 4, 3, 2, 3],
            "trio_dates": [30, 40, 30, 40, 40],
            "trio_observed_hours": [360, 480, 360, 480, 480],
        }
    )

    candidates = agreement.derive_agreement_candidates(cohorts, config=config)

    assert candidates.select("device_id").to_series().to_list() == ["mid", "near-a", "near-b"]
    assert candidates.filter(pl.col("distance_km") <= 0.5).height == 2
    assert candidates.filter(pl.col("distance_km") <= 1.0).height == 2
    assert candidates.filter(pl.col("distance_km") <= 2.0).height == 3


def test_a_manifest_change_during_member_reads_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    generation, generation_sha256, geography = _empty_annual_fixture(tmp_path)
    manifest_path = generation / "manifest.json"
    original_read = pl.read_parquet
    changed = False

    def read_and_change_manifest(path: str | Path) -> pl.DataFrame:
        nonlocal changed
        result = original_read(path)
        if not changed:
            changed = True
            manifest_path.write_bytes(manifest_path.read_bytes() + b"\n")
        return result

    monkeypatch.setattr(pl, "read_parquet", read_and_change_manifest)
    with pytest.raises(RuntimeError, match="manifest changed during read"):
        agreement._load_annual_readiness_input(
            generation,
            expected_generation_sha256=generation_sha256,
            reviewed_geography=geography,
            config=_fixture_config(agreement, generation_sha256),
        )


def test_declared_output_rows_must_equal_the_parquet_members(tmp_path: Path) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    generation, _, geography = _empty_annual_fixture(tmp_path)
    manifest_path = generation / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["output_rows"]["device_cohorts"] = 1
    identity = {field: manifest[field] for field in agreement._ANNUAL_IDENTITY_FIELDS}
    generation_sha256 = _canonical_hash(identity)
    manifest["generation_sha256"] = generation_sha256
    _write_json(manifest_path, manifest)
    renamed = generation.parent / generation_sha256
    generation.replace(renamed)
    generation = renamed

    with pytest.raises(RuntimeError, match="row counts changed"):
        agreement._load_annual_readiness_input(
            generation,
            expected_generation_sha256=generation_sha256,
            reviewed_geography=geography,
            config=_fixture_config(agreement, generation_sha256),
        )


def test_a_geography_provenance_change_cannot_reuse_the_annual_generation(
    tmp_path: Path,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    generation, generation_sha256, geography = _empty_annual_fixture(tmp_path)
    changed = geography.with_columns(pl.lit("reviewed_historical").alias("geo_source"))

    with pytest.raises(RuntimeError, match="geography identity changed"):
        agreement._load_annual_readiness_input(
            generation,
            expected_generation_sha256=generation_sha256,
            reviewed_geography=changed,
            config=_fixture_config(agreement, generation_sha256),
        )


def test_multiple_reviewed_geography_rows_form_one_stable_identity() -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    geography = pl.concat(
        [
            _reviewed_geography_fixture(),
            pl.DataFrame(
                {
                    "station_name": ["station-b"],
                    "lon": [120.5],
                    "lat": [23.5],
                    "geo_source": ["reviewed_historical"],
                    "geo_source_record_namespace": ["AIRTW central station detail"],
                    "geo_source_record_id": ["2"],
                }
            ),
        ]
    )

    assert agreement._geography_identity(geography) == _geography_hash(geography)


def test_a_parquet_member_change_during_its_read_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    generation, generation_sha256, geography = _empty_annual_fixture(tmp_path)
    original_read = pl.read_parquet
    changed = False

    def read_and_change_member(path: str | Path) -> pl.DataFrame:
        nonlocal changed
        result = original_read(path)
        path = Path(path)
        if not changed:
            changed = True
            path.write_bytes(path.read_bytes() + b"changed")
        return result

    monkeypatch.setattr(pl, "read_parquet", read_and_change_member)
    with pytest.raises(RuntimeError, match="member changed during read"):
        agreement._load_annual_readiness_input(
            generation,
            expected_generation_sha256=generation_sha256,
            reviewed_geography=geography,
            config=_fixture_config(agreement, generation_sha256),
        )


def test_the_path_backed_device_day_member_stays_pinned_through_the_row_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    generation, _, geography = _empty_annual_fixture(tmp_path)
    _device_day_fixture("reviewed-device").write_parquet(generation / "device_days.parquet")
    generation, generation_sha256 = _rebind_generation(generation)
    alternate = tmp_path / "alternate-device-days.parquet"
    _device_day_fixture("changed-device").write_parquet(alternate)
    original_scan = pl.scan_parquet
    changed = False

    def scan_after_replacement(path: str | Path) -> pl.LazyFrame:
        nonlocal changed
        if not changed and Path(path).name == "device_days.parquet":
            changed = True
            alternate.replace(path)
        return original_scan(path)

    monkeypatch.setattr(pl, "scan_parquet", scan_after_replacement)
    with pytest.raises(RuntimeError, match=r"device_days\.parquet changed during read"):
        agreement._load_annual_readiness_input(
            generation,
            expected_generation_sha256=generation_sha256,
            reviewed_geography=geography,
            config=_fixture_config(agreement, generation_sha256),
        )


@pytest.mark.parametrize(
    ("devices", "stations", "message"),
    [
        (123, 13, "primary device count changed"),
        (124, 12, "primary station count changed"),
    ],
)
def test_loading_the_annual_input_enforces_the_reviewed_primary_cohort(
    tmp_path: Path,
    devices: int,
    stations: int,
    message: str,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    generation, _, geography = _empty_annual_fixture(tmp_path)
    _cohort_fixture(devices=devices, stations=stations).write_parquet(
        generation / "device_cohorts.parquet"
    )
    generation, generation_sha256 = _rebind_generation(generation)

    with pytest.raises(RuntimeError, match=message):
        agreement._load_annual_readiness_input(
            generation,
            expected_generation_sha256=generation_sha256,
            reviewed_geography=geography,
            config=_fixture_config(
                agreement,
                generation_sha256,
                primary_devices=124,
                primary_stations=13,
            ),
        )


def test_the_public_loader_does_not_accept_a_caller_supplied_generation_or_cohort_contract(
    tmp_path: Path,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    generation, _, geography = _empty_annual_fixture(tmp_path)
    _cohort_fixture(devices=123, stations=13).write_parquet(generation / "device_cohorts.parquet")
    generation, generation_sha256 = _rebind_generation(generation)
    public_loader: Any = agreement.load_annual_readiness_input

    with pytest.raises(TypeError, match="unexpected keyword argument"):
        public_loader(
            generation,
            expected_generation_sha256=generation_sha256,
            reviewed_geography=geography,
            config=_fixture_config(
                agreement,
                generation_sha256,
                primary_devices=123,
                primary_stations=13,
            ),
        )


def test_a_new_generation_entry_created_during_loading_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    generation, generation_sha256, geography = _empty_annual_fixture(tmp_path)
    original_read = pl.read_parquet
    changed = False

    def read_and_add_entry(path: str | Path) -> pl.DataFrame:
        nonlocal changed
        result = original_read(path)
        if not changed:
            changed = True
            (generation / "late-entry").mkdir()
        return result

    monkeypatch.setattr(pl, "read_parquet", read_and_add_entry)
    with pytest.raises(RuntimeError, match="file set changed during read"):
        agreement._load_annual_readiness_input(
            generation,
            expected_generation_sha256=generation_sha256,
            reviewed_geography=geography,
            config=_fixture_config(agreement, generation_sha256),
        )


def test_a_linked_generation_member_is_rejected_before_it_is_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    generation, generation_sha256, geography = _empty_annual_fixture(tmp_path)
    original_is_symlink = Path.is_symlink

    def identify_device_days_as_link(path: Path) -> bool:
        return path.name == "device_days.parquet" or original_is_symlink(path)

    monkeypatch.setattr(Path, "is_symlink", identify_device_days_as_link)
    with pytest.raises(RuntimeError, match="linked or outside"):
        agreement._load_annual_readiness_input(
            generation,
            expected_generation_sha256=generation_sha256,
            reviewed_geography=geography,
            config=_fixture_config(agreement, generation_sha256),
        )


def test_a_pinned_member_rechecks_link_and_generation_containment_on_every_use(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    generation, generation_sha256, geography = _empty_annual_fixture(tmp_path)
    loaded = agreement._load_annual_readiness_input(
        generation,
        expected_generation_sha256=generation_sha256,
        reviewed_geography=geography,
        config=_fixture_config(agreement, generation_sha256),
    )
    original_is_symlink = Path.is_symlink

    def identify_device_days_as_link(path: Path) -> bool:
        return path == loaded.device_days.path or original_is_symlink(path)

    monkeypatch.setattr(Path, "is_symlink", identify_device_days_as_link)
    with (
        pytest.raises(RuntimeError, match="linked or outside"),
        agreement.stable_annual_member_path(loaded.device_days),
    ):
        pass


def test_an_incomplete_annual_generation_is_rejected(tmp_path: Path) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    generation, generation_sha256, geography = _empty_annual_fixture(tmp_path)
    manifest_path = generation / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["complete"] = False
    _write_json(manifest_path, manifest)

    with pytest.raises(RuntimeError, match="not complete"):
        agreement._load_annual_readiness_input(
            generation,
            expected_generation_sha256=generation_sha256,
            reviewed_geography=geography,
            config=_fixture_config(agreement, generation_sha256),
        )


@pytest.mark.parametrize(
    ("field", "changed"),
    [
        ("schema_version", 2),
        ("schema_version", 1.0),
        ("analysis", "another_analysis"),
    ],
)
def test_the_annual_manifest_has_one_fixed_schema_and_analysis(
    tmp_path: Path,
    field: str,
    changed: object,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    generation, _, geography = _empty_annual_fixture(tmp_path)
    manifest_path = generation / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest[field] = changed
    _write_json(manifest_path, manifest)
    generation, generation_sha256 = _rebind_generation(generation)

    with pytest.raises(RuntimeError, match="manifest contract changed"):
        agreement._load_annual_readiness_input(
            generation,
            expected_generation_sha256=generation_sha256,
            reviewed_geography=geography,
            config=_fixture_config(agreement, generation_sha256),
        )


def test_the_annual_summary_rejects_an_unknown_field(tmp_path: Path) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    generation, _, geography = _empty_annual_fixture(tmp_path)
    summary_path = generation / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["unreviewed"] = 1
    _write_json(summary_path, summary)
    generation, generation_sha256 = _rebind_generation(generation)

    with pytest.raises(RuntimeError, match="summary fields changed"):
        agreement._load_annual_readiness_input(
            generation,
            expected_generation_sha256=generation_sha256,
            reviewed_geography=geography,
            config=_fixture_config(agreement, generation_sha256),
        )


def test_claim_boundary_values_are_real_booleans_and_cannot_be_mutated() -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    raw = deepcopy(load_conf("micro_sensor_annual_agreement"))
    raw["analysis"]["claim_boundary"]["validated_calibration"] = 0

    with pytest.raises(ConfigError, match="claim_boundary values must be booleans"):
        agreement.load_annual_agreement_config(raw)

    config = agreement.load_annual_agreement_config()
    boundary: Any = config.claim_boundary
    with pytest.raises(TypeError):
        boundary[0] = ("validated_calibration", True)


def test_a_missing_member_is_rejected_before_loading_starts(tmp_path: Path) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    missing, missing_sha, missing_geo = _empty_annual_fixture(tmp_path / "missing")
    (missing / "calendar_coverage.parquet").unlink()
    with pytest.raises(RuntimeError, match="file set changed"):
        agreement._load_annual_readiness_input(
            missing,
            expected_generation_sha256=missing_sha,
            reviewed_geography=missing_geo,
            config=_fixture_config(agreement, missing_sha),
        )


@pytest.mark.parametrize("member", ["calendar_coverage", "device_days"])
def test_eager_and_path_backed_members_both_reject_a_wrong_schema(
    tmp_path: Path,
    member: str,
) -> None:
    agreement = importlib.import_module("twair.analysis.micro_sensor_annual_agreement")
    wrong, _, wrong_geo = _empty_annual_fixture(tmp_path / member)
    pl.DataFrame({"wrong": [1]}).write_parquet(wrong / f"{member}.parquet")
    wrong, wrong_sha = _rebind_generation(wrong)
    with pytest.raises(RuntimeError, match=rf"schema changed: {member}\.parquet"):
        agreement._load_annual_readiness_input(
            wrong,
            expected_generation_sha256=wrong_sha,
            reviewed_geography=wrong_geo,
            config=_fixture_config(agreement, wrong_sha),
        )
