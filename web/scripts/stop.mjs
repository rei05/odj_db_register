/**
 * 開発サーバーを止める。`npm run stop`
 *
 * ターミナルを閉じてしまった、Ctrl+C が効かなかった等でプロセスが残ったとき用。
 * 通常は dev サーバーのターミナルで q + Enter を押せば終了する。
 */
import { execFileSync } from 'node:child_process'

const PORT = process.argv[2] ?? '5173'

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

const pids = pidsOnPort(PORT)
if (pids.length === 0) {
  console.log(`ポート ${PORT} で動いているプロセスはありません`)
  process.exit(0)
}

for (const pid of pids) {
  try {
    process.kill(Number(pid), 'SIGTERM')
    console.log(`停止しました: PID ${pid} (ポート ${PORT})`)
  } catch (err) {
    console.error(`PID ${pid} を停止できませんでした: ${err.message}`)
  }
}

// SIGTERM で落ちない場合に備えて少し待ってから確認する
setTimeout(() => {
  const alive = pidsOnPort(PORT)
  if (alive.length > 0) {
    for (const pid of alive) process.kill(Number(pid), 'SIGKILL')
    console.log(`応答が無いので強制終了しました: ${alive.join(', ')}`)
  }
}, 800)
