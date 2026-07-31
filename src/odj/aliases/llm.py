"""候補クラスタを LLM に見せて、統合の**提案**を作る。

    PYTHONPATH=src python3 -m odj.aliases ask --field work --dry-run
    PYTHONPATH=src python3 -m odj.aliases ask --field work
    PYTHONPATH=src python3 -m odj.aliases ask --field artist

`block` が作った out/aliases/clusters.<field>.json と、`fetch` が作った
out/aliases/evidence.<field>.json（無くてもよい）を読み、
data/aliases/_proposed/<field>s.toml に提案を書く。**辞書は書かない。**
人間が npm run review で1件ずつ判断して初めて works.toml / artists.toml に入る。

ここに LLM を入れる理由は1つだけで、**文字列類似では原理的に繋がらない組**が
実データにあるため。「デレマス」(agg_key: でれます) と
「アイドルマスターシンデレラガールズ」(あいどるますたーしんでれらがーるず) は
bigram でも編集距離でも部分一致でも一度も同じクラスタに入らない。block.py の
閾値をいくら緩めても届かず、緩めた分だけ別作品が混ざる。

**過剰統合を防ぐことが、表記ゆれを1つ拾うことより大事**という方針は、
プロンプトの文面だけでなく構造にも入れてある。

  - 出力の groups は**配列**。「1クラスタ = 1グループ」を強制しないので、
    LLM は入力3件を2グループに割れるし、1件も出さないこともできる。
    「全部同じですか?」と訊くと LLM は「はい」と答えたがるので、訊き方のほうを変えた
  - `approved` を JSON スキーマに入れない。LLM が承認済みを書くことが構造的に不可能
  - canonical は candidates か api_results にある文字列だけ（創作禁止）。
    書き出す前に Python 側でも検証し、違反した group は捨てる
  - keep_apart.toml の組をプロンプトに載せたうえで、返ってきたものも
    block.load_keep_apart() で再検査する（プロンプトは守られない前提で書く）

クラスタは1件ずつではなく、SAFE_INPUT_TOKENS まで詰めてバッチで投げる。
1リクエストごとに system_prompt の固定費が丸ごと乗るため、1件ずつ投げると
固定費をクラスタ数ぶん払うことになる。Gemini API には無料枠があるが、
**レート制限はモデルとティアで変わり、公式ドキュメントも具体値を載せずに
AI Studio で確認せよとしている**（なのでここにも数字は書かない）。
リクエスト数を減らしておけば、どのティアでも 429 待ちに当たりにくい。
実測（--dry-run）で work の 151 クラスタが 29 リクエスト、
artist の 105 クラスタが 34 リクエスト（work は「ブランド単位でまとめる」方針転換で
keep_apart の組を消した結果クラスタが繋がり、152 → 151 に減っている）。
artist はクラスタが少ないのにリクエストが多い。1クラスタが大きい
（`わか・ふうり・すなお from STAR☆ANIS` のような長い生表記が並ぶ）ことと、
system_prompt が field 固有の規則ぶん長い（work 1543tok に対して artist 2168tok）
ことの両方が効いている。プロンプトを足すときは system_prompt の説明を先に読む。

プロンプト全文の SHA256 で data/raw/llm/ にキャッシュするので、**入力が同じなら
再実行はネットワークに出ず、バイト単位で同じ結果**になる。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from odj import paths
from odj.aliases import block, rules, store
from odj.aliases.store import AliasError

# ---------------------------------------------------------------------------
# Gemini API（generateContent）
# ---------------------------------------------------------------------------

# 経緯: GitHub Models の無料枠 → 2026-07-30 に廃止（410）→ OpenAI 直叩き →
# OpenAI アカウントが insufficient_quota で叩けず Gemini へ。
#
# **定数ではなくテンプレートなのは、Gemini がモデル ID を URL パスに置くため。**
# OpenAI 互換の chat/completions（本文の "model" でモデルを指定する）と構造が
# 一番違うのがここ。組み立ては endpoint_for() が持つ。
#
# Gemini の OpenAI 互換エンドポイント（/v1beta/openai/）を選ばなかった理由:
# あちらは beta で「**リストにないパラメータは黙って無視する**」と公式に
# 書かれている。responseSchema が無視されて素の文章が返ると parse_groups は
# JSON として読めずバッチを丸ごと捨てるので、黙って落ちる形になる。
ENDPOINT_TEMPLATE = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)


def endpoint_for(model: str) -> str:
    """そのモデルの generateContent の URL。

    API キーはクエリ ?key= でも渡せるが、**URL に鍵が入るとシェルの履歴や
    プロキシのログに残る**ので、post() は x-goog-api-key ヘッダで渡す。
    ここに鍵を足さないこと。
    """
    return ENDPOINT_TEMPLATE.format(model=model)


# プロバイダ接頭辞は付けない（URL パスにそのまま入るため、余計な "/" が混ざると
# 別のパスを叩きにいって 404 になる）。
#
# 出力が途中で切れることを一番警戒して選んでいる。ここは responseSchema で
# 構造化 JSON を強制しており、maxOutputTokens に当たって切れる
# （finishReason="MAX_TOKENS"）と parse_groups がバッチまるごと諦める。
# 出力トークンを思考にも使うモデルを既定に置くとその枠を思考が食うので、
# 素直に出し切るほうに寄せて flash 系を既定にしてある。
# --model で差し替えられるので、試すときはそちらで。
DEFAULT_MODEL = "gemini-3.6-flash"

# 1リクエストに詰める入力量の上限。**API が強制する上限ではなく、こちらが選んで
# いる保守的なバッチサイズ**（Gemini のコンテキスト長はこれよりはるかに大きい）。
# 由来は GitHub Models の無料枠が入力 4000tok だったこと。
#
# 上げれば1リクエストあたりのクラスタ数が増えてリクエスト数を減らせるが、
# **MAX_CLUSTERS_PER_CALL と MAX_OUTPUT_TOKENS も一緒に見ないと出力側で答えが
# 切れる**（1クラスタ ≒ 出力 200tok なので、入力だけ倍にすると出力が足りない）。
# もう1つ、**上げるとプロンプト全文の SHA256 が変わってキャッシュ
# （data/raw/llm/）が全部無効になる**。動かす価値があるときだけ動かすこと。
INPUT_TOKEN_LIMIT = 4000

# 実際に詰めてよい量。上限ちょうどを狙うと、推定の誤差ぶんだけリクエストが
# まるごと無駄になる。1割の余裕を持たせてある。
SAFE_INPUT_TOKENS = 3600

MAX_OUTPUT_TOKENS = 4000

# 1リクエストに詰めるクラスタ数の上限。入力に収まっても、1クラスタ ≒ 出力 200tok
# なので、これを超えると出力側の 4000tok を超えて答えが途中で切れる。
MAX_CLUSTERS_PER_CALL = 18

# pack_batches が入る限り詰めるので、この値は --batch-size の既定でしかない。
BATCH_SIZE = 6

# ---------------------------------------------------------------------------
# プロンプトに載せる量の上限
# ---------------------------------------------------------------------------

# 共起の例（DJ 名・曲名・アーティスト名）をいくつ渡すか。block.py が既に 4 件に
# 絞っているのを更に 3 件にする。多いほうが判断材料は増えるが、上位バッチ
# （行数の多いクラスタが集まる）が 8k を超えるほうが困る。
SAMPLE = 3

# 辺をペアごとに列挙する上限。12 種のクラスタは 66 ペアになり、それだけで
# 1500 字を食う。超えたら種別の集合だけを渡す。
EDGE_MAX = 12

# 1つの生表記あたりの外部 API のヒット数と、note の長さ。
EVIDENCE_MAX = 3
NOTE_MAX = 100

# ---------------------------------------------------------------------------
# 出力スキーマ
# ---------------------------------------------------------------------------

# work だけが持つ2項目。**artist のスキーマからは落としてある。**
#
# strict な json_schema は properties を全部 required に入れる必要があり、置いたままに
# すると LLM は artist でも必ず series と kind を埋めることになる。kind の enum は
# work / vocaloid / vtuber / odj-self / artist-as-work / unknown で、アーティスト名を
# どれかに分類させても意味が無いうえ、その値は to_entry → artists.toml → aliases.json の
# `k` まで素通りする。**「どれでもないので unknown」を毎回書かせるより、訊かないほうが
# 短くて嘘が混ざらない。**
#
# 他の2か所も既にそうなっている。store._ARTISTS_HEADER が説明している列は
# canonical / variants / approved / confidence / reason だけで series と kind は
# 無く、レビュー GUI（web/src/review/ClusterCard.tsx）も field === 'work' のときしか
# kind を送らない。ここだけ訊いていると、LLM の提案には kind があるのに人間が
# 承認した行には無い、という食い違いが artists.toml に残る。
_WORK_ONLY_PROPERTIES: dict[str, Any] = {
    "series": {"type": "string"},
    "kind": {
        "type": "string",
        "enum": ["work", "vocaloid", "vtuber", "odj-self", "artist-as-work", "unknown"],
    },
}


def response_schema(field: str) -> dict[str, Any]:
    """LLM に強制する出力スキーマ。generationConfig.responseSchema にそのまま入る。

    **approved はここに無い。** LLM が承認済みを書く手段が存在しないことが、
    「未承認のものが公開データに出ない」の一番外側の担保になっている。

    OpenAI 時代の {"type":"json_schema","json_schema":{"name":…,"strict":True,
    "schema":…}} というラッパーは Gemini には無い。**スキーマ本体だけ**を返す
    （名前を response_format から改めたのはこのため。返すものが別物になった）。
    strict に当たる指定も無く、**additionalProperties も使えない**。実際に投げて
    400 が返って分かった:
        Unknown name "additionalProperties" at 'generation_config.response_schema':
        Cannot find field.
    generateContent の responseSchema は OpenAPI のサブセットで、JSON Schema の
    全部が通るわけではない（type / properties / required / items / enum は通る）。
    **ここに additionalProperties を足し直すと全リクエストが 400 で落ちる。**

    外しても「LLM が approved を書けない」担保は変わらない。to_entry() が既知の
    キーだけを明示的に組み立てていて未知のキーを写さないうえ、
    store.write_proposals() が approved を含む提案を例外で弾く。スキーマは
    3枚あるうちの1枚でしかなく、それも一番外側ではない。
    required は通るので「全項目を必ず埋めさせる」ほうは維持できている。

    field で分けているのは series / kind の2項目だけ（_WORK_ONLY_PROPERTIES の
    説明を参照）。スキーマは cache_key に含まれるので、ここを変えると
    data/raw/llm/ のキャッシュは無効になる。
    """
    properties: dict[str, Any] = {
        "cluster_id": {"type": "string"},
        "canonical": {"type": "string"},
        **(_WORK_ONLY_PROPERTIES if field == "work" else {}),
        "variants": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "reason": {"type": "string"},
    }
    return {
        "type": "object",
        "required": ["groups"],
        "properties": {
            "groups": {
                "type": "array",
                "items": {
                    "type": "object",
                    # required は properties と揃える。省略されうる項目を作ると、
                    # 「series が無い提案」が work 側に混ざって to_entry が
                    # 空文字で埋めることになる。並びも properties のまま。
                    "required": list(properties),
                    "properties": properties,
                },
            }
        },
    }


# ---------------------------------------------------------------------------
# 入力を読む
# ---------------------------------------------------------------------------


def clusters_path(field: str) -> Path:
    return paths.OUT_ALIASES_DIR / f"clusters.{field}.json"


def evidence_path(field: str) -> Path:
    return paths.OUT_ALIASES_DIR / f"evidence.{field}.json"


def load_clusters(field: str) -> list[dict]:
    path = clusters_path(field)
    if not path.exists():
        raise AliasError(
            f"候補クラスタがありません: {store.rel_to_repo(path)}"
            "（先に odj.aliases block --field " + field + " を実行してください）"
        )
    with path.open(encoding="utf-8") as fh:
        return list(json.load(fh).get("clusters", []))


def load_evidence(field: str) -> dict[str, list[dict]]:
    """外部 API の裏取り結果。**無くても動く。**

    fetch は 2 回以上出る値しか引かないので、キーが無い値は「引いていない」、
    空配列は「引いたが見つからなかった（タイポ疑い）」を意味する。この2つは
    LLM にとって意味が違うので、プロンプトでも区別して渡す。
    """
    path = evidence_path(field)
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        # 別プロセスが書いている途中の可能性がある。evidence は無くても
        # 提案は作れるので、ここで落とさない。
        return {}
    raw = data.get("evidence")
    if not isinstance(raw, dict):
        return {}
    return {k: list(v) for k, v in raw.items() if isinstance(v, list)}


# ---------------------------------------------------------------------------
# プロンプトを組む
# ---------------------------------------------------------------------------


def _trim(items: Any, limit: int) -> list[str]:
    return [str(x) for x in list(items or [])[:limit]]


def pack_cluster(cluster: dict, evidence: dict[str, list[dict]]) -> dict:
    """クラスタ1つ → LLM に見せる形。

    events（開催回の番号）は落としている。同一性の判断材料としてはほとんど効かない
    のに、14 回ぶんの数字が全クラスタに付くとバッチが 1 割膨らむため。
    出典の追跡は decide 側が clusters.json から `where` を組み立てるので、
    LLM に渡す必要が無い。
    """
    candidates = []
    for value in cluster.get("values", []):
        raw = value.get("raw", "")
        item: dict[str, Any] = {"raw": raw, "rows": value.get("rows", 0)}
        if value.get("djs"):
            item["djs"] = _trim(value["djs"], SAMPLE)
        if value.get("coTitles"):
            item["coTitles"] = _trim(value["coTitles"], SAMPLE)
        if value.get("coArtists"):
            item["coArtists"] = _trim(value["coArtists"], SAMPLE)
        if value.get("coWorks"):
            item["coWorks"] = _trim(value["coWorks"], SAMPLE)
        if value.get("crossField"):
            item["alsoAnArtistName"] = True
        candidates.append(item)

    packed: dict[str, Any] = {
        "cluster_id": cluster.get("id", ""),
        "hints": list(cluster.get("hints", [])),
        "candidates": candidates,
    }

    edges = cluster.get("edges", [])
    if len(edges) <= EDGE_MAX:
        # [a, b, "agg,bigram"] の3要素。キー名を繰り返すより短い。
        packed["edges"] = [
            [e.get("a", ""), e.get("b", ""), ",".join(e.get("kinds", []))] for e in edges
        ]
    else:
        packed["edgeKinds"] = list(cluster.get("edgeKinds", []))

    api: dict[str, list[dict]] = {}
    for value in cluster.get("values", []):
        raw = value.get("raw", "")
        if raw not in evidence:
            continue  # 引いていない値。空配列（＝引いて見つからなかった）と区別する
        # 同じ title のヒットは1件に畳む。Wikidata の検索は「アイカツ!」に対して
        # Q729857 / Q6560785 / Q113674971 を返し、title は3つとも「アイカツ!」に
        # なる（フランチャイズ・一覧記事・アニメ）。判断材料は増えないのに
        # 入力だけ3倍になるので、最初の1件（説明が一番具体的なもの）だけ残す。
        hits = []
        seen: set[str] = set()
        for hit in evidence[raw]:
            title = str(hit.get("title", ""))
            if title in seen:
                continue
            seen.add(title)
            item = {
                "source": hit.get("source", ""),
                "title": title,
                "kind": hit.get("kind", ""),
            }
            # リダイレクトの note は「「ナナシス」は Tokyo 7th シスターズ への
            # リダイレクト」で、raw と title と kind から完全に導ける。
            # 検索ヒットの説明は残す — 「ユーフォ」に対する「未確認飛行物体」
            # 「トウダイグサ属」のように、引きが外れていることの手掛かりになる。
            note = str(hit.get("note", ""))
            if hit.get("kind") != "redirect" and note:
                item["note"] = note.removeprefix("Wikidata 検索: ")[:NOTE_MAX]
            hits.append(item)
            if len(hits) >= EVIDENCE_MAX:
                break
        api[raw] = hits
    if api:
        packed["api_results"] = api
    return packed


def evidence_titles(cluster: dict, evidence: dict[str, list[dict]]) -> set[str]:
    """このクラスタについて API が返した正準名の候補。

    canonical はここか candidates からしか選べない。「ナナシス」を
    「Tokyo 7th シスターズ」に寄せたいが、その正式名称は plays.json のどこにも
    無いので、variants だけに限ると一番効く提案ができなくなる。
    """
    out: set[str] = set()
    for value in cluster.get("values", []):
        for hit in evidence.get(value.get("raw", ""), []):
            title = (hit.get("title") or "").strip()
            if title:
                out.add(title)
    return out


def keep_apart_lines(values: set[str] | None = None) -> list[str]:
    """keep_apart.toml を「A ≠ B」の行に展開する。

    store.load_keep_apart_pairs() を使う（block.load_keep_apart() のほうは
    注記を剥がした内部キーまで膨らませるので、そのまま見せると
    「あいどるますたーしんでれらがーるず」のような読めない文字列が並ぶ）。
    検証には block 側の膨らませた集合を使い、見せるのは生の組だけ、と分けている。

    values を渡すと、**その値に関係する組だけ**に絞る。組を全部載せるとそれだけで
    1400tok 使い、入力上限 4000 に対して固定費が重すぎるため。絞っても防止力は
    落ちない。プロンプトに出さなかった組も、返ってきた提案は
    block.load_keep_apart() で必ず再検査する（プロンプトは守られない前提で書き、
    守られなかったときに落ちる場所を Python 側に置く、という方針は変えない）。

    関係するかどうかは注記を剥がしたキーでも見る。「アイカツ! 楽曲」と
    「アイカツスターズ」のような迂回路が実データにあり、生の文字列だけを
    突き合わせると取りこぼす。

    **work のバッチではここが空になるのが普通。** ブランド単位でまとめる方針転換に
    伴って keep_apart.toml から work の 26 組を削り、残っているのは artist の組だけ
    （`LiSA` と `ELISA` のような別人）。関数の側は field を見ないので、work に組が
    戻ってくれば今のまま載る。
    """
    lines = []
    keys = None
    if values is not None:
        keys = {rules.agg_key(rules.strip_notes(v)) for v in values} | {
            rules.agg_key(v) for v in values
        }
        keys.discard("")
    for pair in store.load_keep_apart_pairs():
        a, b = (pair.get("a") or "").strip(), (pair.get("b") or "").strip()
        if not (a and b):
            continue
        if keys is not None and not _touches(a, keys) and not _touches(b, keys):
            continue
        lines.append(f"- 「{a}」 ≠ 「{b}」")
    return lines


def _touches(name: str, keys: set[str]) -> bool:
    """その表記が、バッチに出てくる値のどれかと同じものを指しているか。"""
    return rules.agg_key(rules.strip_notes(name)) in keys or rules.agg_key(name) in keys


def cluster_values(clusters: list[dict]) -> set[str]:
    """バッチに出てくる生表記を全部集める。keep_apart の絞り込みに使う。"""
    return {
        v.get("raw", "")
        for c in clusters
        for v in c.get("values", [])
        if v.get("raw")
    }


_FIELD_LABEL = {
    "work": ("元ネタ名（アニメ・ゲーム等の作品名や、ボカロ・VTuber などのジャンル）", "作品"),
    # artist は「アーティスト名の欄」ではなく**何でも入る欄**である。実データ 353 種の
    # 内訳が、個人・グループ・キャラクター名義・声優名・合同名義・リミキサー名で、
    # 「アーティスト名」とだけ言うと LLM が人間の音楽アーティストを前提にしてしまう
    # （`大槻唯(CV:山下七海)` や `わか・ふうり・すなお from STAR☆ANIS` が普通に来る）。
    "artist": (
        "アーティスト名（個人・グループ・キャラクター名義・声優名・合同名義が混在する欄）",
        "アーティスト",
    ),
}

# field ごとに差し替える部分。共通の骨格（絶対規則の並びと番号、規則2・3・4の前半、
# 規則5、入力の読み方の大枠、出力の形）は system_prompt の f-string 側に1つだけ持ち、
# **中身が field で本当に変わる箇所だけ**をここに出してある。work と artist で丸ごと
# 2本の文面を持つと、一番効く規則が片方だけ直されて必ず乖離する。
#
# 規則1と4は **field で判断の向きが逆になる**（下の work の rule1 / rule4 を参照）が、
# それでも文ごと2本に分けてはいない。骨格の文は f-string 側に残し、向きが決まる
# 結論の一文だけを rule1 / rule4 として差し替えている。
#
# work 側の値は、**同じブランド名を冠する作品はすべて1つの work にまとめる**という
# 方針転換（利用者の指示。「同じシリーズのシーズン1と2などを区別する必要はない」）に
# 合わせて書き直した。それまでは逆に「1期と2期、ブランドが同じだけの別タイトルは
# 別物」と書いていた。同時に data/aliases/keep_apart.toml から work のペア 26 組
# （シーズン/ブランドの分離指示）を全削除しているので、**work のバッチでは keepApart が
# ほぼ常に空**になる。
# この書き直しでリクエスト本文の SHA256 が変わり、data/raw/llm/ のキャッシュは
# work のぶんが全部無効になる（Actions 上は data/raw/ が gitignore で常にコールドなので
# 実害は無いが、ローカルの再実行は 29 リクエストぶんネットワークに出る）。
_FIELD_TEXT: dict[str, dict[str, str]] = {
    "work": {
        # 規則1の結論。work は**ブランド単位でまとめる**方針なので「迷ったら分ける」を
        # そのまま読ませると逆に働く。続く「1つのクラスタを複数のグループに割ってよく…
        # groups は空配列でもよい」は骨格側に残してあり、方針転換後も生きている
        # （ブランドも系統も無関係な値が混ざったクラスタからは何も出さなくてよい）。
        "rule1": "**同じブランドなら迷わずまとめる。**",
        # 規則2の後半。「keepApart に挙がっていなくても」に続く。
        # 分ける根拠として残るのは substr や略語で偶然繋がった組だけ。「ひだまり」は
        # 曲名の可能性があり（_proposed/works.toml で実際に分離された）、
        # 「ラブライブ」と「ラブライバー」はファンの呼称で作品ではない。
        "rule2": """ブランドも系統も無関係で
   文字列が偶然似ているだけの組は分けます（「ひだまり」と「ひだまりスケッチ」、
   「ラブライブ」と「ラブライバー」）。逆に**同じブランド名を冠するものは、続編・
   シーズン違い・スピンオフ・劇場版まで全部1つ**です。""",
        # 規則3の後半。「創作は禁止…と考えて書いてはいけません。」に続く。
        # ブランド単位でまとめる方針では、api_results の redirect 先である
        # 「アイドルマスターシリーズ」「涼宮ハルヒシリーズ」のようなシリーズ記事名が
        # canonical としてむしろ適切になる（実例は _proposed/works.toml の
        # 「アイマス」「涼宮ハルヒの憂鬱」）。ただし規則3の「実際にある文字列から
        # 選ぶ」は変わらないので、「アイカツ!シリーズ」のような**無い**シリーズ名を
        # 組み立てさせてはいけない（check_group が弾くが、弾けば提案が丸ごと消える）。
        "rule3": """api_results にある正式名称やシリーズ記事名を優先します
   （「ナナシス」→「Tokyo 7th シスターズ」、「アイマス」→「アイドルマスターシリーズ」）。
   無ければ rows の多い表記（api_results に無いシリーズ名は作らない）。一覧記事や
   関連商品（「〜の楽曲一覧」「〜 Solo Collection」）は canonical にしないこと。""",
        # 規則4の後半。work では「同じブランドだから」が正当な根拠に変わった
        # （artist 側は現状のまま「同じシリーズだから」を却下する）。引用の義務は
        # 骨格側に残してあるので、ここで免除しないことだけ言っておく。
        "rule4": "「同じブランドだから」も理由になりますが、引用は必須です。",
        # field 固有の節。work には無い（もともと全体が work 向けに書かれている）。
        "extra": "",
        # 共起（coTitles / coArtists）の読み方。骨格に置いていた「曲名が1つも重ならず
        # アーティストの系統も違うなら別作品を疑う」は、ブランド単位でまとめる方針と
        # 正面から衝突する。1期と2期・無印とスピンオフは曲が1つも重ならないのが普通で
        # （「機動戦士ガンダム」と「水星の魔女」に共通の曲は無い）、そのまま読ませると
        # 規則2に反して分けにいく。work では否定側の推論を落としてある。
        "cooccur": """**曲名が重ならないことは分ける根拠になりません**
  （シーズン違いは曲が別）。""",
        # edges の agg の説明。「agg=」と末尾の「 / 」まで入れてあるのは、
        # work 側の改行位置を1文字も動かさないため（下の artist と説明の長さが違う）。
        "agg": """agg=注記
  (OP・ED・楽曲・TVアニメ「」)を剥がすと一致 / """,
        # block.py はこの2つを今後も出す（人間のレビュー GUI 向けの情報として残す）が、
        # ブランド単位でまとめる方針では**分離の根拠にならなくなった**。
        # series-mark-mismatch（「2期」等の印の食い違い）は同じブランド内のシーズン違いを
        # 示すだけ。series-risk（substr だけで繋がった組）は依然として注意が要るが、理由が
        # 「シリーズの別作品の恐れ」から「同シリーズ作品と無関係な語の部分一致の両方が
        # あり得る」に変わった。2つで言うことが同じになったので、1項目に畳んである。
        "hints": """series-risk / series-mark-mismatch=部分一致だけで繋がった・
  「2期」等の印が食い違う(同じブランドなら統合してよい。無関係な語の部分一致だけ注意) /
  split-from-large=繋がりすぎた塊の破片(中身を疑う) /
  artist-as-work=元ネタ欄にアーティスト名""",
        "api": """外部 API の裏取り。**空配列は「引いたが記事が無かった」**で
  タイポか通称のシグナル。キーごと無い値は引いていないだけなので根拠にしない""",
        # 出力の series / kind。artist では response_schema() のスキーマから
        # 落としてあるので、ここで説明すると書けない項目を求めることになる。
        #
        # series の使われ方は1つだけで、**検索の的を広げること**である。
        # store.export_json が同値クラスの `s` に入れ（store.py の 444-447 行）、
        # web/src/lib/data.ts が haystack に同値クラス全体 + series 名を足す。
        # ブランド単位でまとめると canonical 自体がブランド名になることが多く、
        # そのとき series を埋めても haystack に同じ語が2度入るだけで効かない。
        # 「canonical と違う語のときだけ書く」に振り直してある。
        "output": """
- series … canonical と違うシリーズ名。無ければ空文字
- kind … work=作品 / vocaloid=ボカロ曲 / vtuber=VTuber / odj-self=大会自体のネタ /
  artist-as-work=元ネタ欄にアーティスト名 / unknown=判断できない""",
    },
    "artist": {
        # work とは逆で、artist は**過剰統合のほうが危険**な欄のまま。ここは
        # 方針転換の対象外で、従来の文面をそのまま持っている。
        "rule1": "**迷ったら「分ける」。** 統合は不可逆で情報が失われ、分離は後から可逆です。",
        # 最も危険なのは「綴りが近いだけの別人」。規則2（絶対に統合しない）の側に
        # 置いてあるのは、下の固有規則より上に読ませたいため。
        "rule2": """綴りが1〜2文字
   違うだけの別アーティスト（LiSA と ELISA、HALCALI と halca、Ray と Ray Volpe、
   スピカ と スピラ・スピカ）も別物です。**綴りが近いことは統合の根拠になりません。**""",
        # work と違い「API の正式名称を優先」してはいけない。MusicBrainz の検索は
        # 必ず何かを返すので、そのまま優先させると Ray → Ray Charles を正準に採る。
        "rule3": """基本は rows の多い表記を選びます。
   api_results[].title を canonical にしてよいのは、raw との差が大小・空白・記号
   だけのとき（`Claris` より `ClariS`）に限ります。""",
        # work は「同じブランドだから」を根拠として認めるようになったが、artist では
        # 認めない。`AKINO with bless4` と `AKINO` は同じ系統だが別名義である。
        "rule4": "「一般的にそう呼ばれるため」「同じシリーズだから」は理由として認められません。",
        # 4項目に畳んである。1項目ずつ節に分けたほうが読みやすいが、この節は全バッチに
        # 乗る固定費で、長くするとリクエスト数が直に増える（system_prompt の説明を参照）。
        # 挙げる実例も、実データで実際に踏んだ組を1〜2個までに絞ってある。
        "extra": """
## この欄に固有の規則（実データ 353 種を確認して決めた方針）

A. **合同名義は第3の名義。分解しない。** `feat.` `×` `&` `with` `from` で複数の名前が
   並ぶ表記は、含まれるどの単独名義とも別物です（`AKINO with bless4` ≠ `AKINO`、
   `TAKU INOUE・DECO*27` はどちらでもない）。連名とその中の1人も別。**この欄では
   構造を分解せず、表記の統一だけを行います。**

B. **キャラクター名義と声優本人は別**（`長門有希(茅原実里)` ≠ `茅原実里`）。逆に
   **同じキャラクター名**なら `CV:` や声優名の注記の有無・連名の区切り（`、` `・` `,`）
   だけの違いは表記ゆれ（`大槻唯` / `大槻唯(CV:山下七海)`）。`(CV:声優名)` の書式が
   揃っているだけでは根拠にならず、同じ声優が複数のキャラを演じるため
   `千石撫子(花澤香菜)` と `小野寺小咲(花澤香菜)` も別物です。

C. **大小・空白・記号・不可視文字の差だけなら同一で confidence="high"**
   （`ClariS`/`Claris`、`sasakure.UK`/`sasakure.‌UK`=ゼロ幅文字の混入）。タイポも
   統合してよい（`BUMP OF CHIKEN`、`黄緑色社会`=緑と黄が逆）。api_results の**空配列**は
   タイポのシグナルですが、MusicBrainz 未登録の同人アーティストや VTuber でも空になる
   ので単独の根拠にはしないこと。

D. **cooccur はこの欄では弱い。** リミックスやカバーは原曲と同じ曲名でアーティスト欄が
   リミキサー名になり、ボカロ曲は行によって作者名（`kz(livetune)`）と歌唱ボカロ
   （`初音ミク`）のどちらが入るかが違います。cooccur だけの組は、文字列がほぼ同じで
   タイポか大小差と言える場合を除いて統合しないこと。混ざるリミックス表記
   （`YUPPUN Remix`）は、同じ人かを判断せず confidence="low" で人間に回してください。
""",
        # agg 辺は artist では注記剥がしではなく agg_key の一致で張られる
        # （`じん`/`ジン`、`μ's`/`μ′s`）。work 向けの「OP・ED を剥がすと一致」を
        # そのまま見せると、**この欄で一番当たる辺**を LLM が読み違える。
        "agg": """agg=記号・全角半角・カタカナとひらがなを
  均すと一致(`じん`と`ジン`、`μ's`と`μ′s`) / """,
        # series-mark-mismatch と artist-as-work は work 向けの説明が意味を成さない。
        # 実データでは `AKINO with bless4` の 4 や `96猫` の数字で series-mark-mismatch
        # が付き、ボカロP・VTuber が元ネタ欄にも出ることで artist-as-work が付く。
        # どちらも危険信号ではないと言わないと、LLM が過剰に分ける側へ倒れる。
        "hints": """series-risk=部分一致だけで繋がった(合同名義と
  単独名義の恐れ) / split-from-large=繋がりすぎた塊の破片(中身を疑う) /
  series-mark-mismatch と artist-as-work はこの欄では意味が薄い（`bless4` の数字や、
  ボカロP が元ネタ欄にも出ることで付く。危険信号ではない）""",
        # 共起の読み方。artist は従来のまま（「別アーティスト」まで含めて1文字も
        # 変えていない）。この欄では綴りが近いだけの別人が一番危ないので、曲名も
        # アーティストの系統も重ならないことは疑う理由として生きている。
        "cooccur": """**曲名が1つも重ならずアーティストの系統も違うなら
  別アーティストを疑う**。""",
        # 「それ以上違うとき」の内訳（合同名義の親名義・キャラ名から片方だけ・誤ヒット）は
        # 固有規則 A / B と重なるので、ここでは例を1つずつに絞ってある。
        "api": """MusicBrainz。kind=alias は「別名として登録」の明示で最も
  強い根拠。kind=search は**曖昧検索で必ず何かが返る**のでヒット自体は根拠にならず、
  title と raw の差が大小・空白・記号だけのときだけ正式表記です（それ以上違うのは
  合同名義の親名義 `… from STAR☆ANIS`→`STAR☆ANIS` か、無関係な誤ヒット
  `Ray`→`Ray Charles`）。キーごと無い値は引いていないだけ""",
        "output": "",
    },
}


def system_prompt(field: str) -> str:
    """システムプロンプト。全バッチで共通の部分だけ。

    以前は keep_apart.toml の 26 組をここに全展開していたが、それだけで 1400tok
    使い、入力上限 4000 に対して固定費が重すぎた。組は各バッチの入力側へ移し、
    そのバッチに関係するものだけを載せる（keep_apart_lines の説明を参照）。
    要約して一般則にはしない。artist で「綴りが近い L 始まりは分ける」に化けると、
    逆に `LiSA` の表記ゆれまで分かれる。

    **ここが長くなるとリクエスト数が増える。** 全バッチに乗る固定費なので、
    1バッチに詰められる量は SAFE_INPUT_TOKENS からこの長さを引いた残りになる。
    artist で実測すると、work と同じ文面（当時 1447tok）のままなら 22 リクエスト、
    固有規則を書きたいだけ書いた 2577tok では **50 リクエスト**まで膨らみ、
    削って 2168tok / 34 リクエストに落としてある。100tok につき 2 リクエストほど
    増える勘定。規則を1つ足すときは、実データで実際に踏んだ組を1〜2個挙げるだけに
    して、一般論や他の規則と重なる説明は書かないこと。

    2168tok は「どのバッチも SAFE_INPUT_TOKENS に収まる」上限でもある
    （artist で一番大きいクラスタが単独で 1363tok あり、これ以上固定費が増えると
    そのクラスタだけ 1件で枠を超える。pack_batches は1件だけのバッチを割れない）。
    """
    label, thing = _FIELD_LABEL[field]
    text = _FIELD_TEXT[field]
    return f"""\
あなたはオタクDJ大会のプレイログDBの表記ゆれを整理する助手です。対象は{label}。
目的は**検索で確実に曲を見つけられるようにすること**で、{thing}の分類ではありません。

入力は機械が文字列の類似だけで集めた候補です。**別の{thing}が同じクラスタに
入っているのが普通**なので、本当に同じものを指す表記だけをグループにしてください。

## 絶対規則

1. {text["rule1"]}
   1つのクラスタを複数のグループに割ってよく、まとめる根拠が無い値はどのグループにも
   入れなくてよい。まとめられるものが1つも無いクラスタからは、グループを1つも
   出さないこと（groups は空配列でもよい）。全クラスタに答えを出す必要はありません。

2. 入力の keepApart に挙げた組は、実データを突き合わせて別物と確認済みです。
   **絶対に同じグループへ入れないこと。** そこに挙がっていなくても、{text["rule2"]}

3. canonical は、そのクラスタの candidates[].raw か api_results[].title に
   **実際にある文字列**からのみ選ぶこと。**創作は禁止**で、「正式名称はこうあるべき」
   と考えて書いてはいけません。{text["rule3"]}

4. reason には**与えられた材料を引用**すること。rows（行数）、djs、coTitles、
   coArtists、api_results の title か note のいずれかを必ず含めてください。
   {text["rule4"]}

5. **確信が持てなければ confidence="low"。** low の提案は公開データには出ませんが、
   人間のレビューには残るので、捨てずに low で出すほうが有益です。
{text["extra"]}
## 入力の読み方

- candidates[].raw … 生の表記。variants には**そのまま**書く（整えない）
- rows … 行数。djs / coTitles / coArtists / coWorks … 同じ行の DJ・曲名・
  アーティスト（先頭 {SAMPLE} 件）。{text["cooccur"]}同じ DJ が両方の表記を使っていれば表記ゆれの証拠
- edges … 2つを結んだ根拠 [A, B, 種別]。redirect=外部 API が「同じものの別名だ」と
  明示している(**最も強い**) / caseonly=大小と空白だけ / {text["agg"]}cooccur=同じ曲で\
アーティストだけ違う /
  edit=綴りが近い(タイポ) / bigram=文字の重なり / substr=片方が片方に含まれるだけ。
  **substr しか無い組は最も弱いので疑う**
- hints … {text["hints"]}
- api_results … {text["api"]}

## 出力

groups は配列。1つのクラスタから 0 個・1 個・複数個を出せます。cluster_id は
元のクラスタの id をそのまま。

- variants … 同じと判断した生表記。**必ず candidates[].raw のどれか**
- canonical … variants か api_results[].title にある文字列{text["output"]}
- confidence … high / medium / low
- reason … 規則4の通り、実データの引用を含めた日本語
"""


def user_prompt(packed: list[dict], keep_apart: list[str] | None = None) -> str:
    """1バッチぶんの入力。

    keepApart はこのバッチに出てくる値に関係する組だけ。システムプロンプトに
    全部載せると固定費が重すぎるので、ここへ移してある。
    """
    payload: dict[str, Any] = {"clusters": packed}
    if keep_apart:
        payload["keepApart"] = keep_apart
    return json.dumps(payload, ensure_ascii=False, indent=1)


def batches(items: list[Any], size: int = BATCH_SIZE) -> Iterator[list[Any]]:
    """先頭から size 件ずつ切る。中身の順は変えない（再現性のため）。"""
    if size < 1:
        raise AliasError(f"バッチサイズは1以上です: {size}")
    for i in range(0, len(items), size):
        yield items[i : i + size]


def estimate_tokens(text: str) -> int:
    """ざっくりのトークン数。バッチが SAFE_INPUT_TOKENS に収まるかを見るための目安。

    tiktoken は依存を増やせないので入れられない。ASCII は 4 文字 1 トークン、
    それ以外（日本語）は 1 文字 1 トークンで数える。cl100k 系の実測に対して
    1〜2 割多めに出るので、上限に対しては安全側に外れる。
    """
    ascii_n = sum(1 for c in text if ord(c) < 128)
    return (ascii_n + 3) // 4 + (len(text) - ascii_n)


# ---------------------------------------------------------------------------
# 呼び出しとキャッシュ
# ---------------------------------------------------------------------------


def request_body(model: str, system: str, user: str, *, field: str) -> dict[str, Any]:
    """generateContent のリクエスト本文（+ キャッシュ用の "model"）。

    **"model" は Gemini の本文には存在しないキー。** そのまま送ると
    400 INVALID_ARGUMENT になるので、post() が送信直前に外して URL 側
    （endpoint_for）に回す。それでもここに入れてあるのは cache_key() が
    **リクエスト本文全体の SHA256** だからで、モデル名が本文から消えると
    キャッシュキーからも消える。そうなると `--model A` で1回回したあと
    `--model B` に変えても data/raw/llm/ の同じファイルに当たり、**Aで生成した
    古い応答が黙って返る**（tests/test_aliases.py の
    test_a_different_model_does_not_reuse_the_cache がこれを見張っている）。
    「本文に入っているがネットワークには出ないキー」はこの1つだけ。

    temperature は送らない。既定値以外を受け付けないモデルがあり、--model で
    差し替えられる以上、どれでも通る形にしておきたい。再現性はプロンプトの
    SHA256 キャッシュで担保しているので実害は無い。

    responseSchema を効かせるには responseMimeType が "application/json" で
    ある必要がある。片方だけ書くとスキーマが効かず、素の文章が返って
    parse_groups がバッチを丸ごと捨てる。**必ず対で書くこと。**

    system は messages ではなく systemInstruction に置く（Gemini では
    role="system" の contents は受け付けない）。

    field は response_schema のためだけに要る（既定値を置いていないのは、
    呼ぶ側が work を暗黙に選んでしまうのを防ぐため）。
    """
    return {
        "model": model,
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": response_schema(field),
            "maxOutputTokens": MAX_OUTPUT_TOKENS,
        },
    }


def cache_dir() -> Path:
    """data/raw/llm/。data/raw/ は gitignore 済み。"""
    return paths.RAW_DIR / "llm"


def cache_key(body: dict) -> str:
    """リクエスト本文全体の SHA256。

    プロンプトだけでなくモデル名と responseSchema も含める。スキーマを直したのに
    古い応答が返ってくると、原因の分からない検証エラーになるため。モデル名が
    Gemini の本文には無いキーであるのにここまで届いている理由は request_body の
    docstring を参照。
    """
    canonical = json.dumps(body, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def cached_response(body: dict) -> dict | None:
    path = cache_dir() / f"{cache_key(body)}.json"
    if not path.exists():
        return None
    try:
        with path.open(encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def store_response(body: dict, response: dict) -> Path:
    path = cache_dir() / f"{cache_key(body)}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(response, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return path


_UA = "odj-db-register/0.1 ( hasegawa0kn@gmail.com )"


def post(body: dict, token: str, *, retries: int = 3, timeout: int = 180) -> dict:
    """POST 1回。drive.py の _get() と同じくリトライ + バックオフを持つ。

    **429（status=RESOURCE_EXHAUSTED）は区別せず全部リトライする。** Gemini の
    429 は「1分あたりの上限に当たった」（待てば回復する。無料枠では普通に
    当たる）でも「1日あたりの上限を使い切った」（待っても回復しない）でも
    同じ code / status で返り、**本文から機械的に区別できない**。区別を諦めて
    素直に待つほうを採った。どちらだったかは、メッセージに載せた応答本文
    （error.message）を人が読めば分かる — GitHub Models 時代からの方針。
    OpenAI 用に足した insufficient_quota による即時打ち切りは、Gemini に
    その文字列が無いので落としてある。
    """
    # モデル名は本文から取り出して URL に回す。**Gemini は本文に "model" を
    # 持たない**ので、付けたまま投げると 400 INVALID_ARGUMENT になる
    # （なぜ本文に入っているかは request_body の docstring を参照）。
    model = str(body.get("model") or DEFAULT_MODEL)
    url = endpoint_for(model)
    payload = {k: v for k, v in body.items() if k != "model"}
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    last: Exception | None = None
    for attempt in range(retries):
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                # クエリ ?key= ではなくヘッダ。URL に鍵が入ると履歴やログに残る。
                "x-goog-api-key": token,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": _UA,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:  # noqa: PERF203
            detail = exc.read().decode("utf-8", "replace")[:500]
            hint = ""
            if exc.code == 404 or "NOT_FOUND" in detail:
                # 存在しないモデル ID か、この API キーで使えないモデルを指定すると
                # 404 / status=NOT_FOUND が返る。**Gemini はモデル ID が URL パスに
                # 入る**ので、綴りを間違えるとエンドポイントごと存在しないことに
                # なり、本文のバリデーション（400）ではなく 404 で落ちる。
                # **綴り間違いと権限不足が同じエラーになる**ので、まず一覧を引いて
                # もらうのが早い。
                #
                # ここで使う model は、上で body から取り出しておいた変数。
                # post() の引数には model が無く、直に model と書くと NameError で
                # 本来のエラー本文まで消える、という失敗を一度やっている。
                hint = (
                    f"\n  モデル {model!r} が見つからないか、"
                    "この API キーでは使えません。"
                    "\n  使えるモデルの一覧はこれで引けます:"
                    '\n    curl -s "https://generativelanguage.googleapis.com'
                    '/v1beta/models"'
                    ' -H "x-goog-api-key: $GEMINI_API_KEY"'
                    "\n  --model で切り替えられます。"
                )
            last = AliasError(f"Gemini API が {exc.code} を返しました: {detail}{hint}")
            # 400（プロンプトが長すぎる等）や 401 / 403 は待っても直らないので
            # 即座に上げる。429 はここに含めない（上の docstring を参照）。
            if exc.code not in (429, 500, 502, 503, 504):
                raise last from exc
            wait = 20 * (attempt + 1) if exc.code == 429 else 3 * (attempt + 1)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last = exc
            wait = 3 * (attempt + 1)
        if attempt < retries - 1:  # 最後の1回のあとは待たずに諦める
            time.sleep(wait)
    raise AliasError(f"Gemini API の呼び出しに失敗しました: {last}")


def parse_groups(response: dict) -> list[dict]:
    """応答から groups を取り出す。本文は candidates[0].content.parts[].text。

    responseSchema で強制しているので普通は素直に JSON だが、**読めない応答が
    3通りある。どれも例外にせず、そのバッチだけ諦めて空を返す**（1バッチ落ちても
    他のバッチの提案は書けるので、ここで落とすほうが損が大きい）。

      - 出力上限で切れる（finishReason="MAX_TOKENS"）。JSON が途中で終わる。
        OpenAI 時代の finish_reason="length" に当たるもの
      - 安全性フィルタで止まる（"SAFETY" / "BLOCKED"）。このとき
        **candidates[0] に content ごと無いことがある**ので、parts を無条件に
        添字アクセスすると IndexError / KeyError で落ちる
      - プロンプト自体がブロックされる。candidates が空で、理由は
        promptFeedback.blockReason にだけ入る

    黙って捨てるのは避けたいので、止まった理由は no_groups_reason() が組み立て、
    ask() がログに流す。

    text は parts を連結して読む。1パートで返るのが普通だが、分割されたときに
    先頭だけ読むと JSON が途中で切れたのと同じ壊れ方をするため。
    """
    candidates = response.get("candidates") or []
    if not candidates:
        return []
    content = candidates[0].get("content")
    if not isinstance(content, dict):
        return []
    parts = content.get("parts")
    if not isinstance(parts, list):
        return []
    text = "".join(
        p["text"] for p in parts if isinstance(p, dict) and isinstance(p.get("text"), str)
    )
    if not text.strip():
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return []
    groups = parsed.get("groups") if isinstance(parsed, dict) else None
    return [g for g in groups or [] if isinstance(g, dict)]


def no_groups_reason(response: dict) -> str:
    """提案が0件だったときに、応答のどこで止まったのかを一言で返す。

    parse_groups が空を返す事情は「モデルがまとめる根拠を見つけなかった」
    （正常）から「安全性フィルタで落ちた」まで幅があり、**どれも同じ空配列に
    なってしまう**。ログに出す文面だけでも分けておかないと、29 リクエストの
    うち何本が本当に静かに捨てられたのか後から分からない。
    """
    candidates = response.get("candidates") or []
    if not candidates:
        blocked = (response.get("promptFeedback") or {}).get("blockReason")
        if blocked:
            return f"プロンプトがブロックされました（blockReason={blocked}）"
        return "応答に candidates がありません"
    finish = str(candidates[0].get("finishReason") or "")
    if finish == "MAX_TOKENS":
        return "出力上限（maxOutputTokens）で切れました。JSON が途中で終わっています"
    if finish in ("SAFETY", "BLOCKED"):
        return f"安全性フィルタで止まりました（finishReason={finish}）"
    if not isinstance(candidates[0].get("content"), dict):
        return f"応答に content がありません（finishReason={finish or '不明'}）"
    if finish and finish != "STOP":
        return f"finishReason={finish}"
    return "モデルがグループを1つも出しませんでした（まとめる根拠が無いという判断）"


# ---------------------------------------------------------------------------
# 検証（プロンプトは守られない前提で書く）
# ---------------------------------------------------------------------------


def blocked_pair(a: str, b: str, keep_apart: set[frozenset[str]]) -> bool:
    """keep_apart.toml が「別物」と決めた組か。

    cli._blocked_pair と block.build_edges() の add() と同じ条件。生の組だけを
    見ると「アイカツ! 楽曲」と「アイカツスターズ」のような注記違いの迂回路を
    すり抜けるので、注記を剥がしたキーの組でも見る。
    """
    if frozenset((a, b)) in keep_apart:
        return True
    ka = rules.agg_key(rules.strip_notes(a))
    kb = rules.agg_key(rules.strip_notes(b))
    return bool(ka and kb and ka != kb and frozenset((ka, kb)) in keep_apart)


def check_group(
    group: dict,
    cluster: dict | None,
    evidence: dict[str, list[dict]],
    keep_apart: set[frozenset[str]],
) -> str:
    """提案1件を検証する。問題なければ空文字、あれば捨てる理由を返す。

    ここは「LLM がプロンプトを守らなかったとき」に効く。守られている限り何も
    起きないが、守られなかったときに黙って辞書の手前まで通すわけにいかない。
    """
    if cluster is None:
        return f"知らない cluster_id: {group.get('cluster_id')!r}"

    raws = [v.get("raw", "") for v in cluster.get("values", [])]
    variants = [v for v in (group.get("variants") or []) if isinstance(v, str)]
    variants = [v for v in variants if v]
    if not variants:
        return "variants が空"

    unknown = [v for v in variants if v not in raws]
    if unknown:
        # 表記を「整えて」返してくるのが典型（「ボカロ?」→「ボカロ？」）。
        # plays.json に無い文字列は検索に一致しないので、そのまま通せない。
        return "クラスタに無い variants: " + "、".join(f"「{v}」" for v in unknown)

    canonical = (group.get("canonical") or "").strip()
    if not canonical:
        return "canonical が空"
    allowed = set(raws) | evidence_titles(cluster, evidence)
    if canonical not in allowed:
        return f"canonical が候補にも API の結果にも無い（創作）: 「{canonical}」"

    # 重複を除いた実際の同値クラス。canonical が API 由来なら plays.json に
    # 無いので、keep_apart の検査からは外す（実データの組ではないため）。
    values = list(dict.fromkeys(variants + ([canonical] if canonical in raws else [])))
    if len(values) < 2:
        return "variants が1件だけで、canonical も同じ（提案の中身が無い）"
    for i in range(len(values)):
        for j in range(i + 1, len(values)):
            if blocked_pair(values[i], values[j], keep_apart):
                return (
                    "keep_apart.toml で別物と決めた組を含む: "
                    f"「{values[i]}」と「{values[j]}」"
                )

    if not (group.get("reason") or "").strip():
        return "reason が空"
    return ""


def to_entry(group: dict, model: str) -> dict[str, Any]:
    """検証を通った提案 → _proposed/*.toml の1ブロック。

    **approved は書かない。** 人間が npm run review で判断して初めて works.toml に
    approved = true 付きで入る。id はクラスタの id をそのまま使うので、1つの
    クラスタを複数グループに割った場合は同じ id のブロックが並ぶ
    （store.load_proposals は先頭を採る。レビュー側でどう見せるかは Phase 1 の話）。
    """
    variants = [v for v in group.get("variants") or [] if isinstance(v, str) and v]
    return {
        "id": (group.get("cluster_id") or "").strip(),
        "canonical": (group.get("canonical") or "").strip(),
        "series": (group.get("series") or "").strip(),
        "kind": (group.get("kind") or "").strip(),
        "variants": list(dict.fromkeys(variants)),
        "confidence": (group.get("confidence") or "low").strip(),
        "source": f"llm:{model}",
        "reason": (group.get("reason") or "").strip(),
    }


# ---------------------------------------------------------------------------
# 組み立て
# ---------------------------------------------------------------------------


def _build(chunk: list[dict], evidence: dict) -> tuple[str, int]:
    """バッチ1つぶんの入力文字列と、その推定トークン数。"""
    user = user_prompt(
        [pack_cluster(c, evidence) for c in chunk],
        keep_apart_lines(cluster_values(chunk)),
    )
    return user, estimate_tokens(user)


def pack_batches(clusters: list[dict], system_tokens: int, evidence: dict) -> list[dict]:
    """入る限り詰めてバッチにする。

    以前は「一定数ずつに切ってから、収まらないバッチを半分に割る」やり方だった。
    これだと割った片方が枠の半分しか使わず、実データで 37 リクエストになった
    （その半端な分だけ system_prompt の固定費を余計に払うことになる）。
    1つずつ足しては上限を確かめるほうが、枠を使い切れてリクエスト数が減る。

    クラスタ数の上限も要る。入力に収まっても、1クラスタ ≒ 出力 200tok なので
    20 を超えると今度は出力の 4000tok を超えて途中で切れる。
    """
    out: list[dict] = []
    current: list[dict] = []
    for cluster in clusters:
        trial = [*current, cluster]
        user, tokens = _build(trial, evidence)
        if current and (
            system_tokens + tokens > SAFE_INPUT_TOKENS or len(trial) > MAX_CLUSTERS_PER_CALL
        ):
            done_user, done_tokens = _build(current, evidence)
            out.append(
                {"clusters": current, "user": done_user, "tokens": system_tokens + done_tokens}
            )
            current = [cluster]
        else:
            current = trial
    if current:
        user, tokens = _build(current, evidence)
        out.append({"clusters": current, "user": user, "tokens": system_tokens + tokens})
    return out


def plan(field: str, *, limit: int | None = None, size: int = BATCH_SIZE) -> dict:
    """ネットワークに出ずに、投げる材料だけを組み立てる。--dry-run の中身。"""
    clusters = load_clusters(field)
    if limit is not None:
        clusters = clusters[:limit]
    evidence = load_evidence(field)
    system = system_prompt(field)
    system_tokens = estimate_tokens(system)
    plans = pack_batches(clusters, system_tokens, evidence)
    return {
        "field": field,
        "system": system,
        "systemTokens": system_tokens,
        "evidence": evidence,
        "batches": plans,
        "total": len(clusters),
    }


def ask(
    field: str,
    *,
    limit: int | None = None,
    size: int = BATCH_SIZE,
    model: str = DEFAULT_MODEL,
    token: str | None = None,
    log: Any = None,
) -> dict[str, Any]:
    """クラスタを投げて提案を作り、_proposed/<field>s.toml を書き直す。

    返すのは件数の内訳。log は print 互換の関数で、進捗と**捨てた提案の理由**を
    ここに流す（捨てたことが黙って起きるのが一番困る）。
    """
    def say(text: str) -> None:
        if log is not None:
            log(text)

    token = token or os.environ.get("GEMINI_API_KEY") or ""
    prepared = plan(field, limit=limit, size=size)
    evidence = prepared["evidence"]
    keep_apart = block.load_keep_apart()

    entries: list[dict[str, Any]] = []
    rejected: list[str] = []
    cached_hits = 0
    calls = 0

    for i, batch in enumerate(prepared["batches"], start=1):
        by_id = {c.get("id"): c for c in batch["clusters"]}
        body = request_body(model, prepared["system"], batch["user"], field=field)
        response = cached_response(body)
        if response is None:
            if not token:
                raise AliasError(
                    "GEMINI_API_KEY がありません（--dry-run なら不要です）。"
                    "GitHub Actions ではリポジトリの secrets に GEMINI_API_KEY を"
                    "登録し、env で渡してください"
                )
            say(f"  [{i}/{len(prepared['batches'])}] 送信中… 推定 {batch['tokens']} tok")
            response = post(body, token)
            store_response(body, response)
            calls += 1
        else:
            cached_hits += 1
            say(f"  [{i}/{len(prepared['batches'])}] キャッシュ命中")

        groups = parse_groups(response)
        if not groups:
            # 理由を必ず添える。空配列だけだと「モデルが何も出さなかった」と
            # 「安全性フィルタで丸ごと落ちた」が区別できない。
            say(f"  [{i}] 提案なし: {no_groups_reason(response)}")
        for group in groups:
            cluster = by_id.get((group.get("cluster_id") or "").strip())
            problem = check_group(group, cluster, evidence, keep_apart)
            if problem:
                label = (group.get("cluster_id") or "?") + " " + str(group.get("canonical"))
                rejected.append(f"{label}: {problem}")
                continue
            entries.append(to_entry(group, model))

    # クラスタの並び（行数の多い順）に揃える。LLM が返す順に依らせないと、
    # 同じキャッシュから毎回同じバイト列にならない。
    order = {c.get("id"): n for n, c in enumerate(load_clusters(field))}
    entries.sort(key=lambda e: order.get(e["id"], len(order)))

    path = store.write_proposals(field, entries)
    for line in rejected:
        say(f"  捨てた提案: {line}")
    return {
        "path": store.rel_to_repo(path),
        "clusters": prepared["total"],
        "requests": len(prepared["batches"]),
        "calls": calls,
        "cached": cached_hits,
        "proposed": len(entries),
        "rejected": rejected,
    }
