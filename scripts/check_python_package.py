"""Build and exercise the publishable Python artifacts outside the checkout."""

from __future__ import annotations

import os
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WHEEL_CONF_ROOT = "twair/_conf"
SDIST_REQUIRED = (
    ".env.example",
    "LICENSE",
    "LICENSE-DATA",
    "README.md",
    "pyproject.toml",
    "src/twair/__init__.py",
    "src/twair/py.typed",
)
SDIST_EXCLUDED_ROOTS = (
    ".github",
    ".superpowers",
    "data",
    "docs",
    "reports",
    "scripts",
    "spaces",
    "tests",
    "web",
)
SDIST_ALLOWED_ROOT_FILES = {
    ".env.example",
    ".gitignore",
    "LICENSE",
    "LICENSE-DATA",
    "PKG-INFO",
    "README.md",
    "pyproject.toml",
}


class PackageCheckError(RuntimeError):
    """Raised when an artifact cannot satisfy the public package contract."""


def reviewed_configs(conf_dir: Path) -> dict[str, bytes]:
    root = conf_dir.parent
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "--", ":(glob)conf/*.yaml"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise PackageCheckError(f"cannot read tracked config inventory: {detail}")
    tracked = sorted(Path(line).name for line in result.stdout.splitlines() if line.strip())
    return {name: (conf_dir / name).read_bytes() for name in tracked}


def wheel_problems(wheel: Path, expected_configs: Mapping[str, bytes]) -> list[str]:
    problems: list[str] = []
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        expected_members = {f"{WHEEL_CONF_ROOT}/{filename}" for filename in expected_configs}
        packaged_members = {name for name in names if name.startswith(f"{WHEEL_CONF_ROOT}/")}
        for filename, expected in expected_configs.items():
            member = f"{WHEEL_CONF_ROOT}/{filename}"
            if member not in names:
                problems.append(f"wheel is missing reviewed config {member}")
            elif archive.read(member) != expected:
                problems.append(f"wheel config {member} differs from conf/{filename}")
        for member in sorted(packaged_members - expected_members):
            problems.append(f"wheel contains unreviewed config {member}")
        if "twair/py.typed" not in names:
            problems.append("wheel is missing twair/py.typed")
        for license_name in ("LICENSE", "LICENSE-DATA"):
            suffix = f".dist-info/licenses/{license_name}"
            if not any(name.endswith(suffix) for name in names):
                problems.append(f"wheel is missing {license_name}")
    return problems


def _sdist_members(sdist: Path) -> dict[str, bytes]:
    members: dict[str, bytes] = {}
    with tarfile.open(sdist, "r:gz") as archive:
        for member in archive.getmembers():
            if not member.isfile():
                continue
            parts = Path(member.name).parts
            if len(parts) < 2:
                continue
            extracted = archive.extractfile(member)
            if extracted is not None:
                members[Path(*parts[1:]).as_posix()] = extracted.read()
    return members


def sdist_problems(sdist: Path, expected_configs: Mapping[str, bytes]) -> list[str]:
    members = _sdist_members(sdist)
    problems: list[str] = []
    expected_config_members = {f"conf/{filename}" for filename in expected_configs}
    for required in SDIST_REQUIRED:
        if required not in members:
            problems.append(f"sdist is missing {required}")
    for filename, expected in expected_configs.items():
        member = f"conf/{filename}"
        if member not in members:
            problems.append(f"sdist is missing reviewed config {member}")
        elif members[member] != expected:
            problems.append(f"sdist config {member} differs from the reviewed source")
    for name in sorted(members):
        root = name.split("/", 1)[0]
        if root in SDIST_EXCLUDED_ROOTS:
            problems.append(f"sdist contains excluded path {name}")
        elif (
            name in SDIST_ALLOWED_ROOT_FILES
            or name.startswith("src/twair/")
            or name in expected_config_members
        ):
            continue
        else:
            problems.append(f"sdist contains unlisted path {name}")
    return problems


def _run(command: Sequence[str], *, cwd: Path, env: Mapping[str, str] | None = None) -> None:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=dict(env) if env is not None else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode:
        detail = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
        raise PackageCheckError(
            f"command failed ({result.returncode}): {' '.join(command)}\n{detail}"
        )


def _one(directory: Path, pattern: str) -> Path:
    matches = sorted(directory.glob(pattern))
    if len(matches) != 1:
        raise PackageCheckError(
            f"expected one {pattern} in {directory}, found {[path.name for path in matches]}"
        )
    return matches[0]


def _exercise_wheel(wheel: Path, workspace: Path) -> None:
    workspace.mkdir(parents=True)
    (workspace / ".env").write_text("TWAIR_DATA_DIR=dotenv-data\n", encoding="utf-8")
    code = """
import sys
from pathlib import Path

wheel = Path(sys.argv[1]).resolve()
workspace = Path(sys.argv[2]).resolve()
sys.path.insert(0, str(wheel))

import twair
from twair.config import CONF_DIR, load_conf, write_conf
from twair.paths import data_root

if wheel not in Path(twair.__file__).parents:
    raise SystemExit(f"twair leaked from outside wheel: {twair.__file__}")
if load_conf("project").get("quality_gates", {}).get("expected_collected_tests") is None:
    raise SystemExit("packaged project config did not load")
if CONF_DIR != workspace / "conf":
    raise SystemExit(f"config path escaped workspace: {CONF_DIR}")
if data_root() != workspace / "dotenv-data":
    raise SystemExit(f"data path escaped workspace: {data_root()}")

write_conf("project", {"origin": "workspace"})
if load_conf("project") != {"origin": "workspace"}:
    raise SystemExit("workspace config did not override packaged default after write")
"""
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["TWAIR_WORKSPACE_DIR"] = str(workspace)
    env.pop("TWAIR_DATA_DIR", None)
    _run(
        [sys.executable, "-I", "-c", code, str(wheel), str(workspace)],
        cwd=workspace,
        env=env,
    )


def _require_clean_artifact(artifact: Path, problems: list[str], *, description: str) -> None:
    if problems:
        formatted = "\n".join(f"- {problem}" for problem in problems)
        raise PackageCheckError(f"{description} failed:\n{formatted}")
    print(f"python package: {description} passed ({artifact.stat().st_size:,} bytes)")


def check_repository(root: Path = REPO_ROOT) -> None:
    expected = reviewed_configs(root / "conf")
    if not expected:
        raise PackageCheckError(f"no reviewed configs found in {root / 'conf'}")

    with tempfile.TemporaryDirectory(prefix="twair-package-check-") as raw_temp:
        temp = Path(raw_temp)
        direct = temp / "direct"
        rebuilt = temp / "rebuilt"
        direct.mkdir()
        rebuilt.mkdir()

        _run(
            [
                "uv",
                "build",
                "--offline",
                "--no-sources",
                "--no-create-gitignore",
                "--out-dir",
                str(direct),
                str(root),
            ],
            cwd=root,
        )
        wheel = _one(direct, "*.whl")
        sdist = _one(direct, "*.tar.gz")
        _require_clean_artifact(
            wheel, wheel_problems(wheel, expected), description="direct wheel contract"
        )
        _require_clean_artifact(
            sdist, sdist_problems(sdist, expected), description="source distribution contract"
        )
        _exercise_wheel(wheel, temp / "workspace-direct")

        _run(
            [
                "uv",
                "build",
                "--offline",
                "--no-sources",
                "--no-create-gitignore",
                "--wheel",
                "--out-dir",
                str(rebuilt),
                str(sdist),
            ],
            cwd=temp,
        )
        rebuilt_wheel = _one(rebuilt, "*.whl")
        _require_clean_artifact(
            rebuilt_wheel,
            wheel_problems(rebuilt_wheel, expected),
            description="sdist-derived wheel contract",
        )
        _exercise_wheel(rebuilt_wheel, temp / "workspace-rebuilt")
        print("python package: isolated runtime passed for both wheels")


def main() -> int:
    try:
        check_repository()
    except (OSError, PackageCheckError, tarfile.TarError, zipfile.BadZipFile) as exc:
        print(f"python package check failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
