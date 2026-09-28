<script setup lang="ts">
// 日志查看器：等宽深色视口 + 自动滚动开关；行数过多时只渲染末尾窗口（近似虚拟滚动）。
// 两种喂数据方式：受控 `lines`（父组件维护行数组），或 `source`（组件内部拉 SSE 流）。
import { computed, nextTick, ref, watch } from 'vue'

import { formatJobLogEntry } from '@/api/jobs'
import { useResourceStream } from '@/composables/useResourceStream'

const props = withDefaults(
  defineProps<{
    /** 受控模式：由父组件提供日志行（优先于 source）。 */
    lines?: string[] | null
    /** SSE 模式：流地址（如 /api/v1/jobs/{id}/log），变化时自动清空并重开。 */
    source?: string | null
    /** SSE 模式监听的命名事件（初始化时固定）。 */
    sourceTopics?: string[]
    /** SSE 数据 → 显示行的转换（返回 null 丢弃）；默认按任务日志格式解析。 */
    format?: (data: unknown) => string | null
    /** 视口高度。 */
    height?: string
  }>(),
  {
    lines: null,
    source: null,
    sourceTopics: () => ['job-log'],
    format: undefined,
    height: '360px',
  },
)

const emit = defineEmits<{
  /** SSE 模式下收到结束标记（{done,status}）时触发。 */
  done: [status: string]
}>()

/** 最多渲染的行数：只保留末尾窗口，长日志不至于拖垮 DOM。 */
const MAX_RENDER_LINES = 2000

const streamLines = ref<string[]>([])
const autoScroll = ref(true)
const bodyRef = ref<HTMLElement | null>(null)

const allLines = computed(() => props.lines ?? streamLines.value)
const truncated = computed(() => allLines.value.length > MAX_RENDER_LINES)
const visibleLines = computed(() =>
  truncated.value ? allLines.value.slice(-MAX_RENDER_LINES) : allLines.value,
)

function toLine(data: unknown): string | null {
  return (props.format ?? formatJobLogEntry)(data)
}

const stream = useResourceStream({
  topics: props.sourceTopics,
  onEvent: (_topic, data) => {
    const line = toLine(data)
    if (line !== null) {
      streamLines.value.push(line)
    }
    if (data && typeof data === 'object' && (data as { done?: boolean }).done === true) {
      // 任务已结束：主动关闭流（否则服务端断开会触发 error 回调）。
      stream.close()
      emit('done', String((data as { status?: unknown }).status ?? ''))
    }
  },
})

watch(
  () => props.source,
  (url) => {
    stream.close()
    streamLines.value = []
    if (url) {
      stream.open(url)
    }
  },
  { immediate: true },
)

// 有新行且开启自动滚动时贴底。
watch(
  () => visibleLines.value.length,
  async () => {
    if (!autoScroll.value) {
      return
    }
    await nextTick()
    const el = bodyRef.value
    if (el) {
      el.scrollTop = el.scrollHeight
    }
  },
)

/** 用户滚离底部时暂停自动滚动，滚回底部时恢复。 */
function handleScroll(): void {
  const el = bodyRef.value
  if (!el) {
    return
  }
  autoScroll.value = el.scrollTop + el.clientHeight >= el.scrollHeight - 16
}
</script>

<template>
  <div class="log-viewer">
    <div class="log-viewer__toolbar">
      <span class="log-viewer__count">
        共 {{ allLines.length }} 行<template v-if="truncated"
          >（仅显示最近 {{ MAX_RENDER_LINES }} 行）</template
        >
      </span>
      <el-switch v-model="autoScroll" size="small" active-text="自动滚动" />
    </div>
    <div ref="bodyRef" class="log-viewer__body" :style="{ height }" @scroll.passive="handleScroll">
      <div v-if="visibleLines.length === 0" class="log-viewer__empty">暂无日志</div>
      <div v-for="(line, index) in visibleLines" :key="index" class="log-viewer__line">
        {{ line }}
      </div>
    </div>
  </div>
</template>

<style scoped>
.log-viewer {
  border: 1px solid var(--el-border-color-light);
  border-radius: 4px;
  overflow: hidden;
}

.log-viewer__toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 4px 12px;
  background: var(--el-fill-color-light);
  border-bottom: 1px solid var(--el-border-color-light);
}

.log-viewer__count {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.log-viewer__body {
  overflow: auto;
  background: #1d1e1f;
  padding: 8px 12px;
}

.log-viewer__line {
  font-family: 'JetBrains Mono', Consolas, Menlo, 'Courier New', monospace;
  font-size: 12px;
  line-height: 1.7;
  color: #d4d7de;
  white-space: pre-wrap;
  word-break: break-all;
}

.log-viewer__empty {
  color: #8a8f99;
  font-size: 12px;
  text-align: center;
  padding: 24px 0;
}
</style>
