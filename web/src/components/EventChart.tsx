import { useId, useState } from 'react'
import type { EventSummary } from '../lib/stats.ts'
import './EventChart.css'

/**
 * 開催回ごとの曲数を、その回で初出だった曲と既出だった曲に分けた積み上げ棒。
 * 新規曲は総曲数の内訳なので、並べるのではなく積む。
 */
export default function EventChart({ events }: { events: EventSummary[] }) {
  const [hover, setHover] = useState<number | null>(null)
  const titleId = useId()
  const max = Math.max(1, ...events.map((e) => e.tracks))

  return (
    <figure className="evchart" aria-labelledby={titleId}>
      <figcaption id={titleId} className="evchart-title">
        開催回ごとの曲数
        <span className="evchart-legend">
          <span className="evchart-key">
            <i className="evchart-swatch evchart-swatch-new" />
            初出
          </span>
          <span className="evchart-key">
            <i className="evchart-swatch evchart-swatch-old" />
            既出
          </span>
        </span>
      </figcaption>

      <div className="evchart-plot">
        {events.map((ev) => {
          const repeat = ev.tracks - ev.newTitles
          const on = hover === ev.eventNo
          return (
            <div
              key={ev.eventNo}
              className={on ? 'evchart-col evchart-col-on' : 'evchart-col'}
              onMouseEnter={() => setHover(ev.eventNo)}
              onMouseLeave={() => setHover(null)}
              onFocus={() => setHover(ev.eventNo)}
              onBlur={() => setHover(null)}
              tabIndex={0}
              role="img"
              aria-label={`第${ev.eventNo}回 ${ev.date} 合計${ev.tracks}曲 うち初出${ev.newTitles}曲 ${ev.djs}DJ`}
            >
              {on && (
                <div className="evchart-tip">
                  <strong>第{ev.eventNo}回</strong>
                  <span>{ev.date}</span>
                  <span>{ev.djs} DJ</span>
                  <span>合計 {ev.tracks} 曲</span>
                  <span>初出 {ev.newTitles} 曲</span>
                </div>
              )}
              <div className="evchart-stack">
                <div
                  className="evchart-seg evchart-seg-old"
                  style={{ height: `${(repeat / max) * 100}%` }}
                />
                <div
                  className="evchart-seg evchart-seg-new"
                  style={{ height: `${(ev.newTitles / max) * 100}%` }}
                />
              </div>
              <span className="evchart-tick">{ev.eventNo}</span>
            </div>
          )
        })}
      </div>
      <p className="evchart-axis">開催回</p>
    </figure>
  )
}
