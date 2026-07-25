import { baseKey, normKey } from './normalize.ts'
import type { Play } from './types.ts'

export interface Ranked {
  label: string
  count: number
  detail?: string
}

function rank(
  plays: Play[],
  keyOf: (p: Play) => string | null,
  labelOf: (p: Play) => string,
  limit: number,
): Ranked[] {
  const buckets = new Map<string, { label: string; plays: Play[] }>()
  for (const p of plays) {
    const key = keyOf(p)
    if (!key) continue
    const hit = buckets.get(key)
    if (hit) hit.plays.push(p)
    else buckets.set(key, { label: labelOf(p), plays: [p] })
  }
  return [...buckets.values()]
    .map(({ label, plays: group }) => ({
      label,
      count: group.length,
      detail: [...new Set(group.map((g) => g.dj))].join('・'),
    }))
    .sort((a, b) => b.count - a.count || a.label.localeCompare(b.label, 'ja'))
    .slice(0, limit)
}

export function topTitles(plays: Play[], limit = 20): Ranked[] {
  return rank(plays, (p) => p.base || null, (p) => p.title, limit)
}

export function topWorks(plays: Play[], limit = 20): Ranked[] {
  return rank(
    plays,
    (p) => (p.work ? p.work.normalize('NFKC').toLowerCase() : null),
    (p) => p.work ?? '',
    limit,
  )
}

export function topArtists(plays: Play[], limit = 20): Ranked[] {
  return rank(
    plays,
    (p) => (p.artist ? p.artist.normalize('NFKC').toLowerCase() : null),
    (p) => p.artist ?? '',
    limit,
  )
}

export interface DjSummary {
  dj: string
  tracks: number
  events: number
  remixRate: number | null
}

export function perDj(plays: Play[]): DjSummary[] {
  const byDj = new Map<string, Play[]>()
  for (const p of plays) {
    const list = byDj.get(p.dj)
    if (list) list.push(p)
    else byDj.set(p.dj, [p])
  }
  return [...byDj.entries()]
    .map(([dj, group]) => {
      const known = group.filter((g) => g.isRemix !== null)
      return {
        dj,
        tracks: group.length,
        events: new Set(group.map((g) => g.eventNo)).size,
        remixRate: known.length
          ? known.filter((g) => g.isRemix).length / known.length
          : null,
      }
    })
    .sort((a, b) => b.tracks - a.tracks)
}

export interface EventSummary {
  eventNo: number
  date: string
  djs: number
  tracks: number
  newTitles: number
}

/** 開催回ごとの規模と、その回で初めてかかった曲の数。 */
export function perEvent(plays: Play[]): EventSummary[] {
  const byEvent = new Map<number, Play[]>()
  for (const p of plays) {
    const list = byEvent.get(p.eventNo)
    if (list) list.push(p)
    else byEvent.set(p.eventNo, [p])
  }
  const seen = new Set<string>()
  return [...byEvent.entries()]
    .sort((a, b) => a[0] - b[0])
    .map(([eventNo, group]) => {
      let fresh = 0
      for (const p of group) {
        if (!p.base) continue
        if (!seen.has(p.base)) {
          seen.add(p.base)
          fresh++
        }
      }
      return {
        eventNo,
        date: group[0].eventDate,
        djs: new Set(group.map((g) => g.dj)).size,
        tracks: group.length,
        newTitles: fresh,
      }
    })
}

export interface PlayedResult {
  query: string
  exact: Play[]
  sameBase: Play[]
}

/** 既出判定。完全一致と「同じ原曲の別リミックス」を分けて返す。 */
export function checkPlayed(plays: Play[], queries: string[]): PlayedResult[] {
  const byKey = new Map<string, Play[]>()
  const byBase = new Map<string, Play[]>()
  for (const p of plays) {
    if (p.key) {
      const l = byKey.get(p.key)
      if (l) l.push(p)
      else byKey.set(p.key, [p])
    }
    if (p.base) {
      const l = byBase.get(p.base)
      if (l) l.push(p)
      else byBase.set(p.base, [p])
    }
  }

  const order = (a: Play, b: Play) =>
    a.eventNo - b.eventNo || (a.trackNo ?? 0) - (b.trackNo ?? 0)

  return queries.map((query) => {
    const exact = byKey.get(normKey(query)) ?? []
    const sameBase = (byBase.get(baseKey(query)) ?? []).filter(
      (p) => !exact.includes(p),
    )
    return {
      query,
      exact: [...exact].sort(order),
      sameBase: [...sameBase].sort(order),
    }
  })
}
