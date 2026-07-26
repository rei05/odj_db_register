/**
 * レビュー GUI の型定義。
 *
 * `/private/tmp/.../review-api-contract.md`（3者の唯一の取り決め）の JSON 形を
 * そのまま写している。vite.config.ts 側にも似た型があるが、あちらは
 * tsconfig.node.json（vite.config.ts だけを見る別プロジェクト）の管轄で
 * こちら（tsconfig.app.json = src/ 配下）とは型チェックの単位が分かれているため、
 * 意図的に重複させてある。
 */

export type Field = 'work' | 'artist'

export interface ClusterValue {
  raw: string
  rows: number
  events: number[]
  djs: string[]
  coArtists: string[]
  coTitles: string[]
  /** 曲名欄など、work/artist 以外の列にも同じ文字列が出ている（要注意フラグ） */
  crossField?: boolean
}

export interface ClusterEdge {
  a: string
  b: string
  kinds: string[]
}

/** Phase 2 で LLM が data/aliases/_proposed/ に埋める統合提案。無いことがある。 */
export interface Proposal {
  canonical: string
  series?: string
  /** work|vocaloid|vtuber|odj-self|artist-as-work|unknown を想定するが、
   * 未知の値が来ても落ちないよう緩く string で受ける。 */
  kind?: string
  variants: string[]
  confidence?: string
  reason: string
  source?: string
}

export interface Cluster {
  id: string
  field: Field
  rows: number
  hints: string[]
  edgeKinds: string[]
  values: ClusterValue[]
  edges: ClusterEdge[]
  proposal?: Proposal
}

export interface QueueResponse {
  field: Field
  total: number
  decided: number
  clusters: Cluster[]
}

export type DecideAction = 'accept' | 'reject' | 'defer' | 'keep-apart'

export interface KeepApartPair {
  a: string
  b: string
}

/**
 * canonical の分類。work フィールドのときだけ意味を持つ
 * （アーティストは「アーティストという種別」しかないので artist フィールドでは送らない）。
 */
export type ProposalKind =
  | 'work'
  | 'vocaloid'
  | 'vtuber'
  | 'odj-self'
  | 'artist-as-work'
  | 'unknown'

export interface DecidePayload {
  id: string
  field: Field
  action: DecideAction
  /** 必須。空文字はサーバが 400 で弾く。 */
  reason: string
  where?: string
  // --- action=accept のときだけ ---
  canonical?: string
  series?: string
  kind?: ProposalKind
  variants?: string[]
  // --- action=keep-apart のときだけ ---
  pairs?: KeepApartPair[]
}

export interface DecideOk {
  ok: true
  wrote: string[]
}

export interface ExportOk {
  ok: true
  path: string
  works: number
  artists: number
}

export interface ApiError {
  ok: false
  error: string
}

export type DecideResult = DecideOk | ApiError
export type ExportResult = ExportOk | ApiError
