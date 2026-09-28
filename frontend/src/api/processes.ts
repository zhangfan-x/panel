import { client } from './client'

/** 进程列表行（GET /processes 列表项，字段与后端 psutil 采集逐字一致）。 */
export interface ProcessRow {
  pid: number
  ppid: number
  user: string
  /** 两次列表请求之间的 CPU 平均占用百分比（首次请求为 0）。 */
  cpu: number
  /** 内存占用百分比。 */
  mem: number
  /** psutil 进程状态（running/sleeping/zombie 等）。 */
  status: string
  /** 进程启动时间（ISO 字符串）。 */
  started: string
  name: string
  cmdline: string
}

/** 进程详情（GET /processes/{pid}，在列表字段基础上附加资源占用与父子关系）。 */
export interface ProcessDetail extends ProcessRow {
  /** nice 值（-20..19，越小优先级越高）。 */
  nice: number
  num_threads: number
  /** 常驻内存字节数。 */
  rss: number
  /** 虚拟内存字节数。 */
  vms: number
  /** 可执行文件路径（无权限读取时为空字符串）。 */
  exe: string
  /** 工作目录（无权限读取时为空字符串）。 */
  cwd: string
  /** 直接子进程的 pid 列表。 */
  children: number[]
}

/** GET /processes 响应。 */
export interface ProcessListResponse {
  processes: ProcessRow[]
  total: number
}

/** 服务端支持的排序字段（与后端校验一致；user/status 不支持服务端排序）。 */
export type ProcessSortField = 'pid' | 'cpu' | 'mem' | 'name'

/** 排序方向。 */
export type ProcessSortOrder = 'asc' | 'desc'

/** 允许发送的进程信号（后端仅接受这两种）。 */
export type ProcessSignal = 'SIGTERM' | 'SIGKILL'

/** GET /processes 查询参数。 */
export interface ListProcessesParams {
  /** 按进程名或命令行做不区分大小写的子串搜索。 */
  search?: string
  sort?: ProcessSortField
  order?: ProcessSortOrder
  limit?: number
  offset?: number
}

/** GET /processes：进程列表（搜索/排序/分页均在服务端完成）。 */
export async function listProcesses(
  params: ListProcessesParams = {},
): Promise<ProcessListResponse> {
  const { data } = await client.get<ProcessListResponse>('/processes', { params })
  return data
}

/** GET /processes/{pid}：单个进程详情（含直接子进程 pid）。 */
export async function getProcess(pid: number): Promise<ProcessDetail> {
  const { data } = await client.get<ProcessDetail>(`/processes/${pid}`)
  return data
}

/** POST /processes/{pid}/signal：发送 SIGTERM/SIGKILL（需近期重认证）。 */
export async function signalProcess(pid: number, signal: ProcessSignal): Promise<{ ok: boolean }> {
  const { data } = await client.post<{ ok: boolean }>(`/processes/${pid}/signal`, { signal })
  return data
}

/** POST /processes/{pid}/renice：调整进程 nice 值（-20..19）。 */
export async function reniceProcess(pid: number, nice: number): Promise<{ nice: number }> {
  const { data } = await client.post<{ nice: number }>(`/processes/${pid}/renice`, { nice })
  return data
}
