"""The seventh prose gate, tested on the ways it could pass while checking nothing.

`tests/test_prose_agreement_gates.py` exists because three gates reported zero
disagreements against real files while unable to catch the drift they were
written for. The lesson recorded there is that a gate verified only against
correct input is a gate whose failure path has never run.

This one has more ways to be silently ornamental than those did, because it
matches on prose rather than on numbers:

* a term that leaves its route would make its entry describe a page that no
  longer exists, and a `find` returning −1 is the easiest thing in the world to
  read as "nothing wrong here";
* `explains: null` records a measured gap, and a mistyped key that read as
  absent instead of as a string would quietly demote a real check into one;
* the window is the whole discrimination — 67 characters separates every term
  the site explains well from 367 for the nearest one it does not — so a
  comparison that lost its sign, or its `abs`, would admit the failures it was
  measured to exclude.

The extraction has its own failure paths, and two of them were real. Chapter 10's
file table concatenated adjacent cells into `JSON0.36` until block elements
separated their text, and chapter 9's SQL examples contributed `mean`,
`station_name` and `NULL` as if they were vocabulary until `<code>` became
opaque. Both would have made this gate report on text no reader ever sees.

**No test here may require `web/dist`.** Most of `data/` and all of the built
site are gitignored, so a test that reads them passes on the machine that built
them and fails for everyone else — the trap `AGENTS.md` names. The pages below
are written out by hand for that reason.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml
from scripts import check_term_first_use as gate

WINDOW = 40


def page(body: str, *, nav: str = "第一章 趨勢 深色") -> str:
    """A built page's shape: chrome, then `<main>`, then chrome.

    The nav carries chapter names on every route, so a term counted outside
    `<main>` would appear on all eleven and be explained on none.
    """
    return (
        f"<html><body><nav>{nav}</nav><main><p>{body}</p></main><footer>目錄</footer></body></html>"
    )


def glossary(tmp_path: pathlib.Path, window: int, terms: list[dict[str, object]]) -> pathlib.Path:
    path = tmp_path / "glossary.yaml"
    path.write_text(yaml.safe_dump({"window": window, "terms": terms}), encoding="utf-8")
    return path


def term(**overrides: object) -> gate.Term:
    fields: dict[str, object] = {
        "term": "CBPF",
        "route": "/sources/",
        "explains": "描述條件機率",
        "why": None,
    }
    fields.update(overrides)
    return gate.Term(**fields)  # type: ignore[arg-type]


class TestTheWindowIsTheWholeDiscrimination:
    """Measured, not chosen: 67 characters against 367, with nothing between.

    Every term the site explains well puts the explanation within 67 characters
    of first use; the nearest one it explains badly is at 367. A comparison that
    dropped its `abs` or its sign would let one of those groups pass as the
    other, and nothing about the output would look different.
    """

    def test_an_explanation_at_the_window_still_passes(self) -> None:
        text = "CBPF" + "填" * (WINDOW - len("CBPF")) + "描述條件機率"

        assert gate.problems_for(term(), text, WINDOW) == []

    def test_one_character_further_does_not(self) -> None:
        text = "CBPF" + "填" * (WINDOW - len("CBPF") + 1) + "描述條件機率"

        problems = gate.problems_for(term(), text, WINDOW)

        assert len(problems) == 1
        assert "further than" in problems[0]

    def test_the_distance_is_reported_so_the_reader_learns_something(self) -> None:
        text = "CBPF" + "填" * 600 + "描述條件機率"

        problems = gate.problems_for(term(), text, WINDOW)

        assert "604 characters after" in problems[0], problems
        assert "first used at 0" in problems[0]

    def test_an_explanation_before_the_term_is_measured_the_same_way(self) -> None:
        """反事實 is explained 42 characters *before* its first use. An unsigned
        subtraction would read that as a negative distance and pass everything
        that came before the term, however far back."""
        near = "以什麼濃度作為比較基準" + "填" * 10 + "反事實"
        far = "以什麼濃度作為比較基準" + "填" * 600 + "反事實"
        entry = term(term="反事實", route="/health/", explains="以什麼濃度作為比較基準")

        assert gate.problems_for(entry, near, WINDOW) == []
        problems = gate.problems_for(entry, far, WINDOW)
        assert len(problems) == 1
        assert "before that" in problems[0]

    def test_the_first_use_is_the_one_measured_from(self) -> None:
        """The defect this gate exists for is a term introduced by an index or a
        legend ahead of the prose that defines it. Measuring from a later
        occurrence would find the definition next door and report nothing —
        which is exactly what a proofread does, one screen at a time."""
        text = "PM10" + "填" * 900 + "PM10 直徑 10 微米以下的顆粒"
        entry = term(term="PM10", route="/methods/", explains="直徑 10 微米以下的顆粒")

        problems = gate.problems_for(entry, text, WINDOW)

        assert len(problems) == 1
        assert "first used at 0" in problems[0]


class TestTheDistancesAreComputedRatherThanRecorded:
    """The first version of this gate wrote them into a comment instead.

    Seven of the thirteen were wrong on the day they were committed — measured
    with a draft of the extractor rather than the one that shipped — and one term
    was listed as unexplained while the page explained it 35 characters after
    first use. None of that was catchable, because a comment is not checked by
    anything. The numbers are recomputed every run now, so they cannot describe a
    page that has changed underneath them.
    """

    def test_the_distance_is_signed_so_a_gloss_before_the_term_is_visible(self) -> None:
        after = term()
        assert gate.distance_for(after, "CBPF" + "填" * 6 + "描述條件機率") == 10
        assert gate.distance_for(after, "描述條件機率" + "填" * 4 + "CBPF") == -10

    def test_an_unexplained_term_has_no_distance_to_report(self) -> None:
        entry = term(explains=None, why="沒有任何一章解釋它")

        assert gate.distance_for(entry, "CBPF 是一張極座標圖") is None

    def test_a_missing_string_yields_no_number_rather_than_a_wrong_one(self) -> None:
        """`find` returns −1 for either side. Subtracting that produces a real
        integer that would print as a plausible distance."""
        assert gate.distance_for(term(), "描述條件機率 只有解釋") is None
        assert gate.distance_for(term(), "CBPF 只有詞") is None

    def test_every_explained_term_prints_its_own_measurement(
        self,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        path = glossary(
            tmp_path, 160, [{"term": "CBPF", "route": "/sources/", "explains": "描述條件機率"}]
        )
        monkeypatch.setattr(gate, "GLOSSARY", path)
        dist = tmp_path / "dist"
        (dist / "sources").mkdir(parents=True)
        (dist / "sources" / "index.html").write_text(
            page("CBPF 描述條件機率，不識別污染來源"), encoding="utf-8"
        )

        assert gate.main(["check", str(dist)]) == 0
        assert "/sources/ CBPF — 解釋在首次出現後 5 字" in capsys.readouterr().out


class TestAGateThatFindsNothingMustNotReportSuccess:
    """−1 from `find` is the easiest failure in the file to read as a pass."""

    def test_a_term_that_left_its_route_is_a_problem_not_a_pass(self) -> None:
        problems = gate.problems_for(term(), "這一章沒有那個詞", WINDOW)

        assert len(problems) == 1
        assert "stale" in problems[0]

    def test_a_missing_term_is_stale_even_when_it_has_no_explanation(self) -> None:
        """An unexplained term is bookkeeping for a known gap. If the term is
        gone the gap is gone too, and the entry is now describing nothing."""
        entry = term(term="holdout", route="/trend/", explains=None, why="全站只出現這一次")

        problems = gate.problems_for(entry, "這一章沒有那個詞", WINDOW)

        assert len(problems) == 1
        assert "stale" in problems[0]

    def test_a_deleted_explanation_says_how_to_fix_it(self) -> None:
        """Rewording a sentence is allowed and will trip this. The message has to
        say so, or the next person reads a red gate as a broken gate."""
        problems = gate.problems_for(term(), "CBPF 是一種圖", WINDOW)

        assert len(problems) == 1
        assert "描述條件機率" in problems[0]
        assert "conf/glossary.yaml" in problems[0]

    def test_an_unexplained_term_that_is_present_is_not_a_problem(self) -> None:
        entry = term(term="holdout", route="/trend/", explains=None, why="全站只出現這一次")

        assert gate.problems_for(entry, "holdout R² 中位數 0.445", WINDOW) == []


class TestTheGlossaryIsCheckedRatherThanTrusted:
    """A mistyped key is the one way this gate could check nothing and say so."""

    def test_a_null_explanation_must_say_what_is_known(self, tmp_path: pathlib.Path) -> None:
        path = glossary(tmp_path, 160, [{"term": "holdout", "route": "/trend/", "explains": None}])

        with pytest.raises(ValueError, match="why must say what is known"):
            gate.load_glossary(path)

    def test_a_misspelled_key_cannot_pass_as_an_unexplained_term(
        self, tmp_path: pathlib.Path
    ) -> None:
        """`explain` instead of `explains` would leave `explains` absent, which
        reads as null — silently turning a checked term into a listed gap."""
        path = glossary(
            tmp_path,
            160,
            [{"term": "CBPF", "route": "/sources/", "explain": "描述條件機率"}],
        )

        with pytest.raises(ValueError, match="unknown key"):
            gate.load_glossary(path)

    def test_an_explained_term_may_not_also_carry_a_reason(self, tmp_path: pathlib.Path) -> None:
        """`why` beside a real explanation reads as a documented gap. It is not
        one, and leaving both would hide which of the two the gate acted on."""
        path = glossary(
            tmp_path,
            160,
            [{"term": "CBPF", "route": "/sources/", "explains": "描述條件機率", "why": "沒有"}],
        )

        with pytest.raises(ValueError, match="unexplained term only"):
            gate.load_glossary(path)

    def test_an_empty_explanation_is_not_a_string_the_page_can_satisfy(
        self, tmp_path: pathlib.Path
    ) -> None:
        """`""` is in every page at position 0, so it would pass for any term
        whose first use is within the window of the start of the document."""
        path = glossary(tmp_path, 160, [{"term": "CBPF", "route": "/sources/", "explains": ""}])

        with pytest.raises(ValueError, match="non-empty string or null"):
            gate.load_glossary(path)

    def test_the_same_term_on_the_same_route_may_not_be_listed_twice(
        self, tmp_path: pathlib.Path
    ) -> None:
        path = glossary(
            tmp_path,
            160,
            [
                {"term": "R²", "route": "/forecast/", "explains": "都是無單位的比例"},
                {"term": "R²", "route": "/forecast/", "explains": "衡量的是"},
            ],
        )

        with pytest.raises(ValueError, match="one entry per term per route"):
            gate.load_glossary(path)

    def test_the_same_term_on_two_routes_is_the_point(self, tmp_path: pathlib.Path) -> None:
        """R² is explained in chapter 6 and not in chapter 1, and chapter 1 is
        where most readers arrive. Per-route entries are what lets the gate say
        that."""
        path = glossary(
            tmp_path,
            160,
            [
                {"term": "R²", "route": "/forecast/", "explains": "都是無單位的比例"},
                {"term": "R²", "route": "/trend/", "explains": None, "why": "第一章沒有"},
            ],
        )

        _, terms = gate.load_glossary(path)

        assert [(t.route, t.explains) for t in terms] == [
            ("/forecast/", "都是無單位的比例"),
            ("/trend/", None),
        ]

    @pytest.mark.parametrize("window", [0, -1, "160", None, 1.5])
    def test_a_window_that_is_not_a_positive_integer_is_refused(
        self, tmp_path: pathlib.Path, window: object
    ) -> None:
        """A window of 0 would fail every term whose explanation is not exactly
        adjacent; a string compares as neither."""
        path = tmp_path / "glossary.yaml"
        path.write_text(
            yaml.safe_dump(
                {"window": window, "terms": [{"term": "a", "route": "/x/", "explains": "b"}]}
            ),
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="window must be a positive integer"):
            gate.load_glossary(path)

    def test_a_route_must_be_a_route(self, tmp_path: pathlib.Path) -> None:
        path = glossary(tmp_path, 160, [{"term": "CBPF", "route": "sources", "explains": "x"}])

        with pytest.raises(ValueError, match="route beginning with /"):
            gate.load_glossary(path)

    def test_the_committed_glossary_loads(self) -> None:
        """The file this gate actually reads, checked for shape in the suite so a
        malformed edit fails in the `test` job rather than only after a build."""
        window, terms = gate.load_glossary()

        assert window > 0
        assert terms
        assert all(t.why is None for t in terms if t.explains is not None)
        assert all(t.why for t in terms if t.explains is None)


class TestTheTextIsWhatAReaderMeets:
    """Two extraction defects were real, and both would have gated fiction."""

    def test_adjacent_cells_do_not_concatenate(self) -> None:
        """Chapter 10's file table produced `JSON0.36` and `MBParquet2.37`. A
        term glued to a number is a term this gate cannot find."""
        html = "<main><table><tr><td>JSON</td><td>0.36 MB</td></tr></table></main>"

        assert gate.rendered(html) == "JSON 0.36 MB"

    def test_inline_markup_does_not_split_a_sentence(self) -> None:
        """The opposite failure: separating every tag would break an explanation
        that spans `<strong>`, and most of them do."""
        html = "<main><p>R² 和 skill 都是<strong>無單位</strong>的比例</p></main>"

        assert gate.rendered(html) == "R² 和 skill 都是無單位的比例"

    def test_code_is_shown_but_is_not_prose(self) -> None:
        """Chapter 9's SQL examples contributed `mean`, `station_name` and
        `NULL`. They are column names a reader is shown, not vocabulary a reader
        is expected to have."""
        html = "<main><p>範例</p><pre><code>SELECT mean FROM pm25 WHERE mean IS NOT NULL</code></pre></main>"

        assert gate.rendered(html) == "範例"

    def test_entities_are_what_the_reader_sees(self) -> None:
        """The page carries `&lt;` in prose. Read literally it matches nothing a
        person ever sees."""
        html = "<main><p>時數不足（&lt; 20 小時）：不予報告</p></main>"

        assert gate.rendered(html) == "時數不足（< 20 小時）：不予報告"

    def test_navigation_is_not_the_chapter(self) -> None:
        """偵測極限 is in the nav of all eleven routes and explained on one. Text
        counted outside `<main>` would make every route claim the term."""
        assert gate.rendered(page("這一章量的就是它")) == "這一章量的就是它"

    def test_a_page_with_no_main_yields_nothing_rather_than_everything(self) -> None:
        """Falling back to the whole document would silently restore the nav."""
        assert gate.rendered("<html><body><nav>偵測極限</nav></body></html>") == ""


class TestAFigureContributesItsWordsNotItsValues:
    """殘差 was carried as an unexplained term for a week on this arithmetic.

    圖 3.1's caption defines it immediately after naming it, which is where a
    reader looking at the correlogram already is. Between the term's first use in
    the chapter's question list and that caption lay the plot — eight band
    labels, eight values, three axis ticks — and flattening those measured 259
    where the page a reader sees puts the two one glance apart. The entry
    recorded a real number and a false conclusion, and the two fixes that would
    have closed it faster were both wrong: a gloss above the figure pushes the
    primary plot below the fold, and widening the window is tuning a threshold
    until one case fits.
    """

    CORRELOGRAM = (
        "<figure><div class='correlogram'>"
        "<div class='corr-row'><span>0–10 km</span><span>+0.277</span></div>"
        "<div class='corr-row'><span>100–150 km</span><span>−0.230</span></div>"
        "<span class='axis'><span>−0.2</span><span>0</span><span>+0.2</span></span>"
        "</div><figcaption>站均殘差的 Moran's I。殘差是扣掉模型已經解釋掉的部分。</figcaption></figure>"
    )
    PAGE = f"<main><p>殘差相依如何改變？</p>{CORRELOGRAM}<p>這個問題有數字可答。</p></main>"

    def test_a_plots_values_are_scanned_rather_than_read(self) -> None:
        assert gate.rendered(self.PAGE) == (
            "殘差相依如何改變？ 站均殘差的 Moran's I。殘差是扣掉模型已經解釋掉的部分。 這個問題有數字可答。"
        )

    def test_reading_the_plot_is_what_put_the_caption_out_of_reach(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The failure path, run rather than described. Restoring the containers
        a plot is built from is the behaviour this gate shipped with, and on the
        same page it more than doubles the distance to the same caption."""
        entry = term(term="殘差", route="/space/", explains="扣掉模型已經解釋掉的部分")

        assert gate.distance_for(entry, gate.rendered(self.PAGE)) == 29

        monkeypatch.setattr(gate, "FIGURE_PROSE", gate.FIGURE_PROSE | {"div", "span"})
        assert gate.distance_for(entry, gate.rendered(self.PAGE)) == 70

    def test_a_legend_is_still_where_a_reader_meets_the_word(self) -> None:
        """圖 6.1's legend is a list and `persistence` is first used in it. A
        reader who has to understand a legend to read the chart under it is the
        defect this gate was built for, so dropping `<li>` with the rest of the
        furniture would move that first use down into the prose, where the
        explanation is next door and nothing looks wrong."""
        html = (
            "<main><figure><ul><li><span>模型</span></li><li><span>persistence</span></li></ul>"
            "<figcaption>兩條基準線都是規則，說「下一小時就跟現在一樣」。</figcaption>"
            "</figure></main>"
        )
        entry = term(term="persistence", route="/forecast/", explains="說「下一小時就跟現在一樣」")
        text = gate.rendered(html)

        assert text.startswith("模型 persistence")
        assert gate.problems_for(entry, text, WINDOW) == []

    def test_a_gloss_inside_a_list_item_survives(self) -> None:
        """安慰劑's sits directly inside an `<li>` of 圖 5.1's reading steps —
        inside the figure and outside its caption. Keeping only `<figcaption>`,
        or `<p>` and `<figcaption>`, loses it outright and the gate then reports
        an explanation as gone while a reader is looking at it."""
        html = (
            "<main><figure><div class='chart'><span>8.8</span></div>"
            "<ol><li><strong>先看灰線</strong>：沒有事件標記時，同一程序仍會算出的差額。</li></ol>"
            "</figure><p>安慰劑檢定就是這樣讀的。</p></main>"
        )

        assert gate.rendered(html) == (
            "先看灰線：沒有事件標記時，同一程序仍會算出的差額。 安慰劑檢定就是這樣讀的。"
        )

    def test_a_paragraph_inside_a_figure_is_prose(self) -> None:
        """圖 4.1's radius legend is glossed in a `<p>` beside the plot."""
        html = "<main><figure><div><span>0.32</span></div><p>半徑＝風速分組</p></figure></main>"

        assert gate.rendered(html) == "半徑＝風速分組"

    def test_the_page_resumes_after_the_figure(self) -> None:
        """Two figures in a row is the shape that would expose a counter which
        never returns to zero: everything after the first one would vanish, and
        the gate would report explanations as missing across whole chapters."""
        html = (
            "<main><figure><div><span>99</span></div></figure><p>中間的正文</p>"
            "<figure><div><span>88</span></div></figure><p>後面的正文</p></main>"
        )

        assert gate.rendered(html) == "中間的正文 後面的正文"

    def test_the_rule_is_scoped_to_figures(self) -> None:
        """A `<div>` of numbers in running prose is text a reader meets. The
        claim is about plots, not about tags."""
        html = "<main><div><span>99</span> 不在圖裡</div></main>"

        assert gate.rendered(html) == "99 不在圖裡"


class TestTheRouteReachesTheRightFile:
    @pytest.mark.parametrize(
        ("route", "expected"),
        [("/", "index.html"), ("/trend/", "trend/index.html"), ("/space/", "space/index.html")],
    )
    def test_a_route_maps_to_its_built_page(
        self, tmp_path: pathlib.Path, route: str, expected: str
    ) -> None:
        assert gate.page_for(tmp_path, route) == tmp_path / pathlib.PurePosixPath(expected)


class TestTheGateRunsEndToEnd:
    """The exit code, on pages written here rather than built."""

    @staticmethod
    def build(tmp_path: pathlib.Path, sources_body: str) -> pathlib.Path:
        dist = tmp_path / "dist"
        (dist / "sources").mkdir(parents=True)
        (dist / "sources" / "index.html").write_text(page(sources_body), encoding="utf-8")
        return dist

    def test_a_page_that_still_explains_itself_exits_zero(
        self,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        path = glossary(
            tmp_path, 160, [{"term": "CBPF", "route": "/sources/", "explains": "描述條件機率"}]
        )
        monkeypatch.setattr(gate, "GLOSSARY", path)
        dist = self.build(tmp_path, "CBPF 描述條件機率，不識別污染來源")

        assert gate.main(["check", str(dist)]) == 0
        assert "problems         : 0" in capsys.readouterr().out

    def test_a_page_that_lost_its_explanation_exits_one(
        self,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        path = glossary(
            tmp_path, 160, [{"term": "CBPF", "route": "/sources/", "explains": "描述條件機率"}]
        )
        monkeypatch.setattr(gate, "GLOSSARY", path)
        dist = self.build(tmp_path, "CBPF 是一張極座標圖")

        assert gate.main(["check", str(dist)]) == 1
        assert "problems         : 1" in capsys.readouterr().out

    def test_a_route_with_no_built_page_is_a_problem(
        self,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A renamed slug would otherwise read as a page with no terms in it."""
        path = glossary(tmp_path, 160, [{"term": "CBPF", "route": "/gone/", "explains": "x"}])
        monkeypatch.setattr(gate, "GLOSSARY", path)
        dist = self.build(tmp_path, "CBPF 描述條件機率")

        assert gate.main(["check", str(dist)]) == 1
        assert "no built page" in capsys.readouterr().out

    def test_a_missing_dist_is_named_rather_than_skipped(
        self, tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert gate.main(["check", str(tmp_path / "nope")]) == 1
        assert "npm run build" in capsys.readouterr().err

    def test_the_summary_does_not_claim_a_gloss_it_could_not_find(
        self,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A single 「N explained within 160 characters」 is false exactly when a
        term has lost its gloss, which is the state the line is there to report.
        `check_bold_stands_alone.py` carries the same repair for the same
        reason."""
        path = glossary(
            tmp_path, 160, [{"term": "CBPF", "route": "/sources/", "explains": "描述條件機率"}]
        )
        monkeypatch.setattr(gate, "GLOSSARY", path)
        dist = self.build(tmp_path, "CBPF 是一張極座標圖")

        assert gate.main(["check", str(dist)]) == 1
        out = capsys.readouterr().out
        assert "terms explained  : 1 recorded in conf/glossary.yaml" in out
        assert "found on the page: 0   within 160 characters: 0" in out

    def test_a_gloss_out_of_reach_is_counted_as_found_and_not_as_near(
        self,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The other half: present on the page and too far to help. Collapsing
        the two would report a drifted term as a missing one."""
        path = glossary(
            tmp_path, 160, [{"term": "CBPF", "route": "/sources/", "explains": "描述條件機率"}]
        )
        monkeypatch.setattr(gate, "GLOSSARY", path)
        dist = self.build(tmp_path, "CBPF" + "填" * 200 + "描述條件機率")

        assert gate.main(["check", str(dist)]) == 1
        assert "found on the page: 1   within 160 characters: 0" in capsys.readouterr().out

    def test_the_open_gaps_are_printed_every_run(
        self,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A gap that is only true is not the same as a gap that is visible. The
        list is the reason this gate is not an exemption mechanism."""
        path = glossary(
            tmp_path,
            160,
            [{"term": "CBPF", "route": "/sources/", "explains": None, "why": "沒有任何一章解釋它"}],
        )
        monkeypatch.setattr(gate, "GLOSSARY", path)
        dist = self.build(tmp_path, "CBPF 是一張極座標圖")

        assert gate.main(["check", str(dist)]) == 0
        out = capsys.readouterr().out
        assert "terms unexplained: 1" in out
        assert "待補 /sources/ CBPF — 沒有任何一章解釋它" in out
