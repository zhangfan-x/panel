import dayjs from 'dayjs'
import duration from 'dayjs/plugin/duration'
import relativeTime from 'dayjs/plugin/relativeTime'
import 'dayjs/locale/zh-cn'

dayjs.extend(duration)
dayjs.extend(relativeTime)
dayjs.locale('zh-cn')

/** 把秒数格式化成人性化的中文时长（如「3 天」「5 小时」）。 */
export function formatUptime(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) {
    return '未知'
  }
  return dayjs.duration(seconds, 'seconds').humanize()
}

/** 格式化时间戳为 YYYY-MM-DD HH:mm:ss。 */
export function formatDateTime(value: string | number | Date): string {
  return dayjs(value).format('YYYY-MM-DD HH:mm:ss')
}
