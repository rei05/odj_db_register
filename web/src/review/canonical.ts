/**
 * 「まとめた後の名前」（正準名）を自動で推定する。
 *
 * 以前は**行数が一番多い生表記**を初期値にしていた。提案がある 134 クラスタでは
 * LLM の canonical で上書きされるので目立たないが、提案が付かなかったクラスタ
 * （実データで 16 件。series-risk で LLM が判断を保留したものが多い）ではこれが
 * そのまま出る。行数だけで選ぶと注記付きの表記が正準名になってしまい、
 * 「君のことが大大大大大好きな100人の彼女 2期 ED」「その着せ替え人形は恋をする OP」
 * のような名前が検索の見出しになる。
 *
 * 推定の順は下の guessCanonical の通り。**1文字も創作しない**のがここでの制約で、
 * 出せるのは生表記か、外部 API の正式名称（提案経由）か、
 * rules.strip_notes が注記を剥がした形だけ。creating の禁止は
 * src/odj/aliases/cli.py の _accept が最終的に検査しているので、ここで作った
 * 名前が通らなければサーバが 400 で弾く（**両側の許す範囲は揃えてある**）。
 *
 * 注記を剥がす規則そのものは TS へ移植していない。src/odj/aliases/rules.py の
 * strip_notes 1か所に置いたままにして、block.py が剥がした結果を
 * clusters.<field>.json の value.base に載せてくる。移植すると、
 * 「【推しの子】は括弧まで含めて作品名」「ONE PIECE FILM RED の RED を消さない」
 * といった実データで踏んだ例外が2か所に分かれて必ず乖離する。
 *
 * **base が載るのは work だけ。** strip_notes は元ネタ列の注記を落とす関数なので、
 * アーティスト名に当てると末尾が「劇場版」「映画」「楽曲」で終わる名義を削り
 * かねない。artist では base が無く、③ までで決まる。
 */
import type { Cluster, ClusterValue } from './types.ts'

/** 注記を剥がした形。work 以外と、注記の無い値では生表記そのもの。 */
function baseOf(v: ClusterValue): string {
  return v.base || v.raw
}

export interface CanonicalOption {
  value: string
  /** 生表記ではなく、注記を剥がして作った名前（plays.json には現れない） */
  derived?: boolean
  /** 判断済みの兄弟が既に登録されている正準名 */
  registered?: boolean
}

/**
 * 正準名として選べるもの。並びがそのままプルダウンの並びになる。
 *
 * 判断済みの兄弟が属する正準名も入れる。新しい開催回で増えた表記を既存の
 * クラスへ足す操作が、これが無いとできない。
 */
export function canonicalOptions(
  cluster: Cluster,
  checked: Set<string>,
): CanonicalOption[] {
  const out: CanonicalOption[] = []
  const seen = new Set<string>()
  const add = (value: string, extra?: Omit<CanonicalOption, 'value'>) => {
    if (!value || seen.has(value)) return
    seen.add(value)
    out.push({ value, ...extra })
  }

  for (const v of cluster.values) {
    if (checked.has(v.raw)) add(v.raw)
  }
  if (cluster.proposal?.canonical) add(cluster.proposal.canonical)
  // 注記を剥がした形。チェックした値のぶんだけ出す（チェックを外した値の
  // 注記違いが選べると、まとめない値の名前をクラス全体に付けることになる）。
  for (const v of cluster.values) {
    if (checked.has(v.raw)) add(baseOf(v), { derived: true })
  }
  for (const v of cluster.values) {
    if (v.decidedAs) add(v.decidedAs, { registered: true })
  }
  return out
}

/** 行数の多い順。同数のときはクラスタの並び（= 安定ソート）のまま。 */
function byRows(values: ClusterValue[]): ClusterValue[] {
  return [...values].sort((a, b) => b.rows - a.rows)
}

/**
 * 選んだ名前が、他の値から剥がした形で始まっているなら、そちらへ短くする。
 *
 * strip_notes が知らない注記がある。実データの「ふつうの軽音部 コラボMV」と
 * 「ふつうの軽音部 劇中曲」がそれで、「劇中曲」は落ちるが「コラボMV」は落ちない。
 * すると前者だけが「注記の付いていない表記」に見えて、正準名が
 * 「ふつうの軽音部 コラボMV」になっていた。
 *
 * **別の値から剥がした形が前方一致する**ことが、そこまでが作品名だという
 * 実データ側の証拠になる（1つの値の中だけでは分からない）。ブランド単位で
 * まとめる方針では、より一般的な名前のほうが正準名として適切でもある
 * （「ゆるゆり 1期OP」と「ゆるゆり♪♪」なら、ブランド名の「ゆるゆり」）。
 *
 * 逆向き（剥がした形のほうが長い）には動かさない。「とある科学の超電磁砲」と
 * 「TVアニメ「とある科学の超電磁砲S」 ED」で、シーズン付きの S を正準名に
 * 引き上げてしまう。
 */
function trimToSharedStem(pick: string, alive: ClusterValue[]): string {
  let out = pick
  for (const v of alive) {
    const stem = baseOf(v)
    if (stem !== v.raw && stem.length < out.length && out.startsWith(stem)) out = stem
  }
  return out
}

/**
 * もっともらしい正準名を1つ選ぶ。
 *
 * 順に見て、最初に決まったものを採る。**src/odj/aliases/llm.py の
 * _FIELD_TEXT["work"]["rule3"]（LLM に与えている規則3）と同じ順**にしてある。
 * 人間の画面と LLM の提案で違う名前が出ると、レビューのたびに直すことになる。
 *
 *   ① LLM の提案の canonical … 外部 API の正式名称まで見て決めた結果なので最優先
 *   ② 判断済みの兄弟が登録されている正準名 … 別の名前を立てると検索が割れる
 *   ③ 注記の付いていない表記のうち行数の多いもの（ただし他の値から剥がした形で
 *      始まっていれば、そこまで短くする。trimToSharedStem を参照）
 *   ④ ③が無い（候補が全部注記付き）とき、注記を剥がした形のうち
 *      一番多くの表記が共有するもの
 *
 * ④ で「共有する数」を先に見るのは、同じクラスタの他の値と剥がした形が一致する
 * ことが「それが作品名である」ことの実データ側の裏付けになるから。どれも共有され
 * なければ**短いほうを採る** —— 注記を剥がした結果が長いまま残る値は、注記ではなく
 * 曲名を抱えている。「【MAD】 けいおん! 『ハリケーン!! たくあん!!』」を剥がしても
 * 「MAD けいおん! ハリケーン!! たくあん!!」にしかならず（括弧を外すだけで曲名は
 * 残る）、行数だけで選ぶとこれが正準名になっていた。
 */
export function guessCanonical(cluster: Cluster, checked: Set<string>): string {
  const options = canonicalOptions(cluster, checked)
  if (options.length === 0) return ''
  const has = (value: string) => options.some((o) => o.value === value)

  const proposed = cluster.proposal?.canonical
  if (proposed && has(proposed)) return proposed

  // 複数の正準名に散っているときは選べない（どちらへ足すべきか機械には
  // 決められないので、人間に選ばせる）。
  const registered = [...new Set(cluster.values.map((v) => v.decidedAs).filter(Boolean))]
  if (registered.length === 1 && registered[0] && has(registered[0])) return registered[0]

  const alive = cluster.values.filter((v) => checked.has(v.raw))
  if (alive.length === 0) return options[0].value

  const plain = alive.filter((v) => baseOf(v) === v.raw)
  if (plain.length > 0) return trimToSharedStem(byRows(plain)[0].raw, alive)

  const shared = new Map<string, ClusterValue[]>()
  for (const v of alive) {
    const key = baseOf(v)
    const group = shared.get(key)
    if (group) group.push(v)
    else shared.set(key, [v])
  }
  const rows = (g: ClusterValue[]) => g.reduce((n, v) => n + v.rows, 0)
  const ranked = [...shared.entries()].sort((a, b) => {
    if (b[1].length !== a[1].length) return b[1].length - a[1].length
    if (a[0].length !== b[0].length) return a[0].length - b[0].length
    return rows(b[1]) - rows(a[1])
  })
  return ranked[0][0]
}
