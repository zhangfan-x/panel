<script setup lang="ts">
// 进程管理：可搜索/排序/分页的进程列表 + 详情抽屉 + 发送信号（终止/强杀）与优先级调整。
import { computed, ref } from 'vue'
import { keepPreviousData, useQuery } from '@tanstack/vue-query'
import { ElMessage } from 'element-plus'
import { Search } from '@element-plus/icons-vue'

import type { ApiError } from '@/api/client'
import { problemMessage } from '@/api/problem'
import { getProcess, listProcesses, reniceProcess, signalProcess } from '@/api/processes'
import type {
  ListProcessesParams,
  ProcessRow,
  ProcessSignal,
  ProcessSortField,
  ProcessSortOrder,
} from '@/api/processes'
import ConfirmDangerDialog from '@/components/ConfirmDangerDialog.vue'
import ReauthDialog from '@/components/ReauthDialog.vue'
import { formatDateTime } from '@/composables/format'
import { useAuthStore } from '@/stores/auth'

/** 自动刷新间隔（毫秒）。 */
const AUTO_REFRESH_MS = 5000

/** 服务端支持排序的列（其余列不提供排序，避免后端 422）。 */
const SERVER_SORT_FIELDS: readonly ProcessSortField[] = ['pid', 'cpu', 'mem', 'name']

type TagType = 'primary' | 'success' | 'warning' | 'info' | 'danger'

interface StatusMeta {
  text: string
  type: TagType
}

/** psutil 常见进程状态的中文文案与标签配色（未知状态原样展示）。 */
const STATUS_META: Record<string, StatusMeta> = {
  running: { text: '运行', type: 'success' },
  sleeping: { text: '睡眠', type: 'info' },
  'disk-sleep': { text: '磁盘等待', type: 'warning' },
  stopped: { text: '已停止', type: 'warning' },
  'tracing-stop': { text: '跟踪停止', type: 'warning' },
  zombie: { text: '僵尸', type: 'danger' },
  dead: { text: '已退出', type: 'danger' },
  idle: { text: '空闲', type: 'info' },
  waking: { text: '唤醒中', type: 'info' },
  parked: { text: '挂起', type: 'info' },
}

/** 信号/优先级操作的目标进程。 */
interface ProcessTarget {
  pid: number
  name: string
}

const auth = useAuthStore()

// ---- 列表查询状态 ----
const searchInput = ref('')
const search = ref('')
const sort = ref<ProcessSortField>('cpu')
const order = ref<ProcessSortOrder>('desc')
const page = ref(1)
const pageSize = ref(50)
const autoRefresh = ref(false)

const queryParams = computed<ListProcessesParams>(() => ({
  search: search.value,
  sort: sort.value,
  order: order.value,
  limit: pageSize.value,
  offset: (page.value - 1) * pageSize.value,
}))

const {
  data: listData,
  isPending,
  isFetching,
  isError: isListError,
  error: listError,
  refetch,
} = useQuery({
  queryKey: ['processes', queryParams],
  queryFn: () => listProcesses(queryParams.value),
  placeholderData: keepPreviousData,
  refetchInterval: computed(() => (autoRefresh.value ? AUTO_REFRESH_MS : false)),
})

const rows = computed<ProcessRow[]>(() => listData.value?.processes ?? [])
const total = computed(() => listData.value?.total ?? 0)
const listErrorMessage = computed(() => problemMessage(listError.value, '获取进程列表失败'))

function statusMeta(status: string): StatusMeta {
  return STATUS_META[status] ?? { text: status || '未知', type: 'info' }
}

function percentText(value: number): string {
  return `${value.toFixed(1)}%`
}

/** 把字节数格式化为人读单位。 */
function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) {
    return '未知'
  }
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB']
  let value = bytes
  let index = 0
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024
    index += 1
  }
  const text = index === 0 ? String(value) : value.toFixed(1)
  return `${text} ${units[index] ?? 'B'}`
}

function handleSearch(): void {
  search.value = searchInput.value.trim()
  page.value = 1
}

function handleRefresh(): void {
  void refetch()
}

function handleSizeChange(): void {
  page.value = 1
}

/** el-table 服务端排序：非法列或取消排序时回到默认的 CPU 降序。 */
function handleSortChange(event: { prop: string | null; order: string | null }): void {
  const { prop, order: tableOrder } = event
  const isServerField = prop !== null && (SERVER_SORT_FIELDS as readonly string[]).includes(prop)
  if (isServerField && tableOrder !== null) {
    sort.value = prop as ProcessSortField
    order.value = tableOrder === 'ascending' ? 'asc' : 'desc'
  } else {
    sort.value = 'cpu'
    order.value = 'desc'
  }
  page.value = 1
}

// ---- 详情抽屉 ----
const detailVisible = ref(false)
const detailPid = ref<number | null>(null)

const {
  data: detail,
  isPending: detailPending,
  isError: isDetailError,
  error: detailError,
  refetch: refetchDetail,
} = useQuery({
  queryKey: ['process-detail', detailPid],
  queryFn: () => getProcess(detailPid.value ?? 0),
  enabled: computed(() => detailVisible.value && detailPid.value !== null),
})

const detailTitle = computed(() =>
  detailPid.value !== null ? `进程详情 #${detailPid.value}` : '进程详情',
)
const detailErrorMessage = computed(() => problemMessage(detailError.value, '获取进程详情失败'))

const detailItems = computed(() => {
  const d = detail.value
  if (!d) {
    return []
  }
  return [
    { label: 'PID', value: String(d.pid) },
    { label: '父进程 PID', value: String(d.ppid) },
    { label: '名称', value: d.name || '—' },
    { label: '用户', value: d.user || '—' },
    { label: '状态', value: `${statusMeta(d.status).text}（${d.status}）` },
    { label: 'CPU 占用', value: percentText(d.cpu) },
    { label: '内存占用', value: percentText(d.mem) },
    { label: '常驻内存 RSS', value: formatBytes(d.rss) },
    { label: '虚拟内存 VMS', value: formatBytes(d.vms) },
    { label: '线程数', value: String(d.num_threads) },
    { label: '优先级 nice', value: String(d.nice) },
    { label: '启动时间', value: formatDateTime(d.started) },
    { label: '可执行文件', value: d.exe || '—' },
    { label: '工作目录', value: d.cwd || '—' },
    { label: '命令行', value: d.cmdline || '—' },
    { label: '子进程', value: d.children.length > 0 ? d.children.join(', ') : '无' },
  ]
})

function openDetail(row: ProcessRow): void {
  detailPid.value = row.pid
  detailVisible.value = true
}

// ---- 信号（终止/强杀）----
const signalTarget = ref<ProcessTarget | null>(null)
const signalType = ref<ProcessSignal>('SIGTERM')
const signalConfirmVisible = ref(false)
const signalLoading = ref(false)
const reauthVisible = ref(false)

const signalDialogTitle = computed(() =>
  signalType.value === 'SIGKILL' ? '强制终止进程' : '终止进程',
)

/** 确认输入使用进程名；名称为空（如内核线程）时退化为 pid，避免空串直接可确认。 */
const signalResourceName = computed(() => {
  const target = signalTarget.value
  if (!target) {
    return ''
  }
  return target.name || String(target.pid)
})

const signalDialogMessage = computed(() => {
  const target = signalTarget.value
  if (!target) {
    return ''
  }
  const base = `即将向进程 ${target.pid}（${target.name || '未知名称'}）发送 ${signalType.value} 信号。`
  return signalType.value === 'SIGKILL'
    ? `${base}该信号无法被捕获，进程会被立即杀死，未保存的数据将丢失。`
    : `${base}进程将收到终止请求，可完成清理后自行退出。`
})

function rowTarget(row: ProcessRow): ProcessTarget {
  return { pid: row.pid, name: row.name }
}

function openSignalDialog(target: ProcessTarget, signal: ProcessSignal): void {
  signalTarget.value = target
  signalType.value = signal
  signalConfirmVisible.value = true
}

/** 危险确认通过后：重认证窗口未开启则先验证身份，成功后再发送信号。 */
function handleSignalConfirm(): void {
  if (auth.reauthRecent) {
    void performSignal()
  } else {
    reauthVisible.value = true
  }
}

function handleReauthSuccess(): void {
  void performSignal()
}

function isReauthRequired(error: unknown): boolean {
  return (error as ApiError | null)?.problem?.code === 'auth.reauth_required'
}

async function performSignal(): Promise<void> {
  const target = signalTarget.value
  if (!target || signalLoading.value) {
    return
  }
  signalLoading.value = true
  try {
    await signalProcess(target.pid, signalType.value)
    ElMessage.success(`已向进程 ${target.pid} 发送 ${signalType.value}`)
    signalConfirmVisible.value = false
    void refetch()
  } catch (error) {
    if (isReauthRequired(error)) {
      // 重认证窗口已过期：同步 store 状态并重新弹出身份验证，成功后自动重试。
      auth.reauthRecent = false
      reauthVisible.value = true
    } else {
      ElMessage.error(problemMessage(error, '发送信号失败'))
    }
  } finally {
    signalLoading.value = false
  }
}

// ---- 优先级调整 ----
const reniceVisible = ref(false)
const reniceTarget = ref<ProcessTarget | null>(null)
const reniceValue = ref<number | undefined>(0)
const reniceLoading = ref(false)

function openReniceDialog(target: ProcessTarget, currentNice = 0): void {
  reniceTarget.value = target
  reniceValue.value = currentNice
  reniceVisible.value = true
}

async function handleRenice(): Promise<void> {
  const target = reniceTarget.value
  const nice = reniceValue.value
  if (!target || reniceLoading.value) {
    return
  }
  if (nice === undefined || !Number.isInteger(nice) || nice < -20 || nice > 19) {
    ElMessage.warning('请输入 -20 到 19 之间的整数 nice 值')
    return
  }
  reniceLoading.value = true
  try {
    const result = await reniceProcess(target.pid, nice)
    ElMessage.success(`进程 ${target.pid} 的 nice 值已调整为 ${result.nice}`)
    reniceVisible.value = false
    void refetch()
    if (detailVisible.value && detailPid.value === target.pid) {
      void refetchDetail()
    }
  } catch (error) {
    ElMessage.error(problemMessage(error, '调整优先级失败'))
  } finally {
    reniceLoading.value = false
  }
}

// ---- 抽屉内操作 ----
function handleDetailSignal(signal: ProcessSignal): void {
  const d = detail.value
  if (d) {
    openSignalDialog({ pid: d.pid, name: d.name }, signal)
  }
}

function handleDetailRenice(): void {
  const d = detail.value
  if (d) {
    openReniceDialog({ pid: d.pid, name: d.name }, d.nice)
  }
}
</script>

<template>
  <div class="processes-page">
    <el-card shadow="never">
      <template #header>
        <div class="processes-page__header">
          <span class="processes-page__heading">进程列表</span>
          <div class="processes-page__header-actions">
            <el-switch v-model="autoRefresh" size="small" active-text="自动刷新" />
            <el-button link type="primary" :loading="isFetching" @click="handleRefresh">
              刷新
            </el-button>
          </div>
        </div>
      </template>

      <div class="processes-page__toolbar">
        <el-input
          v-model="searchInput"
          class="processes-page__search"
          placeholder="按进程名或命令行搜索"
          clearable
          @clear="handleSearch"
          @keyup.enter="handleSearch"
        >
          <template #append>
            <el-button :icon="Search" @click="handleSearch" />
          </template>
        </el-input>
        <span class="processes-page__hint">CPU 为两次刷新之间的平均占用，首次加载可能显示 0</span>
      </div>

      <el-skeleton v-if="isPending" :rows="8" animated />
      <el-alert
        v-else-if="isListError"
        type="error"
        :title="listErrorMessage"
        :closable="false"
        show-icon
      />
      <template v-else>
        <el-table
          :data="rows"
          row-key="pid"
          :default-sort="{ prop: 'cpu', order: 'descending' }"
          empty-text="暂无匹配的进程"
          class="processes-page__table"
          @sort-change="handleSortChange"
        >
          <el-table-column prop="pid" label="PID" width="90" sortable="custom" />
          <el-table-column
            prop="name"
            label="名称"
            min-width="140"
            sortable="custom"
            show-overflow-tooltip
          />
          <el-table-column prop="user" label="用户" width="110" show-overflow-tooltip />
          <el-table-column prop="cpu" label="CPU" width="100" sortable="custom">
            <template #default="{ row }">{{ percentText(row.cpu) }}</template>
          </el-table-column>
          <el-table-column prop="mem" label="内存" width="100" sortable="custom">
            <template #default="{ row }">{{ percentText(row.mem) }}</template>
          </el-table-column>
          <el-table-column prop="status" label="状态" width="110">
            <template #default="{ row }">
              <el-tag size="small" :type="statusMeta(row.status).type">
                {{ statusMeta(row.status).text }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="启动时间" width="170">
            <template #default="{ row }">{{ formatDateTime(row.started) }}</template>
          </el-table-column>
          <el-table-column prop="cmdline" label="命令行" min-width="220" show-overflow-tooltip />
          <el-table-column label="操作" width="230" fixed="right">
            <template #default="{ row }">
              <el-button link type="primary" size="small" @click="openDetail(row)">详情</el-button>
              <el-button
                link
                type="danger"
                size="small"
                @click="openSignalDialog(rowTarget(row), 'SIGTERM')"
              >
                终止
              </el-button>
              <el-button
                link
                type="danger"
                size="small"
                @click="openSignalDialog(rowTarget(row), 'SIGKILL')"
              >
                强杀
              </el-button>
              <el-button link type="warning" size="small" @click="openReniceDialog(rowTarget(row))">
                优先级
              </el-button>
            </template>
          </el-table-column>
        </el-table>

        <div class="processes-page__footer">
          <el-pagination
            v-model:current-page="page"
            v-model:page-size="pageSize"
            :total="total"
            :page-sizes="[50, 100, 200, 500]"
            layout="total, sizes, prev, pager, next"
            @size-change="handleSizeChange"
          />
        </div>
      </template>
    </el-card>

    <el-drawer v-model="detailVisible" :title="detailTitle" size="520px">
      <el-skeleton v-if="detailPending" :rows="8" animated />
      <el-alert
        v-else-if="isDetailError"
        type="error"
        :title="detailErrorMessage"
        :closable="false"
        show-icon
      />
      <el-descriptions v-else-if="detail" :column="1" border size="small">
        <el-descriptions-item v-for="item in detailItems" :key="item.label" :label="item.label">
          {{ item.value }}
        </el-descriptions-item>
      </el-descriptions>
      <template #footer>
        <div class="processes-page__drawer-actions">
          <el-button :disabled="!detail" @click="handleDetailRenice">调整优先级</el-button>
          <el-button type="danger" plain :disabled="!detail" @click="handleDetailSignal('SIGTERM')">
            终止
          </el-button>
          <el-button type="danger" :disabled="!detail" @click="handleDetailSignal('SIGKILL')">
            强杀
          </el-button>
        </div>
      </template>
    </el-drawer>

    <ConfirmDangerDialog
      v-model="signalConfirmVisible"
      :title="signalDialogTitle"
      :resource-name="signalResourceName"
      :message="signalDialogMessage"
      :loading="signalLoading"
      @confirm="handleSignalConfirm"
    />

    <ReauthDialog
      v-model="reauthVisible"
      message="发送进程信号属于敏感操作，请输入当前登录密码确认身份。"
      @success="handleReauthSuccess"
    />

    <el-dialog
      v-model="reniceVisible"
      title="调整进程优先级"
      width="380px"
      :close-on-click-modal="false"
    >
      <p class="processes-page__renice-hint">
        为进程 {{ reniceTarget?.pid }}（{{ reniceTarget?.name || '未知名称' }}）设置新的 nice
        值：数值越小优先级越高（-20 最高，19 最低）。
      </p>
      <el-input-number v-model="reniceValue" :min="-20" :max="19" :step="1" step-strictly />
      <template #footer>
        <el-button @click="reniceVisible = false">取消</el-button>
        <el-button type="primary" :loading="reniceLoading" @click="handleRenice">确认</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.processes-page__header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.processes-page__heading {
  font-size: 16px;
  font-weight: 600;
}

.processes-page__header-actions {
  display: flex;
  align-items: center;
  gap: 16px;
}

.processes-page__toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 16px;
}

.processes-page__search {
  width: 320px;
}

.processes-page__hint {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.processes-page__footer {
  margin-top: 16px;
  display: flex;
  justify-content: flex-end;
}

.processes-page__drawer-actions {
  display: flex;
  justify-content: flex-end;
}

.processes-page__renice-hint {
  margin: 0 0 12px;
  color: var(--el-text-color-regular);
  font-size: 14px;
}
</style>
