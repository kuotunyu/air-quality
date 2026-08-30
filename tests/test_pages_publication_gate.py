"""The Pages download register must agree with source, build, and rendered links."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from twair.paths import REPO_ROOT


GATE = REPO_ROOT / "scripts" / "check_pages_publication.mjs"
REGISTER = REPO_ROOT / "web" / "src" / "data" / "pages-publication.json"


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    public = tmp_path / "public"
    dist = tmp_path / "dist"
    register = tmp_path / "pages-publication.json"
    members = ["meta.json", "l0/index.json", "l0/pm25.json", "l1/pm25.parquet"]
    for root in (public, dist / "data"):
        for member in members:
            path = root / member
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"published")
    _write_json(
        public / "manifest.json",
        {"files": [{"file": member, "bytes": 9, "sha256": "x"} for member in members]},
    )
    _write_json(
        register,
        {
            "schema_version": 1,
            "metadata": ["meta.json"],
            "l0": ["l0/index.json", "l0/pm25.json"],
            "l1": ["l1/pm25.parquet"],
            "l2": [],
        },
    )
    (dist / "index.html").write_text(
        "".join(
            f'<a download href="/air-quality/data/{member}">x</a>' for member in members
        ),
        encoding="utf-8",
    )
    return register, public, dist


def _run(register: Path, public: Path, dist: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "node",
            str(GATE),
            "--register",
            str(register),
            "--public",
            str(public),
            "--dist",
            str(dist),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def test_a_complete_pages_publication_passes(tmp_path: Path) -> None:
    register, public, dist = _fixture(tmp_path)

    assert _run(register, public, dist).returncode == 0


def test_repository_register_selects_the_approved_pages_subset() -> None:
    payload = json.loads(REGISTER.read_text(encoding="utf-8"))

    assert payload["schema_version"] == 1
    assert payload["metadata"] == ["meta.json"]
    assert payload["l1"] == ["l1/pm10.parquet", "l1/pm25.parquet"]
    assert payload["l2"] == []
    assert payload["l0"][0] == "l0/index.json"
    assert len(payload["l0"]) == 22


def test_a_selected_member_missing_from_the_manifest_fails(tmp_path: Path) -> None:
    register, public, dist = _fixture(tmp_path)
    _write_json(public / "manifest.json", {"files": []})

    result = _run(register, public, dist)

    assert result.returncode == 1
    assert "manifest" in result.stdout


def test_a_selected_member_missing_from_dist_fails(tmp_path: Path) -> None:
    register, public, dist = _fixture(tmp_path)
    (dist / "data" / "l1" / "pm25.parquet").unlink()

    result = _run(register, public, dist)

    assert result.returncode == 1
    assert "dist" in result.stdout


def test_a_rendered_download_outside_the_register_fails(tmp_path: Path) -> None:
    register, public, dist = _fixture(tmp_path)
    (dist / "index.html").write_text(
        '<a download href="/air-quality/data/l1/so2.parquet">Parquet</a>',
        encoding="utf-8",
    )

    result = _run(register, public, dist)

    assert result.returncode == 1
    assert "rendered download" in result.stdout
