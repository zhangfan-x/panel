import { client } from './client'

/** ufw 中的一条规则（序号来自 `ufw status numbered`，每次增删都会重排）。 */
export interface FirewallRule {
  /** 1 起的序号；删除时用它定位，但必须同时带上 expect 指纹。 */
  num: number
  /** 「至」列：如 22/tcp、80,443/tcp、Nginx Full（应用配置）。 */
  to: string
  /** 如 ALLOW IN、DENY OUT。 */
  action: string
  /** 「来自」列：Anywhere 或具体 IP/CIDR。 */
  from: string
  comment: string
  /** 该行是 IPv6 规则（ufw 会为多数 IPv4 规则生成一条 (v6) 孪生规则）。 */
  v6: boolean
}

/** GET /website/firewall/status 响应。 */
export interface FirewallStatus {
  /** 本机是否装了 ufw；false 时整页降级为提示。 */
  installed: boolean
  active: boolean
  /** 日志级别，如 "on (low)"。 */
  logging: string
  default_incoming: string
  default_outgoing: string
  default_routed: string
  rules: FirewallRule[]
  /** sshd 实际监听的端口（由 `sshd -T` 得到）。 */
  ssh_ports: number[]
  /** 面板自身监听端口。 */
  panel_port: number
  /** 受保护端口 = 面板端口 ∪ SSH 端口，丢失后可能再也连不上本机。 */
  protected_ports: number[]
  /** 当前没有入站放行规则的受保护端口。 */
  uncovered_ports: number[]
}

/** POST /website/firewall/rules 请求体。 */
export interface AddRuleRequest {
  action: 'allow' | 'deny' | 'reject' | 'limit'
  direction: 'in' | 'out'
  /** 单端口或 起始:结束 区间；留空表示不限端口。 */
  port: string
  proto: '' | 'tcp' | 'udp'
  from_addr: string
  to_addr: string
  comment: string
}

/** 规则指纹：与后端 rule_fingerprint 保持一致，用于防止序号漂移误删。 */
export function ruleFingerprint(rule: FirewallRule): string {
  return `${rule.to}|${rule.action}|${rule.from}`
}

/** GET /website/firewall/status：防火墙状态、规则与受保护端口分析。 */
export async function getFirewallStatus(): Promise<FirewallStatus> {
  const { data } = await client.get<FirewallStatus>('/website/firewall/status')
  return data
}

/** POST /website/firewall/enable：启用防火墙（需 CSRF + 近期重认证）。 */
export async function enableFirewall(confirmLockout = false): Promise<FirewallStatus> {
  const { data } = await client.post<FirewallStatus>('/website/firewall/enable', {
    confirm_lockout: confirmLockout,
  })
  return data
}

/** POST /website/firewall/disable：停用防火墙。 */
export async function disableFirewall(): Promise<FirewallStatus> {
  const { data } = await client.post<FirewallStatus>('/website/firewall/disable')
  return data
}

/** POST /website/firewall/rules：新增一条规则。 */
export async function addFirewallRule(body: AddRuleRequest): Promise<FirewallStatus> {
  const { data } = await client.post<FirewallStatus>('/website/firewall/rules', body)
  return data
}

/** DELETE /website/firewall/rules/{num}：删除规则，需带指纹与（必要时）风险确认。 */
export async function deleteFirewallRule(
  num: number,
  expect: string,
  confirmLockout = false,
): Promise<FirewallStatus> {
  const { data } = await client.delete<FirewallStatus>(`/website/firewall/rules/${num}`, {
    data: { expect, confirm_lockout: confirmLockout },
  })
  return data
}

/** POST /website/firewall/default：修改默认策略。 */
export async function setFirewallDefault(
  direction: 'incoming' | 'outgoing' | 'routed',
  policy: 'allow' | 'deny' | 'reject',
  confirmLockout = false,
): Promise<FirewallStatus> {
  const { data } = await client.post<FirewallStatus>('/website/firewall/default', {
    direction,
    policy,
    confirm_lockout: confirmLockout,
  })
  return data
}

/** POST /website/firewall/reload：重载规则。 */
export async function reloadFirewall(): Promise<FirewallStatus> {
  const { data } = await client.post<FirewallStatus>('/website/firewall/reload')
  return data
}
