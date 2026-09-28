import { client } from './client'

/** 站点摘要（GET /website/sites 列表项）。 */
export interface SiteSummary {
  name: string
  /** 是否已在 sites-enabled 建立软链接（启用）。 */
  enabled: boolean
  /** 解析自配置文件的 server_name 值（尽力而为）。 */
  server_names: string[]
  /** 解析自配置文件的 listen 端口/地址（尽力而为）。 */
  listens: string[]
  /** 解析自配置文件的 proxy_pass 目标（站点实际转发到的后端服务；静态站为空）。 */
  upstreams: string[]
}

/** GET /website/sites 响应。 */
export interface SiteListResponse {
  sites: SiteSummary[]
}

/** GET /website/sites/{name} 响应：站点原始配置文本。 */
export interface SiteContent {
  name: string
  content: string
  enabled: boolean
}

/** POST /website/sites 请求体（新建或覆盖）。 */
export interface SiteWriteRequest {
  name: string
  content: string
}

/** 启用/停用软链接切换结果。 */
export interface SiteToggleResponse {
  name: string
  enabled: boolean
  changed: boolean
}

/** POST /website/test 响应：nginx -t 结果。 */
export interface NginxTestResult {
  ok: boolean
  output: string
}

/** POST /website/reload 响应：先测试再重载。 */
export interface NginxReloadResult {
  ok: boolean
  reloaded: boolean
  output: string
}

/** GET /website/status 响应：nginx 服务状态。 */
export interface NginxStatus {
  active: string
  enabled: string
  version: string
}

/** 证书摘要（GET /website/certs 列表项）。 */
export interface CertSummary {
  /** 证书名（文件名去扩展名，如 example）。 */
  name: string
  /** 证书文件名（如 example.crt）。 */
  file: string
  /** 是否存在同名私钥 <name>.key。 */
  has_key: boolean
  /** 证书主题（openssl 解析，如 CN=example.com）。 */
  subject: string
  /** 到期时间（ISO；无法解析时为空串）。 */
  expires: string
  /** openssl 原始 notAfter 字符串（兜底展示）。 */
  not_after: string
}

/** GET /website/certs 响应。 */
export interface CertListResponse {
  certs: CertSummary[]
  /** 证书目录绝对路径，用于生成 ssl_certificate 路径。 */
  cert_dir: string
}

/** POST /website/certs 请求体：上传证书 + 私钥（PEM 文本）。 */
export interface CertUploadRequest {
  name: string
  cert_pem: string
  key_pem: string
}

/** POST /website/certs/self-signed 请求体：生成自签证书。 */
export interface SelfSignedRequest {
  name: string
  domain: string
  days: number
}

/** GET /website/sites：站点列表（含启用标记与解析出的域名/端口）。 */
export async function listSites(): Promise<SiteListResponse> {
  const { data } = await client.get<SiteListResponse>('/website/sites')
  return data
}

/** GET /website/sites/{name}：读取站点原始配置。 */
export async function getSite(name: string): Promise<SiteContent> {
  const { data } = await client.get<SiteContent>(`/website/sites/${encodeURIComponent(name)}`)
  return data
}

/** POST /website/sites：新建或覆盖站点配置（不自动重载）。 */
export async function writeSite(body: SiteWriteRequest): Promise<{ name: string }> {
  const { data } = await client.post<{ name: string }>('/website/sites', body)
  return data
}

/** POST /website/sites/{name}/{enable|disable}：启用或停用站点。 */
export async function toggleSite(
  name: string,
  action: 'enable' | 'disable',
): Promise<SiteToggleResponse> {
  const { data } = await client.post<SiteToggleResponse>(
    `/website/sites/${encodeURIComponent(name)}/${action}`,
  )
  return data
}

/** DELETE /website/sites/{name}：删除站点（同时移除软链接）。 */
export async function deleteSite(name: string): Promise<void> {
  await client.delete(`/website/sites/${encodeURIComponent(name)}`)
}

/** POST /website/test：执行 nginx -t 校验配置。 */
export async function testNginx(): Promise<NginxTestResult> {
  const { data } = await client.post<NginxTestResult>('/website/test')
  return data
}

/** POST /website/reload：先 nginx -t 再 systemctl reload（校验失败则不重载）。 */
export async function reloadNginx(): Promise<NginxReloadResult> {
  const { data } = await client.post<NginxReloadResult>('/website/reload')
  return data
}

/** GET /website/status：nginx 运行/自启状态与版本。 */
export async function getNginxStatus(): Promise<NginxStatus> {
  const { data } = await client.get<NginxStatus>('/website/status')
  return data
}

/** GET /website/certs：证书列表（含证书目录）。 */
export async function listCerts(): Promise<CertListResponse> {
  const { data } = await client.get<CertListResponse>('/website/certs')
  return data
}

/** POST /website/certs：上传证书 + 私钥。 */
export async function uploadCert(body: CertUploadRequest): Promise<{ name: string }> {
  const { data } = await client.post<{ name: string }>('/website/certs', body)
  return data
}

/** POST /website/certs/self-signed：生成自签证书。 */
export async function generateSelfSigned(body: SelfSignedRequest): Promise<{ name: string }> {
  const { data } = await client.post<{ name: string }>('/website/certs/self-signed', body)
  return data
}

/** DELETE /website/certs/{name}：删除证书及其私钥。 */
export async function deleteCert(name: string): Promise<void> {
  await client.delete(`/website/certs/${encodeURIComponent(name)}`)
}
