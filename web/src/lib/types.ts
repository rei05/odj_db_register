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
  /** 表記ゆれを吸収した検索キー */
  key: string
  /** リミックス表記を落とした原曲キー */
  base: string
  /** 検索対象をまとめた小文字列 */
  haystack: string
}
