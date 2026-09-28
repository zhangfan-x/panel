<script setup lang="ts">
// Compose 项目管理：根目录发现/导入、配置校验与运行状态、compose 文件编辑、up/down/restart 任务。
import { computed, nextTick, onBeforeUnmount, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import type { FormInstance, FormRules } from 'element-plus'
import { useQuery } from '@tanstack/vue-query'

import type { ApiError } from '@/api/client'
import {
  deleteComposeProject,
  getComposeFile,
  getComposeProject,
  importComposeProject,
  listComposeProjects,
  putComposeFile,
  runComposeAction,
} from '@/api/compose'
import type { ComposeProjectDetail, ComposeProjectSummary, ComposeVerb } from '@/api/compose'
import { problemMessage } from '@/api/problem'
import ConfirmDangerDialog from '@/components/ConfirmDangerDialog.vue'
import ReauthDialog from '@/components/ReauthDialog.vue'
import { formatDateTime } from '@/composables/format'
import { useJobsStore } from '@/stores/jobs'

const jobsStore = useJobsStore()

const { data, isPending, isError, error, refetch } = useQuery({
  queryKey: ['compose-projects'],
  queryFn: listComposeProjects,
})

const errorMessage = computed(() => problemMessage(error.value, '获取 Compose 项目列表失败'))
const projects = computed(() => data.value?.projects ?? [])
const composeRoot = computed(() => data.value?.compose_root ?? '')

const VERB_TEXT: Record<ComposeVerb, string> = { up: '启动', down: '停止', restart: '重启' }

/** 从错误对象里取 problem code（用于按错误码分支处理）。 */
function problemCode(err: unknown): string | undefined {
  return (err as ApiError | undefined)?.problem?.code
}

// —— up / down / restart（后台任务） ——

/** 正在提交的动作标识（`${name}:${verb}`），用于按钮 loading。 */
const actionBusy = ref<string | null>(null)

async function handleVerb(project: ComposeProjectSummary, verb: ComposeVerb): Promise<void> {
  actionBusy.value = `${project.name}:${verb}`
  try {
    const { job_id } = await runComposeAction(project.name, verb)
    ElMessage.success(`已提交「${VERB_TEXT[verb]}」任务，可在任务中心查看进度`)
    jobsStore.openDrawer(job_id)
  } catch (err) {
    ElMessage.error(problemMessage(err, `提交「${VERB_TEXT[verb]}」任务失败`))
  } finally {
    actionBusy.value = null
  }
}

// —— 导入项目 ——

const importVisible = ref(false)
const importSubmitting = ref(false)
const importFormRef = ref<FormInstance>()
const importForm = reactive({ name: '', path: '' })

const importRules: FormRules<typeof importForm> = {
  name: [
    { required: true, message: '请输入项目名称', trigger: 'blur' },
    {
      pattern: /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/,
      message: '名称需以字母或数字开头，仅可含字母、数字与 _ . -',
      trigger: 'blur',
    },
  ],
  path: [{ required: true, message: '请输入项目目录的绝对路径', trigger: 'blur' }],
}

function openImport(project?: ComposeProjectSummary): void {
  importForm.name = project?.name ?? ''
  importForm.path = project?.path ?? ''
  importVisible.value = true
}

async function handleImport(): Promise<void> {
  if (!importFormRef.value) {
    return
  }
  const valid = await importFormRef.value.validate().catch(() => false)
  if (!valid) {
    return
  }
  importSubmitting.value = true
  try {
    await importComposeProject({ name: importForm.name, path: importForm.path })
    ElMessage.success('项目导入成功')
    importVisible.value = false
    await refetch()
  } catch (err) {
    ElMessage.error(problemMessage(err, '导入项目失败，请确认目录位于项目根目录之下'))
  } finally {
    importSubmitting.value = false
  }
}

// —— 项目详情 ——

const detailVisible = ref(false)
const detailLoading = ref(false)
const detail = ref<ComposeProjectDetail | null>(null)

async function openDetail(project: ComposeProjectSummary): Promise<void> {
  detailVisible.value = true
  detailLoading.value = true
  detail.value = null
  try {
    detail.value = await getComposeProject(project.name)
  } catch (err) {
    ElMessage.error(problemMessage(err, '获取项目详情失败'))
    detailVisible.value = false
  } finally {
    detailLoading.value = false
  }
}

// —— compose 文件编辑（Monaco 懒加载；加载失败退化为纯文本域） ——

const fileVisible = ref(false)
const fileLoading = ref(false)
const fileSaving = ref(false)
const fileProject = ref('')
const fileName = ref('')
const fileEtag = ref('')
const fileContent = ref('')

/** monaco 模块缓存（首次打开编辑器时动态加载，构建时拆入独立 chunk）。 */
let monacoModule: typeof import('monaco-editor') | null = null
let editor: import('monaco-editor').editor.IStandaloneCodeEditor | null = null
const editorHost = ref<HTMLElement | null>(null)
/** monaco 加载/创建失败（如离线、测试环境）时退化为纯文本域。 */
const editorFallback = ref(false)

async function loadMonaco(): Promise<typeof import('monaco-editor')> {
  if (monacoModule) {
    return monacoModule
  }
  // monaco-editor 的 package.json 没有 main/exports 字段，包名导入在 Node 语义
  // 解析（vitest/vite-node）下会失败；深路径直达完整构建（含 YAML 语法高亮）。
  const monaco = (await import(
    // @ts-expect-error -- monaco 的 esm 深路径未附带类型声明，运行时导出与包根类型一致
    'monaco-editor/esm/vs/editor/editor.main.js'
  )) as typeof import('monaco-editor')
  try {
    // YAML 只有内置语法高亮、无独立语言服务，通用 editor worker 即可。
    const worker = await import('monaco-editor/esm/vs/editor/editor.worker.js?worker')
    self.MonacoEnvironment = { getWorker: () => new worker.default() }
  } catch {
    // worker 资源加载失败时 monaco 自动退化为主线程模式，仅损失少量流畅度。
  }
  monacoModule = monaco
  return monaco
}

/** 在编辑对话框打开、宿主元素渲染后创建编辑器实例（幂等）。 */
async function mountEditor(): Promise<void> {
  if (editorFallback.value || editor) {
    return
  }
  try {
    const monaco = await loadMonaco()
    await nextTick()
    const host = editorHost.value
    if (!host || !fileVisible.value || editor) {
      return
    }
    editor = monaco.editor.create(host, {
      value: fileContent.value,
      language: 'yaml',
      automaticLayout: true,
      minimap: { enabled: false },
      fontSize: 13,
      tabSize: 2,
      wordWrap: 'on',
      scrollBeyondLastLine: false,
    })
    editor.onDidChangeModelContent(() => {
      fileContent.value = editor?.getValue() ?? ''
    })
  } catch {
    editorFallback.value = true
  }
}

/** 释放编辑器实例（对话框关闭 / 组件卸载时）。 */
function destroyEditor(): void {
  editor?.dispose()
  editor = null
}

onBeforeUnmount(destroyEditor)

/** 更新本地内容并同步进已创建的编辑器。 */
function setEditorContent(content: string): void {
  fileContent.value = content
  if (editor && editor.getValue() !== content) {
    editor.setValue(content)
  }
}

async function openFile(project: ComposeProjectSummary): Promise<void> {
  fileProject.value = project.name
  fileName.value = ''
  fileEtag.value = ''
  setEditorContent('')
  fileVisible.value = true
  await loadFile()
  // 对话框 opened 事件先于文件加载完成时，这里兜底再挂载一次。
  void mountEditor()
}

async function loadFile(): Promise<void> {
  fileLoading.value = true
  try {
    const res = await getComposeFile(fileProject.value)
    fileName.value = res.file
    fileEtag.value = res.etag
    setEditorContent(res.content)
  } catch (err) {
    if (problemCode(err) === 'not_found') {
      // 目录尚无 compose 文件：允许直接新建 compose.yaml。
      fileName.value = ''
      fileEtag.value = ''
      setEditorContent('')
      ElMessage.warning('该项目还没有 compose 文件，保存后将新建 compose.yaml')
    } else {
      ElMessage.error(problemMessage(err, '读取 compose 文件失败'))
      fileVisible.value = false
    }
  } finally {
    fileLoading.value = false
  }
}

async function saveFile(): Promise<void> {
  fileSaving.value = true
  try {
    const res = await putComposeFile(fileProject.value, {
      content: fileContent.value,
      file: fileName.value || undefined,
      expected_etag: fileEtag.value || null,
    })
    fileName.value = res.file
    fileEtag.value = res.etag
    ElMessage.success('compose 文件已保存（服务端已校验 YAML）')
    // 保存可能新建了 compose 文件，刷新列表里的文件标签。
    void refetch()
  } catch (err) {
    if (problemCode(err) === 'conflict') {
      ElMessage.error('文件已在磁盘上被其他方修改，请「重新加载」后再保存')
    } else {
      ElMessage.error(problemMessage(err, '保存失败，请检查 YAML 是否有效'))
    }
  } finally {
    fileSaving.value = false
  }
}

// —— 移除导入（危险操作 + 重认证） ——

const deleteVisible = ref(false)
const deleteLoading = ref(false)
const deleteTarget = ref<ComposeProjectSummary | null>(null)
const reauthVisible = ref(false)

function openDelete(project: ComposeProjectSummary): void {
  deleteTarget.value = project
  deleteVisible.value = true
}

async function handleDelete(): Promise<void> {
  const target = deleteTarget.value
  if (!target) {
    return
  }
  deleteLoading.value = true
  try {
    await deleteComposeProject(target.name)
    ElMessage.success('已移除导入记录（目录与容器不受影响）')
    deleteVisible.value = false
    await refetch()
  } catch (err) {
    if (problemCode(err) === 'auth.reauth_required') {
      reauthVisible.value = true
    } else {
      ElMessage.error(problemMessage(err, '移除项目失败'))
    }
  } finally {
    deleteLoading.value = false
  }
}

// —— 展示辅助 ——

function configTagType(valid: boolean | null): 'success' | 'danger' | 'info' {
  if (valid === null) {
    return 'info'
  }
  return valid ? 'success' : 'danger'
}

function configTagText(valid: boolean | null): string {
  if (valid === null) {
    return '未知'
  }
  return valid ? '有效' : '无效'
}

function createdText(project: ComposeProjectSummary): string {
  return project.created_at ? formatDateTime(project.created_at) : '—'
}
</script>

<template>
  <div class="compose-page">
    <el-card shadow="never">
      <template #header>
        <div class="compose-page__header">
          <div>
            <span class="compose-page__title">Compose 项目</span>
            <span v-if="composeRoot" class="compose-page__root">根目录：{{ composeRoot }}</span>
          </div>
          <div>
            <el-button link type="primary" :loading="isPending" @click="() => refetch()">
              刷新
            </el-button>
            <el-button type="primary" @click="openImport()">导入项目</el-button>
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
      <el-table v-else :data="projects">
        <template #empty>
          <el-empty description="根目录下暂未发现任何 Compose 项目" />
        </template>
        <el-table-column label="名称" min-width="180">
          <template #default="{ row }">
            <span class="compose-page__name">{{ row.name }}</span>
            <el-tag v-if="row.imported" size="small" type="primary" effect="plain">已导入</el-tag>
            <el-tag v-else size="small" type="info" effect="plain">自动发现</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="path" label="路径" min-width="220" show-overflow-tooltip />
        <el-table-column label="compose 文件" width="160">
          <template #default="{ row }">
            <el-tag v-if="row.has_compose_file" size="small" type="success" effect="plain">
              {{ row.compose_file }}
            </el-tag>
            <el-tag v-else size="small" type="warning" effect="plain">缺失</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="导入时间" width="170">
          <template #default="{ row }">{{ createdText(row) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="360" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="openDetail(row)">详情</el-button>
            <el-button link type="primary" @click="openFile(row)">文件</el-button>
            <el-button
              link
              type="success"
              :loading="actionBusy === `${row.name}:up`"
              @click="handleVerb(row, 'up')"
            >
              启动
            </el-button>
            <el-button
              link
              type="warning"
              :loading="actionBusy === `${row.name}:restart`"
              @click="handleVerb(row, 'restart')"
            >
              重启
            </el-button>
            <el-button
              link
              type="danger"
              :loading="actionBusy === `${row.name}:down`"
              @click="handleVerb(row, 'down')"
            >
              停止
            </el-button>
            <el-button v-if="!row.imported" link type="primary" @click="openImport(row)">
              导入
            </el-button>
            <el-button v-else link type="danger" @click="openDelete(row)">移除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 导入项目 -->
    <el-dialog v-model="importVisible" title="导入 Compose 项目" width="520px">
      <el-form
        ref="importFormRef"
        :model="importForm"
        :rules="importRules"
        label-position="top"
        @submit.prevent="handleImport"
      >
        <el-form-item label="项目名称" prop="name">
          <el-input v-model="importForm.name" placeholder="如 blog" />
        </el-form-item>
        <el-form-item label="项目目录（绝对路径，需位于根目录之下）" prop="path">
          <el-input v-model="importForm.path" :placeholder="`${composeRoot}/blog`" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="importVisible = false">取消</el-button>
        <el-button type="primary" :loading="importSubmitting" @click="handleImport">导入</el-button>
      </template>
    </el-dialog>

    <!-- 项目详情 -->
    <el-dialog v-model="detailVisible" title="项目详情" width="720px">
      <el-skeleton v-if="detailLoading" :rows="6" animated />
      <template v-else-if="detail">
        <el-alert
          v-if="!detail.docker_available"
          type="warning"
          title="Docker 不可用，仅展示配置信息"
          :closable="false"
          show-icon
          class="compose-page__section"
        />
        <el-descriptions :column="2" border class="compose-page__section">
          <el-descriptions-item label="名称">{{ detail.name }}</el-descriptions-item>
          <el-descriptions-item label="来源">
            {{ detail.imported ? '已导入' : '自动发现' }}
          </el-descriptions-item>
          <el-descriptions-item label="路径" :span="2">{{ detail.path }}</el-descriptions-item>
          <el-descriptions-item label="compose 文件">
            {{ detail.compose_file ?? '缺失' }}
          </el-descriptions-item>
          <el-descriptions-item label="配置校验">
            <el-tag size="small" :type="configTagType(detail.config_valid)" effect="plain">
              {{ configTagText(detail.config_valid) }}
            </el-tag>
            <span v-if="detail.config_checked_by" class="compose-page__hint">
              （{{
                detail.config_checked_by === 'docker' ? 'docker compose 校验' : '仅 YAML 解析'
              }}）
            </span>
          </el-descriptions-item>
        </el-descriptions>
        <el-alert
          v-if="detail.config_error"
          type="error"
          :title="detail.config_error"
          :closable="false"
          show-icon
          class="compose-page__section"
        />
        <div class="compose-page__section">
          <span class="compose-page__label">声明的服务：</span>
          <template v-if="detail.services.length">
            <el-tag
              v-for="service in detail.services"
              :key="service"
              size="small"
              class="compose-page__service"
            >
              {{ service }}
            </el-tag>
          </template>
          <span v-else class="compose-page__hint">无</span>
        </div>
        <el-table :data="detail.ps" size="small">
          <template #empty>
            <el-empty description="没有运行中的服务容器" :image-size="60" />
          </template>
          <el-table-column prop="name" label="容器" min-width="160" />
          <el-table-column prop="state" label="状态" width="110" />
          <el-table-column prop="status" label="详情" min-width="140" />
          <el-table-column prop="ports" label="端口" min-width="120" />
        </el-table>
      </template>
    </el-dialog>

    <!-- compose 文件编辑（Monaco 懒加载） -->
    <el-dialog
      v-model="fileVisible"
      :title="`编辑 ${fileName || 'compose.yaml'}`"
      width="820px"
      top="6vh"
      :close-on-click-modal="false"
      @opened="mountEditor"
      @closed="destroyEditor"
    >
      <div v-loading="fileLoading" class="compose-page__editor">
        <template v-if="editorFallback">
          <el-alert
            type="info"
            title="代码编辑器加载失败，已退化为纯文本编辑"
            :closable="false"
            show-icon
            class="compose-page__section"
          />
          <el-input
            v-model="fileContent"
            type="textarea"
            :rows="18"
            spellcheck="false"
            class="compose-page__textarea"
            :placeholder="'services:\n  web:\n    image: nginx:alpine'"
          />
        </template>
        <div v-else ref="editorHost" class="compose-page__editor-host" />
      </div>
      <template #footer>
        <el-button :loading="fileLoading" @click="loadFile">重新加载</el-button>
        <el-button @click="fileVisible = false">关闭</el-button>
        <el-button type="primary" :loading="fileSaving" @click="saveFile">保存</el-button>
      </template>
    </el-dialog>

    <!-- 移除导入确认（输入项目名） + 重认证 -->
    <ConfirmDangerDialog
      v-model="deleteVisible"
      title="移除 Compose 项目"
      :resource-name="deleteTarget?.name ?? ''"
      message="仅移除面板中的导入记录，项目目录与容器不会被删除。"
      :loading="deleteLoading"
      @confirm="handleDelete"
    />
    <ReauthDialog v-model="reauthVisible" @success="handleDelete" />
  </div>
</template>

<style scoped>
.compose-page__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.compose-page__title {
  font-weight: 600;
}

.compose-page__root {
  margin-left: 12px;
  color: var(--el-text-color-secondary);
  font-size: 13px;
}

.compose-page__name {
  margin-right: 8px;
  font-weight: 500;
}

.compose-page__section {
  margin-bottom: 16px;
}

.compose-page__label {
  color: var(--el-text-color-secondary);
  font-size: 13px;
}

.compose-page__service {
  margin-right: 8px;
}

.compose-page__hint {
  color: var(--el-text-color-secondary);
  font-size: 13px;
}

.compose-page__editor-host {
  height: 440px;
  border: 1px solid var(--el-border-color-light);
  border-radius: 4px;
  overflow: hidden;
}

.compose-page__textarea :deep(textarea) {
  font-family: var(--el-font-family-mono, 'JetBrains Mono', Consolas, monospace);
  font-size: 13px;
  line-height: 1.6;
}
</style>
