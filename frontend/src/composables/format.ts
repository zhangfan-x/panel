import dayjs from 'dayjs'
import duration from 'dayjs/plugin/duration'
import relativeTime from 'dayjs/plugin/relativeTime'
import 'dayjs/locale/zh-cn'

dayjs.extend(duration)
dayjs.extend(relativeTime)
dayjs.locale('zh-cn')

/**
 * 把秒数格式化成「年月日小时」时长（如「4天15小时」「1年2月3天0小时」）。
 * 从最大单位起，前面值为 0 的单位不展示；不足 1 小时显示「不足 1 小时」。
 */
export function formatUptime(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) {
    return '未知'
  }
  const units: Array<{ label: string; secs: number }> = [
    { label: '年', secs: 365 * 86400 },
    { label: '月', secs: 30 * 86400 },
    { label: '天', secs: 86400 },
    { label: '小时', secs: 3600 },
  ]
  let remaining = Math.floor(seconds)
  const parts = units.map((unit) => {
    const value = Math.floor(remaining / unit.secs)
    remaining -= value * unit.secs
    return { label: unit.label, value }
  })
  // 跳过开头连续为 0 的单位（保留至少「小时」一项作为兜底）。
  let start = 0
  while (start < parts.length - 1 && parts[start]?.value === 0) {
    start += 1
  }
  const shown = parts.slice(start)
  if (shown.every((part) => part.value === 0)) {
    return '不足 1 小时'
  }
  return shown.map((part) => `${part.value}${part.label}`).join('')
}

/** 格式化时间戳为 YYYY-MM-DD HH:mm:ss。 */
export function formatDateTime(value: string | number | Date): string {
  return dayjs(value).format('YYYY-MM-DD HH:mm:ss')
}
