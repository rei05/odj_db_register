import { useEffect, useMemo, useState } from 'react'
import './App.css'
import PlayedTab from './components/PlayedTab.tsx'
import SearchTab from './components/SearchTab.tsx'
import SetlistTab from './components/SetlistTab.tsx'
import StatsTab from './components/StatsTab.tsx'
import { loadData, type Loaded } from './lib/data.ts'

const TABS = [
  { id: 'search', label: '検索' },
  { id: 'played', label: '既出判定' },
  { id: 'stats', label: '集計' },
  { id: 'setlist', label: 'セトリ' },
] as const

type TabId = (typeof TABS)[number]['id']

export default function App() {
  const [data, setData] = useState<Loaded | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [tab, setTab] = useState<TabId>('search')

  useEffect(() => {
    loadData().then(setData, (e: unknown) =>
      setError(e instanceof Error ? e.message : String(e)),
    )
  }, [])

  const summary = useMemo(() => {
    if (!data) return null
    return {
      plays: data.plays.length,
      events: data.events.length,
      djs: data.djs.length,
    }
  }, [data])

  return (
    <div className="app">
      <header className="app-header">
        <div className="app-title">
          <h1>オタクDJ大会 セトリDB</h1>
          {summary && (
            <p className="app-sub">
              第1〜{data!.events.at(-1)!.no}回 / {summary.events} 公演 /{' '}
              {summary.djs} DJ / {summary.plays.toLocaleString()} プレイ
              <span className="app-stamp">（{data!.generatedAt} 時点）</span>
            </p>
          )}
        </div>
        <nav className="tabs" aria-label="表示切り替え">
          {TABS.map((t) => (
            <button
              key={t.id}
              type="button"
              className={t.id === tab ? 'tab tab-on' : 'tab'}
              aria-current={t.id === tab ? 'page' : undefined}
              onClick={() => setTab(t.id)}
            >
              {t.label}
            </button>
          ))}
        </nav>
      </header>

      <main className="app-main">
        {error && <p className="notice notice-error">{error}</p>}
        {!error && !data && <p className="notice">読み込み中…</p>}
        {data && tab === 'search' && <SearchTab data={data} />}
        {data && tab === 'played' && <PlayedTab data={data} />}
        {data && tab === 'stats' && <StatsTab data={data} />}
        {data && tab === 'setlist' && <SetlistTab data={data} />}
      </main>
    </div>
  )
}
