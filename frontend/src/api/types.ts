// ownpanel 前端 API 类型定义（M1 手写）。
//
// 说明：这些接口是前后端固定契约，字段名必须与后端 problem+json / 响应体一字不差。
// 后续里程碑将由 `pnpm gen:api`（openapi-typescript 从 /api/openapi.json 生成 src/api/schema.d.ts）
// 生成的类型替换本文件中与端点绑定的部分；届时保留稳定的领域别名（如 SessionState）即可。

/** RFC 9457 problem+json 错误体。429 额外带 retry_after（秒）。 */
export interface Problem {
  type: string
  title: string
  status: number
  code: string
  detail?: string
  request_id?: string
  /** 仅 429 限速响应携带，单位：秒 */
  retry_after?: number
}

/** 会话状态。当前只有 active；TOTP 两步验证作为后续补充特性时会扩展此联合类型。 */
export type SessionState = 'active'

export interface SessionUser {
  id: number
  username: string
}

/** GET /auth/session */
export interface SessionResponse {
  user: SessionUser
  session_state: SessionState
  csrf_token: string
  reauth_recent: boolean
}

/** 多个端点统一返回下一步会话状态（login / bootstrap owner）。 */
export interface SessionStateResponse {
  session_state: SessionState
}

/** GET /bootstrap/state */
export interface BootstrapStateResponse {
  needs_setup: boolean
}

/** POST /bootstrap/owner */
export interface BootstrapOwnerRequest {
  token: string
  username: string
  password: string
}

/** POST /auth/login */
export interface LoginRequest {
  username: string
  password: string
}

/** GET /system/info */
export interface SystemInfo {
  hostname: string
  kernel: string
  os_release: string
  arch: string
  uptime_s: number
  cpu_count: number
}
