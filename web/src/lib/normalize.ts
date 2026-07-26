/**
 * 曲名の表記ゆれを吸収する。
 *
 * DB はプレイログをそのまま持っていて名寄せしていないので、既出判定と集計は
 * ここで作るキーで突き合わせる。
 */

/** 全角半角・大小・記号・空白を均して比較用のキーにする。 */
export function normKey(text: string): string {
  return text
    .normalize('NFKC')
    .toLowerCase()
    .replace(/[〜～~]/g, '~')
    .replace(/[‐‑‒–—―ー−]/g, '-')
    .replace(/[’‘`´]/g, "'")
    .replace(/[”“]/g, '"')
    .replace(/[!！]/g, '!')
    .replace(/[?？]/g, '?')
    .replace(/[\s　]+/g, '')
    .replace(/[・･,、.。/／\\|:：;；*＊☆★♡♪＿_+＋]/g, '')
    .trim()
}

/**
 * リミックス/ブートレグの表記を落として原曲名に寄せる。
 *
 * 「硝子ドール [TeddyLoid Remix]」と「硝子ドール(deshino NRG Bootleg)im Yurima mix」
 * を同じ原曲として扱いたい。
 */
const BRACKETS = /[([{（【〔［][^)\]}）】〕］]*[)\]}）】〕］]/g
const BRACKET_GROUP = /[([{（【〔［]([^)\]}）】〕］]*)[)\]}）】〕］]/g
const REMIX_WORDS =
  /\b(remix|remixed|bootleg|boot|edit|mix|ver|version|cover|arrange|arranged|mashup|vip|extended|inst|instrumental)\b/i
// 語の途中で切らないよう、前後の両方に境界を要求する。後ろの \b しか無いと
// 「FOREVER LOST」が「FORE」になる（FORE|VER の ver を拾ってしまう）。
const REMIX_TAIL =
  /[-_\s]*(?<![A-Za-z])(remixed|remix|bootleg|boot|edit|mix|ver|version|covered|cover|arranged|arrange|mashup|vip|extended|short|full|inst|instrumental|feat|ft)\b.*$/i

/**
 * リミックス表記を含む括弧が出てきたら、そこから後ろはまとめて捨てる。
 *
 * 「硝子ドール(deshino NRG Bootleg)im Yurima mix」のように括弧の後ろにも
 * 書き足されていることがあり、括弧を外すだけでは原曲名にならない。
 * 一方「Won(*3*)ChuKissMe!」のように括弧が曲名の一部のこともあるので、
 * 中身がリミックス表記のときだけ切る。
 */
function cutAtRemixBracket(text: string): string {
  BRACKET_GROUP.lastIndex = 0
  let m: RegExpExecArray | null
  while ((m = BRACKET_GROUP.exec(text)) !== null) {
    if (REMIX_WORDS.test(m[1]) && m.index > 0) return text.slice(0, m.index)
  }
  return text
}

/**
 * ダッシュ区切りのリミックス表記が出てきたら、そこから後ろは捨てる。
 *
 * 「PSI-missing -2011 remix-」「Tulip -TAKU INOUE Remix-」のように括弧ではなく
 * ダッシュでリミックス表記を囲う書き方があり、REMIX_TAIL だけでは足りない。
 * REMIX_TAIL はダッシュとリミックス語の間に別の語が挟まると届かず、
 * 「-2011 remix-」では「 remix-」しか落ちずに「PSI-missing -2011」が残る。
 *
 * 「PSI-missing」の側のハイフンを切ってしまわないよう、前後どちらかに空白が
 * あるダッシュだけを境界と見る。曲名の一部のハイフンは空白を伴わない。
 * 長音記号は「ジャーニー」のような曲名を壊すので入れない。
 *
 * 括弧を外した後に呼ぶ。「Triad Primus - Trancing Pulse (brz_bootleg_remix)」の
 * ように Artist - Title 形式で括弧内にリミックス表記があるものは、先に
 * cutAtRemixBracket が括弧ごと落としてくれるので、ここには残らない。
 */
const REMIX_DASH = /(?:\s[-‐‑‒–—―−]|[-‐‑‒–—―−]\s)/g
function cutAtRemixDash(text: string): string {
  REMIX_DASH.lastIndex = 0
  let m: RegExpExecArray | null
  while ((m = REMIX_DASH.exec(text)) !== null) {
    if (m.index > 0 && REMIX_WORDS.test(text.slice(m.index))) {
      return text.slice(0, m.index)
    }
  }
  return text
}

export function baseKey(text: string): string {
  let s = cutAtRemixBracket(text.normalize('NFKC'))
  // 残った括弧の中身は作者名などなので外す（曲名側の文字は残す）
  let prev = ''
  while (prev !== s) {
    prev = s
    s = s.replace(BRACKETS, ' ')
  }
  s = cutAtRemixDash(s)
  s = s.replace(REMIX_TAIL, ' ')
  const key = normKey(s)
  // 丸ごとリミックス表記だった場合は元の文字列に戻す
  return key.length >= 2 ? key : normKey(text)
}

/** 検索用に前処理した語で部分一致を見る。 */
export function matchesQuery(haystack: string, query: string): boolean {
  const terms = query
    .split(/[\s　]+/)
    .filter(Boolean)
    .map((t) => normKey(t))
    .filter(Boolean)
  if (terms.length === 0) return true
  return terms.every((t) => haystack.includes(t))
}
