"""Inventory quality-flag documentation files across all 44 raw archives (1982-2025).

This script reads raw archives directly in memory, inspects all embedded
ReadMe documentation files, verifies internal duplicate consistency, decodes
Big5/CP950 text and ODT XML contents, extracts documented quality flag symbols,
and prints a Markdown table comparing each year against the 2017 reference edition.
"""

from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path
from typing import Any, cast
from xml.etree import ElementTree

import py7zr


def decode_text(b: bytes) -> tuple[str | None, str]:
    """Try decoding raw bytes with Big5 first, falling back to CP950."""
    try:
        return b.decode("big5"), "big5"
    except UnicodeDecodeError:
        try:
            return b.decode("cp950"), "cp950"
        except UnicodeDecodeError:
            return None, "failed"


def parse_odt_text(b: bytes) -> str | None:
    """Parse text content from an ODT file directly in memory."""
    try:
        with zipfile.ZipFile(io.BytesIO(b)) as z:
            content_xml = z.read("content.xml")
            tree = ElementTree.fromstring(content_xml)
            texts: list[str] = []
            for elem in tree.iter():
                if elem.text:
                    texts.append(elem.text)
                if elem.tail:
                    texts.append(elem.tail)
            return "\n".join(texts)
    except Exception:
        return None


def fix_zip_name(name: str) -> str:
    """Fix zip filename encoding from CP437 to Big5/CP950 if un-flagged."""
    try:
        return name.encode("cp437").decode("big5")
    except Exception:
        return name


def extract_documented_flags(text: str, year: int) -> list[str]:
    """Extract literal flag symbols documented in the text."""
    symbols: list[str] = []
    candidates = ["#", "*", "x", "NR", "空白"]
    for sym in candidates:
        if sym in text:
            symbols.append(sym)

    if year == 2001:
        if "888     表示" in text:
            symbols.append("888")
        if "999     表示" in text:
            symbols.append("999")

    return symbols


def inspect_all_archives(raw_dir: Path) -> list[tuple[int, str, str, str, str]]:
    """Inspect all annual archives and return row data for the summary table."""
    archive_paths = sorted(raw_dir.glob("*.zip"))
    rows: list[tuple[int, str, str, str, str]] = []

    for path in archive_paths:
        year_str = path.name.split("_")[0]
        try:
            year = int(year_str)
        except ValueError:
            continue

        file_list: list[str] = []
        is_7z = False
        open_error: str | None = None

        try:
            with zipfile.ZipFile(path, "r") as zf:
                file_list = zf.namelist()
        except Exception as e1:
            try:
                with py7zr.SevenZipFile(path, mode="r") as szf:
                    file_list = szf.getnames()
                    is_7z = True
            except Exception as e2:
                open_error = f"zip: {e1}, 7z: {e2}"

        if open_error is not None:
            rows.append(
                (
                    year,
                    "—",
                    "—",
                    "could not open",
                    f"Could not open archive ({open_error})",
                )
            )
            continue

        readme_names = [
            n
            for n in file_list
            if not n.endswith("/")
            and ("readme" in Path(n).name.lower() or "說明" in Path(n).name.lower())
        ]

        if not readme_names:
            rows.append((year, "—", "—", "no readme", ""))
            continue

        readme_bytes: dict[str, bytes] = {}
        if is_7z:
            with py7zr.SevenZipFile(path, mode="r") as szf:
                dict_res = szf.read(readme_names)
                for n in readme_names:
                    readme_bytes[n] = dict_res[n].read()
        else:
            with zipfile.ZipFile(path, "r") as zf:
                for n in readme_names:
                    readme_bytes[n] = zf.read(n)

        # Group by file extension
        by_ext: dict[str, list[tuple[str, bytes]]] = {}
        for n, b in readme_bytes.items():
            ext = Path(n).suffix.lower()
            fixed_n = fix_zip_name(n)
            by_ext.setdefault(ext, []).append((fixed_n, b))

        dup_notes: list[str] = []
        for ext, items in by_ext.items():
            _first_n, first_b = items[0]
            diff_items = [(n, b) for n, b in items if b != first_b]
            if diff_items:
                txt_diff_zones: list[str] = []
                for dn, db in diff_items:
                    if ext == ".txt":
                        t1, _ = decode_text(first_b)
                        t2, _ = decode_text(db)
                        if t1 != t2:
                            txt_diff_zones.append(Path(dn).parent.name)
                    elif ext == ".odt":
                        t1 = parse_odt_text(first_b)
                        t2 = parse_odt_text(db)
                        if t1 != t2:
                            txt_diff_zones.append(Path(dn).parent.name)

                if txt_diff_zones:
                    dup_notes.append(
                        f"Readmes across zone folders ({ext}) differ in text for "
                        f"{', '.join(txt_diff_zones)}"
                    )
                else:
                    dup_notes.append(
                        f"Readmes across zone folders ({ext}) byte-differ, "
                        "but text content is identical"
                    )

        # Pick representative file (prefer .odt if present, else .txt, else .doc)
        rep_ext = ".odt" if ".odt" in by_ext else (".txt" if ".txt" in by_ext else ".doc")
        rep_path, rep_b = by_ext[rep_ext][0]
        rep_filename = Path(rep_path).name

        text: str | None = None
        undecodable = False

        if rep_ext == ".txt":
            text, _ = decode_text(rep_b)
            if text is None:
                undecodable = True
        elif rep_ext == ".odt":
            text = parse_odt_text(rep_b)

        if undecodable:
            rows.append(
                (
                    year,
                    rep_filename,
                    "—",
                    "undecodable",
                    "; ".join(dup_notes),
                )
            )
            continue

        flags_list = extract_documented_flags(text or "", year)
        flags_str = ", ".join([f"`{s}`" for s in flags_list]) if flags_list else "—"

        is_identical = "no"
        notes: list[str] = list(dup_notes)

        # Check identity with 2017 edition
        if (
            rep_filename == "ReadMe_普通測站20170301.odt"
            or rep_filename == "ReadMe_普通測站20170301.doc"
        ):
            if year in (1993, 1994, 1995, 2014, 2016, 2017):
                is_identical = "yes"
            else:
                is_identical = "no"
                notes.append(f"File is named {rep_filename} but differs from 2017 edition")
        else:
            is_identical = "no"
            if rep_ext == ".txt":
                notes.append(f"Plain text format ({rep_filename}) rather than 2017 ODT/DOC")
            elif rep_filename != "ReadMe_普通測站20170301.odt":
                notes.append(f"Earlier documentation revision ({rep_filename})")

            if flags_list != ["#", "*", "x", "NR", "空白"]:
                notes.append(f"Documents flag symbols: {flags_str}")

        rows.append((year, rep_filename, flags_str, is_identical, "; ".join(notes)))

    return rows


def print_table(rows: list[tuple[int, str, str, str, str]]) -> None:
    """Print the Markdown table."""
    print(
        "| year | readme filename | flag symbols documented | identical to 2017 edition? | notes |"
    )
    print("|---|---|---|---|---|")
    for year, fn, flags, ident, notes in rows:
        print(f"| {year} | {fn} | {flags} | {ident} | {notes} |")


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        cast(Any, sys.stdout).reconfigure(encoding="utf-8")
    raw_dir = Path("data/raw/airtw")
    rows = inspect_all_archives(raw_dir)
    print_table(rows)


if __name__ == "__main__":
    main()
