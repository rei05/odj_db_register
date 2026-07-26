"""LLM に投げる前に、規則で書ける表記ゆれを潰す。

判定を LLM に任せるのは「規則で書けないもの」だけにしたい。ここに置くのは
実データを数えて型が分かっているものだけで、迷ったら**触らない**（統合しすぎて
別作品が混ざるより、候補として後段に上げるほうが安い）。

方針は「表記ゆれの統一だけ」で、**構造の分解はしない**。`feat.` や `CV:` や
`from` で切って「本体アーティスト」を取り出す、といったことはやらない。
`AKINO` と `AKINO from bless4` は別の値のまま候補として後段に渡す。
"""

from __future__ import annotations

import re
import unicodedata

# ---------------------------------------------------------------------------
# 比較用のキー
# ---------------------------------------------------------------------------

# カタカナ → ひらがな（U+30A1..U+30F6 が U+3041..U+3096 に対応する）。
# ヷヸヹヺ(U+30F7..) には対応するひらがなが無いので範囲から外してある。
_KATAKANA_START = 0x30A1
_KATAKANA_END = 0x30F6
_KANA_OFFSET = 0x60


def _to_hiragana(text: str) -> str:
    return "".join(
        chr(ord(c) - _KANA_OFFSET)
        if _KATAKANA_START <= ord(c) <= _KATAKANA_END
        else c
        for c in text
    )


def agg_key(text: str) -> str:
    """クラスタリングのための内部キー。

    NFKC → 小文字化 → カタカナをひらがなへ → 文字と数字以外を全部落とす。
    記号を落とすので「アイカツ!」と「アイカツ」、「ヴァイオレット・エヴァーガーデン」と
    「ヴァイオレットエヴァーガーデン」、「CLANNAD~AFTER STORY」と
    「CLANNAD 〜AFTER STORY〜」、「☆」と「★」がここで潰れる。

    **web/src/lib/normalize.ts の normKey() を移植したものではない。**
    あちらは GUI が既出判定と集計に使う本番のキーで、こちらは候補を束ねるためだけの
    内部キー。同じに保とうとすると必ず乖離する（片方を直したときにもう片方が
    追随しない）ので、意図的に別物として書いてある。ここを変えても GUI の挙動は
    1ミリも変わらないし、変わってはいけない。

    長音記号「ー」は残す（Unicode 上も記号ではなく文字）。落とすと「ビート」と
    「ビト」が同じになってしまい、行き過ぎる。
    """
    s = unicodedata.normalize("NFKC", text or "").lower()
    s = _to_hiragana(s)
    # L*(文字) と N*(数字) だけ残す。P*(約物) S*(記号) Z*(空白) は落ちる。
    return "".join(c for c in s if unicodedata.category(c)[0] in "LN")


# ---------------------------------------------------------------------------
# 注記の除去（主に元ネタ列）
# ---------------------------------------------------------------------------

# 括弧は「中身を残して括弧文字だけ落とす」。
# 中身を取り出す実装にすると「【MAD】 けいおん! 『ハリケーン!! たくあん!!』」で
# 曲名のほうを作品名として拾ってしまう。逆に括弧ごと捨てる実装にすると
# 「【推しの子】 第2期」が消える（【推しの子】は括弧まで含めて作品名）。
_BRACKET_CHARS = str.maketrans("", "", "「」『』【】〔〕〈〉《》")

# 先頭の媒体表記。「TVアニメ「Engage Kiss」 ED」「劇場版 呪術廻戦 0」
# 「ゲームアマガミED」「映画けいおん!」など。
# 「映画けいおん!」→「けいおん!」のように TV シリーズと劇場版が同じ値に寄るが、
# それを統合するかどうかは後段の判断なので、ここでは候補に上げるだけにする。
_PREFIX_RE = re.compile(
    r"^\s*(?:TVアニメーション|TVアニメ|テレビアニメ|アニメ映画|劇場版アニメ"
    r"|劇場版|映画|アニメ|ゲーム|TV)\s*",
    re.IGNORECASE,
)

# 末尾の注記。1回で全部消さず、消えなくなるまで繰り返し当てる
# （「ゆるゆり 1期OP」→「ゆるゆり 1期」→「ゆるゆり」、
#   「てーきゅう5期 主題歌」→「てーきゅう5期」→「てーきゅう」）。
#
# OP / ED の直前に英字が来る場合は消さない。これを入れないと
# 「ONE PIECE FILM RED」が「ONE PIECE FILM R」に、「J-POP」が「J-P」になる。
_TAIL_RE = re.compile(
    r"(?:"
    r"(?<![A-Za-z])(?:OP|ED)\d*"
    # 「シーズン」は数字を伴うものだけ。裸で消すと「ヤマノススメ サードシーズン」が
    # 「ヤマノススメ サード」に、「進撃の巨人 The Final Season」が「The Final」になる。
    r"|第?\d+期|第\d+シーズン|シーズン\d+|SEASON\s*\d+|\d+年目\d*"
    r"|収録楽曲|タイアップ楽曲|\d*周年楽曲|楽曲"
    r"|ED?主題歌|主題歌|挿入歌|劇中曲|劇伴|テーマソング"
    r"|キャラクターソング|キャラソン|イメージソング"
    r"|劇場版|映画"
    r")\s*$",
    re.IGNORECASE,
)

_SPACES_RE = re.compile(r"[\s　]+")


def strip_notes(text: str) -> str:
    """元ネタ列に付く注記を剥がす。

    実データの内訳（2410 行時点）は OP/ED の接尾が約 100 行、主題歌・挿入歌・
    劇中曲が 20 行、TVアニメ「」や劇場版の接頭が 22 行、「〜楽曲」の接尾が 18 行。
    どれも作品名そのものではないので、比較の前に落とす。

        strip_notes("TVアニメ「Engage Kiss」 ED") == "Engage Kiss"
        strip_notes("アイカツ! 楽曲")             == "アイカツ!"
        strip_notes("てーきゅう5期 主題歌")        == "てーきゅう"

    剥がした結果が 2 文字未満になる場合は剥がさない（「OP」だけの値など、
    注記しか書かれていない行を空文字にしてしまわないため）。
    """
    s = _SPACES_RE.sub(" ", (text or "").translate(_BRACKET_CHARS)).strip()
    while True:
        before = s
        s = _PREFIX_RE.sub("", s, count=1).strip()
        if len(s) < 2:  # 接頭辞しか無かった
            s = before
        before_tail = s
        s = _TAIL_RE.sub("", s, count=1).strip()
        if len(s) < 2:
            s = before_tail
        if s == before:
            break
    return s or (text or "").strip()


# 「第2期」「シンデレラグレイ」のような続編・派生を表す手掛かり。
# 揃っていない組は series-risk（別作品を混ぜる危険）として後段に警告を出す。
_SERIES_MARK_RE = re.compile(
    r"\d+期|\d+年目|シーズン\d*|season\s*\d*|\d+nd|\d+rd|\d+th"
    r"|劇場版|映画|新編|続編|第\d+部|part\s*\d+|\d+",
    re.IGNORECASE,
)


def series_marks(text: str) -> frozenset[str]:
    """続編・シリーズを示す印を拾う。中身の比較ではなく有無の比較に使う。

    「響け!ユーフォニアム」と「響け!ユーフォニアム3」、「てーきゅう 2期」と
    「てーきゅう 4期」のように、印が食い違う組は統合してはいけない可能性が高い。
    """
    return frozenset(m.group().lower() for m in _SERIES_MARK_RE.finditer(text or ""))


# ---------------------------------------------------------------------------
# 区切り文字・feat. / CV: の統一（主にアーティスト列）
# ---------------------------------------------------------------------------

# 実データでは同じメンバー列が中黒版と読点版で並存している
# （「わか,ふうり,すなお from STAR☆ANIS」と「わか・ふうり・すなお from STAR☆ANIS」）。
# 数は中黒 71 行 / 読点 48 行 / カンマ 26 行 / スラッシュ 19 行。多数派の中黒に寄せる。
# 連続した区切りは1つに畳む（"DJ'TEKINA//SOMETHING" が中黒2つにならないように）。
#
# なお「Wake Up, Girls!」「Fear, and Loathing in Las Vegas」のように、区切りでない
# カンマまで中黒になる。agg_key は区切り文字を落とすのでクラスタリングには
# 影響しないが、**この関数の出力は正しい表記の提案ではない**。表示と最終的な
# 正準表記の決定には生の値を使うこと。
_LIST_SEP_RE = re.compile(r"\s*[、,，/／|｜･・]+\s*")

# 「&」は区切りに見えて名前の一部であることが多い（MYTH & ROID、W&W、Y&Co.）ので
# 区切りとしては扱わない。全角の ＆ は NFKC が & にしてくれるし、前後の空白の有無は
# agg_key が落とすので、ここで触る必要はない。

# feat. / feat / ft. / featuring が混在。英単語の一部を誤爆しないよう前後を見る
# （"Daft Punk" の ft、"Feather" の feat）。ピリオド付きの綴りを先に置いてあるのは、
# 後ろに置くと "feat.PANXI" で feat だけが食われて ".PANXI" が残るため。
_FEAT_RE = re.compile(
    r"\s*(?<![A-Za-z])"
    r"(?:featuring(?![A-Za-z])|feat\.|ft\.|feat(?![A-Za-z])|ft(?![A-Za-z]))\s*",
    re.IGNORECASE,
)

# (CV: / (cv: / (CV. / (CV:␣ の 5 通りが実在する。
# なお CV を省いて「(声優名)」とだけ書く行もあるが、そちらは補わない
# （書いてないものを足すのは表記ゆれの統一ではなく推定なので、後段に任せる）。
_CV_RE = re.compile(r"\s*\(\s*CV\s*[:.、,]?\s*", re.IGNORECASE)


def normalize_separators(text: str) -> str:
    """アーティスト名の区切り文字と feat. / CV: の書き方を揃える。

        normalize_separators("わか,ふうり,すなお from STAR☆ANIS")
            == "わか・ふうり・すなお from STAR☆ANIS"
        normalize_separators("宮内れんげ (CV.小岩井ことり)")
            == "宮内れんげ(CV:小岩井ことり)"

    アーティスト列向け。元ネタ列に当てるとスラッシュを潰してしまうので
    （「Fate/stay night」）、呼ぶ側で使い分ける。
    """
    s = unicodedata.normalize("NFKC", text or "").strip()
    s = _SPACES_RE.sub(" ", s)
    s = _CV_RE.sub("(CV:", s)
    s = _FEAT_RE.sub(" feat. ", s)
    s = _LIST_SEP_RE.sub("・", s)
    # 「W&W (feat. Kizuna AI)」のように括弧の中で置換が起きると空白が余る
    s = re.sub(r"\(\s+", "(", s)
    s = re.sub(r"\s+\)", ")", s)
    return s.strip(" ・")
