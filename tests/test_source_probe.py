"""Phase 0's source probe, which is the only thing that resolves the downloads.

`ingest/probe.py` was 88 statements at 0% coverage, and the untested part was
the one that had already failed: `download_sample` called gdown through a name
that cannot be called that way, caught the resulting AttributeError in a bare
`except`, and reported it as a download failure. `docs/data-sources.md` has been
publishing 「_not captured_」 under 「### 樣本」 with the 592 KB 離島 archive
sitting in `data/raw/_samples/`.

It is the same misspelling that was found and fixed in `ingest/download.py`,
whose comment records that it meant 「this project's first pipeline step did not
run at all」. Finding it in the second of two places is the argument for the
tests below: nothing here needs the network, and any of them would have caught
it.
"""

from __future__ import annotations

import importlib
import zipfile
from pathlib import Path

import pytest
import yaml

from twair.ingest.airtw import HOURLY_DATA_TYPE, AirtwFile
from twair.ingest.probe import (
    SAMPLE_STATION_GROUP,
    SAMPLE_YEAR,
    _catalog_summary,
    download_sample,
    probe_credentialed_sources,
    run_probe,
    write_sources_conf,
    write_sources_doc,
)

ZIP_MAGIC = b"PK\x03\x04"

# What Google Drive serves instead of a 404 when a file id has been rotated:
# a 200, an HTML page, and a filename that looks exactly like the archive.
DRIVE_INTERSTITIAL = b"<!DOCTYPE html><html><head><title>Google Drive</title>"


def archive(
    year: int = SAMPLE_YEAR, group: str = SAMPLE_STATION_GROUP, **over: object
) -> AirtwFile:
    fields: dict[str, object] = {
        "year": year,
        "data_type": HOURLY_DATA_TYPE,
        "station_group": group,
        "drive_file_id": f"id-{year}-{group}",
        "url": f"https://drive.google.com/file/d/id-{year}-{group}",
    }
    fields.update(over)
    return AirtwFile(**fields)  # type: ignore[arg-type]


@pytest.fixture
def samples(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the whole data tree, so nothing here touches the real archives."""
    monkeypatch.setenv("TWAIR_DATA_DIR", str(tmp_path))
    out = tmp_path / "raw" / "_samples"
    out.mkdir(parents=True)
    return out


def serve(monkeypatch: pytest.MonkeyPatch, body: bytes) -> list[str]:
    """Stand in for gdown, recording the ids it was asked for.

    Reached through `sys.modules`, and it has to be. `monkeypatch.setattr` with
    a dotted string walks `gdown` → `download` and lands on the re-exported
    FUNCTION, then fails with 「'function' object at gdown.download has no
    attribute 'download'」 — the same shadowing that broke the call under test,
    reproduced here by the test harness itself.
    """
    asked: list[str] = []

    def fake(*, id: str, output: str, quiet: bool = False) -> str:
        asked.append(id)
        Path(output).write_bytes(body)
        return output

    monkeypatch.setattr(importlib.import_module("gdown.download"), "download", fake)
    return asked


def zip_bytes() -> bytes:
    import io

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("金門_2024.csv", "測站,日期\n")
    return buf.getvalue()


# ── the catalogue summary that the published doc is built from ───────────────


def test_the_summary_separates_hourly_archives_from_everything_else() -> None:
    """Three kinds of file live behind one page; only one of them is the data."""
    catalog = [
        archive(2024, "離島"),
        archive(2023, "全部"),
        archive(2018, "全部", data_type="品保查核報告"),
        archive(2018, "全部", data_type="年報"),
    ]

    summary = _catalog_summary(catalog)

    assert summary["total_files"] == 4
    assert summary["hourly_files"] == 2
    assert summary["hourly_year_min"] == 2023
    assert summary["hourly_year_max"] == 2024
    assert summary["hourly_year_count"] == 2
    assert summary["data_types"] == ["全年逐時資料", "品保查核報告", "年報"]


def test_the_year_count_counts_years_present_not_the_span() -> None:
    """A gap in the archive is a fact about the archive, not an off-by-one."""
    summary = _catalog_summary([archive(2000, "全部"), archive(2010, "全部")])

    assert (summary["hourly_year_min"], summary["hourly_year_max"]) == (2000, 2010)
    assert summary["hourly_year_count"] == 2, "the span was reported as if every year were present"


def test_an_empty_catalogue_reports_no_years_rather_than_guessing() -> None:
    summary = _catalog_summary([])

    assert summary["hourly_year_min"] is None
    assert summary["hourly_year_max"] is None
    assert summary["total_files"] == 0


# ── the sample, and the call that could not work ─────────────────────────────


def test_the_sample_downloads(samples: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The regression. `import gdown.download as gdown` binds the FUNCTION.

    gdown re-exports `download` on the package, and for the `import x.y as z`
    form the package attribute wins — so the name was the function and
    `gdown.download(...)` raised 「'function' object has no attribute
    'download'」. Under the old spelling this test fails at the assertion below,
    with the sample reported as absent.
    """
    asked = serve(monkeypatch, zip_bytes())

    got = download_sample([archive(), archive(2023, "全部")])

    assert got is not None, "the sample download failed with the network mocked out"
    assert got.exists()
    assert asked == [f"id-{SAMPLE_YEAR}-{SAMPLE_STATION_GROUP}"], "the wrong archive was sampled"


def test_the_smallest_group_is_preferred_over_the_newest_year(
    samples: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """離島 is three stations; 全部 is eighty. The probe is meant to stay light."""
    asked = serve(monkeypatch, zip_bytes())

    download_sample([archive(2025, "全部"), archive(SAMPLE_YEAR, SAMPLE_STATION_GROUP)])

    assert asked == [f"id-{SAMPLE_YEAR}-{SAMPLE_STATION_GROUP}"]


def test_a_catalogue_without_the_preferred_sample_falls_back_to_the_newest(
    samples: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked = serve(monkeypatch, zip_bytes())

    download_sample([archive(2019, "全部"), archive(2025, "全部")])

    assert asked == ["id-2025-全部"]


def test_a_catalogue_with_no_hourly_archive_gives_up_quietly(
    samples: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked = serve(monkeypatch, zip_bytes())

    assert download_sample([archive(2018, "全部", data_type="年報")]) is None
    assert asked == [], "a non-hourly file was downloaded as if it were the sample"


def test_an_html_interstitial_is_not_a_sample(
    samples: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drive answers a rotated file id with a 200 and a web page.

    Left on disk it would satisfy every later `exists()` check, and the probe
    would publish its byte count in `docs/data-sources.md` as the captured
    sample.
    """
    serve(monkeypatch, DRIVE_INTERSTITIAL)

    assert download_sample([archive()]) is None
    assert list(samples.iterdir()) == [], "the interstitial was kept"


def test_a_truncated_earlier_download_is_not_reused(
    samples: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`st_size > 0` used to be the entire cache check.

    An interrupted download leaves a non-empty file, and from then on the probe
    reports it as present and never fetches it again — a sample poisoned for
    good, published by byte count as though it were the archive.
    """
    stale = samples / f"airtw_{SAMPLE_YEAR}_{SAMPLE_STATION_GROUP}.zip"
    stale.write_bytes(ZIP_MAGIC[:2] + b"truncated")
    asked = serve(monkeypatch, zip_bytes())

    got = download_sample([archive()])

    assert asked, "the truncated file was accepted as the sample"
    assert got is not None
    assert zipfile.is_zipfile(got)


def test_an_intact_sample_is_not_downloaded_again(
    samples: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    good = samples / f"airtw_{SAMPLE_YEAR}_{SAMPLE_STATION_GROUP}.zip"
    good.write_bytes(zip_bytes())
    asked = serve(monkeypatch, zip_bytes())

    assert download_sample([archive()]) == good
    assert asked == [], "a good sample was fetched a second time"


def test_a_failing_download_reports_none_rather_than_raising(
    samples: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A probe that dies on one unavailable file resolves nothing else."""

    def boom(**_: object) -> str:
        raise RuntimeError("Too many users have viewed or downloaded this file recently")

    monkeypatch.setattr(importlib.import_module("gdown.download"), "download", boom)

    assert download_sample([archive()]) is None


# ── the credential inventory ─────────────────────────────────────────────────


def test_missing_keys_are_reported_not_raised() -> None:
    """The whole point: airtw needs no key, so an unconfigured checkout works."""
    status = probe_credentialed_sources()

    assert set(status) == {"moenv_api", "cwa_opendata", "era5_cds", "earthengine"}
    for name, info in status.items():
        assert isinstance(info["credential_present"], bool), name
        assert info["register_at"].startswith("https://"), name
        assert info["probed"] is False, "nothing here actually contacts the provider"


# ── the published document ───────────────────────────────────────────────────


def test_the_doc_records_the_sample_it_captured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("twair.ingest.probe.DOCS_DIR", tmp_path)
    sample = tmp_path / "airtw_2024_離島.zip"
    sample.write_bytes(zip_bytes())

    write_sources_doc(_catalog_summary([archive()]), sample)
    text = (tmp_path / "data-sources.md").read_text(encoding="utf-8")

    assert "airtw_2024_離島.zip" in text
    assert "_not captured_" not in text


def test_the_doc_says_so_when_no_sample_was_captured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("twair.ingest.probe.DOCS_DIR", tmp_path)

    write_sources_doc(_catalog_summary([archive()]), None)

    assert "_not captured_" in (tmp_path / "data-sources.md").read_text(encoding="utf-8")


def test_the_source_doc_does_not_describe_a_read_only_probe_as_incremental_ingest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("twair.ingest.probe.DOCS_DIR", tmp_path)

    write_sources_doc(_catalog_summary([archive()]), None)
    text = (tmp_path / "data-sources.md").read_text(encoding="utf-8")

    assert "每日增量更新" not in text
    assert "唯讀 freshness 檢查" in text
    assert "不寫回 canonical store" in text


def test_the_generated_source_doc_preserves_the_measured_era5_release_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("twair.ingest.probe.DOCS_DIR", tmp_path)

    write_sources_doc(_catalog_summary([archive()]), None)
    text = (tmp_path / "data-sources.md").read_text(encoding="utf-8")

    assert "674,520 筆 station-hour" in text
    assert "六個來源變數皆為 0 個 null" in text
    assert "尚未納入已發布的 M4" in text
    assert "尚未取得" not in text[text.index("| Copernicus ERA5") : text.index("| Sentinel-5P")]


def test_the_conf_keeps_every_resolved_file_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`conf/sources.yaml` is a cache of resolutions, not a contract.

    Drive rotates the ids whenever MOENV re-uploads, so the whole value of the
    file is that every row of the catalogue survives into it — a summary alone
    would resolve nothing on the next run.
    """
    monkeypatch.setattr("twair.config.CONF_DIR", tmp_path)
    catalog = [archive(2024, "離島"), archive(2023, "全部")]

    write_sources_conf(catalog, _catalog_summary(catalog), probe_credentialed_sources())
    written = yaml.safe_load((tmp_path / "sources.yaml").read_text(encoding="utf-8"))

    ids = [f["drive_file_id"] for f in written["airtw"]["files"]]
    assert ids == ["id-2024-離島", "id-2023-全部"]
    assert written["airtw"]["license"] == "政府資料開放授權條款第1版"
    assert set(written["credentialed"]) >= {"moenv_api", "cwa_opendata"}
    # Whether *this* machine holds a key is not a property of the source, and
    # this file is committed: it recorded one developer's key inventory and
    # flipped three lines every time someone else re-probed.
    assert all("credential_present" not in info for info in written["credentialed"].values()), (
        "the probe published which credentials the operator happens to hold"
    )
    assert written["credentialed"]["moenv_api"]["register_at"].startswith("https://")


# ── the entry point ──────────────────────────────────────────────────────────


def test_the_probe_writes_both_files_without_touching_the_network(
    samples: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One run, end to end: catalogue in, conf and doc out.

    `download_samples=False` is the flag a re-probe uses when it only wants the
    file ids refreshed, and it is the path that must not quietly leave the doc
    claiming a sample it did not fetch.
    """
    conf = tmp_path / "conf"
    conf.mkdir()
    monkeypatch.setattr("twair.config.CONF_DIR", conf)
    monkeypatch.setattr("twair.ingest.probe.DOCS_DIR", tmp_path / "docs")
    monkeypatch.setattr("twair.ingest.probe.fetch_catalog", lambda _client: [archive()])

    run_probe(download_samples=False)

    assert (conf / "sources.yaml").exists()
    doc = (tmp_path / "docs" / "data-sources.md").read_text(encoding="utf-8")
    assert "_not captured_" in doc
    assert "| 檔案總數 | 1 |" in doc
