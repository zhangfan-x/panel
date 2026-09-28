import { client } from './client'

/** 服务器监听配置（GET /settings/system 的 server 字段）。 */
export interface ServerSettings {
  host: string
  port: number
  dev_http: boolean
  tls_cert: string
  tls_key: string
}

/** 运行限制配置（GET /settings/system 的 limits 字段，与后端 LimitsConfig 逐字一致）。 */
export interface LimitsSettings {
  session_idle_minutes: number
  session_max_hours: number
  reauth_window_seconds: number
  login_max_attempts: number
  upload_max_bytes: number
  job_concurrency: number
  audit_retention_days: number
  metric_retention_days: number
  terminal_idle_minutes: number
  terminal_max_minutes: number
}

/** GET /settings/system 响应：只读系统配置视图（不含任何机密）。 */
export interface SystemSettings {
  server: ServerSettings
  limits: LimitsSettings
  docker: { compose_root: string }
  version: string
}

/** GET /settings/profile 响应：所有者账号信息。 */
export interface OwnerProfile {
  username: string
  created_at: string
  password_changed_at: string
}

/** 单个在线会话（GET /auth/sessions 列表项，当前会话带 current 标记）。 */
export interface UserSession {
  id: number
  created_at: string
  last_seen_at: string
  ip: string
  user_agent: string
  current: boolean
}

/** POST /auth/password 请求体。 */
export interface ChangePasswordRequest {
  current_password: string
  new_password: string
}

/** GET /settings/system：只读系统配置。 */
export async function getSystemSettings(): Promise<SystemSettings> {
  const { data } = await client.get<SystemSettings>('/settings/system')
  return data
}

/** GET /settings/profile：所有者账号信息（用户名 + 创建/改密时间）。 */
export async function getOwnerProfile(): Promise<OwnerProfile> {
  const { data } = await client.get<OwnerProfile>('/settings/profile')
  return data
}

/** GET /auth/sessions：当前用户的全部在线会话。 */
export async function listSessions(): Promise<UserSession[]> {
  const { data } = await client.get<{ sessions: UserSession[] }>('/auth/sessions')
  return data.sessions
}

/** DELETE /auth/sessions/{id}：下线指定会话（当前会话也可下线，等同退出登录）。 */
export async function revokeSession(id: number): Promise<void> {
  await client.delete(`/auth/sessions/${id}`)
}

/** POST /auth/password：修改密码（需近期重认证；成功后其他会话全部失效）。 */
export async function changePassword(body: ChangePasswordRequest): Promise<void> {
  await client.post('/auth/password', body)
}
