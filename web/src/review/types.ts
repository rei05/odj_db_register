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
  /**
   * 注記（OP・ED・「2期」・TVアニメ「」…）を剥がした形。生表記と同じときと、
   * field が artist のときは入らない（src/odj/aliases/block.py の Value.to_json）。
   * 正準名の自動推定に使う（canonical.ts）。
   */
  base?: string
  /** 曲名欄など、work/artist 以外の列にも同じ文字列が出ている（要注意フラグ） */
  crossField?: boolean
  /**
   * 既に判断済みの値。どの正準名に登録されたかが入る。
   * 新しい開催回で増えた表記を既存の正準名へ足すとき、相手が見えないと
   * 何に寄せればよいか分からないので、カードには出すが選択はさせない。
   */
  decidedAs?: string
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
  /**
   * `odj.aliases auto` が規則で自動承認したクラスタ数（decided の内数）。
   * この画面は人間の判断が要るものだけを出すので、その差分がどこへ行ったかを
   * 説明するために持つ。古い dev サーバーが返さないこともあるので任意にしてある。
   */
  autoApproved?: number
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

/**
 * ProposalKind の実行時版。ユニオン型はコンパイル時に消えるため、値として
 * 検証したい場所（ClusterCard.tsx のプルダウンの並び、BulkReviewList.tsx の
 * 「kind が確定しているか」判定）はここを唯一の正として参照する。二重に持つと
 * 片方だけ更新して食い違う恐れがあるので、種別を増やすときはここ1か所を直す。
 */
export const PROPOSAL_KINDS: readonly ProposalKind[] = [
  'work',
  'vocaloid',
  'vtuber',
  'odj-self',
  'artist-as-work',
  'unknown',
]

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

/**
 * 失敗の種別。**文面ではなくこれで分岐する。**
 *
 *   already-decided … 同じ id を二度判断した。キューを取り直せばよい
 *   keep-apart      … 人間が別物と決めた組を統合しようとした。中身を直す必要がある
 *   conflict        … 同じ表記が別の正準名にも寄っている。既存の項目を先に直す
 *   invalid         … 入力そのものが不正（理由が空、canonical の創作など）
 */
export type ApiErrorCode = 'already-decided' | 'keep-apart' | 'conflict' | 'invalid'

export interface ApiError {
  ok: false
  /** 古い応答には無いことがあるので任意にしてある */
  code?: ApiErrorCode
  error: string
}

export type DecideResult = DecideOk | ApiError
export type ExportResult = ExportOk | ApiError
