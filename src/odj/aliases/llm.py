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

GitHub Models の無料枠（High tier: 50 req/日・**入力 4000tok**）に収めるため、
入力上限まで詰めてバッチにする。実測（--dry-run）で work の 152 クラスタが
28 リクエスト、artist の 105 クラスタが 34 リクエスト。**1回の dispatch で
1 field ぶんしか投げない**（aliases.yml が field を入力に取る）ので1回なら収まるが、
**同じ日に work と artist の両方を回すと 62 で枠を超える**。日を分けること。
artist はクラスタが少ないのにリクエストが多い。1クラスタが大きい
（`わか・ふうり・すなお from STAR☆ANIS` のような長い生表記が並ぶ）ことと、
system_prompt が field 固有の規則ぶん長い（work 1479tok に対して artist 2168tok）
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
# GitHub Models
# ---------------------------------------------------------------------------

# OpenAI 互換のエンドポイント。models.inference.ai.azure.com ではなくこちら
# （2024 年末に models.github.ai へ移った）。
ENDPOINT = "https://models.github.ai/inference/chat/completions"

# **Anthropic Claude は GitHub Models のカタログに無い**（実際に引いて確認済み）。
# 使えるのは openai/* と meta/*、mistral-ai/* など。
#
# gpt-5 ではない理由: カタログには載っているが rate_limit_tier が "custom" で、
# 標準の無料枠には含まれない。Actions から投げると 400 が返る:
#   {"code":"unavailable_model","message":"Unavailable model: gpt-5"}
# **カタログに載っていることと使えることは別**だった。gpt-4.1 は "high" tier で
# 標準の無料枠に入っている。
#
# gpt-5 は reasoning 持ちで、出力トークンを思考にも使う。max_completion_tokens
# の枠を思考が食って答えが途中で切れる心配があり、その意味でも 4.1 のほうが素直。
DEFAULT_MODEL = "openai/gpt-4.1"

# 入力の上限。**実測値**で、事前の調べで 8000 としていたのは誤りだった。
# GitHub Actions 上で 413 が返って判明した:
#   {"code":"tokens_limit_reached",
#    "message":"Request body too large for gpt-5 model. Max size: 4000 tokens."}
INPUT_TOKEN_LIMIT = 4000

# 実際に詰めてよい量。上限ちょうどを狙うと、推定の誤差ぶんだけリクエストが
# まるごと無駄になる。1割の余裕を持たせるほうが、413 で落ちるより安い。
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


def response_format(field: str) -> dict[str, Any]:
    """LLM に強制する出力スキーマ。

    **approved はここに無い。** LLM が承認済みを書く手段が存在しないことが、
    「未承認のものが公開データに出ない」の一番外側の担保になっている。

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
        "type": "json_schema",
        "json_schema": {
            "name": "alias_groups",
            "strict": True,
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["groups"],
                "properties": {
                    "groups": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            # strict モードは properties と required が一致していないと
                            # 400 を返す。並びも properties のまま揃えておく。
                            "required": list(properties),
                            "properties": properties,
                        },
                    }
                },
            },
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

    values を渡すと、**その値に関係する組だけ**に絞る。26 組を全部載せると
    それだけで 1400tok 使い、入力上限 4000 に対して固定費が重すぎるため。
    絞っても防止力は落ちない。プロンプトに出さなかった組も、返ってきた提案は
    block.load_keep_apart() で必ず再検査する（プロンプトは守られない前提で書き、
    守られなかったときに落ちる場所を Python 側に置く、という方針は変えない）。

    関係するかどうかは注記を剥がしたキーでも見る。「アイカツ! 楽曲」と
    「アイカツスターズ」のような迂回路が実データにあり、生の文字列だけを
    突き合わせると取りこぼす。
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

# field ごとに差し替える部分。共通の骨格（絶対規則の 1・4・5、入力の読み方の大枠、
# 出力の形）は system_prompt の f-string 側に1つだけ持ち、**中身が field で本当に
# 変わる箇所だけ**をここに出してある。work と artist で丸ごと2本の文面を持つと、
# 「迷ったら分ける」のような一番効く規則が片方だけ直されて必ず乖離する。
#
# work 側の値は現行の文面をそのまま写したもので、**1文字も変えていない**
# （変えるとリクエスト本文の SHA256 が変わり data/raw/llm/ のキャッシュが無効になる。
# Actions 上は data/raw/ が gitignore で常にコールドなので実害は無いが、ローカルの
# 再実行が 28 リクエストぶん無駄にネットワークへ出る）。例外は edges の説明に
# redirect を足した1点だけで、あちらは work でも説明が抜けていたバグである。
_FIELD_TEXT: dict[str, dict[str, str]] = {
    "work": {
        # 規則2の後半。「keepApart に挙がっていなくても」に続く。
        "rule2": """シリーズの
   別作品（1期と2期、無印と続編、ブランドが同じだけの別タイトル）は
   別物として扱ってください。""",
        # 規則3の後半。「創作は禁止…と考えて書いてはいけません。」に続く。
        "rule3": """api_results に正式名称があればそちらを優先します
   （「ナナシス」より「Tokyo 7th シスターズ」）。無ければ rows の多い表記を選びます。
   ただし api_results は検索のヒットなので、一覧記事や関連商品（「〜の楽曲一覧」
   「〜 Solo Collection」）が混ざります。**作品そのものを指す title だけ**を
   canonical にしてください。""",
        # field 固有の節。work には無い（もともと全体が work 向けに書かれている）。
        "extra": "",
        # edges の agg の説明。「agg=」と末尾の「 / 」まで入れてあるのは、
        # work 側の改行位置を1文字も動かさないため（下の artist と説明の長さが違う）。
        "agg": """agg=注記
  (OP・ED・楽曲・TVアニメ「」)を剥がすと一致 / """,
        "hints": """series-risk=部分一致だけで繋がった(別作品の恐れ) /
  series-mark-mismatch=「2期」等の印が食い違う / split-from-large=繋がりすぎた塊の
  破片(中身を疑う) / artist-as-work=元ネタ欄にアーティスト名""",
        "api": """外部 API の裏取り。**空配列は「引いたが記事が無かった」**で
  タイポか通称のシグナル。キーごと無い値は引いていないだけなので根拠にしない""",
        # 出力の series / kind。artist では response_format() のスキーマから
        # 落としてあるので、ここで説明すると書けない項目を求めることになる。
        "output": """
- series … シリーズ名。分からなければ空文字
- kind … work=作品 / vocaloid=ボカロ曲 / vtuber=VTuber / odj-self=大会自体のネタ /
  artist-as-work=元ネタ欄にアーティスト名 / unknown=判断できない""",
    },
    "artist": {
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
    要約して一般則にはしない。「アイマス系は分ける」に化けると、逆に
    「アイドルマスターシンデレラガールズ」と「デレマス」（同じもの）まで分かれる。

    **ここが長くなるとリクエスト数が増える。** 全バッチに乗る固定費なので、
    1バッチに詰められる量は SAFE_INPUT_TOKENS からこの長さを引いた残りになる。
    artist で実測すると、work と同じ文面（1447tok）のままなら 22 リクエスト、
    固有規則を書きたいだけ書いた 2577tok では **50 リクエスト（無料枠ちょうど）**で、
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

1. **迷ったら「分ける」。** 統合は不可逆で情報が失われ、分離は後から可逆です。
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
   「一般的にそう呼ばれるため」「同じシリーズだから」は理由として認められません。

5. **確信が持てなければ confidence="low"。** low の提案は公開データには出ませんが、
   人間のレビューには残るので、捨てずに low で出すほうが有益です。
{text["extra"]}
## 入力の読み方

- candidates[].raw … 生の表記。variants には**そのまま**書く（整えない）
- rows … 行数。djs / coTitles / coArtists / coWorks … 同じ行の DJ・曲名・
  アーティスト（先頭 {SAMPLE} 件）。**曲名が1つも重ならずアーティストの系統も違うなら
  別{thing}を疑う**。同じ DJ が両方の表記を使っていれば表記ゆれの証拠
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
    """ざっくりのトークン数。無料枠に収まるかを見るためだけの目安。

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
    """OpenAI 互換のリクエスト本文。

    temperature は送らない。既定値以外を受け付けないモデル（gpt-5 系）があり、
    --model で差し替えられる以上、どれでも通る形にしておきたい。再現性は
    プロンプトの SHA256 キャッシュで担保しているので実害は無い。
    max_tokens ではなく max_completion_tokens なのも同じ理由。

    field は response_format のためだけに要る（既定値を置いていないのは、
    呼ぶ側が work を暗黙に選んでしまうのを防ぐため）。
    """
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "response_format": response_format(field),
        "max_completion_tokens": MAX_OUTPUT_TOKENS,
    }


def cache_dir() -> Path:
    """data/raw/llm/。data/raw/ は gitignore 済み。"""
    return paths.RAW_DIR / "llm"


def cache_key(body: dict) -> str:
    """リクエスト本文全体の SHA256。

    プロンプトだけでなくモデル名と response_format も含める。スキーマを直したのに
    古い応答が返ってくると、原因の分からない検証エラーになるため。
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

    無料枠は 10 req/分なので、429 を踏んだら待って引き直す。50 req/日を使い切った
    ときも 429 が返るが、そちらは待っても回復しないので retries を使い切って落ちる
    （メッセージに本文を載せるので、どちらかは人が読めば分かる）。
    """
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    last: Exception | None = None
    for attempt in range(retries):
        req = urllib.request.Request(
            ENDPOINT,
            data=data,
            headers={
                "Authorization": f"Bearer {token}",
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
            if "unavailable_model" in detail:
                # カタログに載っていても、rate_limit_tier が "custom" のモデルは
                # 標準の無料枠では使えない（gpt-5 がこれで、Actions から 400 が
                # 返る）。tier は下のコマンドで確認できる。
                # モデル名は body から取る。post() の引数には無いので、
                # ここで model を直に参照すると NameError になって本来の
                # エラー本文まで消える（この分岐は gpt-5 で実際に通った）。
                hint = (
                    f"\n  モデル {body.get('model')!r} はこのトークンでは使えません。"
                    "\n  無料枠で使えるのは rate_limit_tier が low / high のものです:"
                    "\n    curl -s https://models.github.ai/catalog/models |"
                    " python3 -c \"import sys,json;"
                    " [print(m['id'], m['rate_limit_tier'])"
                    " for m in json.load(sys.stdin)]\""
                    "\n  --model で切り替えられます。"
                )
            last = AliasError(f"GitHub Models が {exc.code} を返しました: {detail}{hint}")
            # 400（プロンプトが長すぎる等）や 401 は待っても直らないので即座に上げる。
            if exc.code not in (429, 500, 502, 503, 504):
                raise last from exc
            wait = 20 * (attempt + 1) if exc.code == 429 else 3 * (attempt + 1)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last = exc
            wait = 3 * (attempt + 1)
        if attempt < retries - 1:  # 最後の1回のあとは待たずに諦める
            time.sleep(wait)
    raise AliasError(f"GitHub Models の呼び出しに失敗しました: {last}")


def parse_groups(response: dict) -> list[dict]:
    """応答から groups を取り出す。

    response_format で強制しているので普通は素直に JSON だが、出力上限に当たって
    途中で切れる（finish_reason="length"）ことがある。その場合は JSON として
    読めないので、バッチ全体を諦めて呼ぶ側に空を返す。
    """
    choices = response.get("choices") or []
    if not choices:
        return []
    message = choices[0].get("message") or {}
    if message.get("refusal"):
        return []
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        return []
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return []
    groups = parsed.get("groups") if isinstance(parsed, dict) else None
    return [g for g in groups or [] if isinstance(g, dict)]


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
    （無料枠は 50 req/日なので、プロンプトを一度直したら枯渇する）。
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

    token = token or os.environ.get("GITHUB_TOKEN") or ""
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
                    "GITHUB_TOKEN がありません（--dry-run なら不要です）。"
                    "GitHub Actions では permissions: models: read を付けてください"
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
            say(f"  [{i}] 提案なし（応答が空か、出力上限で切れた可能性）")
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
