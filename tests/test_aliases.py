"""名寄せの前処理（odj.aliases）の回帰テスト。

    python3 -m unittest discover -s tests

ここで守りたいのは「統合しすぎない」ことのほうである。表記ゆれを1つ取りこぼす
より、別作品を同じものとして潰してしまうほうが後から気付きにくく、直すのも高く
つく。そのため、規則が**行き過ぎない**ことを確かめるテスト（ONE PIECE FILM RED
の RED を OP/ED の注記と読まない、keep_apart のペアに辺を張らない、長音記号を
落とさない）を厚めに置いてある。

辞書に書く側（store / decide / export）で守りたいのは別のことで、こちらは
**未承認のものが公開データに出ない**こと。approved = true を書けるのは人間の
判断だけ、export に出せるのは approved なものだけ、という2点さえ壊れなければ、
誤った統合が公開サイトに出ることは起こらない。
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tomllib
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from odj import paths  # noqa: E402
from odj.aliases import block, cli, llm, rules, sources, store  # noqa: E402
from odj.aliases.block import (  # noqa: E402
    Edge, Value, build_edges, components, components_capped,
)


@contextlib.contextmanager
def sandbox():
    """data/aliases/ と web/public/data/ を一時ディレクトリに差し替える。

    テストがリポジトリの辞書に1行でも書いてしまうと、そのまま公開データに
    混ざりうる。paths のほうを丸ごと付け替えて、書き込み先を物理的に外に出す。
    WEB_DATA_JSON（plays.json）と RAW_DIR（API キャッシュ）も外に出すのは
    sources.fetch() のテストのためで、ここが本物の data/raw/api/ に書いてしまうと
    実際のリクエストと混ざって再現性が無くなる。
    """
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        aliases = root / "data" / "aliases"
        aliases.mkdir(parents=True)
        with mock.patch.multiple(
            paths,
            REPO_ROOT=root,
            DATA_DIR=root / "data",
            ALIASES_DIR=aliases,
            KEEP_APART_PATH=aliases / "keep_apart.toml",
            DECISIONS_PATH=aliases / "decisions.jsonl",
            OUT_ALIASES_DIR=root / "out" / "aliases",
            WEB_ALIASES_JSON=root / "web" / "public" / "data" / "aliases.json",
            WEB_DATA_JSON=root / "web" / "public" / "data" / "plays.json",
            RAW_DIR=root / "data" / "raw",
        ):
            yield root


def run_cli(*argv: str) -> tuple[int, dict]:
    """CLI を呼んで (終了コード, 標準出力の JSON) を返す。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli.main(list(argv))
    return code, json.loads(buf.getvalue())


def decide_payload(**over) -> str:
    payload = {
        "id": "work-0001",
        "field": "work",
        "action": "accept",
        "canonical": "アイカツ!",
        "kind": "work",
        "variants": ["アイカツ!", "アイカツ"],
        "reason": "テスト",
    }
    payload.update(over)
    return json.dumps(payload, ensure_ascii=False)


class AggKeyTest(unittest.TestCase):
    """クラスタリング用の内部キー。

    web/src/lib/normalize.ts の normKey() とは別物で、あちらと一致させる意図は
    無い（GUI の既出判定はこのキーを使っていない）。ここで見るのは、実データで
    並存している表記が同じキーに落ちるかどうかだけ。
    """

    def test_exclamation_mark_is_dropped(self) -> None:
        # 「アイカツ!」「アイカツ」が別々の元ネタとして入っている
        self.assertEqual(rules.agg_key("アイカツ!"), rules.agg_key("アイカツ"))

    def test_middle_dot_is_dropped(self) -> None:
        self.assertEqual(
            rules.agg_key("ヴァイオレット・エヴァーガーデン"),
            rules.agg_key("ヴァイオレットエヴァーガーデン"),
        )

    def test_wave_dash_and_space_are_dropped(self) -> None:
        # 波ダッシュは全角(〜)と半角(~)が混在し、囲うか前置くかも揃っていない
        self.assertEqual(
            rules.agg_key("CLANNAD~AFTER STORY"),
            rules.agg_key("CLANNAD 〜AFTER STORY〜"),
        )

    def test_black_and_white_stars_are_the_same(self) -> None:
        self.assertEqual(
            rules.agg_key("スペース☆ダンディ"), rules.agg_key("スペース★ダンディ")
        )

    def test_full_width_letters_meet_half_width(self) -> None:
        self.assertEqual(
            rules.agg_key("ＴＨＥ　ＩＤＯＬＭ＠ＳＴＥＲ"), rules.agg_key("THE iDOLM@STER")
        )

    def test_katakana_meets_hiragana(self) -> None:
        self.assertEqual(rules.agg_key("ボカロ"), rules.agg_key("ぼかろ"))

    def test_long_vowel_mark_is_kept(self) -> None:
        # 長音記号まで落とすと「ビート」と「ビト」が同じキーになり、行き過ぎる。
        # 記号ではなく文字として扱っているという規則そのものの回帰テスト。
        self.assertNotEqual(rules.agg_key("ビート"), rules.agg_key("ビト"))


class StripNotesTest(unittest.TestCase):
    """元ネタ列に付く注記（OP/ED・主題歌・TVアニメ「」・〜楽曲）の除去。"""

    def test_media_prefix_and_op_ed_suffix_are_removed(self) -> None:
        self.assertEqual(rules.strip_notes("TVアニメ「Engage Kiss」 ED"), "Engage Kiss")

    def test_rakkyoku_suffix_is_removed(self) -> None:
        # 「アイカツ! 楽曲」と「アイカツ!」が別の値として入っている
        self.assertEqual(rules.strip_notes("アイカツ! 楽曲"), "アイカツ!")

    def test_season_and_shudaika_are_removed_together(self) -> None:
        # 1回では落ちない。消えなくなるまで繰り返し当てている
        self.assertEqual(rules.strip_notes("てーきゅう5期 主題歌"), "てーきゅう")
        self.assertEqual(rules.strip_notes("ゆるゆり 1期OP"), "ゆるゆり")

    def test_english_word_ending_in_ed_is_left_alone(self) -> None:
        # OP/ED の直前が英字なら注記ではない。この規則が無いと
        # 「ONE PIECE FILM RED」が「ONE PIECE FILM R」に、「J-POP」が「J-P」になる。
        self.assertEqual(rules.strip_notes("ONE PIECE FILM RED"), "ONE PIECE FILM RED")
        self.assertEqual(rules.strip_notes("J-POP"), "J-POP")

    def test_note_only_value_is_not_emptied(self) -> None:
        # 注記しか書かれていない行を空文字にすると、その値がキーを失って
        # 何とでも部分一致するようになる
        self.assertEqual(rules.strip_notes("OP"), "OP")
        self.assertEqual(rules.strip_notes("楽曲"), "楽曲")

    def test_brackets_keep_their_content(self) -> None:
        # 【推しの子】は括弧まで含めて作品名なので、括弧ごと捨てると値が消える。
        # 括弧の中身を作品名として取り出す実装にもできない（「【MAD】 けいおん!
        # 『ハリケーン!! たくあん!!』」で曲名のほうを拾ってしまう）ため、
        # 括弧文字だけを落としている。
        self.assertEqual(rules.strip_notes("【推しの子】 第2期"), "推しの子")

    def test_sequel_number_survives(self) -> None:
        # 「3」を落とすと「響け!ユーフォニアム」と区別が付かなくなる。
        # 統合するかどうかは後段の判断なので、ここでは消さない。
        self.assertEqual(rules.strip_notes("響け!ユーフォニアム3 OP"), "響け!ユーフォニアム3")


class NormalizeSeparatorsTest(unittest.TestCase):
    """アーティスト列の区切り文字と feat. / CV: の統一。"""

    def test_comma_variant_meets_middle_dot_variant(self) -> None:
        # 同じメンバー列が中黒版と読点版・カンマ版で並存している
        self.assertEqual(
            rules.normalize_separators("わか,ふうり,すなお from STAR☆ANIS"),
            "わか・ふうり・すなお from STAR☆ANIS",
        )

    def test_cv_spellings_are_unified(self) -> None:
        # (CV: / (cv: / (CV. / (CV:␣ の 5 通りが実在する
        self.assertEqual(
            rules.normalize_separators("宮内れんげ (CV.小岩井ことり)"),
            "宮内れんげ(CV:小岩井ことり)",
        )
        self.assertEqual(
            rules.normalize_separators("水上 雛 (cv:大森日雅)"), "水上 雛(CV:大森日雅)"
        )

    def test_ampersand_is_not_a_separator(self) -> None:
        # 「&」を区切りとして中黒に寄せると「MYTH・ROID」という実在しない表記が
        # できてしまう。W&W や Y&Co. も同様に名前の一部。
        self.assertEqual(rules.normalize_separators("MYTH & ROID"), "MYTH & ROID")
        self.assertEqual(
            rules.normalize_separators("W&W (feat. Kizuna AI)"), "W&W (feat. Kizuna AI)"
        )

    def test_feat_spellings_are_unified(self) -> None:
        for raw in (
            "Calliope Mori ft. BOOGEY VOXX",
            "Calliope Mori feat BOOGEY VOXX",
            "Calliope Mori featuring BOOGEY VOXX",
        ):
            with self.subTest(raw=raw):
                self.assertEqual(
                    rules.normalize_separators(raw), "Calliope Mori feat. BOOGEY VOXX"
                )

    def test_feat_without_a_space_keeps_the_next_name_whole(self) -> None:
        # ピリオド付きの綴りを先に試さないと "feat" だけが食われて
        # "lapix feat. .PANXI" のようにピリオドが残る
        self.assertEqual(
            rules.normalize_separators("lapix feat.PANXI"), "lapix feat. PANXI"
        )

    def test_word_containing_ft_is_left_alone(self) -> None:
        # "Daft Punk" の ft、"Feather" の feat を feat. 表記と読まないこと
        self.assertEqual(rules.normalize_separators("Daft Punk"), "Daft Punk")
        self.assertEqual(rules.normalize_separators("Feather"), "Feather")

    def test_structure_is_not_decomposed(self) -> None:
        # 方針は「表記ゆれの統一だけ」。from や feat. で切って本体を取り出すのは
        # 推定なので後段（LLM と人間）に任せる。
        self.assertEqual(
            rules.normalize_separators("AKINO from bless4"), "AKINO from bless4"
        )


class SeriesMarksTest(unittest.TestCase):
    def test_sequel_number_changes_the_marks(self) -> None:
        # 続編の印が食い違う組は統合してはいけない可能性が高いので、
        # クラスタに series-mark-mismatch として警告を出すための材料
        self.assertNotEqual(
            rules.series_marks("響け!ユーフォニアム"),
            rules.series_marks("響け!ユーフォニアム3"),
        )
        self.assertNotEqual(
            rules.series_marks("てーきゅう 2期"), rules.series_marks("てーきゅう 4期")
        )

    def test_same_title_has_the_same_marks(self) -> None:
        self.assertEqual(
            rules.series_marks("けいおん!"), rules.series_marks("けいおん!")
        )


class ConnectedComponentsTest(unittest.TestCase):
    """連結成分の基本的な性質。"""

    def test_edge_direction_does_not_matter(self) -> None:
        # 辺は無向。どちら向きに張っても同じクラスタになること
        self.assertEqual(
            components([Edge("a", "b", "agg")]), components([Edge("b", "a", "agg")])
        )

    def test_edges_are_transitive(self) -> None:
        # 別種の辺でも繋がれば1つのクラスタになる（根拠は edges に残る）
        got = components([Edge("a", "b", "agg"), Edge("b", "c", "bigram")])
        self.assertEqual(got, [["a", "b", "c"]])

    def test_unrelated_values_stay_apart(self) -> None:
        got = sorted(components([Edge("a", "b", "agg"), Edge("x", "y", "agg")]))
        self.assertEqual(got, [["a", "b"], ["x", "y"]])


class ComponentsCappedTest(unittest.TestCase):
    """過剰連結の割り直し。

    部分一致は数珠つなぎを作る。実データでは「ボカロ」⊂「ボカロ/はるまきごはん」
    ⊃「はるまきごはん」…と辿れて 60 種・275 行の塊ができた。そのままでは LLM にも
    人間にも渡せる単位ではないので、弱い辺から落として割り直す。
    """

    def setUp(self) -> None:
        # v0-v1-…-v6 を最弱の substr で数珠つなぎにし、v0 にだけ最強の
        # caseonly（大小・空白だけの差）を1本足す
        self.edges = [Edge(f"v{i}", f"v{i + 1}", "substr") for i in range(6)]
        self.edges.append(Edge("v0", "V0 ", "caseonly"))

    def test_component_within_the_limit_is_left_alone(self) -> None:
        # 返すのは (値のリスト, 割り直しの結果か) の組。ここは割っていないので False
        self.assertEqual(
            components_capped(self.edges, 12),
            [(["V0 ", "v0", "v1", "v2", "v3", "v4", "v5", "v6"], False)],
        )

    def test_oversized_component_drops_its_weakest_edges(self) -> None:
        # 上限 3 に対して 8 種なので、substr を落として強い根拠だけで割り直す
        self.assertEqual(components_capped(self.edges, 3), [(["V0 ", "v0"], True)])

    def test_split_is_marked_so_the_caller_can_warn(self) -> None:
        # 「元は繋がりすぎた塊の一部だった」は中身を疑う理由になるので、
        # 破片にはその印を残す。build() がこれを split-from-large に変える。
        self.assertTrue(all(was_split for _, was_split in components_capped(self.edges, 3)))

    def test_values_left_alone_after_the_split_leave_the_cluster(self) -> None:
        # 弱い根拠でしか繋がらなかった値は単独値に戻る。候補に上げないほうが
        # 後段が楽になるので、これは意図した挙動。
        got = {m for part, _ in components_capped(self.edges, 3) for m in part}
        self.assertNotIn("v3", got)

    def test_strong_edges_alone_are_never_cut(self) -> None:
        # caseonly だけで上限を超えても、それ以上落とす辺が無いので割れない
        edges = [Edge("c0", f"c{i}", "caseonly") for i in range(1, 6)]
        members, was_split = components_capped(edges, 3)[0]
        self.assertEqual(len(members), 6)
        self.assertFalse(was_split)


class BuildEdgesTest(unittest.TestCase):
    """辺を張る側の、張ってはいけない条件。"""

    @staticmethod
    def _values(*raws: str) -> dict[str, Value]:
        return {r: Value(raw=r) for r in raws}

    def test_variants_are_linked(self) -> None:
        edges = build_edges(self._values("アイカツ!", "アイカツ"), set())
        self.assertTrue(edges)
        self.assertIn("agg", {e.kind for e in edges})

    def test_keep_apart_pair_gets_no_edge(self) -> None:
        # 過剰統合を止める最後の砦。人間が「別物」と判断した組は、
        # どの根拠でも繋がってはいけない。
        values = self._values("アイカツ!", "アイカツ")
        keep_apart = {frozenset(("アイカツ!", "アイカツ"))}
        self.assertEqual(build_edges(values, keep_apart), [])

    def test_keep_apart_is_direction_free(self) -> None:
        # frozenset で持っているので、書いた順で効き方が変わらないこと
        values = self._values("アイカツ", "アイカツ!")
        keep_apart = {frozenset(("アイカツ", "アイカツ!"))}
        self.assertEqual(build_edges(values, keep_apart), [])

    def test_value_with_newlines_gets_no_edge(self) -> None:
        # 1セルに7作品が改行で詰め込まれた行が実データに1件ある。部分一致で
        # そこに書かれた全作品と繋がってしまうので候補から外している。
        # 行の分割は表記ゆれの統一ではなく overrides.toml の仕事。
        crammed = "アイカツ!\nラブライブ!\nアイドルマスター シンデレラガールズ"
        edges = build_edges(self._values("アイカツ!", crammed), set())
        self.assertEqual(edges, [])

    def test_short_values_do_not_match_by_substring(self) -> None:
        # 短すぎる値は何にでも含まれてしまう
        edges = build_edges(self._values("BT", "BTS"), set())
        self.assertEqual([e for e in edges if e.kind == "substr"], [])

    def test_redirect_evidence_links_an_abbreviation_to_its_full_name(self) -> None:
        """外部 API のリダイレクトを辺として使う。

        **文字列の類似では原理的に届かない層がここで埋まる。** 「ナナシス」の
        agg_key は「ななしす」、「Tokyo 7th シスターズ」は「tokyo7thしすたーず」で、
        bigram も編集距離も部分一致も一度も繋がらない。実データで9組あり、
        この辺が無いと単独値のまま後段の LLM にも人間にも届かなかった。
        """
        values = self._values("ナナシス", "Tokyo 7th シスターズ")
        # 文字列だけでは繋がらないことを先に確かめる
        self.assertEqual(build_edges(values, set()), [])
        evidence = {"ナナシス": [{"kind": "redirect", "title": "Tokyo 7th シスターズ"}]}
        edges = block.redirect_edges(values, evidence, set())
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0].kind, "redirect")
        self.assertEqual({edges[0].a, edges[0].b}, {"ナナシス", "Tokyo 7th シスターズ"})

    def test_redirect_to_a_value_not_in_the_data_is_ignored(self) -> None:
        # 統合相手がいない正式名称に辺を張っても候補にならない
        values = self._values("ブルアカ")
        evidence = {"ブルアカ": [{"kind": "redirect", "title": "ブルーアーカイブ"}]}
        self.assertEqual(block.redirect_edges(values, evidence, set()), [])

    def test_redirect_respects_keep_apart(self) -> None:
        values = self._values("A", "B")
        evidence = {"A": [{"kind": "redirect", "title": "B"}]}
        keep = {frozenset(("A", "B"))}
        self.assertEqual(block.redirect_edges(values, evidence, keep), [])

    def test_search_hits_do_not_make_edges(self) -> None:
        # 検索は当てにならない。「ユーフォ」に Wikidata が「未確認飛行物体」を
        # 返した実例がある。辺にしてよいのはリダイレクトだけ。
        values = self._values("ユーフォ", "未確認飛行物体")
        evidence = {"ユーフォ": [{"kind": "search", "title": "未確認飛行物体"}]}
        self.assertEqual(block.redirect_edges(values, evidence, set()), [])

    def test_keep_apart_blocks_the_detour_through_annotated_variants(self) -> None:
        """注記違いの表記を経由した迂回路も塞がれること。

        「アイカツ!」と「アイカツスターズ」の辺を1本消すだけでは、
        「アイカツ! 楽曲」⊂「アイカツスターズ」の部分一致で繋がり直してしまう。
        実データでそうなっていた（work-0002 に両者が同居していた）ので、
        keep_apart は注記を剥がしたキーの組でも持つ。
        """
        values = self._values("アイカツ!", "アイカツ! 楽曲", "アイカツスターズ")
        # load_keep_apart() が生の組に加えて入れているキーの組
        keep_apart = {
            frozenset(("アイカツ!", "アイカツスターズ")),
            frozenset((rules.agg_key("アイカツ"), rules.agg_key("アイカツスターズ"))),
        }
        edges = build_edges(values, keep_apart)
        linked = {frozenset((e.a, e.b)) for e in edges}
        self.assertNotIn(frozenset(("アイカツ!", "アイカツスターズ")), linked)
        self.assertNotIn(frozenset(("アイカツ! 楽曲", "アイカツスターズ")), linked)
        # 「アイカツ!」と「アイカツ! 楽曲」は同じものなので繋がったままでよい
        self.assertIn(frozenset(("アイカツ!", "アイカツ! 楽曲")), linked)


class TomlWriterTest(unittest.TestCase):
    """自前の TOML 書き出し。tomllib で読み直せることだけが正しさの基準。

    標準ライブラリに TOML ライターが無く、依存も増やせないので自前で持っている。
    ここが静かに壊れると、人が書いた reason ごとファイルが読めなくなる。
    """

    def _round_trip(self, value) -> object:
        with sandbox():
            store.append_entry("work", {"canonical": "x", "reason": value})
            (entry,) = store.load_entries("work")
        return entry["reason"]

    def test_multiline_reason_survives(self) -> None:
        # reason は複数行で書かれる（keep_apart.toml の既存のものがそう）。
        text = "1期と2期。\n曲名は完全に排他的で、\n重複する曲名は無い。"
        self.assertEqual(self._round_trip(text), text)

    def test_quotes_inside_a_multiline_reason_survive(self) -> None:
        # 実データの根拠には曲名の引用が入る（"START DASH SENSATION" など）。
        text = 'e6 マスオ の "START DASH SENSATION" 1行だけ\n表記が違う。\n'
        self.assertEqual(self._round_trip(text), text)

    def test_triple_quotes_do_not_close_the_string(self) -> None:
        # 3連の引用符と末尾の引用符は終端と区別が付かないのでエスケープが要る
        text = 'これは """ を含む。\n末尾も引用符 "'
        self.assertEqual(self._round_trip(text), text)

    def test_backslash_and_quote_in_a_single_line_reason(self) -> None:
        self.assertEqual(self._round_trip('C:\\path "x"'), 'C:\\path "x"')

    def test_crlf_is_not_lost(self) -> None:
        # ブラウザの textarea から来る値は CRLF のことがある
        self.assertEqual(self._round_trip("一行目\r\n二行目"), "一行目\r\n二行目")

    def test_values_keep_their_types(self) -> None:
        with sandbox():
            store.append_entry(
                "work",
                {"canonical": "a", "variants": ["a", "b"], "approved": True,
                 "reason": "r"},
            )
            (entry,) = store.load_entries("work")
        self.assertIs(entry["approved"], True)
        self.assertEqual(entry["variants"], ["a", "b"])

    def test_long_variant_lists_stay_readable(self) -> None:
        # 1行に収まらない配列は縦に並べる。読めれば整形は何でもよいので、
        # ここで見るのは「縦に並べても読み直せる」ことだけ。
        many = [f"とても長い作品名の表記ゆれ{i}" for i in range(6)]
        with sandbox():
            store.append_entry("work", {"canonical": many[0], "variants": many})
            (entry,) = store.load_entries("work")
        self.assertEqual(entry["variants"], many)

    def test_appending_does_not_rewrite_what_is_already_there(self) -> None:
        # 人が手で書いた整形やコメントを機械が崩さないこと
        with sandbox():
            path = store.entries_path("work")
            path.write_text('# 手で書いた見出し\n\n[[work]]\ncanonical = "既存"\n', "utf-8")
            store.append_entry("work", {"canonical": "追記", "reason": "r"})
            text = path.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# 手で書いた見出し\n\n[[work]]\ncanonical = \"既存\""))
        self.assertEqual(len(tomllib.loads(text)["work"]), 2)


class DecideTest(unittest.TestCase):
    """判断を辞書に落とすところ。ここが辞書を書き換える唯一の入口。"""

    def test_accept_writes_an_approved_entry(self) -> None:
        with sandbox():
            code, res = run_cli("decide", "--json", decide_payload())
            entries = store.load_entries("work")
            decisions = store.load_decisions()
        self.assertEqual(code, 0)
        self.assertTrue(res["ok"])
        self.assertEqual(len(entries), 1)
        self.assertIs(entries[0]["approved"], True)
        self.assertEqual(entries[0]["canonical"], "アイカツ!")
        self.assertEqual(decisions[0]["action"], "accept")
        self.assertIn("at", decisions[0])

    def test_stdin_carries_the_payload(self) -> None:
        # 日本語をシェルのクォートに通すと事故るので、GUI からは標準入力で渡す
        with sandbox(), mock.patch("sys.stdin", io.StringIO(decide_payload())):
            code, res = run_cli("decide", "--json", "-")
            entries = store.load_entries("work")
        self.assertEqual((code, res["ok"]), (0, True))
        self.assertEqual(len(entries), 1)

    def test_empty_reason_is_refused(self) -> None:
        # なぜ統合したかが残らない判断は、後から検証できない
        with sandbox():
            code, res = run_cli("decide", "--json", decide_payload(reason="  "))
            entries = store.load_entries("work")
        self.assertEqual(code, 1)
        self.assertEqual(res, {"ok": False, "code": "invalid", "error": "理由は必須です"})
        self.assertEqual(entries, [])

    def test_failures_carry_a_machine_readable_code(self) -> None:
        """失敗の種別を code で返すこと。

        GUI はこれで分岐する。以前は文面から 400/409 を振り分けていて、
        「既に判断済み」と「keep_apart で別物と決めた組が含まれる」がどちらも
        409 になり、GUI が後者まで「二重送信」と解釈してキューを取り直していた。
        結果、キーを押しても同じカードが出続けて何も起きないように見えた。
        """
        with sandbox() as root:
            (root / "data" / "aliases" / "keep_apart.toml").write_text(
                '[[pair]]\na = "アイカツ!"\nb = "アイカツスターズ"\nreason = "別作品"\n',
                encoding="utf-8",
            )
            # keep_apart の組を統合しようとした
            _, res = run_cli(
                "decide",
                "--json",
                decide_payload(variants=["アイカツ!", "アイカツスターズ"]),
            )
            self.assertEqual(res["code"], "keep-apart")

            # 同じ id を二度
            run_cli("decide", "--json", decide_payload())
            _, again = run_cli("decide", "--json", decide_payload())
            self.assertEqual(again["code"], "already-decided")

    def test_invented_canonical_is_refused(self) -> None:
        # 正準名は variants か提案の中から選ぶ。実データのどこにも無い表記を
        # 作ると、その名前では誰も検索できないうえ出典も辿れない。
        with sandbox():
            code, res = run_cli(
                "decide", "--json", decide_payload(canonical="アイカツ!シリーズ")
            )
            entries = store.load_entries("work")
        self.assertEqual(code, 1)
        self.assertIn("canonical", res["error"])
        self.assertEqual(entries, [])

    def test_canonical_from_a_proposal_is_allowed(self) -> None:
        # 「ナナシス」→「Tokyo 7th シスターズ」のように、正式名称が実データに
        # 無いことがある。LLM の提案にあるものだけは選べる。
        with sandbox():
            proposed = paths.ALIASES_DIR / "_proposed"
            proposed.mkdir()
            (proposed / "works.toml").write_text(
                '[[work]]\nid = "work-0001"\ncanonical = "Tokyo 7th シスターズ"\n',
                encoding="utf-8",
            )
            code, res = run_cli(
                "decide",
                "--json",
                decide_payload(
                    canonical="Tokyo 7th シスターズ", variants=["ナナシス", "ナナシス!"]
                ),
            )
            entries = store.load_entries("work")
        self.assertEqual((code, res["ok"]), (0, True))
        self.assertEqual(entries[0]["canonical"], "Tokyo 7th シスターズ")

    def test_deciding_the_same_value_twice_is_refused(self) -> None:
        # レビュー中にリロードしても壊れないこと。UI ではなくサーバが弾く。
        with sandbox():
            first, _ = run_cli("decide", "--json", decide_payload())
            second, res = run_cli("decide", "--json", decide_payload())
            entries = store.load_entries("work")
        self.assertEqual((first, second), (0, 1))
        self.assertIn("既に判断済み", res["error"])
        self.assertEqual(len(entries), 1)

    def test_the_rest_of_a_card_can_be_decided_afterwards(self) -> None:
        """1枚のカードを何回かに分けて判断できること。

        以前はクラスタ id で「判断済み」を見ていたため、一度判断した時点で
        **チェックを外した値ごとカードが消えて二度と出てこなかった**。実データで
        「とある」系8個がこれで失われている。artist 側はもっと深刻で、
        1枚から複数のグループを作るのが常態になる（Aiobahn 系 /
        Mitsukiyo・ミツキヨ / わか・ふうり・すなお が同じカードに来る）。
        """
        with sandbox():
            first, _ = run_cli(
                "decide",
                "--json",
                decide_payload(canonical="アイカツ!", variants=["アイカツ!", "アイカツ"]),
            )
            # 同じ id の残りを、あとから別のグループとして採用する
            second, res = run_cli(
                "decide",
                "--json",
                decide_payload(
                    canonical="アイカツスターズ", variants=["アイカツスターズ"]
                ),
            )
            entries = store.load_entries("work")
        self.assertEqual((first, second), (0, 0), res)
        self.assertEqual(
            [e["canonical"] for e in entries], ["アイカツ!", "アイカツスターズ"]
        )

    def test_keep_apart_does_not_settle_the_values(self) -> None:
        # 「この2つは別物」と決めただけで、それぞれが他の表記と統合できるかは未判断。
        # ここを判断済みにすると、keep-apart を押しただけでカードごと消える。
        with sandbox():
            run_cli(
                "decide",
                "--json",
                json.dumps(
                    {
                        "id": "work-0001",
                        "field": "work",
                        "action": "keep-apart",
                        "pairs": [{"a": "とある科学の超電磁砲", "b": "とある魔術の禁書目録"}],
                        "reason": "別作品",
                    },
                    ensure_ascii=False,
                ),
            )
            code, res = run_cli(
                "decide",
                "--json",
                decide_payload(
                    canonical="とある科学の超電磁砲",
                    variants=["とある科学の超電磁砲", "超電磁砲"],
                ),
            )
        self.assertEqual(code, 0, res)

    def test_a_deferred_cluster_can_still_be_decided(self) -> None:
        # defer は「まだ決めない」の記録。後から本判断できないと意味が無い。
        with sandbox():
            run_cli("decide", "--json", decide_payload(action="defer"))
            code, res = run_cli("decide", "--json", decide_payload())
            entries = store.load_entries("work")
        self.assertEqual((code, res["ok"]), (0, True))
        self.assertEqual(len(entries), 1)

    def test_defer_does_not_record_variants(self) -> None:
        # variants を書くと block.load_decided がその値を判断済みとみなし、
        # 次回のキューから消える。defer は次回また出す約束。
        with sandbox():
            run_cli("decide", "--json", decide_payload(action="defer"))
            (rec,) = store.load_decisions()
        self.assertNotIn("variants", rec)

    def test_reject_writes_only_the_log(self) -> None:
        with sandbox():
            code, res = run_cli("decide", "--json", decide_payload(action="reject"))
            entries = store.load_entries("work")
            (rec,) = store.load_decisions()
        self.assertEqual((code, res["ok"]), (0, True))
        self.assertEqual(entries, [])
        # 却下した値は候補から外す（毎回出てくるとレビューが終わらない）
        self.assertEqual(rec["variants"], ["アイカツ!", "アイカツ"])

    def test_keep_apart_pair_is_written_and_read_back_by_block(self) -> None:
        with sandbox():
            code, res = run_cli(
                "decide",
                "--json",
                decide_payload(
                    action="keep-apart",
                    pairs=[{"a": "アイカツ!", "b": "アイカツスターズ"}],
                    reason="歌唱ユニットが別",
                ),
            )
            pairs = store.load_keep_apart_pairs()
            known = block.load_keep_apart()
            entries = store.load_entries("work")
        self.assertEqual((code, res["ok"]), (0, True))
        self.assertEqual(len(pairs), 1)
        self.assertIn(frozenset(("アイカツ!", "アイカツスターズ")), known)
        self.assertEqual(entries, [])

    def test_accepting_a_keep_apart_pair_is_refused(self) -> None:
        # keep_apart のほうが常に強い。人間が別物と決めた組は統合できない。
        with sandbox():
            run_cli(
                "decide",
                "--json",
                decide_payload(
                    id="work-0100",
                    action="keep-apart",
                    pairs=[{"a": "アイカツ!", "b": "アイカツスターズ"}],
                    reason="歌唱ユニットが別",
                ),
            )
            code, res = run_cli(
                "decide",
                "--json",
                decide_payload(variants=["アイカツ!", "アイカツスターズ"]),
            )
            entries = store.load_entries("work")
        self.assertEqual(code, 1)
        self.assertIn("keep_apart", res["error"])
        self.assertEqual(entries, [])

    def test_keep_apart_blocks_the_detour_through_annotated_variants(self) -> None:
        # 「アイカツ!」と「アイカツスターズ」を分けたのに「アイカツ! 楽曲」と
        # 「アイカツスターズ」なら統合できてしまう、では意味が無い。
        # 辺を張る側（build_edges）と同じ条件で見ている。
        with sandbox():
            run_cli(
                "decide",
                "--json",
                decide_payload(
                    id="work-0100",
                    action="keep-apart",
                    pairs=[{"a": "アイカツ!", "b": "アイカツスターズ"}],
                    reason="歌唱ユニットが別",
                ),
            )
            code, res = run_cli(
                "decide",
                "--json",
                decide_payload(
                    canonical="アイカツ! 楽曲",
                    variants=["アイカツ! 楽曲", "アイカツスターズ"],
                ),
            )
        self.assertEqual(code, 1)
        self.assertIn("keep_apart", res["error"])

    def test_a_variant_cannot_belong_to_two_canonicals(self) -> None:
        # 同じ表記が2つの正準名に寄ると、検索の同値クラスが割れる
        with sandbox():
            run_cli("decide", "--json", decide_payload())
            code, res = run_cli(
                "decide",
                "--json",
                decide_payload(
                    id="work-0002", canonical="アイカツ", variants=["アイカツ", "あいかつ"]
                ),
            )
            entries = store.load_entries("work")
        self.assertEqual(code, 1)
        self.assertIn("既に", res["error"])
        self.assertEqual(len(entries), 1)

    def test_unknown_field_is_refused(self) -> None:
        with sandbox():
            code, res = run_cli("decide", "--json", decide_payload(field="title"))
        self.assertEqual(code, 1)
        self.assertIn("field", res["error"])

    def test_broken_json_becomes_a_json_error(self) -> None:
        # ミドルウェアがそのまま中継できるよう、失敗も標準出力は JSON
        with sandbox():
            code, res = run_cli("decide", "--json", "{壊れている")
        self.assertEqual(code, 1)
        self.assertFalse(res["ok"])

    def test_where_is_filled_from_the_cluster_when_omitted(self) -> None:
        with sandbox():
            paths.OUT_ALIASES_DIR.mkdir(parents=True)
            (paths.OUT_ALIASES_DIR / "clusters.work.json").write_text(
                json.dumps(
                    {"clusters": [{
                        "id": "work-0001",
                        "values": [{"raw": "アイカツ!", "events": [2], "djs": ["せーや", "あちょ"]}],
                    }]},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            run_cli("decide", "--json", decide_payload())
            (entry,) = store.load_entries("work")
        self.assertEqual(entry["where"], "第2回 あちょ ほか")


class ExportTest(unittest.TestCase):
    """公開データに出せるものの線引き。**このテストが最後の砦**。"""

    @staticmethod
    def _write(field: str, *entries: dict) -> None:
        for entry in entries:
            store.append_entry(field, entry)

    @staticmethod
    def _export() -> dict:
        code, res = run_cli("export")
        assert code == 0, res
        return json.loads(paths.WEB_ALIASES_JSON.read_text(encoding="utf-8"))

    def test_unapproved_entries_never_reach_the_public_data(self) -> None:
        # LLM も自動処理も approved を書かない。人間が見ていないものは出さない。
        with sandbox():
            self._write(
                "work",
                {"canonical": "アイカツ!", "variants": ["アイカツ!", "アイカツ"],
                 "approved": False, "confidence": "high", "reason": "提案のまま"},
                {"canonical": "ゆるゆり", "variants": ["ゆるゆり", "ゆるゆり♪♪"],
                 "confidence": "high", "reason": "approved 自体が無い"},
            )
            data = self._export()
        self.assertEqual(data["works"], {})

    def test_low_confidence_entries_are_not_exported(self) -> None:
        with sandbox():
            self._write(
                "work",
                {"canonical": "アイカツ!", "variants": ["アイカツ!", "アイカツ"],
                 "approved": True, "confidence": "low", "reason": "自信が無い"},
            )
            data = self._export()
        self.assertEqual(data["works"], {})

    def test_approved_entry_is_keyed_by_every_raw_value(self) -> None:
        with sandbox():
            self._write(
                "work",
                {"canonical": "Tokyo 7th シスターズ", "series": "ナナシス",
                 "kind": "work", "variants": ["ナナシス", "Tokyo 7th シスターズ"],
                 "approved": True, "confidence": "high", "reason": "同じ"},
            )
            data = self._export()
        self.assertEqual(sorted(data["works"]), ["Tokyo 7th シスターズ", "ナナシス"])
        # canonical == raw の側も引けること（同値クラス全体を検索に入れるため）
        self.assertEqual(
            data["works"]["Tokyo 7th シスターズ"],
            {"c": "Tokyo 7th シスターズ", "s": "ナナシス", "k": "work",
             "v": ["ナナシス", "Tokyo 7th シスターズ"]},
        )

    def test_optional_fields_are_omitted(self) -> None:
        with sandbox():
            self._write(
                "artist",
                {"canonical": "DECO*27", "variants": ["Deco*27", "DECO*27"],
                 "approved": True, "confidence": "medium", "reason": "大小差"},
            )
            data = self._export()
        self.assertEqual(
            data["artists"]["Deco*27"],
            {"c": "DECO*27", "v": ["Deco*27", "DECO*27"]},
        )

    def test_canonical_outside_the_variants_joins_the_class(self) -> None:
        # 提案から採った正式名称は plays.json に無いが、その名前でも引きたい
        with sandbox():
            self._write(
                "work",
                {"canonical": "Tokyo 7th シスターズ", "variants": ["ナナシス"],
                 "approved": True, "confidence": "high", "reason": "同じ"},
            )
            data = self._export()
        self.assertEqual(
            data["works"]["ナナシス"]["v"], ["ナナシス", "Tokyo 7th シスターズ"]
        )

    def test_export_is_byte_identical_when_run_twice(self) -> None:
        with sandbox():
            self._write(
                "work",
                {"canonical": "アイカツ!", "variants": ["アイカツ!", "アイカツ"],
                 "approved": True, "confidence": "high", "kind": "work",
                 "reason": "同じ"},
                {"canonical": "ゆるゆり", "variants": ["ゆるゆり", "ゆるゆり♪♪"],
                 "approved": True, "confidence": "high", "kind": "work",
                 "reason": "同じ"},
            )
            run_cli("export")
            first = paths.WEB_ALIASES_JSON.read_bytes()
            run_cli("export")
            second = paths.WEB_ALIASES_JSON.read_bytes()
        self.assertEqual(first, second)
        # キーが並べ替えてあること（辞書に足した順に依らない）
        works = json.loads(first)["works"]
        self.assertEqual(list(works), sorted(works))

    def test_export_reports_counts(self) -> None:
        with sandbox():
            self._write(
                "work",
                {"canonical": "アイカツ!", "variants": ["アイカツ!", "アイカツ"],
                 "approved": True, "confidence": "high", "reason": "同じ"},
            )
            _, res = run_cli("export")
        self.assertEqual((res["ok"], res["works"], res["artists"]), (True, 1, 0))

    def test_export_writes_an_empty_dictionary_when_nothing_is_approved(self) -> None:
        # 辞書がまだ無くても web 側は aliases.json を読みに来る
        with sandbox():
            data = self._export()
        self.assertEqual(data["works"], {})
        self.assertEqual(data["artists"], {})
        self.assertIn("generatedAt", data)

    def test_decide_then_export_carries_the_decision_through(self) -> None:
        # 人間の判断 → 辞書 → 公開データ の一本道が繋がっていること
        with sandbox():
            run_cli("decide", "--json", decide_payload())
            data = self._export()
        self.assertEqual(data["works"]["アイカツ"]["c"], "アイカツ!")


class AskTest(unittest.TestCase):
    """LLM の提案（odj.aliases ask）。**ネットワークには一切出ない。**

    ここで守りたいのは Phase 2 の一番外側で、「LLM が何を返してこようと、
    未承認でない・創作された・別物と決めた組を含む提案が辞書の手前を通らない」
    こと。プロンプトは守られない前提で書いてあるので、テストも守らなかった場合
    （canonical の創作、keep_apart のペア、クラスタに無い variants）を先に置く。
    """

    # 「アイカツ!」と「アイカツ」は同じ作品、「アイカツスターズ」は別作品。
    # 実データの work-0002 がこの形で、keep_apart.toml に既に登録してある。
    CLUSTERS: dict = {
        "field": "work",
        "clusters": [
            {
                "id": "work-aaa",
                "field": "work",
                "rows": 10,
                "hints": ["series-risk"],
                "edgeKinds": ["agg", "substr"],
                "values": [
                    {"raw": "アイカツ!", "rows": 6, "events": [1], "djs": ["tri"],
                     "coTitles": ["カレンダーガール"]},
                    {"raw": "アイカツ", "rows": 3, "events": [2], "djs": ["せーや"],
                     "coTitles": ["カレンダーガール"]},
                    {"raw": "アイカツスターズ", "rows": 1, "events": [6], "djs": ["ましゅー"],
                     "coTitles": ["Episode Solo"]},
                ],
                "edges": [{"a": "アイカツ!", "b": "アイカツ", "kinds": ["agg"]}],
            },
            {
                "id": "work-bbb",
                "field": "work",
                "rows": 5,
                "hints": [],
                "edgeKinds": ["agg"],
                "values": [
                    {"raw": "ナナシス", "rows": 4, "events": [3], "djs": ["ha"],
                     "coTitles": ["H-A-J-I-M-A-R-I-U-T-A-!!"]},
                    {"raw": "ナナシス!", "rows": 1, "events": [4], "djs": ["ha"],
                     "coTitles": ["H-A-J-I-M-A-R-I-U-T-A-!!"]},
                ],
                "edges": [{"a": "ナナシス", "b": "ナナシス!", "kinds": ["agg"]}],
            },
        ],
    }

    EVIDENCE: dict = {
        "field": "work",
        "evidence": {
            "ナナシス": [{
                "source": "wikipedia-ja",
                "id": "Tokyo 7th シスターズ",
                "title": "Tokyo 7th シスターズ",
                "kind": "redirect",
                "note": "「ナナシス」は Tokyo 7th シスターズ へのリダイレクト",
                "url": "https://ja.wikipedia.org/wiki/Tokyo_7th_シスターズ",
            }],
            "ナナシス!": [],
        },
    }

    @contextlib.contextmanager
    def fixture(self, *, evidence: dict | None = None, keep_apart: bool = False):
        """sandbox に候補クラスタ（と任意で evidence・keep_apart）を置く。

        data/raw/ も一時ディレクトリに寄せる。LLM の応答キャッシュがそこに
        できるので、本物の data/raw/llm/ を汚すとテスト間で結果が混ざる。
        """
        with sandbox() as root, mock.patch.object(paths, "RAW_DIR", root / "data" / "raw"):
            paths.OUT_ALIASES_DIR.mkdir(parents=True)
            (paths.OUT_ALIASES_DIR / "clusters.work.json").write_text(
                json.dumps(self.CLUSTERS, ensure_ascii=False), encoding="utf-8"
            )
            if evidence is not None:
                (paths.OUT_ALIASES_DIR / "evidence.work.json").write_text(
                    json.dumps(evidence, ensure_ascii=False), encoding="utf-8"
                )
            if keep_apart:
                store.append_keep_apart(
                    [{"a": "アイカツ!", "b": "アイカツスターズ"}], "歌唱ユニットが別"
                )
            yield root

    @staticmethod
    def reply(*groups: dict) -> dict:
        """GitHub Models（OpenAI 互換）の応答をそのまま真似た形。"""
        return {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": json.dumps({"groups": list(groups)}, ensure_ascii=False),
                },
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 2500, "completion_tokens": 300},
        }

    @staticmethod
    def group(**over) -> dict:
        payload = {
            "cluster_id": "work-aaa",
            "canonical": "アイカツ!",
            "series": "アイカツ",
            "kind": "work",
            "variants": ["アイカツ!", "アイカツ"],
            "confidence": "high",
            "reason": "どちらも coTitles に「カレンダーガール」があり、6行と3行。",
        }
        payload.update(over)
        return payload

    def run_ask(self, response: dict, **kw) -> tuple[dict, list[str], mock.Mock]:
        """post をモックして ask を回し、(結果, ログ, モック) を返す。"""
        logs: list[str] = []
        with mock.patch.object(llm, "post", return_value=response) as posted:
            result = llm.ask("work", token="dummy", log=logs.append, **kw)
        return result, logs, posted

    # -- 守られた場合 -------------------------------------------------------

    def test_a_valid_group_becomes_a_proposal(self) -> None:
        with self.fixture():
            result, _, _ = self.run_ask(self.reply(self.group()))
            entries = tomllib.loads(
                store.proposals_path("work").read_text(encoding="utf-8")
            )["work"]
        self.assertEqual(result["proposed"], 1)
        self.assertEqual(entries[0]["canonical"], "アイカツ!")
        self.assertEqual(entries[0]["variants"], ["アイカツ!", "アイカツ"])
        self.assertEqual(entries[0]["source"], f"llm:{llm.DEFAULT_MODEL}")

    def test_approved_is_never_written(self) -> None:
        """**最重要。** LLM が approved を返しても提案には出ない。

        JSON スキーマに approved を入れていないので普通は返ってこないが、
        「未承認のものが公開データに出ない」の担保をプロンプト任せにはしない。
        """
        with self.fixture():
            result, _, _ = self.run_ask(
                self.reply(self.group(approved=True, decided_at="2026-07-26"))
            )
            text = store.proposals_path("work").read_text(encoding="utf-8")
            entries = tomllib.loads(text)["work"]
        self.assertEqual(result["proposed"], 1)
        # 見出しのコメントには「approved は書かない」と書いてあるので、
        # 代入の行だけを見る
        self.assertNotIn("approved =", text)
        self.assertNotIn("approved", entries[0])
        self.assertNotIn("decided_at", entries[0])

    def test_write_proposals_refuses_an_approved_entry(self) -> None:
        # 書き出す側にも同じ検査を置く。呼ぶ側を1つ増やしたときに漏れないように。
        with self.fixture():
            with self.assertRaises(store.AliasError) as caught:
                store.write_proposals("work", [{"canonical": "x", "approved": True}])
        self.assertIn("approved", str(caught.exception))

    def test_a_cluster_can_be_split_into_two_groups(self) -> None:
        # groups を配列にしてある理由。「1クラスタ = 1グループ」を強制すると
        # LLM は入力を全部まとめる方向にしか答えられない。
        with self.fixture():
            result, _, _ = self.run_ask(
                self.reply(
                    self.group(),
                    self.group(
                        canonical="アイカツスターズ",
                        variants=["アイカツスターズ"],
                        reason="coTitles が「Episode Solo」で他と重ならない",
                    ),
                )
            )
        # 2つ目は variants 1件・canonical も同じで中身が無いので捨てられる
        self.assertEqual(result["proposed"], 1)
        self.assertEqual(len(result["rejected"]), 1)

    # -- 守られなかった場合 -------------------------------------------------

    def test_invented_canonical_is_dropped(self) -> None:
        # 「アイカツ!シリーズ」は plays.json にも API の結果にも無い。その名前で
        # 検索する人はいないし、出典も辿れない。
        with self.fixture():
            result, logs, _ = self.run_ask(
                self.reply(self.group(canonical="アイカツ!シリーズ"))
            )
            text = store.proposals_path("work").read_text(encoding="utf-8")
        self.assertEqual(result["proposed"], 0)
        self.assertIn("創作", result["rejected"][0])
        self.assertNotIn("アイカツ!シリーズ", text)
        self.assertTrue(any("捨てた提案" in line for line in logs))

    def test_keep_apart_pair_is_dropped(self) -> None:
        # 人間が「別物」と決めた組。プロンプトにも全展開して渡しているが、
        # 守られなかったときに黙って通すわけにいかない。
        with self.fixture(keep_apart=True):
            result, _, _ = self.run_ask(
                self.reply(
                    self.group(variants=["アイカツ!", "アイカツ", "アイカツスターズ"])
                )
            )
            text = store.proposals_path("work").read_text(encoding="utf-8")
        self.assertEqual(result["proposed"], 0)
        self.assertIn("keep_apart", result["rejected"][0])
        self.assertNotIn("アイカツスターズ", text)

    def test_keep_apart_blocks_the_detour_through_annotated_variants(self) -> None:
        # 「アイカツ!」ではなく「アイカツ」（注記違い）で来ても同じく塞がる。
        # cli._blocked_pair・block.build_edges と同じ条件で見ている。
        with self.fixture(keep_apart=True):
            result, _, _ = self.run_ask(
                self.reply(
                    self.group(
                        canonical="アイカツ",
                        variants=["アイカツ", "アイカツスターズ"],
                    )
                )
            )
        self.assertEqual(result["proposed"], 0)

    def test_variants_outside_the_cluster_are_dropped(self) -> None:
        # 表記を「整えて」返してくるのが典型（「ボカロ?」→「ボカロ？」）。
        # plays.json に無い文字列は検索に一致しないので通せない。
        with self.fixture():
            result, _, _ = self.run_ask(
                self.reply(self.group(variants=["アイカツ!", "アイカツ！"]))
            )
        self.assertEqual(result["proposed"], 0)
        self.assertIn("クラスタに無い variants", result["rejected"][0])

    def test_an_unknown_cluster_id_is_dropped(self) -> None:
        with self.fixture():
            result, _, _ = self.run_ask(self.reply(self.group(cluster_id="work-zzz")))
        self.assertEqual(result["proposed"], 0)
        self.assertIn("知らない cluster_id", result["rejected"][0])

    def test_an_empty_reason_is_dropped(self) -> None:
        # なぜ統合したかが残らない提案は、人間がレビューしようがない
        with self.fixture():
            result, _, _ = self.run_ask(self.reply(self.group(reason="  ")))
        self.assertEqual(result["proposed"], 0)

    def test_a_truncated_response_yields_nothing(self) -> None:
        # 出力上限（4k）に当たると JSON が途中で切れる。バッチごと諦める。
        broken = {"choices": [{"message": {"content": '{"groups": [{"cluster'},
                               "finish_reason": "length"}]}
        with self.fixture():
            result, _, _ = self.run_ask(broken)
        self.assertEqual(result["proposed"], 0)

    # -- 外部 API の裏取り --------------------------------------------------

    def test_canonical_from_the_api_is_allowed(self) -> None:
        # 「ナナシス」→「Tokyo 7th シスターズ」。正式名称は plays.json のどこにも
        # 無いので、variants だけに限ると一番効く提案ができなくなる。
        with self.fixture(evidence=self.EVIDENCE):
            result, _, _ = self.run_ask(
                self.reply(
                    self.group(
                        cluster_id="work-bbb",
                        canonical="Tokyo 7th シスターズ",
                        variants=["ナナシス", "ナナシス!"],
                        reason="api_results が Tokyo 7th シスターズ へのリダイレクトを返した",
                    )
                )
            )
            entries = store.load_proposals("work")
        self.assertEqual(result["proposed"], 1)
        self.assertEqual(entries["work-bbb"]["canonical"], "Tokyo 7th シスターズ")

    def test_evidence_reaches_the_prompt(self) -> None:
        with self.fixture(evidence=self.EVIDENCE):
            prepared = llm.plan("work")
        user = prepared["batches"][0]["user"]
        self.assertIn("Tokyo 7th シスターズ", user)
        # ヒット無しは空配列のまま渡す（タイポ疑いのシグナルなので消さない）
        self.assertIn('"ナナシス!": []', user)

    def test_duplicate_api_titles_are_folded(self) -> None:
        # Wikidata の検索は「アイカツ!」に対して Q729857 / Q6560785 / Q113674971 を
        # 返し、title は3つとも「アイカツ!」になる（フランチャイズ・一覧記事・
        # アニメ）。判断材料は増えないのに入力だけ3倍になるので畳む。
        evidence = {
            "field": "work",
            "evidence": {
                "アイカツ!": [
                    {"source": "wikidata", "id": "Q729857", "title": "アイカツ!",
                     "kind": "search", "note": "日本のメディア・フランチャイズ"},
                    {"source": "wikidata", "id": "Q6560785", "title": "アイカツ!",
                     "kind": "search", "note": "ウィキメディアの一覧記事"},
                ]
            },
        }
        with self.fixture(evidence=evidence):
            packed = llm.pack_cluster(self.CLUSTERS["clusters"][0], llm.load_evidence("work"))
        self.assertEqual(len(packed["api_results"]["アイカツ!"]), 1)
        # url と id はプロンプトに載せない（判断に使わないのに長い）
        self.assertEqual(
            sorted(packed["api_results"]["アイカツ!"][0]),
            ["kind", "note", "source", "title"],
        )

    def test_proposals_are_rewritten_not_appended(self) -> None:
        # 提案は毎回作り直すもの。追記にすると同じクラスタの提案が実行のたびに
        # 積み上がり、レビュー側でどれが最新か分からなくなる。
        with self.fixture():
            store.write_proposals("work", [{"id": "work-old", "canonical": "古い提案"}])
            self.run_ask(self.reply(self.group()))
            text = store.proposals_path("work").read_text(encoding="utf-8")
        self.assertNotIn("work-old", text)
        self.assertIn("work-aaa", text)

    def test_a_missing_evidence_file_is_not_an_error(self) -> None:
        # fetch を回していなくても提案は作れる。evidence は材料であって前提ではない。
        with self.fixture():
            prepared = llm.plan("work")
            result, _, _ = self.run_ask(self.reply(self.group()))
        self.assertEqual(prepared["evidence"], {})
        self.assertEqual(result["proposed"], 1)

    def test_missing_clusters_file_is_a_readable_error(self) -> None:
        with sandbox():
            with self.assertRaises(store.AliasError) as caught:
                llm.plan("work")
        self.assertIn("block", str(caught.exception))

    # -- バッチ化とキャッシュ -----------------------------------------------

    def test_batches_keep_the_order_of_the_clusters(self) -> None:
        # 中身の順は変えない（同じ入力なら同じキャッシュキーになるため）
        items = list(range(30))
        self.assertEqual([x for b in llm.batches(items) for x in b], items)

    def test_batches_are_filled_up_to_the_input_limit(self) -> None:
        """入る限り詰めること。

        以前は「一定数ずつ切ってから、収まらないバッチを半分に割る」やり方で、
        割った片方が枠の半分しか使わず実データで 37 リクエストになっていた。
        無料枠は 50 req/日しかないので、枠を使い切れないと一度の直しで枯渇する。
        """
        with self.fixture():
            prepared = llm.plan("work")
        self.assertTrue(prepared["batches"])
        for batch in prepared["batches"]:
            with self.subTest(batch=batch["clusters"][0].get("id")):
                self.assertLessEqual(batch["tokens"], llm.SAFE_INPUT_TOKENS)
                self.assertLessEqual(batch["tokens"], llm.INPUT_TOKEN_LIMIT)
                self.assertLessEqual(len(batch["clusters"]), llm.MAX_CLUSTERS_PER_CALL)

    def test_a_tight_limit_forces_one_cluster_per_call(self) -> None:
        # 上限を極端に下げても、1クラスタずつには必ず割れて落ちないこと
        with self.fixture():
            with mock.patch.object(llm, "SAFE_INPUT_TOKENS", 1):
                prepared = llm.plan("work")
        self.assertTrue(all(len(b["clusters"]) == 1 for b in prepared["batches"]))

    def test_the_input_limit_matches_what_the_api_actually_allows(self) -> None:
        # 実測値。事前の調べで 8000 としていたのは誤りで、GitHub Actions 上で
        # 413 tokens_limit_reached が返って判明した。
        self.assertEqual(llm.INPUT_TOKEN_LIMIT, 4000)
        self.assertLess(llm.SAFE_INPUT_TOKENS, llm.INPUT_TOKEN_LIMIT)

    def test_the_same_input_hits_the_cache_instead_of_the_network(self) -> None:
        # プロンプト全文の SHA256 が鍵。無料枠が 50 req/日しかないので、
        # 作り直しのたびに投げ直すわけにいかない。
        with self.fixture():
            _, _, first = self.run_ask(self.reply(self.group()))
            second_result, logs, second = self.run_ask(self.reply(self.group()))
        self.assertEqual(first.call_count, 1)
        self.assertEqual(second.call_count, 0)
        self.assertEqual((second_result["calls"], second_result["cached"]), (0, 1))
        self.assertTrue(any("キャッシュ命中" in line for line in logs))

    def test_a_cached_run_is_byte_identical(self) -> None:
        with self.fixture():
            self.run_ask(self.reply(self.group()))
            first = store.proposals_path("work").read_bytes()
            self.run_ask(self.reply(self.group()))
            second = store.proposals_path("work").read_bytes()
        self.assertEqual(first, second)

    def test_a_different_model_does_not_reuse_the_cache(self) -> None:
        # モデル名もスキーマも鍵に入れてある。差し替えたのに古い応答が返ると、
        # 原因の分からない検証エラーになる。
        with self.fixture():
            self.run_ask(self.reply(self.group()))
            _, _, again = self.run_ask(self.reply(self.group()), model="openai/gpt-4o")
        self.assertEqual(again.call_count, 1)

    def test_the_cache_lands_outside_the_repository(self) -> None:
        with self.fixture() as root:
            self.run_ask(self.reply(self.group()))
            self.assertTrue(llm.cache_dir().is_relative_to(root))
            self.assertTrue(list(llm.cache_dir().glob("*.json")))

    # -- プロンプト ---------------------------------------------------------

    def test_keep_apart_pairs_are_never_summarised(self) -> None:
        """keep_apart.toml は要約せず、組のまま渡すこと。

        要約すると必ず「アイマス系は分ける」のような一般則に化けて、逆に
        「アイドルマスターシンデレラガールズ」と「デレマス」（同じもの）まで
        分けられてしまう。組で持てば効き目が正確になる。

        載せる場所はシステムプロンプトではなく各バッチの入力側。全 26 組で
        664tok あり、入力上限 4000 に対して固定費として重すぎるため。
        """
        pairs = store.load_keep_apart_pairs()  # 本物の data/aliases/keep_apart.toml
        self.assertGreater(len(pairs), 20)
        lines = llm.keep_apart_lines()
        for pair in pairs:
            with self.subTest(pair=(pair["a"], pair["b"])):
                self.assertIn(f"- 「{pair['a']}」 ≠ 「{pair['b']}」", lines)

    def test_keep_apart_is_narrowed_to_the_values_in_the_batch(self) -> None:
        """バッチに関係する組だけを載せること。

        絞っても防止力は落ちない。プロンプトに出さなかった組も、返ってきた提案は
        block.load_keep_apart() で必ず再検査する。
        """
        lines = llm.keep_apart_lines({"アイカツ!", "アイカツ"})
        self.assertIn("- 「アイカツ!」 ≠ 「アイカツスターズ」", lines)
        # 無関係な組は載らない
        self.assertNotIn("- 「けいおん!」 ≠ 「けいおん!!」", lines)
        self.assertLess(len(lines), len(llm.keep_apart_lines()))

    def test_the_detour_through_an_annotated_variant_still_gets_the_pair(self) -> None:
        # 「アイカツ! 楽曲」しか無いバッチでも「アイカツ! ≠ アイカツスターズ」が
        # 要る。生の文字列だけで突き合わせると取りこぼす組。
        lines = llm.keep_apart_lines({"アイカツ! 楽曲"})
        self.assertIn("- 「アイカツ!」 ≠ 「アイカツスターズ」", lines)

    def test_the_system_prompt_carries_the_absolute_rules(self) -> None:
        text = llm.system_prompt("work")
        for phrase in ("迷ったら「分ける」", "創作", "空配列でもよい", "confidence=\"low\""):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)

    def test_hints_are_explained_in_the_prompt(self) -> None:
        # series-risk と split-from-large は「なぜ疑うか」まで書かないと効かない。
        # 実データの「アイカツ!」と「アイカツスターズ」がまさにこれ。
        text = llm.system_prompt("work")
        self.assertIn("series-risk", text)
        self.assertIn("split-from-large", text)
        self.assertIn("substr", text)

    def test_hints_reach_the_user_prompt(self) -> None:
        with self.fixture():
            prepared = llm.plan("work")
        self.assertIn("series-risk", prepared["batches"][0]["user"])

    def test_the_response_schema_has_no_approved(self) -> None:
        # LLM が承認済みを書くことが構造的に不可能であること
        schema = llm.RESPONSE_FORMAT["json_schema"]["schema"]
        props = schema["properties"]["groups"]["items"]["properties"]
        self.assertNotIn("approved", props)
        self.assertIs(schema["properties"]["groups"]["items"]["additionalProperties"], False)
        self.assertIs(llm.RESPONSE_FORMAT["json_schema"]["strict"], True)

    def test_dry_run_never_touches_the_network(self) -> None:
        # --dry-run は GITHUB_TOKEN が無くても通ること（CI 前の確認に使う）
        buf = io.StringIO()
        with self.fixture(), mock.patch.dict("os.environ", {}, clear=True):
            with mock.patch.object(llm, "post", side_effect=AssertionError("送信した")):
                with contextlib.redirect_stdout(buf):
                    code = cli.main(["ask", "--field", "work", "--dry-run"])
        self.assertEqual(code, 0)
        out = buf.getvalue()
        self.assertIn("1 リクエスト", out)
        self.assertIn("迷ったら「分ける」", out)  # システムプロンプト全文
        self.assertIn("work-aaa", out)  # 実際に投げる入力も全文

    def test_ask_without_a_token_fails_readably(self) -> None:
        with self.fixture(), mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(store.AliasError) as caught:
                llm.ask("work")
        self.assertIn("GITHUB_TOKEN", str(caught.exception))

    @staticmethod
    def _http_error(code: int) -> mock.Mock:
        return mock.Mock(
            side_effect=urllib.error.HTTPError(
                llm.ENDPOINT, code, "boom", {}, io.BytesIO(b'{"error":"..."}')  # type: ignore[arg-type]
            )
        )

    def test_a_client_error_is_not_retried(self) -> None:
        # 400（プロンプトが長すぎる）や 401（トークン切れ）は待っても直らない。
        # 無料枠が 50 req/日しかないので、無駄撃ちを3回に増やしてはいけない。
        opened = self._http_error(400)
        with mock.patch("urllib.request.urlopen", opened):
            with mock.patch("time.sleep") as slept:
                with self.assertRaises(store.AliasError) as caught:
                    llm.post({"model": "m"}, "tok")
        self.assertIn("400", str(caught.exception))
        self.assertEqual((opened.call_count, slept.call_count), (1, 0))

    def test_a_rate_limit_is_retried_with_a_wait(self) -> None:
        # 10 req/分の上限は待てば空く。最後の1回のあとは待たずに諦める。
        opened = self._http_error(429)
        with mock.patch("urllib.request.urlopen", opened):
            with mock.patch("time.sleep") as slept:
                with self.assertRaises(store.AliasError):
                    llm.post({"model": "m"}, "tok", retries=3)
        self.assertEqual((opened.call_count, slept.call_count), (3, 2))

    def test_the_request_body_matches_what_github_models_accepts(self) -> None:
        # temperature を受け付けないモデルがあり、max_tokens ではなく
        # max_completion_tokens を見る。どちらも間違えると 400 で全滅する。
        body = llm.request_body(llm.DEFAULT_MODEL, "sys", "user")
        self.assertNotIn("temperature", body)
        self.assertEqual(body["max_completion_tokens"], llm.MAX_OUTPUT_TOKENS)
        self.assertEqual(body["model"], llm.DEFAULT_MODEL)
        self.assertEqual([m["role"] for m in body["messages"]], ["system", "user"])

    def test_the_default_model_is_in_the_free_tier(self) -> None:
        """既定のモデルは無料枠で使えるものであること。

        カタログに載っていることと使えることは別だった。gpt-5 は
        rate_limit_tier が "custom" で、Actions から投げると 400 が返る:
          {"code":"unavailable_model","message":"Unavailable model: gpt-5"}
        カタログを引くテストにはしない（ネットワークに出るため）。
        """
        self.assertNotIn("gpt-5", llm.DEFAULT_MODEL)


class SandboxTest(unittest.TestCase):
    def test_the_sandbox_really_moves_the_paths_out_of_the_repo(self) -> None:
        # このテスト群が本物の data/aliases/ に書いてしまうと、承認していない
        # 項目が公開データに紛れうる。差し替えが効いていることを先に確かめる。
        repo = Path(__file__).resolve().parents[1]
        with sandbox() as root:
            self.assertFalse(store.entries_path("work").is_relative_to(repo))
            self.assertFalse(paths.WEB_ALIASES_JSON.is_relative_to(repo))
            self.assertTrue(store.entries_path("work").is_relative_to(root))
        # 抜けたら元に戻っていること
        self.assertTrue(paths.WEB_ALIASES_JSON.is_relative_to(repo))


# ---------------------------------------------------------------------------
# sources（odj.aliases fetch。外部 API の裏取り）
# ---------------------------------------------------------------------------
#
# ここで固定データとして使う応答は、実際に API を叩いて確認した実測結果
# （phase2-contract.md に記録済み）。ネットワークには一切出ない
# （urllib.request.urlopen を丸ごと差し替える）。


class _FakeResponse:
    """urlopen が返す最小限のレスポンス。read() だけ使う。"""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _fake_urlopen(responses: dict[str, dict]):
    """URL の部分一致で応答を切り替える urlopen の代役。

    どの needle にも当たらないリクエストは AssertionError で落とす。
    「キャッシュがあるのにネットワークに出た」を検出する用途にも使う
    （空の dict を渡せば、呼ばれた時点で必ず落ちる）。
    """

    def _open(req, timeout=None):
        url = req.full_url
        for needle, body in responses.items():
            if needle in url:
                return _FakeResponse(json.dumps(body).encode("utf-8"))
        raise AssertionError(f"想定外のリクエスト: {url}")

    return _open


def _play(*, w: str | None = None, a: str | None = None, e: int = 1, dj: str = "dj",
          t: str = "title") -> dict:
    """block.collect() が読む最小限の plays.json レコード。"""
    rec: dict = {"e": e, "dj": dj, "t": t}
    if w is not None:
        rec["w"] = w
    if a is not None:
        rec["a"] = a
    return rec


def _write_plays(root: Path, plays: list[dict]) -> None:
    path = root / "web" / "public" / "data" / "plays.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"plays": plays}), encoding="utf-8")


class WikipediaRedirectTest(unittest.TestCase):
    """work の①。略称→正式名称のリダイレクト解決。"""

    def test_redirect_is_resolved(self) -> None:
        # 実測: ナナシス → Tokyo 7th シスターズ
        response = {"query": {"redirects": [{"from": "ナナシス", "to": "Tokyo 7th シスターズ"}]}}
        with sandbox(), mock.patch(
            "urllib.request.urlopen", _fake_urlopen({"ja.wikipedia.org": response})
        ):
            evidence = sources._wikipedia_redirect("ナナシス", sources._RateLimiter())
        self.assertEqual(len(evidence), 1)
        got = evidence[0]
        self.assertEqual(got["source"], "wikipedia-ja")
        self.assertEqual(got["id"], "Tokyo 7th シスターズ")
        self.assertEqual(got["title"], "Tokyo 7th シスターズ")
        self.assertEqual(got["kind"], "redirect")
        self.assertIn("ナナシス", got["note"])
        self.assertEqual(got["url"], "https://ja.wikipedia.org/wiki/Tokyo_7th_シスターズ")

    def test_no_redirect_returns_empty(self) -> None:
        # 実測: 「ユーフォ」は曖昧語でリダイレクトが無い。これは誤りではなく、
        # 自動的に「要人手」へ落ちる正しい挙動。
        response = {"query": {"pages": [{"title": "ユーフォ"}]}}
        with sandbox(), mock.patch(
            "urllib.request.urlopen", _fake_urlopen({"ja.wikipedia.org": response})
        ):
            evidence = sources._wikipedia_redirect("ユーフォ", sources._RateLimiter())
        self.assertEqual(evidence, [])


class MusicBrainzTest(unittest.TestCase):
    """artist の①。当たれば正確、外れればタイポの検出器。"""

    def test_zero_hits_is_recorded_as_empty(self) -> None:
        # 実測: Aiobarn（"Aiobahn" のタイポ）は 0件
        response = {"artists": []}
        with sandbox(), mock.patch(
            "urllib.request.urlopen", _fake_urlopen({"musicbrainz.org": response})
        ):
            evidence = sources._musicbrainz_artist("Aiobarn", sources._RateLimiter())
        self.assertEqual(evidence, [])

    def test_exact_match_is_recorded_as_search(self) -> None:
        # 実測: ChouCho → score 100
        response = {"artists": [{"id": "mbid-1", "name": "ChouCho", "score": 100}]}
        with sandbox(), mock.patch(
            "urllib.request.urlopen", _fake_urlopen({"musicbrainz.org": response})
        ):
            evidence = sources._musicbrainz_artist("ChouCho", sources._RateLimiter())
        self.assertEqual(len(evidence), 1)
        got = evidence[0]
        self.assertEqual(got["source"], "musicbrainz")
        self.assertEqual(got["kind"], "search")
        self.assertEqual(got["title"], "ChouCho")
        self.assertEqual(got["id"], "mbid-1")

    def test_alias_match_is_recorded(self) -> None:
        # 実測: AKINO from bless4 → AKINO（alias 経由で解決）
        search_response = {"artists": [{"id": "mbid-2", "name": "AKINO", "score": 90}]}
        alias_response = {"aliases": [{"name": "AKINO from bless4"}, {"name": "秋乃"}]}

        def opener(req, timeout=None):
            url = req.full_url
            if "inc=aliases" in url:
                return _FakeResponse(json.dumps(alias_response).encode("utf-8"))
            return _FakeResponse(json.dumps(search_response).encode("utf-8"))

        with sandbox(), mock.patch("urllib.request.urlopen", opener):
            evidence = sources._musicbrainz_artist("AKINO from bless4", sources._RateLimiter())
        self.assertEqual(len(evidence), 1)
        got = evidence[0]
        self.assertEqual(got["kind"], "alias")
        self.assertEqual(got["title"], "AKINO")
        self.assertIn("AKINO from bless4", got["note"])

    def test_fuzzy_match_without_alias_is_still_recorded(self) -> None:
        # 実測: kz(livetune) → kz。完全一致でも別名登録でもないが、
        # 検索自体はヒットしているのでスコアごと渡す。
        search_response = {"artists": [{"id": "mbid-3", "name": "kz", "score": 100}]}
        alias_response = {"aliases": []}

        def opener(req, timeout=None):
            url = req.full_url
            if "inc=aliases" in url:
                return _FakeResponse(json.dumps(alias_response).encode("utf-8"))
            return _FakeResponse(json.dumps(search_response).encode("utf-8"))

        with sandbox(), mock.patch("urllib.request.urlopen", opener):
            evidence = sources._musicbrainz_artist("kz(livetune)", sources._RateLimiter())
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0]["kind"], "search")
        self.assertEqual(evidence[0]["title"], "kz")


class SourceCacheTest(unittest.TestCase):
    """data/raw/api/ へのキャッシュ。drive.fetch() と同じ思想。"""

    def test_cache_hit_skips_the_network(self) -> None:
        response = {"query": {"redirects": [{"to": "正式名称"}]}}
        with sandbox():
            with mock.patch(
                "urllib.request.urlopen", _fake_urlopen({"ja.wikipedia.org": response})
            ):
                first = sources._wikipedia_redirect("略称", sources._RateLimiter())
            # 2回目はキャッシュから読む。urlopen が呼ばれたら即座に落ちる代役に
            # 差し替えて、本当にネットワークへ出ていないことを確認する。
            with mock.patch("urllib.request.urlopen", _fake_urlopen({})):
                second = sources._wikipedia_redirect("略称", sources._RateLimiter())
        self.assertEqual(first, second)

    def test_cache_file_is_written_under_raw_dir(self) -> None:
        response = {"query": {"redirects": [{"to": "正式名称"}]}}
        with sandbox() as root:
            with mock.patch(
                "urllib.request.urlopen", _fake_urlopen({"ja.wikipedia.org": response})
            ):
                sources._wikipedia_redirect("略称", sources._RateLimiter())
            cache_dir = root / "data" / "raw" / "api" / "wikipedia-ja"
            self.assertTrue(cache_dir.exists())
            self.assertEqual(len(list(cache_dir.glob("*.json"))), 1)


class RateLimiterTest(unittest.TestCase):
    """1 req/sec の間隔。実際に sleep はせず、時計を進めるモックで確認する。"""

    def test_musicbrainz_interval_is_enforced(self) -> None:
        clock = {"t": 0.0}
        sleeps: list[float] = []

        def fake_monotonic() -> float:
            return clock["t"]

        def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)
            clock["t"] += seconds

        with mock.patch.object(sources.time, "monotonic", fake_monotonic), mock.patch.object(
            sources.time, "sleep", fake_sleep
        ):
            limiter = sources._RateLimiter()
            limiter.wait("musicbrainz", 1.0)
            self.assertEqual(sleeps, [])  # 初回は待たない
            clock["t"] += 0.1  # 0.1秒しか経っていないのに次を呼ぶ
            limiter.wait("musicbrainz", 1.0)
        self.assertEqual(sleeps, [0.9])  # 残り0.9秒を待って1秒間隔を守る

    def test_waiting_long_enough_needs_no_extra_sleep(self) -> None:
        clock = {"t": 0.0}
        sleeps: list[float] = []
        with mock.patch.object(sources.time, "monotonic", lambda: clock["t"]), mock.patch.object(
            sources.time, "sleep", lambda s: sleeps.append(s)
        ):
            limiter = sources._RateLimiter()
            limiter.wait("musicbrainz", 1.0)
            clock["t"] += 1.5  # 1秒以上経ってから次を呼ぶ
            limiter.wait("musicbrainz", 1.0)
        self.assertEqual(sleeps, [])

    def test_different_sources_do_not_share_the_clock(self) -> None:
        # Wikipedia と MusicBrainz の間隔は無関係。片方を待たせたからといって
        # もう片方まで遅くする理由が無い。
        clock = {"t": 0.0}
        sleeps: list[float] = []
        with mock.patch.object(sources.time, "monotonic", lambda: clock["t"]), mock.patch.object(
            sources.time, "sleep", lambda s: sleeps.append(s)
        ):
            limiter = sources._RateLimiter()
            limiter.wait("musicbrainz", 1.0)
            limiter.wait("wikipedia-ja", 0.2)
        self.assertEqual(sleeps, [])


class FetchTest(unittest.TestCase):
    """odj.aliases fetch の組み立て（対象の絞り込み・evidence.json の書き出し）。"""

    def test_only_values_seen_twice_or_more_are_targeted(self) -> None:
        # 実測の閾値と同じ考え方: 1回きりの値まで引くとリクエスト数が現実的でない
        plays = [
            _play(w="ナナシス"), _play(w="ナナシス"),  # rows=2 → 対象
            _play(w="単発の元ネタ"),                    # rows=1 → 対象外
        ]
        response = {"query": {"redirects": [{"to": "Tokyo 7th シスターズ"}]}}
        with sandbox() as root:
            _write_plays(root, plays)
            with mock.patch(
                "urllib.request.urlopen", _fake_urlopen({"ja.wikipedia.org": response})
            ):
                result = sources.fetch("work")
            data = json.loads(
                (root / "out" / "aliases" / "evidence.work.json").read_text(encoding="utf-8")
            )
        self.assertEqual(result["candidateTotal"], 1)
        self.assertEqual(data["field"], "work")
        self.assertIn("ナナシス", data["evidence"])
        self.assertNotIn("単発の元ネタ", data["evidence"])
        self.assertEqual(data["evidence"]["ナナシス"][0]["title"], "Tokyo 7th シスターズ")

    def test_a_miss_is_recorded_as_an_empty_list(self) -> None:
        # ヒット無しを [] として必ず記録する。キーごと省略しない。
        plays = [_play(w="謎の略語"), _play(w="謎の略語")]
        no_redirect = {"query": {"pages": [{"title": "謎の略語"}]}}
        no_search = {"search": []}
        with sandbox() as root:
            _write_plays(root, plays)
            opener = _fake_urlopen(
                {"ja.wikipedia.org": no_redirect, "wikidata.org": no_search}
            )
            with mock.patch("urllib.request.urlopen", opener):
                sources.fetch("work")
            data = json.loads(
                (root / "out" / "aliases" / "evidence.work.json").read_text(encoding="utf-8")
            )
        self.assertIn("謎の略語", data["evidence"])
        self.assertEqual(data["evidence"]["謎の略語"], [])

    def test_one_failure_does_not_stop_the_rest(self) -> None:
        """1件の取得失敗で残り全部を止めない。

        GitHub Actions 上で「[ahi:]」（元ネタ列にそう書かれた行が実データに3件
        ある）の取得が落ちただけで、279 件の実行がまるごと死んだ。裏取りは
        補助情報でしかないので、落ちた値は空として記録して先へ進む。
        """
        plays = [
            _play(w="[ahi:]"), _play(w="[ahi:]"),
            _play(w="ナナシス"), _play(w="ナナシス"),
        ]
        redirect = {"query": {"redirects": [{"to": "Tokyo 7th シスターズ"}]}}

        def opener(req, timeout=None):  # noqa: ARG001
            if "ahi" in urllib.parse.unquote(req.full_url):
                raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, None)
            return _FakeResponse(json.dumps(redirect).encode("utf-8"))

        with sandbox() as root:
            _write_plays(root, plays)
            with mock.patch("urllib.request.urlopen", opener), mock.patch("time.sleep"):
                result = sources.fetch("work")
            data = json.loads(
                (root / "out" / "aliases" / "evidence.work.json").read_text(encoding="utf-8")
            )
        # 落ちた値も空として残り、後続の値はちゃんと引けている
        self.assertEqual(data["evidence"]["[ahi:]"], [])
        self.assertEqual(data["evidence"]["ナナシス"][0]["title"], "Tokyo 7th シスターズ")
        self.assertEqual(result["failed"], ["[ahi:]"])
        self.assertEqual(result["hits"], 1)

    def test_everything_failing_stops_early(self) -> None:
        # 相手に絞られている場合に叩き続けても得るものが無い。
        plays = []
        for i in range(_MANY := sources._MAX_CONSECUTIVE_FAILURES + 5):
            plays += [_play(w=f"値{i}"), _play(w=f"値{i}")]

        def opener(req, timeout=None):  # noqa: ARG001
            raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {}, None)

        with sandbox() as root:
            _write_plays(root, plays)
            with mock.patch("urllib.request.urlopen", opener), mock.patch("time.sleep"):
                result = sources.fetch("work")
        self.assertEqual(result["fetched"], sources._MAX_CONSECUTIVE_FAILURES)
        self.assertLess(result["fetched"], _MANY)

    def test_falls_back_to_wikidata_when_wikipedia_has_no_redirect(self) -> None:
        plays = [_play(w="謎の略語"), _play(w="謎の略語")]
        no_redirect = {"query": {"pages": [{"title": "謎の略語"}]}}
        wikidata_hit = {"search": [{"id": "Q1", "label": "正式名称", "description": "何か"}]}
        with sandbox() as root:
            _write_plays(root, plays)
            opener = _fake_urlopen(
                {"ja.wikipedia.org": no_redirect, "wikidata.org": wikidata_hit}
            )
            with mock.patch("urllib.request.urlopen", opener):
                sources.fetch("work")
            data = json.loads(
                (root / "out" / "aliases" / "evidence.work.json").read_text(encoding="utf-8")
            )
        got = data["evidence"]["謎の略語"]
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["source"], "wikidata")
        self.assertEqual(got[0]["title"], "正式名称")

    def test_limit_caps_how_many_are_fetched(self) -> None:
        plays = []
        for i in range(3):
            plays += [_play(w=f"作品{i}"), _play(w=f"作品{i}")]
        no_redirect = {"query": {"pages": []}}
        no_search = {"search": []}
        with sandbox() as root:
            _write_plays(root, plays)
            opener = _fake_urlopen(
                {"ja.wikipedia.org": no_redirect, "wikidata.org": no_search}
            )
            with mock.patch("urllib.request.urlopen", opener):
                result = sources.fetch("work", limit=2)
        self.assertEqual(result["candidateTotal"], 3)
        self.assertEqual(result["fetched"], 2)

    def test_only_new_skips_values_already_in_the_dictionary_and_evidence(self) -> None:
        plays = [
            _play(w="アイカツ!"), _play(w="アイカツ!"),
            _play(w="ラブライブ!"), _play(w="ラブライブ!"),
        ]
        no_redirect = {"query": {"pages": []}}
        no_search = {"search": []}
        with sandbox() as root:
            _write_plays(root, plays)
            store.append_entry(
                "work",
                {"canonical": "アイカツ!", "variants": ["アイカツ!"], "approved": True,
                 "confidence": "high", "reason": "既に辞書済み"},
            )
            opener = _fake_urlopen(
                {"ja.wikipedia.org": no_redirect, "wikidata.org": no_search}
            )
            with mock.patch("urllib.request.urlopen", opener):
                result = sources.fetch("work", only_new=True)
            data = json.loads(
                (root / "out" / "aliases" / "evidence.work.json").read_text(encoding="utf-8")
            )
        # 辞書に既にある「アイカツ!」は飛ばし、「ラブライブ!」だけ引く
        self.assertEqual(result["fetched"], 1)
        self.assertEqual(result["skipped"], 1)
        self.assertIn("ラブライブ!", data["evidence"])
        self.assertNotIn("アイカツ!", data["evidence"])

    def test_a_later_run_merges_into_the_existing_evidence_file(self) -> None:
        # --limit で少しずつ引く運用を想定。今回引かなかった値の結果を消さない。
        plays = [_play(w="A値"), _play(w="A値"), _play(w="B値"), _play(w="B値")]
        no_redirect = {"query": {"pages": []}}
        no_search = {"search": []}
        with sandbox() as root:
            _write_plays(root, plays)
            opener = _fake_urlopen(
                {"ja.wikipedia.org": no_redirect, "wikidata.org": no_search}
            )
            with mock.patch("urllib.request.urlopen", opener):
                sources.fetch("work", limit=1)  # "A値" だけ引く（rows 同点は raw の昇順）
                result = sources.fetch("work", only_new=True)  # 残りの "B値" を引く
            data = json.loads(
                (root / "out" / "aliases" / "evidence.work.json").read_text(encoding="utf-8")
            )
        self.assertEqual(result["fetched"], 1)
        self.assertIn("A値", data["evidence"])
        self.assertIn("B値", data["evidence"])

    def test_invalid_field_is_refused(self) -> None:
        with sandbox() as root:
            _write_plays(root, [])
            with self.assertRaises(sources.SourceError):
                sources.fetch("title")


class FetchCliTest(unittest.TestCase):
    """odj.aliases fetch サブコマンドの配線。"""

    def test_fetch_subcommand_writes_evidence_and_reports_counts(self) -> None:
        plays = [_play(w="ナナシス"), _play(w="ナナシス")]
        response = {"query": {"redirects": [{"to": "Tokyo 7th シスターズ"}]}}
        with sandbox() as root:
            _write_plays(root, plays)
            opener = _fake_urlopen({"ja.wikipedia.org": response})
            buf = io.StringIO()
            with mock.patch("urllib.request.urlopen", opener):
                with contextlib.redirect_stdout(buf):
                    code = cli.main(["fetch", "--field", "work"])
            data = json.loads(
                (root / "out" / "aliases" / "evidence.work.json").read_text(encoding="utf-8")
            )
        self.assertEqual(code, 0)
        self.assertIn("ナナシス", data["evidence"])
        self.assertIn("evidence.work.json", buf.getvalue())
        self.assertIn("ヒット 1", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
