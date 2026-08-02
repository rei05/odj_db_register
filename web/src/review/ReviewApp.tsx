import { useCallback, useEffect, useRef, useState } from 'react'
import { loadData, type Loaded } from '../lib/data.ts'
import { fetchQueue, postDecide, postExport } from './api.ts'
import BulkReviewList from './BulkReviewList.tsx'
import ClusterCard, { type ClusterActions } from './ClusterCard.tsx'
import type { DecidePayload, Field, QueueResponse } from './types.ts'
import './review.css'

const FIELDS: { id: Field; label: string }[] = [
  { id: 'work', label: '元ネタ（work）' },
  { id: 'artist', label: 'アーティスト（artist）' },
]

type ReviewMode = 'single' | 'bulk'

const MODES: { id: ReviewMode; label: string }[] = [
  { id: 'single', label: '1件ずつ' },
  { id: 'bulk', label: 'まとめて' },
]

/** 説明パネルを閉じたかどうか。**既定は開く**（初めて開いた人に読ませたいので）。 */
const GUIDE_KEY = 'odj-review-guide'

/** 1件ずつ／まとめての、どちらで開いていたか。Guide の GUIDE_KEY と同じ理屈で
 * localStorage に覚える（毎回選び直させるのは邪魔なので）。 */
const MODE_KEY = 'odj-review-mode'

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
          <p>
            <strong>ここに出るのは、人間の判断が要るものだけです。</strong>
            規則で安全と言い切れる候補（提案がクラスタ全体を覆っていて、
            アーティスト名の混入や大きな塊を割った破片といった危険の印が無いもの）は
            候補を作る側で承認済みになっているので、ここには出てきません。
          </p>
          <ol className="review-guide-steps">
            <li>
              <strong>残った候補を見る。</strong>
              提案が別物を巻き込んでいたり、LLM が自信を持てなかったり、
              そもそも提案が無かったりしたものが残っています
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
  const [mode, setMode] = useState<ReviewMode>(() =>
    localStorage.getItem(MODE_KEY) === 'bulk' ? 'bulk' : 'single',
  )
  // まとめてモードの行から「1件ずつ確認」で飛んできたクラスタ。指定が無ければ
  // 従来どおり queue.clusters[0]（先頭）を1件ずつモードのカードに出す。
  const [selectedId, setSelectedId] = useState<string | null>(null)

  useEffect(() => {
    localStorage.setItem(GUIDE_KEY, guideOpen ? 'open' : 'closed')
  }, [guideOpen])

  useEffect(() => {
    localStorage.setItem(MODE_KEY, mode)
  }, [mode])

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

  // selectedId が指すクラスタがまだキューにあればそれを、無ければ（未指定 /
  // 既に判断済みで消えた）先頭を出す。「まとめて」の行から個別に開いたクラスタを
  // 見終えたら selectedId をクリアするので、そこでも自然に先頭へ戻る。
  const cluster =
    queue && queue.field === field
      ? (selectedId ? queue.clusters.find((c) => c.id === selectedId) : undefined) ??
        queue.clusters[0] ??
        null
      : null

  const actionsRef = useRef<ClusterActions | null>(null)
  const registerActions = useCallback((a: ClusterActions | null) => {
    actionsRef.current = a
  }, [])

  // 「まとめて」で1件承認するたびに呼ぶ。1件ずつモードの submit と同じ形で
  // キューを縮め、残数・判断済み件数の表示を合わせる。
  const markDecided = useCallback((id: string) => {
    setQueue((q) =>
      q ? { ...q, clusters: q.clusters.filter((c) => c.id !== id), decided: q.decided + 1 } : q,
    )
  }, [])

  // 「まとめて」の行の「1件ずつ確認」から。1件ずつモードへ切り替えて
  // そのクラスタをカードに出す。
  const openSingle = useCallback((id: string) => {
    setSelectedId(id)
    setMode('single')
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
            // 「まとめて」から個別に開いたクラスタだった場合、判断が付いたので
            // 指名を解除する（次に出すのは通常どおり先頭のクラスタ）。
            setSelectedId((id) => (id === payload.id ? null : id))
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
  const autoApproved = queue?.autoApproved ?? 0

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
              onClick={() => {
                setField(f.id)
                // 別フィールドの id は「まとめて」から指名しても見つからない
                // （find が undefined を返して先頭にフォールバックするだけ）が、
                // 切り替えたのに古い指名が残っているとまぎらわしいので消す。
                setSelectedId(null)
              }}
            >
              {f.label}
            </button>
          ))}
        </nav>
        <nav className="review-mode-toggle" aria-label="表示モード">
          {MODES.map((m) => (
            <button
              key={m.id}
              type="button"
              className={m.id === mode ? 'tab tab-on' : 'tab'}
              aria-current={m.id === mode ? 'page' : undefined}
              onClick={() => setMode(m.id)}
            >
              {m.label}
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
              / 全 {total.toLocaleString()} 件（判断済み {queue.decided.toLocaleString()} 件
              {/* 自動承認は「見なくてよかったぶん」。黙って総数から引くと、
                  候補が減ったのか画面が壊れているのか利用者に区別が付かない。 */}
              {autoApproved > 0 && `。うち自動承認 ${autoApproved.toLocaleString()} 件`}）
            </span>
            {loading && <span className="muted">（更新中…）</span>}
          </p>
        )}
        {!queue && loading && <p className="notice">読み込み中…</p>}

        {mode === 'single' && data && cluster && (
          <ClusterCard
            key={cluster.id}
            cluster={cluster}
            plays={data.plays}
            onSubmit={submit}
            registerActions={registerActions}
          />
        )}
        {mode === 'single' && !data && !error && (
          <p className="notice">plays.json を読み込み中…</p>
        )}

        {mode === 'bulk' && queue && queue.field === field && (
          // key={field} で、フィールドを切り替えるたびに一覧を作り直す
          // （ClusterCard が key={cluster.id} で1件ごとに作り直すのと同じ理屈）。
          // 承認が進んでも queue 側の再レンダリングで一覧の入力途中の状態
          // （チェック・編集した正準名）を消したくないので、この key は field
          // だけに絞ってあり、承認のたびには変えない。
          <BulkReviewList
            key={field}
            field={field}
            clusters={queue.clusters}
            onDecided={markDecided}
            onOpenSingle={openSingle}
          />
        )}

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
