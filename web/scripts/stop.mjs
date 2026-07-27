/**
 * 開発サーバーを止める。`npm run stop`
 *
 * ターミナルを閉じてしまった、Ctrl+C が効かなかった等でプロセスが残ったとき用。
 * 通常は dev サーバーのターミナルで q + Enter を押せば終了する。
 *
 * 既定では閲覧 GUI（npm run dev = 5173）とレビュー GUI（npm run review = 5174）の
 * 両方を見る。片方だけ止めたいときは `npm run stop -- 5174` のように渡す。
 */
import { execFileSync } from 'node:child_process'

const PORTS = process.argv.slice(2).filter(Boolean)
if (PORTS.length === 0) PORTS.push('5173', '5174')

function pidsOnPort(port) {
  try {
    return execFileSync('lsof', ['-ti', `tcp:${port}`], { encoding: 'utf8' })
      .split('\n')
      .map((s) => s.trim())
      .filter(Boolean)
  } catch {
    return [] // lsof は該当なしのとき終了コード1
  }
}

let stopped = 0
for (const port of PORTS) {
  const pids = pidsOnPort(port)
  if (pids.length === 0) {
    console.log(`ポート ${port} で動いているプロセスはありません`)
    continue
  }
  for (const pid of pids) {
    try {
      process.kill(Number(pid), 'SIGTERM')
      console.log(`停止しました: PID ${pid} (ポート ${port})`)
      stopped++
    } catch (err) {
      console.error(`PID ${pid} を停止できませんでした: ${err.message}`)
    }
  }
}

// SIGTERM で落ちない場合に備えて少し待ってから確認する
if (stopped > 0) {
  setTimeout(() => {
    for (const port of PORTS) {
      const alive = pidsOnPort(port)
      if (alive.length > 0) {
        for (const pid of alive) process.kill(Number(pid), 'SIGKILL')
        console.log(`応答が無いので強制終了しました: ${alive.join(', ')} (ポート ${port})`)
      }
    }
  }, 800)
}
