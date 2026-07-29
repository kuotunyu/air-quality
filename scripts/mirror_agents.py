"""Regenerate `.github/copilot-instructions.md` from `AGENTS.md`, or check it.

The two files are the same rules read by different agents, and they have to say
the same thing. Keeping that true by hand failed in a way worth recording.

The documented regeneration was a one-liner that wrote the copy with
``newline="\\n"``. `AGENTS.md` is checked out CRLF on Windows, so the copy came
out LF and a plain ``diff`` reported all 224 lines as different — every time,
immediately after regenerating. The contents were identical throughout; the
check was reporting an artefact.

That is worse than no check. A comparison that always fails teaches you to stop
running it, and then a real divergence looks exactly like the noise you have
been ignoring for months.

So the copy now carries the source's bytes verbatim, header included, which
makes byte equality the right test and ``--check`` a thing CI can run.

    python scripts/mirror_agents.py            # rewrite the copy
    python scripts/mirror_agents.py --check    # exit 1 if it has drifted
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE = REPO_ROOT / "AGENTS.md"
MIRROR = REPO_ROOT / ".github" / "copilot-instructions.md"

HEADER_LINES = (
    "<!-- MIRROR OF AGENTS.md — edit both together, or neither. -->",
    "<!-- GitHub Copilot reads this file automatically for repository custom instructions. -->",
    "",
)


def expected() -> bytes:
    """The mirror's exact bytes, in the source's own line ending."""
    raw = SOURCE.read_bytes()
    # Match whatever the working copy actually has rather than assuming: this
    # file is edited on Windows and read in CI on Linux.
    eol = b"\r\n" if b"\r\n" in raw else b"\n"
    header = eol.join(line.encode("utf-8") for line in HEADER_LINES) + eol
    return header + raw


def main(argv: list[str]) -> int:
    if not SOURCE.exists():
        print(f"missing {SOURCE}", file=sys.stderr)
        return 1

    wanted = expected()
    if "--check" in argv:
        current = MIRROR.read_bytes() if MIRROR.exists() else b""
        if current == wanted:
            print(f"{MIRROR.relative_to(REPO_ROOT)} matches AGENTS.md")
            return 0
        print(
            f"{MIRROR.relative_to(REPO_ROOT)} has drifted from AGENTS.md — "
            "run: python scripts/mirror_agents.py",
            file=sys.stderr,
        )
        return 1

    MIRROR.write_bytes(wanted)
    print(f"wrote {MIRROR.relative_to(REPO_ROOT)} ({len(wanted):,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
