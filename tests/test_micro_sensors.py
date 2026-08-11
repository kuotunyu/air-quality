"""The official micro-sensor catalogue is preserved before observations are fetched."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import polars as pl
import pytest
import respx

from twair.config import ConfigError
from twair.ingest.micro_sensors import (
    FileGatorHistoryBackend,
    MicroSensorCatalogSnapshot,
    build_catalog_snapshot,
    load_micro_sensor_source,
    normalize_month_catalog,
    parse_station_metadata,
    write_catalog_snapshot,
)

STATION_HEADER = "deviceId,locationId,desc,lat,lon,area,areatype,town,county,project_name\n"


def _station_csv(*rows: str) -> bytes:
    return (STATION_HEADER + "\n".join(rows) + "\n").encode()


def _file(name: str, *, size: int, time: int) -> dict[str, Any]:
    root = "/空氣品質/環境部_智慧城鄉空品微型感測器/202501"
    return {
        "type": "file",
        "path": f"{root}/{name}",
        "name": name,
        "size": size,
        "time": time,
        "permissions": 644,
    }


def _directory(*files: dict[str, Any]) -> dict[str, Any]:
    root = "/空氣品質/環境部_智慧城鄉空品微型感測器/202501"
    return {
        "data": {
            "location": root,
            "files": [
                {
                    "type": "back",
                    "path": "/空氣品質/環境部_智慧城鄉空品微型感測器",
                    "name": "..",
                    "size": 0,
                    "time": 0,
                    "permissions": -1,
                },
                *files,
            ],
        }
    }


def test_the_shipped_source_contract_names_the_official_guest_history_service() -> None:
    source = load_micro_sensor_source()

    assert source.history_base_url == "https://history.colife.org.tw/"
    assert source.history_root_path == "/空氣品質/環境部_智慧城鄉空品微型感測器"
    assert source.station_metadata_filename == "MOENV_iot_station.csv"
    assert source.required_variables == ("pm25", "humidity", "temperature")
    assert source.max_directory_entries == 1000


def test_station_rows_keep_empty_descriptions_and_duplicate_coordinates() -> None:
    raw = _station_csv(
        "12796768701,GR0112,,24,120,,,,苗栗縣,108年及109年度苗栗縣空氣品質感測物聯網維運計畫",
        "12783787849,GR0155,,24,120,,,,苗栗縣,108年及109年度苗栗縣空氣品質感測物聯網維運計畫",
        "6876856151,TW050104A0201978,柏昇SAQ-200,25.076273,121.186213,大園,,,桃園市,107年度桃園市空氣品質感測物聯網布建計畫",
    )

    stations = parse_station_metadata(raw)

    assert stations.columns == [
        "device_id",
        "location_id",
        "description",
        "lat",
        "lon",
        "area",
        "area_type",
        "township",
        "county",
        "project_name",
    ]
    assert stations.height == 3
    assert stations.schema["device_id"] == pl.String
    assert stations.schema["lat"] == pl.Float64
    assert stations.schema["lon"] == pl.Float64
    assert stations["description"].to_list()[:2] == ["", ""]
    assert stations.filter((pl.col("lat") == 24) & (pl.col("lon") == 120)).height == 2


def test_duplicate_device_ids_are_rejected_instead_of_deduplicated() -> None:
    raw = _station_csv(
        "7737132222,TW020101A0201227,柏昇 / SAQ-200-002,25.08876,121.69755,六堵科技園區,鄰近工業區社區,七堵區,基隆市,107年度基隆市空氣品質感測物聯網布建計畫",
        "7737132222,TW020101D0201288,柏昇 / SAQ-200-002,25.14657,121.70029,基隆市人工測站,輔助區,安樂區,基隆市,107年度基隆市空氣品質感測物聯網布建計畫",
    )

    with pytest.raises(ConfigError, match="duplicate deviceId"):
        parse_station_metadata(raw)


@pytest.mark.parametrize(
    "raw, message",
    [
        (
            b"deviceId,locationId,lat,lon\n1,L1,24,120\n",
            "station metadata columns",
        ),
        (
            _station_csv("1,L1,device,not-a-latitude,120,area,type,town,county,project"),
            "latitude or longitude",
        ),
        (
            _station_csv("1,L1,device,95,120,area,type,town,county,project"),
            "WGS84",
        ),
    ],
)
def test_invalid_station_schema_or_coordinates_fail_loudly(raw: bytes, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        parse_station_metadata(raw)


def test_a_month_is_a_dense_calendar_matrix_with_provider_absence_kept_as_null() -> None:
    payload = _directory(
        _file("moenv_micro_humidity_20250101.zip", size=207_751_024, time=1_735_754_403),
        _file("moenv_micro_pm25_20250101.zip", size=215_145_898, time=1_735_754_405),
        _file("moenv_micro_temperature_20250101.zip", size=202_534_238, time=1_735_754_404),
    )

    catalog = normalize_month_catalog(payload, month="202501")

    assert catalog.height == 31 * 3
    assert catalog.filter(pl.col("archive_present")).height == 3
    absent = catalog.filter(
        (pl.col("date") == pl.date(2025, 1, 2)) & (pl.col("variable") == "pm25")
    )
    assert absent["archive_present"].item() is False
    assert absent["filename"].item() is None
    assert absent["bytes"].item() is None
    assert absent["modified_unix"].item() is None
    present = catalog.filter(
        (pl.col("date") == pl.date(2025, 1, 1)) & (pl.col("variable") == "pm25")
    )
    assert present["filename_prefix"].item() == "moenv_micro"
    assert present["bytes"].item() == 215_145_898


def test_the_later_observed_filename_prefix_is_captured_not_hard_coded() -> None:
    payload = {
        "data": {
            "location": "/空氣品質/環境部_智慧城鄉空品微型感測器/202508",
            "files": [
                {
                    "type": "file",
                    "path": "/空氣品質/環境部_智慧城鄉空品微型感測器/202508/moenviot_pm25_20250801.zip",
                    "name": "moenviot_pm25_20250801.zip",
                    "size": 200_000_000,
                    "time": 1_754_000_000,
                    "permissions": 644,
                }
            ],
        }
    }

    catalog = normalize_month_catalog(payload, month="202508")

    row = catalog.filter(pl.col("archive_present"))
    assert row["filename_prefix"].item() == "moenviot"
    assert row["variable"].item() == "pm25"


@pytest.mark.parametrize(
    "payload, message",
    [
        (
            _directory(
                _file("moenv_micro_pm25_20250101.zip", size=1, time=1),
                _file("second_pm25_20250101.zip", size=2, time=2),
            ),
            "duplicate archive",
        ),
        (
            _directory(_file("moenv_micro_pm25_20250201.zip", size=1, time=1)),
            "outside requested month",
        ),
        (
            _directory(_file("README.zip", size=1, time=1)),
            "unrecognised archive entry",
        ),
        (
            {"data": {"location": "wrong", "files": []}},
            "directory location",
        ),
    ],
)
def test_provider_directory_drift_is_rejected_instead_of_silently_ignored(
    payload: dict[str, Any], message: str
) -> None:
    with pytest.raises(ConfigError, match=message):
        normalize_month_catalog(payload, month="202501")


@pytest.mark.parametrize("month", ["2025-01", "202513", "20250", "month"])
def test_invalid_months_are_rejected_before_any_calendar_is_built(month: str) -> None:
    with pytest.raises(ConfigError, match="YYYYMM"):
        normalize_month_catalog(_directory(), month=month)


def _guest_routes(
    source_url: str,
    *,
    token: str = "measured-csrf-token",
    permissions: list[str] | None = None,
) -> tuple[respx.Route, respx.Route]:
    config = respx.get(f"{source_url}?r=%2Fgetconfig").mock(
        return_value=httpx.Response(200, json={"data": {}})
    )
    user = respx.get(f"{source_url}?r=%2Fgetuser").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "homedir": "/",
                    "name": "Guest",
                    "permissions": permissions or ["read", "download"],
                    "role": "guest",
                    "username": "guest",
                }
            },
            headers={"X-CSRF-Token": token} if token else {},
        )
    )
    return config, user


@respx.mock
def test_the_guest_adapter_sends_utf8_json_and_an_encoded_download_path() -> None:
    source = load_micro_sensor_source()
    config_route, user_route = _guest_routes(source.history_base_url)
    directory_url = f"{source.history_base_url}?r=%2Fgetdir"
    directory_route = respx.post(directory_url).mock(
        return_value=httpx.Response(200, json=_directory())
    )
    encoded_path = quote(
        base64.b64encode(source.station_metadata_path.encode()).decode(),
        safe="",
    )
    station_route = respx.get(f"{source.history_base_url}?r=%2Fdownload&path={encoded_path}").mock(
        return_value=httpx.Response(
            200,
            content=_station_csv(
                "7737132222,TW020101A0201227,柏昇 / SAQ-200-002,25.08876,121.69755,六堵科技園區,鄰近工業區社區,七堵區,基隆市,107年度基隆市空氣品質感測物聯網布建計畫"
            ),
            headers={"Content-Type": "text/csv;charset=UTF-8"},
        )
    )

    with FileGatorHistoryBackend(source, min_interval=0) as backend:
        listing = backend.list_month("202501")
        station_bytes = backend.fetch_station_metadata()

    assert listing == _directory()
    assert station_bytes.startswith(STATION_HEADER.encode())
    assert config_route.call_count == 1
    assert user_route.call_count == 1
    assert directory_route.call_count == 1
    assert station_route.call_count == 1
    request = directory_route.calls[0].request
    assert request.headers["X-CSRF-Token"] == "measured-csrf-token"
    assert json.loads(request.content.decode()) == {"dir": source.month_path("202501")}


@respx.mock
def test_a_missing_guest_token_is_rejected_before_data_requests() -> None:
    source = load_micro_sensor_source()
    _guest_routes(source.history_base_url, token="")

    with (
        FileGatorHistoryBackend(source, min_interval=0) as backend,
        pytest.raises(ConfigError, match="CSRF"),
    ):
        backend.list_month("202501")

    assert len(respx.calls) == 2


@respx.mock
def test_guest_access_must_explicitly_allow_both_reading_and_download() -> None:
    source = load_micro_sensor_source()
    _guest_routes(source.history_base_url, permissions=["read"])

    with (
        FileGatorHistoryBackend(source, min_interval=0) as backend,
        pytest.raises(ConfigError, match="download permission"),
    ):
        backend.fetch_station_metadata()

    assert len(respx.calls) == 2


@respx.mock
def test_an_html_download_page_is_not_parsed_as_station_csv() -> None:
    source = load_micro_sensor_source()
    _guest_routes(source.history_base_url)
    encoded_path = quote(
        base64.b64encode(source.station_metadata_path.encode()).decode(),
        safe="",
    )
    respx.get(f"{source.history_base_url}?r=%2Fdownload&path={encoded_path}").mock(
        return_value=httpx.Response(
            200,
            text="<!DOCTYPE html>",
            headers={"Content-Type": "text/html;charset=UTF-8"},
        )
    )

    with (
        FileGatorHistoryBackend(source, min_interval=0) as backend,
        pytest.raises(ConfigError, match="CSV content type"),
    ):
        backend.fetch_station_metadata()


def _catalog_snapshot(
    *, generated_at: str = "2026-08-12T01:00:00+00:00"
) -> MicroSensorCatalogSnapshot:
    station_bytes = _station_csv(
        "12796768701,GR0112,,24,120,,,,苗栗縣,108年及109年度苗栗縣空氣品質感測物聯網維運計畫",
        "12783787849,GR0155,,24,120,,,,苗栗縣,108年及109年度苗栗縣空氣品質感測物聯網維運計畫",
    )
    listing = _directory(
        _file("moenv_micro_humidity_20250101.zip", size=207_751_024, time=1_735_754_403),
        _file("moenv_micro_pm25_20250101.zip", size=215_145_898, time=1_735_754_405),
        _file("moenv_micro_temperature_20250101.zip", size=202_534_238, time=1_735_754_404),
    )
    return build_catalog_snapshot(
        station_bytes,
        listing,
        month="202501",
        generated_at=generated_at,
        git_sha="abc1234",
        git_dirty=True,
    )


def test_generation_identity_comes_from_source_bytes_and_catalog_not_run_time() -> None:
    first = _catalog_snapshot(generated_at="2026-08-12T01:00:00+00:00")
    second = _catalog_snapshot(generated_at="2026-08-12T02:00:00+00:00")

    assert first.generation_sha256 == second.generation_sha256
    assert first.manifest["generated_at"] != second.manifest["generated_at"]
    assert first.manifest["station_metadata"] == {
        "bytes": len(first.station_metadata_bytes),
        "sha256": hashlib.sha256(first.station_metadata_bytes).hexdigest(),
        "rows": 2,
        "empty_strings": {
            "device_id": 0,
            "location_id": 0,
            "description": 2,
            "area": 2,
            "area_type": 2,
            "township": 2,
            "county": 0,
            "project_name": 0,
        },
        "duplicate_coordinate_groups": 1,
        "rows_in_duplicate_coordinates": 2,
        "largest_duplicate_coordinate_group": 2,
    }
    assert first.manifest["archive_catalog"] == {
        "month": "202501",
        "rows": 93,
        "present": 3,
        "absent": 90,
        "present_bytes": 625_431_160,
    }


def test_snapshot_writer_binds_exact_raw_and_normalized_members(tmp_path: Path) -> None:
    snapshot = _catalog_snapshot()

    written = write_catalog_snapshot(
        snapshot,
        raw_root=tmp_path / "raw",
        interim_root=tmp_path / "interim",
    )

    assert written.raw_directory.name == snapshot.generation_sha256
    assert written.interim_directory.name == snapshot.generation_sha256
    assert (written.raw_directory / "station_metadata.csv").read_bytes() == (
        snapshot.station_metadata_bytes
    )
    assert pl.read_parquet(written.interim_directory / "stations.parquet").equals(snapshot.stations)
    assert pl.read_parquet(written.interim_directory / "archive_catalog.parquet").equals(
        snapshot.catalog
    )
    raw_manifest = json.loads((written.raw_directory / "manifest.json").read_text(encoding="utf-8"))
    interim_manifest = json.loads(
        (written.interim_directory / "manifest.json").read_text(encoding="utf-8")
    )
    assert set(raw_manifest["members"]) == {"directory.json", "station_metadata.csv"}
    assert set(interim_manifest["members"]) == {
        "archive_catalog.parquet",
        "stations.parquet",
    }
    for directory, manifest in (
        (written.raw_directory, raw_manifest),
        (written.interim_directory, interim_manifest),
    ):
        for name, identity in manifest["members"].items():
            member = directory / name
            assert identity == {
                "bytes": member.stat().st_size,
                "sha256": hashlib.sha256(member.read_bytes()).hexdigest(),
            }
    assert (
        interim_manifest["raw_manifest_sha256"]
        == hashlib.sha256((written.raw_directory / "manifest.json").read_bytes()).hexdigest()
    )


def test_an_existing_generation_is_validated_and_never_silently_replaced(tmp_path: Path) -> None:
    snapshot = _catalog_snapshot()
    written = write_catalog_snapshot(
        snapshot,
        raw_root=tmp_path / "raw",
        interim_root=tmp_path / "interim",
    )
    station_path = written.raw_directory / "station_metadata.csv"
    station_path.write_bytes(station_path.read_bytes() + b"changed")

    with pytest.raises(RuntimeError, match="checksum"):
        write_catalog_snapshot(
            snapshot,
            raw_root=tmp_path / "raw",
            interim_root=tmp_path / "interim",
        )

    assert station_path.read_bytes().endswith(b"changed")


def test_manifest_counts_cannot_be_rebound_to_unchanged_source_members(tmp_path: Path) -> None:
    snapshot = _catalog_snapshot()
    written = write_catalog_snapshot(
        snapshot,
        raw_root=tmp_path / "raw",
        interim_root=tmp_path / "interim-first",
    )
    manifest_path = written.raw_directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["station_metadata"]["rows"] = 999
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    with pytest.raises(RuntimeError, match="manifest metadata changed"):
        write_catalog_snapshot(
            snapshot,
            raw_root=tmp_path / "raw",
            interim_root=tmp_path / "interim-second",
        )


def test_an_interrupted_interim_rename_keeps_raw_and_can_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = _catalog_snapshot()
    raw_root = tmp_path / "raw"
    interim_root = tmp_path / "interim"
    original_replace = Path.replace

    def interrupt_interim(self: Path, target: Path) -> Path:
        if interim_root in target.parents and self.name.startswith(".staging-"):
            raise KeyboardInterrupt
        return original_replace(self, target)

    with monkeypatch.context() as patcher:
        patcher.setattr(Path, "replace", interrupt_interim)
        with pytest.raises(KeyboardInterrupt):
            write_catalog_snapshot(
                snapshot,
                raw_root=raw_root,
                interim_root=interim_root,
            )

    raw_generation = raw_root / snapshot.generation_sha256
    interim_generation = interim_root / snapshot.generation_sha256
    assert raw_generation.is_dir()
    assert not interim_generation.exists()
    assert list(interim_root.glob(".staging-*")) == []

    written = write_catalog_snapshot(
        snapshot,
        raw_root=raw_root,
        interim_root=interim_root,
    )

    assert written.raw_directory == raw_generation
    assert written.interim_directory == interim_generation
