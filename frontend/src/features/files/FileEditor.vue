<script setup lang="ts">
import { onBeforeUnmount, ref, shallowRef } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
// 仅类型导入：monaco 运行时代码经下方动态 import 懒加载（独立 'monaco' chunk）。
import type * as Monaco from 'monaco-editor'

import type { ApiError } from '@/api/client'
import { getContent, putContent } from '@/api/files'
import { problemMessage } from '@/api/problem'

/**
 * 文件编辑器：全屏对话框内嵌 Monaco，打开时加载文件内容与 etag，
 * 保存走 PUT /files/content 的乐观并发（expected_etag，冲突时 409）。
 */
const props = defineProps<{
  /** 对话框可见性（v-model）。 */
  modelValue: boolean
  /** 要编辑的文件绝对路径。 */
  path: string
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', value: boolean): void
  /** 保存成功（调用方可刷新目录列表）。 */
  (e: 'saved'): void
}>()

const loading = ref(false)
const saving = ref(false)
/** 当前内容对应的 etag；保存成功后更新。 */
const etag = ref('')
/** 读取时内容被截断（超预览上限），此时编辑器只读，禁止保存以免损坏文件。 */
const truncated = ref(false)
/** 保存遭遇 409：磁盘内容已被其他方修改。 */
const conflict = ref(false)
/** 有未保存的本地修改。 */
const dirty = ref(false)

const editorEl = ref<HTMLElement | null>(null)
const editor = shallowRef<Monaco.editor.IStandaloneCodeEditor | null>(null)

// —— Monaco 懒加载（模块级单例，多次打开只加载一次） ——

let monacoPromise: Promise<typeof Monaco> | null = null

function loadMonaco(): Promise<typeof Monaco> {
  monacoPromise ??= (async () => {
    // Vite 官方 ESM 接法：主包 + 各语言服务的专属 worker（?worker 深路径导入）。
    const [monaco, editorWorker, jsonWorker, cssWorker, htmlWorker, tsWorker] = await Promise.all([
      import('monaco-editor'),
      import('monaco-editor/esm/vs/editor/editor.worker?worker'),
      import('monaco-editor/esm/vs/language/json/json.worker?worker'),
      import('monaco-editor/esm/vs/language/css/css.worker?worker'),
      import('monaco-editor/esm/vs/language/html/html.worker?worker'),
      import('monaco-editor/esm/vs/language/typescript/ts.worker?worker'),
    ])
    self.MonacoEnvironment = {
      getWorker: (_workerId: string, label: string) => {
        if (label === 'json') {
          return new jsonWorker.default()
        }
        if (label === 'css' || label === 'scss' || label === 'less') {
          return new cssWorker.default()
        }
        if (label === 'html' || label === 'handlebars' || label === 'razor') {
          return new htmlWorker.default()
        }
        if (label === 'typescript' || label === 'javascript') {
          return new tsWorker.default()
        }
        return new editorWorker.default()
      },
    }
    return monaco
  })()
  return monacoPromise
}

// —— 语言推断 ——

const LANGUAGE_BY_EXT: Record<string, string> = {
  js: 'javascript',
  mjs: 'javascript',
  cjs: 'javascript',
  ts: 'typescript',
  json: 'json',
  yml: 'yaml',
  yaml: 'yaml',
  md: 'markdown',
  py: 'python',
  sh: 'shell',
  bash: 'shell',
  html: 'html',
  htm: 'html',
  vue: 'html',
  css: 'css',
  scss: 'scss',
  less: 'less',
  xml: 'xml',
  ini: 'ini',
  conf: 'ini',
  toml: 'ini',
  env: 'ini',
  sql: 'sql',
  go: 'go',
  rs: 'rust',
  java: 'java',
  c: 'c',
  h: 'c',
  cpp: 'cpp',
  hpp: 'cpp',
}

function languageFor(path: string): string {
  const name = path.split('/').pop()?.toLowerCase() ?? ''
  if (name === 'dockerfile') {
    return 'dockerfile'
  }
  const ext = name.includes('.') ? (name.split('.').pop() ?? '') : ''
  return LANGUAGE_BY_EXT[ext] ?? 'plaintext'
}

// —— 打开 / 关闭 ——

/** 对话框动画结束（容器已渲染）后加载内容并创建编辑器。 */
async function handleOpened(): Promise<void> {
  conflict.value = false
  loading.value = true
  try {
    const [monaco, res] = await Promise.all([loadMonaco(), getContent(props.path)])
    etag.value = res.etag
    truncated.value = res.truncated
    if (!editorEl.value) {
      // 加载期间对话框已被关闭。
      return
    }
    editor.value?.dispose()
    const instance = monaco.editor.create(editorEl.value, {
      value: res.content,
      language: languageFor(props.path),
      automaticLayout: true,
      readOnly: res.truncated,
      minimap: { enabled: false },
      scrollBeyondLastLine: false,
      fontSize: 13,
    })
    instance.onDidChangeModelContent(() => {
      dirty.value = true
    })
    // Ctrl/Cmd+S 保存。
    instance.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, () => {
      void save(false)
    })
    editor.value = instance
    dirty.value = false
  } catch (err) {
    ElMessage.error(problemMessage(err, '读取文件失败'))
    emit('update:modelValue', false)
  } finally {
    loading.value = false
  }
}

function teardown(): void {
  editor.value?.dispose()
  editor.value = null
  etag.value = ''
  truncated.value = false
  conflict.value = false
  dirty.value = false
}

/** 有未保存修改时先确认再关闭。 */
function handleBeforeClose(done: () => void): void {
  if (!dirty.value) {
    done()
    return
  }
  ElMessageBox.confirm('有未保存的修改，确定要关闭编辑器吗？', '关闭编辑器', {
    type: 'warning',
    confirmButtonText: '放弃修改',
    cancelButtonText: '继续编辑',
  })
    .then(() => done())
    .catch(() => undefined)
}

function requestClose(): void {
  handleBeforeClose(() => emit('update:modelValue', false))
}

onBeforeUnmount(teardown)

// —— 保存 / 冲突处理 ——

/**
 * 保存内容。force=true 时不带 expected_etag（无条件覆盖磁盘版本，
 * 用于用户在冲突提示中选择「强制覆盖」）。
 */
async function save(force: boolean): Promise<void> {
  const instance = editor.value
  if (!instance || saving.value || truncated.value) {
    return
  }
  saving.value = true
  try {
    const res = await putContent({
      path: props.path,
      content: instance.getValue(),
      expected_etag: force ? null : etag.value,
    })
    etag.value = res.etag
    conflict.value = false
    dirty.value = false
    ElMessage.success('已保存')
    emit('saved')
  } catch (err) {
    if ((err as ApiError | undefined)?.problem?.code === 'conflict') {
      // 磁盘上的文件已被其他方修改：展示冲突条，让用户选择重新加载或强制覆盖。
      conflict.value = true
    } else {
      ElMessage.error(problemMessage(err, '保存失败'))
    }
  } finally {
    saving.value = false
  }
}

/** 放弃本地修改，重新加载磁盘上的最新内容。 */
async function reload(): Promise<void> {
  const instance = editor.value
  if (!instance) {
    return
  }
  loading.value = true
  try {
    const res = await getContent(props.path)
    etag.value = res.etag
    truncated.value = res.truncated
    instance.updateOptions({ readOnly: res.truncated })
    instance.setValue(res.content)
    conflict.value = false
    dirty.value = false
  } catch (err) {
    ElMessage.error(problemMessage(err, '重新加载失败'))
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <el-dialog
    :model-value="modelValue"
    fullscreen
    :close-on-click-modal="false"
    :before-close="handleBeforeClose"
    class="file-editor"
    @update:model-value="(value: boolean) => emit('update:modelValue', value)"
    @opened="handleOpened"
    @closed="teardown"
  >
    <template #header>
      <span class="file-editor__title">编辑：{{ path }}</span>
    </template>
    <el-alert
      v-if="truncated"
      type="warning"
      title="文件超出预览上限已被截断，编辑器处于只读模式，请下载后编辑"
      :closable="false"
      show-icon
      class="file-editor__alert"
    />
    <el-alert
      v-if="conflict"
      type="warning"
      title="保存冲突：文件已在磁盘上被其他方修改"
      :closable="false"
      show-icon
      class="file-editor__alert"
    >
      <div class="file-editor__conflict-actions">
        <el-button size="small" :loading="loading" @click="reload"
          >重新加载（放弃本地修改）</el-button
        >
        <el-button size="small" type="danger" :loading="saving" @click="save(true)">
          强制覆盖磁盘版本
        </el-button>
      </div>
    </el-alert>
    <div ref="editorEl" v-loading="loading" class="file-editor__container" />
    <template #footer>
      <el-button @click="requestClose">关闭</el-button>
      <el-button
        type="primary"
        :loading="saving"
        :disabled="loading || truncated"
        @click="save(false)"
      >
        保存
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.file-editor__title {
  font-weight: 600;
  word-break: break-all;
}

.file-editor__alert {
  margin-bottom: 8px;
}

.file-editor__conflict-actions {
  margin-top: 8px;
}

.file-editor__container {
  height: calc(100vh - 220px);
  border: 1px solid var(--el-border-color-light);
  border-radius: 4px;
  overflow: hidden;
}
</style>
