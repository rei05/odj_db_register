"""「同じものかもしれない値」を候補クラスタに切り分ける。

1073 種の元ネタ名を総当たりすると 57 万組になるが、Python では数秒で終わるので
速さは問題にならない。ここでの目的は**後段に渡せる単位に落とすこと**で、
LLM に投げるバッチも人間が GUI で1件ずつ見る単位もクラスタだからである。

値をノード、「似ている根拠」を辺として張り、連結成分をクラスタとする。
辺には種別を残す。後で LLM のプロンプトと GUI の両方に「なぜこの2つが候補に
上がったか」を見せるためで、根拠の質がそのまま判断の速さになる。

入力は web/public/data/plays.json だけ。out/plays.csv を使わないのは、そちらが
odj.build の実行（＝ Drive へのアクセス）を要求するためで、この処理は GitHub
Actions 上でも動かすので Drive に触れないことが必須。

**ここは work の「ブランド単位でまとめる」方針には届かない。** これは既知の
到達点で、利用者の判断でここに留めてある。work は同じブランド名を冠する作品を
まとめて1つにする方針（llm.py の _FIELD_TEXT を参照）だが、ブランドの所属は
文字列の類似では発見できない。実測すると、

    「学園アイドルマスター」と「アイドルマスターシンデレラガールズ」
        agg_key で 10 字 / 17 字。最長共通は「あいどるますたー」8 字あるのに
        部分一致にならず、bigram Jaccard は 0.28 で BIGRAM_MIN 0.65 に届かない
    「機動戦士ガンダムSEED」と「機動戦士ガンダム 水星の魔女」  同上（共通 8 字）
    「マクロスΔ」と「マクロスF」   bigram 0.60。**わずかに**閾値未満
    「学マス」と「デレマス」        共通は「ます」2 字。繋がる材料が無い

の通りで、アイマス系は 6 クラスタに分散し、`機動戦士ガンダムSEED` `マクロスΔ`
`マクロス7` `ラブライブ!虹ヶ咲学園スクールアイドル同好会` など 13 値は単独値の
まま LLM の判定にもレビュー GUI にも回らない。1クラスタの中でしかグループを
作れないので、LLM 側では埋められない。まとまったのは1クラスタに収まった
とある系・けいおん・ウマ娘・ユーフォ・アイカツ・水星の魔女だけである。

届かせるには閾値を下げるだけでは足りず、(1) 長い共通部分で辺を張る仕組みと
OVERSIZED の引き上げ（ブランドを全部繋ぐと 12 を超えて必ず割られる。実際
「ラブライブ!虹ヶ咲…」は substr の辺があるのにこれで切り離されている）か、
(2) ブランドの所属を人手で列挙した辞書、のどちらかが要る。閾値だけ緩めると
無関係な値の統合（不可逆）が増えるので、安易に下げないこと。
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from collections import defaultdict
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

from odj import paths
from odj.aliases import rules

# ---------------------------------------------------------------------------
# 閾値
# ---------------------------------------------------------------------------

# 文字 bigram の Jaccard。「響け!ユーフォニアム」と「響け!ユーフォニアム3」が
# 0.89、「アイカツ」と「アイカツスターズ」が 0.43。前者は拾って後者は
# 部分一致（辺 substr）のほうに任せたいので、その間に置く。
BIGRAM_MIN = 0.65

# 長さがこれ以上違う組は bigram を計算するまでもなく別物として枝刈りする。
LENGTH_BAND = 2.0

# difflib の類似度。タイポ（THE IDLOM@STER / Guiltry Crown / Aiobarn）を拾う。
# 0.80 まで下げると「ラブライブ」と「ラブライバー」のような別物が入り始める。
EDIT_MIN = 0.85

# 編集距離を当てる最短の長さ。短い語ほど1文字の差が偶然一致しやすく、
# 「Lia」と「LiSA」（別人）が「Aiobahn」と「Aiobarn」（同じ人のタイポ）と
# 同じ 0.857 で並んでしまう。長さで切るほうが閾値をいじるより素直。
EDIT_MIN_LEN = 4

# 部分一致を許す最短の長さ。短すぎる値（「チ」「ODJ」）が何にでも含まれてしまう。
SUBSTR_MIN_LEN = 3

# これを超えたクラスタは自動処理させず、人間に個別に見せる。
# 実データの「アイマス」系（アイマス/デレマス/学マス/シャニマス/正式名称…）が
# 確実にここに落ちる。
OVERSIZED = 12

# 候補値に添える共起の例の数。多すぎると LLM の入力が膨らむ。
SAMPLE = 4


# ---------------------------------------------------------------------------
# 値の棚卸し
# ---------------------------------------------------------------------------


@dataclass
class Value:
    """1つの生表記と、それがどこに現れるか。"""

    raw: str
    rows: int = 0
    events: set[int] = field(default_factory=set)
    djs: set[str] = field(default_factory=set)
    co_titles: set[str] = field(default_factory=set)
    co_artists: set[str] = field(default_factory=set)
    co_works: set[str] = field(default_factory=set)
    # artist 列にも同じ文字列が現れる（元ネタ列にアーティスト名が入っている）。
    # 実データで 116 種・288 行あり、Ado / TRUE / Avicii など。
    cross_field: bool = False

    def to_json(self, *, with_base: bool = False) -> dict:
        out = {
            "raw": self.raw,
            "rows": self.rows,
            "events": sorted(self.events),
            "djs": sorted(self.djs),
        }
        if self.co_titles:
            out["coTitles"] = sorted(self.co_titles)[:SAMPLE]
        if self.co_artists:
            out["coArtists"] = sorted(self.co_artists)[:SAMPLE]
        if self.co_works:
            out["coWorks"] = sorted(self.co_works)[:SAMPLE]
        # 注記（OP・ED・「2期」・TVアニメ「」…）を剥がした形。レビュー GUI が
        # 正準名を自動で推定するのに使う（web/src/review/canonical.ts）。
        # 「その着せ替え人形は恋をする 2期」と「〜 OP」しか無いクラスタでは、
        # もっともらしい正準名「その着せ替え人形は恋をする」が生表記のどこにも
        # 無い。剥がす規則は rules.strip_notes 1か所に置いたままにしたいので、
        # TS 側へ移植せず**ここで剥がした結果を JSON に載せて渡す**。
        #
        # **work のときだけ。** strip_notes は元ネタ列の注記を落とす関数で、
        # アーティスト名に当てると末尾が「劇場版」「映画」「楽曲」で終わる名義を
        # 削りかねない。artist の正準名は従来どおり rows の多い生表記から選ぶ
        # （llm.py の _FIELD_TEXT["artist"]["rule3"] と同じ方針）。
        # 生表記と同じときは出さない。大半の値がそうなので、載せると JSON が
        # 膨らむだけで読む側の分岐も増える。
        if with_base:
            base = rules.strip_notes(self.raw)
            if base and base != self.raw:
                out["base"] = base
        if self.cross_field:
            out["crossField"] = True
        return out


def collect(plays: list[dict], field_key: str) -> dict[str, Value]:
    """plays.json の1フィールドを値ごとにまとめる。

    field_key は "w"（元ネタ）か "a"（アーティスト）。
    """
    other = "a" if field_key == "w" else "w"
    values: dict[str, Value] = {}
    for p in plays:
        raw = (p.get(field_key) or "").strip()
        if not raw:
            continue
        v = values.get(raw)
        if v is None:
            v = values[raw] = Value(raw=raw)
        v.rows += 1
        v.events.add(p["e"])
        v.djs.add(p["dj"])
        v.co_titles.add(p["t"])
        if p.get(other):
            (v.co_artists if other == "a" else v.co_works).add(p[other])

    # 辺7: クロスフィールド。元ネタ列とアーティスト列に同じ文字列があるものを
    # 集合の積で機械的に検出する（LLM は要らない）。
    works = {(p.get("w") or "").strip() for p in plays} - {""}
    artists = {(p.get("a") or "").strip() for p in plays} - {""}
    both = works & artists
    for raw in both:
        if raw in values:
            values[raw].cross_field = True
    return values


# ---------------------------------------------------------------------------
# 辺を張る
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Edge:
    a: str
    b: str
    kind: str


def _bigrams(s: str) -> set[str]:
    return {s[i : i + 2] for i in range(len(s) - 1)} or {s}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _is_ascii_ish(s: str) -> bool:
    """difflib を当てるかどうか。

    日本語は1文字の重みが大きく、「東方Project」と「東方project」のような
    大小差以外はほとんど 0.85 に届かない一方、「ラブライブ」と「ラブライバー」が
    0.90 で通ってしまう。タイポ検出は英字の値に絞る。
    """
    return all(ord(c) < 0x3000 for c in s)


def build_edges(values: dict[str, Value], keep_apart: set[frozenset[str]]) -> list[Edge]:
    """似ている根拠ごとに辺を張る。種別は後段への説明なので必ず残す。"""
    raws = sorted(values)
    keys = {r: rules.agg_key(rules.strip_notes(r)) for r in raws}
    # 注記を剥がす前のキーも持っておく。strip_notes が行き過ぎたときに
    # 素の一致まで見失わないようにするため。
    plain = {r: rules.agg_key(r) for r in raws}
    grams = {r: _bigrams(keys[r]) for r in raws}
    marks = {r: rules.series_marks(r) for r in raws}
    flat = {r: r.lower().replace(" ", "").replace("　", "") for r in raws}

    edges: list[Edge] = []
    seen: set[tuple[frozenset[str], str]] = set()

    def add(a: str, b: str, kind: str) -> None:
        if frozenset((a, b)) in keep_apart:
            return
        # 注記違いの表記を経由した迂回路も塞ぐ（load_keep_apart の説明を参照）。
        ka, kb = keys.get(a, ""), keys.get(b, "")
        if ka and kb and ka != kb and frozenset((ka, kb)) in keep_apart:
            return
        # 1セルに複数の作品が改行で詰め込まれた行が実データに1件ある
        # （「アイカツ!\nラブライブ!\nアイドルマスター シンデレラガールズ…」）。
        # 部分一致でそこに書かれた全作品と繋がってしまうので候補から外す。
        # 行の分割は表記ゆれの統一ではなく overrides.toml の仕事。
        if "\n" in a or "\n" in b:
            return
        # 注記あり／なしの両方のキーで一致を見るので、同じ組に同じ種別の辺を
        # 二度張ろうとすることがある。辺の本数を数える処理を後から足したときに
        # 二重計上にならないよう、ここで落としておく。
        marker = (frozenset((a, b)), kind)
        if marker in seen:
            return
        seen.add(marker)
        edges.append(Edge(a, b, kind))

    # 辺1: 注記を剥がしたキーの衝突。「アイカツ! 楽曲」と「アイカツ」など。
    by_key: dict[str, list[str]] = defaultdict(list)
    for r in raws:
        if keys[r]:
            by_key[keys[r]].append(r)
    for group in by_key.values():
        for i in range(1, len(group)):
            add(group[0], group[i], "agg")
    by_plain: dict[str, list[str]] = defaultdict(list)
    for r in raws:
        if plain[r]:
            by_plain[plain[r]].append(r)
    for group in by_plain.values():
        for i in range(1, len(group)):
            add(group[0], group[i], "agg")

    # 辺5: 大小・空白だけの差。信頼度が高いので後段で LLM を通さず扱える。
    by_flat: dict[str, list[str]] = defaultdict(list)
    for r in raws:
        by_flat[flat[r]].append(r)
    for group in by_flat.values():
        for i in range(1, len(group)):
            add(group[0], group[i], "caseonly")

    # 辺2/3/4: 総当たり。1073 種でも 57 万組なので長さバンドで枝刈りすれば足りる。
    for i, a in enumerate(raws):
        ka, ga, ma = keys[a], grams[a], marks[a]
        if not ka:
            continue
        for b in raws[i + 1 :]:
            kb = keys[b]
            if not kb:
                continue
            la, lb = len(ka), len(kb)
            if la == 0 or lb == 0:
                continue

            # 辺4: 部分一致。「アイカツ」⊂「アイカツスターズ」のようにブランド名を
            # 共有する同シリーズ作品（work の方針転換後は統合してよい組）を拾える
            # 一方で、無関係な語がたまたま他方に含まれるだけの組も同じ経路で
            # 引っかかる。どちらなのかはここでは分からないので、後段に
            # series-risk として渡して人間・LLM の判断に委ねる。
            # キーが完全に一致する組は辺1が拾っているし、そちらは危険でもない
            # （「ボカロ」と「ボカロ?」）。ka != kb を入れないと全ての一致組に
            # series-risk が付いて、本物の危険が埋もれる。
            if min(la, lb) >= SUBSTR_MIN_LEN and ka != kb and (ka in kb or kb in ka):
                add(a, b, "substr")
                continue

            if not (1 / LENGTH_BAND <= la / lb <= LENGTH_BAND):
                continue

            # 辺2: bigram Jaccard。
            if _jaccard(ga, grams[b]) >= BIGRAM_MIN:
                add(a, b, "bigram")
                continue

            # 辺3: 編集距離（英字のみ）。タイポ用。
            if (
                min(la, lb) >= EDIT_MIN_LEN
                and _is_ascii_ish(ka)
                and _is_ascii_ish(kb)
                and ma == marks[b]
                and SequenceMatcher(None, ka, kb).ratio() >= EDIT_MIN
            ):
                add(a, b, "edit")

    return edges


def load_evidence(field_name: str) -> dict[str, list[dict]]:
    """fetch が書いた外部 API の裏取り結果。**無ければ空**で、その場合は
    リダイレクトの辺が張られないだけで他は変わらない。"""
    path = paths.OUT_ALIASES_DIR / f"evidence.{field_name}.json"
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    raw = data.get("evidence")
    if not isinstance(raw, dict):
        return {}
    return {k: list(v) for k, v in raw.items() if isinstance(v, list)}


def redirect_edges(
    values: dict[str, Value],
    evidence: dict[str, list[dict]],
    keep_apart: set[frozenset[str]],
) -> list[Edge]:
    """辺8: 外部 API が「これは別名だ」と明示している組。

    **文字列の類似では原理的に届かない層がここで埋まる。** 「ナナシス」と
    「Tokyo 7th シスターズ」は agg_key が「ななしす」と「tokyo7thしすたーず」で、
    bigram も編集距離も部分一致も一度も繋がらない。閾値をいくら緩めても届かず、
    緩めた分だけ別作品が混ざる。Wikipedia のリダイレクトはこれを直接教えてくれる。

    work では実データで9組（ナナシス / デレマス / 学マス / シャニマス / まどマギ /
    よりもい / ガルパン / 俺妹 / ボーカロイド）。いずれも単独値で、この辺が無いと
    クラスタにならず後段の LLM にも人間にも届かない。

    採るのは Wikipedia の `redirect` と MusicBrainz の `alias` の2種別だけ。
    どちらも「出典が同一のものへの別名として登録している」という同じ意味なので、
    辺の種別名も `redirect` に揃えてある（KIND_STRENGTH に別名を2つ持たせて
    強度が同じ、という状態のほうが読みにくい）。artist では
    「40メートルP」→「40mP」の1組がこれで拾える。

    **MusicBrainz の `search` は採らない。** 表記が完全一致しなかったときの
    「検索でスコア100だったが実際の表記はこちら」というヒットで、artist 266値の
    裏取り結果では raw と title が食い違う23組のうち12組が、ユーザーが明示的に
    禁じた統合になる:

        ふうり from STAR☆ANIS          → STAR☆ANIS      （from の分解）
        わか・ふうり・すなお from …     → STAR☆ANIS      （同上・6組）
        May'n・中島愛                  → 中島愛          （合同名義の分解）
        supercell/やなぎなぎ           → やなぎなぎ      （同上）
        長門有希(茅原実里)             → 茅原実里        （キャラ名義→声優本人名義）

    これで新しく拾えるのは「cametek」→「かめりあ」など4組しかなく、害のある側の
    ほうが行数も多い（STAR☆ANIS 系だけで8行以上）。`search` の結果は evidence
    として api_results に載り LLM とレビュー GUI の両方に届くので、辺にしなくても
    判断材料としては失われない。

    別名の先が実データに存在する値のときだけ張る。存在しない正式名称
    （「ブルアカ」→「ブルーアーカイブ」だが後者がデータに無い。artist なら
    「May'n」→「May’n」）は、統合相手がいないので候補にする意味が無い。
    """
    edges: list[Edge] = []
    for raw, hits in evidence.items():
        if raw not in values:
            continue
        for hit in hits:
            if hit.get("kind") not in ("redirect", "alias"):
                continue
            target = str(hit.get("title", "")).strip()
            if not target or target == raw or target not in values:
                continue
            if frozenset((raw, target)) in keep_apart:
                continue
            edges.append(Edge(raw, target, "redirect"))
            break  # 最初の別名だけ見る。2つ目以降は候補が増えるだけ
    return edges


def cooccurrence_edges(
    plays: list[dict], values: dict[str, Value], keep_apart: set[frozenset[str]]
) -> list[Edge]:
    """辺6: 同じ曲・同じ元ネタなのにアーティスト表記が割れている組。

    「AKINO」と「AKINO from bless4」、「kz(livetune)」と「livetune」のように、
    文字列の類似度では絶対に捕まらない組がここで出る。実データで 40 組。
    """
    by_song: dict[tuple[str, str], set[str]] = defaultdict(set)
    for p in plays:
        artist = (p.get("a") or "").strip()
        if not artist or artist not in values:
            continue
        title = rules.agg_key(p["t"])
        work = rules.agg_key(p.get("w") or "")
        if not title:
            continue
        by_song[(title, work)].add(artist)

    edges: list[Edge] = []
    seen: set[frozenset[str]] = set()
    for artists in by_song.values():
        if len(artists) < 2:
            continue
        ordered = sorted(artists)
        for i in range(len(ordered)):
            for j in range(i + 1, len(ordered)):
                pair = frozenset((ordered[i], ordered[j]))
                if pair in seen or pair in keep_apart:
                    continue
                seen.add(pair)
                edges.append(Edge(ordered[i], ordered[j], "cooccur"))
    return edges


# ---------------------------------------------------------------------------
# 連結成分
# ---------------------------------------------------------------------------


class _Union:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def components(edges: list[Edge]) -> list[list[str]]:
    uf = _Union()
    for e in edges:
        uf.union(e.a, e.b)
    groups: dict[str, list[str]] = defaultdict(list)
    for node in uf.parent:
        groups[uf.find(node)].append(node)
    return [sorted(g) for g in groups.values()]


# 辺の信頼度。小さいほど強い。割れないクラスタを弱い順に緩めていくのに使う。
KIND_STRENGTH = {
    "caseonly": 0,  # 大小・空白だけの差。ほぼ確実に同じもの
    "redirect": 1,  # 外部 API が「同じものへの別名」と言っている
    "agg": 2,  # 注記を剥がしたら一致
    "cooccur": 3,  # 同じ曲・同じ元ネタでアーティストだけ割れている
    "edit": 4,  # 綴りが近い（タイポ）
    "bigram": 5,  # 文字の重なりが多い
    "substr": 6,  # 片方が片方に含まれる。最も弱い
}


def components_capped(edges: list[Edge], limit: int) -> list[tuple[list[str], bool]]:
    """連結成分を作る。大きすぎる塊は弱い辺から落として割り直す。

    部分一致（substr）は数珠つなぎを作りやすい。「ボカロ」⊂「ボカロ/はるまきごはん」
    ⊃「はるまきごはん」…と辿れてしまい、実データでは 60 種・275 行の塊ができた。
    「喜多村英梨」⊂「轟八千代(CV:喜多村英梨)」で別人の声優が繋がる例もある。
    そのままでは LLM にも人間にも渡せる単位ではないので、強い根拠だけで割り直す。

    辺を落として孤立した値はクラスタから外れ、単独値に戻る。これは意図した挙動で、
    「弱い根拠でしか繋がらなかったもの」は候補に上げないほうが後段が楽になる。

    返すのは (値のリスト, 割り直しの結果か) の組。割った破片は「元は大きな塊の
    一部だった」という事実が判断材料になるので、印を落とさずに後段へ渡す。
    """
    out: list[tuple[list[str], bool]] = []
    stack = [(comp, max(KIND_STRENGTH.values()), False) for comp in components(edges)]
    while stack:
        members, strength, was_split = stack.pop()
        if len(members) <= limit or strength <= 0:
            out.append((sorted(members), was_split))
            continue
        member_set = set(members)
        allowed = {k for k, v in KIND_STRENGTH.items() if v < strength}
        sub = [
            e
            for e in edges
            if e.kind in allowed and e.a in member_set and e.b in member_set
        ]
        parts = components(sub)
        if len(parts) == 1 and len(parts[0]) == len(members):
            # 落としても割れなかった。もう一段弱い辺まで削る。
            stack.append((members, strength - 1, was_split))
        else:
            stack.extend((p, strength - 1, True) for p in parts)
    return out


# ---------------------------------------------------------------------------
# 既存の判断を読む
# ---------------------------------------------------------------------------


def load_keep_apart() -> set[frozenset[str]]:
    """統合してはいけないペア。まだファイルが無ければ空。

    生の表記そのものに加えて、注記を剥がしたキーの組も返す。「アイカツ!」と
    「アイカツスターズ」を分けたいのに、辺を1本消すだけでは
    「アイカツ! 楽曲」⊂「アイカツスターズ」の迂回路が残って同じ塊に戻ってしまう。
    キーの組で持てば「アイカツ!」「アイカツ! ED」「アイカツ! 楽曲」…の全部が
    まとめて遮断される。
    """
    path = paths.KEEP_APART_PATH
    if not path.exists():
        return set()
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    pairs: set[frozenset[str]] = set()
    for p in data.get("pair", []):
        a, b = p.get("a"), p.get("b")
        if not a or not b:
            continue
        pairs.add(frozenset((a, b)))
        ka = rules.agg_key(rules.strip_notes(a))
        kb = rules.agg_key(rules.strip_notes(b))
        if ka and kb and ka != kb:
            pairs.add(frozenset((ka, kb)))
    return pairs


def load_decided() -> set[str]:
    """判断済みの生値。却下したものが毎回出てくるとレビューが終わらない。"""
    path = paths.DECISIONS_PATH
    if not path.exists():
        return set()
    decided: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        decided.update(rec.get("variants", []))
    return decided


# ---------------------------------------------------------------------------
# 組み立て
# ---------------------------------------------------------------------------

FIELD_KEYS = {"work": "w", "artist": "a"}


def cluster_id(field_name: str, members: list[str]) -> str:
    """クラスタの中身から決まる ID。

    行数順の連番にすると block を実行するたびに振り直され、decisions.jsonl に
    記録した判断が別のクラスタを指してしまう。新しい開催回が増えれば行数は動くし、
    レビューの途中で候補を作り直すこともあるので、**中身が同じなら同じ ID** に
    なる必要がある。

    メンバーが1つでも変われば ID も変わるが、それは別のクラスタになったという
    ことなので正しい。判断済みかどうかは decisions.jsonl の variants でも
    見ているので、ID が変わっても却下した値が復活はしない。
    """
    digest = hashlib.sha256("\n".join(sorted(members)).encode("utf-8")).hexdigest()
    return f"{field_name}-{digest[:8]}"


def build(field_name: str, plays: list[dict]) -> dict:
    field_key = FIELD_KEYS[field_name]
    keep_apart = load_keep_apart()
    decided = load_decided()

    values = collect(plays, field_key)
    edges = build_edges(values, keep_apart)
    edges += redirect_edges(values, load_evidence(field_name), keep_apart)
    if field_name == "artist":
        edges += cooccurrence_edges(plays, values, keep_apart)

    by_pair: dict[frozenset[str], set[str]] = defaultdict(set)
    for e in edges:
        by_pair[frozenset((e.a, e.b))].add(e.kind)

    clusters: list[dict] = []
    for members, was_split in components_capped(edges, OVERSIZED):
        # 判断済みの値しか入っていないクラスタは出さない。
        if all(m in decided for m in members):
            continue
        rows = sum(values[m].rows for m in members if m in values)
        kinds: set[str] = set()
        pairs = []
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                pair = frozenset((members[i], members[j]))
                if pair in by_pair:
                    kinds |= by_pair[pair]
                    pairs.append(
                        {"a": members[i], "b": members[j], "kinds": sorted(by_pair[pair])}
                    )
        hints = []
        # 部分一致だけで繋がった組（辺4）に付く注意フラグ。work では
        # 「ブランド名を共有する同シリーズ作品」（統合してよい）と「無関係な語が
        # たまたま部分一致しただけ」（統合してはいけない）の両方がここに混ざるので、
        # どちらなのかは人間か LLM が中身を見て判断する必要がある。artist では
        # 「合同名義と単独名義の恐れ」という従来の意味のまま。
        if "substr" in kinds:
            hints.append("series-risk")
        # 続編の印（「2期」「劇場版」など）が食い違う値が混ざっている。
        # work は方針転換により「印が食い違う＝別物」ではなくなった（同じブランドの
        # シーズン違いはむしろ積極的に統合する）ので、このヒントはもう分離の根拠
        # ではない。それでも「どの表記がどのシーズンを指すか」は人間がレビュー GUI
        # で中身を把握する材料として引き続き有用なので、生成自体は変えない
        # （LLM 側の解釈は llm.py のプロンプトで変える）。
        if len({rules.series_marks(m) for m in members}) > 1:
            hints.append("series-mark-mismatch")
        if any(values[m].cross_field for m in members if m in values):
            hints.append("artist-as-work")
        # 部分一致の数珠つなぎでできた大きな塊を割った破片。元が繋がりすぎて
        # いたので、この中身も本当に同じものか疑ってかかる必要がある。
        if was_split:
            hints.append("split-from-large")

        clusters.append(
            {
                "id": cluster_id(field_name, members),
                "field": field_name,
                "rows": rows,
                "hints": hints,
                "edgeKinds": sorted(kinds),
                "values": [
                    values[m].to_json(with_base=field_name == "work")
                    for m in members
                    if m in values
                ],
                "edges": pairs,
            }
        )

    # 行数の多いものから見せる。上位のクラスタほど検索への効きが大きい。
    # ID は中身から決まるので、並べ替えても指すクラスタは変わらない。
    clusters.sort(key=lambda c: (-c["rows"], c["id"]))

    singles = len(values) - sum(len(c["values"]) for c in clusters)
    return {
        "field": field_name,
        "totalValues": len(values),
        "clustered": sum(len(c["values"]) for c in clusters),
        "singletons": singles,
        "clusters": clusters,
    }


def load_plays() -> list[dict]:
    with paths.WEB_DATA_JSON.open(encoding="utf-8") as fh:
        return json.load(fh)["plays"]


def write(field_name: str, result: dict) -> Path:
    paths.OUT_ALIASES_DIR.mkdir(parents=True, exist_ok=True)
    out = paths.OUT_ALIASES_DIR / f"clusters.{field_name}.json"
    with out.open("w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
    return out
