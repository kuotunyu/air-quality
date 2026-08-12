"""Raw micro-sensor rows stay visible while becoming a bounded interim table."""

from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path

import polars as pl
import pytest

from twair.config import ConfigError
from twair.ingest import micro_sensor_observations
from twair.ingest.micro_sensor_observations import (
    OBSERVATION_OUTPUT_SCHEMA,
    load_micro_sensor_observation_generation,
    load_micro_sensor_parser_contract,
    parse_micro_sensor_observation_generation,
    parse_observation_csv,
)
from twair.ingest.micro_sensors import acquire_micro_sensor_day

from .test_micro_sensors import (
    _ArchiveBackend,
    _written_observation_catalog,
    _zip_bytes,
)

VALUE_COLUMNS = {
    "pm25": "PM2.5",
    "humidity": "humidity",
    "temperature": "temperature",
}


def _csv(variable: str, *rows: tuple[str, str, str, str, str]) -> bytes:
    header = f'"deviceId","{VALUE_COLUMNS[variable]}","time","lon","lat"\r\n'
    body = "".join(",".join(f'"{value}"' for value in row) + "\r\n" for row in rows)
    return (header + body).encode()


def _write_csv(
    tmp_path: Path,
    variable: str,
    *rows: tuple[str, str, str, str, str],
) -> Path:
    path = tmp_path / f"{variable}.csv"
    path.write_bytes(_csv(variable, *rows))
    return path


def _write_reviewed_csv(
    tmp_path: Path,
    *,
    header: tuple[str, str, str, str, str],
    row: tuple[str, str, str, str, str],
) -> Path:
    path = tmp_path / "reviewed.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerow(row)
    return path


def _reviewed_csv_bytes(
    header: tuple[str, str, str, str, str],
    row: tuple[str, str, str, str, str],
) -> bytes:
    text = io.StringIO(newline="")
    writer = csv.writer(text)
    writer.writerow(header)
    writer.writerow(row)
    return text.getvalue().encode()


def _raw_generation_from_payloads(
    tmp_path: Path,
    payloads: dict[str, bytes],
) -> tuple[str, Path, Path, Path]:
    snapshot, raw_catalog_root, interim_catalog_root = _written_observation_catalog(
        tmp_path,
        payloads,
    )
    raw_observation_root = tmp_path / "raw-observations"
    written = acquire_micro_sensor_day(
        snapshot.generation_sha256,
        day="2025-01-01",
        backend=_ArchiveBackend(payloads),
        raw_catalog_root=raw_catalog_root,
        interim_catalog_root=interim_catalog_root,
        observation_root=raw_observation_root,
        generated_at="2026-08-12T04:00:00+00:00",
        git_sha="abc1234",
        git_dirty=False,
    )
    return (
        written.generation_sha256,
        raw_observation_root,
        raw_catalog_root,
        interim_catalog_root,
    )


def _raw_generation(tmp_path: Path) -> tuple[str, Path, Path, Path]:
    payloads = {
        variable: _zip_bytes(
            (
                f"moenv_micro_{variable}_20250101.csv",
                _csv(variable, ("1", "1", "2025-01-01 00:00:00", "120", "23")),
            )
        )
        for variable in VALUE_COLUMNS
    }
    return _raw_generation_from_payloads(tmp_path, payloads)


def _new_schema_raw_generation(tmp_path: Path) -> tuple[str, Path, Path, Path]:
    records = {
        "pm25": (
            ("stationID", "phenomenonTime", "PM2.5", "StationLongitude", "StationLatitude"),
            ("1", "2025-01-01 00:00:00", "12.5", "120", "23"),
        ),
        "humidity": (
            (
                "stationID",
                "Relative humidity",
                "phenomenonTime",
                "StationLongitude",
                "StationLatitude",
            ),
            ("1", "50", "2025-01-01 00:00:00", "120", "23"),
        ),
        "temperature": (
            (
                "stationID",
                "phenomenonTime",
                "Temperature",
                "StationLongitude",
                "StationLatitude",
            ),
            ("1", "2025-01-01 00:00:00", "20", "120", "23"),
        ),
    }
    payloads = {
        variable: _zip_bytes(
            (
                f"moenv_micro_{variable}_20250101.csv",
                _reviewed_csv_bytes(header, row),
            )
        )
        for variable, (header, row) in records.items()
    }
    return _raw_generation_from_payloads(tmp_path, payloads)


def test_source_rows_nulls_repeated_keys_and_invalid_coordinates_are_preserved(
    tmp_path: Path,
) -> None:
    source = _write_csv(
        tmp_path,
        "pm25",
        ("device-a", "12.5", "2025-01-01 00:00:00", "120.5", "23.5"),
        ("device-a", "", "2025-01-01 00:00:00", "120.5", "120.5"),
        ("device-b", "0", "", "", ""),
    )
    destination = tmp_path / "pm25.parquet"

    summary = parse_observation_csv(
        source,
        destination,
        variable="pm25",
        day="2025-01-01",
    )

    parsed = pl.read_parquet(destination)
    assert parsed.schema == pl.Schema(OBSERVATION_OUTPUT_SCHEMA)
    assert parsed["source_row_number"].to_list() == [1, 2, 3]
    assert parsed["value"].to_list() == [12.5, None, 0.0]
    assert parsed["coordinate_wgs84_valid"].to_list() == [True, False, None]
    assert parsed.filter(pl.col("device_id") == "device-a").height == 2
    assert summary.rows == 3
    assert summary.null_counts == {"device_id": 0, "ts_local": 1, "value": 1, "lon": 1, "lat": 1}
    assert summary.duplicate_key_groups == 1
    assert summary.rows_in_duplicate_keys == 2
    assert summary.coordinate_wgs84_invalid_rows == 1


@pytest.mark.parametrize(
    "row, message",
    [
        (("device-a", "not-a-value", "2025-01-01 00:00:00", "120", "23"), "value"),
        (("device-a", "1", "not-a-time", "120", "23"), "time"),
        (("device-a", "1", "2025-01-01 00:00:00", "not-a-lon", "23"), "longitude"),
        (("device-a", "1", "2025-01-01 00:00:00", "120", "not-a-lat"), "latitude"),
        (("device-a", "NaN", "2025-01-01 00:00:00", "120", "23"), "finite"),
        (("device-a", " ", "2025-01-01 00:00:00", "120", "23"), "value"),
        (("device-a", "1", " ", "120", "23"), "time"),
        (("device-a", "1", "2025-01-01 00:00:00", " ", "23"), "longitude"),
        (("device-a", "1", "2025-01-01 00:00:00", "120", " "), "latitude"),
    ],
)
def test_nonempty_malformed_or_nonfinite_fields_stop_the_whole_parse(
    tmp_path: Path,
    row: tuple[str, str, str, str, str],
    message: str,
) -> None:
    source = _write_csv(tmp_path, "pm25", row)
    destination = tmp_path / "pm25.parquet"

    with pytest.raises(ConfigError, match=message):
        parse_observation_csv(
            source,
            destination,
            variable="pm25",
            day="2025-01-01",
        )

    assert not destination.exists()


def test_a_row_outside_the_generation_day_is_rejected_not_dropped(tmp_path: Path) -> None:
    source = _write_csv(
        tmp_path,
        "temperature",
        ("device-a", "18", "2025-01-02 00:00:00", "120", "23"),
    )
    destination = tmp_path / "temperature.parquet"

    with pytest.raises(ConfigError, match="outside 2025-01-01"):
        parse_observation_csv(
            source,
            destination,
            variable="temperature",
            day="2025-01-01",
        )

    assert not destination.exists()


def test_a_provider_header_change_stops_before_any_parquet_is_written(tmp_path: Path) -> None:
    source = tmp_path / "pm25.csv"
    source.write_text("deviceId,value,time,lon,lat\n", encoding="utf-8")
    destination = tmp_path / "pm25.parquet"

    with pytest.raises(ConfigError, match="header"):
        parse_observation_csv(
            source,
            destination,
            variable="pm25",
            day="2025-01-01",
        )

    assert not destination.exists()


@pytest.mark.parametrize(
    (
        "variable",
        "header",
        "row",
        "expected_device",
        "expected_value",
        "expected_lon",
        "expected_lat",
    ),
    [
        (
            "pm25",
            (
                "stationID",
                "phenomenonTime",
                "PM2.5",
                "StationLongitude",
                "StationLatitude",
            ),
            ("11803873982", "2025-02-05 00:00:00", "24.12", "", ""),
            "11803873982",
            24.12,
            None,
            None,
        ),
        (
            "humidity",
            (
                "stationID",
                "Relative humidity",
                "phenomenonTime",
                "StationLongitude",
                "StationLatitude",
            ),
            ("12662618508", "54.68", "2025-02-05 00:00:00", "120.1520600", "23.6341300"),
            "12662618508",
            54.68,
            120.15206,
            23.63413,
        ),
        (
            "temperature",
            (
                "stationID",
                "phenomenonTime",
                "Temperature",
                "StationLongitude",
                "StationLatitude",
            ),
            ("13430718225", "2025-02-05 00:00:03", "12.38", "121.1933440", "25.0779330"),
            "13430718225",
            12.38,
            121.193344,
            25.077933,
        ),
    ],
)
def test_each_measured_moenviot_header_maps_to_the_existing_canonical_schema(
    tmp_path: Path,
    variable: str,
    header: tuple[str, str, str, str, str],
    row: tuple[str, str, str, str, str],
    expected_device: str,
    expected_value: float,
    expected_lon: float | None,
    expected_lat: float | None,
) -> None:
    source = _write_reviewed_csv(tmp_path, header=header, row=row)
    destination = tmp_path / f"{variable}.parquet"

    summary = parse_observation_csv(
        source,
        destination,
        variable=variable,
        day="2025-02-05",
    )

    parsed = pl.read_parquet(destination)
    assert parsed.schema == pl.Schema(OBSERVATION_OUTPUT_SCHEMA)
    assert parsed["device_id"].to_list() == [expected_device]
    assert parsed["value"].to_list() == [expected_value]
    assert parsed["ts_local"].dt.to_string("%Y-%m-%d %H:%M:%S").to_list() == [
        row[1] if variable != "humidity" else row[2]
    ]
    assert parsed["lon"].to_list() == [expected_lon]
    assert parsed["lat"].to_list() == [expected_lat]
    assert summary.rows == 1
    assert summary.null_counts["value"] == 0


def test_a_reordered_moenviot_header_is_not_guessed_from_column_names(tmp_path: Path) -> None:
    source = _write_reviewed_csv(
        tmp_path,
        header=(
            "stationID",
            "PM2.5",
            "phenomenonTime",
            "StationLongitude",
            "StationLatitude",
        ),
        row=("11803873982", "24.12", "2025-02-05 00:00:00", "120", "23"),
    )
    destination = tmp_path / "pm25.parquet"

    with pytest.raises(ConfigError, match="header"):
        parse_observation_csv(
            source,
            destination,
            variable="pm25",
            day="2025-02-05",
        )

    assert not destination.exists()


def test_a_short_csv_record_is_rejected_instead_of_becoming_source_nulls(
    tmp_path: Path,
) -> None:
    source = tmp_path / "pm25.csv"
    source.write_text(
        '"deviceId","PM2.5","time","lon","lat"\n"device-a","1","2025-01-01 00:00:00"\n',
        encoding="utf-8",
    )
    destination = tmp_path / "pm25.parquet"

    with pytest.raises(ConfigError, match="five fields"):
        parse_observation_csv(
            source,
            destination,
            variable="pm25",
            day="2025-01-01",
        )

    assert not destination.exists()


def test_one_raw_generation_becomes_three_immutable_parquet_members_and_is_reused(
    tmp_path: Path,
) -> None:
    raw_generation, raw_root, raw_catalog_root, interim_catalog_root = _raw_generation(tmp_path)
    interim_root = tmp_path / "parsed-observations"

    written = parse_micro_sensor_observation_generation(
        raw_generation,
        raw_observation_root=raw_root,
        raw_catalog_root=raw_catalog_root,
        interim_catalog_root=interim_catalog_root,
        interim_observation_root=interim_root,
        generated_at="2026-08-12T05:00:00+00:00",
        git_sha="abc1234",
        git_dirty=False,
    )

    assert set(written.manifest["members"]) == {
        "humidity.parquet",
        "pm25.parquet",
        "temperature.parquet",
    }
    assert sum(member["summary"]["rows"] for member in written.manifest["members"].values()) == 3
    assert {path.name for path in written.directory.iterdir()} == {
        "manifest.json",
        "humidity.parquet",
        "pm25.parquet",
        "temperature.parquet",
    }
    pm25 = pl.read_parquet(written.directory / "pm25.parquet")
    assert pm25.select("lon", "lat").row(0) == (120.0, 23.0)
    generation_identity = {
        key: written.manifest[key]
        for key in (
            "schema_version",
            "request_sha256",
            "raw_observation_generation_sha256",
            "date",
            "parser_contract_sha256",
            "raw_members",
            "members",
        )
    }
    expected_generation = hashlib.sha256(
        (
            json.dumps(
                generation_identity,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode()
    ).hexdigest()
    assert written.generation_sha256 == expected_generation
    assert written.manifest["parser_contract"] == {
        "timestamp_format": "%Y-%m-%d %H:%M:%S",
        "value_columns": VALUE_COLUMNS,
        "output_schema": {name: str(dtype) for name, dtype in OBSERVATION_OUTPUT_SCHEMA},
    }

    second = parse_micro_sensor_observation_generation(
        raw_generation,
        raw_observation_root=raw_root,
        raw_catalog_root=raw_catalog_root,
        interim_catalog_root=interim_catalog_root,
        interim_observation_root=interim_root,
    )
    assert second.directory == written.directory


def test_a_new_header_generation_binds_only_the_three_selected_source_schemas(
    tmp_path: Path,
) -> None:
    raw_generation, raw_root, raw_catalog_root, interim_catalog_root = _new_schema_raw_generation(
        tmp_path
    )

    written = parse_micro_sensor_observation_generation(
        raw_generation,
        raw_observation_root=raw_root,
        raw_catalog_root=raw_catalog_root,
        interim_catalog_root=interim_catalog_root,
        interim_observation_root=tmp_path / "parsed-observations",
    )

    source_schemas = written.manifest["parser_contract"]["source_schemas"]
    assert set(source_schemas) == {"pm25", "humidity", "temperature"}
    assert source_schemas["pm25"] == {
        "header": [
            "stationID",
            "phenomenonTime",
            "PM2.5",
            "StationLongitude",
            "StationLatitude",
        ],
        "device_id": "stationID",
        "value": "PM2.5",
        "timestamp": "phenomenonTime",
        "longitude": "StationLongitude",
        "latitude": "StationLatitude",
    }
    assert "schemas" not in written.manifest["parser_contract"]


def test_a_changed_parquet_member_is_rejected_instead_of_reparsed(tmp_path: Path) -> None:
    raw_generation, raw_root, raw_catalog_root, interim_catalog_root = _raw_generation(tmp_path)
    interim_root = tmp_path / "parsed-observations"
    written = parse_micro_sensor_observation_generation(
        raw_generation,
        raw_observation_root=raw_root,
        raw_catalog_root=raw_catalog_root,
        interim_catalog_root=interim_catalog_root,
        interim_observation_root=interim_root,
    )
    member = written.directory / "pm25.parquet"
    member.write_bytes(member.read_bytes() + b"changed")

    with pytest.raises(RuntimeError, match="checksum changed"):
        parse_micro_sensor_observation_generation(
            raw_generation,
            raw_observation_root=raw_root,
            raw_catalog_root=raw_catalog_root,
            interim_catalog_root=interim_catalog_root,
            interim_observation_root=interim_root,
        )


def test_a_changed_raw_archive_is_rejected_before_parsing(tmp_path: Path) -> None:
    raw_generation, raw_root, raw_catalog_root, interim_catalog_root = _raw_generation(tmp_path)
    archive = next((raw_root / raw_generation).glob("*.zip"))
    archive.write_bytes(archive.read_bytes() + b"changed")

    with pytest.raises(RuntimeError, match="checksum changed"):
        parse_micro_sensor_observation_generation(
            raw_generation,
            raw_observation_root=raw_root,
            raw_catalog_root=raw_catalog_root,
            interim_catalog_root=interim_catalog_root,
            interim_observation_root=tmp_path / "parsed-observations",
        )


def test_an_archive_with_two_members_is_rejected_without_publishing(tmp_path: Path) -> None:
    payloads = {
        variable: _zip_bytes(
            (
                f"moenv_micro_{variable}_20250101.csv",
                _csv(variable, ("device-a", "1", "2025-01-01 00:00:00", "120", "23")),
            ),
            *((("unexpected.csv", b"extra\n"),) if variable == "pm25" else ()),
        )
        for variable in VALUE_COLUMNS
    }
    snapshot, raw_catalog_root, interim_catalog_root = _written_observation_catalog(
        tmp_path,
        payloads,
    )
    raw_root = tmp_path / "raw-observations"
    raw = acquire_micro_sensor_day(
        snapshot.generation_sha256,
        day="2025-01-01",
        backend=_ArchiveBackend(payloads),
        raw_catalog_root=raw_catalog_root,
        interim_catalog_root=interim_catalog_root,
        observation_root=raw_root,
    )
    interim_root = tmp_path / "parsed-observations"

    with pytest.raises(ConfigError, match="exactly one CSV member"):
        parse_micro_sensor_observation_generation(
            raw.generation_sha256,
            raw_observation_root=raw_root,
            raw_catalog_root=raw_catalog_root,
            interim_catalog_root=interim_catalog_root,
            interim_observation_root=interim_root,
        )

    assert [path for path in interim_root.iterdir() if path.is_dir()] == []


@pytest.mark.parametrize("phase", ["extract", "parse"])
def test_an_interrupted_member_step_removes_only_its_staging_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    raw_generation, raw_root, raw_catalog_root, interim_catalog_root = _raw_generation(tmp_path)
    interim_root = tmp_path / "parsed-observations"

    if phase == "extract":

        def interrupt_extract(_archive: Path, destination: Path) -> None:
            destination.write_bytes(b"partial")
            raise KeyboardInterrupt

        monkeypatch.setattr(
            micro_sensor_observations,
            "_extract_only_member",
            interrupt_extract,
        )
    else:

        def interrupt_parse(_source: Path, destination: Path, **_kwargs: object) -> None:
            destination.with_suffix(".parquet.part").write_bytes(b"partial")
            raise KeyboardInterrupt

        monkeypatch.setattr(
            micro_sensor_observations,
            "parse_observation_csv",
            interrupt_parse,
        )

    with pytest.raises(KeyboardInterrupt):
        parse_micro_sensor_observation_generation(
            raw_generation,
            raw_observation_root=raw_root,
            raw_catalog_root=raw_catalog_root,
            interim_catalog_root=interim_catalog_root,
            interim_observation_root=interim_root,
        )

    assert list(interim_root.glob(".staging-*")) == []
    assert [path for path in interim_root.iterdir() if path.is_dir()] == []


def test_only_one_csv_member_is_extracted_at_a_time(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_generation, raw_root, raw_catalog_root, interim_catalog_root = _raw_generation(tmp_path)
    interim_root = tmp_path / "parsed-observations"
    original_extract = micro_sensor_observations._extract_only_member
    simultaneous_members: list[int] = []

    def observe_extract(archive: Path, destination: Path) -> None:
        original_extract(archive, destination)
        simultaneous_members.append(len(list(destination.parent.glob(".*-source.csv"))))

    monkeypatch.setattr(
        micro_sensor_observations,
        "_extract_only_member",
        observe_extract,
    )

    parse_micro_sensor_observation_generation(
        raw_generation,
        raw_observation_root=raw_root,
        raw_catalog_root=raw_catalog_root,
        interim_catalog_root=interim_catalog_root,
        interim_observation_root=interim_root,
    )

    assert simultaneous_members == [1, 1, 1]


def test_an_interrupted_publish_leaves_no_generation_or_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_generation, raw_root, raw_catalog_root, interim_catalog_root = _raw_generation(tmp_path)
    interim_root = tmp_path / "parsed-observations"
    original_replace = Path.replace

    def interrupt_publish(self: Path, target: Path) -> Path:
        if interim_root in target.parents and self.name.startswith(".staging-"):
            raise KeyboardInterrupt
        return original_replace(self, target)

    with monkeypatch.context() as patcher:
        patcher.setattr(Path, "replace", interrupt_publish)
        with pytest.raises(KeyboardInterrupt):
            parse_micro_sensor_observation_generation(
                raw_generation,
                raw_observation_root=raw_root,
                raw_catalog_root=raw_catalog_root,
                interim_catalog_root=interim_catalog_root,
                interim_observation_root=interim_root,
            )

    assert list(interim_root.glob(".staging-*")) == []
    assert [path for path in interim_root.iterdir() if path.is_dir()] == []


def test_the_shipped_parser_contract_names_only_the_reviewed_schema_variants() -> None:
    contract = load_micro_sensor_parser_contract()

    assert contract.timestamp_format == "%Y-%m-%d %H:%M:%S"
    assert contract.value_columns == {
        "pm25": "PM2.5",
        "humidity": "humidity",
        "temperature": "temperature",
    }
    assert {
        variable: [
            {
                "header": schema.header,
                "device_id": schema.device_id,
                "value": schema.value,
                "timestamp": schema.timestamp,
                "longitude": schema.longitude,
                "latitude": schema.latitude,
            }
            for schema in schemas
        ]
        for variable, schemas in contract.schemas.items()
    } == {
        "pm25": [
            {
                "header": ("deviceId", "PM2.5", "time", "lon", "lat"),
                "device_id": "deviceId",
                "value": "PM2.5",
                "timestamp": "time",
                "longitude": "lon",
                "latitude": "lat",
            },
            {
                "header": (
                    "stationID",
                    "phenomenonTime",
                    "PM2.5",
                    "StationLongitude",
                    "StationLatitude",
                ),
                "device_id": "stationID",
                "value": "PM2.5",
                "timestamp": "phenomenonTime",
                "longitude": "StationLongitude",
                "latitude": "StationLatitude",
            },
        ],
        "humidity": [
            {
                "header": ("deviceId", "humidity", "time", "lon", "lat"),
                "device_id": "deviceId",
                "value": "humidity",
                "timestamp": "time",
                "longitude": "lon",
                "latitude": "lat",
            },
            {
                "header": (
                    "stationID",
                    "Relative humidity",
                    "phenomenonTime",
                    "StationLongitude",
                    "StationLatitude",
                ),
                "device_id": "stationID",
                "value": "Relative humidity",
                "timestamp": "phenomenonTime",
                "longitude": "StationLongitude",
                "latitude": "StationLatitude",
            },
        ],
        "temperature": [
            {
                "header": ("deviceId", "temperature", "time", "lon", "lat"),
                "device_id": "deviceId",
                "value": "temperature",
                "timestamp": "time",
                "longitude": "lon",
                "latitude": "lat",
            },
            {
                "header": (
                    "stationID",
                    "phenomenonTime",
                    "Temperature",
                    "StationLongitude",
                    "StationLatitude",
                ),
                "device_id": "stationID",
                "value": "Temperature",
                "timestamp": "phenomenonTime",
                "longitude": "StationLongitude",
                "latitude": "StationLatitude",
            },
        ],
    }


def test_the_read_only_loader_validates_and_returns_one_existing_generation(
    tmp_path: Path,
) -> None:
    raw_generation, raw_root, raw_catalog_root, interim_catalog_root = _raw_generation(tmp_path)
    interim_root = tmp_path / "parsed-observations"
    written = parse_micro_sensor_observation_generation(
        raw_generation,
        raw_observation_root=raw_root,
        raw_catalog_root=raw_catalog_root,
        interim_catalog_root=interim_catalog_root,
        interim_observation_root=interim_root,
    )

    loaded = load_micro_sensor_observation_generation(
        written.generation_sha256,
        interim_observation_root=interim_root,
    )

    assert loaded.generation_sha256 == written.generation_sha256
    assert loaded.directory == written.directory
    assert loaded.manifest == written.manifest


def test_the_read_only_loader_never_creates_a_missing_generation(tmp_path: Path) -> None:
    root = tmp_path / "parsed-observations"

    with pytest.raises(FileNotFoundError, match="parsed generation"):
        load_micro_sensor_observation_generation("a" * 64, interim_observation_root=root)

    assert not root.exists()


@pytest.mark.parametrize("identity", ["short", "g" * 64, "A" * 64])
def test_the_read_only_loader_requires_a_lowercase_sha256_identity(
    tmp_path: Path,
    identity: str,
) -> None:
    with pytest.raises(ValueError, match="64-character lowercase SHA-256"):
        load_micro_sensor_observation_generation(
            identity,
            interim_observation_root=tmp_path / "parsed-observations",
        )


def test_the_read_only_loader_rejects_a_tampered_member(tmp_path: Path) -> None:
    raw_generation, raw_root, raw_catalog_root, interim_catalog_root = _raw_generation(tmp_path)
    interim_root = tmp_path / "parsed-observations"
    written = parse_micro_sensor_observation_generation(
        raw_generation,
        raw_observation_root=raw_root,
        raw_catalog_root=raw_catalog_root,
        interim_catalog_root=interim_catalog_root,
        interim_observation_root=interim_root,
    )
    member = written.directory / "temperature.parquet"
    member.write_bytes(member.read_bytes() + b"changed")

    with pytest.raises(RuntimeError, match="checksum changed"):
        load_micro_sensor_observation_generation(
            written.generation_sha256,
            interim_observation_root=interim_root,
        )
