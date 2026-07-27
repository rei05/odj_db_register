/**
 * 理由の下書きを、クラスタの事実だけから組み立てる。
 *
 * LLM の提案が付くのは 152 クラスタ中 92 件で、**残り 60 件のうち 41 件は
 * series-risk**。LLM が「迷ったら分ける」に従ってグループを出さなかったもので、
 * つまり**一番判断が難しいクラスタほど手掛かりが無い**状態になっていた。
 *
 * ここで書くのは行数・根拠の種別・共起・ヒントといった見れば分かる事実だけで、
 * 統合の可否は書かない。人間が消すなり足すなりする土台にする。
 */
import type { Cluster } from './types.ts'

/** 辺の種別を、何が根拠なのか読んで分かる日本語にする。 */
const EDGE_LABEL: Record<string, string> = {
  caseonly: '大小・空白だけの差',
  redirect: '外部 API が同じものへの別名だと言っている',
  agg: '注記（OP・ED・楽曲など）を剥がすと一致',
  cooccur: '同じ曲で表記だけ違う',
  edit: '綴りが近い（タイポ）',
  bigram: '文字の重なりが多い',
  substr: '片方が片方に含まれるだけ（最も弱い）',
}

const HINT_LABEL: Record<string, string> = {
  'series-risk': '部分一致だけで繋がった組を含む。シリーズの別作品が混ざっている恐れ',
  'series-mark-mismatch': '「2期」「劇場版」のような続編の印が食い違う値が混ざる',
  'split-from-large': '元は繋がりすぎた大きな塊の破片。中身を疑ってかかること',
  'artist-as-work': 'アーティスト名が元ネタ欄に入っている値を含む',
}

export function draftReason(cluster: Cluster): string {
  const lines: string[] = []

  const counts = [...cluster.values]
    .sort((a, b) => b.rows - a.rows)
    .map((v) => `「${v.raw}」${v.rows}行`)
    .join(' / ')
  lines.push(counts + '。')

  const kinds = cluster.edgeKinds.length
    ? cluster.edgeKinds
    : [...new Set(cluster.edges.flatMap((e) => e.kinds))]
  const known = kinds.filter((k) => EDGE_LABEL[k])
  if (known.length) {
    lines.push('機械が繋いだ根拠: ' + known.map((k) => EDGE_LABEL[k]).join('、') + '。')
  }

  // 共起は「同じ DJ が両方の表記を使っている」「曲名が重なる」が表記ゆれの証拠に
  // なるので、重なっているものだけを出す。
  const shared = (pick: (v: (typeof cluster.values)[number]) => string[]) => {
    if (cluster.values.length < 2) return []
    return cluster.values
      .map((v) => new Set(pick(v)))
      .reduce((acc, s) => new Set([...acc].filter((x) => s.has(x))))
  }
  const djs = [...shared((v) => v.djs)]
  if (djs.length) lines.push(`同じ DJ（${djs.slice(0, 3).join('、')}）が両方の表記を使っている。`)
  const titles = [...shared((v) => v.coTitles)]
  if (titles.length) lines.push(`曲名が重なる: ${titles.slice(0, 3).join('、')}。`)
  const artists = [...shared((v) => v.coArtists)]
  if (artists.length) lines.push(`アーティストが重なる: ${artists.slice(0, 3).join('、')}。`)

  for (const h of cluster.hints) {
    if (HINT_LABEL[h]) lines.push(`※ ${HINT_LABEL[h]}。`)
  }
  return lines.join('\n')
}
