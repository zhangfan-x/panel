<script setup lang="ts">
// 服务管理：systemd 单元列表（状态/名称过滤）、生命周期操作与 journald 日志（快照 + 实时跟随）。
import { computed, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Search } from '@element-plus/icons-vue'
import { useQuery } from '@tanstack/vue-query'

import { problemMessage } from '@/api/problem'
import {
  formatJournalLine,
  getService,
  getServiceJournal,
  listServices,
  runServiceAction,
  serviceJournalStreamUrl,
} from '@/api/services'
import type { ServiceDetail, ServiceUnit, ServiceVerb } from '@/api/services'
import LogViewer from '@/components/LogViewer.vue'

type TagType = 'primary' | 'success' | 'warning' | 'danger' | 'info'

/** 各生命周期动作的中文文案。 */
const VERB_TEXT: Record<ServiceVerb, string> = {
  start: '启动',
  stop: '停止',
  restart: '重启',
  reload: '重载',
  enable: '启用',
  disable: '禁用',
}

/** 抽屉里的动作按钮（顺序即展示顺序）。 */
const VERB_BUTTONS: { verb: ServiceVerb; type: TagType }[] = [
  { verb: 'start', type: 'success' },
  { verb: 'stop', type: 'danger' },
  { verb: 'restart', type: 'warning' },
  { verb: 'reload', type: 'primary' },
  { verb: 'enable', type: 'success' },
  { verb: 'disable', type: 'info' },
]

/** 需要二次确认的动作（可能中断正在提供的服务）。 */
const CONFIRM_VERBS: ReadonlySet<ServiceVerb> = new Set(['stop', 'restart'])

// —— 单元列表与过滤 ——

const { data, isPending, isError, error, refetch } = useQuery({
  queryKey: ['services'],
  queryFn: listServices,
})

const errorMessage = computed(() => problemMessage(error.value, '获取服务列表失败'))
const units = computed(() => data.value?.units ?? [])

const stateFilter = ref('')
const keyword = ref('')

/** 数据里实际出现过的运行状态（动态生成过滤项）。 */
const stateOptions = computed(() => {
  const states = new Set<string>()
  for (const unit of units.value) {
    if (unit.active) {
      states.add(unit.active)
    }
  }
  return [...states].sort()
})

const filteredUnits = computed(() => {
  const needle = keyword.value.trim().toLowerCase()
  return units.value.filter((unit) => {
    if (stateFilter.value && unit.active !== stateFilter.value) {
      return false
    }
    return needle === '' || unit.name.toLowerCase().includes(needle)
  })
})

/** 运行状态 → 标签颜色。 */
function activeTagType(active: string): TagType {
  if (active === 'active') {
    return 'success'
  }
  if (active === 'failed') {
    return 'danger'
  }
  if (active === 'activating' || active === 'deactivating' || active === 'reloading') {
    return 'warning'
  }
  return 'info'
}

/** 运行状态展示文本（active + sub）。 */
function activeText(unit: { active: string; sub: string }): string {
  if (!unit.active) {
    return '未知'
  }
  return unit.sub ? `${unit.active} (${unit.sub})` : unit.active
}

/** unit-file 安装状态 → 标签颜色。 */
function enabledTagType(enabled: string): TagType {
  if (enabled === 'enabled' || enabled === 'enabled-runtime') {
    return 'success'
  }
  if (enabled === 'masked' || enabled === 'masked-runtime') {
    return 'danger'
  }
  return 'info'
}

/** 字节数 → 人读单位（内存占用展示；未知显示「—」）。 */
function formatMemory(bytes: number | null): string {
  if (bytes === null || !Number.isFinite(bytes) || bytes < 0) {
    return '—'
  }
  if (bytes < 1024) {
    return `${bytes} B`
  }
  let value = bytes
  let unit = 'B'
  for (const name of ['KiB', 'MiB', 'GiB', 'TiB']) {
    value /= 1024
    unit = name
    if (value < 1024) {
      break
    }
  }
  return `${value.toFixed(1)} ${unit}`
}

// —— 详情抽屉 ——

const drawerVisible = ref(false)
const currentUnit = ref('')
const detail = ref<ServiceDetail | null>(null)
const detailLoading = ref(false)
const verbBusy = ref<ServiceVerb | null>(null)

function handleRowClick(row: ServiceUnit): void {
  void openDetail(row.name)
}

async function openDetail(unit: string): Promise<void> {
  currentUnit.value = unit
  detail.value = null
  follow.value = false
  journalLines.value = []
  drawerVisible.value = true
  await Promise.all([loadDetail(), loadJournal()])
}

async function loadDetail(): Promise<void> {
  const unit = currentUnit.value
  if (!unit) {
    return
  }
  detailLoading.value = true
  try {
    const res = await getService(unit)
    if (unit === currentUnit.value) {
      detail.value = res
    }
  } catch (err) {
    // 抽屉已切换到其他单元时丢弃过期请求的失败，避免误关新抽屉。
    if (unit === currentUnit.value) {
      ElMessage.error(problemMessage(err, '获取服务详情失败'))
      drawerVisible.value = false
    }
  } finally {
    if (unit === currentUnit.value) {
      detailLoading.value = false
    }
  }
}

/** 执行生命周期动作；stop/restart 先弹确认框。 */
async function handleVerb(verb: ServiceVerb): Promise<void> {
  const unit = currentUnit.value
  if (!unit) {
    return
  }
  if (CONFIRM_VERBS.has(verb)) {
    const confirmed = await ElMessageBox.confirm(
      `确定要${VERB_TEXT[verb]}服务 ${unit} 吗？该操作可能中断其正在提供的功能。`,
      `${VERB_TEXT[verb]}确认`,
      { type: 'warning', confirmButtonText: `确认${VERB_TEXT[verb]}`, cancelButtonText: '取消' },
    ).then(
      () => true,
      () => false,
    )
    if (!confirmed) {
      return
    }
  }
  verbBusy.value = verb
  try {
    await runServiceAction(unit, verb)
    ElMessage.success(`已${VERB_TEXT[verb]}服务 ${unit}`)
    await Promise.all([loadDetail(), refetch()])
  } catch (err) {
    ElMessage.error(problemMessage(err, `${VERB_TEXT[verb]}服务失败`))
  } finally {
    verbBusy.value = null
  }
}

// —— journald 日志（快照 + 实时跟随） ——

const follow = ref(false)
const journalLines = ref<string[]>([])
const journalLoading = ref(false)

/** 实时跟随的 SSE 地址（未开启时为 null，LogViewer 不建流）。 */
const journalStreamUrl = computed(() =>
  follow.value && currentUnit.value ? serviceJournalStreamUrl(currentUnit.value) : null,
)

async function loadJournal(): Promise<void> {
  const unit = currentUnit.value
  if (!unit) {
    return
  }
  journalLoading.value = true
  try {
    const res = await getServiceJournal(unit, 200)
    if (unit === currentUnit.value) {
      journalLines.value = res.lines
        .map((line) => formatJournalLine(line))
        .filter((line): line is string => line !== null)
    }
  } catch (err) {
    if (unit === currentUnit.value) {
      ElMessage.error(problemMessage(err, '获取服务日志失败'))
    }
  } finally {
    if (unit === currentUnit.value) {
      journalLoading.value = false
    }
  }
}

// 关闭实时跟随时刷新一次快照，避免停留在打开跟随前的旧内容。
watch(follow, (value) => {
  if (!value && drawerVisible.value) {
    void loadJournal()
  }
})
</script>

<template>
  <div class="services-page">
    <el-card shadow="never">
      <template #header>
        <div class="services-page__header">
          <span class="services-page__title">系统服务</span>
          <div class="services-page__header-right">
            <span class="services-page__count">
              共 {{ filteredUnits.length }} / {{ units.length }} 个单元
            </span>
            <el-button link type="primary" :loading="isPending" @click="() => refetch()">
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
      <template v-else>
        <div class="services-page__toolbar">
          <el-radio-group v-model="stateFilter">
            <el-radio-button value="">全部</el-radio-button>
            <el-radio-button v-for="state in stateOptions" :key="state" :value="state">
              {{ state }}
            </el-radio-button>
          </el-radio-group>
          <el-input
            v-model="keyword"
            class="services-page__search"
            placeholder="按名称过滤"
            clearable
            :prefix-icon="Search"
          />
        </div>
        <el-table :data="filteredUnits" @row-click="handleRowClick">
          <template #empty>
            <el-empty description="没有匹配的服务单元" />
          </template>
          <el-table-column
            prop="name"
            label="单元名称"
            min-width="220"
            sortable
            show-overflow-tooltip
          />
          <el-table-column prop="description" label="描述" min-width="260" show-overflow-tooltip />
          <el-table-column label="运行状态" width="170">
            <template #default="{ row }">
              <el-tag size="small" :type="activeTagType(row.active)" effect="plain">
                {{ activeText(row) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="开机自启" width="130">
            <template #default="{ row }">
              <el-tag
                v-if="row.enabled"
                size="small"
                :type="enabledTagType(row.enabled)"
                effect="plain"
              >
                {{ row.enabled }}
              </el-tag>
              <span v-else class="services-page__muted">—</span>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="80" fixed="right">
            <template #default="{ row }">
              <el-button link type="primary" @click.stop="openDetail(row.name)">详情</el-button>
            </template>
          </el-table-column>
        </el-table>
      </template>
    </el-card>

    <el-drawer
      v-model="drawerVisible"
      :title="`服务详情：${currentUnit}`"
      size="720px"
      destroy-on-close
    >
      <el-skeleton v-if="detailLoading && !detail" :rows="6" animated />
      <template v-else-if="detail">
        <el-descriptions :column="2" border class="services-page__section">
          <el-descriptions-item label="单元名称" :span="2">{{ detail.name }}</el-descriptions-item>
          <el-descriptions-item label="描述" :span="2">
            {{ detail.description || '—' }}
          </el-descriptions-item>
          <el-descriptions-item label="运行状态">
            <el-tag size="small" :type="activeTagType(detail.active)" effect="plain">
              {{ activeText(detail) }}
            </el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="开机自启">{{ detail.enabled || '—' }}</el-descriptions-item>
          <el-descriptions-item label="加载状态">{{ detail.load || '—' }}</el-descriptions-item>
          <el-descriptions-item label="主进程 PID">
            {{ detail.main_pid > 0 ? detail.main_pid : '—' }}
          </el-descriptions-item>
          <el-descriptions-item label="内存占用">
            {{ formatMemory(detail.memory) }}
          </el-descriptions-item>
          <el-descriptions-item label="活跃起始">{{ detail.since || '—' }}</el-descriptions-item>
          <el-descriptions-item label="单元文件" :span="2">
            {{ detail.fragment_path || '—' }}
          </el-descriptions-item>
        </el-descriptions>

        <div class="services-page__actions">
          <el-button
            v-for="button in VERB_BUTTONS"
            :key="button.verb"
            :type="button.type"
            plain
            :loading="verbBusy === button.verb"
            :disabled="verbBusy !== null && verbBusy !== button.verb"
            @click="handleVerb(button.verb)"
          >
            {{ VERB_TEXT[button.verb] }}
          </el-button>
        </div>

        <div class="services-page__journal-header">
          <span class="services-page__label">journald 日志</span>
          <div class="services-page__journal-tools">
            <el-switch v-model="follow" size="small" active-text="实时跟随" />
            <el-button
              v-if="!follow"
              link
              type="primary"
              :loading="journalLoading"
              @click="loadJournal"
            >
              刷新
            </el-button>
          </div>
        </div>
        <LogViewer
          :lines="follow ? null : journalLines"
          :source="journalStreamUrl"
          :source-topics="['journal']"
          :format="formatJournalLine"
          height="320px"
        />
      </template>
    </el-drawer>
  </div>
</template>

<style scoped>
.services-page__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.services-page__title {
  font-weight: 600;
}

.services-page__header-right {
  display: flex;
  align-items: center;
  gap: 12px;
}

.services-page__count {
  color: var(--el-text-color-secondary);
  font-size: 13px;
}

.services-page__toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 12px;
  margin-bottom: 16px;
}

.services-page__search {
  width: 220px;
}

.services-page :deep(.el-table__row) {
  cursor: pointer;
}

.services-page__muted {
  color: var(--el-text-color-secondary);
}

.services-page__section {
  margin-bottom: 16px;
}

.services-page__actions {
  margin-bottom: 16px;
}

.services-page__journal-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
}

.services-page__label {
  color: var(--el-text-color-secondary);
  font-size: 13px;
}

.services-page__journal-tools {
  display: flex;
  align-items: center;
  gap: 12px;
}
</style>
