import { execFile } from 'node:child_process'
import { readFile } from 'node:fs/promises'
import type { IncomingMessage, ServerResponse } from 'node:http'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'
import type { Plugin } from 'vite'

// web/ の1つ上がリポジトリルート。odj.aliases はここを cwd にして呼ぶ
// （PYTHONPATH=src の src/ もここ基準）。
const here = path.dirname(fileURLToPath(import.meta.url))
const repoRoot = path.resolve(here, '..')

// ---------------------------------------------------------------------------
// 表記ゆれレビュー GUI 用 dev 専用 API（/api/review/*）
//
// 3者（Python / このミドルウェア / React UI）の唯一の取り決めは
// review-api-contract.md にある。ここはその契約を素直になぞるだけの薄い橋渡し。
// `npm run build` の出力には一切混ざらない
// （下の reviewApiPlugin は apply:'serve' に加えて review モードのときしか
//   plugins 配列に積まない二重の歯止めをかけてある）。
// ---------------------------------------------------------------------------

type ReviewField = 'work' | 'artist'

/** out/aliases/clusters.<field>.json の1クラスタ分。src/odj/aliases/block.py の出力そのまま。 */
interface RawClusterValue {
  raw: string
  rows: number
  events: number[]
  djs: string[]
  coArtists: string[]
  coTitles: string[]
  crossField?: boolean
}
interface RawClusterEdge {
  a: string
  b: string
  kinds: string[]
}
interface RawCluster {
  id: string
  field: ReviewField
  rows: number
  hints: string[]
  edgeKinds: string[]
  values: RawClusterValue[]
  edges: RawClusterEdge[]
}
interface RawClustersFile {
  field: ReviewField
  totalValues: number
  clustered: number
  singletons: number
  clusters: RawCluster[]
}

/** Phase 2 で LLM が data/aliases/_proposed/ に埋める統合提案。 */
interface Proposal {
  canonical: string
  series?: string
  kind?: string
  variants: string[]
  confidence?: string
  reason: string
  source?: string
}

function sendJson(res: ServerResponse, status: number, body: unknown): void {
  res.statusCode = status
  res.setHeader('Content-Type', 'application/json; charset=utf-8')
  res.end(JSON.stringify(body))
}

function readRequestBody(req: IncomingMessage): Promise<string> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = []
    req.on('data', (c: Buffer) => chunks.push(c))
    req.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')))
    req.on('error', reject)
  })
}

/**
 * `[[table]]` 形式の TOML をゆるく読む。フルスペックの TOML パーサではなく、
 * このリポジトリの data/aliases/*.toml が実際に使っている範囲
 * （文字列 / """三重引用符の複数行文字列""" / 文字列配列 / # コメント）だけを拾う。
 * テーブル名(work / artist など)は問わず、[[...]] が出るたびに新しいテーブルを開く。
 */
function parseArrayOfTables(text: string): Record<string, string | string[]>[] {
  const tables: Record<string, string | string[]>[] = []
  let current: Record<string, string | string[]> | null = null
  const lines = text.split('\n')
  let i = 0
  while (i < lines.length) {
    const trimmed = lines[i].trim()
    if (!trimmed || trimmed.startsWith('#')) {
      i++
      continue
    }
    if (/^\[\[.+\]\]$/.test(trimmed)) {
      current = {}
      tables.push(current)
      i++
      continue
    }
    if (!current) {
      i++
      continue
    }
    const arrayMatch = trimmed.match(/^([A-Za-z_][\w-]*)\s*=\s*\[(.*)\]$/)
    if (arrayMatch) {
      current[arrayMatch[1]] = [...arrayMatch[2].matchAll(/"([^"]*)"/g)].map((m) => m[1])
      i++
      continue
    }
    const tripleStart = trimmed.match(/^([A-Za-z_][\w-]*)\s*=\s*"""(.*)$/)
    if (tripleStart) {
      const key = tripleStart[1]
      const rest = tripleStart[2]
      const closeIdx = rest.indexOf('"""')
      if (closeIdx !== -1) {
        current[key] = rest.slice(0, closeIdx)
        i++
        continue
      }
      const buf = [rest]
      i++
      while (i < lines.length && !lines[i].includes('"""')) {
        buf.push(lines[i])
        i++
      }
      if (i < lines.length) {
        buf.push(lines[i].slice(0, lines[i].indexOf('"""')))
        i++
      }
      current[key] = buf.join('\n')
      continue
    }
    const stringMatch = trimmed.match(/^([A-Za-z_][\w-]*)\s*=\s*"((?:[^"\\]|\\.)*)"$/)
    if (stringMatch) {
      current[stringMatch[1]] = stringMatch[2].replace(/\\"/g, '"')
    }
    i++
  }
  return tables
}

/** decisions.jsonl から「もう出さなくていい id」を拾う。defer は次回も出す契約なので除く。 */
async function readDecidedIds(field: ReviewField): Promise<Set<string>> {
  const p = path.join(repoRoot, 'data', 'aliases', 'decisions.jsonl')
  let text: string
  try {
    text = await readFile(p, 'utf8')
  } catch {
    return new Set() // まだ1件も判断していない
  }
  const ids = new Set<string>()
  for (const line of text.split('\n')) {
    const trimmed = line.trim()
    if (!trimmed) continue
    try {
      const d = JSON.parse(trimmed) as { id?: string; field?: string; action?: string }
      if (d.field === field && d.action !== 'defer' && d.id) ids.add(d.id)
    } catch {
      // decisions.jsonl は追記専用ログ。書き込みの最中に読んで末尾の行が
      // 壊れて見えることがあり得るので、その1行だけ無視して読み進める。
    }
  }
  return ids
}

/**
 * data/aliases/_proposed/ の提案ファイルを読む。無ければ空 Map
 * （「無ければ proposal 無しで返す」契約どおり）。
 * ファイル名は最終辞書と同じ works.toml / artists.toml（_proposed/works.toml が実例）。
 * 契約書の素直な読み <field>.toml（work.toml）も念のため両対応しておく。
 */
async function readProposals(field: ReviewField): Promise<Map<string, Proposal>> {
  const candidates = [
    path.join(repoRoot, 'data', 'aliases', '_proposed', `${field}s.toml`),
    path.join(repoRoot, 'data', 'aliases', '_proposed', `${field}.toml`),
  ]
  let text: string | null = null
  for (const p of candidates) {
    try {
      text = await readFile(p, 'utf8')
      break
    } catch {
      // 次の候補へ
    }
  }
  if (text === null) return new Map()

  const map = new Map<string, Proposal>()
  try {
    for (const row of parseArrayOfTables(text)) {
      const id = row.id
      const canonical = row.canonical
      const reason = row.reason
      if (typeof id !== 'string' || typeof canonical !== 'string' || typeof reason !== 'string') {
        continue // id/canonical/reason を欠く行は契約を満たさないので proposal 扱いしない
      }
      map.set(id, {
        canonical,
        series: typeof row.series === 'string' ? row.series : undefined,
        kind: typeof row.kind === 'string' ? row.kind : undefined,
        variants: Array.isArray(row.variants) ? row.variants : [],
        confidence: typeof row.confidence === 'string' ? row.confidence : undefined,
        reason,
        source: typeof row.source === 'string' ? row.source : undefined,
      })
    }
  } catch (err) {
    // 提案が読めないだけで queue 全体を落とす理由にはならない。proposal 無しで続行する。
    console.warn('[review-api] _proposed の読み取りに失敗しました（proposal 無しで続行）:', err)
    return new Map()
  }
  return map
}

async function handleQueue(url: URL, res: ServerResponse): Promise<void> {
  const field = url.searchParams.get('field')
  if (field !== 'work' && field !== 'artist') {
    sendJson(res, 400, { ok: false, error: 'field は work か artist を指定してください' })
    return
  }
  const clustersPath = path.join(repoRoot, 'out', 'aliases', `clusters.${field}.json`)
  let raw: string
  try {
    raw = await readFile(clustersPath, 'utf8')
  } catch {
    sendJson(res, 500, {
      ok: false,
      error:
        `${path.relative(repoRoot, clustersPath)} が見つかりません。` +
        `先に \`uv run python -m odj.aliases block --field ${field}\` を実行してください。`,
    })
    return
  }

  let parsed: RawClustersFile
  try {
    parsed = JSON.parse(raw) as RawClustersFile
  } catch (err) {
    sendJson(res, 500, {
      ok: false,
      error: `${clustersPath} が JSON として読めません: ${err instanceof Error ? err.message : String(err)}`,
    })
    return
  }

  const [decidedIds, proposals] = await Promise.all([
    readDecidedIds(field),
    readProposals(field),
  ])
  const remaining = parsed.clusters.filter((c) => !decidedIds.has(c.id))
  const clusters = remaining.map((c) => {
    const proposal = proposals.get(c.id)
    return proposal ? { ...c, proposal } : c
  })

  sendJson(res, 200, {
    field,
    total: parsed.clusters.length,
    decided: parsed.clusters.length - remaining.length,
    clusters,
  })
}

/**
 * AliasError（バリデーション失敗・二重判断など、人間に見せて直してもらう種類の
 * 失敗）は src/odj/aliases/cli.py の _cmd_decide/_cmd_export が
 * `{"ok":false,"error":…}` を標準出力に出し、終了コード 1 で終わる決め事になって
 * いる（stderr には出ない。stderr は本当に想定外の例外だけ）。
 * ステータスコードまではその JSON に含まれないので、契約書に載っている
 * エラー文言のパターンから 400/409 を振り分ける。判定できない・想定外の文言は
 * 400 側に倒す（409 は「衝突」を意味するので、確信が持てるときだけ使う）。
 */
function guessErrorStatus(message: string | undefined): number {
  const msg = message ?? ''
  // 「この id は既に判断済みです」「〈raw〉は既に〈canonical〉に寄せられています」
  // 「keep_apart.toml で別物と決めた組が含まれています」→ いずれも二重送信・衝突
  if (msg.includes('既に') || msg.includes('含まれています')) return 409
  return 400
}

/**
 * decide / export は Python(src/odj/aliases/cli.py) にそのまま委ねる。
 * 標準入力に JSON を渡すのは、シェル経由で日本語の理由文などを渡すとクォートで
 * 事故るため（契約書に明記されている）。
 *
 * export はペイロードを取らない（store.export_json() を呼ぶだけで、
 * p_export に --json オプションは無い）。--json - を付けるのは decide だけ。
 */
async function proxyToPython(
  cmd: 'decide' | 'export',
  req: IncomingMessage,
  res: ServerResponse,
): Promise<void> {
  const body = await readRequestBody(req)
  const args =
    cmd === 'decide' ? ['-m', 'odj.aliases', 'decide', '--json', '-'] : ['-m', 'odj.aliases', 'export']
  const child = execFile(
    'python3',
    args,
    { cwd: repoRoot, env: { ...process.env, PYTHONPATH: 'src' }, maxBuffer: 16 * 1024 * 1024 },
    (error, stdout, stderr) => {
      const out = stdout.trim()
      if (out) {
        try {
          const parsed = JSON.parse(out) as { ok?: boolean; error?: string }
          if (parsed && typeof parsed === 'object' && 'ok' in parsed) {
            sendJson(res, parsed.ok ? 200 : guessErrorStatus(parsed.error), parsed)
            return
          }
        } catch {
          // stdout が JSON ではない → 下のフォールバックへ落ちて 500 にする
        }
      }
      if (error) {
        sendJson(res, 500, { ok: false, error: stderr.trim() || error.message })
        return
      }
      sendJson(res, 500, {
        ok: false,
        error: `python3 -m odj.aliases ${cmd} の出力が JSON ではありません: ${stdout.slice(0, 500)}`,
      })
    },
  )
  child.stdin?.end(cmd === 'decide' ? body : undefined)
}

function reviewApiPlugin(): Plugin {
  return {
    name: 'odj-review-api',
    apply: 'serve', // dev サーバーのみ。build には一切混ざらない
    configureServer(server) {
      server.middlewares.use('/api/review', async (req, res) => {
        try {
          const url = new URL(req.url ?? '/', 'http://localhost')
          const seg = url.pathname.replace(/^\/+/, '')
          if (req.method === 'GET' && seg === 'queue') {
            await handleQueue(url, res)
          } else if (req.method === 'POST' && seg === 'decide') {
            await proxyToPython('decide', req, res)
          } else if (req.method === 'POST' && seg === 'export') {
            await proxyToPython('export', req, res)
          } else {
            sendJson(res, 404, {
              ok: false,
              error: `不明なエンドポイントです: ${req.method} /api/review/${seg}`,
            })
          }
        } catch (err) {
          sendJson(res, 500, { ok: false, error: err instanceof Error ? err.message : String(err) })
        }
      })
    },
  }
}

// https://vite.dev/config/
export default defineConfig(({ mode }) => {
  const isReview = mode === 'review'
  return {
    base: '/odj_db_register/',
    plugins: [react(), ...(isReview ? [reviewApiPlugin()] : [])],
    // レビュー画面は既存の閲覧 GUI（npm run dev、5173 固定）とポートが
    // 衝突しないよう別ポートに固定する。
    server: isReview ? { port: 5174, strictPort: true } : undefined,
  }
})
