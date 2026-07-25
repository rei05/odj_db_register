/**
 * GUI のロジックを実データに当てて確かめる。
 *   node --experimental-strip-types scripts/verify.ts
 */
import { readFileSync } from 'node:fs'
import { baseKey, matchesQuery, normKey } from '../src/lib/normalize.ts'
import { checkPlayed, perEvent, topTitles } from '../src/lib/stats.ts'
import type { Dataset, Play } from '../src/lib/types.ts'

const raw: Dataset = JSON.parse(
  readFileSync(new URL('../public/data/plays.json', import.meta.url), 'utf8'),
)
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

let failed = 0
function check(name: string, ok: boolean, detail = '') {
  console.log(`${ok ? 'ok  ' : 'FAIL'}  ${name}${detail ? ` — ${detail}` : ''}`)
  if (!ok) failed++
}

// 8. 検索
const zankoku = plays.filter((p) => matchesQuery(p.haystack, '残酷な天使のテーゼ'))
check(
  '検索「残酷な天使のテーゼ」が第15回 せーや を含む',
  zankoku.some((p) => p.eventNo === 15 && p.dj === 'せーや'),
  `${zankoku.length} 件ヒット`,
)
check(
  '検索が表記ゆれを吸収する（全角スペース・記号違い）',
  matchesQuery(normKey('Won(*3*)ChuKissMe!　桜Trick'), 'won(*3*)chukissme!'),
)

// 9. 既出判定
const [glass] = checkPlayed(plays, ['硝子ドール'])
const glassAll = [...glass.exact, ...glass.sameBase]
check(
  '既出判定「硝子ドール」が第1回 あぴす を拾う',
  glassAll.some((p) => p.eventNo === 1 && p.dj === 'あぴす'),
)
check(
  '既出判定「硝子ドール」が第11回 あちょ を拾う',
  glassAll.some((p) => p.eventNo === 11 && p.dj === 'あちょ'),
)
check(
  '硝子ドールの別リミックスが「原曲一致」側に入る',
  glass.sameBase.length > 0,
  glassAll.map((p) => `第${p.eventNo}回 ${p.title}`).join(' / '),
)
const [unknown] = checkPlayed(plays, ['この曲は絶対にかかっていないはず12345'])
check(
  '未プレイの曲は該当なしになる',
  unknown.exact.length === 0 &&
    unknown.sameBase.length === 0 &&
    unknown.partial.length === 0,
)

// 部分一致
const [fragment] = checkPlayed(plays, ['硝子'])
check(
  '部分一致「硝子」が硝子ドールを拾う',
  fragment.partial.some((p) => p.title.includes('硝子ドール')),
  `部分一致 ${fragment.partial.length} 件`,
)
check(
  '部分一致は完全一致・原曲一致と重複しない',
  checkPlayed(plays, ['硝子ドール']).every((r) => {
    const seen = new Set([...r.exact, ...r.sameBase, ...r.partial])
    return seen.size === r.exact.length + r.sameBase.length + r.partial.length
  }),
)
const [longer] = checkPlayed(plays, ['ふわふわ時間 けいおん 劇中歌バージョン'])
check(
  '長く打っても、短く登録されている曲を部分一致で拾う',
  [...longer.exact, ...longer.sameBase, ...longer.partial].some(
    (p) => p.title === 'ふわふわ時間',
  ),
)
const [kana1] = checkPlayed(plays, ['ラ'])
check('かな1文字では部分一致を試さない', kana1.partial.length === 0)
const [latin1] = checkPlayed(plays, ['e'])
check('英字1文字では部分一致を試さない', latin1.partial.length === 0)
const [kanji1] = checkPlayed(plays, ['恋'])
check(
  '漢字1文字なら部分一致する',
  kanji1.partial.length > 0 && kanji1.partial.every((p) => p.title.includes('恋')),
  `${kanji1.partial.length} 件`,
)

// 10. セトリ（play順）
const ev15 = plays.filter((p) => p.eventNo === 15)
const order15 = [...new Set(ev15.map((p) => `${p.playOrder}:${p.dj}`))].sort()
check('第15回に play順 が付いている', ev15.every((p) => p.playOrder !== null))
console.log('      第15回の並び:', order15.join(', '))
const djs15 = raw.events.find((e) => e.no === 15)!.djs
check(
  '第15回のセトリ未登録 DJ はデータに現れない',
  !djs15.includes('tri') && !djs15.includes('あちょ'),
  `登録あり: ${djs15.join('、')}`,
)

// 集計
const events = perEvent(plays)
check('全15回ぶんの集計が出る', events.length === 15)
check(
  '初出曲数が総曲数を超えない',
  events.every((e) => e.newTitles <= e.tracks),
)
const base1 = new Set(plays.filter((p) => p.eventNo === 1).map((p) => p.base))
check(
  '第1回の初出曲数＝第1回に現れる原曲の種類数',
  events[0].newTitles === base1.size,
  `初出 ${events[0].newTitles} / 種類 ${base1.size} / プレイ ${events[0].tracks}`,
)
console.log('      よくかかる曲 上位5:', topTitles(plays, 5).map((t) => `${t.label}(${t.count})`).join(', '))

console.log(failed === 0 ? '\nすべて通過' : `\n${failed} 件失敗`)
process.exit(failed === 0 ? 0 : 1)
