import { formatDateTime } from '@/composables/format'

import { client } from './client'

/** 单个 systemd 服务单元（GET /services 列表项；loaded 态与 unit-file enabled 态已合并）。 */
export interface ServiceUnit {
  name: string
  load: string
  active: string
  sub: string
  description: string
  /** unit-file 的安装状态（enabled/disabled/static/masked…），无 unit 文件时为空串。 */
  enabled: string
}

/** GET /services 响应。 */
export interface ServiceListResponse {
  units: ServiceUnit[]
}

/** 服务单元详情（GET /services/{unit}，来自 systemctl show）。 */
export interface ServiceDetail {
  name: string
  active: string
  sub: string
  load: string
  enabled: string
  description: string
  /** 主进程 PID，无主进程时为 0。 */
  main_pid: number
  /** 内存占用（字节），未知时为 null。 */
  memory: number | null
  since: string
  fragment_path: string
}

/** 服务生命周期动作（POST /services/{unit}/{action} 的路径段）。 */
export type ServiceVerb = 'start' | 'stop' | 'restart' | 'reload' | 'enable' | 'disable'

/** 一条 journald 日志（快照行与 SSE `journal` 事件同构）。 */
export interface JournalLine {
  ts: string
  message: string
  /** syslog 优先级 0-7（0=emerg … 7=debug）。 */
  priority: number
  unit: string
}

/** GET /services/{unit}/journal 响应。 */
export interface ServiceJournalResponse {
  lines: JournalLine[]
}

/** GET /services：全部服务单元列表。 */
export async function listServices(): Promise<ServiceListResponse> {
  const { data } = await client.get<ServiceListResponse>('/services')
  return data
}

/** GET /services/{unit}：单个服务单元详情。 */
export async function getService(unit: string): Promise<ServiceDetail> {
  const { data } = await client.get<ServiceDetail>(`/services/${encodeURIComponent(unit)}`)
  return data
}

/** POST /services/{unit}/{action}：对单元执行生命周期动作（start/stop/…/disable）。 */
export async function runServiceAction(unit: string, action: ServiceVerb): Promise<void> {
  await client.post(`/services/${encodeURIComponent(unit)}/${action}`)
}

/** GET /services/{unit}/journal：journald 日志快照（最近 lines 行，时间正序）。 */
export async function getServiceJournal(
  unit: string,
  lines = 200,
): Promise<ServiceJournalResponse> {
  const { data } = await client.get<ServiceJournalResponse>(
    `/services/${encodeURIComponent(unit)}/journal`,
    { params: { lines } },
  )
  return data
}

/** journald 实时跟随 SSE 地址（EventSource 直连，含 /api/v1 前缀；事件名为 `journal`）。 */
export function serviceJournalStreamUrl(unit: string, lines = 100): string {
  return `/api/v1/services/${encodeURIComponent(unit)}/journal/stream?lines=${lines}`
}

/** syslog 优先级 → 显示标签。 */
const PRIORITY_LABELS: Record<number, string> = {
  0: 'EMERG',
  1: 'ALERT',
  2: 'CRIT',
  3: 'ERR',
  4: 'WARN',
  5: 'NOTICE',
  6: 'INFO',
  7: 'DEBUG',
}

/** 把一条 journald 日志（快照行或 SSE `journal` 数据）转成显示文本；无法识别返回 null。 */
export function formatJournalLine(data: unknown): string | null {
  if (!data || typeof data !== 'object') {
    return null
  }
  const line = data as Partial<JournalLine>
  if (typeof line.message !== 'string') {
    return null
  }
  const label =
    typeof line.priority === 'number' ? (PRIORITY_LABELS[line.priority] ?? 'INFO') : 'INFO'
  const ts = typeof line.ts === 'string' && line.ts !== '' ? formatDateTime(line.ts) : ''
  return ts === '' ? `[${label}] ${line.message}` : `${ts} [${label}] ${line.message}`
}
