import { client } from './client'
import type { LoginRequest, SessionResponse, SessionStateResponse } from './types'

/** GET /auth/session：拉取当前会话；未登录时后端返回 401。 */
export async function getSession(): Promise<SessionResponse> {
  const { data } = await client.get<SessionResponse>('/auth/session')
  return data
}

/** POST /auth/login：密码校验，成功后会话即为 active。 */
export async function login(body: LoginRequest): Promise<SessionStateResponse> {
  const { data } = await client.post<SessionStateResponse>('/auth/login', body)
  return data
}

/** POST /auth/logout：撤销当前会话（需 CSRF）。 */
export async function logout(): Promise<void> {
  await client.post('/auth/logout')
}
