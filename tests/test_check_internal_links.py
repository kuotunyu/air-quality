"""The link gate needs the same protections every other gate here already has.

`check_internal_links.py` was written after `check_like_ci.py` had already
shipped — and been fixed for — the defect where a gate reports success for
having examined nothing. It went in without that guard and without a single
test, while the five prose gates beside it carry twenty-five between them.

Its own subject matter makes a silent zero especially plausible. `SKIP_PARTS`
excludes by path *component*, so adding "docs" or "web" to it would skip most of
the tree; and `repoFile` is a helper name that a refactor could rename, after
which the site half of the check would match nothing. Either would have printed
zeroes and exited 0 — a gate whose whole purpose is catching links that look
fine in the source, itself looking fine while checking nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from scripts import check_internal_links as links

REAL_REPO = links.REPO_ROOT


def build(root: Path, tree: dict[str, str]) -> None:
    for relative, body in tree.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A stand-in repository, with both surfaces pointed at it.

    `READER_FACING` is widened to accept anything here. The tests below are
    about whether a link *resolves*; which files a reader may be sent to is an
    editorial decision about the real site, and letting it leak into these
    fixtures would make every unrelated case pick its targets to satisfy a
    policy it is not testing. The policy has its own tests, further down, which
    set the list explicitly.
    """
    monkeypatch.setattr(links, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(links, "WEB_SRC", tmp_path / "web" / "src")
    monkeypatch.setattr(links, "READER_FACING", _ACCEPTS_ANY)
    return tmp_path


class _AcceptsAny:
    """Stands in for `READER_FACING` when a test is not about that rule."""

    def __contains__(self, _item: object) -> bool:
        return True


_ACCEPTS_ANY = _AcceptsAny()


# --- the real repository -----------------------------------------------------


def test_the_real_repository_still_presents_both_surfaces() -> None:
    """If either half stops finding anything, the guards below are why CI fails
    rather than passing quietly — but this says so in one line."""
    found = {path.relative_to(REAL_REPO).as_posix() for path in links.markdown_files()}

    assert "README.md" in found
    assert "docs/methodology.md" in found, "the file the seventeen broken links were in"


def test_the_file_the_gate_was_built_for_still_contains_relative_links() -> None:
    """`docs/methodology.md` held seventeen links that resolved to `docs/src/...`
    and 404ed. The fix made them `../`-relative. If a later edit reverts to the
    bare form, this gate is the only thing that would notice — so the links have
    to still be there to be checked."""
    body = (REAL_REPO / "docs" / "methodology.md").read_text(encoding="utf-8")
    targets = [
        match.group(1)
        for match in links.LINK.finditer(body)
        if not match.group(1).startswith(links.EXTERNAL)
    ]

    assert targets, "no internal links left in methodology.md"
    assert any(target.startswith("../") for target in targets)


# --- refusing to pass for having checked nothing -----------------------------


def test_finding_no_markdown_files_is_refused(repo: Path) -> None:
    build(repo, {"web/src/page.astro": 'repoFile("web/src/page.astro")'})

    with pytest.raises(SystemExit) as excinfo:
        links.main()

    assert "no markdown files" in str(excinfo.value)


def test_markdown_files_with_no_internal_links_is_refused(repo: Path) -> None:
    """The shape a careless `SKIP_PARTS` addition would produce: files still
    found, but every one of them excluded from the part that matters."""
    build(
        repo,
        {
            "README.md": "[upstream](https://example.org) and [top](#intro)",
            "web/src/page.astro": 'repoFile("README.md")',
        },
    )

    with pytest.raises(SystemExit) as excinfo:
        links.main()

    assert "no internal links" in str(excinfo.value)


def test_no_repofile_calls_is_refused(repo: Path) -> None:
    """What renaming the helper would look like."""
    build(
        repo,
        {
            "README.md": "[legal](docs/legal.md)",
            "docs/legal.md": "",
            "web/src/page.astro": 'githubBlob("README.md")',
        },
    )

    with pytest.raises(SystemExit) as excinfo:
        links.main()

    assert "repoFile" in str(excinfo.value)


# --- what it is supposed to catch --------------------------------------------


def test_a_link_resolving_outside_its_own_directory_is_broken(repo: Path) -> None:
    """Exactly the seventeen: written from the repository root, but sitting in
    `docs/`, so they resolve to `docs/docs/...`."""
    build(
        repo,
        {
            "docs/methodology.md": "[pitfalls](docs/pitfalls.py)",
            "docs/pitfalls.py": "",
            "web/src/page.astro": 'repoFile("docs/pitfalls.py")',
        },
    )

    assert links.main() == 1


def test_the_same_link_written_relative_to_its_own_file_passes(repo: Path) -> None:
    build(
        repo,
        {
            "docs/methodology.md": "[pitfalls](pitfalls.py)",
            "docs/pitfalls.py": "",
            "web/src/page.astro": 'repoFile("docs/pitfalls.py")',
        },
    )

    assert links.main() == 0


def test_a_repofile_target_that_moved_is_broken(repo: Path) -> None:
    """The site failure the Astro build cannot see: a wrong URL is still a valid
    string, so moving `docs/legal.md` 404s the live site silently."""
    build(
        repo,
        {
            "README.md": "[legal](docs/legal.md)",
            "docs/legal.md": "",
            "web/src/page.astro": 'repoFile("docs/legal-notice.md")',
        },
    )

    assert links.main() == 1


def test_an_anchor_and_a_percent_encoded_path_are_both_handled(repo: Path) -> None:
    """A trailing `#anchor` is a position inside a file, not part of its name;
    and a spaced filename arrives percent-encoded."""
    build(
        repo,
        {
            "README.md": "[section](docs/legal.md#terms) [spaced](docs/a%20name.md)",
            "docs/legal.md": "",
            "docs/a name.md": "[back](../README.md)",
            "web/src/page.astro": 'repoFile("README.md")',
        },
    )

    assert links.main() == 0


def test_the_repofile_template_in_repo_ts_is_not_read_as_a_path(repo: Path) -> None:
    """`repo.ts` contains the template the helper is built from, not a call."""
    build(
        repo,
        {
            "README.md": "[legal](docs/legal.md)",
            "docs/legal.md": "",
            "web/src/lib/repo.ts": 'repoFile("${path}")',
            "web/src/page.astro": 'repoFile("README.md")',
        },
    )

    assert links.main() == 0


def test_external_urls_are_never_resolved_as_files(repo: Path) -> None:
    """This has to run offline in CI; an upstream outage must not turn it red."""
    build(
        repo,
        {
            "README.md": "[a](https://example.org/x) [b](http://x) [c](mailto:a@b.c)",
            "docs/legal.md": "[home](../README.md)",
            "web/src/page.astro": 'repoFile("README.md")',
        },
    )

    assert links.main() == 0


# --- which files a reader may be sent to -------------------------------------


def test_a_repofile_target_outside_the_reader_facing_list_fails(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rule the owner asked for: a reader should not be sent into the source
    tree unless that file is what they came for. A config file that exists, and
    whose link therefore resolves, still fails."""
    monkeypatch.setattr(links, "READER_FACING", {"docs/legal.md"})
    build(
        repo,
        {
            "README.md": "[legal](docs/legal.md)",
            "docs/legal.md": "",
            "conf/spatial.yaml": "",
            "web/src/page.astro": 'repoFile("conf/spatial.yaml")',
        },
    )

    assert links.main() == 1


def test_a_reader_facing_target_passes(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The same tree, linking the document that earns the trip."""
    monkeypatch.setattr(links, "READER_FACING", {"docs/legal.md"})
    build(
        repo,
        {
            "README.md": "[legal](docs/legal.md)",
            "docs/legal.md": "",
            "conf/spatial.yaml": "",
            "web/src/page.astro": 'repoFile("docs/legal.md")',
        },
    )

    assert links.main() == 0


def test_a_missing_target_is_reported_as_broken_not_as_unwarranted(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The two questions must not collapse into each other. A target that does
    not exist is broken, whatever the policy says about it — reporting it as
    'not reader-facing' would send the next person to edit a list when the fix
    is to correct a path."""
    monkeypatch.setattr(links, "READER_FACING", {"docs/legal.md"})
    build(
        repo,
        {
            "README.md": "[legal](docs/legal.md)",
            "docs/legal.md": "",
            "web/src/page.astro": 'repoFile("conf/gone.yaml")',
        },
    )

    assert links.main() == 1
    out = capsys.readouterr().out
    assert "broken           : 1" in out
    assert "not reader-facing: 0" in out


def test_the_real_site_only_links_reader_facing_files() -> None:
    """The list is not aspirational: the shipped site satisfies it today."""
    assert links.main() == 0
