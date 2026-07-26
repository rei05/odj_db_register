/**
 * レビュー用ミドルウェア（vite.config.ts の /api/review/*、dev サーバーのみ）を叩く薄いラッパー。
 */
import type {
  DecidePayload,
  DecideResult,
  ExportResult,
  Field,
  QueueResponse,
} from './types.ts'

/** queue は「取得できて当たり前」のデータなので、失敗時は例外にして
 * 呼び出し側は loadData() と同じ調子（catch して notice を出す）で扱えるようにする。 */
export async function fetchQueue(field: Field): Promise<QueueResponse> {
  const res = await fetch(`/api/review/queue?field=${field}`)
  const text = await res.text()
  let body: unknown
  try {
    body = text ? JSON.parse(text) : null
  } catch {
    throw new Error(
      `queue の応答が JSON ではありません (${res.status}): ${text.slice(0, 300)}`,
    )
  }
  if (!res.ok || !body || typeof body !== 'object' || 'error' in body) {
    const message =
      body && typeof body === 'object' && 'error' in body
        ? String((body as { error: unknown }).error)
        : `HTTP ${res.status}`
    throw new Error(message)
  }
  return body as QueueResponse
}

/**
 * decide は 400（バリデーション）/ 409（二重送信）も業務エラーとして起こり得るので、
 * ここでは throw せず { status, body } をそのまま返して呼び出し側に判断させる。
 * 「サーバが弾く。UI 側で二重送信を防ぐのではない」という契約どおり、
 * このラッパーも多重送信の抑止はしない。
 */
export async function postDecide(
  payload: DecidePayload,
): Promise<{ status: number; body: DecideResult }> {
  const res = await fetch('/api/review/decide', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  const text = await res.text()
  try {
    return { status: res.status, body: text ? JSON.parse(text) : { ok: false, error: '空の応答です' } }
  } catch {
    return {
      status: res.status,
      body: { ok: false, error: `応答が JSON ではありません: ${text.slice(0, 300)}` },
    }
  }
}

export async function postExport(): Promise<{ status: number; body: ExportResult }> {
  const res = await fetch('/api/review/export', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({}),
  })
  const text = await res.text()
  try {
    return { status: res.status, body: text ? JSON.parse(text) : { ok: false, error: '空の応答です' } }
  } catch {
    return {
      status: res.status,
      body: { ok: false, error: `応答が JSON ではありません: ${text.slice(0, 300)}` },
    }
  }
}
