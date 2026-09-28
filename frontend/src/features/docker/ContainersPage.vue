<script setup lang="ts">
// 容器管理:列表 + 生命周期操作(启动/停止/重启/暂停/删除)+ 日志抽屉(快照回放 + 实时跟随)。
import { computed, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { ArrowDown } from '@element-plus/icons-vue'
import { useQuery } from '@tanstack/vue-query'

import type { ApiError } from '@/api/client'
import {
  containerAction,
  containerLogStreamUrl,
  dockerLogLine,
  formatBytes,
  formatPorts,
  getAllContainerStats,
  getDockerInfo,
  isDockerUnavailable,
  listContainers,
} from '@/api/docker'
import type { ContainerLifecycleAction, ContainerSummary } from '@/api/docker'
import { problemMessage } from '@/api/problem'
import ConfirmDangerDialog from '@/components/ConfirmDangerDialog.vue'
import LogViewer from '@/components/LogViewer.vue'
import ReauthDialog from '@/components/ReauthDialog.vue'
import { formatDateTime } from '@/composables/format'
import ContainerDetail from './ContainerDetail.vue'

/** 是否包含已停止的容器（默认全量展示，面板场景更常用）。 */
const showAll = ref(true)

/** 当前打开详情的容器 id；非空时以详情视图替换列表。 */
const detailId = ref<string | null>(null)

const infoQuery = useQuery({ queryKey: ['docker-info'], queryFn: getDockerInfo })
const { data, isPending, isError, error, refetch } = useQuery({
  queryKey: ['docker-containers', showAll],
  queryFn: () => listContainers(showAll.value),
})

const containers = computed(() => data.value?.containers ?? [])
const info = computed(() => infoQuery.data.value ?? null)
const errorMessage = computed(() => problemMessage(error.value, '获取容器列表失败'))

/** 守护进程不可用:info 明确报告 available=false,或列表请求返回 503 docker.unavailable。 */
const dockerUnavailable = computed(
  () => info.value?.available === false || isDockerUnavailable(error.value),
)

// —— 批量资源占用（仅列表视图可见且 docker 可用时轮询；失败静默，列显示 —） ——

const statsQuery = useQuery({
  queryKey: ['docker-stats'],
  queryFn: getAllContainerStats,
  refetchInterval: 8000,
  retry: false,
  enabled: computed(() => detailId.value === null && !dockerUnavailable.value),
})

/** 完整容器 id → 资源快照（列表 id 与 helper 返回的键都是完整 id，直接对齐）。 */
const statsMap = computed(() => statsQuery.data.value?.stats ?? {})

/** 该行是否有 stats 数据（运行中且采样已返回）。 */
function hasStats(row: ContainerSummary): boolean {
  return statsMap.value[row.id] !== undefined
}

/** CPU 百分比文本（如 12.3%）；无数据显示 —。 */
function cpuText(row: ContainerSummary): string {
  const snapshot = statsMap.value[row.id]
  return snapshot ? `${snapshot.cpu_percent.toFixed(1)}%` : '—'
}

/** CPU 进度条数值（超过 100 截断，多核容器可能超额）。 */
function cpuPercent(row: ContainerSummary): number {
  const snapshot = statsMap.value[row.id]
  return snapshot ? Math.min(100, snapshot.cpu_percent) : 0
}

/** 内存主文本：已用字节人性化；无数据显示 —。 */
function memText(row: ContainerSummary): string {
  const snapshot = statsMap.value[row.id]
  return snapshot ? formatBytes(snapshot.mem_used) : '—'
}

/** 内存百分比小字（相对 limit）；无 limit 时为空。 */
function memPercentText(row: ContainerSummary): string {
  const snapshot = statsMap.value[row.id]
  return snapshot && snapshot.mem_limit > 0 ? `${snapshot.mem_percent.toFixed(1)}%` : ''
}

async function refresh(): Promise<void> {
  await Promise.all([refetch(), infoQuery.refetch()])
}

/** 从错误对象里取 problem code（用于按错误码分支处理）。 */
function problemCode(err: unknown): string | undefined {
  return (err as ApiError | undefined)?.problem?.code
}

// —— 状态展示 ——

interface StateMeta {
  text: string
  tagType: 'success' | 'info' | 'warning' | 'danger'
}

const STATE_META: Record<string, StateMeta> = {
  running: { text: '运行中', tagType: 'success' },
  paused: { text: '已暂停', tagType: 'warning' },
  restarting: { text: '重启中', tagType: 'warning' },
  removing: { text: '删除中', tagType: 'warning' },
  created: { text: '已创建', tagType: 'info' },
  exited: { text: '已退出', tagType: 'info' },
  dead: { text: '已死亡', tagType: 'danger' },
}

function stateMeta(state: string): StateMeta {
  return STATE_META[state] ?? { text: state || '未知', tagType: 'info' }
}

// —— 生命周期操作 ——

const ACTION_TEXT: Record<ContainerLifecycleAction, string> = {
  start: '启动',
  stop: '停止',
  restart: '重启',
  pause: '暂停',
  unpause: '恢复',
  kill: '强制终止',
  remove: '删除',
}

/** 正在执行的操作标识（`${id}:${action}`），用于按钮 loading。 */
const actionBusy = ref<string | null>(null)

async function runAction(row: ContainerSummary, action: ContainerLifecycleAction): Promise<void> {
  actionBusy.value = `${row.id}:${action}`
  try {
    await containerAction(row.id, action)
    ElMessage.success(`容器「${row.name}」已${ACTION_TEXT[action]}`)
    await refresh()
  } catch (err) {
    if (isDockerUnavailable(err)) {
      ElMessage.error('Docker 守护进程不可用')
      await refresh()
    } else {
      ElMessage.error(problemMessage(err, `${ACTION_TEXT[action]}容器失败`))
    }
  } finally {
    actionBusy.value = null
  }
}

/** 该行是否有生命周期操作进行中（用于「更多」触发按钮 loading）。 */
function isRowBusy(row: ContainerSummary): boolean {
  return actionBusy.value !== null && actionBusy.value.startsWith(`${row.id}:`)
}

/** 「更多」下拉命令分发：删除走名称确认对话框，其余走 runAction。 */
function onMoreCommand(row: ContainerSummary, command: string): void {
  if (command === 'remove') {
    openRemove(row)
  } else if (command === 'pause' || command === 'restart' || command === 'stop') {
    void runAction(row, command)
  }
}

// —— 删除容器（输入名称确认 + 近期重认证） ——

const removeVisible = ref(false)
const removeLoading = ref(false)
const removeTarget = ref<ContainerSummary | null>(null)
const reauthVisible = ref(false)

function openRemove(row: ContainerSummary): void {
  removeTarget.value = row
  removeVisible.value = true
}

async function handleRemove(): Promise<void> {
  const target = removeTarget.value
  if (!target) {
    return
  }
  removeLoading.value = true
  try {
    await containerAction(target.id, 'remove')
    ElMessage.success(`容器「${target.name}」已删除`)
    removeVisible.value = false
    await refresh()
  } catch (err) {
    if (problemCode(err) === 'auth.reauth_required') {
      reauthVisible.value = true
    } else if (problemCode(err) === 'conflict') {
      ElMessage.error('容器仍在运行，请先停止后再删除')
    } else {
      ElMessage.error(problemMessage(err, '删除容器失败'))
    }
  } finally {
    removeLoading.value = false
  }
}

// —— 日志抽屉（快照回放 + 实时跟随，经 LogViewer 内部 SSE） ——

const logsVisible = ref(false)
const logsTarget = ref<ContainerSummary | null>(null)

/** 日志流地址:后端 follow 模式先回放最近 200 行再实时跟随。 */
const logSource = computed(() =>
  logsTarget.value ? containerLogStreamUrl(logsTarget.value.id, 200) : null,
)

function openLogs(row: ContainerSummary): void {
  logsTarget.value = row
  logsVisible.value = true
}

// —— 详情视图（行点击进入，返回时刷新列表） ——

function openDetail(row: ContainerSummary): void {
  detailId.value = row.id
}

/** 行点击进入详情；行内按钮的点击不触发（避免与操作列冲突）。 */
function onRowClick(row: ContainerSummary, _column: unknown, event: MouseEvent): void {
  const target = event.target as HTMLElement | null
  if (target?.closest('button')) {
    return
  }
  openDetail(row)
}

async function onDetailChanged(): Promise<void> {
  await refresh()
}

function closeDetail(): void {
  detailId.value = null
  void refresh()
}
</script>

<template>
  <div class="containers-page">
    <ContainerDetail
      v-if="detailId"
      :container-id="detailId"
      @back="closeDetail"
      @changed="onDetailChanged"
    />
    <template v-else>
      <el-card shadow="never">
        <template #header>
          <div class="containers-page__header">
            <div class="containers-page__meta">
              <span class="containers-page__title">容器</span>
              <el-tag v-if="info?.available" size="small" type="info" effect="plain">
                Docker {{ info.version }} · 运行中 {{ info.running }}/{{ info.containers }}
              </el-tag>
            </div>
            <div class="containers-page__actions">
              <el-switch v-model="showAll" active-text="含已停止" />
              <el-button link type="primary" :loading="isPending" @click="refresh">刷新</el-button>
            </div>
          </div>
        </template>

        <el-skeleton v-if="isPending" :rows="5" animated />
        <el-empty v-else-if="dockerUnavailable" description="Docker 守护进程不可用">
          <p class="containers-page__hint">请确认 Docker 服务已在主机上启动，然后重试。</p>
          <el-button type="primary" @click="refresh">重试</el-button>
        </el-empty>
        <el-alert
          v-else-if="isError"
          type="error"
          :title="errorMessage"
          :closable="false"
          show-icon
        />
        <el-table v-else :data="containers" class="containers-page__table" @row-click="onRowClick">
          <template #empty>
            <el-empty description="暂无容器" />
          </template>
          <el-table-column label="名称" min-width="160" show-overflow-tooltip>
            <template #default="{ row }">
              <span class="containers-page__name">{{ row.name }}</span>
            </template>
          </el-table-column>
          <el-table-column prop="image" label="镜像" min-width="180" show-overflow-tooltip />
          <el-table-column label="状态" width="100">
            <template #default="{ row }">
              <el-tag size="small" :type="stateMeta(row.state).tagType" effect="plain">
                {{ stateMeta(row.state).text }}
              </el-tag>
            </template>
          </el-table-column>
          <!-- CPU/内存来自 /docker/stats 8s 轮询；非运行中或暂无数据显示 — -->
          <el-table-column label="CPU" width="110">
            <template #default="{ row }">
              <div v-if="hasStats(row)" class="containers-page__stat">
                <span>{{ cpuText(row) }}</span>
                <el-progress
                  :percentage="cpuPercent(row)"
                  :show-text="false"
                  :stroke-width="4"
                />
              </div>
              <span v-else>—</span>
            </template>
          </el-table-column>
          <el-table-column label="内存" width="120">
            <template #default="{ row }">
              <div v-if="hasStats(row)" class="containers-page__stat">
                <span>{{ memText(row) }}</span>
                <span class="containers-page__stat-sub">{{ memPercentText(row) }}</span>
              </div>
              <span v-else>—</span>
            </template>
          </el-table-column>
          <el-table-column prop="status" label="详情" min-width="140" show-overflow-tooltip />
          <el-table-column label="端口" min-width="160" show-overflow-tooltip>
            <template #default="{ row }">{{ formatPorts(row.ports) || '—' }}</template>
          </el-table-column>
          <el-table-column label="创建时间" width="170">
            <template #default="{ row }">{{ formatDateTime(row.created * 1000) }}</template>
          </el-table-column>
          <el-table-column label="操作" width="210" fixed="right">
            <template #default="{ row }">
              <el-button link type="primary" @click="openDetail(row)">详情</el-button>
              <el-button link type="primary" @click="openLogs(row)">日志</el-button>
              <el-button
                v-if="row.state !== 'running' && row.state !== 'paused'"
                link
                type="success"
                :loading="actionBusy === `${row.id}:start`"
                @click="runAction(row, 'start')"
              >
                启动
              </el-button>
              <el-button
                v-if="row.state === 'paused'"
                link
                type="success"
                :loading="actionBusy === `${row.id}:unpause`"
                @click="runAction(row, 'unpause')"
              >
                恢复
              </el-button>
              <!-- 暂停/重启/停止/删除收入下拉；菜单渲染在 body 弹层,不会触发行点击 -->
              <el-dropdown
                class="containers-page__more"
                trigger="click"
                @command="(command: string) => onMoreCommand(row, command)"
              >
                <el-button link type="primary" :loading="isRowBusy(row)">
                  更多
                  <el-icon><ArrowDown /></el-icon>
                </el-button>
                <template #dropdown>
                  <el-dropdown-menu>
                    <el-dropdown-item command="pause" :disabled="row.state !== 'running'">
                      暂停
                    </el-dropdown-item>
                    <el-dropdown-item
                      command="restart"
                      :disabled="row.state !== 'running' && row.state !== 'paused'"
                    >
                      重启
                    </el-dropdown-item>
                    <el-dropdown-item
                      command="stop"
                      :disabled="row.state !== 'running' && row.state !== 'paused'"
                    >
                      停止
                    </el-dropdown-item>
                    <el-dropdown-item command="remove" divided>
                      <span class="containers-page__danger">删除</span>
                    </el-dropdown-item>
                  </el-dropdown-menu>
                </template>
              </el-dropdown>
            </template>
          </el-table-column>
        </el-table>
      </el-card>

      <!-- 日志抽屉:destroy-on-close 保证关闭时卸载 LogViewer 并断开 SSE -->
      <el-drawer
        v-model="logsVisible"
        :title="`容器日志 — ${logsTarget?.name ?? ''}`"
        size="55%"
        destroy-on-close
        @closed="logsTarget = null"
      >
        <LogViewer
          v-if="logsTarget"
          :source="logSource"
          :source-topics="['log']"
          :format="dockerLogLine"
          height="calc(100vh - 180px)"
        />
      </el-drawer>

      <!-- 删除确认（输入容器名） + 重认证 -->
      <ConfirmDangerDialog
        v-model="removeVisible"
        title="删除容器"
        :resource-name="removeTarget?.name ?? ''"
        message="删除后容器可写层中的数据不可恢复；运行中的容器请先停止。"
        :loading="removeLoading"
        @confirm="handleRemove"
      />
      <ReauthDialog v-model="reauthVisible" @success="handleRemove" />
    </template>
  </div>
</template>

<style scoped>
.containers-page__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.containers-page__meta {
  display: flex;
  align-items: center;
  gap: 12px;
}

.containers-page__title {
  font-weight: 600;
}

.containers-page__actions {
  display: flex;
  align-items: center;
  gap: 16px;
}

.containers-page__name {
  font-weight: 500;
}

.containers-page__table :deep(.el-table__row) {
  cursor: pointer;
}

.containers-page__stat {
  display: flex;
  flex-direction: column;
  gap: 2px;
  line-height: 1.4;
}

.containers-page__stat-sub {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.containers-page__more {
  margin-left: 8px;
  vertical-align: middle;
}

.containers-page__danger {
  color: var(--el-color-danger);
}

.containers-page__hint {
  margin: 0 0 12px;
  color: var(--el-text-color-secondary);
  font-size: 13px;
}
</style>
