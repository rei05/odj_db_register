"""標準ライブラリだけで xlsx を読む。

openpyxl を入れないのは依存を増やさないためだが、そのぶん sharedStrings の
ふりがな（rPh）を自前で捨てる必要がある。これを怠ると
「雑踏、僕らの街」が「雑踏、僕らの街ザットウボクマチ」になる。
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_CELL_REF_RE = re.compile(r"([A-Z]+)(\d+)")
_SHEET_PATH_RE = re.compile(r"xl/worksheets/sheet\d+\.xml")


def _col_index(ref: str) -> int:
    """'A' -> 0, 'B' -> 1, ... 'AA' -> 26"""
    m = _CELL_REF_RE.match(ref)
    letters = m.group(1) if m else ref
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n - 1


def _si_text(si: ET.Element) -> str:
    """<si> のテキスト。ふりがな（<rPh>）と phonetic 設定（<phoneticPr>）は除く。

    <si> の子は「直下の <t>」か「<r> のリッチテキスト断片」のどちらか。
    <rPh> もまた <t> を持つので、iter() で全部拾うと本文にルビが混ざる。
    """
    parts: list[str] = []
    for child in si:
        if child.tag == f"{_NS}t":
            parts.append(child.text or "")
        elif child.tag == f"{_NS}r":
            for t in child.findall(f"{_NS}t"):
                parts.append(t.text or "")
    return "".join(parts)


def _shared_strings(zf: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in zf.namelist():
        return []
    root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
    return [_si_text(si) for si in root.findall(f"{_NS}si")]


def _cell_text(cell: ET.Element, shared: list[str]) -> str:
    kind = cell.get("t")
    if kind == "inlineStr":
        is_el = cell.find(f"{_NS}is")
        return _si_text(is_el) if is_el is not None else ""

    v = cell.find(f"{_NS}v")
    if v is None or v.text is None:
        return ""
    if kind == "s":
        idx = int(v.text)
        return shared[idx] if idx < len(shared) else ""
    if kind == "e":
        return ""  # #REF! などのエラー値は欠損扱い
    return v.text


def sheet_names(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as zf:
        wb = ET.fromstring(zf.read("xl/workbook.xml"))
        return [s.get("name") or "" for s in wb.iter(f"{_NS}sheet")]


def read_sheets(path: Path) -> list[tuple[str, list[list[str]]]]:
    """(シート名, 行列) のリスト。行内の欠けたセルは空文字で埋める。"""
    with zipfile.ZipFile(path) as zf:
        shared = _shared_strings(zf)
        names = sheet_names(path)
        paths = sorted(
            (n for n in zf.namelist() if _SHEET_PATH_RE.fullmatch(n)),
            key=lambda n: int(re.search(r"(\d+)", n).group(1)),
        )
        out: list[tuple[str, list[list[str]]]] = []
        for i, sheet_path in enumerate(paths):
            root = ET.fromstring(zf.read(sheet_path))
            rows: list[list[str]] = []
            for row_el in root.iter(f"{_NS}row"):
                row: list[str] = []
                for cell in row_el.findall(f"{_NS}c"):
                    ref = cell.get("r")
                    if ref:
                        idx = _col_index(ref)
                        while len(row) < idx:
                            row.append("")
                    row.append(_cell_text(cell, shared))
                rows.append(row)
            out.append((names[i] if i < len(names) else f"sheet{i + 1}", rows))
        return out
