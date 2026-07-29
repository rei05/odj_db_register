"""名寄せ辞書（data/aliases/*.toml と decisions.jsonl）の読み書き。

    works.toml      [[work]]    元ネタ名の同値クラス
    artists.toml    [[artist]]  アーティスト名の同値クラス
    keep_apart.toml [[pair]]    統合してはいけない組
    decisions.jsonl             1件1行の判断ログ

**`approved = true` を書くのは人間の判断（odj.aliases decide）だけ**で、
`export_json()` は approved なものしか公開データ（web/public/data/aliases.json）に
出さない。この2つが守られている限り、誤った統合が公開サイトに出ることは
構造的に起こらない。LLM の提案は `_proposed/` に置き、ここには入らない。

TOML の書き出しを自前で持っているのは、tomllib が読み取り専用で、標準ライブラリに
ライターが無く、そして依存を増やせないため。**書き出しは追記だけ**にしてある。
ファイル全体を再シリアライズすると、人が手で書いた reason の改行や節ごとの
コメント（keep_apart.toml がそうなっている）が毎回崩れるからで、ここは
「機械が足す・人が直す」の両方が起きるファイルである。
"""

from __future__ import annotations

import json
import tomllib
from datetime import date, datetime
from pathlib import Path
from typing import Any

from odj import paths


class AliasError(Exception):
    """人間に見せて直してもらう種類の失敗。CLI が JSON のエラーに変換する。

    code は呼び出し側（レビュー GUI）が**文面ではなく種別で**分岐するためのもの。
    以前は文面から 400/409 を振り分けていて、「既に判断済み」と
    「keep_apart で別物と決めた組が含まれる」がどちらも 409 になり、
    GUI が後者まで「二重送信」と解釈してキューを取り直していた。
    その結果、キー操作しても同じカードが出続けて何も起きないように見えた。

      already-decided … 同じ id を二度判断した。キューを取り直せばよい
      keep-apart      … 人間が別物と決めた組を統合しようとした。中身を直す必要がある
      conflict        … 同じ表記が別の正準名にも寄っている。既存の項目を先に直す
      invalid         … 入力そのものが不正（理由が空、canonical の創作など）
    """

    def __init__(self, message: str, code: str = "invalid") -> None:
        super().__init__(message)
        self.code = code


# フィールド名 → (ファイル名, TOML の配列テーブル名)
FIELDS = {"work": ("works.toml", "work"), "artist": ("artists.toml", "artist")}

# export に出す信頼度。low は「候補としては残すが公開はしない」。
EXPORTED_CONFIDENCE = {"high", "medium"}

# 書き出すときのキーの順。ここに無いキーは後ろに名前順で並ぶ。
# reason を最後にしているのは、複数行になって長いため。
_KEY_ORDER = (
    "id", "canonical", "series", "kind", "variants",
    "approved", "confidence", "source", "where", "decided_at", "reason",
)

_WORKS_HEADER = """\
# 元ネタ名の同値クラス。表記ゆれを1つの正準表記に寄せた結果。
#
# odj.aliases decide が末尾に追記する（人間が GUI で1件ずつ判断した結果）。
# 追記しかしないので、手で直した整形やコメントは残る。
#
# canonical  … 検索と表示に使う正準表記。variants か提案の中にある文字列しか
#              置かない（実データのどこにも無い名前を創作しないため）
# variants   … この正準表記に寄せる生表記。plays.json にそのまま現れる文字列
# approved   … 人間が確認したか。**true を書くのは人間の判断だけ**
# confidence … high / medium / low。export に出るのは high と medium だけ
# reason     … なぜ同じと判断したか。実データの根拠を書く
"""

_ARTISTS_HEADER = _WORKS_HEADER.replace("元ネタ名", "アーティスト名")

_KEEP_APART_HEADER = """\
# 絶対に統合しないペア。
#
# odj.aliases decide の action=keep-apart が末尾に追記する。
"""


# ---------------------------------------------------------------------------
# TOML の書き出し（値だけ。文字列以外はエスケープが要らない）
# ---------------------------------------------------------------------------

_SHORT_ESCAPES = {
    "\\": "\\\\", '"': '\\"', "\b": "\\b", "\f": "\\f",
    "\n": "\\n", "\r": "\\r", "\t": "\\t",
}


def _needs_unicode_escape(ch: str) -> bool:
    return ord(ch) < 0x20 or ord(ch) == 0x7F


def _basic_string(text: str) -> str:
    """1行の基本文字列。制御文字は全部エスケープする。"""
    out = []
    for ch in text:
        if ch in _SHORT_ESCAPES:
            out.append(_SHORT_ESCAPES[ch])
        elif _needs_unicode_escape(ch):
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _multiline_string(text: str) -> str:
    '''複数行の基本文字列。reason は複数行で書かれるのでこちらを使う。

    引用符は「次も引用符」か「末尾」のときだけエスケープする。こうすると
    `"""` が本文中にも終端の直前にも現れなくなり、なおかつ普通の引用符
    （`"START DASH SENSATION"` のような実データの引用）はそのまま読める。

    改行とタブはそのまま置ける。`\\r` は単独では置けない規則なのでエスケープする
    （ブラウザの textarea から来る値が CRLF のことがある。往復で消えては困る）。
    '''
    out = []
    for i, ch in enumerate(text):
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            last = i == len(text) - 1
            out.append('\\"' if last or text[i + 1] == '"' else '"')
        elif ch in "\n\t":
            out.append(ch)
        elif ch == "\r":
            out.append("\\r")
        elif _needs_unicode_escape(ch):
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    return '"""' + "".join(out) + '"""'


def _fmt_string(text: str) -> str:
    return _multiline_string(text) if "\n" in text else _basic_string(text)


def _fmt_value(value: Any) -> str:
    if isinstance(value, bool):  # bool は int の派生なので先に見る
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return _fmt_string(value)
    if isinstance(value, (list, tuple)):
        items = [_fmt_value(v) for v in value]
        one_line = "[" + ", ".join(items) + "]"
        if len(one_line) <= 88:
            return one_line
        # 日本語の作品名が並ぶと1行に収まらない。縦に並べたほうが人が直しやすい。
        return "[\n" + "".join(f"    {it},\n" for it in items) + "]"
    raise AliasError(f"TOML に書けない型です: {type(value).__name__}")


def _fmt_block(table: str, entry: dict[str, Any]) -> str:
    keys = [k for k in _KEY_ORDER if k in entry]
    keys += sorted(k for k in entry if k not in _KEY_ORDER)
    lines = [f"[[{table}]]"]
    for key in keys:
        value = entry[key]
        if value is None or value == [] or value == "":
            continue  # 空の項目は書かない。読む側は「無い」と同じに扱う
        lines.append(f"{key} = {_fmt_value(value)}")
    return "\n".join(lines) + "\n"


def _append_block(path: Path, header: str, block: str) -> Path:
    """ファイル末尾にブロックを1つ足す。無ければ見出しのコメント付きで作る。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        current = path.read_text(encoding="utf-8")
        prefix = "" if current.endswith("\n") else "\n"
        prefix += "" if current.endswith("\n\n") or not current.strip() else "\n"
    else:
        current, prefix = header, "\n"
    path.write_text(current + prefix + block, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# works.toml / artists.toml
# ---------------------------------------------------------------------------


def entries_path(field: str) -> Path:
    """field の辞書ファイル。paths を実行時に見るのでテストで差し替えられる。"""
    if field not in FIELDS:
        raise AliasError(f"field は work か artist です: {field!r}")
    name, _ = FIELDS[field]
    return paths.ALIASES_DIR / name


def load_entries(field: str, *, path: Path | None = None) -> list[dict]:
    """既存の同値クラスを読む。ファイルが無ければ空。"""
    target = path or entries_path(field)  # 不正な field はここで弾かれる
    _, table = FIELDS[field]
    if not target.exists():
        return []
    with target.open("rb") as fh:
        data = tomllib.load(fh)
    return list(data.get(table, []))


def append_entry(field: str, entry: dict[str, Any], *, path: Path | None = None) -> Path:
    """[[work]] / [[artist]] を1つ追記する。

    ここは書くだけで、canonical の妥当性や keep_apart との衝突は呼ぶ側
    （cli.decide）が見る。**approved を true にしてよいのは人間の判断だけ**。
    """
    if field not in FIELDS:
        raise AliasError(f"field は work か artist です: {field!r}")
    _, table = FIELDS[field]
    header = _WORKS_HEADER if field == "work" else _ARTISTS_HEADER
    return _append_block(path or entries_path(field), header, _fmt_block(table, entry))


def variant_index(entries: list[dict]) -> dict[str, str]:
    """生表記 → 正準表記。同じ表記が複数のクラスに出たら先に書かれたほうが勝つ。

    重複は「別の正準名に寄せる指示が2つある」ということなので、決着させずに
    先勝ちにしておき、検出は呼ぶ側に任せる（decide は弾き、export は警告する）。
    """
    index: dict[str, str] = {}
    for entry in entries:
        canonical = (entry.get("canonical") or "").strip()
        if not canonical:
            continue
        for raw in class_values(entry):
            index.setdefault(raw, canonical)
    return index


def class_values(entry: dict) -> list[str]:
    """同値クラスに属する生表記。canonical が variants に無ければ足す。

    canonical は提案（_proposed）から採ることがあり、その場合 plays.json には
    現れない正式名称になる。検索では正式名称でも引きたいので同値クラスに入れる。
    """
    seen: list[str] = []
    for raw in list(entry.get("variants") or []) + [entry.get("canonical") or ""]:
        raw = (raw or "").strip()
        if raw and raw not in seen:
            seen.append(raw)
    return seen


# ---------------------------------------------------------------------------
# keep_apart.toml
# ---------------------------------------------------------------------------


def load_keep_apart_pairs(*, path: Path | None = None) -> list[dict]:
    """keep_apart.toml のペアを書かれたまま読む。

    block.load_keep_apart() とは別物で、あちらは**注記を剥がしたキーの組まで
    膨らませた集合**を返す（迂回路を塞ぐため）。こちらは重複追記を避けるために
    生の組をそのまま見たいので、膨らませない。
    """
    target = path or paths.KEEP_APART_PATH
    if not target.exists():
        return []
    with target.open("rb") as fh:
        data = tomllib.load(fh)
    return list(data.get("pair", []))


def append_keep_apart(
    pairs: list[dict],
    reason: str,
    where: str | None = None,
    *,
    path: Path | None = None,
) -> tuple[Path, int]:
    """[[pair]] を追記する。既にあるペアは飛ばす。

    返すのは (書いたファイル, 実際に足した本数)。0 本でもファイルは返す
    （呼ぶ側が「何も足さなかった」と「失敗」を区別できるように）。
    """
    target = path or paths.KEEP_APART_PATH
    known = {
        frozenset((p.get("a", ""), p.get("b", "")))
        for p in load_keep_apart_pairs(path=target)
    }
    added = 0
    for pair in pairs:
        a, b = (pair.get("a") or "").strip(), (pair.get("b") or "").strip()
        if frozenset((a, b)) in known:
            continue
        known.add(frozenset((a, b)))
        entry: dict[str, Any] = {"a": a, "b": b}
        if where:
            entry["where"] = where
        entry["reason"] = reason
        _append_block(target, _KEEP_APART_HEADER, _fmt_block("pair", entry))
        added += 1
    return target, added


# ---------------------------------------------------------------------------
# decisions.jsonl
# ---------------------------------------------------------------------------


def load_decisions(*, path: Path | None = None) -> list[dict]:
    """判断ログを読む。壊れた行は飛ばす（block.load_decided と同じ扱い）。"""
    target = path or paths.DECISIONS_PATH
    if not target.exists():
        return []
    out: list[dict] = []
    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def append_decision(record: dict, *, path: Path | None = None) -> Path:
    """判断を1行足す。**辞書に何を書いたかに関わらず、ここには必ず書く。**"""
    target = path or paths.DECISIONS_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    return target


def now_stamp() -> str:
    """decisions.jsonl の at。ローカルの時差付きで秒まで。"""
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


# ---------------------------------------------------------------------------
# LLM の提案（odj.aliases ask が書き、人間の decide が読む）
# ---------------------------------------------------------------------------

_PROPOSED_HEADER = """\
# LLM が生成した統合候補（未承認）。
# このディレクトリのファイルは提案であって辞書ではない。
# 人間が npm run review で1件ずつ判断し、承認したものだけが
# data/aliases/ 直下の辞書に移る。approved は LLM も自動処理も書かない。
"""


def proposals_path(field: str) -> Path:
    if field not in FIELDS:
        raise AliasError(f"field は work か artist です: {field!r}")
    name, _ = FIELDS[field]
    return paths.ALIASES_DIR / "_proposed" / name


def load_proposals(field: str, *, path: Path | None = None) -> dict[str, dict]:
    """data/aliases/_proposed/{works,artists}.toml をクラスタ id で引ける形に。

    提案は **approved を持たない**（人間が decide して初めて works.toml に入る）。
    ここを読むのは「canonical が提案の中にあるか」を確かめるためだけ。
    """
    target = path or proposals_path(field)  # 不正な field はここで弾かれる
    _, table = FIELDS[field]
    if not target.exists():
        return {}
    with target.open("rb") as fh:
        data = tomllib.load(fh)
    out: dict[str, dict] = {}
    for entry in data.get(table, []):
        cluster_id = (entry.get("id") or "").strip()
        if cluster_id:
            out.setdefault(cluster_id, entry)
    return out


def write_proposals(
    field: str, entries: list[dict[str, Any]], *, path: Path | None = None
) -> Path:
    """_proposed/{works,artists}.toml を丸ごと書き直す。

    works.toml と違って**追記ではなく上書き**にしてある。提案は odj.aliases ask が
    毎回作り直すもので、人が手で育てるファイルではないため（人が直すのは
    npm run review を通ったあとの works.toml のほう）。追記にすると同じクラスタの
    提案が実行のたびに積み上がり、レビュー側でどれが最新か分からなくなる。

    **approved を含む提案は書けない。** LLM 側のスキーマにも入れていないので
    普通は起こらないが、「未承認のものが公開データに出ない」の担保はここでも持つ。
    """
    target = path or proposals_path(field)
    _, table = FIELDS[field]
    for entry in entries:
        if "approved" in entry:
            raise AliasError(
                "提案に approved は書けません（承認は人間の decide だけ）: "
                f"{entry.get('id') or entry.get('canonical')}"
            )
    body = "".join("\n" + _fmt_block(table, entry) for entry in entries)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_PROPOSED_HEADER + body, encoding="utf-8")
    return target


# ---------------------------------------------------------------------------
# 公開データの書き出し
# ---------------------------------------------------------------------------


def _class_map(field: str, warn: list[str]) -> tuple[dict[str, dict], int]:
    """辞書 → {生表記: {c,s,k,v}}。approved で high/medium のものだけ。

    キーは **normKey を通す前の生の文字列**。web/src/lib/normalize.ts の
    normKey() を Python に移植すると必ず片方だけが直されて乖離するので、
    正規化は読み込んだ web 側が一手にやる。
    """
    # 同じ正準名のエントリは1つの同値クラスにまとめる。
    # 新しい開催回で「ラブライブ！」（全角）が現れたとき、既に判断済みの
    # 「ラブライブ!」に足す形で追記される。エントリごとに v を作ると
    # 「ラブライブ！」の同値クラスが ["ラブライブ！","ラブライブ!"] だけになり、
    # 「ラブライブ! 楽曲」と検索で繋がらなくなる。**追記のたびに検索が
    # 分断されては、定期的にデータが増える運用で使い物にならない。**
    classes: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    used = 0
    for entry in load_entries(field):
        if entry.get("approved") is not True:
            continue
        if entry.get("confidence") not in EXPORTED_CONFIDENCE:
            continue
        canonical = (entry.get("canonical") or "").strip()
        values = class_values(entry)
        if not canonical or not values:
            continue
        used += 1
        cls = classes.get(canonical)
        if cls is None:
            cls = classes[canonical] = {"c": canonical, "v": []}
            order.append(canonical)
        # series と kind は先に書かれたほうを採る（後から足す行は
        # 表記を1つ増やすだけのことが多く、省略されがちなため）
        if entry.get("series") and "s" not in cls:
            cls["s"] = entry["series"]
        if entry.get("kind") and "k" not in cls:
            cls["k"] = entry["kind"]
        for raw in values:
            if raw not in cls["v"]:
                cls["v"].append(raw)

    out: dict[str, dict] = {}
    for canonical in order:
        cls = classes[canonical]
        for raw in cls["v"]:
            if raw in out:
                if out[raw]["c"] != canonical:
                    warn.append(f"{field}: 「{raw}」が {out[raw]['c']} と {canonical} の"
                                "両方に登録されています。先に書かれたほうを採ります")
                continue
            out[raw] = cls
    # 同じ入力なら同じバイト列にするためキーを並べ替える
    return {k: out[k] for k in sorted(out)}, used


def export_json(
    *, path: Path | None = None, generated_at: str | None = None
) -> dict[str, Any]:
    """works.toml / artists.toml → web/public/data/aliases.json。

    ネットワークを見ないので CI でも odj.build の末尾でも安全に呼べる。
    返り値はそのまま API の応答（{"ok":true,"path":…,"works":…,"artists":…}）。
    """
    warn: list[str] = []
    works, n_works = _class_map("work", warn)
    artists, n_artists = _class_map("artist", warn)
    payload = {
        "generatedAt": generated_at or date.today().isoformat(),
        "works": works,
        "artists": artists,
    }
    target = path or paths.WEB_ALIASES_JSON
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    result: dict[str, Any] = {
        "ok": True,
        "path": rel_to_repo(target),
        "works": n_works,
        "artists": n_artists,
    }
    if warn:
        result["warnings"] = warn
    return result


def rel_to_repo(path: Path) -> str:
    """応答に載せるパス。リポジトリの外（テストの一時ディレクトリ）ならそのまま。"""
    try:
        return str(path.relative_to(paths.REPO_ROOT))
    except ValueError:
        return str(path)
