from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"

REVIEWED_ACTIONS = {
    "actions/checkout": ("3d3c42e5aac5ba805825da76410c181273ba90b1", "v7.0.1"),
    "actions/setup-node": ("820762786026740c76f36085b0efc47a31fe5020", "v7.0.0"),
    "astral-sh/setup-uv": ("c771a70e6277c0a99b617c7a806ffedaca235ff9", "v9.0.0"),
    "actions/configure-pages": ("45bfe0192ca1faeb007ade9deae92b16b8254a0d", "v6.0.0"),
    "actions/upload-pages-artifact": ("fc324d3547104276b827a68afc52ff2a11cc49c9", "v5.0.0"),
    "actions/deploy-pages": ("cd2ce8fcbc39b97be8ca5fce6e763baed58fa128", "v5.0.0"),
}
USES_PATTERN = re.compile(r"uses:\s*([^@\s]+)@([^\s#]+)(?:\s*#\s*(\S+))?")


def test_workflows_pin_every_external_action_to_the_reviewed_release_commit() -> None:
    found: dict[str, list[tuple[Path, str, str]]] = {}
    for path in sorted(WORKFLOW_DIR.glob("*.yml")):
        for action, revision, release in USES_PATTERN.findall(path.read_text(encoding="utf-8")):
            found.setdefault(action, []).append((path, revision, release))

    assert set(found) == set(REVIEWED_ACTIONS)
    for action, usages in found.items():
        expected_revision, expected_release = REVIEWED_ACTIONS[action]
        assert all(revision == expected_revision for _path, revision, _release in usages), action
        assert all(release == expected_release for _path, _revision, release in usages), action
