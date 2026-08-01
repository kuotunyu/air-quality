"""Which commit produced a file, and whether that is the whole story.

Lived inside :mod:`twair.viz.export`, whose payloads were the first thing here
to carry a sha. It moved when a second artefact needed the same stamp for the
same reason, and importing it from the export module would have made the build
stage depend on the publishing stage.
"""

from __future__ import annotations

import subprocess

__all__ = ["git_state"]


def _git(*args: str) -> str | None:
    """Run a git command, or return None if git is not there to answer."""
    try:
        out = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip()


def git_state() -> tuple[str | None, bool]:
    """The commit an artefact was made at, and whether the tree was clean.

    This used to be `_git_sha()` alone, documented as "the commit that produced
    this export" — a sentence that is false every time the export is run over
    uncommitted work, which is most times. `rev-parse HEAD` names the last
    commit, not the tree; a payload built from a modified working directory is
    stamped with the commit BEFORE the change that produced it, and every
    consumer of that field then points at code which cannot have made it.

    That is not hypothetical here. The site's footer prints this sha and now
    links it, `status.py` reports 「generated … from <sha>」, and
    `scripts/check_web_export.py` — whose whole reason for existing is that a
    payload and its manifest drifted apart for two days — printed the field and
    never checked it.

    So the dirty flag travels with the sha. A reader is told 「這份資料匯出時工作
    區還有未提交的變更」 instead of being given a commit link that lies, and the
    gate can say so out loud rather than implying a provenance nobody verified.
    """
    sha = _git("rev-parse", "--short", "HEAD") or None
    if sha is None:
        return None, False
    # `--porcelain` is empty exactly when the tree matches HEAD. Untracked files
    # are excluded: a scratch file beside the repo does not change what the code
    # did, and counting it would mark almost every honest export dirty.
    status = _git("status", "--porcelain", "--untracked-files=no")
    return sha, bool(status)
