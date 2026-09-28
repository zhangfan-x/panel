import { client } from './client'
import type { JobStatus } from './jobs'
import type { SystemInfo } from './types'

/** GET /system/info：主机基础信息（需 active 会话）。 */
export async function getSystemInfo(): Promise<SystemInfo> {
  const { data } = await client.get<SystemInfo>('/system/info')
  return data
}

/** 内存占用（字节 + 百分比）。 */
export interface MetricMem {
  total: number
  used: number
  available: number
  percent: number
}

/** Swap 占用（字节 + 百分比）。 */
export interface MetricSwap {
  total: number
  used: number
  percent: number
}

/** 单个真实挂载点的磁盘用量。 */
export interface MetricDisk {
  mount: string
  device: string
  fstype: string
  total: number
  used: number
  percent: number
}

/** 网络吞吐：瞬时速率（字节/秒）与累计字节数。 */
export interface MetricNet {
  rx_bytes_per_s: number
  tx_bytes_per_s: number
  rx_total: number
  tx_total: number
}

/** 单块 GPU 快照（nvidia-smi 采集；无 GPU 或采集失败时 gpus 为空数组）。 */
export interface MetricGpu {
  index: number
  name: string
  util_percent: number
  mem_used_mb: number
  mem_total_mb: number
  temp_c: number
}

/** 一次指标采样（后端 MetricsSampler 产出，亦即全局 SSE `metrics` 事件负载）。 */
export interface MetricSample {
  /** 采样时刻（ISO 字符串，UTC）。 */
  ts: string
  cpu_percent: number
  per_cpu: number[]
  /** 1/5/15 分钟平均负载。 */
  load: number[]
  mem: MetricMem
  swap: MetricSwap
  disks: MetricDisk[]
  net: MetricNet
  /** GPU 快照；旧历史行可能缺失该字段。 */
  gpus?: MetricGpu[]
}

/** Docker 守护进程摘要（不可用时 available=false 且计数为 0）。 */
export interface DockerSummary {
  available: boolean
  running: number
  total: number
}

/** systemd 单元摘要（helper 不可用时 available=false 且无 total）。 */
export interface SystemdSummary {
  failed: number
  available: boolean
  total?: number
}

/** 概览里的最近任务条目（精简自任务详情）。 */
export interface OverviewJob {
  id: string
  type: string
  title: string
  status: JobStatus
  created_at: string | null
}

/** 概览里的安全事件条目（审计日志中 result != ok 的行）。 */
export interface SecurityEvent {
  event_id: string
  ts: string | null
  action: string
  resource: string
  result: string
  error_code: string | null
  source_ip: string | null
}

/** GET /system/overview 响应：仪表盘首屏聚合数据。 */
export interface SystemOverview {
  host: SystemInfo
  /** 最新一次采样；采集器刚启动尚无数据时为 null。 */
  metrics: MetricSample | null
  docker: DockerSummary
  systemd: SystemdSummary
  recent_jobs: OverviewJob[]
  security_events: SecurityEvent[]
}

/** GET /system/metrics 响应：图表用采样序列（按时间升序）。 */
export interface SystemMetricsResponse {
  samples: MetricSample[]
  /** live=内存环形缓冲；history=SQLite 聚合行。 */
  source: 'live' | 'history'
}

/** GET /system/overview：仪表盘聚合数据（主机、指标、服务摘要、最近任务/安全事件）。 */
export async function getOverview(): Promise<SystemOverview> {
  const { data } = await client.get<SystemOverview>('/system/overview')
  return data
}

/** GET /system/metrics：最近 minutes 分钟的指标采样（内存缓冲优先，历史兜底）。
 *
 * maxPoints 限制历史路径返回的最大点数（后端等距抽样，默认 2000，一般无需传）。
 */
export async function getMetrics(minutes = 30, maxPoints?: number): Promise<SystemMetricsResponse> {
  const { data } = await client.get<SystemMetricsResponse>('/system/metrics', {
    params: { minutes, max_points: maxPoints },
  })
  return data
}
