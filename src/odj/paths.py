"""リポジトリ内の固定パス。"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

DATA_DIR = REPO_ROOT / "data"
MANIFEST_PATH = DATA_DIR / "manifest.json"
MANUAL_DIR = DATA_DIR / "manual"
OVERRIDES_PATH = DATA_DIR / "overrides.toml"
RAW_DIR = DATA_DIR / "raw"

# 名寄せ（表記ゆれの統合）の人手側の入力。どれも無くても動く。
ALIASES_DIR = DATA_DIR / "aliases"
KEEP_APART_PATH = ALIASES_DIR / "keep_apart.toml"
DECISIONS_PATH = ALIASES_DIR / "decisions.jsonl"
# works.toml / artists.toml は odj.aliases store が名前を組み立てる
# （field ごとに引くので、定数を2つ置くより entries_path(field) 1本のほうが素直）。

OUT_DIR = REPO_ROOT / "out"
PLAYS_CSV = OUT_DIR / "plays.csv"
SQLITE_PATH = OUT_DIR / "odj.sqlite"
PASTE_TSV = OUT_DIR / "paste.tsv"
REPORT_PATH = OUT_DIR / "report.md"

OUT_ALIASES_DIR = OUT_DIR / "aliases"

WEB_DATA_JSON = REPO_ROOT / "web" / "public" / "data" / "plays.json"
# 承認済みの同値クラスだけを載せた公開用の辞書（odj.aliases export が書く）。
WEB_ALIASES_JSON = REPO_ROOT / "web" / "public" / "data" / "aliases.json"
