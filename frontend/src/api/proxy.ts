import { client } from './client'

/** 代理模式：规则 / 全局 / 直连。 */
export type ProxyMode = 'rule' | 'global' | 'direct'

/** GET /proxy/status：控制器版本 + 运行态配置摘要。 */
export interface ProxyStatus {
  version: string
  mode: ProxyMode | string
  mixed_port: number
  port: number
  socks_port: number
  redir_port: number
  allow_lan: boolean
  log_level: string
}

/** 组内的一个候选节点（含最近一次测得的延迟，0 表示未知/超时）。 */
export interface ProxyNode {
  name: string
  type: string
  /** 最近一次记录的延迟（毫秒）；0 表示未知或超时。 */
  delay: number
  /**
   * 延迟的来源节点名。成员本身是代理组时（组自身没有延迟记录），延迟沿其当前
   * 选中节点继承而来，这里给出那个真实节点；直接测得则为空串。
   */
  delay_of: string
}

/** 一个可选择的代理组（Selector/URLTest/Fallback…）。 */
export interface ProxyGroup {
  name: string
  type: string
  /** 当前选中的节点名。 */
  now: string
  all: ProxyNode[]
}

/** GET /proxy/groups 响应。 */
export interface ProxyGroupsResponse {
  groups: ProxyGroup[]
}

/** GET /proxy/proxies/{name}/delay 响应。 */
export interface DelayResult {
  /** 延迟（毫秒）；0 表示超时/失败。 */
  delay: number
  /** 失败时的控制器消息（可选）。 */
  message?: string
}

/** GET /proxy/groups/{group}/delay 响应：整组一次测速的结果。 */
export interface GroupDelayResult {
  /** 节点名 → 延迟（毫秒）；超时的成员不会出现在这里。 */
  delays: Record<string, number>
  /** true 表示由控制器批量测速，false 表示回退到逐节点探测。 */
  batched: boolean
}

/** GET /proxy/config-file 响应。 */
export interface ConfigFileResponse {
  path: string
  content: string
}

/** 实时流量事件（SSE `traffic`）：每秒上/下行字节数。 */
export interface ProxyTraffic {
  up: number
  down: number
}

/** GET /proxy/status：控制器状态与模式。 */
export async function getProxyStatus(): Promise<ProxyStatus> {
  const { data } = await client.get<ProxyStatus>('/proxy/status')
  return data
}

/** GET /proxy/groups：可选择的代理组及其节点。 */
export async function getProxyGroups(): Promise<ProxyGroupsResponse> {
  const { data } = await client.get<ProxyGroupsResponse>('/proxy/groups')
  return data
}

/** POST /proxy/groups/{group}/select：切换组内选中的节点。 */
export async function selectNode(group: string, name: string): Promise<void> {
  await client.post(`/proxy/groups/${encodeURIComponent(group)}/select`, { name })
}

/** GET /proxy/proxies/{name}/delay：对单个节点做延迟测试。 */
export async function testDelay(name: string, timeout = 5000): Promise<DelayResult> {
  const { data } = await client.get<DelayResult>(
    `/proxy/proxies/${encodeURIComponent(name)}/delay`,
    { params: { timeout } },
  )
  return data
}

/** GET /proxy/groups/{group}/delay：一次请求测完整组，避免每个节点发一个请求。 */
export async function testGroupDelay(group: string, timeout = 5000): Promise<GroupDelayResult> {
  const { data } = await client.get<GroupDelayResult>(
    `/proxy/groups/${encodeURIComponent(group)}/delay`,
    { params: { timeout } },
  )
  return data
}

/** PATCH /proxy/config：切换代理模式（规则/全局/直连）。 */
export async function patchMode(mode: ProxyMode): Promise<void> {
  await client.patch('/proxy/config', { mode })
}

/** GET /proxy/connections：连接快照与累计上/下行流量。 */
export async function getConnections(): Promise<ConnectionsSnapshot> {
  const { data } = await client.get<ConnectionsSnapshot>('/proxy/connections')
  return data
}

/** DELETE /proxy/connections：关闭全部连接。 */
export async function closeConnections(): Promise<void> {
  await client.delete('/proxy/connections')
}

/** DELETE /proxy/connections/{id}：关闭单个连接。 */
export async function closeConnection(id: string): Promise<void> {
  await client.delete(`/proxy/connections/${encodeURIComponent(id)}`)
}

/** GET /proxy/config-file：读取 Clash 配置文件文本。 */
export async function getConfigFile(): Promise<ConfigFileResponse> {
  const { data } = await client.get<ConfigFileResponse>('/proxy/config-file')
  return data
}

/** PUT /proxy/config-file：写入并重载 Clash 配置文件（需 CSRF + 重认证）。 */
export async function putConfigFile(content: string): Promise<void> {
  await client.put('/proxy/config-file', { content })
}

/** 实时流量 SSE 地址（EventSource 直连，含 /api/v1 前缀；事件名为 `traffic`）。 */
export function proxyTrafficStreamUrl(): string {
  return '/api/v1/proxy/traffic/stream'
}

/** 把一条 SSE `traffic` 数据解析为 {up, down}；无法识别返回 null。 */
export function parseTraffic(data: unknown): ProxyTraffic | null {
  if (!data || typeof data !== 'object') {
    return null
  }
  const record = data as Record<string, unknown>
  const up = record.up
  const down = record.down
  if (typeof up !== 'number' || typeof down !== 'number') {
    return null
  }
  return { up, down }
}

// —— 服务开关（clash 核心 systemd 用户服务） ——

/** GET /proxy/service：clash 核心服务运行状态（与控制器可达性无关）。 */
export interface ProxyServiceStatus {
  service: string
  running: boolean
  /** systemctl is-active 输出：active / inactive / failed / unknown…。 */
  state: string
}

/** 服务开关动作。 */
export type ProxyServiceAction = 'start' | 'stop' | 'restart'

/** POST /proxy/service 响应。 */
export interface ProxyServiceActionResult {
  ok: boolean
  action: ProxyServiceAction
  running: boolean
  state: string
}

/** GET /proxy/service：查询 clash 核心服务状态。 */
export async function getProxyService(): Promise<ProxyServiceStatus> {
  const { data } = await client.get<ProxyServiceStatus>('/proxy/service')
  return data
}

/** POST /proxy/service：启动/停止/重启 clash 核心服务。 */
export async function proxyServiceAction(
  action: ProxyServiceAction,
): Promise<ProxyServiceActionResult> {
  const { data } = await client.post<ProxyServiceActionResult>('/proxy/service', { action })
  return data
}

// —— 规则（生效规则 + 面板自定义规则） ——

/** 控制器当前生效的一条路由规则。 */
export interface ActiveRule {
  type: string
  payload: string
  proxy: string
}

/** GET /proxy/rules：控制器当前生效规则。 */
export async function getActiveRules(): Promise<{ rules: ActiveRule[] }> {
  const { data } = await client.get<{ rules: ActiveRule[] }>('/proxy/rules')
  return data
}

/** 面板保存的一条自定义规则（注入配置文件、不被上传覆盖）。 */
export interface CustomRule {
  id: string
  rule: string
  created_at: string
}

/** GET /proxy/rules/custom：面板自定义规则列表。 */
export async function getCustomRules(): Promise<{ rules: CustomRule[] }> {
  const { data } = await client.get<{ rules: CustomRule[] }>('/proxy/rules/custom')
  return data
}

/** POST /proxy/rules/custom：新增一条自定义规则并注入配置文件（立即生效）。 */
export async function addCustomRule(rule: string): Promise<CustomRule> {
  const { data } = await client.post<{ ok: boolean; rule: CustomRule }>('/proxy/rules/custom', {
    rule,
  })
  return data.rule
}

/** DELETE /proxy/rules/custom/{id}：删除一条自定义规则并同步配置文件。 */
export async function deleteCustomRule(id: string): Promise<void> {
  await client.delete(`/proxy/rules/custom/${encodeURIComponent(id)}`)
}

// —— 连接跟踪（面板侧 tracker：活跃 + 最近关闭） ——

/** tracker 记录的一条连接（活跃或已关闭）。 */
export interface TrackedConnection {
  id: string
  /** 目标主机名；无 SNI/Host 时为 "IP:端口"。 */
  host: string
  network: string
  type: string
  source: string
  destination: string
  rule: string
  rule_payload: string
  chains: string[]
  /** 累计字节数。 */
  upload: number
  download: number
  start: string
  /** 相邻两次轮询差分出的速率（字节/秒），仅活跃连接。 */
  upload_rate?: number
  download_rate?: number
  /** 关闭时间（仅已关闭连接）。 */
  closed_at?: string
}

/** GET /proxy/connections 响应（后台 tracker 快照，约 2s 新鲜度）。 */
export interface ConnectionsSnapshot {
  /** false 表示控制器当前不可达（active 为空，closed 保留）。 */
  available: boolean
  active: TrackedConnection[]
  /** 最近关闭的连接（最新在前，最多 100 条）。 */
  closed: TrackedConnection[]
  download_total: number
  upload_total: number
  updated_at: string | null
}

// —— 流量历史（每分钟平均速率，内存保留 7 天） ——

/** 一分钟平均上/下行速率（字节/秒）。 */
export interface TrafficSample {
  ts: string
  up: number
  down: number
}

/** GET /proxy/traffic/history 响应。 */
export interface TrafficHistoryResponse {
  samples: TrafficSample[]
  latest: { up: number; down: number }
}

/** GET /proxy/traffic/history：按窗口取每分钟平均速率（默认 24h）。 */
export async function getTrafficHistory(minutes = 1440): Promise<TrafficHistoryResponse> {
  const { data } = await client.get<TrafficHistoryResponse>('/proxy/traffic/history', {
    params: { minutes },
  })
  return data
}

// —— 订阅（多订阅，选一个为当前生效）与扩展配置/脚本 ——

/** 机场通过 subscription-userinfo 头返回的用量信息（字节 / Unix 秒）。 */
export interface SubscriptionUsage {
  upload?: number
  download?: number
  total?: number
  expire?: number
}

/** 一个订阅源。当前生效的那个（active）决定配置文件由谁生成。 */
export interface ProxyProfile {
  id: string
  name: string
  url: string
  /** 为空时后端使用默认 UA（多数机场按 UA 返回 clash 格式）。 */
  user_agent: string
  /** 自动更新间隔（分钟）；0 表示不自动更新。 */
  interval_min: number
  active: boolean
  /** 最近一次成功下载时间（ISO 8601），未成功过为空串。 */
  last_updated: string
  /** 最近一次失败原因，成功后清空。 */
  last_error: string
  subscription: SubscriptionUsage
}

/** GET /proxy/profiles 响应。 */
export interface ProxyProfilesResponse {
  profiles: ProxyProfile[]
  /** 当前生效订阅的 id；没有则为空串。 */
  active_id: string
}

/** 新增订阅的表单字段。 */
export interface ProfileCreatePayload {
  name: string
  url: string
  interval_min: number
  user_agent: string
}

/** 编辑订阅：省略的字段保持原值。 */
export type ProfileUpdatePayload = Partial<ProfileCreatePayload>

/** 扩展配置（merge YAML）与扩展脚本（JavaScript）。 */
export interface ProxyExtend {
  merge: string
  script: string
}

/** POST /proxy/extend/preview 响应：合成结果与脚本 console 输出。 */
export interface ExtendPreview {
  content: string
  logs: string[]
}

/** GET /proxy/profiles：订阅列表与当前生效项。 */
export async function getProfiles(): Promise<ProxyProfilesResponse> {
  const { data } = await client.get<ProxyProfilesResponse>('/proxy/profiles')
  return data
}

/** POST /proxy/profiles：新增订阅并立即下载（首个订阅自动成为当前生效）。 */
export async function createProfile(payload: ProfileCreatePayload): Promise<ProxyProfile> {
  const { data } = await client.post<{ ok: boolean; profile: ProxyProfile }>(
    '/proxy/profiles',
    payload,
  )
  return data.profile
}

/** PATCH /proxy/profiles/{id}：修改名称 / 地址 / 更新间隔 / UA。 */
export async function updateProfile(
  id: string,
  payload: ProfileUpdatePayload,
): Promise<ProxyProfile> {
  const { data } = await client.patch<{ ok: boolean; profile: ProxyProfile }>(
    `/proxy/profiles/${encodeURIComponent(id)}`,
    payload,
  )
  return data.profile
}

/** DELETE /proxy/profiles/{id}：删除订阅及其缓存（不改动已生成的配置文件）。 */
export async function deleteProfile(id: string): Promise<ProxyProfilesResponse> {
  const { data } = await client.delete<ProxyProfilesResponse>(
    `/proxy/profiles/${encodeURIComponent(id)}`,
  )
  return data
}

/** POST /proxy/profiles/{id}/update：立即重新下载；是当前生效项则重新生成配置。 */
export async function refreshProfile(id: string): Promise<ProxyProfile> {
  const { data } = await client.post<{ ok: boolean; profile: ProxyProfile }>(
    `/proxy/profiles/${encodeURIComponent(id)}/update`,
  )
  return data.profile
}

/** POST /proxy/profiles/{id}/activate：设为当前生效并重新生成配置。 */
export async function activateProfile(id: string): Promise<ProxyProfilesResponse> {
  const { data } = await client.post<ProxyProfilesResponse>(
    `/proxy/profiles/${encodeURIComponent(id)}/activate`,
  )
  return data
}

/** GET /proxy/extend：读取扩展配置与扩展脚本。 */
export async function getExtend(): Promise<ProxyExtend> {
  const { data } = await client.get<ProxyExtend>('/proxy/extend')
  return data
}

/** PUT /proxy/extend：保存并立即重新生成配置（需 CSRF + 重认证）。 */
export async function putExtend(payload: Partial<ProxyExtend>): Promise<ProxyExtend> {
  const { data } = await client.put<{ ok: boolean } & ProxyExtend>('/proxy/extend', payload)
  return { merge: data.merge, script: data.script }
}

/** POST /proxy/extend/preview：试跑流水线，不写盘、不影响运行中的代理。 */
export async function previewExtend(payload: Partial<ProxyExtend>): Promise<ExtendPreview> {
  const { data } = await client.post<ExtendPreview>('/proxy/extend/preview', payload)
  return data
}
