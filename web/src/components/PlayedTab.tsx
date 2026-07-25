import { useMemo, useState } from 'react'
import type { Loaded } from '../lib/data.ts'
import { checkPlayed } from '../lib/stats.ts'
import type { Play } from '../lib/types.ts'

const SAMPLE = '硝子ドール\n残酷な天使のテーゼ\nふわふわ時間'

const SHOW_AT_FIRST = 8

function History({ plays }: { plays: Play[] }) {
  const [all, setAll] = useState(false)
  const shown = all ? plays : plays.slice(0, SHOW_AT_FIRST)
  return (
    <>
      <ul className="history">
        {shown.map((p, i) => (
          <li key={i}>
            第{p.eventNo}回（{p.eventDate}）・{p.dj}
            {p.trackNo ? ` ${p.trackNo}曲目` : ''} — {p.title}
          </li>
        ))}
      </ul>
      {plays.length > SHOW_AT_FIRST && (
        <button
          type="button"
          className="link-button"
          onClick={() => setAll((v) => !v)}
        >
          {all ? '折りたたむ' : `残り ${plays.length - SHOW_AT_FIRST} 件を表示`}
        </button>
      )}
    </>
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
          吸収します。確かさの高い順に、曲名がそのまま一致する
          <strong>完全一致</strong>、リミックス表記を外すと一致する
          <strong>原曲一致</strong>、どちらかがもう一方を含む
          <strong>部分一致</strong>の3段階で出します。
          「硝子」のような一部だけでも構いません（2文字以上、漢字なら1文字でも）。
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
            const isNew =
              r.exact.length === 0 &&
              r.sameBase.length === 0 &&
              r.partial.length === 0
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
                {r.partial.length > 0 && (
                  <div
                    className={
                      r.exact.length > 0 || r.sameBase.length > 0
                        ? 'history-sub'
                        : undefined
                    }
                  >
                    <p className="played-verdict">
                      部分一致 — {r.partial.length} 回
                      <span className="muted">（別の曲かもしれません）</span>
                    </p>
                    <History plays={r.partial} />
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
