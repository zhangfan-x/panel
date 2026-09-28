import { client } from './client'

/** 审计结果的已知取值（后端 record_audit：成功 ok / 被拒 denied）。 */
export type AuditResult = 'ok' | 'denied'

/** 单条审计事件（GET /audit 列表项与 GET /audit/{event_id} 响应，字段与后端 _event_dict 逐字一致）。 */
export interface AuditEvent {
  event_id: string
  ts: string
  request_id: string
  actor_user_id: number | null
  source_ip: string
  user_agent: string
  resource: string
  action: string
  target: string
  result: string
  error_code: string | null
  duration_ms: number
}

/** GET /audit 响应。 */
export interface AuditListResponse {
  events: AuditEvent[]
  total: number
}

/** GET /audit 查询参数（action/resource/result 为精确匹配；since/until 为 ISO 时间字符串）。 */
export interface ListAuditParams {
  action?: string
  resource?: string
  result?: string
  since?: string
  until?: string
  limit?: number
  offset?: number
}

/** GET /audit：审计事件列表（时间倒序，支持过滤 + 分页）。 */
export async function listAuditEvents(params: ListAuditParams = {}): Promise<AuditListResponse> {
  const { data } = await client.get<AuditListResponse>('/audit', { params })
  return data
}

/** GET /audit/{event_id}：按公开事件 ID 查询单条审计事件。 */
export async function getAuditEvent(eventId: string): Promise<AuditEvent> {
  const { data } = await client.get<AuditEvent>(`/audit/${encodeURIComponent(eventId)}`)
  return data
}
