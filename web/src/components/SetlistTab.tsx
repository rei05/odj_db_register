import { useMemo, useState } from 'react'
import { setlistsForEvent, type Loaded } from '../lib/data.ts'

export default function SetlistTab({ data }: { data: Loaded }) {
  const latest = data.events.at(-1)!.no
  const [eventNo, setEventNo] = useState(latest)

  const event = data.events.find((e) => e.no === eventNo)!
  const sets = useMemo(
    () => setlistsForEvent(data.plays, eventNo),
    [data.plays, eventNo],
  )
  // フォルダはあるがセトリが残っていない DJ も見えるようにする
  const noSetlist = event.djs.filter((d) => !sets.some((s) => s.dj === d))

  return (
    <section>
      <div className="filters">
        <label>
          開催回
          <select
            className="field"
            value={eventNo}
            onChange={(e) => setEventNo(Number(e.target.value))}
          >
            {data.events.map((ev) => (
              <option key={ev.no} value={ev.no}>
                第{ev.no}回（{ev.date}）
              </option>
            ))}
          </select>
        </label>
        <span className="result-count" style={{ margin: 0 }}>
          {sets.length} DJ / {sets.reduce((n, s) => n + s.tracks.length, 0)} 曲
        </span>
      </div>

      <div className="card-grid">
        {sets.map((set) => (
          <div className="card" key={set.dj}>
            <h3 className="setlist-dj">
              {set.playOrder !== null && (
                <span className="setlist-order">
                  {String(set.playOrder).padStart(2, '0')}
                </span>
              )}
              {set.dj}
              <span className="setlist-count">{set.tracks.length}曲</span>
            </h3>
            <ol className="setlist-tracks">
              {set.tracks.map((t, i) => (
                <li key={i}>
                  <span className="setlist-no">{t.trackNo ?? i + 1}</span>
                  <span>
                    {t.url ? (
                      <a href={t.url} target="_blank" rel="noreferrer">
                        {t.title}
                      </a>
                    ) : (
                      t.title
                    )}
                    {t.work && (
                      <>
                        <br />
                        <span className="setlist-work">{t.work}</span>
                      </>
                    )}
                  </span>
                </li>
              ))}
            </ol>
          </div>
        ))}
      </div>

      {noSetlist.length > 0 && (
        <p className="empty-set" style={{ marginTop: 16 }}>
          セトリ未登録: {noSetlist.join('、')}
        </p>
      )}
    </section>
  )
}
