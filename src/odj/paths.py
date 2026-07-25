"""リポジトリ内の固定パス。"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

DATA_DIR = REPO_ROOT / "data"
MANIFEST_PATH = DATA_DIR / "manifest.json"
MANUAL_DIR = DATA_DIR / "manual"
OVERRIDES_PATH = DATA_DIR / "overrides.toml"
RAW_DIR = DATA_DIR / "raw"

OUT_DIR = REPO_ROOT / "out"
PLAYS_CSV = OUT_DIR / "plays.csv"
SQLITE_PATH = OUT_DIR / "odj.sqlite"
PASTE_TSV = OUT_DIR / "paste.tsv"
REPORT_PATH = OUT_DIR / "report.md"

WEB_DATA_JSON = REPO_ROOT / "web" / "public" / "data" / "plays.json"
