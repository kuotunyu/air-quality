"""Canonical on-disk layout.

Every path the pipeline touches is derived here so that nothing downstream
hard-codes a directory. The root honours ``TWAIR_DATA_DIR`` (see ``.env``).
"""

from __future__ import annotations

import os
from pathlib import Path

# Repository root = two levels above this file (src/twair/paths.py).
REPO_ROOT = Path(__file__).resolve().parents[2]

CONF_DIR = REPO_ROOT / "conf"
DOCS_DIR = REPO_ROOT / "docs"
REPORTS_DIR = REPO_ROOT / "reports"
WEB_DIR = REPO_ROOT / "web"


def data_root() -> Path:
    """Root of all generated data. Resolved at call time so tests can redirect it."""
    raw = os.environ.get("TWAIR_DATA_DIR", "data")
    root = Path(raw)
    return root if root.is_absolute() else REPO_ROOT / root


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
