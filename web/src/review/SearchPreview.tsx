import { useMemo, useState } from 'react'
import { matchesQuery } from '../lib/normalize.ts'
import type { Play } from '../lib/types.ts'
import type { Cluster } from './types.ts'

const SHOW_AT_FIRST = 8

/**
 * この統合を採用すると検索で何が新たに見つかるようになるかを実データで見せる。
 *
 * ランキングの見え方ではなく検索のヒット差分を見せるのが目的（背景に書かれている
 * とおり「過去にプレイされた曲を確実に見つけ出す」ことが GUI 全体の目的なので）。
 *
 * 「統合前」= チェックが入っている中で一番行数の多い表記だけで検索したときのヒット。
 * 「統合後」= チェックが入っている表記すべてを同義語として扱い、それぞれで検索した
 * ヒットの和集合。両者の差分が「この統合で新たに引っかかるようになる行」。
 *
 * matchesQuery は部分一致なので、素の表記同士が互いを含む関係（「アイカツ」⊂
 * 「アイカツ!」）だと差分は出ない。差分が効くのは略称・言い換えなど互いを含まない
 * 表記同士を束ねたとき — まさにこのレビューで拾いたいケース。
 */
export default function SearchPreview({
  plays,
  cluster,
  checked,
}: {
  plays: Play[]
  cluster: Cluster
  checked: Set<string>
}) {
  const [showAll, setShowAll] = useState(false)

  const checkedValues = useMemo(
    () => cluster.values.filter((v) => checked.has(v.raw)),
    [cluster, checked],
  )

  const representative = useMemo(() => {
    if (checkedValues.length === 0) return null
    return [...checkedValues].sort((a, b) => b.rows - a.rows)[0].raw
  }, [checkedValues])

  const { beforeCount, diff } = useMemo(() => {
    if (!representative) return { beforeCount: 0, diff: [] as Play[] }
    const before = plays.filter((p) => matchesQuery(p.haystack, representative))
    const beforeSet = new Set(before)
    const after = new Set(before)
    for (const v of checkedValues) {
      if (v.raw === representative) continue
      for (const p of plays) {
        if (matchesQuery(p.haystack, v.raw)) after.add(p)
      }
    }
    const newlyFound = [...after]
      .filter((p) => !beforeSet.has(p))
      .sort((a, b) => a.eventNo - b.eventNo || (a.trackNo ?? 0) - (b.trackNo ?? 0))
    return { beforeCount: before.length, diff: newlyFound }
  }, [plays, checkedValues, representative])

  if (!representative) {
    return (
      <p className="review-preview-empty muted">
        採用する表記を1つ以上選ぶとプレビューが出ます。
      </p>
    )
  }

  const afterCount = beforeCount + diff.length
  const shown = showAll ? diff : diff.slice(0, SHOW_AT_FIRST)

  return (
    <div className="review-preview">
      <p className="review-preview-summary">
        「{representative}」で検索: {beforeCount.toLocaleString()}件 → {afterCount.toLocaleString()}件
        {diff.length > 0 && (
          <span className="review-preview-delta">
            {' '}
            （+{diff.length.toLocaleString()}件）
          </span>
        )}
      </p>
      {diff.length === 0 && (
        <p className="review-preview-empty muted">
          差分なし（互いが互いの部分文字列になっているなど、既に検索で拾えている表記どうしです）
        </p>
      )}
      {diff.length > 0 && (
        <>
          <ul className="review-preview-list">
            {shown.map((p, i) => (
              <li key={`${p.eventNo}-${p.dj}-${p.trackNo}-${i}`}>
                + 第{p.eventNo}回 {p.dj} {p.title}
              </li>
            ))}
          </ul>
          {diff.length > SHOW_AT_FIRST && (
            <button
              type="button"
              className="link-button"
              onClick={() => setShowAll((v) => !v)}
            >
              {showAll ? '折りたたむ' : `残り ${diff.length - SHOW_AT_FIRST} 件を表示`}
            </button>
          )}
        </>
      )}
    </div>
  )
}
