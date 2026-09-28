<script setup lang="ts">
import { computed, nextTick, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import type { FormInstance, FormRules, InputInstance, UploadRequestOptions } from 'element-plus'
import {
  ArrowDown,
  Box,
  Download,
  DocumentAdd,
  EditPen,
  FolderAdd,
  Refresh,
  Upload,
} from '@element-plus/icons-vue'
import { keepPreviousData, useQuery } from '@tanstack/vue-query'

import type { ApiError } from '@/api/client'
import {
  archivePaths,
  chmodPath,
  chownPath,
  copyPath,
  deletePath,
  DOWNLOAD_ZIP_MAX_ITEMS,
  downloadUrl,
  downloadZipUrl,
  extractArchive,
  guessArchiveFormat,
  isImageName,
  listDir,
  mkdir,
  previewUrl,
  putContent,
  renamePath,
  statPath,
  uploadFile,
} from '@/api/files'
import type { ArchiveFormat, FileEntry, UploadResponse } from '@/api/files'
import { problemMessage } from '@/api/problem'
import ConfirmDangerDialog from '@/components/ConfirmDangerDialog.vue'
import ReauthDialog from '@/components/ReauthDialog.vue'
import { useJobsStore } from '@/stores/jobs'
import FileEditor from './FileEditor.vue'
import FileList from './FileList.vue'

/** 文本预览/编辑上限（与后端 /files/content 的 2 MiB 限制一致）。 */
const CONTENT_MAX_BYTES = 2 * 1024 * 1024

const jobsStore = useJobsStore()

/** 从错误对象里取 problem code（用于按错误码分支处理）。 */
function problemCode(err: unknown): string | undefined {
  return (err as ApiError | undefined)?.problem?.code
}

// —— 目录浏览 ——

const currentPath = ref('/')
const showHidden = ref(false)
const selection = ref<FileEntry[]>([])

const { data, isPending, isError, error, refetch, isFetching } = useQuery({
  queryKey: ['files-list', currentPath, showHidden],
  queryFn: () => listDir(currentPath.value, showHidden.value),
  placeholderData: keepPreviousData,
})

const entries = computed(() => data.value?.entries ?? [])
const errorMessage = computed(() => problemMessage(error.value, '读取目录失败'))

/** 面包屑：根目录 + 逐级路径段。 */
const crumbs = computed(() => {
  const items = [{ label: '/', path: '/' }]
  let acc = ''
  for (const segment of currentPath.value.split('/').filter(Boolean)) {
    acc += `/${segment}`
    items.push({ label: segment, path: acc })
  }
  return items
})

function joinPath(dir: string, name: string): string {
  return dir === '/' ? `/${name}` : `${dir}/${name}`
}

function fullPath(entry: FileEntry): string {
  return joinPath(currentPath.value, entry.name)
}

function navigate(path: string): void {
  selection.value = []
  currentPath.value = path || '/'
}

// —— 路径直接编辑（面包屑 ↔ 输入框切换） ——

const pathEditing = ref(false)
const pathDraft = ref('')
const pathInputRef = ref<InputInstance>()

/**
 * 把用户输入规范成后端接受的绝对路径（helper 要求 normpath 后不变：
 * 无 `.`/`..` 段、无重复斜杠、除根外无尾斜杠）。非绝对路径返回 null。
 */
function normalizeAbsolutePath(input: string): string | null {
  const trimmed = input.trim()
  if (!trimmed.startsWith('/')) {
    return null
  }
  const parts: string[] = []
  for (const segment of trimmed.split('/')) {
    if (segment === '' || segment === '.') {
      continue
    }
    if (segment === '..') {
      parts.pop()
      continue
    }
    parts.push(segment)
  }
  return `/${parts.join('/')}`
}

function startPathEdit(): void {
  pathDraft.value = currentPath.value
  pathEditing.value = true
  void nextTick(() => pathInputRef.value?.focus())
}

function cancelPathEdit(): void {
  pathEditing.value = false
}

function submitPathEdit(): void {
  const normalized = normalizeAbsolutePath(pathDraft.value)
  if (normalized === null) {
    ElMessage.warning('请输入以 / 开头的绝对路径')
    return
  }
  pathEditing.value = false
  navigate(normalized)
}

// —— 打开 / 下载 ——

const editorVisible = ref(false)
const editorPath = ref('')

// 图片预览（inline 流式返回，不受编辑器 2 MiB 上限约束）。
const imageVisible = ref(false)
const imageTarget = ref<{ name: string; path: string } | null>(null)
const imageSrc = computed(() => (imageTarget.value ? previewUrl(imageTarget.value.path) : ''))

function handleOpen(entry: FileEntry): void {
  if (entry.type === 'dir') {
    navigate(fullPath(entry))
    return
  }
  if (entry.type !== 'file') {
    return
  }
  if (isImageName(entry.name)) {
    imageTarget.value = { name: entry.name, path: fullPath(entry) }
    imageVisible.value = true
    return
  }
  if (entry.size > CONTENT_MAX_BYTES) {
    ElMessage.warning('文件超过 2 MiB 预览上限，请下载后查看')
    return
  }
  editorPath.value = fullPath(entry)
  editorVisible.value = true
}

/** 经临时锚点触发浏览器下载（Cookie 鉴权，Content-Disposition 由后端下发）；目录由后端打包为 zip。 */
function handleDownload(entry: FileEntry): void {
  const anchor = document.createElement('a')
  anchor.href = downloadUrl(fullPath(entry))
  anchor.download = entry.type === 'dir' ? `${entry.name}.zip` : entry.name
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
}

/** 把选中项即时打包成一个 zip 下载；不落盘，与「打包」后台任务不同。 */
function downloadSelectionAsZip(): void {
  const names = selection.value.map((entry) => entry.name)
  if (names.length === 0) {
    return
  }
  if (names.length > DOWNLOAD_ZIP_MAX_ITEMS) {
    ElMessage.warning(
      `一次最多打包下载 ${DOWNLOAD_ZIP_MAX_ITEMS} 项，选中项更多时请改用「打包」生成归档文件`,
    )
    return
  }
  // 只选中一个文件时没必要套一层 zip。
  const single = selection.value[0]
  if (names.length === 1 && single && single.type === 'file') {
    handleDownload(single)
    return
  }
  window.location.assign(downloadZipUrl(currentPath.value, names))
}

/** 逐个下载选中的文件（目录无法逐个下载，跳过并提示）。 */
function downloadSelectionEach(): void {
  const files = selection.value.filter((entry) => entry.type === 'file')
  const skipped = selection.value.length - files.length
  if (files.length === 0) {
    ElMessage.warning('选中项里没有文件；目录请使用「打包为 zip 下载」')
    return
  }
  files.forEach((entry, index) => {
    // 浏览器会拦截同一时刻发起的多个下载，稍作间隔逐个触发。
    window.setTimeout(() => handleDownload(entry), index * 300)
  })
  if (skipped > 0) {
    ElMessage.info(`已跳过 ${skipped} 个目录，目录请使用「打包为 zip 下载」`)
  }
}

function handleDownloadCommand(command: string): void {
  if (command === 'zip') {
    downloadSelectionAsZip()
  } else {
    downloadSelectionEach()
  }
}

/** 图片预览对话框内的下载按钮。 */
function downloadPreviewedImage(): void {
  if (!imageTarget.value) {
    return
  }
  const anchor = document.createElement('a')
  anchor.href = downloadUrl(imageTarget.value.path)
  anchor.download = imageTarget.value.name
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
}

// —— 上传 ——

const uploadingCount = ref(0)
const uploading = computed(() => uploadingCount.value > 0)

/** el-upload 自定义请求：走 axios client（携带 CSRF 头），目标为当前目录。 */
async function handleUpload(options: UploadRequestOptions): Promise<UploadResponse> {
  const name = options.file.name
  uploadingCount.value += 1
  try {
    const res = await uploadFile(joinPath(currentPath.value, name), options.file)
    ElMessage.success(`已上传 ${name}`)
    await refetch()
    return res
  } catch (err) {
    ElMessage.error(problemMessage(err, `上传 ${name} 失败`))
    throw err
  } finally {
    uploadingCount.value -= 1
  }
}

// —— 新建文件 / 目录 ——

const createVisible = ref(false)
const createType = ref<'file' | 'dir'>('file')
const createSubmitting = ref(false)
const createFormRef = ref<FormInstance>()
const createForm = reactive({ name: '' })

const createRules: FormRules<typeof createForm> = {
  name: [
    { required: true, message: '请输入名称', trigger: 'blur' },
    {
      validator: (_rule, value: string, callback) => {
        if (value.includes('/')) {
          callback(new Error('名称不能包含斜杠'))
        } else if (value === '.' || value === '..') {
          callback(new Error('名称不合法'))
        } else {
          callback()
        }
      },
      trigger: 'blur',
    },
  ],
}

const createTitle = computed(() => (createType.value === 'dir' ? '新建目录' : '新建文件'))

function openCreate(type: 'file' | 'dir'): void {
  createType.value = type
  createForm.name = ''
  createVisible.value = true
}

async function submitCreate(): Promise<void> {
  if (!createFormRef.value) {
    return
  }
  const valid = await createFormRef.value.validate().catch(() => false)
  if (!valid) {
    return
  }
  const path = joinPath(currentPath.value, createForm.name)
  createSubmitting.value = true
  try {
    if (createType.value === 'dir') {
      await mkdir({ path })
    } else {
      // 空内容写入会覆盖同名文件，先 stat 确认目标不存在（404 才继续）。
      const exists = await statPath(path).then(
        () => true,
        (err) => {
          if (problemCode(err) === 'not_found') {
            return false
          }
          throw err
        },
      )
      if (exists) {
        ElMessage.error('同名路径已存在')
        return
      }
      await putContent({ path, content: '' })
    }
    ElMessage.success(createType.value === 'dir' ? '目录已创建' : '文件已创建')
    createVisible.value = false
    await refetch()
  } catch (err) {
    ElMessage.error(problemMessage(err, '创建失败'))
  } finally {
    createSubmitting.value = false
  }
}

// —— 行操作对话框共用的目标条目 ——

const target = ref<{ entry: FileEntry; path: string } | null>(null)

// —— 重命名 / 移动 ——

const renameVisible = ref(false)
const renameSubmitting = ref(false)
const renameFormRef = ref<FormInstance>()
const renameForm = reactive({ dst: '' })

const renameRules: FormRules<typeof renameForm> = {
  dst: [{ required: true, message: '请输入新路径', trigger: 'blur' }],
}

function openRename(entry: FileEntry): void {
  target.value = { entry, path: fullPath(entry) }
  renameForm.dst = fullPath(entry)
  renameVisible.value = true
}

async function submitRename(): Promise<void> {
  if (!renameFormRef.value || !target.value) {
    return
  }
  const valid = await renameFormRef.value.validate().catch(() => false)
  if (!valid) {
    return
  }
  renameSubmitting.value = true
  try {
    await renamePath({ src: target.value.path, dst: renameForm.dst })
    ElMessage.success('重命名成功')
    renameVisible.value = false
    await refetch()
  } catch (err) {
    ElMessage.error(problemMessage(err, '重命名失败（目标可能已存在）'))
  } finally {
    renameSubmitting.value = false
  }
}

// —— 复制 ——

const copyVisible = ref(false)
const copySubmitting = ref(false)
const copyFormRef = ref<FormInstance>()
const copyForm = reactive({ dst: '' })

const copyRules: FormRules<typeof copyForm> = {
  dst: [{ required: true, message: '请输入目标路径', trigger: 'blur' }],
}

function openCopy(entry: FileEntry): void {
  target.value = { entry, path: fullPath(entry) }
  copyForm.dst = `${fullPath(entry)}.copy`
  copyVisible.value = true
}

async function submitCopy(): Promise<void> {
  if (!copyFormRef.value || !target.value) {
    return
  }
  const valid = await copyFormRef.value.validate().catch(() => false)
  if (!valid) {
    return
  }
  const recursive = target.value.entry.type === 'dir'
  copySubmitting.value = true
  try {
    const res = await copyPath({ src: target.value.path, dst: copyForm.dst, recursive })
    copyVisible.value = false
    if (res.job_id) {
      // 递归目录复制转后台任务执行。
      ElMessage.success('已提交复制任务，可在任务中心查看进度')
      jobsStore.openDrawer(res.job_id)
    } else {
      ElMessage.success('复制成功')
    }
    await refetch()
  } catch (err) {
    ElMessage.error(problemMessage(err, '复制失败'))
  } finally {
    copySubmitting.value = false
  }
}

// —— 修改权限 ——

const chmodVisible = ref(false)
const chmodSubmitting = ref(false)
const chmodFormRef = ref<FormInstance>()
const chmodForm = reactive({ mode: '' })

const chmodRules: FormRules<typeof chmodForm> = {
  mode: [
    { required: true, message: '请输入权限', trigger: 'blur' },
    { pattern: /^[0-7]{3,4}$/, message: '请输入 3–4 位八进制权限（如 644）', trigger: 'blur' },
  ],
}

function openChmod(entry: FileEntry): void {
  target.value = { entry, path: fullPath(entry) }
  chmodForm.mode = (entry.mode & 0o7777).toString(8).padStart(3, '0')
  chmodVisible.value = true
}

async function submitChmod(): Promise<void> {
  if (!chmodFormRef.value || !target.value) {
    return
  }
  const valid = await chmodFormRef.value.validate().catch(() => false)
  if (!valid) {
    return
  }
  chmodSubmitting.value = true
  try {
    await chmodPath({ path: target.value.path, mode: Number.parseInt(chmodForm.mode, 8) })
    ElMessage.success('权限已修改')
    chmodVisible.value = false
    await refetch()
  } catch (err) {
    ElMessage.error(problemMessage(err, '修改权限失败'))
  } finally {
    chmodSubmitting.value = false
  }
}

// —— 修改所有者 ——

const chownVisible = ref(false)
const chownSubmitting = ref(false)
const chownForm = reactive({ uid: 0, gid: 0 })

function openChown(entry: FileEntry): void {
  target.value = { entry, path: fullPath(entry) }
  chownForm.uid = entry.uid
  chownForm.gid = entry.gid
  chownVisible.value = true
}

async function submitChown(): Promise<void> {
  if (!target.value) {
    return
  }
  chownSubmitting.value = true
  try {
    await chownPath({ path: target.value.path, uid: chownForm.uid, gid: chownForm.gid })
    ElMessage.success('所有者已修改')
    chownVisible.value = false
    await refetch()
  } catch (err) {
    ElMessage.error(problemMessage(err, '修改所有者失败'))
  } finally {
    chownSubmitting.value = false
  }
}

// —— 删除（危险操作：输入名称确认 + 需近期重认证） ——

const deleteVisible = ref(false)
const deleteLoading = ref(false)
const reauthVisible = ref(false)

const deleteMessage = computed(() => {
  if (!target.value) {
    return ''
  }
  const suffix = target.value.entry.type === 'dir' ? ' 及其全部内容' : ''
  return `将删除 ${target.value.path}${suffix}，删除后不可恢复。`
})

function openDelete(entry: FileEntry): void {
  target.value = { entry, path: fullPath(entry) }
  deleteVisible.value = true
}

async function confirmDelete(): Promise<void> {
  if (!target.value) {
    return
  }
  deleteLoading.value = true
  try {
    await deletePath({
      path: target.value.path,
      recursive: target.value.entry.type === 'dir',
    })
    ElMessage.success('已删除')
    deleteVisible.value = false
    await refetch()
  } catch (err) {
    if (problemCode(err) === 'auth.reauth_required') {
      // 重认证窗口已过期：弹出密码确认，成功后自动重试删除。
      reauthVisible.value = true
    } else {
      ElMessage.error(problemMessage(err, '删除失败'))
    }
  } finally {
    deleteLoading.value = false
  }
}

// —— 打包（多选 → 后台任务） ——

const archiveVisible = ref(false)
const archiveSubmitting = ref(false)
const archiveFormRef = ref<FormInstance>()
const archiveForm = reactive({ dst: '', format: 'zip' as ArchiveFormat })

const archiveRules: FormRules<typeof archiveForm> = {
  dst: [{ required: true, message: '请输入归档文件路径', trigger: 'blur' }],
}

function openArchive(): void {
  if (selection.value.length === 0) {
    return
  }
  archiveForm.format = 'zip'
  archiveForm.dst = joinPath(currentPath.value, 'archive.zip')
  archiveVisible.value = true
}

async function submitArchive(): Promise<void> {
  if (!archiveFormRef.value) {
    return
  }
  const valid = await archiveFormRef.value.validate().catch(() => false)
  if (!valid) {
    return
  }
  archiveSubmitting.value = true
  try {
    const { job_id } = await archivePaths({
      paths: selection.value.map(fullPath),
      dst: archiveForm.dst,
      format: archiveForm.format,
    })
    archiveVisible.value = false
    ElMessage.success('已提交打包任务，可在任务中心查看进度')
    jobsStore.openDrawer(job_id)
  } catch (err) {
    ElMessage.error(problemMessage(err, '提交打包任务失败'))
  } finally {
    archiveSubmitting.value = false
  }
}

// —— 解压（后台任务） ——

const extractVisible = ref(false)
const extractSubmitting = ref(false)
const extractFormRef = ref<FormInstance>()
const extractForm = reactive({ dst_dir: '', format: 'zip' as ArchiveFormat })

const extractRules: FormRules<typeof extractForm> = {
  dst_dir: [{ required: true, message: '请输入解压目标目录', trigger: 'blur' }],
}

function openExtract(entry: FileEntry): void {
  target.value = { entry, path: fullPath(entry) }
  extractForm.format = guessArchiveFormat(entry.name) ?? 'zip'
  extractForm.dst_dir = currentPath.value
  extractVisible.value = true
}

async function submitExtract(): Promise<void> {
  if (!extractFormRef.value || !target.value) {
    return
  }
  const valid = await extractFormRef.value.validate().catch(() => false)
  if (!valid) {
    return
  }
  extractSubmitting.value = true
  try {
    const { job_id } = await extractArchive({
      archive: target.value.path,
      dst_dir: extractForm.dst_dir,
      format: extractForm.format,
    })
    extractVisible.value = false
    ElMessage.success('已提交解压任务，可在任务中心查看进度')
    jobsStore.openDrawer(job_id)
  } catch (err) {
    ElMessage.error(problemMessage(err, '提交解压任务失败'))
  } finally {
    extractSubmitting.value = false
  }
}
</script>

<template>
  <div class="files-page">
    <el-card shadow="never">
      <template #header>
        <div class="files-page__header">
          <div class="files-page__path">
            <template v-if="!pathEditing">
              <el-breadcrumb separator="/" class="files-page__breadcrumb">
                <el-breadcrumb-item v-for="crumb in crumbs" :key="crumb.path">
                  <span class="files-page__crumb" @click="navigate(crumb.path)">
                    {{ crumb.label }}
                  </span>
                </el-breadcrumb-item>
              </el-breadcrumb>
              <el-button
                :icon="EditPen"
                link
                size="small"
                title="编辑路径"
                @click="startPathEdit"
              />
            </template>
            <el-input
              v-else
              ref="pathInputRef"
              v-model="pathDraft"
              class="files-page__path-input"
              size="small"
              placeholder="/absolute/path"
              @keyup.enter="submitPathEdit"
              @keyup.esc="cancelPathEdit"
              @blur="cancelPathEdit"
            />
          </div>
          <div class="files-page__toolbar">
            <el-button :icon="DocumentAdd" @click="openCreate('file')">新建文件</el-button>
            <el-button :icon="FolderAdd" @click="openCreate('dir')">新建目录</el-button>
            <el-upload
              class="files-page__upload"
              action="#"
              multiple
              :show-file-list="false"
              :http-request="handleUpload"
            >
              <el-button :icon="Upload" :loading="uploading">上传</el-button>
            </el-upload>
            <el-dropdown
              class="files-page__download"
              :disabled="selection.length === 0"
              @command="handleDownloadCommand"
            >
              <el-button :icon="Download" :disabled="selection.length === 0">
                下载<el-icon class="el-icon--right"><ArrowDown /></el-icon>
              </el-button>
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item command="zip">
                    打包为 zip 下载（{{ selection.length }} 项）
                  </el-dropdown-item>
                  <el-dropdown-item command="each">逐个下载（仅文件）</el-dropdown-item>
                </el-dropdown-menu>
              </template>
            </el-dropdown>
            <el-button :icon="Box" :disabled="selection.length === 0" @click="openArchive">
              打包
            </el-button>
            <el-switch v-model="showHidden" active-text="显示隐藏文件" />
            <el-button :icon="Refresh" :loading="isFetching" @click="() => refetch()">
              刷新
            </el-button>
          </div>
        </div>
      </template>

      <el-skeleton v-if="isPending" :rows="6" animated />
      <el-alert
        v-else-if="isError"
        type="error"
        :title="errorMessage"
        :closable="false"
        show-icon
      />
      <FileList
        v-else
        :entries="entries"
        :loading="isFetching"
        @open="handleOpen"
        @download="handleDownload"
        @rename="openRename"
        @copy="openCopy"
        @chmod="openChmod"
        @chown="openChown"
        @delete="openDelete"
        @extract="openExtract"
        @selection-change="selection = $event"
      />
    </el-card>

    <!-- 新建文件 / 目录 -->
    <el-dialog v-model="createVisible" :title="createTitle" width="420px">
      <el-form
        ref="createFormRef"
        :model="createForm"
        :rules="createRules"
        label-position="top"
        @submit.prevent="submitCreate"
      >
        <el-form-item :label="`将在 ${currentPath} 下创建`" prop="name">
          <el-input v-model="createForm.name" placeholder="名称" autocomplete="off" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createVisible = false">取消</el-button>
        <el-button type="primary" :loading="createSubmitting" @click="submitCreate">创建</el-button>
      </template>
    </el-dialog>

    <!-- 重命名 / 移动 -->
    <el-dialog v-model="renameVisible" title="重命名 / 移动" width="560px">
      <el-form
        ref="renameFormRef"
        :model="renameForm"
        :rules="renameRules"
        label-position="top"
        @submit.prevent="submitRename"
      >
        <el-form-item :label="`原路径：${target?.path ?? ''}`" prop="dst">
          <el-input v-model="renameForm.dst" placeholder="新的完整路径" autocomplete="off" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="renameVisible = false">取消</el-button>
        <el-button type="primary" :loading="renameSubmitting" @click="submitRename">确定</el-button>
      </template>
    </el-dialog>

    <!-- 复制 -->
    <el-dialog v-model="copyVisible" title="复制" width="560px">
      <el-form
        ref="copyFormRef"
        :model="copyForm"
        :rules="copyRules"
        label-position="top"
        @submit.prevent="submitCopy"
      >
        <el-form-item :label="`复制 ${target?.path ?? ''} 到`" prop="dst">
          <el-input v-model="copyForm.dst" placeholder="目标完整路径" autocomplete="off" />
        </el-form-item>
      </el-form>
      <p v-if="target?.entry.type === 'dir'" class="files-page__hint">
        目录为递归复制，将以后台任务执行。
      </p>
      <template #footer>
        <el-button @click="copyVisible = false">取消</el-button>
        <el-button type="primary" :loading="copySubmitting" @click="submitCopy">确定</el-button>
      </template>
    </el-dialog>

    <!-- 修改权限 -->
    <el-dialog v-model="chmodVisible" title="修改权限" width="420px">
      <el-form
        ref="chmodFormRef"
        :model="chmodForm"
        :rules="chmodRules"
        label-position="top"
        @submit.prevent="submitChmod"
      >
        <el-form-item :label="`路径：${target?.path ?? ''}`" prop="mode">
          <el-input v-model="chmodForm.mode" placeholder="八进制权限，如 644 / 755" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="chmodVisible = false">取消</el-button>
        <el-button type="primary" :loading="chmodSubmitting" @click="submitChmod">确定</el-button>
      </template>
    </el-dialog>

    <!-- 修改所有者 -->
    <el-dialog v-model="chownVisible" title="修改所有者" width="420px">
      <el-form label-position="top" @submit.prevent="submitChown">
        <el-form-item :label="`路径：${target?.path ?? ''}`">
          <div class="files-page__chown">
            <el-input-number v-model="chownForm.uid" :min="-1" :step="1" step-strictly />
            <span class="files-page__chown-sep">:</span>
            <el-input-number v-model="chownForm.gid" :min="-1" :step="1" step-strictly />
          </div>
        </el-form-item>
      </el-form>
      <p class="files-page__hint">依次为 uid 与 gid，填 -1 表示保持现值。</p>
      <template #footer>
        <el-button @click="chownVisible = false">取消</el-button>
        <el-button type="primary" :loading="chownSubmitting" @click="submitChown">确定</el-button>
      </template>
    </el-dialog>

    <!-- 打包 -->
    <el-dialog v-model="archiveVisible" title="打包为归档" width="560px">
      <el-form
        ref="archiveFormRef"
        :model="archiveForm"
        :rules="archiveRules"
        label-position="top"
        @submit.prevent="submitArchive"
      >
        <el-form-item :label="`已选 ${selection.length} 项，归档文件路径`" prop="dst">
          <el-input v-model="archiveForm.dst" placeholder="/path/to/archive.zip" />
        </el-form-item>
        <el-form-item label="格式">
          <el-radio-group v-model="archiveForm.format">
            <el-radio value="zip">zip</el-radio>
            <el-radio value="tar">tar</el-radio>
            <el-radio value="tar.gz">tar.gz</el-radio>
          </el-radio-group>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="archiveVisible = false">取消</el-button>
        <el-button type="primary" :loading="archiveSubmitting" @click="submitArchive">
          提交任务
        </el-button>
      </template>
    </el-dialog>

    <!-- 解压 -->
    <el-dialog v-model="extractVisible" title="解压归档" width="560px">
      <el-form
        ref="extractFormRef"
        :model="extractForm"
        :rules="extractRules"
        label-position="top"
        @submit.prevent="submitExtract"
      >
        <el-form-item :label="`归档：${target?.path ?? ''}`" prop="dst_dir">
          <el-input v-model="extractForm.dst_dir" placeholder="解压到的目标目录" />
        </el-form-item>
        <el-form-item label="格式">
          <el-radio-group v-model="extractForm.format">
            <el-radio value="zip">zip</el-radio>
            <el-radio value="tar">tar</el-radio>
            <el-radio value="tar.gz">tar.gz</el-radio>
          </el-radio-group>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="extractVisible = false">取消</el-button>
        <el-button type="primary" :loading="extractSubmitting" @click="submitExtract">
          提交任务
        </el-button>
      </template>
    </el-dialog>

    <!-- 删除确认（输入名称）+ 重认证 -->
    <ConfirmDangerDialog
      v-model="deleteVisible"
      title="删除"
      :resource-name="target?.entry.name ?? ''"
      :message="deleteMessage"
      :loading="deleteLoading"
      @confirm="confirmDelete"
    />
    <ReauthDialog v-model="reauthVisible" @success="confirmDelete" />

    <!-- 图片预览（inline 同源加载；点击图片可进入全屏缩放查看） -->
    <el-dialog
      v-model="imageVisible"
      :title="`预览：${imageTarget?.name ?? ''}`"
      width="720px"
      @closed="imageTarget = null"
    >
      <div class="files-page__image-wrap">
        <el-image
          v-if="imageTarget"
          :src="imageSrc"
          :preview-src-list="[imageSrc]"
          hide-on-click-modal
          fit="contain"
          class="files-page__image"
        >
          <template #error>
            <div class="files-page__image-error">图片加载失败</div>
          </template>
        </el-image>
      </div>
      <template #footer>
        <el-button @click="imageVisible = false">关闭</el-button>
        <el-button type="primary" @click="downloadPreviewedImage">下载</el-button>
      </template>
    </el-dialog>

    <!-- 编辑器（Monaco 懒加载） -->
    <FileEditor v-model="editorVisible" :path="editorPath" @saved="() => refetch()" />
  </div>
</template>

<style scoped>
.files-page__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
}

.files-page__path {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  flex: 1 1 320px;
}

.files-page__path-input {
  max-width: 560px;
}

.files-page__breadcrumb {
  min-width: 0;
}

.files-page__crumb {
  cursor: pointer;
  color: var(--el-color-primary);
}

.files-page__crumb:hover {
  text-decoration: underline;
}

.files-page__toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}

.files-page__toolbar .el-button + .el-button {
  margin-left: 0;
}

.files-page__upload {
  display: inline-flex;
}

.files-page__hint {
  margin: 0 0 8px;
  color: var(--el-text-color-secondary);
  font-size: 13px;
}

.files-page__chown {
  display: flex;
  align-items: center;
  gap: 8px;
}

.files-page__chown-sep {
  color: var(--el-text-color-secondary);
}

.files-page__image-wrap {
  display: flex;
  justify-content: center;
  min-height: 120px;
}

.files-page__image {
  max-width: 100%;
  max-height: 60vh;
}

.files-page__image-error {
  padding: 40px 0;
  color: var(--el-text-color-secondary);
}
</style>
