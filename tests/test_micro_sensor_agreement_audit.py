from __future__ import annotations

import copy
import json
import re
from dataclasses import replace
from datetime import date
from hashlib import sha256
from pathlib import Path
from typing import Any

import polars as pl
import pytest

import twair.analysis.micro_sensor_agreement_audit as audit
from twair.analysis.micro_sensor_agreement_audit import (
    AgreementAuditConfig,
    FrozenAuditInputs,
    load_frozen_agreement_audit_inputs,
    load_micro_sensor_agreement_audit_config,
)
from twair.analysis.micro_sensor_annual_readiness import (
    ANNUAL_CALENDAR_SCHEMA,
    ANNUAL_COHORT_THRESHOLD_SCHEMA,
    ANNUAL_DEVICE_COHORT_SCHEMA,
    ANNUAL_DEVICE_DAY_SCHEMA,
    ANNUAL_EXCLUSION_SCHEMA,
)
from twair.config import ConfigError, load_conf
from twair.net import sha256_file

_ANNUAL_SCHEMAS = {
    "calendar_coverage": ANNUAL_CALENDAR_SCHEMA,
    "device_days": ANNUAL_DEVICE_DAY_SCHEMA,
    "device_cohorts": ANNUAL_DEVICE_COHORT_SCHEMA,
    "cohort_thresholds": ANNUAL_COHORT_THRESHOLD_SCHEMA,
    "exclusions": ANNUAL_EXCLUSION_SCHEMA,
}
AGREEMENT_CALENDAR_SCHEMA = (
    ("date", pl.Date),
    ("calendar_state", pl.String),
    ("catalog_generation_sha256", pl.String),
    ("parsed_generation_sha256", pl.String),
)
AGREEMENT_PAIRED_DAY_SCHEMA = (
    ("radius_km", pl.Float64),
    ("calendar_state", pl.String),
    ("quarter", pl.Int64),
    ("date", pl.Date),
    ("device_id", pl.String),
    ("station_name", pl.String),
    ("distance_km", pl.Float64),
    ("lon_min", pl.Float64),
    ("lat_min", pl.Float64),
    ("micro_pm25_mean", pl.Float64),
    ("micro_humidity_mean", pl.Float64),
    ("micro_temperature_mean", pl.Float64),
    ("ground_pm25_mean", pl.Float64),
    ("reason", pl.String),
)
AGREEMENT_EXCLUSION_SCHEMA = (
    ("radius_km", pl.Float64),
    ("date", pl.Date),
    ("device_id", pl.String),
    ("station_name", pl.String),
    ("quarter", pl.Int64),
    ("reason", pl.String),
)
AGREEMENT_PREDICTION_SCHEMA = (
    ("evaluation", pl.String),
    ("fold", pl.String),
    ("fold_state", pl.String),
    ("radius_km", pl.Float64),
    ("date", pl.Date),
    ("device_id", pl.String),
    ("station_name", pl.String),
    ("station_fold", pl.Int64),
    ("quarter", pl.Int64),
    ("train_membership_sha256", pl.String),
    ("test_membership_sha256", pl.String),
    ("test_truth_sha256", pl.String),
    ("model", pl.String),
    ("model_features", pl.String),
    ("y_true", pl.Float64),
    ("y_pred", pl.Float64),
)
AGREEMENT_SCORE_SCHEMA = (
    ("scope", pl.String),
    ("evaluation", pl.String),
    ("fold", pl.String),
    ("radius_km", pl.Float64),
    ("model", pl.String),
    ("unit", pl.String),
    ("state", pl.String),
    ("n", pl.Int64),
    ("intended_n", pl.Int64),
    ("membership_sha256", pl.String),
    ("truth_sha256", pl.String),
    ("scored_membership_sha256", pl.String),
    ("scored_truth_sha256", pl.String),
    ("total_folds", pl.Int64),
    ("scored_folds", pl.Int64),
    ("unscored_folds_sha256", pl.String),
    ("rmse", pl.Float64),
    ("mae", pl.Float64),
    ("r2", pl.Float64),
    ("bias", pl.Float64),
    ("absolute_bias", pl.Float64),
)
AGREEMENT_DELTA_SCHEMA = (
    *AGREEMENT_SCORE_SCHEMA[:4],
    ("unit", pl.String),
    ("model", pl.String),
    ("baseline_model", pl.String),
    *AGREEMENT_SCORE_SCHEMA[6:16],
    ("delta_rmse", pl.Float64),
    ("delta_mae", pl.Float64),
    ("delta_r2", pl.Float64),
    ("delta_bias", pl.Float64),
    ("delta_absolute_bias", pl.Float64),
    ("improved_rmse", pl.Boolean),
    ("improved_mae", pl.Boolean),
    ("improved_r2", pl.Boolean),
    ("improved_absolute_bias", pl.Boolean),
)
_FOLD_MEMBERSHIP_SCHEMA = (
    ("evaluation", pl.String),
    ("fold", pl.String),
    ("role", pl.String),
    ("station_fold", pl.Int64),
    *AGREEMENT_PAIRED_DAY_SCHEMA,
    ("fold_state", pl.String),
    ("fold_reason", pl.String),
    ("train_rows", pl.Int64),
    ("test_rows", pl.Int64),
    ("train_unique_targets", pl.Int64),
    ("test_unique_targets", pl.Int64),
    ("train_membership_sha256", pl.String),
    ("test_membership_sha256", pl.String),
    ("test_truth_sha256", pl.String),
)
_FOLD_SCHEMA = (
    ("evaluation", pl.String),
    ("fold", pl.String),
    ("fold_state", pl.String),
    ("fold_reason", pl.String),
    ("train_rows", pl.Int64),
    ("test_rows", pl.Int64),
    ("train_unique_targets", pl.Int64),
    ("test_unique_targets", pl.Int64),
    ("train_membership_sha256", pl.String),
    ("test_membership_sha256", pl.String),
    ("test_truth_sha256", pl.String),
    ("train_devices", pl.Int64),
    ("test_devices", pl.Int64),
    ("train_stations", pl.Int64),
    ("test_stations", pl.Int64),
    ("train_dates", pl.Int64),
    ("test_dates", pl.Int64),
    ("device_overlap", pl.Int64),
    ("excluded_rows", pl.Int64),
)
_SATELLITE_SCHEMA = (
    ("source", pl.String),
    ("station_name", pl.String),
    ("month", pl.Date),
    ("satellite_value", pl.Float64),
    ("satellite_unit", pl.String),
    ("ground_value", pl.Float64),
    ("ground_unit", pl.String),
    ("satellite_observed", pl.Boolean),
    ("ground_row_present", pl.Boolean),
    ("ground_meets_threshold", pl.Boolean),
    ("ground_observed", pl.Boolean),
    ("ground_withheld", pl.Boolean),
    ("pair_observed", pl.Boolean),
    ("collection_id", pl.String),
    ("band", pl.String),
    ("sample_scale_m", pl.Int32),
)
_AGREEMENT_SCHEMAS = {
    "calendar": AGREEMENT_CALENDAR_SCHEMA,
    "paired_days": AGREEMENT_PAIRED_DAY_SCHEMA,
    "exclusions": AGREEMENT_EXCLUSION_SCHEMA,
    "fold_membership": _FOLD_MEMBERSHIP_SCHEMA,
    "folds": _FOLD_SCHEMA,
    "predictions": AGREEMENT_PREDICTION_SCHEMA,
    "scores": AGREEMENT_SCORE_SCHEMA,
    "deltas": AGREEMENT_DELTA_SCHEMA,
}
_COORDINATE_FIELDS = (
    "station_name",
    "lon",
    "lat",
    "geo_source",
    "geo_source_record_namespace",
    "geo_source_record_id",
)


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
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def _schema_dict(schema: tuple[tuple[str, pl.DataType | type[pl.DataType]], ...]) -> dict[str, Any]:
    return dict(schema)


def _empty(schema: tuple[tuple[str, pl.DataType | type[pl.DataType]], ...]) -> pl.DataFrame:
    return pl.DataFrame(schema=_schema_dict(schema))


def _default(dtype: pl.DataType | type[pl.DataType]) -> object:
    if dtype == pl.String:
        return ""
    if dtype in (pl.Float32, pl.Float64):
        return 0.0
    if dtype in (pl.Int8, pl.Int16, pl.Int32, pl.Int64):
        return 0
    if dtype == pl.Boolean:
        return False
    if dtype == pl.Date:
        return date(2025, 10, 1)
    raise AssertionError(f"unsupported fixture dtype: {dtype}")


def _frame(
    schema: tuple[tuple[str, pl.DataType | type[pl.DataType]], ...],
    rows: list[dict[str, object]],
) -> pl.DataFrame:
    complete = [
        {name: row.get(name, _default(dtype)) for name, dtype in schema}
        for row in rows
    ]
    return pl.DataFrame(complete, schema=_schema_dict(schema))


def _write_members(
    directory: Path,
    frames: dict[str, pl.DataFrame],
) -> tuple[dict[str, dict[str, object]], dict[str, int], dict[str, dict[str, str]]]:
    members: dict[str, dict[str, object]] = {}
    rows: dict[str, int] = {}
    schemas: dict[str, dict[str, str]] = {}
    for name, frame in frames.items():
        path = directory / f"{name}.parquet"
        frame.write_parquet(path)
        members[name] = {
            "path": path.name,
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        rows[name] = frame.height
        schemas[name] = {column: str(dtype) for column, dtype in frame.schema.items()}
    return members, rows, schemas


def _geography() -> pl.DataFrame:
    zone_sizes = (5, 4, 4)
    rows: list[dict[str, object]] = []
    index = 0
    for zone_index, size in enumerate(zone_sizes):
        for _ in range(size):
            rows.append(
                {
                    "station_name": f"station-{index:02d}",
                    "lon": 120.0 + index / 100,
                    "lat": 23.0 + index / 100,
                    "geo_source": "fixture",
                    "geo_source_record_namespace": "fixture-stations",
                    "geo_source_record_id": f"fixture-{index:02d}",
                    "airzone_official": f"zone-{zone_index + 1}",
                }
            )
            index += 1
    rows.append(
        {
            "station_name": "historical-unzoned",
            "lon": 121.75,
            "lat": 25.15,
            "geo_source": "fixture-historical",
            "geo_source_record_namespace": "fixture-stations",
            "geo_source_record_id": "fixture-historical",
            "airzone_official": None,
        }
    )
    return pl.DataFrame(rows)


def _agreement_frames() -> dict[str, pl.DataFrame]:
    eligible_rows: list[dict[str, object]] = []
    for station_index in range(12):
        for day in range(1, 4):
            eligible_rows.append(
                {
                    "radius_km": 0.5,
                    "calendar_state": "complete",
                    "quarter": 4,
                    "date": date(2025, 10, day),
                    "device_id": f"device-{station_index:02d}",
                    "station_name": f"station-{station_index:02d}",
                    "distance_km": 0.1,
                    "micro_pm25_mean": float(10 + station_index + day),
                    "micro_humidity_mean": float(60 + day),
                    "micro_temperature_mean": float(24 + day),
                    "ground_pm25_mean": float(11 + station_index + day),
                    "reason": "eligible",
                }
            )
    paired_rows = [
        *eligible_rows,
        {
            "radius_km": 0.5,
            "calendar_state": "complete",
            "quarter": 4,
            "date": date(2025, 10, 1),
            "device_id": "device-12",
            "station_name": "station-12",
            "distance_km": 0.1,
            "reason": "insufficient_micro_hours",
        },
    ]
    fold_rows: list[dict[str, object]] = []
    empty_hash = _canonical_hash([])
    for fold_index in range(5):
        fold_rows.append(
            {
                "evaluation": "held_station",
                "fold": f"held_station_{fold_index:02d}",
                "fold_state": "scored",
                "fold_reason": "scored",
                "train_rows": 30,
                "test_rows": 6,
                "train_unique_targets": 20,
                "test_unique_targets": 6,
                "train_membership_sha256": empty_hash,
                "test_membership_sha256": empty_hash,
                "test_truth_sha256": empty_hash,
                "train_devices": 10,
                "test_devices": 2,
                "train_stations": 10,
                "test_stations": 2,
                "train_dates": 3,
                "test_dates": 3,
                "device_overlap": 0,
                "excluded_rows": 0,
            }
        )
    for quarter in range(1, 5):
        state = "unscored_empty_train" if quarter == 4 else "unscored_empty_test"
        fold_rows.append(
            {
                "evaluation": "held_quarter",
                "fold": f"held_quarter_{quarter:02d}",
                "fold_state": state,
                "fold_reason": state,
                "train_membership_sha256": empty_hash,
                "test_membership_sha256": empty_hash,
                "test_truth_sha256": empty_hash,
            }
        )
    for fold_index in range(5):
        for quarter in range(1, 5):
            state = "unscored_empty_train" if quarter == 4 else "unscored_empty_test"
            fold_rows.append(
                {
                    "evaluation": "joint",
                    "fold": f"joint_{fold_index:02d}_{quarter:02d}",
                    "fold_state": state,
                    "fold_reason": state,
                    "train_membership_sha256": empty_hash,
                    "test_membership_sha256": empty_hash,
                    "test_truth_sha256": empty_hash,
                }
            )
    return {
        "calendar": _frame(
            AGREEMENT_CALENDAR_SCHEMA,
            [
                {
                    "date": date(2025, 10, day),
                    "calendar_state": "complete",
                    "catalog_generation_sha256": "a" * 64,
                    "parsed_generation_sha256": "b" * 64,
                }
                for day in range(1, 4)
            ],
        ),
        "paired_days": _frame(AGREEMENT_PAIRED_DAY_SCHEMA, paired_rows),
        "exclusions": _frame(
            AGREEMENT_EXCLUSION_SCHEMA,
            [
                {
                    "radius_km": 0.5,
                    "date": date(2025, 10, 1),
                    "device_id": "device-12",
                    "station_name": "station-12",
                    "quarter": 4,
                    "reason": "insufficient_micro_hours",
                }
            ],
        ),
        "fold_membership": _empty(_FOLD_MEMBERSHIP_SCHEMA),
        "folds": _frame(_FOLD_SCHEMA, fold_rows),
        "predictions": _empty(AGREEMENT_PREDICTION_SCHEMA),
        "scores": _empty(AGREEMENT_SCORE_SCHEMA),
        "deltas": _empty(AGREEMENT_DELTA_SCHEMA),
    }


def _create_frozen_source(root: Path) -> tuple[dict[str, int], dict[str, int]]:
    reviewed_geography = _geography()
    reviewed_geography_sha256 = _canonical_hash(
        reviewed_geography.select(*_COORDINATE_FIELDS).sort("station_name").to_dicts()
    )
    annual_staging = root / "annual-staging"
    annual_staging.mkdir(parents=True)
    annual_frames = {name: _empty(schema) for name, schema in _ANNUAL_SCHEMAS.items()}
    annual_members, annual_rows, _ = _write_members(annual_staging, annual_frames)
    annual_summary = {"fixture": "annual", "output_rows": annual_rows}
    annual_summary_path = annual_staging / "summary.json"
    _write_json(annual_summary_path, annual_summary)
    annual_identity = {
        "schema_version": 1,
        "analysis": "annual_micro_sensor_readiness",
        "config": {"fixture": True},
        "inputs": {"reviewed_geography_sha256": reviewed_geography_sha256},
        "checkpoint_inventory": [],
        "claim_boundary": {},
        "output_rows": annual_rows,
        "members": annual_members,
        "summary_file": {
            "path": "summary.json",
            "bytes": annual_summary_path.stat().st_size,
            "sha256": sha256_file(annual_summary_path),
        },
    }
    annual_generation = _canonical_hash(annual_identity)
    annual_manifest = {
        **annual_identity,
        "complete": True,
        "generated_at": "2026-08-29T00:00:00Z",
        "generation_sha256": annual_generation,
        "git_sha": "e4839bc",
        "git_dirty": False,
        "checkpoint_run": "fixture",
    }
    _write_json(annual_staging / "manifest.json", annual_manifest)
    annual_dir = (
        root
        / "outputs"
        / "micro_sensor_annual_readiness"
        / "generations"
        / annual_generation
    )
    annual_dir.parent.mkdir(parents=True)
    annual_staging.rename(annual_dir)

    agreement_staging = root / "agreement-staging"
    agreement_staging.mkdir()
    agreement_frames = _agreement_frames()
    agreement_members, agreement_rows, agreement_schemas = _write_members(
        agreement_staging, agreement_frames
    )
    agreement_summary = {"fixture": "agreement", "output_rows": agreement_rows}
    agreement_summary_path = agreement_staging / "summary.json"
    _write_json(agreement_summary_path, agreement_summary)
    agreement_identity = {
        "schema_version": 1,
        "analysis": "q4_supported_cross_station_agreement",
        "annual_generation_sha256": annual_generation,
        "panel_generation_sha256": "c" * 64,
        "evaluation_generation_sha256": "d" * 64,
        "panel_manifest": {"fixture": True},
        "evaluation_manifest": {"fixture": True},
        "checkpoint_inventory": [],
        "config": {"fixture": True},
        "claim_boundary": {},
        "output_rows": agreement_rows,
        "schemas": agreement_schemas,
        "members": agreement_members,
        "summary_file": {
            "path": "summary.json",
            "bytes": agreement_summary_path.stat().st_size,
            "sha256": sha256_file(agreement_summary_path),
        },
        "summary_sha256": _canonical_hash(agreement_summary),
        "git_sha": "b7bff3e",
        "git_dirty": False,
    }
    agreement_generation = _canonical_hash(agreement_identity)
    agreement_manifest = {
        **agreement_identity,
        "complete": True,
        "generated_at": "2026-08-29T00:00:00Z",
        "generation_sha256": agreement_generation,
    }
    _write_json(agreement_staging / "manifest.json", agreement_manifest)
    agreement_dir = (
        root
        / "outputs"
        / "micro_sensor_annual_agreement"
        / "generations"
        / agreement_generation
    )
    agreement_dir.parent.mkdir(parents=True)
    agreement_staging.rename(agreement_dir)

    satellite_dir = (
        root
        / "outputs"
        / "m8_satellite"
        / "generations"
        / ("e" * 64)
        / "year=2025"
    )
    satellite_dir.mkdir(parents=True)
    satellite_rows: list[dict[str, object]] = [
        {
            "source": source,
            "station_name": f"station-{station_index:02d}",
            "month": date(2025, 10, 1),
            "satellite_value": float(station_index + 1),
            "satellite_unit": "fixture",
            "ground_value": float(station_index + 2),
            "ground_unit": "ug/m3",
            "satellite_observed": True,
            "ground_row_present": True,
            "ground_meets_threshold": True,
            "ground_observed": True,
            "ground_withheld": False,
            "pair_observed": True,
            "collection_id": f"fixture-{source}",
            "band": "fixture",
            "sample_scale_m": 1000,
        }
        for station_index in range(12)
        for source in ("maiac", "s5p", "era5")
    ]
    satellite_path = satellite_dir / "panel.parquet"
    _frame(_SATELLITE_SCHEMA, satellite_rows).write_parquet(satellite_path)
    coordinate_records = (
        reviewed_geography.select(*_COORDINATE_FIELDS).sort("station_name").to_dicts()
    )
    airzone_records = (
        reviewed_geography.select(*_COORDINATE_FIELDS, "airzone_official")
        .sort("station_name")
        .to_dicts()
    )
    _write_json(
        root / "fixture-bindings.json",
        {
            "annual_generation_sha256": annual_generation,
            "annual_manifest_sha256": sha256_file(annual_dir / "manifest.json"),
            "agreement_generation_sha256": agreement_generation,
            "agreement_manifest_sha256": sha256_file(agreement_dir / "manifest.json"),
            "agreement_summary_sha256": _canonical_hash(agreement_summary),
            "satellite_generation_sha256": "e" * 64,
            "satellite_panel_bytes": satellite_path.stat().st_size,
            "satellite_panel_sha256": sha256_file(satellite_path),
            "reviewed_geography_sha256": _canonical_hash(coordinate_records),
            "reviewed_airzone_sha256": _canonical_hash(airzone_records),
        },
    )
    return annual_rows, agreement_rows


def shipped_config_payload() -> dict[str, Any]:
    return copy.deepcopy(load_conf("micro_sensor_agreement_audit"))


def mutate_config_path(payload: dict[str, Any], path: str) -> None:
    _, field = path.split(".", maxsplit=1)
    analysis = payload["analysis"]
    if field == "claim_boundary":
        analysis[field]["validated_calibration"] = True
    elif field.endswith("sha256"):
        analysis[field] = "0" * 64
    elif field == "ridge_alpha":
        analysis[field] = 2.0
    else:
        analysis[field] = int(analysis[field]) + 1


def synthetic_audit_config(root: Path) -> AgreementAuditConfig:
    bindings = json.loads((root / "fixture-bindings.json").read_text(encoding="utf-8"))
    return replace(
        load_micro_sensor_agreement_audit_config(),
        **bindings,
    )


def mutate_frozen_fixture(root: Path, mutation: str) -> None:
    annual_dir = next(
        (root / "outputs" / "micro_sensor_annual_readiness" / "generations").iterdir()
    )
    agreement_dir = next(
        (root / "outputs" / "micro_sensor_annual_agreement" / "generations").iterdir()
    )
    if mutation == "manifest_hash":
        with (annual_dir / "manifest.json").open("ab") as handle:
            handle.write(b"\n")
    elif mutation == "member_hash":
        with (annual_dir / "calendar_coverage.parquet").open("ab") as handle:
            handle.write(b"changed")
    elif mutation == "extra_member":
        (agreement_dir / "unexpected.txt").write_text("unexpected\n", encoding="utf-8")
    else:
        manifest = agreement_dir / "manifest.json"
        target = root / "linked-manifest.json"
        target.write_bytes(manifest.read_bytes())
        manifest.unlink()
        try:
            manifest.symlink_to(target)
        except OSError as exc:
            pytest.skip(f"Windows denied symlink creation: {exc}")


@pytest.fixture
def audit_source_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    annual_rows, agreement_rows = _create_frozen_source(tmp_path)
    monkeypatch.setattr(audit, "_ANNUAL_EXPECTED_ROWS", annual_rows)
    monkeypatch.setattr(audit, "_AGREEMENT_EXPECTED_ROWS", agreement_rows)
    monkeypatch.setattr(audit, "resolve_station_geo", _geography)
    return tmp_path


@pytest.fixture
def loaded_audit_fixture(
    audit_source_fixture: Path,
) -> tuple[FrozenAuditInputs, AgreementAuditConfig]:
    config = synthetic_audit_config(audit_source_fixture)
    return load_frozen_agreement_audit_inputs(audit_source_fixture, config), config


def test_shipped_config_pins_the_audit_protocol() -> None:
    config = load_micro_sensor_agreement_audit_config()
    assert config.protocol_revision == 1
    assert config.annual_git_sha == "e4839bc"
    assert config.agreement_git_sha == "b7bff3e"
    assert config.permutation_draws == 999
    assert config.bootstrap_draws == 1999
    assert config.target_time_shifts_days == (7, 14, 28)
    assert config.neighbor_exclusion_buffers_km == (0.5, 1.0, 2.0)


@pytest.mark.parametrize(
    "path",
    [
        "analysis.protocol_revision",
        "analysis.annual_generation_sha256",
        "analysis.annual_manifest_sha256",
        "analysis.agreement_generation_sha256",
        "analysis.agreement_manifest_sha256",
        "analysis.satellite_generation_sha256",
        "analysis.satellite_panel_sha256",
        "analysis.satellite_panel_bytes",
        "analysis.reviewed_geography_sha256",
        "analysis.reviewed_airzone_sha256",
        "analysis.ridge_alpha",
        "analysis.permutation_draws",
        "analysis.bootstrap_draws",
        "analysis.claim_boundary",
    ],
)
def test_config_rejects_each_scientific_drift(path: str) -> None:
    payload = shipped_config_payload()
    mutate_config_path(payload, path)
    with pytest.raises(ConfigError, match=re.escape(path)):
        load_micro_sensor_agreement_audit_config(payload)


def test_frozen_input_loader_accepts_the_exact_fixture(audit_source_fixture: Path) -> None:
    inputs = load_frozen_agreement_audit_inputs(
        audit_source_fixture, synthetic_audit_config(audit_source_fixture)
    )
    assert inputs.agreement_folds.height == 29
    assert inputs.agreement_paired_days.filter(pl.col("reason") == "eligible").height == 36
    assert inputs.satellite_panel.select("source").n_unique() == 3


@pytest.mark.parametrize("mutation", ["manifest_hash", "member_hash", "extra_member", "link"])
def test_frozen_input_loader_rejects_bound_input_mutation(
    audit_source_fixture: Path, mutation: str
) -> None:
    mutate_frozen_fixture(audit_source_fixture, mutation)
    with pytest.raises(RuntimeError, match="frozen input"):
        load_frozen_agreement_audit_inputs(
            audit_source_fixture, synthetic_audit_config(audit_source_fixture)
        )
