/**
 * 画面に出す言葉。**内部の語彙をそのまま見せないための対応表。**
 *
 * クラスタや hint の識別子（series-risk, split-from-large…）は block.py が付けた
 * 内部の名前で、初めてこの画面を開いた人には意味が取れない。かといって消すと
 * 作者側が「どの経路で候補に上がったか」を追えなくなるので、**日本語の見出しを
 * 主にして、識別子は小さく添える**方針にしてある。
 *
 * draft.ts にも hint の説明文があるが、あちらは理由欄の下書きに流し込む長い文で、
 * こちらは見出し用の短い語。用途が違うので別に持っている（片方を直したときに
 * もう片方が追随しないのは承知のうえ。統合すると、下書きの文体と画面のラベルの
 * どちらかが必ず不自然になる）。
 */
import type { Field } from './types.ts'

/** hint の見出しと、なぜ注意が要るのかの一行。 */
export interface HintLabel {
  title: string
  detail: string
}

const HINT_COMMON: Record<string, HintLabel> = {
  'split-from-large': {
    title: '大きな塊の破片',
    detail:
      '部分一致の数珠つなぎでできた大きな候補を機械的に割ったもの。中身が本当に同じかは特に疑ってかかること',
  },
  'artist-as-work': {
    title: 'アーティスト名が混ざる',
    detail: '元ネタ欄にアーティスト名が書かれている値を含む',
  },
}

const HINT_BY_FIELD: Record<Field, Record<string, HintLabel>> = {
  work: {
    'series-risk': {
      title: '部分一致だけで繋がった',
      detail:
        '同じブランドのシリーズ作品なら統合してよいが、無関係な語がたまたま部分一致しただけのこともある',
    },
    'series-mark-mismatch': {
      title: '「2期」などの印が食い違う',
      detail:
        '同じブランドならシーズン違いはまとめてよい方針なので、これ単体は分ける理由にならない',
    },
  },
  artist: {
    'series-risk': {
      title: '部分一致だけで繋がった',
      detail: '合同名義と単独名義（`AKINO with bless4` と `AKINO`）が混ざっている恐れ',
    },
    'series-mark-mismatch': {
      title: '数字や「2期」などの印が食い違う',
      detail: 'この欄では意味が薄い（`bless4` の 4 などで付くため）。危険信号ではない',
    },
  },
}

export function hintLabel(hint: string, field: Field): HintLabel | undefined {
  return HINT_BY_FIELD[field]?.[hint] ?? HINT_COMMON[hint]
}

/** 対象フィールドを日本語1語で。カードの問いかけに埋める。 */
export const FIELD_NOUN: Record<Field, string> = {
  work: '元ネタ（作品）',
  artist: 'アーティスト',
}

/**
 * LLM の確信度。**そのまま採用してよい度合いではない**ことが伝わる言い方にする。
 * low は export に出ない（人間が承認しない限り公開データには入らない）。
 */
export const CONFIDENCE_LABEL: Record<string, string> = {
  high: '高（提案は自信あり）',
  medium: '中（要確認）',
  low: '低（疑ってかかること）',
}
