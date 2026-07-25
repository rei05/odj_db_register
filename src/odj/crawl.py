"""Drive のフォルダ構成を走査して data/manifest.json を作る。

構成は 「開催回フォルダ / DJフォルダ / セトリファイル」 の3階層。
DJフォルダの下にさらにフォルダ（VIDEO, movie, 素材 ...）があることもあるが、
そこにセトリが入っていた例は無いので降りない。
"""

from __future__ import annotations

import argparse
import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path

from . import drive
from .drive import DriveItem
from .paths import MANIFEST_PATH

EVENT_FOLDER_RE = re.compile(r"^第(\d+)回_(\d{8})$")
DJ_PREFIX_RE = re.compile(r"^(\d+|[xX]{2})_")

# 開催回フォルダ直下にある、DJ ではないフォルダ
NON_DJ_FOLDERS = {"現地映像", "現地動画", "flyer", "ポスター・ロゴ等", "素材"}

# セトリではないと判っているファイル（拡張子だけでは弾けないもの）
NOT_SETLIST_NAMES = {
    "ODJ tutorial.pdf",  # 第10回 あぴす。DJ入門資料
    "然るべきタイミングで開くこと歌詞.txt",  # 第7回 tri。歌詞
    "readme.txt",  # 第5回 あちょ。セトリ本体は同フォルダの xlsx
}
NOT_SETLIST_PATTERNS = (re.compile(r"flyer", re.I), re.compile(r"ポスター"))

MEDIA_SUFFIXES = {
    ".wav", ".mp3", ".m4a", ".aif", ".aiff", ".flac", ".ogg",
    ".mp4", ".mov", ".avi", ".mkv", ".wmv", ".m4v",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic",
    ".zip", ".pptx", ".key",
}

# .numbers は PDF か xlsx の兄弟が必ず存在するので解析対象にしない
SKIP_SUFFIXES = MEDIA_SUFFIXES | {".numbers"}

DJ_ALIASES = {
    "⛩": "tri",
    "⛩️": "tri",
    "神社の人": "tri",
}


def normalize_dj(folder_name: str) -> str:
    name = DJ_PREFIX_RE.sub("", folder_name).strip()
    return DJ_ALIASES.get(name, name)


def play_order_of(folder_name: str) -> int | None:
    m = DJ_PREFIX_RE.match(folder_name)
    if not m or not m.group(1).isdigit():
        return None
    return int(m.group(1))


def is_setlist_candidate(item: DriveItem) -> bool:
    if item.is_folder:
        return False
    if item.name in NOT_SETLIST_NAMES:
        return False
    if any(p.search(item.name) for p in NOT_SETLIST_PATTERNS):
        return False
    if item.mime.startswith(("audio/", "video/", "image/")):
        return False
    if Path(item.name).suffix.lower() in SKIP_SUFFIXES:
        return False
    return True


def _crawl_event(event: DriveItem) -> dict:
    m = EVENT_FOLDER_RE.match(event.name)
    assert m, event.name
    event_no = int(m.group(1))
    event_date = datetime.strptime(m.group(2), "%Y%m%d").date()

    children = drive.list_folder(event.id)
    dj_folders = [
        c for c in children if c.is_folder and c.name not in NON_DJ_FOLDERS
    ]

    def build_dj(folder: DriveItem) -> dict:
        files = drive.list_folder(folder.id)
        return {
            "dj": normalize_dj(folder.name),
            "folder": folder.name,
            "folder_id": folder.id,
            "play_order": play_order_of(folder.name),
            "setlists": [
                {"id": f.id, "name": f.name, "mime": f.mime}
                for f in files
                if is_setlist_candidate(f)
            ],
            # セトリが無い DJ 向けの手掛かり（曲名がファイル名になっている例がある）
            "media_files": [f.name for f in files if not is_setlist_candidate(f)],
        }

    with ThreadPoolExecutor(8) as pool:
        djs = list(pool.map(build_dj, dj_folders))

    return {
        "no": event_no,
        "date": event_date.isoformat(),
        "folder": event.name,
        "folder_id": event.id,
        "djs": djs,
    }


def crawl() -> dict:
    root = drive.list_folder(drive.ROOT_FOLDER_ID)
    events = [i for i in root if i.is_folder and EVENT_FOLDER_RE.match(i.name)]
    with ThreadPoolExecutor(6) as pool:
        results = list(pool.map(_crawl_event, events))
    results.sort(key=lambda e: e["no"])
    return {
        "root_folder_id": drive.ROOT_FOLDER_ID,
        "crawled_at": date.today().isoformat(),
        "events": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-o", "--out", type=Path, default=MANIFEST_PATH)
    args = parser.parse_args()

    manifest = crawl()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )

    n_dj = sum(len(e["djs"]) for e in manifest["events"])
    n_set = sum(len(d["setlists"]) for e in manifest["events"] for d in e["djs"])
    missing = [
        f"第{e['no']}回 {d['dj']}"
        for e in manifest["events"]
        for d in e["djs"]
        if not d["setlists"]
    ]
    print(f"{args.out}: {len(manifest['events'])}回 / {n_dj} DJ / {n_set} セトリ候補")
    if missing:
        print(f"セトリファイル無し ({len(missing)}件): {', '.join(missing)}")


if __name__ == "__main__":
    main()
