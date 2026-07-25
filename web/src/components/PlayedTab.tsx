import { useMemo, useState } from 'react'
import type { Loaded } from '../lib/data.ts'
import { checkPlayed } from '../lib/stats.ts'
import type { Play } from '../lib/types.ts'

const SAMPLE = '硝子ドール\n残酷な天使のテーゼ\nふわふわ時間'

function History({ plays }: { plays: Play[] }) {
  return (
    <ul className="history">
      {plays.map((p, i) => (
        <li key={i}>
          第{p.eventNo}回（{p.eventDate}）・{p.dj}
          {p.trackNo ? ` ${p.trackNo}曲目` : ''} — {p.title}
        </li>
      ))}
    </ul>
  )
}

export default function PlayedTab({ data }: { data: Loaded }) {
  const [text, setText] = useState('')

  const queries = useMemo(
    () =>
      text
        .split('\n')
        .map((l) => l.trim())
        .filter(Boolean),
    [text],
  )
  const results = useMemo(
    () => checkPlayed(data.plays, queries),
    [data.plays, queries],
  )

  return (
    <section>
      <div className="card">
        <h2>かけたい曲が既出か調べる</h2>
        <p className="result-count">
          1行に1曲ずつ貼り付けてください。表記ゆれ（全角半角・記号・スペース）は
          吸収します。リミックス表記を外した原曲名でも照合するので、
          「別のリミックスなら過去にかかっている」ケースも拾えます。
        </p>
        <textarea
          className="field"
          value={text}
          placeholder={SAMPLE}
          onChange={(e) => setText(e.target.value)}
          aria-label="曲名リスト"
        />
      </div>

      {results.length > 0 && (
        <div className="card">
          {results.map((r, i) => {
            const isNew = r.exact.length === 0 && r.sameBase.length === 0
            return (
              <div className="played-item" key={i}>
                <p className="played-query">{r.query}</p>
                {isNew && (
                  <p className="played-verdict played-new">
                    未プレイ — この曲がかかった記録はありません
                  </p>
                )}
                {r.exact.length > 0 && (
                  <>
                    <p className="played-verdict">
                      既出 — 同じ曲名で {r.exact.length} 回
                    </p>
                    <History plays={r.exact} />
                  </>
                )}
                {r.sameBase.length > 0 && (
                  <div className={r.exact.length > 0 ? 'history-sub' : undefined}>
                    <p className="played-verdict">
                      原曲一致（別リミックス）— {r.sameBase.length} 回
                    </p>
                    <History plays={r.sameBase} />
                  </div>
                )}
              </div>
            )
          })}
        </div>
      )}
    </section>
  )
}
