import { client } from './client'
import type { SystemInfo } from './types'

/** GET /system/info：主机基础信息（需 active 会话）。 */
export async function getSystemInfo(): Promise<SystemInfo> {
  const { data } = await client.get<SystemInfo>('/system/info')
  return data
}
