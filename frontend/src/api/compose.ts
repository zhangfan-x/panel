import { client } from './client'

/** Compose 项目摘要（GET /compose/projects 列表项，合并已导入与自动发现）。 */
export interface ComposeProjectSummary {
  name: string
  path: string
  /** true 表示已写入 compose_projects 表；false 为根目录下自动发现。 */
  imported: boolean
  has_compose_file: boolean
  /** 识别到的 compose 文件名（compose.yaml / compose.yml / docker-compose.yml）。 */
  compose_file: string | null
  /** 仅已导入项目有值（ISO 时间）。 */
  created_at: string | null
}

/** GET /compose/projects 响应。 */
export interface ComposeProjectListResponse {
  projects: ComposeProjectSummary[]
  /** 服务端配置的项目根目录（settings.docker.compose_root）。 */
  compose_root: string
}

/** compose ps 中单个服务容器的运行状态。 */
export interface ComposePsService {
  name: string
  state: string
  status: string
  ports: string
}

/** GET /compose/projects/{name} 响应：配置校验结果 + 运行状态。 */
export interface ComposeProjectDetail {
  name: string
  path: string
  imported: boolean
  has_compose_file: boolean
  compose_file: string | null
  /** null 表示无法判定（无 compose 文件）。 */
  config_valid: boolean | null
  /** 校验方式：docker CLI 存在时为 'docker'，否则退化为纯 YAML 解析 'yaml'。 */
  config_checked_by: 'docker' | 'yaml' | null
  config_error: string | null
  /** compose 文件中声明的服务名（已排序）。 */
  services: string[]
  ps: ComposePsService[]
  /** false 表示 docker CLI / 守护进程不可用，ps 为空但配置信息仍有效。 */
  docker_available: boolean
}

/** POST /compose/projects 请求体。 */
export interface ComposeImportRequest {
  name: string
  path: string
}

/** POST /compose/projects 响应。 */
export interface ComposeImportResponse {
  name: string
  path: string
  imported: boolean
}

/** GET /compose/projects/{name}/file 响应。 */
export interface ComposeFileResponse {
  name: string
  path: string
  file: string
  content: string
  /** 乐观并发标识，保存时经 expected_etag 传回。 */
  etag: string
}

/** PUT /compose/projects/{name}/file 请求体。 */
export interface ComposeFileWriteRequest {
  content: string
  /** 省略时写入现有文件（无文件时新建 compose.yaml）。 */
  file?: string
  /** 传入读取时的 etag 可防止覆盖磁盘上的并发修改；null 表示不校验。 */
  expected_etag?: string | null
}

/** PUT /compose/projects/{name}/file 响应。 */
export interface ComposeFileWriteResponse {
  file: string
  etag: string
}

/** Compose 生命周期动作（均以后台任务执行）。 */
export type ComposeVerb = 'up' | 'down' | 'restart'

/** 202 任务受理响应。 */
export interface ComposeJobResponse {
  job_id: string
}

/** GET /compose/projects：项目列表（数据库导入项 + 根目录自动发现项）。 */
export async function listComposeProjects(): Promise<ComposeProjectListResponse> {
  const { data } = await client.get<ComposeProjectListResponse>('/compose/projects')
  return data
}

/** POST /compose/projects：把根目录下的已有目录导入为命名项目。 */
export async function importComposeProject(
  body: ComposeImportRequest,
): Promise<ComposeImportResponse> {
  const { data } = await client.post<ComposeImportResponse>('/compose/projects', body)
  return data
}

/** GET /compose/projects/{name}：项目详情（配置校验 + 服务运行状态）。 */
export async function getComposeProject(name: string): Promise<ComposeProjectDetail> {
  const { data } = await client.get<ComposeProjectDetail>(
    `/compose/projects/${encodeURIComponent(name)}`,
  )
  return data
}

/** GET /compose/projects/{name}/file：读取 compose 文件内容与 etag。 */
export async function getComposeFile(name: string): Promise<ComposeFileResponse> {
  const { data } = await client.get<ComposeFileResponse>(
    `/compose/projects/${encodeURIComponent(name)}/file`,
  )
  return data
}

/** PUT /compose/projects/{name}/file：校验 YAML 后原子替换 compose 文件。 */
export async function putComposeFile(
  name: string,
  body: ComposeFileWriteRequest,
): Promise<ComposeFileWriteResponse> {
  const { data } = await client.put<ComposeFileWriteResponse>(
    `/compose/projects/${encodeURIComponent(name)}/file`,
    body,
  )
  return data
}

/** POST /compose/projects/{name}/{up|down|restart}：提交生命周期任务，返回任务 id。 */
export async function runComposeAction(
  name: string,
  action: ComposeVerb,
): Promise<ComposeJobResponse> {
  const { data } = await client.post<ComposeJobResponse>(
    `/compose/projects/${encodeURIComponent(name)}/${action}`,
  )
  return data
}

/** DELETE /compose/projects/{name}：移除导入记录（目录与容器不受影响；需重认证）。 */
export async function deleteComposeProject(name: string): Promise<void> {
  await client.delete(`/compose/projects/${encodeURIComponent(name)}`)
}
