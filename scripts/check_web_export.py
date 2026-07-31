"""Check that the exported website data describes itself completely.

`twair.viz.export.write_manifest` has always been correct: it walks whatever is
under `web/public/data` and checksums it. What nothing checked was the manifest
**in the repository** against the files **in the repository**, and that is where
it went wrong. `story/sarima.json` and `story/spatial-structure.json` were
committed and deployed while the manifest still described the export before
them, so two of the site's chapters drew on bytes the provenance record did not
cover, `bytes_total` under-reported by 6,585, and `git_sha` named a commit that
predated both files. `twair status` said `STALE`, which is advice, not a gate.

Two things are checked, and both are about a claim the export makes about
itself rather than about the data:

1.  Every file present under `web/public/data` is listed in `manifest.json`
    with a matching size and SHA-256, and `bytes_total` is the sum of the
    listed sizes.

    Listed-but-absent is *not* a failure. Most of `l1/` is gitignored, so a
    fresh clone legitimately carries a subset. Present-but-unlisted is the
    failure this exists for, along with a payload edited by hand after export.

2.  `meta.json` carries `hourly_observations` as a number.

    The site falls back to a declared constant when that key is missing
    (`web/src/lib/data.ts`), so an export whose row count silently failed —
    which happened, and shipped — looks identical on the page to one that
    succeeded. A fallback that always has a value never errors.

    uv run python scripts/check_web_export.py [web/public/data]
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

DEFAULT_ROOT = Path("web/public/data")

MANIFEST = "manifest.json"


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_ROOT
    manifest_path = root / MANIFEST
    if not manifest_path.is_file():
        print(f"{manifest_path} not found — run `uv run twair export web`", file=sys.stderr)
        return 1

    manifest = load(manifest_path)
    listed: dict[str, dict[str, Any]] = {entry["file"]: entry for entry in manifest["files"]}
    present = sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path.name != MANIFEST
    )

    unlisted: list[str] = []
    mismatched: list[str] = []
    for name in present:
        entry = listed.get(name)
        if entry is None:
            unlisted.append(
                f"{name} ({(root / name).stat().st_size:,} bytes) is not in the manifest"
            )
            continue
        blob = (root / name).read_bytes()
        if len(blob) != entry["bytes"]:
            mismatched.append(
                f"{name}: manifest says {entry['bytes']:,} bytes, file is {len(blob):,}"
            )
        elif hashlib.sha256(blob).hexdigest() != entry["sha256"]:
            mismatched.append(f"{name}: SHA-256 does not match the manifest")

    total = sum(int(entry["bytes"]) for entry in listed.values())
    wrong_total = total != int(manifest["bytes_total"])

    meta = load(root / "meta.json") if (root / "meta.json").is_file() else {}
    observations = meta.get("hourly_observations")
    unmeasured = not isinstance(observations, int)

    absent = [name for name in listed if not (root / name).is_file()]

    # ASCII markers on purpose: this runs on a cp950 console as well as in CI,
    # and a check that dies encoding its own failure marker reports nothing.
    print(f"manifest        : {manifest['generated_at']} from {manifest['git_sha']}")
    print(f"files listed    : {len(listed)}")
    print(f"files present   : {len(present)}  (listed but not in this tree: {len(absent)})")
    print(
        f"bytes_total     : {int(manifest['bytes_total']):,}"
        + ("" if not wrong_total else f"  FAIL: listed sizes sum to {total:,}")
    )
    print(
        "hourly_obs      : "
        + (
            f"{observations:,}"
            if isinstance(observations, int)
            else f"{observations!r}  FAIL: not a measured count"
        )
    )

    for line in unlisted + mismatched:
        print(f"  FAIL: {line}")

    if unlisted or mismatched or wrong_total or unmeasured:
        print("\nre-run `uv run twair export web` and commit the result", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
