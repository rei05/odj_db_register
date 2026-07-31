"""名寄せ関連のサブコマンド。

    uv run python -m odj.aliases block --field work
    python3 -m odj.aliases decide --json '<判断1件>'   # '-' で標準入力から
    python3 -m odj.aliases export

odj.build とは独立した手動コマンドにしてある。build は Drive を見に行くが、
こちらは web/public/data/plays.json と data/aliases/ しか読まないので、
ネットワークが無くても（そして GitHub Actions 上でも）動く。

decide と export はレビュー GUI（web/ の dev サーバーのミドルウェア）から
execFile で呼ばれる。**結果は JSON を標準出力に1行**だけ出す約束で、
失敗しても標準出力は JSON（`{"ok":false,"error":…}`）＋終了コード 1 にする。
想定外の例外だけは握り潰さずそのまま落として、stderr を呼び出し側に見せる。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from odj import paths
from odj.aliases import block, llm, rules, sources, store
from odj.aliases.store import AliasError

_ACTIONS = ("accept", "reject", "defer", "keep-apart")

# 元ネタの種別。work 以外（ボカロ曲、VTuber、ODJ 自体のネタ、アーティスト名が
# 元ネタ列に入っているもの）を分けておくと、後で検索の重み付けに使える。
_KINDS = ("work", "vocaloid", "vtuber", "odj-self", "artist-as-work", "unknown")

_CONFIDENCE = ("high", "medium", "low")


def _cmd_block(args: argparse.Namespace) -> int:
    plays = block.load_plays()
    fields = [args.field] if args.field != "all" else list(block.FIELD_KEYS)
    for name in fields:
        result = block.build(name, plays)
        out = block.write(name, result)
        clusters = result["clusters"]
        split = [c for c in clusters if "split-from-large" in c["hints"]]
        risky = [c for c in clusters if "series-risk" in c["hints"]]
        print(f"[{name}] {out}")
        print(
            f"  値 {result['totalValues']} 種 → "
            f"クラスタ {len(clusters)} 個（{result['clustered']} 種）/ "
            f"単独 {result['singletons']} 種"
        )
        print(
            f"  うち 大きな塊を割った破片 {len(split)} 個 / "
            f"部分一致で繋がった要注意 {len(risky)} 個"
        )
        for c in clusters[:5]:
            names = "、".join(v["raw"] for v in c["values"])
            hints = f" [{','.join(c['hints'])}]" if c["hints"] else ""
            print(f"    {c['id']} {c['rows']}行{hints}: {names}")
    return 0


# ---------------------------------------------------------------------------
# fetch（生表記を外部 API で裏取りする。書くのは evidence.<field>.json だけ）
# ---------------------------------------------------------------------------


def _cmd_fetch(args: argparse.Namespace) -> int:
    fields = [args.field] if args.field != "all" else list(block.FIELD_KEYS)
    for name in fields:
        try:
            result = sources.fetch(name, only_new=args.only_new, limit=args.limit)
        except sources.SourceError as exc:
            print(f"[{name}] 失敗: {exc}", file=sys.stderr)
            return 1
        print(f"[{name}] {store.rel_to_repo(result['path'])}")
        print(
            f"  裏取り対象 {result['candidateTotal']} 件"
            + (f"（既知 {result['skipped']} 件を飛ばした）" if result["skipped"] else "")
            + f" のうち {result['fetched']} 件を実行"
        )
        print(
            f"  ヒット {result['hits']} / 空 {result['misses']}"
            f"（evidence.json 全体では {result['totalEvidence']} 件）"
        )
        if result["failed"]:
            # 裏取りに失敗した値は空として記録済み。ここで名前を出しておかないと
            # 「ヒット無し」と区別が付かない。
            names = "、".join(result["failed"][:5])
            more = f" ほか{len(result['failed']) - 5}件" if len(result["failed"]) > 5 else ""
            print(f"  取得に失敗（空として記録）: {names}{more}")
    return 0


# ---------------------------------------------------------------------------
# ask（LLM に提案を作らせる。書くのは _proposed/ だけで、辞書には触らない）
# ---------------------------------------------------------------------------


def _print_plan(prepared: dict, model: str, dry_run: bool) -> None:
    """--dry-run の中身。**ネットワークに出ないまま**投げるものを全部見せる。

    実際に投げるのと同じ文字列を出すことが要件。要約を見せても「リクエスト数や
    推定トークンが妥当か」「keep_apart が本当に埋まっているか」の確認にならないので、
    システムプロンプトも各バッチの入力も全文を出す。
    """
    field = prepared["field"]
    plans = prepared["batches"]
    tokens = [b["tokens"] for b in plans] or [0]
    ev = prepared["evidence"]
    print(f"[{field}] {prepared['total']} クラスタ → {len(plans)} リクエスト")
    print(f"  モデル: {model}（1リクエスト 入力 {llm.SAFE_INPUT_TOKENS}×"
          f"{llm.TOKEN_ESTIMATE_SLACK} + 出力 {llm.MAX_OUTPUT_TOKENS} tok / "
          f"Groq の TPM {llm.TPM_LIMIT} 以内）")
    print(f"  裏取り: {'evidence ' + str(len(ev)) + ' 値' if ev else 'evidence 無し（省略して続行）'}")
    print(f"  システムプロンプト: 推定 {prepared['systemTokens']} tok")
    print(f"  推定トークン: 合計 {sum(tokens)} / 1回あたり最大 {max(tokens)}")
    sizes = [len(b["clusters"]) for b in plans] or [0]
    print(f"  1回あたりのクラスタ数: {min(sizes)}〜{max(sizes)}")
    # 入力と出力の合計が TPM を超えるバッチは、どの1分にも収まらないので必ず
    # 413 になる。**推定は下振れする**ので、判定には TOKEN_ESTIMATE_SLACK を
    # 掛けた値を使う（実際に推定 4,167 が実測 4,637 だった例がある）。
    over = [
        i
        for i, b in enumerate(plans, start=1)
        if b["tokens"] * llm.TOKEN_ESTIMATE_SLACK + llm.MAX_OUTPUT_TOKENS > llm.TPM_LIMIT
    ]
    if over:
        print(f"  ⚠ 出力と合わせて TPM {llm.TPM_LIMIT} を超える見込みのバッチ: {over}"
              "（413 で落ちます。プロンプトを削るしかありません）")
    if not dry_run:
        return
    print(f"\n===== システムプロンプト（{prepared['systemTokens']} tok） =====")
    print(prepared["system"])
    for i, b in enumerate(plans, start=1):
        print(f"\n===== バッチ {i}/{len(plans)}"
              f"（{len(b['clusters'])} クラスタ・推定 {b['tokens']} tok） =====")
        print(b["user"])
    print(f"\n[{field}] --dry-run のためネットワークには出ていません。"
          f"{len(plans)} リクエスト / 推定 合計 {sum(tokens)} tok")


def _cmd_ask(args: argparse.Namespace) -> int:
    try:
        prepared = llm.plan(args.field, limit=args.limit, size=args.batch_size)
        _print_plan(prepared, args.model, args.dry_run)
        if args.dry_run:
            return 0
        result = llm.ask(
            args.field,
            limit=args.limit,
            size=args.batch_size,
            model=args.model,
            log=print,
        )
    except AliasError as exc:
        print(f"失敗: {exc}", file=sys.stderr)
        return 1
    print(f"[{args.field}] {result['path']}")
    print(f"  提案 {result['proposed']} 件 / 捨てた {len(result['rejected'])} 件"
          f"（呼び出し {result['calls']} 回・キャッシュ {result['cached']} 回）")
    return 0


# ---------------------------------------------------------------------------
# decide
# ---------------------------------------------------------------------------


def _text(payload: dict, key: str) -> str:
    value = payload.get(key)
    return value.strip() if isinstance(value, str) else ""


def _string_list(payload: dict, key: str) -> list[str]:
    """文字列の配列を、順を保ったまま重複と空文字を落として取り出す。"""
    value = payload.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise AliasError(f"{key} は配列である必要があります")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise AliasError(f"{key} には文字列以外を入れられません")
        item = item.strip()
        if item and item not in out:
            out.append(item)
    return out


def _blocked_pair(a: str, b: str, keep_apart: set[frozenset[str]]) -> bool:
    """keep_apart.toml が「別物」と決めた組か。

    block.build_edges() が辺を張らない条件と同じにしてある。生の組だけを見ると
    「アイカツ! 楽曲」と「アイカツスターズ」のような注記違いの迂回路をすり抜ける。
    """
    if frozenset((a, b)) in keep_apart:
        return True
    ka = rules.agg_key(rules.strip_notes(a))
    kb = rules.agg_key(rules.strip_notes(b))
    return bool(ka and kb and ka != kb and frozenset((ka, kb)) in keep_apart)


def _auto_where(field: str, cluster_id: str) -> str:
    """where 省略時に、クラスタの events / djs から「第2回 せーや ほか」を作る。

    出典は block が書いた out/aliases/clusters.<field>.json。無ければ諦める
    （where は追跡のための注記なので、埋まらなくても判断は成立する）。
    """
    path = paths.OUT_ALIASES_DIR / f"clusters.{field}.json"
    if not path.exists():
        return ""
    try:
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return ""
    cluster = next((c for c in data.get("clusters", []) if c.get("id") == cluster_id), None)
    if cluster is None:
        return ""
    events: set[int] = set()
    djs: set[str] = set()
    for value in cluster.get("values", []):
        events.update(value.get("events", []))
        djs.update(value.get("djs", []))
    # 「第2回 せーや ほか」「第2回〜第10回 tri ほか」の形。数え上げではなく
    # 後で人が現物に当たるための手掛かりなので、範囲と代表1人で足りる。
    order = sorted(events)
    head = ""
    if order:
        head = f"第{order[0]}回" if len(order) == 1 else f"第{order[0]}回〜第{order[-1]}回"
    name = sorted(djs)[0] if djs else ""
    if len(djs) > 1:
        name += " ほか"
    return " ".join(x for x in (head, name) if x)


def _check_already_decided(field: str, values: list[str]) -> None:
    """同じ値を二度判断させない。

    見るのはクラスタ id ではなく**個々の生表記**。id で弾くと、1枚のカードで
    一度判断した時点で残りの値を扱えなくなる。実データの「とある」系のように
    1枚に複数の作品が混じるカードや、artist 側のように1枚から複数のグループを
    作るのが常態のカードでは、部分採用を繰り返せる必要がある。

    defer と keep-apart は値を判断していないので、記録があっても素通しする
    （defer は「まだ決めない」、keep-apart は「この2つは別物」と決めただけ）。
    """
    if not values:
        return
    done: dict[str, dict] = {}
    for rec in store.load_decisions():
        if rec.get("field") != field or rec.get("action") not in ("accept", "reject"):
            continue
        for raw in rec.get("variants") or []:
            done.setdefault(raw, rec)
    for raw in values:
        hit = done.get(raw)
        if hit is not None:
            raise AliasError(
                f"「{raw}」は既に判断済みです"
                f"（{hit.get('action')} / {hit.get('at', '時刻不明')}）",
                code="already-decided",
            )


def _accept(payload: dict, field: str, cluster_id: str, reason: str, where: str) -> list[str]:
    variants = _string_list(payload, "variants")
    if not variants:
        raise AliasError("variants は1つ以上必要です")
    canonical = _text(payload, "canonical")
    if not canonical:
        raise AliasError("canonical は必須です")

    kind = _text(payload, "kind")
    if field == "work" and not kind:
        raise AliasError("kind は必須です（" + " / ".join(_KINDS) + "）")
    if kind and kind not in _KINDS:
        raise AliasError(f"kind が不正です: {kind}（{' / '.join(_KINDS)}）")

    confidence = _text(payload, "confidence") or "high"
    if confidence not in _CONFIDENCE:
        raise AliasError(f"confidence が不正です: {confidence}（{' / '.join(_CONFIDENCE)}）")

    # 正準名の創作を許さない。ただし**既に辞書にある正準名は創作ではない**ので許す。
    # 新しい開催回で「ラブライブ！」（全角）が現れたとき、判断済みの
    # 「ラブライブ!」に足せないと、その表記は永久に検索から漏れる。
    # variants だけに限っていた頃はこれができず、追加された表記が
    # レビュー対象外のまま溜まっていく状態だった。
    proposal = store.load_proposals(field).get(cluster_id, {})
    existing = store.load_entries(field)
    allowed = set(variants)
    allowed.update(
        (e.get("canonical") or "").strip() for e in existing if e.get("canonical")
    )
    if proposal:
        allowed.add((proposal.get("canonical") or "").strip())
        allowed.update(v.strip() for v in proposal.get("variants", []) if isinstance(v, str))
    if canonical not in allowed:
        raise AliasError(
            f"canonical は variants・提案・既存の辞書から選んでください: 「{canonical}」は"
            "どれにもありません（実データに無い表記は作れません）"
        )

    # 人間が「別物」と決めた組を含んでいないか。keep_apart のほうが常に強い。
    keep_apart = block.load_keep_apart()
    values = variants + ([canonical] if canonical not in variants else [])
    for i in range(len(values)):
        for j in range(i + 1, len(values)):
            if _blocked_pair(values[i], values[j], keep_apart):
                raise AliasError(
                    "keep_apart.toml で別物と決めた組が含まれています: "
                    f"「{values[i]}」と「{values[j]}」",
                    code="keep-apart",
                )

    # 同じ表記を別の正準名にも寄せていないか。両方が生きると検索が割れる。
    index = store.variant_index(store.load_entries(field))
    for raw in values:
        if raw in index and index[raw] != canonical:
            raise AliasError(
                f"「{raw}」は既に「{index[raw]}」に寄せられています"
                f"（今回は「{canonical}」）。先に既存の項目を直してください",
                code="conflict",
            )

    entry: dict[str, Any] = {
        "id": cluster_id,
        "canonical": canonical,
        "series": _text(payload, "series"),
        "kind": kind,
        "variants": variants,
        # 人間が GUI で1件ずつ見た結果だけがここを true にする。
        "approved": True,
        "confidence": confidence,
        "source": _text(payload, "source") or "human",
        "where": where,
        "decided_at": store.now_stamp(),
        "reason": reason,
    }
    written = store.append_entry(field, entry)
    return [store.rel_to_repo(written)]


def _keep_apart(payload: dict, reason: str, where: str) -> tuple[list[str], list[dict]]:
    raw_pairs = payload.get("pairs")
    if not isinstance(raw_pairs, list) or not raw_pairs:
        raise AliasError("pairs は1組以上必要です")
    pairs: list[dict] = []
    for pair in raw_pairs:
        if not isinstance(pair, dict):
            raise AliasError("pairs の要素は {a, b} のオブジェクトです")
        a = (pair.get("a") or "").strip() if isinstance(pair.get("a"), str) else ""
        b = (pair.get("b") or "").strip() if isinstance(pair.get("b"), str) else ""
        if not a or not b:
            raise AliasError("pairs の a と b は必須です")
        if a == b:
            raise AliasError(f"同じ表記どうしは分けられません: 「{a}」")
        if {"a": a, "b": b} not in pairs:
            pairs.append({"a": a, "b": b})
    written, _added = store.append_keep_apart(pairs, reason, where or None)
    return [store.rel_to_repo(written)], pairs


def decide(payload: dict) -> dict[str, Any]:
    """判断1件を確定する。**辞書を書き換えるのはここだけ。**"""
    field = _text(payload, "field")
    if field not in store.FIELDS:
        raise AliasError(f"field は work か artist です: {field or '（空）'}")
    cluster_id = _text(payload, "id")
    if not cluster_id:
        raise AliasError("id は必須です")
    action = _text(payload, "action")
    if action not in _ACTIONS:
        raise AliasError(f"action が不正です: {action or '（空）'}（{' / '.join(_ACTIONS)}）")
    reason = _text(payload, "reason")
    if not reason:
        raise AliasError("理由は必須です")

    # 値を判断する action だけ、その値が既に決着していないかを見る。
    # keep-apart は「この2つは別物」と決めるだけ、defer は「まだ決めない」なので素通し。
    if action in ("accept", "reject"):
        _check_already_decided(field, _string_list(payload, "variants"))
    where = _text(payload, "where") or _auto_where(field, cluster_id)

    record: dict[str, Any] = {"id": cluster_id, "field": field, "action": action}
    wrote: list[str] = []

    if action == "accept":
        wrote += _accept(payload, field, cluster_id, reason, where)
        record["variants"] = _string_list(payload, "variants")
        record["canonical"] = _text(payload, "canonical")
    elif action == "keep-apart":
        paths_written, pairs = _keep_apart(payload, reason, where)
        wrote += paths_written
        # variants は書かない。「この2つは別物」と決めただけで、それぞれの値が
        # 他の表記と統合できるかはまだ未判断のため（block.load_decided が
        # variants を見て候補から外してしまう）。
        record["pairs"] = pairs
    elif action == "reject":
        # 却下した値は候補から外したい（block.load_decided が variants を見る）。
        # 契約では accept のときだけ必須だが、送られていれば記録する。
        variants = _string_list(payload, "variants")
        if variants:
            record["variants"] = variants
    # defer は variants を書かない。次回もキューに出す約束。

    record["reason"] = reason
    if where:
        record["where"] = where
    record["at"] = store.now_stamp()
    wrote.append(store.rel_to_repo(store.append_decision(record)))
    return {"wrote": wrote}


def _read_payload(arg: str) -> dict:
    """--json の中身。'-' なら標準入力から読む。

    日本語を含む JSON をシェルのクォートに通すと事故る（引用符・改行・全角空白）
    ので、GUI からは標準入力で渡す想定。
    """
    text = sys.stdin.read() if arg == "-" else arg
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AliasError(f"JSON として読めません: {exc}") from exc
    if not isinstance(payload, dict):
        raise AliasError("JSON のトップレベルはオブジェクトである必要があります")
    return payload


def _emit(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def _cmd_decide(args: argparse.Namespace) -> int:
    try:
        result = decide(_read_payload(args.json))
    except AliasError as exc:
        # code は GUI が種別で分岐するため（文面で判定すると、種類の違う失敗が
        # 同じ扱いになる。store.AliasError の説明を参照）。
        _emit({"ok": False, "code": exc.code, "error": str(exc)})
        return 1
    _emit({"ok": True, **result})
    return 0


def _cmd_export(args: argparse.Namespace) -> int:
    try:
        result = store.export_json()
    except AliasError as exc:
        _emit({"ok": False, "code": exc.code, "error": str(exc)})
        return 1
    _emit(result)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="odj.aliases", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_block = sub.add_parser("block", help="候補クラスタを作る（ネットワーク不要）")
    p_block.add_argument(
        "--field",
        choices=[*block.FIELD_KEYS, "all"],
        default="work",
        help="対象フィールド（既定: work）",
    )
    p_block.set_defaults(func=_cmd_block)

    p_fetch = sub.add_parser(
        "fetch", help="候補の生表記を外部 API で裏取りする（ネットワークが要る）"
    )
    p_fetch.add_argument(
        "--field",
        choices=[*block.FIELD_KEYS, "all"],
        default="work",
        help="対象フィールド（既定: work）",
    )
    p_fetch.add_argument(
        "--only-new", action="store_true",
        help="辞書（works.toml/artists.toml）と既存の evidence に無い値だけを引く",
    )
    p_fetch.add_argument(
        "--limit", type=int, default=None, metavar="N",
        help="裏取りする件数の上限（レート制限があるので試すときはまずここを絞る）",
    )
    p_fetch.set_defaults(func=_cmd_fetch)

    p_ask = sub.add_parser(
        "ask", help="候補クラスタを LLM に投げて _proposed/ に提案を書く（辞書には触らない）"
    )
    p_ask.add_argument(
        "--field", choices=list(block.FIELD_KEYS), default="work",
        help="対象フィールド（既定: work）",
    )
    p_ask.add_argument(
        "--dry-run", action="store_true",
        help="ネットワークに出ず、リクエスト数・推定トークン数・プロンプト全文だけ出す",
    )
    p_ask.add_argument(
        "--limit", type=int, default=None, metavar="N",
        help="先頭 N クラスタ（行数の多い順）だけ投げる。少量で試すとき用。"
             "_proposed/ は毎回作り直すので、限った実行の結果で上書きされる",
    )
    p_ask.add_argument(
        "--batch-size", type=int, default=llm.BATCH_SIZE, metavar="N",
        help=f"1リクエストに詰めるクラスタ数（既定: {llm.BATCH_SIZE}）",
    )
    p_ask.add_argument(
        "--model", default=llm.DEFAULT_MODEL,
        help=f"Groq のモデル名（既定: {llm.DEFAULT_MODEL}）。"
             "スキーマ強制（strict な json_schema）が効くのは gpt-oss 系だけなので、"
             "他のモデルに差し替えると提案が静かに質を落とす",
    )
    p_ask.set_defaults(func=_cmd_ask)

    p_decide = sub.add_parser("decide", help="判断1件を辞書に書く（人間の判断だけ）")
    p_decide.add_argument(
        "--json",
        required=True,
        metavar="JSON",
        help="判断1件の JSON。'-' なら標準入力から読む",
    )
    p_decide.set_defaults(func=_cmd_decide)

    p_export = sub.add_parser(
        "export", help="承認済みの辞書だけを web/public/data/aliases.json に書く"
    )
    p_export.set_defaults(func=_cmd_export)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
