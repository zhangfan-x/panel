import { client } from './client'

/** POST /terminal/session 响应：一次性 WebSocket 票据（明文仅返回这一次）与会话 id。 */
export interface TerminalSessionResponse {
  ticket: string
  session_id: string
}

/** POST /terminal/session：签发一次性终端票据（需 CSRF + 近期重认证；票据 60 秒内有效）。 */
export async function createTerminalSession(): Promise<TerminalSessionResponse> {
  const { data } = await client.post<TerminalSessionResponse>('/terminal/session')
  return data
}

/** 终端 WebSocket 地址：同源 /api/v1/terminal/ws，协议随页面协议切换（https → wss）。 */
export function terminalWsUrl(): string {
  const scheme = window.location.protocol === 'https:' ? 'wss' : 'ws'
  return `${scheme}://${window.location.host}/api/v1/terminal/ws`
}
