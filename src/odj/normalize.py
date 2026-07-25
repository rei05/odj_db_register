"""バラバラな列構成を正準スキーマへ寄せる。

DJ ごとにテンプレートの改造具合が違うので、ヘッダ名の同義語辞書＋値の形に
よる救済（URL っぽい文字列は URL 列へ）で吸収する。
"""

from __future__ import annotations

import re
import unicodedata

# 正準フィールド
TRACK_NO = "track_no"
TITLE = "title"
SOURCE_WORK = "source_work"
ARTIST = "artist"
IS_REMIX = "is_remix"
URL = "url"
PLAY_ORDER = "play_order"
EVENT_NO = "event_no"
BPM = "bpm"
YEAR = "year"
NOTE = "note"
DJ = "dj"

_EXACT_HEADERS: dict[str, str] = {
    "曲順": TRACK_NO, "no": TRACK_NO, "no.": TRACK_NO, "列1": TRACK_NO,
    "#": TRACK_NO, "＃": TRACK_NO, "番号": TRACK_NO,
    "タイトル": TITLE, "曲名": TITLE, "曲": TITLE, "トラックタイトル": TITLE,
    "曲名(原曲)": TITLE, "title": TITLE, "song": TITLE,
    "アニメ/元ネタ": SOURCE_WORK, "アニメ": SOURCE_WORK, "元ネタ": SOURCE_WORK,
    "原作": SOURCE_WORK, "作品": SOURCE_WORK,
    "アーティスト": ARTIST, "アーティスト(音源)": ARTIST, "artist": ARTIST,
    "artists": ARTIST, "歌手": ARTIST,
    "remix": IS_REMIX,
    "url": URL, "動画": URL, "リンク": URL, "link": URL,
    "play順": PLAY_ORDER, "順番": PLAY_ORDER,
    "開催": EVENT_NO, "回": EVENT_NO,
    "bpm": BPM,
    "年": YEAR, "年代": YEAR,
    "メモ": NOTE, "備考": NOTE, "特記事項": NOTE, "オタクコメ": NOTE,
    "dj": DJ,
}

# 部分一致で拾う（「タイトル（既出は太字）」のような注記付きヘッダ用）
_PARTIAL_HEADERS: list[tuple[str, str]] = [
    ("タイトル", TITLE), ("曲名", TITLE),
    ("アニメ", SOURCE_WORK), ("元ネタ", SOURCE_WORK),
    ("アーティスト", ARTIST),
    ("remix", IS_REMIX),
    ("url", URL),
]

# tri 氏のファイルはテンプレートを使い回すため1列目のヘッダが「第9回」等の
# 開催回表記のまま残っている。中身は曲順。
_EVENT_LABEL_RE = re.compile(r"^第\d+回$")

_NULLISH = {"", "-", "‐", "―", "ー", "#ref!", "#n/a", "n/a", "なし", "未定"}
_REMIX_TRUE = {"○", "◯", "〇", "o", "◎", "●", "yes", "y", "有", "あり", "remix", "true", "1"}
_REMIX_FALSE = {"原曲", "×", "✕", "x", "no", "n", "無", "なし", "false", "0"}


def norm_header(text: str) -> str:
    s = unicodedata.normalize("NFKC", text or "").strip().lower()
    return re.sub(r"[\s　]+", "", s)


def header_field(text: str) -> str | None:
    key = norm_header(text)
    if not key:
        return None
    if key in _EXACT_HEADERS:
        return _EXACT_HEADERS[key]
    if _EVENT_LABEL_RE.match(key):
        return TRACK_NO
    for needle, field in _PARTIAL_HEADERS:
        if needle in key:
            return field
    return None


def clean(value: str | None) -> str | None:
    """前後の空白（全角含む）を落とし、欠損を表す値は None にする。"""
    if value is None:
        return None
    s = unicodedata.normalize("NFKC", str(value)).strip()
    s = s.strip("　 \t\r\n")
    if s.lower() in _NULLISH:
        return None
    return s or None


def to_int(value: str | None) -> int | None:
    s = clean(value)
    if s is None:
        return None
    # xlsx の数値セルは "1" ではなく "1.0" で入ってくることがある
    m = re.match(r"^-?\d+(?:\.\d+)?$", s)
    if not m:
        m2 = re.search(r"\d+", s)  # "1." や "01_" のような表記
        return int(m2.group()) if m2 else None
    return int(float(s))


def to_float(value: str | None) -> float | None:
    s = clean(value)
    if s is None:
        return None
    m = re.search(r"\d+(?:\.\d+)?", s)
    return float(m.group()) if m else None


def is_url(value: str | None) -> bool:
    s = clean(value)
    return bool(s and s.lower().startswith(("http://", "https://")))


def to_remix(value: str | None) -> bool | None:
    s = clean(value)
    if s is None:
        return None
    key = s.lower()
    if key in _REMIX_TRUE:
        return True
    if key in _REMIX_FALSE:
        return False
    # せーや氏のシートは REMIX 列に「リミックス音源の URL」を入れている
    if is_url(s):
        return True
    return None


def find_header(rows: list[list[str]], scan: int = 12) -> tuple[int, dict[int, str]]:
    """ヘッダ行の位置と 列index -> 正準フィールド の対応を返す。

    ヘッダは必ずしも1行目ではない（第1回 ha の xlsx は2行目）。先頭 scan 行の
    うち、正準フィールドを最も多く認識できた行をヘッダとみなす。
    """
    best_idx, best_map = -1, {}
    for i, row in enumerate(rows[:scan]):
        mapping = {}
        for j, cell in enumerate(row):
            field = header_field(cell)
            if field is not None and field not in mapping.values():
                mapping[j] = field
        # タイトル列が取れない行はヘッダとして採用しない
        if TITLE in mapping.values() and len(mapping) > len(best_map):
            best_idx, best_map = i, mapping
    return best_idx, best_map


def find_track_no_column(
    rows: list[list[str]], used: set[int], title_col: int
) -> int | None:
    """曲順のヘッダが無いシート向けに、1,2,3... が並ぶ列を探す。

    あぴす氏や既存マスターDBのように、曲順の列にヘッダが無いファイルがある。
    """
    width = max((len(r) for r in rows), default=0)
    for col in range(min(width, 6)):
        if col in used or col == title_col:
            continue
        seq = [to_int(r[col]) for r in rows if col < len(r) and clean(r[col])]
        if len(seq) >= 5 and seq == list(range(1, len(seq) + 1)):
            return col
    return None


def sequence_quality(records: list[dict]) -> float:
    """曲順が 1..N と素直に並んでいる割合。本物のセトリを見分ける手掛かり。"""
    if len(records) < 3:
        return 0.0
    hit = sum(1 for i, r in enumerate(records, 1) if r.get(TRACK_NO) == i)
    return hit / len(records)


def repair_urls(record: dict) -> dict:
    """URL 列がずれているファイルを値の形で救済する。

    第15回 せーや のシートはヘッダと中身が1列ずれており、URL 列には元ネタの
    再掲が、REMIX 列には音源 URL が入っている。
    """
    if not is_url(record.get(URL)):
        record[URL] = None
        for field in (IS_REMIX, NOTE, ARTIST, SOURCE_WORK):
            if is_url(record.get(field)):
                record[URL] = clean(record[field])
                if field != IS_REMIX:
                    record[field] = None
                break
    return record
