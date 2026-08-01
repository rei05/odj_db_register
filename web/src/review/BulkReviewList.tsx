/**
 * 一括承認モード。
 *
 * work では 150 クラスタのうち 71 件を「危険信号ゼロの候補を機械が承認する」
 * Python 側のコマンドが片付ける見込みで、残る約 79 件の大半は LLM の提案どおり
 * 採用してよいものが占める。1件ずつのカード（ClusterCard.tsx）で捌くには数が
 * 多すぎるので、キューを1クラスタ1行の一覧にして、外すものだけ外してまとめて
 * 承認できるようにする。
 *
 * **新しい一括 API は作らない。** 既存の POST /api/review/decide を選んだ行ぶんだけ
 * 1件ずつ直列で叩く。理由は2つ:
 *   - Python 側（src/odj/aliases/cli.py の _accept）の検査が1件ごとにそのまま効く
 *     （正準名の創作・二重判断などをサーバがいつもどおり弾ける）
 *   - 直列なので、途中の1件が失敗しても後続の行の送信を止めない
 *     （並列だとエラーの原因切り分けが難しくなる。1件ずつ順に見えるほうが、
 *     「どこで何が起きたか」を行の状態としてそのまま表示できる）
 */
import { useState } from 'react'
import { postDecide } from './api.ts'
import { guessCanonical, initialChecked } from './canonical.ts'
import { draftReason } from './draft.ts'
import { hintLabel } from './labels.ts'
import type { ApiError, Cluster, DecidePayload, Field, ProposalKind } from './types.ts'
import { PROPOSAL_KINDS } from './types.ts'

// ProposalKind として妥当な値の集合。types.ts の PROPOSAL_KINDS
// （ClusterCard.tsx のプルダウンとも共有する唯一の正）から作る。
const KNOWN_KINDS = new Set(PROPOSAL_KINDS)

interface BulkRow {
  id: string
  cluster: Cluster
  /** 承認対象に含めるか。ユーザーが自由に付け外しできる。 */
  checked: boolean
  /** 一括承認の対象になり得るか。work で kind が確定しないクラスタは対象外。 */
  eligible: boolean
  /** まとめた後の名前。テキスト入力でその場編集できる。 */
  canonical: string
  /** まとめる生表記（提案の variants、無ければ未判断の値すべて）。 */
  variants: string[]
  /** 提案が「別グループ」と判断して variants に含めなかった値の数（表示用）。 */
  excludedCount: number
  kind: ProposalKind | undefined
  series: string | undefined
  /** 送信する理由の元になる文（一括承認である旨は送信直前に足す。finalReason 参照）。 */
  reason: string
  status: 'idle' | 'sending' | 'error'
  error: string | null
}

/** proposal.kind が KIND_OPTIONS の値として正しいときだけ採用する。
 * 未知の値や欠落は「kind 不明」= 一括承認の対象外の合図にする。 */
function resolveKind(cluster: Cluster): ProposalKind | undefined {
  const k = cluster.proposal?.kind
  return k && KNOWN_KINDS.has(k as ProposalKind) ? (k as ProposalKind) : undefined
}

function buildRow(cluster: Cluster, field: Field): BulkRow {
  const variantsSet = initialChecked(cluster)
  const kind = resolveKind(cluster)
  // artist は kind を送らないので確定させる必要が無い。work だけ kind が要る。
  const eligible = field === 'artist' || kind !== undefined
  const hasProposal = !!cluster.proposal
  const excludedCount = cluster.values.filter(
    (v) => !v.decidedAs && !variantsSet.has(v.raw),
  ).length
  return {
    id: cluster.id,
    cluster,
    // 提案があるクラスタは既定でオン、無いクラスタは既定でオフ。
    // 対象外（!eligible）の行はどのみち送れないのでオンにしない。
    checked: hasProposal && eligible,
    eligible,
    canonical: guessCanonical(cluster, variantsSet),
    variants: [...variantsSet],
    excludedCount,
    kind,
    series: cluster.proposal?.series,
    reason: cluster.proposal?.reason ?? draftReason(cluster),
    status: 'idle',
    error: null,
  }
}

/** 実際に送る理由。あとで見返したとき一括承認だったと分かるよう一筆添える。 */
function finalReason(row: BulkRow): string {
  return `${row.reason}\n※一括承認モードでチェックした内容をそのまま承認`
}

function buildPayload(row: BulkRow, field: Field): DecidePayload {
  const payload: DecidePayload = {
    id: row.id,
    field,
    action: 'accept',
    reason: finalReason(row),
    canonical: row.canonical.trim(),
    variants: row.variants,
  }
  if (field === 'work') {
    if (row.kind) payload.kind = row.kind
    if (row.series?.trim()) payload.series = row.series.trim()
  }
  return payload
}

/** body.code による文言の出し分け。ReviewApp.tsx の submit と同じ扱いにする
 * （特に keep-apart は「別物と決めた組が混ざっている」ことが伝わる言い方にする）。 */
function describeError(body: ApiError): string {
  if (body.code === 'keep-apart') {
    return `${body.error} — チェックを外して分けてください`
  }
  if (body.code === 'already-decided') {
    return `${body.error}（既に判断済みです。画面を開き直すと一覧から消えます）`
  }
  return body.error
}

export default function BulkReviewList({
  field,
  clusters,
  onDecided,
  onOpenSingle,
}: {
  field: Field
  clusters: Cluster[]
  /** 1件承認できるたびに呼ぶ。ReviewApp 側のキュー件数・残数表示を合わせるため。 */
  onDecided: (id: string) => void
  /** 「1件ずつ確認」ボタンから。1件モードへ切り替えてこのクラスタを開く。 */
  onOpenSingle: (id: string) => void
}) {
  // クラスタの並びは mount 時（field 切り替え時。ReviewApp が key={field} で
  // 都度作り直す）に1回だけ取り込む。承認が進むごとに ReviewApp の queue も
  // 縮むが、そちらの再レンダリングでこの一覧の入力途中の状態（チェック・
  // 編集した正準名）が消えては困るので、props の再購読はしない
  // （ClusterCard が cluster.id を key にして丸ごと作り直すのと対称的に、
  // こちらは「一覧全体を作り直さない」ことが要件になる）。
  const [rows, setRows] = useState<BulkRow[]>(() => clusters.map((c) => buildRow(c, field)))
  const [running, setRunning] = useState(false)
  const [progress, setProgress] = useState<{ done: number; total: number } | null>(null)
  const [summary, setSummary] = useState<{ success: number; failure: number } | null>(null)

  const selectedCount = rows.filter((r) => r.checked && r.eligible).length

  function toggle(id: string) {
    setRows((prev) => prev.map((r) => (r.id === id ? { ...r, checked: !r.checked } : r)))
  }

  function editCanonical(id: string, value: string) {
    setRows((prev) => prev.map((r) => (r.id === id ? { ...r, canonical: value } : r)))
  }

  async function runBulk() {
    const targets = rows.filter((r) => r.checked && r.eligible)
    if (targets.length === 0 || running) return
    setRunning(true)
    setSummary(null)
    let success = 0
    let failure = 0
    for (let i = 0; i < targets.length; i++) {
      const row = targets[i]
      setProgress({ done: i, total: targets.length })

      // 空の正準名を創作扱いでサーバに送っても 400 が返るだけなので、
      // ラウンドトリップさせずその場で弾く。
      if (!row.canonical.trim()) {
        failure++
        setRows((prev) =>
          prev.map((r) =>
            r.id === row.id
              ? { ...r, status: 'error', error: 'まとめた後の名前を入力してください' }
              : r,
          ),
        )
        continue
      }

      setRows((prev) =>
        prev.map((r) => (r.id === row.id ? { ...r, status: 'sending', error: null } : r)),
      )
      try {
        const { body } = await postDecide(buildPayload(row, field))
        if (body.ok) {
          success++
          setRows((prev) => prev.filter((r) => r.id !== row.id))
          onDecided(row.id)
        } else {
          failure++
          const message = describeError(body)
          setRows((prev) =>
            prev.map((r) => (r.id === row.id ? { ...r, status: 'error', error: message } : r)),
          )
        }
      } catch (e) {
        failure++
        const message = e instanceof Error ? e.message : String(e)
        setRows((prev) =>
          prev.map((r) => (r.id === row.id ? { ...r, status: 'error', error: message } : r)),
        )
      }
    }
    setProgress({ done: targets.length, total: targets.length })
    setRunning(false)
    setSummary({ success, failure })
  }

  // 一覧も結果まとめも無ければ、この一覧が出す情報は何も無い
  // （ReviewApp 側の「このフィールドは全件レビュー済みです」カードに任せる）。
  if (rows.length === 0 && !summary) return null

  return (
    <div className="review-bulk">
      <div className="review-bulk-toolbar">
        <button
          type="button"
          className="review-btn review-btn-primary review-bulk-run"
          disabled={running || selectedCount === 0}
          onClick={() => void runBulk()}
        >
          選択中 {selectedCount} 件を承認
        </button>
        {running && progress && (
          <span className="muted">
            {progress.done}/{progress.total} 件を送信中…
          </span>
        )}
        {summary && (
          <span className="review-bulk-summary">
            成功 {summary.success} 件 / 失敗 {summary.failure} 件
          </span>
        )}
      </div>

      {rows.length > 0 && (
        <ul className="review-bulk-list">
          {rows.map((row) => (
            <li
              key={row.id}
              className={
                row.eligible ? 'review-bulk-row' : 'review-bulk-row review-bulk-row-disabled'
              }
            >
              <div className="review-bulk-row-head">
                <input
                  type="checkbox"
                  checked={row.checked}
                  disabled={!row.eligible || running}
                  onChange={() => toggle(row.id)}
                />
                <input
                  type="text"
                  className="field review-bulk-canonical"
                  value={row.canonical}
                  disabled={running}
                  onChange={(e) => editCanonical(row.id, e.target.value)}
                />
                <span className="review-bulk-rows muted">{row.cluster.rows}行</span>
                <button
                  type="button"
                  className="link-button review-bulk-open-single"
                  onClick={() => onOpenSingle(row.id)}
                >
                  1件ずつ確認 →
                </button>
              </div>

              {!row.eligible && (
                <p className="review-bulk-note">
                  kind（種別）が分からないため一括承認の対象外です。
                  「1件ずつ確認」から個別カードで判断してください。
                </p>
              )}

              <div className="review-bulk-badges">
                {row.cluster.hints.map((h) => {
                  const label = hintLabel(h, field)
                  return label ? (
                    <span key={h} className="tag" title={label.detail}>
                      {label.title}
                    </span>
                  ) : null
                })}
                <span className="tag">{row.cluster.proposal ? '提案あり' : '提案なし'}</span>
              </div>

              <p className="review-bulk-variants muted">
                {row.variants
                  .map((raw) => {
                    const v = row.cluster.values.find((x) => x.raw === raw)
                    return `「${raw}」${v ? v.rows : 0}行`
                  })
                  .join(' / ')}
                {row.excludedCount > 0 &&
                  `（他 ${row.excludedCount} 件は提案で別グループのため含めません）`}
              </p>

              <details className="review-bulk-reason">
                <summary>送信する理由</summary>
                <pre>{finalReason(row)}</pre>
              </details>

              {row.status === 'sending' && <p className="muted">送信中…</p>}
              {row.status === 'error' && row.error && (
                <p className="notice notice-error review-bulk-error">{row.error}</p>
              )}
            </li>
          ))}
        </ul>
      )}
      {rows.length === 0 && summary && (
        <p className="muted">選択した行はすべて片付きました。</p>
      )}
    </div>
  )
}
