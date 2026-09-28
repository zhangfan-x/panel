<script setup lang="ts">
// 站点管理：sites-available 列表、启用/停用、可视化新建（生成 nginx server{} 块）
// 与原始配置编辑（Monaco 懒加载），以及 nginx -t 测试与重载。
import { computed, nextTick, onBeforeUnmount, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { useQuery } from '@tanstack/vue-query'

import type { ApiError } from '@/api/client'
import {
  deleteSite,
  getNginxStatus,
  getSite,
  listCerts,
  listSites,
  reloadNginx,
  testNginx,
  toggleSite,
  writeSite,
} from '@/api/website'
import type { SiteSummary } from '@/api/website'
import { problemMessage } from '@/api/problem'
import ConfirmDangerDialog from '@/components/ConfirmDangerDialog.vue'

const NAME_RE = /^[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}$/

const { data, isPending, isError, error, refetch } = useQuery({
  queryKey: ['website-sites'],
  queryFn: listSites,
})
const certsQuery = useQuery({ queryKey: ['website-certs'], queryFn: listCerts })
const statusQuery = useQuery({
  queryKey: ['website-status'],
  queryFn: getNginxStatus,
  retry: false,
})

const errorMessage = computed(() => problemMessage(error.value, '获取站点列表失败'))
const sites = computed(() => data.value?.sites ?? [])
const certOptions = computed(() => certsQuery.data.value?.certs ?? [])
const certDir = computed(() => certsQuery.data.value?.cert_dir ?? '/etc/nginx/mypanel-certs')
const status = computed(() => statusQuery.data.value)

function problemCode(err: unknown): string | undefined {
  return (err as ApiError | undefined)?.problem?.code
}

// —— 新建 / 编辑对话框 ——

const dialogVisible = ref(false)
const editing = ref<string | null>(null)
const activeTab = ref<'visual' | 'raw'>('visual')
const saving = ref(false)
const rawContent = ref('')

const form = reactive({
  name: '',
  type: 'static' as 'static' | 'proxy',
  serverName: '',
  listen: '80',
  root: '/var/www/html',
  proxyPass: 'http://127.0.0.1:3000',
  certName: '',
})

/** 由可视化表单生成一个 nginx server{} 配置块。 */
function buildConfig(): string {
  const names =
    form.serverName
      .trim()
      .split(/[\s,]+/)
      .filter(Boolean)
      .join(' ') || '_'
  const ssl = form.certName !== ''
  const lines: string[] = ['server {', `    listen ${form.listen || '80'};`]
  if (ssl) {
    lines.push('    listen 443 ssl;')
  }
  lines.push(`    server_name ${names};`)
  if (ssl) {
    lines.push(`    ssl_certificate ${certDir.value}/${form.certName}.crt;`)
    lines.push(`    ssl_certificate_key ${certDir.value}/${form.certName}.key;`)
  }
  if (form.type === 'static') {
    lines.push(`    root ${form.root || '/var/www/html'};`, '    index index.html index.htm;', '')
    lines.push('    location / {', '        try_files $uri $uri/ =404;', '    }')
  } else {
    lines.push(
      '',
      '    location / {',
      `        proxy_pass ${form.proxyPass || 'http://127.0.0.1:3000'};`,
    )
    lines.push('        proxy_set_header Host $host;')
    lines.push('        proxy_set_header X-Real-IP $remote_addr;')
    lines.push('        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;')
    lines.push('        proxy_set_header X-Forwarded-Proto $scheme;')
    lines.push('    }')
  }
  lines.push('}')
  return lines.join('\n') + '\n'
}

function resetForm(): void {
  form.name = ''
  form.type = 'static'
  form.serverName = ''
  form.listen = '80'
  form.root = '/var/www/html'
  form.proxyPass = 'http://127.0.0.1:3000'
  form.certName = ''
}

function openCreate(): void {
  editing.value = null
  resetForm()
  activeTab.value = 'visual'
  setEditorContent(buildConfig())
  dialogVisible.value = true
}

async function openEdit(site: SiteSummary): Promise<void> {
  editing.value = site.name
  resetForm()
  form.name = site.name
  activeTab.value = 'raw'
  setEditorContent('')
  dialogVisible.value = true
  try {
    const res = await getSite(site.name)
    setEditorContent(res.content)
  } catch (err) {
    ElMessage.error(problemMessage(err, '读取站点配置失败'))
    dialogVisible.value = false
  }
}

/** 把可视化表单生成的配置写入「配置文件」标签并切换过去。 */
function applyFormToConfig(): void {
  setEditorContent(buildConfig())
  activeTab.value = 'raw'
}

async function saveSite(): Promise<void> {
  const name = editing.value ?? form.name.trim()
  if (!NAME_RE.test(name)) {
    ElMessage.error('站点名称需以字母或数字开头，仅可含字母、数字与 _ . @ -')
    return
  }
  if (!rawContent.value.trim()) {
    ElMessage.error('配置内容不能为空')
    return
  }
  saving.value = true
  try {
    await writeSite({ name, content: rawContent.value })
    ElMessage.success(editing.value ? '站点配置已保存' : '站点已创建（记得测试并重载）')
    dialogVisible.value = false
    await refetch()
  } catch (err) {
    if (problemCode(err) === 'conflict') {
      ElMessage.error('该名称已被目录占用，请换一个名称')
    } else {
      ElMessage.error(problemMessage(err, '保存站点失败'))
    }
  } finally {
    saving.value = false
  }
}

// —— 启用 / 停用 ——

const toggleBusy = ref<string | null>(null)

async function handleToggle(site: SiteSummary): Promise<void> {
  toggleBusy.value = site.name
  try {
    await toggleSite(site.name, site.enabled ? 'disable' : 'enable')
    ElMessage.success(site.enabled ? '已停用（记得重载生效）' : '已启用（记得重载生效）')
    await refetch()
  } catch (err) {
    ElMessage.error(problemMessage(err, '切换站点状态失败'))
  } finally {
    toggleBusy.value = null
  }
}

// —— 删除 ——

const deleteVisible = ref(false)
const deleteLoading = ref(false)
const deleteTarget = ref<SiteSummary | null>(null)

function openDelete(site: SiteSummary): void {
  deleteTarget.value = site
  deleteVisible.value = true
}

async function handleDelete(): Promise<void> {
  const target = deleteTarget.value
  if (!target) {
    return
  }
  deleteLoading.value = true
  try {
    await deleteSite(target.name)
    ElMessage.success('站点已删除')
    deleteVisible.value = false
    await refetch()
  } catch (err) {
    ElMessage.error(problemMessage(err, '删除站点失败'))
  } finally {
    deleteLoading.value = false
  }
}

// —— nginx -t 测试 / 重载 ——

const outputVisible = ref(false)
const outputTitle = ref('')
const outputOk = ref(true)
const outputText = ref('')
const testing = ref(false)
const reloading = ref(false)

function showOutput(title: string, ok: boolean, text: string): void {
  outputTitle.value = title
  outputOk.value = ok
  outputText.value = text || '（无输出）'
  outputVisible.value = true
}

async function runTest(): Promise<void> {
  testing.value = true
  try {
    const res = await testNginx()
    showOutput('配置测试结果', res.ok, res.output)
  } catch (err) {
    ElMessage.error(problemMessage(err, '测试配置失败'))
  } finally {
    testing.value = false
  }
}

async function runReload(): Promise<void> {
  reloading.value = true
  try {
    const res = await reloadNginx()
    if (res.ok) {
      ElMessage.success('nginx 已重载')
      await statusQuery.refetch()
    } else {
      showOutput('配置校验未通过，未重载', false, res.output)
    }
  } catch (err) {
    ElMessage.error(problemMessage(err, '重载 nginx 失败'))
  } finally {
    reloading.value = false
  }
}

// —— Monaco 懒加载（失败时退化为纯文本域） ——

let monacoModule: typeof import('monaco-editor') | null = null
let editor: import('monaco-editor').editor.IStandaloneCodeEditor | null = null
const editorHost = ref<HTMLElement | null>(null)
const editorFallback = ref(false)

async function loadMonaco(): Promise<typeof import('monaco-editor')> {
  if (monacoModule) {
    return monacoModule
  }
  const monaco = (await import(
    // @ts-expect-error -- monaco 的 esm 深路径未附带类型声明，运行时导出与包根类型一致
    'monaco-editor/esm/vs/editor/editor.main.js'
  )) as typeof import('monaco-editor')
  try {
    const worker = await import('monaco-editor/esm/vs/editor/editor.worker.js?worker')
    self.MonacoEnvironment = { getWorker: () => new worker.default() }
  } catch {
    // worker 加载失败时 monaco 自动退化为主线程模式。
  }
  monacoModule = monaco
  return monaco
}

async function mountEditor(): Promise<void> {
  if (editorFallback.value || editor || activeTab.value !== 'raw' || !dialogVisible.value) {
    return
  }
  try {
    const monaco = await loadMonaco()
    await nextTick()
    const host = editorHost.value
    if (!host || editor) {
      return
    }
    editor = monaco.editor.create(host, {
      value: rawContent.value,
      language: 'ini',
      automaticLayout: true,
      minimap: { enabled: false },
      fontSize: 13,
      tabSize: 4,
      scrollBeyondLastLine: false,
    })
    editor.onDidChangeModelContent(() => {
      rawContent.value = editor?.getValue() ?? ''
    })
  } catch {
    editorFallback.value = true
  }
}

function destroyEditor(): void {
  editor?.dispose()
  editor = null
}

onBeforeUnmount(destroyEditor)

/** 更新本地内容并同步进已创建的编辑器。 */
function setEditorContent(content: string): void {
  rawContent.value = content
  if (editor && editor.getValue() !== content) {
    editor.setValue(content)
  }
}

watch(activeTab, (tab) => {
  if (tab === 'raw') {
    void mountEditor()
  }
})

function onDialogClosed(): void {
  destroyEditor()
  editorFallback.value = false
}

function joinList(values: string[]): string {
  return values.length ? values.join(', ') : '—'
}

/** 后端服务展示：去掉 http(s):// 前缀，突出实际运行的地址与端口。 */
function formatUpstreams(values: string[]): string {
  return values.map((v) => v.replace(/^https?:\/\//, '')).join(', ')
}
</script>

<template>
  <div class="sites-page">
    <el-card shadow="never">
      <template #header>
        <div class="sites-page__header">
          <div>
            <span class="sites-page__title">站点</span>
            <el-tag
              v-if="status"
              size="small"
              :type="status.active === 'active' ? 'success' : 'info'"
              effect="plain"
            >
              nginx {{ status.active }}
            </el-tag>
            <span v-if="status?.version" class="sites-page__version">{{ status.version }}</span>
          </div>
          <div class="sites-page__actions">
            <el-button link type="primary" :loading="isPending" @click="() => refetch()"
              >刷新</el-button
            >
            <el-button :loading="testing" @click="runTest">测试配置</el-button>
            <el-button :loading="reloading" @click="runReload">重载</el-button>
            <el-button type="primary" @click="openCreate">新建站点</el-button>
          </div>
        </div>
      </template>

      <el-skeleton v-if="isPending" :rows="5" animated />
      <el-alert
        v-else-if="isError"
        type="error"
        :title="errorMessage"
        :closable="false"
        show-icon
      />
      <el-table v-else :data="sites">
        <template #empty>
          <el-empty description="还没有任何站点，点击右上角「新建站点」" />
        </template>
        <el-table-column label="名称" min-width="160">
          <template #default="{ row }">
            <span class="sites-page__name">{{ row.name }}</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag size="small" :type="row.enabled ? 'success' : 'info'" effect="plain">
              {{ row.enabled ? '已启用' : '未启用' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="域名" min-width="200">
          <template #default="{ row }">{{ joinList(row.server_names) }}</template>
        </el-table-column>
        <el-table-column label="监听" min-width="120">
          <template #default="{ row }">{{ joinList(row.listens) }}</template>
        </el-table-column>
        <el-table-column label="后端服务" min-width="160" show-overflow-tooltip>
          <template #default="{ row }">
            <span v-if="row.upstreams?.length">{{ formatUpstreams(row.upstreams) }}</span>
            <span v-else class="sites-page__static">静态</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="240" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="openEdit(row)">编辑</el-button>
            <el-button
              link
              :type="row.enabled ? 'warning' : 'success'"
              :loading="toggleBusy === row.name"
              @click="handleToggle(row)"
            >
              {{ row.enabled ? '停用' : '启用' }}
            </el-button>
            <el-button link type="danger" @click="openDelete(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 新建 / 编辑站点 -->
    <el-dialog
      v-model="dialogVisible"
      :title="editing ? `编辑站点 ${editing}` : '新建站点'"
      width="820px"
      top="6vh"
      :close-on-click-modal="false"
      @closed="onDialogClosed"
    >
      <el-form label-position="top">
        <el-form-item label="站点名称（文件名）">
          <el-input v-model="form.name" :disabled="editing !== null" placeholder="如 example.com" />
        </el-form-item>
      </el-form>

      <el-tabs v-model="activeTab" class="sites-page__tabs">
        <el-tab-pane label="可视化" name="visual">
          <el-form label-position="top" class="sites-page__form">
            <el-form-item label="类型">
              <el-radio-group v-model="form.type">
                <el-radio-button value="static">静态站点</el-radio-button>
                <el-radio-button value="proxy">反向代理</el-radio-button>
              </el-radio-group>
            </el-form-item>
            <el-form-item label="域名 server_name（空格或逗号分隔）">
              <el-input v-model="form.serverName" placeholder="example.com www.example.com" />
            </el-form-item>
            <el-form-item label="监听端口 listen">
              <el-input v-model="form.listen" placeholder="80" />
            </el-form-item>
            <el-form-item v-if="form.type === 'static'" label="网站根目录 root">
              <el-input v-model="form.root" placeholder="/var/www/example" />
            </el-form-item>
            <el-form-item v-else label="反向代理目标 proxy_pass">
              <el-input v-model="form.proxyPass" placeholder="http://127.0.0.1:3000" />
            </el-form-item>
            <el-form-item label="SSL 证书（可选，选中后追加 443 ssl 配置）">
              <el-select
                v-model="form.certName"
                clearable
                placeholder="不启用 HTTPS"
                class="sites-page__select"
              >
                <el-option
                  v-for="cert in certOptions"
                  :key="cert.name"
                  :label="cert.name"
                  :value="cert.name"
                />
              </el-select>
            </el-form-item>
          </el-form>
          <el-button type="primary" plain @click="applyFormToConfig"
            >生成配置到「配置文件」</el-button
          >
        </el-tab-pane>

        <el-tab-pane label="配置文件" name="raw">
          <template v-if="editorFallback">
            <el-alert
              type="info"
              title="代码编辑器加载失败，已退化为纯文本编辑"
              :closable="false"
              show-icon
              class="sites-page__section"
            />
            <el-input
              v-model="rawContent"
              type="textarea"
              :rows="18"
              spellcheck="false"
              class="sites-page__textarea"
            />
          </template>
          <div v-else ref="editorHost" class="sites-page__editor-host" />
        </el-tab-pane>
      </el-tabs>

      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="saveSite">保存</el-button>
      </template>
    </el-dialog>

    <!-- nginx -t / 重载 输出 -->
    <el-dialog v-model="outputVisible" :title="outputTitle" width="640px">
      <el-alert
        :type="outputOk ? 'success' : 'error'"
        :title="outputOk ? '配置有效' : '配置存在问题'"
        :closable="false"
        show-icon
        class="sites-page__section"
      />
      <pre class="sites-page__output">{{ outputText }}</pre>
    </el-dialog>

    <!-- 删除确认（输入站点名） -->
    <ConfirmDangerDialog
      v-model="deleteVisible"
      title="删除站点"
      :resource-name="deleteTarget?.name ?? ''"
      message="将从 sites-available 删除该配置并移除启用软链接（记得随后重载 nginx）。"
      :loading="deleteLoading"
      @confirm="handleDelete"
    />
  </div>
</template>

<style scoped>
.sites-page__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.sites-page__title {
  margin-right: 12px;
  font-weight: 600;
}

.sites-page__version {
  margin-left: 10px;
  color: var(--el-text-color-secondary);
  font-size: 13px;
}

.sites-page__actions {
  display: flex;
  gap: 8px;
}

.sites-page__name {
  font-weight: 500;
}

.sites-page__section {
  margin-bottom: 12px;
}

.sites-page__select {
  width: 100%;
}

.sites-page__editor-host {
  height: 380px;
  border: 1px solid var(--el-border-color-light);
  border-radius: 4px;
  overflow: hidden;
}

.sites-page__textarea :deep(textarea),
.sites-page__output {
  font-family: var(--el-font-family-mono, 'JetBrains Mono', Consolas, monospace);
  font-size: 13px;
  line-height: 1.6;
}

.sites-page__output {
  margin: 0;
  padding: 12px;
  max-height: 360px;
  overflow: auto;
  border-radius: 4px;
  background: var(--el-fill-color-light);
  white-space: pre-wrap;
  word-break: break-all;
}

.sites-page__static {
  color: var(--el-text-color-secondary);
}
</style>
