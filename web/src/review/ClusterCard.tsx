import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { Play } from '../lib/types.ts'
import SearchPreview from './SearchPreview.tsx'
import { draftReason } from './draft.ts'
import type { Cluster, DecidePayload, ProposalKind } from './types.ts'

const KIND_OPTIONS: { value: ProposalKind; label: string }[] = [
  { value: 'work', label: '作品（アニメ・ゲーム等）' },
  { value: 'vocaloid', label: 'ボーカロイド' },
  { value: 'vtuber', label: 'VTuber' },
  { value: 'odj-self', label: 'オタクDJ大会自体' },
  { value: 'artist-as-work', label: 'アーティスト名を元ネタ欄に書いたもの' },
  { value: 'unknown', label: '不明' },
]
const KNOWN_KINDS = new Set(KIND_OPTIONS.map((k) => k.value))

/** キーボード操作([a][r][s][k][e])から呼ばれるハンドラ一式。ReviewApp が保持する。 */
export interface ClusterActions {
  accept: () => void
  reject: () => void
  defer: () => void
  toggleKeepApart: () => void
  cancelKeepApart: () => void
  focusReason: () => void
}

/**
 * 初期チェック。**提案があるときは提案の variants だけ**をチェックする。
 *
 * LLM は1つのクラスタから「まとめる根拠があるものだけ」を返す契約で（llm.py の
 * 絶対規則1「迷ったら分ける」）、variants に入らなかった値は**別物と判断された値**
 * である。全部チェックした状態で出していた頃は、その分を人間が毎回手で外していた
 * （実データで work 21 件・artist 22 件の提案がクラスタの一部を除外している）。
 *
 * 判断済みの値（decidedAs）はどちらの経路でもチェックしない。既存の正準名へ足す
 * 相手として見せるだけで、もう一度送るとサーバに already-decided で弾かれる。
 *
 * 提案の variants が1つも残らなかったときは全部チェックへ戻す。queue 側が未判断の
 * 値だけに絞る（vite.config.ts）ため起こり得るが、0件のカードは採用ボタンが
 * disabled で判断できなくなるため。
 */
function initialChecked(cluster: Cluster): Set<string> {
  const alive = cluster.values.filter((v) => !v.decidedAs).map((v) => v.raw)
  const proposed = cluster.proposal?.variants ?? []
  const picked = alive.filter((raw) => proposed.includes(raw))
  return new Set(picked.length > 0 ? picked : alive)
}

/**
 * 提案が「別グループ」と判断した値。カードで印を付けるためだけに使う。
 *
 * チェックが外れている理由が見えないと、人間には「提案の判断」と「単に忘れている」
 * の区別が付かない。トグルしても印は消さない（現在の状態ではなく、提案が何と
 * 言ったかを示すラベルなので）。
 */
function proposalExcluded(cluster: Cluster, initial: Set<string>): Set<string> {
  if (!cluster.proposal?.variants.length) return new Set()
  return new Set(
    cluster.values
      .filter((v) => !v.decidedAs && !initial.has(v.raw))
      .map((v) => v.raw),
  )
}

function initialCanonical(cluster: Cluster): string {
  if (cluster.proposal?.canonical) return cluster.proposal.canonical
  const byRows = [...cluster.values].sort((a, b) => b.rows - a.rows)
  return byRows[0]?.raw ?? ''
}

function initialKind(cluster: Cluster): ProposalKind {
  const k = cluster.proposal?.kind
  return k && KNOWN_KINDS.has(k as ProposalKind) ? (k as ProposalKind) : 'unknown'
}

export default function ClusterCard({
  cluster,
  plays,
  onSubmit,
  registerActions,
}: {
  cluster: Cluster
  plays: Play[]
  onSubmit: (payload: DecidePayload) => void
  registerActions: (actions: ClusterActions | null) => void
}) {
  // すべて cluster.id をキーに親から再マウントされる前提の初期値（ClusterCard 自体は
  // 親（ReviewApp）側で key={cluster.id} を付けて呼ばれるので、ここでの useState 初期化は
  // カードが替わるたびにやり直される。
  const [checked, setChecked] = useState<Set<string>>(() => initialChecked(cluster))
  // 印の基準は初期状態のほう。checked を見ると、人間がチェックを外した値まで
  // 「提案が別グループと言った」ことになる。
  const excluded = useMemo(
    () => proposalExcluded(cluster, initialChecked(cluster)),
    [cluster],
  )
  const [canonical, setCanonical] = useState(() => initialCanonical(cluster))
  const [series, setSeries] = useState(() => cluster.proposal?.series ?? '')
  const [kind, setKind] = useState<ProposalKind>(() => initialKind(cluster))
  // 提案が無いクラスタほど判断が難しい（series-risk で LLM が答えを出さなかった
  // ものが多い）ので、空欄で放り出さずに事実の下書きを入れておく。
  const [reason, setReason] = useState(
    () => cluster.proposal?.reason ?? draftReason(cluster),
  )
  const [reasonWarn, setReasonWarn] = useState(false)
  const [keepApartMode, setKeepApartMode] = useState(false)
  const [keepApartSelected, setKeepApartSelected] = useState<Set<string>>(new Set())
  const [keepApartError, setKeepApartError] = useState<string | null>(null)
  const reasonRef = useRef<HTMLTextAreaElement>(null)

  const canonicalOptions = useMemo(() => {
    const opts: string[] = []
    for (const v of cluster.values) {
      if (checked.has(v.raw) && !opts.includes(v.raw)) opts.push(v.raw)
    }
    if (cluster.proposal?.canonical && !opts.includes(cluster.proposal.canonical)) {
      opts.push(cluster.proposal.canonical)
    }
    // 判断済みの兄弟が属する正準名も選べるようにする。新しい開催回で増えた
    // 表記を既存のクラスへ足す操作が、これが無いとできない。
    for (const v of cluster.values) {
      if (v.decidedAs && !opts.includes(v.decidedAs)) opts.push(v.decidedAs)
    }
    return opts
  }, [cluster, checked])

  // チェックを外して canonical が選べなくなったら、選べる候補へ寄せる
  // （正準名は「創作」させず、必ず variants か proposal の中から選ばせる契約のため）。
  useEffect(() => {
    if (canonicalOptions.length > 0 && !canonicalOptions.includes(canonical)) {
      setCanonical(canonicalOptions[0])
    }
  }, [canonicalOptions, canonical])

  function toggleChecked(raw: string) {
    setChecked((prev) => {
      const next = new Set(prev)
      if (next.has(raw)) next.delete(raw)
      else next.add(raw)
      return next
    })
  }

  function toggleKeepApartPick(raw: string) {
    setKeepApartError(null) // 選び直したら案内は消す
    setKeepApartSelected((prev) => {
      const next = new Set(prev)
      if (next.has(raw)) next.delete(raw)
      else next.add(raw)
      return next
    })
  }

  const requireReason = useCallback((): boolean => {
    if (reason.trim()) return true
    setReasonWarn(true)
    reasonRef.current?.focus()
    return false
  }, [reason])

  const actions = useMemo<ClusterActions>(() => {
    return {
      accept: () => {
        if (checked.size === 0 || !requireReason()) return
        const variants = cluster.values
          .filter((v) => checked.has(v.raw))
          .map((v) => v.raw)
        const payload: DecidePayload = {
          id: cluster.id,
          field: cluster.field,
          action: 'accept',
          reason,
          canonical,
          variants,
        }
        if (cluster.field === 'work') {
          payload.kind = kind
          if (series.trim()) payload.series = series.trim()
        }
        onSubmit(payload)
      },
      reject: () => {
        if (!requireReason()) return
        // 却下は「このカードの値はどれも統合しない」という判断なので、
        // 対象を variants として明示する。これが無いとキューが
        // 「まだ判断していない値」として次の周でまた出してしまう。
        onSubmit({
          id: cluster.id,
          field: cluster.field,
          action: 'reject',
          reason,
          // 判断済みの値まで送ると already-decided で弾かれる
          variants: cluster.values.filter((v) => !v.decidedAs).map((v) => v.raw),
        })
      },
      defer: () => {
        if (!requireReason()) return
        onSubmit({ id: cluster.id, field: cluster.field, action: 'defer', reason })
      },
      toggleKeepApart: () => {
        if (!keepApartMode) {
          setKeepApartMode(true)
          setKeepApartSelected(new Set())
          return
        }
        // ちょうど2つだけ受ける。以前は選んだ全組み合わせを登録していたため、
        // 「とある」系で5つ選んだら 10 組が入り、「超電磁砲」と
        // 「とある科学の超電磁砲」（略称なので同じ作品）や
        // 「とある科学の超電磁砲S」と「TVアニメ「…S」ED」（注記違い）まで
        // 別物として固定されてしまった。3つ以上を分けたいときは k を繰り返す。
        if (keepApartSelected.size === 2) {
          if (!requireReason()) return
          const [a, b] = [...keepApartSelected]
          onSubmit({
            id: cluster.id,
            field: cluster.field,
            action: 'keep-apart',
            reason,
            pairs: [{ a, b }],
          })
          setKeepApartMode(false)
          setKeepApartSelected(new Set())
          return
        }
        if (keepApartSelected.size > 2) {
          // 黙って取り消すと「なぜ登録されないのか」が分からない
          setKeepApartError('別物として固定するのはちょうど2つです（3つ以上は k を繰り返す）')
          return
        }
        // 1つも選ばないまま k をもう一度押したら取消として扱う
        setKeepApartError(null)
        setKeepApartMode(false)
      },
      cancelKeepApart: () => {
        setKeepApartMode(false)
        setKeepApartSelected(new Set())
        setKeepApartError(null)
      },
      focusReason: () => reasonRef.current?.focus(),
    }
  }, [
    checked,
    canonical,
    series,
    kind,
    reason,
    keepApartMode,
    keepApartSelected,
    cluster,
    onSubmit,
    requireReason,
  ])

  useEffect(() => {
    registerActions(actions)
    return () => registerActions(null)
  }, [actions, registerActions])

  return (
    <div className="card review-card">
      <div className="review-card-head">
        <span className="review-card-id">{cluster.id}</span>
        <span className="review-card-rows">rows {cluster.rows}</span>
        {cluster.hints.map((h) => (
          <span className="tag" key={h}>
            {h}
          </span>
        ))}
      </div>

      <ul className="review-values">
        {cluster.values.map((v) => (
          <li key={v.raw} className="review-value">
            {keepApartMode ? (
              <button
                type="button"
                className={
                  keepApartSelected.has(v.raw)
                    ? 'review-chip review-chip-on'
                    : 'review-chip'
                }
                onClick={() => toggleKeepApartPick(v.raw)}
              >
                {v.raw}
              </button>
            ) : v.decidedAs ? (
              <span className="review-value-label muted">
                {v.raw} <span className="tag">→ {v.decidedAs} に登録済み</span>
              </span>
            ) : (
              <label className="review-value-label">
                <input
                  type="checkbox"
                  checked={checked.has(v.raw)}
                  onChange={() => toggleChecked(v.raw)}
                />
                <span>{v.raw}</span>
                {excluded.has(v.raw) && <span className="tag">提案では別グループ</span>}
              </label>
            )}
            <span className="review-value-rows">{v.rows}行</span>
            <span className="review-value-meta muted">
              {v.events.length > 0 && `第${v.events.join('・')}回`}
              {v.djs.length > 0 && ` ${v.djs.join('・')}`}
              {v.crossField && <span className="tag">crossField</span>}
            </span>
          </li>
        ))}
      </ul>
      {!keepApartMode && (
        <p className="review-hint muted">
          ↑ チェックを外すと、その値だけ採用から除ける（次回のキューに単独で出てくる）
          {excluded.size > 0 &&
            '。提案が別グループと判断した値は最初から外してある — 同じものだと思うなら足し直してよい'}
        </p>
      )}
      {keepApartMode && (
        <>
          <p className="review-hint">
            別物として固定する表記を<strong>2つ</strong>クリックしてください。選択中:{' '}
            {keepApartSelected.size > 0 ? [...keepApartSelected].join('・') : 'なし'}
            　もう一度 <kbd>k</kbd> で確定 / <kbd>Esc</kbd> で取消
          </p>
          {keepApartError && <p className="notice notice-error">{keepApartError}</p>}
        </>
      )}

      {!keepApartMode && (
        <>
          <SearchPreview plays={plays} cluster={cluster} checked={checked} />

          <div className="review-proposal">
            <div className="review-field-row">
              <label className="review-field">
                正準名
                <select
                  className="field"
                  value={canonical}
                  onChange={(e) => setCanonical(e.target.value)}
                  disabled={canonicalOptions.length === 0}
                >
                  {canonicalOptions.map((o) => (
                    <option key={o} value={o}>
                      {o}
                    </option>
                  ))}
                </select>
              </label>
              {cluster.field === 'work' && (
                <>
                  <label className="review-field">
                    シリーズ（任意）
                    <input
                      className="field"
                      type="text"
                      value={series}
                      onChange={(e) => setSeries(e.target.value)}
                      placeholder="例: アイカツ!シリーズ"
                    />
                  </label>
                  <label className="review-field">
                    種別
                    <select
                      className="field"
                      value={kind}
                      onChange={(e) => setKind(e.target.value as ProposalKind)}
                    >
                      {KIND_OPTIONS.map((k) => (
                        <option key={k.value} value={k.value}>
                          {k.label}
                        </option>
                      ))}
                    </select>
                  </label>
                </>
              )}
            </div>

            {cluster.proposal && (
              <p className="review-proposal-meta muted">
                提案（confidence: {cluster.proposal.confidence ?? '?'}
                {cluster.proposal.source ? ` / source: ${cluster.proposal.source}` : ''}）
              </p>
            )}

            <label className="review-reason-label">
              理由（必須）
              <textarea
                ref={reasonRef}
                className={
                  reasonWarn ? 'field review-reason review-reason-warn' : 'field review-reason'
                }
                value={reason}
                onChange={(e) => {
                  setReason(e.target.value)
                  setReasonWarn(false)
                }}
                placeholder={
                  cluster.proposal
                    ? undefined
                    : '判断の理由を書いてください（サーバが空文字を弾きます）'
                }
              />
            </label>
            {reasonWarn && <p className="review-reason-warn-text">理由を入力してください</p>}
          </div>
        </>
      )}

      <div className="review-actions">
        <button type="button" className="review-btn" onClick={actions.accept} disabled={checked.size === 0}>
          <kbd>a</kbd> 採用
        </button>
        <button type="button" className="review-btn" onClick={actions.reject}>
          <kbd>r</kbd> 却下
        </button>
        <button type="button" className="review-btn" onClick={actions.defer}>
          <kbd>s</kbd> 保留
        </button>
        <button type="button" className="review-btn" onClick={actions.toggleKeepApart}>
          <kbd>k</kbd> 別物として固定
        </button>
        <button type="button" className="review-btn" onClick={actions.focusReason}>
          <kbd>e</kbd> 理由編集
        </button>
      </div>
    </div>
  )
}
