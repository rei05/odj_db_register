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
固定費をクラスタ数ぶん払うことになる。刻みは **Groq 無料枠の TPM 8,000** から
逆算していて、推定入力 3,600 に下振れ ぶんの係数を掛けた 4,500 と出力 3,000 の
合計 7,500 が1リクエストの上限（TPM_LIMIT と TOKEN_ESTIMATE_SLACK の説明を参照）。
実測（--dry-run）で work の 150 クラスタが 30 リクエスト、
artist の 105 クラスタが 34 リクエスト（work は「ブランド単位でまとめる」方針転換で
keep_apart の組を消した結果クラスタが繋がって 152 → 151 に減り、さらに全員が
判断済みになったクラスタを block が落とすので 150）。
artist はクラスタが少ないのにリクエストが多い。1クラスタが大きい
（`わか・ふうり・すなお from STAR☆ANIS` のような長い生表記が並ぶ）ことと、
system_prompt が field 固有の規則ぶん長い（work 1656tok に対して artist 2187tok）
ことの両方が効いている。プロンプトを足すときは system_prompt の説明を先に読む。

プロンプト全文の SHA256 で data/raw/llm/ にキャッシュするので、**入力が同じなら
再実行はネットワークに出ず、バイト単位で同じ結果**になる。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
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
# Groq API（OpenAI 互換の chat/completions）
# ---------------------------------------------------------------------------

# 経緯: GitHub Models の無料枠 → 2026-07-30 に廃止（410）→ OpenAI 直叩き
# （アカウントが insufficient_quota で叩けず）→ Gemini ネイティブの
# generateContent（無料枠が 1日 20 リクエストしかなく完走できない）→ Groq。
#
# **Groq は OpenAI 互換**なので、リクエスト本文も応答の形も OpenAI 時代
# （d5a42ea）のものがそのまま通る。Gemini はモデル ID を URL パスに置くため
# ENDPOINT_TEMPLATE + endpoint_for() でパスを組み立てていたが、Groq は本文の
# "model" で指定するので URL は固定に戻る。
ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"

# **プロバイダ接頭辞 "openai/" を必ず付ける。** Gemini 期はモデル ID が URL パスに
# 入るせいで「"/" を混ぜると別のパスを叩いて 404」だったが、**Groq では本文に入る
# ので制約が反転する** — 接頭辞を落とした "gpt-oss-120b" は存在しないモデル名に
# なり、そちらが 404 になる。前のコメントの癖のまま外さないこと。
#
# gpt-oss を既定に置いている理由は、**strict モード（strict: true のスキーマ強制）に
# 対応するのが openai/gpt-oss-120b と openai/gpt-oss-20b だけ**だから。他のモデルは
# json_object しか受けず、キー名も型も強制できない。スキーマが効かずに素の文章が
# 返ると parse_groups は JSON として読めずバッチを丸ごと捨てるので、黙って提案が
# 減る形になる。--model で差し替えられるので、試すときはそちらで。
DEFAULT_MODEL = "openai/gpt-oss-120b"

# Groq 無料枠（openai/gpt-oss-120b）の1分あたりトークン数。**1リクエストの
# 入力+出力がこれを超えると、どの1分にも収まらないので必ず 429 になる。**
# 実質これが1リクエストの上限で、下の刻みはすべてここから逆算している
# （モデルのコンテキスト長はこれよりはるかに大きいので効かない）。
#
# 同じ枠の RPD 1,000 は work 22 回 / artist 23 回では当たらない。TPD 200,000 は
# work と artist を同じ日に回すと超えるので、日を分けること。
# いずれもドキュメント記載時点の値。429 が続くようになったら引き直す。
TPM_LIMIT = 8000

# estimate_tokens の下振れを見込む係数。**推定をそのまま信じてはいけない。**
# 実際に踏んだ:
#     413 Request too large ... on tokens per minute (TPM):
#     Limit 8000, Requested 8337
# こちらの推定 4,167 に対して Groq の数えは 4,637 で **+11%**。estimate_tokens は
# cl100k 系に合わせた目安（ASCII 4文字=1tok / それ以外 1文字=1tok）で、
# gpt-oss のトークナイザとはずれる。**TPM 超過は 413 で即死し、待っても
# リトライしても回復しない**ので、枠は推定にこの係数を掛けた値で組む。
# 観測した +11% に余裕を足して 1.25 にしてある。
TOKEN_ESTIMATE_SLACK = 1.25

# 出力の上限。1クラスタ ≒ 出力 200tok で、下の SAFE_INPUT_TOKENS だと1回に
# 最大 12 クラスタしか詰まらないので必要量は 2,400tok。**25% の余裕を足してある**
# —— reason に実データの引用を求めているぶん 200tok を超えるクラスタがあり、
# 足りないと finish_reason="length" で JSON が途中で切れて、**そのバッチの
# クラスタが丸ごと提案なしになる**（画面には「提案なし」としか出ない）。
MAX_OUTPUT_TOKENS = 3000

# 入力に使える残り。**推定値なので TOKEN_ESTIMATE_SLACK を掛けてから TPM に
# 収まるか見る**こと: 3,600 × 1.25 + 3,000 = 7,500 <= 8,000。
#
# **入力と出力はトレードオフ**で、入力を増やすと1回に詰まるクラスタが増え、
# 必要な出力も増える。両方を実測しながら決めた配分がこれ。
# **動かすとプロンプト全文の SHA256 が変わってキャッシュが全部無効になる。**
SAFE_INPUT_TOKENS = 3600

# 1リクエストに詰めるクラスタ数の上限。**出力枠から逆算した値**で、これを超えると
# 出力が足りずに答えが切れる。実際には入力側が先に埋まって 12 クラスタ前後で
# 切れるので、ここが効くのは極端に小さいクラスタばかりが並んだときの保険。
# **大きくするほど、1リクエストが失敗したときに巻き添えで失うクラスタが増える。**
MAX_CLUSTERS_PER_CALL = MAX_OUTPUT_TOKENS // 200

# pack_batches が入る限り詰めるので、この値は --batch-size の既定でしかない。
BATCH_SIZE = 6

# gpt-oss は**推論モデル**で、推論トークンも出力枠（max_completion_tokens）を食う。
# Groq の既定は "medium" だが、それだと 3,000tok の枠を推論が先に消費して JSON が
# 組み上がらず、400 の json_validate_failed（failed_generation が空）で落ちた。
# 実際に踏んでいる。**枠の大半を答えに使わせたいので "low" にしてある。**
#
# 判断の質とのトレードオフではある。上げるなら MAX_OUTPUT_TOKENS も一緒に上げる
# 必要があるが、その合計は TPM に縛られる（TPM_LIMIT の説明を参照）ので、
# 上げるには入力側＝1回あたりのクラスタ数を削ることになる。
# 値は "low" / "medium" / "high"（gpt-oss 以外のモデルは受け付けない）。
REASONING_EFFORT = "low"

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
    """LLM に強制する出力スキーマ。OpenAI 互換の response_format にそのまま入る。

    **approved はここに無い。** LLM が承認済みを書く手段が存在しないことが、
    「未承認のものが公開データに出ない」の一番外側の担保になっている。
    仮にスキーマから漏れても to_entry() は既知のキーだけを明示的に組み立てて
    未知のキーを写さず、store.write_proposals() が approved を含む提案を例外で
    弾く。スキーマは3枚あるうちの1枚でしかないが、一番手前で効く。

    **strict と additionalProperties の要否はプロバイダで逆になる。** 両方
    書き残しておく（戻すときに再び踏むため）:
      - Groq / OpenAI … strict モードの要件が「全フィールドを required に入れる」
        「オブジェクトに additionalProperties: false を置く」。**どちらも省くと
        400**。strict に対応するモデルの縛りは DEFAULT_MODEL の説明を参照
      - Gemini（generateContent、75e4854 まで）… 逆で、json_schema のラッパーも
        strict も無く**スキーマ本体だけ**を渡す。responseSchema は OpenAPI の
        サブセットで additionalProperties を受け付けず、足すと全リクエストが
        400 で落ちた:
            Unknown name "additionalProperties" at
            'generation_config.response_schema': Cannot find field.

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
        # description はスキーマの中で唯一「文面ではなく構造として」効く指示。
        # プロンプト（規則4と出力節）にも日本語で書けと明記してあるが、gpt-oss は
        # 実際に英語で返してくる（_proposed/works.toml の 136 件のうち3件が全文
        # 英語で、ほかに「候補は agg・bigram で結ばれ、both appear as artists…」の
        # ように節ごと英語のものが十数件ある）。プロンプトの文言だけでは守られない
        # ので、スキーマ側にも同じ要求を置いて二重にしてある。
        "reason": {"type": "string", "description": "日本語で書くこと"},
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
                            # strict モードは properties と required が一致して
                            # いないと 400 を返す。省略されうる項目を作ると
                            # 「series が無い提案」が work 側に混ざって to_entry が
                            # 空文字で埋めることにもなる。並びも properties のまま。
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
   次に注記の付いていない表記のうち rows の多いもの。**候補が全部注記付きなら注記
   (OP・ED・2期・劇中曲・TVアニメ「」)を剥がした形を選べます**（「その着せ替え人形は
   恋をする OP」→「その着せ替え人形は恋をする」）。剥がす以外の言い換えと、
   api_results に無いシリーズ名は作らないこと。一覧記事や関連商品
   （「〜の楽曲一覧」）は canonical にしない。""",
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
        # 出力の series / kind。artist では response_format() のスキーマから
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
    削って 2168tok / 34 リクエストに落としてある（reason を日本語で書けという要求を
    足した現在は 2187tok。リクエスト数は 34 のまま）。100tok につき 2 リクエストほど
    増える勘定。規則を1つ足すときは、実データで実際に踏んだ組を1〜2個挙げるだけに
    して、一般論や他の規則と重なる説明は書かないこと。

    **artist 側はもう伸ばす余地がほとんど無い。** 「どのバッチも SAFE_INPUT_TOKENS に
    収まる」ためには、一番大きいクラスタ（単独で 1363tok）と足して 3600 に収まる
    必要があり、現在の 2187tok では残り 50tok しかない。これを超えるとそのクラスタ
    だけ1件で枠を超える（pack_batches は1件だけのバッチを割れない）。artist の規則を
    足すときは、同じだけどこかを削ること。
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

4. reason は**日本語で**書き、**与えられた材料を引用**すること。rows（行数）、djs、
   coTitles、coArtists、api_results の title か note のいずれかを必ず含めてください。
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
- reason … 規則4の通り実データの引用を含める。**必ず日本語**（英語で書かない）
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
    それ以外（日本語）は 1 文字 1 トークンで数える。

    **この値を上限と直接比べてはいけない。安全側に外れる保証は無い。**
    以前ここには「cl100k 系の実測に対して 1〜2 割多めに出るので上限に対しては
    安全側」と書いてあったが、**gpt-oss では逆に下振れした** —— 推定 4,167 に
    対して Groq の数えは 4,637（+11%）で、TPM 8,000 に対して 413 を踏んだ。
    トークナイザが違えばずれる向きも変わる。枠に収まるかを見るときは
    TOKEN_ESTIMATE_SLACK を掛けること。
    """
    ascii_n = sum(1 for c in text if ord(c) < 128)
    return (ascii_n + 3) // 4 + (len(text) - ascii_n)


# ---------------------------------------------------------------------------
# 呼び出しとキャッシュ
# ---------------------------------------------------------------------------


def request_body(model: str, system: str, user: str, *, field: str) -> dict[str, Any]:
    """OpenAI 互換のリクエスト本文。Groq にはこのまま送れる。

    **"model" が本文にある**ので、cache_key()（リクエスト本文全体の SHA256）に
    モデル名が自然に含まれる。Gemini 期はモデルが URL パスに移るため、本文に
    "model" を残して post() が送信直前に外す、という仕掛けをわざわざ置いていた
    — 本文から消えるとキャッシュキーからも消え、`--model A` で回したあと
    `--model B` に変えても data/raw/llm/ の同じファイルに当たって**Aで生成した
    古い応答が黙って返る**（tests/test_aliases.py の
    test_a_different_model_does_not_reuse_the_cache が見張っている）。

    temperature は送らない。既定値以外を受け付けないモデルがあり、--model で
    差し替えられる以上、どれでも通る形にしておきたい。再現性はプロンプトの
    SHA256 キャッシュで担保しているので実害は無い。max_tokens ではなく
    max_completion_tokens なのも同じ理由。

    field は response_format のためだけに要る（既定値を置いていないのは、
    呼ぶ側が work を暗黙に選んでしまうのを防ぐため）。
    """
    body: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "response_format": response_format(field),
        "max_completion_tokens": MAX_OUTPUT_TOKENS,
    }
    # reasoning_effort の "low" / "medium" / "high" は gpt-oss 系しか受け付けない
    # （qwen は "none" / "default"、他は非対応）。--model で差し替えたときに
    # 400 にならないよう、載せるのは対象のモデルのときだけにする。
    if "gpt-oss" in model:
        body["reasoning_effort"] = REASONING_EFFORT
    return body


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


# 429 で待つ上限。Groq は「あとどれだけ待てば回復するか」を教えてくれるので
# （retry-after ヘッダか本文の "Please try again in 19m2.208s"）、それに従う。
# ただし**分あたりの詰まり（TPM / RPM）と日あたりの枯渇（TPD / RPD）で桁が違う**。
# 前者は数秒〜数十秒で回復するので待つ価値があるが、後者は十数分〜数時間になり、
# その間ジョブを空回しさせるのは無駄なので打ち切る。実際に踏んだ TPD 枯渇では
# "try again in 19m2.208s" が返っていて、固定 20 秒のバックオフでは3回とも
# 無駄撃ちして落ちていた。
MAX_RETRY_WAIT = 120


def retry_after_seconds(exc: urllib.error.HTTPError, detail: str) -> float | None:
    """あとどれだけ待てば回復するか。読み取れなければ None。

    retry-after ヘッダを優先し、無ければ本文の文言から拾う。Groq は
    "Please try again in 19m2.208s" のように分と秒で書いてくる。
    """
    header = exc.headers.get("retry-after") if exc.headers else None
    if header:
        try:
            return float(header)
        except ValueError:
            pass
    m = re.search(r"try again in (?:(\d+)m)?([\d.]+)s", detail)
    if m:
        return int(m.group(1) or 0) * 60 + float(m.group(2))
    return None


def post(body: dict, token: str, *, retries: int = 3, timeout: int = 180) -> dict:
    """POST 1回。drive.py の _get() と同じくリトライ + バックオフを持つ。

    **429 は待ち時間を読み取って従う。** Groq の制限は RPM / TPM / RPD / TPD の
    4種類あり、分あたり（RPM / TPM）は数秒〜数十秒で回復するが、日あたり
    （RPD / TPD）は十数分〜数時間かかる。どちらなのかは応答が教えてくれるので
    （retry_after_seconds を参照）、MAX_RETRY_WAIT に収まるなら待って引き直し、
    超えるなら**待たずに上げる**。以前は一律 20 秒のバックオフで、日あたりの
    枯渇に当たったときに3回とも無駄撃ちしていた。
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
            if exc.code == 404 or "model_not_found" in detail:
                # 存在しないモデル ID か、この API キーで使えないモデルを指定すると
                # 404 が返る。**綴り間違いと権限不足が同じエラーになる**ので、
                # まず一覧を引いてもらうのが早い。一番ありがちなのは "openai/" の
                # 接頭辞を落とすこと（DEFAULT_MODEL の説明を参照）なので、それも
                # 書いておく。
                #
                # モデル名は body から取る。post() の引数には無いので、ここで
                # model を直に参照すると NameError になって本来のエラー本文まで
                # 消える、という失敗を一度やっている。
                hint = (
                    f"\n  モデル {body.get('model')!r} が見つからないか、"
                    "この API キーでは使えません。"
                    "\n  Groq のモデル ID は openai/gpt-oss-120b のように"
                    "プロバイダ接頭辞まで含めた形です（落とすと 404）。"
                    "\n  使えるモデルの一覧はこれで引けます:"
                    "\n    curl -s https://api.groq.com/openai/v1/models"
                    ' -H "Authorization: Bearer $GROQ_API_KEY"'
                    "\n  --model で切り替えられます。"
                )
            elif exc.code == 413:
                # 1リクエストが TPM を超えた。**待っても直らない**（どの1分にも
                # 収まらない）ので、刻みを下げるしかない。ここに来るのは
                # estimate_tokens が下振れしたときで、実際に踏んでいる
                # （TOKEN_ESTIMATE_SLACK の説明を参照）。応答本文の Requested に
                # Groq が数えた実際のトークン数が入っているので、それを見て
                # 係数を上げ直せる。
                hint = (
                    "\n  1リクエストが TPM を超えました。**待っても直りません。**"
                    "\n  応答の Requested がこちらの推定より大きいなら、"
                    "estimate_tokens が下振れしています。"
                    f"\n  llm.py の SAFE_INPUT_TOKENS（現在 {SAFE_INPUT_TOKENS}）を下げるか、"
                    f"TOKEN_ESTIMATE_SLACK（現在 {TOKEN_ESTIMATE_SLACK}）を上げてください。"
                )
            if exc.code == 429:
                # あとどれだけ待てば回復するかを応答が教えてくれる。長すぎるなら
                # 待たずに上げる（日あたりの枠が枯れたときは十数分〜数時間かかる）。
                after = retry_after_seconds(exc, detail)
                if after is not None and after > MAX_RETRY_WAIT:
                    hint = (
                        f"\n  回復まで約 {after / 60:.0f} 分と返ってきました"
                        f"（{MAX_RETRY_WAIT} 秒を超えるので待たずに上げます）。"
                        "\n  TPD / RPD なら1日ぶんの枠を使い切っています。"
                        "work と artist を同じ日に回すと超えます。"
                        "\n  成功したぶんは data/raw/llm/ にキャッシュされているので、"
                        "**ローカルなら**回復後に同じコマンドで続きから進みます。"
                    )
                    raise AliasError(
                        f"Groq API が {exc.code} を返しました: {detail}{hint}"
                    ) from exc
            last = AliasError(f"Groq API が {exc.code} を返しました: {detail}{hint}")
            # json_validate_failed は 400 だが**確率的**で、同じ本文でも投げ直すと
            # 通ることが多い（strict の制約付きデコードが組み立て切れなかった
            # だけ。Groq 側でも1割ほど出ると報告がある）。ここで拾っておかないと
            # 1バッチにつき1割の確率で落ちる勘定になり、29 バッチではまず完走
            # しない。待つ必要は無いので短いバックオフで引き直す。
            retryable = exc.code in (429, 500, 502, 503, 504) or (
                "json_validate_failed" in detail
            )
            # 401 / 403 / 413 や、モデル名の間違いは待っても直らないので即座に上げる。
            if not retryable:
                raise last from exc
            if exc.code == 429:
                # 教えられた時間に少しだけ足して待つ。読み取れなければ従来の
                # 20 / 40 秒のバックオフに落とす。
                after = retry_after_seconds(exc, detail)
                wait = after + 1 if after is not None else 20 * (attempt + 1)
            else:
                wait = 3 * (attempt + 1)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last = exc
            wait = 3 * (attempt + 1)
        if attempt < retries - 1:  # 最後の1回のあとは待たずに諦める
            time.sleep(wait)
    raise AliasError(f"Groq API の呼び出しに失敗しました: {last}")


def parse_groups(response: dict) -> list[dict]:
    """応答から groups を取り出す。本文は choices[0].message.content。

    response_format で強制しているので普通は素直に JSON だが、**読めない応答が
    ある。どれも例外にせず、そのバッチだけ諦めて空を返す**（1バッチ落ちても
    他のバッチの提案は書けるので、ここで落とすほうが損が大きい）。

      - 出力上限に当たって切れる（finish_reason="length"）。JSON が途中で終わる
      - モデルが答えを拒否する（message.refusal が入り content は null）
      - choices が空、content が空文字、content が JSON として読めない

    黙って捨てるのは避けたいので、止まった理由は no_groups_reason() が組み立て、
    ask() がログに流す（Gemini 期に足した仕組み。プロバイダが変わっても、0件の
    内訳が分からないと 29 リクエストのうち何本が捨てられたのか追えない）。
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


def no_groups_reason(response: dict) -> str:
    """提案が0件だったときに、応答のどこで止まったのかを一言で返す。

    parse_groups が空を返す事情は「モデルがまとめる根拠を見つけなかった」
    （正常）から「出力が途中で切れてバッチを丸ごと捨てた」まで幅があり、
    **どれも同じ空配列になってしまう**。ログに出す文面だけでも分けておかないと、
    29 リクエストのうち何本が本当に静かに捨てられたのか後から分からない。

    **どの分岐にも当てはまらないときに「理由不明」で潰さない。** 知らない
    finish_reason が増えても、その値がそのままログに出るようにしてある
    （潰すと、原因の分からない「提案なし」が黙って積み上がる）。
    """
    choices = response.get("choices") or []
    if not choices:
        # post() が 400 系を例外にしているのでここには来ないはずだが、
        # 応答の形が想定と違うときのために error も見ておく。
        error = response.get("error")
        if isinstance(error, dict) and error.get("message"):
            return f"応答がエラーでした（{str(error.get('message'))[:200]}）"
        return "応答に choices がありません"
    choice = choices[0] if isinstance(choices[0], dict) else {}
    message = choice.get("message")
    if not isinstance(message, dict):
        message = {}
    finish = str(choice.get("finish_reason") or "")
    if finish == "length":
        return (
            "出力上限（max_completion_tokens）で切れました。JSON が途中で終わっています"
        )
    if message.get("refusal"):
        return f"モデルが回答を拒否しました（refusal: {str(message['refusal'])[:100]}）"
    if finish == "content_filter":
        return "コンテンツフィルタで止まりました（finish_reason=content_filter）"
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        return f"応答に content がありません（finish_reason={finish or '不明'}）"
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        # response_format が効かずに素の文章が返ったときがこれ。strict を
        # 受けないモデルに --model で切り替えると起きる（DEFAULT_MODEL の説明）。
        return f"content が JSON として読めませんでした（{exc}）"
    if not isinstance(parsed, dict):
        return "content の JSON がオブジェクトではありません"
    if not parsed.get("groups"):
        if finish and finish != "stop":
            return f"finish_reason={finish}"
        return "モデルがグループを1つも出しませんでした（まとめる根拠が無いという判断）"
    # groups はあるのに parse_groups が空を返した = 中身が dict でない。
    return f"groups の中身が想定の形ではありません（finish_reason={finish or '不明'}）"


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


def allowed_canonicals(
    cluster: dict, evidence: dict[str, list[dict]], field: str
) -> set[str]:
    """そのクラスタで canonical に選んでよい文字列。

    3系統ある。**どれも1文字も創作していない**（実データか外部 API の応答から
    そのまま採るか、注記を機械的に剥がすだけ）ことが、この関数の存在理由。

      ① candidates[].raw … 生表記そのまま
      ② api_results[].title … 外部 API が返した正式名称・シリーズ記事名
         （「ナナシス」→「Tokyo 7th シスターズ」）
      ③ ②の生表記から注記を剥がした形 … **work のときだけ**

    ③ を足したのは、もっともらしい正準名が生表記のどこにも無いクラスタが実際に
    あるため。「その着せ替え人形は恋をする 2期」と「その着せ替え人形は恋をする OP」
    しか無いクラスタでは、①②のどちらを選んでも注記付きの名前が正準名になる
    （「ふつうの軽音部 劇中曲」「君のことが大大大大大好きな100人の彼女 2期 ED」も同じ）。
    剥がすのは rules.strip_notes の規則で書けるものだけなので、「正式名称はこう
    あるべき」という**推測は依然として禁止**のまま。

    artist で ③ を足さないのは、strip_notes が元ネタ列の注記（OP・ED・「2期」）を
    落とす関数で、アーティスト名に当てると末尾が「劇場版」「映画」「楽曲」で終わる
    名義を削りかねないため。block.py の Value.to_json が base を work にだけ載せて
    いるのと同じ理由。
    """
    raws = [v.get("raw", "") for v in cluster.get("values", [])]
    out = set(raws) | evidence_titles(cluster, evidence)
    if field == "work":
        out |= {rules.strip_notes(r) for r in raws if r}
    out.discard("")
    return out


def check_group(
    group: dict,
    cluster: dict | None,
    evidence: dict[str, list[dict]],
    keep_apart: set[frozenset[str]],
    field: str = "work",
) -> str:
    """提案1件を検証する。問題なければ空文字、あれば捨てる理由を返す。

    ここは「LLM がプロンプトを守らなかったとき」に効く。守られている限り何も
    起きないが、守られなかったときに黙って辞書の手前まで通すわけにいかない。

    field は canonical に許す文字列の範囲を決めるためだけに要る
    （allowed_canonicals の説明を参照）。
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
    if canonical not in allowed_canonicals(cluster, evidence, field):
        return (
            "canonical が候補にも API の結果にも注記を剥がした形にも無い（創作）: "
            f"「{canonical}」"
        )

    # 重複を除いた実際の同値クラス。canonical が API 由来か注記を剥がした形なら
    # plays.json に無いので、keep_apart の検査からは外す（実データの組ではない）。
    values = list(dict.fromkeys(variants + ([canonical] if canonical in raws else [])))
    # 中身が無い提案を弾く。数えるのは**canonical を含めた同値クラスの大きさ**で、
    # 生表記が1つでも canonical がそれと違えば「その表記を別の名前に寄せる」という
    # 中身がある（「ふつうの軽音部 劇中曲」→「ふつうの軽音部」、
    # 「ナナシス」→「Tokyo 7th シスターズ」）。raws に無い canonical を values から
    # 外しているのは keep_apart の検査のためなので、こちらでその集合を数えると
    # **API 由来・注記剥がしの正準名を持つ1件の提案が丸ごと消える**。
    if len({*variants, canonical}) < 2:
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


# 日本語の文とみなすひらがなの割合。実測（_proposed/works.toml の 136 件）では、
# 英語で返ってきた3件が 0.004〜0.016、日本語の 133 件が 0.072 以上ときれいに
# 分かれた。その間に置いてある。
HIRAGANA_MIN_RATIO = 0.04


def looks_japanese(text: str) -> bool:
    """日本語の文になっているか。**ひらがなの割合**で見る。

    「かなが1文字でもあれば日本語」では足りない。英語で書かれた理由も、規則4が
    求める引用のために作品名をそのまま載せるので、カタカナと漢字は普通に混ざる
    （実データの「API results for VOCALOID, ボカロ, ボーカロイド all redirect to
    the Wikipedia/Wikidata entry ...」がまさにこれで、この基準では日本語になって
    しまう）。助詞の「が」「の」「で」が並ぶひらがなだけが、日本語の文であることの
    印になる。

    **提案を捨てるための関数ではない。** 英語で返ってきた件数をログに出すためだけの
    もので、捨てる側に回すと規則4を守った中身のある提案まで言語だけを理由に消える
    （理由の文面はレビュー画面で人間が直せるが、消えた提案は戻ってこない）。
    割合で見る以上どこかで外すが、外しても起きるのはログの1行だけ。
    """
    if not text:
        return False
    # U+3041..U+309F = ひらがなブロック。境界の文字は未割り当てだったり
    # 見分けの付かない記号だったりするので、文字リテラルでは書かない。
    hiragana = sum(1 for c in text if 0x3041 <= ord(c) <= 0x309F)
    return hiragana / len(text) >= HIRAGANA_MIN_RATIO


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

    token = token or os.environ.get("GROQ_API_KEY") or ""
    prepared = plan(field, limit=limit, size=size)
    evidence = prepared["evidence"]
    keep_apart = block.load_keep_apart()

    entries: list[dict[str, Any]] = []
    rejected: list[str] = []
    # API が確率的に失敗して丸ごと諦めたバッチ（json_validate_failed）。
    # 提案が減ったのが「まとめる根拠が無かった」からなのか「投げ損ねた」からなのか
    # を区別できないと、次に回すべきかどうかが判断できない。
    skipped: list[str] = []
    cached_hits = 0
    calls = 0

    for i, batch in enumerate(prepared["batches"], start=1):
        by_id = {c.get("id"): c for c in batch["clusters"]}
        body = request_body(model, prepared["system"], batch["user"], field=field)
        response = cached_response(body)
        if response is None:
            if not token:
                raise AliasError(
                    "GROQ_API_KEY がありません（--dry-run なら不要です）。"
                    "GitHub Actions ではリポジトリの secrets に GROQ_API_KEY を"
                    "手で登録し、env で渡してください"
                )
            say(f"  [{i}/{len(prepared['batches'])}] 送信中… 推定 {batch['tokens']} tok")
            try:
                response = post(body, token)
            except AliasError as exc:
                # **json_validate_failed だけは、そのバッチを飛ばして先へ進む。**
                # strict の制約付きデコードが JSON を組み立て切れなかったときに
                # 400 で返るもので、同じ入力でも通ったり落ちたりする確率的な失敗
                # （Groq 側でも1割ほど出ると報告がある）。ここで実行全体を落とすと、
                # **成功済みのバッチまで道連れになる** — Actions は毎回新品の
                # ランナーで data/raw/llm/ のキャッシュが残らないので、次の実行も
                # 1件目からやり直しになり、確率的に必ずどこかで落ちる以上、
                # 永久に完走しない。parse_groups が読めない応答を捨てて続けるのと
                # 同じ扱いにする（1バッチ諦めるほうが、全部失うより安い）。
                #
                # 他のエラー（401 / 413 / クォータ切れなど）は systematic で、
                # 続けても同じところで落ちるだけなので従来どおり上げる。
                if "json_validate_failed" not in str(exc):
                    # 途中で落ちたことと、どこまで進んだかを出してから上げる。
                    # **提案ファイルは書かない。** 途中までの結果で write_proposals
                    # を呼ぶと、前回まとめて作った提案を部分的な結果で上書きして
                    # しまう（あれは追記ではなく毎回の書き直し）。
                    say(
                        f"  ✗ {i - 1}/{len(prepared['batches'])} バッチまで成功した"
                        f"ところで中断しました（提案 {len(entries)} 件は"
                        "**書き出しません**。途中の結果で既存の提案を潰さないため）"
                    )
                    raise
                skipped.append(f"[{i}] {exc}")
                say(f"  [{i}] スキップ: JSON の生成に失敗（json_validate_failed）")
                continue
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
            problem = check_group(group, cluster, evidence, keep_apart, field)
            if problem:
                label = (group.get("cluster_id") or "?") + " " + str(group.get("canonical"))
                rejected.append(f"{label}: {problem}")
                continue
            entries.append(to_entry(group, model))

    # クラスタの並び（行数の多い順）に揃える。LLM が返す順に依らせないと、
    # 同じキャッシュから毎回同じバイト列にならない。
    order = {c.get("id"): n for n, c in enumerate(load_clusters(field))}
    entries.sort(key=lambda e: order.get(e["id"], len(order)))

    # 理由が日本語で書かれていないもの。**捨てずに数えて出すだけ。**
    # プロンプト（規則4・出力節）と出力スキーマの description で日本語を要求して
    # いるが、gpt-oss はそれでも英語で返すことがある。黙って混ざると、レビュー
    # 画面で英語の理由をひとつずつ書き直すことになって初めて気づく。
    non_japanese = [
        f"{e['id']} {e['canonical']}" for e in entries if not looks_japanese(e["reason"])
    ]

    path = store.write_proposals(field, entries)
    for line in rejected:
        say(f"  捨てた提案: {line}")
    if non_japanese:
        say(
            f"  ⚠ 理由が日本語で書かれていない提案 {len(non_japanese)} 件"
            "（提案自体は残してあります。文面はレビュー画面で直せます）:"
        )
        for line in non_japanese:
            say(f"    {line}")
    if skipped:
        # 黙って減らさない。**もう一度回せばこのバッチだけ拾い直せる**
        # （確率的な失敗なので、次は通ることが多い。成功したぶんは
        # data/raw/llm/ のキャッシュに載っているのでネットワークには出ない）。
        say(f"  ⚠ 投げ損ねたバッチ {len(skipped)} 件（もう一度回すと拾い直せます）:")
        for line in skipped:
            say(f"    {line}")
    return {
        "path": store.rel_to_repo(path),
        "clusters": prepared["total"],
        "requests": len(prepared["batches"]),
        "calls": calls,
        "cached": cached_hits,
        "proposed": len(entries),
        "rejected": rejected,
        "skipped": skipped,
        "nonJapanese": non_japanese,
    }
