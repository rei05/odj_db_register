import { baseKey, normKey } from './normalize.ts'
import type { AliasEntry, Aliases, Dataset, Play, RawEvent } from './types.ts'

export interface Loaded {
  generatedAt: string
  events: RawEvent[]
  plays: Play[]
  djs: string[]
}

/**
 * aliases.json の1フィールド分（works か artists）を引きやすくしたもの。
 *
 * 辞書のキーは odj.aliases store が書いた生の文字列で、normKey は
 * Python 側に無い（意図的に移植していない。理由は AliasLookup のコメント参照）。
 * そこで読み込み時に1回だけ normKey(生キー) → エントリ の二次インデックスを
 * 張り、①生値の完全一致 → ②normKey 一致 の順で引く。
 */
interface AliasLookup {
  raw: Record<string, AliasEntry>
  byNorm: Map<string, AliasEntry>
}

function buildLookup(dict: Record<string, AliasEntry> | undefined): AliasLookup {
  const raw = dict ?? {}
  const byNorm = new Map<string, AliasEntry>()
  for (const k of Object.keys(raw)) {
    const nk = normKey(k)
    if (!byNorm.has(nk)) byNorm.set(nk, raw[k])
  }
  return { raw, byNorm }
}

/** 生値から同値クラスを引く。3段フォールバックの①②に当たる部分。 */
function resolveAlias(lookup: AliasLookup, value: string | null): AliasEntry | null {
  if (!value) return null
  return lookup.raw[value] ?? lookup.byNorm.get(normKey(value)) ?? null
}

/**
 * Dataset の生レコード（詰めたキー）を GUI で使う Play へ展開する。
 *
 * verify.ts（Node から `node --experimental-strip-types` で直接走る）でも
 * 同じ変換が要るため、fetch を含む loadData() から切り出してある。
 * aliases（表記ゆれの同値クラス辞書）は第2引数で受け取る。まだ辞書が無い
 * 環境やテストでは省略でき、その場合は③（生値をそのまま canonical とする）
 * だけが効く。
 */
export function toPlays(raw: Dataset, aliases?: Aliases | null): Play[] {
  const dateByEvent = new Map(raw.events.map((e) => [e.no, e.date]))
  const works = buildLookup(aliases?.works)
  const artists = buildLookup(aliases?.artists)

  return raw.plays.map((p) => {
    const workEntry = resolveAlias(works, p.w)
    const artistEntry = resolveAlias(artists, p.a)
    // ③ ヒットしなければ生値をそのまま canonical とする
    const workCanon = workEntry?.c ?? p.w
    const artistCanon = artistEntry?.c ?? p.a

    // 検索対象（haystack）は同値クラス全体（v）とシリーズ名まで広げる。
    // 「ラブライブ」で検索しても「ラブライブ!」表記の行を、逆も拾えるように
    // したい。検索は再現率志向で過剰統合のリスクが無い場所なので、辞書に
    // ある表記ゆれは全部足してよい。
    const haystackParts = [p.t, p.w ?? '', p.a ?? '']
    if (workEntry) {
      haystackParts.push(...workEntry.v)
      if (workEntry.s) haystackParts.push(workEntry.s)
    }
    if (artistEntry) haystackParts.push(...artistEntry.v)

    return {
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
      workCanon,
      workSeries: workEntry?.s ?? null,
      workKind: workEntry?.k ?? null,
      artistCanon,
      key: normKey(p.t),
      base: baseKey(p.t),
      haystack: normKey(haystackParts.join(' ')),
    }
  })
}

/**
 * aliases.json を読む。辞書がまだ書き出されていない環境（レビューが
 * 1件も終わっていない、開発中の worktree など）でも壊れないよう、
 * 404 や読み込み失敗は「辞書なし」として扱う（plays.json は必須のまま）。
 */
async function loadAliases(): Promise<Aliases | null> {
  try {
    const res = await fetch(`${import.meta.env.BASE_URL}data/aliases.json`)
    if (!res.ok) return null
    return (await res.json()) as Aliases
  } catch {
    return null
  }
}

export async function loadData(): Promise<Loaded> {
  const [res, aliases] = await Promise.all([
    fetch(`${import.meta.env.BASE_URL}data/plays.json`),
    loadAliases(),
  ])
  if (!res.ok) {
    throw new Error(
      `plays.json を読み込めませんでした (${res.status})。` +
        'リポジトリのルートで `uv run python -m odj.build` を実行してください。',
    )
  }
  const raw: Dataset = await res.json()
  const plays = toPlays(raw, aliases)

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
