"""Build the public Hugging Face Dataset bundle from the website layers.

L0 and L1 are the publication boundary. The complete station-hour L2 record
stays local; this module deliberately has no code path that can read it.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import polars as pl
import pyarrow.parquet as pq

from twair.paths import REPO_ROOT, data_root
from twair.viz.export import web_data_dir, write_json

__all__ = ["DatasetBundleReport", "build_dataset_bundle", "dataset_bundle_dir"]

ROW_GROUP_SIZE = 100_000


def dataset_bundle_dir() -> Path:
    """Ignored local directory that is ready for a later manual upload."""
    return data_root() / "exports" / "huggingface" / "air-quality"


@dataclass(frozen=True, slots=True)
class DatasetBundleReport:
    """Measured contents of one local dataset bundle."""

    destination: Path
    rows: dict[str, int]
    files: tuple[Path, ...]
    bytes_total: int

    def summary(self) -> str:
        return (
            f"monthly {self.rows['monthly']:,} rows, "
            f"daily {self.rows['daily']:,} rows, "
            f"{len(self.files)} data files, {self.bytes_total / 1e6:.1f} MB"
        )


def _write_parquet(frame: pl.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        frame.to_arrow(),
        path,
        compression="zstd",
        row_group_size=ROW_GROUP_SIZE,
        write_statistics=True,
        write_page_index=True,
    )


def _monthly_frame(payload: dict[str, Any]) -> pl.DataFrame:
    months = [date.fromisoformat(f"{value}-01") for value in payload["months"]]
    pollutant = str(payload["pollutant"])
    name_zh = str(payload["name_zh"])
    unit = str(payload["unit"])

    rows: list[tuple[str, str, str, str, date, float | None, int]] = []
    for station, means, counts in zip(
        payload["stations"], payload["mean"], payload["n_days"], strict=True
    ):
        if len(means) != len(months) or len(counts) != len(months):
            raise RuntimeError(f"{pollutant} L0 rectangle does not match its month axis")
        rows.extend(
            (pollutant, name_zh, unit, str(station), month, mean, int(count))
            for month, mean, count in zip(months, means, counts, strict=True)
        )

    return pl.DataFrame(
        rows,
        schema={
            "pollutant": pl.String,
            "name_zh": pl.String,
            "unit": pl.String,
            "station_name": pl.String,
            "month": pl.Date,
            "mean": pl.Float32,
            "n_days": pl.UInt8,
        },
        orient="row",
    )


def _daily_frame(path: Path, *, pollutant: str, name_zh: str, unit: str) -> pl.DataFrame:
    return (
        pl.read_parquet(path)
        .select(
            pl.lit(pollutant).alias("pollutant"),
            pl.lit(name_zh).alias("name_zh"),
            pl.lit(unit).alias("unit"),
            pl.col("station_name").cast(pl.String),
            pl.col("date").cast(pl.Date),
            pl.col("mean").cast(pl.Float32),
            pl.col("n_valid").cast(pl.UInt8),
        )
        .sort("station_name", "date")
    )


def _dataset_card(*, monthly_rows: int, daily_rows: int) -> str:
    return f"""---
language:
- zh
- en
license: other
tags:
- air-quality
- taiwan
- pm25
- environmental-monitoring
configs:
- config_name: monthly
  default: true
  data_files:
  - split: full
    path: data/monthly/*.parquet
- config_name: daily
  data_files:
  - split: full
    path: data/daily/*.parquet
---

# Taiwan Air Quality — Derived Daily and Monthly Aggregates

This bundle contains the public aggregate layers from the `air-quality`
reanalysis: {monthly_rows:,} station-month rows and {daily_rows:,}
station-day rows across the documented measurands.

## Configurations

- `monthly`: station-month mean plus `n_days`.
- `daily`: station-day mean plus `n_valid` hours.

A null mean is never filled or interpolated. A zero count means that no
qualifying observations were present; a positive count beside a null mean
means the aggregate was deliberately withheld for insufficient coverage.

The complete station-hour record (L2) is not redistributed. It can be rebuilt
from the public pipeline and upstream archives documented in the source
repository. This Dataset Card therefore describes L0 and L1 only.

## Load

```python
from datasets import load_dataset

monthly = load_dataset("steven0226/air-quality", "monthly")
daily = load_dataset("steven0226/air-quality", "daily")
```

The source code is MIT licensed. Dataset reuse remains subject to the upstream
source terms documented by the project; the data license is intentionally
recorded as `other` rather than inheriting the code license.
"""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _prepare_destination(destination: Path, *, overwrite: bool) -> None:
    if not destination.exists():
        destination.mkdir(parents=True)
        return
    if not any(destination.iterdir()):
        return
    if not overwrite:
        raise FileExistsError(f"{destination} is not empty; pass overwrite=True to rebuild it")

    resolved = destination.resolve()
    protected = {
        REPO_ROOT.resolve(),
        data_root().resolve(),
        Path.cwd().resolve(),
        Path(resolved.anchor).resolve(),
    }
    if any(path == resolved or path.is_relative_to(resolved) for path in protected):
        raise RuntimeError(f"refusing to replace broad directory {resolved}")
    shutil.rmtree(resolved)
    resolved.mkdir(parents=True)


def build_dataset_bundle(
    *,
    source: Path | None = None,
    destination: Path | None = None,
    overwrite: bool = False,
) -> DatasetBundleReport:
    """Package public L0/L1 exports without reading or redistributing L2."""
    source_root = source or web_data_dir()
    destination_root = destination or dataset_bundle_dir()
    if (source_root / "l2").exists():
        raise RuntimeError("refusing to package L2; only public L0/L1 aggregates are allowed")

    l0_files = sorted(
        path for path in (source_root / "l0").glob("*.json") if path.name != "index.json"
    )
    if not l0_files:
        raise FileNotFoundError(f"no L0 files in {source_root / 'l0'}")

    missing_l1 = [
        source_root / "l1" / f"{path.stem}.parquet"
        for path in l0_files
        if not (source_root / "l1" / f"{path.stem}.parquet").exists()
    ]
    if missing_l1:
        raise FileNotFoundError(
            f"{len(missing_l1)} L1 files are missing; rebuild the complete public export "
            f"before packaging (first missing: {missing_l1[0]})"
        )

    _prepare_destination(destination_root, overwrite=overwrite)

    written: list[Path] = []
    manifest_files: list[dict[str, Any]] = []
    rows = {"monthly": 0, "daily": 0}

    for l0_path in l0_files:
        payload = json.loads(l0_path.read_text(encoding="utf-8"))
        slug = l0_path.stem
        l1_path = source_root / "l1" / f"{slug}.parquet"

        monthly = _monthly_frame(payload)
        daily = _daily_frame(
            l1_path,
            pollutant=str(payload["pollutant"]),
            name_zh=str(payload["name_zh"]),
            unit=str(payload["unit"]),
        )
        for config_name, frame in (("monthly", monthly), ("daily", daily)):
            path = destination_root / "data" / config_name / f"{slug}.parquet"
            _write_parquet(frame, path)
            written.append(path)
            rows[config_name] += frame.height
            manifest_files.append(
                {
                    "path": path.relative_to(destination_root).as_posix(),
                    "rows": frame.height,
                    "bytes": path.stat().st_size,
                    "sha256": _sha256(path),
                    "schema": {name: str(dtype) for name, dtype in frame.schema.items()},
                }
            )

    card = destination_root / "README.md"
    card.write_text(
        _dataset_card(monthly_rows=rows["monthly"], daily_rows=rows["daily"]),
        encoding="utf-8",
        newline="\n",
    )
    manifest = write_json(
        destination_root / "manifest.json",
        {
            "format_version": 1,
            "levels": ["L0", "L1"],
            "configs": {
                "monthly": {"split": "full", "rows": rows["monthly"]},
                "daily": {"split": "full", "rows": rows["daily"]},
            },
            "files": manifest_files,
        },
    )
    bytes_total = sum(path.stat().st_size for path in (*written, card, manifest))
    return DatasetBundleReport(destination_root, rows, tuple(written), bytes_total)
