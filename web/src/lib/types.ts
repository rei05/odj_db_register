/** plays.json のキーは行数が多いので詰めてある。ここで名前を戻す。 */
export interface RawPlay {
  e: number // 開催回
  p: number | null // play順
  dj: string
  n: number | null // 曲順
  t: string // タイトル
  w: string | null // アニメ・元ネタ
  a: string | null // アーティスト
  r: boolean | null // REMIX
  u: string | null // URL
  k: string // 由来（xlsx / manual / txt / master-db）
}

export interface RawEvent {
  no: number
  date: string
  djs: string[]
}

export interface Dataset {
  generatedAt: string
  events: RawEvent[]
  plays: RawPlay[]
}

/**
 * 元ネタの分類。work 以外は「作品名」ではない
 * （ボカロ・VTuber・オタクDJ大会自体・アーティスト名が元ネタ欄に
 * 書かれたもの・分類不能）ので、作品ランキングでは work だけを数える。
 */
export type AliasKind =
  | 'work'
  | 'vocaloid'
  | 'vtuber'
  | 'odj-self'
  | 'artist-as-work'
  | 'unknown'

/**
 * 表記ゆれの同値クラス1件。web/public/data/aliases.json の値の形。
 *
 * キーは odj.aliases store が書いた生の文字列（normKey 適用前）。
 * 同じ同値クラスに属する全ての生表記がキーとして辞書に入っており、
 * どれを引いても同じ c / v が返る。
 */
export interface AliasEntry {
  /** canonical（正準名） */
  c: string
  /** series（シリーズ名、省略あり） */
  s?: string
  /** kind（省略あり。artist 側は付かないことがある） */
  k?: AliasKind
  /** variants（この同値クラスに属する生表記の全体） */
  v: string[]
}

/** web/public/data/aliases.json の形。辞書がまだ育っていない生表記も多い。 */
export interface Aliases {
  generatedAt: string
  works: Record<string, AliasEntry>
  artists: Record<string, AliasEntry>
}

export interface Play {
  eventNo: number
  eventDate: string
  playOrder: number | null
  dj: string
  trackNo: number | null
  title: string
  work: string | null
  artist: string | null
  isRemix: boolean | null
  url: string | null
  sourceKind: string
  /** 辞書適用後の元ネタ正準名。辞書に無ければ work をそのまま、work が無ければ null */
  workCanon: string | null
  /** 元ネタのシリーズ名。辞書に無ければ null */
  workSeries: string | null
  /** 元ネタの分類。辞書に無ければ null（work とは限らないので集計側で弾く） */
  workKind: AliasKind | null
  /** 辞書適用後のアーティスト正準名。辞書に無ければ artist をそのまま、artist が無ければ null */
  artistCanon: string | null
  /** 表記ゆれを吸収した検索キー */
  key: string
  /** リミックス表記を落とした原曲キー */
  base: string
  /** 検索対象をまとめた小文字列 */
  haystack: string
}
