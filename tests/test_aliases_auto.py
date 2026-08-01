"""統合候補の自動承認（odj.aliases auto）の回帰テスト。

    python3 -m unittest discover -s tests
    python3 -m unittest tests.test_aliases_auto

ここで守りたいのは**規則から外れたクラスタが1つも自動承認されない**ことのほう。
自動承認は approved = true を辞書に書く（＝ export で公開データに出る）操作なので、
拾い漏らしは人間のキューに残るだけで済むが、取りこぼした危険はそのまま公開される。
実データで踏んだ危険（hints に artist-as-work が付いたクラスタに
「星街すいせい ← さくらみこ」のような合同名義の分解が混ざる）は
src/odj/aliases/auto.py の冒頭に書いてある。
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
# sandbox（data/aliases/ を一時ディレクトリに差し替える）は test_aliases.py と共有する。
# discover でも `python3 -m unittest tests.test_aliases_auto` でも import できるよう、
# tests/ 自身を先に sys.path に足しておく。
sys.path.insert(0, str(Path(__file__).resolve().parent))

from odj import paths  # noqa: E402
from odj.aliases import auto, block, cli, store  # noqa: E402
from test_aliases import sandbox  # noqa: E402


def make_cluster(
    cluster_id: str,
    raws: list[str],
    *,
    hints: tuple[str, ...] = (),
    kinds: tuple[str, ...] = ("agg",),
) -> dict:
    """block.build() が書くクラスタ1件。自動承認が見る項目だけ埋める。"""
    return {
        "id": cluster_id,
        "field": "work",
        "rows": len(raws),
        "hints": list(hints),
        "edgeKinds": sorted(kinds),
        "values": [
            {"raw": raw, "rows": 1, "events": [3], "djs": ["ha"]} for raw in raws
        ],
        "edges": [],
    }


def write_clusters(field: str, clusters: list[dict]) -> None:
    paths.OUT_ALIASES_DIR.mkdir(parents=True, exist_ok=True)
    (paths.OUT_ALIASES_DIR / f"clusters.{field}.json").write_text(
        json.dumps({"field": field, "clusters": clusters}, ensure_ascii=False),
        encoding="utf-8",
    )


def make_proposal(
    cluster_id: str,
    canonical: str,
    variants: list[str],
    *,
    confidence: str = "high",
    kind: str = "work",
) -> dict:
    return {
        "id": cluster_id,
        "canonical": canonical,
        "kind": kind,
        "variants": variants,
        "confidence": confidence,
        "source": "llm:openai/gpt-oss-120b",
        "reason": "候補はどちらも同じ作品を指しており、agg で結ばれている。",
    }


def setup_work(
    *,
    raws: list[str] | None = None,
    hints: tuple[str, ...] = (),
    kinds: tuple[str, ...] = ("agg",),
    canonical: str = "アイカツ!",
    variants: list[str] | None = None,
    confidence: str = "high",
    proposals: list[dict] | None = None,
) -> None:
    """1クラスタ + その提案1件、という最小の入力を用意する。"""
    raws = raws if raws is not None else ["アイカツ!", "アイカツ"]
    write_clusters("work", [make_cluster("work-0001", raws, hints=hints, kinds=kinds)])
    if proposals is None:
        proposals = [
            make_proposal(
                "work-0001",
                canonical,
                variants if variants is not None else list(raws),
                confidence=confidence,
            )
        ]
    store.write_proposals("work", proposals)


def run_auto(*argv: str) -> tuple[int, str]:
    """CLI を呼んで (終了コード, 出力) を返す。

    decide / export と違い auto は GUI から呼ばれないので、出力は JSON ではなく
    人間向けの文。失敗の文面は stderr に出るため、まとめて1つの文字列にする
    （テストの実行ログに混ざるのも防げる）。
    """
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        code = cli.main(["auto", *argv])
    return code, buf.getvalue()


def skip_codes(field: str = "work") -> list[str]:
    return [item["code"] for item in auto.plan(field)["skipped"]]


class AutoAcceptTest(unittest.TestCase):
    """規則に合うクラスタだけが auto.toml に入る。"""

    def test_a_clean_cluster_is_accepted(self) -> None:
        with sandbox():
            setup_work()
            code, out = run_auto("--field", "work")
            self.assertEqual(code, 0)
            (entry,) = store.load_auto_entries("work")
            self.assertEqual(entry["canonical"], "アイカツ!")
            self.assertEqual(entry["variants"], ["アイカツ!", "アイカツ"])
            self.assertIs(entry["approved"], True)
            self.assertEqual(entry["confidence"], "high")
            # 人間の判断（source="human"）と区別が付くこと。
            self.assertEqual(entry["source"], "auto:v1")
            self.assertTrue(entry["reason"].startswith("自動承認（規則 v1）: "))
            # where はクラスタの events / djs から埋まる（人間の判断と同じ形）。
            self.assertEqual(entry["where"], "第3回 ha")
            self.assertIn("自動承認 1 個", out)

    def test_the_human_dictionary_is_left_alone(self) -> None:
        # 人が育てる works.toml は追記専用で、機械が触ると手で書いた整形が壊れる。
        with sandbox():
            setup_work()
            run_auto("--field", "work")
            self.assertFalse(store.entries_path("work").exists())
            self.assertTrue(store.auto_entries_path("work").exists())

    def test_the_decision_log_records_it_as_auto(self) -> None:
        with sandbox():
            setup_work()
            run_auto("--field", "work")
            (record,) = store.load_decisions()
            # 人間用の accept と別の action にしておかないと、--undo しても
            # block.load_decided() が候補に戻してくれない。
            self.assertEqual(record["action"], "auto-accept")
            self.assertEqual(record["id"], "work-0001")
            self.assertEqual(record["canonical"], "アイカツ!")

    def test_auto_entries_reach_the_public_data(self) -> None:
        with sandbox():
            setup_work()
            run_auto("--field", "work")
            store.export_json()
            payload = json.loads(paths.WEB_ALIASES_JSON.read_text(encoding="utf-8"))
            self.assertEqual(payload["works"]["アイカツ"]["c"], "アイカツ!")

    def test_running_twice_does_not_duplicate_the_entry(self) -> None:
        with sandbox():
            setup_work()
            run_auto("--field", "work")
            code, out = run_auto("--field", "work")
            self.assertEqual(code, 0)
            self.assertEqual(len(store.load_auto_entries("work")), 1)
            # 承認済みのぶんは「人間に残す」に数えない。数えると1度回した
            # あとの表示が「自動承認 0 個 / 人間に残す 150 個」になり、
            # 何も片付いていないように見える（cli._print_auto を参照）。
            self.assertIn("既に承認済み 1 個", out)
            self.assertIn("人間に残す 0 個", out)


class AutoSkipTest(unittest.TestCase):
    """外れたクラスタは書かず、理由を残して人間のキューに置く。"""

    def assert_only_skipped(self, code: str) -> None:
        self.assertEqual(skip_codes(), [code])
        run_auto("--field", "work")
        self.assertEqual(store.load_auto_entries("work"), [])
        self.assertFalse(store.auto_entries_path("work").exists())

    def test_artist_as_work_hint_is_left_to_a_human(self) -> None:
        # 実データではここに「星街すいせい ← さくらみこ」のような合同名義の
        # 分解が混ざっていた。文字列からは見分けが付かない。
        with sandbox():
            setup_work(hints=("artist-as-work",))
            self.assert_only_skipped("hint")

    def test_split_from_large_hint_is_left_to_a_human(self) -> None:
        with sandbox():
            setup_work(hints=("split-from-large",))
            self.assert_only_skipped("hint")

    def test_series_risk_alone_does_not_block(self) -> None:
        # 部分一致（substr）で繋がった組。work のブランド単位でまとめる方針では
        # 大半が正しい統合なので、これだけでは人間に回さない。
        with sandbox():
            setup_work(hints=("series-risk", "series-mark-mismatch"), kinds=("substr",))
            self.assertEqual(skip_codes(), [])
            run_auto("--field", "work")
            self.assertEqual(len(store.load_auto_entries("work")), 1)

    def test_medium_confidence_is_left_to_a_human(self) -> None:
        with sandbox():
            setup_work(confidence="medium")
            self.assert_only_skipped("confidence")

    def test_a_partial_proposal_is_left_to_a_human(self) -> None:
        # クラスタ3値のうち2値だけの提案。「残りをどうするか」は人間の判断。
        with sandbox():
            setup_work(
                raws=["アイカツ!", "アイカツ", "アイカツスターズ"],
                variants=["アイカツ!", "アイカツ"],
            )
            self.assert_only_skipped("partial")

    def test_a_split_proposal_is_left_to_a_human(self) -> None:
        with sandbox():
            setup_work(
                raws=["アイカツ!", "アイカツ", "アイカツスターズ"],
                proposals=[
                    make_proposal("work-0001", "アイカツ!", ["アイカツ!", "アイカツ"]),
                    make_proposal("work-0001", "アイカツスターズ", ["アイカツスターズ"]),
                ],
            )
            self.assert_only_skipped("many-proposals")

    def test_a_cluster_without_a_proposal_is_left_to_a_human(self) -> None:
        with sandbox():
            setup_work(proposals=[])
            self.assert_only_skipped("no-proposal")

    def test_an_invented_canonical_is_refused(self) -> None:
        # 提案の canonical をそのまま信じると、実データのどこにも無い名前が
        # 正準名になる（「アイドルマスターシリーズ」が実際にこれで落ちている）。
        with sandbox():
            setup_work(canonical="アイカツシリーズ")
            self.assert_only_skipped("canonical")

    def test_a_canonical_with_the_notes_stripped_is_accepted(self) -> None:
        # 「その着せ替え人形は恋をする 2期」しか無いクラスタ型。注記を剥がした形は
        # rules.strip_notes が規則で書けるので創作ではない。
        with sandbox():
            setup_work(
                raws=["ガールズバンドクライ OP", "ガールズバンドクライ 劇中歌"],
                canonical="ガールズバンドクライ",
            )
            self.assertEqual(skip_codes(), [])
            run_auto("--field", "work")
            (entry,) = store.load_auto_entries("work")
            self.assertEqual(entry["canonical"], "ガールズバンドクライ")

    def test_the_notes_are_not_stripped_for_artists(self) -> None:
        # strip_notes は元ネタ列の関数。アーティスト名に当てると「〜 劇場版」の
        # ような名義を削りかねないので、artist では生表記しか正準名にできない。
        with sandbox():
            write_clusters(
                "artist",
                [
                    {
                        "id": "artist-0001",
                        "field": "artist",
                        "rows": 2,
                        "hints": [],
                        "edgeKinds": ["agg"],
                        "values": [
                            {"raw": raw, "rows": 1, "events": [3], "djs": ["ha"]}
                            for raw in ("やなぎなぎ 劇場版", "やなぎなぎ劇場版")
                        ],
                        "edges": [],
                    }
                ],
            )
            store.write_proposals(
                "artist",
                [
                    {
                        "id": "artist-0001",
                        "canonical": "やなぎなぎ",
                        "variants": ["やなぎなぎ 劇場版", "やなぎなぎ劇場版"],
                        "confidence": "high",
                        "reason": "同じ名義の表記ゆれ。",
                    }
                ],
            )
            self.assertEqual(skip_codes("artist"), ["canonical"])

    def test_a_keep_apart_pair_is_left_to_a_human(self) -> None:
        with sandbox():
            setup_work(
                raws=["アイカツ!", "アイカツスターズ"],
                canonical="アイカツ!",
            )
            store.append_keep_apart(
                [{"a": "アイカツ!", "b": "アイカツスターズ"}], "別のシリーズ"
            )
            self.assert_only_skipped("keep-apart")

    def test_an_unknown_edge_kind_is_left_to_a_human(self) -> None:
        # cooccur（同じ曲名に別のアーティスト名が付いていた）は「同じ人」の根拠と
        # しては弱く、合同名義と単独名義が並ぶ形とも見分けが付かない。
        with sandbox():
            setup_work(kinds=("agg", "cooccur"))
            self.assert_only_skipped("edge-kind")

    def test_a_value_the_human_already_decided_is_left_alone(self) -> None:
        with sandbox():
            setup_work()
            store.append_decision(
                {
                    "id": "work-9999",
                    "field": "work",
                    "action": "reject",
                    "variants": ["アイカツ"],
                    "reason": "別作品だった",
                    "at": store.now_stamp(),
                }
            )
            self.assert_only_skipped("already-decided")

    def test_a_variant_already_pointing_elsewhere_is_left_alone(self) -> None:
        with sandbox():
            setup_work()
            store.append_entry(
                "work",
                {
                    "id": "work-8888",
                    "canonical": "アイカツスターズ",
                    "variants": ["アイカツ"],
                    "approved": True,
                    "confidence": "high",
                    "source": "human",
                    "reason": "手で入れた既存の項目",
                },
            )
            self.assert_only_skipped("conflict")


class AutoDryRunTest(unittest.TestCase):
    def test_dry_run_writes_nothing(self) -> None:
        with sandbox():
            setup_work()
            code, out = run_auto("--field", "work", "--dry-run")
            self.assertEqual(code, 0)
            self.assertFalse(store.auto_entries_path("work").exists())
            self.assertFalse(paths.DECISIONS_PATH.exists())
            self.assertFalse(store.entries_path("work").exists())
            self.assertIn("自動承認 1 個", out)
            self.assertIn("ファイルは1つも書き変えていません", out)

    def test_dry_run_shows_why_each_cluster_stays(self) -> None:
        with sandbox():
            write_clusters(
                "work",
                [
                    make_cluster("work-0001", ["アイカツ!", "アイカツ"]),
                    make_cluster("work-0002", ["けいおん!", "けいおん"],
                                 hints=("artist-as-work",)),
                    make_cluster("work-0003", ["まどマギ", "魔法少女まどか☆マギカ"]),
                ],
            )
            store.write_proposals(
                "work",
                [
                    make_proposal("work-0001", "アイカツ!", ["アイカツ!", "アイカツ"]),
                    make_proposal("work-0002", "けいおん!", ["けいおん!", "けいおん"]),
                    make_proposal(
                        "work-0003", "魔法少女まどか☆マギカ",
                        ["まどマギ", "魔法少女まどか☆マギカ"], confidence="low",
                    ),
                ],
            )
            _, out = run_auto("--field", "work", "--dry-run")
            self.assertIn("自動承認 1 個 / 人間に残す 2 個", out)
            self.assertIn("危険なヒントが付いている: 1 個", out)
            self.assertIn("confidence が high でない: 1 個", out)


class AutoUndoTest(unittest.TestCase):
    def test_undo_empties_the_file_and_the_values_come_back(self) -> None:
        with sandbox():
            setup_work()
            run_auto("--field", "work")
            # 自動承認したぶんは「判断済み」なので候補には出ない。
            self.assertIn("アイカツ", block.load_decided())

            code, out = run_auto("--field", "work", "--undo")
            self.assertEqual(code, 0)
            self.assertEqual(store.load_auto_entries("work"), [])
            self.assertIn("1 件を取り消しました", out)
            # 取り消したら未判断に戻る（decisions.jsonl は追記専用で消えないので、
            # auto-accept を判断済みの根拠にしていると戻らない）。
            self.assertNotIn("アイカツ", block.load_decided())
            self.assertNotIn("アイカツ!", block.load_decided())

    def test_undo_leaves_an_audit_trail(self) -> None:
        with sandbox():
            setup_work()
            run_auto("--field", "work")
            run_auto("--field", "work", "--undo")
            actions = [rec["action"] for rec in store.load_decisions()]
            self.assertEqual(actions, ["auto-accept", "auto-undo"])

    def test_undo_does_not_touch_what_a_human_decided(self) -> None:
        with sandbox():
            setup_work()
            store.append_entry(
                "work",
                {
                    "id": "work-7777",
                    "canonical": "けいおん!",
                    "variants": ["けいおん!", "けいおん"],
                    "approved": True,
                    "confidence": "high",
                    "source": "human",
                    "reason": "人間の判断",
                },
            )
            run_auto("--field", "work")
            run_auto("--field", "work", "--undo")
            # 消えるのは *.auto.toml の中身だけ。人が育てるファイルは触らない。
            (entry,) = store.load_entries("work")
            self.assertEqual(entry["canonical"], "けいおん!")
            self.assertEqual(entry["source"], "human")

    def test_undo_after_dry_run_writes_nothing(self) -> None:
        with sandbox():
            setup_work()
            run_auto("--field", "work")
            code, out = run_auto("--field", "work", "--undo", "--dry-run")
            self.assertEqual(code, 0)
            self.assertEqual(len(store.load_auto_entries("work")), 1)
            self.assertIn("ファイルは1つも書き変えていません", out)


class AutoInputTest(unittest.TestCase):
    def test_missing_clusters_file_is_a_readable_failure(self) -> None:
        with sandbox():
            code, _ = run_auto("--field", "work")
            self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
