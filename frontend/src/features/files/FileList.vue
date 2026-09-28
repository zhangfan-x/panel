<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ArrowDown, Document, Folder, Link, QuestionFilled } from '@element-plus/icons-vue'

import { guessArchiveFormat, isImageName } from '@/api/files'
import type { FileEntry, FileType } from '@/api/files'
import { formatDateTime } from '@/composables/format'

/**
 * 文件表格：展示目录条目（名称/类型/大小/权限/所有者/修改时间）并把行操作
 * 以事件形式抛给 FilesPage 处理；自身不发起任何请求。
 */
const props = defineProps<{
  /** 当前目录下的条目（来自 GET /files/list）。 */
  entries: FileEntry[]
  /** 刷新中的加载态（覆盖在表格上）。 */
  loading?: boolean
}>()

const emit = defineEmits<{
  /** 打开条目：目录进入、普通文件预览/编辑。 */
  (e: 'open', entry: FileEntry): void
  (e: 'download', entry: FileEntry): void
  (e: 'rename', entry: FileEntry): void
  (e: 'copy', entry: FileEntry): void
  (e: 'chmod', entry: FileEntry): void
  (e: 'chown', entry: FileEntry): void
  (e: 'delete', entry: FileEntry): void
  /** 解压归档（仅对可识别的 zip/tar/tar.gz 文件出现）。 */
  (e: 'extract', entry: FileEntry): void
  /** 多选变化（用于「打包」）。 */
  (e: 'selection-change', entries: FileEntry[]): void
}>()

/** 目录排前，其余按名称排序（后端已按名称排好，这里只做目录置顶）。 */
const sortedEntries = computed(() =>
  [...props.entries].sort((a, b) => {
    const rankA = a.type === 'dir' ? 0 : 1
    const rankB = b.type === 'dir' ? 0 : 1
    return rankA === rankB ? a.name.localeCompare(b.name) : rankA - rankB
  }),
)

/**
 * 前端分页：每行的操作下拉都是独立的弹层实例，一次性渲染几百行会阻塞主线程
 * 数秒（如 /tmp），分页把同屏行数压到常数级。目录切换（entries 引用变化）时
 * 回到第一页。
 */
const PAGE_SIZE = 100
const currentPage = ref(1)

watch(
  () => props.entries,
  () => {
    currentPage.value = 1
  },
)

const pagedEntries = computed(() => {
  const start = (currentPage.value - 1) * PAGE_SIZE
  return sortedEntries.value.slice(start, start + PAGE_SIZE)
})

const TYPE_TEXT: Record<FileType, string> = {
  file: '文件',
  dir: '目录',
  symlink: '链接',
  socket: '套接字',
  fifo: 'FIFO',
  block: '块设备',
  char: '字符设备',
  unknown: '未知',
}

function typeText(type: FileType): string {
  return TYPE_TEXT[type] ?? '未知'
}

function iconFor(entry: FileEntry): typeof Document {
  if (entry.type === 'dir') {
    return Folder
  }
  if (entry.type === 'symlink') {
    return Link
  }
  if (entry.type === 'file') {
    return Document
  }
  return QuestionFilled
}

/** 人性化字节数（二进制单位）。 */
function formatBytes(size: number): string {
  if (!Number.isFinite(size) || size < 0) {
    return '—'
  }
  if (size < 1024) {
    return `${size} B`
  }
  let value = size
  let unit = 'B'
  for (const next of ['KiB', 'MiB', 'GiB', 'TiB']) {
    if (value < 1024) {
      break
    }
    value /= 1024
    unit = next
  }
  return `${value.toFixed(1)} ${unit}`
}

function rwx(bits: number): string {
  const r = (bits & 4) !== 0 ? 'r' : '-'
  const w = (bits & 2) !== 0 ? 'w' : '-'
  const x = (bits & 1) !== 0 ? 'x' : '-'
  return `${r}${w}${x}`
}

/** 权限展示：八进制 + 符号形式（如 644 rw-r--r--）。 */
function formatMode(mode: number): string {
  const octal = (mode & 0o7777).toString(8).padStart(3, '0')
  const symbolic = rwx((mode >> 6) & 7) + rwx((mode >> 3) & 7) + rwx(mode & 7)
  return `${octal} ${symbolic}`
}

/** 行内「更多」菜单命令分发。 */
function handleCommand(command: string, entry: FileEntry): void {
  switch (command) {
    case 'rename':
      emit('rename', entry)
      break
    case 'copy':
      emit('copy', entry)
      break
    case 'chmod':
      emit('chmod', entry)
      break
    case 'chown':
      emit('chown', entry)
      break
    case 'extract':
      emit('extract', entry)
      break
    case 'delete':
      emit('delete', entry)
      break
    default:
      break
  }
}
</script>

<template>
  <el-table
    :data="pagedEntries"
    v-loading="loading"
    row-key="name"
    class="file-list"
    @selection-change="(rows: FileEntry[]) => emit('selection-change', rows)"
  >
    <el-table-column type="selection" width="40" />
    <el-table-column label="名称" min-width="260">
      <template #default="{ row }">
        <span class="file-list__name">
          <el-icon class="file-list__icon" :class="{ 'file-list__icon--dir': row.type === 'dir' }">
            <component :is="iconFor(row)" />
          </el-icon>
          <el-link
            v-if="row.type === 'dir' || row.type === 'file'"
            type="primary"
            underline="never"
            @click="emit('open', row)"
          >
            {{ row.name }}
          </el-link>
          <span v-else>{{ row.name }}</span>
        </span>
      </template>
    </el-table-column>
    <el-table-column label="类型" width="96">
      <template #default="{ row }">
        <el-tag size="small" effect="plain" :type="row.type === 'dir' ? 'warning' : 'info'">
          {{ typeText(row.type) }}
        </el-tag>
      </template>
    </el-table-column>
    <el-table-column label="大小" width="110" align="right">
      <template #default="{ row }">
        {{ row.type === 'file' ? formatBytes(row.size) : '—' }}
      </template>
    </el-table-column>
    <el-table-column label="权限" width="160">
      <template #default="{ row }">
        <code class="file-list__mode">{{ formatMode(row.mode) }}</code>
      </template>
    </el-table-column>
    <el-table-column label="所有者" width="110">
      <template #default="{ row }">{{ row.uid }}:{{ row.gid }}</template>
    </el-table-column>
    <el-table-column label="修改时间" width="170">
      <template #default="{ row }">{{ formatDateTime(row.mtime * 1000) }}</template>
    </el-table-column>
    <el-table-column label="操作" width="220" fixed="right">
      <template #default="{ row }">
        <el-button
          v-if="row.type === 'file'"
          link
          type="primary"
          size="small"
          @click="emit('open', row)"
        >
          {{ isImageName(row.name) ? '预览' : '编辑' }}
        </el-button>
        <el-button
          v-if="row.type === 'file' || row.type === 'dir'"
          link
          type="primary"
          size="small"
          @click="emit('download', row)"
        >
          下载
        </el-button>
        <el-dropdown
          class="file-list__more"
          trigger="click"
          @command="(command: string) => handleCommand(command, row)"
        >
          <el-button link type="primary" size="small">
            更多
            <el-icon><ArrowDown /></el-icon>
          </el-button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item command="rename">重命名/移动</el-dropdown-item>
              <el-dropdown-item command="copy">复制</el-dropdown-item>
              <el-dropdown-item command="chmod">修改权限</el-dropdown-item>
              <el-dropdown-item command="chown">修改所有者</el-dropdown-item>
              <el-dropdown-item
                v-if="row.type === 'file' && guessArchiveFormat(row.name) !== null"
                command="extract"
              >
                解压
              </el-dropdown-item>
              <el-dropdown-item command="delete" divided>
                <span class="file-list__danger">删除</span>
              </el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </template>
    </el-table-column>
    <template #empty>
      <el-empty description="该目录为空" :image-size="60" />
    </template>
  </el-table>
  <div v-if="sortedEntries.length > PAGE_SIZE" class="file-list__pager">
    <span class="file-list__pager-total">共 {{ sortedEntries.length }} 项</span>
    <el-pagination
      v-model:current-page="currentPage"
      layout="prev, pager, next, jumper"
      :page-size="PAGE_SIZE"
      :total="sortedEntries.length"
    />
  </div>
</template>

<style scoped>
.file-list__name {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  max-width: 100%;
}

.file-list__icon {
  color: var(--el-text-color-secondary);
  flex-shrink: 0;
}

.file-list__icon--dir {
  color: var(--el-color-warning);
}

.file-list__mode {
  font-size: 12px;
  color: var(--el-text-color-regular);
}

.file-list__more {
  margin-left: 8px;
  vertical-align: middle;
}

.file-list__danger {
  color: var(--el-color-danger);
}

.file-list__pager {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 12px;
  margin-top: 12px;
}

.file-list__pager-total {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
</style>
