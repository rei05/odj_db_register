/**
 * GUI のロジックを実データに当てて確かめる。
 *   node --experimental-strip-types scripts/verify.ts
 */
import { readFileSync } from 'node:fs'
import { toPlays } from '../src/lib/data.ts'
import { draftReason } from '../src/review/draft.ts'
import { baseKey, matchesQuery, normKey } from '../src/lib/normalize.ts'
import { checkPlayed, perEvent, topTitles, topWorks } from '../src/lib/stats.ts'
import type { Aliases, Dataset } from '../src/lib/types.ts'

const raw: Dataset = JSON.parse(
  readFileSync(new URL('../public/data/plays.json', import.meta.url), 'utf8'),
)
const aliases: Aliases = JSON.parse(
  readFileSync(new URL('../public/data/aliases.json', import.meta.url), 'utf8'),
)
const plays = toPlays(raw, aliases)

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

// 11. baseKey: ダッシュ区切りのリミックス表記（cutAtRemixDash）
// 期待値どうしの比較にする。normKey がハイフンや記号をどう潰すか手で書くと
// 間違えるので、baseKey(原曲名) との比較で確かめる。
check(
  '「PSI-missing -2011 remix-」の base が原曲相当になる',
  baseKey('PSI-missing -2011 remix-') === baseKey('PSI-missing'),
)
check(
  '「Tulip -TAKU INOUE Remix-」の base が原曲相当になる',
  baseKey('Tulip -TAKU INOUE Remix-') === baseKey('Tulip'),
)
check(
  '「Magia -dark trance mix-」の base が原曲相当になる',
  baseKey('Magia -dark trance mix-') === baseKey('Magia'),
)
check(
  '「Morning Arch - Natino Remix」の base が原曲相当になる',
  baseKey('Morning Arch - Natino Remix') === baseKey('Morning Arch'),
)
check(
  '「NEXT COLOR PLANET -TEKINA//remix-」の base が原曲相当になる',
  baseKey('NEXT COLOR PLANET -TEKINA//remix-') === baseKey('NEXT COLOR PLANET'),
)

// 壊してはいけないもの: 曲名の一部のハイフンは空白を伴わないので切られない
check(
  'リミックス表記のない「PSI-missing」の base はハイフンを保ったまま',
  baseKey('PSI-missing') === normKey('PSI-missing'),
)
// 「硝子ドール」6表記が1つの base に束ねられている点は9節の既出判定で
// 確認済み（cutAtRemixDash 追加後も壊れていないこと）

// 12. aliases.json: 検索の再現率（表記ゆれの吸収で件数が減らないこと）
// plays（辞書あり）と playsNoAlias（辞書なし）を比べる。辞書適用は haystack に
// 語を足すだけ（既存の語は消さない）なので、どのクエリでもヒット数は
// 「辞書あり ≧ 辞書なし」でなければならない。
const playsNoAlias = toPlays(raw)
const hitCount = (ps: typeof plays, q: string) =>
  ps.filter((p) => matchesQuery(p.haystack, q)).length

// 辞書は人間が GUI で1件ずつ承認して育てるものなので、public/data/aliases.json の
// 中身は日々変わるし、まだ1件も承認されていない状態（＝いまの CI）もありうる。
// ロジックの検証を実データの辞書に依存させると、そこで落ちる。合成した辞書で見る。
const LOVELIVE = ['ラブライブ', 'ラブライブ!', 'ラブライブ! 楽曲']
const syntheticAliases: Aliases = {
  generatedAt: '',
  works: Object.fromEntries(
    LOVELIVE.map((raw) => [
      raw,
      { c: 'ラブライブ!', s: 'ラブライブ!シリーズ', k: 'work' as const, v: LOVELIVE },
    ]),
  ),
  artists: {},
}
const playsSynthetic = toPlays(raw, syntheticAliases)

const loveliveHits = playsSynthetic.filter((p) => matchesQuery(p.haystack, 'ラブライブ'))
check(
  '「ラブライブ」で検索すると「ラブライブ!」登録行も拾う（表記ゆれ辞書）',
  loveliveHits.some((p) => p.work === 'ラブライブ!'),
  `${loveliveHits.length} 件ヒット`,
)
// 逆方向のほうが辞書の効きが分かる。「ラブライブ」は「ラブライブ!」の部分文字列
// なので順方向は辞書なしでも当たるが、逆は当たらない。
const loveliveBangHits = playsSynthetic.filter((p) => matchesQuery(p.haystack, 'ラブライブ!'))
check(
  '「ラブライブ!」で検索すると「ラブライブ」登録行も拾う（逆方向）',
  loveliveBangHits.some((p) => p.work === 'ラブライブ'),
  `${loveliveBangHits.length} 件ヒット`,
)
console.log(
  `      「ラブライブ!」ヒット数: 辞書なし ${hitCount(playsNoAlias, 'ラブライブ!')} 件 → ` +
    `辞書あり ${hitCount(playsSynthetic, 'ラブライブ!')} 件`,
)

// 辞書適用は haystack に語を足すだけ（既存の語は消さない）なので、
// どのクエリでもヒット数は「辞書あり ≧ 辞書なし」でなければならない。
for (const q of ['ラブライブ', 'ラブライブ!', '硝子ドール', '残酷な天使のテーゼ', 'アイカツ']) {
  const before = hitCount(playsNoAlias, q)
  const after = hitCount(playsSynthetic, q)
  check(
    `辞書適用でヒット数が減らない（「${q}」）`,
    after >= before,
    `${before} 件 → ${after} 件`,
  )
}

// 実データの辞書（中身は運用で変わる）でも、同じ不変条件が成り立つこと
for (const q of ['ラブライブ', '硝子ドール', 'アイカツ']) {
  check(
    `実データの辞書でもヒット数が減らない（「${q}」）`,
    hitCount(plays, q) >= hitCount(playsNoAlias, q),
  )
}

// 辞書が空/未取得でも toPlays が動くこと
check(
  '辞書が空（{}）でも toPlays が動く',
  toPlays(raw, { generatedAt: '', works: {}, artists: {} }).length === raw.plays.length,
)
check('辞書が null でも toPlays が動く', toPlays(raw, null).length === raw.plays.length)
check('aliases 引数を省略しても toPlays が動く', toPlays(raw).length === raw.plays.length)

// 13. 作品ランキング: workKind による絞り込み
// 実データの aliases.json は検証用に「ラブライブ」系しか入っていないため、
// 「ボカロ」「初音ミク」等を除外できるかはロジックそのものを合成データで
// 確かめる（実データに vocaloid/vtuber の承認済みエントリが増えても壊れない）。
const kindDataset: Dataset = {
  generatedAt: '2026-01-01',
  events: [{ no: 1, date: '2026-01-01', djs: ['検証用DJ'] }],
  plays: [
    { e: 1, p: 1, dj: '検証用DJ', n: 1, t: '曲A', w: 'ボカロ', a: null, r: null, u: null, k: 'test' },
    { e: 1, p: 1, dj: '検証用DJ', n: 2, t: '曲B', w: '未収載の作品', a: null, r: null, u: null, k: 'test' },
    { e: 1, p: 1, dj: '検証用DJ', n: 3, t: '曲C', w: '収載済みの作品', a: null, r: null, u: null, k: 'test' },
  ],
}
const kindAliases: Aliases = {
  generatedAt: '2026-01-01',
  works: {
    ボカロ: { c: 'ボカロ', k: 'vocaloid', v: ['ボカロ'] },
    収載済みの作品: { c: '収載済みの作品', k: 'work', v: ['収載済みの作品'] },
  },
  artists: {},
}
const kindWorks = topWorks(toPlays(kindDataset, kindAliases), 100)
check(
  '作品ランキングは workKind が work 以外（例: vocaloid）を除外する',
  !kindWorks.some((w) => w.label === 'ボカロ'),
)
check(
  '作品ランキングは workKind が null（辞書未収載）を除外しない',
  kindWorks.some((w) => w.label === '未収載の作品'),
)
check(
  '作品ランキングは workKind が work のものを含める',
  kindWorks.some((w) => w.label === '収載済みの作品'),
)

console.log(
  '      作品ランキング 上位10（辞書適用後）:',
  topWorks(plays, 10)
    .map((w) => `${w.label}(${w.count})`)
    .join(', '),
)

// 13. レビュー GUI: 理由の下書き
// LLM の提案が付くのは 152 クラスタ中 92 件で、残り 60 件のうち 41 件は
// series-risk（LLM が「迷ったら分ける」に従って答えを出さなかったもの）。
// **一番判断が難しいクラスタほど手掛かりが無い**状態にしないための下書き。
const draftFixture = {
  id: 'work-test',
  field: 'work' as const,
  rows: 9,
  hints: ['series-risk'],
  edgeKinds: ['agg', 'substr'],
  edges: [{ a: 'CLANNAD', b: 'CLANNAD~AFTER STORY', kinds: ['agg'] }],
  values: [
    {
      raw: 'CLANNAD',
      rows: 7,
      events: [1],
      djs: ['おかりん'],
      coArtists: ['Lia'],
      coTitles: ['メグメル'],
    },
    {
      raw: 'CLANNAD~AFTER STORY',
      rows: 1,
      events: [3],
      djs: ['おかりん'],
      coArtists: ['Lia'],
      coTitles: ['時を刻む唄'],
    },
  ],
}
const draft = draftReason(draftFixture)
check('提案が無いクラスタでも理由の下書きが空にならない', draft.trim().length > 0)
check('下書きが各表記の行数を挙げる', draft.includes('「CLANNAD」7行'))
check('下書きが繋いだ根拠を日本語で説明する', draft.includes('注記'))
check(
  '下書きが「同じ DJ が両方を使っている」ことを拾う',
  draft.includes('おかりん'),
  draft.split('\n')[2] ?? '',
)
check('下書きが series-risk の警告を書く', draft.includes('シリーズの別作品'))

console.log(failed === 0 ? '\nすべて通過' : `\n${failed} 件失敗`)
process.exit(failed === 0 ? 0 : 1)
