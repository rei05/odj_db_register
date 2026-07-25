import type { Ranked } from '../lib/stats.ts'
import './BarList.css'

/**
 * 大きさの比較だけを見せる横棒。単系列なので凡例は置かず、
 * 値はテキスト色で直接ラベルする（色は本数の長さだけが情報）。
 */
export default function BarList({
  items,
  unit = '回',
}: {
  items: Ranked[]
  unit?: string
}) {
  const max = Math.max(1, ...items.map((i) => i.count))
  return (
    <ol className="barlist">
      {items.map((item, i) => (
        <li key={`${item.label}-${i}`} title={item.detail}>
          <span className="barlist-label">{item.label}</span>
          <span className="barlist-track">
            <span
              className="barlist-fill"
              style={{ width: `${(item.count / max) * 100}%` }}
            />
          </span>
          <span className="barlist-value">
            {item.count}
            <span className="barlist-unit">{unit}</span>
          </span>
        </li>
      ))}
    </ol>
  )
}
