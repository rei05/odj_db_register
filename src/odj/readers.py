"""セトリファイル1つ -> 正準フィールドの辞書リスト。

形式ごとに読み方が違うので、ここで吸収して build 側には均一なレコードを渡す。
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import normalize as nz
from . import xlsx
from .paths import MANUAL_DIR


@dataclass
class ReadResult:
    records: list[dict] = field(default_factory=list)
    source_kind: str = "unknown"
    detail: str = ""
    skipped_reason: str | None = None


def _records_from_table(rows: list[list[str]]) -> list[dict]:
    header_idx, mapping = nz.find_header(rows)
    if header_idx < 0:
        return []

    if nz.TRACK_NO not in mapping.values():
        title_col = next(c for c, f in mapping.items() if f == nz.TITLE)
        col = nz.find_track_no_column(
            rows[header_idx + 1 :], set(mapping), title_col
        )
        if col is not None:
            mapping[col] = nz.TRACK_NO

    out: list[dict] = []
    for row in rows[header_idx + 1 :]:
        rec: dict = {}
        for col, field_name in mapping.items():
            if col < len(row):
                rec[field_name] = nz.clean(row[col])
        if not rec.get(nz.TITLE):
            continue
        rec = nz.repair_urls(rec)
        rec[nz.TRACK_NO] = nz.to_int(rec.get(nz.TRACK_NO))
        rec[nz.PLAY_ORDER] = nz.to_int(rec.get(nz.PLAY_ORDER))
        rec[nz.BPM] = nz.to_float(rec.get(nz.BPM))
        rec[nz.YEAR] = nz.to_int(rec.get(nz.YEAR))
        rec[nz.IS_REMIX] = nz.to_remix(rec.get(nz.IS_REMIX))
        rec.pop(nz.EVENT_NO, None)  # 開催回はフォルダ名から決める
        rec.pop(nz.DJ, None)  # DJ もフォルダ名から決める
        out.append(rec)

    return _trim_and_renumber(out)


def _trim_and_renumber(records: list[dict]) -> list[dict]:
    """曲順の付いていない行を、位置を見て「曲」と「曲以外」に振り分ける。

    最後の番号付き行より後ろに続く無番号行は、セトリのテーマ名
    （"脱・京アニ偏重路線"）や候補曲の控え（第10回 tri は52曲ぶん並んでいる）で
    あって流した曲ではない。一方、番号付き行に挟まれた無番号行は、DJ が番号を
    振り忘れただけの実在の曲なので残す（第5回 ふっちー "Unwelcome School"）。
    """
    numbered = [i for i, r in enumerate(records) if r.get(nz.TRACK_NO) is not None]
    if not numbered:
        return records
    out = records[: numbered[-1] + 1]

    # 無番号行を挟んだぶん元の番号は飛ぶので、通し番号を振り直す
    if [r.get(nz.TRACK_NO) for r in out] != list(range(1, len(out) + 1)):
        for i, rec in enumerate(out, 1):
            rec[nz.TRACK_NO] = i
    return out


def _score(records: list[dict]) -> tuple[int, int, int, int]:
    """候補ファイル／タブの優劣。

    同じフォルダに「候補曲リスト」や「清書前の下書き」が同居していることが多く、
    単純な行数比べだと必ずそちらが勝ってしまう。実際に流したセトリは
    (1) 列が揃っていて (2) 曲順が 1..N と並ぶ ので、その順で見る。
    """
    if not records:
        return (0, 0, 0, 0)
    used_fields = {
        f
        for r in records
        for f in (nz.TRACK_NO, nz.SOURCE_WORK, nz.ARTIST, nz.URL, nz.IS_REMIX, nz.BPM)
        if r.get(f) is not None
    }
    filled = sum(
        1
        for r in records
        for f in (nz.SOURCE_WORK, nz.ARTIST, nz.URL, nz.IS_REMIX)
        if r.get(f) not in (None, "")
    )
    return (len(used_fields), round(nz.sequence_quality(records) * 10), filled, len(records))


def read_xlsx(path: Path) -> ReadResult:
    """複数タブある場合は、最もセトリらしいタブを採用する。"""
    best: tuple[tuple[int, ...], str, list[dict]] = ((0, 0, 0, 0), "", [])
    for name, rows in xlsx.read_sheets(path):
        recs = _records_from_table(rows)
        if _score(recs) > best[0]:
            best = (_score(recs), name, recs)
    return ReadResult(records=best[2], source_kind="xlsx", detail=f"sheet={best[1]}")


def read_manual_csv(path: Path) -> ReadResult:
    rows = list(csv.reader(io.StringIO(path.read_text(encoding="utf-8"))))
    return ReadResult(
        records=_records_from_table(rows), source_kind="manual", detail=path.name
    )


_NUMBERED_RE = re.compile(r"^\s*(\d{1,3})\s*[.．、]\s*(\S.*)$")
_WORK_RE = re.compile(r"[『「]([^』」]+)[』」]")
_URL_RE = re.compile(r"(https?://\S+)")


def read_text(path: Path) -> ReadResult:
    """自由形式のテキスト。2つの書き方を扱う。

    (a) 「N.曲名 / TVアニメ『作品』 OP / URL：...」の3行ブロック（第2回 マスオ）
    (b) 曲名だけを1行ずつ並べたもの（第1回 あちょ）
    """
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    lines = [ln.strip() for ln in text.splitlines()]

    structured = "『" in text or "URL：" in text or "http" in text
    if not structured:
        titles = [
            ln
            for ln in lines
            if ln and not ln.startswith("【") and not ln.startswith("―")
        ]
        bare = [
            {nz.TRACK_NO: i, nz.TITLE: nz.clean(t)} for i, t in enumerate(titles, 1)
        ]
        return ReadResult(records=bare, source_kind="txt", detail="bare-title-list")

    records: list[dict] = []
    current: dict | None = None
    for line in lines:
        if not line:
            continue
        m = _NUMBERED_RE.match(line)
        if m and (current is None or nz.TITLE in current):
            current = {nz.TRACK_NO: int(m.group(1)), nz.TITLE: nz.clean(m.group(2))}
            records.append(current)
            continue
        if current is None:
            continue
        url = _URL_RE.search(line)
        if url:
            current[nz.URL] = url.group(1)
            continue
        work = _WORK_RE.search(line)
        if work:
            current[nz.SOURCE_WORK] = nz.clean(work.group(1))
            current[nz.NOTE] = nz.clean(line)
    return ReadResult(records=records, source_kind="txt", detail="numbered-blocks")


def read(path: Path, file_id: str) -> ReadResult:
    """1ファイルを読む。手読み済み CSV があれば常にそちらを優先する。"""
    manual = MANUAL_DIR / f"{file_id}.csv"
    if manual.exists():
        return read_manual_csv(manual)

    suffix = path.suffix.lower()
    if suffix == ".xlsx":
        return read_xlsx(path)
    if suffix == ".txt":
        return read_text(path)
    if suffix == ".pdf":
        return ReadResult(
            source_kind="pdf",
            skipped_reason="PDF は ToUnicode を持たずテキスト抽出不可。"
            "data/manual/<file_id>.csv に手読み結果を置けば取り込まれる",
        )
    return ReadResult(source_kind=suffix or "?", skipped_reason=f"未対応の形式: {suffix}")
