"""公開共有された Google Drive フォルダを認証なしで読むための薄いクライアント。

「リンクを知っている全員」で共有されたフォルダは、フォルダページの HTML に
``window['_DRIVE_ivd']`` という JSON が埋め込まれており、そこから子要素の
一覧が取れる。ファイル本体も uc?export=download で落とせるため、OAuth も
API キーも要らない。
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ROOT_FOLDER_ID = "1Ti5vLERqTNbK1WMLuTh1Xk_Y6o9ZL_Ud"
MASTER_DB_ID = "1MEDuHQixRB9_2Kf3YLfK2QHmsT-PV1pTUnSGSgWkISU"

FOLDER_MIME = "application/vnd.google-apps.folder"
GSHEET_MIME = "application/vnd.google-apps.spreadsheet"
GDOC_MIME = "application/vnd.google-apps.document"

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
_IVD_RE = re.compile(rb"window\['_DRIVE_ivd'\]\s*=\s*'(.*?)';", re.S)
_HEX_ESCAPE_RE = re.compile(rb"\\x([0-9a-fA-F]{2})")


@dataclass(frozen=True)
class DriveItem:
    id: str
    name: str
    mime: str

    @property
    def is_folder(self) -> bool:
        return self.mime == FOLDER_MIME


def _get(url: str, *, retries: int = 3) -> bytes:
    last: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read()
        except (urllib.error.URLError, TimeoutError) as exc:  # noqa: PERF203
            last = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"取得に失敗: {url}") from last


def list_folder(folder_id: str) -> list[DriveItem]:
    """公開フォルダ直下の要素を返す。空フォルダなら空リスト。"""
    raw = _get(f"https://drive.google.com/drive/folders/{folder_id}")
    m = _IVD_RE.search(raw)
    if not m:
        raise RuntimeError(
            f"_DRIVE_ivd が見つからない（非公開 or 形式変更）: {folder_id}"
        )

    # JS 文字列リテラル。日本語は生の UTF-8 バイトのまま入っていることも、
    # \xNN エスケープになっていることもあるので、バイト列のまま復元してから
    # 一度だけ UTF-8 デコードする。先に str 化すると多バイト文字が壊れる。
    body = _HEX_ESCAPE_RE.sub(lambda g: bytes([int(g.group(1), 16)]), m.group(1))
    body = body.replace(rb"\/", b"/")
    rows = json.loads(body.decode("utf-8"))[0] or []
    return [DriveItem(id=r[0], name=r[2], mime=r[3]) for r in rows]


def download_url(item: DriveItem) -> str:
    """item を「そのまま解析できる形式」で取得するための URL。

    Google ネイティブ形式はエクスポートが必要。スプレッドシートは csv ではなく
    xlsx で取る（csv は先頭タブしか返さず、複数タブを黙って捨てるため）。
    """
    if item.mime == GSHEET_MIME:
        return f"https://docs.google.com/spreadsheets/d/{item.id}/export?format=xlsx"
    if item.mime == GDOC_MIME:
        return f"https://docs.google.com/document/d/{item.id}/export?format=txt"
    return f"https://drive.google.com/uc?export=download&id={item.id}"


def local_suffix(item: DriveItem) -> str:
    if item.mime == GSHEET_MIME:
        return ".xlsx"
    if item.mime == GDOC_MIME:
        return ".txt"
    suffix = Path(item.name).suffix.lower()
    return suffix if suffix else ".bin"


def fetch(item: DriveItem, cache_dir: Path) -> Path:
    """item をキャッシュへ落として、そのパスを返す。既にあれば再取得しない。"""
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{item.id}{local_suffix(item)}"
    if path.exists() and path.stat().st_size > 0:
        return path
    path.write_bytes(_get(download_url(item)))
    return path


def fetch_master_db(cache_dir: Path) -> Path:
    """既存のマスターDB「オタクDJ大会DB」を xlsx で取得する。"""
    return fetch(
        DriveItem(id=MASTER_DB_ID, name="オタクDJ大会DB", mime=GSHEET_MIME), cache_dir
    )
