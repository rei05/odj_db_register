import { baseKey, normKey } from './normalize.ts'
import type { Dataset, Play, RawEvent } from './types.ts'

export interface Loaded {
  generatedAt: string
  events: RawEvent[]
  plays: Play[]
  djs: string[]
}

export async function loadData(): Promise<Loaded> {
  const res = await fetch(`${import.meta.env.BASE_URL}data/plays.json`)
  if (!res.ok) {
    throw new Error(
      `plays.json を読み込めませんでした (${res.status})。` +
        'リポジトリのルートで `uv run python -m odj.build` を実行してください。',
    )
  }
  const raw: Dataset = await res.json()
  const dateByEvent = new Map(raw.events.map((e) => [e.no, e.date]))

  const plays: Play[] = raw.plays.map((p) => ({
    eventNo: p.e,
    eventDate: dateByEvent.get(p.e) ?? '',
    playOrder: p.p,
    dj: p.dj,
    trackNo: p.n,
    title: p.t,
    work: p.w,
    artist: p.a,
    isRemix: p.r,
    url: p.u,
    sourceKind: p.k,
    key: normKey(p.t),
    base: baseKey(p.t),
    haystack: normKey([p.t, p.w ?? '', p.a ?? ''].join(' ')),
  }))

  const djs = [...new Set(plays.map((p) => p.dj))].sort((a, b) =>
    a.localeCompare(b, 'ja'),
  )
  return { generatedAt: raw.generatedAt, events: raw.events, plays, djs }
}

/** 開催回ごと、play順で DJ を並べる。 */
export function setlistsForEvent(plays: Play[], eventNo: number) {
  const byDj = new Map<string, Play[]>()
  for (const p of plays) {
    if (p.eventNo !== eventNo) continue
    const list = byDj.get(p.dj)
    if (list) list.push(p)
    else byDj.set(p.dj, [p])
  }
  return [...byDj.entries()]
    .map(([dj, tracks]) => ({
      dj,
      playOrder: tracks[0].playOrder,
      tracks: [...tracks].sort((a, b) => (a.trackNo ?? 0) - (b.trackNo ?? 0)),
    }))
    .sort((a, b) => (a.playOrder ?? 99) - (b.playOrder ?? 99))
}
