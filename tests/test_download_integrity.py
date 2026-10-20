"""Offline download replacement regressions: no provider or real data."""

import importlib
import io
import socket
import zipfile
from pathlib import Path
from typing import Any

import pytest

from twair.ingest import download as dl
from twair.ingest.airtw import HOURLY_DATA_TYPE, AirtwFile
from twair.net import is_cached, read_manifest, record_download


def zip_bytes(text: str) -> bytes:
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr("sample.csv", text)
    return data.getvalue()


@pytest.fixture
def download_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[AirtwFile, Path, bytes]:
    monkeypatch.setenv("TWAIR_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(socket.socket, "connect", lambda *_args: pytest.fail("network forbidden"))
    item = AirtwFile(2000, HOURLY_DATA_TYPE, "synthetic", "fake", "https://example.invalid")
    dest = dl._destination(item)
    dest.parent.mkdir(parents=True)
    original = zip_bytes("a,b\n1,2\n")
    dest.write_bytes(original)
    record_download(key=item.key, url=item.url, path=dest, source="synthetic")
    return item, dest, original


@pytest.mark.parametrize("failure", ["html", "truncated", "empty", "missing", "exception", "none"])
def test_failed_replacement_preserves_file_and_ledger(
    download_case: tuple[AirtwFile, Path, bytes], monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    item, dest, original = download_case
    ledger = list(read_manifest())

    def fake_download(**kwargs: Any) -> str | None:
        output = Path(kwargs["output"])
        if failure == "missing":
            return str(output)
        if failure == "none":
            return None
        output.write_bytes(
            b"<html>unavailable</html>"
            if failure == "html"
            else b"PK\x03\x04\x14\x00\x00\x00"
            if failure == "truncated"
            else b""
            if failure == "empty"
            else b"partial"
        )
        if failure == "exception":
            raise OSError("synthetic interrupted download")
        return str(output)

    monkeypatch.setattr(importlib.import_module("gdown.download"), "download", fake_download)
    result = dl.download_one(item, force=True)
    assert not result.ok
    assert dest.read_bytes() == original
    assert list(read_manifest()) == ledger
    assert is_cached(item.key) == dest
    assert list(dest.parent.iterdir()) == [dest]


def test_valid_replacement_publishes_and_records(
    download_case: tuple[AirtwFile, Path, bytes], monkeypatch: pytest.MonkeyPatch
) -> None:
    item, dest, original = download_case
    replacement = zip_bytes("a,b\n3,4\n")

    def fake_download(**kwargs: Any) -> str:
        output = Path(kwargs["output"])
        assert output != dest
        assert dest.read_bytes() == original
        output.write_bytes(replacement)
        return str(output)

    monkeypatch.setattr(importlib.import_module("gdown.download"), "download", fake_download)
    result = dl.download_one(item, force=True)
    assert result.ok and result.path == dest
    assert dest.read_bytes() == replacement
    entries = list(read_manifest())
    assert len(entries) == 2
    assert entries[-1]["path"] == str(dest)
    assert entries[-1]["bytes"] == len(replacement)
    assert is_cached(item.key) == dest
    assert list(dest.parent.iterdir()) == [dest]


def test_new_truncated_download_never_becomes_cache(
    download_case: tuple[AirtwFile, Path, bytes], monkeypatch: pytest.MonkeyPatch
) -> None:
    item, dest, _ = download_case
    dest.unlink()
    before = list(read_manifest())

    def fake_download(**kwargs: Any) -> str:
        Path(kwargs["output"]).write_bytes(b"PK\x03\x04\x14\x00\x00\x00")
        return str(kwargs["output"])

    monkeypatch.setattr(importlib.import_module("gdown.download"), "download", fake_download)
    assert not dl.download_one(item, force=True).ok
    assert not dest.exists()
    assert list(read_manifest()) == before
    assert is_cached(item.key) is None
    assert list(dest.parent.iterdir()) == []


def test_retry_discards_failed_attempt_before_wait(
    download_case: tuple[AirtwFile, Path, bytes], monkeypatch: pytest.MonkeyPatch
) -> None:
    item, dest, original = download_case
    attempts = []
    waits = []
    replacement = zip_bytes("a,b\n5,6\n")

    def fake_download(**kwargs: Any) -> str:
        output = Path(kwargs["output"])
        attempts.append(output)
        assert not output.exists()
        output.write_bytes(replacement)
        if len(attempts) == 1:
            output.with_suffix(".partial").write_bytes(b"synthetic sidecar")
            raise OSError("quota")
        return str(output)

    def fake_sleep(delay: int) -> None:
        waits.append(delay)
        assert dest.read_bytes() == original
        assert list(dest.parent.iterdir()) == [dest]

    monkeypatch.setattr(importlib.import_module("gdown.download"), "download", fake_download)
    monkeypatch.setattr("twair.ingest.download.time.sleep", fake_sleep)
    assert dl.download_one(item, force=True, patient=True).ok
    assert waits == [60]
    assert len(attempts) == 2 and attempts[0] != attempts[1]
    assert dest.read_bytes() == replacement
    assert len(list(read_manifest())) == 2


def test_interrupt_cleans_staging_preserving_original(
    download_case: tuple[AirtwFile, Path, bytes], monkeypatch: pytest.MonkeyPatch
) -> None:
    item, dest, original = download_case
    before = list(read_manifest())

    def interrupted(**kwargs: Any) -> None:
        Path(kwargs["output"]).write_bytes(b"partial")
        raise KeyboardInterrupt()

    monkeypatch.setattr(importlib.import_module("gdown.download"), "download", interrupted)
    with pytest.raises(KeyboardInterrupt):
        dl.download_one(item, force=True)
    assert dest.read_bytes() == original
    assert list(read_manifest()) == before
    assert list(dest.parent.iterdir()) == [dest]


@pytest.mark.parametrize("magic", [b"%PDF-1.4\n", b"7z\xbc\xaf\x27\x1c\x00\x04"])
def test_report_magic_contract_unchanged(tmp_path: Path, magic: bytes) -> None:
    candidate = tmp_path / "report"
    candidate.write_bytes(magic)
    assert dl.is_supported_archive(candidate, "品保查核報告")
