"""The emphasis gate, on the ways it could pass while checking nothing.

This one exists because of a reader rather than a re-run. Three non-expert
participants read the site on 2026-08-24 and all three named chapter 3 as where
they disengaged; the third said what they did instead of leaving — 「我主要看粗體
結論」 — and that path was ten bare nouns and four conclusions mixed together.

Its scope is three exclusions, and each of them is the whole gate if it is wrong:

* counting a chart's guide labels as prose put chapter 1 at 74% bare emphasis in
  a report when it is 54%, and aimed a plan at the wrong two chapters;
* a card title is a `<strong>` doing a heading's job, and there are eighteen of
  them on the site — flagging those would make the gate noise on its first run;
* the no-JavaScript fallback is a second rendering, and the station name bolded
  inside it is not an exception anybody needs to write down.

**No test here may read `web/dist`.** The built site is gitignored, so a test
that reads it passes on the machine that built it and fails for everyone else —
which happened to three tests in `tests/test_story.py` this morning and turned
CI red. Pages below are written by hand.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml
from scripts import check_bold_stands_alone as gate


def page(body: str) -> str:
    """A built page's shape, with the chrome a real one carries."""
    return (
        "<html><body><nav><strong>導覽</strong></nav>"
        f"<main>{body}</main>"
        "<footer><strong>頁尾</strong></footer></body></html>"
    )


def config(tmp_path: pathlib.Path, minimum: int, allow: list[dict[str, str]]) -> pathlib.Path:
    path = tmp_path / "emphasis.yaml"
    path.write_text(yaml.safe_dump({"min_chars": minimum, "allow": allow}), encoding="utf-8")
    return path


class TestOnlyRunningProseCounts:
    """Three exclusions, each of which is a different rendering of the page."""

    def test_a_chart_guide_label_is_data_not_emphasis(self) -> None:
        """「WHO 年均指引 5」 bolds a reference line's value. Counting those is
        what made an earlier measurement name the wrong two chapters."""
        html = page(
            "<p>正文<strong>這是一句站得住的宣稱</strong></p>"
            "<figure><p><strong>5</strong></p>"
            "<figcaption><strong>12</strong></figcaption></figure>"
        )

        assert gate.emphasis_in(html) == ["這是一句站得住的宣稱"]

    def test_a_card_title_is_a_heading(self) -> None:
        """`<li><a><strong>基準優勢</strong><p>…</p></a></li>` — outside a `<p>`
        by construction, which is how it tells itself apart without a label."""
        html = page(
            "<ul><li><a href='#x'><strong>基準優勢</strong>"
            "<p>再看圖 6.2：同一批預測相對兩條基準線還剩多少優勢。</p></a></li></ul>"
        )

        assert gate.emphasis_in(html) == []

    def test_the_no_javascript_fallback_is_a_second_rendering(self) -> None:
        html = page(
            "<p class='note cbpf-nojs'>目前顯示的是<strong>富貴角</strong>。</p>"
            "<p>正文<strong>這是一句站得住的宣稱</strong></p>"
        )

        assert gate.emphasis_in(html) == ["這是一句站得住的宣稱"]

    def test_chrome_outside_main_is_not_prose(self) -> None:
        assert gate.emphasis_in(page("<p>沒有強調</p>")) == []

    def test_nested_markup_inside_the_emphasis_is_kept(self) -> None:
        """A bold spanning a link or a value still reads as one passage."""
        html = page("<p><strong>差額 <em>8.8</em> μg/m³，佔了整個下降的 43%</strong></p>")

        assert gate.emphasis_in(html) == ["差額 8.8 μg/m³，佔了整個下降的 43%"]


class TestShortEmphasisNeedsAReason:
    MIN = 12

    def test_a_bare_term_is_reported(self) -> None:
        problems = gate.problems_for("/space/", ["殘差"], self.MIN, set())

        assert len(problems) == 1
        assert "殘差" in problems[0]
        assert "2 characters" in problems[0]

    def test_the_message_names_all_three_ways_out(self) -> None:
        """A gate that only says no teaches nothing. Rewriting, unbolding and
        recording a reason are all correct answers here."""
        problem = gate.problems_for("/space/", ["殘差"], self.MIN, set())[0]

        assert "Say the claim" in problem
        assert "drop the bold" in problem
        assert "conf/emphasis.yaml" in problem

    def test_a_reviewed_passage_passes(self) -> None:
        assert gate.problems_for("/health/", ["差了將近一倍"], self.MIN, {"差了將近一倍"}) == []

    def test_a_passage_long_enough_needs_no_entry(self) -> None:
        assert (
            gate.problems_for("/space/", ["沒有任何單站足以擔負「熱點」之名"], self.MIN, set())
            == []
        )

    def test_the_threshold_is_a_floor_not_a_range(self) -> None:
        assert gate.problems_for("/x/", ["一" * self.MIN], self.MIN, set()) == []
        assert len(gate.problems_for("/x/", ["一" * (self.MIN - 1)], self.MIN, set())) == 1

    def test_an_allowance_is_scoped_to_the_route_it_was_reviewed_on(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The same words can carry a claim in one chapter and name a thing in
        another, so an exception is granted where somebody looked at it."""
        monkeypatch.setattr(
            gate,
            "CONFIG",
            config(
                tmp_path,
                self.MIN,
                [{"route": "/health/", "text": "差了將近一倍", "why": "落差量級"}],
            ),
        )
        _, allowed = gate.load_config()
        by_route = {entry.route for entry in allowed}

        assert by_route == {"/health/"}
        assert gate.problems_for("/health/", ["差了將近一倍"], self.MIN, {"差了將近一倍"}) == []
        assert len(gate.problems_for("/space/", ["差了將近一倍"], self.MIN, set())) == 1


class TestTheConfigIsCheckedRatherThanTrusted:
    def test_an_exception_must_say_why(self, tmp_path: pathlib.Path) -> None:
        path = config(tmp_path, 12, [{"route": "/space/", "text": "殘差"}])

        with pytest.raises(ValueError, match="must say why it stands alone"):
            gate.load_config(path)

    def test_an_exception_that_needs_no_exception_is_refused(self, tmp_path: pathlib.Path) -> None:
        """An entry above the threshold does nothing except make the list look
        longer than the deficit it records."""
        path = config(
            tmp_path,
            12,
            [{"route": "/space/", "text": "沒有任何單站足以擔負「熱點」之名", "why": "多餘"}],
        )

        with pytest.raises(ValueError, match="needs no exception"):
            gate.load_config(path)

    def test_a_misspelled_key_is_refused(self, tmp_path: pathlib.Path) -> None:
        path = config(tmp_path, 12, [{"route": "/space/", "text": "殘差", "reason": "x"}])

        with pytest.raises(ValueError, match="unknown key"):
            gate.load_config(path)

    @pytest.mark.parametrize("minimum", [0, -1, "12", None])
    def test_a_threshold_that_is_not_a_positive_integer_is_refused(
        self, tmp_path: pathlib.Path, minimum: object
    ) -> None:
        path = tmp_path / "emphasis.yaml"
        path.write_text(yaml.safe_dump({"min_chars": minimum, "allow": []}), encoding="utf-8")

        with pytest.raises(ValueError, match="min_chars must be a positive integer"):
            gate.load_config(path)

    def test_the_committed_config_loads(self) -> None:
        minimum, allowed = gate.load_config()

        assert minimum > 0
        assert allowed
        assert all(len(entry.text) < minimum for entry in allowed)
        assert all(entry.why for entry in allowed)


class TestTheGateRunsEndToEnd:
    @staticmethod
    def build(tmp_path: pathlib.Path, bodies: dict[str, str]) -> pathlib.Path:
        dist = tmp_path / "dist"
        for route in gate.ROUTES:
            target = gate.page_for(dist, route)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(page(bodies.get(route, "<p>沒有強調</p>")), encoding="utf-8")
        return dist

    def test_a_site_whose_emphasis_stands_alone_exits_zero(
        self,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setattr(gate, "CONFIG", config(tmp_path, 12, []))
        dist = self.build(
            tmp_path, {"/space/": "<p><strong>沒有任何單站足以擔負熱點之名</strong></p>"}
        )

        assert gate.main(["check", str(dist)]) == 0
        assert "problems         : 0" in capsys.readouterr().out

    def test_a_bare_term_exits_one(
        self,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setattr(gate, "CONFIG", config(tmp_path, 12, []))
        dist = self.build(tmp_path, {"/space/": "<p>量的是<strong>殘差</strong>。</p>"})

        assert gate.main(["check", str(dist)]) == 1
        assert "「殘差」" in capsys.readouterr().out

    def test_the_summary_does_not_claim_review_it_has_not_got(
        self,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """「10 (10 reviewed)」 beside a non-zero problem count would be false
        exactly when it matters."""
        monkeypatch.setattr(
            gate,
            "CONFIG",
            config(
                tmp_path, 12, [{"route": "/health/", "text": "差了將近一倍", "why": "落差量級"}]
            ),
        )
        dist = self.build(
            tmp_path,
            {
                "/health/": "<p><strong>差了將近一倍</strong></p>",
                "/space/": "<p><strong>殘差</strong></p>",
            },
        )

        assert gate.main(["check", str(dist)]) == 1
        out = capsys.readouterr().out
        assert "under 12 characters: 2 (1 reviewed in conf/emphasis.yaml)" in out

    def test_an_allowance_for_a_passage_that_is_gone_is_reported(
        self,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Otherwise the entry describes a page that no longer exists as
        written, and passes forever while checking nothing."""
        monkeypatch.setattr(
            gate,
            "CONFIG",
            config(tmp_path, 12, [{"route": "/space/", "text": "早就刪掉了", "why": "過期"}]),
        )
        dist = self.build(tmp_path, {})

        assert gate.main(["check", str(dist)]) == 1
        assert "stale entry" in capsys.readouterr().out

    def test_a_missing_page_is_a_problem_not_an_empty_pass(
        self,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setattr(gate, "CONFIG", config(tmp_path, 12, []))
        dist = self.build(tmp_path, {})
        gate.page_for(dist, "/space/").unlink()

        assert gate.main(["check", str(dist)]) == 1
        assert "no built page" in capsys.readouterr().out

    def test_a_missing_dist_is_named(
        self, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert gate.main(["check", str(tmp_path / "nope")]) == 1
        assert "npm run build" in capsys.readouterr().err
