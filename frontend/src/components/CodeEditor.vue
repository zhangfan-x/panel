<script setup lang="ts">
// 代码编辑器：懒加载 Monaco（独立 'monaco' 分包），加载失败时退化为纯文本域。
// 抽成组件是因为代理页有三处编辑场景（配置文件 YAML、扩展配置 YAML、扩展脚本 JS），
// 各自复制一遍懒加载/销毁逻辑会很快失控。
import { onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'
// 仅类型导入：monaco 运行时经动态 import 懒加载。
import type * as Monaco from 'monaco-editor'

const props = withDefaults(
  defineProps<{
    modelValue: string
    /** Monaco 语言标识（yaml / javascript / json…）。 */
    language?: string
    readonly?: boolean
    /** 编辑区高度（CSS 长度）。 */
    height?: string
    /** 退化文本域的行数。 */
    fallbackRows?: number
    loading?: boolean
  }>(),
  {
    language: 'yaml',
    readonly: false,
    height: '420px',
    fallbackRows: 20,
    loading: false,
  },
)

const emit = defineEmits<{ 'update:modelValue': [string] }>()

const editorEl = ref<HTMLElement | null>(null)
const editor = shallowRef<Monaco.editor.IStandaloneCodeEditor | null>(null)
/** Monaco 加载/创建失败（如离线、测试环境）时退化为纯文本域。 */
const fallback = ref(false)

let monacoPromise: Promise<typeof Monaco> | null = null

function loadMonaco(): Promise<typeof Monaco> {
  monacoPromise ??= (async () => {
    // monaco-editor 的 package.json 缺少 main/exports，包名导入在 Node 解析（vitest）下
    // 会失败；深路径直达完整构建，构建时仍被拆入独立 'monaco' 分包。
    const monaco = (await import(
      // @ts-expect-error -- monaco 的 esm 深路径未附带类型声明，运行时导出与包根类型一致
      'monaco-editor/esm/vs/editor/editor.main.js'
    )) as typeof Monaco
    try {
      // YAML/JS 在这里只用内置语法高亮，通用 editor worker 即可。
      const worker = await import('monaco-editor/esm/vs/editor/editor.worker.js?worker')
      self.MonacoEnvironment = { getWorker: () => new worker.default() }
    } catch {
      // worker 加载失败时 monaco 自动退化为主线程模式，仅损失少量流畅度。
    }
    return monaco
  })()
  return monacoPromise
}

/** 创建编辑器实例；DOM 未就绪或 monaco 不可用时退化。 */
async function mount(): Promise<void> {
  if (editor.value || fallback.value || !editorEl.value) {
    return
  }
  try {
    const monaco = await loadMonaco()
    if (!editorEl.value || editor.value) {
      return
    }
    const instance = monaco.editor.create(editorEl.value, {
      value: props.modelValue,
      language: props.language,
      readOnly: props.readonly,
      automaticLayout: true,
      minimap: { enabled: false },
      scrollBeyondLastLine: false,
      fontSize: 13,
    })
    instance.onDidChangeModelContent(() => {
      emit('update:modelValue', instance.getValue())
    })
    editor.value = instance
  } catch {
    fallback.value = true
  }
}

onMounted(mount)

// 外部改值（加载完成、放弃修改）时同步进编辑器，但不要打断用户正在输入的内容。
watch(
  () => props.modelValue,
  (value) => {
    const instance = editor.value
    if (instance && instance.getValue() !== value) {
      instance.setValue(value)
    }
  },
)

watch(
  () => props.readonly,
  (value) => editor.value?.updateOptions({ readOnly: value }),
)

/** 让父组件在切到编辑态时把光标交给编辑器。 */
function focus(): void {
  editor.value?.focus()
}

defineExpose({ focus })

onBeforeUnmount(() => {
  editor.value?.dispose()
  editor.value = null
})
</script>

<template>
  <el-input
    v-if="fallback"
    :model-value="modelValue"
    type="textarea"
    :rows="fallbackRows"
    :readonly="readonly"
    class="code-editor__fallback"
    @update:model-value="(value: string) => emit('update:modelValue', value)"
  />
  <div
    v-else
    ref="editorEl"
    v-loading="loading"
    class="code-editor"
    :style="{ height }"
  />
</template>

<style scoped>
.code-editor {
  width: 100%;
  border: 1px solid var(--el-border-color);
  border-radius: 4px;
  overflow: hidden;
}

.code-editor__fallback :deep(textarea) {
  font-family: var(--el-font-family-mono, monospace);
  font-size: 13px;
}
</style>
