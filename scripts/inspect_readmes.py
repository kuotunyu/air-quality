"""Inventory the flag legends shipped inside all 44 raw archives (1982-2025).

This answers a question that has sat open in `docs/archive-formats.md` since the
archives were first unpacked: do the `.txt` ReadMe files in the 1996-2011
packages carry a legend that changes from year to year?

**The previous version of this script could not answer it.** It looked for the
sentinel codes with

    if year == 2001:
        if "888     表示" in text:

so 888 and 999 were findable in exactly one archive, by construction. The table
it generated therefore recorded the 2017 edition as documenting five symbols,
while `docs/methodology.md` and `twair.qc.sentinels` both say that edition
defines 888 and 999 with the opposite semantics to the 2001 one. Both statements
were in the repository at once and they contradicted each other; the instrument
was the one at fault. It also read a single representative ReadMe per year, so
the six archives that ship more than one edition of the same filename reported
whichever the archive happened to list first.

This version discovers instead of confirming:

* every documentation member of every archive is read, not one per year
* editions are identified by content hash, never by filename — 2002-2007 all
  ship a file called `ReadMe_普通測站_20080801.txt` with five different bodies
* the legend section is located by its heading and printed **verbatim**, so a
  token this script fails to parse is still visible in the output
* tokens are parsed with one pattern over the whole block rather than tested
  against a list of expected symbols

Run from the repository root, with the archives present under `data/raw/airtw`:

    uv run python scripts/inspect_readmes.py > /tmp/legends.md

The output is Markdown, and the tables under
「歷年 ReadMe 說明文件品質旗標盤點」in `docs/archive-formats.md` are pasted
from it.
"""

from __future__ import annotations

import difflib
import hashlib
import io
import re
import sys
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from xml.etree import ElementTree

from twair.ingest.archive import ArchiveContainer

RAW = Path("data/raw/airtw")

DOC_SUFFIXES = (".txt", ".odt", ".doc", ".pdf", ".rtf")

# Zip stores names as bytes; without the UTF-8 flag Python decodes them as
# cp437, which turns Big5 into mojibake. Round-tripping recovers the original.
NAME_FALLBACK = ("cp437", "cp950")

TEXT_ENCODINGS = ("utf-8-sig", "cp950", "big5hkscs")

LEGEND_HEADING = "資料註記說明"

# A definition line names a token and then says what it means. Both verbs occur:
# the 2017 edition writes 「888代表無風」 inside a sentence, every other edition
# writes 「888     表示風向不定」 on its own line. Anchoring on the verb rather
# than on a list of symbols is what lets a token nobody expected be found.
DEFINITION = re.compile(r"(?P<token>[#*xX]+|[A-Z]{2,3}|空白|\d{2,4})\s*(?:則)?(?:表示|代表)")

NUMBERED_HEADING = re.compile(r"^\d+[.、]")


@dataclass(frozen=True, slots=True)
class Legend:
    """One documentation file, as found inside one archive."""

    year: int
    member: str
    digest: str
    text: str | None
    """None for the binary formats (.doc, .pdf) this script does not parse."""

    @property
    def filename(self) -> str:
        return Path(self.member).name

    @property
    def suffix(self) -> str:
        return Path(self.member).suffix.lower()


def repair_name(name: str) -> str:
    """Recover a Big5 zip member name that Python decoded as cp437."""
    try:
        return name.encode(NAME_FALLBACK[0]).decode(NAME_FALLBACK[1])
    except (UnicodeEncodeError, UnicodeDecodeError):
        return name


def decode_plain(blob: bytes) -> str | None:
    for encoding in TEXT_ENCODINGS:
        try:
            return blob.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


def decode_odt(blob: bytes) -> str | None:
    """Flatten an ODT's content.xml to text.

    Paragraph and line-break elements become newlines, because the legend is a
    list of one-line definitions and joining them would hide where each ends.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as odt:
            tree = ElementTree.fromstring(odt.read("content.xml"))
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError):
        return None

    parts: list[str] = []
    for element in tree.iter():
        if element.tag.rsplit("}", 1)[-1] in {"p", "h", "line-break", "tab"}:
            parts.append("\n")
        if element.text:
            parts.append(element.text)
        if element.tail:
            parts.append(element.tail)
    return "".join(parts)


def decode_legend(member: str, blob: bytes) -> str | None:
    suffix = Path(member).suffix.lower()
    if suffix == ".txt":
        return decode_plain(blob)
    if suffix == ".odt":
        return decode_odt(blob)
    return None


def collect(raw_dir: Path) -> list[Legend]:
    """Read every documentation member of every archive."""
    found: list[Legend] = []
    for path in sorted(raw_dir.glob("*.zip")):
        stem = path.name.split("_")[0]
        if not stem.isdigit():
            continue
        year = int(stem)
        with ArchiveContainer(path) as archive:
            for raw_member in archive.namelist():
                if not raw_member.lower().endswith(DOC_SUFFIXES):
                    continue
                blob = archive.read(raw_member)
                member = repair_name(raw_member) if archive.kind == "zip" else raw_member
                found.append(
                    Legend(
                        year=year,
                        member=member,
                        digest=hashlib.sha256(blob).hexdigest()[:12],
                        text=decode_legend(raw_member, blob),
                    )
                )
    return found


def legend_block(text: str) -> list[str]:
    """The lines of the 資料註記說明 section, verbatim apart from stripping.

    Definition lines are the ones that say what a token means, so the section
    ends at the first non-blank line that does not — in practice the units
    table that follows it.
    """
    lines = text.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if LEGEND_HEADING in line) + 1
    except StopIteration:
        return []

    block: list[str] = []
    for line in lines[start:]:
        stripped = line.strip()
        if not stripped:
            continue
        if NUMBERED_HEADING.match(stripped) or not DEFINITION.search(stripped):
            break
        block.append(re.sub(r"\s+", " ", stripped))
    return block


def tokens(block: list[str]) -> list[str]:
    """Every token the block defines, in the order it defines them."""
    seen: list[str] = []
    for line in block:
        for match in DEFINITION.finditer(line):
            token = match.group("token")
            if token not in seen:
                seen.append(token)
    return seen


def by_content(legends: list[Legend]) -> dict[str, list[Legend]]:
    grouped: dict[str, list[Legend]] = defaultdict(list)
    for legend in legends:
        grouped[legend.digest].append(legend)
    return grouped


def years_of(group: list[Legend]) -> list[int]:
    return sorted({legend.year for legend in group})


def span(years: list[int]) -> str:
    if not years:
        return "—"
    if len(years) == 1:
        return str(years[0])
    runs: list[tuple[int, int]] = []
    start = previous = years[0]
    for year in years[1:]:
        if year == previous + 1:
            previous = year
            continue
        runs.append((start, previous))
        start = previous = year
    runs.append((start, previous))
    return ", ".join(str(a) if a == b else f"{a}–{b}" for a, b in runs)


def report(legends: list[Legend]) -> None:
    grouped = by_content(legends)
    per_year: dict[int, list[Legend]] = defaultdict(list)
    for legend in legends:
        per_year[legend.year].append(legend)

    all_years = sorted(
        {int(p.name.split("_")[0]) for p in RAW.glob("*.zip") if p.name[0].isdigit()}
    )

    parsed = [legend for legend in legends if legend.text is not None]
    wordings = {tuple(legend_block(legend.text or "")) for legend in parsed}
    token_sets = {tuple(tokens(list(block))) for block in wordings}

    print("### 掃描結果")
    print()
    print(f"- 掃描資料包：**{len(all_years)}**（{span(all_years)}）")
    print(f"- 附說明文件者：**{len(per_year)}**；未附者：**{len(all_years) - len(per_year)}**")
    print(f"- 說明文件總份數：**{len(legends)}**，其中可解析文字者 **{len(parsed)}**")
    print(f"- 以內容雜湊計，不同的文件：**{len(grouped)}**")
    print(f"- 不同的旗標段落逐字內容：**{len(wordings)}**")
    print(f"- 不同的旗標 token 集合：**{len(token_sets)}**")
    print()

    print("### 每個資料包內的說明文件")
    print()
    print("| 年 | 說明文件份數 | 不同內容 | 檔名 | 記載的旗標 |")
    print("|---|---|---|---|---|")
    for year in all_years:
        group = per_year.get(year, [])
        if not group:
            print(f"| {year} | 0 | — | — | **未附說明文件** |")
            continue
        digests = sorted({legend.digest for legend in group})
        names = sorted({legend.filename for legend in group})
        found: list[str] = []
        for digest in digests:
            sample = grouped[digest][0]
            if sample.text is None:
                continue
            for token in tokens(legend_block(sample.text)):
                if token not in found:
                    found.append(token)
        shown = ", ".join(f"`{t}`" for t in found) if found else "（二進位格式，未解析）"
        print(f"| {year} | {len(group)} | {len(digests)} | {'<br>'.join(names)} | {shown} |")

    print()
    print("### 不同內容的說明文件（以內容雜湊識別，不看檔名）")
    print()
    print("| 雜湊 | 檔名 | 涵蓋年份 | 份數 | 記載的旗標 |")
    print("|---|---|---|---|---|")
    for digest, group in sorted(grouped.items(), key=lambda kv: min(years_of(kv[1]))):
        names = sorted({legend.filename for legend in group})
        sample = group[0]
        found = tokens(legend_block(sample.text)) if sample.text is not None else []
        shown = ", ".join(f"`{t}`" for t in found) if found else "（二進位格式，未解析）"
        print(
            f"| `{digest}` | {'<br>'.join(names)} | {span(years_of(group))} "
            f"| {len(group)} | {shown} |"
        )

    print()
    print("### 同一個檔名，不同內容")
    print()
    name_digests: dict[str, set[str]] = defaultdict(set)
    for legend in legends:
        name_digests[legend.filename].add(legend.digest)
    collisions = {name: ds for name, ds in name_digests.items() if len(ds) > 1}
    if not collisions:
        print("（無）")
    else:
        print("| 檔名 | 不同內容數 | 雜湊 |")
        print("|---|---|---|")
        for name, variants in sorted(collisions.items()):
            listed = ", ".join(f"`{d}`" for d in sorted(variants))
            print(f"| {name} | {len(variants)} | {listed} |")
        print()
        print("同名編輯之間的實際差異（以份數最多者為基準）：")
        print()
        for _name, variants in sorted(collisions.items()):
            # Total order, not just "most copies first": the two 2009 editions
            # have eight copies each, and a tie broken by set iteration order
            # made this report come out differently on consecutive runs — which
            # is worthless for something that gets pasted into a document.
            ordered = sorted(variants, key=lambda d: (-len(grouped[d]), years_of(grouped[d])[0], d))
            base = grouped[ordered[0]][0]
            if base.text is None:
                continue
            for digest in ordered[1:]:
                other = grouped[digest][0]
                if other.text is None:
                    continue
                changed = [
                    line
                    for line in difflib.unified_diff(
                        base.text.splitlines(), other.text.splitlines(), lineterm="", n=0
                    )
                    if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
                ]
                print(
                    f"- `{ordered[0]}`（{span(years_of(grouped[ordered[0]]))}）"
                    f" → `{digest}`（{span(years_of(grouped[digest]))}）"
                )
                for line in changed:
                    print(f"  - `{line}`")
        print()

    print()
    print("### 旗標段落逐字")
    print()
    blocks: dict[tuple[str, ...], list[str]] = defaultdict(list)
    for digest, group in grouped.items():
        sample = group[0]
        if sample.text is None:
            continue
        blocks[tuple(legend_block(sample.text))].append(digest)
    for block, digests in sorted(
        blocks.items(),
        key=lambda kv: min(min(years_of(grouped[d])) for d in kv[1]),
    ):
        covered = sorted({y for d in digests for y in years_of(grouped[d])})
        print(f"**{span(covered)}**（{len(digests)} 種內容共用這一段）")
        print()
        print("```")
        for line in block:
            print(line)
        print("```")
        print()


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        cast(Any, sys.stdout).reconfigure(encoding="utf-8")
    if not RAW.is_dir():
        raise SystemExit(
            f"{RAW} not found — run from the repository root with the archives present"
        )
    report(collect(RAW))


if __name__ == "__main__":
    main()
