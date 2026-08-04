from __future__ import annotations

import re

from twair.paths import REPO_ROOT


def test_the_explorer_keeps_the_duckdb_runtime_behind_the_click_time_import() -> None:
    source = (REPO_ROOT / "web" / "src" / "components" / "Explorer.astro").read_text(
        encoding="utf-8"
    )

    runtime_imports = re.findall(
        r'^\s*import\s+(?!type\b).*?from\s+["\']\.\./lib/duck["\'];',
        source,
        flags=re.MULTILINE,
    )
    assert runtime_imports == []
    assert 'const duck = () => import("../lib/duck");' in source
