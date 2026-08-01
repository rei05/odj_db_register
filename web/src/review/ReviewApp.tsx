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

/** 説明パネルを閉じたかどうか。**既定は開く**（初めて開いた人に読ませたいので）。 */
const GUIDE_KEY = 'odj-review-guide'

/**
 * この画面が何をするところなのかの説明。
 *
 * レビューは1件ずつ手で捌く作業で、**何を訊かれているのかが分からないと最初の
 * 1件で手が止まる**。折りたためるようにして、閉じた状態は localStorage に覚える
 * （毎回同じ説明を読まされるのは邪魔なので）。
 */
function Guide({ open, onToggle }: { open: boolean; onToggle: () => void }) {
  return (
    <section className="review-guide">
      <button type="button" className="review-guide-toggle" onClick={onToggle}>
        {open ? '▾' : '▸'} この画面ですること
      </button>
      {open && (
        <div className="review-guide-body">
          <p>
            表記ゆれ（同じ作品なのに「アイカツ!」「アイカツ」「アイカツ! 楽曲」と
            書かれている、など）を1つにまとめる作業です。まとめると
            <strong>検索でどれを打っても同じ曲が見つかる</strong>ようになります。
          </p>
          <ol className="review-guide-steps">
            <li>
              <strong>候補を1件ずつ見る。</strong>
              機械と LLM が「同じかもしれない表記」を集めてあります。
              提案がある候補は、あらかじめ答えが埋まっています
            </li>
            <li>
              <strong>同じものにだけチェックを残す。</strong>
              1つの候補に別物が混ざっていることは普通にあります
            </li>
            <li>
              <strong>まとめた後の名前と理由を決めて、採用する。</strong>
              判断に迷ったら保留（<kbd>s</kbd>）で構いません
            </li>
            <li>
              全部片付いたら、最後に<strong>書き出し</strong>（画面下）。
              ここまでやって初めて閲覧画面の検索に反映されます
            </li>
          </ol>
          <p className="muted">
            まとめるほうが取り消しにくいので、<strong>迷ったら分ける</strong>のが安全です。
            承認したものだけが公開データに入ります。判断は
            <code>data/aliases/</code> に記録され、あとから直せます。
          </p>
        </div>
      )}
    </section>
  )
}

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
  const [guideOpen, setGuideOpen] = useState(
    () => localStorage.getItem(GUIDE_KEY) !== 'closed',
  )

  useEffect(() => {
    localStorage.setItem(GUIDE_KEY, guideOpen ? 'open' : 'closed')
  }, [guideOpen])

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
        // 分岐は HTTP ステータスではなく body.code で行う（種別のほうが確か）。
        .then(({ body }) => {
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
          // 種別で分ける。以前は status 409 をすべて「二重送信」と見なして
          // キューを取り直していたが、keep_apart 違反も 409 だったため、
          // キーを押しても同じカードが出続けて何も起きないように見えていた。
          if (body.code === 'already-decided') {
            // 他のセッションで先に判断された等。取り直せば整合する。
            setError(`${body.error}（キューを最新化します）`)
            refresh(field)
            return
          }
          if (body.code === 'keep-apart') {
            // 統合してはいけない組が入っている。このカードで直せるので、
            // 何をすればよいかまで書く。
            setError(
              `${body.error} — チェックを外して分けるか、[k] で別物として固定してください`,
            )
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

        <Guide open={guideOpen} onToggle={() => setGuideOpen((v) => !v)} />

        {queue && queue.field === field && (
          <p className="review-progress">
            残り <strong>{remaining.toLocaleString()}</strong> 件
            <span className="muted">
              {' '}
              / 全 {total.toLocaleString()} 件（判断済み {queue.decided.toLocaleString()} 件）
            </span>
            {loading && <span className="muted">（更新中…）</span>}
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
          <div className="card review-done">
            <p>
              <strong>このフィールドは全件レビュー済みです。</strong>
            </p>
            <p className="muted">
              下の「書き出す」を押すと、承認したぶんが検索に反映されます。
              もう一方のフィールドが残っていれば、上のタブから切り替えてください。
            </p>
          </div>
        )}

        <div className="review-footer">
          <h2 className="review-footer-title">最後にすること</h2>
          <p className="review-footer-note muted">
            承認した判断を <code>web/public/data/aliases.json</code> にまとめます。
            <strong>これを実行するまで閲覧画面の検索は変わりません。</strong>
            何度押しても構いません（毎回まとめ直すだけです）。
          </p>
          <button
            type="button"
            className="review-btn review-btn-primary review-export"
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
            {exporting ? '書き出し中…' : '承認済みを書き出す'}
          </button>
        </div>
      </main>
    </div>
  )
}
