"""Canonical workspace and on-disk layout.

Every path the pipeline touches is derived here so that nothing downstream
hard-codes a directory. ``TWAIR_WORKSPACE_DIR`` separates a source checkout or
user-owned workspace from an installed package; ``TWAIR_DATA_DIR`` selects the
data tree inside it (see ``.env``).
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values


def _discover_source_checkout() -> Path | None:
    candidate = Path(__file__).resolve().parents[2]
    source_init = candidate / "src" / "twair" / "__init__.py"
    if not (candidate / "pyproject.toml").is_file() or not (candidate / "conf").is_dir():
        return None
    try:
        return candidate if source_init.samefile(Path(__file__).with_name("__init__.py")) else None
    except FileNotFoundError:
        return None


_SOURCE_CHECKOUT_ROOT = _discover_source_checkout()


def _resolve_workspace_root(
    configured: str | None, current_directory: Path, source_checkout: Path | None
) -> Path:
    if configured:
        root = Path(configured).expanduser()
        return (root if root.is_absolute() else current_directory / root).resolve()
    if source_checkout is not None:
        return source_checkout
    return current_directory.resolve()


_WORKSPACE_ROOT = _resolve_workspace_root(
    os.environ.get("TWAIR_WORKSPACE_DIR"), Path.cwd(), _SOURCE_CHECKOUT_ROOT
)


def workspace_root() -> Path:
    """Workspace selected at process startup, never an install directory."""
    return _WORKSPACE_ROOT


# Kept as the compatibility name used by repository-only checks and exports.
# In a source checkout it is still the repository root; from a wheel it is the
# user-owned workspace selected above, never the package installation path.
REPO_ROOT = _WORKSPACE_ROOT

CONF_DIR = REPO_ROOT / "conf"
DOCS_DIR = REPO_ROOT / "docs"
REPORTS_DIR = REPO_ROOT / "reports"
WEB_DIR = REPO_ROOT / "web"


def data_root() -> Path:
    """Root of all generated data. Resolved at call time so tests can redirect it."""
    raw = os.environ.get("TWAIR_DATA_DIR")
    if not raw:
        raw = dotenv_values(workspace_root() / ".env").get("TWAIR_DATA_DIR") or "data"
    root = Path(raw).expanduser()
    return root if root.is_absolute() else workspace_root() / root


def raw_dir(source: str | None = None) -> Path:
    """Untouched downloads, exactly as the provider served them."""
    base = data_root() / "raw"
    return base / source if source else base


def interim_dir(stage: str | None = None) -> Path:
    """Parsed but not yet quality-controlled."""
    base = data_root() / "interim"
    return base / stage if stage else base


def processed_dir(table: str | None = None) -> Path:
    """Canonical, schema-validated Parquet. This is what gets published."""
    base = data_root() / "processed"
    return base / table if table else base


def outputs_dir(module: str | None = None, run_id: str | None = None) -> Path:
    """Analysis artefacts, one directory per (module, run_id)."""
    base = data_root() / "outputs"
    if module is None:
        return base
    return base / module / run_id if run_id else base / module


def manifest_path() -> Path:
    """Append-only download ledger: URL, checksum, size, timestamp."""
    return raw_dir() / "_manifest.jsonl"


def samples_dir() -> Path:
    """Small real samples captured during source probing (Phase 0)."""
    return raw_dir() / "_samples"


def ensure_dirs() -> None:
    """Create the standard tree. Safe to call repeatedly."""
    for path in (raw_dir(), interim_dir(), processed_dir(), outputs_dir(), samples_dir()):
        path.mkdir(parents=True, exist_ok=True)
