import { useMemo, useState } from 'react'
import type { Loaded } from '../lib/data.ts'
import { matchesQuery } from '../lib/normalize.ts'

const LIMIT = 300

export default function SearchTab({ data }: { data: Loaded }) {
  const [query, setQuery] = useState('')
  const [event, setEvent] = useState('')
  const [dj, setDj] = useState('')
  const [remix, setRemix] = useState('')

  const hits = useMemo(() => {
    const eventNo = event ? Number(event) : null
    return data.plays.filter((p) => {
      if (eventNo !== null && p.eventNo !== eventNo) return false
      if (dj && p.dj !== dj) return false
      if (remix === 'yes' && p.isRemix !== true) return false
      if (remix === 'no' && p.isRemix !== false) return false
      return matchesQuery(p.haystack, query)
    })
  }, [data.plays, query, event, dj, remix])

  return (
    <section>
      <div className="filters">
        <input
          className="field field-grow"
          type="search"
          placeholder="曲名・アーティスト・アニメ / 元ネタ で検索"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          aria-label="キーワード検索"
        />
        <label>
          開催回
          <select
            className="field"
            value={event}
            onChange={(e) => setEvent(e.target.value)}
          >
            <option value="">すべて</option>
            {data.events.map((ev) => (
              <option key={ev.no} value={ev.no}>
                第{ev.no}回（{ev.date}）
              </option>
            ))}
          </select>
        </label>
        <label>
          DJ
          <select
            className="field"
            value={dj}
            onChange={(e) => setDj(e.target.value)}
          >
            <option value="">すべて</option>
            {data.djs.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
        </label>
        <label>
          REMIX
          <select
            className="field"
            value={remix}
            onChange={(e) => setRemix(e.target.value)}
          >
            <option value="">すべて</option>
            <option value="yes">リミックスのみ</option>
            <option value="no">原曲のみ</option>
          </select>
        </label>
      </div>

      <p className="result-count">
        {hits.length.toLocaleString()} 件
        {hits.length > LIMIT && `（先頭 ${LIMIT} 件を表示）`}
      </p>

      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th className="num">回</th>
              <th>DJ</th>
              <th className="num">曲順</th>
              <th>タイトル</th>
              <th>アニメ / 元ネタ</th>
              <th>アーティスト</th>
              <th>REMIX</th>
              <th>音源</th>
            </tr>
          </thead>
          <tbody>
            {hits.slice(0, LIMIT).map((p, i) => (
              <tr key={`${p.eventNo}-${p.dj}-${p.trackNo}-${i}`}>
                <td className="num">{p.eventNo}</td>
                <td className="nowrap">{p.dj}</td>
                <td className="num">{p.trackNo ?? ''}</td>
                <td>{p.title}</td>
                <td className={p.work ? undefined : 'muted'}>{p.work ?? '—'}</td>
                <td className={p.artist ? undefined : 'muted'}>
                  {p.artist ?? '—'}
                </td>
                <td className="nowrap">
                  {p.isRemix === null ? (
                    <span className="muted">—</span>
                  ) : p.isRemix ? (
                    <span className="tag">REMIX</span>
                  ) : (
                    <span className="muted">原曲</span>
                  )}
                </td>
                <td>
                  {p.url ? (
                    <a href={p.url} target="_blank" rel="noreferrer">
                      開く
                    </a>
                  ) : (
                    <span className="muted">—</span>
                  )}
                </td>
              </tr>
            ))}
            {hits.length === 0 && (
              <tr>
                <td colSpan={8} className="muted">
                  該当なし
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </section>
  )
}
