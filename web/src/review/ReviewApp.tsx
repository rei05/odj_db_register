import { useCallback, useEffect, useRef, useState } from 'react'
import { loadData, type Loaded } from '../lib/data.ts'
import { fetchQueue, postDecide, postExport } from './api.ts'
import ClusterCard, { type ClusterActions } from './ClusterCard.tsx'
import type { DecidePayload, Field, QueueResponse } from './types.ts'
import './review.css'

const FIELDS: { id: Field; label: string }[] = [
  { id: 'work', label: '元ネタ（work）' },
  { id: 'artist', label: 'アーティスト（artist）' },
]

/**
 * 表記ゆれ辞書レビュー画面。
 *
 * 約250件を1件ずつ捌くので速度が最優先。カード内の状態（チェック・正準名・理由…）は
 * ClusterCard が持ち、キーボード操作([a][r][s][k][e])はここで window に1つだけ
 * 貼ったハンドラから actionsRef 経由で ClusterCard に届ける。カードごとに状態を
 * lift せずに済むぶん ClusterCard 側の実装が素直になる。
 */
export default function ReviewApp() {
  const [field, setField] = useState<Field>('work')
  const [queue, setQueue] = useState<QueueResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [data, setData] = useState<Loaded | null>(null)
  const [exporting, setExporting] = useState(false)

  useEffect(() => {
    loadData().then(setData, (e: unknown) =>
      setError(e instanceof Error ? e.message : String(e)),
    )
  }, [])

  const refresh = useCallback((f: Field) => {
    setLoading(true)
    setError(null)
    fetchQueue(f)
      .then((res) => setQueue(res))
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => {
    refresh(field)
  }, [field, refresh])

  const cluster =
    queue && queue.clusters.length > 0 && queue.field === field ? queue.clusters[0] : null

  const actionsRef = useRef<ClusterActions | null>(null)
  const registerActions = useCallback((a: ClusterActions | null) => {
    actionsRef.current = a
  }, [])

  const submit = useCallback(
    (payload: DecidePayload) => {
      setNotice(null)
      setError(null)
      postDecide(payload)
        .then(({ status, body }) => {
          if (body.ok) {
            setNotice(`保存しました（${payload.action}）: ${body.wrote.join(', ')}`)
            // 判断済みの1件をキューから外す。件数の帳尻も合わせる。
            setQueue((q) =>
              q
                ? {
                    ...q,
                    clusters: q.clusters.filter((c) => c.id !== payload.id),
                    decided: payload.action === 'defer' ? q.decided : q.decided + 1,
                  }
                : q,
            )
            return
          }
          if (status === 409) {
            // 既に判断済み（二重送信、あるいは他のセッションで先に判断された）。
            // サーバが弾く契約なので、こちらはキューを取り直して整合させるだけでよい。
            setError(`${body.error}（キューを最新化します）`)
            refresh(field)
            return
          }
          setError(body.error)
        })
        .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)))
    },
    [field, refresh],
  )

  // キーボード操作。テキスト入力中([e]で理由欄にフォーカスした後など)は奪わない。
  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      const target = e.target as HTMLElement | null
      const typing =
        !!target && ['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName)
      if (e.key === 'Escape') {
        if (typing) target?.blur()
        else actionsRef.current?.cancelKeepApart()
        return
      }
      if (typing) return
      const actions = actionsRef.current
      if (!actions) return
      switch (e.key) {
        case 'a':
          actions.accept()
          break
        case 'r':
          actions.reject()
          break
        case 's':
          actions.defer()
          break
        case 'k':
          actions.toggleKeepApart()
          break
        case 'e':
          e.preventDefault()
          actions.focusReason()
          break
        default:
          return
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [])

  const remaining = queue?.clusters.length ?? 0
  const total = queue?.total ?? 0

  return (
    <div className="review-app">
      <header className="review-header">
        <div className="review-title">
          <h1>表記ゆれレビュー</h1>
          {data && (
            <p className="review-sub muted">
              plays.json {data.plays.length.toLocaleString()} 行に対する統合候補（
              {data.generatedAt} 時点のデータ）
            </p>
          )}
        </div>
        <nav className="review-field-toggle" aria-label="対象フィールド">
          {FIELDS.map((f) => (
            <button
              key={f.id}
              type="button"
              className={f.id === field ? 'tab tab-on' : 'tab'}
              aria-current={f.id === field ? 'page' : undefined}
              onClick={() => setField(f.id)}
            >
              {f.label}
            </button>
          ))}
        </nav>
      </header>

      <main className="review-main">
        {error && <p className="notice notice-error">{error}</p>}
        {notice && <p className="notice review-notice-ok">{notice}</p>}

        {queue && queue.field === field && (
          <p className="review-progress">
            残り {remaining.toLocaleString()} / {total.toLocaleString()}
            {loading && '（更新中…）'}
          </p>
        )}
        {!queue && loading && <p className="notice">読み込み中…</p>}

        {data && cluster && (
          <ClusterCard
            key={cluster.id}
            cluster={cluster}
            plays={data.plays}
            onSubmit={submit}
            registerActions={registerActions}
          />
        )}
        {!data && !error && <p className="notice">plays.json を読み込み中…</p>}

        {queue && queue.field === field && queue.clusters.length === 0 && !loading && (
          <div className="card">
            <p>このフィールドは全件レビュー済みです。</p>
          </div>
        )}

        <div className="review-footer">
          <button
            type="button"
            className="link-button"
            disabled={exporting}
            onClick={() => {
              setExporting(true)
              setError(null)
              postExport()
                .then(({ body }) => {
                  if (body.ok) {
                    setNotice(
                      `辞書を書き出しました: works ${body.works} / artists ${body.artists}`,
                    )
                  } else {
                    setError(body.error)
                  }
                })
                .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)))
                .finally(() => setExporting(false))
            }}
          >
            {exporting ? '書き出し中…' : '承認済みを web/public/data/aliases.json に書き出す'}
          </button>
        </div>
      </main>
    </div>
  )
}
