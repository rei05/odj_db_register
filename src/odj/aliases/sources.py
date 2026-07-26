"""表記ゆれの候補を外部 API で裏取りする（フェーズ2の fetch）。

block.py が作る候補クラスタは文字列の類似度だけで組んでいるので、略称と
正式名称のように**文字列として無関係なもの**は原理的に繋げられない
（「ナナシス」と「Tokyo 7th シスターズ」）。ここはその隙間を、Wikipedia の
リダイレクト・MusicBrainz の別名・Wikidata の検索といった外部知識で埋める。

**やるのは根拠集めだけ。統合するかどうかは決めない。** 結果は
out/aliases/evidence.<field>.json に {生表記: [根拠, ...]} として書き、
次工程（LLM が同一性を裁定する odj.aliases ask）の判断材料にする。
ヒット無しも `[]` として必ず記録する。MusicBrainz が引けないことは
「タイポかもしれない」というシグナルそのものだからで、キーごと省略すると
その情報が失われる。

対象を絞る理由: plays.json 全体の元ネタ・アーティストは 1073 種／1110 種あるが、
1回しか出てこない値まで引くと現実的でないリクエスト数になる。実測で
「2回以上出る値」に絞ると work 279 種（全行の63%）・artist 266 種（51%）に
収まる。裏取りは block.py が作ったクラスタの中身に限らない
（「ナナシス」のように似た表記が無いために単独値のままクラスタに現れない値こそ、
まさにここで裏取りしたい対象なので）。plays.json から `block.collect()` で
作る値の一覧を直接見る。

叩く API と使い分け:
    work   … ① Wikipedia ja リダイレクト → ② Wikidata 検索
             AniList は使わない。日本語の略称にほぼ効かないことを実測で確認済み
             （「ナナシス」「学マス」「リゼロ」が全滅、「ユーフォ」は
             euphoria に誤マッチ）。
    artist … ① MusicBrainz 検索 → ② Wikidata 検索
             MusicBrainz は商業アーティストに強いが、同人・ボカロP・VTuber には
             当たらない。

キャッシュは data/raw/api/<source>/<sha256(クエリ)>.json（drive.fetch() と同じ
思想。data/raw/ は gitignore 済みで、キャッシュがあればネットワークに出ない）。
"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

from odj import paths
from odj.aliases import block, store


class SourceError(Exception):
    """裏取りが続けられない失敗（通信断・想定外の field 指定など）。"""


# 2回以上出る値だけを引く。1回きりの値まで含めると work だけで 1073 リクエストに
# なり、MusicBrainz の 1 req/sec 制限では現実的でない（詳しくはモジュール docstring）。
MIN_ROWS = 2

_UA = "odj-db-register/0.1 ( hasegawa0kn@gmail.com )"

# MusicBrainz は 1 req/sec 厳守。破ると BAN される。
_MB_INTERVAL = 1.0
# Wikipedia / Wikidata は明文化されたレート制限は無いが、常識的な間隔を空ける。
_WIKI_INTERVAL = 0.2

_WIKIPEDIA_API = "https://ja.wikipedia.org/w/api.php"
_WIKIDATA_API = "https://www.wikidata.org/w/api.php"
_MUSICBRAINZ_API = "https://musicbrainz.org/ws/2"

# Wikidata 検索の候補は上位いくつまで拾うか。多すぎると LLM への入力が膨らむ。
_WIKIDATA_LIMIT = 3


# ---------------------------------------------------------------------------
# HTTP（drive.py の _get() と同じ雛形: UA + リトライ + バックオフ）
# ---------------------------------------------------------------------------


def _get(url: str, *, retries: int = 3) -> bytes:
    last: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read()
        except (urllib.error.URLError, TimeoutError) as exc:  # noqa: PERF203
            last = exc
            time.sleep(1.5 * (attempt + 1))
    raise SourceError(f"取得に失敗: {url}") from last


# ---------------------------------------------------------------------------
# レート制限
# ---------------------------------------------------------------------------


class _RateLimiter:
    """source ごとに最小間隔を空けてから返す。

    fetch() の実行のたびに1つ作ってその中の呼び出しへ配る。モジュール変数に
    しないのは、単体テストが互いの待ち時間（前のテストの最終アクセス時刻）を
    引きずらないようにするため。

    キャッシュがヒットしたときはこのクラスを一切呼ばないので、待たない
    （キャッシュ済みの値を何度読み直しても速いままにするため）。
    """

    def __init__(self) -> None:
        self._last: dict[str, float] = {}

    def wait(self, source: str, interval: float) -> None:
        now = time.monotonic()
        last = self._last.get(source)
        if last is not None:
            remain = interval - (now - last)
            if remain > 0:
                time.sleep(remain)
                now = time.monotonic()
        self._last[source] = now


# ---------------------------------------------------------------------------
# キャッシュ付き取得
# ---------------------------------------------------------------------------


def _cache_path(source: str, cache_key: str) -> Path:
    digest = hashlib.sha256(cache_key.encode("utf-8")).hexdigest()
    return paths.RAW_DIR / "api" / source / f"{digest}.json"


def _fetch_json(
    source: str, cache_key: str, url: str, limiter: _RateLimiter, interval: float
) -> Any:
    """source の API を1回叩いて JSON を返す。キャッシュがあればネットワークに出ない。

    cache_key はクエリを一意に決める文字列（検索語や MBID など）。URL そのものを
    キーにしないのは、将来クエリパラメータの並びを変えても同じキャッシュを
    再利用できるようにするため。
    """
    path = _cache_path(source, cache_key)
    if path.exists():
        with path.open(encoding="utf-8") as fh:
            return json.load(fh)
    limiter.wait(source, interval)
    data = json.loads(_get(url))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    return data


# ---------------------------------------------------------------------------
# Wikipedia ja リダイレクト（work の①）
# ---------------------------------------------------------------------------


def _wikipedia_redirect(name: str, limiter: _RateLimiter) -> list[dict]:
    """略称→正式名称のリダイレクトを解決する。実測で6件中5件正解の主力。

        ナナシス → Tokyo 7th シスターズ / 学マス → 学園アイドルマスター
        リゼロ   → Re:ゼロから始める異世界生活 / デレマス → シンデレラガールズ
        シュタゲ → STEINS;GATE

    「ユーフォ」のような曖昧語はリダイレクトが無い（曖昧さ回避ページか、
    そもそも記事が無い）。これは誤りではなく、正しく「要人手」に落ちる挙動。
    """
    url = (
        f"{_WIKIPEDIA_API}?action=query&titles={urllib.parse.quote(name)}"
        "&redirects=1&format=json&formatversion=2"
    )
    data = _fetch_json("wikipedia-ja", f"redirect:{name}", url, limiter, _WIKI_INTERVAL)
    redirects = (data.get("query") or {}).get("redirects") or []
    if not redirects:
        return []
    to = (redirects[0].get("to") or "").strip()
    if not to:
        return []
    return [
        {
            "source": "wikipedia-ja",
            "id": to,
            "title": to,
            "kind": "redirect",
            "note": f"「{name}」は {to} へのリダイレクト",
            "url": "https://ja.wikipedia.org/wiki/" + to.replace(" ", "_"),
        }
    ]


# ---------------------------------------------------------------------------
# Wikidata 検索（work・artist 共通の②）
# ---------------------------------------------------------------------------


def _wikidata_search(name: str, limiter: _RateLimiter) -> list[dict]:
    """Wikidata の項目検索。①で引けなかったときの拾い直し。

    同人・VTuber・ボカロP のように MusicBrainz が弱い対象や、Wikipedia に
    リダイレクトが無い曖昧語のさらなる手掛かりに使う。上位 3 件まで候補として残し、
    どれが正しいかの判断は後段（LLM・人間）に任せる。
    """
    url = (
        f"{_WIKIDATA_API}?action=wbsearchentities&language=ja&uselang=ja"
        f"&search={urllib.parse.quote(name)}&format=json"
    )
    data = _fetch_json("wikidata", f"search:{name}", url, limiter, _WIKI_INTERVAL)
    results = (data.get("search") or [])[:_WIKIDATA_LIMIT]
    out: list[dict] = []
    for item in results:
        qid = (item.get("id") or "").strip()
        if not qid:
            continue
        label = (item.get("label") or name).strip()
        note = "Wikidata 検索"
        if item.get("description"):
            note += f": {item['description']}"
        out.append(
            {
                "source": "wikidata",
                "id": qid,
                "title": label,
                "kind": "search",
                "note": note,
                "url": f"https://www.wikidata.org/wiki/{qid}",
            }
        )
    return out


# ---------------------------------------------------------------------------
# MusicBrainz 検索（artist の①）
# ---------------------------------------------------------------------------


def _musicbrainz_artist(name: str, limiter: _RateLimiter) -> list[dict]:
    """MusicBrainz でアーティスト名を裏取りする。当たれば正確、外れればタイポの検出器。

        ChouCho           → score 100（表記そのまま一致）
        AKINO from bless4 → AKINO（alias 経由で解決）
        kz(livetune)      → kz
        Aiobarn           → 0件（"Aiobahn" のタイポ）

    表記が完全一致すればそれで確定。一致しない場合は、その候補の別名(alias)一覧を
    追加で引いて、クエリした文字列がそのまま別名登録されていないか確認する
    （"AKINO from bless4" のような feat. 的な表記が実際に別名として登録されている）。
    どちらでも無ければ、検索スコアの一致度をそのまま渡し、判断は後段に任せる。
    """
    search_url = f"{_MUSICBRAINZ_API}/artist?query={urllib.parse.quote(name)}&fmt=json&limit=3"
    data = _fetch_json("musicbrainz", f"search:{name}", search_url, limiter, _MB_INTERVAL)
    candidates = data.get("artists") or []
    if not candidates:
        return []  # 0件 = タイポ疑い。呼び出し側が [] のまま記録する

    top = candidates[0]
    mbid = (top.get("id") or "").strip()
    title = (top.get("name") or "").strip()
    score = top.get("score", 0)
    if not mbid or not title:
        return []
    url = f"https://musicbrainz.org/artist/{mbid}"

    if title == name:
        return [
            {
                "source": "musicbrainz",
                "id": mbid,
                "title": title,
                "kind": "search",
                "note": f"MusicBrainz 検索でスコア {score} の一致",
                "url": url,
            }
        ]

    alias_url = f"{_MUSICBRAINZ_API}/artist/{mbid}?inc=aliases&fmt=json"
    alias_data = _fetch_json(
        "musicbrainz", f"aliases:{mbid}", alias_url, limiter, _MB_INTERVAL
    )
    alias_names = {(a.get("name") or "").strip() for a in (alias_data.get("aliases") or [])}
    if name in alias_names:
        return [
            {
                "source": "musicbrainz",
                "id": mbid,
                "title": title,
                "kind": "alias",
                "note": f"「{name}」は {title} の別名(alias)として登録",
                "url": url,
            }
        ]

    # 別名にも見つからないが検索自体はヒットしている。スコアと実際の表記を
    # そのまま渡し、これが同じものかどうかの判断は後段に委ねる。
    return [
        {
            "source": "musicbrainz",
            "id": mbid,
            "title": title,
            "kind": "search",
            "note": f"MusicBrainz 検索でスコア {score}（表記は「{title}」）",
            "url": url,
        }
    ]


# ---------------------------------------------------------------------------
# フィールドごとの優先順位
# ---------------------------------------------------------------------------

_SourceFn = Callable[[str, "_RateLimiter"], list[dict]]

_SOURCE_CHAINS: dict[str, tuple[_SourceFn, ...]] = {
    "work": (_wikipedia_redirect, _wikidata_search),
    "artist": (_musicbrainz_artist, _wikidata_search),
}


def _evidence_for(field: str, name: str, limiter: _RateLimiter) -> list[dict]:
    """1つの生表記を優先順位どおりに裏取りする。最初に当たった時点で止める。

    複数ソースを足し合わせて多数決にはしない。目的はあくまで根拠集めで、
    値1つあたりのリクエスト数を抑えることが 279+266 種を現実的な時間で
    引き切るために必要だから。
    """
    for src in _SOURCE_CHAINS[field]:
        result = src(name, limiter)
        if result:
            return result
    return []


# ---------------------------------------------------------------------------
# 組み立て
# ---------------------------------------------------------------------------


def _evidence_path(field: str) -> Path:
    return paths.OUT_ALIASES_DIR / f"evidence.{field}.json"


def _target_values(field: str) -> list[block.Value]:
    """裏取り対象の生表記。plays.json の全体からクラスタに関係なく集める。

    「ナナシス」は似た表記が無いために block.py のクラスタには現れない
    単独値だが、まさにこういう値こそ外部 API で裏取りしたい対象なので、
    out/aliases/clusters.<field>.json ではなく plays.json を直接見る。
    """
    plays = block.load_plays()
    values = block.collect(plays, block.FIELD_KEYS[field])
    targets = [v for v in values.values() if v.rows >= MIN_ROWS]
    # 行数が多い（効きが大きい）ものから引く。block.py の並びと同じ考え方。
    targets.sort(key=lambda v: (-v.rows, v.raw))
    return targets


def _load_evidence_file(field: str) -> dict[str, list[dict]]:
    path = _evidence_path(field)
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as fh:
        return dict(json.load(fh).get("evidence", {}))


def fetch(field: str, *, only_new: bool = False, limit: int | None = None) -> dict[str, Any]:
    """生表記を外部 API で裏取りし、out/aliases/evidence.<field>.json に書く。

    既存の evidence.json があれば読み込んでマージする（今回引かなかった値の
    結果を消さないため）。--only-new は辞書（works.toml / artists.toml）と
    既存の evidence の両方に既に載っている値を飛ばす。--limit と組み合わせて
    「まだ引いていない分だけ N 件」という使い方ができる。
    """
    if field not in block.FIELD_KEYS:
        raise SourceError(f"field は work か artist です: {field!r}")

    all_targets = _target_values(field)
    targets = all_targets

    existing = _load_evidence_file(field)
    if only_new:
        already = set(store.variant_index(store.load_entries(field))) | set(existing)
        targets = [v for v in targets if v.raw not in already]
    skipped = len(all_targets) - len(targets)

    if limit is not None:
        targets = targets[:limit]

    limiter = _RateLimiter()
    evidence = dict(existing)
    hits = 0
    for value in targets:
        result = _evidence_for(field, value.raw, limiter)
        evidence[value.raw] = result  # ヒット無しでも [] を必ず記録する
        if result:
            hits += 1

    payload = {"field": field, "evidence": {k: evidence[k] for k in sorted(evidence)}}
    out_path = _evidence_path(field)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)
        fh.write("\n")

    return {
        "path": out_path,
        "candidateTotal": len(all_targets),
        "skipped": skipped,
        "fetched": len(targets),
        "hits": hits,
        "misses": len(targets) - hits,
        "totalEvidence": len(evidence),
    }
