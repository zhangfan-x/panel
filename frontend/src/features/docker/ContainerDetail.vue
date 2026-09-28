<script setup lang="ts">
// 容器详情：inspect 基本信息 + 实时资源统计（~2s 轮询 /stats）+ 日志抽屉 + 进入终端 + 生命周期操作。
import { computed, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { ArrowLeft, Refresh } from '@element-plus/icons-vue'
import { useQuery } from '@tanstack/vue-query'

import {
  containerAction,
  containerLogStreamUrl,
  dockerLogLine,
  formatBytes,
  getContainerInspect,
  getContainerStats,
  isDockerUnavailable,
  shortId,
} from '@/api/docker'
import type { ContainerLifecycleAction, PortBinding } from '@/api/docker'
import { problemMessage } from '@/api/problem'
import LogViewer from '@/components/LogViewer.vue'
import SecretInput from '@/components/SecretInput.vue'
import { formatDateTime } from '@/composables/format'
import ContainerTerminal from './ContainerTerminal.vue'

const props = defineProps<{
  /** 目标容器 id（来自列表行）。 */
  containerId: string
}>()

const emit = defineEmits<{
  /** 返回列表。 */
  (e: 'back'): void
  /** 容器状态发生变更（生命周期操作成功），供列表刷新。 */
  (e: 'changed'): void
}>()

const inspectQuery = useQuery({
  queryKey: computed(() => ['docker-container', props.containerId]),
  queryFn: () => getContainerInspect(props.containerId),
  retry: false,
})

const inspect = computed(() => inspectQuery.data.value ?? null)
const isRunning = computed(() => inspect.value?.state.running === true)
const errorMessage = computed(() => problemMessage(inspectQuery.error.value, '获取容器详情失败'))

// 运行中才轮询资源统计；停止的容器无有效实时数据。
const statsQuery = useQuery({
  queryKey: computed(() => ['docker-stats', props.containerId]),
  queryFn: () => getContainerStats(props.containerId),
  enabled: isRunning,
  refetchInterval: () => (isRunning.value ? 2000 : false),
  retry: false,
})

/** 资源统计条的展示值（未运行或尚无数据时为 null）。 */
const statsView = computed(() => {
  const s = statsQuery.data.value
  if (!s || !isRunning.value) {
    return null
  }
  return {
    cpu: `${s.cpu_percent.toFixed(1)}%`,
    mem: `${formatBytes(s.mem_used)} / ${formatBytes(s.mem_limit)}`,
    memPercent: `${s.mem_percent.toFixed(1)}%`,
    netRx: formatBytes(s.net_rx),
    netTx: formatBytes(s.net_tx),
    blkRead: formatBytes(s.blk_read),
    blkWrite: formatBytes(s.blk_write),
    pids: String(s.pids),
  }
})

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

const stateMeta = computed<StateMeta>(() => {
  const status = inspect.value?.state.status ?? ''
  return STATE_META[status] ?? { text: status || '未知', tagType: 'info' }
})

/** 命令行（entrypoint + cmd 拼接展示）。 */
const commandText = computed(() => {
  const info = inspect.value
  if (!info) {
    return '—'
  }
  const parts = [...(info.entrypoint ?? []), ...(info.cmd ?? [])]
  return parts.length > 0 ? parts.join(' ') : '—'
})

/** 端口映射：把 { "80/tcp": [{HostIp,HostPort}] } 拍平成「宿主->容器/协议」串。 */
const portList = computed<string[]>(() => {
  const ports = inspect.value?.ports ?? {}
  const out: string[] = []
  for (const [key, binds] of Object.entries(ports)) {
    if (!binds || binds.length === 0) {
      out.push(key)
      continue
    }
    for (const bind of binds as PortBinding[]) {
      const host =
        bind.HostIp && bind.HostIp !== '0.0.0.0' && bind.HostIp !== '::' ? `${bind.HostIp}:` : ''
      out.push(`${host}${bind.HostPort ?? ''}->${key}`)
    }
  }
  return out
})

const networkList = computed(() =>
  Object.entries(inspect.value?.networks ?? {}).map(([name, info]) => ({ name, ...info })),
)

// —— 环境变量（敏感值以密文态展示） ——

const SECRET_KEY_RE = /(PASS|PASSWORD|SECRET|TOKEN|KEY|CREDENTIAL|PRIVATE|APIKEY|ACCESS)/i

interface EnvVar {
  key: string
  value: string
  secret: boolean
}

const envVars = computed<EnvVar[]>(() =>
  (inspect.value?.env ?? []).map((entry) => {
    const idx = entry.indexOf('=')
    const key = idx >= 0 ? entry.slice(0, idx) : entry
    const value = idx >= 0 ? entry.slice(idx + 1) : ''
    return { key, value, secret: SECRET_KEY_RE.test(key) }
  }),
)

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

const actionBusy = ref<ContainerLifecycleAction | null>(null)

async function runAction(action: ContainerLifecycleAction): Promise<void> {
  actionBusy.value = action
  try {
    await containerAction(props.containerId, action)
    ElMessage.success(`已${ACTION_TEXT[action]}`)
    emit('changed')
    await inspectQuery.refetch()
  } catch (err) {
    if (isDockerUnavailable(err)) {
      ElMessage.error('Docker 守护进程不可用')
    } else {
      ElMessage.error(problemMessage(err, `${ACTION_TEXT[action]}容器失败`))
    }
  } finally {
    actionBusy.value = null
  }
}

// —— 日志抽屉 / 终端抽屉 ——

const logsVisible = ref(false)
const terminalVisible = ref(false)

const logSource = computed(() => containerLogStreamUrl(props.containerId, 200))

async function refresh(): Promise<void> {
  await inspectQuery.refetch()
}
</script>

<template>
  <div class="container-detail">
    <el-card shadow="never">
      <template #header>
        <div class="container-detail__header">
          <div class="container-detail__title">
            <el-button link :icon="ArrowLeft" @click="emit('back')">返回</el-button>
            <span class="container-detail__name">{{ inspect?.name || containerId }}</span>
            <el-tag v-if="inspect" size="small" :type="stateMeta.tagType" effect="plain">
              {{ stateMeta.text }}
            </el-tag>
          </div>
          <div class="container-detail__actions">
            <el-button link type="primary" :icon="Refresh" @click="refresh">刷新</el-button>
            <el-button size="small" @click="logsVisible = true">日志</el-button>
            <el-button
              size="small"
              type="primary"
              :disabled="!isRunning"
              @click="terminalVisible = true"
            >
              进入终端
            </el-button>
          </div>
        </div>
      </template>

      <el-skeleton v-if="inspectQuery.isPending.value" :rows="6" animated />
      <el-alert
        v-else-if="inspectQuery.isError.value"
        type="error"
        :title="errorMessage"
        :closable="false"
        show-icon
      />

      <template v-else-if="inspect">
        <!-- 生命周期操作条 -->
        <div class="container-detail__lifecycle">
          <el-button
            v-if="!isRunning && inspect.state.status !== 'paused'"
            size="small"
            type="success"
            :loading="actionBusy === 'start'"
            @click="runAction('start')"
          >
            启动
          </el-button>
          <el-button
            v-if="inspect.state.paused"
            size="small"
            type="success"
            :loading="actionBusy === 'unpause'"
            @click="runAction('unpause')"
          >
            恢复
          </el-button>
          <el-button
            v-if="isRunning && !inspect.state.paused"
            size="small"
            type="warning"
            :loading="actionBusy === 'pause'"
            @click="runAction('pause')"
          >
            暂停
          </el-button>
          <el-button
            v-if="isRunning"
            size="small"
            type="warning"
            :loading="actionBusy === 'restart'"
            @click="runAction('restart')"
          >
            重启
          </el-button>
          <el-button
            v-if="isRunning"
            size="small"
            type="danger"
            :loading="actionBusy === 'stop'"
            @click="runAction('stop')"
          >
            停止
          </el-button>
          <el-button
            v-if="isRunning"
            size="small"
            type="danger"
            plain
            :loading="actionBusy === 'kill'"
            @click="runAction('kill')"
          >
            强制终止
          </el-button>
        </div>

        <!-- 实时资源统计条 -->
        <div class="container-detail__stats">
          <div class="container-detail__stat">
            <span class="container-detail__stat-label">CPU</span>
            <span class="container-detail__stat-value">{{ statsView?.cpu ?? '—' }}</span>
          </div>
          <div class="container-detail__stat">
            <span class="container-detail__stat-label">内存</span>
            <span class="container-detail__stat-value">
              {{ statsView ? `${statsView.mem}（${statsView.memPercent}）` : '—' }}
            </span>
          </div>
          <div class="container-detail__stat">
            <span class="container-detail__stat-label">网络 ↓/↑</span>
            <span class="container-detail__stat-value">
              {{ statsView ? `${statsView.netRx} / ${statsView.netTx}` : '—' }}
            </span>
          </div>
          <div class="container-detail__stat">
            <span class="container-detail__stat-label">磁盘 读/写</span>
            <span class="container-detail__stat-value">
              {{ statsView ? `${statsView.blkRead} / ${statsView.blkWrite}` : '—' }}
            </span>
          </div>
          <div class="container-detail__stat">
            <span class="container-detail__stat-label">进程数</span>
            <span class="container-detail__stat-value">{{ statsView?.pids ?? '—' }}</span>
          </div>
        </div>

        <!-- 基本信息 -->
        <el-descriptions :column="2" border class="container-detail__desc">
          <el-descriptions-item label="容器 ID">
            <span class="container-detail__mono">{{ shortId(inspect.id) }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="镜像">{{ inspect.image }}</el-descriptions-item>
          <el-descriptions-item label="镜像 ID">
            <span class="container-detail__mono">{{ shortId(inspect.image_id) }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="创建时间">
            {{ inspect.created ? formatDateTime(inspect.created) : '—' }}
          </el-descriptions-item>
          <el-descriptions-item label="重启策略">
            {{ inspect.restart_policy || '无' }}
          </el-descriptions-item>
          <el-descriptions-item label="健康状态">
            {{ inspect.state.health || '—' }}
          </el-descriptions-item>
          <el-descriptions-item label="命令" :span="2">
            <span class="container-detail__mono">{{ commandText }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="端口" :span="2">
            <template v-if="portList.length">
              <el-tag
                v-for="port in portList"
                :key="port"
                size="small"
                effect="plain"
                class="container-detail__port"
              >
                {{ port }}
              </el-tag>
            </template>
            <span v-else>—</span>
          </el-descriptions-item>
        </el-descriptions>

        <!-- 网络 -->
        <h4 class="container-detail__section">网络</h4>
        <el-table v-if="networkList.length" :data="networkList" size="small" border>
          <el-table-column prop="name" label="网络" min-width="120" />
          <el-table-column prop="ip_address" label="IP 地址" min-width="140" />
          <el-table-column prop="gateway" label="网关" min-width="140" />
          <el-table-column prop="mac_address" label="MAC" min-width="150" />
        </el-table>
        <el-empty v-else :image-size="60" description="无网络连接" />

        <!-- 挂载 -->
        <h4 class="container-detail__section">挂载</h4>
        <el-table v-if="inspect.mounts.length" :data="inspect.mounts" size="small" border>
          <el-table-column prop="type" label="类型" width="90" />
          <el-table-column prop="source" label="源" min-width="200" show-overflow-tooltip />
          <el-table-column
            prop="destination"
            label="容器内路径"
            min-width="180"
            show-overflow-tooltip
          />
          <el-table-column label="读写" width="80">
            <template #default="{ row }">{{ row.rw ? '读写' : '只读' }}</template>
          </el-table-column>
        </el-table>
        <el-empty v-else :image-size="60" description="无挂载" />

        <!-- 环境变量（敏感值密文展示） -->
        <h4 class="container-detail__section">环境变量</h4>
        <el-table v-if="envVars.length" :data="envVars" size="small" border>
          <el-table-column prop="key" label="变量" min-width="180" show-overflow-tooltip />
          <el-table-column label="值" min-width="280">
            <template #default="{ row }">
              <SecretInput v-if="row.secret" :model-value="row.value" />
              <span v-else class="container-detail__mono">{{ row.value }}</span>
            </template>
          </el-table-column>
        </el-table>
        <el-empty v-else :image-size="60" description="无环境变量" />
      </template>
    </el-card>

    <!-- 日志抽屉：destroy-on-close 保证关闭时卸载 LogViewer 并断开 SSE -->
    <el-drawer
      v-model="logsVisible"
      :title="`容器日志 — ${inspect?.name ?? ''}`"
      size="55%"
      destroy-on-close
    >
      <LogViewer
        v-if="logsVisible"
        :source="logSource"
        :source-topics="['log']"
        :format="dockerLogLine"
        height="calc(100vh - 180px)"
      />
    </el-drawer>

    <!-- 终端抽屉：destroy-on-close 保证关闭时卸载 ContainerTerminal 并断开 WebSocket -->
    <el-drawer
      v-model="terminalVisible"
      :title="`容器终端 — ${inspect?.name ?? ''}`"
      size="70%"
      destroy-on-close
    >
      <div class="container-detail__terminal">
        <ContainerTerminal
          v-if="terminalVisible"
          :container-id="containerId"
          :container-name="inspect?.name"
          @close="terminalVisible = false"
        />
      </div>
    </el-drawer>
  </div>
</template>

<style scoped>
.container-detail__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.container-detail__title {
  display: flex;
  align-items: center;
  gap: 12px;
}

.container-detail__name {
  font-weight: 600;
  font-size: 15px;
}

.container-detail__actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.container-detail__lifecycle {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 16px;
}

.container-detail__stats {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  margin-bottom: 20px;
}

.container-detail__stat {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 150px;
  padding: 10px 14px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  background: var(--el-fill-color-lighter);
}

.container-detail__stat-label {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.container-detail__stat-value {
  font-size: 16px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}

.container-detail__desc {
  margin-bottom: 8px;
}

.container-detail__section {
  margin: 20px 0 10px;
  font-size: 14px;
  font-weight: 600;
}

.container-detail__mono {
  font-family: Menlo, Consolas, 'DejaVu Sans Mono', monospace;
  word-break: break-all;
}

.container-detail__port {
  margin: 0 6px 6px 0;
}

.container-detail__terminal {
  height: calc(100vh - 120px);
}
</style>
