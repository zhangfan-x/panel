import { client } from './client'
import type {
  LoginRequest,
  SessionResponse,
  SessionStateResponse,
  TotpConfirmRequest,
  TotpSetupResponse,
  TotpVerifyRequest,
} from './types'

/** GET /auth/session：拉取当前会话；未登录时后端返回 401。 */
export async function getSession(): Promise<SessionResponse> {
  const { data } = await client.get<SessionResponse>('/auth/session')
  return data
}

/** POST /auth/login：密码校验，成功后进入 mfa_pending（或 totp_enroll）。 */
export async function login(body: LoginRequest): Promise<SessionStateResponse> {
  const { data } = await client.post<SessionStateResponse>('/auth/login', body)
  return data
}

/** POST /auth/totp/verify：登录第二步，校验 6 位动态码，成功升级为 active。 */
export async function verifyTotp(body: TotpVerifyRequest): Promise<SessionStateResponse> {
  const { data } = await client.post<SessionStateResponse>('/auth/totp/verify', body)
  return data
}

/** POST /auth/totp/setup：生成 TOTP 密钥与 otpauth URI。 */
export async function setupTotp(): Promise<TotpSetupResponse> {
  const { data } = await client.post<TotpSetupResponse>('/auth/totp/setup', {})
  return data
}

/** POST /auth/totp/confirm：确认绑定 TOTP，成功升级为 active。 */
export async function confirmTotp(body: TotpConfirmRequest): Promise<SessionStateResponse> {
  const { data } = await client.post<SessionStateResponse>('/auth/totp/confirm', body)
  return data
}

/** POST /auth/logout：撤销当前会话（需 CSRF）。 */
export async function logout(): Promise<void> {
  await client.post('/auth/logout')
}
