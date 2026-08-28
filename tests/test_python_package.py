"""The published Python artifacts carry configuration without the website."""

from __future__ import annotations

import io
import subprocess
import tarfile
import zipfile
from pathlib import Path

from scripts.check_python_package import reviewed_configs, sdist_problems, wheel_problems


def _wheel(path: Path, members: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return path


def _sdist(path: Path, members: dict[str, bytes]) -> Path:
    with tarfile.open(path, "w:gz") as archive:
        for name, content in members.items():
            info = tarfile.TarInfo(f"twair-0.1.0/{name}")
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return path


def test_a_wheel_missing_a_reviewed_config_is_rejected(tmp_path: Path) -> None:
    expected = {"pollutants.yaml": b'pollutants:\n  "NO": {}\n', "qc.yaml": b"rules: {}\n"}
    wheel = _wheel(
        tmp_path / "twair.whl",
        {
            "twair/_conf/pollutants.yaml": expected["pollutants.yaml"],
            "twair/py.typed": b"",
            "twair-0.1.0.dist-info/licenses/LICENSE": b"MIT",
            "twair-0.1.0.dist-info/licenses/LICENSE-DATA": b"data terms",
        },
    )

    assert wheel_problems(wheel, expected) == [
        "wheel is missing reviewed config twair/_conf/qc.yaml"
    ]


def test_a_wheel_config_must_match_the_reviewed_source_bytes(tmp_path: Path) -> None:
    expected = {"qc.yaml": b"coverage: 0.75\n"}
    wheel = _wheel(
        tmp_path / "twair.whl",
        {
            "twair/_conf/qc.yaml": b"coverage: 0.50\n",
            "twair/py.typed": b"",
            "twair-0.1.0.dist-info/licenses/LICENSE": b"MIT",
            "twair-0.1.0.dist-info/licenses/LICENSE-DATA": b"data terms",
        },
    )

    assert wheel_problems(wheel, expected) == [
        "wheel config twair/_conf/qc.yaml differs from conf/qc.yaml"
    ]


def test_a_wheel_cannot_add_an_unreviewed_config(tmp_path: Path) -> None:
    expected = {"qc.yaml": b"coverage: 0.75\n"}
    wheel = _wheel(
        tmp_path / "twair.whl",
        {
            "twair/_conf/qc.yaml": expected["qc.yaml"],
            "twair/_conf/rogue.yaml": b"unreviewed: true\n",
            "twair/py.typed": b"",
            "twair-0.1.0.dist-info/licenses/LICENSE": b"MIT",
            "twair-0.1.0.dist-info/licenses/LICENSE-DATA": b"data terms",
        },
    )

    assert wheel_problems(wheel, expected) == [
        "wheel contains unreviewed config twair/_conf/rogue.yaml"
    ]


def test_only_git_tracked_configs_define_the_reviewed_inventory(tmp_path: Path) -> None:
    conf_dir = tmp_path / "conf"
    conf_dir.mkdir()
    (conf_dir / "qc.yaml").write_text("coverage: 0.75\n", encoding="utf-8")
    (conf_dir / "rogue.yaml").write_text("unreviewed: true\n", encoding="utf-8")
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", "conf/qc.yaml"],
        check=True,
    )

    assert reviewed_configs(conf_dir) == {"qc.yaml": (conf_dir / "qc.yaml").read_bytes()}


def test_the_sdist_rejects_website_and_public_data_roots(tmp_path: Path) -> None:
    expected = {"qc.yaml": b"coverage: 0.75\n"}
    sdist = _sdist(
        tmp_path / "twair.tar.gz",
        {
            "pyproject.toml": b"[build-system]\n",
            "README.md": b"# twair\n",
            "LICENSE": b"MIT",
            "LICENSE-DATA": b"data terms",
            ".env.example": b"TWAIR_DATA_DIR=data\n",
            "src/twair/__init__.py": b"",
            "src/twair/py.typed": b"",
            "conf/qc.yaml": expected["qc.yaml"],
            "web/public/data/l0/pm25.parquet": b"public data",
        },
    )

    assert sdist_problems(sdist, expected) == [
        "sdist contains excluded path web/public/data/l0/pm25.parquet"
    ]


def test_a_minimal_sdist_contains_every_wheel_build_input(tmp_path: Path) -> None:
    expected = {"qc.yaml": b"coverage: 0.75\n"}
    sdist = _sdist(
        tmp_path / "twair.tar.gz",
        {
            "pyproject.toml": b"[build-system]\n",
            "README.md": b"# twair\n",
            "LICENSE": b"MIT",
            "LICENSE-DATA": b"data terms",
            ".env.example": b"TWAIR_DATA_DIR=data\n",
            "src/twair/__init__.py": b"",
            "src/twair/py.typed": b"",
            "conf/qc.yaml": expected["qc.yaml"],
        },
    )

    assert sdist_problems(sdist, expected) == []


def test_the_sdist_rejects_an_unlisted_root_file(tmp_path: Path) -> None:
    expected = {"qc.yaml": b"coverage: 0.75\n"}
    sdist = _sdist(
        tmp_path / "twair.tar.gz",
        {
            "pyproject.toml": b"[build-system]\n",
            "README.md": b"# twair\n",
            "LICENSE": b"MIT",
            "LICENSE-DATA": b"data terms",
            ".env.example": b"TWAIR_DATA_DIR=data\n",
            "src/twair/__init__.py": b"",
            "src/twair/py.typed": b"",
            "conf/qc.yaml": expected["qc.yaml"],
            "private-notes.md": b"repository-only notes\n",
        },
    )

    assert sdist_problems(sdist, expected) == ["sdist contains unlisted path private-notes.md"]
