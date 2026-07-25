"""マニフェストと既存マスターDBを突き合わせてプレイログを組み立てる。

方針は「ファイルを正、既存マスターDBで穴埋め」の union マージ。
ファイルが無い DJ（音源しか残っていない回がある）はマスターDBの行をそのまま
採用するので、手入力された情報を落とさない。
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import tomllib
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, fields
from datetime import date
from pathlib import Path

from . import drive, normalize as nz, readers, xlsx
from .crawl import DJ_PREFIX_RE
from .drive import DriveItem
from .paths import (
    MANIFEST_PATH, OUT_DIR, OVERRIDES_PATH, PASTE_TSV, PLAYS_CSV, RAW_DIR,
    REPORT_PATH, SQLITE_PATH, WEB_DATA_JSON,
)

MASTER_SHEET = "DB"
# マスターDB の DB タブは固定レイアウト（1列目のヘッダが空欄なので自動判定できない）
MASTER_COLUMNS = [
    "event_no", "play_order", "dj", "track_no",
    "title", "source_work", "artist", "is_remix", "url",
]

FILENAME_TRACK_RE = re.compile(r"^(\d{1,2})[-_.]\s*(.+?)\.[A-Za-z0-9]+$")


@dataclass
class Play:
    event_no: int
    event_date: str
    play_order: int | None
    dj: str
    dj_folder: str
    track_no: int | None
    title: str
    source_work: str | None
    artist: str | None
    is_remix: bool | None
    url: str | None
    bpm: float | None
    year: int | None
    note: str | None
    source_file_id: str | None
    source_file_name: str | None
    source_kind: str
    confidence: str


CONFIDENCE = {
    "xlsx": "high",
    "manual": "medium",
    "txt": "medium",
    "master-db": "medium",
    "filename": "low",
}


@dataclass
class Overrides:
    skips: dict[str, dict]
    drop_rows: dict[str, set[str]]
    fixes: list[dict]


def load_overrides() -> Overrides:
    if not OVERRIDES_PATH.exists():
        return Overrides({}, {}, [])
    data = tomllib.loads(OVERRIDES_PATH.read_text(encoding="utf-8"))
    drop_rows: dict[str, set[str]] = defaultdict(set)
    for row in data.get("drop_row", []):
        drop_rows[row["file_id"]].add(row["title"])
    return Overrides(
        skips={s["file_id"]: s for s in data.get("skip", [])},
        drop_rows=dict(drop_rows),
        fixes=data.get("fix", []),
    )


# --------------------------------------------------------------------------
# 既存マスターDB
# --------------------------------------------------------------------------


def load_master_db(fixes: list[dict]) -> list[dict]:
    path = drive.fetch_master_db(RAW_DIR)
    rows: list[list[str]] = []
    for name, sheet_rows in xlsx.read_sheets(path):
        if name == MASTER_SHEET:
            rows = sheet_rows
            break
    if not rows:
        raise RuntimeError(f"マスターDBに '{MASTER_SHEET}' タブが無い: {path}")

    out: list[dict] = []
    for row in rows[1:]:
        rec = {
            col: nz.clean(row[i]) if i < len(row) else None
            for i, col in enumerate(MASTER_COLUMNS)
        }
        if not rec["title"] or not rec["dj"] or not rec["event_no"]:
            continue
        rec["event_no"] = nz.to_int(rec["event_no"])
        rec["play_order"] = nz.to_int(rec["play_order"])
        rec["track_no"] = nz.to_int(rec["track_no"])
        rec["is_remix"] = nz.to_remix(rec["is_remix"])
        if not nz.is_url(rec["url"]):
            rec["url"] = None
        out.append(rec)

    for fix in fixes:
        if fix.get("kind") != "move_event":
            continue
        moved = 0
        for rec in out:
            if rec["event_no"] == fix["from_event"] and rec["dj"] == fix["dj"]:
                rec["event_no"] = fix["to_event"]
                moved += 1
        print(
            f"  補正: 第{fix['from_event']}回 {fix['dj']} の {moved} 行を "
            f"第{fix['to_event']}回へ移動"
        )
    return out


# --------------------------------------------------------------------------
# ファイル由来のレコード
# --------------------------------------------------------------------------


def read_dj_files(dj: dict, ov: Overrides) -> tuple[list[dict], dict]:
    """DJ の候補ファイルを全部読み、最も内容の濃いものを採用する。"""
    info = {"tried": [], "chosen": None, "skipped": []}
    best: tuple[tuple[int, ...], list[dict], dict] = ((0, 0, 0, 0), [], {})

    for entry in dj["setlists"]:
        if entry["id"] in ov.skips:
            info["skipped"].append(
                {"name": entry["name"], "reason": ov.skips[entry["id"]]["reason"]}
            )
            continue
        item = DriveItem(id=entry["id"], name=entry["name"], mime=entry["mime"])
        try:
            path = drive.fetch(item, RAW_DIR)
            result = readers.read(path, item.id)
        except Exception as exc:  # ネットワーク・破損ファイル
            info["skipped"].append({"name": entry["name"], "reason": f"読込失敗: {exc}"})
            continue
        if result.skipped_reason:
            info["skipped"].append(
                {"name": entry["name"], "reason": result.skipped_reason}
            )
            continue
        drop_titles = ov.drop_rows.get(item.id)
        if drop_titles:
            result.records = [
                r for r in result.records if r.get(nz.TITLE) not in drop_titles
            ]

        info["tried"].append({"name": entry["name"], "rows": len(result.records)})
        score = readers._score(result.records)
        if score > best[0]:
            best = (
                score,
                result.records,
                {
                    "id": item.id,
                    "name": item.name,
                    "kind": result.source_kind,
                    "detail": result.detail,
                },
            )

    info["chosen"] = best[2] or None
    return best[1], info


def infer_from_filenames(dj: dict) -> list[dict]:
    """音源ファイル名が「0-曲名.mp3」形式なら曲順付きの曲名として拾う。"""
    found: list[tuple[int, str]] = []
    for name in dj["media_files"]:
        m = FILENAME_TRACK_RE.match(name)
        if m:
            found.append((int(m.group(1)), m.group(2).replace("_", " ").strip()))
    if len(found) < 3:
        return []
    found.sort()
    return [
        {nz.TRACK_NO: i, nz.TITLE: title} for i, (_, title) in enumerate(found, 1)
    ]


# --------------------------------------------------------------------------
# マージ
# --------------------------------------------------------------------------

ENRICHABLE = ("source_work", "artist", "url", "is_remix", "play_order")


def build(*, infer_filenames: bool = False) -> tuple[list[Play], dict]:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    ov = load_overrides()

    print("既存マスターDBを取得中...")
    master = load_master_db(ov.fixes)
    master_by_pair: dict[tuple[int, str], list[dict]] = defaultdict(list)
    for rec in master:
        master_by_pair[(rec["event_no"], rec["dj"])].append(rec)

    jobs = [(ev, dj) for ev in manifest["events"] for dj in ev["djs"]]
    print(f"セトリファイルを読み込み中... ({len(jobs)} DJ)")
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda j: read_dj_files(j[1], ov), jobs))

    plays: list[Play] = []
    report: dict = {
        "from_file": [], "from_master_only": [], "missing": [],
        "count_mismatch": [], "skipped_files": [], "master_orphans": [],
    }
    seen_pairs: set[tuple[int, str]] = set()

    for (event, dj), (records, info) in zip(jobs, results):
        pair = (event["no"], dj["dj"])
        seen_pairs.add(pair)
        master_rows = master_by_pair.get(pair, [])
        master_by_track = {r["track_no"]: r for r in master_rows if r["track_no"]}

        for skipped in info["skipped"]:
            report["skipped_files"].append(
                {"event": event["no"], "dj": dj["dj"], **skipped}
            )

        source_kind = info["chosen"]["kind"] if info["chosen"] else None
        if not records and infer_filenames and not master_rows:
            records = infer_from_filenames(dj)
            if records:
                source_kind = "filename"

        if records:
            for i, rec in enumerate(records, 1):
                track_no = rec.get(nz.TRACK_NO) or i
                enrich = master_by_track.get(track_no, {})
                merged = {
                    f: rec.get(f) if rec.get(f) is not None else enrich.get(f)
                    for f in ENRICHABLE
                }
                plays.append(
                    Play(
                        event_no=event["no"],
                        event_date=event["date"],
                        play_order=dj["play_order"] or merged["play_order"],
                        dj=dj["dj"],
                        dj_folder=dj["folder"],
                        track_no=track_no,
                        title=rec[nz.TITLE],
                        source_work=merged["source_work"],
                        artist=merged["artist"],
                        is_remix=merged["is_remix"],
                        url=merged["url"],
                        bpm=rec.get(nz.BPM),
                        year=rec.get(nz.YEAR),
                        note=rec.get(nz.NOTE),
                        source_file_id=(info["chosen"] or {}).get("id"),
                        source_file_name=(info["chosen"] or {}).get("name"),
                        source_kind=source_kind or "unknown",
                        confidence=CONFIDENCE.get(source_kind or "", "medium"),
                    )
                )
            report["from_file"].append(
                {
                    "event": event["no"], "dj": dj["dj"], "rows": len(records),
                    "file": (info["chosen"] or {}).get("name"),
                    "kind": source_kind,
                    "master_rows": len(master_rows),
                }
            )
            if master_rows and len(master_rows) != len(records):
                report["count_mismatch"].append(
                    {
                        "event": event["no"], "dj": dj["dj"],
                        "file_rows": len(records), "master_rows": len(master_rows),
                        "file": (info["chosen"] or {}).get("name"),
                    }
                )
        elif master_rows:
            for rec in master_rows:
                plays.append(
                    Play(
                        event_no=event["no"],
                        event_date=event["date"],
                        play_order=dj["play_order"] or rec["play_order"],
                        dj=dj["dj"],
                        dj_folder=dj["folder"],
                        track_no=rec["track_no"],
                        title=rec["title"],
                        source_work=rec["source_work"],
                        artist=rec["artist"],
                        is_remix=rec["is_remix"],
                        url=rec["url"],
                        bpm=None, year=None, note=None,
                        source_file_id=None,
                        source_file_name="オタクDJ大会DB (既存マスター)",
                        source_kind="master-db",
                        confidence=CONFIDENCE["master-db"],
                    )
                )
            report["from_master_only"].append(
                {"event": event["no"], "dj": dj["dj"], "rows": len(master_rows)}
            )
        else:
            report["missing"].append(
                {
                    "event": event["no"], "dj": dj["dj"], "folder": dj["folder"],
                    "media_files": len(dj["media_files"]),
                }
            )

    # マニフェストに対応する DJ フォルダが無いマスターDB の行
    for pair, rows in sorted(master_by_pair.items()):
        if pair not in seen_pairs:
            report["master_orphans"].append(
                {"event": pair[0], "dj": pair[1], "rows": len(rows)}
            )

    plays.sort(key=lambda p: (p.event_no, p.play_order or 99, p.dj, p.track_no or 0))
    return plays, report


# --------------------------------------------------------------------------
# 出力
# --------------------------------------------------------------------------

COLUMNS = [f.name for f in fields(Play)]


def write_csv(plays: list[Play]) -> None:
    with PLAYS_CSV.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        for play in plays:
            writer.writerow(asdict(play))


def write_paste_tsv(plays: list[Play]) -> None:
    """既存スプレッドシートの DB タブに貼り付けられる列順で出力する。"""
    header = ["回", "play順", "DJ", "曲順", "タイトル", "アニメ/元ネタ", "アーティスト", "REMIX", "URL"]
    with PASTE_TSV.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, delimiter="\t", lineterminator="\n")
        writer.writerow(header)
        for p in plays:
            remix = "" if p.is_remix is None else ("○" if p.is_remix else "×")
            writer.writerow([
                p.event_no, p.play_order or "", p.dj, p.track_no or "",
                p.title, p.source_work or "", p.artist or "", remix, p.url or "",
            ])


def write_sqlite(plays: list[Play]) -> None:
    SQLITE_PATH.unlink(missing_ok=True)
    con = sqlite3.connect(SQLITE_PATH)
    con.execute(f"""
        CREATE TABLE plays (
            id INTEGER PRIMARY KEY,
            {', '.join(f'{c} TEXT' for c in COLUMNS)}
        )
    """)
    con.executemany(
        f"INSERT INTO plays ({','.join(COLUMNS)}) "
        f"VALUES ({','.join('?' * len(COLUMNS))})",
        [tuple(getattr(p, c) for c in COLUMNS) for p in plays],
    )
    for col in ("event_no", "dj", "title"):
        con.execute(f"CREATE INDEX idx_plays_{col} ON plays({col})")
    con.commit()
    con.close()


def write_web_json(plays: list[Play]) -> None:
    """GUI 用。行数が多いのでキーは1〜2文字に詰める。"""
    events: dict[int, dict] = {}
    for p in plays:
        ev = events.setdefault(
            p.event_no, {"no": p.event_no, "date": p.event_date, "djs": []}
        )
        if p.dj not in ev["djs"]:
            ev["djs"].append(p.dj)

    payload = {
        "generatedAt": date.today().isoformat(),
        "events": [events[k] for k in sorted(events)],
        "plays": [
            {
                "e": p.event_no, "p": p.play_order, "dj": p.dj, "n": p.track_no,
                "t": p.title, "w": p.source_work, "a": p.artist,
                "r": p.is_remix, "u": p.url, "k": p.source_kind,
            }
            for p in plays
        ],
    }
    WEB_DATA_JSON.parent.mkdir(parents=True, exist_ok=True)
    WEB_DATA_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )


def write_report(plays: list[Play], report: dict) -> None:
    lines: list[str] = ["# 取り込みレポート", ""]
    lines.append(f"生成日: {date.today().isoformat()}")
    lines.append(f"総プレイ数: **{len(plays)}**")

    by_kind: dict[str, int] = defaultdict(int)
    for p in plays:
        by_kind[p.source_kind] += 1
    lines.append("")
    lines.append("| 由来 | 行数 |")
    lines.append("|---|---:|")
    for kind, n in sorted(by_kind.items(), key=lambda kv: -kv[1]):
        lines.append(f"| {kind} | {n} |")

    lines += ["", "## 開催回ごと", "", "| 回 | 日付 | DJ数 | 曲数 |", "|---:|---|---:|---:|"]
    per_event: dict[int, list[Play]] = defaultdict(list)
    for p in plays:
        per_event[p.event_no].append(p)
    for no in sorted(per_event):
        rows = per_event[no]
        lines.append(
            f"| {no} | {rows[0].event_date} | {len({r.dj for r in rows})} | {len(rows)} |"
        )

    if report["missing"]:
        lines += ["", "## セトリ未登録（元ファイルもマスターDBも無い）", ""]
        for m in report["missing"]:
            lines.append(
                f"- 第{m['event']}回 **{m['dj']}**（フォルダ `{m['folder']}`、"
                f"メディア {m['media_files']} 件）"
            )

    if report["from_master_only"]:
        lines += ["", "## 既存マスターDBのみを採用（元ファイルが無い）", ""]
        for m in report["from_master_only"]:
            lines.append(f"- 第{m['event']}回 {m['dj']}: {m['rows']} 曲")

    if report["count_mismatch"]:
        lines += [
            "", "## 曲数がマスターDBと食い違う（要確認）", "",
            "| 回 | DJ | ファイル | ファイル | マスターDB |", "|---:|---|---|---:|---:|",
        ]
        for m in report["count_mismatch"]:
            lines.append(
                f"| {m['event']} | {m['dj']} | {m['file']} | "
                f"{m['file_rows']} | {m['master_rows']} |"
            )

    if report["master_orphans"]:
        lines += ["", "## マスターDBにあるが DJ フォルダが無い", ""]
        for m in report["master_orphans"]:
            lines.append(f"- 第{m['event']}回 {m['dj']}: {m['rows']} 曲")

    if report["skipped_files"]:
        lines += ["", "## 使わなかったファイル", ""]
        for s in report["skipped_files"]:
            reason = " ".join(s["reason"].split())
            lines.append(f"- 第{s['event']}回 {s['dj']} / `{s['name']}`: {reason}")

    lines += ["", "## 項目の充足率", ""]
    lines.append("| 項目 | 埋まっている行 | 割合 |")
    lines.append("|---|---:|---:|")
    for col in ("source_work", "artist", "is_remix", "url", "play_order", "year"):
        n = sum(1 for p in plays if getattr(p, col) is not None)
        lines.append(f"| {col} | {n} | {100 * n / max(len(plays), 1):.0f}% |")

    REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--infer-from-filenames",
        action="store_true",
        help="セトリもマスターDBの行も無い DJ に限り、音源ファイル名から曲順と"
        "曲名を推定する（confidence=low）",
    )
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    plays, report = build(infer_filenames=args.infer_from_filenames)

    write_csv(plays)
    write_paste_tsv(plays)
    write_sqlite(plays)
    write_web_json(plays)
    write_report(plays, report)

    print(f"\nプレイ数 {len(plays)}")
    print(f"  {PLAYS_CSV}")
    print(f"  {SQLITE_PATH}")
    print(f"  {PASTE_TSV}")
    print(f"  {WEB_DATA_JSON}")
    print(f"  {REPORT_PATH}")
    if report["missing"]:
        print(f"  セトリ未登録 {len(report['missing'])} 件（レポート参照）")
    if report["count_mismatch"]:
        print(f"  曲数不一致 {len(report['count_mismatch'])} 件（レポート参照）")


if __name__ == "__main__":
    main()
