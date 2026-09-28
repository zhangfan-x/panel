import { client } from './client'
import { openSse } from './events'

/** 任务状态（与后端 JobStatus 枚举逐字一致）。 */
export type JobStatus =
  'queued' | 'running' | 'succeeded' | 'failed' | 'cancel_requested' | 'cancelled'

/** 任务摘要（GET /jobs 列表项与 GET /jobs/{id} 响应，字段与后端 _job_to_dict 一致）。 */
export interface JobSummary {
  id: string
  type: string
  title: string
  status: JobStatus
  progress: number | null
  stage: string | null
  created_at: string | null
  started_at: string | null
  finished_at: string | null
  error_code: string | null
  error_message: string | null
  resource_key: string | null
}

/** GET /jobs 响应。 */
export interface JobListResponse {
  jobs: JobSummary[]
  total: number
}

/** 全局 SSE `job` 事件负载（任务状态变更推送）。 */
export interface JobEventPayload {
  job_id: string
  status: JobStatus
  progress: number | null
  stage: string | null
  error_code: string | null
}

/**
 * SSE `job-log` 数据的三种形态：
 * 回放行 {replay,line}、实时行 {job_id,ts,level,message}、结束标记 {done,status}。
 */
export interface JobLogEntry {
  replay?: boolean
  line?: string
  job_id?: string
  ts?: string
  level?: string
  message?: string
  done?: boolean
  status?: JobStatus
}

/** el-tag 的 type 取值。 */
export type JobTagType = 'primary' | 'success' | 'warning' | 'info' | 'danger'

/** 任务状态的展示元数据（中文文案 + el-tag 类型）。 */
export interface JobStatusMeta {
  text: string
  tagType: JobTagType
}

/** 各任务状态的展示元数据。 */
export const JOB_STATUS_META: Record<JobStatus, JobStatusMeta> = {
  queued: { text: '排队中', tagType: 'info' },
  running: { text: '运行中', tagType: 'primary' },
  succeeded: { text: '已成功', tagType: 'success' },
  failed: { text: '已失败', tagType: 'danger' },
  cancel_requested: { text: '取消中', tagType: 'warning' },
  cancelled: { text: '已取消', tagType: 'warning' },
}

/** GET /jobs 的查询参数。 */
export interface ListJobsParams {
  status?: JobStatus
  limit?: number
  offset?: number
}

/** GET /jobs：任务列表（时间倒序，可按状态过滤）。 */
export async function listJobs(params: ListJobsParams = {}): Promise<JobListResponse> {
  const { data } = await client.get<JobListResponse>('/jobs', { params })
  return data
}

/** GET /jobs/{id}：单个任务详情。 */
export async function getJob(id: string): Promise<JobSummary> {
  const { data } = await client.get<JobSummary>(`/jobs/${encodeURIComponent(id)}`)
  return data
}

/** POST /jobs/{id}/cancel：请求取消（排队中直接取消，运行中转 cancel_requested）。 */
export async function cancelJob(id: string): Promise<void> {
  await client.post(`/jobs/${encodeURIComponent(id)}/cancel`)
}

/** POST /jobs/{id}/retry：按原参数重新提交任务，返回新任务 id。 */
export async function retryJob(id: string): Promise<{ job_id: string }> {
  const { data } = await client.post<{ job_id: string }>(`/jobs/${encodeURIComponent(id)}/retry`)
  return data
}

/** 任务日志 SSE 地址（EventSource 直连，含 /api/v1 前缀）。 */
export function jobLogUrl(id: string): string {
  return `/api/v1/jobs/${encodeURIComponent(id)}/log`
}

/** 打开任务日志流（先回放历史再实时跟随），返回关闭函数。 */
export function openJobLogStream(id: string, onEntry: (entry: JobLogEntry) => void): () => void {
  return openSse(
    jobLogUrl(id),
    (_topic, data) => {
      if (data && typeof data === 'object') {
        onEntry(data as JobLogEntry)
      }
    },
    { topics: ['job-log'] },
  )
}

/** 把 job-log SSE 数据转成一条显示行；无法识别时返回 null（调用方丢弃）。 */
export function formatJobLogEntry(data: unknown): string | null {
  if (typeof data === 'string') {
    return data
  }
  if (!data || typeof data !== 'object') {
    return null
  }
  const entry = data as JobLogEntry
  if (entry.done === true) {
    const meta = entry.status ? JOB_STATUS_META[entry.status] : undefined
    return `--- 任务已结束：${meta?.text ?? entry.status ?? '未知状态'} ---`
  }
  if (typeof entry.line === 'string') {
    return entry.line
  }
  if (typeof entry.message === 'string') {
    const level = (entry.level ?? 'info').toUpperCase()
    return entry.ts ? `${entry.ts} ${level} ${entry.message}` : `${level} ${entry.message}`
  }
  return null
}
