import { client } from './client'

/** 文件条目类型（与后端 helper `_file_type` 输出逐字一致）。 */
export type FileType = 'file' | 'dir' | 'symlink' | 'socket' | 'fifo' | 'block' | 'char' | 'unknown'

/** 目录条目（GET /files/list 的 entries 项；lstat 语义，不跟随符号链接）。 */
export interface FileEntry {
  name: string
  type: FileType
  /** 字节数（目录为目录项自身大小）。 */
  size: number
  /** 权限位（S_IMODE 数值，展示时转八进制）。 */
  mode: number
  uid: number
  gid: number
  /** 修改时间（Unix 秒，浮点）。 */
  mtime: number
}

/** GET /files/list 响应。 */
export interface FileListResponse {
  path: string
  entries: FileEntry[]
}

/** GET /files/stat 响应（符号链接额外携带 target，且不会被跟随）。 */
export interface FileStatResponse {
  path: string
  type: FileType
  size: number
  mode: number
  uid: number
  gid: number
  mtime: number
  target?: string
}

/** GET /files/content 响应（文本预览/编辑；超出上限的文件后端以 413 拒绝）。 */
export interface FileContentResponse {
  content: string
  /** 乐观并发标识（"size-mtime_ns"），保存时经 expected_etag 传回。 */
  etag: string
  truncated: boolean
}

/** PUT /files/content 请求体。 */
export interface WriteContentRequest {
  path: string
  content: string
  /** 传读取时的 etag 可防覆盖并发修改（不符时 409）；null/省略为无条件写入。 */
  expected_etag?: string | null
}

/** PUT /files/content 响应。 */
export interface WriteContentResponse {
  etag: string
}

/** POST /files/upload 响应。 */
export interface UploadResponse {
  path: string
  etag: string
}

/** POST /files/mkdir 请求体。 */
export interface MkdirRequest {
  path: string
  mode?: number
  parents?: boolean
}

/** POST /files/rename 请求体（目标已存在时 409）。 */
export interface RenameRequest {
  src: string
  dst: string
}

/** POST /files/copy 请求体（recursive 目录复制转后台任务）。 */
export interface CopyRequest {
  src: string
  dst: string
  recursive: boolean
}

/** POST /files/copy 响应：同步复制返回 ok，递归复制返回 202 + job_id。 */
export interface CopyResponse {
  ok?: boolean
  job_id?: string
}

/** POST /files/chmod 请求体（mode 为数值权限位，0–0o7777）。 */
export interface ChmodRequest {
  path: string
  mode: number
}

/** POST /files/chown 请求体（-1 表示保持现值）。 */
export interface ChownRequest {
  path: string
  uid: number
  gid: number
}

/** DELETE /files 请求体（危险操作，需近期重认证）。 */
export interface DeleteRequest {
  path: string
  recursive: boolean
}

/** 打包/解压支持的归档格式。 */
export type ArchiveFormat = 'zip' | 'tar' | 'tar.gz'

/** POST /files/archive 请求体。 */
export interface ArchiveRequest {
  paths: string[]
  dst: string
  format: ArchiveFormat
}

/** POST /files/extract 请求体。 */
export interface ExtractRequest {
  archive: string
  dst_dir: string
  format: ArchiveFormat
}

/** 202 任务受理响应。 */
export interface FilesJobResponse {
  job_id: string
}

/** GET /files/list：目录列表（show_hidden 控制是否包含点号开头的条目）。 */
export async function listDir(path: string, showHidden = false): Promise<FileListResponse> {
  const { data } = await client.get<FileListResponse>('/files/list', {
    params: { path, show_hidden: showHidden },
  })
  return data
}

/** GET /files/stat：查看单个路径的 lstat 信息。 */
export async function statPath(path: string): Promise<FileStatResponse> {
  const { data } = await client.get<FileStatResponse>('/files/stat', { params: { path } })
  return data
}

/** GET /files/content：读取文本内容与 etag（编辑器/预览用）。 */
export async function getContent(path: string): Promise<FileContentResponse> {
  const { data } = await client.get<FileContentResponse>('/files/content', { params: { path } })
  return data
}

/** PUT /files/content：原子保存文本内容；expected_etag 过期时后端返回 409 conflict。 */
export async function putContent(body: WriteContentRequest): Promise<WriteContentResponse> {
  const { data } = await client.put<WriteContentResponse>('/files/content', body)
  return data
}

/** POST /files/upload：multipart 上传到目标文件路径（path 为完整目标路径）。 */
export async function uploadFile(path: string, file: File): Promise<UploadResponse> {
  const form = new FormData()
  form.append('path', path)
  form.append('file', file)
  const { data } = await client.post<UploadResponse>('/files/upload', form)
  return data
}

/** POST /files/mkdir：创建目录（已存在时 409）。 */
export async function mkdir(body: MkdirRequest): Promise<void> {
  await client.post('/files/mkdir', body)
}

/** POST /files/rename：重命名/移动（同一文件系统内）。 */
export async function renamePath(body: RenameRequest): Promise<void> {
  await client.post('/files/rename', body)
}

/** POST /files/copy：复制文件；recursive 目录复制以后台任务执行并返回 job_id。 */
export async function copyPath(body: CopyRequest): Promise<CopyResponse> {
  const { data } = await client.post<CopyResponse>('/files/copy', body)
  return data
}

/** POST /files/chmod：修改权限位。 */
export async function chmodPath(body: ChmodRequest): Promise<void> {
  await client.post('/files/chmod', body)
}

/** POST /files/chown：修改属主/属组（-1 保持现值）。 */
export async function chownPath(body: ChownRequest): Promise<void> {
  await client.post('/files/chown', body)
}

/** DELETE /files：删除文件/目录（需近期重认证，符号链接只删链接本身）。 */
export async function deletePath(body: DeleteRequest): Promise<void> {
  await client.delete('/files', { data: body })
}

/** POST /files/archive：把多个路径打包为归档（后台任务，202）。 */
export async function archivePaths(body: ArchiveRequest): Promise<FilesJobResponse> {
  const { data } = await client.post<FilesJobResponse>('/files/archive', body)
  return data
}

/** POST /files/extract：解压归档到目录（后台任务，202）。 */
export async function extractArchive(body: ExtractRequest): Promise<FilesJobResponse> {
  const { data } = await client.post<FilesJobResponse>('/files/extract', body)
  return data
}

/** 文件下载地址（浏览器直接跳转/锚点下载，含 /api/v1 前缀，凭 Cookie 鉴权）。目录由后端即时打包为 zip 流。 */
export function downloadUrl(path: string): string {
  return `/api/v1/files/download?path=${encodeURIComponent(path)}`
}

/** 一次可打包下载的条目上限（与后端 _DOWNLOAD_ZIP_MAX_ITEMS 一致）。 */
export const DOWNLOAD_ZIP_MAX_ITEMS = 100

/**
 * 多选打包下载地址：把 `dir` 下的若干条目即时打包成 zip 流。
 *
 * 传「父目录 + 各条目名」而非多个完整路径，URL 短得多，后端也能直接拒绝
 * 含 `/` 或 `..` 的名字。不落盘，与「归档」后台任务的用途不同。
 */
export function downloadZipUrl(dir: string, names: string[]): string {
  const params = new URLSearchParams()
  params.set('path', dir)
  for (const name of names) {
    params.append('name', name)
  }
  return `/api/v1/files/download-zip?${params.toString()}`
}

/** 图片内联预览地址（inline=1 时后端按扩展名白名单回发正确的 image/* Content-Type）。 */
export function previewUrl(path: string): string {
  return `/api/v1/files/download?path=${encodeURIComponent(path)}&inline=1`
}

/** 可内联预览的图片扩展名（与后端 _INLINE_IMAGE_MIME 白名单一致；SVG 属主动内容，刻意排除）。 */
const IMAGE_EXTENSIONS = new Set(['avif', 'bmp', 'gif', 'ico', 'jpeg', 'jpg', 'png', 'webp'])

/** 按文件名判断是否为可预览图片。 */
export function isImageName(name: string): boolean {
  const dot = name.lastIndexOf('.')
  if (dot <= 0) {
    return false
  }
  return IMAGE_EXTENSIONS.has(name.slice(dot + 1).toLowerCase())
}

/** 根据文件名推断归档格式；不是可识别的归档时返回 null。 */
export function guessArchiveFormat(name: string): ArchiveFormat | null {
  const lower = name.toLowerCase()
  if (lower.endsWith('.zip')) {
    return 'zip'
  }
  if (lower.endsWith('.tar.gz') || lower.endsWith('.tgz')) {
    return 'tar.gz'
  }
  if (lower.endsWith('.tar')) {
    return 'tar'
  }
  return null
}
