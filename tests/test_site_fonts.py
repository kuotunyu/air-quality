"""The site's three faces are subsets of what the built pages use, and say so."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from scripts import site_fonts

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_the_display_set_takes_headings_and_the_hero_finding_and_nothing_else(
    tmp_path: Path,
) -> None:
    page = tmp_path / "index.html"
    page.write_text(
        "<html><head><style>h1{color:red}</style><script>var x='略'</script></head>"
        "<body><h1>趨勢</h1><p class='lede hero-finding'>降到</p><h2>方法</h2>"
        "<p>內文</p><svg><title>縣</title></svg></body></html>",
        encoding="utf-8",
    )
    sets = site_fonts.character_sets([page])
    assert {"趨", "勢", "降", "到", "方", "法"} <= sets["display"]
    assert not ({"內", "文"} & sets["display"])
    assert {"內", "文", "趨"} <= sets["sans"]
    assert "略" not in sets["sans"], "script text is not rendered"
    assert "縣" not in sets["sans"], "SVG titles are tooltips, not page text"


def test_every_set_carries_the_base_characters_and_latin_is_the_low_plane(
    tmp_path: Path,
) -> None:
    page = tmp_path / "index.html"
    page.write_text("<p>PM2.5 μg/m³ 的 R²（）</p><h1>趨</h1>", encoding="utf-8")
    sets = site_fonts.character_sets([page])
    assert sets["sans"] >= site_fonts.BASE_CHARACTERS
    assert sets["display"] >= site_fonts.DISPLAY_BASE_CHARACTERS
    assert "（" in sets["display"], "a fullwidth form the body renders is lent to the headings"
    assert "的" not in sets["display"]
    assert all(ord(c) < site_fonts.LATIN_LIMIT for c in sets["latin"])
    assert "的" not in sets["latin"]
    assert "μ" in sets["latin"]


def test_the_check_names_a_character_the_served_faces_lack(tmp_path: Path) -> None:
    fonts = tmp_path / "fonts"
    dist = tmp_path / "dist"
    site_fonts.write_fixture_site(dist, fonts, page_text="A中", face_text="A中")
    assert site_fonts.check(dist, fonts) == []
    (dist / "index.html").write_text("<h1>A中文</h1>", encoding="utf-8")
    problems = site_fonts.check(dist, fonts)
    assert any("文" in p and p.startswith("display") for p in problems)
    assert any("文" in p and p.startswith("sans") for p in problems)


def test_the_self_test_passes() -> None:
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "site_fonts.py"), "--self-test"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "site fonts self-test passed" in result.stdout
