import { client } from './client'
import type { BootstrapOwnerRequest, BootstrapStateResponse, SessionStateResponse } from './types'

/** GET /bootstrap/state：是否需要初始化（无 owner）。公开端点。 */
export async function getBootstrapState(): Promise<BootstrapStateResponse> {
  const { data } = await client.get<BootstrapStateResponse>('/bootstrap/state')
  return data
}

/** POST /bootstrap/owner：用一次性 token 创建 Owner，成功后会话即为 active。公开端点（无 CSRF）。 */
export async function createOwner(body: BootstrapOwnerRequest): Promise<SessionStateResponse> {
  const { data } = await client.post<SessionStateResponse>('/bootstrap/owner', body)
  return data
}
