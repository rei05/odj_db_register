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
  /**
   * 束ねられた生表記を detail に併記したいときに渡す（辞書適用後の
   * topWorks / topArtists 用）。canonical と異なる生値がグループ内に
   * あれば「canonical ← 生値1、生値2」を detail の頭に足す。辞書を
   * 適用しても黙って統合しない、が方針なので常に出所を見せる。
   */
  rawOf?: (p: Play) => string | null,
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
    .map(({ label, plays: group }) => {
      const djDetail = [...new Set(group.map((g) => g.dj))].join('・')
      const raws = rawOf
        ? [...new Set(group.map(rawOf).filter((r): r is string => !!r && r !== label))]
        : []
      const detail =
        raws.length > 0 ? `${label} ← ${raws.join('、')} / ${djDetail}` : djDetail
      return { label, count: group.length, detail }
    })
    .sort((a, b) => b.count - a.count || a.label.localeCompare(b.label, 'ja'))
    .slice(0, limit)
}

export function topTitles(plays: Play[], limit = 20): Ranked[] {
  return rank(plays, (p) => p.base || null, (p) => p.title, limit)
}

/**
 * 作品・元ネタランキング。辞書適用後の workCanon でまとめる。
 *
 * workKind が 'work' 以外（ボカロ・VTuber・オタクDJ大会自体・
 * アーティスト名が元ネタ欄に書かれたもの等）は作品ではないので除外する。
 * ただし workKind が null（辞書にまだ無い、大半のレコードがこれ）は
 * 除外しない。除外すると辞書が育つまでランキングが空になってしまう。
 */
export function topWorks(plays: Play[], limit = 20): Ranked[] {
  return rank(
    plays,
    (p) => {
      if (!p.workCanon) return null
      if (p.workKind && p.workKind !== 'work') return null
      return normKey(p.workCanon)
    },
    (p) => p.workCanon ?? '',
    limit,
    (p) => p.work,
  )
}

export function topArtists(plays: Play[], limit = 20): Ranked[] {
  return rank(
    plays,
    (p) => (p.artistCanon ? normKey(p.artistCanon) : null),
    (p) => p.artistCanon ?? '',
    limit,
    (p) => p.artist,
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
  /** 曲名がそのまま一致 */
  exact: Play[]
  /** リミックス表記を外すと一致 */
  sameBase: Play[]
  /** どちらかがもう一方を含む */
  partial: Play[]
}

/**
 * 部分一致を試してよい問い合わせか。
 *
 * 短すぎる語は何にでも当たって役に立たない。ただし絞り込める度合いは文字種で
 * 大きく違い、実データ 2,410 曲では漢字1文字なら「夢」5件・「恋」36件に収まる
 * のに対し、かな・英字1文字は「ラ」173件・「e」1,195件と使い物にならない。
 * そこで漢字だけ1文字を許し、それ以外は2文字以上を求める。
 */
export function canPartialMatch(key: string): boolean {
  if (key.length >= 2) return true
  return key.length === 1 && /\p{Script=Han}/u.test(key)
}

/**
 * 既出判定。確かさの高い順に3段階へ振り分ける。
 *
 * 1. 完全一致 … 曲名が（表記ゆれを均したうえで）そのまま一致
 * 2. 原曲一致 … リミックス表記を外すと一致。別アレンジでかかっている
 * 3. 部分一致 … どちらかがもう一方を含む。曲名うろ覚えや副題違いを拾う
 *
 * 同じプレイが複数の段に出ることはない。
 */
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
    const key = normKey(query)
    const exact = byKey.get(key) ?? []
    const taken = new Set(exact)

    const sameBase = (byBase.get(baseKey(query)) ?? []).filter(
      (p) => !taken.has(p),
    )
    for (const p of sameBase) taken.add(p)

    // 「硝子」で「硝子ドール」を、逆に副題まで打った長い曲名で
    // 短く登録されている曲を拾う
    const partial = canPartialMatch(key)
      ? plays.filter(
          (p) =>
            !taken.has(p) &&
            p.key.length > 0 &&
            (p.key.includes(key) || key.includes(p.key)),
        )
      : []

    return {
      query,
      exact: [...exact].sort(order),
      sameBase: [...sameBase].sort(order),
      partial: partial.sort(order),
    }
  })
}
