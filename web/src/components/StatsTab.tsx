import { useMemo, useState } from 'react'
import type { Loaded } from '../lib/data.ts'
import {
  perDj,
  perEvent,
  topArtists,
  topTitles,
  topWorks,
} from '../lib/stats.ts'
import BarList from './BarList.tsx'
import EventChart from './EventChart.tsx'

export default function StatsTab({ data }: { data: Loaded }) {
  const [event, setEvent] = useState('')

  const scoped = useMemo(() => {
    if (!event) return data.plays
    const no = Number(event)
    return data.plays.filter((p) => p.eventNo === no)
  }, [data.plays, event])

  const titles = useMemo(() => topTitles(scoped), [scoped])
  const works = useMemo(() => topWorks(scoped), [scoped])
  const artists = useMemo(() => topArtists(scoped), [scoped])
  const djs = useMemo(() => perDj(scoped), [scoped])
  const events = useMemo(() => perEvent(data.plays), [data.plays])

  return (
    <section>
      <div className="filters">
        <label>
          対象
          <select
            className="field"
            value={event}
            onChange={(e) => setEvent(e.target.value)}
          >
            <option value="">全開催回</option>
            {data.events.map((ev) => (
              <option key={ev.no} value={ev.no}>
                第{ev.no}回のみ
              </option>
            ))}
          </select>
        </label>
        <span className="result-count" style={{ margin: 0 }}>
          {scoped.length.toLocaleString()} プレイを集計
        </span>
      </div>

      <div className="card">
        <EventChart events={events} />
        <p className="result-count" style={{ margin: '18px 0 0' }}>
          「初出」はその回で初めてかかった曲。リミックス表記を外した原曲名で
          判定しているので、別アレンジでの再登場は既出として数えています。
          （このグラフは絞り込みの影響を受けません）
        </p>
      </div>

      <div className="card-grid">
        <div className="card">
          <h3>よくかかる曲</h3>
          <BarList items={titles} />
        </div>
        <div className="card">
          <h3>よくかかる作品・元ネタ</h3>
          <BarList items={works} />
        </div>
        <div className="card">
          <h3>よくかかるアーティスト</h3>
          <BarList items={artists} />
        </div>
        <div className="card">
          <h3>DJ 別</h3>
          <div className="table-wrap" style={{ border: 'none' }}>
            <table>
              <thead>
                <tr>
                  <th>DJ</th>
                  <th className="num">曲数</th>
                  <th className="num">出演</th>
                  <th className="num">REMIX率</th>
                </tr>
              </thead>
              <tbody>
                {djs.map((d) => (
                  <tr key={d.dj}>
                    <td className="nowrap">{d.dj}</td>
                    <td className="num">{d.tracks}</td>
                    <td className="num">{d.events}</td>
                    <td className="num">
                      {d.remixRate === null ? (
                        <span className="muted">—</span>
                      ) : (
                        `${Math.round(d.remixRate * 100)}%`
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </section>
  )
}
