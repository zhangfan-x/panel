import { client } from './client'
import type { ApiError } from './client'

/** GET /docker/info 响应（守护进程不可达时仅返回 available=false）。 */
export interface DockerInfo {
  available: boolean
  version?: string
  api_version?: string
  os?: string
  kernel?: string
  arch?: string
  containers?: number
  running?: number
  paused?: number
  stopped?: number
  images?: number
}

/** 容器端口映射（Docker Engine 原始字段，保持 PascalCase）。 */
export interface ContainerPort {
  IP?: string
  PrivatePort: number
  PublicPort?: number
  Type: string
}

/** 容器摘要（GET /docker/containers 列表项）。 */
export interface ContainerSummary {
  id: string
  name: string
  image: string
  state: string
  status: string
  ports: ContainerPort[]
  /** Unix 秒。 */
  created: number
  labels: Record<string, string>
}

/** GET /docker/containers 响应。 */
export interface ContainerListResponse {
  containers: ContainerSummary[]
}

/** 容器生命周期动作（与后端 ContainerAction 逐字一致）。 */
export type ContainerLifecycleAction =
  'start' | 'stop' | 'restart' | 'pause' | 'unpause' | 'kill' | 'remove'

/** GET /docker/containers/{id}/logs 响应（truncated 表示超出快照预算被截断）。 */
export interface ContainerLogsResponse {
  lines: string[]
  truncated: boolean
}

/** 容器运行态（GET /docker/containers/{id} inspect 的 state 子对象）。 */
export interface ContainerInspectState {
  status: string
  running: boolean
  paused: boolean
  exit_code: number
  started_at: string
  finished_at: string
  error: string
  pid: number
  /** 健康检查状态；无健康检查时为 null。 */
  health: string | null
}

/** 单个网络的容器侧地址信息。 */
export interface ContainerNetworkInfo {
  ip_address: string
  gateway: string
  mac_address: string
}

/** 容器挂载项。 */
export interface ContainerMount {
  type: string
  source: string
  destination: string
  rw: boolean
}

/** inspect 端口绑定（NetworkSettings.Ports 的值项，保持 PascalCase）。 */
export interface PortBinding {
  HostIp?: string
  HostPort?: string
}

/** GET /docker/containers/{id}：精简 inspect（字段名即后端契约）。 */
export interface ContainerInspect {
  id: string
  name: string
  image: string
  image_id: string
  /** ISO 时间字符串。 */
  created: string
  state: ContainerInspectState
  restart_policy: string
  cmd: string[] | null
  entrypoint: string[] | null
  /** 形如 "KEY=VALUE" 的环境变量数组。 */
  env: string[]
  labels: Record<string, string>
  /** 形如 { "80/tcp": [{ HostIp, HostPort }], "443/tcp": null } 的端口映射。 */
  ports: Record<string, PortBinding[] | null>
  networks: Record<string, ContainerNetworkInfo>
  mounts: ContainerMount[]
}

/** GET /docker/containers/{id}/stats：一次性资源快照（后端已扁平化并算好百分比）。 */
export interface ContainerStats {
  /** 采样时间（ISO 字符串）。 */
  read: string
  cpu_percent: number
  online_cpus: number
  /** 字节。 */
  mem_used: number
  /** 字节。 */
  mem_limit: number
  mem_percent: number
  /** 累计接收字节。 */
  net_rx: number
  /** 累计发送字节。 */
  net_tx: number
  /** 累计块设备读字节。 */
  blk_read: number
  /** 累计块设备写字节。 */
  blk_write: number
  pids: number
}

/** 按完整容器 id 索引的批量资源快照。 */
export type ContainerStatsMap = Record<string, ContainerStats>

/** GET /docker/stats 响应。 */
export interface AllContainerStatsResponse {
  stats: ContainerStatsMap
}

/** 镜像摘要（GET /docker/images 列表项）。 */
export interface ImageSummary {
  id: string
  tags: string[]
  /** 字节。 */
  size: number
  /** Unix 秒。 */
  created: number
}

/** GET /docker/images 响应。 */
export interface ImageListResponse {
  images: ImageSummary[]
}

/** POST /docker/images/pull 请求体。 */
export interface ImagePullRequest {
  repository: string
  tag?: string
}

/** 202 任务受理响应。 */
export interface JobAcceptedResponse {
  job_id: string
}

/** 网络驱动（与后端 NetworkCreateRequest 一致）。 */
export type NetworkDriver = 'bridge' | 'overlay' | 'macvlan' | 'ipvlan'

/** 网络摘要（GET /docker/networks 列表项）。 */
export interface NetworkSummary {
  id: string
  name: string
  driver: string
  scope: string
  internal: boolean
  /** ISO 时间字符串（部分引擎可能为空串）。 */
  created: string
}

/** GET /docker/networks 响应。 */
export interface NetworkListResponse {
  networks: NetworkSummary[]
}

/** POST /docker/networks 请求体。 */
export interface NetworkCreateRequest {
  name: string
  driver: NetworkDriver
  internal: boolean
}

/** 卷摘要（GET /docker/volumes 列表项）。 */
export interface VolumeSummary {
  name: string
  driver: string
  mountpoint: string
  /** ISO 时间字符串（部分引擎可能为空串）。 */
  created: string
  scope: string
  labels: Record<string, string>
}

/** GET /docker/volumes 响应。 */
export interface VolumeListResponse {
  volumes: VolumeSummary[]
}

/** GET /docker/info：守护进程概要；不可用时返回 { available: false } 而非报错。 */
export async function getDockerInfo(): Promise<DockerInfo> {
  const { data } = await client.get<DockerInfo>('/docker/info')
  return data
}

/** GET /docker/containers：容器列表（默认仅运行中，all=true 含已停止）。 */
export async function listContainers(all = false): Promise<ContainerListResponse> {
  const { data } = await client.get<ContainerListResponse>('/docker/containers', {
    params: { all },
  })
  return data
}

/** POST /docker/containers/{id}/{action}：容器生命周期操作（remove 需要近期重认证）。 */
export async function containerAction(
  id: string,
  action: ContainerLifecycleAction,
  force = false,
): Promise<void> {
  await client.post(`/docker/containers/${encodeURIComponent(id)}/${action}`, { force })
}

/** GET /docker/containers/{id}/logs：最近 tail 行日志快照。 */
export async function getContainerLogs(id: string, tail = 200): Promise<ContainerLogsResponse> {
  const { data } = await client.get<ContainerLogsResponse>(
    `/docker/containers/${encodeURIComponent(id)}/logs`,
    { params: { tail } },
  )
  return data
}

/** 容器日志实时 SSE 地址（EventSource 直连，含 /api/v1 前缀；事件名为 log/eof）。 */
export function containerLogStreamUrl(id: string, tail = 200): string {
  return `/api/v1/docker/containers/${encodeURIComponent(id)}/logs/stream?tail=${tail}`
}

/** GET /docker/containers/{id}：精简 inspect（详情页展示用）。 */
export async function getContainerInspect(id: string): Promise<ContainerInspect> {
  const { data } = await client.get<ContainerInspect>(
    `/docker/containers/${encodeURIComponent(id)}`,
  )
  return data
}

/** GET /docker/containers/{id}/stats：一次性资源快照（详情页 ~2s 轮询）。 */
export async function getContainerStats(id: string): Promise<ContainerStats> {
  const { data } = await client.get<ContainerStats>(
    `/docker/containers/${encodeURIComponent(id)}/stats`,
  )
  return data
}

/** GET /docker/stats：全部运行中容器的资源快照，按完整容器 id 索引（列表页 ~8s 轮询）。 */
export async function getAllContainerStats(): Promise<AllContainerStatsResponse> {
  const { data } = await client.get<AllContainerStatsResponse>('/docker/stats')
  return data
}

/** 容器 exec 终端 WebSocket 地址：同源 /api/v1，协议随页面协议切换（https → wss）。 */
export function containerExecWsUrl(id: string): string {
  const scheme = window.location.protocol === 'https:' ? 'wss' : 'ws'
  return `${scheme}://${window.location.host}/api/v1/docker/containers/${encodeURIComponent(id)}/exec`
}

/** GET /docker/images：镜像列表。 */
export async function listImages(): Promise<ImageListResponse> {
  const { data } = await client.get<ImageListResponse>('/docker/images')
  return data
}

/** POST /docker/images/pull：提交镜像拉取后台任务，返回任务 id。 */
export async function pullImage(body: ImagePullRequest): Promise<JobAcceptedResponse> {
  const { data } = await client.post<JobAcceptedResponse>('/docker/images/pull', body)
  return data
}

/** DELETE /docker/images/{ref}：按 id 或 repo:tag 删除镜像（被容器引用时 409）。 */
export async function removeImage(ref: string, force = false): Promise<void> {
  await client.delete(`/docker/images/${encodeURIComponent(ref)}`, { params: { force } })
}

/** GET /docker/networks：网络列表。 */
export async function listNetworks(): Promise<NetworkListResponse> {
  const { data } = await client.get<NetworkListResponse>('/docker/networks')
  return data
}

/** POST /docker/networks：创建自定义网络。 */
export async function createNetwork(body: NetworkCreateRequest): Promise<void> {
  await client.post('/docker/networks', body)
}

/** DELETE /docker/networks/{id}：按 id 或名称删除网络（内置网络会被引擎拒绝）。 */
export async function removeNetwork(id: string): Promise<void> {
  await client.delete(`/docker/networks/${encodeURIComponent(id)}`)
}

/** GET /docker/volumes：卷列表。 */
export async function listVolumes(): Promise<VolumeListResponse> {
  const { data } = await client.get<VolumeListResponse>('/docker/volumes')
  return data
}

/** POST /docker/volumes：创建命名卷。 */
export async function createVolume(name: string): Promise<void> {
  await client.post('/docker/volumes', { name })
}

/** DELETE /docker/volumes/{name}：删除卷（仍被容器使用时 409）。 */
export async function removeVolume(name: string, force = false): Promise<void> {
  await client.delete(`/docker/volumes/${encodeURIComponent(name)}`, { params: { force } })
}

/** GET /docker/daemon-config 响应：daemon.json 原文、解析结果与是否待重启。 */
export interface DaemonConfig {
  path: string
  exists: boolean
  /** 文件原文（解析失败时也照原样返回，供编辑器修复）。 */
  content: string
  /** 解析后的对象；文件缺失或非法时为 {}。 */
  parsed: Record<string, unknown>
  /** 解析失败原因；成功时为空串。 */
  parse_error: string
  /** 文件修改时间（epoch 秒），文件不存在时为 0。 */
  mtime: number
  /** 配置比运行中的守护进程更新，即改动尚未生效。 */
  restart_pending: boolean
}

/** PUT /docker/daemon-config 响应。 */
export interface DaemonConfigWriteResult {
  ok: boolean
  path: string
  /** 规范化后实际写入的内容。 */
  content: string
  /** 本次生成的备份文件名；此前无配置时为空串。 */
  backup: string
}

/** GET /docker/daemon-config：读取 /etc/docker/daemon.json。 */
export async function getDaemonConfig(): Promise<DaemonConfig> {
  const { data } = await client.get<DaemonConfig>('/docker/daemon-config')
  return data
}

/** PUT /docker/daemon-config：写入配置（需 CSRF + 近期重认证）。写入不影响运行中的守护进程。 */
export async function putDaemonConfig(content: string): Promise<DaemonConfigWriteResult> {
  const { data } = await client.put<DaemonConfigWriteResult>('/docker/daemon-config', { content })
  return data
}

/** POST /docker/daemon-restart：重启 dockerd 使配置生效（会重启容器，除非开启 live-restore）。 */
export async function restartDaemon(): Promise<void> {
  await client.post('/docker/daemon-restart')
}

/** 判断错误是否为 503 docker.unavailable（守护进程不可用，页面应显示空态而非报错）。 */
export function isDockerUnavailable(error: unknown): boolean {
  return (error as ApiError | undefined)?.problem?.code === 'docker.unavailable'
}

/** 容器日志流 SSE `log` 事件 → 显示行；其他事件（如 eof）返回 null 丢弃。 */
export function dockerLogLine(data: unknown): string | null {
  if (data && typeof data === 'object' && typeof (data as { line?: unknown }).line === 'string') {
    return (data as { line: string }).line
  }
  return null
}

/** 把端口映射数组格式化为「宿主端口->容器端口/协议」串（IPv4/IPv6 重复项去重）。 */
export function formatPorts(ports: ContainerPort[]): string {
  const parts = new Set<string>()
  for (const port of ports) {
    if (port.PublicPort !== undefined) {
      const host = port.IP && port.IP !== '0.0.0.0' && port.IP !== '::' ? `${port.IP}:` : ''
      parts.add(`${host}${port.PublicPort}->${port.PrivatePort}/${port.Type}`)
    } else {
      parts.add(`${port.PrivatePort}/${port.Type}`)
    }
  }
  return [...parts].join(', ')
}

/** 字节数 → 人性化大小（如 1.5 GB）。 */
export function formatBytes(size: number): string {
  if (!Number.isFinite(size) || size < 0) {
    return '未知'
  }
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let value = size
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024
    unit += 1
  }
  const text = unit === 0 || value >= 100 ? String(Math.round(value)) : value.toFixed(1)
  return `${text} ${units[unit] ?? 'B'}`
}

/** 截短对象 id（去掉 sha256: 前缀，保留前 12 位）。 */
export function shortId(id: string): string {
  return id.replace(/^sha256:/, '').slice(0, 12)
}
