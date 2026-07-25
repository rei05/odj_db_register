"""xlsx リーダの、実データで踏んだ罠に対する回帰テスト。

    python3 -m unittest discover -s tests

いずれも「読めない」のではなく「静かに値が化ける」種類の不具合なので、
気付きにくい。
"""

from __future__ import annotations

import sys
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from odj import xlsx  # noqa: E402

_WB = """<?xml version="1.0"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheets><sheet name="セトリ" sheetId="1" r:id="rId1"
    xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"/></sheets>
</workbook>"""

# 2番目の <si> は Excel のふりがな付きセル。<rPh> の中身は本文ではない。
_SHARED = """<?xml version="1.0"?>
<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="3" uniqueCount="3">
  <si><t>タイトル</t></si>
  <si><t>雑踏、僕らの街</t><rPh sb="0" eb="2"><t>ザットウ</t></rPh>
      <rPh sb="3" eb="5"><t>ボクマチ</t></rPh><phoneticPr fontId="1"/></si>
  <si><r><t>Great</t></r><r><t> Days</t></r></si>
</sst>"""

_SHEET = """<?xml version="1.0"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
 <sheetData>
  <row r="1">
    <c r="A1" t="s"><v>0</v></c>
    <c r="B1" t="s"><v>1</v></c>
    <c r="C1" t="s"><v>2</v></c>
    <c r="D1" t="b"><v>1</v></c>
    <c r="E1" t="b"><v>0</v></c>
    <c r="F1"><v>14.0</v></c>
    <c r="G1"><v>121.60</v></c>
    <c r="H1" t="e"><v>#REF!</v></c>
    <c r="J1" t="inlineStr"><is><t>末尾</t></is></c>
  </row>
 </sheetData>
</worksheet>"""


def _write_xlsx(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("xl/workbook.xml", _WB)
        zf.writestr("xl/sharedStrings.xml", _SHARED)
        zf.writestr("xl/worksheets/sheet1.xml", _SHEET)


class XlsxReaderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = TemporaryDirectory()
        path = Path(cls._tmp.name) / "sample.xlsx"
        _write_xlsx(path)
        (cls.name, cls.row), = [(n, r[0]) for n, r in xlsx.read_sheets(path)]

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_sheet_name(self) -> None:
        self.assertEqual(self.name, "セトリ")

    def test_furigana_is_not_part_of_the_text(self) -> None:
        # rPh を拾うと「雑踏、僕らの街ザットウボクマチ」になる
        self.assertEqual(self.row[1], "雑踏、僕らの街")

    def test_rich_text_runs_are_joined(self) -> None:
        self.assertEqual(self.row[2], "Great Days")

    def test_boolean_keeps_its_word(self) -> None:
        # スプレッドシートが文字列 TRUE を真偽値にしてしまうため、
        # 1 のまま読むとアーティスト名 TRUE が数字の 1 になる
        self.assertEqual(self.row[3], "TRUE")
        self.assertEqual(self.row[4], "FALSE")

    def test_integral_numbers_lose_the_decimal_point(self) -> None:
        # Google スプレッドシートの書き出しは 14 を "14.0" と書く。
        # アーティスト名が "14" の曲が実在する。
        self.assertEqual(self.row[5], "14")

    def test_fractional_numbers_are_left_alone(self) -> None:
        self.assertEqual(self.row[6], "121.60")

    def test_error_cells_are_blank(self) -> None:
        self.assertEqual(self.row[7], "")

    def test_gap_between_cells_is_filled(self) -> None:
        # I 列が無いので、J 列がずれずに 9 番目に来ること
        self.assertEqual(len(self.row), 10)
        self.assertEqual(self.row[8], "")
        self.assertEqual(self.row[9], "末尾")


if __name__ == "__main__":
    unittest.main()
