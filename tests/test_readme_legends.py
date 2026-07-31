"""Tests for the archive legend inventory in `scripts/inspect_readmes.py`.

The script itself needs `data/raw/airtw`, which is not in the repository, so
what is tested here is its parsing — which is where the defect was. The previous
version could only find `888`/`999` inside `if year == 2001`, so it reported the
2017 edition as documenting five symbols while two other files in this repo said
that edition defines seven. An instrument that can only confirm what it was told
produces a table that looks like a measurement and is not one.
"""

from __future__ import annotations

import io
import zipfile

from scripts.inspect_readmes import (
    decode_legend,
    legend_block,
    repair_name,
    span,
    tokens,
)

# The wording used by every edition except 2017: one definition per line, the
# token separated from its meaning by run-on spaces.
COLUMNAR = """9.普通測站資料註記說明：
  #       表示儀器檢核為無效值
  *       表示程式檢核為無效值
  x       表示人工檢核為無效值
  NR      表示無降雨
  空白    表示缺值
  888     表示風向不定
  999     表示靜風

10.測項簡稱        單位           測項名稱
  SO2             ppb            二氧化硫
"""

# The 2017 edition, whose sixth line is a sentence rather than a table row.
PROSE = """普通測站資料註記說明：
#表示儀器檢核為無效值
*表示程式檢核為無效值
x表示人工檢核為無效值
NR表示無降雨
空白表示缺值
風向資料888代表無風，999則代表儀器故障。
測項簡稱
單位
SO2
"""


def _odt(paragraphs: list[str]) -> bytes:
    """A minimal ODT: the parser only ever reads content.xml."""
    body = "".join(f"<text:p>{p}</text:p>" for p in paragraphs)
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<office:document-content xmlns:office="urn:office" xmlns:text="urn:text">'
        f"<office:body><office:text>{body}</office:text></office:body>"
        "</office:document-content>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("content.xml", xml)
    return buffer.getvalue()


class TestTokenDiscovery:
    def test_a_token_nobody_listed_in_advance_is_still_found(self) -> None:
        """This is the whole point of the rewrite.

        The parser is given no list of expected symbols. A legend that defined
        a code this project has never seen would show up in the report rather
        than be silently dropped, which is the only way the report can be
        evidence about a set of files nobody has read.
        """
        invented = COLUMNAR.replace("888     表示風向不定", "777     表示測試用途")

        assert "777" in tokens(legend_block(invented))

    def test_the_sentinel_codes_are_found_inside_a_sentence(self) -> None:
        """The 2017 edition writes them as prose, not as two more table rows.

        Matching on 「表示」 alone would miss it: that edition says 「代表」.
        """
        assert tokens(legend_block(PROSE)) == ["#", "*", "x", "NR", "空白", "888", "999"]

    def test_the_two_editions_define_the_same_seven_tokens(self) -> None:
        """Which is why the disagreement between them is about meaning, not coverage."""
        assert tokens(legend_block(COLUMNAR)) == tokens(legend_block(PROSE))

    def test_a_legend_without_the_sentinels_yields_five(self) -> None:
        five = COLUMNAR.replace("  888     表示風向不定\n", "").replace("  999     表示靜風\n", "")

        assert tokens(legend_block(five)) == ["#", "*", "x", "NR", "空白"]


class TestSectionBoundaries:
    def test_the_units_table_is_not_part_of_the_legend(self) -> None:
        """`SO2 ppb 二氧化硫` is not a definition and must not become a token."""
        block = legend_block(COLUMNAR)

        assert not any("SO2" in line for line in block)
        assert "SO2" not in tokens(block)

    def test_a_document_without_the_heading_yields_nothing(self) -> None:
        assert legend_block("just some prose about downloads") == []

    def test_the_prose_edition_ends_at_the_units_heading(self) -> None:
        assert len(legend_block(PROSE)) == 6


class TestDecoding:
    def test_a_big5_member_name_survives_pythons_cp437_guess(self) -> None:
        """Zip stores names as bytes; without the UTF-8 flag Python assumes cp437."""
        original = "85年離島監測站/ReadMe_普通測站_20080818.txt"
        mojibake = original.encode("cp950").decode("cp437")

        assert repair_name(mojibake) == original

    def test_a_name_that_is_already_correct_is_left_alone(self) -> None:
        assert repair_name("ReadMe.txt") == "ReadMe.txt"

    def test_a_big5_text_member_decodes(self) -> None:
        text = decode_legend("ReadMe_普通測站.txt", COLUMNAR.encode("cp950"))

        assert text is not None
        assert "表示儀器檢核為無效值" in text

    def test_an_odt_becomes_one_line_per_paragraph(self) -> None:
        """Joining the paragraphs would weld two definitions into one line."""
        text = decode_legend("ReadMe.odt", _odt(["#表示儀器檢核為無效值", "NR表示無降雨"]))

        assert text is not None
        assert "#表示儀器檢核為無效值" in text.splitlines()
        assert "NR表示無降雨" in text.splitlines()

    def test_a_binary_edition_yields_no_text_rather_than_mojibake(self) -> None:
        """`.doc` is Word binary. Reporting it as unparsed is honest; guessing is not."""
        assert decode_legend("ReadMe_普通測站20170301.doc", b"\xd0\xcf\x11\xe0") is None


class TestYearSpans:
    def test_consecutive_years_collapse_and_gaps_survive(self) -> None:
        """The 2017 edition ships with 1993-1995 and again with 2014-2017 minus 2015.

        Printing that as a single range would erase the gap, and the gap is the
        finding: the edition was retro-fitted onto years it postdates.
        """
        assert span([1993, 1994, 1995, 2014, 2016, 2017]) == "1993–1995, 2014, 2016–2017"

    def test_a_single_year_prints_alone(self) -> None:
        assert span([2001]) == "2001"

    def test_no_years_prints_a_dash(self) -> None:
        assert span([]) == "—"
