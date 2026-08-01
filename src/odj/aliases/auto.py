"""LLM の提案のうち、規則で安全と言い切れるものだけを人手を通さずに承認する。

    PYTHONPATH=src python3 -m odj.aliases auto --field work --dry-run
    PYTHONPATH=src python3 -m odj.aliases auto --field work
    PYTHONPATH=src python3 -m odj.aliases auto --field work --undo

読むのは out/aliases/clusters.<field>.json と data/aliases/ だけで、**ネットワーク
には出ない**。書くのは data/aliases/<field>s.auto.toml と decisions.jsonl の2つで、
人が育てる works.toml / artists.toml には触らない。

**なぜ要るか。** work の候補は 150 クラスタあり、うち 133 に LLM の提案が付いていて、
その 129 は「クラスタ全体を1つのグループにまとめる」提案である。npm run review は
1クラスタ1カードで人間に見せるので、大半のカードが「提案のとおり」を押すだけの
作業になっていた。下の規則に合致した 71 クラスタ（377 行・187 表記）は全件を目視
して誤りが1件も無いことを確認済みで、その確認をもって規則ごと承認されている。

**なぜ全部は自動化しないか。** 同じ目視で、hints に `artist-as-work`（元ネタ列に
アーティスト名が入っている）が付いた 8 件には誤った統合が混ざっていた。

    「星街すいせい ← さくらみこ」
    「森カリオペ ← がうるぐら&森カリオペ」
    「Yunomi ← Yunomi & nicamoq」

どれも sources.py の redirect_edges が「ユーザーが明示的に禁じた統合」として挙げて
いる合同名義の分解と同型で、辺の種別や文字列の見た目からは見分けが付かない。
`split-from-large`（部分一致の数珠つなぎでできた大きな塊を割った破片）も同じ理由で
人間に回す。

逆に `series-risk`（部分一致 substr 由来）は**除外理由にしない**。work は同じ
ブランド名を冠する作品をまとめる方針なので、ここに落ちる組の大半は正しい統合に
なる（block.py の冒頭を参照）。単独で弾くと自動承認できるクラスタがほとんど
残らないうえ、残った目視の中身も「正しい統合を承認するだけ」の作業に戻る。

取り消しは `--undo`。*.auto.toml を空にするだけで、その値は再び未判断（＝候補）に
戻る（block.load_decided の説明を参照）。人間の判断は decisions.jsonl に追記されて
消えないので、**取り消せるのは自動承認したぶんだけ**であり、そこが人間の判断と
別ファイル・別 action になっている理由である。
"""

from __future__ import annotations

from typing import Any

from odj.aliases import block, llm, rules, store
from odj.aliases.store import AliasError

# 自動承認してよい辺の種別。block.build_edges と redirect_edges が張るもののうち、
# 「同じものを指している」根拠として説明が付くものだけ。
#
# cooccur（artist だけ。同じ曲名に別のアーティスト名が付いていた）が入っていない
# のは、これが「同じ人」の根拠として弱く、合同名義（「がうるぐら&森カリオペ」）と
# 単独名義が同じ曲に並ぶ形とも見分けが付かないため。知らない種別が増えたときも
# 自動では通さない（規則を書いた時点で確認していない根拠だから）。
KNOWN_EDGE_KINDS = frozenset({"caseonly", "agg", "bigram", "redirect", "edit", "substr"})

# このヒントが付いたクラスタは自動承認しない（モジュールの docstring を参照）。
RISKY_HINTS = ("split-from-large", "artist-as-work")

# 除外理由のコードと、人に見せる日本語。--dry-run の内訳はこの順で出す
# （「提案が無い」→「提案の形が合わない」→「中身が危ない」→「既に決まっている」）。
SKIP_LABELS: dict[str, str] = {
    "no-proposal": "LLM の提案が無い",
    "many-proposals": "提案が2件以上ある（クラスタを割る提案）",
    "partial": "提案がクラスタ全体を覆っていない（部分採用）",
    "confidence": "confidence が high でない",
    "canonical": "canonical が生表記にも注記を剥がした形にも無い",
    "hint": "危険なヒントが付いている",
    "edge-kind": "自動承認に使えない辺の種別が含まれる",
    "keep-apart": "keep_apart.toml で別物と決めた組を含む",
    "conflict": "同じ表記が別の正準名に寄っている",
    "already-decided": "既に人間が判断済み",
    "auto-done": "既に自動承認済み",
}


def _raws(cluster: dict) -> list[str]:
    """クラスタのメンバー（生表記）。"""
    return [
        (v.get("raw") or "").strip()
        for v in cluster.get("values", [])
        if (v.get("raw") or "").strip()
    ]


def _variants(proposal: dict) -> list[str]:
    """提案の variants。順は提案のまま（辞書に書くときの並びになる）。"""
    out: list[str] = []
    for raw in proposal.get("variants") or []:
        if isinstance(raw, str) and raw.strip() and raw.strip() not in out:
            out.append(raw.strip())
    return out


def _canonical_is_grounded(field: str, canonical: str, variants: list[str]) -> bool:
    """規則4: 正準名の創作を許さない。

    許すのは生表記そのものと、**work のときだけ**注記を剥がした形
    （「その着せ替え人形は恋をする 2期」しか無いクラスタの
    「その着せ替え人形は恋をする」）。cli._accept が許している「提案に書いてある
    名前」「既存の辞書にある正準名」はここでは許さない。前者は提案の canonical
    そのものなので検査にならず、後者は既存の同値クラスへの追加という人間が
    見るべき判断だからである。

    strip_notes を work に限るのは、アーティスト名に当てると末尾が「劇場版」
    「映画」「楽曲」で終わる名義を削りかねないため（block.py の Value.to_json、
    llm.allowed_canonicals、store.check_canonical と同じ線引き）。

    ここを通れば store.check_canonical は必ず通る（許す集合がその部分集合）。
    """
    if canonical in variants:
        return True
    return field == "work" and canonical in {rules.strip_notes(v) for v in variants}


def _entry(field: str, cluster: dict, proposal: dict, variants: list[str]) -> dict[str, Any]:
    """辞書に書く1ブロック。人間の判断（cli._accept）と同じ形にする。"""
    reason = (proposal.get("reason") or "").strip() or "（提案に理由が無い）"
    return {
        "id": (cluster.get("id") or "").strip(),
        "canonical": (proposal.get("canonical") or "").strip(),
        "series": (proposal.get("series") or "").strip(),
        # kind は提案のまま写す（LLM 側のスキーマが cli._KINDS と同じ enum なので、
        # 知らない値は入らない）。artist の提案には元から無い。
        "kind": (proposal.get("kind") or "").strip(),
        "variants": variants,
        # **人間が承認した規則に合致したときだけ true。** 規則はこのモジュールに
        # 1か所だけ書いてあり、外れたクラスタは人間のキューに残る。
        "approved": True,
        "confidence": "high",
        "source": store.AUTO_SOURCE,
        "where": block.where_hint(cluster),
        "decided_at": store.now_stamp(),
        # 前置きを付けるのは、後から grep で「機械が入れた行」を追えるようにする
        # ため（source だけでも分かるが、辞書を読むのは人間で、目に入るのは理由）。
        "reason": store.AUTO_REASON_PREFIX + reason,
    }


def plan(field: str) -> dict[str, Any]:
    """クラスタを1つずつ規則に当てる。**何も書かない。**

    返すのは {field, total, accepted, skipped}。accepted の要素は
    {id, canonical, variants, entry}、skipped の要素は {id, code, detail, values}。
    --dry-run はこれをそのまま人に見せる。
    """
    clusters = llm.load_clusters(field)
    proposals = store.load_proposal_groups(field)
    keep_apart = block.load_keep_apart()
    decided = store.decided_index(field)
    # 承認するたびに更新する。1回の実行で何十件も書くので、途中で作った衝突
    # （同じ表記が2つの正準名に寄る）をファイルの読み直しでは見つけられない。
    index = store.variant_index(store.load_entries(field))
    auto_values = {
        raw
        for entry in store.load_auto_entries(field)
        for raw in store.class_values(entry)
    }

    accepted: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for cluster in clusters:
        cluster_id = (cluster.get("id") or "").strip()
        raws = _raws(cluster)

        def skip(code: str, detail: str = "") -> None:
            skipped.append(
                {"id": cluster_id, "code": code, "detail": detail, "values": raws}
            )

        # 既に決まっているクラスタ。block は「全員が判断済み」のクラスタを落とすが、
        # 一部だけ判断済みのクラスタは残るので、ここでも見る必要がある。
        if any(raw in auto_values for raw in raws):
            skip("auto-done")
            continue
        hit = next((raw for raw in raws if raw in decided), "")
        if hit:
            skip("already-decided", f"「{hit}」")
            continue

        # 規則1: 提案がちょうど1件。
        groups = proposals.get(cluster_id) or []
        if not groups:
            skip("no-proposal")
            continue
        if len(groups) > 1:
            skip("many-proposals", f"{len(groups)} 件")
            continue
        proposal = groups[0]
        variants = _variants(proposal)

        # 規則2: 提案の variants がクラスタ全体と一致する。部分採用・分割提案は
        # 「どれを残すか」の判断が要るので人間に回す。
        if set(variants) != set(raws):
            missing = [r for r in raws if r not in variants]
            extra = [v for v in variants if v not in raws]
            detail = ""
            if missing:
                detail += "提案に無い: " + "、".join(f"「{v}」" for v in missing[:3])
            if extra:
                detail += ("  " if detail else "") + "クラスタに無い: " + "、".join(
                    f"「{v}」" for v in extra[:3]
                )
            skip("partial", detail)
            continue

        # 規則3: confidence が high。
        confidence = (proposal.get("confidence") or "").strip()
        if confidence != "high":
            skip("confidence", confidence or "（空）")
            continue

        # 規則4: 正準名を創作していない。
        canonical = (proposal.get("canonical") or "").strip()
        if not canonical or not _canonical_is_grounded(field, canonical, variants):
            skip("canonical", f"「{canonical}」" if canonical else "（空）")
            continue

        # 規則5: 危ないヒントが付いていない。
        hints = [h for h in cluster.get("hints", []) if h in RISKY_HINTS]
        if hints:
            skip("hint", "、".join(hints))
            continue

        # 規則6: 知っている辺の種別だけでできている。
        unknown = [k for k in cluster.get("edgeKinds", []) if k not in KNOWN_EDGE_KINDS]
        if unknown:
            skip("edge-kind", "、".join(unknown))
            continue

        # 規則7: 人間の decide が通る検査を全部通る（store 側で共有している）。
        values = variants + ([canonical] if canonical not in variants else [])
        try:
            store.check_canonical(field, cluster_id, canonical, variants)
            store.check_keep_apart(values, keep_apart)
            store.check_conflict(values, canonical, index)
        except AliasError as exc:
            skip(exc.code if exc.code in SKIP_LABELS else "canonical", str(exc))
            continue

        entry = _entry(field, cluster, proposal, variants)
        accepted.append(
            {
                "id": cluster_id,
                "canonical": canonical,
                "variants": variants,
                # 効き目の大きさ。何行ぶんの表記ゆれが埋まったかを出力に出す。
                "rows": cluster.get("rows", 0),
                "entry": entry,
            }
        )
        # 次のクラスタの検査に、いま承認したぶんを効かせる。
        for raw in store.class_values(entry):
            index.setdefault(raw, canonical)
            auto_values.add(raw)

    return {
        "field": field,
        "total": len(clusters),
        "accepted": accepted,
        "skipped": skipped,
    }


def _decision(field: str, action: str, entry: dict) -> dict[str, Any]:
    """decisions.jsonl に積む監査ログ1件。

    action を人間用の accept / reject と分けてあるのは、block.load_decided() と
    レビュー GUI の判断済み判定がどちらも accept / reject しか見ないため。
    自動承認を取り消したときに候補へ自然に戻す必要がある。
    """
    record: dict[str, Any] = {
        "id": entry.get("id", ""),
        "field": field,
        "action": action,
        "variants": list(entry.get("variants") or []),
        "canonical": entry.get("canonical", ""),
        "reason": entry.get("reason", ""),
    }
    if entry.get("where"):
        record["where"] = entry["where"]
    record["at"] = store.now_stamp()
    return record


def run(field: str, *, dry_run: bool = False) -> dict[str, Any]:
    """規則に合うクラスタを承認して *.auto.toml に書く。"""
    result = plan(field)
    result["wrote"] = []
    if dry_run or not result["accepted"]:
        return result
    # 既にあるぶんを読んでから連結して丸ごと書き直す（追記だと同じ id が積み上がる）。
    entries = store.load_auto_entries(field) + [a["entry"] for a in result["accepted"]]
    written = store.write_auto_entries(field, entries)
    log = None
    for item in result["accepted"]:
        log = store.append_decision(_decision(field, "auto-accept", item["entry"]))
    result["wrote"] = [store.rel_to_repo(p) for p in (written, log) if p]
    return result


def undo(field: str, *, dry_run: bool = False) -> dict[str, Any]:
    """自動承認したぶんを全部取り消す。

    *.auto.toml を空にするだけ。人が育てる辞書には最初から書いていないので、
    ここで消えるのは機械が入れたぶんだけである。decisions.jsonl には
    auto-undo を積む（履歴は消さない。あとで「いつ取り消したか」を追うため）。
    """
    entries = store.load_auto_entries(field)
    result: dict[str, Any] = {
        "field": field,
        "removed": [
            {
                "id": e.get("id", ""),
                "canonical": e.get("canonical", ""),
                "variants": list(e.get("variants") or []),
            }
            for e in entries
        ],
        "wrote": [],
    }
    if dry_run or not entries:
        return result
    written = store.write_auto_entries(field, [])
    log = None
    for entry in entries:
        log = store.append_decision(_decision(field, "auto-undo", entry))
    result["wrote"] = [store.rel_to_repo(p) for p in (written, log) if p]
    return result
